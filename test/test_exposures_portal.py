#!/usr/bin/env python3
"""Freigaben im Portal (oaap.net.connector 0.3 §2.8.10).

Bis hierher zeigte die Gesundheitsseite Freigaben nur an -- geoeffnet,
verlaengert, loslassen und geschlossen wurde nur an der Kommandozeile.
Hier steht, was der Knopf hinzufuegt:

- der Worker auf dem Host entscheidet -- Rolle --, nicht die Seite: der
  Spool ist Daten, kein Vertrauen. Also wird hier NICHT die Route
  geprueft, sondern der echte Worker mit gefaelschten Anfragen (wie
  test_destinations_portal.py);
- NUR server_admin oeffnet, verlaengert, laesst los oder schliesst --
  anders als beim Binden einer Destination gibt es hier keine
  Vorpruefung, der Knopf IST die Autorisierung eines beliebigen Ziels;
- ein Versuch ohne die Rolle aendert nichts und steht als 'denied' im
  Protokoll, mit dem Namen;
- lehnt der aeussere Knoten eine Anfrage ab, wird sie auf der inneren
  Seite sofort zurueckgenommen -- die Seite zeigt keine Anfrage, auf
  deren Antwort niemand mehr wartet;
- dieselben Funktionen wie an der Kommandozeile (tunnel_exposure_add /
  _extend / _remove, exposure_close_outer), damit die zwei Wege nicht
  auseinanderlaufen koennen.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_exposures_portal.py
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
DATA = tempfile.mkdtemp(prefix="oaap-exposures-portal-test-")
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
        print(f"      {str(detail)[:800]}")


# --- Attrappen ---------------------------------------------------------
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.docker_subnets = lambda net=None: []
m.container_env = lambda container: None
m.recreate_instance_containers = lambda name, *a, **kw: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
KUNDE, _ = m.tenant_create("kunde")


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
             user("uwe", ["user"], KUNDE)])

m.tunnel_connector_add("up", "https://oaap-test.example/",
                       "oaapc_" + "a" * 24, who="root", role="server_admin")

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, **req):
    """Eine Anfrage durch den echten Worker schicken."""
    _rid[0] += 1
    rid = f"r{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": "", "action": "exposure", "by": by, **req}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def audit():
    with open(m.TENANT_LOG, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def exposures_of(label="up"):
    return dict((m.load_connect()["connectors"][label].get("exposures")) or {})


print("=== wer nicht darf ===")
n_before = len(audit())
r = queue("uwe", op="open", connector="up", target="http://192.168.1.5:3000")
ok("ein Benutzer ohne Verwalterrolle oeffnet nichts",
   not r["ok"] and "server_admin" in r["message"], r)
ok("... nichts steht in connect.json", exposures_of() == {}, exposures_of())
lines = audit()
ok("... der Versuch steht als 'denied' im Protokoll, mit dem Namen",
   len(lines) == n_before + 1 and lines[-1]["result"] == "denied"
   and lines[-1]["who"] == "uwe", lines[-1] if lines else None)

r = queue("karin", op="open", connector="up", target="http://192.168.1.5:3000")
ok("ein tenant_admin oeffnet auch nichts -- anders als beim Binden gibt es "
   "hier keine Vorpruefung des Ziels",
   not r["ok"] and "server_admin" in r["message"], r)
ok("... auch er aendert connect.json nicht", exposures_of() == {}, exposures_of())

r = queue("root", op="umdrehen", connector="up", target="http://192.168.1.5:3000")
ok("eine unbekannte Aktion wird abgelehnt", not r["ok"] and "unknown" in r["message"], r)

print("")
print("=== oeffnen: der aeussere Knoten antwortet ===")
m._expose_wait = lambda label, ref, seconds=20: {
    "url": "https://k3f9x2mh4a.t.example.com/", "expires": "2026-10-04T12:00", "public": False}
r = queue("root", op="open", connector="up", target="http://192.168.1.5:3000", ttl="8h")
ok("server_admin oeffnet, und die Antwort des aeusseren Knotens steht drin",
   r["ok"] and r.get("detail", {}).get("url") == "https://k3f9x2mh4a.t.example.com/", r)
ok("... die Freigabe steht jetzt in connect.json", len(exposures_of()) == 1, exposures_of())
REF = next(iter(exposures_of()))
lines = [e for e in audit() if e["action"] == "connector.expose" and e["result"] == "ok"]
ok("... und genau eine ERFOLGREICHE Zeile im Protokoll, mit root und der Rolle server_admin"
   " (die zwei Versuche davor stehen als 'denied')",
   len(lines) == 1 and lines[0]["who"] == "root" and lines[0]["role"] == "server_admin"
   and lines[0]["subject"] == f"up/{REF}", lines)
ok("... die Adresse des Ziels steht in KEINER Protokollzeile",
   all("192.168.1.5" not in json.dumps(e) for e in audit()))

print("")
print("=== oeffnen: der aeussere Knoten lehnt ab ===")
m._expose_wait = lambda label, ref, seconds=20: {"error": "at most 10 exposures per connector"}
before = exposures_of()
r = queue("root", op="open", connector="up", target="http://10.0.0.9:8080", ttl="8h")
ok("eine Ablehnung des aeusseren Knotens ist kein Erfolg, aber ein Satz",
   not r["ok"] and "refused by the outer node" in r["message"], r)
ok("... die abgelehnte Anfrage wird nicht liegen gelassen",
   exposures_of() == before, exposures_of())

print("")
print("=== oeffnen: keine Antwort binnen 20 s ===")
m._expose_wait = lambda label, ref, seconds=20: {}
before = exposures_of()
r = queue("root", op="open", connector="up", target="http://10.0.0.9:8080", ttl="8h")
ok("keine Antwort ist kein Fehler -- die Anfrage bleibt, bis der Connector antwortet",
   r["ok"] and r.get("detail", {}).get("pending"), r)
ok("... und sie steht (noch ohne URL) in connect.json",
   len(exposures_of()) == len(before) + 1, exposures_of())
PENDING_REF = (set(exposures_of()) - set(before)).pop()

print("")
print("=== verlaengern und loslassen ===")
r = queue("uwe", op="extend", connector="up", ref=REF, ttl="1d")
ok("ein Benutzer ohne Verwalterrolle verlaengert nichts",
   not r["ok"] and "server_admin" in r["message"], r)
r = queue("root", op="extend", connector="up", ref=REF, ttl="1d")
ok("server_admin verlaengert", r["ok"] and "extended" in r["message"], r)
ok("... im Protokoll steht connector.extend",
   any(e["action"] == "connector.extend" and e["who"] == "root" for e in audit()))
r = queue("root", op="extend", connector="up", ref="x-unbekannt", ttl="1d")
ok("eine unbekannte Freigabe verlaengern wird mit einem Satz abgelehnt",
   not r["ok"] and "holds no exposure" in r["message"], r)

r = queue("uwe", op="unexpose", connector="up", ref=REF)
ok("ein Benutzer ohne Verwalterrolle laesst nichts los",
   not r["ok"] and REF in exposures_of(), r)
r = queue("root", op="unexpose", connector="up", ref=REF)
ok("server_admin laesst los, und sie ist weg aus connect.json",
   r["ok"] and REF not in exposures_of(), r)
ok("... die andere (angefragte, noch unbeantwortete) Freigabe bleibt unberuehrt",
   PENDING_REF in exposures_of(), exposures_of())
ok("... im Protokoll steht connector.unexpose",
   any(e["action"] == "connector.unexpose" and e["who"] == "root" for e in audit()))

print("")
print("=== schliessen (aeussere Seite) ===")
STATE_DIR = m.CONNECT_STATE_DIR
os.makedirs(STATE_DIR, exist_ok=True)
with open(os.path.join(STATE_DIR, "state.json"), "w", encoding="utf-8") as f:
    json.dump({"exposures": {"probe.t.oaap.example": {"tenant": KUNDE,
                                                       "host": "probe.t.oaap.example"}}}, f)
r = queue("uwe", op="close", name="probe.t.oaap.example")
ok("ein Benutzer ohne Verwalterrolle schliesst nichts", not r["ok"], r)
ok("... der Name bleibt fuer 'resume' offen",
   "probe.t.oaap.example" not in (m.load_connect().get("exposure_closed") or {}))
r = queue("root", op="close", name="unbekannt.t.oaap.example")
ok("ein unbekannter Name wird mit einem Satz abgelehnt",
   not r["ok"] and "no live exposure" in r["message"], r)
r = queue("root", op="close", name="probe.t.oaap.example")
ok("server_admin schliesst eine lebende Freigabe der AEUSSEREN Seite",
   r["ok"] and "closed" in r["message"], r)
ok("... der Name kann nicht wiederaufgenommen werden",
   "probe.t.oaap.example" in (m.load_connect().get("exposure_closed") or {}))
ok("... im Protokoll steht exposure.close.requested, mit root",
   any(e["action"] == "exposure.close.requested" and e["who"] == "root"
       and e["tenant"] == KUNDE for e in audit()))

print("")
print("=== die Route ===")
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
src = io.open(APP_PY, encoding="utf-8").read()
route = src.split('@app.post("/exposures")')[1].split("@app.post(")[0].split("@app.get(")[0]
ok("die Route prueft server_admin, bevor sie ueberhaupt fragt",
   '"server_admin" not in caller_roles()' in route)
ok("... und fragt den Worker, sie schreibt connect.json nicht selbst",
   '"action": "exposure"' in route and "save_connect" not in route)

print("")
print("=== die Seite zeigt den Knopf nur, wo gebunden werden darf ===")
tree = ast.parse(src)
body = next(ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "HEALTH_BODY" for t in n.targets))
frag = body.split('<div class="card" id="freigaben">')[1].split(
    '{% endif %}\n{% if reach and reach.rows %}')[0]
CARD = Environment(autoescape=True).from_string(frag)


def render(can_edit):
    cx = {"tunnels": [], "reported": True, "scheme": "https",
          "certs_week": 2, "certs_limit": 50, "can_edit": can_edit,
          "connectors": [{"label": "up", "endpoint": "https://oaap-test.example/",
                          "plain": False, "paused": False, "key": True,
                          "connected": True, "since": "2026-09-27T08:00",
                          "last_error": "", "next_attempt": "", "offers": [],
                          "exposures": [{"ref": "x-1", "target": "http://192.168.1.5:3000",
                                         "public": False, "url": "https://a.t.example.com/",
                                         "error": "", "ended": "", "by": "root",
                                         "expires": "2026-10-04 12:00"}]}],
          "exposures": [{"name": "probe.t.example.com", "host": "probe.t.example.com",
                         "tenant": "Kunde", "public": False, "by": "root",
                         "expires": "2026-10-01 00:00", "calls": 3, "connected": True}]}
    return CARD.render(cx=cx)


html_admin = render(True)
html_view = render(False)
ok("server_admin sieht das Formular zum Oeffnen und die Knoepfe Verlaengern/Beenden/Schliessen",
   'name="op" value="open"' in html_admin and 'value="extend"' in html_admin
   and 'value="unexpose"' in html_admin and 'value="close"' in html_admin, html_admin)
ok("wer nicht binden darf (support), sieht dieselben Zeilen, aber keinen Knopf",
   'value="open"' not in html_view and 'value="extend"' not in html_view
   and 'value="unexpose"' not in html_view and 'value="close"' not in html_view
   and "192.168.1.5" in html_view and "probe.t.example.com" in html_view, html_view)
ok("die Adresse des Ziels steht auf der Seite (fuer server_admin, der sie selbst gewaehlt hat)",
   "192.168.1.5" in html_admin)

print("")
print("PASS" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
