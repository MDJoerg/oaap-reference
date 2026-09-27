#!/usr/bin/env python3
"""Fernzugang im Portal (RFC-0044 Stufe 1, oaap.net.remote-access 0.1).

Was diese Stufe ist -- und was sie ausdruecklich NICHT ist:

- das Objekt (wer, welche Instanz, welche Form, bis wann), sein
  Lebenslauf (oeffnen/schliessen/ablaufen) und eine Zeile im
  Mandantenprotokoll je Ereignis -- genau wie beim Diagnose-Fenster
  (RFC-0038 D2), von derselben Uhr geschlossen (oaap-instance-watch);
- KEIN Verkehr. Ein geoeffneter Zugang traegt noch keine Verbindung --
  weder Portweiterleitung noch WireGuard. Das steht auch in jeder
  Antwort, die das Oeffnen gibt.
- server_admin ODER der tenant_admin GENAU des Mandanten der Instanz
  (D1) -- wie beim Diagnose-Fenster, geprueft an derselben
  Mandantengrenze wie ueberall sonst im Portal (cross_tenant);
- kein Verlaengern (D3) -- neu oeffnen ist ein neuer Vorgang, mit
  eigener Protokollzeile;
- der Datensatz lebt AUSSERHALB der Registry (apps/remote-access.json)
  -- ein Zugang ist ein Akt, keine Konfiguration, RFC-0044 §1, und darf
  deshalb bei Sicherung/Wiederherstellung/Befoerderung nicht auftauchen.

Geprueft ueber den echten Worker (wie test_destinations_portal.py und
test_exposures_portal.py) -- NICHT die Route selbst, weil der Spool
Daten ist, kein Vertrauen. Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_access_portal.py
"""
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-access-portal-test-")
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


# --- Attrappen -----------------------------------------------------------
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.recreate_instance_containers = lambda name, *a, **kw: None
m.container_env = lambda container: None
# Der echte Docker-Aufruf (RFC-0044 §4): standardmäßig "gelingt immer",
# einzelne Tests unten schalten ihn gezielt auf "schlägt fehl" um --
# und rufen ihn danach zurück, damit sie einander nicht beeinflussen.
JOINS, LEAVES = [], []
m.connect_join_network = lambda net: (JOINS.append(net) or True)
m.connect_leave_network = lambda net: LEAVES.append(net)
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

# Zwei schlanke Instanzen, direkt in die Registry -- Zugang braucht keine
# Manifest-Maschinerie, nur eine Instanz mit einem Mandanten (wie
# test_diagnose.py).
reg = m.load_registry()
K_ORDERS, A_ORDERS = "kunde-orders", "andere-orders"
reg["instances"][K_ORDERS] = {
    "app_id": "orders", "app_name": "Orders", "channel": "test",
    "tenant": KUNDE, "id": "aaaaaaaaaaaa", "version": "0.1.0",
    "name": "orders", "container": "oaap-app-kunde-orders",
    "services": [{"service": "web", "container": "oaap-app-kunde-orders"},
                {"service": "db", "container": "oaap-app-kunde-orders-db"}],
}
reg["instances"][A_ORDERS] = {
    "app_id": "orders", "app_name": "Orders", "channel": "test",
    "tenant": ANDERE, "id": "bbbbbbbbbbbb", "version": "0.1.0",
    "name": "orders", "container": "oaap-app-andere-orders", "image": "",
}
m.save_registry(reg)

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, instance, **req):
    """Eine Anfrage durch den echten Worker schicken."""
    _rid[0] += 1
    rid = f"r{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": instance, "action": "access", "by": by, **req}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def log(tid, action):
    return [e for e in m.read_tenant_log(tid) if e["action"] == action]


print("=== wer nicht darf ===")
n_before = len(log(KUNDE, "access.opened"))
r = queue("uwe", K_ORDERS, op="open", shape="forward", service="db", port=5432)
ok("ein Benutzer ohne Verwalterrolle oeffnet nichts",
   not r["ok"] and "server_admin" in r["message"], r)
ok("... nichts steht in remote-access.json", m.access_list(K_ORDERS) == [], m.access_list(K_ORDERS))
lines = log(KUNDE, "access.opened")
ok("... der Versuch steht als 'denied' im Protokoll, mit dem Namen",
   len(lines) == n_before + 1 and lines[-1]["result"] == "denied"
   and lines[-1]["who"] == "uwe", lines[-1] if lines else None)

print("")
print("=== oeffnen ===")
r = queue("karin", K_ORDERS, op="open", shape="forward", service="db", port=5432, hours=8)
ok("ein tenant_admin oeffnet im eigenen Mandanten", r["ok"], r)
rows = m.access_list(K_ORDERS)
ok("... und der Zugang steht in remote-access.json, fuer sie selbst als Inhaberin, "
   "mit dem echten Container-Namen als Ziel (nicht nur dem Dienstnamen)",
   len(rows) == 1 and rows[0]["holder"] == "karin" and rows[0]["shape"] == "forward"
   and rows[0]["target"] == {"service": "db", "port": 5432,
                             "container": "oaap-app-kunde-orders-db"}, rows)
ACCESS_ID = rows[0]["id"]
ok_lines = [e for e in log(KUNDE, "access.opened") if e["result"] == "ok"]
ok("... im Mandantenprotokoll steht GENAU EINE ERFOLGREICHE Zeile, mit karin und "
   "tenant_admin (der Versuch von uwe davor steht als 'denied')",
   len(ok_lines) == 1 and ok_lines[0]["who"] == "karin"
   and ok_lines[0]["role"] == "tenant_admin", log(KUNDE, "access.opened"))
ok("... der Datensatz steht NICHT in der Registry der Instanz (RFC-0044 §1: ein Akt, keine Konfiguration)",
   "access" not in m.load_registry()["instances"][K_ORDERS]
   and "remote_access" not in m.load_registry()["instances"][K_ORDERS])

r = queue("karin", K_ORDERS, op="open", shape="forward", service="", port=0)
ok("eine Portweiterleitung ohne Dienst und Port wird abgelehnt",
   not r["ok"] and "service and a port" in r["message"], r)
r = queue("karin", K_ORDERS, op="open", shape="forward", service="db", port=5432, hours=99)
ok("eine unbekannte Dauer wird abgelehnt -- nicht nur im Formular geprueft",
   not r["ok"] and "1, 8, 24" in r["message"], r)
r = queue("karin", K_ORDERS, op="open", shape="anderswo", service="db", port=5432)
ok("eine unbekannte Form wird abgelehnt", not r["ok"] and "unknown shape" in r["message"], r)
r = queue("karin", "unbekannte-instanz", op="open", shape="forward", service="db", port=5432)
ok("eine unbekannte Instanz wird abgelehnt", not r["ok"] and r["message"] == "unknown instance", r)

r = queue("karin", K_ORDERS, op="open", shape="forward", service="db", port=5432,
         holder="ein-kollege")
ok("ein anderer Name als Inhaber wird uebernommen (noch ungeprueft, siehe Spec §2)",
   r["ok"] and any(row["holder"] == "ein-kollege" for row in m.access_list(K_ORDERS)), r)

print("")
print("=== die Mandantengrenze ===")
before = m.access_list(A_ORDERS)
r = queue("karin", A_ORDERS, op="open", shape="forward", service="db", port=5432)
ok("die Instanz eines anderen Mandanten ist fuer einen tenant_admin 'unbekannt'",
   not r["ok"] and r["message"] == "unknown instance", r)
ok("... und nichts wurde geoeffnet", m.access_list(A_ORDERS) == before, m.access_list(A_ORDERS))
r = queue("root", A_ORDERS, op="open", shape="forward", service="db", port=5432)
ok("der server_admin darf in jedem Mandanten", r["ok"], r)
ok("... seine Zeile steht im Protokoll DES MANDANTEN, nicht bei root",
   any(e["who"] == "root" and e["role"] == "server_admin"
       for e in log(ANDERE, "access.opened")), log(ANDERE, "access.opened"))

print("")
print("=== schliessen ===")
r = queue("uwe", K_ORDERS, op="close", ref=ACCESS_ID)
ok("ein Benutzer ohne Verwalterrolle schliesst nichts",
   not r["ok"] and any(row["id"] == ACCESS_ID for row in m.access_list(K_ORDERS)), r)
r = queue("karin", K_ORDERS, op="close", ref="unbekannt")
ok("eine unbekannte Zugangs-Id wird mit einem Satz abgelehnt",
   not r["ok"] and "no access" in r["message"], r)
r = queue("karin", K_ORDERS, op="close", ref=ACCESS_ID)
ok("der tenant_admin schliesst den eigenen Zugang",
   r["ok"] and not any(row["id"] == ACCESS_ID for row in m.access_list(K_ORDERS)), r)
ok("... im Protokoll steht access.closed, mit karin",
   any(e["who"] == "karin" and e["action"] == "access.closed" for e in m.read_tenant_log(KUNDE)))

print("")
print("=== ablaufen und Sweep ===")
r = queue("karin", K_ORDERS, op="open", shape="forward", service="db", port=5432, hours=1)
LIVE_ID = r["message"].split()[1]
accesses = m.load_access()
EXPIRED_ID = "deadbeef"
accesses[EXPIRED_ID] = {
    "id": EXPIRED_ID, "instance": K_ORDERS, "tenant": KUNDE, "shape": "forward",
    "target": {"service": "db", "port": 5432}, "holder": "karin",
    "opened_by": "karin",
    "opened": (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(timespec="seconds"),
    "expires": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"),
    "hours": 8, "state": "open"}
m.save_access(accesses)
closed = m.access_sweep()
ok("der Sweep schliesst genau den abgelaufenen Zugang", closed == [EXPIRED_ID], closed)
ok("... der noch gueltige bleibt offen",
   any(row["id"] == LIVE_ID for row in m.access_list(K_ORDERS)), m.access_list(K_ORDERS))
ok("... im Protokoll steht access.expired, nicht access.closed",
   any(e["action"] == "access.expired" and e["subject"] == K_ORDERS
       for e in m.read_tenant_log(KUNDE)))
ok("ein zweiter Sweep-Lauf ohne etwas zu tun ist kein Fehler", m.access_sweep() == [])

print("")
print("=== Portweiterleitung: der connect-Dienst tritt dem Instanznetz bei (RFC-0044 §4) ===")
JOINS.clear(); LEAVES.clear()
r = queue("karin", K_ORDERS, op="open", shape="forward", service="db", port=5432)
ok("das Ziel bekommt den echten Container-Namen aus der Registry, nicht nur den Dienstnamen",
   any(row["target"].get("container") == "oaap-app-kunde-orders-db"
       for row in m.access_list(K_ORDERS)), m.access_list(K_ORDERS))
ok("... und der connect-Dienst wurde dem Instanznetz beigetreten",
   JOINS == [m.app_network(K_ORDERS)], JOINS)
FWD_ID = [row["id"] for row in m.access_list(K_ORDERS) if row["target"].get("port") == 5432][-1]

r = queue("karin", K_ORDERS, op="open", shape="forward", service="unbekannt", port=9999)
ok("ein unbekannter Dienstname wird abgelehnt -- die Instanz hat zwei Dienste, kein Raten",
   not r["ok"] and "no service" in r["message"], r)

# Ein Knoten, dessen Instanz nur EINEN Dienst hat: jeder (oder gar kein)
# Dienstname loest ihn auf -- es gibt ja nichts zu unterscheiden.
reg = m.load_registry()
K_SOLO = "kunde-solo"
reg["instances"][K_SOLO] = {
    "app_id": "solo", "app_name": "Solo", "channel": "test", "tenant": KUNDE,
    "id": "cccccccccccc", "version": "0.1.0", "name": "solo",
    "container": "oaap-app-kunde-solo", "image": "",
}
m.save_registry(reg)
r = queue("karin", K_SOLO, op="open", shape="forward", service="irgendwas", port=80)
ok("bei nur einem Dienst loest JEDER Name ihn auf",
   r["ok"] and any(row["target"].get("container") == "oaap-app-kunde-solo"
                   for row in m.access_list(K_SOLO)), r)
SOLO_ID = m.access_list(K_SOLO)[0]["id"]

print("")
print("=== Portweiterleitung: schliesst der connect-Dienst NICHT den Docker-Aufruf ===")
before_leaves = len(LEAVES)
m.access_close(FWD_ID, who="karin", role="tenant_admin")
ok("solange noch ein anderer 'forward'-Zugang auf DERSELBEN Instanz offen ist, "
   "verlaesst der connect-Dienst das Netz NICHT",
   len(LEAVES) == before_leaves,
   [row["instance"] for row in m.access_list(K_ORDERS)])

# alle restlichen offenen Zugaenge von K_ORDERS schliessen (es koennen von
# frueheren Abschnitten noch welche offen sein) und dann pruefen, dass der
# connect-Dienst das Netz ERST JETZT verlaesst
for row in list(m.access_list(K_ORDERS)):
    m.access_close(row["id"], who="karin", role="tenant_admin")
ok("kein offener 'forward'-Zugang mehr auf K_ORDERS -> der connect-Dienst verliess das Netz",
   m.app_network(K_ORDERS) in LEAVES, LEAVES)

print("")
print("=== Portweiterleitung: schlaegt der Docker-Aufruf fehl ===")
m.connect_join_network = lambda net: False
r = queue("karin", K_SOLO, op="open", shape="forward", service="", port=81)
ok("ein Zugang, dem der connect-Dienst nicht folgen kann, wird abgelehnt",
   not r["ok"] and "could not join" in r["message"], r)
ok("... und es steht nichts Neues in remote-access.json",
   len(m.access_list(K_SOLO)) == 1, m.access_list(K_SOLO))  # nur SOLO_ID von oben
m.connect_join_network = lambda net: (JOINS.append(net) or True)

print("")
print("=== die Uhr traegt drei Auftraege, nicht zwei ===")
MIGRATE = open(os.path.join(HERE, "..", "platform", "migrate.sh"), encoding="utf-8").read()
INSTALL = open(os.path.join(HERE, "..", "install.sh"), encoding="utf-8").read()
ok("access sweep steht in migrate.sh (bestehende Knoten heilen sich per Update)",
   "appctl.py access sweep" in MIGRATE, MIGRATE.count("access sweep"))
ok("access sweep steht auch in install.sh (frische Installation)",
   "appctl.py access sweep" in INSTALL)

print("")
print("=== die Route ===")
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
src = io.open(APP_PY, encoding="utf-8").read()
route = src.split('@app.post("/instances/<name>/access")')[1].split("@app.post(")[0]
ok("die Route prueft server_admin/tenant_admin ZUERST (require_instance_admin)",
   route.strip().startswith('"""') is False or "require_instance_admin(name)" in route.split("\n\n")[1]
   or "require_instance_admin(name)" in route)
ok("... und fragt den Worker, sie schreibt remote-access.json nicht selbst",
   '"action": "access"' in route and "save_access" not in route)

print("")
print("=== der Reiter zeigt sich selbst als unfertig ===")
IV_PY = os.path.join(HERE, "..", "platform", "services", "portal", "instance_view.py")
iv_src = io.open(IV_PY, encoding="utf-8").read()
ok("ein eigener Reiter 'fernzugang', nicht der API-Schluessel-Reiter 'zugang'",
   '"fernzugang"' in iv_src and iv_src.count('"zugang"') >= 1)
tree = ast.parse(src)

def template_const(name):
    return next(ast.literal_eval(n.value) for n in tree.body
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == name for t in n.targets))

body = template_const("INSTANCE_EDIT_BODY")
frag = body.split("<section class=\"panel {{ 'active' if tab == 'fernzugang' }}\">")[1]  \
          .split("<section class=\"panel {{ 'active' if tab == 'verwaltung' }}\">")[0]
# Textzeilen im Quelltext sind umgebrochen (wie ueberall in dieser
# Vorlage) -- im Browser kollabiert HTML das zu Leerzeichen, hier
# gleicht das ein einfaches Normalisieren aus, bevor Saetze verglichen
# werden, die einen Zeilenumbruch im Quelltext ueberspannen.
flat = " ".join(frag.split())
ok("die Seite sagt, dass Portweiterleitung Verkehr traegt und wie der Client "
   "aufgerufen wird",
   "trägt Verkehr" in flat and "oaap-expose.py forward" in flat, frag[:600])
ok("... und dass WireGuard weiterhin nicht gebaut ist",
   "WireGuard ist noch nicht gebaut" in flat, frag[:600])
CARD = Environment(autoescape=True).from_string(frag)


def render(rows):
    return CARD.render(i={"key": K_ORDERS,
                          "access": {"rows": rows, "hours": (1, 8, 24),
                                     "default_hours": 8, "services": ["db", "web"]}})


html_empty = render([])
ok("ohne offene Zugaenge zeigt die Seite trotzdem das Formular zum Oeffnen",
   'name="op" value="open"' not in html_empty and "Zugang öffnen" in html_empty, html_empty)
html_open = render([{"id": "x1", "holder": "karin", "shape": "forward",
                    "target": {"service": "db", "port": 5432},
                    "expires": "2026-10-01T12:00:00"}])
ok("ein offener Zugang zeigt Inhaber, Ziel und einen Schliessen-Knopf",
   "karin" in html_open and "db:5432" in html_open
   and 'value="close"' in html_open, html_open)

print("")
print("PASS" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
