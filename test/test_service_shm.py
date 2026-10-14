#!/usr/bin/env python3
"""Gemeinsamer Speicher je Dienst (Manifest 0.7, oaap.apps.runtime 0.2.39, 2.20).

Gemessen wird die Eigenschaft, nicht die Absicht:

  * ein Manifest mit `shm: 1g` besteht die Pruefung, eins mit `512`, `1k`,
    `32m` oder Text nicht -- und zwar bei der MANIFEST-Pruefung, nicht
    erst an `docker run`;
  * der Wert kommt im echten `docker run` an (`--shm-size 1g`, VOR dem
    Image), und zwar durch jede Tuer, die Container neu baut -- auch die,
    die dem Aufrufer den Instanz-Eintrag NICHT mitgibt;
  * ein Dienst ohne `shm` bekommt kein Flag (keine Vorgabe);
  * ein handbearbeiteter Eintrag wird kein kaputter Befehl;
  * die Betreibergrenzen (2.18) bleiben unberuehrt.

Braucht nur Python 3; Docker wird durch einen Rekorder ersetzt.
"""
import contextlib
import importlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, PLATFORM)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


DATA = tempfile.mkdtemp(prefix="oaap-shm-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.modules.pop("appctl", None)
a = importlib.import_module("appctl")

# ------------------------------------------------------------ Testboden
RUNS = []


class Done:
    stderr = ""
    stdout = ""


def fake_run(cmd, **kw):
    RUNS.append(list(cmd))
    return Done()


a.run = fake_run
a.subprocess.run = lambda *x, **k: Done()
a.ensure_app_network = lambda name: "net-" + name
a.connect_gateway = lambda net: None
a.image_uid = lambda image: None
a.restore_links = lambda name: None
a.state_view_write = lambda *x, **k: None


def last_run():
    docker_run = [r for r in RUNS if r[:2] == ["docker", "run"]]
    return docker_run[-1] if docker_run else []


def flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def validate(doc):
    """(accepted, output) -- validate_manifest reports by dying."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            a.validate_manifest(doc)
        return True, buf.getvalue()
    except SystemExit:
        return False, buf.getvalue()


def manifest(**svc):
    m = {"oaap_manifest": "0.7",
         "app": {"id": "webtop", "name": "Desktop", "version": "0.1.0",
                 "type": "wrapped"},
         "services": {"desktop": {"build": ".", "port": 3000, **svc}},
         "routes": [{"path": "/", "roles": ["user"]}],
         "health": {"path": "/"}}
    return m


# ------------------------------------------------------------ Werte
print("")
print("Werte -- was das Manifest annimmt und was es ablehnt")

ok("Manifest 0.7 ist dem Knoten bekannt (kein 'neuer als die Plattform')",
   a.MANIFEST_MINOR >= 7, a.MANIFEST_MINOR)
acc, out = validate(manifest(shm="1g"))
ok("shm: 1g wird angenommen", acc, out)
ok("shm: 512m wird angenommen", validate(manifest(shm="512m"))[0])
ok("Gross-/Kleinschreibung ist egal, gespeichert wird klein",
   a.parse_shm("1G") == "1g")
ok("ohne shm ist das Manifest wie bisher gueltig", validate(manifest())[0])
for bad in ("512", "1k", "32m", "viel", ""):
    acc, out = validate(manifest(shm=bad))
    ok(f"shm: {bad!r} wird bei der Manifest-Pruefung abgelehnt",
       not acc and "services.desktop.shm" in out, out)

# ------------------------------------------------------------ docker run
print("")
print("Der Befehl -- ohne shm wie bisher, mit shm mit Flag, durch jede Tuer")

KEY = "desk"
tid = a.ensure_default_tenant()
reg = a.load_registry()
reg["instances"][KEY] = {
    "app_id": "webtop", "app_name": "Desktop", "channel": "test",
    "tenant": tid, "id": "bbbbbbbbbbbb", "version": "0.1.0", "name": KEY,
    "port": 8131, "svc_port": 3000, "container": "oaap-app-" + KEY,
    "image": "oaap-app/webtop:0.1.0",
    "services": [{"service": "", "container": "oaap-app-" + KEY,
                  "image": "oaap-app/webtop:0.1.0", "port": 3000}],
    "routes": [{"path": "/", "roles": ["user"]}], "storage": [],
}
a.save_registry(reg)

svcs = a.instance_services(a.load_registry()["instances"][KEY])
a.recreate_instance_containers(KEY, svcs, [])
plain = last_run()
ok("ohne shm kein --shm-size", plain and "--shm-size" not in plain, plain)

svcs[0]["shm"] = "1g"
a.recreate_instance_containers(KEY, svcs, [])
with_shm = last_run()
ok("mit shm: --shm-size 1g", flag(with_shm, "--shm-size") == "1g", with_shm)
ok("das Flag steht VOR dem Image (danach waere es ein Argument der App)",
   with_shm.index("--shm-size") < with_shm.index("oaap-app/webtop:0.1.0"))
a.recreate_instance_containers(KEY, svcs, [], inst={"id": "bbbbbbbbbbbb"})
ok("Installation (nur die Kennung als inst) traegt es ebenso",
   flag(last_run(), "--shm-size") == "1g", last_run())
ok("Log-Grenze und Neustart-Regel sind unberuehrt",
   "--restart" in with_shm and "max-size=10m" in with_shm)

reg = a.load_registry()
reg["instances"][KEY]["resources"] = {"memory": "3g"}
a.save_registry(reg)
a.recreate_instance_containers(KEY, svcs, [])
both = last_run()
ok("Betreibergrenze und shm stehen nebeneinander",
   flag(both, "--memory") == "3g" and flag(both, "--shm-size") == "1g", both)

svcs[0]["shm"] = "viel"
a.recreate_instance_containers(KEY, svcs, [])
bad = last_run()
ok("ein handbearbeiteter Eintrag wird KEIN kaputter Befehl: Container ohne Flag",
   bad and "--shm-size" not in bad and flag(bad, "--memory") == "3g", bad)

# Mehrere Dienste: nur der erklaert bekommt es
multi = [{"service": "ui", "container": "oaap-app-m-ui",
          "image": "x/ui:1", "port": 80, "shm": "256m"},
         {"service": "db", "container": "oaap-app-m-db",
          "image": "x/db:1", "port": 5432}]
reg = a.load_registry()
reg["instances"]["m"] = {"app_id": "m", "app_name": "m", "channel": "test",
                         "tenant": tid, "id": "cccccccccccc", "version": "1",
                         "name": "m", "port": 8132, "services": multi,
                         "routes": [{"path": "/", "roles": ["user"],
                                     "service": "ui"}], "storage": []}
a.save_registry(reg)
del RUNS[:]
a.recreate_instance_containers("m", multi, [])
runs = [r for r in RUNS if r[:2] == ["docker", "run"]]
ui = [r for r in runs if "oaap-app-m-ui" in r][0]
db = [r for r in runs if "oaap-app-m-db" in r][0]
ok("Mehr-Container-App: nur der Dienst mit shm bekommt das Flag",
   flag(ui, "--shm-size") == "256m" and "--shm-size" not in db, (ui, db))

# ------------------------------------------------------------ Register beim Installieren
print("")
print("Das Register -- was das Manifest erklaert, steht im Dienst-Eintrag")

SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("der Installer schreibt shm aus dem Manifest in den Dienst-Eintrag",
   'rec["shm"] = parse_shm(svc["shm"])' in SRC)
ok("die eine Funktion, die Container baut, fragt den Dienst-Eintrag",
   "shm = shm_args(s)" in SRC and "*limits, *shm," in SRC)
ok("shm_args ohne Eintrag: nichts; mit Eintrag: das Flag",
   a.shm_args({"port": 1}) == [] and a.shm_args({"shm": "2g"}) == ["--shm-size", "2g"]
   and a.shm_args(None) == [])

print("")
print("FAIL" if fails else "OK", f"-- {fails} Fehler" if fails else "")
sys.exit(1 if fails else 0)
