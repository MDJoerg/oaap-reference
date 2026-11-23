#!/usr/bin/env python3
"""A declared health path the app never built (0.1.294).

Found 2026-11-15 on oaapx02: an AI-built web site declares a health path,
answers it with 404 and its home page with 200 -- the node read "Auffällig",
FleetView with it. The node cannot make others meet its rules, so:

  * the gateway's internal probe endpoint gets /r/<name> (the home page)
    beside /h/<name>, for an instance whose health path is not "/";
  * the portal's probe asks /r/<name> only when /h/<name> said 404/405,
    and an answer below 400 there is "Erreichbar", the missing path named;
  * a 5xx or no answer stays what it was -- that is a broken app;
  * install says so at once (NOTE), so a builder sees it before the fleet.

Run: python3 test/test_health_fallback.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-health-fallback-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as m                                            # noqa: E402
import fleet_view as fv                                       # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


print("=== interner Endpunkt ===")
m.docker_subnets = lambda net=None: ["172.18.0.0/16"]
reg = m.load_registry()
reg["instances"]["sgl-web"] = {"container": "oaap-sgl-web", "svc_port": 3000,
                               "health_path": "/healthz"}
reg["instances"]["plain"] = {"container": "oaap-plain", "svc_port": 80, "health_path": "/"}
reg["instances"]["none"] = {"container": "oaap-none", "svc_port": 80, "health_path": ""}
m.save_registry(reg)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
m.write_internal_health_caddy()
caddy = open(os.path.join(m.CADDY_APPS_DIR, "_internal-health.caddy"), encoding="utf-8").read()
ok("/r/sgl-web -> / auf den Dienst der App",
   "handle /r/sgl-web {\n\t\t\trewrite * /\n\t\t\treverse_proxy oaap-sgl-web:3000" in caddy, caddy)
ok("/h/sgl-web bleibt auf /healthz",
   "handle /h/sgl-web {\n\t\t\trewrite * /healthz" in caddy, caddy)
ok("kein /r/ wo /h/ schon die Startseite ist", "/r/plain" not in caddy and "/r/none" not in caddy)
ok("/r/ hinter remote_ip des Plattformnetzes",
   caddy.index("@platform remote_ip 172.18.0.0/16") < caddy.index("/r/sgl-web"))

print("=== die Entscheidung (fleet_view) ===")
ok("404 am Pfad = fehlt", fv.health_path_missing("warn", "HTTP 404"))
ok("405 am Pfad = fehlt", fv.health_path_missing("warn", "HTTP 405"))
ok("500 ist kein fehlender Pfad", not fv.health_path_missing("warn", "HTTP 500"))
ok("401 ist kein fehlender Pfad", not fv.health_path_missing("warn", "HTTP 401"))
ok("keine Antwort ist kein fehlender Pfad",
   not fv.health_path_missing("err", "ConnectionError"))
alt = fv.root_fallback("/healthz", "HTTP 404", ("ok", "Gesund", "HTTP 200"))
ok("Startseite 200 -> Erreichbar, der Pfad benannt",
   alt and alt[0] == "ok" and alt[1] == "Erreichbar" and "/healthz fehlt" in alt[2]
   and "Manifest" in alt[2], alt)
ok("Startseite 302 -> Erreichbar",
   fv.root_fallback("/healthz", "HTTP 404", ("warn", "Antwortet unerwartet", "HTTP 302")))
ok("Startseite 500 -> bleibt auffällig",
   fv.root_fallback("/healthz", "HTTP 404", ("warn", "x", "HTTP 500")) is None)
ok("Startseite 404 -> bleibt auffällig",
   fv.root_fallback("/healthz", "HTTP 404", ("warn", "x", "HTTP 404")) is None)
ok("Startseite stumm -> bleibt auffällig",
   fv.root_fallback("/healthz", "HTTP 404", ("err", "x", "ConnectionError")) is None)

print("=== Portal: die Prüfung fragt /r/ nur nach 404/405 ===")
psrc = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
            encoding="utf-8").read()
body = psrc.split("def _instance_probe(", 1)[1].split("\ndef ", 1)[0]
ok("_instance_probe nutzt health_path_missing und root_fallback über /r/",
   "fleet_view.health_path_missing(state, detail)" in body
   and 'f"{GATEWAY_HEALTH}/r/{name}"' in body and "fleet_view.root_fallback(" in body, body)

print("=== Installation sagt es sofort ===")
n = m.health_path_note("/healthz", 404, 200)
ok("404 + Startseite 200 -> NOTE mit Rat", n and n.startswith("NOTE:") and "health: path: /" in n, n)
n = m.health_path_note("/healthz", 404, None)
ok("404 + Startseite stumm -> WARNING", n and n.startswith("WARNING:") and "not at all" in n, n)
ok("200 -> nichts", m.health_path_note("/healthz", 200, 200) is None)
ok("500 -> nichts (das sagt die Gesundheitsseite)", m.health_path_note("/healthz", 500, 200) is None)
ok("Pfad / -> nichts", m.health_path_note("/", 404, 404) is None)
calls = []


def fake_status(url):
    calls.append(url)
    return 404 if url.endswith("/healthz") else 200


m._http_status = fake_status
note = m.check_health_path({"container": "oaap-sgl-web", "svc_port": 3000, "image": "x",
                            "health_path": "/healthz"}, seconds=1)
ok("check_health_path fragt Pfad, dann Startseite",
   calls == ["http://oaap-sgl-web:3000/healthz", "http://oaap-sgl-web:3000/"]
   and note and note.startswith("NOTE:"), (calls, note))
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
# 0.1.358: not asked when the instance does not run (F2 already said why)
ok("install ruft check_health_path nach 'Entry point' und scheitert nie daran",
   "_note = None if _bad else check_health_path(reg[\"instances\"][name])" in src
   and src.index('print(f"Entry point: port') < src.index("else check_health_path("))
hs = src.split("def _http_status(", 1)[1].split("\ndef ", 1)[0]
ok("_http_status nutzt nur Optionen, die busybox-wget kennt (gefunden auf oaap-test)",
   '"--max-redirect' not in hs and '"-S"' in hs, hs)

print(f"\n{'ALLE GRUEN' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
