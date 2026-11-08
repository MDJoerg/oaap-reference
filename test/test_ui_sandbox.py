#!/usr/bin/env python3
"""Die zweite Herkunft für Ansichten von MCP Apps (Manifest 0.13, RFC-0075 D6).

Festgehalten:
- `ui_sandbox: {path}` wird geprüft und im Register abgelegt;
- eine solche Instanz bekommt je automatischer Adresse eine zweite,
  `<name>--ui.<rest>`, und erfährt sie als `OAAP_UI_SANDBOX_ORIGINS`;
- der Block dieser Adresse reicht NUR den Sandbox-Pfad durch, ohne Anmeldung,
  ohne Cookie, ohne Authorization, ohne Identitäts-Kopfzeilen, ohne Set-Cookie
  in der Antwort, und erlaubt das Einbetten nur den eigenen Adressen;
- die Endung `--ui` ist für Instanznamen reserviert (installieren, umbenennen);
  eine Altlast mit so einem Namen verdrängt keine Sandbox, sie bekommt keine;
- eine App ohne `ui_sandbox` und eine Generalprobe bekommen keine.

Braucht kein Docker. Aufruf: python3 test/test_ui_sandbox.py
"""
import argparse
import contextlib
import io
import ipaddress
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-ui-sandbox-test-")
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


NETS = {"oaap_default": ["172.18.0.0/16"]}
m.docker_subnets = lambda net=None: [ipaddress.ip_network(s) for k, v in NETS.items()
                                     for s in v if net is None or k == net]
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.image_uid = lambda img: None
m.recreate_instance_containers = lambda name, *a, **kw: None
m.container_env = lambda container: None
m.load_external = lambda: "node.example"
m.load_external_conf = lambda: ("node.example", "")
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()
KUNDE, _ = m.tenant_create("kunde")


def manifest(app_id, extra=""):
    return "\n".join(['oaap_manifest: "0.13"', "app:", f"  id: {app_id}", f"  name: {app_id}",
                      "  version: 0.1.0", "  type: native", "services:", "  web:", "    build: .",
                      "    port: 80", "routes:", "  - path: /", "    roles: [user]", "health:",
                      "  path: /healthz", extra, ""])


def install(app_id, name, extra="", tenant="kunde"):
    pkg = tempfile.mkdtemp(prefix="oaap-ui-pkg-")
    with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
        f.write(manifest(app_id, extra))
    args = argparse.Namespace(package=pkg, path="", ref="", name=name, channel="test", store_source="",
                              tenant=tenant, key="", ident=None, rehearsal=None, bind=[])
    NETS[m.app_network(f"{tenant}-{name}")] = ["172.29.0.0/16"]
    before = set(m.load_registry()["instances"])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
    except SystemExit:
        return None, buf.getvalue()
    new = set(m.load_registry()["instances"]) - before
    return (new.pop() if new else None), buf.getvalue()


def errors(extra):
    """Die Fehlerzeilen der Prüfung -- sie bricht mit die() ab, sonst gibt es keine."""
    import yaml
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.validate_manifest(yaml.safe_load(manifest("x-app", extra)))
    except SystemExit:
        pass
    return [l.strip(" -") for l in buf.getvalue().splitlines() if l.strip()]


print("=== Manifest 0.13 ===")
ok("ui_sandbox mit Pfad ist gültig", not [e for e in errors("ui_sandbox:\n  path: /ui-sandbox") if "ui_sandbox" in e])
ok("Pfad / ist abgewiesen", any("ui_sandbox.path" in e for e in errors("ui_sandbox:\n  path: /")))
ok("eine URL ist abgewiesen", any("ui_sandbox.path" in e for e in errors("ui_sandbox:\n  path: https://x/")))
ok("ein fremdes Feld ist abgewiesen", any("only 'path'" in e for e in errors("ui_sandbox:\n  path: /s\n  open: true")))

print("")
print("=== Adresse, Umgebung, Block ===")
CHAT, out = install("chat", "chat", "ui_sandbox:\n  path: /ui-sandbox")
ok("eine App mit ui_sandbox wird installiert", CHAT is not None, out)
reg = m.load_registry()
inst = reg["instances"][CHAT]
ok("... das Register kennt den Pfad", inst.get("ui_sandbox") == {"path": "/ui-sandbox"}, inst.get("ui_sandbox"))
hosts = m.instance_sandbox_hosts(CHAT, inst, ext_host="node.example", reg=reg)
ok("... und sie bekommt die zweite Adresse <name>--ui.<mandant>.<knoten>",
   hosts == ["chat--ui.kunde.node.example"], hosts)
ok("... und erfährt sie als OAAP_UI_SANDBOX_ORIGINS",
   m.sandbox_origins_env(CHAT, inst, ("node.example", ""), reg) == "https://chat--ui.kunde.node.example")
PLAIN, _ = install("orders", "orders")
ok("eine App ohne ui_sandbox bekommt keine",
   m.instance_sandbox_hosts(PLAIN, m.load_registry()["instances"][PLAIN], ext_host="node.example") == [])

m.write_external_caddy()
site = io.open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), encoding="utf-8").read()
block = site.split("https://chat--ui.kunde.node.example {")[1].split("\n}\n")[0] if \
    "https://chat--ui.kunde.node.example {" in site else ""
ok("der Gateway-Block der zweiten Adresse steht da", bool(block), site[-1500:])
ok("... reicht nur den Sandbox-Pfad durch, alles andere 404",
   "@sandbox path /ui-sandbox /ui-sandbox/*" in block and "respond 404" in block)
ok("... ohne Anmeldung (kein forward_auth)", "forward_auth" not in block)
ok("... ohne Cookie, Authorization und Identitäts-Kopfzeilen, ohne Set-Cookie in der Antwort",
   "request_header -Cookie" in block and "request_header -Authorization" in block
   and all(f"request_header -{h}" in block for h in m.IDENTITY_HEADERS) and "header -Set-Cookie" in block, block)
ok("... einbetten dürfen nur die eigenen Adressen der Instanz",
   'frame-ancestors https://chat.kunde.node.example"' in block, block)
ok("... und der Block der Chat-Adresse selbst bleibt unverändert geschützt",
   "forward_auth" in site.split("https://chat.kunde.node.example {")[1].split("\n}\n")[0])

m.load_external_conf = lambda: ("node.example", "10.0.0.1")
m.write_external_caddy()
site_e = io.open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), encoding="utf-8").read()
block_e = site_e.split("http://chat--ui.kunde.node.example {")[1].split("\n}\n")[0] \
    if "http://chat--ui.kunde.node.example {" in site_e else ""
reg = m.load_registry()
ok("hinter einem Edge: OAAP_UI_SANDBOX_ORIGINS nennt die Herkunft des Browsers (https)",
   m.sandbox_origins_env(CHAT, reg["instances"][CHAT], ("node.example", "10.0.0.1"), reg) == "https://chat--ui.kunde.node.example")
ok("... und frame-ancestors erlaubt https (was der Browser sieht) und http (was der Knoten bedient)",
   "frame-ancestors https://chat.kunde.node.example http://chat.kunde.node.example" in block_e, block_e)
m.load_external_conf = lambda: ("node.example", "")

print("")
print("=== reservierte Endung ===")
X, out = install("chat", "spiel--ui")
ok("eine Instanz namens ...--ui wird nicht installiert", X is None and "reserved" in out, out[-300:])
key, err = m.rename_check(m.load_registry(), CHAT, "chat2--ui")
ok("... und niemand benennt eine um in ...--ui", not key and "reserved" in err, err)
reg = m.load_registry()
legacy = dict(reg["instances"][PLAIN], name="chat--ui")
reg["instances"]["kunde-chat--ui"] = legacy
m.save_registry(reg)
reg = m.load_registry()
ok("eine Altlast mit so einem Namen verdrängt nichts: die Sandbox entfällt (fail closed)",
   m.instance_sandbox_hosts(CHAT, reg["instances"][CHAT], ext_host="node.example", reg=reg) == [])
del reg["instances"]["kunde-chat--ui"]
m.save_registry(reg)
reg = m.load_registry()
reg["instances"][CHAT]["rehearsal"] = {"of": "chat", "until": "2999-01-01T00:00:00Z"}
ok("eine Generalprobe bekommt keine",
   m.instance_sandbox_hosts(CHAT, reg["instances"][CHAT], ext_host="node.example", reg=reg) == [])

print("")
print("PASS" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
