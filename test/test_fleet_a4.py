#!/usr/bin/env python3
"""RFC-0085 Nachtrag A4 (§18): was der Ausfall von oaapx02 am 24.11.2026 lehrte.

1. Wachhund: eine Brücke, die zehn Minuten getrennt ist und es nicht einmal
   mehr versucht, wird angestoßen (Broker der Zentrale neu) -- mit Abstand.
2. Erinnerung: die Lagen der Flotte kommen täglich wieder, nicht nur einmal.
3. Urteil am Eingang: der Eingang meldet seine Knoten selbst, solange ihre
   Zentrale fehlt; die Zentrale überlässt sie ihm danach.
4. Rückstau: die Brücke der Zentrale in ihrer neuen Form.

Aufruf: python3 test/test_fleet_a4.py
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import fleet_pair                                              # noqa: E402
import fleet_watch as fw                                       # noqa: E402
import notify                                                  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or detail == "" else f"  -- {str(detail)[:600]}"))


NOW = 1_800_000_000


def alive(age):
    return {"t": fw.iso(NOW - age), "seq": 1, "version": "0.1.377", "interval": 60, "lost": 0}


def hub_state(up, since, nodes=None, brokers=None):
    return {"started": NOW - 86400, "written": NOW - 5, "connected": True,
            "connected_since": NOW - 86400,
            "nodes": nodes or {}, "brokers": brokers or {},
            "bridges": {"oaapx01@oaap-demo": {"entry": "oaapx01", "hub": "oaap-demo",
                                              "up": up, "since": since, "rx": since}}}


print("=== 1. Wachhund ===")
log = ("1795556402: Connecting bridge entry-oaapx01 (oaap.joomp.de:8883)\n"
       "1795556458: Connecting bridge entry-oaapx01 (oaap.joomp.de:8883)\n"
       "1795556460: Connecting bridge entry-hsp (hsp.btocs.de:8883)\n"
       "1795556461: New connection from 10.0.0.1:1 on port 1883.\n"
       "Connecting bridge entry-x ohne Zeit\n")
ok("das Log nennt je Brücke ihren letzten Versuch",
   fw.bridge_attempts(log) == {"oaapx01": 1795556458, "hsp": 1795556460}, fw.bridge_attempts(log))

s = hub_state(False, NOW - 4 * 86400)
ok("vier Tage getrennt, kein Versuch im Log -> anstoßen",
   fw.kick_due(s, NOW, "oaap-demo", {}, {}) == ["oaapx01"])
ok("aber nicht, wenn sie es noch versucht (der Eingang hängt, nicht die Brücke)",
   fw.kick_due(s, NOW, "oaap-demo", {"oaapx01": NOW - 50}, {}) == [])
ok("ein alter Versuch (vor über zehn Minuten) zählt nicht",
   fw.kick_due(s, NOW, "oaap-demo", {"oaapx01": NOW - 700}, {}) == ["oaapx01"])
ok("erst nach zehn Minuten Trennung",
   fw.kick_due(hub_state(False, NOW - 500), NOW, "oaap-demo", {}, {}) == [])
ok("eine stehende Brücke nie", fw.kick_due(hub_state(True, NOW - 9999), NOW, "oaap-demo", {}, {}) == [])
ok("nur die eigenen Brücken (eine andere Zentrale ist nicht unsere Sache)",
   fw.kick_due(s, NOW, "andere-zentrale", {}, {}) == [])
conf_texts = ["connection entry-oaapx01\naddress x:8883\n", "max_queued_messages 1000\n"]
ok("die konfigurierten Brücken stehen in conf.d", fw.bridge_connections(conf_texts) == {"oaapx01"})
ok("eine nur noch gehaltene '0' ohne Brücke in conf.d -> nie anstoßen (gemessen: sim-entry)",
   fw.kick_due(s, NOW, "oaap-demo", {}, {}, configured=set()) == []
   and fw.kick_due(s, NOW, "oaap-demo", {}, {}, configured={"oaapx01"}) == ["oaapx01"])
k1 = {"oaapx01": {"at": NOW - 1000, "n": 1}}
ok("nach dem ersten Anstoß 30 Minuten Ruhe", fw.kick_due(s, NOW, "oaap-demo", {}, k1) == [])
k1b = {"oaapx01": {"at": NOW - 1801, "n": 1}}
ok("danach wieder", fw.kick_due(s, NOW, "oaap-demo", {}, k1b) == ["oaapx01"])
k3 = {"oaapx01": {"at": NOW - 7000, "n": 3}}
ok("der Abstand verdoppelt sich (dritter: zwei Stunden)",
   fw.kick_backoff(3) == 7200 and fw.kick_due(s, NOW, "oaap-demo", {}, k3) == [])
ok("höchstens sechs Stunden", fw.kick_backoff(10) == 6 * 3600)
kold = {"oaapx01": {"at": NOW - 5 * 86400, "n": 9}}
ok("ein Anstoß aus einer früheren Trennung bremst nicht",
   fw.kick_due(s, NOW, "oaap-demo", {}, kold) == ["oaapx01"])
ok("eine wieder stehende Brücke vergisst ihre Anstöße",
   fw.kicks_after(hub_state(True, NOW - 60), "oaap-demo", k3) == {}
   and fw.kicks_after(s, "oaap-demo", k3) == k3)
c = fw.hub_conditions(s, NOW, "oaap-demo", kicks={"oaapx01": {"at": NOW - 600, "n": 2}})
t = c["bridge.down/oaapx01"]["text"]
ok("die Meldung der Brücke sagt, was der Wachhund tat", "2-mal neu gestartet" in t, t)
ok("bridge.down nur für konfigurierte Brücken (eine verwaiste '0' erinnerte sonst täglich)",
   "bridge.down/oaapx01" not in fw.hub_conditions(s, NOW, "oaap-demo", configured=set())
   and "bridge.down/oaapx01" in fw.hub_conditions(s, NOW, "oaap-demo", configured={"oaapx01"}))
c = fw.hub_conditions(s, NOW, "oaap-demo", kicks={"oaapx01": {"at": NOW - 5 * 86400, "n": 2}})
ok("einen Anstoß aus früherer Trennung nicht", "Wachhund" not in c["bridge.down/oaapx01"]["text"])

print("\n=== 2. Erinnerung ===")
t0 = datetime.datetime(2026, 11, 20, 8, 38, tzinfo=datetime.timezone.utc)
for typ in ("bridge.down", "node.silent", "link.down", "broker.queue"):
    cur = {f"{typ}/x": {"type": typ, "kurz": "x", "text": "t"}}
    _o, st = notify.matter_ops(cur, {}, t0)
    o1, st = notify.matter_ops(cur, st, t0 + datetime.timedelta(hours=23))
    o2, st = notify.matter_ops(cur, st, t0 + datetime.timedelta(hours=24))
    o3, st = notify.matter_ops(cur, st, t0 + datetime.timedelta(hours=47))
    o4, st = notify.matter_ops(cur, st, t0 + datetime.timedelta(hours=48))
    ok(f"{typ}: nach 24 h eine Erinnerung, dann wieder nach 24 h",
       o1 == [] and [x[0] for x in o2] == ["remind"] and o3 == []
       and [x[0] for x in o4] == ["remind"], (o1, o2, o3, o4))
cur = {"disk.full/x": {"type": "disk.full", "text": "t"}}
_o, st = notify.matter_ops(cur, {}, t0)
o, _ = notify.matter_ops(cur, st, t0 + datetime.timedelta(hours=30))
ok("andere Gesundheits-Lagen bleiben wie entschieden (RFC-0080 D6)", o == [])
ok("kritische weiter täglich", notify.remind_hours("hub.unreachable") == 24
   and notify.remind_hours("backup.failed") == 24 and notify.remind_hours("disk.full") is None)
b = notify.body("bridge.down", "remind", {"knoten": "oaap-demo", "kurz": "oaapx01"}, "x")
ok("die Erinnerung sagt, dass sie täglich kommt",
   b.startswith("Besteht weiterhin: ") and "einmal täglich" in b, b)
ok("ihr Betreff", notify.subject_for("remind", "[oaap-demo] Brücke getrennt: oaapx01")
   == "Erinnerung: [oaap-demo] Brücke getrennt: oaapx01")

print("\n=== 3. Urteil am Eingang ===")


def watch(hub_up, hub_since, nodes, uptime=86400, held=None):
    return {"at": NOW - 5, "uptime": uptime, "held": held or [],
            "hubs": {"oaap-demo": {"entry": "oaapx01", "up": hub_up, "since": hub_since}},
            "nodes": nodes}


away = NOW - 4 * 86400        # the hub left four days ago (20.11.)
w = watch(False, away, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 600)},
                        "frisch": {"hub": "oaap-demo", "t": fw.iso(NOW - 30)}})
c = fw.entry_conditions(w, NOW)
ok("Zentrale weg, Knoten schweigt seit 10 Minuten -> der Eingang meldet ihn",
   "node.silent/oaapx02" in c and "node.silent/frisch" not in c, sorted(c))
ok("dazu hub.unreachable wie bisher", "hub.unreachable/oaap-demo" in c)
txt = c["node.silent/oaapx02"]["text"]
ok("der Text sagt, warum der Eingang meldet", "oaap-demo" in txt and "Eingang" in txt, txt)
ok("held nennt den Knoten für die Zentrale", fw.entry_held_names(c) == ["oaapx02"])

w = watch(False, NOW - 300, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 600)}})
c = fw.entry_conditions(w, NOW)
ok("die Zentrale erst fünf Minuten weg -> noch nichts (sie urteilte bis eben)",
   "node.silent/oaapx02" not in c, sorted(c))
w = watch(False, NOW - 3600, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 2 * 86400)}})
c = fw.entry_conditions(w, NOW)
ok("schwieg schon, bevor die Zentrale ging -> war die Sache der Zentrale, nicht doppelt",
   "node.silent/oaapx02" not in c, sorted(c))
w = watch(False, NOW - 3600, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 3600 - 200)}})
c = fw.entry_conditions(w, NOW)
ok("verstummte kurz bevor die Zentrale ging (konnte noch nicht urteilen) -> Eingang meldet",
   "node.silent/oaapx02" in c, sorted(c))
w = watch(True, NOW - 60, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 900)}})
c = fw.entry_conditions(w, NOW)
ok("Zentrale verbunden -> der Eingang schweigt (die Zentrale urteilt)", c == {}, c)
held = {"node.silent/oaapx02": {"type": "node.silent", "kurz": "oaapx02", "text": "alt"}}
c = fw.entry_conditions(w, NOW, held)
ok("eine eigene offene Lage bleibt beim Eingang, auch wenn die Zentrale zurück ist",
   "node.silent/oaapx02" in c)
w2 = watch(True, NOW - 60, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 20)}})
ok("bis der Knoten wieder da ist -> geschlossen (Behoben ist dann wahr)",
   fw.entry_conditions(w2, NOW, held) == {})
w3 = watch(True, NOW - 60, {"oaapx02": {"hub": "oaap-demo", "t": None}})
ok("kein Lebenszeichen mehr im Broker (unbekannt) -> die Lage bleibt, wie sie war",
   fw.entry_conditions(w3, NOW, held) == held)
w4 = watch(False, away, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 900)}}, uptime=120)
ok("der Broker des Eingangs gerade neu -> die Sender holen auf, noch kein Urteil",
   "node.silent/oaapx02" not in fw.entry_conditions(w4, NOW))
ok("ein veraltetes Wachprotokoll urteilt nicht", fw.entry_conditions(dict(w, at=NOW - 999), NOW) is None)
ok("ohne gekoppelte Knoten wie bisher",
   set(fw.entry_conditions({"at": NOW, "hubs": {}}, NOW)) == set())

print("\n=== 3b. Die Zentrale überlässt es dem Eingang ===")
nodes = {"oaapx01/oaapx02": {"name": "oaapx02", "via": "oaapx01", "alive": alive(1200)}}
s = hub_state(True, NOW - 1800, nodes,
              brokers={"oaapx01": {"rx": NOW - 30, "held": ["oaapx02"]}})
r = fw.view(s, NOW, "oaap-demo")[0]
ok("die Zentrale zeigt ihn als schweigend, Eigentümer der Eingang",
   r["state"] == "silent" and r["owner"] == "oaapx01" and "Eingang" in r["reason"], r)
c = fw.hub_conditions(s, NOW, "oaap-demo")
ok("und öffnet keine eigene Lage", "node.silent/oaapx02" not in c, sorted(c))
mine = {"node.silent/oaapx02": {"type": "node.silent", "kurz": "oaapx02", "text": "zentrale"}}
c = fw.hub_conditions(s, NOW, "oaap-demo", mine)
ok("eine Lage, die die Zentrale vorher selbst öffnete, bleibt offen (kein falsches Behoben)",
   c.get("node.silent/oaapx02") == mine["node.silent/oaapx02"])
s2 = hub_state(True, NOW - 1800, nodes, brokers={"oaapx01": {"rx": NOW - 999, "held": ["oaapx02"]}})
ok("ein veraltetes Brokerdokument des Eingangs gilt nicht -> die Zentrale meldet selbst",
   "node.silent/oaapx02" in fw.hub_conditions(s2, NOW, "oaap-demo"))
s3 = hub_state(False, NOW - 1800, nodes, brokers={"oaapx01": {"rx": NOW - 30, "held": []}})
c = fw.hub_conditions(s3, NOW, "oaap-demo")
ok("Brücke getrennt: die Meldung sagt, dass der Eingang für die Knoten dahinter spricht",
   "meldet es der Eingang oaapx01" in c["bridge.down/oaapx01"]["text"], c["bridge.down/oaapx01"]["text"])
v = fw.verdict(s, NOW, "oaap-demo", {})
ok("der Zwilling sieht dasselbe Urteil (schweigt)", v["nodes"]["oaapx02"]["state"] == "silent")

print("\n=== 4. Die Brücke der Zentrale ===")
conf = fleet_pair.bridge_conf("oaapx01", "oaap-demo", "mqtts://oaap.joomp.de", "k1", "geheim")
ok("trägt ihre Fassung (für das Nachziehen alter Dateien)",
   fleet_pair.BRIDGE_FORM in conf and fleet_pair.bridge_form(conf) == fleet_pair.BRIDGE_FORM)
for line in fleet_pair.BRIDGE_LINES:
    ok(f"Zeile '{line}'", "\n" + line + "\n" in conf, conf)
old = conf.replace(fleet_pair.BRIDGE_FORM, "").replace("keepalive_interval 120", "keepalive_interval 30")
ok("eine alte Datei hat keine Fassung", fleet_pair.bridge_form(old) is None)
f = fleet_pair.bridge_fields(old)
ok("aus einer alten Datei lassen sich die Felder lesen",
   f == {"entry": "oaapx01", "hub": "oaap-demo", "broker": "mqtts://oaap.joomp.de:8883",
         "user": "k1", "secret": "geheim", "cafile": "/etc/ssl/certs/ca-certificates.crt"}, f)
ok("und sie neu schreiben -- gleiche Schlüssel, neue Form",
   fleet_pair.bridge_conf(**f) == fleet_pair.bridge_conf(
       "oaapx01", "oaap-demo", "mqtts://oaap.joomp.de:8883", "k1", "geheim"))
hand = "connection entry-oaapx01\naddress oaap.joomp.de:8883\n"
ok("eine von Hand geschriebene Datei (ohne Kopf der Kopplung) wird nicht angefasst",
   fleet_pair.bridge_fields(hand) is None)
tpl = open(os.path.join(HERE, "..", "platform", "services", "broker", "mosquitto.conf.template"),
           encoding="utf-8").read()
ok("der Broker merkt sich identitys Antworten 60 s (go-auth-Cache), hinter auth_plugin",
   all(x in tpl for x in ("auth_opt_cache true", "auth_opt_cache_type go-cache",
                          "auth_opt_auth_cache_seconds 60", "auth_opt_acl_cache_seconds 60"))
   and tpl.index("auth_plugin ") < tpl.index("auth_opt_cache true"))
mig = open(os.path.join(HERE, "..", "platform", "migrate.sh"), encoding="utf-8").read()
ok("das Update startet einen laufenden Broker ohne Cache einmal neu",
   "grep -q '^auth_opt_cache true' /tmp/mosquitto.conf" in mig)

print("\n=== 6. Platten-Vorhersage (disk.trend / disk.soon) ===")
import disk_trend as dt                                        # noqa: E402


def series(rate_per_h, hours, start=50.0, step=300, accel=None):
    """5-minute points ending at NOW; accel: f(age_h) -> rate at that age."""
    out, v = [], start
    n = int(hours * 3600 / step)
    for i in range(n + 1):
        t = NOW - (n - i) * step
        age_h = (NOW - t) / 3600
        r = accel(age_h) if accel else rate_per_h
        v += r * step / 3600
        out.append({"t": t, "disk": [5, round(v, 3), v, v]})
    return out


flat = series(0.0, 30)
ok("eine ruhige Platte: keine Vorhersage, keine Lage",
   dt.forecast(dt.points([[], flat]), NOW)["hours"] is None
   and dt.conditions(dt.forecast(dt.points([[], flat]), NOW)) == {})
noisy = [dict(e, disk=[5, 50 + (0.05 if i % 2 else -0.05), 0, 0]) for i, e in enumerate(flat)]
ok("Rauschen ohne Zuwachs (unter 0,25 Punkten am Tag) zählt nicht",
   dt.forecast(dt.points([[], noisy]), NOW)["hours"] is None)
ok("zu wenig Daten (2 Stunden): kein Urteil", dt.forecast(dt.points([[], series(1.0, 2)]), NOW) is None)
lin = series(1.0, 30, start=50)               # 1 point/h -> at ~80 %, 20 h left
fc = dt.forecast(dt.points([[], lin]), NOW)
ok("linear: 1 Punkt je Stunde bei ~80 % -> etwa 20 Stunden",
   fc["how"] == "linear" and 19 < fc["hours"] < 21 and fc["exp_h"] is None, fc)
c = dt.conditions(fc, total_bytes=500e9)
ok("unter 48 h: Warnung disk.trend, noch nicht kritisch",
   set(c) == {"disk.trend"} and "20 Stunden" in c["disk.trend"]["kurz"], c)
ok("der Text nennt Zuwachs am Tag in GB", "GB am Tag" in c["disk.trend"]["text"]
   and "docker system df" in c["disk.trend"]["text"])
fast = series(3.0, 26, start=10)              # ends at ~88 %, 4 h left
c = dt.conditions(dt.forecast(dt.points([[], fast]), NOW))
ok("unter 12 h: dazu kritisch disk.soon", set(c) == {"disk.trend", "disk.soon"}, c)
fcw = {"pct": 70, "hours": 55, "how": "linear", "rate_day": 13, "linear_h": 55, "exp_h": None}
ok("Hysterese: 55 h öffnet keine Warnung ...", dt.conditions(fcw) == {})
ok("... hält aber eine offene (bis 60 h)", set(dt.conditions(fcw, {"disk.trend"})) == {"disk.trend"})
ok("eine offene kritische hält bis 18 h", set(dt.conditions(dict(fcw, hours=15), {"disk.soon"}))
   == {"disk.trend", "disk.soon"} and set(dt.conditions(dict(fcw, hours=15))) == {"disk.trend"})
week = series(0.02, 7 * 24, start=40, step=1800,
              accel=lambda a: 0.02 if a > 24 else (0.08 if a > 6 else 0.4))
fine = [e for e in week if e["t"] > NOW - 24 * 3600]
fc = dt.forecast(dt.points([week, fine]), NOW)
ok("beschleunigt (7 d < 24 h < 6 h, je deutlich): auch exponentiell geschätzt, die frühere zählt",
   fc["exp_h"] is not None and fc["hours"] == min(fc["exp_h"], fc["linear_h"] or 1e9)
   and fc["exp_h"] < (fc["linear_h"] or 1e9), fc)
steady = series(0.05, 7 * 24, start=40, step=1800)
fc = dt.forecast(dt.points([steady, [e for e in steady if e["t"] > NOW - 86400]]), NOW)
ok("gleichmäßig: keine exponentielle Schätzung", fc["exp_h"] is None, fc)
ok("die feinere Stufe ersetzt die gröbere, wo beide sind",
   dt.points([[{"t": NOW - 100, "disk": [30, 1, 1, 1]}, {"t": NOW - 7200, "disk": [30, 9, 9, 9]}],
              [{"t": NOW - 3000, "disk": [5, 5, 5, 5]}]]) == [(NOW - 7200, 9.0), (NOW - 3000, 5.0)])
ok("Katalog: disk.trend Gesundheit, disk.soon kritisch (täglich), nie nur im Bericht",
   notify.CATALOGUE["disk.trend"]["category"] == "health"
   and notify.CATALOGUE["disk.soon"]["category"] == "critical"
   and notify.remind_hours("disk.soon") == 24 and "disk.soon" in notify.NEVER_REPORT_ONLY)

print("\n=== 7. Wichtigkeit von Mails ===")
ok("kritisch beginnt: rot", notify.marked_subject("disk.soon", "open", "[k] x").startswith("\U0001F534 "))
ok("ein kritisches Ereignis: rot", notify.subject_mark("backup.failed", "open") == notify.MARK_CRITICAL)
ok("Gesundheit beginnt: orange", notify.subject_mark("bridge.down", "open") == notify.MARK_WARNING)
ok("Erinnerung: Kreislauf", notify.subject_mark("bridge.down", "remind") == notify.MARK_REMIND)
ok("Behoben: Haken", notify.subject_mark("node.silent", "close") == notify.MARK_CLOSED)
ok("sparsam: Betrieb, Sicherheitshinweis, Wochenbericht, Probe ohne Symbol",
   all(notify.subject_mark(t, o) == "" for t, o in (("reboot.planned", "event"),
       ("advisory.host", "open"), ("report.weekly", "event"), ("node.loss", "event"),
       ("disk.full", "test"))))
ok("nur kritisch bekommt Importance/X-Priority",
   notify.importance_headers("hub.unreachable", "open") == {"Importance": "high", "X-Priority": "1"}
   and notify.importance_headers("hub.unreachable", "remind")
   and notify.importance_headers("bridge.down", "open") == {}
   and notify.importance_headers("disk.soon", "close") == {})

try:
    import fcntl  # noqa: F401 -- appctl needs it
except ImportError:
    print("\n(5 übersprungen: braucht Linux)")
    print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
    sys.exit(1 if fails else 0)

print("\n=== 5. appctl: alte Brückendateien, Wachhund, Eingang ===")
import json                                                    # noqa: E402
import tempfile                                                # noqa: E402

os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-a4-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import appctl as a                                             # noqa: E402

cd, kd = tempfile.mkdtemp(), tempfile.mkdtemp()
with open(os.path.join(cd, "bridge-oaapx01.conf"), "w", encoding="utf-8") as fh:
    fh.write(old)
with open(os.path.join(cd, "bridge-hand.conf"), "w", encoding="utf-8") as fh:
    fh.write(hand)
done = a.bridge_files_upgrade(cd, kd, now=NOW)
new = open(os.path.join(cd, "bridge-oaapx01.conf"), encoding="utf-8").read()
ok("die alte Datei der Kopplung wird in neuer Form geschrieben, mit denselben Schlüsseln",
   done == ["oaapx01"] and fleet_pair.bridge_form(new) == fleet_pair.BRIDGE_FORM
   and "remote_password geheim" in new and "local_cleansession true" in new, (done, new))
ok("0600", oct(os.stat(os.path.join(cd, "bridge-oaapx01.conf")).st_mode & 0o777) == "0o600")
ok("die alte Fassung liegt daneben in data/fleet-pair",
   os.listdir(kd) == [f"bridge-oaapx01.conf.before-{NOW}"])
ok("die Datei von Hand bleibt, wie sie war",
   open(os.path.join(cd, "bridge-hand.conf"), encoding="utf-8").read() == hand)
ok("ein zweiter Lauf ändert nichts (kein zweiter Neustart)", a.bridge_files_upgrade(cd, kd) == [])

calls = []
logtext = {"v": ""}


def fake_run(cmd, *args, **kw):
    calls.append(cmd)

    class R:
        stdout, stderr, returncode = "", "", 0
    return R()


a.run = fake_run
a._broker_log_since = lambda s: logtext["v"]
a.fleet_node_name = lambda: "oaap-demo"
a.audit_fleet = lambda e: calls.append(["audit", e["event"]])
a._broker_conf_texts = lambda: ["connection entry-oaapx01\n"]
s = hub_state(False, NOW - 4 * 86400)
k = a.fleet_bridge_watchdog(s, NOW)
ok("Wachhund: Brücke hängt ohne Versuch -> docker restart oaap-broker-1, Zählung 1",
   ["docker", "restart", "oaap-broker-1"] in calls and k["oaapx01"] == {"at": NOW, "n": 1}
   and ["audit", "bridge.kick"] in calls, (calls, k))
ok("steht in data/fleet/kicks.json", json.load(open(a.FLEET_KICKS)) == k)
calls.clear()
ok("eine Minute später nichts (Abstand)", a.fleet_bridge_watchdog(s, NOW + 60) == k and calls == [])
k2 = a.fleet_bridge_watchdog(s, NOW + 1801)
ok("nach 30 Minuten der zweite Anstoß", k2["oaapx01"] == {"at": NOW + 1801, "n": 2})
calls.clear()
logtext["v"] = f"{NOW + 9000}: Connecting bridge entry-oaapx01 (x:8883)\n"
a.fleet_bridge_watchdog(s, NOW + 9050)
ok("versucht sie es wieder -> kein Neustart", calls == [])
ok("steht sie wieder -> die Zählung ist weg",
   a.fleet_bridge_watchdog(hub_state(True, NOW + 9100), NOW + 9200) == {})

wf = a.FLEET_WATCH_FILE
os.makedirs(os.path.dirname(wf), exist_ok=True)
w = watch(False, away, {"oaapx02": {"hub": "oaap-demo", "t": fw.iso(NOW - 600)}})
with open(wf, "w", encoding="utf-8") as fh:
    json.dump(w, fh)
a._entry_held_write(w, ["oaapx02"])
ok("der Eingang schreibt held ins Wachprotokoll (geht mit dem Brokerdokument zur Zentrale)",
   json.load(open(wf))["held"] == ["oaapx02"])

import smtplib                                                 # noqa: E402
sent = []


class FakeSMTP:
    def __init__(self, *args, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def send_message(self, msg, to_addrs=None):
        sent.append(msg)
        return {}


smtplib.SMTP = FakeSMTP
box = {"from": "oaap@example.org", "host": "localhost", "port": 25, "tls": "none"}
r = {"to": ["a@example.org"], "cc": [], "bcc": []}
a.notify_smtp(box, "OAAP", r, "\U0001F534 [k] x", "b", headers=notify.importance_headers("disk.soon", "open"))
a.notify_smtp(box, "OAAP", r, "\U0001F7E0 [k] y", "b", headers=notify.importance_headers("disk.trend", "open"))
ok("die echte Mail: kritisch mit Importance: high und X-Priority: 1",
   sent[0]["Importance"] == "high" and sent[0]["X-Priority"] == "1"
   and sent[0]["Subject"].startswith("\U0001F534"), dict(sent[0].items()))
ok("Warnung ohne diese Kopfzeilen", sent[1]["Importance"] is None and sent[1]["X-Priority"] is None)

mdir = a.METRICS_DIR
os.makedirs(mdir, exist_ok=True)
with open(os.path.join(mdir, "5m.jsonl"), "w", encoding="utf-8") as fh:
    for e in series(1.0, 30, start=50):
        fh.write(json.dumps(e) + "\n")
c = a.disk_trend_conditions(None, set(), now=NOW)
ok("appctl liest die Stufen des Knotens und meldet disk.trend", set(c) == {"disk.trend"}, c)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
