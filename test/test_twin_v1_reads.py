#!/usr/bin/env python3
"""Die Lesetür /twin/v1 (RFC-0120 Stufe 1) gegen eine echte PostgreSQL.

Ein kleiner Zwilling mit Geschichte: zwei Hallen, sieben Anlagen, ein Objekt
eines Typs, den der Client nicht lesen darf, eine beendete Beziehung, eine
Zusammenführung und ein Wert, der sich vor einer Stunde geändert hat. Ein
Integrations-Client liest ihn über die Routen des Dienstes.

Festgehalten wird:

- Listen: nach Titel geordnet, mit Blätter-Marke vollständig und ohne
  Doppelte; Filter nach Werttyp (Zahl, ja/nein, in, has); ein falscher Filter,
  ein unbekanntes Attribut, ein fremder Typ: 400 mit Satz;
- `at`: was damals galt (alter Wert, ein später angelegtes Objekt fehlt, eine
  spätere Zusammenführung noch nicht geschehen);
- eine zusammengeführte Kennung antwortet als ihr Objekt, ihre Beziehungen
  zählen dort mit;
- Nachbarschaft und Pfade: eine beendete Beziehung zählt nicht, ein Objekt
  eines fremden Typs erscheint nie, auch nicht als Ende einer Kante;
- Export JSON und GraphML: dieselben Knoten und Kanten, persönliche Attribute
  fehlen (D10);
- Änderungs-Feed: in Reihenfolge, seitenweise, ohne fremde Typen, `now`;
- der Mandant kommt aus der Eintragung des Clients: ein Client eines anderen
  Mandanten sieht nichts davon.

Aufruf: TS_DSN=... python3 test/test_twin_v1_reads.py   (im Linux-Runner)
"""
import datetime
import json
import os
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-twin-v1-"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
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
import twin_read as tr              # noqa: E402

# ------------------------------------------------------------ the database
DDL = []
real = a._store_psql
a._store_psql = lambda sql, *x, **k: DDL.append(sql) or type("R", (), {"stdout": "", "returncode": 0})()
a._model_ensure_schema()
a._twin_ensure_tables("twin_t1")
a._twin_ensure_tables("twin_t2")
a._store_psql = real
admin = psycopg2.connect(DSN)
admin.autocommit = True
with admin.cursor() as cur:
    for sch in ("oaap_model", "twin_t1", "twin_t2"):
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    for sch in ("twin_t1", "twin_t2"):
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


def typ(key, kind, d):
    q("INSERT INTO oaap_model.type_definitions (key, kind, origin, package, version, definition) "
      "VALUES (%s, %s, 'app:plant', 'plant', '1.0.0', %s)", key, kind, json.dumps(d))


typ("Site", "object_types", {"key": "Site", "title": "Halle"})
typ("Machine", "object_types", {"key": "Machine", "title": "Anlage", "semantic_id": "oaap-aas:Machine"})
typ("Secret", "object_types", {"key": "Secret", "title": "Geheim"})
typ("power", "attribute_types", {"key": "power", "value_type": "decimal"})
typ("active", "attribute_types", {"key": "active", "value_type": "bool"})
typ("serial", "attribute_types", {"key": "serial", "value_type": "text", "personal": True})
typ("plant.core", "group_types", {"key": "plant.core", "on": "Machine",
                                  "attributes": ["power", "active", "serial"]})
typ("isPartOf", "relation_types", {"key": "isPartOf"})
for t in ("Site", "Machine", "Secret", "power", "active", "serial", "plant.core"):
    q("INSERT INTO oaap_model.activations (tenant_id, type_key) VALUES ('t1', %s)", t)
for t in ("Site", "Machine"):
    q("INSERT INTO oaap_model.activations (tenant_id, type_key) VALUES ('t2', %s)", t)
q("INSERT INTO oaap_model.bindings (instance, type_key, direction, role) "
  "VALUES ('plant', 'Machine', 'contributes', 'owner')")

NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
H = datetime.timedelta(hours=1)
T0 = NOW - 3 * H
YESTERDAY = (NOW - 24 * H).date()
IDS = {}


def obj(label, type_key, title, at=T0, group="plant.core"):
    i = str(uuid.uuid4())
    IDS[label] = i
    q("INSERT INTO twin_t1.objects (id, type_key, title, owner_origin, recorded_at, recorded_by) "
      "VALUES (%s, %s, %s, 'app:plant', %s, 'plant')", i, type_key, title, at)
    q("INSERT INTO twin_t1.groups (object_id, group_key, origin, created_at, created_by) "
      "VALUES (%s, %s, 'app:plant', %s, 'plant')", i, group, at)
    q("INSERT INTO twin_t1.events (recorded_at, kind, object_id, group_key, origin) "
      "VALUES (%s, 'object.created', %s, %s, 'app:plant')", at, i, group)
    return i


def attr(label, key, value, at=T0, group="plant.core"):
    rows = q("INSERT INTO twin_t1.attributes (object_id, group_key, attr_key, value, recorded_at, "
             "recorded_by) VALUES (%s, %s, %s, %s, %s, 'plant') RETURNING id",
             IDS[label], group, key, json.dumps(value), at)
    new = rows[0]["id"]
    q("UPDATE twin_t1.attributes SET superseded_by = %s WHERE object_id = %s AND group_key = %s "
      "AND attr_key = %s AND id <> %s AND superseded_by IS NULL", new, IDS[label], group, key, new)


def rel(src, key, dst, group="plant.core", valid_to=None, at=T0):
    q("INSERT INTO twin_t1.relations (object_id, group_key, rel_key, target_id, valid_to, "
      "recorded_at, recorded_by) VALUES (%s, %s, %s, %s, %s, %s, 'plant')",
      IDS[src], group, key, IDS[dst], valid_to, at)


obj("S1", "Site", "Halle Nord", group="plant.site")
obj("S2", "Site", "Halle Süd", group="plant.site")
for lab, title in (("M1", "Anlage A"), ("M2", "Anlage B"), ("M3", "Anlage C"),
                   ("M4", "Anlage D"), ("M5", "Anlage E"), ("M6", "Anlage B (Dublette)")):
    obj(lab, "Machine", title)
obj("M7", "Machine", "Anlage F", at=NOW - datetime.timedelta(minutes=10))
obj("H1", "Secret", "Geheim")
attr("M1", "power", 10, at=T0)
attr("M1", "power", 20, at=NOW - H)
attr("M1", "active", True)
attr("M1", "serial", "SN-1")
attr("M2", "power", 5)
attr("M2", "active", False)
attr("M3", "power", 30)
attr("M3", "active", True)
attr("M5", "power", 40)
rel("M1", "isPartOf", "S1")
rel("M2", "isPartOf", "S1")
rel("M3", "isPartOf", "S2")
rel("M4", "uses", "H1")
rel("M5", "isPartOf", "S2", valid_to=YESTERDAY)
rel("M6", "isPartOf", "S2")
rel("S1", "feeds", "S2", group="plant.site")
MERGED_AT = NOW - datetime.timedelta(minutes=30)
q("INSERT INTO twin_t1.aliases (alias_id, canonical_id, merged_at, merged_by) "
  "VALUES (%s, %s, %s, 'jo')", IDS["M6"], IDS["M2"], MERGED_AT)
q("INSERT INTO twin_t1.events (recorded_at, kind, object_id, group_key, origin) "
  "VALUES (%s, 'group.written', %s, 'plant.core', 'app:plant')", NOW - 2 * H, IDS["H1"])
q("INSERT INTO twin_t1.events (recorded_at, kind, object_id, group_key, origin) "
  "VALUES (%s, 'group.written', %s, 'plant.core', 'app:plant')", NOW - H, IDS["M6"])

# ------------------------------------------------------------- the service
APPS = os.path.join(os.environ["OAAP_DATA_DIR"], "platform-apps")
os.makedirs(APPS, exist_ok=True)
twin.REGISTRY_FILE = os.path.join(APPS, "registry.json")
twin.CLIENTS_FILE = os.path.join(APPS, "twin-clients.json")
twin.READERS_FILE = os.path.join(APPS, "twin-readers.json")
with open(twin.REGISTRY_FILE, "w") as f:
    json.dump({"instances": {"plant": {"tenant": "t1", "app_id": "plant"}}}, f)
with open(twin.CLIENTS_FILE, "w") as f:
    json.dump({"clients": {"viewer": {"tenant": "t1", "reads": ["Site", "Machine"]},
                           "fremd": {"tenant": "t2", "reads": ["Site", "Machine"]}}}, f)
CONN_TENANTS = []


def get_conn(tid):
    CONN_TENANTS.append(tid)
    return psycopg2.connect(DSN, options=f"-c search_path=twin_{tid}",
                            cursor_factory=psycopg2.extras.RealDictCursor)


twin.get_conn = get_conn
client = twin.app.test_client()


def get(path, who="client:viewer", **params):
    rv = client.get(path, query_string=params, headers={"X-OAAP-User": who})
    try:
        body = rv.get_json()
    except Exception:
        body = None
    return rv.status_code, body if body is not None else rv.get_data(as_text=True)


def urn(label):
    return "urn:oaap:obj:" + IDS[label]


def titles(objs):
    return [o["title"] for o in objs]


# ------------------------------------------------------------------- lists
st, b = get("/twin/v1/objects", type="Machine")
ok("1 Liste: alle Anlagen nach Titel, die Dublette nicht als eigenes Objekt",
   st == 200 and titles(b["objects"]) == ["Anlage A", "Anlage B", "Anlage C", "Anlage D",
                                          "Anlage E", "Anlage F"] and b["next"] is None, b)
seen, cur, pages = [], None, 0
while True:
    params = {"type": "Machine", "limit": 2}
    if cur:
        params["cursor"] = cur
    st, b = get("/twin/v1/objects", **params)
    pages += 1
    seen += titles(b["objects"])
    cur = b["next"]
    if not cur or pages > 10:
        break
ok("2 Blätter-Marke: drei Seiten zu zwei, vollständig, ohne Doppelte",
   pages == 3 and seen == ["Anlage A", "Anlage B", "Anlage C", "Anlage D", "Anlage E", "Anlage F"],
   (pages, seen))
st, b = get("/twin/v1/objects", type="Machine", where="plant.core.power>=20")
ok("3 Filter als Zahl: Leistung >= 20 -> A (20), C (30), E (40)",
   st == 200 and titles(b["objects"]) == ["Anlage A", "Anlage C", "Anlage E"], b)
st, b = get("/twin/v1/objects", type="Machine", where=["plant.core.active=true",
                                                       "plant.core.power<25"])
ok("4 zwei Filter zusammen (ja/nein und Zahl): nur A", titles(b["objects"]) == ["Anlage A"], b)
st, b = get("/twin/v1/objects", type="Machine", where="plant.core.power in 5,30")
ok("5 'in': B und C", titles(b["objects"]) == ["Anlage B", "Anlage C"], b)
st, b = get("/twin/v1/objects", type="Machine", where="plant.core.power has")
ok("6 'has': die mit Leistung (A, B, C, E)",
   titles(b["objects"]) == ["Anlage A", "Anlage B", "Anlage C", "Anlage E"], b)
st, b = get("/twin/v1/objects", type="Machine", where="plant.core.power>=20", limit=1)
st2, b2 = get("/twin/v1/objects", type="Machine", where="plant.core.power>=20", limit=1,
              cursor=b["next"])
ok("7 Filter und Blättern zusammen: Seite 2 beginnt hinter Seite 1",
   titles(b["objects"]) == ["Anlage A"] and titles(b2["objects"]) == ["Anlage C"], (b, b2))
st, b = get("/twin/v1/objects", type="Machine", fields="")
ok("8 fields leer: nur Kopf, keine Gruppen",
   st == 200 and all(o["groups"] == {} for o in b["objects"]), b)
bad = [get("/twin/v1/objects", type="Machine", where="plant.core.power>=viel"),
       get("/twin/v1/objects", type="Machine", where="plant.core.farbe=rot"),
       get("/twin/v1/objects", type="Machine", where="plant.core.active<true"),
       get("/twin/v1/objects", type="Secret"),
       get("/twin/v1/objects", type="Machine", cursor="kaputt"),
       get("/twin/v1/objects", type="Machine", at="gestern")]
ok("9 KÖDER falscher Wert, unbekanntes Attribut, ja/nein mit <, fremder Typ, kaputte Marke, "
   "falsches at: je 400 mit Satz",
   all(s == 400 and isinstance(x, dict) and x.get("error") for s, x in bad)
   and "decimal" in bad[0][1]["error"] and "farbe" in bad[1][1]["error"]
   and "Secret" in bad[3][1]["error"], bad)

# ---------------------------------------------------------------------- at
then = (NOW - datetime.timedelta(minutes=90)).isoformat().replace("+00:00", "Z")
st, b = get("/twin/v1/objects", type="Machine", at=then)
a_then = next((o for o in b.get("objects", []) if o["title"] == "Anlage A"), {})
ok("10 at vor 90 Minuten: A hatte Leistung 10, F gab es noch nicht, die Dublette war noch eigen",
   st == 200 and a_then.get("groups", {}).get("plant.core", {}).get("attributes", {}).get("power") == 10
   and "Anlage F" not in titles(b["objects"]) and "Anlage B (Dublette)" in titles(b["objects"])
   and b["at"].startswith(then[:16]), b)
st, b = get("/twin/v1/objects", type="Machine")
a_now = next(o for o in b["objects"] if o["title"] == "Anlage A")
ok("11 jetzt: A hat 20, das persönliche Attribut steht in der Liste (D10 gilt dem Export)",
   a_now["groups"]["plant.core"]["attributes"].get("power") == 20
   and a_now["groups"]["plant.core"]["attributes"].get("serial") == "SN-1", a_now)

# ------------------------------------------------------------------- merge
st, b = get(f"/twin/v1/objects/{IDS['M6']}")
rels = [r for g in b.get("groups", {}).values() for r in g["relations"]]
ok("12 die Kennung der Dublette antwortet als B, mit merged_from, ihre Beziehung zu Halle Süd zählt",
   st == 200 and b["id"] == urn("M2") and b.get("merged_from") == urn("M6")
   and {"key": "isPartOf", "target": urn("S2")} in rels
   and {"key": "isPartOf", "target": urn("S1")} in rels, b)
st, b = get(f"/twin/v1/objects/{IDS['M4']}")
ok("13 D: die Beziehung zum fremden Typ fehlt", st == 200 and all(
    not g["relations"] for g in b["groups"].values()), b)
st, b = get(f"/twin/v1/objects/{IDS['H1']}")
ok("14 KÖDER ein Objekt des fremden Typs: 404", st == 404, (st, b))

# ------------------------------------------------------------------- graph
st, b = get(f"/twin/v1/objects/{IDS['S1']}/graph", depth=1)
ids1 = {n["id"] for n in b.get("nodes", [])}
ok("15 Nachbarschaft Halle Nord, Tiefe 1: A, B, Halle Süd",
   st == 200 and ids1 == {urn("S1"), urn("M1"), urn("M2"), urn("S2")}, b)
st, b = get(f"/twin/v1/objects/{IDS['S1']}/graph", depth=2)
ids2 = {n["id"] for n in b["nodes"]}
ends = {e["source"] for e in b["edges"]} | {e["target"] for e in b["edges"]}
ok("16 Tiefe 2: dazu C; E nicht (Beziehung beendet), nie das geheime Objekt, jede Kante zwischen Knoten",
   ids2 == ids1 | {urn("M3")} and urn("M5") not in ids2 and urn("H1") not in ends
   and ends <= ids2, b)
ok("17 die Kante der Dublette hängt an B (B -> Halle Süd)",
   {"source": urn("M2"), "key": "isPartOf", "target": urn("S2"), "group": "plant.core"}
   in b["edges"], b["edges"])
st, b = get(f"/twin/v1/objects/{IDS['S1']}/graph", depth=2, direction="in")
ok("18 direction=in: nur wer auf Halle Nord zeigt (A, B)",
   {n["id"] for n in b["nodes"]} == {urn("S1"), urn("M1"), urn("M2")}, b)
st, b = get(f"/twin/v1/objects/{IDS['S1']}/graph", depth=2, types="Site")
ok("19 types=Site: der Weg betritt nur Hallen", {n["id"] for n in b["nodes"]} == {urn("S1"), urn("S2")}, b)
st, b = get(f"/twin/v1/objects/{IDS['S1']}/graph", depth=2, at=then)
ok("20 at vor 90 Minuten: B war noch nicht mit Halle Süd verbunden (Dublette eigen)",
   {"source": urn("M2"), "key": "isPartOf", "target": urn("S2"), "group": "plant.core"}
   not in b["edges"] and urn("M6") in {n["id"] for n in b["nodes"]}, b)

# ------------------------------------------------------------------- paths
st, b = get("/twin/v1/paths", **{"from": urn("M1"), "to": urn("M3")})
ok("21 kürzester Weg A -> C: über beide Hallen, Länge 3, ein Weg",
   st == 200 and [p["nodes"] for p in b["paths"]] == [[urn("M1"), urn("S1"), urn("S2"), urn("M3")]], b)
st, b = get("/twin/v1/paths", relations="isPartOf", **{"from": urn("M1"), "to": urn("M3")})
ok("22 nur isPartOf: A -> Halle Nord <- B -> Halle Süd <- C (über die Dublette)",
   [p["nodes"] for p in b["paths"]] == [[urn("M1"), urn("S1"), urn("M2"), urn("S2"), urn("M3")]]
   and b["paths"][0]["length"] == 4, b)
st, b = get("/twin/v1/paths", **{"from": urn("M4"), "to": urn("M1")})
ok("23 D hängt nur am geheimen Objekt: kein Weg", st == 200 and b["paths"] == [], b)
st, b = get("/twin/v1/paths", **{"from": urn("M1"), "to": urn("H1")})
ok("24 KÖDER Weg zum geheimen Objekt: 404", st == 404, (st, b))

# ------------------------------------------------------------------ export
st, ex = get("/twin/v1/export")
nodes = {n["id"]: n for n in ex.get("nodes", [])}
serials = [g["attributes"].get("serial") for n in ex.get("nodes", [])
           for g in n.get("groups", {}).values()]
ok("25 Export JSON: 8 Knoten (2 Hallen, 6 Anlagen), keine Kante zum geheimen Objekt",
   st == 200 and len(nodes) == 8 and urn("H1") not in nodes
   and all(e["source"] in nodes and e["target"] in nodes for e in ex["edges"]), ex)
ok("26 Export: das persönliche Attribut fehlt (D10), semantic_iri des Typs steht dabei",
   not any(serials) and any(t.get("semantic_iri") for t in ex["types"] if t["key"] == "Machine"),
   (serials, ex.get("types")))
st, gml = get("/twin/v1/export", format="graphml")
root = ET.fromstring(gml)
ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
ok("27 Export GraphML: gültiges XML, dieselben Knoten und Kanten wie JSON",
   st == 200 and len(root.findall(".//g:node", ns)) == 8
   and len(root.findall(".//g:edge", ns)) == len(ex["edges"]), gml[:400])
st, b = get("/twin/v1/export", types="Site", attributes="0")
ok("28 Export nur Hallen, ohne Attribute: 2 Knoten, die Kante feeds",
   len(b["nodes"]) == 2 and [e["key"] for e in b["edges"]] == ["feeds"]
   and "groups" not in b["nodes"][0], b)

# ----------------------------------------------------------------- changes
st, b = get("/twin/v1/changes", limit=3)
first = b.get("changes", [])
st2, b2 = get("/twin/v1/changes", since=b.get("next"), limit=100)
allc = first + b2.get("changes", [])
ok("29 Feed: seitenweise, aufsteigend, ohne Lücke zwischen den Seiten",
   st == 200 and b["more"] and len(first) == 3 and b2["more"] is False
   and [c["id"] for c in allc] == sorted(c["id"] for c in allc), (b, b2))
ok("30 Feed: nichts vom fremden Typ; die Änderung an der Dublette zeigt auf B mit merged_from",
   all(c["type"] != "Secret" for c in allc)
   and any(c["object"] == urn("M2") and c.get("merged_from") == urn("M6") for c in allc), allc)
st, b = get("/twin/v1/changes", since="now")
st2, b2 = get("/twin/v1/changes", since=b["next"])
ok("31 since=now: leer, next = letztes Ereignis; danach nichts Neues",
   b["changes"] == [] and b2["changes"] == [] and b2["next"] == b["next"], (b, b2))
q("INSERT INTO twin_t1.events (kind, object_id, group_key, origin) "
  "VALUES ('group.written', %s, 'plant.core', 'app:plant')", IDS["M1"])
st, b3 = get("/twin/v1/changes", since=b["next"])
ok("32 ein Ereignis jünger als die Wartezeit erscheint noch nicht (keine Lücke durch späte Commits)",
   b3["changes"] == [], b3)

# --------------------------------------------------------- tenant, callers
CONN_TENANTS.clear()
st, b = get("/twin/v1/objects", who="client:fremd", type="Machine")
st2, b2 = get("/twin/v1/export", who="client:fremd")
ok("33 Client eines anderen Mandanten: liest dessen (leeren) Zwilling, nie t1",
   st == 200 and b["objects"] == [] and b2["nodes"] == [] and set(CONN_TENANTS) == {"t2"},
   (b, CONN_TENANTS))
st, b = get("/twin/v1/objects", who="instance:plant", type="Machine")
st2, b2 = get("/twin/v1/objects", who="instance:plant", type="Site")
ok("34 eine App liest /twin/v1 mit ihren Bindungen: Anlagen ja, Hallen nicht",
   st == 200 and len(b["objects"]) == 6 and st2 == 400, (st, st2, b2))
st, b = get("/twin/v1/objects", who="jo", type="Machine")
ok("35 KÖDER eine Person an der Tür: 403", st == 403, (st, b))
st, doc = get("/twin/v1/openapi.json")
ok("36 OpenAPI nennt die Lesewege", all(p in doc["paths"] for p in (
    "/objects", "/objects/{id}", "/objects/{id}/graph", "/paths", "/export", "/changes")),
   list(doc.get("paths", {})))

# ------------------------------------------------------------- pure parts
ok("37 at als Datum = Ende dieses Tages (UTC)",
   tr.parse_at("2026-11-22").isoformat() == "2026-11-22T23:59:59.999999+00:00")
c = tr.encode_cursor("Anlage ö", IDS["M1"])
ok("38 Blätter-Marke hin und zurück, auch mit Umlaut", tr.decode_cursor(c) == ("Anlage ö", IDS["M1"]))

print(f"\n{'ALLE GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
