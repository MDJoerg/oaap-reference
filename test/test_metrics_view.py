#!/usr/bin/env python3
"""Der Verlauf auf der Gesundheitsseite (RFC-0051, Stufe 2).

Die Daten kommen aus dem ECHTEN Messweg (`take_sample` mit einem falschen
/proc), die Darstellung aus `metrics_view.block`. Geprueft wird, was man
dem Bild ansehen muss: drei Diagramme, vier Fenster, ein Band fuer die
Spitze, eine Luecke als Luecke, ein ehrlicher Text bei wenig Daten, und
dass die Seite auch wirklich damit ausgeliefert wird.

Aufruf: python3 test/test_metrics_view.py
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import metrics as m                                            # noqa: E402
import metrics_view as v                                       # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


BASE = 1_790_000_000 - 1_790_000_000 % 7200
PROC = tempfile.mkdtemp(prefix="oaap-mv-proc-")
STORE = tempfile.mkdtemp(prefix="oaap-mv-store-")
DATA = tempfile.mkdtemp(prefix="oaap-mv-data-")


def proc(total, idle, avail):
    with open(os.path.join(PROC, "stat"), "w") as f:
        f.write(f"cpu  {total - idle} 0 0 {idle} 0 0 0 0 0 0\n")
    with open(os.path.join(PROC, "meminfo"), "w") as f:
        f.write(f"MemTotal: 1000 kB\nMemAvailable: {avail} kB\n")


# Zwei Tage Minuten. 20 Prozent CPU, 40 Prozent Speicher, eine Spitze, und
# ein Ausfall von drei Stunden in der Mitte.
SPIKE = BASE + 20 * 3600
OFF = (BASE + 30 * 3600, BASE + 33 * 3600)
END = BASE + 48 * 3600
total, idle = 10_000, 9_000
proc(total, idle, 600)
m.take_sample(STORE, DATA, now=BASE, proc=PROC, sleep=lambda s: proc(total + 100, idle + 80, 600))
total, idle = total + 100, idle + 80
t = BASE + 60
while t <= END:
    if OFF[0] <= t < OFF[1]:
        total, idle = 500, 400
    else:
        total += 6000
        idle += 0 if t == SPIKE else 4800
        proc(total, idle, 600)
        m.take_sample(STORE, DATA, now=t, proc=PROC, sleep=lambda s: None)
    t += 60

print("=== der Block ===")
html = {k: v.block(STORE, k, now=END) for k in m.WINDOWS}
for k, h in html.items():
    ok(f"{k}: drei Diagramme", h.count("<svg") == 3, h.count("<svg"))
    ok(f"{k}: vier Knoepfe, genau einer aktiv",
       h.count('class="vl-btn') == 4 and h.count("vl-btn active") == 1)
ok("der aktive Knopf ist der gewaehlte", 'vl-btn active" data-w="24h"' in
   v.block(STORE, "24h", now=END))
ok("ein unbekanntes Fenster faellt auf 24 h zurueck",
   'vl-btn active" data-w="24h"' in v.block(STORE, "boese<script>", now=END))
ok("jeder Knopf ist ohne Skript ein Link", all(
   f'href="/health?w={k}#verlauf"' in html["4h"] for k in m.WINDOWS))
ok("CPU, Arbeitsspeicher und Platte stehen drauf",
   all(x in html["24h"] for x in ("CPU", "Arbeitsspeicher", "Platte")))
ok("der aktuelle CPU-Wert steht in der Ecke (rund 20 %)",
   re.search(r"CPU</span><strong>(\d+) %", html["4h"]) is not None
   and 19 <= int(re.search(r"CPU</span><strong>(\d+) %", html["4h"]).group(1)) <= 21,
   re.search(r"CPU</span><strong>[^<]*", html["4h"]))
ok("der Arbeitsspeicher steht auf 40 %", "<strong>40 %</strong>" in html["4h"])
ok("nichts wird von aussen geladen",
   not re.search(r'(src|href)="https?://', html["24h"]) and "<script src" not in html["24h"])

print("\n=== die Spitze und die Luecke ===")
w24 = v.block(STORE, "24h", now=END)
ok("ein Band (Minimum bis Maximum) ist da", "vl-band" in w24)
m1 = m.window(STORE, "1m", now=END)["series"]["cpu"]
ok("die Spitze steht im Maximum des Monatsfensters", max(p[3] for p in m1) >= 99)
bands_24 = w24.count("<polygon")
ok("die Luecke zerreisst die Linie: mehr als ein Stueck",
   bands_24 >= 2 * 1 and w24.count("<polyline") > 3, (w24.count("<polyline"), bands_24))
chart = v.chart("CPU", [[100, 20, 20, 20], [160, 20, 20, 20],
                        [10_000, 20, 20, 20], [10_060, 20, 20, 20]],
                now=10_100, span=10_000, step=60)
ok("... und zwei Stuecke sind zwei Linien, nicht eine",
   chart.count("<polyline") == 2, chart)
one = v.chart("CPU", [[100, 20, 20, 20]], now=200, span=1000, step=60)
ok("ein einzelner Punkt ist ein Punkt", "<circle" in one and "<polyline" not in one)
empty = v.chart("CPU", [], now=200, span=1000, step=60)
ok("ohne Punkte bleibt nur das Gitter", "<polyline" not in empty
   and "<line" in empty)

print("\n=== ehrlich bei wenig Daten ===")
fresh = tempfile.mkdtemp(prefix="oaap-mv-neu-")
h = v.block(fresh, "24h", now=END)
ok("ohne Daten steht ein Satz, keine leeren Diagramme",
   "Noch keine Messwerte" in h and "<svg" not in h)
proc(100_000, 90_000, 500)
for i in range(5):
    m.take_sample(fresh, DATA, now=END + 60 * i, proc=PROC, sleep=lambda s: None)
    proc(100_000 + 6000 * (i + 1), 90_000 + 4800 * (i + 1), 500)
h = v.block(fresh, "24h", now=END + 300)
ok("wenig Daten: der Text sagt, seit wann es welche gibt",
   "Daten seit" in h, re.findall(r'class="muted">[^<]*', h)[-1:])
ok("... ein voller Verlauf sagt das nicht", "Daten seit" not in html["4h"])

print("\n=== die Zeile zum Sender (RFC-0052) ===")
import json                                                     # noqa: E402

h0 = v.block(STORE, "24h", now=END)
ok("ohne eingerichteten Sender steht keine Zeile da", "Senden an" not in h0)
INFO = {"host": "mqtt.example.org", "port": 8883, "node": "oaapx02", "root": "oaap-node"}


def put(name, obj):
    with open(os.path.join(STORE, name), "w") as f:
        json.dump(obj, f)


put(m.SENDER_INFO_FILE, INFO)
h = v.block(STORE, "24h", now=END)
ok("eingerichtet, noch nichts gesendet: die Zeile sagt es", "Senden an" in h
   and "mqtt.example.org" in h and "noch nichts gesendet" in h, re.findall(r"vl-send.*", h))
put(m.SENDER_STATE_FILE, {"ok_at": END - 130 * 60, "fails": 0, "err": "", "sent_total": 9})
h = v.block(STORE, "24h", now=END)
ok("Erfolg: ok, zuletzt vor 2 Std. 10 Min.", 'class="ok">ok</span>' in h
   and "zuletzt vor 2 Std. 10 Min." in h, re.findall(r"vl-send.*", h))
ok("... mit der Zahl der Wartenden", " wartend" in h)
put(m.SENDER_STATE_FILE, {"ok_at": END - 600, "err": "cannot connect <b>x</b>",
                          "err_at": END - 300, "fails": 3, "kind": "network"})
h = v.block(STORE, "24h", now=END)
ok("Fehler: sagt seit wann, wann zuletzt Erfolg war und was los ist",
   "nicht erreichbar" in h and "seit 5 Min." in h and "letzter Erfolg vor 10 Min." in h
   and "cannot connect" in h, re.findall(r"vl-send.*", h))
ok("... der Fehlertext ist maskiert (kein eingeschleustes Markup)",
   "<b>x</b>" not in h and "&lt;b&gt;" in h)
ok("kein Verlust, kein Satz darueber", "gingen verloren" not in h)
for i in range(3):
    m.queue_add(STORE, {"t": END - 9 * 86400 + i * 60, "cpu": 1.0})
m.queue_trim(STORE, END)
h = v.block(STORE, "24h", now=END)
ok("ein Verlust steht in Worten da, nicht nur als Zahl",
   "Messwerte gingen verloren" in h and "zu voll oder zu alt" in h,
   re.findall(r"vl-send.*", h))
ok("die Zeile liegt im Block (er wird als Ganzes ausgetauscht)",
   h.index("vl-send") < h.rindex("</div>"))
os.remove(os.path.join(STORE, m.SENDER_STATE_FILE))
os.remove(os.path.join(STORE, m.SENDER_INFO_FILE))
ok("ein Block ohne Daten bleibt ein Satz (die Zeile kommt nicht dazu)",
   "Senden an" not in v.block(tempfile.mkdtemp(prefix="oaap-mv-leer-"), "24h", now=END))

print("\n=== Kleinigkeiten ===")
ok("Zeit relativ, ohne Zeitzone zu raten",
   v._ago(130 * 60) == "2 Std. 10 Min." and v._ago(90_000) == "1 Tg. 1 Std."
   and v._ago(5 * 60) == "5 Min.")
ok("kein Wert ausserhalb des Bildes (0-100 abgeschnitten)",
   v._y(150) == v._y(100) and v._y(-5) == v._y(0))

print("\n=== die Seite liefert es aus ===")
SRV = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
src = open(SRV, encoding="utf-8").read()
ok("/health reicht den Block an die Vorlage",
   "mx=Markup(metrics_view.block(" in src and "{{ mx_style }}{{ mx }}" in src)
ok("der Block steht VOR der Knoten-Tabelle",
   src.index("{{ mx_style }}{{ mx }}") < src.index("<h2>Knoten</h2>"))
ok("es gibt die Teilroute fuer die Knoepfe, mit derselben Rollenpruefung",
   '@app.get("/health/verlauf")' in src
   and "caller_roles() & {" in src[src.index('@app.get("/health/verlauf")'):
                                   src.index('@app.get("/health")')])
df = open(os.path.join(HERE, "..", "platform", "services", "portal",
                       "Dockerfile"), encoding="utf-8").read()
ok("das Image enthaelt beide Dateien (sonst Neustartschleife, CURRENT_STATE 132)",
   "COPY metrics.py ." in df and "COPY traffic.py ." in df
   and "portal/metrics_view.py" in df)
dc = open(os.path.join(HERE, "..", "platform", "docker-compose.yml"),
          encoding="utf-8").read()
ok("das Portal liest das Verzeichnis nur lesend",
   "data/metrics:/metrics:ro" in dc)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
