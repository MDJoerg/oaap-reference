#!/usr/bin/env python3
"""Die erste Installation meldet Rollen jenseits von `user` (2026-10-18).

Die Huellenpruefung vergleicht mit dem, was eine Instanz schon hatte. Eine
NEUE Instanz hatte nichts, also ging `partner` auf `/` bei der ersten
Installation unbemerkt durch -- dort, wo der Kreis der Menschen am groessten
gewaehlt wird. Joerg (18.10.): melden, nicht anhalten.

Der Test geht durch `_install_from_dir`, den echten Weg (Docker durch
Attrappen ersetzt), denn eine Regel in einem Zweig kann man nur lesen, eine im
laufenden Weg ausfuehren:

    - eine neue Instanz mit nur `user`: keine Meldung, kein Eintrag;
    - eine neue Instanz mit `partner` auf `/`: die Meldung nennt Pfad und
      Rolle, der Eintrag im Protokoll des Mandanten auch, und die Instanz
      ENTSTEHT (nichts wird angehalten);
    - `admin`, `keyuser`, `support`, `guest` zaehlen, `public` nicht;
    - ein erneutes Ausrollen derselben Instanz meldet NICHT noch einmal
      (das ist Sache der Huellenpruefung);
    - die Meldung steht auch, wenn der Mandant nicht der eigene Knoten ist.

Aufruf: python3 test/test_first_install_roles.py
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-first-roles-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.image_uid = lambda img: None
m.recreate_instance_containers = lambda name, *a, **kw: None
m.container_env = lambda container: None
AUDIT = []
m.audit_tenant = lambda action, tenant, subject="", **kw: AUDIT.append(
    (action, tenant, subject, kw.get("detail", "")))
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()


def manifest(app_id, routes):
    lines = ['oaap_manifest: "0.1"', "app:", f"  id: {app_id}", "  name: X",
             "  version: 0.1.0", "  type: native", "services:", "  web:",
             "    build: .", "    port: 80", "routes:"]
    for path, roles in routes:
        lines += [f"  - path: {path}", f"    roles: [{', '.join(roles)}]"]
    lines += ["health:", "  path: /healthz", ""]
    return "\n".join(lines)


def install(app_id, routes, channel="test", name=None):
    pkg = tempfile.mkdtemp(prefix="oaap-first-roles-pkg-")
    with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
        f.write(manifest(app_id, routes))
    args = argparse.Namespace(package=pkg, path="", ref="", name=name or app_id,
                              channel=channel, store_source="", tenant="",
                              key="", ident=None, rehearsal=None, bind=[])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
    except SystemExit:
        return "ABGELEHNT: " + buf.getvalue()
    return buf.getvalue()


def entries():
    return [e for e in AUDIT if e[0] == "instance.roles-beyond-user"]


print("Das Hilfsmittel")
ok("public_route_paths: nur Routen mit `public`, sortiert",
   m.public_route_paths([{"path": "/z", "roles": ["public"]},
                         {"path": "/a", "roles": ["user", "public"]},
                         {"path": "/u", "roles": ["user"]}]) == ["/a", "/z"])
ok("public_route_paths ohne Routen: nichts", m.public_route_paths(None) == [])
ok("nur `user`: nichts", m.roles_beyond_user([{"path": "/", "roles": ["user"]}]) == [])
ok("`public` zaehlt nicht (eigene Regel)",
   m.roles_beyond_user([{"path": "/", "roles": ["public"]}]) == [])
ok("`partner` neben `user`",
   m.roles_beyond_user([{"path": "/", "roles": ["user", "partner"]}]) == ["/ (partner)"])
ok("mehrere Pfade und Rollen, sortiert",
   m.roles_beyond_user([{"path": "/b", "roles": ["keyuser", "admin"]},
                        {"path": "/a", "roles": ["guest"]}])
   == ["/a (guest)", "/b (admin, keyuser)"])
ok("ohne Routen: nichts", m.roles_beyond_user(None) == []
   and m.roles_beyond_user([]) == [])

print("Der Weg: eine neue Instanz")
said = install("nuruser", [("/", ["user"])])
ok("nur `user`: die Instanz entsteht, keine Meldung, kein Eintrag",
   "nuruser" in m.load_registry()["instances"] and "NOTE" not in said
   and entries() == [] and AUDIT == [], (said, AUDIT))
said = install("nurpublic", [("/", ["user"]), ("/hook", ["public"])])
ok("nur eine oeffentliche Route: gemeldet, KEINE Meldung zu Rollen jenseits von user",
   "WITHOUT login: /hook" in said and "roles beyond" not in said
   and entries() == [], (said, AUDIT))
ok("auch dann sagt die Meldung, dass nichts angehalten wurde und die Instanz entstand",
   "Nothing was held back" in said and "nurpublic" in m.load_registry()["instances"],
   said)
ok("und der Protokolleintrag der oeffentlichen Route steht",
   [e[2:] for e in AUDIT if e[0] == "instance.public-routes"] == [("nurpublic", "/hook")],
   AUDIT)

said = install("clubapp", [("/", ["user", "partner"]), ("/health", ["public"])])
ok("`partner` auf `/`: die Meldung nennt Pfad und Rolle",
   "NOTE: first installation" in said and "/ (partner)" in said, said)
ok("sie sagt, dass nichts angehalten wurde", "Nothing was held back" in said, said)
ok("KOEDER: die Instanz ENTSTEHT trotzdem (melden, nicht anhalten)",
   "clubapp" in m.load_registry()["instances"] and "ABGELEHNT" not in said, said)
ok("die oeffentliche Route /health wird eigens gemeldet, mit eigener Handlung im Protokoll",
   "routes reachable WITHOUT login: /health" in said
   and [e for e in AUDIT if e[0] == "instance.public-routes"][-1][3] == "/health", said)
ok("und die Rolle `public` steht NICHT in der Liste der Rollen jenseits von user",
   "/ (partner)" in said and "(partner, public)" not in said and "public)" not in said, said)
ok("der Eintrag im Protokoll des Mandanten: Handlung, Mandant, Instanz, Einzelheit",
   len(entries()) == 1 and entries()[0][1] == DEFAULT
   and entries()[0][2] == "clubapp" and "/ (partner)" in entries()[0][3], AUDIT)

said = install("verw", [("/", ["user"]), ("/admin", ["admin", "keyuser"])])
ok("`admin` und `keyuser` zaehlen",
   "/admin (admin, keyuser)" in said and len(entries()) == 2, (said, AUDIT))

print("Der Weg: dieselbe Instanz noch einmal")
n = len(entries())
said = install("clubapp", [("/", ["user", "partner"]), ("/health", ["public"])])
ok("ein erneutes Ausrollen meldet NICHT noch einmal (das macht die Huellenpruefung)",
   "first installation" not in said and len(entries()) == n
   and len([e for e in AUDIT if e[0] == "instance.public-routes"]) == 2, (said, AUDIT))

print("\nALLE PRUEFUNGEN BESTANDEN" if not fails
      else f"\n{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
