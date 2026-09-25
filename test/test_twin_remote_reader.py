#!/usr/bin/env python3
"""Der entfernte Leser des Zwillings (oaap.data.twin 0.4 §2.14,
RFC-0033 §6 / D10).

Gemessen am 25.09., bevor es ihn gab: der Tunnel trug den Aufruf,
identity nahm den Schluessel an, und der Zwilling lehnte ab -- er
schlug jeden Aufrufer in der Instanzliste DIESES Knotens nach.

Festgehalten wird:

- ein Leser liest genau die Typen, die der Betreiber ihm gab, und
  keinen anderen (14);
- er schreibt nie -- beide Schreibwege sagen es in einem eigenen Satz,
  bevor sie Bindungen ansehen (14);
- der Mandant kommt aus der Eintragung, nie aus der Anfrage (15);
- ohne Eintragung ist derselbe Schluessel wertlos (14);
- ein lokaler Aufrufer `instance:` verhaelt sich wie vorher;
- appctl: kein Typ ist voreingestellt, nur aktive Typen, das Geheimnis
  steht nicht in der Datei, und Entfernen widerruft und deaktiviert.

Der Dienst laeuft mit Flask; psycopg2 ist durch eine Attrappe ersetzt,
die Datenbank durch vorgegebene Antworten.

Aufruf: python3 test/test_twin_remote_reader.py
"""
import argparse
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-twin-reader-")
os.environ["OAAP_DATA_DIR"] = DATA

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


# --- psycopg2-Attrappe, bevor der Dienst importiert wird -------------------
fake_pg = types.ModuleType("psycopg2")
fake_pg.extras = types.ModuleType("psycopg2.extras")
fake_pg.extras.RealDictCursor = object
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
with open(twin.REGISTRY_FILE, "w") as f:
    json.dump({"instances": {"orders": {"tenant": "t1", "app_id": "orders"}}}, f)
with open(twin.READERS_FILE, "w") as f:
    json.dump({"readers": {"x01-aipc": {"tenant": "t1", "reads": ["machine"]}}}, f)

CONN_TENANTS = []


class Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        pass

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return Cur([{"key": "machine", "kind": "object"}, {"key": "customer", "kind": "object"}])

    def close(self):
        pass


def fake_get_conn(tenant_id):
    CONN_TENANTS.append(tenant_id)
    return Conn()


OBJ_TYPE = {"type": "machine"}
twin.get_conn = fake_get_conn
twin._load_object = lambda conn, obj_id, at: {
    "type_key": OBJ_TYPE["type"], "canonical_id": obj_id, "title": "M1",
    "owner": "app:orders", "groups": {}}
c = twin.app.test_client()
OID = "urn:oaap:obj:0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"


def get(path, user, **kw):
    return c.get(path, headers={"X-OAAP-User": user, "X-OAAP-Roles": "user"}, **kw)


r = get("/twin/types", "remote:x01-aipc")
body = r.get_json() or {}
ok("14 a remote reader sees /twin/types", r.status_code == 200, r.data)
ok("14 ... its bindings are exactly the types it was given, as consumes",
   [(b["type_key"], b["direction"]) for b in body.get("bindings", [])] == [("machine", "consumes")],
   body.get("bindings"))
ok("15 the tenant is the record's", CONN_TENANTS[-1] == "t1", CONN_TENANTS)

r = get(f"/twin/objects/{OID}", "remote:x01-aipc")
ok("14 it reads an object of a type it was given", r.status_code == 200, r.data)
OBJ_TYPE["type"] = "customer"
r = get(f"/twin/objects/{OID}", "remote:x01-aipc")
ok("14 ... and not one of another type", r.status_code == 403, r.data)
OBJ_TYPE["type"] = "machine"

r = c.post("/twin/objects", json={"type": "machine", "title": "x", "group": {"key": "core"}},
           headers={"X-OAAP-User": "remote:x01-aipc"})
ok("14 POST /twin/objects refused, saying read-only",
   r.status_code == 403 and b"reads only" in r.data, r.data)
r = c.put(f"/twin/objects/{OID}/groups/core", json={"attributes": {"a": 1}},
          headers={"X-OAAP-User": "remote:x01-aipc"})
ok("14 PUT a group refused, saying read-only",
   r.status_code == 403 and b"reads only" in r.data, r.data)

r = get("/twin/types", "remote:nobody")
ok("14 a remote name without a record is refused", r.status_code == 403, r.data)
r = get("/twin/types?tenant=t2", "remote:x01-aipc")
ok("15 a tenant in the request changes nothing", CONN_TENANTS[-1] == "t1", CONN_TENANTS)

r = get("/twin/types", "instance:ghost")
ok("a local caller not in the registry is refused as before",
   r.status_code == 403 and b"not a registered instance" in r.data, r.data)
r = get("/twin/types", "alice")
ok("a person is refused as before", r.status_code == 403, r.data)
ok("a registry entry can never answer for a remote name",
   twin.resolve_instance("remote:x01-aipc") is None)

# removing the record makes the same key worthless here, too
with open(twin.READERS_FILE, "w") as f:
    json.dump({"readers": {}}, f)
r = get("/twin/types", "remote:x01-aipc")
ok("14 after remove-reader the same principal is refused here", r.status_code == 403, r.data)

# --- appctl --------------------------------------------------------------
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import appctl as m                                             # noqa: E402

os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
CALLS = []


def fake_identity(script, env=None):
    CALLS.append((script, dict(env or {})))
    if "issue_key" in script:
        return json.dumps({"secret": "oaapk_abcd1234_SECRET", "id": "abcd1234",
                           "expires": "2026-12-24T00:00:00Z"}) + "\n"
    return json.dumps({"revoked": 1}) + "\n"


m._identity_exec = fake_identity


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except SystemExit:
        return True
    return False


ACTIVE = {"machine", "customer"}
ok("appctl: no type named -> refused", refused(m.twin_reader_add, "x01-aipc", DEFAULT, "",
                                                active_types=ACTIVE))
ok("appctl: a type not active for the tenant -> refused",
   refused(m.twin_reader_add, "x01-aipc", DEFAULT, "machine,ghost", active_types=ACTIVE))
ok("appctl: a bad label -> refused",
   refused(m.twin_reader_add, "X01", DEFAULT, "machine", active_types=ACTIVE))
key, expires = m.twin_reader_add("x01-aipc", DEFAULT, "machine", active_types=ACTIVE)
ok("appctl: the key comes back once", key == "oaapk_abcd1234_SECRET")
env = CALLS[-1][1]
ok("appctl: principal remote:<label> in the tenant, scoped to oaap.twin",
   env["OAAP_R_NAME"] == "remote:x01-aipc" and env["OAAP_R_TENANT"] == DEFAULT
   and env["OAAP_R_SCOPE"] == m.TWIN_KEY_SCOPE, env)
text = open(m.TWIN_READERS_FILE, encoding="utf-8").read()
rec = m.load_twin_readers()["x01-aipc"]
ok("appctl: the record names tenant and types", rec["tenant"] == DEFAULT and rec["reads"] == ["machine"])
ok("appctl: the record holds no secret", "SECRET" not in text)
ok("appctl: the record is where the twin looks",
   os.path.basename(m.TWIN_READERS_FILE) == os.path.basename(twin.READERS_FILE)
   and m.TWIN_REMOTE_PREFIX == twin.REMOTE_PREFIX)
ok("appctl: the same label twice -> refused",
   refused(m.twin_reader_add, "x01-aipc", DEFAULT, "machine", active_types=ACTIVE))
log = [json.loads(x) for x in open(m.TENANT_LOG, encoding="utf-8")]
ok("appctl: add is in the tenant's audit log",
   any(e["action"] == "twin.reader.add" and e["subject"] == "remote:x01-aipc" for e in log))
n = m.twin_reader_remove("x01-aipc")
ok("appctl: remove revokes in identity and deactivates",
   "revoke_key" in CALLS[-1][0] and "'active'] = False" in CALLS[-1][0] and n == 1)
ok("appctl: remove takes the record away", "x01-aipc" not in m.load_twin_readers())
ok("appctl: removing an unknown reader -> refused", refused(m.twin_reader_remove, "x01-aipc"))

# CLI shape
import argparse as _ap                                         # noqa: E402
captured = {}
real = _ap.ArgumentParser.parse_args


def grab(self, *a, **kw):
    captured["p"] = self
    raise SystemExit(0)


_ap.ArgumentParser.parse_args = grab
try:
    m.main()
except SystemExit:
    pass
finally:
    _ap.ArgumentParser.parse_args = real
a = captured["p"].parse_args(["data", "twin", "add-reader", "x01-aipc", "--tenant", "meier",
                              "--reads", "machine,customer", "--days", "30"])
ok("CLI: data twin add-reader parses",
   (a.object, a.action, a.arg1, a.tenant, a.reads, a.days)
   == ("twin", "add-reader", "x01-aipc", "meier", "machine,customer", 30))

print("")
print("OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
