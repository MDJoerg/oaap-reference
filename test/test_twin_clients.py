#!/usr/bin/env python3
"""Integrations-Clients des Zwillings und die Tür /twin/v1 (RFC-0120 Stufe 0).

Festgehalten wird:

- ein Client liest genau die Typen, für die er gebunden ist; /twin/v1/types
  nennt nur sie, mit Zugriff „read“;
- der Mandant kommt aus der Eintragung, nie aus der Anfrage;
- ohne Eintragung ist derselbe Name wertlos;
- er schreibt nie (Stufe 0) -- Objekte, Gruppen und Zeitreihen sagen es;
- /twin/v1/openapi.json ist OpenAPI 3.1 und nennt nur seine Typen;
- ein Aufrufer `instance:` liest /twin/v1 mit seinen eigenen Bindungen.

psycopg2 ist durch eine Attrappe ersetzt, die Datenbank durch vorgegebene
Antworten -- wie test_twin_remote_reader.py.

Aufruf: python3 test/test_twin_clients.py
"""
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-twin-clients-")
os.environ["OAAP_DATA_DIR"] = DATA

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


fake_pg = types.ModuleType("psycopg2")
fake_pg.extras = types.ModuleType("psycopg2.extras")
fake_pg.extras.RealDictCursor = object
fake_pg.Error = Exception
fake_pg.connect = lambda **kw: None
sys.modules["psycopg2"] = fake_pg
sys.modules["psycopg2.extras"] = fake_pg.extras

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Dienst kann hier nicht laufen")
    sys.exit(0)

sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))
import app as twin                                             # noqa: E402

APPS = os.path.join(DATA, "platform-apps")
os.makedirs(APPS, exist_ok=True)
twin.REGISTRY_FILE = os.path.join(APPS, "registry.json")
twin.READERS_FILE = os.path.join(APPS, "twin-readers.json")
twin.CLIENTS_FILE = os.path.join(APPS, "twin-clients.json")
with open(twin.REGISTRY_FILE, "w") as f:
    json.dump({"instances": {"orders": {"tenant": "t1", "app_id": "orders"}}}, f)
with open(twin.CLIENTS_FILE, "w") as f:
    json.dump({"clients": {"graph": {"tenant": "t1", "reads": ["FleetNode"]}}}, f)

TYPES = [{"key": "FleetNode", "kind": "object",
          "definition": {"title": "Knoten", "semantic_id": "oaap-aas:Node",
                         "groups": ["oaap.software"]}},
         {"key": "Customer", "kind": "object", "definition": {}}]
BINDINGS = [{"type_key": "Customer", "direction": "contributes", "role": "owner",
             "group_key": None, "declared_as": "orders"}]
CONN_TENANTS = []
LAST_SQL = []


class Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        LAST_SQL.append(sql)

    def fetchall(self):
        return BINDINGS if "oaap_model.bindings" in LAST_SQL[-1] else TYPES

    def fetchone(self):
        return None


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return Cur()

    def close(self):
        pass


def fake_get_conn(tenant_id):
    CONN_TENANTS.append(tenant_id)
    return Conn()


twin.get_conn = fake_get_conn
c = twin.app.test_client()


def get(path, user, **headers):
    return c.get(path, headers=dict({"X-OAAP-User": user}, **headers))


print("=== /twin/v1/types für einen Client ===")
r = get("/twin/v1/types", "client:graph")
b = r.get_json() or {}
ok("200, nur FleetNode, Zugriff read, mit semantic_iri",
   r.status_code == 200 and [t["key"] for t in b.get("types", [])] == ["FleetNode"]
   and b["types"][0]["access"] == ["read"] and b["types"][0].get("semantic_iri"), b)
ok("der Mandant aus der Eintragung", CONN_TENANTS[-1] == "t1", CONN_TENANTS)
r = get("/twin/v1/types", "client:graph", **{"X-OAAP-Twin-Tenant": "t2", "X-OAAP-Tenant": "t2"})
ok("KÖDER ein Mandant in der Anfrage ändert nichts", CONN_TENANTS[-1] == "t1", CONN_TENANTS)
r = get("/twin/v1/types", "client:unbekannt")
ok("KÖDER ein Client ohne Eintragung: 403", r.status_code == 403, r.get_data(as_text=True))
r = get("/twin/v1/types", "jemand")
ok("KÖDER eine Person (kein Maschinen-Prinzipal): 403", r.status_code == 403)

print("=== OpenAPI ===")
r = get("/twin/v1/openapi.json", "client:graph")
d = r.get_json() or {}
ok("OpenAPI 3.1, Bearer, Pfade /types, /openapi.json die Lesewege der Stufen 1 und 2 und die Schreibwege der Stufe 3, nur seine Typen",
   r.status_code == 200 and d.get("openapi") == "3.1.0"
   and d["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
   and set(d["paths"]) == {"/types", "/openapi.json", "/objects", "/objects/{id}",
                           "/objects/{id}/graph", "/paths", "/export", "/changes",
                           "/objects/{id}/periods", "/series", "/series/{id}/range",
                           "/series/{id}/latest",
                           # Stufe 3: die Schreibwege
                           "/types", "/objects/by-key/{key}", "/objects/{id}/groups/{group}",
                           "/objects/{id}/groups/{group}/periods", "/objects/{id}/retire",
                           "/batch", "/webhooks", "/webhooks/{id}", "/webhooks/{id}/resume",
                           # D13: Empfänger für eingehende Webhooks
                           "/receivers", "/receivers/{name}", "/inbound/{client}/{name}",
                           # Stufe 4: Status
                           "/objects/{id}/status"}
   and d["x-oaap-types"] == [{"key": "FleetNode", "access": ["read"]}], d)

print("=== ein Client schreibt nie über die Wege einer App (Stufe 3: nur über /twin/v1) ===")
r = c.post("/twin/objects", headers={"X-OAAP-User": "client:graph"},
           json={"type": "FleetNode", "title": "x", "group": {"key": "oaap.software"}})
ok("KÖDER Objekt anlegen: 403 mit Satz, der auf /twin/v1 zeigt", r.status_code == 403
   and "integration client writes through its own door /twin/v1" in r.get_data(as_text=True),
   r.get_data(as_text=True))
r = c.put("/twin/objects/urn:oaap:obj:00000000-0000-0000-0000-000000000000/groups/oaap.software",
          headers={"X-OAAP-User": "client:graph"}, json={"attributes": {}})
ok("KÖDER Gruppe schreiben: 403 mit Satz", r.status_code == 403
   and "/twin/v1" in r.get_data(as_text=True), r.get_data(as_text=True))
r = c.post("/twin/points", headers={"X-OAAP-User": "client:graph"}, json={"points": []})
ok("KÖDER Zeitreihenpunkte schreiben: 403", r.status_code == 403, r.get_data(as_text=True))

print("=== eine App liest /twin/v1 mit ihren eigenen Bindungen ===")
r = get("/twin/v1/types", "instance:orders")
b = r.get_json() or {}
ok("200, Customer mit write (contributes), FleetNode nicht",
   r.status_code == 200 and [(t["key"], t["access"]) for t in b.get("types", [])]
   == [("Customer", ["write"])], b)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
