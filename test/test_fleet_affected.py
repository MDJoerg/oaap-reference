#!/usr/bin/env python3
"""RFC-0087 §6, stage 2: affected, not alarmed.

Defends three things:

    THE WALK GOES THE RIGHT WAY ROUND. From a broken object, backwards:
    who uses it, who is bound to that, who reaches that -- the RFC's own
    chain ("VPN of ais-ovc down -> gamingpc -> ais-mg -> tunnel ->
    ais-oaapi01 -> ais-chat") is rebuilt here and must come out in exactly
    that order, nearest first, each object once even through a cycle, and
    nothing that does not depend on the start.

    ONE BROKEN TUNNEL, ONE MATTER. The hub's report lists a tunnel once,
    whichever half reports it down, keeps its `since` while the state holds,
    and the minute job opens a matter only after five minutes -- a matter
    that names what depends on it and its tenants.

    A STALE REPORT DECIDES NOTHING. Without a fresh report from the fleet
    service open matters stay as they are; an empty fresh report closes them.

Run: python3 test/test_fleet_affected.py   (pure, no database)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))

import fleet_affected as fa    # noqa: E402
import fleet_model as fm       # noqa: E402
import fleet_watch as fw       # noqa: E402
import notify                  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -- {detail}"))
    if not cond:
        fails += 1


O = {
    "nx": {"type": fm.NODE, "title": "oaapx01", "source": "oaapx01"},
    "ni": {"type": fm.NODE, "title": "oaapi01", "source": "oaapi01"},
    "ta": {"type": fm.TENANT, "title": "ais", "source": "tenant:oaapi01/x"},
    "ovc": {"type": fm.INSTANCE, "title": "ais-ovc", "source": "oaapi01/ais-ovc"},
    "egpc": {"type": fm.EXTERNAL, "title": "gamingpc", "source": "ext:oaapi01/gamingpc"},
    "dgpc": {"type": fm.DESTINATION, "title": "gamingpc", "source": "dest:oaapi01/gamingpc"},
    "mg": {"type": fm.INSTANCE, "title": "ais-mg", "source": "oaapi01/ais-mg"},
    "tun": {"type": fm.TUNNEL, "title": "t-9f2c", "source": "tunnel:t-9f2c"},
    "doi": {"type": fm.DESTINATION, "title": "ais-oaapi01", "source": "dest:oaapx01/ais-oaapi01"},
    "chat": {"type": fm.INSTANCE, "title": "ais-chat", "source": "oaapx01/ais-chat"},
    "studio": {"type": fm.INSTANCE, "title": "studio", "source": "oaapx01/studio"},
    # a second tunnel back from oaapx01 to oaapi01: makes a cycle
    "tun2": {"type": fm.TUNNEL, "title": "t-b00c", "source": "tunnel:t-b00c"},
    "dback": {"type": fm.DESTINATION, "title": "chat-back", "source": "dest:oaapi01/chat-back"},
}
R = [
    ("egpc", "fleetBehind", "ovc"),
    ("dgpc", "fleetUsesExternal", "egpc"),
    ("dgpc", "fleetDestOf", "ta"),
    ("mg", "fleetBindsTo", "dgpc"),
    ("mg", "fleetHeldBy", "ta"),
    ("ovc", "fleetHeldBy", "ta"),
    ("tun", "fleetReaches", "mg"),
    ("tun", "fleetServedBy", "nx"),
    ("tun", "fleetDialedBy", "ni"),
    ("doi", "fleetUsesTunnel", "tun"),
    ("doi", "fleetDestOf", "ta"),
    ("chat", "fleetBindsTo", "doi"),
    ("chat", "fleetHeldBy", "ta"),
    ("ta", "fleetTenantOn", "ni"),
    # the cycle: tun2 reaches ais-chat, chat-back uses tun2, ais-mg binds chat-back
    ("tun2", "fleetReaches", "chat"),
    ("dback", "fleetUsesTunnel", "tun2"),
    ("mg", "fleetBindsTo", "dback"),
]

print("=== affected(): die Kette aus dem RFC ===")
a = fa.affected(R, O, "ovc")
order = [r["title"] for r in a["affected"]]
ok("VPN von ais-ovc aus: gamingpc (extern), gamingpc (Destination), ais-mg, t-9f2c, "
   "ais-oaapi01, ais-chat -- in dieser Reihenfolge",
   order[:6] == ["gamingpc", "gamingpc", "ais-mg", "t-9f2c", "ais-oaapi01", "ais-chat"], order)
by = {r["id"]: r for r in a["affected"]}
ok("jede Zeile sagt, wodurch sie abhängt",
   by["egpc"]["via"] == "behind" and by["dgpc"]["via"] == "usesExternal"
   and by["mg"]["via"] == "bindsTo" and by["tun"]["via"] == "reaches"
   and by["doi"]["via"] == "usesTunnel" and by["chat"]["via"] == "bindsTo", by)
ok("Ort und Mandant je Zeile", by["chat"]["node"] == "oaapx01" and by["chat"]["tenant"] == "ais"
   and by["dgpc"]["node"] == "oaapi01" and by["doi"]["tenant"] == "ais")
ok("die Schleife endet, jedes Objekt einmal", len(order) == len(set(by)) and "ovc" not in by
   and set(by) == {"egpc", "dgpc", "mg", "tun", "doi", "chat", "tun2", "dback"}, order)
ok("was nicht abhängt, fehlt: studio, die Knoten, der Mandant als Zeile",
   not ({"studio", "nx", "ni", "ta"} & set(by)))
ok("Mandanten zusammengefasst", a["tenants"] == ["ais"], a["tenants"])
t = fa.affected(R, O, "tun")
ok("vom Tunnel aus: ais-oaapi01, ais-chat, dann über die Schleife weiter",
   [r["title"] for r in t["affected"]][:2] == ["ais-oaapi01", "ais-chat"], t["affected"])
ok("der Tunnel nennt seine Enden", t["start"]["outer"] == "oaapx01" and t["start"]["inner"] == "oaapi01")
ok("kein Objekt: None", fa.affected(R, O, "gibtsnicht") is None)
ok("an nichts hängt nichts", fa.affected(R, O, "studio")["affected"] == [])
lim = fa.affected(R, O, "ovc", limit=3)
ok("eine Grenze wird gesagt, nicht verschwiegen", lim["truncated"] and len(lim["affected"]) == 3)
ok("kurze Schlüssel gehen auch", fa.short("fleetUsesTunnel") == "usesTunnel"
   and fa.short("bindsTo") == "bindsTo")

print("=== link_report(): ein Tunnel, ein Eintrag ===")
r1 = fa.link_report({"tun": "down-inner", "tun2": "ok"}, R, O, {}, 1_800_000_000)
ok("nur der gestörte Tunnel steht drin", list(r1["links"]) == ["t-9f2c"], r1["links"].keys())
l1 = r1["links"]["t-9f2c"]
ok("mit Zustand, Enden, Betroffenen und Mandanten",
   l1["state"] == "down-inner" and l1["outer"] == "oaapx01" and l1["inner"] == "oaapi01"
   and [x["title"] for x in l1["affected"]][:2] == ["ais-oaapi01", "ais-chat"]
   and l1["tenants"] == ["ais"], l1)
r2 = fa.link_report({"tun": "down-inner"}, R, O, r1, 1_800_000_600)
ok("`since` bleibt, solange der Zustand bleibt", r2["links"]["t-9f2c"]["since"] == l1["since"])
r3 = fa.link_report({"tun": "down-outer"}, R, O, r2, 1_800_000_900)
ok("ein anderer Zustand beginnt neu", r3["links"]["t-9f2c"]["since"] != l1["since"])
ok("paused, half und ok sind nicht gestört",
   fa.link_report({"tun": "paused", "tun2": "half"}, R, O, {}, 1_800_000_000)["links"] == {})

print("=== fleet_watch.link_conditions(): die Meldung ===")
now = 1_800_001_000
doc = {"written": fw.iso(now - 20),
       "links": {"t-9f2c": dict(l1, since=fw.iso(now - 400))}}
c = fw.link_conditions(doc, now)
m = c.get("link.down/t-9f2c") or {}
ok("nach fünf Minuten eine Meldung je Tunnel", list(c) == ["link.down/t-9f2c"]
   and m.get("type") == "link.down" and m.get("kurz") == "t-9f2c", c)
txt = m.get("text", "")
ok("der Text nennt Dauer, Enden, Seite, Betroffene und Mandanten",
   "seit 6 Minuten" in txt and "außen oaapx01" in txt and "innen oaapi01" in txt
   and "innere Seite" in txt and "Destination ais-oaapi01 auf oaapx01, Mandant ais" in txt
   and "Instanz ais-chat auf oaapx01" in txt and "bindet ais-oaapi01" in txt
   and "Mandanten: ais." in txt, txt)
ok("ein Tunnel, der eine betroffene Instanz erreicht, heißt möglicherweise betroffen",
   "möglicherweise betroffen" in txt, txt)
ok("eingerückt nach Entfernung", "\n- Destination ais-oaapi01" in txt
   and "\n  - Instanz ais-chat" in txt, txt)
young = {"written": fw.iso(now), "links": {"t-9f2c": dict(l1, since=fw.iso(now - 100))}}
ok("unter fünf Minuten: keine Meldung", fw.link_conditions(young, now) == {})
held = {"link.down/t-9f2c": {"type": "link.down", "kurz": "t-9f2c", "text": "alt"}}
ok("... aber eine offene bleibt offen (Seite gewechselt, since neu)",
   fw.link_conditions(young, now, held) == held)
ok("ein frischer leerer Bericht schließt (-> Behoben)",
   fw.link_conditions({"written": fw.iso(now), "links": {}}, now, held) == {})
ok("ein alter Bericht entscheidet nichts (None: offene bleiben)",
   fw.link_conditions(dict(doc, written=fw.iso(now - 400)), now, held) is None
   and fw.link_conditions(None, now) is None and fw.link_conditions({}, now) is None)
many = dict(l1, since=fw.iso(now - 400),
            affected=[dict(l1["affected"][0], id=f"x{i}", title=f"d{i}") for i in range(40)])
t2 = fw.link_conditions({"written": fw.iso(now), "links": {"t": many}}, now)["link.down/t"]["text"]
ok("viele Betroffene: 25 genannt, der Rest gezählt", "... und 15 weitere" in t2
   and t2.count("- Destination") == 25, t2[-200:])

print("=== Dauer im Behoben: ab dem Beginn der Störung (0.1.287, Befund b) ===")
from datetime import datetime, timedelta, timezone   # noqa: E402
ok("die Bedingung trägt ihren Beginn", m.get("began") == fw.iso(now - 400), m)
t0 = datetime.fromtimestamp(now, timezone.utc)
ops, st = notify.matter_ops({"link.down/t-9f2c": m}, {}, t0)
rec = st["link.down/t-9f2c"]
ok("die Lage merkt sich began neben opened",
   rec.get("began") == fw.iso(now - 400) and rec.get("opened") == t0.isoformat(), rec)
# die Seite wechselt (innen -> außen): since beginnt neu, began bleibt der früheste
later = dict(m, began=fw.iso(now + 60))
_ops, st2 = notify.matter_ops({"link.down/t-9f2c": later}, st, t0 + timedelta(minutes=2))
ok("began bleibt der früheste, auch wenn since neu beginnt",
   st2["link.down/t-9f2c"]["began"] == fw.iso(now - 400), st2)
t_close = t0 + timedelta(minutes=6)
ops3, _st3 = notify.matter_ops({}, st2, t_close)
closing = [r for op, _k, r in ops3 if op == "close"][0]
lasted = notify.matter_duration(closing, t_close)
ok("Dauer = Störung (6 min 40 s ab began), nicht ab Öffnung (6 min)",
   lasted == 400 + 360, lasted)
ok("... in Worten wie im Text", notify.span_words(lasted) == notify.span_words(760),
   notify.span_words(lasted))
ok("ohne began (alle anderen Typen): wie bisher ab opened",
   notify.matter_duration({"opened": t0.isoformat()}, t_close) == 360)
ok("weder began noch opened: unbekannt", notify.matter_duration({}, t_close) is None)
src_w = open(os.path.join(HERE, "..", "platform", "services", "fleet_watch.py"),
             encoding="utf-8").read()
ok("node.silent, bridge.down, hub.unreachable, link.down tragen began",
   src_w.count('"began": ') == 4)
src_a = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("appctl rechnet die Dauer über matter_duration", "notify.matter_duration(rec, now)" in src_a)

print("=== Stufe 3a: ein externes Ende, das seine Tunnel-App gestört meldet (§8.1) ===")
O3 = dict(O, egpc=dict(O["egpc"], source="ext:oaapi01/ais-ovc/gamingpc"))
r5 = fa.link_report({"egpc": "down-target", "tun": "ok"}, R, O3, {}, 1_800_000_000)
ok("das externe Ende steht im Bericht unter Knoten/Instanz/Ziel",
   list(r5["links"]) == ["oaapi01/ais-ovc/gamingpc"], r5["links"].keys())
e5 = r5["links"]["oaapi01/ais-ovc/gamingpc"]
ok("Art external, liegt hinter ais-ovc, auf oaapi01, ohne Tunnel-Enden",
   e5["kind"] == "external" and e5["behind"] == "ais-ovc" and e5["node"] == "oaapi01"
   and e5["outer"] == "" and e5["inner"] == "", e5)
ok("Betroffene: die Destination gamingpc, ais-mg, dann der Tunnel und weiter",
   [x["title"] for x in e5["affected"]][:4] == ["gamingpc", "ais-mg", "t-9f2c", "ais-oaapi01"],
   e5["affected"])
ok("ein Tunnel im Bericht heißt weiter tunnel",
   fa.link_report({"tun": "down-inner"}, R, O3, {}, 1)["links"]["t-9f2c"]["kind"] == "tunnel")
ok("down-tunnel ist gestört; paused, unknown, present, ok nicht",
   list(fa.link_report({"egpc": "down-tunnel"}, R, O3, {}, 1)["links"]) == ["oaapi01/ais-ovc/gamingpc"]
   and all(fa.link_report({"egpc": st}, R, O3, {}, 1)["links"] == {}
           for st in ("paused", "unknown", "present", "ok")))
r6 = fa.link_report({"egpc": "down-target"}, R, O3, r5, 1_800_000_600)
ok("since bleibt, solange der Zustand bleibt",
   r6["links"]["oaapi01/ais-ovc/gamingpc"]["since"] == e5["since"])
now3 = 1_800_001_000
doc3 = {"written": fw.iso(now3), "links": {"oaapi01/ais-ovc/gamingpc": dict(e5, since=fw.iso(now3 - 420))}}
c3 = fw.link_conditions(doc3, now3)
m3 = c3.get("link.down/oaapi01/ais-ovc/gamingpc") or {}
ok("nach fünf Minuten: link.down für das externe Ende, kurz = Knoten/Instanz/Ziel",
   m3.get("type") == "link.down" and m3.get("kurz") == "oaapi01/ais-ovc/gamingpc", c3)
t3 = m3.get("text", "")
ok("der Text: externes Ende gamingpc hinter ais-ovc auf oaapi01, nicht erreichbar, Ziel antwortet nicht",
   "Das externe Ende gamingpc hinter ais-ovc auf oaapi01 ist seit 7 Minuten nicht erreichbar" in t3
   and "das Ziel antwortet nicht" in t3, t3)
ok("... nennt die Betroffenen und den Weg zur Seite der App, nicht 'oaap connect list'",
   "Destination gamingpc auf oaapi01" in t3 and "Instanz ais-mg" in t3
   and "Seite der Tunnel-App" in t3 and "connect list" not in t3, t3)
e7 = dict(e5, state="down-tunnel", since=fw.iso(now3 - 400))
t7 = fw.link_conditions({"written": fw.iso(now3), "links": {"oaapi01/ais-ovc/gamingpc": e7}},
                        now3)["link.down/oaapi01/ais-ovc/gamingpc"]["text"]
ok("down-tunnel: der Text sagt, dass der Tunnel der App getrennt ist",
   "Der Tunnel der App selbst ist getrennt." in t7, t7)
ok("der Betreff sagt Verbindung, nicht Tunnel",
   notify.CATALOGUE["link.down"]["subject"] == "[{knoten}] Verbindung getrennt: {kurz}")

print("=== Stufe 3b: Assets einer App -- Modellnamen hinter dem externen Ende (§8.2) ===")
O4 = dict(O3, **{
    "sgpc": {"type": fm.ASSET, "title": "gaming", "source": "asset:oaapi01/ais-mg/source/gaming"},
    "mchat": {"type": fm.ASSET, "title": "chat-gaming",
              "source": "asset:oaapi01/ais-mg/model/chat-gaming"},
    "mcode": {"type": fm.ASSET, "title": "code", "source": "asset:oaapi01/ais-mg/model/code"},
    "sfern": {"type": fm.ASSET, "title": "anthropic",
              "source": "asset:oaapi01/ais-mg/source/anthropic"},
    "mfern": {"type": fm.ASSET, "title": "chat-fern", "source": "asset:oaapi01/ais-mg/model/chat-fern"},
})
R4 = R + [
    ("sgpc", "fleetAssetOf", "mg"), ("sgpc", "fleetAssetVia", "dgpc"),
    ("mchat", "fleetAssetOf", "mg"), ("mchat", "fleetAssetUses", "sgpc"),
    ("mcode", "fleetAssetOf", "mg"), ("mcode", "fleetAssetUses", "sgpc"),
    ("mcode", "fleetAssetUses", "sfern"),
    ("sfern", "fleetAssetOf", "mg"), ("mfern", "fleetAssetOf", "mg"),
    ("mfern", "fleetAssetUses", "sfern"),
]
a8 = fa.affected(R4, O4, "egpc")
by8 = {x["id"]: x for x in a8["affected"]}
ok("die Quelle gaming haengt an der Destination (assetVia), Art source, in ais-mg auf oaapi01",
   by8.get("sgpc", {}).get("kind") == "source" and by8["sgpc"]["via"] == "assetVia"
   and by8["sgpc"]["from"] == "dgpc" and by8["sgpc"]["instance"] == "ais-mg"
   and by8["sgpc"]["node"] == "oaapi01", by8.get("sgpc"))
ok("das Asset traegt den Mandanten seiner Instanz", by8["sgpc"]["tenant"] == "ais", by8["sgpc"])
ok("die Modellnamen chat-gaming und code nutzen sie (assetUses), Art model",
   all(by8.get(k, {}).get("kind") == "model" and by8[k]["via"] == "assetUses"
       and by8[k]["from"] == "sgpc" for k in ("mchat", "mcode")), a8["affected"])
ok("genau: der Modellname nur ueber den fremden Anbieter und dessen Quelle sind NICHT betroffen",
   "mfern" not in by8 and "sfern" not in by8, sorted(by8))
ok("assetOf zaehlt nicht als Abhaengigkeit: die Instanz zieht nicht alle ihre Assets mit",
   fa.affected(R4, O4, "mg")["affected"] == fa.affected(R, O3, "mg")["affected"])
r8 = fa.link_report({"egpc": "down-target"}, R4, O4, {}, 1_800_000_000)
e8 = r8["links"]["oaapi01/ais-ovc/gamingpc"]
t8 = fw.link_text("oaapi01/ais-ovc/gamingpc", dict(e8, since=fw.iso(now3 - 420)), now3)
ok("die Meldung nennt die Quelle: Bezugsquelle gaming in ais-mg auf oaapi01 (geht über gamingpc)",
   "- Bezugsquelle gaming in ais-mg auf oaapi01, Mandant ais (geht über gamingpc)" in t8, t8)
ok("... und die Modellnamen, die sie nutzen",
   "- Modellname chat-gaming in ais-mg auf oaapi01, Mandant ais (nutzt gaming)" in t8
   and "- Modellname code in ais-mg" in t8 and "chat-fern" not in t8, t8)
ok("Schoenheitsfehler aus 3a behoben: 'nutzt gamingpc' statt des Berichtsschluessels",
   "Destination gamingpc auf oaapi01, Mandant ais (nutzt gamingpc)" in t8
   and "nutzt oaapi01/ais-ovc/gamingpc" not in t8, t8)
ok("ein Tunnel heisst in der Zeile darunter weiter mit seinem Namen",
   "(nutzt t-9f2c)" in fw.link_text("t-9f2c", fa.link_report({"tun": "down-inner"}, R, O3, {},
                                                              1)["links"]["t-9f2c"], 1000))
src_f = open(os.path.join(HERE, "..", "platform", "services", "twin", "fleet_affected.py"),
             encoding="utf-8").read()
ok("load_graph liest assetOf mit (fuer Mandant und Instanz eines Assets)",
   "BACK + OWNER + ENDS + ASSET_OF" in src_f)

print("=== Katalog ===")
ok("link.down: Gesundheit, Knoten, eine Meldung mit Behoben",
   notify.CATALOGUE.get("link.down", {}).get("matter") is True
   and notify.CATALOGUE["link.down"]["scope"] == "node")
ok("link.down gehört zu den Flottenmeldungen (bleibt offen, wenn die Quelle schweigt)",
   "link.down" in fw.FLEET_MATTERS)
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("die Mail zeigt auf eine Seite", '"link.down": "/health"' in src)

print(f"\n{'ALLE GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
