"""The fleet's judgement (RFC-0085 §6, stage 2) -- pure.

What the hub's fleet service saw (its `state.json`, written by
services/twin/fleet.py) and what an entry's broker watch saw
(`broker-watch.json`, written by appctl) go in, with the time; conditions
for RFC-0080 come out. Nothing here reads a file or a clock, so every rule
is tested without a broker.

The rules, in one place:

  * a node is **silent** when its last `alive` is older than five minutes
    -- by the time the NODE wrote into it, not the time it arrived: a
    retained `alive` that reaches a restarted hub tells the truth about
    its age;
  * a node behind an entry whose bridge is down is **unknown**, not silent
    -- the hub cannot know (§6.1); the bridge's own matter names it;
  * the first two minutes after the fleet service started are a warm-up:
    nothing new is opened (a hub restart before the first full interval
    is "unknown", §6.1);
  * a **bridge** is down when its notification says `0` for five minutes;
  * a **broker queue** warns above half of its bound (§6.2);
  * **node.loss**: the node's own outbox reported more lost samples than
    the last time (the first sighting only sets the baseline);
  * on an entry, **hub.unreachable** when the hub's bridge has been away
    for ten minutes (§6.3).
"""
import calendar
import time

SILENT_AFTER = 300
BRIDGE_DOWN_AFTER = 300
HUB_UNREACHABLE_AFTER = 600
QUEUE_WARN = 0.5
STATE_STALE = 180          # an older state.json is not trusted at all
WARMUP = 120
FLEET_MATTERS = ("node.silent", "bridge.down", "broker.queue")


def parse_t(s):
    try:
        return calendar.timegm(time.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def minutes(seconds):
    m = int(seconds) // 60
    if m < 120:
        return f"{m} Minuten"
    if m < 2880:
        return f"{m // 60} Stunden"
    return f"{m // 1440} Tagen"


def trusted(state, now):
    """Is the fleet service alive and connected? Otherwise its silence
    says nothing about the nodes, and open matters must stay as they are."""
    return (isinstance(state, dict) and bool(state.get("connected"))
            and isinstance(state.get("written"), (int, float))
            and now - state["written"] <= STATE_STALE)


def own_bridges(state, me):
    """{entry: record} of the bridges THIS hub opened (the topic names the
    hub); with `me` None, all of them."""
    out = {}
    for b in (state.get("bridges") or {}).values():
        if isinstance(b, dict) and b.get("entry") and (me is None or b.get("hub") == me):
            out[b["entry"]] = b
    return out


def bridge_up(state, entry, me=None):
    b = own_bridges(state, me).get(entry)
    return None if b is None else bool(b.get("up"))


def _just_back(state, via, me, now, t):
    b = own_bridges(state, me).get(via) or {}
    since = b.get("since")
    return (bool(b.get("up")) and isinstance(since, (int, float))
            and now - since < WARMUP and t < since)


def view(state, now, me=None):
    """[{name, via, state, age, version, seq, gaps, lost, reason}] sorted --
    what `oaap fleet hub show` prints and what the conditions use."""
    rows = []
    warm = now - (state.get("started") or now) < WARMUP
    for key, n in sorted((state.get("nodes") or {}).items()):
        a = n.get("alive") or {}
        t = parse_t(a.get("t"))
        age = None if t is None else max(0, now - t)
        via = n.get("via") or ""
        st, why = "up", ""
        if age is None:
            st, why = "unknown", "noch kein Lebenszeichen"
        elif via and bridge_up(state, via, me) is False:
            st, why = "unknown", f"Eingang {via} getrennt"
        elif via and age > SILENT_AFTER and _just_back(state, via, me, now, t):
            # The bridge returned a moment ago and this node's last alive is
            # from before: the entry's backlog is still arriving. Without
            # this, every node behind a returning bridge was "silent" for one
            # minute -- an open and a close mail each (measured on oaap-test,
            # 2026-11-12).
            st, why = "unknown", f"Eingang {via} gerade zurück"
        elif age > SILENT_AFTER:
            st, why = ("unknown", "Dienst startet gerade") if warm else ("silent", "")
        rows.append({"name": n.get("name") or key, "via": via, "state": st, "age": age,
                     "version": a.get("version", ""), "seq": a.get("seq"),
                     "gaps": int(n.get("gaps", 0)), "lost": a.get("lost"),
                     "reason": why})
    return rows


def hub_conditions(state, now, me=None):
    """{key: condition} for the hub's default scope; `me` is the hub's name
    on the wire (only its own bridges count)."""
    out = {}
    rows = view(state, now, me)
    for r in rows:
        if r["state"] != "silent":
            continue
        last = iso(now - r["age"])
        out[f"node.silent/{r['name']}"] = {
            "type": "node.silent", "kurz": r["name"],
            "text": (f"Der Knoten {r['name']} hat seit {minutes(r['age'])} kein "
                     f"Lebenszeichen geschickt (zuletzt {last} UTC"
                     + (f", über den Eingang {r['via']}" if r["via"] else "")
                     + (f", Version {r['version']}" if r["version"] else "") + ").\n"
                     "Läuft der Rechner? Hat er Netz? Auf dem Knoten zeigt "
                     "'oaap metrics queue', ob sein Sender arbeitet.")}
    warm = now - (state.get("started") or now) < WARMUP
    for entry, b in sorted(own_bridges(state, me).items()):
        since = b.get("since")
        if b.get("up") or not isinstance(since, (int, float)) or warm:
            continue
        if now - since < BRIDGE_DOWN_AFTER:
            continue
        behind = sorted(r["name"] for r in rows if r["via"] == entry)
        out[f"bridge.down/{entry}"] = {
            "type": "bridge.down", "kurz": entry,
            "text": (f"Die Brücke zum Eingang {entry} ist seit {minutes(now - since)} "
                     f"getrennt (seit {iso(since)} UTC).\n"
                     + ("Dahinter, ohne Nachricht bis die Brücke zurück ist: "
                        + ", ".join(behind) + ".\n" if behind else "")
                     + "Was der Eingang inzwischen annimmt, wartet in seiner "
                       "Warteschlange und kommt nach.")}
    for node, b in sorted((state.get("brokers") or {}).items()):
        try:
            queued, bound = int(b.get("queued", 0)), int(b.get("max_queued", 0))
        except (TypeError, ValueError):
            continue
        rx = b.get("rx")
        if bound <= 0 or not isinstance(rx, (int, float)) or now - rx > STATE_STALE:
            continue
        if queued > QUEUE_WARN * bound:
            out[f"broker.queue/{node}"] = {
                "type": "broker.queue", "kurz": node,
                "text": (f"Der Broker auf {node} hält {queued} Nachrichten zurück, "
                         f"die Grenze ist {bound} je Sitzung. Wird sie erreicht, "
                         "verwirft er die neuesten (gemessen, RFC-0085 §14).")}
    return out


def loss_events(state, prev):
    """([(node, before, now)], new_prev) for nodes whose outbox lost more
    samples since the last minute. A node seen for the first time only sets
    its baseline."""
    events, new = [], dict(prev or {})
    for key, n in sorted((state.get("nodes") or {}).items()):
        lost = (n.get("alive") or {}).get("lost")
        if not isinstance(lost, int):
            continue
        name = n.get("name") or key
        before = new.get(name)
        if isinstance(before, int) and lost > before:
            events.append((name, before, lost))
        new[name] = lost
    return events, new


def entry_conditions(watch, now):
    """{key: condition} on an entry from its broker watch, or None when the
    watch is stale (then open matters stay)."""
    if not isinstance(watch, dict) or now - int(watch.get("at", 0)) > STATE_STALE:
        return None
    out = {}
    for hub, h in sorted((watch.get("hubs") or {}).items()):
        since = h.get("since")
        if h.get("up") or not isinstance(since, (int, float)):
            continue
        if now - since >= HUB_UNREACHABLE_AFTER:
            out[f"hub.unreachable/{hub}"] = {
                "type": "hub.unreachable", "kurz": hub,
                "text": (f"Die Zentrale {hub} ist seit {minutes(now - since)} nicht mit "
                         f"diesem Eingang verbunden (seit {iso(since)} UTC).\n"
                         "Solange sie fehlt, sieht niemand die Flotte: kein Alarm über "
                         "schweigende Knoten. Was hier ankommt, wartet in der "
                         "Warteschlange.")}
    return out
