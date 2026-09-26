#!/usr/bin/env python3
"""Destinationen binden im Portal (oaap.net.destinations 0.3 §2.7,
RFC-0033 §1.2: ein tenant_admin bindet).

Was hier festgehalten wird, sind die Regeln, nicht der Knopf:

- der Worker auf dem Host entscheidet -- Rolle, Mandant, Art, Name --,
  nicht die Seite: der Spool ist Daten, kein Vertrauen. Also wird hier
  NICHT die Route geprueft, sondern der echte Worker mit gefaelschten
  Anfragen;
- ein tenant_admin bindet im eigenen Mandanten, an eine Destination des
  eigenen Mandanten, und bekommt fuer die Instanz eines anderen Mandanten
  "gibt es nicht" zu hoeren;
- ein Benutzer ohne Verwalterrolle bindet nichts, und das steht im
  Protokoll;
- eine Generalprobe wird vom Portal nie gebunden (RFC-0033 §1.5) -- das
  ist eine bewusste Handlung an der Maschine;
- jedes Binden und Loesen steht GENAU EINMAL im Protokoll des Mandanten,
  mit dem Menschen, der es getan hat (nicht "portal", nicht doppelt);
- die Seite bietet nur an, was der Worker annimmt: nur erklaerte
  Beduerfnisse, nur Destinationen derselben Art, keine Formulare an einer
  Generalprobe, und bei Uebergabe steht das Wort dabei.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_destinations_portal.py
"""
import argparse
import ast
import contextlib
import io
import ipaddress
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-dest-portal-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

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


# --- Docker-Attrappen ------------------------------------------------------
NETS = {"oaap_default": ["172.18.0.0/16"]}
m.docker_subnets = lambda net=None: [ipaddress.ip_network(s) for k, v in NETS.items()
                                     for s in v if net is None or k == net]
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.image_uid = lambda img: None
m.recreate_instance_containers = lambda name, *a, **kw: None
m.container_env = lambda container: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
KUNDE, _ = m.tenant_create("kunde")
ANDERE, _ = m.tenant_create("andere")


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant,
            "groups": [], "active": True}


write_users([user("root", ["server_admin"]),
             user("karin", ["tenant_admin"], KUNDE),
             user("nils", ["tenant_admin"], ANDERE),
             user("uwe", ["user"], KUNDE)])

MANIFEST = "\n".join([
    'oaap_manifest: "0.5"', "app:", "  id: orders", "  name: Orders",
    "  version: 0.1.0", "  type: native", "services:", "  web:",
    "    build: .", "    port: 80", "routes:", "  - path: /",
    "    roles: [user]", "health:", "  path: /healthz", "destinations:",
    "  - name: erp", "    kind: http", "    purpose: order lookup",
    "  - name: mail", "    kind: tcp",
    "    env: { host: SMTP_HOST, port: SMTP_PORT, user: SMTP_USER, password: SMTP_PASSWORD }",
    "",
])
pkg = tempfile.mkdtemp(prefix="oaap-dest-portal-pkg-")
with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
    f.write(MANIFEST)


def install(name, tenant):
    NETS[m.app_network(name)] = ["172.29.0.0/16"]
    args = argparse.Namespace(package=pkg, path="", ref="", name=name,
                              channel="test", store_source="", tenant=tenant,
                              key="", ident=None, rehearsal=None, bind=[])
    before = set(m.load_registry()["instances"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        m._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
    new = set(m.load_registry()["instances"]) - before
    assert len(new) == 1, (new, buf.getvalue())
    return new.pop()


K_ORDERS = install("orders", "kunde")
A_ORDERS = install("orders", "andere")
ok("zwei Instanzen mit demselben Namen in zwei Mandanten",
   K_ORDERS != A_ORDERS and K_ORDERS in m.load_registry()["instances"]
   and A_ORDERS in m.load_registry()["instances"], (K_ORDERS, A_ORDERS))

m.destination_add(KUNDE, "kunden-erp", "http", "https://erp.example.com/api/", "none")
m.destination_add(KUNDE, "kunden-mail", "tcp", "tcp://mail.example.com:587", "basic",
                  user="bot", secret="mailpw")
m.destination_add(ANDERE, "fremdes-erp", "http", "https://other.example.com/", "none")

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, instance, **req):
    """Eine Anfrage durch den echten Worker schicken."""
    _rid[0] += 1
    rid = f"r{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": instance, "action": "destination", "by": by, **req}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def binds(key):
    return dict((m.load_registry()["instances"][key].get("destinations")) or {})


def log(tid, action):
    return [e for e in m.read_tenant_log(tid) if e["action"] == action]


print("=== der Worker urteilt, nicht die Seite ===")
r = queue("karin", K_ORDERS, op="bind", need="erp", destination="kunden-erp")
ok("ein tenant_admin bindet im eigenen Mandanten", r["ok"], r)
ok("... und die Bindung steht in der Registry",
   binds(K_ORDERS) == {"erp": "kunden-erp"}, binds(K_ORDERS))
ok("... und die Umgebung der App zeigt aufs Gateway, ohne Geheimnis",
   m.load_env(K_ORDERS).get("OAAP_DESTINATION_ERP_URL", "").endswith("/destinations/erp/"),
   m.load_env(K_ORDERS))
lines = log(KUNDE, "destination.bind")
ok("das Binden steht GENAU EINMAL im Protokoll des Mandanten, mit dem Menschen",
   len(lines) == 1 and lines[0]["who"] == "karin" and lines[0]["role"] == "tenant_admin"
   and lines[0]["subject"] == K_ORDERS, lines)

r = queue("karin", K_ORDERS, op="bind", need="erp", destination="kunden-erp")
ok("ein zweites Mal binden ist kein Fehler und schreibt keine zweite Zeile",
   r["ok"] and "already" in r["message"] and len(log(KUNDE, "destination.bind")) == 1, r)

r = queue("karin", K_ORDERS, op="bind", need="mail", destination="kunden-erp")
ok("eine http-Destination an einen tcp-Bedarf wird abgelehnt",
   not r["ok"] and "declares" in r["message"], r)
r = queue("karin", K_ORDERS, op="bind", need="erp", destination="kunden-mail")
ok("... und eine tcp-Destination an einen http-Bedarf auch",
   not r["ok"] and "declares" in r["message"], r)

r = queue("karin", K_ORDERS, op="bind", need="mail", destination="kunden-mail")
ok("tcp an den erklaerten tcp-Bedarf geht -- als Uebergabe", r["ok"], r)
ok("... und das Protokoll sagt Uebergabe",
   any("handover" in e.get("detail", "") for e in log(KUNDE, "destination.bind")))

print("")
print("=== die Mandantengrenze ===")
before = binds(A_ORDERS)
r = queue("karin", A_ORDERS, op="bind", need="erp", destination="fremdes-erp")
ok("die Instanz eines anderen Mandanten ist fuer einen tenant_admin 'unbekannt'",
   not r["ok"] and r["message"] == "unknown instance", r)
ok("... und es wurde nichts gebunden", binds(A_ORDERS) == before)
r = queue("karin", K_ORDERS, op="bind", need="erp", destination="fremdes-erp")
ok("eine Destination eines anderen Mandanten gibt es fuer die eigene Instanz nicht",
   not r["ok"] and "no destination" in r["message"], r)
r = queue("karin", A_ORDERS, op="unbind", need="erp")
ok("auch Loesen an fremder Instanz: 'unbekannt'",
   not r["ok"] and r["message"] == "unknown instance", r)
r = queue("root", A_ORDERS, op="bind", need="erp", destination="fremdes-erp")
ok("der server_admin darf in jedem Mandanten", r["ok"], r)
ok("... und seine Zeile steht im Protokoll DES Mandanten",
   any(e["who"] == "root" and e["role"] == "server_admin"
       for e in log(ANDERE, "destination.bind")), log(ANDERE, "destination.bind"))

print("")
print("=== wer nicht verwalten darf ===")
n_before = len(log(KUNDE, "destination.bind"))
r = queue("uwe", K_ORDERS, op="bind", need="mail", destination="kunden-mail")
ok("ein Benutzer ohne Verwalterrolle bindet nichts",
   not r["ok"] and "requires" in r["message"], r)
ok("... die Umgebung aendert sich nicht", len(log(KUNDE, "destination.bind")) == n_before + 1
   and binds(K_ORDERS)["mail"] == "kunden-mail", log(KUNDE, "destination.bind"))
denied = [e for e in log(KUNDE, "destination.bind") if e["result"] == "denied"]
ok("... der Versuch steht als 'denied' im Protokoll, mit dem Namen",
   len(denied) == 1 and denied[0]["who"] == "uwe", denied)
r = queue("uwe", K_ORDERS, op="unbind", need="erp")
ok("und loesen darf er auch nichts", not r["ok"] and binds(K_ORDERS).get("erp") == "kunden-erp", r)
r = queue("niemand", K_ORDERS, op="bind", need="erp", destination="kunden-erp")
ok("ein unbekannter Name auch nicht", not r["ok"], r)

print("")
print("=== Loesen ===")
r = queue("karin", K_ORDERS, op="unbind", need="mail")
ok("ein tenant_admin loest im eigenen Mandanten", r["ok"] and "mail" not in binds(K_ORDERS), r)
ok("... die Uebergabe-Felder verschwinden aus der Umgebung",
   "SMTP_HOST" not in m.load_env(K_ORDERS), m.load_env(K_ORDERS))
done = [e for e in log(KUNDE, "destination.unbind") if e["result"] == "ok"]
ok("... und das Loesen steht einmal im Protokoll (der Versuch von uwe extra, als denied)",
   len(done) == 1 and done[0]["who"] == "karin", log(KUNDE, "destination.unbind"))
r = queue("karin", K_ORDERS, op="unbind", need="mail")
ok("etwas Ungebundenes loesen wird mit einem Satz abgelehnt",
   not r["ok"] and "no destination bound" in r["message"], r)
r = queue("karin", K_ORDERS, op="umdrehen", need="erp")
ok("eine unbekannte Aktion wird abgelehnt", not r["ok"] and "unknown" in r["message"], r)

print("")
print("=== die Generalprobe ===")
reg = m.load_registry()
reg["instances"][K_ORDERS]["rehearsal"] = {"of": "orders", "until": "2999-01-01T00:00:00Z"}
m.save_registry(reg)
r = queue("karin", K_ORDERS, op="bind", need="mail", destination="kunden-mail")
ok("das Portal bindet eine Generalprobe nie -- auch nicht der tenant_admin",
   not r["ok"] and "by hand on the node" in r["message"] and "mail" not in binds(K_ORDERS), r)
r = queue("root", K_ORDERS, op="bind", need="mail", destination="kunden-mail")
ok("auch der server_admin nicht: die Ausnahme ist eine Handlung an der Maschine",
   not r["ok"] and "--rehearsal-exception" in r["message"], r)
r = queue("karin", K_ORDERS, op="unbind", need="erp")
ok("Loesen an einer Generalprobe geht (das macht sie sicherer, nicht offener)",
   r["ok"] and "erp" not in binds(K_ORDERS), r)

# ---------------------------------------------------------------------------
print("")
print("=== die Seite bietet an, was der Worker annimmt ===")
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
tree = ast.parse(io.open(APP_PY, encoding="utf-8").read())
body = next(ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "INSTANCE_EDIT_BODY" for t in n.targets))
card = body.split("<h2>Destinationen</h2>")[1].split("</section>")[0]
ENV = Environment(autoescape=True)
CARD = ENV.from_string(card)

# _destination_view aus der Quelle, ohne Flask
ns = {"json": json, "DESTINATIONS_FILE": os.path.join(DATA, "destinations.json"),
      "CONNECT_STATE": os.path.join(DATA, "none.json"),
      "resolve_tenant": lambda t: t, "_read_json": lambda p: None}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in ("_dest_target_label",
                                                           "_destination_view"):
        exec(compile(ast.Module([node], []), APP_PY, "exec"), ns)
with open(ns["DESTINATIONS_FILE"], "w", encoding="utf-8") as f:
    json.dump({"destinations": m.load_destinations()}, f)


def page(key, rehearsal=None):
    inst = dict(m.load_registry()["instances"][key])
    inst["tenant"] = m.resolve_tenant(inst.get("tenant"))
    rows = ns["_destination_view"](inst)
    return rows, CARD.render(i={"key": key, "destinations": rows, "rehearsal": rehearsal})


rows, html = page(A_ORDERS)
by = {r_["need"]: r_ for r_ in rows}
ok("der erklaerte, ungebundene Bedarf bekommt nur Destinationen SEINER Art und SEINES Mandanten",
   by["mail"]["options"] == [] and by["erp"]["bound"], by)
rows, html = page(K_ORDERS)
by = {r_["need"]: r_ for r_ in rows}
ok("erp: http-Destinationen des eigenen Mandanten, keine fremden, keine tcp",
   by["erp"]["options"] == ["kunden-erp"], by["erp"])
ok("mail: nur die tcp-Destination", by["mail"]["options"] == ["kunden-mail"], by["mail"])
ok("ein Formular zum Zuordnen steht da -- fuer jeden Bedarf",
   html.count('value="bind"') == 2 and 'action="/instances/' + K_ORDERS + '/destination"' in html)
ok("jedes Formular traegt den Reiter mit zurueck", html.count('name="tab" value="netz"') == 2)
ok("bei Uebergabe steht das Wort dabei -- und bei http nicht",
   html.count("Adresse und Zugangsdaten liegen danach in der Umgebung") == 1, html)
ok("der fremde Name kommt nirgends vor", "fremdes-erp" not in html)

reg = m.load_registry()
reg["instances"][K_ORDERS]["destinations"] = {"erp": "kunden-erp"}
m.save_registry(reg)
rows, html = page(K_ORDERS)
ok("ein gebundener Bedarf bietet 'Loesen' an und kein Zuordnen mehr",
   html.count('value="unbind"') == 1 and html.count('value="bind"') == 1, html)

rows, html = page(K_ORDERS, rehearsal={"badge": "Generalprobe"})
ok("an einer Generalprobe steht kein Zuordnen -- nur Loesen und die Anleitung fuer die Maschine",
   'value="bind"' not in html and "--rehearsal-exception" in html and 'value="unbind"' in html,
   html)

m.destination_remove(KUNDE, "kunden-mail")
with open(ns["DESTINATIONS_FILE"], "w", encoding="utf-8") as f:
    json.dump({"destinations": m.load_destinations()}, f)
rows, html = page(K_ORDERS)
ok("gibt es keine passende Destination, sagt die Seite es und wer sie anlegen kann",
   "keine Destination der Art tcp" in html and "Betreiber" in html, html)

print("")
print("=== die Route ===")
src = io.open(APP_PY, encoding="utf-8").read()
route = src.split('@app.post("/instances/<name>/destination")')[1].split("@app.post(")[0]
ok("die Route prueft die Verwalterrolle der INSTANZ, wie die anderen",
   "require_instance_admin(name)" in route)
ok("... und fragt den Worker, sie schreibt die Registry nicht selbst",
   '"action": "destination"' in route and "save_registry" not in route)
ok("... und gibt keine Rehearsal-Ausnahme weiter",
   "rehearsal_exception" not in route)

print("")
print("PASS" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
