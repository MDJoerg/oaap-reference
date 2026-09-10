#!/usr/bin/env python3
"""Ein profiliertes Bild wird von 'oaap update' nie neu gebaut.

Gefunden live auf oaap-test, 2026-09-10, beim Prüfen von Schritt 5
(RFC-0031, der Zwillings-Browser): `docker compose build`/`up -d` OHNE
`--profile` lässt jeden Dienst mit einer 'profiles:'-Zeile (`store`,
`twin` — RFC-0011) komplett unberührt, auch wenn er längst läuft. Der
Knoten hatte zwei Runden lang ein neues `oaap.data.twin` 0.2 ausgerollt
bekommen und `twin` lief die ganze Zeit mit dem ALTEN Bild weiter —
jede neue Route antwortete mit Flasks eigenem 404, nicht mit einer
Fehlermeldung des Dienstes. `migrate.sh`s bestehende Prüfung startet
den Container nur, wenn er FEHLT; ein laufender wird nie neu gebaut.

Run: python3 test/test_update_profiles.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {detail}")


with open(os.path.join(PLATFORM, "update.sh"), encoding="utf-8") as f:
    SRC = f.read()

print("=== PROFILE_ARGS wird aus node.json gelesen, robust gegen ein "
      "fehlendes oder frisches Feld ===")
ok("PROFILE_ARGS ist ein Bash-Array, leer initialisiert (unter "
   "'set -euo pipefail' sicher, auch wenn kein Profil aktiv ist)",
   "PROFILE_ARGS=()" in SRC)
ok("gelesen aus apps/node.json, nicht neu erraten",
   '"$OAAP_DATA_DIR/apps/node.json"' in SRC.split("PROFILE_ARGS=()", 1)[1][:600])
ok("ein fehlendes oder kaputtes node.json liefert keine Profile, "
   "bricht das Update aber nicht ab (kein 'set -e'-Abbruch)",
   "except Exception:\n    pass" in SRC)

print("\n=== build UND up -d bekommen '--profile' fuer JEDES getragene "
      "Profil -- nicht nur eines mit Namen ===")
build_line = SRC.split("build --quiet", 1)[0].splitlines()[-2:]
ok("'build' nimmt PROFILE_ARGS mit",
   any('"${PROFILE_ARGS[@]}"' in l for l in build_line), build_line)
restart_idx = SRC.find('say "Restarting core services ..."')
restart_block = SRC[restart_idx:restart_idx + 200]
ok("'up -d' beim Neustarten nimmt PROFILE_ARGS ebenfalls mit -- "
   "derselbe Fund gilt fuer das Neustarten wie fuer das Bauen",
   '"${PROFILE_ARGS[@]}"' in restart_block and "up -d" in restart_block,
   restart_block)

print("\n=== keine feste Profil-Liste, die ein zukuenftiges Profil "
      "wieder vergisst ===")
ok("kein 'store' oder 'dev' als Literal in der neuen Logik -- die "
   "Profile kommen ausschliesslich aus node.json",
   not re.search(r'PROFILE_ARGS\+=\(--profile "(store|dev)"\)', SRC))

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
