#!/usr/bin/env python3
"""The `link_state` door (manifest 0.14, RFC-0087 §8.1, runtime spec 2.33).

A tunnel app declares where the node may ask how its tunnel and its targets
are. Defended here:

- the manifest rule (the rule of `mcp:`): a local path under one of the
  app's routes, nothing a URL could smuggle, only `path`;
- the registry record carries it under `link_state` -- NOT `links`, which
  is the RFC-0016 partner list (found while building: the obvious word was
  taken);
- the internal endpoint (gateway:8099, platform network only) gets
  `/l/<name>` -> that path beside `/h/<name>`, and only for instances
  that declare it;
- the portal asks it as it probes health, and a sleeping instance is not
  asked (its targets are `paused`).

Run: python3 test/test_link_state_door.py
"""
import contextlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-link-state-test-")
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
    base = {"oaap_manifest": "0.14",
            "app": {"id": "vpn-client", "name": "VPN", "version": "0.2.0", "type": "native"},
            "services": {"vpn": {"build": ".", "port": 8080}},
            "routes": [{"path": "/", "roles": ["admin"]}]}
    base.update(extra)
    return base


print("=== Manifest ===")
ok("0.14 ist die eigene Version", m.MANIFEST_MINOR == 14)
ok("link_state.path unter einer Route: gültig",
   m.link_state_door_error(manifest(link_state={"path": "/oaap/links"})) == "")
cases = [({"path": "http://x/oaap"}, "local path"), ({"path": "//x/y"}, "local path"),
         ({"path": "/a/../b"}, "local path"), ({"path": "/"}, "not /"),
         ({"path": "/x?y=1"}, "local path"), ({"path": "/l", "port": 1}, "only 'path'"),
         ("/oaap/links", "object expected")]
for door, word in cases:
    err = m.link_state_door_error(manifest(link_state=door))
    ok(f"abgelehnt: {door!r}", word in err, err)
narrow = manifest(link_state={"path": "/oaap/links"}, routes=[{"path": "/app", "roles": ["admin"]}])
ok("außerhalb jeder Route: abgelehnt", "none of the app's routes" in m.link_state_door_error(narrow))


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
bad, said = validated(manifest(link_state={"path": "x"}, **HEALTH))
ok("validate_manifest lehnt ab und sagt den Satz", bad and "link_state.path" in said, said)
bad, said = validated(manifest(link_state={"path": "/oaap/links"}, **HEALTH))
ok("validate_manifest: gültige Tür, keine Ablehnung", not bad and "link_state" not in said, said)
ok("link_state_door liest Manifest und Registereintrag",
   m.link_state_door({"link_state": {"path": "/oaap/links"}}) == "/oaap/links"
   and m.link_state_door({"links": ["partner"]}) == "" and m.link_state_door(None) == "")

print("=== Registereintrag ===")
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("der Eintrag heißt link_state (links ist die Partnerliste, RFC-0016)",
   '"link_state": ({"path": link_state_door(m)} if link_state_door(m) else {}),' in src)

print("=== interner Endpunkt ===")
m.docker_subnets = lambda net=None: ["172.18.0.0/16"]
reg = m.load_registry()
reg["instances"]["ais-ovc"] = {"container": "oaap-ais-ovc", "svc_port": 8080,
                               "health_path": "/healthz",
                               "link_state": {"path": "/oaap/links"}}
reg["instances"]["ais-mg"] = {"container": "oaap-ais-mg", "svc_port": 8000,
                              "health_path": "/health", "links": ["ais-ovc"]}
m.save_registry(reg)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
m.write_internal_health_caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "_internal-health.caddy"), encoding="utf-8") as f:
    caddy = f.read()
ok("/l/ais-ovc -> /oaap/links, auf den Dienst der App",
   "handle /l/ais-ovc {\n\t\t\trewrite * /oaap/links\n\t\t\treverse_proxy oaap-ais-ovc:8080" in caddy,
   caddy)
ok("/h/ais-ovc bleibt, wie es war", "handle /h/ais-ovc {\n\t\t\trewrite * /healthz" in caddy)
ok("ohne Tür kein /l/ -- auch nicht für eine Instanz mit Partnerliste",
   "/l/ais-mg" not in caddy and "handle /h/ais-mg" in caddy, caddy)
ok("alles hinter remote_ip des Plattformnetzes",
   caddy.index("@platform remote_ip 172.18.0.0/16") < caddy.index("/l/ais-ovc"))

print("=== Portal: wer gefragt wird ===")
psrc = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
            encoding="utf-8").read()
body = psrc[psrc.index("def _link_reports("):psrc.index("def _fleet_links(")]
ok("das Portal fragt über /l/<name>, 2 s",
   'f"{GATEWAY_HEALTH}/l/{name}", timeout=2' in body)
ok("eine schlafende Instanz oder eine in Wartung wird nicht gefragt (paused)",
   "_sleep_status(inst) or inst.get(\"maintenance\")" in body and "fleet_links.PAUSED" in body)
ok("der Bericht geht in build()", "reports=_link_reports(insts)" in psrc)

print("")
print("ALLE GRUEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
