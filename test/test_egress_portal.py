#!/usr/bin/env python3
"""Die Seite "Abfluss" im Portal (RFC-0068 Stufe 4, §5 und §6).

Festgehalten werden die Regeln, nicht der Knopf:

  * der Worker auf dem Host urteilt -- Rolle und Mandant aus dem Datensatz
    des Handelnden --, nicht die Seite: der Spool ist Daten, kein Vertrauen.
    Also laufen gefaelschte Anfragen durch den echten Worker;
  * ein tenant_admin schliesst den eigenen Mandanten, oeffnet nicht, nimmt
    nicht aus, erlaubt nicht; einen fremden Mandanten gibt es fuer ihn nicht,
    mit demselben Satz wie einen, den es nicht gibt;
  * jede Handlung und jede Absage steht genau einmal im Protokoll des
    Mandanten, mit dem Menschen;
  * die Sicht, die der Knoten schreibt, und die Seite der Befehlszeile lesen
    dieselben Tatsachen; die Seite bietet nur an, was der Worker annimmt.

Das Verschieben der Netze prueft test_closed_egress.py; hier ist es
ausgeblendet. Braucht kein Docker und keinen Knoten.
"""
import ast
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-egress-portal-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402
# Eine Destination pruefen fragt Docker nach seinen Netzen (dest_target_refusal);
# der Test braucht laut Kopf kein Docker -- also keine.
m.docker_subnets = lambda net=None: []

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


# --- ohne Docker: das Verschieben ist test_closed_egress.py ------------------
MOVED = []
m.egress_apply_tenant = lambda tid: (MOVED.append(tid) or ([], []))
m.egress_apply_instance = lambda name: (MOVED.append(name) or True)
m.egress_node_rule_ensure = lambda: True
m.egress_node_rule_present = lambda: True
m.egress_instance_faults = lambda name, inst, rule_check=None: []
m.refresh_destinations = lambda *x, **k: None
m.state_view_write = lambda *x, **k: None
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
READS = []


def fake_read(tid, name, d):
    READS.append(name)
    return {"at": "2026-11-02T10:00:00+00:00", "gateway": False, "answer": "HTTP 200"}


m.egress_read_target = fake_read
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
PZ, _ = m.tenant_create("pz")
AND, _ = m.tenant_create("and")


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant,
            "groups": [], "active": True}


write_users([user("root", ["server_admin"]),
             user("anna", ["tenant_admin"], PZ),
             user("nils", ["tenant_admin"], AND),
             user("uwe", ["user"], PZ)])


def instance(key, tid, **extra):
    reg = m.load_registry()
    rec = {"tenant": tid, "name": key, "image": "img:1", "port": 80, "routes": [],
           "services": [{"service": "", "container": f"oaap-app-{key}",
                         "image": "img:1", "port": 80}]}
    rec.update(extra)
    reg["instances"][key] = rec
    m.save_registry(reg)


instance("pz-p1", PZ, routes=[{"path": "/frei", "roles": ["public"]}])
instance("pz-p2", PZ)
instance("and-a1", AND)
m.destination_add(PZ, "lager", "http", "http://10.10.10.136:8080/", "none")
m.destination_add(PZ, "post", "tcp", "tcp://mail.example.com:587", "none")

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, op, **args):
    """Eine Anfrage der Seite durch den echten Worker schicken."""
    _rid[0] += 1
    rid = f"{_rid[0]:032x}"
    os.makedirs(QUEUE, exist_ok=True)
    os.makedirs(os.path.join(m.JOBS_DIR, rid), exist_ok=True)
    body = {"id": rid, "instance": "", "action": "egress", "by": by,
            "op": op, "args": args}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        res = json.load(f)
    with open(os.path.join(m.JOBS_DIR, rid, "result.json"), encoding="utf-8") as f:
        job = json.load(f)
    res["job"] = job
    return res


def log(tid, action):
    return [e for e in m.read_tenant_log(tid, limit=500) if e["action"] == action]


def exc(key):
    return m.egress_exception(m.load_registry()["instances"][key])


print("=== wer darf: der Worker urteilt ===")
r = queue("uwe", "close")
ok("ein Benutzer ohne Verwalterrolle schliesst nichts",
   not r["ok"] and not m.tenant_egress_closed(PZ), r)
ok("... und die Absage steht im eigenen Mandanten, mit ihm",
   [(e["who"], e["result"]) for e in log(PZ, "egress.close")] == [("uwe", "denied")],
   log(PZ, "egress.close"))

r = queue("anna", "close")
ok("ein tenant_admin schliesst den eigenen Mandanten", r["ok"] and m.tenant_egress_closed(PZ), r)
ok("... die Seite bekommt die Antwort im Auftrag", r["job"]["ok"] and "Moved" in r["job"]["message"],
   r["job"])
ok("... genau eine Zeile dazu, mit ihr und ihrer Rolle",
   [(e["who"], e["role"], e["result"]) for e in log(PZ, "egress.close")]
   == [("uwe", "-", "denied"), ("anna", "tenant_admin", "ok")], log(PZ, "egress.close"))
ok("... und es wurde wirklich verschoben", PZ in MOVED, MOVED)
with open(m.EGRESS_VIEW, encoding="utf-8") as f:
    ok("... und die Seite sieht es sofort, nicht erst nach dem Minutenlauf",
       json.load(f)["tenants"][PZ]["closed"] is True)

r = queue("anna", "open", reason="wir wollen wieder raus")
ok("oeffnen darf sie nicht", not r["ok"] and "server admin" in r["message"]
   and m.tenant_egress_closed(PZ), r)
ok("... die Absage steht im Protokoll, mit ihr",
   [(e["who"], e["result"]) for e in log(PZ, "egress.open")] == [("anna", "denied")],
   log(PZ, "egress.open"))
r = queue("anna", "except", instance="pz-p1", reason="bitte")
ok("ausnehmen auch nicht", not r["ok"] and not exc("pz-p1"), r)
r = queue("anna", "allow", destination="lager", reason="bitte")
ok("erlauben auch nicht, und das Ziel wird gar nicht erst gelesen",
   not r["ok"] and not READS and "server admin" in r["message"], r)

print("=== die Mandantengrenze ===")
n_and = len(m.read_tenant_log(AND, limit=500))
r = queue("anna", "close", tenant="and")
r2 = queue("anna", "close", tenant="gibtsnicht")
ok("einen fremden Mandanten gibt es fuer sie nicht",
   not r["ok"] and not m.tenant_egress_closed(AND), r)
ok("... mit demselben Satz wie einen, den es nicht gibt",
   r["message"].replace("'and'", "X") == r2["message"].replace("'gibtsnicht'", "X"),
   (r["message"], r2["message"]))
ok("... und der fremde Mandant erfaehrt nichts davon",
   len(m.read_tenant_log(AND, limit=500)) == n_and)
r = queue("anna", "close", tenant="pz")
ok("den eigenen beim Namen nennen geht", r["ok"], r)

print("=== der Server-Admin, mit Begruendung ===")
r = queue("root", "except", tenant="pz", instance="pz-p1", reason="Lieferant im Netz")
ok("er nimmt eine Instanz aus", r["ok"] and exc("pz-p1")["reason"] == "Lieferant im Netz", r)
ok("... im Protokoll mit ihm",
   [(e["who"], e["role"]) for e in log(PZ, "egress.exception")] == [("root", "server_admin")])
r = queue("root", "except", tenant="pz", instance="and-a1", reason="x")
ok("eine Instanz eines anderen Mandanten unter diesem Namen: abgewiesen",
   not r["ok"] and "no instance" in r["message"] and not exc("and-a1"), r)
r = queue("root", "end-exception", tenant="pz", instance="pz-p1")
ok("er beendet die Ausnahme", r["ok"] and not exc("pz-p1"), r)
r = queue("root", "allow", tenant="pz", destination="lager", reason="")
ok("erlauben ohne Begruendung: abgewiesen", not r["ok"] and "say why" in r["message"], r)
r = queue("root", "allow", tenant="pz", destination="lager", reason="Lagerrechner im LAN")
ok("erlauben mit Begruendung, das Ziel wird gelesen",
   r["ok"] and READS == ["lager"] and "HTTP 200" in r["message"], r)
r = queue("root", "allow", tenant="pz", destination="post", reason="Mail")
ok("eine tcp-Destination nicht", not r["ok"] and "http" in r["message"], r)
r = queue("root", "explode", tenant="pz")
ok("eine unbekannte Handlung: abgewiesen", not r["ok"] and "unknown" in r["message"], r)
r = queue("root", "open", tenant="pz")
ok("oeffnen ohne Begruendung: abgewiesen", not r["ok"] and m.tenant_egress_closed(PZ), r)

print("=== die Sicht: dieselben Tatsachen wie die Befehlszeile ===")
m.egress_view_write()
with open(m.EGRESS_VIEW, encoding="utf-8") as f:
    view = json.load(f)
fz = view["tenants"][PZ]
ok("jeder Mandant steht drin, mit seinem Zustand",
   fz["closed"] is True and view["tenants"][AND]["closed"] is False, list(view["tenants"]))
ok("das erlaubte Ziel mit Lesung und Begruendung",
   [(d["name"], d["reason"], d["read_bad"]) for d in fz["allowed"]]
   == [("lager", "Lagerrechner im LAN", False)] and "HTTP 200" in fz["allowed"][0]["read"],
   fz["allowed"])
m.egress_read_target = lambda tid, name, d: {"at": "spaeter", "refused": "no answer"}
m.egress_reread_destination(PZ, "lager")
m.egress_read_target = fake_read
fb = m.egress_facts(PZ, m.load_tenants()[PZ])["allowed"][0]
ok("eine Lesung, die abgewiesen wurde, steht als Fehler da",
   fb["read_bad"] is True and fb["read"] == "no answer", fb)
m.egress_reread_destination(PZ, "lager")
ok("die tcp-Destination steht nicht unter den erlaubbaren",
   [d["name"] for d in fz["allowable"]] == [], fz["allowable"])
ok("der oeffentliche Pfad steht drin", fz["public"] == [{"instance": "pz-p1", "paths": ["/frei"]}],
   fz["public"])
ok("kein Geheimnis und kein Schluessel in der Sicht",
   "secret" not in json.dumps(view).lower() and "credential" not in json.dumps(view).lower())
out = io.StringIO()
with contextlib.redirect_stdout(out):
    m.egress_page(PZ, m.load_tenants()[PZ])
cli = out.getvalue()
ok("die Befehlszeile liest dasselbe",
   "egress CLOSED" in cli and "lager: http://10.10.10.136:8080/. Lagerrechner im LAN" in cli
   and "pz-p1: /frei" in cli and "8. Faults" in cli, cli)
if hasattr(os, "geteuid"):
    ok("die Sicht ist fuer das Portal lesbar (0644)", (os.stat(m.EGRESS_VIEW).st_mode & 0o777) == 0o644)
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
body = src.split("def state_view_write", 1)[1].split("\ndef ", 1)[0]
ok("der Minutenlauf schreibt die Sicht", "egress_view_write()" in body)
instance("pz-e1", PZ, endpoints=[{"proto": "udp", "port": 5000}])
fo = m.egress_facts(AND, m.load_tenants()[AND])
ok("ein offener Mandant ohne Hindernis hat keine Hindernisse", fo["close_blockers"] == [], fo)
m.save_tenants({**m.load_tenants(), PZ: {**m.load_tenants()[PZ], "egress": {"closed": False}}})
fo = m.egress_facts(PZ, m.load_tenants()[PZ])
why = ""
try:
    m.egress_close(PZ)
except m.EgressRefused as e:
    why = str(e)
ok("die Hindernisse der Seite sind die Saetze der Absage",
   fo["close_blockers"] and all(b in why for b in fo["close_blockers"]), (fo["close_blockers"], why))
reg = m.load_registry()
reg["instances"].pop("pz-e1")
m.save_registry(reg)
m.save_tenants({**m.load_tenants(), PZ: {**m.load_tenants()[PZ],
                                         "egress": {"closed": True, "since": "x", "by": "anna"}}})

# ---------------------------------------------------------------------------
print("=== die Seite bietet an, was der Worker annimmt ===")
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
tree = ast.parse(io.open(APP_PY, encoding="utf-8").read())


def const(name, env=None):
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == name for t in n.targets))
    return eval(compile(ast.Expression(node.value), APP_PY, "eval"), env or {})


BANNER = const("BUILD_BANNER")
BODY = Environment(autoescape=True).from_string(const("EGRESS_BODY", {"BUILD_BANNER": BANNER}))
m.egress_view_write()
with open(m.EGRESS_VIEW, encoding="utf-8") as f:
    view = json.load(f)


def render(tid, admin):
    f = view["tenants"][tid]
    insts = f["instances"]
    return BODY.render(label=f["label"], f=f, is_server_admin=admin, node_rule=True,
                       written="jetzt", job=None, error=None,
                       movers=[i["name"] for i in insts if i["rehearsal"] or not i["exception"]],
                       exceptable=[i for i in insts if not i["exception"] and not i["rehearsal"]])


html = render(PZ, False)
ok("tenant_admin, geschlossen: der Zustand, alle acht Abschnitte",
   "GESCHLOSSEN" in html and all(f"<h2>{n}." in html for n in range(1, 9)), html[:400])
ok("... kein Formular, das der Worker ihr abweist",
   not re.search(r'action="/tenant/egress/(open|except|end-exception|allow|disallow)"', html))
ok("... und der Satz, warum", "nur der" in html and "RFC-0068 §6" in html)
html = render(PZ, True)
ok("server_admin: oeffnen, ausnehmen, nicht mehr erlauben",
   'action="/tenant/egress/open"' in html and 'action="/tenant/egress/except"' in html
   and 'action="/tenant/egress/disallow"' in html)
form = html.split('action="/tenant/egress/open">')[1].split("</form>")[0]
ok("... oeffnen verlangt Begruendung und Bestaetigung",
   'name="reason" required' in form and 'name="confirm" value="1" required' in form, form)
ok("... jedes Formular nennt den Mandanten",
   html.count('name="tenant" value="pz"') == len(re.findall(r'action="/tenant/egress/', html)))
html = render(AND, False)
ok("tenant_admin, offen: der Knopf zum Schliessen, mit Bestaetigung",
   re.search(r'action="/tenant/egress/close">.*?name="confirm" value="1" required', html, re.S)
   and "disabled" not in html.split('action="/tenant/egress/close"')[1].split("</form>")[0])
view["tenants"][AND]["close_blockers"] = ["'and-a1': it holds a non-HTTP endpoint"]
html = render(AND, False)
ok("... mit Hindernis: genannt, und der Knopf ist aus",
   "non-HTTP endpoint" in html
   and "disabled" in html.split('action="/tenant/egress/close"')[1].split("</form>")[0])

print("=== die Tuer im Portal: wer welchen Mandanten sieht ===")
ns = {"re": re, "_re": re, "quote": lambda s: s, "json": json, "os": os}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in ("_egress_target", "tenant_egress_act"):
        node.decorator_list = []
        exec(compile(ast.Module([node], []), APP_PY, "exec"), ns)
    elif isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("EGRESS_OPS", "_EGRESS_NAME")
                                               for t in node.targets):
        exec(compile(ast.Module([node], []), APP_PY, "exec"), ns)
SCOPE = {"role": "tenant_admin", "mine": PZ}
REQ = types.SimpleNamespace(values={}, form={})
SENT = []
ns.update(
    caller_scope=lambda: (SCOPE["role"], SCOPE["mine"]),
    request=REQ, load_tenants=m.load_tenants,
    tenant_id_by_label=lambda label: m.tenant_by_label(label)[0],
    require_user_admin=lambda: None, _same_origin=lambda: True,
    redirect=lambda url, code=302: ("redirect", url),
    management_api=types.SimpleNamespace(
        enqueue=lambda tid, role, op, args, action: SENT.append((tid, op, args, action)) or "j1"))


def target(label, role="tenant_admin", mine=PZ):
    SCOPE.update(role=role, mine=mine)
    REQ.values = {"tenant": label} if label else {}
    return ns["_egress_target"]()


ok("tenant_admin ohne Namen: der eigene", target("")[0] == PZ)
ok("tenant_admin mit fremdem Namen: gibt es nicht, wie einen unbekannten",
   target("and")[0] is None and target("and")[2] == target("gibtsnicht")[2], target("and"))
ok("server_admin nennt jeden", target("and", "server_admin", DEFAULT)[0] == AND)
SCOPE.update(role="tenant_admin", mine=PZ)
REQ.values, REQ.form = {"tenant": "pz"}, {"tenant": "pz", "confirm": "1"}
SENT.clear()
ns["tenant_egress_act"]("close")
ok("tenant_admin: der Auftrag nennt keinen Mandanten, der Host nimmt ihren",
   SENT and SENT[0][0] == PZ and "tenant" not in SENT[0][2] and SENT[0][3] == "egress", SENT)
SCOPE.update(role="server_admin", mine=DEFAULT)
REQ.values, REQ.form = {"tenant": "and"}, {"tenant": "and", "confirm": "1",
                                           "instance": "and-a1", "reason": "weil"}
SENT.clear()
ns["tenant_egress_act"]("except")
ok("server_admin: der Auftrag nennt den Mandanten",
   SENT and SENT[0][0] == AND and SENT[0][2].get("tenant") == "and"
   and SENT[0][2].get("instance") == "and-a1", SENT)
REQ.form = {"tenant": "and"}
SENT.clear()
res = ns["tenant_egress_act"]("open")
ok("ohne Bestaetigung geht nichts in den Spool", not SENT and "err=" in res[1], res)
REQ.form = {"tenant": "and", "confirm": "1", "instance": "../x"}
SENT.clear()
res = ns["tenant_egress_act"]("end-exception")
ok("ein Instanzname, der keiner ist, geht nicht in den Spool", not SENT, res)

print(f"\n{'ALLES GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
