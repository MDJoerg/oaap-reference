"""Apps als MCP-Quellen, Stufe 1 (RFC-0073, Manifest 0.11, 0.1.237).

    python3 test/test_mcp_sources.py

Was hier gehalten wird (jede Zeile hat einen Koeder):

1. Manifest: `mcp: {path}` nur als lokaler Pfad unter einer eigenen Route;
   `openapi:` ist reserviert und wird abgelehnt; `mcp_sources.read` nur als
   woertliches true.
2. Der Registry-Eintrag traegt Tuer und Verbraucher, neu gelesen bei jeder
   Installation.
3. Nur die Site eines VERBRAUCHENDEN Gateways traegt /platform/mcp/sources;
   gefragt wird der Schluessel genau dieser Instanz, keine Rolle.
4. Die Liste: derselbe Mandant, Tuer erklaert, keine Generalprobe, nicht das
   Gateway selbst; ein Gateway eines anderen Mandanten sieht nichts davon.
5. Kommt, geht oder aendert sich eine Quelle, ist die Site des Gateways
   hinter (Marker) und wird neu geschrieben.
6. Schluessel und Adresse sind Plattformvariablen, werden bei Umbenennen neu
   gepraegt und beim Entfernen nicht in eine aufgehobene instance.env
   mitgenommen.
"""
import contextlib
import io as _io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-mcp-sources-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def record(name, tenant, app_id="crm", port=8101, mcp="", sources=False, **extra):
    c = f"oaap-app-{name}"
    r = {"app_id": app_id, "app_name": app_id, "version": "1.0.0",
         "channel": "production", "port": port, "container": c, "svc_port": 8000,
         "image": f"oaap-app/{app_id}:1.0.0", "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 8000}],
         "routes": [{"path": "/", "roles": ["user"]}], "visibility": {},
         "mcp": {"path": mcp} if mcp else {}, "mcp_sources": sources}
    r.update(extra)
    return r


def site(name):
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


def block(body, opener):
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == opener:
            out = []
            for x in lines[i + 1:]:
                if x == "\t}":
                    return out
                out.append(x.strip())
    return None


def listed(name):
    """Die Liste, wie sie in der Site steht ([] ohne Block)."""
    b = block(site(name), "handle /platform/mcp/sources {") or []
    resp = [x for x in b if x.startswith("respond `")]
    if not resp:
        return None
    return json.loads(resp[0][len("respond `"):resp[0].rindex("`")])["sources"]


# ------------------------------------------------------------- 1. Manifest
print("1. Manifest 0.11")
base = {"oaap_manifest": "0.11",
        "app": {"id": "probe", "version": "1.0.0", "name": "Probe", "type": "native"},
        "services": {"web": {"build": ".", "port": 8000}},
        "health": {"path": "/healthz"},
        "routes": [{"path": "/app", "roles": ["user"]}]}


def man_errs(extra):
    out, err = _io.StringIO(), _io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.validate_manifest(dict(base, **extra))
    except SystemExit:
        pass
    return [ln for ln in (out.getvalue() + err.getvalue()).splitlines()
            if ln.strip().startswith("- ")]


ok("mcp.path unter der eigenen Route geht durch", not man_errs({"mcp": {"path": "/app/mcp"}}))
ok("… die Route selbst als Pfad geht durch", not man_errs({"mcp": {"path": "/app"}}))
for bad, why in (({"path": "/mcp"}, "unter keiner Route"), ({"path": "mcp"}, "nicht lokal"),
                 ({"path": "//evil/mcp"}, "zwei Schraegstriche"),
                 ({"path": "/app/../admin"}, ".."), ({"path": "/app/mcp?x=1"}, "Abfrage"),
                 ({"path": "http://x/app"}, "Schema"), ({"path": "/app/mcp", "extra": 1}, "fremder Schluessel"),
                 ("/app/mcp", "keine Abbildung"), ({"path": "/app2/mcp"}, "nur Praefix der Route")):
    ok(f"mcp abgelehnt: {why}", any(e.strip().startswith("- mcp") for e in man_errs({"mcp": bad})),
       man_errs({"mcp": bad}))
ok("openapi: ist reserviert und wird mit I-39 abgelehnt",
   any("openapi" in e and "I-39" in e for e in man_errs({"openapi": {"path": "/app/openapi.json"}})))
ok("mcp_sources.read true geht durch", not man_errs({"mcp_sources": {"read": True}}))
for bad in ({"read": "yes"}, {"read": 1}, ["read"], {"read": True, "act": True}):
    ok(f"mcp_sources abgelehnt: {bad!r}",
       any("mcp_sources" in e for e in man_errs({"mcp_sources": bad})))
ok("nur ein woertliches true verlangt den Schluessel",
   [m.mcp_sources_wanted(x) for x in ({"mcp_sources": {"read": True}}, {"mcp_sources": {"read": "yes"}},
                                       {"mcp_sources": {}}, {})] == [True, False, False, False])
ok("die Manifest-Version kennt 0.11", m.MANIFEST_MINOR >= 11)
ok("Schluessel und Adresse sind Plattformvariablen",
   {"OAAP_MCP_SOURCES_KEY", "OAAP_MCP_SOURCES_URL"} <= m.RESERVED_ENV
   and "OAAP_MCP_SOURCES_KEY" in m.INSTANCE_KEY_VARS)

# -------------------------------------------------------- 3./4. Site + Liste
print("")
print("3./4. Die Tuer auf der Site des Gateways, die Liste")
tid = m.ensure_default_tenant()
OTHER = "t-other00000"
reg = m.load_registry()
reg["instances"]["gw"] = record("gw", tid, "mcp-gateway", 8120, sources=True)
reg["instances"]["kp"] = record("kp", tid, "kontext-probe", 8121, mcp="/mcp")
reg["instances"]["crm"] = record("crm", tid, "crm", 8122)                      # keine Tuer
reg["instances"]["kp-probe"] = record("kp-probe", tid, "kontext-probe", 8123, mcp="/mcp",
                                      rehearsal={"of": "kp", "expires": "2099-01-01T00:00:00Z"})
reg["instances"]["fremd-kp"] = record("fremd-kp", OTHER, "kontext-probe", 8124, mcp="/mcp")
reg["instances"]["fremd-gw"] = record("fremd-gw", OTHER, "mcp-gateway", 8125, sources=True, mcp="/mcp")
reg["instances"]["gw-probe"] = record("gw-probe", tid, "mcp-gateway", 8126, sources=True,
                                      rehearsal={"of": "gw", "expires": "2099-01-01T00:00:00Z"})
m.save_registry(reg)
for n in reg["instances"]:
    m.write_app_caddy(n, m.load_registry()["instances"][n])

b = block(site("gw"), "handle /platform/mcp/sources {")
ok("die Site des Gateways traegt /platform/mcp/sources", b is not None, site("gw")[:400])
ok("… gefragt wird der Schluessel DIESER Instanz, keine Rolle",
   b and f"uri /verify?roles=&tenant={tid}&instance=oaap.mcp.sources:gw" in b, b)
ok("… die Antwort ist JSON und wird nicht zwischengespeichert",
   b and "header Content-Type application/json" in b and "header Cache-Control no-store" in b, b)
body = site("gw")
ok("der Block steht vor /platform/* und vor der App-Route",
   body.index("handle /platform/mcp/sources {") < body.index("handle /platform/* {")
   and body.index("handle /platform/mcp/sources {") < body.index("\thandle {"))
names = [s["name"] for s in listed("gw") or []]
ok("die Liste: nur kp (derselbe Mandant, Tuer, keine Generalprobe, nicht sich selbst)",
   names == ["kp"], names)
src = (listed("gw") or [{}])[0]
ok("… mit App, Version, Kanal, Pfad und der Adresse am Plattform-Gateway",
   src == {"name": "kp", "app": "kontext-probe", "version": "1.0.0", "channel": "production",
           "path": "/mcp", "address": f"http://{m.GATEWAY_CONTAINER}:8121/mcp"}, src)
ok("das Gateway des anderen Mandanten sieht nur seine eigene Quelle",
   [s["name"] for s in listed("fremd-gw") or []] == ["fremd-kp"], listed("fremd-gw"))
ok("eine Quelle ohne mcp_sources traegt keine Liste",
   block(site("kp"), "handle /platform/mcp/sources {") is None
   and block(site("crm"), "handle /platform/mcp/sources {") is None)
ok("eine Generalprobe eines Gateways traegt keine Liste (D8)",
   block(site("gw-probe"), "handle /platform/mcp/sources {") is None)

# ------------------------------------------------------------ 5. Marker
print("")
print("5. Kommt, geht oder aendert sich eine Quelle")
ok("nach dem Schreiben ist nichts hinter", m._cases_sites_behind() == [], m._cases_sites_behind())
reg = m.load_registry()
reg["instances"]["kp2"] = record("kp2", tid, "kontext-probe", 8127, mcp="/api/mcp",
                                 routes=[{"path": "/api", "roles": ["user"]}])
m.save_registry(reg)
behind = m._cases_sites_behind()
ok("eine neue Quelle im Mandanten: die Site des Gateways ist hinter, andere nicht wegen ihr",
   "gw" in behind and "fremd-gw" not in behind and "kp" not in behind, behind)
m.refresh_cases_sites()
ok("… neu geschrieben, die Liste nennt beide",
   [s["name"] for s in listed("gw")] == ["kp", "kp2"], listed("gw"))
reg = m.load_registry()
del reg["instances"]["kp"]
m.save_registry(reg)
ok("eine Quelle geht: wieder hinter", "gw" in m._cases_sites_behind())
m.refresh_cases_sites()
ok("… und die Liste nennt nur noch kp2", [s["name"] for s in listed("gw")] == ["kp2"])
reg = m.load_registry()
reg["instances"]["kp2"]["mcp"] = {}
m.save_registry(reg)
m.refresh_cases_sites()
ok("eine Quelle, die ihre Tuer nicht mehr erklaert, faellt heraus", listed("gw") == [])

# ------------------------------------------------- 6. Schluessel
print("")
print("6. Schluessel")
MINTED = []


def fake_identity_exec(script, env=None):
    MINTED.append(dict(env or {}))
    return json.dumps(f"oaapk_id{len(MINTED)}_s{len(MINTED)}") + "\n"


m._identity_exec = fake_identity_exec
k = m._mcp_issue_sources_key("gw", tid)
ok("gepraegt fuer instance:gw im Mandanten, Scope oaap.mcp.sources:gw",
   k == "oaapk_id1_s1" and MINTED[-1]["OAAP_T_NAME"] == "instance:gw"
   and MINTED[-1]["OAAP_T_TENANT"] == tid and MINTED[-1]["OAAP_T_SCOPE"] == "oaap.mcp.sources:gw", MINTED)
out = m.instance_keys_issue("gw2", tid, "mcp-gateway", ["OAAP_MCP_SOURCES_KEY"], port=8130)
ok("Umbenennen praegt neu und nennt die Adresse der NEUEN Site",
   out.get("OAAP_MCP_SOURCES_URL") == f"http://{m.GATEWAY_CONTAINER}:8130/platform/mcp/sources"
   and MINTED[-1]["OAAP_T_SCOPE"] == "oaap.mcp.sources:gw2", out)

print("")
print("FEHLER: %d" % fails if fails else "Alles gruen.")
sys.exit(1 if fails else 0)
