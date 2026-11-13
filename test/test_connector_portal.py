#!/usr/bin/env python3
"""Der Connector im Portal (0.1.277, oaap.net.connector 0.4 §2.9).

Bis hierher zeigte das Portal Tunnel nur an; Schluessel, Connectoren und
Angebote gab es nur an der Kommandozeile. Geprueft wird:

- der echte Worker entscheidet (der Spool ist Daten, kein Vertrauen):
  NUR server_admin stellt Schluessel aus, widerruft, legt Connectoren an,
  pausiert, entfernt, pflegt Angebote; ein Versuch ohne die Rolle aendert
  nichts und steht als 'denied' im Protokoll;
- dieselben Funktionen wie die Kommandozeile -- also dieselben Regeln
  (ein Angebot auf einen Plattformdienst wird abgelehnt);
- der ausgestellte Schluessel kommt EINMAL zurueck, in einer Ergebnisdatei
  mit 0600, und das Portal zeigt ihn aus der Antwort, nie in einer Adresse;
- ein unbekannter Mandant beendet den Worker nicht (resolve_tenant_arg
  ruft die();
- die Seite: Formulare nur fuer server_admin, support sieht nur, ein
  tenant_admin gar nicht; Widerrufen und Entfernen nur mit 'wirklich';
- das Destinationsformular schlaegt via:<tunnel>/<angebot> vor, nur fuer
  Tunnel DIESES Mandanten.

Aufruf: python3 test/test_connector_portal.py
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-connector-portal-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:800]}")


m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.docker_subnets = lambda net=None: []
m.container_env = lambda container: None
m.recreate_instance_containers = lambda name, *a, **kw: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
AIS, _ = m.tenant_create("ais")


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant, "groups": [], "active": True}


write_users([user("root", ["server_admin"]), user("karin", ["tenant_admin"], AIS)])

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, **req):
    _rid[0] += 1
    rid = f"c{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": "", "action": "connect-admin", "by": by, **req}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    p = os.path.join(m.SPOOL_DIR, "results", f"{rid}.json")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    with open(p, encoding="utf-8") as f:
        r = json.load(f)
    r["_mode"] = mode
    return r


def audit():
    try:
        with open(m.TENANT_LOG, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


print("=== Worker: wer nicht darf ===")
n0 = len(audit())
r = queue("karin", op="key-issue", label="oaapi01", tenant="ais")
ok("tenant_admin: kein Schluessel, abgelehnt", not r["ok"] and "server_admin" in r["message"]
   and not m.load_connect()["keys"], r)
ok("... und als 'denied' im Protokoll", any(e.get("result") == "denied" and e.get("who") == "karin"
                                            for e in audit()[n0:]), audit()[n0:])
r = queue("karin", op="add", label="x01", endpoint="https://oaap.example.org", key="oaapc_" + "b" * 30)
ok("tenant_admin: kein Connector", not r["ok"] and not m.load_connect()["connectors"], r)
r = queue("root", op="erfunden", label="x")
ok("unbekannte Aktion: abgelehnt", not r["ok"] and "unknown" in r["message"], r)

print("\n=== Worker: aeussere Seite ===")
r = queue("root", op="key-issue", label="oaapi01", tenant="erfunden")
ok("unbekannter Mandant: abgelehnt, der Worker laeuft weiter", not r["ok"] and "erfunden" in r["message"], r)
r = queue("root", op="key-issue", label="oaapi01", tenant="ais")
sec = (r.get("detail") or {}).get("secret", "")
ok("server_admin: Schluessel ausgestellt, einmal in der Antwort",
   r["ok"] and sec.startswith("oaapc_") and r["detail"]["tenant"] == "ais"
   and r["detail"]["label"] == "oaapi01", r)
rec = m.load_connect()["keys"].get("oaapi01") or {}
ok("gespeichert nur die Pruefsumme, im Mandanten ais",
   rec.get("tenant") == AIS and sec not in json.dumps(m.load_connect()), rec)
ok("die Ergebnisdatei mit dem Schluessel ist 0600", r["_mode"] == 0o600, oct(r["_mode"]))
r = queue("root", op="key-issue", label="oaapi01", tenant="ais")
ok("derselbe Name zweimal: abgelehnt (dieselbe Regel wie die Kommandozeile)",
   not r["ok"] and "exists" in r["message"], r)
r = queue("root", op="key-revoke", label="oaapi01")
ok("widerrufen", r["ok"] and "oaapi01" not in m.load_connect()["keys"], r)

print("\n=== Worker: innere Seite ===")
r = queue("root", op="add", label="x01", endpoint="https://oaap.example.org", key="kein-schluessel")
ok("ein Wert, der kein Schluessel ist: abgelehnt", not r["ok"] and "connect key" in r["message"], r)
r = queue("root", op="add", label="x01", endpoint="http://oaap.example.org", key="oaapc_" + "b" * 30)
ok("http:// statt https://: abgelehnt (aus dem Portal nie --plain)", not r["ok"], r)
r = queue("root", op="add", label="x01", endpoint="https://oaap.example.org", key="oaapc_" + "b" * 30)
ok("Connector angelegt", r["ok"] and "x01" in m.load_connect()["connectors"], r)
ok("der Schluessel steht nicht in der Ergebnisdatei", "oaapc_" + "b" * 30 not in json.dumps(r), r)
r = queue("root", op="offer-add", label="x01", offer="mg", to="http://portal:8000/")
ok("Angebot auf einen Plattformdienst: abgelehnt", not r["ok"] and "platform" in r["message"], r)
r = queue("root", op="offer-add", label="x01", offer="mg",
          to="https://mg.ais.oaap-demo.example/v1", path="", methods="GET,POST")
offers = m.load_connect()["connectors"]["x01"].get("offers") or {}
ok("Angebot angelegt, mit Methoden", r["ok"] and offers.get("mg", {}).get("to") == "https://mg.ais.oaap-demo.example/v1"
   and offers["mg"].get("methods") == ["GET", "POST"], (r, offers))
r = queue("root", op="pause", label="x01")
ok("pausiert", r["ok"] and m.load_connect()["connectors"]["x01"].get("paused"), r)
r = queue("root", op="resume", label="x01")
ok("fortgesetzt", r["ok"] and not m.load_connect()["connectors"]["x01"].get("paused"), r)
r = queue("root", op="set-key", label="x01", key="oaapc_" + "c" * 30)
ok("Schluessel ersetzt", r["ok"], r)
r = queue("root", op="offer-remove", label="x01", offer="mg")
ok("Angebot entfernt", r["ok"] and not m.load_connect()["connectors"]["x01"].get("offers"), r)
r = queue("root", op="remove", label="x01")
ok("Connector entfernt", r["ok"] and "x01" not in m.load_connect()["connectors"], r)


print("\n=== Portal ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal                                           # noqa: E402

CONF = {"keys": {"oaapi01": {"tenant": AIS}, "fremd": {"tenant": DEFAULT}}, "connectors": {}}
STATE = {"tunnels": {"oaapi01": {"connected": True, "offers": [{"name": "mg"}]},
                     "fremd": {"connected": True, "offers": [{"name": "erp"}]}}}
portal._connect_data = lambda: (CONF, STATE)
portal._load_destinations_all = lambda: {}
portal.load_tenants = lambda: {DEFAULT: {"label": "default"}, AIS: {"label": "ais"}}
portal.tenant_label = lambda tid: {DEFAULT: "default", AIS: "ais"}.get(tid, "")
portal.default_tenant_id = lambda: DEFAULT
portal.host_tenant_scope = lambda host: (AIS, True)
portal.caller_scope = lambda: ("server_admin", DEFAULT)
portal.multi_tenant = lambda: True
portal.node_profiles = lambda: []
portal.external_host = lambda: "oaap.example.org"
ok("via-Vorschlaege: nur die Tunnel DIESES Mandanten",
   portal._via_suggestions(AIS) == ["via:oaapi01/mg"], portal._via_suggestions(AIS))

c = portal.app.test_client()
HS = {"X-OAAP-User": "root", "X-OAAP-Roles": "server_admin"}
portal.caller_roles = lambda: {"server_admin"}
t = c.get("/connector", headers=HS).get_data(as_text=True)
ok("server_admin: Seite mit allen Formularen, Mandant des Orts vorgewaehlt",
   all(x in t for x in ('action="/connector/key-issue"', 'action="/connector/add"',
                         'action="/connector/key-revoke"', 'value="ais" selected',
                         "via:oaapi01/mg")), t[t.find("<main"):t.find("<main") + 600])
portal.caller_roles = lambda: {"support"}
t = c.get("/connector", headers=HS).get_data(as_text=True)
ok("support: sieht, aber kein Formular", "oaapi01" in t and 'action="/connector/' not in t)
portal.caller_roles = lambda: {"tenant_admin"}
ok("tenant_admin: kein Zugang", c.get("/connector", headers=HS).status_code == 403)
ok("... und kein Absenden", c.post("/connector/key-issue", headers=HS, data={"label": "x"}).status_code == 403)

portal.caller_roles = lambda: {"server_admin"}
portal._same_origin = lambda: True
sent = []
answer = {}
portal._queue_and_wait = lambda name, payload, wait: sent.append(payload) or dict(answer)
answer.update({"ok": True, "message": "key issued", "detail": {
    "secret": "oaapc_GEHEIM" + "x" * 30, "label": "oaapi01", "tenant": "ais",
    "endpoint": "https://oaap.example.org"}})
r = c.post("/connector/key-issue", headers=HS, data={"label": "oaapi01", "tenant": "ais"})
t = r.get_data(as_text=True)
ok("Ausstellen: der Schluessel steht auf der Antwortseite, nicht in einer Weiterleitung",
   r.status_code == 200 and "oaapc_GEHEIM" in t and "Location" not in r.headers
   and "--endpoint https://oaap.example.org" in t, (r.status_code, r.headers.get("Location")))
ok("... die Anfrage nennt Name und Mandant", sent[-1] == {"action": "connect-admin", "op": "key-issue",
                                                          "label": "oaapi01", "tenant": "ais"}, sent[-1:])
sent.clear()
r = c.post("/connector/key-revoke", headers=HS, data={"label": "oaapi01"})
ok("Widerrufen ohne 'wirklich': nichts in den Spool", sent == [] and "err=" in r.headers.get("Location", ""))
r = c.post("/connector/remove", headers=HS, data={"label": "x01"})
ok("Entfernen ohne 'wirklich': nichts in den Spool", sent == [])
answer.clear()
answer.update({"ok": True, "message": "connector 'x01' added"})
r = c.post("/connector/add", headers=HS, data={"label": "x01", "endpoint": "https://oaap.example.org",
                                               "key": "oaapc_" + "d" * 30})
ok("Anlegen: Schluessel geht in die Anfrage, nicht in die Weiterleitung",
   sent[-1].get("key") == "oaapc_" + "d" * 30 and "oaapc_" not in r.headers.get("Location", ""), r.headers)
r = c.post("/connector/offer-add", headers=HS, data={"label": "x01", "offer": "mg",
                                                     "to": "https://mg.ais.x/v1", "methods": "GET"})
ok("Angebot: Felder in der Anfrage", sent[-1].get("to") == "https://mg.ais.x/v1"
   and sent[-1].get("offer") == "mg" and sent[-1].get("methods") == "GET", sent[-1])
answer.clear()
answer.update({"ok": False, "message": "'portal' is a platform service or this machine"})
r = c.post("/connector/offer-add", headers=HS, data={"label": "x01", "offer": "mg", "to": "http://portal/"})
ok("eine Ablehnung des Workers kommt als Fehler auf die Seite",
   "err=" in r.headers.get("Location", "") and "platform" in portal.unquote(r.headers["Location"])
   if hasattr(portal, "unquote") else "err=" in r.headers.get("Location", ""), r.headers)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
