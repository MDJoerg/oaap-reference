#!/usr/bin/env python3
"""Bleibt eine Instanz beim Umzug am alten Datenpfad haengen? (RFC-0026)

Fund vom 2026-09-05, auf vier Knoten gleichzeitig: Die Migration
verschiebt die Instanzdaten in den Mandantenbaum und **erzeugt die
Container neu, damit der Bind-Mount mitgeht** -- aber nur fuer
Instanzen mit einer `services`-Liste, also nur fuer die seit 0.1.31
installierten. Alle aelteren behielten ihren alten Mount.

Der Kommentar direkt ueber der Zeile sagte voraus, was dann passiert:
*"a container keeps writing to the moved directory and nothing breaks
-- until someone restarts it, whereupon Docker re-resolves the OLD
path, creates it empty, and the app looks wiped."* Genau so kam es, und
getroffen hat es die **aeltesten** Instanzen -- auf oaap-bernd Bernds
CRM, auf oaap-test Forgejo und Vaultwarden.

Und `instance_services()` gibt es genau dafuer; sein eigener Docstring
sagt "so every caller (recreate, migrate, config) treats old and new
the same". Der Aufrufer stellte dann die Frage, die diese Funktion
abschaffen sollte.

Zwei Saetze werden hier verteidigt:

    Eine Instanz ohne `services`-Liste wird beim Umzug genauso
    neu erzeugt wie eine mit.
    Ein Knoten, den es schon getroffen hat, heilt beim naechsten
    Update -- und loescht dabei nichts.

Braucht kein Docker (die Docker-Aufrufe sind ersetzt) und keinen Knoten.

Run: python3 test/test_migrate_stale_mounts.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-stale-mount-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

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


def capture(fn, *a):
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fn(*a)
    except SystemExit:
        pass
    return buf.getvalue()


# Docker gibt es hier nicht. Was der Test braucht, ist genau zwei
# Auskuenfte: welchen Pfad ein Container gemountet hat (die Frage, die
# der Reparaturschritt stellt) und dass ein Neuerzeugen stattfand.
MOUNTS = {}
RECREATED = []
m.shutil.which = lambda x: "/usr/bin/docker" if x == "docker" else None
m.reload_gateway = lambda: None
m.refresh_generated_sites = lambda: None


def fake_inspect(cmd, **kw):
    if cmd[:2] == ["docker", "inspect"]:
        container = cmd[-1]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="\n".join(MOUNTS.get(container, [])) + "\n", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


m.subprocess.run = fake_inspect


def fake_recreate(key, services, storage, endpoints, inst=None):
    RECREATED.append(key)
    # Ein echtes Neuerzeugen loest den Mount neu auf -- hier
    # nachgebildet, damit der zweite Lauf sieht, was der erste tat.
    for s in services:
        MOUNTS[s["container"]] = [
            os.path.join(m.instance_dir(key, inst), "storage", "data")]


m.recreate_instance_containers = fake_recreate

tid = m.ensure_default_tenant()

# Zwei Instanzen, die sich nur in einem Punkt unterscheiden: die eine
# hat eine services-Liste (seit 0.1.31), die andere nicht (aelter).
# Genau dieser Unterschied hat die Luecke gemacht.
reg = {"instances": {
    "neu": {"app_id": "a", "app_name": "Neu", "version": "1", "channel": "test",
            "container": "oaap-app-neu", "image": "x", "port": 8100,
            "svc_port": 80, "tenant": tid, "storage": [{"name": "data"}],
            "services": [{"service": "", "container": "oaap-app-neu",
                          "image": "x", "build": "", "port": 80}]},
    "alt": {"app_id": "b", "app_name": "Alt", "version": "1", "channel": "test",
            "container": "oaap-app-alt", "image": "y", "port": 8101,
            "svc_port": 80, "tenant": tid, "storage": [{"name": "data"}]},
}, "retained": {}}
m.save_registry(reg)

for key in ("neu", "alt"):
    d = os.path.join(m.APPS_DIR, key, "storage", "data")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "wichtig.txt"), "w", encoding="utf-8") as f:
        f.write(f"Die gewachsenen Daten von {key}.\n")
    MOUNTS[f"oaap-app-{key}"] = [d]

print("=== der Umzug erzeugt BEIDE neu, nicht nur die junge ===")
out = capture(m.cmd_migrate_instance_dirs, None)
reg = m.load_registry()
ok("beide haben jetzt eine Identitaet",
   all(reg["instances"][k].get("id") for k in ("neu", "alt")))
ok("die Daten liegen im Mandantenbaum",
   all(os.path.isfile(os.path.join(m.instance_dir(k, reg["instances"][k]),
                                   "storage", "data", "wichtig.txt"))
       for k in ("neu", "alt")))
ok("die alte, gewachsene Instanz wurde mit neu erzeugt -- das war die Luecke",
   "alt" in RECREATED, str(RECREATED))
ok("die junge auch", "neu" in RECREATED, str(RECREATED))
ok("und der Umzug sagt, dass er zwei neu erzeugt hat",
   "2 instance(s) recreated" in out, out[-300:])

print("\n=== ein Knoten, den es schon getroffen hat, heilt ===")
DATA2 = tempfile.mkdtemp(prefix="oaap-stale-mount-test2-")
os.environ["OAAP_DATA_DIR"] = DATA2
import importlib  # noqa: E402
m = importlib.reload(m)
m.shutil.which = lambda x: "/usr/bin/docker" if x == "docker" else None
m.reload_gateway = lambda: None
m.refresh_generated_sites = lambda: None
m.subprocess.run = fake_inspect
m.recreate_instance_containers = fake_recreate
MOUNTS.clear()
RECREATED.clear()

# Der Zustand nach dem Fehler: Daten schon im Baum, Instanz hat ihre
# Identitaet -- der Container liest aber weiter den alten Pfad, und der
# alte Pfad ist inzwischen mit frisch angelegtem Leerzustand gefuellt.
tid2 = m.ensure_default_tenant()
iid = "abcdef123456"
reg2 = {"instances": {"alt": {
    "app_id": "b", "app_name": "Alt", "version": "1", "channel": "test",
    "container": "oaap-app-alt", "image": "y", "port": 8101, "svc_port": 80,
    "tenant": tid2, "id": iid, "storage": [{"name": "data"}]}}, "retained": {}}
m.save_registry(reg2)
echt = os.path.join(m.instance_dir("alt", reg2["instances"]["alt"]),
                    "storage", "data")
os.makedirs(echt, exist_ok=True)
with open(os.path.join(echt, "wichtig.txt"), "w", encoding="utf-8") as f:
    f.write("Die gewachsenen Daten.\n")
falsch = os.path.join(m.APPS_DIR, "alt", "storage", "data")
os.makedirs(falsch, exist_ok=True)
with open(os.path.join(falsch, "frisch-und-leer.txt"), "w", encoding="utf-8") as f:
    f.write("Was die App nach dem Neustart ins Nichts geschrieben hat.\n")
MOUNTS["oaap-app-alt"] = [falsch]

out = capture(m.cmd_migrate_instance_dirs, None)
ok("die Reparatur greift", "alt" in RECREATED, str(RECREATED))
ok("und sagt, was sie getan hat",
   "Repairing instances left on the old data path" in out, out[-400:])
ok("der Container liest jetzt aus dem Mandantenbaum",
   MOUNTS["oaap-app-alt"] == [echt], str(MOUNTS))
ok("die gewachsenen Daten sind unangetastet",
   os.path.isfile(os.path.join(echt, "wichtig.txt")))
beiseite = [d for d in os.listdir(m.APPS_DIR) if d.startswith("alt.abgeloest-")]
ok("der alte Pfad wurde beiseitegelegt, nicht geloescht",
   len(beiseite) == 1, str(os.listdir(m.APPS_DIR)))
ok("und sein Inhalt ist noch da -- niemand loescht hier etwas",
   beiseite and os.path.isfile(os.path.join(
       m.APPS_DIR, beiseite[0], "storage", "data", "frisch-und-leer.txt")))
ok("der Bericht sagt ausdruecklich, dass nichts geloescht wurde",
   "nothing deleted" in out, out[-400:])

print("\n=== der zweite Lauf ist still ===")
RECREATED.clear()
out = capture(m.cmd_migrate_instance_dirs, None)
ok("nichts mehr zu reparieren", RECREATED == [], str(RECREATED))
ok("und kein Wort darueber", "Repairing instances" not in out, out[-200:])

print("\n=== wo der Umzug selbst scheiterte, wird nichts angefasst ===")
# Der gefaehrliche Fall: keine Instanz im Baum. Dann kann der alte Pfad
# die einzige Kopie sein, und Beiseitelegen waere genau der Unfall, den
# diese Funktion verhindern soll.
reg3 = m.load_registry()
reg3["instances"]["gescheitert"] = {
    "app_id": "c", "app_name": "X", "version": "1", "channel": "test",
    "container": "oaap-app-gescheitert", "image": "z", "port": 8102,
    "svc_port": 80, "tenant": tid2, "id": "999888777666",
    "storage": [{"name": "data"}]}
m.save_registry(reg3)
nur_alt = os.path.join(m.APPS_DIR, "gescheitert", "storage", "data")
os.makedirs(nur_alt, exist_ok=True)
with open(os.path.join(nur_alt, "einzige-kopie.txt"), "w", encoding="utf-8") as f:
    f.write("Wenn das verschwindet, ist es weg.\n")
MOUNTS["oaap-app-gescheitert"] = [nur_alt]
RECREATED.clear()
out = capture(m.cmd_migrate_instance_dirs, None)
ok("eine Instanz ohne Verzeichnis im Baum wird nicht angefasst",
   "gescheitert" not in RECREATED, str(RECREATED))
ok("und ihre einzige Kopie liegt unveraendert da",
   os.path.isfile(os.path.join(nur_alt, "einzige-kopie.txt")))

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
