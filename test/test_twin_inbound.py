#!/usr/bin/env python3
"""Eingehende Webhooks: Empfänger mit Zuordnung (RFC-0120 D13, oaap.data.twin 0.14 §2.26)
gegen eine echte PostgreSQL.

Ein Modellierungswerkzeug, das nur Webhooks kann, schickt seine Änderungen an
einen Empfänger des Clients `ea`. Die Zuordnung macht daraus einen Stapel mit
den Rechten und der Herkunft des Clients. Festgehalten wird:

- Anmelden: nur mit `writes`/`models`; Name, Signatur, Zuordnung geprüft; ein
  Typ, den der Client nicht schreibt, schon beim Anmelden abgewiesen; das
  Geheimnis einmal, nie in der Liste; höchstens fünf; Ersetzen behält das
  Geheimnis;
- Signatur: oaap (Zeitstempel), hmac-sha256 mit Präfix, token; gefälscht,
  fehlend, veraltet -> 401, nichts geschrieben, als abgewiesen gezählt;
- Zuordnung: Liste im Rumpf, erste passende Regel, Hülle mit `$.`, fehlender
  Pfad lässt das Attribut weg, Beziehung über den Schlüssel, Stilllegen; ein
  Eintrag ohne Regel übersprungen; ohne Schlüssel 422;
- dieselbe Zustellung noch einmal: `replayed`, nichts geschrieben;
- ein falscher Wert: 422, nichts von der Zustellung geschrieben, der Satz am
  Empfänger und auf der Portalseite;
- Grenzen: ein fremder Client-Name 404, ein entfernter Client 404, ein Client
  ohne Schreibrecht schreibt auch über den Empfänger nichts.

Aufruf: TS_DSN=... python3 test/test_twin_inbound.py   (im Linux-Runner)
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
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-twin-inbound-"))

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


import hashlib    # noqa: E402
import hmac       # noqa: E402

# ------------------------------------------------------------------ types
APP_TYPES = {
    "object_type": {"key": "Application", "title": "Anwendung"},
    "groups": [{"key": "ea.app", "title": "Steckbrief",
                "attributes": [{"key": "appName", "value_type": "text"},
                               {"key": "criticality", "value_type": "enum",
                                "values": ["low", "high"]},
                               {"key": "cost", "value_type": "decimal", "unit": "EUR"}],
                "relations": [{"key": "dependsOn", "title": "hängt ab von",
                               "to": "Application"}]}]}
AT = "t.werk.Application"
st, b = call("POST", "/twin/v1/types", data=APP_TYPES)
ok("0 Vorbereitung: der Typ des Werkzeugs", st == 201, (st, b))

MAPPING = {
    "id": {"header": "X-Delivery"},
    "items": "changes",
    "rules": [
        {"when": {"$.event": ["created", "updated"], "kind": "app"},
         "upsert": {"key": "id", "type": AT, "title": "name",
                    "groups": {"ea.app": {
                        "attributes": {"appName": "name", "cost": "cost.amount",
                                       "criticality": "crit"},
                        "relations": [{"key": "dependsOn", "target_key": "parent"}]}}}},
        {"when": {"$.event": "deleted"},
         "retire": {"key": "id", "reason": {"value": "im Werkzeug gelöscht"}}}]}

# ------------------------------------------------------------------ register
st, b = call("POST", "/twin/v1/receivers", who="viewer", data={"name": "x", "mapping": MAPPING})
ok("1 KÖDER ein Client, der nur liest, legt keinen Empfänger an: 403", st == 403
   and "writes" in err(b), (st, b))
for bad, why in [({"name": "Bad Name", "mapping": MAPPING}, "name"),
                 ({"name": "x", "mapping": {"rules": []}}, "rules"),
                 ({"name": "x", "mapping": {"rules": [{"upsert": {"key": "id"},
                                                       "retire": {"key": "id"}}]}}, "exactly"),
                 ({"name": "x", "mapping": {"rules": [{"upsert": {"key": "id",
                                                                  "type": "Site"}}]}}, "Site"),
                 ({"name": "x", "mapping": {"rules": [{"upsert": {"key": "id", "groups": {
                     "ea.app": {"attributes": {"appName": {"code": "x"}}}}}}]}}, "path"),
                 ({"name": "x", "mapping": {"id": {"value": "1"}, "rules": [
                     {"retire": {"key": "id"}}]}}, "constant"),
                 ({"name": "x", "signature": {"scheme": "md5"}, "mapping": MAPPING}, "scheme")]:
    st, b = call("POST", "/twin/v1/receivers", data=bad)
    ok(f"2 KÖDER Anmeldung mit falschem {why}: 400 mit Satz", st == 400 and why in err(b),
       (st, b))
st, b = call("POST", "/twin/v1/receivers", data={"name": "ea-tool", "mapping": MAPPING})
REC = b if isinstance(b, dict) else {}
SECRET = REC.get("secret", "")
ok("3 Empfänger angelegt: 201, URL und Geheimnis einmal", st == 201 and len(SECRET) > 30
   and REC.get("url") == "/twin/v1/inbound/ea/ea-tool", (st, b))
st, b = call("POST", "/twin/v1/receivers", data={"name": "ea-tool", "mapping": MAPPING})
ok("4 KÖDER derselbe Name noch einmal: 409", st == 409, (st, b))
st, b = call("GET", "/twin/v1/receivers")
ok("5 die Liste nennt den Empfänger mit Zuordnung, nie das Geheimnis", st == 200
   and b["receivers"][0]["name"] == "ea-tool" and SECRET not in json.dumps(b)
   and "secret" not in b["receivers"][0], b)


def post(raw, headers=None, name="ea-tool", label="ea"):
    rv = c.post(f"/twin/v1/inbound/{label}/{name}", data=raw, headers=headers or {},
                content_type="application/json")
    return rv.status_code, body(rv)


def signed(doc, delivery=None, secret=None, ts=None):
    raw = json.dumps(doc).encode()
    ts = int(ts or time.time())
    mac = hmac.new((secret or SECRET).encode(), f"{ts}.".encode() + raw,
                   hashlib.sha256).hexdigest()
    h = {"X-OAAP-Signature": f"t={ts},v1={mac}"}
    if delivery:
        h["X-Delivery"] = delivery
    return raw, h


DOC1 = {"event": "created", "changes": [
    {"kind": "app", "id": "A-1", "name": "CRM", "cost": {"amount": "1200,5"}, "crit": "high"},
    {"kind": "app", "id": "A-2", "name": "ERP", "parent": "A-1"},
    {"kind": "comment"}]}

# ------------------------------------------------------------------ signature
n0 = count("objects")
raw, h = signed(DOC1, "d-1")
st, b = post(raw, {"X-OAAP-Signature": h["X-OAAP-Signature"].replace("v1=", "v1=0"),
                   "X-Delivery": "d-1"})
st2, _ = post(raw, {"X-Delivery": "d-1"})
raw_o, h_o = signed(DOC1, "d-1", ts=time.time() - 900)
st3, _ = post(raw_o, h_o)
st4, _ = post(raw.replace(b"CRM", b"CRX"), h)
ok("6 KÖDER gefälscht, fehlend, veraltet, Rumpf verändert: 401, nichts geschrieben",
   (st, st2, st3, st4) == (401, 401, 401, 401) and count("objects") == n0, (st, st2, st3, st4))
r = q("SELECT refused, received FROM twin_s1.receivers WHERE name = 'ea-tool'")[0]
ok("7 als abgewiesen gezählt, nicht als empfangen", r["refused"] == 4 and r["received"] == 0, r)

# ------------------------------------------------------------------ delivery
st, b = post(raw, h)
ok("8 die Zustellung: 200, zwei Schritte, ein Eintrag ohne Regel übersprungen", st == 200
   and b.get("operations") == 2 and b.get("skipped") == 1 and b.get("replayed") is False,
   (st, b))
st, crm = call("GET", "/twin/v1/objects/by-key/A-1")
st2, erp = call("GET", "/twin/v1/objects/by-key/A-2")
crm = crm if isinstance(crm, dict) else {}
erp = erp if isinstance(erp, dict) else {}
print("   CRM:", json.dumps(crm)[:600])
gc = (crm.get("groups") or {}).get("ea.app", {})
ge = (erp.get("groups") or {}).get("ea.app", {})
cost = (gc.get("attributes") or {}).get("cost")
cost = cost.get("value") if isinstance(cost, dict) else cost
ok("9 Objekte unter dem Schlüssel des Clients, Wert umgewandelt (1200,5 -> 1200.5), "
   "Beziehung über den Schlüssel", st == 200 and st2 == 200 and crm.get("title") == "CRM"
   and cost is not None and float(cost) == 1200.5
   and crm.get("id", "?") in json.dumps(ge.get("relations")), (crm, erp))
ok("10 ein fehlender Pfad lässt das Attribut weg (ERP ohne cost)",
   "cost" not in (ge.get("attributes") or {}), ge)
orig = q("SELECT DISTINCT owner_origin AS origin FROM twin_s1.objects WHERE type_key = %s", AT)
ok("11 Besitzer und Herkunft client:ea", [x["origin"] for x in orig] == ["client:ea"], orig)
ev0 = count("events")
st, b = post(raw, h)
ok("12 dieselbe Zustellung noch einmal: replayed, kein Ereignis", st == 200
   and b.get("replayed") is True and count("events") == ev0, (st, b))
raw2, h2 = signed({"event": "deleted", "changes": [{"id": "A-2"}]})
st, b = post(raw2, h2)
st2, _x = call("GET", "/twin/v1/objects/by-key/A-2")
ok("13 Ereignis aus der Hülle ($.event) -> stillgelegt; ohne Kennung zählt der Hash des Rumpfs",
   st == 200 and b.get("operations") == 1 and st2 == 404, (st, b, st2))
st, b = post(raw2, h2)
ok("14 derselbe Rumpf ohne Kennung noch einmal: replayed", st == 200
   and b.get("replayed") is True, (st, b))

# ------------------------------------------------------------------ refusals
n1, ev1 = count("objects"), count("events")
raw3, h3 = signed({"event": "created", "changes": [
    {"kind": "app", "id": "A-3", "name": "Wiki"},
    {"kind": "app", "id": "A-4", "name": "Mail", "crit": "mittel"}]}, "d-3")
st, b = post(raw3, h3)
ok("15 KÖDER ein falscher Wert im zweiten Eintrag: 422 nennt ihn, auch der erste nicht "
   "geschrieben", st == 422 and "operation 2" in err(b) and count("objects") == n1
   and count("events") == ev1, (st, b))
r = q("SELECT last_error FROM twin_s1.receivers WHERE name = 'ea-tool'")[0]
rv = c.get("/internal/twin/receivers", headers=PERSON)
pr = body(rv).get("receivers", [{}])[0] if rv.status_code == 200 else {}
ok("16 der Satz steht am Empfänger und auf der Portalseite, ohne Geheimnis",
   "operation 2" in (r["last_error"] or "") and pr.get("state") == "failing"
   and SECRET not in rv.get_data(as_text=True), (r, pr))
raw4, h4 = signed({"event": "created", "changes": [{"kind": "app", "name": "ohne"}]}, "d-4")
st, b = post(raw4, h4)
ok("17 KÖDER ein Eintrag ohne Schlüssel: 422", st == 422 and "no key" in err(b), (st, b))
raw5, h5 = signed({"event": "created", "changes": [{"kind": "app", "id": "A-5", "name": "X",
                                                    "parent": "GIBTSNICHT"}]}, "d-5")
st, b = post(raw5, h5)
ok("18 KÖDER Beziehung auf einen unbekannten Schlüssel: 422", st == 422
   and "GIBTSNICHT" in err(b), (st, b))
ts = int(time.time())
junk = b"kein json"
mac = hmac.new(SECRET.encode(), f"{ts}.".encode() + junk, hashlib.sha256).hexdigest()
st, b = post(junk, {"X-OAAP-Signature": f"t={ts},v1={mac}"})
ok("19 kein JSON, richtig signiert: 400", st == 400, (st, b))

# ------------------------------------------------------------------ replace, other schemes
st, b = call("PUT", "/twin/v1/receivers/ea-tool", data={
    "signature": {"scheme": "hmac-sha256", "header": "X-Hub-Signature-256", "prefix": "sha256="},
    "mapping": {"rules": [{"upsert": {"key": "id", "title": "name"}}]}})
ok("20 Zuordnung und Signatur ersetzt, das Geheimnis bleibt", st == 200
   and b["signature"]["scheme"] == "hmac-sha256" and "secret" not in b
   and b["state"] == "active", (st, b))
raw6 = json.dumps({"id": "A-1", "name": "CRM neu"}).encode()
sig6 = "sha256=" + hmac.new(SECRET.encode(), raw6, hashlib.sha256).hexdigest()
st, b = post(raw6, {"X-Hub-Signature-256": sig6})
st2, crm2 = call("GET", "/twin/v1/objects/by-key/A-1")
ok("21 hmac-sha256 im Stil von GitHub/Forgejo: ein Eintrag ohne items, umbenannt",
   st == 200 and b.get("operations") == 1 and crm2.get("title") == "CRM neu", (st, b, crm2))
st, b = post(raw6, {"X-Hub-Signature-256": sig6[:-1] + ("0" if sig6[-1] != "0" else "1")})
ok("22 KÖDER hmac-sha256 falsch: 401", st == 401, (st, b))
st, b = call("POST", "/twin/v1/receivers", data={
    "name": "tok", "signature": {"scheme": "token", "header": "X-Token"},
    "mapping": {"rules": [{"upsert": {"key": "id", "title": "name"}}]}})
TOK = b.get("secret", "") if isinstance(b, dict) else ""
st1, _ = post(json.dumps({"id": "A-1", "name": "CRM"}).encode(), {"X-Token": TOK}, name="tok")
st2, _ = post(json.dumps({"id": "A-1", "name": "CRM"}).encode(), {"X-Token": "x"}, name="tok")
ok("23 token: richtig 200, falsch 401", st == 201 and st1 == 200 and st2 == 401,
   (st, b, st1, st2))
for i in range(3):
    call("POST", "/twin/v1/receivers", data={"name": f"r{i}", "mapping": MAPPING})
st, b = call("POST", "/twin/v1/receivers", data={"name": "r9", "mapping": MAPPING})
ok("24 KÖDER mehr als fünf Empfänger: 409", st == 409, (st, b))

# ------------------------------------------------------------------ boundaries
st, b = post(raw, h, label="fremd")
st2, b2 = post(raw, h, label="gibtsnicht")
st3, b3 = post(raw, h, name="gibtsnicht")
ok("25 KÖDER fremder Client, unbekannter Client, unbekannter Empfänger: dreimal dieselbe 404",
   st == st2 == st3 == 404 and b == b2 == b3, (st, b, st2, b2, st3, b3))
CLIENTS["ea"]["models"] = False
save()
st, b = post(json.dumps({"id": "A-1", "name": "CRM x"}).encode(), {"X-Token": TOK}, name="tok")
ok("26 KÖDER ohne Schreibrecht schreibt der Client auch über den Empfänger nichts: 422",
   st == 422, (st, b))
CLIENTS.pop("ea")
save()
st, b = post(json.dumps({"id": "A-1", "name": "CRM y"}).encode(), {"X-Token": TOK}, name="tok")
ok("27 KÖDER entfernter Client: 404", st == 404, (st, b))
CLIENTS["ea"] = {"tenant": "s1", "reads": [], "models": True}
save()
big = b"{" + b" " * (1024 * 1024 + 10) + b"}"
st, b = post(big, {"X-Token": TOK}, name="tok")
ok("28 KÖDER über 1 MB: 413", st == 413, (st, b))
rv = c.get("/twin/v1/openapi.json", headers=H("ea"))
doc = rv.get_data(as_text=True)
ok("29 OpenAPI beschreibt Empfänger und Eingang", "/receivers" in doc
   and "/inbound/{client}/{name}" in doc, rv.status_code)

print("")
if FAILED:
    print("rot: " + " ".join(FAILED))
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
