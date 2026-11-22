#!/usr/bin/env python3
"""Modellieren und Abgleich über die Tür /twin/v1 (RFC-0120 Stufe 3) gegen eine
echte PostgreSQL -- Anwendungsfall 2 als Ablauf.

Ein Modellierungswerkzeug (Client `ea`, modelliert) legt im Mandanten `werk`
einen Objekttyp mit Gruppe und Beziehung an, legt Objekte über seine eigenen
Schlüssel an, ändert sie, legt sie still; eine Person pflegt im Portal eine
Mandantengruppe auf demselben Objekt; das Werkzeug hört es per Webhook und
darf sie nicht überschreiben. Dazu ein Client, der nur liest (`viewer`), einer,
der einen fremden Typ schreibt (`dash`), und einer aus einem anderen Mandanten
(`fremd`).

Festgehalten wird:

- Typen: nur mit `models`; Objekttyp im Namensraum des Mandanten
  (`t.werk.Application`); derselbe Aufruf noch einmal ändert nichts;
  hinzufügen geht, umdeuten nicht; eine Gruppe nur auf einen Typ, den der
  Client schreibt;
- Upsert über den eigenen Schlüssel: zweimal = ein Objekt; Werte geprüft
  ("1200,5" -> 1200.5, ein falscher Wert schreibt nichts); Beziehung über den
  Schlüssel des Ziels; Umbenennen nur durch den Besitzer, ein Lesen mit
  früherem `at` sieht den alten Titel;
- die Abgleichsregel: eine Gruppe, die eine Person angelegt hat, wird dem
  Werkzeug mit dem Namen des Besitzers verweigert;
- Stapel: alles oder nichts; dieselbe Kennung noch einmal schreibt nichts;
- Stilllegen: nur der Besitzer; danach weg aus allen Lesewegen, mit früherem
  `at` noch da, nie wieder beschrieben;
- Webhooks: nur https ins Internet (Ausnahme ausdrücklich erlaubt);
  signiert, in Reihenfolge, die eigenen Schreibvorgänge nicht; Fehler mit
  Wartezeit, nach dem Limit angehalten, im Portal sichtbar, wieder
  aufgenommen mit allem seit dem Halt; geschlossener Mandant keiner;
  entfernter Client angehalten;
- Mandantengrenze: ein Client eines anderen Mandanten sieht nichts davon.

Aufruf: TS_DSN=... python3 test/test_twin_sync.py   (im Linux-Runner)
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
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-twin-sync-"))

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
ok("1 das Werkzeug legt einen Objekttyp mit Gruppe und Beziehung an: 201, im Namensraum "
   "des Mandanten", st == 201 and b.get("object_type") == AT
   and {AT, "ea.app", "appName", "criticality", "cost", "dependsOn"} <= set(b.get("created", [])),
   (st, b))
st, b = call("POST", "/twin/v1/types", data=APP_TYPES)
ok("2 derselbe Aufruf noch einmal: 200, nichts angelegt", st == 200 and b["created"] == []
   and AT in b["unchanged"] and "ea.app" in b["unchanged"], (st, b))
more = json.loads(json.dumps(APP_TYPES))
more["groups"][0]["attributes"].append({"key": "tco", "value_type": "decimal",
                                        "periodic": True, "unit": "EUR"})
st, b = call("POST", "/twin/v1/types", data=more)
gdef = q("SELECT definition FROM oaap_model.type_definitions WHERE key = 'ea.app'")
gdef = json.loads(gdef[0]["definition"]) if gdef else {"attributes": []}
ok("3 hinzufügen geht: ein periodisches Attribut kommt an die Gruppe",
   st == 201 and "tco" in gdef["attributes"] and gdef["attributes"][:3] == ["appName",
                                                                          "criticality", "cost"],
   (st, b, gdef))
bad = json.loads(json.dumps(APP_TYPES))
bad["groups"][0]["attributes"][2] = {"key": "cost", "value_type": "text"}
st, b = call("POST", "/twin/v1/types", data=bad)
ok("4 KÖDER dasselbe Attribut mit anderer Bedeutung: 409 mit Satz",
   st == 409 and "cost" in err(b) and "decimal" in err(b), (st, b))
st, b = call("POST", "/twin/v1/types", who="viewer", data=APP_TYPES)
ok("5 KÖDER ein Client ohne 'models' legt keine Typen an: 403", st == 403
   and "models" in err(b), (st, b))
st, b = call("POST", "/twin/v1/types", data={"on": "Site", "groups": [
    {"key": "ea.site", "attributes": [{"key": "appName", "value_type": "text"}]}]})
ok("6 KÖDER eine Gruppe auf einen Typ, den es nicht schreibt (Site): 403", st == 403, (st, b))
st, b = call("POST", "/twin/v1/types", data={"object_type": {"key": "Bad Key"}})
ok("7 KÖDER kein Schlüssel: 400", st == 400, (st, b))
st, b = call("POST", "/twin/v1/types", who="fremd", data={
    "object_type": {"key": "Application"}, "groups": [{"key": "ea.app"}]})
ok("8 KÖDER ein anderer Mandant: sein Objekttyp hat seinen Namensraum, die Gruppe 'ea.app' "
   "gehört schon werk: 409", st == 409 and "ea.app" in err(b), (st, b))
st, b = call("GET", "/twin/v1/types")
ok("9 /types nennt dem Werkzeug seinen Typ zum Schreiben",
   st == 200 and [(t["key"], t["access"]) for t in b["types"] if t["kind"] == "object_types"]
   == [(AT, ["write"])], (st, b))

# ------------------------------------------------------------------ upsert
CRM = {"type": AT, "title": "CRM", "groups": {"ea.app": {"attributes": {
    "appName": "CRM", "criticality": "high", "cost": "1200,5"}}}}
st, b = call("PUT", "/twin/v1/objects/by-key/APP-1", data=CRM)
CRM_ID = b.get("id", "") if isinstance(b, dict) else ""
CRM_BARE = CRM_ID.rsplit(":", 1)[-1]
val = q("SELECT value FROM twin_s1.current_attributes WHERE attr_key = 'cost'")
ok("10 Upsert legt das Objekt an: 201, '1200,5' als Zahl", st == 201 and b["created"] is True
   and val and val[0]["value"] == 1200.5, (st, b, val))
st, b = call("PUT", "/twin/v1/objects/by-key/APP-1", data=CRM)
ok("11 dasselbe noch einmal: 200, ein Objekt, nichts geschrieben",
   st == 200 and b["created"] is False and b["written"] == 0 and b["id"] == CRM_ID
   and count("objects") == 1, (st, b))
st, b = call("PUT", "/twin/v1/objects/by-key/APP-2", data={
    "type": AT, "title": "Shop", "groups": {"ea.app": {
        "attributes": {"appName": "Shop"},
        "relations": [{"key": "dependsOn", "target_key": "APP-1"}]}}})
SHOP_ID = b.get("id", "") if isinstance(b, dict) else ""
st2, g = call("GET", f"/twin/v1/objects/{SHOP_ID}/graph?depth=1")
ok("12 Beziehung über den Schlüssel des Ziels: der Graph hat die Kante Shop -> CRM",
   st == 201 and st2 == 200 and any(e.get("source") == SHOP_ID and e.get("target") == CRM_ID
                                    for e in g.get("edges", [])), (st, b, g))
n_obj = count("objects")
st, b = call("PUT", "/twin/v1/objects/by-key/APP-3", data={
    "type": AT, "title": "ERP", "groups": {"ea.app": {"attributes": {
        "appName": "ERP", "criticality": "mittel"}}}})
ok("13 KÖDER Wert außerhalb der Aufzählung: 400, kein Objekt angelegt",
   st == 400 and "criticality" in err(b) and count("objects") == n_obj, (st, b))
st, b = call("PUT", "/twin/v1/objects/by-key/V-1", who="viewer", data={
    "type": "Site", "title": "x", "groups": {"esg.core": {}}})
ok("14 KÖDER ein lesender Client legt nichts an: 403", st == 403, (st, b))
st, b = call("PUT", "/twin/v1/objects/by-key/APP-9", data={
    "type": AT, "title": "X", "groups": {"ea.app": {"attributes": {"unbekannt": 1}}}})
ok("15 KÖDER ein Attribut, das die Gruppe nicht hat: 400", st == 400
   and "unbekannt" in err(b), (st, b))
st, b = call("PUT", "/twin/v1/objects/by-key/APP-9", data={
    "type": AT, "title": "X", "groups": {"ea.app": {
        "relations": [{"key": "dependsOn", "target_key": "GIBTSNICHT"}]}}})
ok("16 KÖDER Beziehung auf einen unbekannten Schlüssel: 404", st == 404, (st, b))
T_OLD = datetime.datetime.now(datetime.timezone.utc).isoformat()
time.sleep(1.1)
st, b = call("PUT", "/twin/v1/objects/by-key/APP-1", data=dict(CRM, title="CRM neu"))
st2, now_ = call("GET", f"/twin/v1/objects/{CRM_ID}")
st3, then = call("GET", f"/twin/v1/objects/{CRM_ID}?at={T_OLD.replace('+00:00', 'Z')}")
ok("17 Umbenennen durch den Besitzer; mit früherem at der alte Titel",
   st == 200 and b["renamed"] is True and now_.get("title") == "CRM neu"
   and then.get("title") == "CRM", (b, now_.get("title"), then))
st, b = call("GET", "/twin/v1/objects/by-key/APP-1")
ok("18 Lesen über den eigenen Schlüssel", st == 200 and b.get("id") == CRM_ID, (st, b))

# ------------------------------------------------- the sync rule: a person's group
rv = c.post("/internal/twin/types", headers=PERSON, json={
    "on": AT, "group_key": "ea.notes", "attributes": [{"key": "note", "value_type": "text"}]})
rv2 = c.put(f"/internal/twin/objects/{CRM_ID}/groups/ea.notes", headers=PERSON,
            json={"attributes": {"note": "Bitte nicht abschalten"}})
ok("19 eine Person legt im Portal eine Mandantengruppe auf dem Objekt des Werkzeugs an",
   rv.status_code == 201 and rv2.status_code == 204, (rv.status_code, body(rv), body(rv2)))

# ------------------------------------------------------------------ webhooks
GOT = []
ANSWER = [204]


class Recv(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        GOT.append((dict(self.headers), raw))
        self.send_response(ANSWER[0])
        self.end_headers()

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Recv)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/hook"

st, b = call("POST", "/twin/v1/webhooks", data={"url": URL})
ok("20 KÖDER ein Webhook ins eigene Netz: 400", st == 400, (st, b))
for u, word in (("https://store/x", "network"), ("https://10.0.0.7/x", "internet"),
                ("https://u:p@example.org/x", "user"), ("http://example.org/x", "https")):
    st, b = call("POST", "/twin/v1/webhooks", data={"url": u})
    ok(f"21 KÖDER {u}: 400 mit Satz", st == 400 and word in err(b), (st, b))
twin.HOOK_ALLOW = ("127.0.0.1",)
hooks.ALLOW = ("127.0.0.1",)
TENANTS["s1"]["egress"] = {"closed": True}
save()
st, b = call("POST", "/twin/v1/webhooks", data={"url": URL})
ok("22 KÖDER geschlossener Mandant: 409", st == 409 and "closed" in err(b), (st, b))
TENANTS["s1"].pop("egress")
save()
st, b = call("POST", "/twin/v1/webhooks", data={"url": URL})
HOOK = b if isinstance(b, dict) else {}
SECRET = HOOK.get("secret", "")
ok("23 die erlaubte Ausnahme: 201, das Geheimnis einmal", st == 201
   and SECRET.startswith("whsec_"), (st, b))
st, b = call("GET", "/twin/v1/webhooks")
ok("24 die Liste nennt den Webhook, nie das Geheimnis", st == 200 and len(b["webhooks"]) == 1
   and "secret" not in json.dumps(b), b)


def deliver(tid="s1"):
    """One round of the service for one tenant, with the tenant's closed flag
    read from the file the way hooks.main reads it."""
    with open(twin.TENANTS_FILE) as f:
        rec = hooks.tsy.tenant_of(json.load(f), tid)
    conn = get_conn(tid)
    try:
        with conn:
            return hooks.run_tenant(conn, tid, CLIENTS, hooks.tsy.egress_closed(rec))
    finally:
        conn.close()


# a change by the person, one by the tool itself
c.put(f"/internal/twin/objects/{CRM_ID}/groups/ea.notes", headers=PERSON,
      json={"attributes": {"note": "Wartung am Freitag"}})
call("PUT", "/twin/v1/objects/by-key/APP-1", data={"groups": {"ea.app": {
    "attributes": {"criticality": "low"}}}})
res = deliver()
evs = json.loads(GOT[-1][1])["events"] if GOT else []
sig_ok = bool(GOT) and hooks.verify(SECRET, GOT[-1][0].get("X-OAAP-Signature", ""), GOT[-1][1])
ok("25 das Werkzeug hört die Änderung der Person per Webhook, signiert -- seine eigene nicht",
   res and res[0][1].startswith("sent") and sig_ok
   and [(e["kind"], e["origin"], e["group"]) for e in evs] == [
       ("group.written", "tenant:jo", "ea.notes")], (res, evs, sig_ok))
ok("26 KÖDER eine verfälschte Signatur oder ein alter Zeitstempel wird erkannt",
   GOT and not hooks.verify(SECRET, GOT[-1][0]["X-OAAP-Signature"], GOT[-1][1] + b" ")
   and not hooks.verify(SECRET, GOT[-1][0]["X-OAAP-Signature"], GOT[-1][1],
                        now=time.time() + 3600))
st, b = call("PUT", f"/twin/v1/objects/{CRM_ID}/groups/ea.notes",
             data={"attributes": {"note": "überschrieben"}})
note = q("SELECT value FROM twin_s1.current_attributes WHERE attr_key = 'note'")
ok("27 und es überschreibt sie nicht: 403, nennt den Besitzer; der Wert der Person bleibt",
   st == 403 and "belongs to 'tenant'" in err(b)
   and note and note[0]["value"] == "Wartung am Freitag", (st, b, note))
ok("28 nichts Neues: kein zweiter POST", deliver()[0][1] == "nothing" and len(GOT) == 1)

# failures, backoff, stop, resume
ANSWER[0] = 500
CUR0 = q("SELECT cursor FROM twin_s1.webhooks")[0]["cursor"]
c.put(f"/internal/twin/objects/{CRM_ID}/groups/ea.notes", headers=PERSON,
      json={"attributes": {"note": "Fehlerfall"}})
res = deliver()
h = q("SELECT * FROM twin_s1.webhooks")[0]
ok("29 der Empfänger antwortet 500: ein Fehlversuch, Wartezeit, der Zeiger bleibt",
   res[0][1] == "failed" and h["failures"] == 1 and h["next_try_at"] is not None
   and "500" in (h["last_error"] or "") and h["cursor"] == CUR0, (res, h))
ok("30 vor der Wartezeit kein neuer Versuch", deliver() == [])
q("UPDATE twin_s1.webhooks SET failures = %s, next_try_at = NULL", hooks.FAILURES_MAX - 1)
res = deliver()
st, b = call("GET", "/twin/v1/webhooks")
rv = c.get("/internal/twin/webhooks", headers=PERSON)
ok("31 nach dem Limit angehalten -- der Client sieht es, das Portal auch",
   res[0][1] == "stopped" and b["webhooks"][0]["state"] == "stopped"
   and body(rv)["webhooks"][0]["state"] == "stopped" and "secret" not in rv.get_data(as_text=True),
   (res, b, body(rv)))
ok("32 angehalten: keine Zustellung mehr", deliver() == [])
ANSWER[0] = 204
st, b = call("POST", f"/twin/v1/webhooks/{HOOK['id']}/resume")
res = deliver()
evs = json.loads(GOT[-1][1])["events"]
ok("33 wieder aufgenommen: zugestellt wird alles seit dem Halt",
   st == 200 and res[0][1].startswith("sent") and any(
       e["kind"] == "group.written" and e["group"] == "ea.notes" for e in evs), (st, res, evs))
st, b = call("POST", "/twin/v1/webhooks/999/resume", who="viewer")
ok("34 KÖDER fremder Webhook: 404", st == 404, (st, b))
TENANTS["s1"]["egress"] = {"closed": True}
save()
c.put(f"/internal/twin/objects/{CRM_ID}/groups/ea.notes", headers=PERSON,
      json={"attributes": {"note": "zu"}})
n = len(GOT)
res = deliver()
ok("35 der Mandant schließt seinen Abfluss: nichts geht hinaus, der Webhook wartet",
   res[0][1] == "waiting" and len(GOT) == n, res)
TENANTS["s1"].pop("egress")
save()
q("UPDATE twin_s1.webhooks SET next_try_at = NULL")
gone = CLIENTS.pop("ea")
res = deliver()
ok("36 ein entfernter Client: sein Webhook hält an", res[0][1] == "stopped", res)
CLIENTS["ea"] = gone
call("POST", f"/twin/v1/webhooks/{HOOK['id']}/resume")
try:
    hooks.ALLOW = ()
    hooks.checked_address("127.0.0.1", 80)
    blocked = False
except hooks.Refused:
    blocked = True
finally:
    hooks.ALLOW = ("127.0.0.1",)
ok("37 KÖDER ohne Ausnahme: die Zustellung prüft die Adresse und verbindet nicht", blocked)

# ------------------------------------------------------------------ batch
OPS = [{"op": "upsert", "key": "APP-3", "type": AT, "title": "ERP",
        "groups": {"ea.app": {"attributes": {"appName": "ERP", "criticality": "low"},
                              "relations": [{"key": "dependsOn", "target_key": "APP-1"}]}}},
       {"op": "group", "key": "APP-2", "group": "ea.app", "attributes": {"cost": 99}},
       {"op": "period", "key": "APP-3", "group": "ea.app", "attribute": "tco", "value": 5000,
        "valid_from": "2025-01-01", "valid_to": "2025-12-31", "quality": "estimated"}]
st, b = call("POST", "/twin/v1/batch", data={"id": "sync-001", "operations": OPS})
ev = count("events")
ok("38 ein Stapel aus Upsert, Gruppe und Zeitraum: alles geschrieben",
   st == 200 and b["replayed"] is False and [r["op"] for r in b["results"]]
   == ["upsert", "group", "period"] and b["results"][0]["created"] is True, (st, b))
st, b = call("POST", "/twin/v1/batch", data={"id": "sync-001", "operations": OPS})
ok("39 dieselbe Kennung noch einmal: dieselbe Antwort, nichts geschrieben",
   st == 200 and b["replayed"] is True and count("events") == ev, (st, b))
n_obj = count("objects")
st, b = call("POST", "/twin/v1/batch", data={"id": "sync-002", "operations": [
    {"op": "upsert", "key": "APP-4", "type": AT, "title": "BI",
     "groups": {"ea.app": {"attributes": {"appName": "BI"}}}},
    {"op": "group", "key": "APP-1", "group": "ea.app", "attributes": {"cost": "teuer"}}]})
ok("40 KÖDER ein schlechter Schritt: 400 nennt ihn, auch der erste ist nicht geschrieben",
   st == 400 and "operation 2" in err(b) and count("objects") == n_obj
   and not q("SELECT 1 FROM twin_s1.batches WHERE batch_id = 'sync-002'"), (st, b))
st, b = call("POST", "/twin/v1/batch", data={"id": "x", "operations": [{"op": "group"}] * 501})
ok("41 KÖDER mehr als 500 Schritte: 400", st == 400 and "500" in err(b), (st, b))
st, b = call("POST", "/twin/v1/batch", data={"operations": OPS})
ok("42 KÖDER ohne Kennung: 400", st == 400 and "id" in err(b), (st, b))

# ------------------------------------------------------------------ retire
T_ALIVE = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
time.sleep(1.1)
st, b = call("POST", f"/twin/v1/objects/{SHOP_ID}/retire", data={"reason": "abgelöst"})
st2, _x = call("GET", f"/twin/v1/objects/{SHOP_ID}")
st3, then = call("GET", f"/twin/v1/objects/{SHOP_ID}?at={T_ALIVE}")
st4, ex = call("GET", f"/twin/v1/export?types={AT}")
ok("43 stillgelegt: weg aus dem Lesen und dem Export, mit früherem at noch da",
   st == 200 and b["written"] is True and st2 == 404 and st3 == 200
   and SHOP_ID not in [n["id"] for n in ex.get("nodes", [])], (st, b, st2, st3))
st, b = call("PUT", "/twin/v1/objects/by-key/APP-2", data={"title": "Shop", "groups": {
    "ea.app": {"attributes": {"appName": "Shop 2"}}}})
ok("44 KÖDER ein stillgelegtes Objekt wird nicht wieder beschrieben: 409", st == 409
   and "retired" in err(b), (st, b))
st, b = call("POST", f"/twin/v1/objects/{SHOP_ID}/retire")
ok("45 noch einmal stilllegen schreibt nichts", st == 200 and b["written"] is False, (st, b))
rv = c.post("/twin/objects", headers={"X-OAAP-User": "instance:esg"}, json={
    "type": "Site", "title": "Werk Nord", "group": {"key": "esg.core"}})
SITE = body(rv).get("id", "")
st, b = call("POST", f"/twin/v1/objects/{SITE}/retire", who="dash")
ok("46 KÖDER ein Client legt nicht still, was ihm nicht gehört: 403 nennt den Besitzer",
   st == 403 and "app:esg" in err(b), (st, b))
st, b = call("PUT", f"/twin/v1/objects/{SITE}/groups/esg.core", who="dash",
             data={"attributes": {"employees": 3}})
ok("47 KÖDER und schreibt nicht in die Gruppe der App: 403", st == 403
   and "app:esg" in err(b), (st, b))
rv = c.get(f"/internal/twin/objects/{SHOP_ID}", headers=PERSON)
ok("48 auch das Portal sieht das stillgelegte Objekt nicht mehr", rv.status_code == 404,
   rv.status_code)
st, b = call("GET", "/twin/v1/changes?since=0")
ok("49 der Änderungs-Feed nennt Anlegen, Umbenennen und Stilllegen",
   {"object.created", "object.renamed", "object.retired"} <= {x["kind"] for x in b["changes"]},
   b)

# ------------------------------------------------------------------ tenants
st, b = call("GET", f"/twin/v1/objects/{CRM_ID}", who="fremd")
st2, b2 = call("PUT", f"/twin/v1/objects/{CRM_ID}/groups/ea.app", who="fremd",
               data={"attributes": {"appName": "x"}})
ok("50 KÖDER ein Client des anderen Mandanten sieht und schreibt nichts davon: 404, 404",
   st == 404 and st2 == 404, (st, b, st2, b2))
st, b = call("PUT", "/twin/v1/objects/by-key/APP-1", who="viewer", data={})
ok("51 KÖDER derselbe Schlüssel bei einem anderen Client ist ein anderes Objekt (hier keins)",
   st == 400 and "type" in err(b), (st, b))
rv = c.post("/twin/v1/batch", headers={"X-OAAP-User": "instance:esg"},
            json={"id": "a", "operations": []})
ok("52 KÖDER eine App schreibt nicht über die Tür der Clients: 403", rv.status_code == 403)

print("")
if FAILED:
    print("rot: " + " ".join(FAILED))
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
