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
  * **catching up**: after the fleet service started or reconnected to its
    broker, a node whose last `alive` is from before gets CATCHUP seconds
    before it can be silent -- a node that could not reach the hub waits
    up to 15 minutes between tries (the metrics sender's backoff); measured
    2026-11-13: oaap-demo stopped 13 minutes, three direct nodes back 4-5
    minutes after the hub, each an open and a close mail;
  * what can no longer be judged ("unknown") keeps an open matter open --
    it is never mailed as "Behoben";
  * a **bridge** is down when its notification says `0` for five minutes;
  * a **broker queue** warns above half of its bound (§6.2);
  * **node.loss**: the node's own outbox reported more lost samples than
    the last time (the first sighting only sets the baseline);
  * on an entry, **hub.unreachable** when the hub's bridge has been away
    for ten minutes (§6.3);
  * **link.down** (RFC-0087 §6): a tunnel whose joined state has been down
    for five minutes -- ONE matter per tunnel, however many of its halves
    report it, naming what depends on it. The fleet service's report
    (`affected.json`) is trusted for three minutes, like its state. Since
    RFC-0087 §8.1 also an external end whose tunnel app says its tunnel
    or the target is down -- the same matter type, one per end.
"""
import calendar
import time

SILENT_AFTER = 300
BRIDGE_DOWN_AFTER = 300
HUB_UNREACHABLE_AFTER = 600
QUEUE_WARN = 0.5
STATE_STALE = 180          # an older state.json is not trusted at all
WARMUP = 120
CATCHUP = 17 * 60          # the sender's longest backoff (15 min) + 2 min
FLEET_MATTERS = ("node.silent", "bridge.down", "broker.queue", "link.down")
LINK_DOWN_AFTER = 300
AFFECTED_STALE = 180       # an older affected.json is no report at all
LINK_ROWS = 25             # a matter's text names this many, then counts


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
    back = max(state.get("started") or 0, state.get("connected_since") or 0)
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
        elif age > SILENT_AFTER and warm:
            st, why = "unknown", "Dienst startet gerade"
        elif age > SILENT_AFTER and t < back and now - back < CATCHUP:
            st, why = "unknown", "Zentrale gerade zurück, Sender holt auf"
        elif age > SILENT_AFTER:
            st, why = "silent", ""
        rows.append({"name": n.get("name") or key, "via": via, "state": st, "age": age,
                     "version": a.get("version", ""), "seq": a.get("seq"),
                     "gaps": int(n.get("gaps", 0)), "lost": a.get("lost"),
                     "reason": why})
    return rows


def hub_conditions(state, now, me=None, open_matters=None):
    """{key: condition} for the hub's default scope; `me` is the hub's name
    on the wire (only its own bridges count). `open_matters` ({key:
    condition}, the matters open now): one whose subject is "unknown" this
    minute stays open as it is."""
    out = {}
    held = open_matters or {}
    rows = view(state, now, me)
    for r in rows:
        key = f"node.silent/{r['name']}"
        if r["state"] == "unknown" and key in held:
            out[key] = held[key]
        if r["state"] != "silent":
            continue
        last = iso(now - r["age"])
        out[f"node.silent/{r['name']}"] = {
            "type": "node.silent", "kurz": r["name"], "began": last,
            "text": (f"Der Knoten {r['name']} hat seit {minutes(r['age'])} kein "
                     f"Lebenszeichen geschickt (zuletzt {last} UTC"
                     + (f", über den Eingang {r['via']}" if r["via"] else "")
                     + (f", Version {r['version']}" if r["version"] else "") + ").\n"
                     "Läuft der Rechner? Hat er Netz? Auf dem Knoten zeigt "
                     "'oaap metrics queue', ob sein Sender arbeitet.")}
    warm = now - (state.get("started") or now) < WARMUP
    for entry, b in sorted(own_bridges(state, me).items()):
        since = b.get("since")
        if warm and not b.get("up") and f"bridge.down/{entry}" in held:
            out[f"bridge.down/{entry}"] = held[f"bridge.down/{entry}"]
        if b.get("up") or not isinstance(since, (int, float)) or warm:
            continue
        if now - since < BRIDGE_DOWN_AFTER:
            continue
        behind = sorted(r["name"] for r in rows if r["via"] == entry)
        out[f"bridge.down/{entry}"] = {
            "type": "bridge.down", "kurz": entry, "began": iso(since),
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
                "type": "hub.unreachable", "kurz": hub, "began": iso(since),
                "text": (f"Die Zentrale {hub} ist seit {minutes(now - since)} nicht mit "
                         f"diesem Eingang verbunden (seit {iso(since)} UTC).\n"
                         "Solange sie fehlt, sieht niemand die Flotte: kein Alarm über "
                         "schweigende Knoten. Was hier ankommt, wartet in der "
                         "Warteschlange.")}
    return out


_KIND = {"tunnel": "Tunnel", "destination": "Destination", "instance": "Instanz",
         "external": "Externes Ende", "tenant": "Mandant", "node": "Knoten",
         # RFC-0087 §8.2: what an app names of itself
         "model": "Modellname", "source": "Bezugsquelle", "asset": "Asset"}
_VIA = {"usesTunnel": "nutzt {}", "usesExternal": "nutzt {}", "behind": "liegt hinter {}",
        "reaches": "erreicht {} -- möglicherweise betroffen", "bindsTo": "bindet {}",
        "assetVia": "geht über {}", "assetUses": "nutzt {}"}
_SIDE = {"down-inner": "Die innere Seite ist nicht verbunden.",
         "down-outer": "Die äußere Seite meldet ihn getrennt.",
         "down-tunnel": "Der Tunnel der App selbst ist getrennt.",
         "down-target": "Der Tunnel der App steht, aber das Ziel antwortet nicht."}


def _external_head(name, link, now, since):
    """The first line for an external end (RFC-0087 §8.1)."""
    node, _, rest = name.partition("/")
    inst, _, target = rest.partition("/")
    behind = link.get("behind") or inst
    return (f"Das externe Ende {target or name} hinter {behind}"
            f"{' auf ' + node if node else ''} ist seit {minutes(now - since)} nicht erreichbar "
            f"(seit {iso(since)} UTC). " + _SIDE.get(link.get("state"), ""))


def link_text(name, link, now):
    """The matter's text: the tunnel, its two ends, and what depends on it,
    nearest first, indented by distance."""
    since = parse_t(link.get("since")) or now
    external = link.get("kind") == "external"
    # an external end is reported as <node>/<instance>/<target>; a row that
    # depends on it says the target's own name (seen live 2026-11-15)
    titles = {link.get("id"): name.rsplit("/", 1)[-1] if external else name}
    for r in link.get("affected") or []:
        titles[r.get("id")] = r.get("title")
    ends = ", ".join(x for x in (f"außen {link['outer']}" if link.get("outer") else "",
                                  f"innen {link['inner']}" if link.get("inner") else "") if x)
    if external:
        lines = [_external_head(name, link, now, since)]
    else:
        lines = [f"Der Tunnel {name} ist seit {minutes(now - since)} getrennt "
                 f"(seit {iso(since)} UTC{', ' + ends if ends else ''}). "
                 + _SIDE.get(link.get("state"), "")]
    rows = link.get("affected") or []
    if rows:
        lines.append("Betroffen, von nah nach fern:")
        for r in rows[:LINK_ROWS]:
            where = (f" in {r['instance']}" if r.get("instance") else "")                 + (f" auf {r['node']}" if r.get("node") else "")
            ten = f", Mandant {r['tenant']}" if r.get("tenant") else ""
            how = _VIA.get(r.get("via"), "{}").format(titles.get(r.get("from"), "?"))
            lines.append("  " * max(0, int(r.get("depth", 1)) - 1)
                         + f"- {_KIND.get(r.get('kind'), r.get('kind'))} {r.get('title')}"
                         + f"{where}{ten} ({how})")
        more = len(rows) - LINK_ROWS
        if more > 0 or link.get("truncated"):
            lines.append(f"  ... und {max(more, 0)} weitere"
                         + (" (die Suche wurde begrenzt)" if link.get("truncated") else ""))
    else:
        lines.append("Im Zwilling hängt nichts an diesem "
                     + ("externen Ende." if external else "Tunnel."))
    if link.get("tenants"):
        lines.append("Mandanten: " + ", ".join(link["tenants"]) + ".")
    if external:
        lines.append("Die Seite der Tunnel-App zeigt ihren Tunnel und jedes Ziel.")
    else:
        lines.append("Außen zeigt 'oaap connect list', innen 'oaap connector list' beide Hälften.")
    return "\n".join(lines)


def link_conditions(doc, now, held=None):
    """{key: condition} for the tunnels that are down (RFC-0087 §6), or None
    when the fleet service's report is missing or stale -- then open matters
    stay as they are, never mailed as "Behoben"."""
    t = parse_t((doc or {}).get("written")) if isinstance(doc, dict) else None
    if t is None or now - t > AFFECTED_STALE:
        return None
    held = held or {}
    out = {}
    for name, link in sorted((doc.get("links") or {}).items()):
        key = f"link.down/{name}"
        since = parse_t(link.get("since"))
        if since is None or now - since < LINK_DOWN_AFTER:
            if key in held:
                # the state changed side (inner -> outer) and `since` began
                # anew: still the same broken tunnel, the matter stays
                out[key] = held[key]
            continue
        out[key] = {"type": "link.down", "kurz": name, "began": iso(since),
                    "text": link_text(name, link, now)}
    return out


VERDICT_STALE = 180        # an older verdict.json is no verdict at all (fleet_sink)


def verdict(state, now, me, previous):
    """The judgement per node for the twin (RFC-0085 amendment A2):
    {"written", "nodes": {name: {state, since, reason}}}. The same rows as
    `view` -- one rule for the mail and the twin -- with `since` kept while
    a node's state does not change. `previous` is the last verdict."""
    prev = (previous or {}).get("nodes") or {}
    out = {}
    for r in view(state, now, me):
        p = prev.get(r["name"]) or {}
        since = p.get("since") if p.get("state") == r["state"] and p.get("since") else iso(now)
        out[r["name"]] = {"state": r["state"], "since": since, "reason": r.get("reason") or ""}
    return {"written": iso(now), "nodes": out}
