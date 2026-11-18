"""Erkunden ohne Person (RFC-0073 §16, D14/D15, 0.1.300).

    python3 test/test_mcp_discover.py

Was hier gehalten wird (jede Zeile hat einen Koeder):

A. Die Site eines verbrauchenden Gateways traegt je Quelle neben der Tuer von
   Verfahren I eine Erkundungs-Tuer /platform/mcp/discover/<quelle>: Groesse
   begrenzt, der Koerper geht als X-OAAP-MCP-Request an identity, die Tuer
   fragt ?discover=<scope des Gateways>, und die App bekommt weder diese
   Kopfzeile noch Schluessel, Act-Token oder Zusicherung. Die Liste der
   Quellen nennt die Adresse der Erkundungs-Tuer. Eine Site ohne
   mcp_sources oder in Wartung traegt keine. Format 8: alte Sites sind
   hinter.
B. discover_refusal (rein): nur die Katalog-Methoden per POST, DELETE ohne
   Koerper. Aufruf, Lesen, Prompt holen, Stapel, Antwort, GET, verschachtelte
   Methode, \\u-Escape, doppelter Schluessel, zu gross: abgelehnt.
C. /verify?discover=oaap.mcp.sources:<gw>&instance=<quelle> (identity):
   der Quellen-Schluessel GENAU dieses Gateways, derselbe Mandant, keine
   Person dabei; die Antwort traegt den Maschinen-Benutzer des Gateways und
   keine Zusicherung; die Route der Quelle gilt fuer ihn; Ablehnungen und
   das Oeffnen einer Sitzung stehen im Mandantenprotokoll, ohne Koerper.
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "platform", "services")
IDENTITY_DIR = os.path.join(SERVICES, "identity")
DATA = tempfile.mkdtemp(prefix="oaap-mcp-discover-test-")
os.environ["OAAP_DATA_DIR"] = os.path.join(DATA, "appctl")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, SERVICES)

import appctl as a                                            # noqa: E402

a.reload_gateway = lambda: None
os.makedirs(a.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


# ------------------------------------------------------------------ A. Site
print("A. Die Erkundungs-Tuer auf der Site des Gateways")


def record(name, tenant, app_id="crm", port=8101, mcp="", sources=False, **extra):
    c = f"oaap-app-{name}"
    r = {"app_id": app_id, "app_name": app_id, "version": "1.0.0",
         "channel": "production", "port": port, "container": c, "svc_port": 8000,
         "image": f"oaap-app/{app_id}:1.0.0", "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 8000}],
         "routes": [{"path": "/", "roles": ["user", "admin"]}], "visibility": {},
         "mcp": {"path": mcp} if mcp else {}, "mcp_sources": sources}
    r.update(extra)
    return r


def site(name):
    with open(os.path.join(a.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


def block(body, opener):
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == opener:
            out, depth = [], 0
            for x in lines[i + 1:]:
                if x == "\t}" and depth == 0:
                    return out
                s = x.strip()
                depth += s.endswith("{") - (s == "}")
                out.append(s)
    return None


tid = a.ensure_default_tenant()
reg = a.load_registry()
reg["instances"]["gw"] = record("gw", tid, "mcp-gateway", 8120, sources=True)
reg["instances"]["help"] = record("help", tid, "help", 8121, mcp="/mcp")
reg["instances"]["crm"] = record("crm", tid, "crm", 8122)
a.save_registry(reg)
for n in ("gw", "help", "crm"):
    a.write_app_caddy(n, a.load_registry()["instances"][n])
body = site("gw")
d = block(body, "handle /platform/mcp/discover/help {")
ok("die Site des Gateways traegt die Erkundungs-Tuer fuer help", d is not None, body[-1500:])
d = d or []
ok("… auch mit Schraegstrich am Ende", block(body, "handle /platform/mcp/discover/help/ {") is not None)
ok("… die Groesse ist begrenzt, bevor der Koerper gelesen wird",
   "request_body {" in d and f"max_size {a.MCP_DISCOVER_MAX_BODY}" in d
   and d.index("request_body {") < next(i for i, x in enumerate(d) if x.startswith("forward_auth")), d)
fa = next((x for x in d if x.startswith("uri /verify")), "")
ok("… gefragt wird ?discover= mit dem Scope DIESES Gateways, Quelle und Route",
   "discover=oaap.mcp.sources:gw" in fa and "instance=help" in fa and "roles=admin,user" in fa
   and "act=" not in fa, fa)
ok("… der Koerper geht als Kopfzeile an identity",
   f"header_up {a.MCP_DISCOVER_HEADER} {{http.request.body_base64}}" in d, d)
for h in ("Authorization", "X-OAAP-Act-Token", "X-OAAP-Act-Assertion", a.MCP_DISCOVER_HEADER):
    ok(f"… und {h} erreicht die App nicht", f"request_header -{h}" in d, d)
ok("… es geht zur declarierten Tuer der Quelle", "rewrite * /mcp" in d and any("oaap-app-help:8000" in x for x in d), d)
ok("die Tuer von Verfahren I steht unveraendert daneben",
   block(body, "handle /platform/mcp/to/help {") is not None
   and not any("discover" in x for x in block(body, "handle /platform/mcp/to/help {")))
ok("keine Erkundungs-Tuer fuer eine Instanz ohne mcp: (crm)",
   "/platform/mcp/discover/crm" not in body)
ok("die Site einer Quelle ohne mcp_sources traegt keine", "/platform/mcp/discover/" not in site("help"))
srcs = a.mcp_sources_of(a.load_registry()["instances"], "gw")
ok("die Liste der Quellen nennt die Adresse der Erkundungs-Tuer",
   srcs and srcs[0]["discover"] == f"http://{a.GATEWAY_CONTAINER}:8120/platform/mcp/discover/help", srcs)
ok("die Site mit Erkundungs-Tuer gilt nicht als veraltet (die Kopfzeile ist keine Identitaets-Kopfzeile)",
   not a._site_identity_stale(body))
ok("Format 8: eine Site von vorher ist hinter",
   a.CASES_BLOCK_FORMAT == 8 and f"{a.CASES_MARKER}8 " in body)
reg = a.load_registry()
reg["instances"]["gw"]["maintenance"] = {"since": "2026-11-18T00:00:00Z", "by": "root"}
a.save_registry(reg)
a.write_app_caddy("gw", reg["instances"]["gw"])
ok("in Wartung: keine Erkundungs-Tuer (wie die Tuer von Verfahren I)",
   "/platform/mcp/discover/" not in site("gw") and "/platform/mcp/to/" not in site("gw"))

# --------------------------------------------------------- B./C. identity
print("")
print("B. discover_refusal")
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    sys.exit(2)

IDATA = os.path.join(DATA, "identity")
os.makedirs(IDATA)
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = IDATA
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
import act                                                    # noqa: E402


def rq(method, mid=1, **params):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params:
        msg["params"] = params
    return json.dumps(msg, separators=(",", ":"))


for meth in ("initialize", "tools/list", "resources/list", "resources/templates/list",
             "prompts/list", "ping"):
    ok(f"durch: {meth}", m.discover_refusal("POST", rq(meth)) == "")
ok("durch: notifications/initialized (ohne id)",
   m.discover_refusal("POST", '{"jsonrpc":"2.0","method":"notifications/initialized"}') == "")
ok("durch: tools/list mit Cursor", m.discover_refusal("POST", rq("tools/list", cursor="abc")) == "")
ok("durch: DELETE ohne Koerper (Sitzung schliessen)", m.discover_refusal("DELETE", "") == "")
ok("durch: resources/read einer Ansicht (ui://, RFC-0075)",
   m.discover_refusal("POST", rq("resources/read", uri="ui://vorgaenge/liste.html")) == "")
for label, params in (("file://", {"uri": "file:///etc/passwd"}),
                      ("help://", {"uri": "help://x"}),
                      ("UI:// gross", {"uri": "UI://x"}),
                      ("nur ui://", {"uri": "ui://"}),
                      ("ohne uri", {}),
                      ("uri keine Zeichenkette", {"uri": ["ui://x"]}),
                      ("ui:// mit weiterem Parameter", {"uri": "ui://x", "arguments": {"a": 1}})):
    why = m.discover_refusal("POST", rq("resources/read", **params))
    ok(f"abgelehnt: resources/read {label}", bool(why), why)
ok("abgelehnt: resources/read ohne params",
   bool(m.discover_refusal("POST", '{"jsonrpc":"2.0","id":1,"method":"resources/read"}')))
BAITS = (
    ("tools/call", "POST", rq("tools/call", name="help_search", arguments={"q": "x"})),
    ("resources/read", "POST", rq("resources/read", uri="help://x")),
    ("prompts/get", "POST", rq("prompts/get", name="guide")),
    ("completion/complete", "POST", rq("completion/complete")),
    ("Stapel mit tools/list", "POST", "[" + rq("tools/list") + "]"),
    ("JSON-RPC-Antwort", "POST", '{"jsonrpc":"2.0","id":1,"result":{}}'),
    ("verschachtelte Methode", "POST",
     '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"method":"tools/list"}}'),
    ("\\u006dethod mit tools/call", "POST", '{"jsonrpc":"2.0","id":1,"\\u006dethod":"tools/call"}'),
    ("doppelter Schluessel (erst list, dann call)", "POST",
     '{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"tools/call"}'),
    ("doppelt ueber Escape", "POST",
     '{"jsonrpc":"2.0","id":1,"method":"tools/list","\\u006dethod":"tools/call"}'),
    ("ohne jsonrpc 2.0", "POST", '{"id":1,"method":"tools/list"}'),
    ("Methode keine Zeichenkette", "POST", '{"jsonrpc":"2.0","id":1,"method":["tools/list"]}'),
    ("kein JSON", "POST", "tools/list"),
    ("leerer Koerper", "POST", ""),
    ("9 KiB", "POST", rq("tools/list", pad="x" * 9216)),
    ("GET", "GET", ""),
    ("PUT", "PUT", rq("tools/list")),
    ("DELETE mit Koerper", "DELETE", rq("tools/call")),
    ("ohne Methode der Anfrage", "", rq("tools/list")),
)
for label, hm, raw in BAITS:
    why = m.discover_refusal(hm, raw)
    ok(f"abgelehnt: {label}", bool(why), why)
ok("verschachtelte JSON-Tiefe faellt nicht um, sie wird abgelehnt",
   bool(m.discover_refusal("POST", "[" * 5000 + "]" * 5000)))


def b64(s):
    return __import__("base64").b64encode(s.encode("utf-8")).decode("ascii") if s else ""


print("")
print("B2. die Kopfzeile ist base64 (ein Zeilenumbruch passt in keine Kopfzeile)")
pretty = '{\n  "jsonrpc": "2.0",\n  "id": 1,\n  "method": "tools/list",\n  "params": {"x": "äöü"}\n}'
ok("ein formatierter Koerper mit Umlaut kommt unversehrt an",
   m.discover_body(b64(pretty)) == pretty)
ok("… und geht durch", m.discover_refusal("POST", m.discover_body(b64(pretty))) == "")
ok("leere Kopfzeile: leerer Koerper", m.discover_body("") == "")
for label, raw in (("kein base64", "{\"method\":\"tools/list\"}"),
                   ("base64 mit fremden Zeichen", b64(rq("tools/list"))[:-4] + "!!!="),
                   ("kein UTF-8", __import__("base64").b64encode(b"\xff\xfe{}").decode()),
                   ("zu lang", "A" * 12000)):
    body = m.discover_body(raw)
    ok(f"abgelehnt: {label}", body is None and bool(m.discover_refusal("POST", body)), body)

print("")
print("C. /verify?discover=... (identity)")
m.USERS_FILE = os.path.join(IDATA, "users.json")
m.KEYS_FILE = os.path.join(IDATA, "api-keys.json")
m.THROTTLE_FILE = os.path.join(IDATA, "login-throttle.json")
m.AUDIT_LOG = os.path.join(IDATA, "audit.jsonl")
m.TENANTS_FILE = os.path.join(IDATA, "tenants.json")
m.ASSERTION_KEY_FILE = os.path.join(IDATA, "assertion.key")
with open(m.ASSERTION_KEY_FILE, "w", encoding="ascii") as f:
    f.write("ab" * 32)
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-vp": {"label": "vp"}, "t-sgl": {"label": "sgl"}}}, f)


def user(uid, name, roles, tenant, kind="human"):
    return {"id": uid, "username": name, "display_name": name, "password_hash": "",
            "kind": kind, "roles": roles, "groups": [], "tenant": tenant, "active": True,
            "session_epoch": 0, "email": "", "email_verified": False}


with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump([user("u-chef", "vp-chef", ["tenant_admin", "user", "admin"], "t-vp"),
               user("u-gw", "instance:gw", ["user"], "t-vp", "machine"),
               user("u-gw2", "instance:gw2", ["user"], "t-vp", "machine"),
               user("u-gws", "instance:gw-sgl", ["user"], "t-sgl", "machine")], f)


def key_for(principal, scope, roles=("user",)):
    _rec, secret = m.issue_key(m.load_users(), principal, list(roles), scope, "test", 30, "root")
    return secret


GW_KEY = key_for("instance:gw", "oaap.mcp.sources:gw")
GW2_KEY = key_for("instance:gw2", "oaap.mcp.sources:gw2")
GWS_KEY = key_for("instance:gw-sgl", "oaap.mcp.sources:gw-sgl")
GW_FREE = key_for("instance:gw", "")
CHEF_FREE = key_for("vp-chef", "", roles=("user", "admin"))
c = m.app.test_client()


def door(raw, key=GW_KEY, gw="gw", target="help", tenant="t-vp", roles="admin,user",
         method="POST", groups="", extra=None):
    h = {"X-Forwarded-Method": method}
    if key:
        h["Authorization"] = "Bearer " + key
    if raw is not None:
        h[m.DISCOVER_HEADER] = b64(raw)
    h.update(extra or {})
    q = f"/verify?roles={roles}&tenant={tenant}&instance={target}&discover=oaap.mcp.sources:{gw}"
    if groups:
        q += f"&groups={groups}"
    return c.get(q, headers=h)


def text(r):
    return r.get_data(as_text=True)


r = door(rq("tools/list"))
ok("tools/list mit dem Schluessel des Gateways: 204", r.status_code == 204, (r.status_code, text(r)))
ok("… die App sieht den Maschinen-Benutzer des Gateways",
   r.headers.get("X-OAAP-User") == "instance:gw" and r.headers.get("X-OAAP-User-Id") == "u-gw"
   and r.headers.get("X-OAAP-Roles") == "user", dict(r.headers))
ok("… und keine Zusicherung (keine Person am Bildschirm)",
   not r.headers.get("X-OAAP-User-Assertion"), r.headers.get("X-OAAP-User-Assertion"))
r = door(rq("initialize"))
ok("initialize: 204", r.status_code == 204, (r.status_code, text(r)))
r = door(rq("resources/read", uri="ui://help/ansicht.html"))
ok("resources/read einer Ansicht (ui://): 204", r.status_code == 204, (r.status_code, text(r)))
r = door(rq("resources/read", uri="help://geheim"))
ok("resources/read einer anderen Resource: 403", r.status_code == 403 and "ui://" in text(r),
   (r.status_code, text(r)))
r = door("", method="DELETE")
ok("DELETE ohne Koerper: 204", r.status_code == 204, (r.status_code, text(r)))
r = door(rq("tools/call", name="help_search"))
ok("tools/call: 403 mit Grund", r.status_code == 403 and "tools/call" in text(r), (r.status_code, text(r)))
r = door('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"method":"tools/list"}}')
ok("verschachteltes tools/list unter tools/call: 403", r.status_code == 403, (r.status_code, text(r)))
r = door(None)
ok("POST ohne die Kopfzeile mit dem Koerper: 403", r.status_code == 403, (r.status_code, text(r)))
r = c.get("/verify?roles=admin,user&tenant=t-vp&instance=help&discover=oaap.mcp.sources:gw",
          headers={"Authorization": "Bearer " + GW_KEY, "X-Forwarded-Method": "POST",
                   m.DISCOVER_HEADER: rq("tools/list")})
ok("der Koerper unkodiert in der Kopfzeile: 403 (kommt nur base64 an)",
   r.status_code == 403 and "intact" in text(r), (r.status_code, text(r)))
r = door("", method="GET")
ok("GET: 403", r.status_code == 403, (r.status_code, text(r)))
r = door(rq("tools/list"), key=None)
ok("ohne Schluessel: 401", r.status_code == 401, (r.status_code, text(r)))
for label, k in (("ein UNBESCHRAENKTER Schluessel (tenant_admin)", CHEF_FREE),
                 ("ein unbeschraenkter Schluessel DESSELBEN Prinzipals", GW_FREE),
                 ("der Schluessel eines ANDEREN Gateways", GW2_KEY)):
    r = door(rq("tools/list"), key=k)
    ok(f"{label}: 403", r.status_code == 403, (r.status_code, text(r)))
r = door(rq("tools/list"), key=GWS_KEY, gw="gw-sgl")
ok("ein Gateway aus sgl an einer Quelle in vp: 403 (Mandanten verschieden)",
   r.status_code == 403 and "tenant" in text(r), (r.status_code, text(r)))
r = door(rq("tools/list"), extra={act.HEADER: "eyJ.x.y"})
ok("mit Act-Token: abgelehnt (an dieser Tuer gibt es keine Person)",
   r.status_code == 401 and "no person" in text(r), (r.status_code, text(r)))
r = door(rq("tools/list"), extra={"X-OAAP-Act-Assertion": "abc"})
ok("mit Zusicherung: abgelehnt", r.status_code == 401, (r.status_code, text(r)))
r = door(rq("tools/list"), roles="tenant_admin")
ok("Route der Quelle verlangt tenant_admin: 403, der Grund nennt das Gateway",
   r.status_code == 403 and "gateway's principal" in text(r), (r.status_code, text(r)))
r = door(rq("tools/list"), groups="nur-anna")
ok("Quelle mit Sichtbarkeitsgruppe, das Gateway in keiner: 403",
   r.status_code == 403 and "visibility group" in text(r), (r.status_code, text(r)))
r = c.get("/verify?roles=user&tenant=t-vp&instance=help&discover=oaap.cases.submit:gw",
          headers={"Authorization": "Bearer " + GW_KEY, "X-Forwarded-Method": "POST",
                   m.DISCOVER_HEADER: b64(rq("tools/list"))})
ok("discover mit einem Scope, der keine MCP-Tuer ist: 403", r.status_code == 403, (r.status_code, text(r)))

with open(m.AUDIT_LOG, encoding="utf-8") as f:
    log = [json.loads(ln) for ln in f]
disc = [e for e in log if e["action"] == "mcp.discover"]
ok("das Oeffnen einer Sitzung steht im Mandantenprotokoll (Quelle, Gateway)",
   any(e["result"] == "ok" and e["subject"] == "help" and e["who"] == "instance:gw" for e in disc), disc[:3])
ok("… einmal je initialize, nicht je Liste",
   len([e for e in disc if e["result"] == "ok"]) == 1, [e for e in disc if e["result"] == "ok"])
ok("… die Ablehnungen auch (tools/call, Route)",
   any(e["result"] == "denied" and "tools/call" in e.get("detail", "") for e in disc)
   and any(e["result"] == "denied" and "role" in e.get("detail", "") for e in disc))
raw = open(m.AUDIT_LOG, encoding="utf-8").read()
ok("… und weder Koerper noch Schluessel stehen darin",
   "help_search" not in raw and GW_KEY not in raw and '"jsonrpc"' not in raw)

print("")
print("FEHLER: %d" % fails if fails else "Alles gruen.")
sys.exit(1 if fails else 0)
