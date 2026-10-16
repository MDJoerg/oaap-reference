"""Which instances are in use -- the gateway's own count (RFC-0059 stage 3).

The gateway (Caddy) counts, per listener and per host name, the requests
it is handling RIGHT NOW (`caddy_http_requests_in_flight`) and the ones it
has finished (`caddy_http_request_duration_seconds_count`). The first is
what the access log cannot give: a WebSocket is in flight for as long as
it is open, so a desktop session someone is working in is "in use" even
when no new HTTP request has been made for an hour. An idle keep-alive
connection is NOT in flight -- that is the difference to `netstat`.

An instance is IN USE in a minute when any of its listeners or host names
has a request in flight, or has finished more requests than a minute ago.
"Idle for N minutes" is then simply "not in use since N minutes".

Pure functions, no I/O: the acting (asking the gateway, stopping the
instance) is appctl's. Every function fails toward "in use": a missing
number, a reshuffled server name or a counter that went DOWN counts as
activity, never as silence -- putting a working desk to sleep is the
mistake that costs a person their afternoon, waiting one more minute costs
nothing.
"""
import json
import re

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+([0-9.eE+\-]+|NaN|\+Inf)\s*$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
IN_FLIGHT = "caddy_http_requests_in_flight"
FINISHED = ("caddy_http_request_duration_seconds_count", "caddy_http_requests_total")


def parse_metrics(text):
    """Text exposition -> {(server, host): {"inflight": n, "done": n}}.

    `done` sums every code and method of one (server, host); a family that
    is not there leaves it at 0 (the other counter is tried per line, but
    only the first family found is used, so nothing is counted twice)."""
    out = {}
    done_family = None
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        name, labels, value = m.group(1), m.group(2), m.group(3)
        if name != IN_FLIGHT and name not in FINISHED:
            continue
        lab = dict(_LABEL.findall(labels))
        key = (lab.get("server", ""), (lab.get("host") or "").lower())
        try:
            v = float(value)
        except ValueError:
            continue
        row = out.setdefault(key, {"inflight": 0, "done": 0})
        if name == IN_FLIGHT:
            row["inflight"] += int(v)
        else:
            if done_family is None:
                done_family = name
            if name == done_family:
                row["done"] += int(v)
    return out


def server_ports(config_text):
    """The gateway's JSON config -> {server name: [ports]}. Names like
    `srv0` are handed out in order and can move when a site is added or
    removed, which is why this is read again every time."""
    try:
        cfg = json.loads(config_text)
        servers = cfg["apps"]["http"]["servers"]
    except (ValueError, KeyError, TypeError):
        return {}
    out = {}
    for name, srv in (servers or {}).items():
        ports = []
        for addr in (srv or {}).get("listen") or []:
            tail = str(addr).rsplit(":", 1)[-1]
            if tail.isdigit():
                ports.append(int(tail))
        out[name] = ports
    return out


def readings(metrics, ports_of, instances):
    """Per instance: {"inflight": n, "done": n, "seen": bool}.

    `instances` is {name: {"port": int|None, "hosts": [names]}}. A server
    counts for an instance when it listens on the instance's own port (the
    LAN site is one listener per instance); a host label counts when it is
    one of the instance's names, on whichever server it arrived.
    `seen` is False when nothing in the gateway's numbers belongs to the
    instance at all -- such an instance is never put to sleep."""
    by_port = {}
    for srv, plist in ports_of.items():
        for p in plist:
            by_port.setdefault(p, set()).add(srv)
    out = {}
    for name, spec in instances.items():
        servers = by_port.get(spec.get("port"), set()) if spec.get("port") else set()
        hosts = {h.lower() for h in spec.get("hosts") or [] if h}
        row = {"inflight": 0, "done": 0, "seen": False}
        for (srv, host), r in metrics.items():
            # the port-wise server also carries the "(other)" host label,
            # so everything on that listener is this instance's
            if (srv in servers) or (host and host in hosts):
                row["inflight"] += r["inflight"]
                row["done"] += r["done"]
                row["seen"] = True
        out[name] = row
    return out


def update(state, now, reads):
    """Fold one minute's readings into the saved state (a dict, returned
    new). Per instance: `done` (the counter last seen) and `last` (epoch
    seconds of the last minute it was in use). A first sight starts the
    clock at `now`, so a freshly seen instance gets its full time."""
    new = {}
    for name, r in reads.items():
        old = state.get(name) or {}
        in_use = (r["inflight"] > 0 or "done" not in old
                  or old.get("done") != r["done"])
        new[name] = {"done": r["done"], "seen": bool(r["seen"]),
                     "last": now if in_use else old.get("last", now)}
    return new


def idle_minutes(state, name, now):
    """Whole minutes the instance has been out of use; None when the
    gateway's numbers never showed it (unknown is not idle)."""
    s = state.get(name)
    if not s or not s.get("seen"):
        return None
    return max(0, int((now - s.get("last", now)) // 60))
