#!/usr/bin/env python3
"""Die Karte „Hochgeladene Pakete" (oaap.apps.runtime 2.14).

Diese Karte war auf jedem migrierten Knoten unsichtbar -- sie las noch
`/apps-registry/<key>/artifacts`, den Ort von vor RFC-0026. Weil sie
mit `{% if i.artifacts %}` beginnt, verschwand sie dabei lautlos: keine
Fehlermeldung, keine leere Tabelle, nur eine Seite, auf der „Erneut
ausrollen", „Hierauf zurueck" und „Loeschen" nicht mehr standen.

Genau das prueft diese Datei: dass die Karte aus dem Index des Knotens
entsteht, und dass der Download-Knopf nur dort steht, wo er auch
bedient wuerde.

Braucht jinja2 (wie test_backup_state_page.py), kein Docker, keinen Knoten.

Aufruf: python3 test/test_artifact_card.py
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.path.join(HERE, "..", "platform", "services", "portal")

try:
    from jinja2 import Template
except ImportError:
    print("SKIP: jinja2 ist nicht installiert (pip install jinja2)")
    sys.exit(0)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


SRC = open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read()

# Die ECHTE Vorlage und die ECHTE Lesefunktion, nicht eine Abschrift:
# eine Abschrift laeuft irgendwann auseinander, und dann prueft der
# Test die Abschrift.
start = SRC.index("{% if i.artifacts %}")
CARD = Template(SRC[start:SRC.index("</section>", start)])

REG = tempfile.mkdtemp(prefix="oaap-artcard-")
block = SRC[SRC.index('ARTIFACT_INDEX = "/apps-registry'):SRC.index("def _pending_envelope(")]
block = block.replace('"/apps-registry/artifacts.json"',
                      repr(os.path.join(REG, "artifacts.json")))
ns = {"json": json, "os": os}
exec(compile(block, "portal-artifact-block", "exec"), ns)  # noqa: S102
_artifacts = ns["_artifacts"]

INST = {"source": {"kind": "artifact", "stored": "0.20.2-abc123def456.zip"}}
STORE_INST = {"source": {"kind": "store", "url": "https://example.invalid"}}


def write_index(data):
    with open(os.path.join(REG, "artifacts.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "0.1", "instances": data}, f)


def render(inst=INST, can_export=True, name="bdt-hub"):
    i = {"key": name, "artifacts": _artifacts(name, inst),
         "can_export": can_export}
    return re.sub(r"\s+", " ", CARD.render(i=i))


write_index({"bdt-hub": [
    {"file": "0.20.2-abc123def456.zip", "bytes": 41943040,
     "received": "2026-09-05T10:22:41+00:00"},
    {"file": "0.20.1-000111222333.zip", "bytes": 39845888,
     "received": "2026-09-04T18:03:12+00:00"},
]})

print("")
print("Die Karte entsteht aus dem Index des Knotens")

html = render()
ok("sie ist ueberhaupt da", "Hochgeladene Pakete" in html, html[:200])
ok("das laufende Paket steht drin", "0.20.2-abc123def456.zip" in html)
ok("das vorige auch", "0.20.1-000111222333.zip" in html)
ok("das laufende ist als 'in Betrieb' erkennbar", "in Betrieb" in html)
ok("die Groesse steht daneben", "40.0 MB" in html, html[:600])
ok("die Empfangszeit lesbar, ohne Sekunden und ohne T",
   "2026-09-05 10:22" in html and "10:22:41" not in html, html[:600])

print("")
print("Und die Knoepfe, die mit ihr verschwunden waren, sind wieder da")

ok("erneut ausrollen (fuer das laufende)", "Erneut ausrollen" in html)
ok("hierauf zurueck (fuer das vorige)", "Hierauf zurück" in html)
ok("loeschen -- aber nicht fuer das in Betrieb",
   html.count("Löschen") == 1, f"{html.count('Löschen')}x")

print("")
print("Herunterladen bekommt nur, wer es auch darf")

ok("server_admin sieht den Knopf", "Herunterladen" in html)
ok("und er zeigt auf die richtige Adresse",
   "/instances/bdt-hub/artifact-download?artifact=0.20.2-abc123def456.zip"
   in html, html[:900])
ok("und die Karte sagt, wofuer er da ist",
   "anderen</em> Knoten" in html or "anderen Knoten" in html, html[-700:])
ok("und dass er protokolliert wird", "Mandantenprotokoll" in html, html[-500:])

low = render(can_export=False)
ok("wer ihn nicht bedienen darf, sieht ihn nicht",
   "Herunterladen" not in low, low[:600])
ok("die uebrigen Knoepfe bleiben ihm aber",
   "Erneut ausrollen" in low and "Löschen" in low)
ok("und der Erklaersatz zum Download auch nicht",
   "Mandantenprotokoll" not in low)

print("")
print("Was keine Pakete hat, bekommt keine Karte")

ok("eine Instanz aus dem Store",
   "Hochgeladene Pakete" not in render(inst=STORE_INST), "")
ok("eine Instanz, die im Index nicht vorkommt",
   "Hochgeladene Pakete" not in render(name="gibt-es-nicht"), "")

print("")
print("Ein kaputter oder fehlender Index legt die Seite nicht lahm")

with open(os.path.join(REG, "artifacts.json"), "w", encoding="utf-8") as f:
    f.write("{kein json")
ok("kaputt: keine Karte, kein Absturz",
   "Hochgeladene Pakete" not in render(), "")
os.remove(os.path.join(REG, "artifacts.json"))
ok("fehlt ganz: dasselbe", "Hochgeladene Pakete" not in render(), "")

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
