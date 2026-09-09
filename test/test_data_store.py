#!/usr/bin/env python3
"""Der Store trägt nichts, bis ein Profil es sagt (oaap.data.store 0.1).

RFC-0031 Schritt 1: ein Postgres je Knoten, gattert hinter dem
Knotenprofil `store` (RFC-0011). Diese Datei verteidigt, was ohne
Docker prüfbar ist -- die Entscheidungslogik, nicht den Container:

    Ohne das Profil sagt der Befehl "nicht getragen", nie ein Fehler.
    Mit Profil, aber ohne laufenden Dienst, wird jede Aktion verweigert
    -- nie ein `docker exec` gegen etwas, das nicht da ist.
    `oaap store` (Paket-Store, RFC-0012) und `oaap data store`
    (dieser Dienst) sind zwei verschiedene Befehle -- die Kollision,
    die beim Bauen gefunden wurde, darf nicht zurückkommen.
    `remove-profile store` verweigert, solange noch Schemas bestehen
    -- geprüft über die Fehlermeldung, ohne dass ein Schema je
    existiert haben muss (kein Docker hier).

Was diese Datei NICHT prüfen kann, weil es Postgres bräuchte: dass
`create`/`copy`/`drop`/`restore` tatsächlich ein Schema anlegen,
kopieren oder wiederherstellen. Das gehört auf eine echte Maschine
(`oaap-test`), wie bei `klicktest.py`.

Run: python3 test/test_data_store.py
"""
import os
import sys
import tempfile
from argparse import Namespace

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
DATA = tempfile.mkdtemp(prefix="oaap-data-store-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)

import appctl as m  # noqa: E402

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


def read(name):
    with open(os.path.join(PLATFORM, name), encoding="utf-8") as f:
        return f.read()


def call(fn, capsys_marker="", **kw):
    """Ruft eine cmd_*-Funktion auf und fängt SystemExit (die()) ein.

    Gibt (exited, stdout) zurück. Die stdout-Erfassung ist ein simpler
    Tausch von sys.stdout, kein pytest -- diese Testdatei kommt ohne
    pytest aus, wie die anderen in test/.
    """
    import contextlib
    import io
    buf = io.StringIO()
    exited = False
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            fn(Namespace(**kw))
        except SystemExit:
            exited = True
    return exited, buf.getvalue()


print("=== die Kollision, die beim Bauen gefunden wurde ===")
with open(os.path.join(HERE, "..", "bin", "oaap"), encoding="utf-8") as f:
    oaap_bin = f.read()
ok("'oaap store' (Paket-Store) existiert weiterhin unverändert",
   'store)       exec python3 "$APP_DIR/appctl.py" store "$@"' in oaap_bin)
ok("'oaap data' ist ein EIGENER, neuer Verb -- keine Kollision",
   'data)        exec python3 "$APP_DIR/appctl.py" data "$@"' in oaap_bin)

print("\n=== Profil 'store' ist registriert (RFC-0011) ===")
ok("'store' steht in PROFILES", "store" in m.PROFILES)
ok("'dev' steht weiterhin drin -- store ergänzt, ersetzt nicht",
   "dev" in m.PROFILES and "exposed" in m.PROFILES)

print("\n=== ohne das Profil: 'nicht getragen', nie ein Absturz ===")
exited, out = call(m.cmd_data, object="store", action="status")
ok("status meldet 'not carried' statt eines Fehlers",
   not exited and "not carried" in out, out)
ok("und nennt den Weg dahin",
   "add-profile store" in out, out)

for action, extra in [
    ("schemas", {}),
    ("create", {"arg1": "twin", "arg2": "sometenantid"}),
    ("copy", {"arg1": "a", "arg2": "b"}),
    ("drop", {"arg1": "a", "yes": True}),
    ("restore", {"arg1": "/no/such/file"}),
]:
    kw = {"object": "store", "action": action, "arg1": None, "arg2": None,
          "yes": False}
    kw.update(extra)
    exited, out = call(m.cmd_data, **kw)
    ok(f"'{action}' ohne Profil wird verweigert (kein Docker-Aufruf)",
       exited and "no profile 'store'" in out, out)

print("\n=== Profil hinzufügen, ohne Docker: warnt, stürzt nicht ab ===")
exited, out = call(m.cmd_node, action="add-profile", profile="store")
ok("add-profile store läuft durch (Docker fehlt -- WARNUNG statt Crash)",
   not exited, out)
ok("das Profil ist jetzt gesetzt", m.has_profile("store"))
ok("Warnung, wenn Docker fehlt, oder ein Start ohne Warnung -- beides ok",
   "added" in out.lower() or "warning" in out.lower(), out)

print("\n=== mit Profil, aber Dienst läuft nicht: jede Aktion verweigert ===")
exited, out = call(m.cmd_data, object="store", action="status")
ok("status zeigt 'carried', aber NOT running (kein Docker im Testlauf)",
   not exited and "carried" in out, out)
for action, extra in [
    ("schemas", {}),
    ("create", {"arg1": "twin", "arg2": "sometenantid"}),
]:
    kw = {"object": "store", "action": action, "arg1": None, "arg2": None,
          "yes": False}
    kw.update(extra)
    exited, out = call(m.cmd_data, **kw)
    ok(f"'{action}' verweigert, weil der Dienst nicht läuft",
       exited and "not running" in out, out)

print("\n=== remove-profile verweigert bei bestehenden Schemas ===")
# Ohne Docker liefert store_schemas() immer [] (siehe _store_running),
# also kann diese Datei die Verweigerung selbst nicht auslösen -- sie
# prüft stattdessen, dass der Code den Fall überhaupt kennt, bevor er
# das Profil entfernt.
ok("cmd_node prüft store_schemas() vor dem Entfernen",
   "store_schemas()" in read("appctl.py").split("def cmd_node")[1].split(
       "def read_manifest_version")[0])
exited, out = call(m.cmd_node, action="remove-profile", profile="store")
ok("ohne bestehende Schemas (kein Docker: store_schemas() == []) "
   "geht das Entfernen durch",
   not exited, out)
ok("und das Profil ist wieder weg", not m.has_profile("store"))

print("\n=== Compose-Dienst: gattert echt, kein Port veröffentlicht ===")
import yaml  # noqa: E402
with open(os.path.join(PLATFORM, "docker-compose.yml"), encoding="utf-8") as f:
    compose = yaml.safe_load(f)
store_svc = compose.get("services", {}).get("store")
ok("Dienst 'store' existiert in docker-compose.yml", store_svc is not None)
if store_svc:
    ok("trägt 'profiles: [store]' -- startet nicht bei einem blossen "
       "'docker compose up -d'", store_svc.get("profiles") == ["store"])
    ok("kein 'ports:' -- nie von aussen erreichbar (spec 2.1/§4)",
       "ports" not in store_svc)
    ok("Healthcheck ist pg_isready",
       "pg_isready" in str(store_svc.get("healthcheck", "")))

print("\n=== migrate.sh: idempotenter Sicherheitsnetz-Schritt ===")
migrate = read("migrate.sh")
ok("migrate.sh prüft das Profil, bevor es den Dienst hochfährt",
   '"store"' in migrate and "--profile store up -d store" in migrate)
ok("migrate.sh reicht STORE_SUPERUSER_PASSWORD auch aktualisierten "
   "Knoten nach -- gefunden auf oaap-test, 09.09.: der erste "
   "'add-profile store' auf einem AKTUALISIERTEN (nicht frisch "
   "installierten) Knoten crash-loopte ohne diesen Schritt, weil "
   "install.sh das Secret nur bei einer Neuinstallation erzeugt",
   "STORE_SUPERUSER_PASSWORD=" in migrate)
ok("migrate.sh entscheidet mit pg_isready, nicht mit einer "
   "'.State.Running'-Abfrage -- ebenfalls auf oaap-test gefunden: ein "
   "Crash-Loop-Container flackert zwischen Running=true und "
   "Running=false, eine darauf gebaute Prüfung kann die Reparatur im "
   "falschen Moment verpassen",
   "docker exec oaap-store-1 pg_isready" in migrate
   and "docker inspect -f '{{.State.Running}}'" not in migrate)

print("\n=== _store_running() fragt pg_isready, nicht .State.Running ===")
_appctl_src = read("appctl.py")
ok("appctl.py hat dieselbe Race NICHT (derselbe Fund, dieselbe Lehre)",
   "pg_isready" in _appctl_src.split("def _store_running")[1].split(
       "def _store_psql")[0])

print("\n=== 'status' unterscheidet 'nicht erreichbar' von 'nicht laufend' ===")
_cmd_data_body = read("appctl.py").split("def cmd_data")[1].split("\ndef ")[0]
ok("cmd_data prüft auf 'permission denied' getrennt von einem echten "
   "pg_isready-Fehlschlag -- gefunden auf oaap-test: ohne sudo/'docker'-"
   "Gruppe meldete status faelschlich 'NOT running' fuer einen "
   "kerngesunden Container, weil 'docker exec' selbst schon an der "
   "Berechtigung scheiterte, nicht an Postgres",
   "permission denied" in _cmd_data_body)

print("\n=== 'status'/'schemas' lesen ohne root, wie 'store list'/'node show' ===")
appctl_src = read("appctl.py")
main_body = appctl_src[appctl_src.index("read_only = ("):]
main_body = main_body[:main_body.index("if not read_only")]
ok("die read_only-Liste in main() kennt 'data status/schemas' -- ohne "
   "diesen Eintrag verlangt jeder Lesebefehl root, entgegen 'store "
   "list' und 'node show' (gefunden beim Prüfen auf oaap-test)",
   '"data"' in main_body and '"status", "schemas"' in main_body)

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
