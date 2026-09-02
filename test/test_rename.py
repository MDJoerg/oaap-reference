#!/usr/bin/env python3
"""Eine Instanz laesst sich umbenennen, und die Aussenwelt folgt.

Bis 0.1.59 ging das gar nicht -- ein Name war hier nicht nur ein Name,
sondern auch die Ablage. Seit RFC-0026 haengen die Daten an einer
Identitaet, und damit kostet eine Umbenennung einen Neustart statt einer
Datenmigration.

Geprueft wird die Regel, nicht Docker:

    Der Name aendert sich, die Adresse folgt, die Identitaet nicht.
    Der alte Name antwortet die Schonfrist ueber weiter -- die Adresse
    UND die Deploy-Adresse.
    Auf der Platte wird nichts verschoben.

Der letzte Satz ist der Grund, warum das ueberhaupt anbietbar ist.

Aufruf: python3 test/test_rename.py
"""
import argparse
import contextlib
import io as _io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-rename-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0
HOST = "oaap.example.org"


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def run(fn, *a):
    buf = _io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fn(*a)
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue(), code


def make_tenant(label):
    with contextlib.redirect_stdout(_io.StringIO()):
        m.cmd_tenant(argparse.Namespace(
            action="create", name=label, target=None, title="Kunde",
            account="", account_name="", grace_days=30, yes=True, count=50))
    return m.tenant_by_label(label)[0]


def put_instance(key, tid, name, iid):
    reg = m.load_registry()
    reg["instances"][key] = {
        "app_id": "demo", "app_name": "Demo", "tenant": tid, "name": name,
        "id": iid, "version": "1.0", "channel": "test", "port": 8600,
        "container": f"oaap-app-{key}", "svc_port": 8000, "image": "demo:1",
        "services": [{"service": "web", "container": f"oaap-app-{key}",
                      "image": "demo:1", "port": 8000}],
        "routes": [{"path": "/", "roles": ["user"]}]}
    m.save_registry(reg)
    return reg["instances"][key]


def Args(name, target, grace=30, yes=False):
    return argparse.Namespace(name=name, target=target, grace_days=grace,
                              yes=yes)


# Docker laeuft hier nicht; die Regeln oben sind der Gegenstand.
m.recreate_instance_containers = lambda *a, **k: None
m.remove_app_network = lambda *a, **k: None
m.write_external_caddy = lambda *a, **k: None

os.makedirs(m.APPS_DIR, exist_ok=True)
with open(os.path.join(m.APPS_DIR, "external.json"), "w") as f:
    f.write('{"host": "%s"}' % HOST)

default_id = m.ensure_default_tenant()
cls_id = make_tenant("cls")
inst = put_instance("cls-viewer", cls_id, "viewer", "a1b2c3d4e5f6")
daten = m.instance_dir("cls-viewer", inst)
os.makedirs(os.path.join(daten, "storage", "data"), exist_ok=True)
with open(os.path.join(daten, "storage", "data", "kunde.txt"), "w") as f:
    f.write("wichtig")
m.save_env("cls-viewer", {"API_KEY": "geheim"}, inst)

print("")
print("Vor der Tat wird gesagt, was sie kostet")

out, code = run(m.cmd_rename, Args("viewer", "modelle"))
ok("ohne --yes wird nichts geaendert",
   code != 0 and "cls-viewer" in m.load_registry()["instances"], out)
ok("die Vorschau nennt die alte und die neue Adresse",
   "viewer.cls." + HOST in out and "modelle.cls." + HOST in out, out)
ok("und die alte und die neue Deploy-Adresse",
   "/deploy/cls-viewer" in out and "/deploy/cls-modelle" in out, out)
ok("sie sagt, dass die App neu startet",
   "restarts" in out, out)
ok("und dass die Daten NICHT bewegt werden",
   "is not moved" in out, out)
ok("die Schonfrist steht dabei", "30 more day(s)" in out, out)

print("")
print("Umbenannt: der Name aendert sich, die Identitaet nicht")

out, code = run(m.cmd_rename, Args("viewer", "modelle", yes=True))
reg = m.load_registry()
ok("die Instanz traegt den neuen Schluessel",
   "cls-modelle" in reg["instances"] and "cls-viewer" not in reg["instances"],
   sorted(reg["instances"]))
neu = reg["instances"]["cls-modelle"]
ok("und den neuen Namen", neu["name"] == "modelle")
ok("die Identitaet ist dieselbe geblieben", neu["id"] == "a1b2c3d4e5f6",
   "an ihr haengen die Daten")

print("")
print("Auf der Platte wurde nichts verschoben")

ok("dasselbe Verzeichnis wie vorher",
   m.instance_dir("cls-modelle", neu) == daten)
ok("die Kundendatei liegt unberuehrt darin",
   open(os.path.join(daten, "storage", "data", "kunde.txt")).read() == "wichtig")
ok("und das Geheimnis ebenso",
   m.load_env("cls-modelle", neu).get("API_KEY") == "geheim")

print("")
print("Die Aussenwelt folgt -- und der alte Name bleibt eine Weile")

adressen = m.instance_auto_hosts("cls-modelle", neu, ext_host=HOST)
ok("die neue Adresse ist die erste", adressen[0] == "modelle.cls." + HOST,
   adressen)
ok("die alte antwortet die Schonfrist ueber weiter",
   "viewer.cls." + HOST in adressen, adressen)
ok("die alte Deploy-Adresse findet die Instanz noch",
   m.resolve_deploy_target(m.load_registry(), "cls-viewer") == "cls-modelle")
ok("die Identitaet ist als Deploy-Adresse immer gueltig",
   m.resolve_deploy_target(m.load_registry(), "a1b2c3d4e5f6") == "cls-modelle",
   "sie aendert sich nie, also muss niemand eine KI nachtraeglich "
   "informieren")
ok("ein Name, den es nie gab, findet nichts",
   m.resolve_deploy_target(m.load_registry(), "gibtesnicht") == "")

print("")
print("Nach der Schonfrist ist der alte Name weg")

reg = m.load_registry()
reg["instances"]["cls-modelle"]["former_names"] = [
    {"name": "viewer", "until": "2000-01-01T00:00:00+00:00"}]
reg["instances"]["cls-modelle"]["former_keys"] = [
    {"key": "cls-viewer", "until": "2000-01-01T00:00:00+00:00"}]
m.save_registry(reg)
spaet = reg["instances"]["cls-modelle"]
ok("die Adresse antwortet nicht mehr",
   "viewer.cls." + HOST
   not in m.instance_auto_hosts("cls-modelle", spaet, ext_host=HOST))
ok("und die Deploy-Adresse auch nicht",
   m.resolve_deploy_target(reg, "cls-viewer") == "")
ok("die Identitaet gilt weiterhin",
   m.resolve_deploy_target(reg, "a1b2c3d4e5f6") == "cls-modelle")

print("")
print("Was abgelehnt wird")

out, code = run(m.cmd_rename, Args("modelle", "modelle", yes=True))
ok("derselbe Name noch einmal", code != 0 and "already its name" in out, out)
out, code = run(m.cmd_rename, Args("modelle", "Gross Schrieben", yes=True))
ok("ein ungueltiger Name", code != 0, out)
out, code = run(m.cmd_rename, Args("gibtesnicht", "egal", yes=True))
ok("eine Instanz, die es nicht gibt", code != 0 and "no instance" in out, out)

put_instance("cls-zweite", cls_id, "zweite", "bbbbbbbbbbbb")
out, code = run(m.cmd_rename, Args("modelle", "zweite", yes=True))
ok("ein Name, den derselbe Mandant schon fuehrt",
   code != 0 and "already exists" in out, out)

# Ein anderer Mandant darf denselben Namen fuehren -- das ist der Sinn
# des mandantenweiten Namensraums (RFC-0025). Also: ein Name, den NUR
# der andere Mandant fuehrt, darf hier trotzdem vergeben werden.
meier_id = make_tenant("meier")
put_instance("meier-nurmeier", meier_id, "nurmeier", "cccccccccccc")
out, code = run(m.cmd_rename, Args("zweite", "nurmeier", yes=True))
reg = m.load_registry()["instances"]
ok("ein Name, den nur ein ANDERER Mandant fuehrt, ist kein Hindernis",
   code == 0 and "cls-nurmeier" in reg and "meier-nurmeier" in reg,
   sorted(reg))
ok("und beide behalten ihre eigene Adresse",
   m.instance_auto_hosts("cls-nurmeier", reg["cls-nurmeier"], ext_host=HOST)[0]
   == "nurmeier.cls." + HOST
   and m.instance_auto_hosts("meier-nurmeier", reg["meier-nurmeier"],
                             ext_host=HOST)[0] == "nurmeier.meier." + HOST)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
