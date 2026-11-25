"""RFC-0104 Stufe 2: der Chat am KI-Gateway ohne Schluessel, die Einrichtungstuer.

    python3 test/test_ai_via_setup.py

Was hier gehalten wird:

A. Manifest 0.18 und Generator (appctl):
   - `setup.path` als Tuer unter einer Route, `ai_client.use`/`ai_gateway.serve`
     nur als woertliches true;
   - die Site eines Chats mit `ai_client` traegt /platform/ai/gateways und je
     KI-Gateway seines Mandanten eine Tuer /platform/ai/via/<gw>/ -> /v1/;
     keine fuer ein Gateway eines anderen Mandanten (Koeder 8), keine fuer
     eine Generalprobe; die Tuer nimmt Schluessel und Act-Zusicherung weg und
     setzt X-OAAP-AI-Client selbst;
   - ein KI-Gateway, das kommt, macht die Site des Chats veraltet;
   - der interne Listener traegt /s/<instanz> nur fuer eine App mit `setup`.
B. Identity: /verify?via=oaap.ai.client:<chat> braucht beide Nachweise
   (Koeder 4) und protokolliert ai.via.
C. Der Aufbau: `app.setup` und die Lesung `setup.ready`; der Aufruf traegt
   den Einrichtungsschluessel nur auf stdin, nie in der Befehlszeile.
"""
import contextlib
import importlib
import io as _io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


print("A. Manifest 0.18 und Generator")
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-ai-via-test-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import appctl as m                                            # noqa: E402
import tenant_build as tb                                     # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
base = {"oaap_manifest": "0.18",
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


ok("die Manifest-Version kennt 0.18", m.MANIFEST_MINOR >= 18)
good = {"setup": {"path": "/oaap/setup"}, "ai_client": {"use": True},
        "ai_gateway": {"serve": True}}
ok("setup, ai_client, ai_gateway gehen durch", not man_errs(good), man_errs(good))
for fld, bad in (("setup", {"path": "/"}), ("setup", {"path": "/x", "more": 1}),
                 ("ai_client", {"use": "yes"}), ("ai_client", {"use": True, "x": 1}),
                 ("ai_gateway", {"serve": 1}), ("ai_gateway", ["serve"])):
    ok(f"{fld} abgelehnt: {bad!r}", any(fld in e for e in man_errs({fld: bad})),
       man_errs({fld: bad}))
ok("nur woertliches true", [m.ai_client_wanted({"ai_client": {"use": True}}),
                            m.ai_client_wanted({"ai_client": {"use": "yes"}}),
                            m.ai_gateway_wanted({"ai_gateway": {"serve": True}}),
                            m.ai_gateway_wanted({})] == [True, False, True, False])
ok("Schluessel, Adresse und Einrichtungsschluessel sind Plattformvariablen",
   {"OAAP_AI_CLIENT_KEY", "OAAP_AI_CLIENT_URL", "OAAP_SETUP_KEY"} <= m.RESERVED_ENV
   and "OAAP_AI_CLIENT_KEY" in m.INSTANCE_KEY_VARS)

GW_ROUTES = [{"path": "/v1", "roles": ["public"]}, {"path": "/", "roles": ["admin"]}]


def record(name, tenant, app_id, port, **extra):
    c = f"oaap-app-{name}"
    r = {"app_id": app_id, "app_name": app_id, "version": "1.0.0",
         "channel": "production", "port": port, "container": c, "svc_port": 8000,
         "image": f"oaap-app/{app_id}:1.0.0", "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 8000}],
         "routes": [{"path": "/", "roles": ["user"]}], "visibility": {},
         "mcp": {}, "mcp_sources": False, "mcp_client": False}
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


tid = m.ensure_default_tenant()
OTHER = "t-other00000"
reg = m.load_registry()
reg["instances"]["kit1-modelle"] = record("kit1-modelle", tid, "ai-gateway", 8140,
                                          ai_gateway=True, routes=GW_ROUTES,
                                          setup={"path": "/oaap/setup"})
reg["instances"]["kit1-chat"] = record("kit1-chat", tid, "chat", 8141, ai_client=True)
reg["instances"]["kit1-hilfe"] = record("kit1-hilfe", tid, "help", 8142)
reg["instances"]["fremd-modelle"] = record("fremd-modelle", OTHER, "ai-gateway", 8143,
                                           ai_gateway=True, routes=GW_ROUTES)
reg["instances"]["kit1-chat-probe"] = record(
    "kit1-chat-probe", tid, "chat", 8144, ai_client=True,
    rehearsal={"of": "kit1-chat", "expires": "2099-01-01T00:00:00Z"})
m.save_registry(reg)
for n in reg["instances"]:
    m.write_app_caddy(n, m.load_registry()["instances"][n])

b = block(site("kit1-chat"), "handle /platform/ai/gateways {") or []
resp = [x for x in b if x.startswith("respond `")]
gws = json.loads(resp[0][len("respond `"):resp[0].rindex("`")])["gateways"] if resp else None
ok("die Site des Chats traegt /platform/ai/gateways mit genau kit1-modelle",
   [g["name"] for g in gws or []] == ["kit1-modelle"], gws)
ok("... mit der Tuer auf der EIGENEN Site des Chats",
   (gws or [{}])[0].get("via") == f"http://{m.GATEWAY_CONTAINER}:8141/platform/ai/via/kit1-modelle", gws)
ok("... die Liste fragt den Client-Schluessel DIESES Chats",
   f"uri /verify?roles=&tenant={tid}&instance=oaap.ai.client:kit1-chat" in b, b)
d = block(site("kit1-chat"), "handle /platform/ai/via/kit1-modelle/* {") or []
ok("die Tuer fragt identity mit via=oaap.ai.client:kit1-chat, Ziel kit1-modelle",
   f"uri /verify?roles=&tenant={tid}&instance=kit1-modelle&via=oaap.ai.client:kit1-chat" in d, d)
ok("... nimmt Schluessel und Act-Zusicherung weg, setzt X-OAAP-AI-Client selbst",
   "request_header -Authorization" in d and "request_header -X-OAAP-Act-Assertion" in d
   and "request_header X-OAAP-AI-Client kit1-chat" in d, d)
ok("... schreibt auf /v1/ um und geht zum Container des KI-Gateways",
   "uri replace /platform/ai/via/kit1-modelle/ /v1/ 1" in d
   and "reverse_proxy oaap-app-kit1-modelle:8000 {" in d, d)
ok("... forward_auth vor dem Wegnehmen",
   d and d.index("forward_auth identity:8000 {") < d.index("request_header -Authorization"), d)
body = site("kit1-chat")
ok("KOEDER 8: keine Tuer zum KI-Gateway eines anderen Mandanten",
   "/platform/ai/via/fremd-modelle/" not in body)
ok("eine Generalprobe des Chats traegt weder Liste noch Tuer",
   "/platform/ai/" not in site("kit1-chat-probe"))
ok("eine App ohne ai_client traegt nichts davon", "/platform/ai/" not in site("kit1-hilfe"))
ok("die MCP-Tuer bleibt, wo sie war: keine auf dieser Site", "/platform/mcp/via/" not in body)
ok("nach dem Schreiben ist nichts hinter", m._cases_sites_behind() == [], m._cases_sites_behind())
reg = m.load_registry()
reg["instances"]["kit1-modelle2"] = record("kit1-modelle2", tid, "ai-gateway", 8145,
                                           ai_gateway=True, routes=GW_ROUTES)
m.save_registry(reg)
ok("ein neues KI-Gateway im Mandanten: die Site des Chats ist hinter",
   "kit1-chat" in m._cases_sites_behind(), m._cases_sites_behind())
m.refresh_cases_sites()
ok("... und traegt danach auch dessen Tuer",
   block(site("kit1-chat"), "handle /platform/ai/via/kit1-modelle2/* {") is not None)

m.docker_subnets = lambda net: []
m.write_internal_health_caddy()
probe = None
for fn in os.listdir(m.CADDY_APPS_DIR):
    with open(os.path.join(m.CADDY_APPS_DIR, fn), encoding="utf-8") as f:
        t = f.read()
    if f":{m.HEALTH_PROBE_PORT} {{" in t:
        probe = t
ok("der interne Listener traegt /s/kit1-modelle -> /oaap/setup mit Query",
   probe is not None and "handle /s/kit1-modelle {" in probe
   and "rewrite * /oaap/setup?{query}" in probe, (probe or "")[:600])
ok("... und keine Einrichtungstuer fuer eine App ohne setup",
   probe is not None and "handle /s/kit1-chat {" not in probe)

print("")
print("B. Identity: /verify?via=oaap.ai.client:")
SERVICES = os.path.join(HERE, "..", "platform", "services")
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    flask = None
if flask:
    DATA = tempfile.mkdtemp(prefix="oaap-ai-via-id-")
    os.environ["SESSION_SECRET"] = "test-session-secret"
    os.environ["SETUP_TOKEN"] = "test-setup-token"
    os.environ["INTERNAL_API_KEY"] = "test-internal-key"
    os.environ["OAAP_IDENTITY_DATA_DIR"] = DATA
    sys.path.insert(0, os.path.join(SERVICES, "identity"))
    saved_app = sys.modules.pop("app", None)
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
        json.dump({"tenants": {"t-kit1": {"label": "kit1"}}}, f)

    def user(uid, name, roles, kind="human"):
        return {"id": uid, "username": name, "display_name": name, "password_hash": "",
                "kind": kind, "roles": roles, "groups": [], "tenant": "t-kit1",
                "active": True, "session_epoch": 0, "email": f"{uid}@x.invalid",
                "email_verified": True}

    with open(idm.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump([user("u-anna", "kit1-anna", ["user"]),
                   user("u-chat", "instance:kit1-chat", ["user"], "machine")], f)

    def key_for(principal, scope):
        return idm.issue_key(idm.load_users(), principal, ["user"], scope, "t", 30, "root")[1]

    AI_KEY = key_for("instance:kit1-chat", "oaap.ai.client:kit1-chat")
    MCP_KEY = key_for("instance:kit1-chat", "oaap.mcp.client:kit1-chat")
    c = idm.app.test_client()

    def asrt(aud="kit1-chat"):
        return idm.assertion.mint(AKEY, "u-anna", "kit1-anna", "t-kit1", aud, {})

    def via(assertion, key, scope="oaap.ai.client:kit1-chat"):
        h = {}
        if key:
            h["Authorization"] = "Bearer " + key
        if assertion:
            h["X-OAAP-Act-Assertion"] = assertion
        return c.get(f"/verify?roles=&tenant=t-kit1&instance=kit1-modelle&via={scope}",
                     headers=h)

    r = via(asrt(), AI_KEY)
    ok("anna mit Zusicherung an den Chat und dem KI-Client-Schluessel: 204, ihre Kopfzeilen",
       r.status_code == 204 and r.headers.get("X-OAAP-User-Id") == "u-anna",
       (r.status_code, r.get_data(as_text=True)))
    new = r.headers.get("X-OAAP-User-Assertion", "")
    ok("... mit einer neuen Zusicherung an das KI-Gateway",
       bool(new) and idm.assertion.check(AKEY, new, "kit1-modelle")[0] is not None)
    r = via(None, AI_KEY)
    ok("KOEDER 4: der Schluessel ohne Zusicherung: abgelehnt", r.status_code == 401,
       r.status_code)
    r = via(asrt(), None)
    ok("KOEDER 4: die Zusicherung ohne Schluessel: abgelehnt", r.status_code in (401, 403),
       r.status_code)
    r = via(asrt(), MCP_KEY)
    ok("der MCP-Schluessel oeffnet die KI-Tuer nicht", r.status_code in (401, 403),
       r.status_code)
    with open(idm.AUDIT_LOG, encoding="utf-8") as f:
        log = f.read()
    ok("das Protokoll nennt ai.via", "ai.via" in log, log[-300:])
    sys.modules.pop("app", None)
    if saved_app is not None:
        sys.modules["app"] = saved_app

print("")
print("C. Der Aufbau: app.setup und setup.ready")
CALLS = []


def fake_run(cmd, input=None, capture_output=True, text=True, timeout=None):
    q = json.loads(input)
    CALLS.append((cmd, q))

    class R:
        pass
    r = R()
    body = q["body"] or {}
    if q["method"] == "GET":
        doc = {"v": 1, "kind": "supplier", "id": "anthropic", "state": "present",
               "ready": STATE["ready"], "note": "Zugangsdaten fehlen" if not STATE["ready"] else ""}
    elif "remove" in body:
        doc = {"v": 1, "results": [dict(x, result="removed", note="") for x in body["remove"]]}
    else:
        res = []
        for it in body["items"]:
            have = STATE["have"].get((it["kind"], it["id"]))
            if have is None:
                result = "would_make" if body.get("dry") else "made"
                if not body.get("dry"):
                    STATE["have"][(it["kind"], it["id"])] = it
            elif have == it:
                result = "present"
            else:
                result = "conflict"
            res.append({"kind": it["kind"], "id": it["id"], "result": result, "note": ""})
        doc = {"v": 1, "results": res}
    r.stdout = json.dumps({"status": 200, "body": json.dumps(doc)})
    r.returncode = 0
    return r


STATE = {"have": {}, "ready": False}
m.subprocess.run = fake_run
m.load_env = lambda name, inst=None: {"OAAP_SETUP_KEY": "setup-geheim-1"}
d = m._BuildDrivers(m.load_registry()["instances"]["kit1-modelle"]["tenant"], who="anna")
d._instance_key = lambda n: n if n in m.load_registry()["instances"] else ""
step = {"name": "kit1-modelle", "items": [
    {"kind": "alias", "id": "standard", "supplier": "anthropic", "model": "claude-sonnet-5-5"},
    {"kind": "client", "id": "kit1-chat", "aliases": ["standard"], "ceiling": "yellow",
     "personal_data": False}]}
v, note = d.check("app.setup", step, {})
ok("vorher: noch einzurichten (dry)", v == tb.ABSENT and "would_make" in note, (v, note))
ok("... die Pruefung aendert nichts", STATE["have"] == {})
okd, note, made = d.do("app.setup", step, {})
ok("legt an; der Aufbau merkt sich, was er machte",
   okd and made == ["setup:kit1-modelle:alias:standard", "setup:kit1-modelle:client:kit1-chat"],
   (okd, note, made))
v, note = d.check("app.setup", step, {})
ok("danach: vorhanden", v == tb.PRESENT, (v, note))
cmd, q = CALLS[-1]
ok("der Aufruf geht ueber den internen Listener /s/<instanz>",
   q["url"] == f"http://{m.GATEWAY_CONTAINER}:{m.HEALTH_PROBE_PORT}/s/kit1-modelle", q["url"])
ok("... aus dem Identity-Container, der Schluessel nur auf stdin",
   cmd[:4] == ["docker", "exec", "-i", m.IDENTITY_CONTAINER]
   and "setup-geheim-1" not in " ".join(cmd) and q["key"] == "setup-geheim-1", cmd)
ok("... mit der Person des Aufbaus", q["actor"] == "anna", q)
STATE["have"][("alias", "standard")] = dict(step["items"][0], model="anderes")
v, note = d.check("app.setup", step, {})
ok("eine Abweichung ist ein Konflikt (die App ueberschreibt nichts)",
   v == tb.CONFLICT and "conflict" in note, (v, note))
okd, note = d.undo("app.setup", step, ["setup:kit1-modelle:client:kit1-chat"], {})
ok("der Rueckbau schickt remove fuer genau dieses Element",
   okd and CALLS[-1][1]["body"] == {"v": 1, "remove": [{"kind": "client", "id": "kit1-chat"}]},
   (okd, note, CALLS[-1][1]["body"]))
v, note = d.check("app.setup", {"name": "kit1-chat", "items": step["items"]}, {})
ok("eine Instanz ohne Einrichtungstuer: Konflikt mit Satz",
   v == tb.CONFLICT and "Einrichtungst" in note, (v, note))
FLAKY = {"n": 2}
real_fake = m.subprocess.run


def starting_run(cmd, input=None, capture_output=True, text=True, timeout=None):
    if FLAKY["n"] > 0:
        FLAKY["n"] -= 1

        class R:
            stdout = json.dumps({"status": 502, "body": ""})
            returncode = 0
        return R()
    return real_fake(cmd, input=input, capture_output=capture_output, text=text, timeout=timeout)


m.subprocess.run = starting_run
SLEPT = []
m.time.sleep = lambda s: SLEPT.append(s)
v, note = d.check("app.setup", step, {})
ok("ein startender Container (502) wird abgewartet, nicht als Fehler gemeldet",
   FLAKY["n"] == 0 and len(SLEPT) == 2 and v == tb.CONFLICT, (v, note, SLEPT))
m.subprocess.run = real_fake
wait = {"text": "Zugangsdaten eintragen", "done_when": "setup.ready",
        "instance": "kit1-modelle", "kind": "supplier", "item": "anthropic"}
v, note = d.check("manual", wait, {})
ok("setup.ready wartet, solange die App nicht bereit sagt", v == tb.ABSENT and "nicht bereit" in note,
   (v, note))
ok("... und fragt ?kind=supplier&id=anthropic",
   CALLS[-1][1]["url"].endswith("/s/kit1-modelle?kind=supplier&id=anthropic"), CALLS[-1][1]["url"])
STATE["ready"] = True
v, note = d.check("manual", wait, {})
ok("danach: erledigt", v == tb.PRESENT, (v, note))

prof = {"profile": tb.PROFILE_FORMAT, "id": "probe", "title": "t",
        "params": {"label": {"kind": "label", "required": True},
                   "model": {"kind": "text", "label": "Modell"}},
        "steps": [{"id": "t", "type": "tenant.create", "label": "{label}"},
                  {"id": "s", "type": "app.setup", "name": "{label}-modelle",
                   "items": [{"kind": "alias", "id": "standard", "supplier": "anthropic",
                              "model": "{model}"},
                             {"kind": "client", "id": "{label}-chat", "aliases": ["standard"],
                              "personal_data": False}]},
                  {"id": "w", "type": "manual", "text": "t", "done_when": "setup.ready",
                   "instance": "{label}-modelle", "kind": "supplier", "item": "anthropic"}]}
ok("ein Profil mit app.setup und setup.ready ist gueltig", tb.profile_problems(prof) == [],
   tb.profile_problems(prof))
r = tb.render_step(prof["steps"][1], {"label": "kit1", "model": "claude-sonnet-5-5"})
ok("die Parameter fuellen die Elemente, auch in Listen",
   r["items"][0]["model"] == "claude-sonnet-5-5" and r["items"][1]["id"] == "kit1-chat"
   and r["items"][1]["personal_data"] is False, r)


def problems(items):
    return tb.profile_problems(dict(prof, steps=[prof["steps"][0], {
        "id": "s", "type": "app.setup", "name": "x", "items": items}]))


ok("KOEDER 1: ein Geheimnis in einem Element wird abgelehnt",
   any("never holds a secret" in p for p in problems([{"kind": "supplier", "id": "a",
                                                        "credentials": "sk-1"}])))
ok("ein Element ohne kind/id wird abgelehnt",
   any("needs a text 'kind'" in p for p in problems([{"id": "a"}])))
ok("ein unbekannter Parameter wird abgelehnt",
   any("not a declared parameter" in p for p in problems([{"kind": "a", "id": "{nix}"}])))
ok("eine leere Liste wird abgelehnt", any("items is a list" in p for p in problems([])))
# RFC-0133: die feste Werkzeugliste einer Sicht -- eine Liste flacher Objekte aus Text
TL = [{"kind": "view", "id": "bdt", "tools": [{"server": "{label}-bdt", "tool": "bdt_kanaele", "effect": "read"},
                                              {"server": "{label}-bdt", "tool": "bdt_aufrufen", "effect": "write"}]}]
ok("RFC-0133: eine Liste flacher Objekte aus Text ist gültig", problems(TL) == [], problems(TL))
rt = tb.render_items(TL, {"label": "kt"}, "s")
ok("… und die Parameter füllen auch die Objekte",
   rt[0]["tools"] == [{"server": "kt-bdt", "tool": "bdt_kanaele", "effect": "read"},
                      {"server": "kt-bdt", "tool": "bdt_aufrufen", "effect": "write"}], rt)
ok("KÖDER: verschachteltes Objekt in der Liste wird abgelehnt",
   any("objects of text" in p for p in problems([{"kind": "view", "id": "v", "tools": [{"a": {"b": "c"}}]}])))
ok("KÖDER: ein Geheimnis als Feld eines Listenobjekts wird abgelehnt",
   any("objects of text" in p for p in problems([{"kind": "view", "id": "v", "tools": [{"token": "x"}]}])))
ok("KÖDER: Text und Objekt gemischt wird abgelehnt",
   any("list of text" in p for p in problems([{"kind": "view", "id": "v", "tools": ["a", {"b": "c"}]}])))
ok("KÖDER: unbekannter Parameter in einem Listenobjekt fällt auf",
   any("not a declared parameter" in p for p in problems([{"kind": "view", "id": "v", "tools": [{"s": "{gibtsnicht}"}]}])))

print("")
print("FEHLER: %d" % fails if fails else "Alles gruen.")
sys.exit(1 if fails else 0)
