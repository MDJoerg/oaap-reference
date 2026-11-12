#!/usr/bin/env python3
"""Zeitreihen ueber den Zwillingsdienst (oaap.data.timeseries 0.1 §3,
RFC-0086 §3/§8): der Dienst mit echtem Flask und echter PostgreSQL 16.

Die Datenbank ist echt, weil das, was hier still schiefginge, in ihr
passiert: eine offene Transaktion nach einer Ablehnung, ein Objekt eines
anderen Mandanten, ein Recht, das an der falschen Bindung haengt. Der
Zwilling selbst ist auf das Noetige verkleinert (objects, aliases,
oaap_model.bindings); Aufrufer und Mandant kommen wie im Betrieb aus den
Kopfzeilen, die der Gateway setzt.

Festgehalten wird:

- schreiben (anlegen, Punkte, loeschen) braucht `contributes` auf den Typ
  des OBJEKTS, lesen irgendeine Bindung; ein entfernter Leser liest nur;
- der Mandant kommt aus dem Schluessel: ein Objekt eines anderen Mandanten
  gibt es nicht, und ohne `oaap data timeseries enable` antwortet 409;
- eine zusammengelegte Kennung landet auf dem kanonischen Objekt;
- doppelte Punkte werden gezaehlt, nicht gespeichert;
- `/twin/points` schreibt, was erlaubt ist, und nennt den Grund fuer den Rest;
- das Minutenkontingent antwortet 429;
- Loeschen braucht die Bestaetigung und steht im Protokoll;
- keine Verbindung bleibt mitten in einer Transaktion stehen.

Aufruf: TS_DSN=... python3 test/test_timeseries_api.py
"""
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or detail == "" else f"  -- {str(detail)[:600]}"))


DSN = os.environ.get("TS_DSN", "")
if not DSN:
    print("SKIP  TS_DSN fehlt -- der Test braucht eine echte PostgreSQL.")
    sys.exit(2)

import psycopg2                                                # noqa: E402
import psycopg2.extras                                         # noqa: E402

import app as twin                                             # noqa: E402
import timeseries as ts                                        # noqa: E402

TENANTS = {"t-one": "twin_t-one", "t-two": "twin_t-two"}
admin = psycopg2.connect(DSN)
admin.autocommit = True
with admin.cursor() as cur:
    for sch in ("twin_t-one", "twin_t-two", "ts_t-one", "oaap_model"):
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    # The tenant's role as `oaap data timeseries enable` makes it: owns its
    # schema, may NOT create schemas in the database (the store is closed).
    # The service and its setup run as this role, never as the superuser --
    # the superuser hid 'CREATE SCHEMA IF NOT EXISTS' failing (oaap-test).
    cur.execute('DROP ROLE IF EXISTS "ts_t-one"')
    cur.execute('CREATE ROLE "ts_t-one" LOGIN PASSWORD \'probe-role\'')
    cur.execute('REVOKE CREATE ON DATABASE ' + admin.info.dbname + ' FROM PUBLIC')
    cur.execute('CREATE SCHEMA "ts_t-one" AUTHORIZATION "ts_t-one"')
    cur.execute("CREATE SCHEMA oaap_model")
    cur.execute("""CREATE TABLE oaap_model.bindings (instance text, type_key text,
                   direction text, role text, group_key text, declared_as text)""")
    for sch in ("twin_t-one", "twin_t-two"):
        cur.execute(f'CREATE SCHEMA "{sch}"')
        cur.execute(f'CREATE TABLE "{sch}".objects (id uuid PRIMARY KEY, type_key text)')
        cur.execute(f"""CREATE TABLE "{sch}".aliases (alias_id uuid, canonical_id uuid,
                        unmerged_at timestamptz)""")
    B = [("writer", "machine", "contributes", "owner"),
         ("writer", "site", "consumes", None),
         ("reader", "machine", "consumes", None),
         ("other", "pump", "contributes", "owner")]
    cur.executemany("INSERT INTO oaap_model.bindings VALUES (%s, %s, %s, %s, NULL, 'test')", B)
    M1, M2, SITE, GONE, FOREIGN = (str(uuid.uuid4()) for _ in range(5))
    cur.execute('INSERT INTO "twin_t-one".objects VALUES (%s, %s), (%s, %s), (%s, %s)',
                (M1, "machine", M2, "machine", SITE, "site"))
    # M2 was merged INTO M1: its id still answers and lands on M1
    cur.execute('INSERT INTO "twin_t-one".aliases VALUES (%s, %s, NULL)', (M2, M1))
    cur.execute('INSERT INTO "twin_t-two".objects VALUES (%s, %s)', (FOREIGN, "machine"))

INSTANCES = {"writer": "t-one", "reader": "t-one", "other": "t-one", "elsewhere": "t-two"}
twin.resolve_instance = lambda n: ({"tenant": INSTANCES[n], "origin": f"app:{n}"}
                                   if n in INSTANCES else None)
twin.resolve_remote = lambda label: ({"tenant": "t-one", "origin": "remote:" + label,
                                      "reads": ["machine"]} if label == "far" else None)


def fake_get_conn(tid):
    if tid not in TENANTS:
        return None
    return psycopg2.connect(DSN, options=f"-c search_path={TENANTS[tid]}",
                            cursor_factory=psycopg2.extras.RealDictCursor)


OPENED = []


def fake_get_ts(tid):
    if tid != "t-one":          # only t-one was enabled
        return None
    d = ts.PostgresDriver(psycopg2.connect(DSN, user="ts_t-one", password="probe-role"),
                          f"ts_{tid}")
    OPENED.append(d.conn)
    return d


twin.get_conn = fake_get_conn
twin.get_ts = fake_get_ts
setup = fake_get_ts("t-one")
try:
    setup.ensure_schema()
    made = None
except psycopg2.Error as e:
    made = f"{type(e).__name__}: {e}"
ok("die Tabellen entstehen mit der Rolle des Mandanten, ohne Recht auf neue Schemata",
   made is None, made)
setup.ensure_partitions(time.time())
setup.conn.close()

client = twin.app.test_client()


def call(method, path, who, **kw):
    r = client.open(path, method=method, headers={"X-OAAP-User": who}, **kw)
    try:
        body = r.get_json(silent=True)
    except Exception:  # noqa: BLE001
        body = None
    return r.status_code, (body if body is not None else r.get_data(as_text=True))


W, R, O, X, FAR = "instance:writer", "instance:reader", "instance:other", "instance:elsewhere", "remote:far"
NOW = int(time.time()) // 60 * 60

print("=== Anlegen ===")
code, b = call("PUT", "/twin/series", W, json={"object": f"urn:oaap:obj:{M1}", "measure": "temp",
                                                "unit": "Cel", "expected_s": 60})
ok("wer zum Typ beitraegt, legt an (201)", code == 201 and b["created"] is True, (code, b))
sid = b["series"]["id"] if code == 201 else None
ok("die Antwort nennt das Objekt als URN", code == 201 and b["series"]["object"] == f"urn:oaap:obj:{M1}", b)
code, b = call("PUT", "/twin/series", W, json={"object": M1, "measure": "temp", "unit": "Cel",
                                                "expected_s": 60})
ok("zum zweiten Mal: gefunden, nicht neu (200)", code == 200 and b["created"] is False
   and b["series"]["id"] == sid, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": M2, "measure": "temp", "unit": "Cel"})
ok("die zusammengelegte Kennung findet dieselbe Reihe", code == 200 and b["series"]["id"] == sid, (code, b))
code, b = call("PUT", "/twin/series", R, json={"object": M1, "measure": "rpm"})
ok("wer nur konsumiert, legt nicht an (403)", code == 403 and "does not contribute" in b, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": SITE, "measure": "rpm"})
ok("auch nicht auf einem Typ, den er nur konsumiert (403)", code == 403, (code, b))
code, b = call("PUT", "/twin/series", O, json={"object": M1, "measure": "rpm"})
ok("ein Beitraeger eines ANDEREN Typs legt nicht an (403)", code == 403, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": FOREIGN, "measure": "rpm"})
ok("ein Objekt eines anderen Mandanten gibt es nicht (404)", code == 404, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": GONE, "measure": "rpm"})
ok("ein unbekanntes Objekt (404)", code == 404, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": "x" * 36, "measure": "rpm"})
ok("eine kaputte Kennung (400)", code == 400, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": ["a"], "measure": "rpm"})
ok("eine Kennung, die kein Text ist (400)", code == 400, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": M1, "measure": "Temp!"})
ok("eine Ablehnung des Treibers wird 400 mit Grund", code == 400 and "measure" in b, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": M1, "measure": "temp", "kind": "text"})
ok("Art fest bei der Anlage (400)", code == 400 and "fixed at creation" in b, (code, b))
code, b = call("PUT", "/twin/series", FAR, json={"object": M1, "measure": "rpm"})
ok("ein entfernter Leser schreibt nicht (403)", code == 403 and "reads only" in b, (code, b))
code, b = call("PUT", "/twin/series", X, json={"object": FOREIGN, "measure": "rpm"})
ok("ein Mandant ohne Zeitreihen: 409 mit dem Befehl", code == 409 and "timeseries enable" in b, (code, b))
code, b = call("PUT", "/twin/series", "user:anna", json={"object": M1, "measure": "rpm"})
ok("eine Person kommt hier nicht herein (403)", code == 403, (code, b))
code, b = call("PUT", "/twin/series", W, json={"object": M1, "measure": "state", "kind": "text",
                                                "expected_s": 60})
tid_ = b["series"]["id"] if code == 201 else None
ok("eine Textreihe", code == 201 and b["series"]["kind"] == "text", (code, b))

print("\n=== Schreiben ===")
pts = [[NOW - 600 + 60 * i, float(i)] for i in range(10)]
code, b = call("POST", f"/twin/series/{sid}/points", W, json={"points": pts})
ok("zehn Punkte geschrieben", code == 200 and b["written"] == 10, (code, b))
code, b = call("POST", f"/twin/series/{sid}/points", W, json={"points": pts[:3]})
ok("drei nochmal: drei Duplikate, nichts geschrieben", code == 200 and b["written"] == 0
   and b["duplicate"] == 3, (code, b))
code, b = call("POST", f"/twin/series/{sid}/points", W, json={"points": [[NOW, "x"]]})
ok("ein Text in einer Zahlenreihe: abgelehnt mit Grund", code == 400, (code, b))
code, b = call("POST", f"/twin/series/{sid}/points", R, json={"points": [[NOW, 1.0]]})
ok("ein Konsument schreibt keine Punkte (403)", code == 403, (code, b))
code, b = call("POST", f"/twin/series/{sid}/points", FAR, json={"points": [[NOW, 1.0]]})
ok("ein entfernter Leser schreibt keine Punkte (403)", code == 403, (code, b))
code, b = call("POST", "/twin/series/999999/points", W, json={"points": [[NOW, 1.0]]})
ok("eine unbekannte Reihe (404)", code == 404, (code, b))
code, b = call("POST", f"/twin/series/{sid}/points", W, json={"points": "1,2"})
ok("Punkte, die keine Liste sind (400)", code == 400, (code, b))
code, b = call("POST", f"/twin/series/{tid_}/points", W,
               json={"points": [[NOW - 300, "running"], [NOW - 240, "running"], [NOW - 180, "stopped"]]})
ok("Text: zwei Zustaende", code == 200 and b["written"] == 2 and b["extended"] == 1, (code, b))
code, b = call("POST", f"/twin/series/{tid_}/points", W, json={"points": [[NOW, "x" * 65]]})
ok("65 Zeichen abgelehnt (400)", code == 400 and "64" in b, (code, b))

print("\n=== Mehrere Reihen in einem Aufruf ===")
setup = fake_get_ts("t-one")
site_s, _ = setup.ensure_series(SITE, "people")            # writer only consumes 'site'
setup.conn.close()
code, b = call("POST", "/twin/points", W, json={"series": {
    str(sid): [[NOW - 30, 99.0]], str(site_s["id"]): [[NOW - 30, 3.0]], "424242": [[NOW, 1.0]],
    "kein": [[NOW, 1.0]]}})
res = b.get("series", {}) if isinstance(b, dict) else {}
ok("die erlaubte Reihe ist geschrieben", code == 200 and res.get(str(sid), {}).get("written") == 1, (code, b))
ok("die nicht erlaubte nennt ihren Grund",
   "does not contribute" in res.get(str(site_s["id"]), {}).get("error", ""), res)
ok("die unbekannte und die kaputte ebenso",
   "no series" in res.get("424242", {}).get("error", "") and res.get("kein", {}).get("error"), res)

print("\n=== Lesen ===")
code, b = call("GET", f"/twin/series?object={M1}", R)
ok("ein Konsument liest die Reihen des Objekts", code == 200
   and {s["measure"] for s in b["series"]} == {"temp", "state"}, (code, b))
code, b = call("GET", f"/twin/series?object=urn:oaap:obj:{M2}", FAR)
ok("ein entfernter Leser auch, ueber die zusammengelegte Kennung", code == 200
   and b["object"] == f"urn:oaap:obj:{M1}" and len(b["series"]) == 2, (code, b))
code, b = call("GET", f"/twin/series?object={SITE}", FAR)
ok("aber nicht die eines Typs, den er nicht liest (403)", code == 403, (code, b))
code, b = call("GET", f"/twin/series?object={M1}", O)
ok("ohne Bindung an den Typ kein Lesen (403)", code == 403, (code, b))
code, b = call("GET", f"/twin/series/{sid}/latest", R)
ok("der letzte Punkt", code == 200 and b["latest"]["x"] == 99.0, (code, b))
code, b = call("GET", f"/twin/series/{sid}/range?from={NOW - 3600}&to={NOW + 60}&step=300", R)
ok("ein Bereich mit Schritt 5 min: Minimum und Maximum erhalten", code == 200 and b["buckets"]
   and min(x["min"] for x in b["buckets"]) == 0.0 and max(x["max"] for x in b["buckets"]) == 99.0, (code, b))
code, b = call("GET", f"/twin/series/{sid}/range?from={NOW - 3600}", R)
ok("ohne 'to' (400)", code == 400, (code, b))
code, b = call("GET", f"/twin/series/{sid}/range?from={NOW - 3600}&to={NOW}&step=fünf", R)
ok("ein Schritt, der keine Zahl ist (400)", code == 400, (code, b))
code, b = call("GET", f"/twin/series/{tid_}/range?from={NOW - 3600}&to={NOW + 60}", R)
ok("Text ohne Schritt: die Zustaende", code == 200
   and [i["value"] for i in b.get("intervals", [])] == ["running", "stopped"], (code, b))
code, b = call("GET", f"/twin/series/{sid}/latest", O)
ok("der letzte Punkt nur mit Bindung (403)", code == 403, (code, b))

print("\n=== Minutenkontingent ===")
twin.TS_POINTS_PER_MINUTE = 5
code, b = call("POST", f"/twin/series/{sid}/points", W,
               json={"points": [[NOW + 600 + 60 * i, 1.0] for i in range(6)]})
ok("mehr als das Kontingent in einem Aufruf: 429", code == 429, (code, b))
twin.TS_POINTS_PER_MINUTE = ts.DEFAULT_POINTS_PER_MINUTE

print("\n=== Fehler des Speichers ===")
with admin.cursor() as cur:
    cur.execute('ALTER TABLE "ts_t-one".series RENAME TO series_away')
code, b = call("GET", f"/twin/series?object={M1}", R)
with admin.cursor() as cur:
    cur.execute('ALTER TABLE "ts_t-one".series_away RENAME TO series')
ok("eine fehlende Tabelle: 503 mit Grund, kein nacktes 500", code == 503
   and "store failed" in b, (code, b))

print("\n=== Loeschen ===")
code, b = call("DELETE", f"/twin/series/{sid}", W)
ok("ohne Bestaetigung nichts (409)", code == 409 and f"confirm={sid}" in b, (code, b))
code, b = call("DELETE", f"/twin/series/{sid}?confirm={sid}", R)
ok("ein Konsument loescht nicht (403)", code == 403, (code, b))
code, b = call("DELETE", f"/twin/series/{sid}?confirm={sid}", W)
ok("mit Bestaetigung geloescht", code == 200 and b["deleted"]["id"] == sid, (code, b))
code, b = call("GET", f"/twin/series/{sid}/latest", W)
ok("danach 404", code == 404, (code, b))
with admin.cursor() as cur:
    cur.execute('SELECT who, detail FROM "ts_t-one".audit WHERE series_id = %s', (sid,))
    rows = cur.fetchall()
ok("im Protokoll: wer und wie viele Punkte", len(rows) == 1 and rows[0][0] == "writer"
   and rows[0][1]["raw_points"] == 11, rows)

print("\n=== Verbindungen ===")
ok("jede Verbindung des Dienstes ist geschlossen", OPENED and all(c.closed for c in OPENED),
   sum(not c.closed for c in OPENED))
with admin.cursor() as cur:
    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'")
    idle = cur.fetchone()[0]
ok("keine bleibt mitten in einer Transaktion", idle == 0, idle)

with admin.cursor() as cur:
    for sch in ("twin_t-one", "twin_t-two", "ts_t-one", "oaap_model"):
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    cur.execute('DROP ROLE IF EXISTS "ts_t-one"')
admin.close()
print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
