"""Verfahren A: ein Chat handelt am MCP-Gateway als die Person (RFC-0074 Stufe 1).

    python3 test/test_mcp_via.py

Was hier gehalten wird (jede Zeile hat einen Koeder):

A. Generator (appctl, Manifest 0.12):
   - `mcp_client.use` nur als woertliches true; Schluessel und Adresse sind
     Plattformvariablen und folgen der Instanz (Umbenennen, Entfernen);
   - nur die Site eines Chats mit `mcp_client` traegt /platform/mcp/gateways
     und je Gateway seines Mandanten eine Tuer /platform/mcp/via/<gw>/;
     keine fuer ein Gateway eines anderen Mandanten, keine fuer eine
     Generalprobe, keine auf der Site einer Generalprobe des Chats;
   - die Tuer fragt identity mit via=Scope DIESES Chats, nimmt Schluessel und
     Act-Zusicherung weg, setzt den Namen des Chats SELBST, schreibt auf
     /mcp/ um und geht zum Container des Gateways;
   - kommt oder geht ein Gateway, ist die Site des Chats hinter;
   - die Tuer /platform/mcp/to/ nimmt auch die Act-Zusicherung weg.
B. Identity (/verify?via= und /verify?act= mit Zusicherung):
   - beide Nachweise noetig: der Client-Schluessel GENAU dieses Chats UND die
     Zusicherung, die die Plattform dem Chat fuer die Person gab (aud = Chat);
   - eine Zusicherung an eine andere Instanz, eine abgelaufene, eine
     gefaelschte, eine eines anderen Mandanten: 401; kein Rueckfall auf den
     Chat selbst;
   - die Antwort: die sechs Kopfzeilen der PERSON, die Zusicherung NEU an
     das Gateway adressiert (nie die des Chats weitergereicht);
   - ein Act-Token (Anbieter) an der via-Tuer: abgelehnt;
   - Verfahren I nimmt die eigene Zusicherung an das Gateway an (aud = gw),
     nicht die an den Chat; Token und Zusicherung zugleich: abgelehnt;
   - jeder Akt steht im Mandantenprotokoll (mcp.via / mcp.act, Verfahren A).
"""
import contextlib
import importlib
import io as _io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


# =============================================================== A. Generator
print("A. Generator (appctl)")
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-mcp-via-test-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

GW_ROUTES = [{"path": "/mcp", "roles": ["public"]},
             {"path": "/.well-known/oauth-protected-resource", "roles": ["public"]},
             {"path": "/", "roles": ["admin"]}]


def record(name, tenant, app_id="crm", port=8101, mcp="", sources=False, client=False, **extra):
    c = f"oaap-app-{name}"
    r = {"app_id": app_id, "app_name": app_id, "version": "1.0.0",
         "channel": "production", "port": port, "container": c, "svc_port": 8000,
         "image": f"oaap-app/{app_id}:1.0.0", "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 8000}],
         "routes": [{"path": "/", "roles": ["user"]}], "visibility": {},
         "mcp": {"path": mcp} if mcp else {}, "mcp_sources": sources, "mcp_client": client}
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


def gateways(name):
    b = block(site(name), "handle /platform/mcp/gateways {") or []
    resp = [x for x in b if x.startswith("respond `")]
    if not resp:
        return None
    return json.loads(resp[0][len("respond `"):resp[0].rindex("`")])["gateways"]


base = {"oaap_manifest": "0.12",
        "app": {"id": "chat", "version": "1.0.0", "name": "Chat", "type": "native"},
        "services": {"web": {"build": ".", "port": 8000}},
        "health": {"path": "/healthz"},
        "routes": [{"path": "/", "roles": ["user"]}]}


def man_errs(extra):
    out, err = _io.StringIO(), _io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.validate_manifest(dict(base, **extra))
    except SystemExit:
        pass
    return [ln for ln in (out.getvalue() + err.getvalue()).splitlines()
            if ln.strip().startswith("- ")]


ok("die Manifest-Version kennt 0.12", m.MANIFEST_MINOR >= 12)
ok("mcp_client.use true geht durch", not man_errs({"mcp_client": {"use": True}}),
   man_errs({"mcp_client": {"use": True}}))
for bad in ({"use": "yes"}, {"use": 1}, ["use"], {"use": True, "act": True}):
    ok(f"mcp_client abgelehnt: {bad!r}",
       any("mcp_client" in e for e in man_errs({"mcp_client": bad})), man_errs({"mcp_client": bad}))
ok("nur ein woertliches true verlangt den Schluessel",
   [m.mcp_client_wanted(x) for x in ({"mcp_client": {"use": True}}, {"mcp_client": {"use": "yes"}},
                                      {"mcp_client": {}}, {})] == [True, False, False, False])
ok("Schluessel und Adresse sind Plattformvariablen, der Schluessel folgt der Instanz",
   {"OAAP_MCP_CLIENT_KEY", "OAAP_MCP_CLIENT_URL"} <= m.RESERVED_ENV
   and "OAAP_MCP_CLIENT_KEY" in m.INSTANCE_KEY_VARS)

tid = m.ensure_default_tenant()
OTHER = "t-other00000"
reg = m.load_registry()
reg["instances"]["gw"] = record("gw", tid, "mcp-gateway", 8120, sources=True, routes=GW_ROUTES)
reg["instances"]["kp"] = record("kp", tid, "kontext-probe", 8121, mcp="/mcp")
reg["instances"]["chat"] = record("chat", tid, "chat", 8130, client=True)
reg["instances"]["crm"] = record("crm", tid, "crm", 8122)
reg["instances"]["chat-probe"] = record("chat-probe", tid, "chat", 8131, client=True,
                                        rehearsal={"of": "chat", "expires": "2099-01-01T00:00:00Z"})
reg["instances"]["gw-probe"] = record("gw-probe", tid, "mcp-gateway", 8126, sources=True,
                                      routes=GW_ROUTES,
                                      rehearsal={"of": "gw", "expires": "2099-01-01T00:00:00Z"})
reg["instances"]["fremd-gw"] = record("fremd-gw", OTHER, "mcp-gateway", 8125, sources=True,
                                      routes=GW_ROUTES)
reg["instances"]["fremd-chat"] = record("fremd-chat", OTHER, "chat", 8132, client=True)
m.save_registry(reg)
for n in reg["instances"]:
    m.write_app_caddy(n, m.load_registry()["instances"][n])

g = gateways("chat")
ok("die Site des Chats traegt /platform/mcp/gateways mit genau gw",
   [x["name"] for x in g or []] == ["gw"], g)
ok("… jeder Eintrag nennt die Tuer auf der EIGENEN Site des Chats",
   (g or [{}])[0] == {"name": "gw", "app": "mcp-gateway", "version": "1.0.0",
                      "channel": "production",
                      "via": f"http://{m.GATEWAY_CONTAINER}:8130/platform/mcp/via/gw"}, g)
b = block(site("chat"), "handle /platform/mcp/gateways {") or []
ok("… die Liste fragt den Client-Schluessel DIESES Chats, keine Rolle",
   f"uri /verify?roles=&tenant={tid}&instance=oaap.mcp.client:chat" in b, b)
d = block(site("chat"), "handle /platform/mcp/via/gw/* {")
ok("die Tuer zu gw steht auf der Site des Chats", d is not None, site("chat")[:800])
d = d or []
ok("… fragt identity mit via=Scope DIESES Chats, Ziel gw, ohne Rolle (/mcp ist public)",
   f"uri /verify?roles=&tenant={tid}&instance=gw&via=oaap.mcp.client:chat" in d, d)
ok("… kopiert die sechs Kopfzeilen der Person", m._COPY_IDENTITY.strip() in d, d)
ok("… nimmt Schluessel und Act-Zusicherung weg",
   "request_header -Authorization" in d and "request_header -X-OAAP-Act-Assertion" in d, d)
ok("… setzt den Namen des Chats selbst (nie aus der Anfrage)",
   "request_header X-OAAP-MCP-Client chat" in d, d)
ok("… schreibt auf /mcp/ um und geht zum Container von gw",
   "uri replace /platform/mcp/via/gw/ /mcp/ 1" in d and "reverse_proxy oaap-app-gw:8000 {" in d, d)
ok("… forward_auth vor dem Wegnehmen (sonst saehe identity nichts)",
   d and d.index("forward_auth identity:8000 {") < d.index("request_header -Authorization"), d)
body = site("chat")
ok("die Bloecke stehen vor /platform/* und vor der App-Route",
   body.index("handle /platform/mcp/via/gw/* {") < body.index("handle /platform/* {")
   and body.index("handle /platform/mcp/gateways {") < body.index("\thandle {"))
ok("die Site mit Tuer gilt nicht als veraltet", not m._site_identity_stale(body))
for n in ("gw-probe", "fremd-gw", "kp", "chat"):
    ok(f"keine Tuer zu {n}", f"/platform/mcp/via/{n}/" not in body)
ok("eine Generalprobe des Chats traegt weder Liste noch Tuer",
   "/platform/mcp/via/" not in site("chat-probe") and "/platform/mcp/gateways" not in site("chat-probe"))
ok("eine App ohne mcp_client traegt nichts davon",
   "/platform/mcp/via/" not in site("crm") and "/platform/mcp/via/" not in site("gw"))
ok("der Chat des anderen Mandanten sieht nur sein Gateway",
   [x["name"] for x in gateways("fremd-chat") or []] == ["fremd-gw"], gateways("fremd-chat"))
dt = block(site("gw"), "handle /platform/mcp/to/kp {") or []
ok("die Tuer /platform/mcp/to/ nimmt auch die Act-Zusicherung weg",
   "request_header -X-OAAP-Act-Assertion" in dt, dt)
ok("nach dem Schreiben ist nichts hinter", m._cases_sites_behind() == [], m._cases_sites_behind())
reg = m.load_registry()
reg["instances"]["gw2"] = record("gw2", tid, "mcp-gateway", 8133, sources=True, routes=GW_ROUTES)
m.save_registry(reg)
ok("ein neues Gateway im Mandanten: die Site des Chats ist hinter",
   "chat" in m._cases_sites_behind(), m._cases_sites_behind())
m.refresh_cases_sites()
ok("… und traegt danach auch die Tuer zu gw2",
   block(site("chat"), "handle /platform/mcp/via/gw2/* {") is not None)
reg = m.load_registry()
del reg["instances"]["gw2"]
m.save_registry(reg)
m.refresh_cases_sites()
ok("das Gateway geht: seine Tuer geht mit", "/platform/mcp/via/gw2/" not in site("chat"))
env_keep = {"OAAP_MCP_CLIENT_KEY": "oaapk_x_y", "OAAP_MCP_CLIENT_URL": "u", "FOO": "1"}
ok("ein aufgehobenes instance.env nimmt Schluessel und Adresse nicht mit (Liste der Plattformvariablen)",
   {k for k in env_keep if k not in m.INSTANCE_KEY_VARS
    and k not in ("OAAP_CASES_URL", "OAAP_CARE_URL", "OAAP_MCP_SOURCES_URL", "OAAP_MCP_CLIENT_URL")}
   == {"FOO"})

# ================================================================ B. Identity
print("")
print("B. Identity: /verify?via= und /verify?act= mit Zusicherung")
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, SERVICES)
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    sys.exit(1 if fails else 0)
import act                                                    # noqa: E402

DATA = tempfile.mkdtemp(prefix="oaap-mcp-via-id-")
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(SERVICES, "identity"))
sys.modules.pop("app", None)
idm = importlib.reload(importlib.import_module("app"))
idm.USERS_FILE = os.path.join(DATA, "users.json")
idm.KEYS_FILE = os.path.join(DATA, "api-keys.json")
idm.THROTTLE_FILE = os.path.join(DATA, "login-throttle.json")
idm.AUDIT_LOG = os.path.join(DATA, "audit.jsonl")
idm.TENANTS_FILE = os.path.join(DATA, "tenants.json")
idm.ASSERTION_KEY_FILE = os.path.join(DATA, "assertion.key")
AKEY = bytes.fromhex("ab" * 32)
with open(idm.ASSERTION_KEY_FILE, "w", encoding="ascii") as f:
    f.write("ab" * 32)
with open(idm.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-vp": {"label": "vp"}, "t-sgl": {"label": "sgl"}}}, f)


def user(uid, name, roles, tenant, kind="human", active=True, groups=()):
    return {"id": uid, "username": name, "display_name": name.title(), "password_hash": "",
            "kind": kind, "roles": roles, "groups": list(groups), "tenant": tenant,
            "active": active, "session_epoch": 0, "email": f"{name}@vp.invalid",
            "email_verified": True}


USERS = [
    user("u-anna", "vp-anna", ["user"], "t-vp", groups=["service"]),
    user("u-gone", "vp-gone", ["user"], "t-vp", active=False),
    user("u-max", "sgl-max", ["user"], "t-sgl"),
    user("u-robo", "vp-robo", ["user"], "t-vp", kind="machine"),
    user("u-chef", "vp-chef", ["tenant_admin", "user", "admin"], "t-vp"),
    user("u-chat", "instance:chat", ["user"], "t-vp", "machine"),
    user("u-chat2", "instance:chat2", ["user"], "t-vp", "machine"),
    user("u-chats", "instance:chat-sgl", ["user"], "t-sgl", "machine"),
    user("u-gw", "instance:gw", ["user"], "t-vp", "machine"),
]
with open(idm.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(USERS, f)


def key_for(principal, scope, roles=("user",)):
    _rec, secret = idm.issue_key(idm.load_users(), principal, list(roles), scope,
                                 "test", 30, "root")
    return secret


CHAT_KEY = key_for("instance:chat", "oaap.mcp.client:chat")
CHAT2_KEY = key_for("instance:chat2", "oaap.mcp.client:chat2")
CHATS_KEY = key_for("instance:chat-sgl", "oaap.mcp.client:chat-sgl")
CHAT_FREE = key_for("instance:chat", "")
CHEF_FREE = key_for("vp-chef", "", roles=("user", "admin"))
GW_SOURCES = key_for("instance:gw", "oaap.mcp.sources:gw")
c = idm.app.test_client()


def asrt(sub="u-anna", usr="vp-anna", ten="t-vp", aud="chat", now=None, key=AKEY):
    return idm.assertion.mint(key, sub, usr, ten, aud, {}, now=now)


def via(assertion=None, key=CHAT_KEY, chat="chat", target="gw", tenant="t-vp", roles="",
        groups="", extra=None):
    h = {}
    if key:
        h["Authorization"] = "Bearer " + key
    if assertion:
        h["X-OAAP-Act-Assertion"] = assertion
    h.update(extra or {})
    q = f"/verify?roles={roles}&tenant={tenant}&instance={target}&via=oaap.mcp.client:{chat}"
    if groups:
        q += f"&groups={groups}"
    return c.get(q, headers=h)


def text(r):
    return r.get_data(as_text=True)


r = via(asrt())
ok("anna mit ihrer Zusicherung an den Chat und dem Schluessel des Chats: 204",
   r.status_code == 204, (r.status_code, text(r)))
ok("… die Kopfzeilen sind die von anna",
   r.headers.get("X-OAAP-User") == "vp-anna" and r.headers.get("X-OAAP-User-Id") == "u-anna"
   and r.headers.get("X-OAAP-Roles") == "user", dict(r.headers))
new = r.headers.get("X-OAAP-User-Assertion", "")
ok("… mit einer NEUEN Zusicherung, adressiert an das Gateway gw",
   bool(new) and idm.assertion.check(AKEY, new, "gw")[0] is not None, new[:40])
ok("… die nicht mehr fuer den Chat gilt",
   idm.assertion.check(AKEY, new, "chat")[0] is None)
gone = asrt(now=time.time() - 400)
forged = asrt(key=bytes.fromhex("cd" * 32))
for label, a in (("an eine andere Instanz (kp)", asrt(aud="kp")),
                 ("an das Gateway statt an den Chat", asrt(aud="gw")),
                 ("abgelaufen", gone), ("gefaelscht (fremder Schluessel)", forged),
                 ("eines anderen Mandanten", asrt(sub="u-max", usr="sgl-max", ten="t-sgl")),
                 ("kaputt", "a.b"), ):
    r = via(a)
    ok(f"Zusicherung {label}: 401, keine Kopfzeilen",
       r.status_code == 401 and not r.headers.get("X-OAAP-User"), (r.status_code, text(r)))
r = via(None)
ok("ohne Zusicherung: 401 -- nie der Chat selbst (kein Rueckfall)",
   r.status_code == 401 and not r.headers.get("X-OAAP-User") and "assertion" in text(r),
   (r.status_code, text(r)))
r = via(asrt(sub="u-gone", usr="vp-gone"))
ok("eine gesperrte Person: 403", r.status_code == 403, (r.status_code, text(r)))
r = via(asrt(sub="u-robo", usr="vp-robo"))
ok("ein Maschinen-Prinzipal mit (gefaelschter) Zusicherung: 403", r.status_code == 403,
   (r.status_code, text(r)))
r = via(asrt(sub="u-niemand", usr="x"))
ok("eine Kennung, die es nicht gibt: 403", r.status_code == 403, (r.status_code, text(r)))
r = via(asrt(), key=None)
ok("ohne Schluessel des Chats: 401", r.status_code == 401, (r.status_code, text(r)))
r = via(asrt(), key=CHAT_FREE)
ok("ein unbeschraenkter Schluessel DESSELBEN Prinzipals instance:chat: 403",
   r.status_code == 403, (r.status_code, text(r)))
r = via(asrt(), key=CHEF_FREE)
ok("ein unbeschraenkter Schluessel eines tenant_admin: 403", r.status_code == 403,
   (r.status_code, text(r)))
r = via(asrt(), key=CHAT2_KEY)
ok("der Schluessel eines ANDEREN Chats an dieser Tuer: 403", r.status_code == 403,
   (r.status_code, text(r)))
r = via(asrt(aud="chat2"), key=CHAT2_KEY, chat="chat2")
ok("… an seiner eigenen Tuer mit seiner eigenen Zusicherung: 204", r.status_code == 204,
   (r.status_code, text(r)))
r = via(asrt(), key=GW_SOURCES)
ok("der Quellen-Schluessel eines Gateways an der via-Tuer: 403", r.status_code == 403,
   (r.status_code, text(r)))
r = via(asrt(ten="t-sgl", aud="chat-sgl"), key=CHATS_KEY, chat="chat-sgl")
ok("ein Chat aus sgl an einem Gateway in vp: 403 (Mandanten verschieden)",
   r.status_code == 403 and "tenant" in text(r), (r.status_code, text(r)))
r = via(asrt(), extra={act.HEADER: "x.y.z"})
ok("ein Act-Token (Anbieter) an der via-Tuer: abgelehnt", r.status_code in (400, 401),
   (r.status_code, text(r)))
r = via(asrt(), roles="tenant_admin")
ok("verlangt die Route des Gateways eine Rolle, die anna fehlt: 403",
   r.status_code == 403 and "role" in text(r), (r.status_code, text(r)))
r = via(asrt(), groups="vorstand")
ok("Sichtbarkeitsgruppe des Gateways, in der anna nicht ist: 403", r.status_code == 403,
   (r.status_code, text(r)))
r = via(asrt(), groups="service")
ok("… in der sie ist: 204", r.status_code == 204, (r.status_code, text(r)))
r = c.get("/verify?roles=&tenant=t-vp&instance=gw&via=oaap.mcp.sources:chat",
          headers={"Authorization": "Bearer " + CHAT_KEY, "X-OAAP-Act-Assertion": asrt()})
ok("via mit einem Scope, der keine Client-Tuer ist: 403", r.status_code == 403,
   (r.status_code, text(r)))
r = c.get("/verify?roles=user&tenant=t-vp&instance=gw", headers={"X-OAAP-Act-Assertion": asrt()})
ok("eine Act-Zusicherung allein an einer gewoehnlichen Route oeffnet nichts",
   r.status_code in (303, 401) and not r.headers.get("X-OAAP-User"), (r.status_code, dict(r.headers)))


# ---- Verfahren I mit der eigenen Zusicherung (aud = Gateway)
def act_door(assertion=None, token=None, key=GW_SOURCES, gw="gw", target="kp", tenant="t-vp",
             roles="user"):
    h = {"Authorization": "Bearer " + key} if key else {}
    if assertion:
        h["X-OAAP-Act-Assertion"] = assertion
    if token:
        h[act.HEADER] = token
    q = f"/verify?roles={roles}&tenant={tenant}&instance={target}&act=oaap.mcp.sources:{gw}"
    return c.get(q, headers=h)


r = act_door(new)
ok("Verfahren I mit der Zusicherung an gw (aus der via-Tuer): 204, anna, an die Quelle kp",
   r.status_code == 204 and r.headers.get("X-OAAP-User") == "vp-anna"
   and idm.assertion.check(AKEY, r.headers.get("X-OAAP-User-Assertion", ""), "kp")[0] is not None,
   (r.status_code, text(r)))
r = act_door(asrt())
ok("… die Zusicherung an den CHAT gilt dort nicht: 401", r.status_code == 401,
   (r.status_code, text(r)))
r = act_door(new, token="x.y.z")
ok("… Token und Zusicherung zugleich: abgelehnt", r.status_code in (400, 401),
   (r.status_code, text(r)))
r = act_door(new, roles="tenant_admin")
ok("… die Route der Quelle gilt fuer die Person (Rolle fehlt: 403)", r.status_code == 403,
   (r.status_code, text(r)))
r = act_door(new, key=CHAT_KEY)
ok("… mit dem Schluessel des Chats statt dem des Gateways: 403", r.status_code == 403,
   (r.status_code, text(r)))
r = act_door(None)
ok("… ohne beides: 401 wie bisher", r.status_code == 401, (r.status_code, text(r)))

with open(idm.AUDIT_LOG, encoding="utf-8") as f:
    log = [json.loads(ln) for ln in f]
vias = [e for e in log if e["action"] == "mcp.via"]
acts = [e for e in log if e["action"] == "mcp.act"]
ok("jeder via-Akt steht im Protokoll: Gateway, Chat, Person, Verfahren A",
   any(e["result"] == "ok" and e["subject"] == "gw" and e["who"] == "instance:chat"
       and "vp-anna" in e.get("detail", "") and "(A)" in e.get("detail", "") for e in vias), vias[:2])
ok("… auch die Ablehnungen", any(e["result"] == "denied" and "assertion" in e.get("detail", "")
                                 for e in vias))
ok("Verfahren I mit Zusicherung steht als mcp.act mit (A)",
   any(e["result"] == "ok" and e["subject"] == "kp" and "(A)" in e.get("detail", "") for e in acts),
   acts[:2])
raw = open(idm.AUDIT_LOG, encoding="utf-8").read()
ok("… und keine Zusicherung, kein Schluessel steht darin",
   new.split(".")[1] not in raw and CHAT_KEY not in raw)

print("")
print("FEHLER: %d" % fails if fails else "Alles gruen.")
sys.exit(1 if fails else 0)
