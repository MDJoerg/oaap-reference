#!/usr/bin/env python3
"""Der Wrapper `bin/oaap` als zweiter Weg zu appctl.py (0.1.114).

Gefunden beim Vorbereiten des Flottenlaufs auf 0.1.113: RFC-0034 sagt an
drei Stellen `oaap files verify` zu -- und genau diese Schreibweise
lehnte der Knoten ab. `files` war als Unterbefehl in appctl.py angelegt,
aber `bin/oaap` kannte ihn nicht; erreichbar war nur `oaap app files`,
weil die Zeile `app)` alles ungefiltert durchreicht.

Das ist wieder die bekannte Gestalt (vgl. 0.1.109/0.1.110/0.1.111):
**zwei Wege zum selben Ort, einer traegt die Regel nicht.** Hier ist der
zweite Weg eine Shell-`case`-Liste, die Wissen WIEDERHOLT, das argparse
schon haelt -- und eine Wiederholung laeuft auseinander, sobald niemand
sie zaehlt. Genau das zaehlt diese Datei.

Nicht geprueft wird, ob JEDER Unterbefehl von appctl.py eine eigene
Zeile hat: die meisten sind absichtlich app-bezogen und werden ueber
`oaap app <cmd>` erreicht (install, list, logs, promote ...). Geprueft
wird das, was hier tatsaechlich auseinanderlaeuft:

1. Die knotenweiten Faehigkeiten haben eine eigene Zeile. `files` steht
   ausdruecklich dabei, mit dem RFC als Begruendung.
2. Jede Zeile kommt auch in der Hilfe vor. Ein Befehl, den nur findet,
   wer den Quelltext liest, ist halb gebaut -- und das ist die Haelfte,
   die beim Nachruesten regelmaessig vergessen wird.

Braucht nichts: kein Docker, kein Netz, keinen Knoten.

Aufruf: python3 test/test_cli_routes.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "..", "bin", "oaap")
APPCTL = os.path.join(HERE, "..", "platform", "appctl.py")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


SRC = open(WRAPPER, encoding="utf-8").read()
CTL = open(APPCTL, encoding="utf-8").read()

# Die `case`-Zweige: '  backup)      exec python3 ...'
routes = set(re.findall(r'^\s{2}([a-z][a-z-]*)\)\s', SRC, re.M))
# Die Hilfe-Zeile am Ende, aus der ein Anwender die Befehle erfaehrt.
usage = re.search(r'echo "Usage: oaap \[(.*?)\]" >&2', SRC, re.S)
usage_text = usage.group(1) if usage else ""

ok("bin/oaap hat eine Hilfe-Zeile", bool(usage),
   "ohne sie kann diese Datei nichts pruefen -- und der Anwender auch nichts finden")

# --- 1. die knotenweiten Faehigkeiten ------------------------------------
# Jede davon ist eine Faehigkeit DES KNOTENS oder DES MANDANTEN, keine
# Eigenschaft einer einzelnen Instanz -- deshalb steht sie neben `app`
# und nicht darunter. Wer diese Liste erweitert, erweitert auch bin/oaap.
NODE_LEVEL = ["backup", "data", "edge", "external", "fleet", "files",
              "key", "machine", "node", "store", "tenant", "user"]

for cmd in NODE_LEVEL:
    ok(f"'oaap {cmd}' wird weitergereicht", cmd in routes,
       f"nur 'oaap app {cmd}' wuerde gehen -- und das sagt kein RFC")

# `files` ist der Fall, der es aufgedeckt hat: das RFC nennt die
# Schreibweise woertlich, also muss sie auch die sein, die funktioniert.
ok("und 'files' ruft dieselbe Stelle wie die anderen",
   bool(re.search(r'^\s{2}files\)\s+exec python3 "\$APP_DIR/appctl\.py" '
                  r'files "\$@" ;;', SRC, re.M)),
   "sonst ist es ein dritter Weg statt derselbe")

ok("appctl.py kennt 'files' ueberhaupt",
   'sub.add_parser("files"' in CTL,
   "die Route zeigt sonst auf einen Befehl, den es nicht gibt")

# --- 2. keine Zeile ohne Hilfe -------------------------------------------
# Ausnahmslos: auch die im Wrapper selbst gebauten Befehle (status,
# version, uninstall) stehen dort schon -- es gibt keinen Grund fuer
# eine Ausnahmeliste, und eine Ausnahmeliste waere die naechste Stelle,
# die auseinanderlaeuft.
for cmd in sorted(routes):
    ok(f"'{cmd}' steht in der Hilfe", cmd in usage_text,
       "ein Befehl, den nur der Quelltext kennt, ist halb gebaut")

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
