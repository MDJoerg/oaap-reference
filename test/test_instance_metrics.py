#!/usr/bin/env python3
"""Ein Verlauf je Instanz (RFC-0108, Stufe 1).

Die Sätze, die diese Datei verteidigt:

    - Eine Instanz ist die Summe ihrer Container: CPU, Speicher, Netz.
    - Netz ist die Differenz zur letzten Minute; ein Zähler, der fällt
      (Container neu gestartet), gibt keine Minute.
    - Anfragen, Fehler und Dauer kommen aus der Zählung je Host, über
      alle Adressen der Instanz; 101-Ströme zählen nicht zur Dauer.
    - Die Datengröße wird alle 15 Minuten gemessen und dazwischen gehalten.
    - Eine schlafende Instanz misst 0; eine ohne Werte hat eine Lücke.
    - Die Ablage ist RFC-0051s: dieselben Stufen, aufgerollt und gekürzt;
      ein Monat simulierter Minuten in Sekunden.
    - Die Reihen liegen unter dem Mandanten; eine entfernte Instanz (und
      ein leerer Mandant) verliert ihr Verzeichnis (D4).
    - Das Berichtsbuch bekommt die Größe je Instanz aus dem letzten du.
    - docker stats liefert jetzt NetIO; der Knoten-Sampler gibt seine
      Lesung weiter (`keep`), statt zweimal zu lesen.

Aufruf: python3 test/test_instance_metrics.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# site_stats (0.1.399) keeps open visits in /run -- never the machine's in a test
os.environ.setdefault("OAAP_SITE_RUN_DIR", tempfile.mkdtemp(prefix="oaap-im-run-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import instance_metrics as im  # noqa: E402
import metrics  # noqa: E402
import traffic  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


T0 = 1_795_000_020 - 1_795_000_020 % 60
INST = {"tenant": "t-bec", "containers": ["oaap-app-bec-tools", "oaap-app-bec-tools-db"],
        "hosts": ["tools.bec.node", "tools.example.com"], "dir": "/x", "asleep": False}

print("=== eine Zeile ===")
keep = {"containers": [("oaap-app-bec-tools", 2.0, 100.0, 1000, 500),
                       ("oaap-app-bec-tools-db", 1.5, 50.5, 3000, 1500),
                       ("oaap-app-pxx-chat", 9.0, 999.0, 1, 1)],
        "hosts": {"tools.bec.node": [10, 1, 5000, 4.5, 2.0, 1],
                  "tools.example.com": [20, 0, 9000, 6.0, 1.0, 2],
                  "chat.pxx.node": [99, 0, 1, 1.0, 1.0, 9]},
        "elapsed": 60}
calls = []


def du(path):
    calls.append(path)
    return 123.4


e, st = im.line_for(INST, T0, keep, {}, du)
ok("CPU und Speicher sind die Summe der eigenen Container, nicht fremder",
   e["cpu"] == 3.5 and e["mem"] == 150.5, e)
ok("ohne Vorwert kein Netz (eine Differenz braucht zwei Lesungen)", "rx" not in e, e)
ok("Anfragen, 5xx und Dauer über beide Adressen, 101 nicht in der Dauer",
   e["req"] == 30.0 and e["err"] == 3.0 and e["lat"] == round(10.5 / 29 * 1000, 1), e)
ok("die erste Minute misst die Datengröße", e["disk"] == 123.4 and calls == ["/x"], (e, calls))

keep2 = dict(keep, containers=[("oaap-app-bec-tools", 1.0, 100.0, 7000, 2500),
                               ("oaap-app-bec-tools-db", 1.0, 50.0, 3000, 1500)])
e2, st2 = im.line_for(INST, T0 + 60, keep2, st, du)
ok("Netz ist die Differenz je Sekunde zur letzten Minute",
   e2["rx"] == 100.0 and e2["tx"] == round(2000 / 60, 1), e2)
ok("die Datengröße wird nicht jede Minute gemessen, aber gehalten",
   e2["disk"] == 123.4 and len(calls) == 1, (e2, calls))
keep3 = dict(keep, containers=[("oaap-app-bec-tools", 1.0, 100.0, 10, 10),
                               ("oaap-app-bec-tools-db", 1.0, 50.0, 3000, 1500)])
e3, st3 = im.line_for(INST, T0 + 120, keep3, st2, du)
ok("ein Zähler, der fällt (Neustart), gibt keine Netzminute", "rx" not in e3, e3)
e4, _ = im.line_for(INST, T0 + 120 + im.DISK_EVERY, keep3, st3, du)
ok("nach 15 Minuten wird wieder gemessen", len(calls) == 2, calls)

e5, _ = im.line_for(dict(INST, asleep=True), T0, {"containers": []}, {}, lambda p: None)
ok("eine schlafende Instanz misst 0 -- sie schläft, das ist eine Tatsache",
   e5 == {"t": T0, "cpu": 0.0, "mem": 0.0}, e5)
e6, _ = im.line_for(INST, T0, {"containers": []}, {}, lambda p: None)
ok("eine wache ohne Werte hat eine Lücke, keine 0", e6 is None, e6)

print("\n=== die Ablage: ein Monat in Sekunden ===")
base = tempfile.mkdtemp(prefix="oaap-im-")
targets = {"bec-tools": INST, "pxx-chat": {"tenant": "t-pxx", "containers": ["oaap-app-pxx-chat"],
                                           "hosts": [], "dir": "/y", "asleep": False}}
now = T0
last_roll = last_trim = None
for i in range(31 * 24 * 60 // 10):          # every 10 minutes for a month
    now = T0 + i * 600
    roll = now // metrics.ROLLUP_EVERY != last_roll
    cut = now // metrics.TRIM_EVERY != last_trim
    last_roll, last_trim = now // metrics.ROLLUP_EVERY, now // metrics.TRIM_EVERY
    k = {"containers": [("oaap-app-bec-tools", 2.0, 100.0, i * 600, i * 60),
                        ("oaap-app-bec-tools-db", 1.0, 50.0, 0, 0),
                        ("oaap-app-pxx-chat", 4.0, 10.0, 0, 0)]}
    im.sample(base, now, k, targets, du=lambda p: 10.0 + i / 100, roll=roll, trim=cut)
d = im.instance_dir(base, "t-bec", "bec-tools")
ok("die Reihen liegen unter dem Mandanten", os.path.isfile(os.path.join(d, "raw.jsonl"))
   and os.path.isdir(im.instance_dir(base, "t-pxx", "pxx-chat")), os.listdir(base))
for key, label in (("4h", "4 h"), ("24h", "24 h"), ("1w", "Woche"), ("1m", "Monat")):
    w = im.window(base, "t-bec", "bec-tools", key, now=now)
    ok(f"Fenster {label}: CPU, Speicher, Daten vorhanden",
       w["series"]["cpu"] and w["series"]["mem"] and w["series"]["disk"], {k: len(v) for k, v in w["series"].items()})
w = im.window(base, "t-bec", "bec-tools", "1m", now=now)
ok("der Monat trägt die gemittelten Werte (CPU 3, Speicher 150)",
   abs(w["series"]["cpu"][-1][1] - 3.0) < 0.01 and abs(w["series"]["mem"][-1][1] - 150.0) < 0.01,
   w["series"]["cpu"][-1])
raw = metrics._read(d, "raw")
ok("die Rohstufe ist gekürzt (4 h mit Spielraum, stündlich geprüft)",
   raw[0]["t"] >= now - 4 * 3600 * metrics.TRIM_SLACK - metrics.TRIM_EVERY,
   (now - raw[0]["t"]) / 3600)
ok("zehn Minuten Abstand sind keine Netzminute (mehr als 5 Minuten Lücke)",
   "rx" not in im.latest(base, "t-bec", "bec-tools"), im.latest(base, "t-bec", "bec-tools"))
i += 1
k1 = {"containers": [("oaap-app-bec-tools", 2.0, 100.0, i * 600 + 6000, i * 60 + 600),
                     ("oaap-app-bec-tools-db", 1.0, 50.0, 0, 0)]}
im.sample(base, now + 60, k1, {"bec-tools": INST, "pxx-chat": targets["pxx-chat"]}, du=lambda p: 10.0)
ok("eine Minute später: 100 B/s rein, 10 B/s raus",
   im.latest(base, "t-bec", "bec-tools").get("rx") == round((i * 600 + 6000 - (i - 1) * 600) / 60, 1),
   im.latest(base, "t-bec", "bec-tools"))
size = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
ok("ein Monat einer Instanz ist klein (< 200 KB)", size < 200_000, size)

print("\n=== entfernt: Verzeichnis weg (D4) ===")
im.sample(base, now + 3600, {"containers": []}, {"bec-tools": INST}, du=lambda p: 1.0,
          roll=True, trim=True)
ok("die entfernte Instanz und der leere Mandant sind weg",
   not os.path.exists(im.instance_dir(base, "t-pxx", "pxx-chat"))
   and not os.path.exists(os.path.join(base, "tenants", "t-pxx")), os.listdir(os.path.join(base, "tenants")))
ok("die verbliebene bleibt", os.path.isdir(d))
ok("fremde Dateien im Metrikverzeichnis bleiben unberührt",
   os.path.isdir(base))

print("\n=== Berichtsbuch ===")
b = im.disk_bytes(base, "t-bec")
ok("Größe je Instanz aus dem letzten du, in Bytes",
   b == {"bec-tools": int(1.0 * 1024 * 1024)}, b)
ok("ein Mandant ohne Reihen: leer", im.disk_bytes(base, "t-niemand") == {})
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("appctl schreibt bytes.instances ins Buch und gibt dem Sampler die Instanzen",
   'parts["bytes"]["instances"] = per' in src and "instances=instance_metrics_targets" in src)

print("\n=== docker stats und der Knoten-Sampler ===")
rows = traffic.parse_docker_stats("oaap-app-x\t1.5%\t100MiB / 1GiB\t36.4kB / 1.2MB\n"
                                  "alt\t2%\t1MiB / 1GiB\n")
ok("NetIO wird zu Bytes rein/raus", rows[0] == ("oaap-app-x", 1.5, 100.0, 36400, 1200000), rows)
ok("eine Zeile ohne NetIO bleibt lesbar", rows[1] == ("alt", 2.0, 1.0), rows)
ok("docker stats fragt NetIO ab", "{{.NetIO}}" in open(traffic.__file__, encoding="utf-8").read())
mdir = tempfile.mkdtemp(prefix="oaap-ms-")
log = os.path.join(mdir, "access.log")
open(log, "w").write("")
keep = {}
traffic.measure(mdir, T0, "/nonexistent", log, lambda: [("c", 1.0, 2.0, 3, 4)], keep)
open(log, "a").write(json.dumps({"status": 200, "size": 10, "duration": 0.1,
                                 "request": {"host": "a.node"}}) + "\n")
keep = {}
traffic.measure(mdir, T0 + 60, "/nonexistent", log, lambda: [("c", 1.0, 2.0, 3, 4)], keep)
ok("measure gibt Container, Host-Zählung und Dauer weiter",
   keep.get("containers") == [("c", 1.0, 2.0, 3, 4)] and keep.get("hosts", {}).get("a.node", [0])[0] == 1
   and keep.get("elapsed") == 60, keep)
seen = {}
mdir2 = tempfile.mkdtemp(prefix="oaap-ms2-")
metrics.take_sample(mdir2, mdir2, now=T0, proc="/nonexistent", sleep=lambda s: None,
                    instances=lambda: seen.setdefault("called", {}))
ok("take_sample fragt die Instanzen (eine Funktion, kein Lesen beim Import)",
   "called" in seen, seen)
metrics.take_sample(mdir2, mdir2, now=T0 + 60, proc="/nonexistent", sleep=lambda s: None,
                    instances=lambda: 1 / 0)
ok("eine Instanzliste, die wirft, kostet die Knotenzeile nicht",
   metrics._read(mdir2, "raw") is not None)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
