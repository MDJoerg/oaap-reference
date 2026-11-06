#!/usr/bin/env python3
"""Die Karte "Neustart-Schutz" auf der Instanzseite (RFC-0069 Stufe 5,
oaap.core.updates 2.7.2: die Tuer des Mandanten-Admins).

Festgehalten werden die Regeln, nicht der Knopf:

  * der Worker auf dem Host urteilt -- Rolle und Mandant aus dem Datensatz
    des Handelnden --, nicht die Seite: der Spool ist Daten, kein Vertrauen.
    Also laufen gefaelschte Anfragen durch den echten Worker;
  * ein tenant_admin schuetzt eine Instanz des eigenen Mandanten, mit
    Begruendung und Ende; eine fremde Instanz gibt es fuer ihn nicht, mit
    demselben Satz wie eine, die es nicht gibt;
  * beide Tueren -- Befehlszeile und Seite -- rufen DIESELBE Funktion
    (zwei Wege, eine Regel);
  * jede Handlung und jede Absage steht genau einmal im Protokoll des
    Mandanten, mit dem Menschen und der Rolle;
  * nach einer Handlung rechnet der Knoten den Plan sofort neu, nicht erst
    in der naechsten Minute;
  * die Karte bietet nur an, was der Worker annimmt, und die Tuer der Seite
    laesst ohne Begruendung nichts in den Spool.

Das Messen und Planen prueft test_host_care.py; hier ist es ausgeblendet.
Braucht kein Docker und keinen Knoten.
"""
import ast
import contextlib
import datetime
import io
import json
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-care-portal-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402
from services import host_care                                # noqa: E402

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


# --- ohne Maschine: messen und planen ist test_host_care.py ------------------
FRESH = []
m._care_fresh = lambda who="root", role="root": FRESH.append((who, role)) or {}
m.refresh_destinations = lambda *x, **k: None
m.state_view_write = lambda *x, **k: None
m.egress_view_write = lambda *x, **k: None
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
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


instance("pz-p1", PZ)
instance("pz-p2", PZ)
instance("and-a1", AND)

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, op, inst, **args):
    """Eine Anfrage der Instanzseite durch den echten Worker schicken."""
    _rid[0] += 1
    rid = f"{_rid[0]:032x}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": inst, "action": "care", "by": by,
            "op": op, "args": args}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def log(tid, action):
    return [(e["who"], e["role"], e["result"]) for e in m.read_tenant_log(tid, limit=500)
            if e["action"] == action]


def prot():
    return host_care.load_protections(m.APPS_DIR)


TODAY = datetime.date.today()

print("=== wer darf: der Worker urteilt ===")
r = queue("uwe", "protect", "pz-p1", reason="bitte nicht")
ok("ein Benutzer ohne Verwalterrolle schuetzt nichts", not r["ok"] and not prot(), r)
ok("... und die Absage steht im eigenen Mandanten, mit ihm",
   log(PZ, "instance.protected") == [("uwe", "-", "denied")], log(PZ, "instance.protected"))

r = queue("anna", "protect", "pz-p1", reason="")
ok("ohne Begruendung: abgewiesen", not r["ok"] and "say why" in r["message"] and not prot(), r)
r = queue("anna", "protect", "pz-p1", reason="Schulung bis Freitag",
          until=(TODAY + datetime.timedelta(days=40)).isoformat())
ok("ueber 30 Tage hinaus: abgewiesen", not r["ok"] and "30 days" in r["message"] and not prot(), r)
r = queue("anna", "protect", "pz-p1", reason="Schulung bis Freitag", until="Freitag")
ok("ein Ende, das kein Tag ist: abgewiesen", not r["ok"] and "YYYY-MM-DD" in r["message"], r)
ok("... jede Absage im Protokoll, mit ihr",
   log(PZ, "instance.protected") == [("uwe", "-", "denied")] + [("anna", "tenant_admin", "denied")] * 3,
   log(PZ, "instance.protected"))

FRESH.clear()
r = queue("anna", "protect", "pz-p1", reason="Schulung bis Freitag")
rec = prot().get("pz-p1") or {}
ok("ein tenant_admin schuetzt die eigene Instanz, Vorgabe 7 Tage",
   r["ok"] and rec.get("reason") == "Schulung bis Freitag"
   and rec.get("until") == (TODAY + datetime.timedelta(days=7)).isoformat(), (r, rec))
ok("... der Datensatz nennt sie und ihren Mandanten",
   rec.get("who") == "anna" and rec.get("tenant") == PZ, rec)
ok("... die Seite bekommt den Satz der Befehlszeile",
   "protected against a care reboot until" in r["message"], r)
ok("... genau eine Zeile dazu, mit ihr und ihrer Rolle",
   log(PZ, "instance.protected")[-1] == ("anna", "tenant_admin", "ok"), log(PZ, "instance.protected"))
ok("... in ihrem Mandanten allein", not log(AND, "instance.protected") and not log(DEFAULT, "instance.protected"))
ok("... und der Plan wird sofort neu gerechnet, mit ihr", FRESH == [("anna", "tenant_admin")], FRESH)

day = (TODAY + datetime.timedelta(days=20)).isoformat()
r = queue("anna", "protect", "pz-p1", reason="Schulung verlaengert", until=day)
rec = prot().get("pz-p1") or {}
ok("verlaengern ist dieselbe Handlung noch einmal, mit Begruendung und Ende",
   r["ok"] and rec.get("until") == day and rec.get("reason") == "Schulung verlaengert", (r, rec))

print("=== die Mandantengrenze ===")
n_and = len(m.read_tenant_log(AND, limit=500))
r = queue("anna", "protect", "and-a1", reason="fremd")
r2 = queue("anna", "protect", "gibtsnicht", reason="fremd")
ok("eine fremde Instanz gibt es fuer sie nicht", not r["ok"] and "and-a1" not in prot(), r)
ok("... mit demselben Satz wie eine, die es nicht gibt",
   r["message"].replace("'and-a1'", "X") == r2["message"].replace("'gibtsnicht'", "X"),
   (r["message"], r2["message"]))
ok("... und der fremde Mandant erfaehrt nichts davon",
   len(m.read_tenant_log(AND, limit=500)) == n_and)
r = queue("nils", "release", "pz-p1")
ok("der Admin des anderen Mandanten hebt ihn nicht auf", not r["ok"] and "pz-p1" in prot(), r)

print("=== der Server-Admin ===")
FRESH.clear()
r = queue("root", "protect", "and-a1", reason="Messung")
ok("er schuetzt jede Instanz, ohne einen Mandanten zu nennen",
   r["ok"] and prot().get("and-a1", {}).get("tenant") == AND, r)
ok("... im Protokoll des Mandanten der Instanz, mit ihm",
   log(AND, "instance.protected") == [("root", "server_admin", "ok")], log(AND, "instance.protected"))
r = queue("root", "release", "and-a1")
ok("er hebt auf", r["ok"] and "and-a1" not in prot() and "released" in r["message"], r)
ok("... im Protokoll", log(AND, "instance.released") == [("root", "server_admin", "ok")])
r = queue("root", "release", "and-a1")
ok("noch einmal aufheben: nichts zu tun, kein Eintrag",
   r["ok"] and "was not protected" in r["message"]
   and log(AND, "instance.released") == [("root", "server_admin", "ok")], r)
r = queue("root", "explode", "and-a1")
ok("eine unbekannte Handlung: abgewiesen", not r["ok"] and "unknown" in r["message"], r)
ok("jede Handlung rechnete den Plan neu", len(FRESH) == 2, FRESH)

print("=== zwei Tueren, eine Regel ===")
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
cli = src.split("def cmd_node_care", 1)[1].split("\ndef ", 1)[0]
ok("die Befehlszeile ruft dieselben Funktionen wie der Worker",
   "care_protect_instance(" in cli and "care_release_instance(" in cli
   and "host_care.protect(" not in cli and "host_care.release(" not in cli)
r = queue("anna", "release", "pz-p1")
ok("sie hebt ihren Schutz auf", r["ok"] and not prot(), r)

# ---------------------------------------------------------------------------
print("=== die Karte bietet an, was der Worker annimmt ===")
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
tree = ast.parse(io.open(APP_PY, encoding="utf-8").read())


def const(name, env=None):
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == name for t in n.targets))
    return eval(compile(ast.Expression(node.value), APP_PY, "eval"), env or {})


CARD = Environment(autoescape=True).from_string(const("CARE_CARD"))
body = const("INSTANCE_EDIT_BODY", {"CARE_CARD": "<!--care-->", "IMAGE_CARD": "<!--image-->"})
ok("die Karte steht auf der Instanzseite, im Reiter Diagnose",
   "<!--care-->" in body.split("tab == 'diagnose'")[1].split("tab == 'fernzugang'")[0])
ok("die Karte Abbild steht daneben (RFC-0071 Stufe 1)",
   "<!--care--><!--image-->" in body)

# RFC-0071 Stufe 1: die Karte Abbild -- Wort, Anzahl, Datum, Basis; kein Paketname.
ICARD = Environment(autoescape=True).from_string(const("IMAGE_CARD"))


def irender(services, state="open"):
    return ICARD.render(i={"key": "x", "imgcare": {
        "known": True, "services": services, "state": state,
        "word": host_care.IMAGE_WORDS_DE.get(state, state), "computed": "2026-11-06 07:35"}})


SVC = {"service": "", "image": "oaap-app/studio:0.4.3", "word": "", "state": "open", "open": 5,
       "oldest": "2026-09-29", "base": "", "built": "", "behind": None, "why": ""}
h = irender([SVC])
ok("Abbild: offen mit Anzahl und Datum, Basis nicht aufgezeichnet",
   "5 offene Sicherheitsmeldung(en)" in h and "2026-09-29" in h and "nicht aufgezeichnet" in h
   and "card warn" in h, h)
h = irender([dict(SVC, open=None, why="no dpkg in the image", base="caddy:2", behind=True)], "unknown")
ok("Abbild: nicht lesbar mit Grund, nie 0; Upstream weitergezogen",
   "nicht lesbar" in h and "no dpkg" in h and "Keine offene" not in h
   and "weitergezogen" in h, h)
h = irender([dict(SVC, open=0, base="python:3.13-slim", built="2026-11-06", behind=False)], "fresh")
ok("Abbild: frisch, entspricht Upstream", "Keine offene" in h and "entspricht Upstream" in h
   and "frisch" in h, h)
h = ICARD.render(i={"key": "x", "imgcare": {"known": False, "services": [], "state": ""}})
ok("Abbild: noch nicht gelesen", "noch nicht gelesen" in h, h)


# RFC-0071 §15: woraus gebaut wird, und die Selbstheilung -- die Funktion der Seite.
RV = {"host_care": host_care}
for node in tree.body:
    if ((isinstance(node, ast.FunctionDef) and node.name == "_rebuild_view")
            or (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "SOURCE_WORDS" for t in node.targets))):
        exec(compile(ast.Module([node], []), APP_PY, "exec"), RV)
ART = {"services": [{"image": "oaap-app/w:1"}], "source": {"kind": "artifact", "stored": "1.zip"}}
GIT_OLD = {"services": [{"image": "oaap-app/s:1"}], "source": {"kind": "git", "url": "https://x"}}
GIT = {"services": [{"image": "oaap-app/s:1"}], "care_rebuild": "self",
       "source": {"kind": "git", "url": "https://x", "commit": "ab" * 20}}
LOCAL = {"services": [{"image": "oaap-app/l:1"}], "source": {"kind": "local", "url": "/srv"}}


def irender2(inst, state="", text=""):
    return ICARD.render(i={"key": "x", "imgcare": {
        "known": True, "services": [SVC], "state": "open", "word": "w", "computed": "c",
        "rebuild": {"state": state, "at": "2026-11-06 09:00", "by": "anna",
                    "why": "", "text": text}, **RV["_rebuild_view"](inst)}})


h = irender2(ART)
ok("Abbild (Stufe 2): mit Paket der Knopf Neu bauen, kein Zurück ohne Vorgänger",
   "/rebuild/start" in h and "/rebuild/back" not in h and "zurückbehaltenen Paket" in h, h)
ok("Abbild (2b): ohne Zusage Selbstheilung nicht möglich, von Hand geht",
   "Selbstheilung: nicht möglich" in h and "care.rebuild: self" in h
   and "Von Hand neu bauen geht" in h, h)
h = irender2(LOCAL)
ok("Abbild: aus einem Pfad kein Knopf, der Grund steht da",
   "/rebuild/start" not in h and "festgehaltenen Git-Commit" in h and "§8.1" in h, h)
h = irender2(GIT_OLD)
ok("Abbild (2b): Git von vor dem Commit -- kein Knopf, Store-Update hält ihn fest",
   "/rebuild/start" not in h and "Store-Update" in h, h)
h = irender2(GIT)
ok("Abbild (2b): Git mit Commit -- Knopf, der Commit steht da, Selbstheilung möglich",
   "/rebuild/start" in h and "abababababab" in h and "Git-Commit" in h
   and "Selbstheilung: möglich" in h, h)
h = irender2(ART, "fallback", "Neu gebaut, aber ... vorigen Abbild.")
ok("Abbild: nach dem Rückfall rot, mit Zurück-Knopf",
   'class="err"' in h and "/rebuild/back" in h and "anna" in h, h)
h = irender2(ART, "running", "Der Neubau läuft gerade.")
ok("Abbild: während des Neubaus kein zweiter Knopf", "/rebuild/start" not in h, h)
ok("die Tür der Seite ist die Spool-Aktion rebuild",
   "\"action\": \"rebuild\"" in io.open(APP_PY, encoding="utf-8").read())


def view(held=None, plan=None, overridden=False, known=True):
    return {"known": known, "held": held,
            "plan_text": host_care.plan_text(plan, de=True) if plan else "",
            "overridden": overridden, "default_days": 7, "max_days": 30,
            "today": TODAY.isoformat(),
            "max_day": (TODAY + datetime.timedelta(days=30)).isoformat()}


def render(**kw):
    return CARD.render(i={"key": "pz-p1", "care": view(**kw)})


html = render()
ok("ungeschuetzt: der Knopf zum Schuetzen, Begruendung Pflicht, kein Aufheben",
   'action="/instances/pz-p1/care/protect"' in html and 'name="reason" maxlength="300" required' in html
   and 'care/release' not in html, html[:500])
form = html.split('care/protect">')[1].split("</form>")[0]
ok("... das Ende ist ein Tag, hoechstens 30 Tage ab heute",
   f'name="until" min="{TODAY.isoformat()}" max="{(TODAY + datetime.timedelta(days=30)).isoformat()}"' in form, form)
plan = {"at_local": "2026-11-09 02:00", "urgency": "care", "state": "planned",
        "source": "manual", "reason": "Messung", "held_by": [], "overridden": []}
html = render(held={"instance": "pz-p1", "until": "2026-11-12", "reason": "Schulung"}, plan=plan)
ok("geschuetzt: Ende und Begruendung, verlaengern und aufheben",
   "GESCHÜTZT" in html and "2026-11-12" in html and "Schulung" in html
   and 'care/protect' in html and 'care/release' in html and "Der Schutz hält nicht" not in html, html[:600])
ok("... verlaengern verlangt die Begruendung noch einmal",
   'required\n             value="Schulung"' in html, html)
html = render(held={"instance": "pz-p1", "until": "2026-11-12", "reason": "Schulung"},
              plan={**plan, "urgency": "critical", "overridden": [{"instance": "pz-p1"}]}, overridden=True)
ok("ueberstimmt: die Karte sagt es", "Der Schutz hält nicht" in html, html)
html = render(known=False)
ok("ohne Pflegedatei: ein Satz, und die Tuer bleibt", "noch nicht gemessen" in html and 'care/protect' in html)

print("=== die Tuer der Seite ===")
ns = {"re": re, "_re": re, "quote": lambda s: s, "json": json, "os": os}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "instance_care_act":
        node.decorator_list = []
        exec(compile(ast.Module([node], []), APP_PY, "exec"), ns)
    elif isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("_CARE_DAY", "CARE_WAIT_SECONDS")
                                               for t in node.targets):
        exec(compile(ast.Module([node], []), APP_PY, "exec"), ns)
SENT, BACK = [], []
REQ = types.SimpleNamespace(values={}, form={})
ns.update(
    request=REQ, require_instance_admin=lambda name: None, _same_origin=lambda: True,
    load_instances=lambda: {"pz-p1": {"name": "pz-p1"}},
    redirect=lambda url, code=302: ("redirect", url),
    _inst_back=lambda name, msg="", err="", keep=(): BACK.append((name, msg, err)) or ("back", err),
    _queue_and_wait=lambda name, payload, wait: SENT.append((name, payload)) or {"ok": True, "message": "fine"})

REQ.form = {"reason": "Schulung", "until": "2026-11-12"}
ns["instance_care_act"]("pz-p1", "protect")
ok("schuetzen: ein Auftrag mit Begruendung und Ende, ohne Rolle, ohne Mandant",
   SENT == [("pz-p1", {"action": "care", "op": "protect",
                       "args": {"reason": "Schulung", "until": "2026-11-12"}})], SENT)
ok("... und die Antwort des Knotens steht auf der Seite", BACK[-1][1] == "fine", BACK)
SENT.clear()
REQ.form = {"reason": "", "until": ""}
ns["instance_care_act"]("pz-p1", "protect")
ok("ohne Begruendung geht nichts in den Spool", not SENT and "Begründung" in BACK[-1][2], BACK)
REQ.form = {"reason": "x", "until": "Freitag"}
ns["instance_care_act"]("pz-p1", "protect")
ok("ein Ende, das kein Tag ist, auch nicht", not SENT and "JJJJ-MM-TT" in BACK[-1][2], BACK)
REQ.form = {}
ns["instance_care_act"]("pz-p1", "release")
ok("aufheben braucht nichts", SENT == [("pz-p1", {"action": "care", "op": "release", "args": {}})], SENT)
SENT.clear()
ns["instance_care_act"]("pz-p1", "explode")
ok("eine unbekannte Handlung geht nicht in den Spool", not SENT)
ns["_same_origin"] = lambda: False
res = ns["instance_care_act"]("pz-p1", "release")
ok("fremde Herkunft: 403, nichts im Spool", res[1] == 403 and not SENT, res)
ns["_same_origin"] = lambda: True
ns["_queue_and_wait"] = lambda name, payload, wait: None
ns["instance_care_act"]("pz-p1", "release")
ok("keine Antwort des Knotens: gesagt, nicht erfunden", "noch nicht geantwortet" in BACK[-1][2], BACK[-1])

print(f"\n{'ALLES GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
