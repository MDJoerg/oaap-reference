#!/usr/bin/env python3
"""Die Instanz sagt der App ihre eigenen Namen (RFC-0043).

Der Befund dahinter (2026-09-24, `oaapx01`): Wegweiser sollte einen
QR-Code unter `go.joomp.de` drucken, sah aber nur den Host der Anfrage
vor sich -- die Plattform gab einer App Hauptname und Aliasse nirgends
mit. Die App bekam eine zweite Liste, und die stimmte binnen einer
Stunde nicht mehr mit dem Register ueberein.

Geprueft wird:
  1. die Rechnung: Hauptname zuerst, Aliasse, Knoten-Adresse zuletzt,
     Schema wie das Gateway es bedient, nichts = Variable fehlt;
  2. der Abgleich: instance.env folgt dem Register immer, der Container
     nur, wenn die Tuer es sagt -- und die Wahrheit ist der CONTAINER;
  3. BEIDE Tueren, durch die ein Name sich aendert (CLI und Portal-
     Spool), erzeugen den Container neu (zwei Wege, eine Regel);
  4. `address show` sagt, was die App sieht, und nennt es STALE, wenn
     der Container etwas anderes traegt.

Braucht kein Docker: `container_env` und `recreate_instance_containers`
sind hier durch ein Gedaechtnis ersetzt, das sich wie Docker verhaelt.

Aufruf: python3 test/test_instance_names_env.py
"""
import argparse
import contextlib
import io as _io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-names-env-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


# --- Docker-Ersatz: was ein Container traegt, und wer neu erzeugt wurde
CONTAINERS = {}       # container -> env dict (None = kein Container)
RECREATED = []


def fake_container_env(container):
    return CONTAINERS.get(container)


def fake_recreate(name, services, storage, endpoints=None, inst=None):
    RECREATED.append(name)
    # wie `docker run --env-file`: der neue Container traegt die Datei
    CONTAINERS[services[0]["container"]] = dict(m.load_env(name, inst))


m.container_env = fake_container_env
m.recreate_instance_containers = fake_recreate


def quiet(fn, *a, **kw):
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


def record(name, port, **over):
    base = {
        "app_id": "crm", "app_name": "CRM", "version": "1.4.0",
        "channel": "production", "port": port, "container": f"oaap-app-{name}",
        "image": "oaap-app/crm:1.4.0", "svc_port": 8000,
        "services": [{"service": "", "container": f"oaap-app-{name}",
                      "image": "oaap-app/crm:1.4.0", "build": "", "port": 8000}],
        "routes": [{"path": "/", "roles": ["user"]}],
        "storage": [], "config": [], "roles": ["user"], "visibility": {},
        "name": name,
    }
    base.update(over)
    return base


def set_external(host, edge=""):
    conf = {"host": host}
    if edge:
        conf["edge"] = edge
    with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(conf, f)


def env_of(name):
    return m.load_env(name, m.load_registry()["instances"][name]).get(m.INSTANCE_NAMES_ENV, "")


V = m.INSTANCE_NAMES_ENV

print("")
print("1. Die Rechnung")

tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["crm"] = record("crm", 8101, tenant=tid, id="aaaaaaaaaaaa",
                                 address="crm.example.org",
                                 aliases=["kunden.example.org"])
reg["instances"]["lager"] = record("lager", 8102, tenant=tid, id="bbbbbbbbbbbb",
                                   app_id="lager", app_name="Lager")
m.save_registry(reg)
set_external("oaap.example.org")
reg = m.load_registry()
crm, lager = reg["instances"]["crm"], reg["instances"]["lager"]

ok("Hauptname zuerst, dann Alias, dann die Knoten-Adresse, alle mit https",
   m.instance_names_env("crm", crm)
   == "https://crm.example.org,https://kunden.example.org,https://crm.oaap.example.org",
   m.instance_names_env("crm", crm))
ok("ohne eigenen Namen: nur die Knoten-Adresse",
   m.instance_names_env("lager", lager) == "https://lager.oaap.example.org")
ok("hinter einer Edge bedient das Gateway http -- die App erfaehrt es",
   m.instance_names_env("crm", crm, ("oaap.example.org", "10.0.0.2"))
   == "http://crm.example.org,http://kunden.example.org,http://crm.oaap.example.org")
ok("Knoten ohne externen Namen: nur die eigenen Namen",
   m.instance_names_env("crm", crm, ("", "")) == "https://crm.example.org,https://kunden.example.org")
ok("gar kein Name: leer, die Variable bleibt dann weg",
   m.instance_names_env("lager", lager, ("", "")) == "")
ok("die Variable ist plattformeigen -- nie in der Konfiguration eines Betreibers",
   V in m.RESERVED_ENV and not [e for e in m.config_entries("crm", crm) if e["key"] == V])

print("")
print("2. Der Abgleich: Datei immer, Container nur auf Ansage")

CONTAINERS["oaap-app-crm"] = {"OAAP_APP_SECRET": "x"}          # laeuft, kennt die Variable nicht
CONTAINERS["oaap-app-lager"] = None                             # kein Container
del RECREATED[:]
recreated, stale = m.sync_instance_names(reg, recreate=False)
ok("ohne Ansage: instance.env beider Instanzen ist frisch",
   env_of("crm") == "https://crm.example.org,https://kunden.example.org,https://crm.oaap.example.org"
   and env_of("lager") == "https://lager.oaap.example.org", (env_of("crm"), env_of("lager")))
ok("ohne Ansage: kein Container neu erzeugt", RECREATED == [] and recreated == [])
ok("der laufende Container wird als veraltet GENANNT, der fehlende nicht",
   stale == ["crm"], stale)

recreated, stale = m.sync_instance_names(reg, only="crm")
ok("mit Ansage: genau dieser Container neu erzeugt, und er traegt die Namen",
   recreated == ["crm"] and RECREATED == ["crm"]
   and CONTAINERS["oaap-app-crm"].get(V) == env_of("crm"))
del RECREATED[:]
m.sync_instance_names(reg, only="crm")
ok("gleiche Namen: kein zweites Mal", RECREATED == [])

out = quiet(m.refresh_generated_sites)
ok("refresh_generated_sites (Plattform-Update) erzeugt keinen Container neu",
   RECREATED == [])

print("")
print("3. Beide Tueren")

def address(action, hostname=""):
    ns = argparse.Namespace(name="crm", hostname=hostname, action=action)
    return quiet(m.cmd_address, ns)

del RECREATED[:]
out = address(action="alias-add", hostname="neu.example.org")
ok("CLI alias-add: Register, Datei und Container tragen den neuen Namen",
   "neu.example.org" in (m.load_registry()["instances"]["crm"].get("aliases") or [])
   and env_of("crm").split(",")[2] == "https://neu.example.org"
   and CONTAINERS["oaap-app-crm"][V] == env_of("crm") and RECREATED == ["crm"], (out, env_of("crm")))
ok("CLI sagt es", "recreated" in out and V in out, out)

del RECREATED[:]
address(action="alias-remove", hostname="kunden.example.org")
ok("CLI alias-remove: der Name ist aus Datei und Container verschwunden",
   "kunden.example.org" not in CONTAINERS["oaap-app-crm"][V] and RECREATED == ["crm"])

del RECREATED[:]
address(action="set", hostname="crm.example.net")
ok("CLI set: der neue Hauptname steht vorn",
   CONTAINERS["oaap-app-crm"][V].startswith("https://crm.example.net,") and RECREATED == ["crm"],
   CONTAINERS["oaap-app-crm"][V])

del RECREATED[:]
address(action="alias-remove", hostname="neu.example.org")
address(action="remove")
ok("CLI remove: nur die Knoten-Adresse bleibt",
   CONTAINERS["oaap-app-crm"][V] == "https://crm.oaap.example.org" and RECREATED == ["crm", "crm"],
   (CONTAINERS["oaap-app-crm"][V], RECREATED))

# Die zweite Tuer: das Portal legt eine Anfrage in den Spool, der Worker
# arbeitet sie ab -- dieselbe Regel muss auch dort greifen.
del RECREATED[:]
queue = os.path.join(m.SPOOL_DIR, "queue")
os.makedirs(queue, exist_ok=True)
os.makedirs(os.path.join(m.SPOOL_DIR, "results"), exist_ok=True)
with open(os.path.join(queue, "0001-crm.json"), "w", encoding="utf-8") as f:
    json.dump({"instance": "crm", "id": "r-1", "action": "address", "op": "set",
               "hostname": "crm.example.org"}, f)
with open(os.path.join(queue, "0002-crm.json"), "w", encoding="utf-8") as f:
    json.dump({"instance": "crm", "id": "r-2", "action": "address", "op": "alias-add",
               "hostname": "kunden.example.org"}, f)
out = quiet(m.cmd_process_deploys, None)
crm = m.load_registry()["instances"]["crm"]
ok("Portal-Spool set + alias-add: das Register hat beide Namen",
   crm.get("address") == "crm.example.org" and crm.get("aliases") == ["kunden.example.org"], (crm.get("address"), crm.get("aliases"), out))
ok("Portal-Spool: der Container wurde je Aenderung neu erzeugt und traegt beide",
   RECREATED == ["crm", "crm"]
   and CONTAINERS["oaap-app-crm"][V] == "https://crm.example.org,https://kunden.example.org,https://crm.oaap.example.org",
   (RECREATED, CONTAINERS["oaap-app-crm"].get(V)))

print("")
print("4. Was die App sieht")

out = address(action="show")
ok("address show nennt die Variable und ihren Wert",
   f"As the app sees it ({V}): https://crm.example.org,https://kunden.example.org,https://crm.oaap.example.org" in out, out)
CONTAINERS["oaap-app-crm"][V] = "https://alt.example.org"
out = address(action="show")
ok("traegt der Container etwas anderes, heisst es STALE und nennt den Weg",
   "STALE" in out and "oaap app restart crm" in out, out)
CONTAINERS["oaap-app-crm"] = None
out = address(action="show")
ok("ohne Container sagt es das", "no container" in out, out)

print("")
print("5. Der Knoten wechselt seinen Namen")

CONTAINERS["oaap-app-crm"] = {V: env_of("crm")}
CONTAINERS["oaap-app-lager"] = {V: env_of("lager")}
del RECREATED[:]
out = quiet(m.cmd_external, argparse.Namespace(action="set", hostname="node.example.net", behind_edge=""))
ok("external set: jede Instanz bekommt die neue Knoten-Adresse, beide Container neu",
   sorted(RECREATED) == ["crm", "lager"]
   and CONTAINERS["oaap-app-lager"][V] == "https://lager.node.example.net"
   and CONTAINERS["oaap-app-crm"][V].endswith(",https://crm.node.example.net"),
   (RECREATED, CONTAINERS["oaap-app-lager"].get(V), out))

print("")
print(f"{'FEHLER: ' + str(fails) + ' Pruefungen' if fails else 'alles gruen'}")
sys.exit(1 if fails else 0)
