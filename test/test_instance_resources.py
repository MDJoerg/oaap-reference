#!/usr/bin/env python3
"""Ressourcen-Grenzen je Instanz (RFC-0046 §7, oaap.apps.runtime 0.2.32).

Was hier gemessen wird, ist die Eigenschaft, nicht die Absicht:

  * die Grenze kommt im echten `docker run` an -- und zwar auch dann,
    wenn der Container von einer Tuer neu gebaut wird, die dem Aufrufer
    den Instanz-Eintrag NICHT mitgibt (Konfiguration speichern, Neustart);
  * ohne Grenze aendert sich am Befehl nichts (keine Vorgabe);
  * eine Redeploy-Fassung des Eintrags verliert sie nicht;
  * kaputte Werte werden abgelehnt, und ein handbearbeitetes Register
    wird kein unlesbarer Docker-Befehl;
  * die Summe zaehlt je Dienst-Container und behandelt "ohne Grenze"
    nicht als null;
  * die Gesundheitsseite sagt "mehr als die Maschine hat" VOR dem OOM;
  * nur der server_admin sieht die Karte und darf sie abschicken.

Braucht Python 3 und (fuer die Portalseite) jinja2/flask nicht -- die
Portalseite wird am Quelltext und an der reinen Funktion gepruft.
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
PORTAL = os.path.join(PLATFORM, "services", "portal")
sys.path.insert(0, PLATFORM)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


DATA = tempfile.mkdtemp(prefix="oaap-resources-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.modules.pop("appctl", None)
a = importlib.import_module("appctl")

APPCTL = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
PORTAL_SRC = open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read()

# ------------------------------------------------------------ Testboden
RUNS = []


class Done:
    stderr = ""
    stdout = ""


DONE = Done()


def fake_run(cmd, **kw):
    RUNS.append(list(cmd))
    return DONE


a.run = fake_run
a.subprocess.run = lambda *x, **k: Done()
a.ensure_app_network = lambda name: "net-" + name
a.connect_gateway = lambda net: None
a.image_uid = lambda image: None
a.restore_links = lambda name: None
a.state_view_write = lambda *x, **k: None
AUDIT = []
a.audit_tenant = lambda action, tenant, **kw: AUDIT.append((action, kw))
a.deployment_in_flight = lambda name, rid="": False

KEY = "kurs-code-anna"
tid = a.ensure_default_tenant()
reg = a.load_registry()
reg["instances"][KEY] = {
    "app_id": "code-server", "app_name": "code-server", "channel": "test",
    "tenant": tid, "id": "aaaaaaaaaaaa", "version": "0.1.2", "name": KEY,
    "port": 8130, "svc_port": 8080, "container": "oaap-app-" + KEY,
    "image": "oaap-app/code-server:0.1.2",
    "services": [{"service": "", "container": "oaap-app-" + KEY,
                  "image": "oaap-app/code-server:0.1.2", "port": 8080}],
    "routes": [{"path": "/", "roles": ["user"]}], "storage": [],
}
a.save_registry(reg)


def last_run():
    docker_run = [r for r in RUNS if r[:2] == ["docker", "run"]]
    return docker_run[-1] if docker_run else []


def flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


# ------------------------------------------------------------ Werte
print("")
print("Werte -- was angenommen und was abgelehnt wird")


def refused(**kw):
    try:
        a.parse_resources(**kw)
    except ValueError:
        return True
    return False


ok("3g, 512m, 1.5 Kerne, 512 Prozesse werden angenommen",
   a.parse_resources("3g", "1.5", "512")
   == {"memory": "3g", "cpus": 1.5, "pids": 512})
ok("Gross-/Kleinschreibung ist egal, gespeichert wird klein",
   a.parse_resources(memory="3G") == {"memory": "3g"})
ok("nichts angegeben -> nichts", a.parse_resources() == {})
ok("'512' ohne Einheit wird abgelehnt (Docker liest es als BYTES)",
   refused(memory="512"))
ok("'k' und 'b' werden abgelehnt (nur m und g)",
   refused(memory="500k") and refused(memory="500b"))
ok("unter 64m wird abgelehnt", refused(memory="32m"))
ok("Text als Speicher / Kerne / Prozesse wird abgelehnt",
   refused(memory="viel") and refused(cpus="zwei") and refused(pids="x"))
ok("Kerne: 0 und 1000 werden abgelehnt",
   refused(cpus="0") and refused(cpus="1000"))
ok("Prozesse: 3 und 10 Millionen werden abgelehnt",
   refused(pids="3") and refused(pids="10000000"))
ok("nichts davon steht schon im Register (Ablehnung veraendert nichts)",
   "resources" not in a.load_registry()["instances"][KEY])

# ------------------------------------------------------------ docker run
print("")
print("Der Befehl -- ohne Grenze wie bisher, mit Grenze mit Flags")

svcs = a.instance_services(a.load_registry()["instances"][KEY])
a.recreate_instance_containers(KEY, svcs, [])
plain = last_run()
ok("ohne Grenze kein --memory, --cpus, --pids-limit",
   plain and not any(f in plain for f in ("--memory", "--cpus", "--pids-limit")),
   plain)

reg = a.load_registry()
reg["instances"][KEY]["resources"] = {"memory": "3g", "cpus": 1.5, "pids": 512}
a.save_registry(reg)

# Die Tueren, die dem Aufrufer den Eintrag NICHT mitgeben:
a.recreate_instance_containers(KEY, svcs, [])
bare = last_run()
ok("Konfiguration/Neustart (ohne inst) tragen die Grenze",
   flag(bare, "--memory") == "3g" and flag(bare, "--cpus") == "1.5"
   and flag(bare, "--pids-limit") == "512", bare)
a.recreate_instance_containers(KEY, svcs, [], inst={"id": "aaaaaaaaaaaa"})
ident = last_run()
ok("Installation (nur die Kennung als inst) traegt die Grenze",
   flag(ident, "--memory") == "3g", ident)
a.recreate_instance_containers(KEY, svcs, [], inst=a.load_registry()["instances"][KEY])
ok("Tuer mit vollem Eintrag traegt sie ebenfalls",
   flag(last_run(), "--memory") == "3g")
ok("Swap ist mit begrenzt: --memory-swap == --memory (sonst gilt das Doppelte)",
   flag(ident, "--memory-swap") == "3g", ident)
ok("die Flags stehen VOR dem Image (danach waeren sie Argumente der App)",
   ident.index("--memory-swap") < ident.index("oaap-app/code-server:0.1.2"))
ok("Log-Grenze und Neustart-Regel sind unberuehrt",
   "--restart" in ident and "max-size=10m" in ident)

reg = a.load_registry()
reg["instances"][KEY]["resources"] = {"memory": "viel", "cpus": "x"}
a.save_registry(reg)
a.recreate_instance_containers(KEY, svcs, [])
bad = last_run()
ok("ein handbearbeitetes Register wird KEIN kaputter Befehl: der Container "
   "entsteht, ohne Grenze",
   bad and "--memory" not in bad and "--cpus" not in bad, bad)

# ------------------------------------------------------------ Befehl
print("")
print("oaap app resources -- setzen, zusammenfuehren, entfernen, Audit")

reg = a.load_registry()
reg["instances"][KEY].pop("resources", None)
a.save_registry(reg)
del RUNS[:]
del AUDIT[:]
msg = a.apply_resources(KEY, {"memory": "2g"}, who="jrg", role="server_admin")
r = a.load_registry()["instances"][KEY]
ok("setzen: im Register, Container neu mit --memory 2g",
   r.get("resources") == {"memory": "2g"} and flag(last_run(), "--memory") == "2g",
   (r.get("resources"), last_run()))
ok("das Audit nennt vorher -> nachher und wer",
   AUDIT and AUDIT[-1][0] == "instance.resources"
   and "unlimited -> memory 2g" in AUDIT[-1][1]["detail"]
   and AUDIT[-1][1]["who"] == "jrg", AUDIT)


class Args:
    def __init__(self, **kw):
        self.name, self.memory, self.cpus, self.pids, self.clear = \
            KEY, "", "", "", False
        self.__dict__.update(kw)


import io  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402

with redirect_stdout(io.StringIO()):
    a.cmd_resources(Args(cpus="2"))
r = a.load_registry()["instances"][KEY]
ok("--cpus 2 verliert die Speichergrenze NICHT (Felder werden zusammengefuehrt)",
   r.get("resources") == {"memory": "2g", "cpus": 2.0}, r.get("resources"))
out = io.StringIO()
with redirect_stdout(out):
    a.cmd_resources(Args())
ok("ohne Optionen: zeigt an, veraendert nichts",
   "memory 2g" in out.getvalue()
   and a.load_registry()["instances"][KEY]["resources"] == {"memory": "2g",
                                                             "cpus": 2.0})
with redirect_stdout(io.StringIO()):
    a.cmd_resources(Args(clear=True))
r = a.load_registry()["instances"][KEY]
ok("--clear entfernt den Block ganz; der naechste Container ist unbegrenzt",
   "resources" not in r and "--memory" not in last_run(), last_run())

a.deployment_in_flight = lambda name, rid="": True
try:
    a.apply_resources(KEY, {"memory": "1g"})
    refused_in_flight = False
except a.DiagnoseRefused:
    refused_in_flight = True
ok("waehrend eines Deployments verweigert (wie der Neustart)",
   refused_in_flight and "resources" not in a.load_registry()["instances"][KEY])
a.deployment_in_flight = lambda name, rid="": False

# ------------------------------------------------------------ Summe
print("")
print("Die Summe -- je Dienst-Container, 'ohne Grenze' ist nicht null")
G = 1024 ** 3
insts = {
    "a": {"resources": {"memory": "3g"}, "services": [{"service": ""}]},
    "b": {"resources": {"memory": "512m"},
          "services": [{"service": "web"}, {"service": "db"}]},
    "c": {"services": [{"service": ""}]},
    "d": {"resources": {"cpus": 1.0}},
}
total, limited, free = a.resources_sum(insts)
ok("3g + 2 x 512m = 4 GB, zwei Instanzen zaehlen als begrenzt",
   total == 3 * G + 1 * G and limited == 2, (total, limited, free))
ok("ohne Speichergrenze -> 'frei' (auch wenn nur Kerne gesetzt sind)",
   free == 2, free)
ok("leeres Register -> null, nichts wirft",
   a.resources_sum({}) == (0, 0, 0) and a.resources_sum(None) == (0, 0, 0))

# ------------------------------------------------------------ Redeploy
print("")
print("Redeploy -- ein Deployment hebt die Grenze nicht auf")
ok("der Install-Weg uebernimmt `resources` in den neuen Eintrag",
   'reg["instances"][name]["resources"] = dict(inst["resources"])' in APPCTL)
ok("die Grenze wird aus dem REGISTER gelesen, nicht nur aus dem Argument",
   'stored = (load_registry().get("instances") or {}).get(name) or {}'
   in APPCTL)

# ------------------------------------------------------------ Portal
print("")
print("Portal -- Karte, Route, Gesundheitsseite")
ok("die Karte erscheint nur mit can_limit, und das ist server_admin",
   "{% if i.can_limit %}" in PORTAL_SRC
   and '"can_limit": "server_admin" in caller_roles()' in PORTAL_SRC)
ok("die Route verweigert alle ausser server_admin, noch vor dem Nachschauen",
   'def instance_resources(name):' in PORTAL_SRC
   and PORTAL_SRC.split("def instance_resources(name):")[1][:400]
   .count("server_admin") >= 1)
ok("der Worker prueft die Rolle noch einmal (der Spool ist Daten, kein Vertrauen)",
   'act_role != "server_admin"' in APPCTL)
ok("der Worker ordnet die Aktion dem Portal zu (Audit-Herkunft)",
   '"resources": "portal"' in APPCTL)
ok("in der Portalkarte kommt kein Wort ueber Mandanten vor",
   "andant" not in PORTAL_SRC.split("Ressourcen-Grenzen</h2>")[1][:1500])

# die Gesundheitszeile ist eine reine Funktion -- mit einem Register aus
# der Datei, das das Portal ohnehin liest
src = PORTAL_SRC
start = src.index("def _mem_bytes(text):")
end = src.index("def external_access():")
ns = {"_re": __import__("re")}
GB = 1024 ** 3
ns["_gb"] = lambda n: f"{n / GB:.1f} GB"
ns["load_instances"] = lambda: INSTANCES
exec(src[start:end], ns)  # noqa: S102 -- die eigene Datei, nur diese Funktionen

INSTANCES = {f"seat{n}": {"resources": {"memory": "3g"},
                          "services": [{"service": ""}]} for n in range(12)}
INSTANCES["x"] = {"services": [{"service": ""}]}
row = ns["_limits_row"](8 * GB)
ok("zwoelf Plaetze zu 3 GB auf 8 GB: die Zeile warnt VOR dem OOM",
   row and row["state"] == "warn" and "36.0 GB" in row["label"]
   and "12 Instanz" in row["label"], row)
ok("und sagt, dass eine Instanz ohne Grenze nicht mitzaehlt",
   "1 Instanz(en) ohne Grenze" in row["detail"], row)
INSTANCES = {f"seat{n}": {"resources": {"memory": "1g"}} for n in range(4)}
row = ns["_limits_row"](8 * GB)
ok("4 GB auf 8 GB: ok, keine Warnung", row and row["state"] == "ok", row)
INSTANCES = {"x": {"services": [{"service": ""}]}}
ok("ohne jede Grenze keine Zeile (die Seite bleibt, wie sie war)",
   ns["_limits_row"](8 * GB) is None)

print("")
print("FAIL" if fails else "OK", f"-- {fails} Fehler" if fails else "")
sys.exit(1 if fails else 0)
