#!/usr/bin/env python3
"""Anmeldung über den Realm und „Für den Chat freigeben“ (oaap.core.portal 2.20,
manifest 0.23 `mcp_store`, Profilschritt `idp.app`, reference 0.1.375).

    python3 test/test_chat_store_signin.py

Gehalten wird:

A. Manifest 0.23: `mcp_store: {kind}` nur neben einer MCP-Tür, nur `kind`, in
   der Form der Ablagetypen; das Register merkt die Art, die Quellenliste
   nennt sie als `store_kind` -- und sonst nichts Neues.
B. Profil: `when` als Liste verlangt ALLE Haken; der Schritt `idp.app` ist
   bekannt und hat nur `name`; das Profil ki-arbeitsplatz ist gültig und baut
   das Wiki nur mit Realm.
C. chat_store_apply: dieselbe Einrichtungstür wie ein Aufbau; Ablagetür für
   alle Zugangsprofile, nie für einen Schlüssel; eine von Hand gesetzte Ampel
   bleibt; ein Konflikt hält an, ohne etwas zu ändern; ohne Profil kein Lauf.
D. Der Worker urteilt: tenant_admin des Mandanten ja, eines anderen nein,
   ohne Rolle nein -- und die Funktion läuft dann gar nicht.
E. Die Karten: Knopf nur für die Verwaltung der Instanz und nur, wo es geht;
   nie ein Geheimnis.
"""
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-chat-store-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, HERE)

import appctl as m                                            # noqa: E402
import tenant_build as tb                                     # noqa: E402
import instance_view as iv                                    # noqa: E402
import build_view                                             # noqa: E402
import template_src                                           # noqa: E402

try:
    from jinja2 import Environment
except ImportError:                                           # pragma: no cover
    print("jinja2 fehlt -- pip install jinja2")
    sys.exit(1)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


print("=== A. Manifest 0.23 mcp_store ===")
ok("die Plattform kennt Manifest 0.23", m.MANIFEST_MINOR >= 23)
base = {"mcp": {"path": "/oaap/mcp"}}
ok("gültig: kind neben einer Tür", m.mcp_store_error(dict(base, mcp_store={"kind": "bookstack"})) == ""
   and m.mcp_store_kind(dict(base, mcp_store={"kind": "bookstack"})) == "bookstack")
ok("eine neue Art braucht kein Plattform-Update (nur die Form zählt)",
   m.mcp_store_error(dict(base, mcp_store={"kind": "nextcloud"})) == "")
for bad, word in (({"mcp_store": {"kind": "bookstack"}}, "MCP door"),
                  (dict(base, mcp_store={"kind": "BookStack"}), "lower case"),
                  (dict(base, mcp_store={"kind": "b"}), "lower case"),
                  (dict(base, mcp_store={"kind": "bookstack", "light": "green"}), "only 'kind'"),
                  (dict(base, mcp_store="bookstack"), "object expected")):
    e = m.mcp_store_error(bad)
    ok(f"KÖDER {json.dumps(bad.get('mcp_store'))}: ein Satz ({word})", word in e, e)
ok("ohne Tür ist die Art leer, auch aus einem Register",
   m.mcp_store_kind({"mcp_store": "bookstack", "mcp": {}}) == ""
   and m.mcp_store_kind({"mcp_store": "bookstack", "mcp": {"path": "/oaap/mcp"}}) == "bookstack")

insts = {"gw": {"tenant": "t1", "port": 8120, "mcp_sources": True, "mcp": {}},
         "wiki": {"tenant": "t1", "port": 8121, "mcp": {"path": "/oaap/mcp"}, "mcp_store": "bookstack"},
         "kp": {"tenant": "t1", "port": 8122, "mcp": {"path": "/mcp"}}}
srcs = {s["name"]: s for s in m.mcp_sources_of(insts, "gw")}
ok("die Quellenliste nennt store_kind nur für die Ablage",
   srcs["wiki"].get("store_kind") == "bookstack" and "store_kind" not in srcs["kp"], srcs)

print("=== B. Profil: when als Liste, idp.app ===")
ok("idp.app: nur name", tb.STEP_TYPES["idp.app"] == (("name",), ()))
P = {"profile": tb.PROFILE_FORMAT, "id": "t", "version": "1.0.0", "title": "T",
     "params": {"label": {"kind": "label", "required": True, "label": "K"},
                "a": {"kind": "bool", "default": False, "label": "A"},
                "b": {"kind": "bool", "default": False, "label": "B"},
                "t": {"kind": "text", "default": "x", "label": "T"}},
     "steps": [{"id": "tenant", "type": "tenant.create", "label": "{label}"},
               {"id": "x", "type": "idp.app", "name": "wiki", "when": ["a", "b"]}]}
ok("when als Liste zweier Haken: gültig", tb.profile_problems(P) == [], tb.profile_problems(P))
for w, word in ((["a", "t"], "kind bool"), ([], "kind bool"), (["a", "a"], "kind bool"),
                (["a", "b", "a", "b", "a"], "up to 4")):
    bad = json.loads(json.dumps(P))
    bad["steps"][1]["when"] = w
    ok(f"KÖDER when {w}: abgelehnt", any(word in x for x in tb.profile_problems(bad)),
       tb.profile_problems(bad))
bad = json.loads(json.dumps(P))
bad["steps"][1]["connector"] = "auth"
ok("KÖDER idp.app mit connector: abgelehnt (nichts zu wählen)",
   any("connector" in x for x in tb.profile_problems(bad)), tb.profile_problems(bad))
st = P["steps"][1]
ok("step_active: nur wenn ALLE Haken gesetzt sind",
   tb.step_active(st, {"a": "true", "b": "true"}) and not tb.step_active(st, {"a": "true", "b": "false"})
   and not tb.step_active(st, {"a": "true"}) and tb.step_active({"id": "y"}, {}))
ok("die Planansicht nennt beide Haken", build_view.plan(P)[1]["when"] == "A und B",
   build_view.plan(P))

prof_path = os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "profile", "ki-arbeitsplatz.json")
if os.path.exists(prof_path):
    prof = json.load(open(prof_path, encoding="utf-8"))
    ok("ki-arbeitsplatz ist gültig", tb.profile_problems(prof) == [], tb.profile_problems(prof))
    wiki_steps = [s for s in prof["steps"] if "with_wiki" in tb.when_names(s)]
    ok("jeder Wiki-Schritt verlangt auch den Realm",
       wiki_steps and all("with_realm" in tb.when_names(s) for s in wiki_steps), wiki_steps)
    types_ = [s["type"] for s in wiki_steps]
    ok("… Installation, Erzeugen, Realm-Client, Freigabe im Gateway",
       "app.install" in types_ and "idp.app" in types_ and "app.setup" in types_
       and "app.config" in types_, types_)
    vals = {"label": "kit2", "with_realm": "false", "with_wiki": "true"}
    ok("Haken Wiki ohne Realm: kein Wiki-Schritt läuft",
       not any(tb.step_active(s, vals) for s in wiki_steps))
    # RFC-0133 (Profil 3.0): vier Haken, jedes Zugangsprofil des Chats mit Ablagetür
    proj = [s for s in prof["steps"] if "with_projects" in tb.when_names(s)]
    ok("RFC-0133: jeder Projekte-Schritt verlangt auch den Realm, BDT und Recherche nicht",
       proj and all("with_realm" in tb.when_names(s) for s in proj)
       and not any("with_realm" in tb.when_names(s) for s in prof["steps"]
                   if {"with_bdt", "with_research"} & set(tb.when_names(s))), proj)
    profiles_ = [i for s in prof["steps"] if s["type"] == "app.setup" for i in s["items"]
                 if i["kind"] == "profile"]
    ok("RFC-0133: jedes Zugangsprofil des Chats bringt die Ablagetür mit (stores: true)",
       len(profiles_) == 3 and all(i.get("stores") is True for i in profiles_), profiles_)
    ok("RFC-0133: alle vier Haken sind aus", all(prof["params"][n]["default"] is False for n in
                                                  ("with_wiki", "with_projects", "with_bdt", "with_research")))
    full = tb.param_values(prof, {"label": "kit3", "title": "KI", "with_realm": "true", "with_wiki": "true",
                                  "with_projects": "true", "with_bdt": "true", "with_research": "true"})
    full = full[0] if isinstance(full, tuple) else full
    ran = [sid for sid, _t, _a in tb.plan(prof, full)]
    bdt_set = [a for sid, _t, a in tb.plan(prof, full) if sid == "bdt-einrichten"][0]
    view = [i for i in bdt_set["items"] if i["kind"] == "view"][0]
    ok("RFC-0133: mit allen Haken laufen Projekte, BDT und Recherche; die BDT-Sicht hat genau vier Werkzeuge",
       {"projekte", "projekte-ablage", "bdt", "bdt-einrichten", "recherche", "recherche-einrichten"} <= set(ran)
       and [x["tool"] for x in view["tools"]] == ["bdt_kanaele", "bdt_werkzeuge", "bdt_ergebnis", "bdt_aufrufen"]
       and view["tools"][0]["server"] == "kit3-bdt-team", (ran, view))
    none_ = tb.param_values(prof, {"label": "kit4", "title": "KI"})
    none_ = none_[0] if isinstance(none_, tuple) else none_
    ran0 = {sid for sid, _t, _a in tb.plan(prof, none_)}
    ok("RFC-0133: ohne Haken läuft keiner der neuen Schritte",
       not ran0 & {"projekte", "bdt", "recherche", "bdt-einrichten", "recherche-einrichten"}, ran0)
else:
    print("SKIP  ki-arbeitsplatz.json nicht neben oaap-reference (Container ohne oaap-docs)")

print("=== C. chat_store_apply ===")
m.refresh_destinations = lambda *x, **k: None
m.state_view_write = lambda *x, **k: None
m.egress_view_write = lambda *x, **k: None
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
VU, _ = m.tenant_create("vuebung")
OTHER, _ = m.tenant_create("anderer")
d = os.path.join(DATA, "data", "identity")
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": "root", "roles": ["server_admin"], "tenant": "", "groups": [], "active": True},
               {"username": "anna", "roles": ["tenant_admin"], "tenant": VU, "groups": [], "active": True},
               {"username": "bert", "roles": ["tenant_admin"], "tenant": OTHER, "groups": [], "active": True},
               {"username": "niemand", "roles": ["user"], "tenant": VU, "groups": [], "active": True}], f)

reg = m.load_registry()
reg["instances"]["vuebung-werkzeuge"] = {
    "tenant": VU, "name": "werkzeuge", "image": "img:1", "port": 8120, "routes": [],
    "mcp_sources": True, "mcp": {}, "setup": {"path": "/oaap/setup"},
    "services": [{"service": "", "container": "oaap-app-vuebung-werkzeuge", "image": "img:1", "port": 8000}]}
reg["instances"]["vuebung-wiki"] = {
    "tenant": VU, "name": "wiki", "image": "img:1", "port": 8121, "routes": [],
    "mcp": {"path": "/oaap/mcp"}, "mcp_store": "bookstack",
    "oidc_client": {"redirect_paths": ["/oidc/callback"], "groups_claim": True},
    "services": [{"service": "wiki", "container": "oaap-app-vuebung-wiki-wiki", "image": "img:1", "port": 80}]}
reg["instances"]["vuebung-kp"] = {
    "tenant": VU, "name": "kp", "image": "img:1", "port": 8122, "routes": [], "mcp": {"path": "/mcp"},
    "services": [{"service": "", "container": "oaap-app-vuebung-kp", "image": "img:1", "port": 8000}]}
m.save_registry(reg)

CALLS = []
ANSWER = {"profiles": ["vuebung-chat", "zweites"], "light": "made", "store": "made"}


def fake_setup(key, inst, method, body=None, query="", actor="root", recipe=""):
    CALLS.append((key, method, query, json.loads(json.dumps(body)) if body else None, actor))
    if method == "GET":
        return {"v": 1, "kind": "profile", "items": [{"id": p} for p in ANSWER["profiles"]]}, ""
    res = []
    for it in body["items"]:
        r = {"refresh": "present", "server_light": ANSWER["light"], "store": ANSWER["store"]}[it["kind"]]
        if body.get("dry") and r == "made":
            r = "would_make"
        res.append({"kind": it["kind"], "id": it["id"], "result": r, "note": f"n-{it['kind']}"})
    return {"v": 1, "results": res}, ""


m.setup_call = fake_setup
lines = []
okk, said = m.chat_store_apply("vuebung-wiki", who="anna", role="tenant_admin", out=lines.append)
posts = [c for c in CALLS if c[1] == "POST"]
ok("freigeben: erst die Liste der Profile, dann Prüfung (dry), dann der Lauf -- am Gateway des Mandanten",
   okk and [c[1] for c in CALLS] == ["GET", "POST", "POST"] and all(c[0] == "vuebung-werkzeuge" for c in CALLS)
   and posts[0][3]["dry"] is True and posts[1][3]["dry"] is False, (said, CALLS))
items = posts[1][3]["items"]
store = items[-1]
ok("Einträge: refresh, Ampel gelb, Ablagesystem bookstack über den Knotenschlüssel",
   [i["kind"] for i in items] == ["refresh", "server_light", "store"]
   and items[1]["light"] == "yellow" and store["type"] == "bookstack"
   and store["server"] == "vuebung-wiki" and store["id"] == "wiki", items)
ok("Ablagetür für ALLE Zugangsprofile des Gateways", store["doors"] == ["vuebung-chat", "zweites"], store)
ok("wer es auslöste, geht als Akteur mit", all(c[4] == "anna" for c in CALLS), CALLS)
rec = m.load_registry()["instances"]["vuebung-wiki"].get("chat_store") or {}
ok("das Register merkt Gateway, Name, Türen, Person",
   rec.get("gateway") == "vuebung-werkzeuge" and rec.get("store") == "wiki"
   and rec.get("doors") == ["vuebung-chat", "zweites"] and rec.get("by") == "anna", rec)
logl = [e for e in m.read_tenant_log(VU, limit=200) if e["action"] == "chat.store"]
ok("eine Zeile im Mandantenprotokoll", logl and logl[0]["result"] == "ok" and logl[0]["who"] == "anna", logl)

CALLS.clear()
ANSWER["light"] = "conflict"
okk, said = m.chat_store_apply("vuebung-wiki", who="root", role="server_admin", out=lines.append)
items = [c for c in CALLS if c[1] == "POST"][-1][3]["items"]
ok("eine von Hand gesetzte Ampel bleibt: der Lauf lässt server_light weg",
   okk and [i["kind"] for i in items] == ["refresh", "store"], (said, items))

CALLS.clear()
ANSWER.update(light="made", store="conflict")
before = json.dumps(m.load_registry()["instances"]["vuebung-wiki"].get("chat_store"))
okk, said = m.chat_store_apply("vuebung-wiki", who="root", role="server_admin", out=lines.append)
ok("KÖDER Konflikt im Gateway: kein Lauf (nur die Prüfung), ein Satz, das Register unverändert",
   not okk and [c[3]["dry"] for c in CALLS if c[1] == "POST"] == [True] and "store wiki" in said
   and json.dumps(m.load_registry()["instances"]["vuebung-wiki"].get("chat_store")) == before, (said, CALLS))

CALLS.clear()
ANSWER.update(store="made", profiles=[])
okk, said = m.chat_store_apply("vuebung-wiki", who="root", role="server_admin", out=lines.append)
ok("ohne Zugangsprofil im Gateway: kein Lauf, der Satz sagt, was fehlt",
   not okk and "access profile" in said and [c[1] for c in CALLS] == ["GET"], (said, CALLS))
ANSWER["profiles"] = ["vuebung-chat"]
okk, said = m.chat_store_apply("vuebung-kp", who="root")
ok("KÖDER eine MCP-Quelle ohne mcp_store: abgelehnt", not okk and "no store connector" in said, said)

reg = m.load_registry()
reg["instances"]["vuebung-werkzeuge2"] = dict(reg["instances"]["vuebung-werkzeuge"], name="werkzeuge2", port=8130)
m.save_registry(reg)
okk, said = m.chat_store_apply("vuebung-wiki", who="root")
ok("zwei Gateways im Mandanten: abgelehnt, beide genannt",
   not okk and "several" in said and "vuebung-werkzeuge2" in said, said)
reg = m.load_registry()
del reg["instances"]["vuebung-werkzeuge2"]
reg["instances"]["anderer-gw"] = dict(reg["instances"]["vuebung-werkzeuge"], tenant=OTHER, name="gw", port=8131)
m.save_registry(reg)
okk, said = m.chat_store_apply("vuebung-wiki", gateway="anderer-gw", who="root")
ok("KÖDER das Gateway eines anderen Mandanten: abgelehnt", not okk and "same tenant" in said, said)

print("=== D. der Worker urteilt ===")
FN = []
m.chat_store_apply = lambda name, gateway="", who="root", role="root", out=print: (
    FN.append(("store", name, who, role)) or (True, "store-satz"))
m.app_idp_apply = lambda name, connector_name="", off=False, dry_run=False, accept_version="", \
    who="root", role="root", out=print: (FN.append(("idp", name, off, who, role)) or (True, "idp-satz"))
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, action, op, inst="vuebung-wiki"):
    _rid[0] += 1
    rid = f"{_rid[0]:032x}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": inst, "action": action, "by": by, "op": op}, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


for action, op in (("app_idp", "on"), ("chat_store", "on")):
    FN.clear()
    r = queue("niemand", action, op)
    ok(f"{action}: ohne Rolle nichts, die Funktion läuft nicht", not r["ok"] and FN == [], (r, FN))
    r = queue("bert", action, op)
    ok(f"KÖDER {action}: der tenant_admin eines ANDEREN Mandanten nicht", not r["ok"] and FN == [], (r, FN))
    r = queue("anna", action, op)
    ok(f"{action}: der tenant_admin der Instanz -- die Funktion mit seiner Rolle, der Satz zurück",
       r["ok"] and FN and FN[0][1] == "vuebung-wiki" and FN[0][-1] == "tenant_admin"
       and r["message"] in ("store-satz", "idp-satz"), (r, FN))
FN.clear()
r = queue("root", "app_idp", "off")
ok("app_idp off: dieselbe Funktion mit off", r["ok"] and FN == [("idp", "vuebung-wiki", True, "root", "server_admin")], FN)
FN.clear()
r = queue("root", "chat_store", "off")
ok("KÖDER chat_store off gibt es nicht: abgewiesen", not r["ok"] and FN == [], (r, FN))
denied = [e for e in m.read_tenant_log(VU, limit=500) if e["action"] in ("chat.store", "app.idp")
          and e["result"] == "denied"]
ok("die Absagen stehen im Protokoll des Mandanten", len(denied) >= 2, denied)

print("=== E. die Karten ===")
src = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"), encoding="utf-8").read()
tree = ast.parse(src)


def card(var):
    return Environment(autoescape=True).from_string(next(
        template_src.resolve(tree, n.value) for n in tree.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == var))


MCP, SIGN = card("MCP_CARD"), card("SIGNIN_CARD")
TEN = {"t1": {"label": "vuebung", "idp": {"issuer": "https://kc/realms/vuebung", "client_id": "oaap-node",
                                          "connector": "auth"}},
       "t2": {"label": "hand", "idp": {"issuer": "https://x/realms/hand", "client_id": "c"}},
       "t3": {"label": "leer"}}
I = {"gw": {"tenant": "t1", "port": 8120, "mcp_sources": True, "mcp": {}, "setup": {"path": "/oaap/setup"}},
     "wiki": {"tenant": "t1", "port": 8121, "mcp": {"path": "/oaap/mcp"}, "mcp_store": "bookstack",
              "oidc_client": {"redirect_paths": ["/oidc/callback"], "groups_claim": True}},
     "kp": {"tenant": "t1", "port": 8122, "mcp": {"path": "/mcp"}}}


def mcp_card(name, insts=I, admin=True):
    v = iv.mcp_view(name, insts[name], insts, TEN, manager=admin, default_tid="t1")
    return v, MCP.render(i={"key": name, "mcp": v})


v, h = mcp_card("wiki")
ok("Ablage: „Für den Chat“ mit Knopf für die Verwaltung, Gateway genannt",
   v["store"]["can"] and "Für den Chat freigeben" in h and 'action="/instances/wiki/chat-store"' in h
   and "gw" in v["store"]["target"], h[-900:])
v, h = mcp_card("wiki", admin=False)
ok("KÖDER ohne Verwaltung: kein Knopf", "chat-store" not in h, h[-500:])
v, h = mcp_card("kp")
ok("eine MCP-Quelle ohne Ablagen-Konnektor: kein Abschnitt", v["store"] is None and "Für den Chat" not in h)
old = dict(I, gw=dict(I["gw"], setup={}))
v, h = mcp_card("wiki", old)
ok("ein Gateway ohne Einrichtungstür: kein Knopf, ein Satz", not v["store"]["can"]
   and "Einrichtungstür" in h, h[-500:])
done = dict(I, wiki=dict(I["wiki"], chat_store={"gateway": "gw", "store": "wiki", "kind": "bookstack",
                                                "doors": ["vuebung-chat"], "at": "2026-11-24T20:00:00Z",
                                                "by": "anna"}))
v, h = mcp_card("wiki", done)
ok("freigegeben: wann, wer, Gateway, Türen; der Knopf heißt jetzt „Erneut freigeben“",
   "2026-11-24 20:00" in h and "anna" in h and "vuebung-chat" in h and "Erneut freigeben" in h, h[-1200:])


def sign(name, insts=I, tenant="t1", admin=True):
    inst = dict(insts[name], tenant=tenant)
    v = iv.signin_view(inst, TEN, manager=admin, default_tid="t1")
    return v, SIGN.render(i={"key": name, "signin": v})


v, h = sign("kp")
ok("eine App ohne eigene Anmeldung: keine Karte", v is None and "anmeldung" not in h)
v, h = sign("wiki")
ok("nicht eingerichtet: Satz und Knopf „verbinden“",
   v["state"] == "none" and "Anmeldung über den Realm verbinden" in h and 'value="on"' in h, h)
v, h = sign("wiki", admin=False)
ok("KÖDER ohne Verwaltung: kein Knopf", "<form" not in h, h)
v, h = sign("wiki", tenant="t3")
ok("ohne Realm: kein Knopf, der Satz sagt warum", not v["can"] and "keinen Anmeldedienst" in h, h)
v, h = sign("wiki", tenant="t2")
ok("Realm von Hand: kein Knopf", not v["can"] and "von Hand" in h, h)
on = dict(I, wiki=dict(I["wiki"], oidc_idp={"client_id": "oaap-app-wiki", "issuer": "https://kc/realms/vuebung",
                                            "space": "vuebung", "redirect_uris": ["https://wiki.x/oidc/callback"],
                                            "groups_claim": True, "at": "2026-11-24T20:01:00Z", "by": "root",
                                            "client_secret": "DARF-NIE-ERSCHEINEN"}))
v, h = sign("wiki", on)
ok("eingerichtet: Client, Realm, Rücksprung, Erneut einrichten und Abschalten",
   "oaap-app-wiki" in h and "https://wiki.x/oidc/callback" in h and "Abschalten" in h
   and "Erneut einrichten" in h and 'value="off"' in h, h)
ok("KÖDER das Geheimnis erscheint nie", "DARF-NIE-ERSCHEINEN" not in h
   and "DARF-NIE" not in json.dumps(v), v)

print("")
print("FAILED" if fails else "ALL PASSED", f"({fails} failed)")
sys.exit(1 if fails else 0)
