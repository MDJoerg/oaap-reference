#!/usr/bin/env python3
"""Die Karte MCP auf der Instanzseite (oaap.core.portal 2.16, RFC-0073 Stufe 3).

  * Der Worker urteilt, nicht die Seite: Ein tenant_admin bekommt eine Absage
    im Protokoll seines Mandanten, und die Funktion laeuft gar nicht. Ein
    server_admin ruft DIESELBE Funktion wie `oaap idp mcp` (zwei Wege, eine
    Regel) -- was sie tut, prueft test_mcp_idp.py.
  * Die Karte zeigt Quelle und Leser, die Quellen eines Gateways und den
    Stand der Anmeldung; den Knopf nur dem server_admin und nur, wenn der
    Realm von einem Konnektor stammt; nie das Geheimnis.
  * Die Route der Seite fragt die Rolle, bevor sie etwas in den Spool legt.

Braucht jinja2 (Portal-Abbild), kein Docker.
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
DATA = tempfile.mkdtemp(prefix="oaap-mcp-portal-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, HERE)

import appctl as m                                            # noqa: E402
import instance_view as iv                                    # noqa: E402
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


m.refresh_destinations = lambda *x, **k: None
m.state_view_write = lambda *x, **k: None
m.egress_view_write = lambda *x, **k: None
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
VU, _ = m.tenant_create("vuebung")


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


write_users([{"username": "root", "roles": ["server_admin"], "tenant": "", "groups": [], "active": True},
             {"username": "anna", "roles": ["tenant_admin"], "tenant": VU, "groups": [], "active": True}])

reg = m.load_registry()
reg["instances"]["vuebung-mcpgw"] = {
    "tenant": VU, "name": "vuebung-mcpgw", "image": "img:1", "port": 8120, "routes": [],
    "mcp_sources": True, "mcp": {},
    "services": [{"service": "", "container": "oaap-app-vuebung-mcpgw", "image": "img:1", "port": 8000}]}
m.save_registry(reg)

CALLS = []


def fake_apply(name, connector_name="", off=False, dry_run=False, accept_version="",
               who="root", role="root", out=print):
    CALLS.append({"name": name, "off": off, "who": who, "role": role})
    return True, "the sentence of the CLI"


m.mcp_idp_apply = fake_apply
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, op, inst="vuebung-mcpgw"):
    _rid[0] += 1
    rid = f"{_rid[0]:032x}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": inst, "action": "mcp_idp", "by": by, "op": op}, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def log(tid):
    return [(e["who"], e["role"], e["result"]) for e in m.read_tenant_log(tid, limit=500)
            if e["action"] == "mcp.idp"]


print("=== der Worker urteilt ===")
r = queue("anna", "on")
ok("ein tenant_admin richtet nichts ein, die Funktion laeuft nicht",
   not r["ok"] and "server_admin" in r["message"] and CALLS == [], (r, CALLS))
ok("... die Absage steht im Protokoll des Mandanten, mit ihr",
   log(VU) == [("anna", "tenant_admin", "denied")], log(VU))
r = queue("root", "sonstwas")
ok("unbekannte Aktion: abgewiesen, nichts gerufen", not r["ok"] and CALLS == [], r)
r = queue("root", "on")
ok("server_admin: dieselbe Funktion wie die Befehlszeile, mit Person und Rolle",
   r["ok"] and CALLS == [{"name": "vuebung-mcpgw", "off": False, "who": "root",
                          "role": "server_admin"}], (r, CALLS))
ok("... und die Seite bekommt ihren Satz", r["message"] == "the sentence of the CLI", r)
r = queue("root", "off")
ok("abschalten: dieselbe Funktion mit off", r["ok"] and CALLS[-1]["off"] is True, CALLS)
r = queue("root", "on", inst="gibt-es-nicht")
ok("fremde Instanz: abgewiesen", not r["ok"] and len(CALLS) == 2, r)

print("=== die Karte ===")
src = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
           encoding="utf-8").read()
tree = ast.parse(src)
card_src = next(template_src.resolve(tree, n.value) for n in tree.body
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "MCP_CARD")
CARD = Environment(autoescape=True).from_string(card_src)

TEN = {"t1": {"label": "vuebung", "idp": {"issuer": "http://kc/realms/vuebung",
                                          "client_id": "oaap-node", "connector": "auth"}},
       "t2": {"label": "hand", "idp": {"issuer": "https://x/realms/hand", "client_id": "c"}},
       "t3": {"label": "leer"}}


def gw(tenant="t1", **extra):
    r = {"tenant": tenant, "port": 8120, "mcp_sources": True, "mcp": {}}
    r.update(extra)
    return r


def src_inst(tenant="t1", **extra):
    r = {"tenant": tenant, "port": 8121, "mcp": {"path": "/mcp"},
         "container": "oaap-app-kp", "svc_port": 8000}
    r.update(extra)
    return r


INSTS = {"gw": gw(), "kp": src_inst(), "crm": {"tenant": "t1", "port": 8122},
         "kp-probe": src_inst(rehearsal={"of": "kp"}),
         "fremd-kp": src_inst("t2"), "fremd-gw": gw("t2")}


def render(name, insts=INSTS, admin=True):
    v = iv.mcp_view(name, insts[name], insts, TEN, server_admin=admin, default_tid="t1")
    return v, CARD.render(i={"key": name, "mcp": v})


v, h = render("crm")
ok("eine Instanz ohne MCP hat keine Karte", v is None and 'id="mcp"' not in h)
v, h = render("kp")
ok("Quelle: Pfad und Leser desselben Mandanten",
   "/mcp" in h and v["readers"] == ["gw"] and 'href="/instances/gw#mcp"' in h, v)
ok("KOEDER: das Gateway eines anderen Mandanten liest sie nicht", "fremd-gw" not in h)
v, h = render("gw")
ok("Gateway: die Quellen desselben Mandanten, ohne Generalprobe und Fremde",
   [q["name"] for q in v["sources"]] == ["kp"] and v["sources"][0]["door"]
   and "kp-probe" not in h and "fremd-kp" not in h, v["sources"])
ok("nicht eingerichtet: der Satz und der Knopf fuer den server_admin",
   "Nicht eingerichtet" in h and "Anmeldung einrichten" in h
   and 'action="/instances/gw/mcp-idp"' in h and 'value="on"' in h, h[-900:])
v, h = render("gw", admin=False)
ok("KOEDER: ein tenant_admin sieht den Stand, aber keinen Knopf",
   "Nicht eingerichtet" in h and "<form" not in h, h[-600:])
on = dict(INSTS, gw=gw(mcp_idp={"connector": "auth", "space": "vuebung",
                                "issuer": "http://kc/realms/vuebung",
                                "audience": "oaap-mcp-gw", "signin": "oaap-mcp-gw-login",
                                "platform_audience": "oaap-node",
                                "at": "2026-11-07T18:00:00Z", "by": "root",
                                "secret": "DARF-NIE-ERSCHEINEN"}))
v, h = render("gw", on)
ok("eingerichtet: Issuer, beide Clients, wer und wann, Knopf zum Abschalten",
   "oaap-mcp-gw-login" in h and "http://kc/realms/vuebung" in h and "2026-11-07 18:00" in h
   and "Anmeldung abschalten" in h and 'value="off"' in h, h[-1200:])
ok("KOEDER: das Geheimnis erscheint nie, selbst wenn es im Datensatz stuende",
   "DARF-NIE-ERSCHEINEN" not in h and "secret" not in json.dumps(v))
off = dict(on, gw=dict(on["gw"], mcp_idp=dict(on["gw"]["mcp_idp"], off="2026-11-08T09:00:00Z",
                                              off_by="root")))
v, h = render("gw", off)
ok("abgeschaltet: deaktiviert, nicht geloescht, und der Knopf richtet wieder ein",
   "deaktiviert, nicht gelöscht" in h and "2026-11-08 09:00" in h and 'value="on"' in h, h[-900:])
v, h = render("fremd-gw")
ok("Realm von Hand eingetragen: kein Knopf, der Satz sagt warum",
   "<form" not in h and "von Hand" in h and v["can_manage"] is False, h[-500:])
insts3 = dict(INSTS, leer=gw("t3"))
v, h = render("leer", insts3)
ok("Mandant ohne Anmeldedienst: kein Knopf, der Weg", "<form" not in h
   and "oaap idp provision" in h, h[-500:])
v, h = render("kp-probe")
ok("Generalprobe: keine Quelle, kein Leser", v["rehearsal"] and "Generalprobe" in h
   and v["readers"] == [], h)

print("=== die Route ===")
route = src[src.index("def instance_mcp_idp(name):"):]
route = route[:route.index("\n@app.")]
ok("die Rolle wird gefragt, bevor etwas in den Spool geht",
   route.index('"server_admin" not in caller_roles()') < route.index("_queue_and_wait("), route[:300])
ok("nur on und off kommen in den Spool",
   route.index('op not in ("on", "off")') < route.index("_queue_and_wait("))

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
