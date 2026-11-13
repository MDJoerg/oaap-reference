#!/usr/bin/env python3
"""Die Wache der Flotte (RFC-0085 §6, Stufe 2): Urteil und Empfang.

Teil 1 prueft das reine Urteil (platform/services/fleet_watch.py): wann ein
Knoten schweigt, wann er nur unbekannt ist, wann eine Bruecke getrennt ist,
wann eine Warteschlange warnt, wann ein Eingang die Zentrale vermisst.
Teil 2 prueft den Empfang des Flottendienstes (services/twin/fleet.py:
handle) mit gebauten Nachrichten -- er braucht paho-mqtt (im Abbild des
Zwillings vorhanden) und wird sonst uebersprungen, laut.
Teil 3: die Regel des Wach-Benutzers `oaap.watch` und das Abonnieren im
kleinen MQTT-Client des Senders.

Aufruf: python3 test/test_fleet_watch.py
"""
import json
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))

import fleet_watch as fw                                       # noqa: E402
import mqtt_acl                                                # noqa: E402
import metrics_sender as ms                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or detail == "" else f"  -- {str(detail)[:500]}"))


NOW = 1_800_000_000


def alive(age, seq=10, lost=0, version="0.1.271"):
    return {"t": fw.iso(NOW - age), "seq": seq, "version": version, "interval": 60, "lost": lost}


def br(entry, up, since, hub="hub"):
    return {f"{entry}@{hub}": {"entry": entry, "hub": hub, "up": up, "since": since, "rx": since}}


def st(nodes=None, bridges=None, brokers=None, started=NOW - 3600, written=NOW - 5, connected=True):
    return {"started": started, "written": written, "connected": connected,
            "nodes": nodes or {}, "bridges": bridges or {}, "brokers": brokers or {}}


print("=== Teil 1: das Urteil ===")
s = st({"a": {"name": "a", "via": "", "alive": alive(30)},
        "b": {"name": "b", "via": "", "alive": alive(299)},
        "c": {"name": "c", "via": "", "alive": alive(301)}})
c = fw.hub_conditions(s, NOW)
ok("nach 5 Minuten ohne alive: schweigt; davor nicht", set(c) == {"node.silent/c"}, c)
ok("die Meldung nennt den Knoten als Kurzwort und die letzte Zeit",
   c["node.silent/c"]["kurz"] == "c" and fw.iso(NOW - 301) in c["node.silent/c"]["text"], c)
ok("gemessen an der Zeit des KNOTENS, nicht an der Ankunft (ein retained alive luegt nicht)",
   fw.view(st({"x": {"name": "x", "via": "", "alive": alive(900), "rx": NOW}}), NOW)[0]["state"] == "silent")
warm = st({"c": {"name": "c", "via": "", "alive": alive(900)}}, started=NOW - 30)
ok("in den ersten zwei Minuten nach dem Start des Dienstes: unbekannt, keine Meldung",
   fw.hub_conditions(warm, NOW) == {} and fw.view(warm, NOW)[0]["state"] == "unknown")
ok("ein Knoten ohne alive ist unbekannt", fw.view(st({"n": {"name": "n"}}), NOW)[0]["state"] == "unknown")

behind = st({"oaapx01/x": {"name": "x", "via": "oaapx01", "alive": alive(1200)},
             "oaapx01/y": {"name": "y", "via": "oaapx01", "alive": alive(1200)},
             "d": {"name": "d", "via": "", "alive": alive(10)}},
            bridges=br("oaapx01", False, NOW - 1200))
c = fw.hub_conditions(behind, NOW, "hub")
ok("Bruecke getrennt: EINE Meldung, nicht eine je Knoten dahinter",
   set(c) == {"bridge.down/oaapx01"}, c)
ok("sie nennt die Knoten dahinter", "x, y" in c["bridge.down/oaapx01"]["text"], c)
ok("die Knoten dahinter sind 'unbekannt, Eingang getrennt'",
   [r["reason"] for r in fw.view(behind, NOW, "hub") if r["via"]] == ["Eingang oaapx01 getrennt"] * 2)
young = st(bridges=br("e", False, NOW - 299))
ok("eine Bruecke, die erst seit 299 s fehlt, meldet noch nichts", fw.hub_conditions(young, NOW, "hub") == {})
ok("eine Bruecke, die da ist, meldet nichts",
   fw.hub_conditions(st(bridges=br("e", True, NOW - 9999)), NOW, "hub") == {})
upb = st({"e/x": {"name": "x", "via": "e", "alive": alive(1200)}}, bridges=br("e", True, NOW - 9999))
ok("hinter einer stehenden Bruecke schweigt ein Knoten ganz normal",
   set(fw.hub_conditions(upb, NOW, "hub")) == {"node.silent/x"})
back = st({"e/x": {"name": "x", "via": "e", "alive": alive(780)}}, bridges=br("e", True, NOW - 30))
ok("Bruecke seit 30 s zurueck, letztes alive von davor: unbekannt, keine Meldung "
   "(gemessen: sonst eine Minute lang 'schweigt', zwei Mails)",
   fw.hub_conditions(back, NOW, "hub") == {} and fw.view(back, NOW, "hub")[0]["reason"].endswith("gerade zurück"))
back2 = st({"e/x": {"name": "x", "via": "e", "alive": alive(780)}}, bridges=br("e", True, NOW - 121))
ok("nach zwei Minuten ohne neues alive: schweigt doch", set(fw.hub_conditions(back2, NOW, "hub")) == {"node.silent/x"})
# Aufholen nach der Rueckkehr der Zentrale (gemessen 13.11.: oaap-demo 13 Min
# aus, drei direkte Knoten 4-5 Min spaeter zurueck -- je zwei Mails)
cu = st({"d": {"name": "d", "via": "", "alive": alive(1000)}}, started=NOW - 240)
ok("Zentrale seit 4 Min zurueck, letztes alive von davor: unbekannt ('holt auf'), keine Meldung",
   fw.hub_conditions(cu, NOW) == {} and "holt auf" in fw.view(cu, NOW)[0]["reason"], fw.view(cu, NOW))
cu2 = dict(cu, started=NOW - 9999, connected_since=NOW - 240)
ok("dasselbe nach einer Wiederverbindung des Dienstes (connected_since)", fw.hub_conditions(cu2, NOW) == {})
cu3 = st({"d": {"name": "d", "via": "", "alive": alive(1100)}}, started=NOW - fw.CATCHUP - 1)
ok("nach CATCHUP ohne neues alive: schweigt doch", set(fw.hub_conditions(cu3, NOW)) == {"node.silent/d"})
cu4 = st({"d": {"name": "d", "via": "", "alive": alive(400)}}, started=NOW - 600)
ok("ein alive von NACH der Rueckkehr zaehlt normal: schweigt", set(fw.hub_conditions(cu4, NOW)) == {"node.silent/d"})
ok("CATCHUP deckt die laengste Wartezeit des Senders (Zwei Wege, eine Regel)",
   fw.CATCHUP >= ms.BACKOFF_MAX + fw.WARMUP, (fw.CATCHUP, ms.BACKOFF_MAX))
# 'unbekannt' schliesst keine offene Sache
held = {"node.silent/x": {"type": "node.silent", "kurz": "x", "text": "alt"}}
ok("offene Sache, Knoten jetzt hinter getrennter Bruecke: bleibt offen, kein 'Behoben'",
   fw.hub_conditions(behind, NOW, "hub", held).get("node.silent/x") == held["node.silent/x"])
ok("offene Sache, Dienst startet gerade: bleibt offen", fw.hub_conditions(warm, NOW, None,
   {"node.silent/c": {"type": "node.silent"}}) == {"node.silent/c": {"type": "node.silent"}})
ok("offene Sache, Knoten wieder da: schliesst", fw.hub_conditions(s, NOW, None,
   {"node.silent/a": {"type": "node.silent"}}).get("node.silent/a") is None)
hb = {"bridge.down/e": {"type": "bridge.down", "kurz": "e"}}
ok("offene Bruecke, Dienst startet gerade, Bruecke noch weg: bleibt offen",
   "bridge.down/e" in fw.hub_conditions(st(bridges=br("e", False, NOW - 999), started=NOW - 30), NOW, "hub", hb))
ok("offene Bruecke, Bruecke wieder da: schliesst",
   "bridge.down/e" not in fw.hub_conditions(st(bridges=br("e", True, NOW - 10), started=NOW - 30), NOW, "hub", hb))
other = st(bridges=br("hub", False, NOW - 9999, hub="anderer"))
ok("die Bruecke einer ANDEREN Zentrale in diesen Knoten wird hier nicht beurteilt "
   "(gemessen auf oaap-test, das beide Rollen trug)", fw.hub_conditions(other, NOW, "hub") == {})

q = st(brokers={"oaapx01": {"queued": 60001, "max_queued": 100000, "rx": NOW - 30},
                "oaap-demo": {"queued": 400, "max_queued": 1000, "rx": NOW - 30},
                "alt": {"queued": 999, "max_queued": 1000, "rx": NOW - 999}})
c = fw.hub_conditions(q, NOW)
ok("Warteschlange ueber der Haelfte: Warnung; darunter nicht; veraltete Zaehler nicht",
   set(c) == {"broker.queue/oaapx01"}, c)

evs, base = fw.loss_events(st({"a": {"name": "a", "alive": alive(10, lost=5)}}), {})
ok("verlorene Zeilen beim ersten Sehen: nur die Basis", evs == [] and base == {"a": 5})
evs, base = fw.loss_events(st({"a": {"name": "a", "alive": alive(10, lost=8)}}), base)
ok("mehr verloren als zuvor: eine Meldung mit vorher/nachher", evs == [("a", 5, 8)] and base["a"] == 8)
evs, _ = fw.loss_events(st({"a": {"name": "a", "alive": alive(10, lost=8)}}), base)
ok("gleich viele: keine Meldung", evs == [])

ok("vertraut: verbunden und frisch", fw.trusted(st(), NOW))
ok("nicht vertraut: Dienst nicht verbunden", not fw.trusted(st(connected=False), NOW))
ok("nicht vertraut: Zustand aelter als 3 Minuten", not fw.trusted(st(written=NOW - 181), NOW))
ok("nicht vertraut: kein Zustand", not fw.trusted(None, NOW))

w = {"at": NOW - 30, "hubs": {"oaap-demo": {"entry": "oaapx01", "up": False, "since": NOW - 601}}}
c = fw.entry_conditions(w, NOW)
ok("Eingang: Zentrale seit 10 Minuten weg -> hub.unreachable", set(c) == {"hub.unreachable/oaap-demo"}, c)
w["hubs"]["oaap-demo"]["since"] = NOW - 599
ok("seit 9:59 noch nicht", fw.entry_conditions(w, NOW) == {})
ok("eine veraltete Wache: None (offene Sachen bleiben offen)",
   fw.entry_conditions({"at": NOW - 999, "hubs": {}}, NOW) is None)
ok("ohne Zentrale (kein Eingang): nichts", fw.entry_conditions({"at": NOW, "hubs": {}}, NOW) == {})

print("\n=== Teil 2: der Empfang des Flottendienstes ===")
try:
    import paho.mqtt.client  # noqa: F401
    have_paho = True
except ImportError:
    have_paho = False
    print("SKIP  paho-mqtt fehlt hier -- Teil 2 laeuft im Abbild des Zwillings (oaap-test)")
if have_paho:
    os.environ["FLEET_DIR"] = tempfile.mkdtemp(prefix="oaap-fleet-")
    import fleet as fl                                          # noqa: E402
    A = lambda seq, t="2027-01-15T08:00:00Z", lost=0: json.dumps(
        {"v": 1, "t": t, "seq": seq, "version": "0.1.271", "interval": 60, "lost": lost}).encode()
    ok("ein alive eines direkten Knotens", fl.handle("oaap-node/oaap-test/alive", A(5), NOW)
       and fl.state["nodes"]["oaap-test"]["alive"]["seq"] == 5)
    fl.handle("oaap-node/oaap-test/alive", A(5), NOW + 1)
    ok("dasselbe retained alive nochmal: keine Luecke, kein Neubeginn",
       fl.state["nodes"]["oaap-test"]["gaps"] == 0 and fl.state["nodes"]["oaap-test"]["resets"] == 0)
    fl.handle("oaap-node/oaap-test/alive", A(9, "2027-01-15T08:04:00Z"), NOW + 2)
    ok("Folgenummer 5 -> 9: drei fehlen", fl.state["nodes"]["oaap-test"]["gaps"] == 3)
    fl.handle("oaap-node/oaap-test/alive", A(1, "2027-01-15T09:00:00Z"), NOW + 3)
    ok("Folgenummer faellt: der Knoten begann neu (gezaehlt, keine Luecke)",
       fl.state["nodes"]["oaap-test"]["resets"] == 1 and fl.state["nodes"]["oaap-test"]["gaps"] == 3)
    ok("ein alive ueber den Eingang", fl.handle("oaap-via/oaapx01/oaapx01/alive", A(2), NOW)
       and fl.state["nodes"]["oaapx01/oaapx01"]["via"] == "oaapx01")
    ok("Bruecke (Stufe-1-Thema): 1, die Zentrale aus dem Thema",
       fl.handle("oaap-node/oaap-demo/bridge/oaapx01", b"1", NOW)
       and fl.state["bridges"]["oaapx01@oaap-demo"]["up"] is True
       and fl.state["bridges"]["oaapx01@oaap-demo"]["hub"] == "oaap-demo")
    fl.handle("oaap-node/oaap-demo/bridge/oaapx01", b"1", NOW + 50)
    ok("gleicher Wert: 'seit' bleibt", fl.state["bridges"]["oaapx01@oaap-demo"]["since"] == NOW)
    fl.handle("oaap-fleet/bridge/oaapx01/oaap-demo", b"0", NOW + 60)
    ok("Bruecke (oaap-fleet-Thema): 0, 'seit' neu", fl.state["bridges"]["oaapx01@oaap-demo"] ==
       {"entry": "oaapx01", "hub": "oaap-demo", "up": False, "since": NOW + 60, "rx": NOW + 60})
    ok("ein oaap-fleet-Thema ohne Zentrale wird nicht eingeordnet",
       not fl.handle("oaap-fleet/bridge/oaapx01", b"1", NOW))
    ok("eine Meldung landet im Ring",
       fl.handle("oaap-node/raspberrypi/events/disk.full",
                 json.dumps({"op": "open", "kurz": "96 %", "text": "t" * 900}).encode(), NOW)
       and fl.state["events"][-1]["type"] == "disk.full" and len(fl.state["events"][-1]["text"]) == 300)
    for i in range(fl.EVENTS_KEEP + 5):
        fl.handle("oaap-node/x/events/instance.down", b'{"op":"open"}', NOW)
    ok("der Ring ist begrenzt", len(fl.state["events"]) == fl.EVENTS_KEEP)
    ok("Broker-Zaehler", fl.handle("oaap-via/oaapx01/oaapx01/broker",
                                   b'{"queued":3,"max_queued":100000}', NOW)
       and fl.state["brokers"]["oaapx01"]["via"] == "oaapx01")
    ok("ein Statusdokument wird abgelegt", fl.handle("oaap-node/oaap-test/status", b'{"node":"t"}', NOW)
       and os.path.isfile(os.path.join(os.environ["FLEET_DIR"], "status", "oaap-test.json")))
    ok("Unbekanntes wird nicht eingeordnet", not fl.handle("oaap-node/x/metrics/cpu", b"{}", NOW)
       and not fl.handle("anderes/x", b"1", NOW) and not fl.handle("oaap-node/x/alive", b"kein json", NOW))
    fl.write_state()
    ok("state.json ist 0600", oct(os.stat(fl.STATE).st_mode & 0o777) == "0o600" or os.name == "nt")
    view = fw.view(json.load(open(fl.STATE, encoding="utf-8")), fw.parse_t("2027-01-15T09:01:00Z"))
    ok("das Urteil liest, was der Dienst schreibt", {r["name"] for r in view} >= {"oaap-test", "oaapx01"})

print("\n=== Teil 3: oaap.watch und das Abonnieren ===")
R, W, S = mqtt_acl.ACC_READ, mqtt_acl.ACC_WRITE, mqtt_acl.ACC_SUBSCRIBE
ok("liest $SYS/broker/#", mqtt_acl.watch_allows("$SYS/broker/#", S, "oaap-node")[0])
ok("liest die Knoten, den Brueckenzweig, den Flottenbaum",
   all(mqtt_acl.watch_allows(t, S, "oaap-node")[0] for t in
       ("oaap-node/+/alive", "oaap-via/+/+/events/#", "oaap-fleet/bridge/#")))
ok("schreibt nirgends hin", not any(mqtt_acl.watch_allows(t, W, "oaap-node")[0] for t in
                                    ("oaap-node/x/alive", "oaap-fleet/bridge/a/b", "$SYS/broker/x")))
ok("liest keinen Mandantenbaum und nicht alles",
   not mqtt_acl.watch_allows("oaap/abc/#", R, "oaap-node")[0]
   and not mqtt_acl.watch_allows("#", S, "oaap-node")[0]
   and not mqtt_acl.watch_allows("$SYS/#", S, "oaap-node")[0])
ok("eine eigene Wurzel gilt", mqtt_acl.watch_allows("home/n/+/alive", S, "home/n")[0]
   and not mqtt_acl.watch_allows("oaap-node/+/alive", S, "home/n")[0])

pkt = ms.subscribe_packet(["$SYS/broker/#", "a/b"], 7)
ok("SUBSCRIBE: Typ 8 mit Pflichtbits, Paketnummer, QoS 0",
   pkt[0] == 0x82 and struct.unpack(">H", pkt[2:4])[0] == 7 and pkt.endswith(b"a/b\x00"))
body = ms._utf8("$SYS/broker/uptime") + b"\x00" + b"123 seconds"
ok("ein PUBLISH (QoS 0, retained) wird gelesen",
   ms.parse_publish(0x01, body) == ("$SYS/broker/uptime", b"123 seconds", True))
body1 = ms._utf8("t/x") + b"\x00\x05" + b"\x02\x01\x00" + b"pl"
ok("auch mit Paketnummer und Eigenschaften", ms.parse_publish(0x02, body1) == ("t/x", b"pl", False))

import notify                                                  # noqa: E402
ok("Dauer in Worten", [notify.span_words(x) for x in (20, 420, 7500, 3 * 86400 + 4 * 3600)] ==
   ["weniger als eine Minute", "7 Minuten", "2 Stunden 5 Minuten", "3 Tage 4 Stunden"])
b = notify.body("node.silent", "close", {"knoten": "oaap-demo", "kurz": "oaap-bernd",
                                         "dauer": "12 Minuten"}, "x")
ok("'wieder da' (Schliessen) nennt die Dauer", "Behoben: " in b and "Dauer: 12 Minuten" in b, b)
ok("das Oeffnen nennt keine Dauer", "Dauer" not in notify.body(
    "node.silent", "open", {"knoten": "k", "kurz": "n", "dauer": "5 Minuten"}, "x"))
ok("die fuenf Typen der Flotte stehen im Katalog, auf den Knoten bezogen",
   all(notify.CATALOGUE[t]["scope"] == "node" for t in
       ("node.silent", "bridge.down", "broker.queue", "node.loss", "hub.unreachable")))

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
