#!/usr/bin/env python3
"""Status als erklärter Übergang (RFC-0120 Stufe 4, D7; oaap.data.twin 0.15 §2.27) gegen eine
echte PostgreSQL.

Ein Modellierungswerkzeug (`ea`) legt einen Typ Maschine mit Status an (geplant, aktiv,
Wartung, stillgelegt; stilllegen nur der Besitzer). Festgehalten wird: falsche Erklärungen
abgewiesen; Anfangsstatus ohne Zeile; ein Wert, den es nicht gibt, 400; ein Übergang, der nicht
erklärt ist, 409 mit den erlaubten Zielen; ein erklärter angehängt, gelesen mit dem Objekt und mit
früherem at der alte; derselbe Status noch einmal nichts; der Feed trägt von und nach; eine
Person im Portal, ein schreibender Client, der Besitzer -- je nach `by`; die Geschichte; das
Relais mit von, nach, Grund; die Erklärung wächst, ändert sich aber nicht; ein Typ ohne
Erklärung; Stapel und eingehende Webhooks mit derselben Regel; ein stillgelegtes Objekt.

Aufruf: TS_DSN=... python3 test/test_twin_status.py   (im Linux-Runner)
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
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-twin-status-"))

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


import relay      # noqa: E402

# ------------------------------------------------------------------ types
DECL = {"values": ["planned", "active", "maintenance", "retired"],
        "transitions": [{"from": "planned", "to": "active"},
                        {"from": "active", "to": "maintenance"},
                        {"from": "maintenance", "to": "active"},
                        {"from": "active", "to": "retired", "by": ["owner"]}]}
MACHINE = {"object_type": {"key": "Machine", "title": "Maschine", "status": DECL},
           "groups": [{"key": "ea.machine", "attributes": [{"key": "serial",
                                                            "value_type": "text"}]}]}
MT = "t.werk.Machine"
for bad, why in [(dict(DECL, initial="gone"), "initial"),
                 (dict(DECL, transitions=[{"from": "planned", "to": "x"}]), "planned -> x"),
                 (dict(DECL, transitions=[{"from": "planned", "to": "active", "by": ["boss"]}]),
                  "by"),
                 (dict(DECL, values=["Bad Value"]), "values")]:
    st, b = call("POST", "/twin/v1/types", data={"object_type": {"key": "Bad", "status": bad}})
    ok(f"1 KÖDER Erklärung mit falschem {why}: 400 mit Satz, nichts angelegt",
       st == 400 and why in err(b)
       and not q("SELECT 1 FROM oaap_model.type_definitions WHERE key = 't.werk.Bad'"), (st, b))
st, b = call("POST", "/twin/v1/types", data=MACHINE)
d = q("SELECT definition FROM oaap_model.type_definitions WHERE key = %s", MT)
d = json.loads(d[0]["definition"]) if d else {}
ok("2 der Typ mit Status: 201, Anfangswert der erste", st == 201
   and d.get("status", {}).get("initial") == "planned", (st, b, d))
st, b = call("POST", "/twin/v1/types", data={"object_type": {"key": "Server"},
                                              "groups": [{"key": "ea.server"}]})
CLIENTS["ops"] = {"tenant": "s1", "reads": [MT], "writes": [MT]}
save()

st, b = call("PUT", "/twin/v1/objects/by-key/M-1", data={
    "type": MT, "title": "Presse 1", "groups": {"ea.machine": {"attributes": {"serial": "P1"}}}})
M1 = b.get("id", "") if isinstance(b, dict) else ""
st, b = call("GET", f"/twin/v1/objects/{M1}")
ok("3 ein neues Objekt steht auf dem Anfangsstatus, nie gewechselt",
   b.get("status") == {"value": "planned", "since": None, "by": None}, b)
T0 = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
time.sleep(1.1)

# ------------------------------------------------------------------ changes
st, b = call("POST", f"/twin/v1/objects/{M1}/status", data={"to": "gone"})
ok("4 KÖDER ein Wert, den der Typ nicht hat: 400 nennt die Werte", st == 400
   and "maintenance" in err(b), (st, b))
ev0 = count("events")
st, b = call("POST", f"/twin/v1/objects/{M1}/status", data={"to": "maintenance"})
ok("5 KÖDER ein Übergang, der nicht erklärt ist: 409 nennt, wohin es geht, nichts angehängt",
   st == 409 and "active" in err(b) and count("statuses") == 0 and count("events") == ev0,
   (st, b))
st, b = call("POST", f"/twin/v1/objects/{M1}/status", data={"to": "active",
                                                             "reason": "abgenommen"})
ok("6 ein erklärter Übergang: angehängt mit von, nach, Zeit", st == 200
   and b.get("written") is True and b.get("from") == "planned" and b.get("to") == "active",
   (st, b))
st, b = call("GET", f"/twin/v1/objects/{M1}")
s1 = b.get("status") or {}
ok("7 mit dem Objekt gelesen: active, seit, von client:ea", s1.get("value") == "active"
   and s1.get("since") and s1.get("by") == "client:ea", b)
ev1 = count("events")
st, b = call("POST", f"/twin/v1/objects/{M1}/status", data={"to": "active"})
ok("8 derselbe Status noch einmal: nichts angehängt, kein Ereignis", st == 200
   and b.get("written") is False and count("statuses") == 1 and count("events") == ev1, (st, b))
st, b = call("GET", f"/twin/v1/objects/{M1}?at={T0}")
st2, h = call("GET", f"/twin/v1/objects/{M1}/status?at={T0}")
ok("9 mit früherem at: der Status von damals (planned), ohne Geschichte", st == 200
   and (b.get("status") or {}).get("value") == "planned" and h.get("history") == [], (b, h))
st, ch = call("GET", "/twin/v1/changes?since=0&types=" + MT)
sc = [c for c in ch.get("changes", []) if c["kind"] == "status.changed"]
ok("10 der Änderungs-Feed trägt status.changed mit von und nach",
   len(sc) == 1 and sc[0].get("status") == {"from": "planned", "to": "active"}, ch)

# ------------------------------------------------------------------ who may
rv = c.post(f"/internal/twin/objects/{M1}/status", headers=PERSON,
            json={"to": "maintenance", "reason": "Ölwechsel"})
ok("11 eine Person wechselt im Portal (persons): active -> maintenance", rv.status_code == 200
   and body(rv).get("to") == "maintenance", (rv.status_code, body(rv)))
rv = c.get(f"/internal/twin/objects/{M1}", headers=PERSON)
ps = body(rv).get("status", {}) if rv.status_code == 200 else {}
ok("12 die Objektseite: Status, erlaubte Ziele für die Person, Geschichte mit Grund",
   ps.get("value") == "maintenance" and ps.get("allowed") == ["active"]
   and ps.get("history", [{}])[0].get("reason") == "Ölwechsel"
   and ps["history"][0].get("recorded_by") == "jo", ps)
rv = c.post(f"/internal/twin/objects/{M1}/status", headers=PERSON, json={"to": "active"})
rv2 = c.post(f"/internal/twin/objects/{M1}/status", headers=PERSON, json={"to": "retired"})
ok("13 KÖDER die Person darf active -> retired nicht (nur owner): 403 nennt wer",
   rv.status_code == 200 and rv2.status_code == 403 and "owner" in body(rv2), body(rv2))
rv = c.post(f"/internal/twin/objects/{M1}/status", headers=dict(
    PERSON, **{"X-OAAP-Person-Roles": "user"}), json={"to": "maintenance"})
ok("14 KÖDER eine Person ohne Schreibrolle: 403", rv.status_code == 403, rv.status_code)
st, b = call("POST", f"/twin/v1/objects/{M1}/status", who="ops", data={"to": "retired"})
ok("15 KÖDER ein Client, der den Typ schreibt, aber nicht besitzt: 403 (writers)",
   st == 403 and "owner" in err(b), (st, b))
st, b = call("POST", f"/twin/v1/objects/{M1}/status", who="viewer", data={"to": "maintenance"})
ok("16 KÖDER ein Client ohne Leserecht: 404", st == 404, (st, b))
st, b = call("POST", f"/twin/v1/objects/{M1}/status", who="ops", data={"to": "maintenance"})
st2, b2 = call("POST", f"/twin/v1/objects/{M1}/status", who="ops", data={"to": "active"})
ok("17 der schreibende Client wechselt, was alle dürfen", st == 200 and st2 == 200, (b, b2))
st, b = call("POST", f"/twin/v1/objects/{M1}/status", data={"to": "retired",
                                                             "reason": "verschrottet"})
ok("18 der Besitzer: active -> retired", st == 200 and b.get("to") == "retired", (st, b))
st, h = call("GET", f"/twin/v1/objects/{M1}/status")
ok("19 die Geschichte, neueste zuerst, mit wer und warum",
   [x["to"] for x in h.get("history", [])] == ["retired", "active", "maintenance", "active",
                                                "maintenance", "active"]
   and h["history"][0]["reason"] == "verschrottet" and h["history"][0]["by"] == "client:ea",
   h)

# ------------------------------------------------------------------ the relay
row = q("SELECT e.id, e.recorded_at, e.kind, e.object_id, e.group_key, e.origin, o.type_key "
        "FROM twin_s1.events e JOIN twin_s1.objects o ON o.id = e.object_id "
        "WHERE e.kind = 'status.changed' ORDER BY e.id DESC LIMIT 1")[0]
rc = get_conn("s1")
msg = json.loads(relay.status_message(rc, row))
rc.close()
topic = relay.topic_for("s1", row["type_key"], row["object_id"], row["group_key"])
ok("20 das Relais: Thema .../<Objekt>/status, Nachricht mit von, nach, Grund",
   topic and topic.endswith(str(row["object_id"])) and msg.get("status") == {
       "from": "active", "to": "retired", "reason": "verschrottet"}, (topic, msg))

# ------------------------------------------------------------------ declaration grows
more = json.loads(json.dumps(MACHINE))
more["object_type"]["status"]["values"].append("archived")
more["object_type"]["status"]["transitions"].append({"from": "retired", "to": "archived"})
st, b = call("POST", "/twin/v1/types", data=more)
d = json.loads(q("SELECT definition FROM oaap_model.type_definitions WHERE key = %s",
                 MT)[0]["definition"])
ok("21 ergänzen geht: ein Wert und ein Übergang kommen dazu", st == 201
   and f"{MT} (status)" in b.get("created", []) and "archived" in d["status"]["values"]
   and len(d["status"]["transitions"]) == 5, (st, b))
st, b = call("POST", "/twin/v1/types", data=more)
ok("22 dieselbe Erklärung noch einmal: 200, nichts", st == 200 and b.get("created") == [],
   (st, b))
bad = json.loads(json.dumps(more))
bad["object_type"]["status"]["initial"] = "active"
st, b = call("POST", "/twin/v1/types", data=bad)
bad2 = json.loads(json.dumps(more))
bad2["object_type"]["status"]["transitions"][0]["by"] = ["persons"]
st2, b2 = call("POST", "/twin/v1/types", data=bad2)
ok("23 KÖDER Anfangswert oder 'by' ändern: 409", st == 409 and st2 == 409
   and "initial" in err(b) and "by" in err(b2), (st, b, st2, b2))

# ------------------------------------------------------------------ other doors
st, b = call("PUT", "/twin/v1/objects/by-key/S-1", data={
    "type": "t.werk.Server", "title": "srv1", "groups": {"ea.server": {}}})
S1 = b.get("id", "") if isinstance(b, dict) else ""
st, b = call("POST", f"/twin/v1/objects/{S1}/status", data={"to": "active"})
st2, b2 = call("GET", f"/twin/v1/objects/{S1}")
ok("24 KÖDER ein Typ ohne Erklärung: 409; sein Objekt hat keinen Status",
   st == 409 and "declares no status" in err(b) and "status" not in b2, (st, b, b2))
call("PUT", "/twin/v1/objects/by-key/M-2", data={
    "type": MT, "title": "Presse 2", "groups": {"ea.machine": {"attributes": {"serial": "P2"}}}})
st, b = call("POST", "/twin/v1/batch", data={"id": "st-1", "operations": [
    {"op": "status", "key": "M-2", "to": "active"},
    {"op": "status", "key": "M-2", "to": "planned"}]})
n_m2 = q("SELECT count(*) AS n FROM twin_s1.statuses s JOIN twin_s1.source_keys k "
         "ON k.object_id = s.object_id WHERE k.source_key = 'M-2'")[0]["n"]
ok("25 KÖDER im Stapel: der zweite Schritt nicht erklärt -> 400/409, auch der erste nicht "
   "angehängt", st == 409 and "operation 2" in err(b) and n_m2 == 0, (st, b))
st, b = call("POST", "/twin/v1/batch", data={"id": "st-2", "operations": [
    {"op": "status", "key": "M-2", "to": "active"}]})
ok("26 im Stapel über den eigenen Schlüssel", st == 200
   and b["results"][0].get("to") == "active", (st, b))
st, b = call("POST", "/twin/v1/receivers", data={
    "name": "mes", "signature": {"scheme": "token", "header": "X-Token"},
    "mapping": {"rules": [{"when": {"event": "state"},
                           "status": {"key": "machine", "to": "state",
                                      "reason": {"value": "vom Leitstand"}}}]}})
TOK = b.get("secret", "") if isinstance(b, dict) else ""
rv = c.post("/twin/v1/inbound/ea/mes", headers={"X-Token": TOK}, content_type="application/json",
            data=json.dumps({"event": "state", "machine": "M-2", "state": "maintenance"}))
rv2 = c.post("/twin/v1/inbound/ea/mes", headers={"X-Token": TOK},
             content_type="application/json",
             data=json.dumps({"event": "state", "machine": "M-2", "state": "planned"}))
st3, b3 = call("GET", "/twin/v1/objects/by-key/M-2")
ok("27 aus einer Zuordnung für eingehende Webhooks: gewechselt; ein nicht erklärter -> 422",
   st == 201 and rv.status_code == 200 and rv2.status_code == 422, (b, body(rv), body(rv2)))
call("POST", "/twin/v1/batch", data={"id": "st-3", "operations": [
    {"op": "retire", "key": "M-2"}]})
M2 = q("SELECT object_id FROM twin_s1.source_keys WHERE source_key = 'M-2'")[0]["object_id"]
st, b = call("POST", f"/twin/v1/objects/urn:oaap:obj:{M2}/status", data={"to": "active"})
ok("28 KÖDER ein stillgelegtes Objekt: kein Status mehr (404 aus dem Lesen oder 409)",
   st in (404, 409), (st, b))
rv = c.get("/twin/v1/openapi.json", headers=H("ea"))
ok("29 OpenAPI beschreibt den Status", "/objects/{id}/status" in rv.get_data(as_text=True),
   rv.status_code)
st, b = call("POST", "/twin/v1/webhooks", data={"url": "https://example.com/h",
                                                "kinds": ["status.changed"]})
ok("30 ein Webhook kann status.changed wählen", st in (201, 400) and "status.changed" not in
   err(b), (st, b))

print("")
if FAILED:
    print("rot: " + " ".join(FAILED))
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
