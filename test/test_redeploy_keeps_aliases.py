#!/usr/bin/env python3
"""Ein Redeploy behaelt die Aliasse einer Instanz (RFC-0018).

Der Befund (2026-09-25, Flottenlauf 0.1.126 auf `oaapx01`): nach dem
Redeploy von Wegweiser trug die Instanz nur noch ihren Hauptnamen --
der Alias `go.objid.info` war weg, das Gateway hatte keine Site mehr
dafuer, von aussen brach der TLS-Handschlag ab. Der Block "survives
redeploy" in `_install_from_dir` rettete die Adresse, die Sichtbarkeit,
die Bremse, die Kachel und die Endpunkte -- die Aliasse nicht. Seit
RFC-0018 (0.1.36) hat jeder Redeploy sie still verworfen; aufgefallen
ist es erst, als eine App ihre Namen zurueckgelesen hat.

Der Test laeuft durch den ganzen Install, mit Docker durch Attrappen
ersetzt: erst anlegen, dann Namen setzen, dann noch einmal ausrollen --
und danach muessen Hauptname UND Aliasse noch da sein.

Aufruf: python3 test/test_redeploy_keeps_aliases.py
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-redeploy-alias-test-")
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


# --- Docker-Attrappen: jeder Aufruf gelingt, nichts laeuft
DOCKER = []


def fake_run(cmd, *a, **kw):
    DOCKER.append(list(cmd))
    return types.SimpleNamespace(stdout="", stderr="", returncode=0)


RECREATED = []
m.run = fake_run
m.reload_gateway = lambda: None
m.image_uid = lambda img: None
m.recreate_instance_containers = lambda name, *a, **kw: RECREATED.append(name)
m.container_env = lambda container: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()

MANIFEST = "\n".join([
    'oaap_manifest: "0.1"',
    "app:",
    "  id: demo",
    "  name: Demo",
    "  version: 0.1.0",
    "  type: native",
    "services:",
    "  web:",
    "    build: .",
    "    port: 80",
    "routes:",
    "  - path: /",
    "    roles: [user]",
    "health:",
    "  path: /healthz",
    "",
])
pkg = tempfile.mkdtemp(prefix="oaap-alias-pkg-")
with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
    f.write(MANIFEST)


def install(channel):
    args = argparse.Namespace(package=pkg, path="", ref="", name="demo",
                              channel=channel, store_source="", tenant="",
                              key="", ident=None, rehearsal=None, bind=[])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
    except SystemExit:
        return "ABGELEHNT: " + buf.getvalue()
    return buf.getvalue()


print("")
print("Anlegen, benennen, noch einmal ausrollen")

said = install("test")
reg = m.load_registry()
ok("die Instanz ist angelegt (Docker durch Attrappen ersetzt)",
   "demo" in reg["instances"] and RECREATED == ["demo"], said)

inst = reg["instances"]["demo"]
inst["address"] = "go.example.org"
inst["aliases"] = ["go.example.net", "go.example.com"]
m.save_registry(reg)
ok("Hauptname und zwei Aliasse stehen im Register",
   m.instance_names(m.load_registry()["instances"]["demo"])
   == ["go.example.org", "go.example.net", "go.example.com"])

said = install("test")
inst = m.load_registry()["instances"]["demo"]
ok("nach dem Redeploy: der Hauptname ist noch da", inst.get("address") == "go.example.org", inst)
ok("nach dem Redeploy: BEIDE Aliasse sind noch da -- das war der Befund",
   inst.get("aliases") == ["go.example.net", "go.example.com"], (inst.get("aliases"), said))
ok("und die App bekommt alle drei Namen gesagt (RFC-0043)",
   m.load_env("demo", inst).get(m.INSTANCE_NAMES_ENV)
   == "https://go.example.org,https://go.example.net,https://go.example.com",
   m.load_env("demo", inst).get(m.INSTANCE_NAMES_ENV))

print("")
print(f"{'FEHLER: ' + str(fails) + ' Pruefungen' if fails else 'alles gruen'}")
sys.exit(1 if fails else 0)
