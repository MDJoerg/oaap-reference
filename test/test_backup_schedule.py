#!/usr/bin/env python3
"""Den Zeitplan im Portal aendern (RFC-0029 D1).

Joerg hat bei der Abnahme die volle Variante gewaehlt -- nicht nur
anzeigen, sondern aendern -- und daran EINE Bedingung geknuepft. Sie ist
der Grund, warum diese Datei existiert:

    Die Seite muss neben dem Zeitfeld sagen, dass zu dieser Uhrzeit jede
    App dieses Knotens fuer Minuten steht -- mit der zuletzt gemessenen
    Dauer DIESES Knotens, nie mit einer allgemeinen Zahl.

Eine Zahl aus einem Handbuch ist immer die Maschine von jemand anderem.
Und ein Knoten, der noch nie gesichert hat, hat keine Zahl -- der muss
das sagen und darf sich keine ausleihen.

Dazu die zweite Haelfte der Entscheidung, die genauso wichtig ist:

    Ziel-Pfad und Abhol-Schluessel bleiben drausssen.

Ein Archiv enthaelt jedes Geheimnis dieser Maschine. Wohin es geschrieben
wird, wird an der Maschine entschieden -- nicht in einem Formular im
Netz. Ein Test, der nur prueft, was da IST, wuerde nie merken, wenn
dieses Feld eines Tages dazukommt.

Braucht jinja2, kein Docker, keinen Knoten.

Aufruf: python3 test/test_backup_schedule.py
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
APPCTL = open(os.path.join(HERE, "..", "platform", "appctl.py"),
              encoding="utf-8").read()

REG = tempfile.mkdtemp(prefix="oaap-sched-")
os.makedirs(os.path.join(REG, "backup-pulls"), exist_ok=True)

# Die ECHTE Karte und die ECHTE Lesefunktion.
start = SRC.index('<div class="card">\n  <h2>Sicherung</h2>')
CARD = Template(SRC[start:SRC.index('<p class="muted">Geprüft wird aus Sicht des Portals', start)])

block = SRC[SRC.index('BACKUP_LAST = "/apps-registry'):SRC.index("def recent_deploys(")]
for a, b in (("backup-last.json", "backup-last.json"),
             ("backup-schedule.json", "backup-schedule.json"),
             ("backup-pulls", "backup-pulls")):
    block = block.replace(f'"/apps-registry/{a}"', repr(os.path.join(REG, b)))
ns = {"json": json, "os": os, "datetime": __import__("datetime").datetime}
ns["_ago"] = lambda s: f"{int(s // 60)} Minuten"
ROLES = {"v": {"server_admin"}}
ns["caller_roles"] = lambda: ROLES["v"]
exec(compile(block, "portal-backup-block", "exec"), ns)  # noqa: S102
backup_state = ns["backup_state"]


def write(name, data):
    with open(os.path.join(REG, name), "w", encoding="utf-8") as f:
        json.dump(data, f)


def clear():
    for f in os.listdir(REG):
        p = os.path.join(REG, f)
        if os.path.isfile(p):
            os.remove(p)


def render():
    return re.sub(r"\s+", " ", CARD.render(bk=backup_state()))


SCHED = {"schema": "0.1", "enabled": True, "at": "03:30", "keep": 2,
         "target": "/var/backups/oaap", "unit": "oaap-backup.timer",
         "next": "2026-09-08T03:34:38+00:00"}
RUN = {"schema": "0.2", "state": "ok", "finished": "2026-09-07T01:10:01+00:00",
       "bytes": 8401433578, "instances": 12,
       "downtime_seconds": 32, "total_seconds": 245}

print("")
print("Der Satz, der die Bedingung der Abnahme ist")

clear()
write("backup-schedule.json", SCHED)
write("backup-last.json", RUN)
html = render()
ok("es gibt ein Formular", "/backup/schedule" in html, html[:300])
ok("mit der Uhrzeit als Feld", 'name="at" value="03:30"' in html, html[:900])
ok("die Seite sagt, dass ALLE Apps dieses Knotens stillstehen",
   "stehen alle Apps dieses Knotens still" in html
   or "stehen alle Apps" in html, html)
ok("und nennt die zuletzt gemessene Dauer DIESES Knotens",
   "32 Sekunden" in html, html)
ok("und sagt ausdruecklich, dass es diese Maschine war",
   "dieser Maschine" in html, html)
ok("und dass es mit wachsenden Daten mehr wird",
   "wird es mehr" in html, html)

print("")
print("Ein Knoten ohne eigene Messung leiht sich keine Zahl")

clear()
write("backup-schedule.json", SCHED)
html = render()
ok("das Formular steht trotzdem da", "/backup/schedule" in html)
ok("aber er sagt, dass er es noch nicht weiss",
   "noch nie gesichert" in html, html)
ok("und nennt KEINE Sekundenzahl",
   not re.search(r"\d+ Sekunden", html), html)
ok("und sagt, dass nur seine eigene zaehlt",
   "nur seine eigene" in html, html)

print("")
print("Was NICHT im Formular steht, und das ist die halbe Entscheidung")

clear()
write("backup-schedule.json", SCHED)
write("backup-last.json", RUN)
html = render()
form = html[html.index("/backup/schedule"):html.index("</form>")]
ok("kein Feld fuer das Ziel", 'name="target"' not in form, form[:400])
ok("kein Feld fuer einen Schluessel",
   "key" not in form.lower() and "schlüssel" not in form.lower(), form[:400])
ok("und nur die zwei Felder, die es geben soll",
   sorted(re.findall(r'name="(\w+)"', form)) == ["at", "keep", "op"],
   re.findall(r'name="(\w+)"', form))
ok("die Seite sagt auch, WARUM das Ziel fehlt",
   "jedes Geheimnis dieser" in html and "an der Maschine entschieden" in html,
   html[-900:])
ok("das Ziel wird aber weiterhin ANGEZEIGT",
   "/var/backups/oaap" in html, "nur nicht aenderbar")

print("")
print("Aufbewahrung: eine Zahl mit einer Untergrenze und einem Grund")

ok("das Feld ist da", 'name="keep" value="2"' in html, html[:900])
ok("mindestens 1 -- null waere kein Rueckweg", 'min="1"' in form, form)
ok("und die Seite sagt, warum nicht null",
   "sofort nach der Übertragung" in html, html)

print("")
print("Aus- und wieder einschalten")

ok("es gibt einen Ausschalter", 'value="off"' in form, form)
clear()
write("backup-schedule.json", dict(SCHED, enabled=False))
off = render()
ok("ein ausgeschalteter Zeitplan sagt das",
   "ausgeschaltet" in off, off[:400])
ok("und wird nicht mit 'nie eingerichtet' verwechselt",
   "noch nie" not in off.replace("noch nie gesichert", ""), off[:400])
ok("der Knopf heisst dann anders", "Wieder einschalten" in off, off[:900])
ok("und behauptet keinen naechsten Lauf", "nächster Lauf" not in off, off[:400])

print("")
print("Wer nicht darf, sieht kein Formular")

clear()
write("backup-schedule.json", SCHED)
write("backup-last.json", RUN)
ROLES["v"] = {"partner"}
low = render()
ok("ein partner sieht den Zustand", "Geplant" in low, low[:300])
ok("aber kein Formular", "/backup/schedule" not in low, low[:600])
ok("und keinen Ausschalter", 'value="off"' not in low)
ROLES["v"] = {"server_admin"}

print("")
print("Ohne Zeitgeber gibt es nichts zu aendern")

clear()
write("backup-last.json", RUN)
none = render()
ok("die Seite sagt, dass keiner eingerichtet ist",
   "Kein Zeitplan" in none, none[:400])
ok("und bietet kein Formular an", "/backup/schedule" not in none,
   "einen Zeitplan ANLEGEN entscheidet, wohin gesichert wird")
ok("sondern nennt den Weg an der Maschine",
   "install-backup-timer.sh" in none)

print("")
print("Und der Knoten laesst sich das nicht aus dem Portal diktieren")

ok("der Host prueft die Rolle selbst, nicht nur der Knopf",
   'act_role != "server_admin"' in
   APPCTL[APPCTL.index('elif action == "backup-schedule"'):
          APPCTL.index('elif action == "backup-schedule"') + 1600],
   "der Spool ist Daten, kein Vertrauen")
ok("die Begruendung nennt den Grund, nicht nur die Regel",
   "stops every app on this" in APPCTL,
   "'erfordert server_admin' allein erklaert niemandem, warum")
ok("der Host nimmt kein Ziel aus dem Spool entgegen",
   '"target"' not in APPCTL[APPCTL.index('elif action == "backup-schedule"'):
                            APPCTL.index('elif action == "backup-schedule"') + 1600],
   "sonst waere die Entscheidung 'nicht im Portal' eine Anzeigefrage")
ok("und er legt keinen Zeitgeber an, den es nicht gibt",
   "has no nightly backup yet" in APPCTL)
ok("systemd ist die Wahrheit, die Datei nur die Ansicht",
   "TimersCalendar" in APPCTL and "ActiveState" in APPCTL,
   "sonst behauptet die Seite einen Zeitplan, der nicht scharf ist")
ok("und der Zeitplan wird per drop-in gesetzt, nicht in der ops-Unit",
   "portal.conf" in APPCTL, "zwei Schreiber einer Datei waeren der Fehler")
# Am laufenden Knoten aufgefallen: Der Code stand da, die Kommandozeile
# kannte ihn nicht -- `oaap backup schedule` antwortete "invalid choice".
# Eine Faehigkeit, die man nicht aufrufen kann, ist keine.
ok("'schedule' ist eine gueltige Aktion von 'oaap backup'",
   'choices=["create", "schedule"]' in APPCTL,
   "sonst steht die Funktion da und niemand kommt an sie heran")
ok("und cmd_backup reicht sie weiter",
   'if args.action == "schedule"' in APPCTL)
ok("mit dem leeren OnCalendar davor",
   '"OnCalendar=",' in APPCTL,
   "systemd sammelt OnCalendar -- sonst sichert der Knoten zu zwei Uhrzeiten")

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
