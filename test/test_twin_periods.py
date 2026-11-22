#!/usr/bin/env python3
"""Werte mit Zeitraum, geprüfte Werte und Messreihen für Personen und Clients
(RFC-0120 Stufe 2) gegen eine echte PostgreSQL.

Ein Standort mit einer Kennzahl je Jahr (CO2e, periodisch, Einheit t CO2e),
eine App, die ihn führt, ein Integrations-Client, der liest, eine Person im
Portal, die Werte einträgt, und die Flotte als Schreiber der Plattform.

Festgehalten wird:

- Werte werden an jeder Tür gegen ihren Typ geprüft: eine App bekommt 400 mit
  Satz und es wird nichts geschrieben (auch nicht die richtigen Werte
  daneben); "5" für eine ganze Zahl wird 5; ein leerer Text für ein Datum ist
  kein Wert; ein nicht registriertes Attribut wird nicht geprüft; die Flotte
  (platform:) verliert nur den falschen Wert, nie den ganzen Schreibvorgang;
- ein periodisches Attribut wird nur als Zeitraum geschrieben, ein
  gewöhnliches nie als Zeitraum;
- zwei Zeiträume eines Attributs bestehen nebeneinander; ein überlappender
  wird abgewiesen (409), dieselben Grenzen noch einmal korrigieren, unverändert
  schreibt nichts, null zieht zurück;
- gelesen wird je Zeitraum (Liste, Fenster, `at` vor der Korrektur) und als
  Wert des Objekts: der Zeitraum, der jetzt gilt, sonst der zuletzt
  abgelaufene, mit seinen Grenzen;
- eine Person trägt in eine Mandantengruppe ein (Rolle nötig, Gruppe entsteht),
  nie in die Gruppe einer App;
- Messreihen: ein Client liest die Reihen der Objekte, die er lesen darf, eine
  Person alle; eine fremde Reihe ist 404;
- die Formular-Regeln des Portals (Zeitraum aus Jahr/Teil oder Von/Bis,
  Beschriftung, Balken).

Aufruf: TS_DSN=... python3 test/test_twin_periods.py   (im Linux-Runner)
"""
import datetime
import json
import os
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-twin-periods-"))

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
import timeseries as ts             # noqa: E402
import twin_write as tw             # noqa: E402

sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import twin_view as tvw             # noqa: E402

# ------------------------------------------------------------ the database
DDL = []
real = a._store_psql
a._store_psql = lambda sql, *x, **k: DDL.append(sql) or type("R", (), {"stdout": "", "returncode": 0})()
a._model_ensure_schema()
a._twin_ensure_tables("twin_p1")
a._store_psql = real
admin = psycopg2.connect(DSN)
admin.autocommit = True
with admin.cursor() as cur:
    for sch in ("oaap_model", "twin_p1", "ts_p1"):
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    cur.execute('CREATE SCHEMA "twin_p1"')
    cur.execute("""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twin_p1')
                   THEN CREATE ROLE "twin_p1" LOGIN; END IF; END $$""")
    for sql in DDL:
        cur.execute(sql)
    cur.execute('CREATE SCHEMA "ts_p1"')


def q(sql, *args):
    with admin.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        try:
            return cur.fetchall()
        except psycopg2.ProgrammingError:
            return None


def typ(key, kind, d, origin="app:esg"):
    q("INSERT INTO oaap_model.type_definitions (key, kind, origin, package, version, definition) "
      "VALUES (%s, %s, %s, 'esg', '1.0.0', %s)", key, kind, origin, json.dumps(d))
    q("INSERT INTO oaap_model.activations (tenant_id, type_key) VALUES ('p1', %s)", key)


typ("Site", "object_types", {"key": "Site", "title": "Standort"})
typ("Pump", "object_types", {"key": "Pump", "title": "Pumpe"})
typ("employees", "attribute_types", {"key": "employees", "value_type": "int"})
typ("opened", "attribute_types", {"key": "opened", "value_type": "date"})
typ("rating", "attribute_types", {"key": "rating", "value_type": "enum", "values": ["A", "B"]})
typ("co2e", "attribute_types", {"key": "co2e", "value_type": "decimal", "periodic": True,
                                "unit": "t CO2e", "title": "CO2e"})
typ("energy", "attribute_types", {"key": "energy", "value_type": "decimal", "periodic": True,
                                  "unit": "MWh"})
typ("esg.core", "group_types", {"key": "esg.core", "on": "Site",
                                "attributes": ["employees", "opened", "rating", "co2e"]})
typ("esg.mine", "group_types", {"key": "esg.mine", "on": "Site", "attributes": ["energy"]},
    origin="tenant:p1")
q("INSERT INTO oaap_model.bindings (instance, type_key, direction, role) "
  "VALUES ('esg', 'Site', 'contributes', 'owner')")

# ------------------------------------------------------------- the service
APPS = os.path.join(os.environ["OAAP_DATA_DIR"], "platform-apps")
os.makedirs(APPS, exist_ok=True)
twin.REGISTRY_FILE = os.path.join(APPS, "registry.json")
twin.CLIENTS_FILE = os.path.join(APPS, "twin-clients.json")
twin.READERS_FILE = os.path.join(APPS, "twin-readers.json")
with open(twin.REGISTRY_FILE, "w") as f:
    json.dump({"instances": {"esg": {"tenant": "p1", "app_id": "esg"}}}, f)
with open(twin.CLIENTS_FILE, "w") as f:
    json.dump({"clients": {"dash": {"tenant": "p1", "reads": ["Site"]}}}, f)


def get_conn(tid):
    return psycopg2.connect(DSN, options=f"-c search_path=twin_{tid}",
                            cursor_factory=psycopg2.extras.RealDictCursor)


def get_ts(tid):
    return ts.PostgresDriver(psycopg2.connect(DSN), f"ts_{tid}")


twin.get_conn = get_conn
twin.get_ts = get_ts
c = twin.app.test_client()
APP = {"X-OAAP-User": "instance:esg"}
PERSON = {"X-OAAP-Person-User": "jo", "X-OAAP-Person-Tenant": "p1",
          "X-OAAP-Person-Roles": "keyuser", twin.INTERNAL_HEADER: "k"}
twin.INTERNAL_KEY = "k"


def body(rv):
    try:
        b = rv.get_json()
    except Exception:
        b = None
    return b if b is not None else rv.get_data(as_text=True)


rv = c.post("/twin/objects", headers=APP, json={
    "type": "Site", "title": "Werk Nord", "group": {"key": "esg.core",
                                                     "attributes": {"employees": "120"}}})
SITE = (body(rv) or {}).get("id", "") if rv.status_code == 201 else ""
ok("0 Standort angelegt; '120' für eine ganze Zahl angenommen", rv.status_code == 201,
   body(rv))
BARE = SITE.rsplit(":", 1)[-1]


def current(attr, group="esg.core"):
    r = q("SELECT value FROM twin_p1.current_attributes WHERE object_id = %s AND group_key = %s "
          "AND attr_key = %s", BARE, group, attr)
    return r[0]["value"] if r else "MISSING"


ok("1 gespeichert als Zahl 120, nicht als Text", current("employees") == 120,
   current("employees"))

# ------------------------------------------------------------ value checks
PUT = f"/twin/objects/{SITE}/groups/esg.core"
rv = c.put(PUT, headers=APP, json={"attributes": {"employees": "viele", "opened": "2020-05-01"}})
ok("2 KÖDER Text für eine ganze Zahl: 400 mit Satz, und das richtige Datum daneben "
   "auch nicht geschrieben", rv.status_code == 400 and "employees" in body(rv)
   and current("opened") == "MISSING", (rv.status_code, body(rv), current("opened")))
rv = c.put(PUT, headers=APP, json={"attributes": {"opened": "2020-13-45"}})
ok("3 KÖDER kein Datum: 400", rv.status_code == 400, body(rv))
rv = c.put(PUT, headers=APP, json={"attributes": {"rating": "C"}})
ok("4 KÖDER Wert außerhalb der Aufzählung: 400 und nennt die erlaubten",
   rv.status_code == 400 and "A, B" in body(rv), body(rv))
rv = c.put(PUT, headers=APP, json={"attributes": {"opened": "", "rating": "A",
                                                   "freitext": {"x": 1}}})
ok("5 leerer Text für ein Datum = kein Wert; nicht registriertes Attribut ungeprüft",
   rv.status_code == 204 and current("opened") is None and current("rating") == "A"
   and current("freitext") == {"x": 1}, (rv.status_code, body(rv)))
rv = c.put(PUT, headers=APP, json={"attributes": {"co2e": 5}})
ok("6 KÖDER periodisches Attribut über die Gruppe: 400, verweist auf /periods",
   rv.status_code == 400 and "periods" in body(rv), body(rv))
conn = get_conn("p1")
with conn:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO groups (object_id, group_key, origin, created_by) "
                    "VALUES (%s, 'esg.fleet', 'platform:fleet', 'fleet')", (BARE,))
    n = tw.write_group(conn, BARE, "esg.fleet", "platform:fleet", "fleet",
                       {"attributes": {"employees": "kaputt", "rating": "B"}})
conn.close()
ok("7 die Flotte (platform:) verliert nur den falschen Wert, der richtige wird geschrieben",
   n == 1 and current("rating", "esg.fleet") == "B"
   and current("employees", "esg.fleet") == "MISSING", n)

# ----------------------------------------------------------------- periods
PER = f"/twin/objects/{SITE}/groups/esg.core/periods"


def post(path, data, headers=APP):
    rv = c.post(path, headers=headers, json=data)
    return rv.status_code, body(rv)


st, b = post(PER, {"attribute": "co2e", "value": "1200,5", "valid_from": "2024-01-01",
                   "valid_to": "2024-12-31", "source": "Bilanz 2024", "quality": "calculated"})
ok("8 CO2e 2024 eingetragen: 201, Wert als Zahl, Einheit vom Typ, Ende = Beginn 2025",
   st == 201 and b["period"]["value"] == 1200.5 and b["period"]["unit"] == "t CO2e"
   and b["period"]["valid_to"] == "2025-01-01T00:00:00Z", (st, b))
st, b = post(PER, {"attribute": "co2e", "value": 1100, "valid_from": "2025-01-01",
                   "valid_to": "2025-12-31", "source": "Bilanz 2025", "quality": "estimated"})
ok("9 CO2e 2025 daneben: 201", st == 201, (st, b))
st, b = post(PER, {"attribute": "co2e", "value": 300, "valid_from": "2025-01-01",
                   "valid_to": "2025-03-31"})
ok("10 KÖDER Q1 2025 überlappt 2025: 409 mit Satz", st == 409 and "overlap" in b, (st, b))
T_BEFORE = datetime.datetime.now(datetime.timezone.utc)
time.sleep(1.1)
st, b = post(PER, {"attribute": "co2e", "value": 1050, "valid_from": "2025-01-01",
                   "valid_to": "2025-12-31", "source": "Bilanz 2025 (geprüft)",
                   "quality": "calculated"})
st2, b2 = post(PER, {"attribute": "co2e", "value": 1050, "valid_from": "2025-01-01",
                     "valid_to": "2025-12-31", "source": "Bilanz 2025 (geprüft)",
                     "quality": "calculated"})
ok("11 dieselben Grenzen korrigieren (201), dasselbe noch einmal schreibt nichts (200)",
   st == 201 and st2 == 200 and b2["written"] is False, (st, b, st2, b2))
rows = q("SELECT count(*) AS n FROM twin_p1.periods WHERE object_id = %s", BARE)[0]["n"]
ok("12 append-only: drei Zeilen (2024, 2025, Korrektur), die alte ist ersetzt, nicht gelöscht",
   rows == 3, rows)
st, b = post(PER, {"attribute": "employees", "value": 3, "valid_from": "2025-01-01",
                   "valid_to": "2025-12-31"})
ok("13 KÖDER gewöhnliches Attribut als Zeitraum: 400", st == 400 and "no periods" in b, (st, b))
st, b = post(PER, {"attribute": "co2e", "value": "viel", "valid_from": "2023-01-01",
                   "valid_to": "2023-12-31"})
ok("14 KÖDER falscher Werttyp im Zeitraum: 400", st == 400 and "decimal" in b, (st, b))
st, b = post(PER, {"attribute": "co2e", "value": 1, "valid_from": "2023-05-01",
                   "valid_to": "2023-01-01"})
ok("15 KÖDER Ende vor Beginn: 400", st == 400, (st, b))
st, b = post(PER, {"attribute": "co2e", "value": 1, "valid_from": "2023-01-01",
                   "valid_to": "2023-12-31", "quality": "geraten"})
ok("16 KÖDER unbekannte Güte: 400", st == 400 and "quality" in b, (st, b))
st, b = post(PER, {"attribute": "co2e", "value": 990, "valid_from": "2023-01-01",
                   "valid_to": "2023-12-31"})
st2, b2 = post(PER, {"attribute": "co2e", "value": None, "valid_from": "2023-01-01",
                     "valid_to": "2023-12-31"})
st3, b3 = post(PER, {"attribute": "co2e", "value": None, "valid_from": "2023-01-01",
                     "valid_to": "2023-12-31"})
st4, b4 = post(PER, {"attribute": "co2e", "value": 980, "valid_from": "2023-03-01",
                     "valid_to": "2023-12-31"})
ok("17 2023 eingetragen und mit null zurückgezogen; noch einmal zurückziehen: 404; "
   "danach überlappt ein neuer Zeitraum nichts mehr", (st, st2, st3, st4) == (201, 201, 404, 201),
   (st, b, st2, b2, st3, b3, st4, b4))
post(PER, {"attribute": "co2e", "value": None, "valid_from": "2023-03-01",
           "valid_to": "2023-12-31"})

# --------------------------------------------------------------- reading
CL = {"X-OAAP-User": "client:dash"}


def cget(path, headers=CL, **params):
    rv = c.get(path, headers=headers, query_string=params)
    return rv.status_code, body(rv)


st, b = cget(f"/twin/v1/objects/{SITE}/periods", attribute="esg.core.co2e")
vals = [(p["valid_from"][:4], p["value"], p.get("source")) for p in b.get("periods", [])] \
    if st == 200 else b
ok("18 Client liest je Zeitraum: 2024 und 2025 (korrigiert), zurückgezogene fehlen",
   vals == [("2024", 1200.5, "Bilanz 2024"), ("2025", 1050, "Bilanz 2025 (geprüft)")], vals)
st, b = cget(f"/twin/v1/objects/{SITE}/periods", attribute="esg.core.co2e",
             at=T_BEFORE.isoformat().replace("+00:00", "Z"))
ok("19 `at` vor der Korrektur: 2025 noch mit 1100",
   st == 200 and [p["value"] for p in b["periods"]] == [1200.5, 1100], (st, b))
st, b = cget(f"/twin/v1/objects/{SITE}/periods", **{"from": "2025-06-01", "to": "2025-06-30"})
ok("20 Fenster Juni 2025: nur der Zeitraum 2025", st == 200 and len(b["periods"]) == 1
   and b["periods"][0]["valid_from"].startswith("2025"), (st, b))
st, b = cget(f"/twin/v1/objects/{SITE}/periods", attribute="co2e")
ok("21 KÖDER Attribut ohne Gruppe: 400", st == 400, (st, b))
st, b = cget(f"/twin/v1/objects/{SITE}")
g = (b.get("groups") or {}).get("esg.core", {}) if st == 200 else {}
ok("22 Objekt jetzt (2026): CO2e = zuletzt abgelaufener Zeitraum 2025, mit Grenzen",
   g.get("attributes", {}).get("co2e") == 1050
   and g.get("periods", {}).get("co2e", {}).get("valid_from") == "2025-01-01T00:00:00Z", b)
st, b = cget(f"/twin/v1/objects/{SITE}/periods", attribute="esg.core.co2e",
             **{"from": "2024-07-01", "to": "2024-07-01"})
st2, b2 = cget(f"/twin/v1/objects/{SITE}", at="2024-07-01")
rv = c.get(f"/internal/twin/objects/{BARE}", headers=PERSON, query_string={"at": "2024-07-01"})
pv = (body(rv).get("groups") or {}).get("esg.core", {}).get("attributes", {}).get("co2e", {})     if rv.status_code == 200 else {}
ok("23 der Wert am 1.7.2024, wie heute bekannt: über das Fenster der Zeiträume 1200,5 (und "
   "auf der Objektseite mit Stichtag); `at` am Objekt bleibt beide Achsen -- der Standort "
   "war damals noch nicht im Zwilling: 404",
   st == 200 and [p["value"] for p in b["periods"]] == [1200.5] and st2 == 404
   and pv.get("value") == 1200.5, (st, b, st2, pv))
st, b = cget("/twin/v1/objects", type="Site", where="esg.core.co2e<1100")
ok("24 Filter auf den periodischen Wert: Werk Nord (1050 < 1100)",
   st == 200 and [o["title"] for o in b["objects"]] == ["Werk Nord"], (st, b))
rv = c.get(f"/twin/objects/{SITE}/periods", headers=APP)
ok("25 die App liest die Zeiträume ihres Typs", rv.status_code == 200
   and len(body(rv)["periods"]) == 2, body(rv))
rv = c.get(f"/internal/twin/objects/{BARE}", headers=PERSON)
a_ = (body(rv).get("groups") or {}).get("esg.core", {}).get("attributes", {}).get("co2e", {}) \
    if rv.status_code == 200 else {}
ok("26 Objektseite (Person): CO2e periodisch, Wert 1050, Einheit, Zeitraum 2025",
   a_.get("periodic") is True and a_.get("value") == 1050 and a_.get("unit") == "t CO2e", a_)

# ------------------------------------------------------------ person door
PPER = f"/internal/twin/objects/{BARE}/groups/esg.mine/periods"
st, b = post(PPER, {"attribute": "energy", "value": "830", "valid_from": "2025-01-01",
                    "valid_to": "2025-12-31", "source": "Zähler"}, headers=PERSON)
grp = q("SELECT origin FROM twin_p1.groups WHERE object_id = %s AND group_key = 'esg.mine'", BARE)
ev = q("SELECT origin FROM twin_p1.events WHERE kind = 'period.written' ORDER BY id DESC LIMIT 1")
ok("27 Person trägt Energie 2025 in eine Mandantengruppe ein: 201, Gruppe entsteht mit "
   "Herkunft tenant, Ereignis nennt die Person",
   st == 201 and grp and grp[0]["origin"] == "tenant" and ev[0]["origin"] == "tenant:jo",
   (st, b, grp, ev))
st, b = post(PPER, {"attribute": "energy", "value": 1, "valid_from": "2024-01-01",
                    "valid_to": "2024-12-31"}, headers=dict(PERSON, **{"X-OAAP-Person-Roles": "user"}))
ok("28 KÖDER Person ohne Schreibrolle: 403", st == 403, (st, b))
st, b = post(f"/internal/twin/objects/{BARE}/groups/esg.core/periods",
             {"attribute": "co2e", "value": 1, "valid_from": "2022-01-01",
              "valid_to": "2022-12-31"}, headers=PERSON)
ok("29 KÖDER Person in die Gruppe der App: 403 nennt die Eigentümerin",
   st == 403 and "app:esg" in b, (st, b))
st, b = post(f"/internal/twin/objects/{BARE}/groups/esg.nichtda/periods",
             {"attribute": "energy", "value": 1, "valid_from": "2022-01-01",
              "valid_to": "2022-12-31"}, headers=PERSON)
ok("30 KÖDER Gruppentyp nicht aktiv: 404", st == 404, (st, b))
rv = c.get(f"/internal/twin/objects/{BARE}/periods", headers=PERSON)
ok("31 Person liest alle Zeiträume des Objekts (CO2e 2024, 2025, Energie 2025)",
   rv.status_code == 200 and [(p["attribute"], p["valid_from"][:4]) for p in body(rv)["periods"]]
   == [("co2e", "2024"), ("co2e", "2025"), ("energy", "2025")], body(rv))
st, b = post(PER, {"attribute": "co2e", "value": 1, "valid_from": "2022-01-01",
                   "valid_to": "2022-12-31"}, headers=CL)
ok("32 KÖDER der Client schreibt keinen Zeitraum (Stufe 3): 403", st == 403, (st, b))

# ------------------------------------------------------------------ series
drv = get_ts("p1")
drv.ensure_schema()
drv.ensure_partitions(time.time())
s, _ = drv.ensure_series(BARE, "power", unit="kW")
now = time.time()
drv.write(s["id"], [[now - 600 + i * 60, 10 + i] for i in range(10)], now)
q("INSERT INTO twin_p1.objects (id, type_key, title, owner_origin, recorded_by) "
  "VALUES (%s, 'Pump', 'Pumpe 1', 'app:esg', 'esg')", str(uuid.uuid4()))
pump = q("SELECT id FROM twin_p1.objects WHERE type_key = 'Pump'")[0]["id"]
s2, _ = drv.ensure_series(str(pump), "flow")
drv.conn.close()
st, b = cget("/twin/v1/series", object=SITE)
ok("33 Client: die Reihen des Standorts", st == 200
   and [x["measure"] for x in b["series"]] == ["power"], (st, b))
st, b = cget(f"/twin/v1/series/{s['id']}/range", **{"from": int(now - 3600), "to": int(now + 60)})
ok("34 Client: Verlauf der letzten Stunde, zehn Punkte",
   st == 200 and sum(x["count"] for x in b["buckets"]) == 10, (st, b))
st, b = cget(f"/twin/v1/series/{s2['id']}/range", **{"from": int(now - 3600), "to": int(now)})
st2, b2 = cget("/twin/v1/series", object=f"urn:oaap:obj:{pump}")
ok("35 KÖDER Reihe eines Typs, den der Client nicht lesen darf: 404 (auch die Liste)",
   st == 404 and st2 == 404, (st, b, st2, b2))
st, b = cget(f"/twin/v1/series/{s['id']}/latest")
ok("36 Client: letzter Punkt", st == 200 and b["latest"]["x"] == 19, (st, b))
rv = c.get("/internal/twin/series", headers=PERSON, query_string={"object": f"urn:oaap:obj:{pump}"})
rv2 = c.get(f"/internal/twin/series/{s2['id']}/range", headers=PERSON,
            query_string={"from": int(now - 3600), "to": int(now)})
ok("37 Person liest jede Reihe des Mandanten (für die Objektseite)",
   rv.status_code == 200 and rv2.status_code == 200, (body(rv), body(rv2)))
st, b = cget("/twin/v1/openapi.json")
ok("38 OpenAPI nennt die Wege der Stufe 2",
   st == 200 and {"/objects/{id}/periods", "/series", "/series/{id}/range"} <= set(b["paths"]),
   (st, b.get("paths") if isinstance(b, dict) else b))

# --------------------------------------------------------- portal rules
ok("39 Beschriftung: Jahr, Quartal, Monat, frei",
   [tvw.period_label(f, t) for f, t in (
       ("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
       ("2025-04-01T00:00:00Z", "2025-07-01T00:00:00Z"),
       ("2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z"),
       ("2025-01-15T00:00:00Z", "2025-02-16T00:00:00Z"))]
   == ["2025", "Q2 2025", "Mär 2025", "15.01.2025 – 15.02.2025"])


class F(dict):
    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([] if v is None else [v])


b1, e1 = tvw.parse_period_form(F(attribute="co2e", year="2025", part="q3", value="7"))
b2, e2 = tvw.parse_period_form(F(attribute="co2e", year="2024", part="m2", value="7"))
b3, e3 = tvw.parse_period_form(F(attribute="co2e", **{"from": "2025-01-10", "to": "2025-01-20"},
                                 value="7", quality="estimated"))
b4, e4 = tvw.parse_period_form(F(attribute="co2e", year="", value="7"))
ok("40 Formular: Q3 2025, Februar 2024 (Schaltjahr), Von/Bis; ohne Jahr ein Satz",
   (b1["valid_from"], b1["valid_to"]) == ("2025-07-01", "2025-09-30")
   and (b2["valid_from"], b2["valid_to"]) == ("2024-02-01", "2024-02-29")
   and (b3["valid_from"], b3["valid_to"], b3["quality"]) == ("2025-01-10", "2025-01-20",
                                                             "estimated")
   and b4 is None and "Jahr" in e4, (b1, b2, b3, e4))
bt, et = tvw.parse_new_type_form(F(on="Site", group_key="esg.kpi", attr_key=["co2e2", "note"],
                                   attr_title=["CO2e", "Notiz"],
                                   attr_value_type=["decimal", "text"],
                                   attr_periodic=["ja", "nein"], attr_unit=["t", ""]))
ok("41 Typ anlegen: periodisch mit Einheit nur in der Zeile, die es sagt",
   et is None and bt["attributes"][0].get("periodic") is True
   and bt["attributes"][0].get("unit") == "t" and "periodic" not in bt["attributes"][1], (bt, et))
bars = tvw.svg_bars([{"label": "2024", "value": 1200.5}, {"label": "2025", "value": 1050}])
line = tvw.svg_line([{"t": "2026-11-22T10:00:00Z", "mean": 1, "min": 0, "max": 2},
                     {"t": "2026-11-22T11:00:00Z", "mean": 3, "min": 2, "max": 4}],
                    datetime.datetime(2026, 11, 22, 9, tzinfo=datetime.timezone.utc).timestamp(),
                    datetime.datetime(2026, 11, 22, 12, tzinfo=datetime.timezone.utc).timestamp())
ok("42 Balken und Linie als SVG, ohne Skript",
   bars.count("<rect") == 2 and "<polyline" in line and "<script" not in bars + line, (bars, line))
TYPES = [{"key": "Site", "kind": "object_types", "title": "Standort", "definition": {}},
         {"key": "co2e", "kind": "attribute_types", "title": "CO2e",
          "definition": {"periodic": True, "unit": "t", "value_type": "decimal"}},
         {"key": "employees", "kind": "attribute_types", "title": "MA",
          "definition": {"value_type": "int"}},
         {"key": "esg.core", "kind": "group_types", "title": "Kern",
          "definition": {"on": "Site", "attributes": ["employees", "co2e"]}},
         {"key": "esg.mine", "kind": "group_types", "title": "Eigene",
          "definition": {"on": "Site", "attributes": ["co2e"]}}]
slots = tvw.period_slots(TYPES, "Site", {"esg.core": {"origin": "app:esg"}})
ok("43 Formular nur für Mandantengruppen (vorhanden oder neu), nie für die der App",
   [sl["key"] for sl in slots] == ["esg.mine"]
   and slots[0]["attributes"][0]["unit"] == "t", slots)

ok("44 ein Attribut periodisch machen (oder es nicht mehr sein lassen) ist eine zerstörende "
   "Änderung, eine Einheit dazu additiv",
   a.type_change_kind({"key": "k", "value_type": "decimal"},
                      {"key": "k", "value_type": "decimal", "periodic": True}) == "destructive"
   and a.type_change_kind({"key": "k", "periodic": True}, {"key": "k"}) == "destructive"
   and a.type_change_kind({"key": "k", "periodic": True},
                          {"key": "k", "periodic": True, "unit": "t"}) == "additive")

print("")
if FAILED:
    print("rot: " + ", ".join(FAILED))
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
