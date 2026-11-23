#!/usr/bin/env python3
"""An app's own doors go to the service that owns the path (runtime spec 2.12, 2.39).

Measured on oaap-test 2026-11-23 (bookstack, RFC-0110 stage 1): an app with
three services declares its setup door /oaap/setup under the route /oaap of
the service `oaap`; the internal endpoint /s/<name> went to the PRIMARY
service (BookStack) and got its 404 page. Defended here:

- the setup, links and assets doors go to the service of the LONGEST route
  that covers the path -- the service the gateway sends a person to;
- a path only `/` covers goes to that route's service;
- a single-service instance and a path no route covers keep the primary;
- the health doors /h and /r stay at the primary (2.12: the primary carries
  the app-level health check).

Run: python3 test/test_door_target.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-door-target-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


WIKI = {
    "container": "oaap-app-w-wiki", "svc_port": 80, "health_path": "/status",
    "services": [{"service": "wiki", "container": "oaap-app-w-wiki", "port": 80},
                 {"service": "db", "container": "oaap-app-w-db", "port": 3306},
                 {"service": "oaap", "container": "oaap-app-w-oaap", "port": 8000}],
    "routes": [{"path": "/", "roles": ["user"], "service": "wiki"},
               {"path": "/oaap", "roles": ["admin"], "service": "oaap"}],
    "setup": {"path": "/oaap/setup"},
    "assets": {"path": "/oaap/assets"},
    "link_state": {"path": "/x/links"},
}

print("=== door_target ===")
ok("/oaap/setup -> Dienst oaap (Route /oaap)",
   m.door_target(WIKI, "/oaap/setup") == ("oaap-app-w-oaap", 8000))
ok("die Abfrage zählt nicht mit", m.door_target(WIKI, "/oaap/setup?kind=role") == ("oaap-app-w-oaap", 8000))
ok("/oaap selbst -> oaap", m.door_target(WIKI, "/oaap") == ("oaap-app-w-oaap", 8000))
ok("/oaapx ist NICHT unter /oaap -> wiki (Route /)",
   m.door_target(WIKI, "/oaapx/setup") == ("oaap-app-w-wiki", 80))
ok("nur / deckt den Pfad -> dessen Dienst", m.door_target(WIKI, "/x/links") == ("oaap-app-w-wiki", 80))
one = {"container": "oaap-app-h", "svc_port": 8000, "routes": [{"path": "/", "roles": ["user"]}]}
ok("ein Dienst: immer der primäre", m.door_target(one, "/oaap/setup") == ("oaap-app-h", 8000))
noroute = dict(WIKI, routes=[{"path": "/app", "roles": ["user"], "service": "oaap"}])
ok("kein Route deckt den Pfad -> der primäre", m.door_target(noroute, "/oaap/setup") == ("oaap-app-w-wiki", 80))
unknown = dict(WIKI, routes=[{"path": "/oaap", "roles": ["admin"], "service": "weg"}])
ok("Route nennt einen Dienst, den es nicht gibt -> der primäre",
   m.door_target(unknown, "/oaap/setup") == ("oaap-app-w-wiki", 80))

print("=== interner Endpunkt ===")
m.docker_subnets = lambda net=None: ["172.18.0.0/16"]
reg = m.load_registry()
reg["instances"]["w"] = WIKI
reg["instances"]["help"] = {"container": "oaap-app-help", "svc_port": 8000, "health_path": "/healthz",
                            "setup": {"path": "/oaap/setup"}}
m.save_registry(reg)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
m.write_internal_health_caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "_internal-health.caddy"), encoding="utf-8") as f:
    caddy = f.read()
ok("/s/w -> oaap-app-w-oaap:8000",
   "handle /s/w {\n\t\t\trewrite * /oaap/setup?{query}\n\t\t\treverse_proxy oaap-app-w-oaap:8000" in caddy, caddy)
ok("/a/w -> oaap-app-w-oaap:8000",
   "handle /a/w {\n\t\t\trewrite * /oaap/assets\n\t\t\treverse_proxy oaap-app-w-oaap:8000" in caddy, caddy)
ok("/h/w bleibt am primären Dienst",
   "handle /h/w {\n\t\t\trewrite * /status\n\t\t\treverse_proxy oaap-app-w-wiki:80" in caddy, caddy)
ok("eine App mit einem Dienst wie bisher",
   "handle /s/help {\n\t\t\trewrite * /oaap/setup?{query}\n\t\t\treverse_proxy oaap-app-help:8000" in caddy, caddy)

print("")
print("OK" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
