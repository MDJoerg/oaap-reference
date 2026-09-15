#!/usr/bin/env python3
"""Die Konfigurationskarte: was zusammengehoert, steht zusammen
(oaap.core.portal 2.4, 0.3.15).

Der Anlass war Joergs Befund vom 2026-09-15, bei der Suche nach einem
CORS-Fehler: "Die Formatierung auf der Konfigurationsseite ist schlecht:
man kommt hier durcheinander" -- und: "Mir war nicht klar, dass eine
Konfigurationsaenderung einen Neustart ausloest."

Beides war wahr, und beides lag an der Anordnung, nicht am Text:

- Die Erklaerzeile eines Werts (technischer Name, "vertraulich") stand
  UNTER seinem Feld -- also direkt UEBER dem Namen des naechsten. Sie las
  sich wie dessen Ueberschrift.
- Dass Speichern die App neu startet, stand in einem grauen Satz unter
  allen Feldern. Wer zum Knopf scrollt, liest ihn nicht.

Dazu ein dritter Fund beim Umbau: Enter in einem Feld loest den ERSTEN
Absendeknopf des Formulars aus. Stand dort "Wert erzeugen", erzeugte
Enter einen neuen Wert, statt zu speichern.

Und die Rueckmeldung: "Gespeichert." sagte nicht, dass die App gerade
neu gestartet wurde -- und auch nicht, wenn gar nichts geschah.

Braucht jinja2, kein Docker, keinen Knoten.

Aufruf: python3 test/test_config_layout.py
"""
import os
import re
import sys

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

# Die ECHTE Vorlage der Konfigurationskarte, nicht eine Abschrift.
start = SRC.index('<form method="post" action="/instances/{{ i.key }}/config">')
CARD = Template(SRC[start:SRC.index("</form>", start) + len("</form>")])


def render(rows):
    return re.sub(r"\s+", " ", CARD.render(i={"key": "livekit", "config": rows}))


def row(key, label, secret=False, multiline=False, generate=False,
        fresh=False, is_set=True, value=""):
    return {"key": key, "label": label, "secret": secret,
            "multiline": multiline, "generate": generate, "fresh": fresh,
            "is_set": is_set, "value": value}


ROWS = [row("LIVEKIT_API_SECRET", "API-Geheimnis", secret=True,
            generate=True, is_set=False),
        row("AIGW_SUPPLIER_KEYS", "Zugangsdaten je Bezugsquelle",
            secret=True, multiline=True),
        row("LIVEKIT_API_KEY", "API-Schlüssel", value="oaap")]
KEYS = [r["key"] for r in ROWS]
html = render(ROWS)

print("")
print("Ein Block je Wert -- und die Erklaerung gehoert zu ihrem eigenen Feld")

blocks = html.split('<div class="cfgfield">')[1:]
ok("jeder Wert steht in genau einem eigenen Block",
   len(blocks) == len(ROWS), len(blocks))

for key, block in zip(KEYS, blocks):
    # Der Block endet, wo der naechste beginnt; der letzte am Speicherteil.
    block = block.split('<div class="cfgsave">')[0]
    label_at = block.find(f'<label for="cfg-{key}">')
    hint_at = block.find(f"<code>{key}</code>")
    field_at = block.find(f'name="cfg-{key}"')
    ok(f"{key}: Name, dann Erklaerung, dann Feld",
       0 <= label_at < hint_at < field_at, (label_at, hint_at, field_at))
    fremde = [k for k in KEYS if k != key and (f"<code>{k}</code>" in block
                                               or f'name="cfg-{k}"' in block)]
    ok(f"{key}: im Block steht nichts von einem anderen Wert",
       not fremde, fremde)
    ok(f"{key}: der Name ist mit seinem Feld verbunden (for/id)",
       f'id="cfg-{key}"' in block)

ok("die Erklaerung des ersten Werts steht NICHT zwischen Feld und naechstem Namen",
   html.find('name="cfg-LIVEKIT_API_SECRET"')
   < html.find('<label for="cfg-AIGW_SUPPLIER_KEYS">')
   and "<code>LIVEKIT_API_SECRET</code>" not in
   html[html.find('name="cfg-LIVEKIT_API_SECRET"'):
        html.find('<label for="cfg-AIGW_SUPPLIER_KEYS">')])

print("")
print("Der Neustart wird am Knopf gesagt, nicht irgendwo")

save = html.split('<div class="cfgsave">')
ok("es gibt einen Speicherteil nach dem letzten Wert", len(save) == 2)
tail = save[-1] if len(save) == 2 else ""
ok("er sagt: Speichern startet die App neu",
   "Speichern startet die App neu" in tail, tail[:400])
ok("und dass sie dabei kurz nicht erreichbar ist",
   "nicht erreichbar" in tail)
ok("und dass ohne Aenderung nichts geschieht",
   "Ändert sich nichts, bleibt die App unberührt" in tail)
ok("der Hinweis steht VOR dem Knopf, gleich darueber",
   0 <= tail.find("startet die App neu")
   < tail.find("Speichern und App neu starten</button>"), tail[:400])
ok("der Knopf selbst sagt es auch",
   "<button>Speichern und App neu starten</button>" in tail)

print("")
print("Enter speichert -- und erzeugt nie einen Wert")

first_button = re.search(r"<button[^>]*>", html)
ok("der erste Absendeknopf im Formular ist KEIN „Wert erzeugen\"",
   first_button and 'name="generate"' not in first_button.group(0),
   first_button.group(0) if first_button else "kein Knopf")
ok("sondern der unsichtbare Speichern-Knopf",
   first_button and 'class="cfgdefault"' in first_button.group(0))
ok("er hat keinen name -- das Portal liest ihn als gewoehnliches Speichern",
   first_button and "name=" not in first_button.group(0))
ok("er ist aus der Tab-Reihenfolge und fuer Vorleseprogramme verborgen",
   first_button and 'tabindex="-1"' in first_button.group(0)
   and 'aria-hidden="true"' in first_button.group(0))

css = re.search(r"\.cfgdefault\{([^}]*)\}", SRC)
ok("er ist aus dem Bild geschoben, nicht display:none",
   css and "position:absolute" in css.group(1)
   and "display:none" not in css.group(1),
   "display:none nimmt nicht jeder Browser als Standardknopf")
ok("die Bloecke haben ein Stylesheet", ".cfgfield{" in SRC)

frisch = render([row("LIVEKIT_API_SECRET", "API-Geheimnis", secret=True,
                     generate=True, fresh=True, is_set=False, value="x" * 43)])
ok("auch mit frisch erzeugtem Wert bleibt der Standardknopf vorn",
   'class="cfgdefault"' in re.search(r"<button[^>]*>", frisch).group(0))

print("")
print("Die Rueckmeldung sagt, was geschehen ist")

# Die ECHTE Uebersetzung der Worker-Antwort, nicht eine Abschrift.
ns = {}
block = SRC[SRC.index("def _config_outcome("):
            SRC.index('@app.post("/instances/<name>/config")')]
exec(compile(block, "portal-config-outcome", "exec"), ns)  # noqa: S102
outcome = ns["_config_outcome"]

msg, err = outcome({"ok": True, "message": "config changed: LIVEKIT_API_KEY"})
ok("ein geaenderter Wert: gespeichert UND neu gestartet",
   "neu gestartet" in msg and not err, (msg, err))
msg, err = outcome({"ok": True, "message": "config no change"})
ok("nichts geaendert: sagt das, und nennt keinen Neustart",
   "Keine Änderung" in msg and "neu gestartet" not in msg and not err,
   (msg, err))
msg, err = outcome({"ok": False, "message": "no such image"})
ok("ein Fehler bleibt ein Fehler, mit dem Grund des Knotens",
   not msg and err == "no such image", (msg, err))
msg, err = outcome(None)
ok("keine Antwort in der Zeit: laeuft noch, kein falsches „gespeichert\"",
   not msg and "läuft noch" in err, (msg, err))

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
