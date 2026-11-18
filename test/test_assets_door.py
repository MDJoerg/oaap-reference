#!/usr/bin/env python3
"""The `assets` door (manifest 0.15, RFC-0087 §8.2, runtime spec 2.34).

An app declares where the node may ask for its assets (model names,
sources) and how they hang together. Defended here, as for `link_state`:

- the manifest rule (the rule of `mcp:`, one shared function now);
- the registry record carries it under `assets`;
- the internal endpoint gets `/a/<name>` -> that path, only for instances
  that declare it, and both doors side by side for an app with both;
- the portal asks it at /a/<name>, 2 s; a sleeping instance is not asked.

Run: python3 test/test_assets_door.py
"""
import contextlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-assets-door-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def manifest(**extra):
    base = {"oaap_manifest": "0.15",
            "app": {"id": "ai-gateway", "name": "KI", "version": "0.7.0", "type": "native"},
            "services": {"web": {"build": ".", "port": 8000}},
            "routes": [{"path": "/", "roles": ["admin"]}]}
    base.update(extra)
    return base


print("=== Manifest ===")
ok("0.15 kennt die Plattform (0.16 seit RFC-0091 Stufe 3)", m.MANIFEST_MINOR >= 15)
ok("assets.path unter einer Route: gültig",
   m.assets_door_error(manifest(assets={"path": "/oaap/assets"})) == "")
cases = [({"path": "http://x/oaap"}, "local path"), ({"path": "//x/y"}, "local path"),
         ({"path": "/a/../b"}, "local path"), ({"path": "/"}, "not /"),
         ({"path": "/x?y=1"}, "local path"), ({"path": "/a", "port": 1}, "only 'path'"),
         ("/oaap/assets", "object expected")]
for door, word in cases:
    err = m.assets_door_error(manifest(assets=door))
    ok(f"abgelehnt: {door!r}", word in err and err.startswith("assets"), err)
narrow = manifest(assets={"path": "/oaap/assets"}, routes=[{"path": "/app", "roles": ["admin"]}])
ok("außerhalb jeder Route: abgelehnt", "none of the app's routes" in m.assets_door_error(narrow))
ok("link_state sagt weiter seine eigenen Sätze (gemeinsame Regel)",
   m.link_state_door_error(manifest(link_state={"path": "/"})).startswith("link_state.path")
   and "/oaap/links" in m.link_state_door_error(manifest(link_state={"path": "/"})))


def validated(mf):
    """(exited, what it printed) -- validate_manifest refuses by exiting."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.validate_manifest(mf)
    except SystemExit:
        return True, buf.getvalue()
    return False, buf.getvalue()


HEALTH = {"health": {"path": "/healthz"}}
bad, said = validated(manifest(assets={"path": "x"}, **HEALTH))
ok("validate_manifest lehnt ab und sagt den Satz", bad and "assets.path" in said, said)
bad, said = validated(manifest(assets={"path": "/oaap/assets"}, **HEALTH))
ok("validate_manifest: gültige Tür, keine Ablehnung", not bad and "assets" not in said, said)
ok("assets_door liest Manifest und Registereintrag",
   m.assets_door({"assets": {"path": "/oaap/assets"}}) == "/oaap/assets"
   and m.assets_door({"assets": {}}) == "" and m.assets_door(None) == "")

print("=== Registereintrag ===")
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("der Eintrag heißt assets",
   '"assets": ({"path": assets_door(m)} if assets_door(m) else {}),' in src)

print("=== interner Endpunkt ===")
m.docker_subnets = lambda net=None: ["172.18.0.0/16"]
reg = m.load_registry()
reg["instances"]["ais-mg"] = {"container": "oaap-ais-mg", "svc_port": 8000,
                              "health_path": "/healthz", "assets": {"path": "/oaap/assets"}}
reg["instances"]["both"] = {"container": "oaap-both", "svc_port": 8080, "health_path": "/h",
                            "link_state": {"path": "/oaap/links"},
                            "assets": {"path": "/oaap/assets"}}
reg["instances"]["plain"] = {"container": "oaap-plain", "svc_port": 80, "health_path": "/"}
m.save_registry(reg)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
m.write_internal_health_caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "_internal-health.caddy"), encoding="utf-8") as f:
    caddy = f.read()
ok("/a/ais-mg -> /oaap/assets, auf den Dienst der App",
   "handle /a/ais-mg {\n\t\t\trewrite * /oaap/assets\n\t\t\treverse_proxy oaap-ais-mg:8000" in caddy,
   caddy)
ok("eine App mit beiden Türen: /l/ und /a/",
   "handle /l/both {" in caddy and "handle /a/both {" in caddy, caddy)
ok("ohne Tür kein /a/", "/a/plain" not in caddy and "handle /h/plain" in caddy, caddy)
ok("alles hinter remote_ip des Plattformnetzes",
   caddy.index("@platform remote_ip 172.18.0.0/16") < caddy.index("/a/ais-mg"))

print("=== Portal: wer gefragt wird ===")
psrc = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
            encoding="utf-8").read()
body = psrc[psrc.index("def _asset_reports("):psrc.index("def _fleet_links(")]
ok("das Portal fragt über /a/<name>, 2 s",
   'f"{GATEWAY_HEALTH}/a/{name}", timeout=2' in body)
ok("eine schlafende Instanz oder eine in Wartung wird nicht gefragt (paused)",
   "_sleep_status(inst) or inst.get(\"maintenance\")" in body and "fleet_links.PAUSED" in body)
ok("die Antwort geht durch den Leser", "fleet_links.read_assets_answer(r.json())" in body)
ok("der Bericht geht in build()", "assets=_asset_reports(insts)" in psrc)

print("")
print("ALLE GRUEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
