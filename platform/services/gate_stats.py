"""What happened at the gates -- counted from the gateway's access log
(RFC-0135 stage 2b, D10 a, Jörg 2026-11-26).

The gateway marks every request of a gate with `log_append oaap_gate
<tenant>/<name>` (appctl.gate_site_lines); the minutely sampler hands the
lines it read anyway (traffic.measure) to fold(). Nothing on the way of a
delivery changes, and every kind of target a gate will ever have is
counted the same way. What the log cannot say, this cannot say either: no
access (the key is redacted), no case number -- those stay in the target.

Per gate and UTC day: deliveries, pings and refusals by reason; the last
delivery, ping and refusal. Kept DAYS days. One event per delivery and per
refusal (RFC-0127, `oaap.gate.delivery.accepted.v1` / `...refused.v1`),
without payload -- the payload belongs to the target.

The log is DATA written by a program from what the internet sent: a line
is parsed, never trusted; only a gate the node's own gates.json knows is
counted, so a forged field adds nothing.

Pure: no disk, no network. appctl loads and saves.
"""
import calendar
import json
import re
import time

FORMAT = 1
DAYS = 30
FIELD = "oaap_gate"
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}/[a-z][a-z0-9-]{1,39}$")
# a refusal's reason, from the status the gate or its target answered
REASONS = ("anmeldung", "inhalt", "zu_gross", "zu_viele", "ziel")
REASON_TITLES = {"anmeldung": "Anmeldung", "inhalt": "Inhalt", "zu_gross": "zu groß",
                 "zu_viele": "zu viele", "ziel": "Ziel nicht erreichbar"}
EVENTS_MAX = 1000            # outbox: what the bus has not taken yet
EVENTS_DAYS = 7


def empty():
    return {"v": FORMAT, "gates": {}}


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def classify(d, known):
    """(gate key, what, reason, status, ts) of one parsed log line, or None.
    what: delivered | ping | refused | other. `known` is the set of gate
    keys gates.json holds."""
    if not isinstance(d, dict):
        return None
    key = d.get(FIELD)
    if not isinstance(key, str) or not _KEY_RE.match(key) or key not in known:
        return None
    status = d.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    ts = d.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else time.time()
    req = d.get("request") if isinstance(d.get("request"), dict) else {}
    method = str(req.get("method") or "").upper()
    uri = str(req.get("uri") or "").split("?")[0]
    name = key.split("/", 1)[1]
    base = f"/gate/{name}"
    rest = uri[len(base):].strip("/") if uri.startswith(base) else None
    if status == 401:
        return key, "refused", "anmeldung", status, ts
    if method in ("GET", "HEAD"):
        if rest == "" and 200 <= status < 300:
            return key, "ping", "", status, ts
        return key, "other", "", status, ts
    if method != "POST":
        return key, "other", "", status, ts
    if 200 <= status < 300:
        return key, "delivered", "", status, ts
    if status == 413:
        reason = "zu_gross"
    elif status == 429:
        reason = "zu_viele"
    elif status >= 500:
        reason = "ziel"
    else:
        reason = "inhalt"
    return key, "refused", reason, status, ts


def fold(stats, lines, known, now=None):
    """Add the lines to `stats` (modified and returned) -> (stats, events).
    Days older than DAYS fall out; gates gone from `known` fall out."""
    now = time.time() if now is None else now
    stats = stats if isinstance(stats, dict) and isinstance(stats.get("gates"), dict) else empty()
    gs = stats["gates"]
    events = []
    for line in lines or ():
        line = line.strip() if isinstance(line, str) else ""
        if FIELD not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        got = classify(d, known)
        if not got:
            continue
        key, what, reason, status, ts = got
        if what == "other":
            continue
        g = gs.setdefault(key, {"days": {}})
        day = g["days"].setdefault(time.strftime("%Y-%m-%d", time.gmtime(ts)),
                                   {"delivered": 0, "ping": 0, "refused": {}})
        at = _iso(ts)
        if what == "delivered":
            day["delivered"] += 1
            g["last_delivery"] = max(g.get("last_delivery") or "", at)
        elif what == "ping":
            day["ping"] += 1
            g["last_ping"] = max(g.get("last_ping") or "", at)
        else:
            day["refused"][reason] = day["refused"].get(reason, 0) + 1
            if at >= (g.get("last_refusal") or ""):
                g["last_refusal"], g["last_refusal_reason"] = at, reason
        if what in ("delivered", "refused"):
            tid, name = key.split("/", 1)
            data = {"gate": name, "status": status}
            if reason:
                data["reason"] = reason
            events.append({"tenant": tid, "publisher": "node",
                           "type": ("oaap.gate.delivery.accepted.v1" if what == "delivered"
                                    else "oaap.gate.delivery.refused.v1"),
                           "subject": f"gate:{name}", "data": data, "at": ts})
    cut = time.strftime("%Y-%m-%d", time.gmtime(now - DAYS * 86400))
    for key in list(gs):
        if key not in known:
            del gs[key]
            continue
        gs[key]["days"] = {k: v for k, v in gs[key].get("days", {}).items() if k > cut}
    return stats, events


def queue_events(outbox, events, now=None):
    """The bus outbox with `events` added; the oldest beyond EVENTS_MAX and
    anything older than EVENTS_DAYS fall out (a bus that is down for a week
    does not grow the node's disk)."""
    now = time.time() if now is None else now
    q = [e for e in (outbox if isinstance(outbox, list) else [])
         if isinstance(e, dict) and e.get("at", 0) > now - EVENTS_DAYS * 86400]
    q += events
    return q[-EVENTS_MAX:]


def summary(g, now=None, days=14):
    """The page's numbers of one gate: today, the last `days` days, the
    last delivery / ping / refusal, and the days with deliveries (for the
    light's usual rhythm). `g` is stats["gates"][key] or None."""
    now = time.time() if now is None else now
    g = g if isinstance(g, dict) else {}
    dd = g.get("days") if isinstance(g.get("days"), dict) else {}
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    since = time.strftime("%Y-%m-%d", time.gmtime(now - (days - 1) * 86400))

    def total(rows):
        out = {"delivered": 0, "ping": 0, "refused": {}}
        for r in rows:
            out["delivered"] += int(r.get("delivered") or 0)
            out["ping"] += int(r.get("ping") or 0)
            for k, n in (r.get("refused") or {}).items():
                out["refused"][k] = out["refused"].get(k, 0) + int(n or 0)
        out["refused_n"] = sum(out["refused"].values())
        return out

    return {"today": total([dd[today]] if today in dd else []),
            "window": total(v for k, v in dd.items() if k >= since),
            "active_days": sorted(k for k, v in dd.items() if (v or {}).get("delivered")),
            "last_delivery": g.get("last_delivery") or "", "last_ping": g.get("last_ping") or "",
            "last_refusal": g.get("last_refusal") or "",
            "last_refusal_reason": g.get("last_refusal_reason") or ""}


def unusually_quiet(s, now=None):
    """'' or the sentence why a gate that is used regularly is quiet now:
    at least three days with deliveries, and the last delivery is longer ago
    than three times the usual gap between them (at least two days)."""
    now = time.time() if now is None else now
    act = s.get("active_days") or []
    if len(act) < 3 or not s.get("last_delivery"):
        return ""
    ds = [calendar.timegm(time.strptime(d, "%Y-%m-%d")) for d in act]
    gaps = sorted(b - a for a, b in zip(ds, ds[1:]))
    usual = gaps[len(gaps) // 2] if gaps else 86400
    limit = max(2 * 86400, 3 * usual)
    try:
        last = calendar.timegm(time.strptime(s["last_delivery"], "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return ""
    quiet = now - last
    if quiet <= limit:
        return ""
    return f"ungewohnt still: keine Lieferung seit {int(quiet // 86400)} Tagen"
