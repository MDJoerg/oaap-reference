#!/usr/bin/env python3
"""Knoten-Metriken: Messen, gestaffelte Ablage, Fenster (RFC-0051, Stufe 1).

Die Uhr ist simuliert: ein ganzer Monat Minuten laeuft in Sekunden durch
den ECHTEN Weg (`take_sample`), mit einem falschen /proc, das der Test
Minute fuer Minute beschreibt. Geprueft wird, was die Spezifikation
verspricht und was am ehesten still schiefginge:

    Spitzen ueberleben die Staffelung (Maximum), Luecken bleiben Luecken,
    die Ablage waechst nicht ueber ihre Grenzen, ein doppelter Aufruf in
    derselben Minute schreibt nichts, und das Format ist das der Spec.

Braucht kein Docker, keinen Knoten und kein Flask.

Aufruf: python3 test/test_metrics.py
"""
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import metrics as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


BASE = 1_790_000_000 - 1_790_000_000 % 7200      # an interval edge of every tier
PROC = tempfile.mkdtemp(prefix="oaap-metrics-proc-")
STORE = tempfile.mkdtemp(prefix="oaap-metrics-store-")
DATA = tempfile.mkdtemp(prefix="oaap-metrics-data-")


def proc(total, idle, mem_total=1000, mem_avail=500):
    with open(os.path.join(PROC, "stat"), "w") as f:
        f.write(f"cpu  {total - idle} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 2 3\n")
    with open(os.path.join(PROC, "meminfo"), "w") as f:
        f.write(f"MemTotal:       {mem_total} kB\nMemFree: 1 kB\n"
                f"MemAvailable:   {mem_avail} kB\n")


def sample(now, **kw):
    return m.take_sample(STORE, DATA, now=now, proc=PROC,
                         sleep=lambda s: None)


print("=== die Messwerte ===")
ok("CPU: halb ausgelastet zwischen zwei Staenden",
   m.cpu_percent((1000, 500), (1200, 600)) == 50.0)
ok("CPU: kein Fortschritt der Zaehler ist keine Messung",
   m.cpu_percent((1000, 500), (1000, 500)) is None)
ok("CPU: ein Zaehler, der rueckwaerts springt (Neustart), ist keine Messung",
   m.cpu_percent((1000, 500), (100, 50)) is None)
proc(1000, 500, 1000, 250)
ok("Arbeitsspeicher: 75 Prozent belegt bei 250 von 1000 verfuegbar",
   m.read_mem_percent(PROC) == 75.0)
ok("Arbeitsspeicher: ohne /proc kein Wert", m.read_mem_percent("/nichts") is None)
d = m.read_disk_percent(DATA)
ok("Platte: ein Wert zwischen 0 und 100 (nur wo es statvfs gibt)",
   not hasattr(os, "statvfs") or (d is not None and 0 <= d <= 100), d)
ok("Platte: ein Pfad, den es nicht gibt, ist kein Wert",
   m.read_disk_percent("/nichts/da") is None)

print("\n=== die erste Messung ===")
proc(10_000, 9_000)
calls = []
e = m.take_sample(STORE, DATA, now=BASE, proc=PROC,
                  sleep=lambda s: (calls.append(s), proc(10_100, 9_050))[0])
ok("ohne Vorwert wird zweimal gelesen, eine Sekunde auseinander",
   calls == [1], calls)
ok("... und der Wert stimmt (100 Schritte, davon 50 frei = 50 Prozent)",
   e and e.get("cpu") == 50.0, e)
ok("der Eintrag liegt auf der vollen Minute", e and e["t"] % 60 == 0, e)
ok("ein zweiter Aufruf in derselben Minute schreibt nichts",
   m.take_sample(STORE, DATA, now=BASE + 20, proc=PROC,
                 sleep=lambda s: None) is None
   and len(m._read(STORE, "raw")) == 1)

print("\n=== ein Monat, eine Minute nach der anderen ===")
# Auslastung 20 Prozent, Speicher 40, ausser: eine Spitze von 100 Prozent
# CPU in genau EINER Minute (Tag 3), und ein Ausfall (Tag 10 bis 11).
SPIKE = BASE + 3 * 86400 + 5 * 3600 + 17 * 60
SPIKE2 = BASE + 29 * 86400 + 5 * 3600 + 17 * 60       # innerhalb der letzten Woche
OFF = (BASE + 10 * 86400, BASE + 11 * 86400)
total, idle = 10_100, 9_050
n = 0
t = BASE + 60
END = BASE + 31 * 86400 + 3600
while t <= END:
    if not (OFF[0] <= t < OFF[1]):
        total += 6000                       # 60 s * 100 Schritte
        idle += 0 if t in (SPIKE, SPIKE2) else 4800   # 20 Prozent, sonst 100
        proc(total, idle, 1000, 600)
        sample(t)
        n += 1
    else:
        # der Knoten laeuft nicht; die Zaehler setzen danach neu an
        total, idle = 500, 400
    t += 60
print(f"      {n} Minuten gemessen")

sizes = {tier: os.path.getsize(m._path(STORE, tier))
         for tier, _s, _k in m.TIERS}
counts = {tier: len(m._read(STORE, tier)) for tier, _s, _k in m.TIERS}
print(f"      Zeilen {counts}, Bytes {sizes}")
for tier, step, keep in m.TIERS:
    ok(f"{tier}: nicht mehr Zeilen als Aufbewahrung plus Zugabe",
       counts[tier] <= keep // step * m.TRIM_SLACK + 2, counts[tier])
ok("die ganze Ablage bleibt unter einem Megabyte",
   sum(sizes.values()) < 1_000_000, sum(sizes.values()))
ok("es gibt keine liegengebliebene Zwischendatei",
   not [f for f in os.listdir(STORE) if f.endswith(".tmp")], os.listdir(STORE))

print("\n=== die Fenster ===")
w = {k: m.window(STORE, k, now=END) for k in m.WINDOWS}
for k, (tier, span) in m.WINDOWS.items():
    pts = w[k]["series"]["cpu"]
    ok(f"{k}: kommt aus der Stufe {tier}", w[k]["tier"] == tier, w[k]["tier"])
    ok(f"{k}: nichts, was aelter ist als das Fenster",
       all(p[0] >= END - span for p in pts) and bool(pts))
ok("4 h: Minutenwerte, rund 240", 200 <= len(w["4h"]["series"]["cpu"]) <= 241,
   len(w["4h"]["series"]["cpu"]))
ok("24 h: Fuenf-Minuten-Werte, rund 288",
   270 <= len(w["24h"]["series"]["cpu"]) <= 289, len(w["24h"]["series"]["cpu"]))
mean = [p[1] for p in w["24h"]["series"]["cpu"]]
ok("das Mittel stimmt (20 Prozent)", all(abs(x - 20.0) < 0.5 for x in mean), mean[:5])
ok("der Arbeitsspeicher auch (40 Prozent)",
   all(abs(p[1] - 40.0) < 0.1 for p in w["24h"]["series"]["mem"]))

# Die Spitze war vor drei Tagen: im 1-Wochen- und im 1-Monats-Bild muss
# sie als MAXIMUM noch da sein, auch wenn das Mittel sie verschluckt.
for k in ("1w", "1m"):
    pts = w[k]["series"]["cpu"]
    peak = max(p[3] for p in pts)
    ok(f"{k}: die Ein-Minuten-Spitze steht noch im Maximum", peak >= 99.0, peak)
    mx_mean = max(p[1] for p in pts)
    ok(f"{k}: ... waehrend das Mittel sie nicht zeigt", mx_mean < 60.0, mx_mean)

# Der Ausfall: Tag 10 bis 11. Im Monatsbild darf dort nichts stehen.
gap = [p for p in w["1m"]["series"]["cpu"] if OFF[0] + 7200 <= p[0] < OFF[1] - 7200]
ok("die Luecke bleibt eine Luecke (nichts erfunden)", not gap, gap[:3])
ok("`since` nennt den ersten Zeitpunkt, den es gibt",
   w["1m"]["since"] is not None and w["1m"]["since"] >= END - 31 * 86400)

print("\n=== was schiefgehen darf ===")
good = len(m._read(STORE, "raw"))
with open(m._path(STORE, "raw"), "a") as f:
    f.write("{kaputt\n\n[1,2]\n")
ok("eine beschaedigte Zeile macht die Ablage nicht unlesbar",
   len(m._read(STORE, "raw")) == good)
fresh = tempfile.mkdtemp(prefix="oaap-metrics-leer-")
ok("ein Fenster ohne Daten ist leer, nicht kaputt",
   m.window(fresh, "24h", now=END)["since"] is None
   and m.window(fresh, "24h", now=END)["series"]["cpu"] == [])
dead = tempfile.mkdtemp(prefix="oaap-metrics-tot-")
r = m.take_sample(fresh, "/nichts/da", now=END, proc=dead, sleep=lambda s: None)
ok("ohne lesbares /proc und ohne Platte wird nichts geschrieben, nichts bricht",
   r is not None and len(r) == 1 and not m._read(fresh, "raw"), r)

print("\n=== das Format der Spezifikation (RFC-0051 3) ===")
lines = m.sample_lines("oaapx02", {"t": BASE, "cpu": 12.4, "mem": 40.0, "disk": 61.0})
ok("eine Zeile je Reihe", [x["m"] for x in lines] == ["cpu", "mem", "disk"])
ok("v, node, t, m, x -- und sonst nichts",
   set(lines[0]) == {"v", "node", "t", "m", "x"} and lines[0]["v"] == 1, lines[0])
ok("die Zeit ist UTC mit Z", lines[0]["t"].endswith("Z") and "T" in lines[0]["t"],
   lines[0]["t"])
ok("fehlt eine Reihe, fehlt ihre Zeile (keine Null erfunden)",
   [x["m"] for x in m.sample_lines("n", {"t": BASE, "mem": 1.0})] == ["mem"])
ok("als JSON lesbar", json.loads(json.dumps(lines[0])) == lines[0])

print("\n=== der Befehl `oaap metrics` (derselbe Weg wie an der Maschine) ===")
import argparse                                                 # noqa: E402
import contextlib                                               # noqa: E402

os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import appctl as a                                              # noqa: E402

a.METRICS_DIR = STORE


def run(action, window=None):
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            a.cmd_metrics(argparse.Namespace(action=action, window=window))
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue(), code


out, code = run("show", "24h")
ok("show nennt Fenster, Stufe und alle drei Reihen",
   code == 0 and "tier '5m'" in out and "CPU" in out
   and "Arbeitsspeicher" in out and "Platte" in out, out)
ok("... mit Mittel, Minimum und Maximum", "mean" in out and "max" in out, out)
out, code = run("show", "7d")
ok("ein unbekanntes Fenster wird abgelehnt", code != 0, out)
a.METRICS_DIR = tempfile.mkdtemp(prefix="oaap-metrics-leer2-")
out, code = run("show")
ok("ohne Messwerte sagt show es und bricht nicht", "No samples yet" in out, out)
a.metrics.take_sample = lambda *x, **k: (_ for _ in ()).throw(OSError("voll"))
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    a.cmd_metrics(argparse.Namespace(action="sample", window=None))
ok("ein Fehler beim Messen beendet den Lauf nicht (er teilt sich die Einheit "
   "mit dem Zustandsindex)", "skipped" in buf.getvalue(), buf.getvalue())

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
