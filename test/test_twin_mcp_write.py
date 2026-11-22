#!/usr/bin/env python3
"""Schreibende MCP-Werkzeuge des Zwillings (RFC-0120 Stufe 5, oaap.data.twin 0.16 §2.28) gegen
eine echte PostgreSQL.

Eine Person (kim, keyuser) schreibt über die MCP-Tür des Zwillings -- so, wie ein Gateway sie
nach der Bestätigung im Chat ruft: Werte in eine Mandantengruppe, einen Wert mit Zeitraum, einen
Statuswechsel. Festgehalten wird: als die Person (Herkunft, Anmeldename, Ereignis), mit den
Regeln des Portals (fremde Gruppe, Wertprüfung, periodisch, erklärter Übergang, by, stillgelegt);
nur mit Schreibrolle; nie als Maschine; die Portal-Routen schreiben weiter wie vorher.

Aufruf: TS_DSN=... python3 test/test_twin_mcp_write.py   (im Linux-Runner)
"""
import datetime
import http.server
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-twin-mcpw-"))

fails = 0
FAILED = []


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    if not cond:
        FAILED.append(label.split(" ")[0])
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or detail == "" else f"  -- {str(detail)[:900]}"))


DSN = os.environ.get("TS_DSN", "")
if not DSN:
    print("SKIP  TS_DSN fehlt -- braucht eine echte PostgreSQL (bash test/runner/run.sh)")
    sys.exit(2)
try:
    import flask  # noqa: F401
    import psycopg2
    import psycopg2.extras
except ImportError as exc:
    print(f"SKIP  {exc}")
    sys.exit(2)

import appctl as a                  # noqa: E402
import app as twin                  # noqa: E402
import hooks                        # noqa: E402
import twin_read as tr              # noqa: E402

tr.FEED_LAG_S = 0                   # the test does not wait for late commits

# ------------------------------------------------------------ the database
DDL = []
real = a._store_psql
a._store_psql = lambda sql, *x, **k: DDL.append(sql) or type("R", (), {"stdout": "", "returncode": 0})()
a._model_ensure_schema()
a._twin_ensure_tables("twin_s1")
a._twin_ensure_tables("twin_s2")
a._store_psql = real
admin = psycopg2.connect(DSN)
admin.autocommit = True
with admin.cursor() as cur:
    for sch in ("oaap_model", "twin_s1", "twin_s2"):
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    for sch in ("twin_s1", "twin_s2"):
        cur.execute(f'CREATE SCHEMA "{sch}"')
        cur.execute(f"""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{sch}')
                       THEN CREATE ROLE "{sch}" LOGIN; END IF; END $$""")
    for sql in DDL:
        cur.execute(sql)


def q(sql, *args):
    with admin.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        try:
            return cur.fetchall()
        except psycopg2.ProgrammingError:
            return None


def typ(key, kind, d, origin="app:esg", tenant="s1"):
    q("INSERT INTO oaap_model.type_definitions (key, kind, origin, package, version, definition) "
      "VALUES (%s, %s, %s, 'esg', '1.0.0', %s)", key, kind, origin, json.dumps(d))
    q("INSERT INTO oaap_model.activations (tenant_id, type_key) VALUES (%s, %s)", tenant, key)


typ("Site", "object_types", {"key": "Site", "title": "Standort"})
typ("employees", "attribute_types", {"key": "employees", "value_type": "int"})
typ("esg.core", "group_types", {"key": "esg.core", "on": "Site", "attributes": ["employees"]})
q("INSERT INTO oaap_model.bindings (instance, type_key, direction, role) "
  "VALUES ('esg', 'Site', 'contributes', 'owner')")

# ------------------------------------------------------------- the service
APPS = os.path.join(os.environ["OAAP_DATA_DIR"], "platform-apps")
os.makedirs(APPS, exist_ok=True)
twin.REGISTRY_FILE = os.path.join(APPS, "registry.json")
twin.CLIENTS_FILE = os.path.join(APPS, "twin-clients.json")
twin.READERS_FILE = os.path.join(APPS, "twin-readers.json")
twin.TENANTS_FILE = os.path.join(APPS, "tenants.json")
CLIENTS = {"ea": {"tenant": "s1", "reads": [], "models": True},
           "viewer": {"tenant": "s1", "reads": ["Site"]},
           "dash": {"tenant": "s1", "reads": [], "writes": ["Site"]},
           "fremd": {"tenant": "s2", "reads": [], "models": True}}
TENANTS = {"s1": {"label": "werk"}, "s2": {"label": "anders"}}


def save():
    with open(twin.CLIENTS_FILE, "w") as f:
        json.dump({"clients": CLIENTS}, f)
    with open(twin.TENANTS_FILE, "w") as f:
        json.dump({"tenants": TENANTS}, f)      # the host's shape (appctl.save_tenants)


with open(twin.REGISTRY_FILE, "w") as f:
    json.dump({"instances": {"esg": {"tenant": "s1", "app_id": "esg"}}}, f)
save()


def get_conn(tid):
    return psycopg2.connect(DSN, options=f"-c search_path=twin_{tid}",
                            cursor_factory=psycopg2.extras.RealDictCursor)


twin.get_conn = get_conn
c = twin.app.test_client()
twin.INTERNAL_KEY = "k"
PERSON = {"X-OAAP-Person-User": "jo", "X-OAAP-Person-Tenant": "s1",
          "X-OAAP-Person-Roles": "tenant_admin", twin.INTERNAL_HEADER: "k"}


def H(who):
    return {"X-OAAP-User": f"client:{who}"}


def body(rv):
    try:
        b = rv.get_json()
    except Exception:
        b = None
    return b if b is not None else rv.get_data(as_text=True)


def call(method, path, who="ea", data=None, headers=None):
    rv = c.open(path, method=method, headers=headers or H(who), json=data)
    return rv.status_code, body(rv)


def err(b):
    return b.get("error", "") if isinstance(b, dict) else str(b)


def count(table, schema="twin_s1"):
    return q(f"SELECT count(*) AS n FROM {schema}.{table}")[0]["n"]


# ------------------------------------------------------------------ setup
APP_TYPES = {
    "object_type": {"key": "Machine", "title": "Maschine", "status": {
        "values": ["planned", "active", "retired"],
        "transitions": [{"from": "planned", "to": "active"},
                        {"from": "active", "to": "retired", "by": ["owner"]}]}},
    "groups": [{"key": "ea.machine", "attributes": [
        {"key": "serial", "value_type": "text"},
        {"key": "crit", "value_type": "enum", "values": ["low", "high"]}]}]}
AT = "t.werk.Machine"
st, b = call("POST", "/twin/v1/types", data=APP_TYPES)
ok("0 Vorbereitung: Typ des Werkzeugs mit Status", st == 201, (st, b))
typ("note", "attribute_types", {"key": "note", "value_type": "text"}, origin="tenant:s1")
typ("level", "attribute_types", {"key": "level", "value_type": "enum",
                                 "values": ["ok", "warn"]}, origin="tenant:s1")
typ("co2e", "attribute_types", {"key": "co2e", "value_type": "decimal", "periodic": True,
                                "unit": "t"}, origin="tenant:s1")
typ("werk.betrieb", "group_types", {"key": "werk.betrieb", "on": AT,
                                    "attributes": ["note", "level", "co2e"]}, origin="tenant:s1")
st, b = call("PUT", "/twin/v1/objects/by-key/M-1", data={
    "type": AT, "title": "Presse 1", "groups": {"ea.machine": {"attributes": {"serial": "P1"}}}})
M1 = b.get("id", "")
st, b = call("PUT", "/twin/v1/objects/by-key/M-2", data={
    "type": AT, "title": "Presse 2", "groups": {"ea.machine": {"attributes": {"serial": "P2"}}}})
M2 = b.get("id", "")


def mcp(name, args, roles="keyuser", user="kim"):
    rv = c.post("/mcp", headers={"X-OAAP-Twin-Tenant": "s1", "X-OAAP-User": user,
                                 "X-OAAP-Roles": roles},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": args}})
    r = (rv.get_json() or {}).get("result") or {}
    text = (r.get("content") or [{}])[0].get("text", "")
    return r.get("isError"), text


# ------------------------------------------------------------------ twin_set_group
e, t = mcp("twin_set_group", {"id": M1, "group": "werk.betrieb",
                              "attributes": {"note": "Bitte nicht abschalten", "level": "warn"}})
row = q("SELECT g.origin, g.created_by FROM twin_s1.groups g WHERE g.group_key = 'werk.betrieb'")
ev = q("SELECT origin FROM twin_s1.events WHERE kind = 'group.written' AND group_key = "
       "'werk.betrieb' ORDER BY id DESC LIMIT 1")
ok("1 twin_set_group: Werte in die Mandantengruppe, Herkunft tenant, angelegt von kim, "
   "Ereignis tenant:kim", e is False and row and row[0]["origin"] == "tenant"
   and row[0]["created_by"] == "kim" and ev and ev[0]["origin"] == "tenant:kim", (e, t, row, ev))
rv = c.get(f"/internal/twin/objects/{M1}", headers=PERSON)
g = body(rv).get("groups", {}).get("werk.betrieb", {}).get("attributes", {})
ok("2 die Objektseite im Portal zeigt, was über MCP geschrieben wurde",
   (g.get("note") or {}).get("value") == "Bitte nicht abschalten", g)
e, t = mcp("twin_set_group", {"id": M1, "group": "ea.machine", "attributes": {"serial": "X"}})
ok("3 KÖDER in die Gruppe des Werkzeugs: abgelehnt mit dem Namen des Besitzers",
   e is True and "client:ea" in t, t)
e, t = mcp("twin_set_group", {"id": M1, "group": "werk.betrieb", "attributes": {"level": "rot"}})
ok("4 KÖDER ein Wert außerhalb der Aufzählung: der Satz der Wertprüfung", e is True
   and "level" in t, t)
e, t = mcp("twin_set_group", {"id": M1, "group": "werk.betrieb", "attributes": {"note": "x"}},
           roles="user")
ok("5 KÖDER eine Person nur mit Leserolle: Satz, nichts geschrieben", e is True
   and "schreiben" in t, t)
e, t = mcp("twin_set_group", {"id": M1, "group": "werk.betrieb", "attributes": {"note": "x"}},
           user="instance:esg")
ok("6 KÖDER eine Maschine statt einer Person", e is True and "Person" in t, t)

# ------------------------------------------------------------------ twin_add_period
e, t = mcp("twin_add_period", {"id": M1, "group": "werk.betrieb", "attribute": "co2e",
                               "value": "12,5", "valid_from": "2025-01-01",
                               "valid_to": "2025-12-31", "source": "Bericht 2025"})
p = q("SELECT value, recorded_by FROM twin_s1.periods WHERE attr_key = 'co2e'")
ok("7 twin_add_period: ein Zeitraum, Wert umgewandelt, von kim", e is False and p
   and float(json.loads(json.dumps(p[0]["value"]))) == 12.5 and p[0]["recorded_by"] == "kim",
   (e, t, p))
e, t = mcp("twin_add_period", {"id": M1, "group": "werk.betrieb", "attribute": "note",
                               "value": "x", "valid_from": "2025-01-01",
                               "valid_to": "2025-12-31"})
ok("8 KÖDER ein Attribut, das nicht periodisch ist: Satz", e is True and "periodic" in t, t)

# ------------------------------------------------------------------ twin_set_status
e, t = mcp("twin_set_status", {"id": M1, "to": "retired"})
ok("9 KÖDER ein Übergang, der nicht erklärt ist: Satz nennt 'active'", e is True
   and "active" in t, t)
e, t = mcp("twin_set_status", {"id": M1, "to": "active", "reason": "aus dem Chat"})
s1 = q("SELECT origin, recorded_by, reason FROM twin_s1.statuses ORDER BY id DESC LIMIT 1")
ok("10 twin_set_status: gewechselt, als die Person, mit Grund", e is False and s1
   and s1[0]["origin"] == "tenant:kim" and s1[0]["recorded_by"] == "kim"
   and s1[0]["reason"] == "aus dem Chat", (e, t, s1))
e, t = mcp("twin_set_status", {"id": M1, "to": "retired"})
ok("11 KÖDER active -> retired darf nur der Besitzer: Satz nennt owner", e is True
   and "owner" in t, t)

# ------------------------------------------------------------------ retired, portal
call("POST", f"/twin/v1/objects/{M2}/retire")
e, t = mcp("twin_set_group", {"id": M2, "group": "werk.betrieb", "attributes": {"note": "x"}})
rv = c.put(f"/internal/twin/objects/{M2}/groups/werk.betrieb", headers=PERSON,
           json={"attributes": {"note": "x"}})
ok("12 KÖDER ein stillgelegtes Objekt: über MCP ein Satz, im Portal 409", e is True
   and "retired" in t and rv.status_code == 409, (t, rv.status_code))
rv = c.put(f"/internal/twin/objects/{M1}/groups/werk.betrieb", headers=PERSON,
           json={"attributes": {"note": "aus dem Portal"}})
ok("13 der Portalweg schreibt weiter wie vorher (204)", rv.status_code == 204,
   (rv.status_code, rv.get_data(as_text=True)))
rv = c.post("/mcp", headers={"X-OAAP-Twin-Tenant": "s1", "X-OAAP-User": "kim",
                             "X-OAAP-Roles": "keyuser"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
names = [x["name"] for x in rv.get_json()["result"]["tools"]]
ok("14 tools/list nennt die drei schreibenden Werkzeuge", {"twin_set_group", "twin_add_period",
                                                          "twin_set_status"} <= set(names),
   names)
e, t = mcp("twin_set_group", {"id": M1, "group": "werk.betrieb", "attributes": {"note": "y"}})
rv = c.post("/mcp", headers={"X-OAAP-User": "kim", "X-OAAP-Roles": "keyuser"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "twin_set_group", "arguments": {}}})
ok("15 KÖDER ohne den Mandanten der Gateway-Tür: 403", rv.status_code == 403, rv.status_code)

print("")
if FAILED:
    print("rot: " + " ".join(FAILED))
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
