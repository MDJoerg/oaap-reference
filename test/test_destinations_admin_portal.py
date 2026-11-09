#!/usr/bin/env python3
"""Destinationen ANLEGEN im Portal (oaap.net.destinations 0.10 §2.7, oaap.core.portal 2.17).

Festgehalten werden die Regeln, nicht der Knopf:

- nur ein server_admin legt an, setzt ein Geheimnis, entfernt -- geprüft vom
  echten Worker aus dem eigenen Datensatz des Auftraggebers, nicht aus der
  Anfrage; ein tenant_admin und ein Benutzer werden abgewiesen, das steht im
  Protokoll des Mandanten;
- dieselben Prüfungen wie an der Kommandozeile (Ziel in den Netzen des Knotens,
  gebunden = nicht entfernen); Personen durchreichen nur an der Kommandozeile;
- das Geheimnis landet im Geheimnisspeicher und NIRGENDS sonst: nicht im
  Ergebnis, nicht im Protokoll, nicht in der Spool-Datei nach der Antwort;
- die Seite zeigt einem tenant_admin die Liste seines Mandanten ohne Formulare,
  einem server_admin mit Formularen, und nie ein Geheimnis;
- 0.11 (2.7.1, oaap.core.tenant 1.7): der server_admin erlaubt es EINEM Mandanten;
  dann legt dessen tenant_admin https-Ziele auf öffentlichen Adressen an und
  ändert nur, was er selbst angelegt hat. Den Schalter selbst bewegt nur der
  server_admin; zurückgenommen bleiben die Destinationen stehen.

Braucht kein Docker und keinen Knoten. Aufruf: python3 test/test_destinations_admin_portal.py
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

from template_src import resolve  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-dest-admin-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402
from jinja2 import Environment                                # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


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
    return {"username": name, "roles": roles, "tenant": tenant, "groups": [], "active": True}


write_users([user("root", ["server_admin"]), user("karin", ["tenant_admin"], KUNDE),
             user("uwe", ["user"], KUNDE), user("anna", ["tenant_admin"], ANDERE)])

# Namen ohne Netz: was ein Name auflöst, bestimmt der Test.
DNS = {"api.example.com": "93.184.216.34", "ki.example.com": "93.184.216.35",
       "op.example.com": "93.184.216.36", "innen.example.com": "10.0.0.5",
       "docker.example.com": "172.18.0.9"}
_real_gai = m.socket.getaddrinfo


def _gai(host, port, *a, **kw):
    if host in DNS:
        return [(2, 1, 6, "", (DNS[host], int(port)))]
    raise m.socket.gaierror("unknown in test")


m.socket.getaddrinfo = _gai

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]
SECRET = "geheim-Wert-4711-xyz"


def queue(by, op, **args):
    _rid[0] += 1
    rid = f"d{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": "", "action": "destination-admin", "by": by, "op": op, "args": args}
    path = os.path.join(QUEUE, f"{rid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        res = json.load(f)
    res["_spool_left"] = os.path.exists(path)
    return res


def dests(tid):
    return m.load_destinations().get(tid) or {}


def log(tid, action):
    return [e for e in m.read_tenant_log(tid) if e["action"] == action]


print("=== nur der server_admin legt an ===")
r = queue("karin", "add", tenant=KUNDE, name="ki", kind="http", target="https://ki.example.com/v1",
          auth="bearer", secret=SECRET)
ok("ein tenant_admin legt keine Destination an", not r["ok"] and "server_admin" in r["message"]
   and "ki" not in dests(KUNDE), r)
ok("... und das steht als abgewiesen im Protokoll seines Mandanten, mit Namen",
   any(e["result"] == "denied" and e["who"] == "karin" for e in log(KUNDE, "destination.add")),
   log(KUNDE, "destination.add"))
r = queue("uwe", "add", tenant=KUNDE, name="ki", kind="http", target="https://ki.example.com/v1",
          auth="none")
ok("ein Benutzer auch nicht", not r["ok"] and "ki" not in dests(KUNDE), r)

r = queue("root", "add", tenant=KUNDE, name="ki", kind="http", target="https://ki.example.com/v1",
          auth="bearer", secret=SECRET)
ok("der server_admin legt im genannten Mandanten an", r["ok"] and "ki" in dests(KUNDE)
   and "ki" not in dests(DEFAULT), r)
ok("... das Geheimnis liegt im Geheimnisspeicher", m.load_dest_secrets().get(KUNDE, {}).get("ki") == SECRET)
ok("... und nicht im Ergebnis, und die Spool-Datei ist weg",
   SECRET not in json.dumps(r) and not r["_spool_left"], r)
ok("... und nicht im Protokoll", SECRET not in json.dumps(m.read_tenant_log(KUNDE)))
ok("... und nicht in der Datei, die das Portal liest",
   SECRET not in json.dumps(m.load_destinations()))
lines = log(KUNDE, "destination.add")
ok("das Anlegen steht im Protokoll mit dem Menschen, nicht „portal“",
   any(e["result"] == "ok" and e["who"] == "root" for e in lines), lines)

print("")
print("=== dieselben Prüfungen wie die Kommandozeile ===")
r = queue("root", "add", tenant=KUNDE, name="innen", kind="http", target="http://172.18.0.5:8000/",
          auth="none")
ok("ein Ziel in den Netzen des Knotens wird abgewiesen", not r["ok"] and "innen" not in dests(KUNDE), r)
r = queue("root", "add", tenant=KUNDE, name="sap", kind="http", target="https://sap.example.com/",
          auth="principal_propagation")
ok("Personen durchreichen gibt es auf dieser Seite nicht (Kommandozeile)",
   not r["ok"] and "command line" in r["message"] and "sap" not in dests(KUNDE), r)
r = queue("root", "add", tenant=KUNDE, name="X Y", kind="http", target="https://a.example.com/", auth="none")
ok("ein ungültiger Name wird abgewiesen", not r["ok"], r)
r = queue("root", "add", tenant="gibtsnicht", name="ki2", kind="http", target="https://a.example.com/",
          auth="none")
ok("ein Mandant, den es nicht gibt: abgewiesen", not r["ok"], r)

print("")
print("=== Geheimnis setzen, entfernen ===")
r = queue("karin", "secret", tenant=KUNDE, name="ki", secret="anders")
ok("ein tenant_admin setzt kein Geheimnis", not r["ok"]
   and m.load_dest_secrets()[KUNDE]["ki"] == SECRET, r)
r = queue("root", "secret", tenant=KUNDE, name="ki", secret="neu-" + SECRET)
ok("der server_admin setzt es neu", r["ok"] and m.load_dest_secrets()[KUNDE]["ki"] == "neu-" + SECRET, r)
ok("... ohne den Wert im Ergebnis", "neu-" + SECRET not in json.dumps(r), r)
reg = m.load_registry()
reg["instances"]["kunde-x"] = {"tenant": KUNDE, "destinations": {"ki": "ki"}}
m.save_registry(reg)
r = queue("root", "remove", tenant=KUNDE, name="ki")
ok("eine gebundene Destination wird nicht entfernt", not r["ok"] and "unbind" in r["message"]
   and "ki" in dests(KUNDE), r)
del reg["instances"]["kunde-x"]
m.save_registry(reg)
r = queue("karin", "remove", tenant=KUNDE, name="ki")
ok("ein tenant_admin entfernt nichts", not r["ok"] and "ki" in dests(KUNDE), r)
r = queue("root", "remove", tenant=KUNDE, name="ki")
ok("der server_admin entfernt die ungebundene -- mit ihrem Geheimnis",
   r["ok"] and "ki" not in dests(KUNDE) and "ki" not in m.load_dest_secrets().get(KUNDE, {}), r)
r = queue("root", "frobnicate", tenant=KUNDE, name="ki")
ok("eine unbekannte Aktion wird abgewiesen", not r["ok"], r)

print("")
print("=== 2.7.1: der Server-Admin erlaubt es einem Mandanten ===")
r = queue("karin", "allow", tenant=KUNDE, on=True)
ok("den Schalter bewegt ein tenant_admin nicht", not r["ok"] and not m.tenant_admin_may(KUNDE, "destinations"), r)
r = queue("root", "allow", tenant=DEFAULT, on=True)
ok("für den eigenen Mandanten des Knotens gibt es nichts zu erlauben", not r["ok"], r)
r = queue("root", "allow", tenant=KUNDE, on=True)
ok("der server_admin erlaubt es dem Mandanten kunde", r["ok"] and m.tenant_admin_may(KUNDE, "destinations")
   and not m.tenant_admin_may(ANDERE, "destinations"), r)
ok("... das steht im Protokoll des Mandanten", any(e["result"] == "ok" and e["who"] == "root"
                                                  for e in log(KUNDE, "tenant.allow")), log(KUNDE, "tenant.allow"))
r = queue("karin", "add", tenant=KUNDE, name="api", kind="http", target="https://api.example.com/v1",
          auth="bearer", secret=SECRET)
ok("jetzt legt karin eine https-Destination an", r["ok"] and dests(KUNDE).get("api", {}).get("author") == "tenant_admin", r)
ok("... ihr Geheimnis liegt im Geheimnisspeicher, nicht im Ergebnis",
   m.load_dest_secrets()[KUNDE]["api"] == SECRET and SECRET not in json.dumps(r), r)
for nm, kind, target, auth, word in (
        ("klar", "http", "http://api.example.com/", "none", "https://"),
        ("roh", "tcp", "tcp://api.example.com:5432", "none", "http destinations only"),
        ("innen", "http", "https://innen.example.com/", "none", "not a public address"),
        ("dock", "http", "https://docker.example.com/", "none", "not a public address"),
        ("weg", "http", "https://gibtsnicht.example.com/", "none", "does not resolve"),
        ("tun", "http", "via:t1/erp", "none", "tunnel"),
        ("mail", "http", "https://api.example.com/", "none", "Nachrichten"),
        ("pp", "http", "https://api.example.com/", "principal_propagation", "command line"),
        ("lokal", "http", "https://172.18.0.5/", "none", "")):
    r = queue("karin", "add", tenant=KUNDE, name=nm, kind=kind, target=target, auth=auth)
    ok(f"karin: {target} ({kind}, {auth}) abgewiesen" + (f" -- „{word}“" if word else ""),
       not r["ok"] and nm not in dests(KUNDE) and word in r["message"], r)
r = queue("root", "add", tenant=KUNDE, name="op", kind="http", target="https://op.example.com/",
          auth="bearer", secret=SECRET)
ok("der server_admin legt daneben eine an (ohne Vermerk)", r["ok"] and "author" not in dests(KUNDE)["op"], r)
r = queue("karin", "secret", tenant=KUNDE, name="op", secret="fremd")
ok("karin ändert nicht, was der Betreiber angelegt hat", not r["ok"] and "operator" in r["message"]
   and m.load_dest_secrets()[KUNDE]["op"] == SECRET, r)
r = queue("karin", "remove", tenant=KUNDE, name="op")
ok("... und entfernt es nicht", not r["ok"] and "op" in dests(KUNDE), r)
r = queue("karin", "secret", tenant=KUNDE, name="api", secret="neu-" + SECRET)
ok("ihr eigenes Geheimnis setzt sie neu", r["ok"] and m.load_dest_secrets()[KUNDE]["api"] == "neu-" + SECRET, r)
r = queue("anna", "add", tenant=ANDERE, name="x", kind="http", target="https://api.example.com/", auth="none")
ok("ein anderer Mandant ohne Erlaubnis bleibt zu", not r["ok"] and "x" not in dests(ANDERE), r)
r = queue("karin", "add", tenant=ANDERE, name="y", kind="http", target="https://api.example.com/", auth="none")
ok("karin schreibt nicht in einen fremden Mandanten", "y" not in dests(ANDERE), r)
r = queue("uwe", "add", tenant=KUNDE, name="z", kind="http", target="https://api.example.com/", auth="none")
ok("ein Benutzer bleibt draußen, auch mit Erlaubnis am Mandanten", not r["ok"] and "z" not in dests(KUNDE), r)
r = queue("root", "allow", tenant=KUNDE, on=False)
ok("der server_admin nimmt es zurück", r["ok"] and not m.tenant_admin_may(KUNDE, "destinations"), r)
ok("... karins Destination bleibt", "api" in dests(KUNDE))
r = queue("karin", "remove", tenant=KUNDE, name="api")
ok("... aber ändern kann sie sie nicht mehr", not r["ok"] and "api" in dests(KUNDE), r)
ok("unbekanntes Recht: abgewiesen, mit den bekannten",
   m.tenant_set_admin_may(KUNDE, "alles", True)[1].endswith("destinations"))
ok("tenant_set_admin_may verlangt server_admin",
   not m.tenant_set_admin_may(KUNDE, "destinations", True, role="tenant_admin")[0])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_tenant(argparse.Namespace(action="allow", name="kunde", target="destinations"))
ok("Kommandozeile: oaap tenant allow kunde destinations", m.tenant_admin_may(KUNDE, "destinations")
   and "may now destinations" in out.getvalue(), out.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_tenant(argparse.Namespace(action="disallow", name="kunde", target="destinations"))
ok("... und disallow", not m.tenant_admin_may(KUNDE, "destinations"))
for nm in ("api", "op"):
    m.destination_remove(KUNDE, nm)

print("")
print("=== die Seite ===")
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
tree = ast.parse(io.open(APP_PY, encoding="utf-8").read())
body = next(resolve(tree, n.value) for n in tree.body if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "DESTINATIONS_BODY" for t in n.targets))
TPL = Environment(autoescape=True).from_string(body)
m.destination_add(KUNDE, "ki", "http", "https://ki.example.com/v1", "bearer", secret=SECRET)
m.destination_add(KUNDE, "erp", "http", "https://erp.example.com/", "none")
m.destination_add(ANDERE, "fremd", "http", "https://fremd.example.com/", "none")
dfile = os.path.join(DATA, "destinations.json")
with open(dfile, "w", encoding="utf-8") as f:
    json.dump({"destinations": m.load_destinations()}, f)
ns = {"json": json, "DESTINATIONS_FILE": dfile, "default_tenant_id": lambda: DEFAULT,
      "load_tenants": m.load_tenants,
      "tenant_id_by_label": lambda l: {"kunde": KUNDE, "andere": ANDERE}.get(l),
      "load_instances": lambda: {"kunde-chat": {"tenant": KUNDE, "destinations": {"ki": "ki"}},
                                 "andere-x": {"tenant": ANDERE, "destinations": {"ki": "fremd"}}}}
for node in tree.body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEST_AUTH_WORDS" for t in node.targets):
        exec(compile(ast.Module([node], []), APP_PY, "exec"), ns)
    if isinstance(node, ast.FunctionDef) and node.name == "_destinations_facts":
        exec(compile(ast.Module([node], []), APP_PY, "exec"), ns)
facts = ns["_destinations_facts"](KUNDE)
names = [r_["name"] for r_ in facts["rows"]]
ok("die Liste nennt die Destinationen des Mandanten, keine fremden", names == ["erp", "ki"], names)
ki = next(r_ for r_ in facts["rows"] if r_["name"] == "ki")
ok("... mit Zuordnung (nur die eigene Instanz) und Anmeldeart",
   ki["bound"] == ["kunde-chat (als ki)"] and ki["auth"] == "Bearer-Token", ki)
html_t = TPL.render(f=facts, admin=False, label="kunde", q="", msg=None, error=None)
ok("tenant_admin: Liste ohne Formulare, mit dem Satz, wer anlegt",
   "kunde-chat (als ki)" in html_t and "<form" not in html_t and "Betreiber des Knotens" in html_t, html_t)
html_s = TPL.render(f=facts, admin=True, label="kunde", q="?tenant=kunde", msg=None, error=None)
ok("server_admin: Anlegen, Geheimnis setzen (nur wo es eins gibt), Entfernen nur ungebunden",
   'action="/destinationen/add?tenant=kunde"' in html_s and html_s.count('action="/destinationen/secret') == 1
   and html_s.count('action="/destinationen/remove') == 1 and "erst lösen" in html_s, html_s)
ok("kein Geheimnis auf der Seite", SECRET not in html_t + html_s)
ok("server_admin auf der Seite eines Mandanten: der Schalter, aus",
   "Einstellung des Mandanten" not in html_s, html_s)
html_w = TPL.render(f=facts, admin=True, switch=True, label="kunde", q="?tenant=kunde", msg=None, error=None)
ok("... mit switch: „darf nicht“ und „Erlauben“, an /destinationen/allow",
   'action="/destinationen/allow?tenant=kunde"' in html_w and "darf nicht" in html_w
   and 'name="on" value="1"' in html_w, html_w)
dfile_rows = dict(facts, may=True,
                  rows=[dict(r_, own=(r_["name"] == "erp")) for r_ in facts["rows"]])
html_k = TPL.render(f=dfile_rows, admin=False, self_admin=True, label="kunde", q="", msg=None, error=None)
ok("tenant_admin mit Erlaubnis: Anlegen nur https, ohne ?tenant, Art fest http",
   'action="/destinationen/add"' in html_k and 'pattern="https://.+"' in html_k
   and 'name="kind" value="http"' in html_k and "tcp://" not in html_k, html_k)
ok("... Knöpfe nur an der eigenen (erp), die des Betreibers ist markiert",
   html_k.count('action="/destinationen/remove') == 1 and "vom Betreiber angelegt" in html_k
   and "vom Mandanten" in html_k, html_k)
ok("... und kein Schalter für ihn", "Einstellung des Mandanten" not in html_k)

src = io.open(APP_PY, encoding="utf-8").read()
route = src.split('@app.post("/destinationen/<op>")')[1].split("\n\n\n")[0]
ok("die Route weist jeden außer server_admin ab und schreibt nichts selbst",
   'role != "server_admin"' in route and '"action": "destination-admin"' in route
   and "save_destinations" not in route and "_same_origin()" in route)
ok("... einen tenant_admin nur mit Erlaubnis, und nie beim Schalter",
   'op != "allow"' in route and '["may"]' in route)

print("")
print("PASS" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
