"""Node metrics: one sample a minute, a tiered local store, window queries.

RFC-0051 stage 1. Pure in the sense that matters here: every function
takes the directory it works in and the time it works at, so a test can
run a month of minutes in seconds. The acting (the systemd job, the
command) is `cmd_metrics` in appctl.py.

Three series -- `cpu`, `mem`, `disk`, each in percent -- are read ON THE
HOST once a minute. They are kept in four tiers, each rolled up from the
one above it:

    raw   1 minute    4 hours
    5m    5 minutes   24 hours
    30m   30 minutes  7 days
    2h    2 hours     31 days

A tier stores mean, minimum and maximum per interval. The maximum is the
point: a one-minute spike must not vanish from the monthly picture
because it was averaged away.

Plain files, appended to. A node may keep its data on an SD card, and a
store rewritten every minute would wear it out for nothing -- so a
sample is ONE appended line, a tier is appended when an interval closes,
and a file is rewritten only when it has outgrown its retention by a
quarter. A gap (the node was off) is a gap: nothing is invented for it.
"""
import json
import os
import time

SERIES = ("cpu", "mem", "disk")

# (name, seconds per interval, how long it is kept)
TIERS = (
    ("raw", 60, 4 * 3600),
    ("5m", 300, 24 * 3600),
    ("30m", 1800, 7 * 86400),
    ("2h", 7200, 31 * 86400),
)

# window key -> (tier it is drawn from, span in seconds)
WINDOWS = {
    "4h": ("raw", 4 * 3600),
    "24h": ("5m", 24 * 3600),
    "1w": ("30m", 7 * 86400),
    "1m": ("2h", 31 * 86400),
}

# A tier file is rewritten only when its oldest line is this much older
# than the retention. Queries cut by time anyway, so the slack is free.
TRIM_SLACK = 1.25
ROLLUP_EVERY = TIERS[1][1]      # seconds: the first rolled-up tier's interval
TRIM_EVERY = 3600

FORMAT_VERSION = 1

# The outbound queue (RFC-0051 5). Bounded by age and by size: an unreachable
# broker loses the oldest data, never the node's health.
QUEUE_FILE = "outbox.jsonl"
# What the sender (RFC-0052) leaves in this directory for the health page:
# the run state, and a non-secret note that a sender is configured at all
# (its real configuration and its secret live elsewhere).
SENDER_STATE_FILE = "sender-state.json"
SENDER_INFO_FILE = "sender-info.json"
QUEUE_STATE = "outbox.json"
QUEUE_MAX_AGE = 7 * 86400
QUEUE_MAX_BYTES = 2_000_000


def _tier(name):
    for t in TIERS:
        if t[0] == name:
            return t
    raise KeyError(name)


def _path(directory, tier):
    return os.path.join(directory, tier + ".jsonl")


def _read(directory, tier):
    """The entries of a tier, oldest first. A damaged line is skipped:
    this is a record, and one bad byte must not make it unreadable."""
    out = []
    try:
        with open(_path(directory, tier), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("t"), int):
                    out.append(e)
    except OSError:
        return []
    out.sort(key=lambda e: e["t"])
    return out


def _append(directory, tier, entries):
    if not entries:
        return
    os.makedirs(directory, exist_ok=True)
    with open(_path(directory, tier), "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def _cell(entry, series):
    """(n, mean, min, max) of one series in one entry, or None.

    A raw entry holds a bare value; a rolled-up entry holds the four.
    """
    v = entry.get(series)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (1, float(v), float(v), float(v))
    if isinstance(v, list) and len(v) == 4:
        return tuple(v)
    return None


def _aggregate(entries, series):
    cells = [c for c in (_cell(e, series) for e in entries) if c]
    n = sum(c[0] for c in cells)
    if not n:
        return None
    mean = sum(c[0] * c[1] for c in cells) / n
    return [n, round(mean, 2), round(min(c[2] for c in cells), 2),
            round(max(c[3] for c in cells), 2)]


def rollup(directory, now):
    """Close every interval that has ended, tier by tier.

    Idempotent and catching up: it looks at what each tier already holds
    and writes only the closed intervals after that, so a missed run
    (the node was off, the job failed) costs nothing the next one cannot
    repair. Tiers go in order, because each is filled from the one above.
    """
    for i in range(1, len(TIERS)):
        name, step, _keep = TIERS[i]
        src = TIERS[i - 1][0]
        have = _read(directory, name)
        last = have[-1]["t"] if have else -1
        buckets = {}
        for e in _read(directory, src):
            b = e["t"] - e["t"] % step
            if b > last and b + step <= now:
                buckets.setdefault(b, []).append(e)
        out = []
        for b in sorted(buckets):
            entry = {"t": b}
            for s in SERIES:
                agg = _aggregate(buckets[b], s)
                if agg:
                    entry[s] = agg
            if len(entry) > 1:
                out.append(entry)
        _append(directory, name, out)


def trim(directory, now):
    """Drop what has aged out -- rarely, and only by rewriting."""
    for name, _step, keep in TIERS:
        entries = _read(directory, name)
        if not entries or entries[0]["t"] >= now - keep * TRIM_SLACK:
            continue
        kept = [e for e in entries if e["t"] >= now - keep]
        tmp = _path(directory, name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in kept:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        os.replace(tmp, _path(directory, name))


# --- reading the host ---------------------------------------------------

def read_cpu_times(proc="/proc"):
    """(total, idle) jiffies from the first line of /proc/stat, or None."""
    try:
        with open(os.path.join(proc, "stat"), encoding="utf-8") as f:
            parts = f.readline().split()
    except OSError:
        return None
    if not parts or parts[0] != "cpu":
        return None
    try:
        v = [int(x) for x in parts[1:9]]
    except ValueError:
        return None
    if len(v) < 5:
        return None
    return (sum(v), v[3] + v[4])


def cpu_percent(prev, cur):
    """Utilisation between two readings, 0-100, or None."""
    if not prev or not cur:
        return None
    dt = cur[0] - prev[0]
    if dt <= 0 or cur[1] < prev[1]:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - (cur[1] - prev[1]) / dt)))


def read_mem_percent(proc="/proc"):
    """Memory in use, in percent of the total -- what is NOT available."""
    try:
        mem = {}
        with open(os.path.join(proc, "meminfo"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = int(v.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable")
    if not total or avail is None:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - avail / total)))


def read_disk_percent(path):
    """Used space of the filesystem holding `path`, as `df` counts it."""
    try:
        vs = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    used = vs.f_blocks - vs.f_bfree
    denom = used + vs.f_bavail
    if denom <= 0:
        return None
    return 100.0 * used / denom


def _load_last(directory):
    try:
        with open(os.path.join(directory, "last.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_last(directory, d):
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, "last.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, os.path.join(directory, "last.json"))


def take_sample(directory, data_path, now=None, proc="/proc",
                sleep=time.sleep):
    """Read the host once and file the sample. Returns the entry, or None
    when this minute already has one (the job may fire twice).

    CPU is the difference to the previous run's reading -- the mean over
    the whole minute, not a one-second snapshot. With no usable previous
    reading (first run, a reboot, a long gap) it reads twice a second
    apart instead, so the first sample is honest too.
    """
    now = int(time.time() if now is None else now)
    t = now - now % 60
    last = _load_last(directory)
    if last.get("t", -1) >= t:
        return None
    cur = read_cpu_times(proc)
    prev = last.get("cpu") if isinstance(last.get("cpu"), list) else None
    stamp = last.get("at", 0)
    cpu = None
    if prev and now - 300 <= stamp <= now:
        cpu = cpu_percent(tuple(prev), cur)
    if cpu is None and cur is not None:
        sleep(1)
        again = read_cpu_times(proc)
        cpu = cpu_percent(cur, again)
        cur = again or cur
    entry = {"t": t}
    for s, v in (("cpu", cpu), ("mem", read_mem_percent(proc)),
                 ("disk", read_disk_percent(data_path))):
        if v is not None:
            entry[s] = round(v, 2)
    if len(entry) > 1:
        _append(directory, "raw", [entry])
        queue_add(directory, entry)
    # The per-minute work is one appended line and one tiny state file.
    # Rolling up reads whole tier files, so it waits until an interval of
    # the first rolled tier has actually closed; trimming rewrites, so it
    # runs about once an hour. Both are idempotent catch-ups, so a gate
    # that skips a run only delays it.
    if now // ROLLUP_EVERY != last.get("rolled", -1):
        rollup(directory, now)
        last["rolled"] = now // ROLLUP_EVERY
    if now // TRIM_EVERY != last.get("trimmed", -1):
        trim(directory, now)
        queue_trim(directory, now)
        last["trimmed"] = now // TRIM_EVERY
    last.update({"cpu": list(cur) if cur else None, "at": now, "t": t})
    _save_last(directory, last)
    return entry


# --- reading the store --------------------------------------------------

def _entries(directory, idx, now):
    """A tier's entries INCLUDING what has not been rolled up into it yet.

    The files only hold closed intervals, so a node updated an hour ago
    would show an empty day, week and month although it has an hour of
    data. The tail is derived from the tier above on the fly -- computed,
    never written (writing is `rollup`'s job and waits for the interval
    to close), so the open interval's mean is a partial one.
    """
    name, step, _keep = TIERS[idx]
    stored = _read(directory, name)
    if idx == 0:
        return stored
    last = stored[-1]["t"] if stored else -1
    buckets = {}
    for e in _entries(directory, idx - 1, now):
        b = e["t"] - e["t"] % step
        if b > last:
            buckets.setdefault(b, []).append(e)
    derived = []
    for b in sorted(buckets):
        entry = {"t": b}
        for s in SERIES:
            agg = _aggregate(buckets[b], s)
            if agg:
                entry[s] = agg
        if len(entry) > 1:
            derived.append(entry)
    return stored + derived


def window(directory, key, now=None):
    """One window as the charts need it:

        {"window", "tier", "step", "since", "series": {name: [[t, mean,
         min, max], ...]}}

    `since` is the first moment there is data for -- a node updated an
    hour ago must say "seit 14:03", not pretend to a full day.
    """
    tier, span = WINDOWS[key]
    step = _tier(tier)[1]
    now = int(time.time() if now is None else now)
    idx = [t[0] for t in TIERS].index(tier)
    entries = [e for e in _entries(directory, idx, now)
               if e["t"] >= now - span]
    series = {}
    for s in SERIES:
        pts = []
        for e in entries:
            c = _cell(e, s)
            if c:
                pts.append([e["t"], round(c[1], 2), round(c[2], 2),
                            round(c[3], 2)])
        series[s] = pts
    return {"window": key, "tier": tier, "step": step,
            "since": entries[0]["t"] if entries else None, "series": series}


# --- the outbound queue (RFC-0051 5, writing only) ----------------------
#
# One line per sample, each with a running number `q`. The sender that
# comes later reads from its last acknowledged number, publishes, and only
# then calls `queue_ack` -- which is also how delivered data is deleted.
# The state file keeps the counters that a deleted line can no longer
# prove: the next number, the acknowledged one, and what was lost or
# discarded. A loss is counted and shown, never hidden.

def _qstate(directory):
    try:
        with open(os.path.join(directory, QUEUE_STATE), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    return {k: d[k] if isinstance(d.get(k), int) and d[k] >= 0 else 0
            for k in ("next", "acked", "lost", "purged")}


def _qsave(directory, st):
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, QUEUE_STATE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, os.path.join(directory, QUEUE_STATE))


def _qread(directory):
    """The queued entries, oldest first; a damaged line is skipped."""
    out = []
    try:
        with open(os.path.join(directory, QUEUE_FILE), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(e, dict) and isinstance(e.get("q"), int)
                        and isinstance(e.get("t"), int)):
                    out.append(e)
    except OSError:
        return []
    return out


def _qwrite(directory, entries):
    """Replace the queue file by `entries` (an empty list removes it)."""
    path = os.path.join(directory, QUEUE_FILE)
    if not entries:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def queue_add(directory, entry):
    """Append one sample, numbered. Returns its number."""
    st = _qstate(directory)
    if not st["next"]:
        # a lost state file must not restart the numbering under a sender
        # that already acknowledged higher numbers
        tail = _qread(directory)
        st["next"] = (tail[-1]["q"] + 1) if tail else 1
    q = st["next"]
    st["next"] = q + 1
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, QUEUE_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(entry, q=q), separators=(",", ":")) + "\n")
    _qsave(directory, st)
    try:
        too_big = os.path.getsize(os.path.join(directory, QUEUE_FILE)) > QUEUE_MAX_BYTES
    except OSError:
        too_big = False
    if too_big:
        queue_trim(directory, entry["t"])
    return q


def queue_trim(directory, now):
    """Enforce the bounds: age first, then size (down to 80 %, so the next
    sample does not trim again). What is dropped unacknowledged is counted
    in `lost`; what was acknowledged is simply gone."""
    entries = _qread(directory)
    if not entries:
        return 0
    st = _qstate(directory)
    kept = [e for e in entries if e["t"] >= now - QUEUE_MAX_AGE]
    size = sum(len(json.dumps(e, separators=(",", ":"))) + 1 for e in kept)
    while kept and size > QUEUE_MAX_BYTES:
        drop = max(1, len(kept) // 5)
        size -= sum(len(json.dumps(e, separators=(",", ":"))) + 1
                    for e in kept[:drop])
        kept = kept[drop:]
    dropped = len(entries) - len(kept)
    if not dropped:
        return 0
    gone = entries[:dropped]
    st["lost"] += sum(1 for e in gone if e["q"] > st["acked"])
    _qwrite(directory, kept)
    _qsave(directory, st)
    return dropped


def queue_pending(directory, limit=None):
    """What has not been acknowledged yet, oldest first."""
    acked = _qstate(directory)["acked"]
    out = [e for e in _qread(directory) if e["q"] > acked]
    return out[:limit] if limit else out


def queue_ack(directory, upto):
    """The sender has delivered everything up to number `upto`: advance the
    mark and DELETE those lines. Never moves backwards. Returns how many
    lines were deleted."""
    st = _qstate(directory)
    if upto <= st["acked"]:
        return 0
    st["acked"] = min(upto, max(st["next"] - 1, 0))
    entries = _qread(directory)
    kept = [e for e in entries if e["q"] > st["acked"]]
    _qwrite(directory, kept)
    _qsave(directory, st)
    return len(entries) - len(kept)


def queue_purge(directory):
    """Delete everything in the queue, delivered or not. The numbering
    goes on (a sender must never see a number twice); the discarded
    lines are counted as `purged`, not as `lost`. Returns the count."""
    entries = _qread(directory)
    st = _qstate(directory)
    if not st["next"] and entries:
        st["next"] = entries[-1]["q"] + 1
    st["purged"] += sum(1 for e in entries if e["q"] > st["acked"])
    st["acked"] = max(st["next"] - 1, st["acked"])
    _qwrite(directory, [])
    _qsave(directory, st)
    return len(entries)


def queue_status(directory):
    st = _qstate(directory)
    entries = _qread(directory)
    pend = [e for e in entries if e["q"] > st["acked"]]
    path = os.path.join(directory, QUEUE_FILE)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"pending": len(pend), "oldest": pend[0]["t"] if pend else None,
            "bytes": size, "next": st["next"], "acked": st["acked"],
            "lost": st["lost"], "purged": st["purged"]}


# --- the message (RFC-0051 3), for the queue ---------------------------------

def sample_lines(node, entry):
    """One message per series of a sample, in the one fixed format."""
    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry["t"]))
    return [{"v": FORMAT_VERSION, "node": node, "t": t, "m": s,
             "x": entry[s]} for s in SERIES if s in entry]
