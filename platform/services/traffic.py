"""Node traffic: what the gateway served and what the network card moved.

RFC-0051 stage 3. The sampler (`metrics.take_sample`) asks this module
once a minute and files what comes back next to CPU, memory and disk.

Five more series, each a plain number per minute:

    req    gateway requests per minute (finished requests; a WebSocket is
           counted when it closes)
    lat    mean duration of a request, in milliseconds. Streams that are
           switched to another protocol (status 101) are NOT part of it:
           a display that stays connected for five minutes is not a slow
           page. A minute without a request has no `lat` -- a gap, not 0.
    tx/rx  bytes per second through the node's real network cards (not
           loopback, not docker bridges, not WireGuard: those count the
           same bytes twice)
    conn   TCP sockets in use
    ctop   the busiest container, in percent of ONE core (so 100 is a
           full core and the value can go beyond it)

Plus a snapshot the health page reads (`traffic.json`): the last hour per
host (requests, bytes, mean and longest duration, 5xx) and the busiest
containers right now.

The source is the gateway's own access log (JSON, one line per request).
It is read from where the last run stopped, never from the start: a log
of 100 MB is not read every minute. Everything in it is DATA from the
internet (the Host header is the sender's), so a host name is cut and
counted but never trusted, and the number of distinct hosts kept per
minute is bounded.

Like the rest of the sampler this never raises into the caller: a source
that cannot be read leaves its series out of that minute.
"""
import json
import math
import os
import shutil
import subprocess

STATE_FILE = "traffic-state.json"
SNAPSHOT_FILE = "traffic.json"
SNAPSHOT_MINUTES = 60
READ_LIMIT = 16 * 1024 * 1024      # bytes of log taken in one run
MAX_GAP = 300                      # seconds; a longer gap is no minute
MAX_HOSTS = 50                     # distinct hosts kept per minute
HOST_LEN = 80
OTHER = "(weitere)"
CONTAINERS_KEPT = 6
DOCKER_TIMEOUT = 20

# interface names that carry nobody's traffic of their own
_SKIP_IFACE = ("lo", "docker", "br-", "veth", "virbr", "wg", "tun", "tap")


def _load(directory, name):
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(directory, name, d):
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(directory, name))


# --- the operating system ----------------------------------------------

def read_nic_bytes(proc="/proc"):
    """(received, sent) bytes summed over the real interfaces, or None."""
    rx = tx = 0
    seen = False
    try:
        with open(os.path.join(proc, "net", "dev"), encoding="utf-8") as f:
            lines = f.readlines()[2:]
    except OSError:
        return None
    for line in lines:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not name or name.startswith(_SKIP_IFACE):
            continue
        f = rest.split()
        if len(f) < 9:
            continue
        try:
            rx += int(f[0])
            tx += int(f[8])
        except ValueError:
            continue
        seen = True
    return (rx, tx) if seen else None


def read_tcp_inuse(proc="/proc"):
    try:
        with open(os.path.join(proc, "net", "sockstat"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("TCP:"):
                    p = line.split()
                    return int(p[p.index("inuse") + 1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def docker_stats():
    """[(name, cpu percent of one core, memory MB)] from `docker stats`,
    or None when it cannot be asked. This is the one slow reading (about
    two seconds): it is bounded and a failure only costs the minute."""
    exe = shutil.which("docker")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return parse_docker_stats(r.stdout)


def parse_docker_stats(text):
    out = []
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 3:
            continue
        try:
            cpu = float(p[1].strip().rstrip("%"))
        except ValueError:
            continue
        if not math.isfinite(cpu):          # float("nan") parses happily
            continue
        out.append((p[0].strip()[:80], cpu, _mem_mb(p[2])))
    return out


def _mem_mb(text):
    """'437.6MiB / 31.3GiB' -> 437.6 (the used part, in MB)."""
    used = text.split("/")[0].strip()
    for unit, factor in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024.0),
                         ("GB", 1000.0), ("MB", 1.0), ("kB", 0.001),
                         ("B", 1 / 1048576.0)):
        if used.endswith(unit):
            try:
                return round(float(used[:-len(unit)]) * factor, 1)
            except ValueError:
                return 0.0
    return 0.0


# --- the access log -----------------------------------------------------

def _read_new(path, state):
    """(lines, new position state) -- the complete lines added since the
    last run. No previous position (first run), a new file (rotation) and
    a long gap each have a rule:

      first run   start at the END: the past is not read, and a number
                  worked out of an unknown period would be a guess
      rotated     the file is a different one or got shorter: start of it
      too much    more than READ_LIMIT waiting: skip the oldest
    """
    try:
        st = os.stat(path)
    except OSError:
        return None, state
    ino = st.st_ino
    off = state.get("off")
    if not isinstance(off, int):
        return [], {"ino": ino, "off": st.st_size}
    if state.get("ino") != ino or st.st_size < off:
        off = 0
    if st.st_size - off > READ_LIMIT:
        off = st.st_size - READ_LIMIT
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(st.st_size - off)
    except OSError:
        return None, state
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], {"ino": ino, "off": off}
    lines = data[:cut].decode("utf-8", errors="replace").split("\n")
    return lines, {"ino": ino, "off": off + cut + 1}


def _host(d):
    r = d.get("request")
    h = r.get("host") if isinstance(r, dict) else None
    h = h if isinstance(h, str) and h else "?"
    return h[:HOST_LEN]


def tally(lines):
    """{host: [n, n101, bytes, duration sum (without 101), longest
    (without 101), 5xx]} over the log lines. A line that is not a request
    is skipped: the log is written by a program, but it is read as data."""
    hosts = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        status, size, dur = d.get("status"), d.get("size"), d.get("duration")
        if not isinstance(status, int) or isinstance(status, bool):
            continue
        size = size if isinstance(size, (int, float)) and size >= 0 else 0
        dur = dur if isinstance(dur, (int, float)) and dur >= 0 else 0.0
        h = _host(d)
        if h not in hosts and len(hosts) >= MAX_HOSTS:
            h = OTHER
        row = hosts.setdefault(h, [0, 0, 0, 0.0, 0.0, 0])
        row[0] += 1
        row[2] += int(size)
        if status == 101:
            row[1] += 1
        else:
            row[3] += dur
            row[4] = max(row[4], dur)
        if status >= 500:
            row[5] += 1
    return hosts


def merge(into, hosts):
    for h, r in hosts.items():
        if h not in into and len(into) >= MAX_HOSTS:
            h = OTHER
        row = into.setdefault(h, [0, 0, 0, 0.0, 0.0, 0])
        row[0] += r[0]
        row[1] += r[1]
        row[2] += r[2]
        row[3] += r[3]
        row[4] = max(row[4], r[4])
        row[5] += r[5]
    return into


# --- the run ------------------------------------------------------------

def measure(directory, now, proc="/proc", log_path=None, containers=None):
    """Read the traffic sources once. Returns {series: value} for what was
    measurable and writes the state and the snapshot. `containers` is a
    function returning docker_stats()'s list (None leaves it out)."""
    out = {}
    if not (log_path or containers or os.path.isdir(os.path.join(proc, "net"))):
        return out                  # nothing to read: write nothing either
    state = _load(directory, STATE_FILE)
    at = state.get("at")
    elapsed = now - at if isinstance(at, (int, float)) else None
    fresh = elapsed is not None and 0 < elapsed <= MAX_GAP

    nic = read_nic_bytes(proc)
    old = state.get("nic")
    if nic and fresh and isinstance(old, list) and len(old) == 2 \
            and nic[0] >= old[0] and nic[1] >= old[1]:
        out["rx"] = round((nic[0] - old[0]) / elapsed, 1)
        out["tx"] = round((nic[1] - old[1]) / elapsed, 1)
    tcp = read_tcp_inuse(proc)
    if tcp is not None:
        out["conn"] = tcp

    snap = _load(directory, SNAPSHOT_FILE)
    minutes = [m for m in snap.get("minutes", [])
               if isinstance(m, dict) and isinstance(m.get("t"), int)
               and m["t"] > now - SNAPSHOT_MINUTES * 60]
    new_pos = {k: state[k] for k in ("ino", "off") if k in state}
    if log_path:
        lines, new_pos = _read_new(log_path, new_pos)
        if lines is not None and fresh:
            hosts = tally(lines)
            n = sum(r[0] for r in hosts.values())
            out["req"] = round(n * 60.0 / elapsed, 1)
            timed = sum(r[0] - r[1] for r in hosts.values())
            if timed:
                dsum = sum(r[3] for r in hosts.values())
                out["lat"] = round(dsum / timed * 1000.0, 1)
            if hosts:
                minutes.append({"t": now - now % 60,
                                "h": {k: [r[0], r[1], r[2], round(r[3], 3),
                                          round(r[4], 3), r[5]]
                                      for k, r in hosts.items()}})
    snap_out = {"at": now, "minutes": minutes}
    if containers:
        try:
            stats = containers()
        except Exception:                                  # noqa: BLE001
            stats = None
        if stats:
            top = sorted(stats, key=lambda c: -c[1])
            out["ctop"] = round(top[0][1], 1)
            snap_out["containers"] = [[c[0], round(c[1], 1), c[2]]
                                      for c in top[:CONTAINERS_KEPT]]
    _save(directory, SNAPSHOT_FILE, snap_out)
    new_state = dict(new_pos, at=now)
    if nic:
        new_state["nic"] = list(nic)
    _save(directory, STATE_FILE, new_state)
    return out


def snapshot(directory):
    """What the health page shows: {"hosts": {host: row over the hour},
    "containers": [...], "at": t}. Reading only."""
    snap = _load(directory, SNAPSHOT_FILE)
    hosts = {}
    for m in snap.get("minutes", []):
        h = m.get("h") if isinstance(m, dict) else None
        if isinstance(h, dict):
            merge(hosts, {k: v for k, v in h.items()
                          if isinstance(v, list) and len(v) == 6})
    return {"at": snap.get("at"), "hosts": hosts,
            "containers": snap.get("containers") or []}
