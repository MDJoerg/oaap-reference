#!/usr/bin/env python3
"""Test-Instanzen bleiben aus Suchmaschinen heraus (I-81 Idee 1, gateway 0.2.26).

Bisher hatte jede Webseite ihren eigenen Schalter (website-starter:
WEB_NOINDEX), und beim Livegang musste ein Mensch daran denken, ihn
umzulegen. Jetzt weiss es die Plattform selbst:

    Test      Kanal test -> jede Antwort traegt X-Robots-Tag: noindex
    Probe     eine Generalprobe ebenso (Kopie der Produktivdaten)
    Prod      eine Produktivinstanz nie -- auch ohne Kanalangabe
    ueberall  im gemeinsamen Rumpf aller Eingaenge (LAN-Port, automatischer
              Name, eigene Namen), auf Site-Ebene vor allen handle-Bloecken
    defer     erst beim Schreiben der Antwort gesetzt, ersetzt also, was die
              App selbst sagt
    Wechsel   eine Instanz, die den Kanal wechselt, bekommt die Regel mit der
              naechsten Site -- aus dem Satz gelesen, kein eigener Schalter

Aufruf: python3 test/test_noindex_test_instances.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-noindex-test-")
os.environ["OAAP_DATA_DIR"] = DATA
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, PLATFORM)

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
m.refresh_destinations = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


ROUTES = [{"path": "/", "roles": ["public"]}]


def record(name, port, **over):
    base = {
        "app_id": "website", "app_name": "Webseite", "version": "0.1.0",
        "channel": "production", "port": port, "container": f"oaap-app-{name}",
        "image": "oaap-app/website:0.1.0", "svc_port": 8080,
        "services": [{"service": "", "container": f"oaap-app-{name}",
                      "image": "oaap-app/website:0.1.0", "build": "", "port": 8080}],
        "routes": [dict(r) for r in ROUTES],
        "storage": [], "config": [], "roles": [], "visibility": {},
        "id": (name * 12)[:12], "name": name,
    }
    base.update(over)
    return base


tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["web"] = record("web", 8201, tenant=tid)
reg["instances"]["web-test"] = record("web-test", 8202, tenant=tid, channel="test")
reg["instances"]["old"] = record("old", 8203, tenant=tid)
del reg["instances"]["old"]["channel"]
reg["instances"]["web-probe"] = record(
    "web-probe", 8204, tenant=tid,
    rehearsal={"of": "web", "code_from": "web", "archive": "x",
               "archive_created": "2026-11-01T00:00:00Z",
               "created": "2026-11-18T00:00:00Z",
               "expires": "2099-01-01T00:00:00Z", "extensions": 0})
m.save_registry(reg)

HDR = 'X-Robots-Tag "noindex, nofollow"'


def site(name):
    m.write_app_caddy(name, m.load_registry()["instances"][name])
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


print("Wer es bekommt")
ok("Test-Instanz: X-Robots-Tag noindex", HDR in site("web-test"), site("web-test"))
ok("Generalprobe: ebenso", HDR in site("web-probe"))
ok("Produktivinstanz: nie", "X-Robots-Tag" not in site("web"), site("web"))
ok("ohne Kanalangabe gilt Produktion: nie", "X-Robots-Tag" not in site("old"))
ok("eine Regel, ein Leser", m.noindex_of(m.load_registry()["instances"]["web-test"])
   and not m.noindex_of(m.load_registry()["instances"]["web"])
   and m.noindex_of({"channel": "production", "rehearsal": {"of": "x"}})
   and not m.noindex_of(None))

print("\nWie es steht")
t = site("web-test")
lines = t.splitlines()
i = next(n for n, ln in enumerate(lines) if "X-Robots-Tag" in ln)
depth = 0
for ln in lines[:i - 1]:
    depth += ln.count("{") - ln.count("}")
# depth 1 = directly inside the site block; Caddy sorts site-level
# directives itself (header before handle), so the place in the file
# does not matter -- being outside every handle block does.
ok("auf Site-Ebene, in keinem handle-Block", depth == 1, (depth, t))
ok("im header-Block mit defer (ersetzt, was die App sagt)",
   lines[i - 1] == "\theader {" and lines[i + 1] == "\t\tdefer" and lines[i + 2] == "\t}",
   lines[i - 1:i + 3])
ok("genau einmal je Site", t.count("X-Robots-Tag") == 1)

print("\nJeder Eingang (der gemeinsame Rumpf)")
body = "\n".join(m.site_body(ROUTES, "c", 8080, None, "web-test", None,
                             edge="10.0.0.1"))
ok("auch hinter einer Kante (externe Namen)", HDR in body)
body = "\n".join(m.site_body(ROUTES, "c", 8080, None, "web", None))
ok("Produktiv auch dort nicht", "X-Robots-Tag" not in body)
body = "\n".join(m.site_body(ROUTES, "c", 8080, None, "", None))
ok("ohne Instanz (kein Scope): nichts", "X-Robots-Tag" not in body)
SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("alle Site-Schreiber gehen durch site_body (keiner baut den Rumpf selbst)",
   SRC.count("+= site_body(") + SRC.count("+ site_body(") >= 3)

print("\nWechsel des Kanals")
reg = m.load_registry()
reg["instances"]["web-test"]["channel"] = "production"
m.save_registry(reg)
ok("nach dem Wechsel zu Produktion: kein noindex mehr", "X-Robots-Tag" not in site("web-test"))
reg = m.load_registry()
reg["instances"]["web"]["channel"] = "test"
m.save_registry(reg)
ok("zurueck in den Test-Kanal: wieder noindex", HDR in site("web"))

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
