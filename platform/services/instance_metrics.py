"""A history per instance (RFC-0108, stage 1).

Once a minute, in the same run as the node sample (`metrics.take_sample`),
one line per instance: CPU, memory and network of its containers, the
requests, errors and mean duration over its addresses, and every
`DISK_EVERY` seconds the size of its data directory. The store is
RFC-0051's -- the same JSONL tiers, rolled up and trimmed by the same
functions -- one directory per instance under its tenant:

    <metrics>/tenants/<tenant-id>/<instance-key>/raw.jsonl, 5m, 30m, 2h

These are TENANT data (RFC-0108 §4): they live under the tenant, are shown
in that tenant's context only, and go away with the instance (the minute
series, D4) or the tenant.

Nothing here reads the host by itself except `du`: the containers and the
access log were already read by `traffic.measure` for the node, and are
handed over (`keep`). Like the rest of the sampler nothing raises into the
caller; an instance whose numbers cannot be had gets a gap, never a 0 --
except a SLEEPING instance, which is measured as 0 because it is asleep.
"""
import json
import os
import shutil
import subprocess

import metrics

SERIES = ("cpu", "mem", "rx", "tx", "req", "err", "lat", "disk")
# cpu: % of one core, summed over the containers; mem: MB; rx/tx: bytes/s;
# req, err: per minute; lat: ms (without 101 streams); disk: MB
UNITS = {"cpu": "% Kern", "mem": "MB", "rx": "B/s", "tx": "B/s",
         "req": "/min", "err": "/min", "lat": "ms", "disk": "MB"}
ROOT = "tenants"
LAST = "last.json"
DISK_EVERY = 15 * 60          # RFC-0108 D2
DU_TIMEOUT = 60
MAX_GAP = 300


def instance_dir(base, tenant, key):
    return os.path.join(base, ROOT, tenant, key)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, path)


def du_mb(path, timeout=DU_TIMEOUT):
    """The size of a directory in MB (`du -sk`), or None."""
    exe = shutil.which("du")
    if not exe or not path or not os.path.isdir(path):
        return None
    try:
        r = subprocess.run([exe, "-sk", path], capture_output=True, text=True,
                           timeout=timeout)
        return round(int(r.stdout.split()[0]) / 1024.0, 1)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def line_for(inst, now, keep, last, du=du_mb):
    """One instance's entry for this minute and its new state.

    inst: {"tenant", "containers": [names], "hosts": [names], "dir": path,
           "asleep": bool}
    keep: what traffic.measure read: {"containers": [(name, cpu, mem[, rx, tx])],
          "hosts": {host: [n, n101, bytes, dursum, durmax, 5xx]}, "elapsed": s}
    last: this instance's previous state (network counters, last du)
    """
    t = now - now % 60
    entry = {"t": t}
    state = {"at": now}
    stats = {c[0]: c for c in keep.get("containers") or []}
    mine = [stats[n] for n in inst.get("containers") or [] if n in stats]
    if mine:
        entry["cpu"] = round(sum(c[1] for c in mine), 2)
        entry["mem"] = round(sum(c[2] for c in mine), 1)
        if all(len(c) >= 5 and c[3] is not None and c[4] is not None for c in mine):
            rx, tx = sum(c[3] for c in mine), sum(c[4] for c in mine)
            state["net"] = [rx, tx]
            old, at = last.get("net"), last.get("at")
            gap = now - at if isinstance(at, (int, float)) else None
            # a counter that went down is a restarted container: no minute
            if (isinstance(old, list) and len(old) == 2 and gap and 0 < gap <= MAX_GAP
                    and rx >= old[0] and tx >= old[1]):
                entry["rx"] = round((rx - old[0]) / gap, 1)
                entry["tx"] = round((tx - old[1]) / gap, 1)
    elif inst.get("asleep"):
        entry["cpu"], entry["mem"] = 0.0, 0.0
    hosts, elapsed = keep.get("hosts"), keep.get("elapsed")
    if isinstance(hosts, dict) and elapsed:
        rows = [hosts[h] for h in inst.get("hosts") or [] if h in hosts]
        n = sum(r[0] for r in rows)
        entry["req"] = round(n * 60.0 / elapsed, 1)
        entry["err"] = round(sum(r[5] for r in rows) * 60.0 / elapsed, 1)
        timed = sum(r[0] - r[1] for r in rows)
        if timed:
            entry["lat"] = round(sum(r[3] for r in rows) / timed * 1000.0, 1)
    disk, disk_at = last.get("disk"), last.get("disk_at")
    if not isinstance(disk_at, (int, float)) or now - disk_at >= DISK_EVERY:
        measured = du(inst.get("dir"))
        if measured is not None:
            disk, disk_at = measured, now
    if isinstance(disk, (int, float)):
        entry["disk"] = disk
        state["disk"], state["disk_at"] = disk, disk_at
    return (entry if len(entry) > 1 else None), state


def sample(base, now, keep, instances, du=du_mb, roll=False, trim=False):
    """File this minute for every instance; roll up and trim when the node
    sample does. Instances that are gone lose their directory (D4)."""
    t = now - now % 60
    wanted = set()
    for key, inst in sorted((instances or {}).items()):
        tenant = inst.get("tenant") or ""
        if not tenant or "/" in tenant or "/" in key or key.startswith("."):
            continue
        d = instance_dir(base, tenant, key)
        wanted.add((tenant, key))
        os.makedirs(d, exist_ok=True)
        last = _load(os.path.join(d, LAST))
        if last.get("t", -1) >= t:
            continue
        try:
            entry, state = line_for(inst, now, keep or {}, last, du)
        except Exception:                                  # noqa: BLE001
            continue
        if entry:
            metrics._append(d, "raw", [entry])
        state["t"] = t
        _save(os.path.join(d, LAST), state)
        if roll:
            metrics.rollup(d, now, SERIES)
        if trim:
            metrics.trim(d, now)
    if trim:
        sweep(base, wanted)


def sweep(base, wanted):
    """Remove the series of instances (and tenants) that no longer exist.
    Only directories this module made, under <metrics>/tenants/."""
    root = os.path.join(base, ROOT)
    try:
        tenants = os.listdir(root)
    except OSError:
        return
    for tenant in tenants:
        tdir = os.path.join(root, tenant)
        if not os.path.isdir(tdir):
            continue
        for key in os.listdir(tdir):
            if (tenant, key) not in wanted and os.path.isfile(os.path.join(tdir, key, LAST)):
                shutil.rmtree(os.path.join(tdir, key), ignore_errors=True)
        if not os.listdir(tdir):
            os.rmdir(tdir)


# --- reading (the portal) -----------------------------------------------

def window(base, tenant, key, wkey, now=None):
    return metrics.window(instance_dir(base, tenant, key), wkey, now=now, series=SERIES)


def latest(base, tenant, key):
    """The last raw line of an instance, or {}."""
    rows = metrics._read(instance_dir(base, tenant, key), "raw")
    return rows[-1] if rows else {}


def disk_bytes(base, tenant):
    """{instance key: bytes} from each instance's last du -- what the report
    ledger keeps per day (RFC-0108 D3). No du of its own."""
    out = {}
    root = os.path.join(base, ROOT, tenant)
    try:
        keys = os.listdir(root)
    except OSError:
        return out
    for key in keys:
        d = _load(os.path.join(root, key, LAST)).get("disk")
        if isinstance(d, (int, float)):
            out[key] = int(d * 1024 * 1024)
    return out
