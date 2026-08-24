#!/usr/bin/env python3
"""Wo Reparaturschritte stehen dürfen — und wo nicht.

Der Fehler, den das hier festhält (gefunden auf oaap-demo, 2026-08-09):
`oaap update` führt die update.sh aus, die beim Tippen des Befehls auf
der Platte lag — die **alte**. Ein Schritt, den eine neue Fassung dort
hinter dem Kopieren einbaut, wird deshalb von genau dem Update
übersprungen, das ihn einführt. Und danach nie mehr, weil der nächste
Lauf als „bereits aktuell" früh aussteigt.

oaap-demo sprang so von 0.1.18 auf 0.1.26 und bekam weder die
Quellen-Migration (RFC-0012 §4) noch die Deploy-Worker-Reparatur. Beide
waren geschrieben, geprüft und ausgeliefert. Der Knoten hat sie nur nie
ausgeführt, und nichts hat das gesagt.

Deshalb: Reparaturschritte gehören in `platform/migrate.sh`, das aus
`$APP_DIR` — also aus dem **neuen** Stand — aufgerufen wird, und zwar
auch dann, wenn es nichts zu aktualisieren gibt.

Run: python3 test/test_migrate.py
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


def read(name):
    with open(os.path.join(PLATFORM, name), encoding="utf-8") as f:
        return f.read()


ok("es gibt platform/migrate.sh",
   os.path.isfile(os.path.join(PLATFORM, "migrate.sh")))

update = read("update.sh")
migrate = read("migrate.sh")

print("\n=== der Aufruf kommt aus dem NEUEN Stand ===")
ok("update.sh ruft migrate.sh unter $APP_DIR auf",
   '"$APP_DIR/migrate.sh"' in update,
   "sonst liefe die alte Fassung — genau der Fehler von oaap-demo")
ok("und nicht aus dem Quellverzeichnis",
   '"$SRC/platform/migrate.sh"' not in update and '$SRC/migrate.sh' not in update)

print("\n=== auf beiden Wegen ===")
calls = update.count("\n  run_migrations") + update.count("\nrun_migrations")
ok("run_migrations wird zweimal gerufen: nach dem Kopieren und im "
   "Bereits-aktuell-Zweig", calls == 2, f"gefunden: {calls}")
uptodate = update[:update.find("Already up to date")]
ok("der Bereits-aktuell-Zweig ruft es VOR seiner Meldung",
   "run_migrations" in uptodate[uptodate.rfind("if [ \"$CHECK\" -eq 0 ]"):],
   "sonst bleibt ein Knoten, der einen Schritt verpasst hat, für immer schief")
ok("aber nicht bei --check, das nichts ändern darf",
   re.search(r'if \[ "\$CHECK" -eq 0 \];\s*then\s*\n\s*run_migrations',
             update) is not None)

print("\n=== die Schritte stehen nur noch an einer Stelle ===")
# Ein Schritt, der wieder in update.sh wandert, ist der Rückfall.
for step, marker in (("Ratengrenze des Deploy-Workers", "StartLimitIntervalSec"),
                     ("Abgleich der Store-Quellen", "store reconcile")):
    ok(f"{step}: in migrate.sh", marker in migrate)
    ok(f"{step}: NICHT mehr in update.sh", marker not in update,
       "ein Schritt in update.sh wird von dem Update übersprungen, "
       "das ihn einführt")

print("\n=== migrate.sh darf jederzeit laufen ===")
# Es läuft bei JEDEM `oaap update`, auch bei denen, die nichts ändern.
ok("es ist gegen Nicht-root abgesichert", "id -u" in migrate)
ok("die Unit-Reparatur ist bedingt und damit wiederholbar",
   "! grep -q '^StartLimitIntervalSec='" in migrate)
ok("es fasst nichts an, was es nicht vorfindet",
   '[ -f "$UNIT" ]' in migrate)
ok("und es bricht den Update-Lauf nicht ab, wenn ein Schritt scheitert",
   "|| say" in update[update.find("run_migrations()"):][:600], "")

print("\n=== was ein Container beim Neuerzeugen verliert, muss zurückkommen ===")
# Derselbe Fehlertyp wie oben, gefunden auf oaapx01 am 2026-08-24:
# `oaap app link add ai-gateway ollama` legte das Netz an und hängte
# beide Container hinein — und die nächste Konfigurationsänderung am
# Gateway erzeugte dessen Container neu. `docker run` kennt genau EIN
# Netz, also war die Verbindung danach still tot: in der Registry
# eingetragen, das Netz vorhanden, der Container nicht mehr daran.
# Auffallen kann das erst, wenn die andere App gerufen wird.
#
# Für das Gateway-Netz war die Lehre schon gezogen (`connect_gateway`
# wird auf jedem erzeugenden Weg gerufen); für App-zu-App-Verbindungen
# fehlte sie. Diese Prüfung hält beides zusammen.
appctl = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
body = appctl[appctl.index("def recreate_instance_containers("):]
body = body[:body.index("\nENDPOINT_PORT_RANGE")]
ok("der erzeugende Weg holt das Gateway zurück ins Instanznetz",
   "connect_gateway(" in body)
ok("und ebenso die erklärten App-zu-App-Verbindungen",
   "restore_links(" in body,
   "recreate_instance_containers ruft restore_links nicht — jede "
   "Konfigurationsänderung tötet damit die Links dieser Instanz")
ok("restore_links gibt es auch wirklich", "def restore_links(" in appctl)
ok("es fragt die Registry nach den Partnern",
   "app_link_partners(" in appctl[appctl.index("def restore_links("):]
   [:appctl[appctl.index("def restore_links("):].index("\ndef reconcile_links")])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
