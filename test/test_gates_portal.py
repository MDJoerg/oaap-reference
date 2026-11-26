#!/usr/bin/env python3
"""Tore im Portal (RFC-0135 Stufe 2) -- die Seite, auf der der Mandanten-Admin
ein Tor einrichtet, ohne Kommandozeile (Jörg 2026-11-26).

Festgehalten werden die Regeln, nicht der Knopf:

- der echte Worker (appctl.gate_job über den Spool) prüft die Rolle aus dem
  Datensatz des Auftraggebers: tenant_admin des Mandanten und server_admin
  dürfen, ein Benutzer und der Admin eines anderen Mandanten nicht -- und das
  steht im Protokoll des Mandanten;
- Prozess, Tor, Zugang, Entziehen, Entfernen tun dasselbe wie `oaap gate`;
- ein Tor ändern behält seine Felder (vorher setzte `add` sie zurück);
- der Schlüssel eines Zugangs entsteht im Portal: zum Knoten und zu identity
  geht nur sein Hash -- der lesbare Schlüssel steht nicht im Ergebnis, nicht im
  Spool, nicht im Protokoll, nicht in der Schlüsseldatei; und identity nimmt
  genau diesen Schlüssel am Tor an (Ping mit Basic wie SM59);
- die Ampel: rot ohne Ziel oder ohne gültigen Zugang, gelb wenn ein Schlüssel
  in 14 Tagen abläuft, sonst grün; das Ziel ist die Produktions-Instanz
  desselben Mandanten, nie eine Generalprobe;
- die Seite zeigt Tore, Zugänge, Prozesse und den Schlüssel genau einmal mit
  den SM59-Angaben.

Braucht flask/werkzeug (identity echt), kein Docker. Aufruf:
python3 test/test_gates_portal.py
"""
import ast
import base64
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

from template_src import resolve  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
PORTAL = os.path.join(SERVICES, "portal")
DATA = tempfile.mkdtemp(prefix="oaap-gates-portal-")
os.environ["OAAP_DATA_DIR"] = DATA

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


try:
    import flask  # noqa: F401
    from werkzeug.security import generate_password_hash
except ImportError:
    print("SKIP  flask/werkzeug fehlen.")
    sys.exit(1)

# --- appctl
sys.path.insert(0, PLATFORM)
sys.path.insert(0, SERVICES)
import appctl as m                                            # noqa: E402

m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.write_external_caddy = lambda *a, **kw: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
KUNDE, _ = m.tenant_create("kunde")
ANDERE, _ = m.tenant_create("andere")

# --- identity, echt, mit derselben Benutzerdatei wie appctl
ID_DIR = os.path.join(DATA, "data", "identity")
os.makedirs(ID_DIR, exist_ok=True)
for k, v in {"SESSION_SECRET": "s", "SETUP_TOKEN": "t", "INTERNAL_API_KEY": "i",
             "OAAP_IDENTITY_DATA_DIR": ID_DIR}.items():
    os.environ[k] = v


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant, "groups": [], "active": True,
            "password_hash": ""}


json.dump([user("root", ["server_admin"]), user("karin", ["tenant_admin"], KUNDE),
           user("uwe", ["user"], KUNDE), user("anna", ["tenant_admin"], ANDERE)],
          open(os.path.join(ID_DIR, "users.json"), "w"))
json.dump({"setup_done": True, "server_admin_migrated": True, "support_migrated": True,
           "tenant_migrated": True}, open(os.path.join(ID_DIR, "state.json"), "w"))
IDENTITY_DIR = os.path.join(SERVICES, "identity")
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
ident = importlib.reload(importlib.import_module("app"))
sys.path.remove(IDENTITY_DIR)
sys.modules.pop("app", None)
ident.TENANTS_FILE = m.TENANTS_FILE
ident.GATES_FILE = m.GATES_FILE
ident.THROTTLE_FILE = os.path.join(ID_DIR, "throttle.json")
ident._login_blocked = lambda b: False
ident._login_failed = lambda b: None


def fake_identity_exec(script, env=None):
    old_env = dict(os.environ)
    os.environ.update(env or {})
    sys.modules["app"] = ident
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(script, "<identity>", "exec"), {"__name__": "__identity__"})
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.modules.pop("app", None)
    return out.getvalue()


m._identity_exec = fake_identity_exec

# the tenant's case inbox (production) and a rehearsal beside it
reg = m.load_registry()
reg["instances"]["kunde-vorgaenge"] = {"app_id": "vorgaenge", "tenant": KUNDE, "channel": "production",
                                       "container": "oaap-app-kunde-vorgaenge", "svc_port": 8000}
reg["instances"]["kunde-vorgaenge-probe"] = {"app_id": "vorgaenge", "tenant": KUNDE, "channel": "production",
                                             "rehearsal": {"of": "kunde-vorgaenge"},
                                             "container": "x", "svc_port": 8000}
m.save_registry(reg)

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, op, **args):
    _rid[0] += 1
    rid = f"g{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": "", "action": "gate-admin", "by": by, "op": op, "args": args}
    path = os.path.join(QUEUE, f"{rid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        res = json.load(f)
    res["_spool_left"] = os.path.exists(path)
    return res


def log(tid, action):
    return [e for e in m.read_tenant_log(tid) if e["action"] == action]


def gate(tid, name):
    return m.load_gates()["gates"].get(f"{tid}/{name}")


print("=== wer darf ===")
r = queue("uwe", "process", tenant=KUNDE, name="sap-anfrage", queue="sap-support")
ok("ein Benutzer legt keinen Prozess an", not r["ok"]
   and "kunde" not in json.dumps(m.load_gates()["processes"]), r)
r = queue("anna", "process", tenant=KUNDE, name="sap-anfrage", queue="sap-support")
ok("der Admin eines anderen Mandanten legt keinen in „kunde“ an",
   not any(p["tenant"] == KUNDE for p in m.load_gates()["processes"].values()), r)
ok("... abgewiesen steht im Protokoll", any(e["result"] == "denied" for e in
                                           log(KUNDE, "gate.process") + log(ANDERE, "gate.process")),
   log(ANDERE, "gate.process"))
r = queue("karin", "process", tenant=KUNDE, name="sap-anfrage", queue="sap-support", kind="question",
          label="SAP fragt")
p = m.load_gates()["processes"].get(f"{KUNDE}/sap-anfrage") or {}
ok("der tenant_admin legt den Prozess in seinem Mandanten an", r["ok"] and p.get("queue") == "sap-support"
   and p.get("case_kind") == "question", r)
ok("... mit seinem Namen im Protokoll", any(e["who"] == "karin" and e["result"] == "ok"
                                           for e in log(KUNDE, "gate.process")), log(KUNDE, "gate.process"))
ok("... und die Antwort nennt das Ziel", "kunde-vorgaenge" in r["message"]
   and "probe" not in r["message"], r)

r = queue("karin", "add", tenant=KUNDE, name="sap-support", process="gibtsnicht")
ok("ein Tor auf einen fehlenden Prozess wird abgewiesen", not r["ok"] and gate(KUNDE, "sap-support") is None, r)
r = queue("karin", "add", tenant=KUNDE, name="Sap Support", process="sap-anfrage")
ok("ein ungültiger Name wird abgewiesen", not r["ok"], r)
r = queue("karin", "add", tenant=KUNDE, name="sap-support", process="sap-anfrage")
g = gate(KUNDE, "sap-support") or {}
ok("der tenant_admin legt das Tor an", r["ok"] and g.get("process") == "sap-anfrage"
   and g.get("user") == "sap-support" and len(g.get("fields") or []) == 5, r)

# eigene Felder (wie mit --fields), dann im Portal nur den Benutzer ändern
doc = m.load_gates()
doc["gates"][f"{KUNDE}/sap-support"]["fields"] = doc["gates"][f"{KUNDE}/sap-support"]["fields"][:3]
m.save_gates(doc)
r = queue("karin", "add", tenant=KUNDE, name="sap-support", process="sap-anfrage", user="SAPSUP")
g = gate(KUNDE, "sap-support") or {}
ok("ein Tor ändern behält seine Felder", r["ok"] and g.get("user") == "SAPSUP"
   and len(g.get("fields") or []) == 3, g)

print("")
print("=== der Schlüssel entsteht im Portal ===")
secret = "Portal-Geheimnis-0123456789abcdefABCDEF_-x"
r = queue("karin", "access", tenant=KUNDE, name="sap-support", label="SAP A4H 001", days="30",
          hash=generate_password_hash(secret))
d = r.get("detail") or {}
ok("ein Zugang wird ausgestellt; die Antwort nennt Kennung, Prinzipal, Ablauf, Adresse und Benutzer",
   r["ok"] and len(d.get("id", "")) == 8 and d.get("principal") == "gate-sap-support-1"
   and d.get("user") == "SAPSUP" and d.get("expires"), r)
ok("der lesbare Schlüssel steht nicht im Ergebnis, und die Spool-Datei ist weg",
   secret not in json.dumps(r) and not r["_spool_left"], r)
ok("... nicht im Protokoll", secret not in json.dumps(m.read_tenant_log(KUNDE)))
keys = ident.load_keys()
rec = next((k for k in keys if k["id"] == d.get("id")), {})
ok("... nicht in der Schlüsseldatei -- dort steht der Hash, der ihn prüft",
   secret not in json.dumps(keys) and ident.check_password_hash(rec.get("hash", ""), secret)
   and rec.get("instance") == f"oaap.gate:{KUNDE}/sap-support", rec)
ok("der Zugang steht im Protokoll mit der Person", any(e["who"] == "karin" for e in log(KUNDE, "gate.access")),
   log(KUNDE, "gate.access"))

token = f"oaapk_{d.get('id')}_{secret}"
basic = "Basic " + base64.b64encode(f"SAPSUP:{token}".encode()).decode()
cl = ident.app.test_client()
with ident.app.test_request_context(headers={"Authorization": basic}):
    acc = ident.gate_access(KUNDE, "sap-support")
ok("identity nimmt genau diesen Schlüssel am Tor an (Basic wie SM59)", acc is not None
   and acc[1]["username"] == "gate-sap-support-1")
with ident.app.test_request_context(headers={"Authorization": "Basic " + base64.b64encode(
        f"SAPSUP:oaapk_{d.get('id')}_falsch-falsch-falsch-falsch".encode()).decode()}):
    ok("... und keinen anderen", ident.gate_access(KUNDE, "sap-support") is None)

r = queue("karin", "access", tenant=KUNDE, name="sap-support", label="ohne Hash")
ok("ein Zugang ohne Hash wird abgewiesen (das Portal macht den Schlüssel)", not r["ok"], r)
r = queue("karin", "access", tenant=KUNDE, name="sap-support", label="X", hash="klartext")
ok("ein „Hash“, der keiner ist, wird abgewiesen", not r["ok"] and "klartext" not in json.dumps(ident.load_keys()), r)
r = queue("anna", "access", tenant=KUNDE, name="sap-support", label="fremd",
          hash=generate_password_hash("x" * 43))
ok("der Admin eines anderen Mandanten stellt keinen Zugang aus",
   not any(k.get("label") == "fremd" for k in ident.load_keys()), r)

print("")
print("=== entziehen, entfernen ===")
r = queue("karin", "revoke", tenant=KUNDE, name="sap-support", access="gate-sap-support-1")
rec = next((k for k in ident.load_keys() if k["id"] == d.get("id")), {})
ok("ein Zugang wird entzogen", r["ok"] and rec.get("revoked"), r)
with ident.app.test_request_context(headers={"Authorization": basic}):
    ok("... und der Schlüssel kommt nicht mehr durch", ident.gate_access(KUNDE, "sap-support") is None)
r = queue("karin", "remove", tenant=KUNDE, name="sap-support")
ok("entfernen ohne Bestätigung wird abgewiesen", not r["ok"] and gate(KUNDE, "sap-support"), r)
r = queue("root", "remove", tenant=KUNDE, name="sap-support", yes=True)
ok("der server_admin entfernt das Tor im genannten Mandanten", r["ok"] and gate(KUNDE, "sap-support") is None, r)
r = queue("karin", "frobnicate", tenant=KUNDE, name="sap-support")
ok("eine unbekannte Aktion wird abgewiesen", not r["ok"], r)

print("")
print("=== die Ansicht: Ziel und Ampel ===")
sys.path.insert(0, PORTAL)
import gates_view as gv                                       # noqa: E402

now = datetime(2026, 11, 26, tzinfo=timezone.utc)


def iso(days):
    return (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


res_ = lambda ref: ref                                         # noqa: E731
insts = m.load_registry()["instances"]
ok("das Ziel ist die Produktions-Instanz desselben Mandanten, nie die Generalprobe",
   gv.case_target(insts, KUNDE, {"kind": "case"}, res_) == "kunde-vorgaenge")
ok("... ein anderer Mandant hat keins", gv.case_target(insts, ANDERE, {"kind": "case"}, res_) == "")
ok("... eine genannte Instanz gilt nur, wenn es sie dort gibt",
   gv.case_target(insts, KUNDE, {"kind": "case", "instance": "gibtsnicht"}, res_) == "")
sc = f"oaap.gate:{KUNDE}/sap-support"
ks = [{"id": "a1", "principal": "gate-sap-support-1", "instance": sc, "expires": iso(60), "last_used": iso(-1)},
      {"id": "a2", "principal": "gate-sap-support-2", "instance": sc, "expires": iso(5), "last_used": ""},
      {"id": "a3", "principal": "gate-sap-support-3", "instance": sc, "expires": iso(60), "revoked": True},
      {"id": "x9", "principal": "gate-other-1", "instance": f"oaap.gate:{KUNDE}/other", "expires": iso(60)},
      {"id": "y1", "principal": "uwe", "instance": "", "expires": iso(60)}]
rows = gv.access_rows(ks, KUNDE, "sap-support", now)
ok("die Zugänge eines Tors: nur Schlüssel mit seiner Grenze", [r_["id"] for r_ in rows] == ["a1", "a2", "a3"]
   and [r_["state"] for r_ in rows] == ["valid", "soon", "revoked"], rows)
ok("grün: Ziel und gültiger Zugang", gv.light("t", rows[:1])[0] == "green")
ok("gelb: ein Schlüssel läuft in 14 Tagen ab", gv.light("t", rows[:2])[0] == "yellow")
ok("rot: kein gültiger Zugang", gv.light("t", rows[2:])[0] == "red")
ok("rot: kein Ziel", gv.light("", rows[:1])[0] == "red")
ok("zuletzt benutzt: der jüngste Gebrauch", gv.last_use(rows) == rows[0]["last_used"] != "")
ok("SM59 aus der Adresse", gv.sm59("https://bec.brandeis.de/gate/sap-support", "sap-support")
   == {"host": "bec.brandeis.de", "port": "443", "path": "/gate/sap-support", "user": "sap-support"})

print("")
print("=== die Seite ===")
from jinja2 import Environment                                # noqa: E402

tree = ast.parse(open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read())


def tpl(name):
    body = next(resolve(tree, n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == name for t in n.targets))
    return Environment(autoescape=True).from_string(body)


doc = {"processes": {f"{KUNDE}/sap-anfrage": {"tenant": KUNDE, "name": "sap-anfrage", "kind": "case",
                                              "queue": "sap-support", "case_kind": "question"}},
       "gates": {f"{KUNDE}/sap-support": {"tenant": KUNDE, "name": "sap-support", "user": "sap-support",
                                          "process": "sap-anfrage",
                                          "fields": [{"name": "ref", "required": True},
                                                     {"name": "system", "required": False}]}}}
v = gv.tenant_view(doc, KUNDE, insts, ks, res_, lambda n: f"https://kunde.example/gate/{n}", now)
html = tpl("GATES_BODY").render(v=v, label="kunde", kinds=dict(gv.CASE_KINDS), q="", msg=None, error=None)
ok("die Seite zeigt Tor, Adresse, Ampel, Prozess mit Ziel und die Zugänge",
   "sap-support" in html and "https://kunde.example/gate/sap-support" in html and "dot warn" in html
   and "kunde-vorgaenge" in html and "gate-sap-support-2" in html and "entzogen" in html, html[:400])
ok("... Entziehen nur für gültige Zugänge", html.count('action="/tore/revoke') == 2)
ok("... Formulare für Zugang, Tor und Prozess", 'action="/tore/access' in html
   and 'action="/tore/add' in html and 'action="/tore/process' in html)
ok("... und nie einen Schlüssel", "oaapk_" not in html)
empty = tpl("GATES_BODY").render(v={"gates": [], "processes": []}, label="", kinds=dict(gv.CASE_KINDS),
                                 q="", msg=None, error=None)
ok("ohne Prozess: erst den Prozess anlegen, kein Formular „Neues Tor“",
   "zuerst einen Prozess" in empty and "Neues Tor" not in empty)
key_html = tpl("GATE_KEY_BODY").render(name="sap-support", key="oaapk_ab12cd34_" + "s" * 43,
                                       d={"label": "SAP A4H 001", "expires": iso(90), "principal": "gate-sap-support-1",
                                          "id": "ab12cd34", "url": "https://kunde.example/gate/sap-support"},
                                       s=gv.sm59("https://kunde.example/gate/sap-support", "sap-support"), q="")
ok("der Schlüssel einmal, mit den SM59-Angaben", "oaapk_ab12cd34_" in key_html and "nur jetzt" in key_html
   and "kunde.example" in key_html and "/gate/sap-support" in key_html and "Basis-Authentifizierung" in key_html)

src = open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read()
ok("das Portal schickt den Hash, nie den Schlüssel, in den Spool",
   'args["hash"] = generate_password_hash(secret)' in src and '"secret": secret' not in src.split("# --- Tore")[1])
nav = open(os.path.join(PORTAL, "nav_view.py"), encoding="utf-8").read()
ok("„Tore“ steht in der Navigation für Mandanten-Admins", '("gates", "/tore", "Tore", "is_user_admin")' in nav)
dockerfile = open(os.path.join(PORTAL, "Dockerfile"), encoding="utf-8").read()
ok("gates_view.py kommt ins Abbild", "portal/gates_view.py" in dockerfile)

print("")
print("ALL PASS" if not fails else f"{fails} FAILURES")
sys.exit(1 if fails else 0)
