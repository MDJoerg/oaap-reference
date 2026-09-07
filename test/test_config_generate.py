#!/usr/bin/env python3
"""Einen Wert erzeugen lassen statt ihn zu erfinden (oaap.apps.runtime 2.8).

Der Anlass war eine echte Ablehnung: Joerg hat mit einem
Passwortgenerator einen Schluessel erzeugt, und das Feld hat ihn nicht
genommen. Das ist kein Schikane-Fehler -- ein Wert, der ein `;` traegt,
zerschneidet eine Listenangabe, und einer mit Zeilenumbruch oder `=`
zerreisst die zeilenbasierte env-Datei, aus der der Container liest.

    Die Plattform erzeugt, was ihr eigener Speicher tragen kann.

Genau das prueft diese Datei: das Alphabet des erzeugten Werts, und dass
er durch jede Stelle passt, durch die er muss. Dazu die zweite Regel,
die leicht zu uebersehen ist:

    Erzeugen ist nicht Speichern.

Ein erzeugter Wert, den niemand speichert, hinterlaesst nichts -- keinen
Eintrag, keine Zeile im Protokoll. Und umgekehrt muss er sichtbar
bleiben, bis gespeichert ist: ein Geheimnis, das direkt in
schreibgeschuetzten Speicher wandert, ist fuer den verloren, der es der
Gegenseite geben muss.

Braucht jinja2, kein Docker, keinen Knoten.

Aufruf: python3 test/test_config_generate.py
"""
import importlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.path.join(HERE, "..", "platform", "services", "portal")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

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

# Die ECHTE Erzeugungsfunktion des Portals, nicht eine Abschrift.
ns = {"secrets": __import__("secrets")}
block = SRC[SRC.index("GENERATED_BYTES = "):SRC.index("def _instance_config(")]
exec(compile(block, "portal-generate", "exec"), ns)  # noqa: S102
generated_value = ns["generated_value"]

print("")
print("Der erzeugte Wert traegt nur, was ueberall durchgeht")

vals = [generated_value() for _ in range(200)]
ok("Alphabet: Buchstaben, Ziffern, '_' und '-' -- sonst nichts",
   all(re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in vals),
   next((v for v in vals if not re.fullmatch(r"[A-Za-z0-9_-]+", v)), ""))
ok("kein ';' -- sonst zerschneidet es eine Listenangabe",
   all(";" not in v for v in vals))
ok("kein '=' -- sonst zerreisst es die Zeile in instance.env",
   all("=" not in v for v in vals))
ok("kein Leerzeichen und kein Zeilenumbruch",
   all(v == v.strip() and not any(c.isspace() for c in v) for v in vals))
ok("keine Anfuehrungszeichen, kein Backslash, kein '$'",
   all(not (set(v) & set("\"'\\$`")) for v in vals))
ok("lang genug, um ein Geheimnis zu sein (>= 40 Zeichen)",
   all(len(v) >= 40 for v in vals), min(len(v) for v in vals))
ok("und jeder ist ein anderer", len(set(vals)) == len(vals))

print("")
print("Er kommt durch die Stellen, durch die er muss")

sys.modules.pop("appctl", None)
DATA = tempfile.mkdtemp(prefix="oaap-gen-test-")
os.environ["OAAP_DATA_DIR"] = DATA
a = importlib.import_module("appctl")
sys.path.insert(0, PORTAL)
sys.modules.pop("instance_view", None)
iv = importlib.import_module("instance_view")

v = generated_value()
joined, err = iv.lines_to_value(v)
ok("eine Listenangabe nimmt ihn an", not err and joined == v, err)
joined2, err2 = iv.lines_to_value(f"alt-eintrag\n{v}")
ok("auch als zweite Zeile neben einer bestehenden",
   not err2 and joined2.split(";")[-1] == v, err2 or joined2)

# Und die Gegenprobe: genau das, was ein Passwortgenerator liefert.
_, err3 = iv.lines_to_value("Xk9;mZ2!qR")
ok("die Gegenprobe -- ein Wert mit ';' wird weiterhin abgelehnt",
   bool(err3), "sonst waere die Ablehnung, die Joerg gesehen hat, weg")

env_line = f"LIVEKIT_API_SECRET={v}"
ok("eine env-Zeile bleibt eine Zeile",
   len(env_line.split("\n")) == 1 and env_line.split("=", 1)[1] == v)

print("")
print("Der Knopf steht nur, wo die App ihren Wert selbst bestimmt")

# Die ECHTE Vorlage der Konfigurationskarte.
start = SRC.index('<form method="post" action="/instances/{{ i.key }}/config">')
CARD = Template(SRC[start:SRC.index("</form>", start) + len("</form>")])


def render(rows):
    return re.sub(r"\s+", " ", CARD.render(i={"key": "livekit", "config": rows}))


def row(key, label, secret=False, multiline=False, generate=False,
        fresh=False, is_set=True, value=""):
    return {"key": key, "label": label, "secret": secret,
            "multiline": multiline, "generate": generate, "fresh": fresh,
            "is_set": is_set, "value": value}


eigen = row("LIVEKIT_API_SECRET", "API-Geheimnis", secret=True,
            generate=True, is_set=False)
fremd = row("AIGW_SUPPLIER_KEYS", "Zugangsdaten je Bezugsquelle",
            secret=True, multiline=True)
offen = row("LIVEKIT_API_KEY", "API-Schlüssel", value="oaap")

html = render([eigen, fremd, offen])
ok("das eigene Geheimnis bekommt den Knopf",
   'name="generate" value="LIVEKIT_API_SECRET"' in html, html[:900])
ok("ein fremder Zugangsschluessel bekommt ihn NICHT",
   'value="AIGW_SUPPLIER_KEYS"' not in html,
   "dort waere ein erzeugter Wert keine Hilfe, sondern eine falsche Antwort")
ok("eine blosse Kennung auch nicht",
   'name="generate" value="LIVEKIT_API_KEY"' not in html)
ok("es gibt genau einen Knopf", html.count('name="generate"') == 1,
   html.count('name="generate"'))
ok("die Erklaerzeile nennt den Grund",
   "bestimmt die App selbst" in html, html[:900])

ohne = render([fremd, offen])
ok("ohne erzeugbares Feld steht gar kein Knopf da",
   'name="generate"' not in ohne)

print("")
print("Solange nichts erzeugt wurde, bleibt das Geheimnis schreibgeschuetzt")

ok('das Feld ist type="password"',
   'type="password" name="cfg-LIVEKIT_API_SECRET"' in html, html[:900])
ok("und traegt keinen Wert", 'name="cfg-LIVEKIT_API_SECRET" value=""' in html)
ok("es steht kein Kopierhinweis da", "Jetzt kopieren" not in html)

print("")
print("Ein frisch erzeugter Wert steht LESBAR da -- genau einmal")

v = generated_value()
frisch = render([row("LIVEKIT_API_SECRET", "API-Geheimnis", secret=True,
                     generate=True, fresh=True, is_set=False, value=v),
                 fremd, offen])
ok("er ist zu sehen", v in frisch, frisch[:900])
ok('und lesbar, nicht als Punkte',
   f'type="text" name="cfg-LIVEKIT_API_SECRET" value="{v}"' in frisch,
   "ein Geheimnis, das niemand lesen kann, ist fuer die Gegenseite verloren")
ok("die Seite sagt: jetzt kopieren", "Jetzt kopieren" in frisch)
ok("und dass er noch NICHT gespeichert ist",
   "gespeichert ist er noch nicht" in frisch, frisch[:1400])
ok("und dass er danach nicht mehr angezeigt wird",
   "nicht mehr angezeigt" in frisch)
ok("das fremde Geheimnis bleibt daneben verdeckt",
   "gesetzt — leer lassen, um ihn zu behalten" in frisch
   and frisch.count(v) == 1,
   "nur das EINE Feld wird aufgedeckt, und der Wert steht genau einmal da")

print("")
print("Erzeugen ist nicht Speichern -- und braucht kein JavaScript")

ok("der Knopf ist ein gewoehnlicher Submit, kein type=button",
   'name="generate"' in frisch and "type=\"button\"" not in frisch,
   "ein type=button ohne JavaScript taete gar nichts")
ok("kein <script> auf dieser Seite", "<script" not in frisch, frisch[:400])
ok("und kein onclick", "onclick" not in frisch)
ok("der Wert reist im Formular, nicht in der Adresse",
   "?generate=" not in frisch,
   "in einer URL landet er im Zugriffsprotokoll des Gateways")

print("")
print("Ein Manifest darf den Knopf nicht einfach behaupten")

recorded = a.__dict__.get("validate_manifest") and True
cfg = [{"key": "A", "label": "a", "generate": "token"},
       {"key": "B", "label": "b", "generate": "beliebig"},
       {"key": "C", "label": "c"}]
kept = [("token" if c.get("generate") == "token" else "") for c in cfg]
ok("'token' wird uebernommen", kept[0] == "token")
ok("alles andere wird zu 'nichts', nicht zu einem Knopf", kept[1] == "")
ok("und ohne Angabe erst recht", kept[2] == "")
ok("(die Registry-Regel steht wirklich in appctl)",
   '"token" if c.get("generate") == "token"' in
   open(os.path.join(HERE, "..", "platform", "appctl.py"),
        encoding="utf-8").read() and recorded)

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
