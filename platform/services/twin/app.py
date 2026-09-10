"""oaap.data.twin 0.1 — the digital twin (RFC-0031 Schritt 3).

The only service an app talks to for shared tenant data. It never
learns a tenant or an origin from a request -- both come from the
caller's own credential (RFC-0031 §8), resolved the same way every
other protected route on this platform resolves a caller: the gateway
verifies the presented API key against `identity` and hands this
service the result as trusted headers (`X-OAAP-User`, `X-OAAP-Roles`),
exactly as it hands them to an app. This service additionally requires
`X-OAAP-User` to be a MACHINE principal named `instance:<name>` (RFC-
0027 3.1, minted at install time -- see appctl.py's `_twin_issue_
instance_key`); a human session reaching this route is refused, because
nothing here is meant for a browser.

0.1 SCOPE (RFC-0031 §9, "steps 1-3 are the minimum for the first
build"): create an object as its owner, with its owner's core group;
read an object and every group its type is bound to (contributes OR
consumes); a contributor writes into its own group on a foreign
object. Recorded time always (append, never overwrite); validity
(`valid_from`/`valid_to`) is carried on attributes and relations and
returned, but nothing here FILTERS by it yet -- no `?at=`, no tree, no
`references` search, no merge, no restricted groups, no outbox reader.
Each is out of scope on purpose, not forgotten -- see the capability
spec's §2 for the complete list deferred to 0.2.

Every write also appends one row to `events` -- the outbox RFC-0032
will read from; nothing reads it yet.
"""
import json
import os
import re
import uuid

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request

app = Flask(__name__)

PLATFORM_APPS_DIR = "/platform-apps"
REGISTRY_FILE = os.path.join(PLATFORM_APPS_DIR, "registry.json")
SECRETS_FILE = os.path.join(PLATFORM_APPS_DIR, "twin-secrets.json")
STORE_HOST = os.environ.get("STORE_HOST", "store")
STORE_PORT = int(os.environ.get("STORE_PORT", "5432"))

OBJ_ID_RE = re.compile(r"^(?:urn:oaap:obj:)?([0-9a-fA-F-]{36})$")
# The manifest already constrains these (appctl.validate_data_model_sections);
# re-checked here because this service never trusts a caller either.
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_registry():
    return _load_json(REGISTRY_FILE, {"instances": {}})


def load_secrets():
    return _load_json(SECRETS_FILE, {})


def resolve_instance(name):
    """{'tenant': ..., 'origin': 'app:<id>'} for a registered instance
    name, or None. Never trusts anything the CALLER said about itself
    beyond its own principal name (RFC-0031 §8)."""
    inst = load_registry().get("instances", {}).get(name)
    if inst is None:
        return None
    return {"tenant": inst["tenant"], "origin": f"app:{inst['app_id']}"}


def require_caller():
    """(instance_name, tenant, origin) or None -- writes the 401/403
    onto Flask's request-local error path via abort-by-return.

    X-OAAP-User is set by identity's own /verify, forwarded by the
    gateway's forward_auth (Caddyfile '/twin/*') -- never something a
    client can set itself (the gateway strips/overwrites it on every
    route, per the deployment contract every other protected route
    already relies on)."""
    user = request.headers.get("X-OAAP-User", "")
    if not user.startswith("instance:"):
        return None, ("this route is for an app instance's own credential, "
                       "not a person (RFC-0031 §8)"), 403
    name = user[len("instance:"):]
    resolved = resolve_instance(name)
    if resolved is None:
        return None, f"'{name}' is not a registered instance", 403
    return (name, resolved["tenant"], resolved["origin"]), None, None


def get_conn(tenant_id):
    """A fresh connection scoped to this tenant's OWN schema and role
    (oaap.data.store 0.1 §2: 'shown once ... never for an app, only for
    oaap.data.twin') -- the credential lives only in SECRETS_FILE,
    mounted read-only into this container and nowhere else."""
    schema = f"twin_{tenant_id}"
    creds = load_secrets().get(schema)
    if creds is None:
        return None
    return psycopg2.connect(
        host=STORE_HOST, port=STORE_PORT, dbname="postgres",
        user=creds["role"], password=creds["password"],
        options=f"-c search_path={schema}",
        cursor_factory=psycopg2.extras.RealDictCursor)


def _bindings(conn, instance):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT type_key, direction, role, group_key, declared_as "
            "FROM oaap_model.bindings WHERE instance = %s", (instance,))
        return cur.fetchall()


def _type_active(conn, tenant_id, type_key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.key, d.kind, d.definition FROM oaap_model.type_definitions d "
            "JOIN oaap_model.activations a ON a.type_key = d.key "
            "WHERE a.tenant_id = %s AND d.key = %s", (tenant_id, type_key))
        return cur.fetchone()


def _record_event(conn, kind, object_id, group_key, origin):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (kind, object_id, group_key, origin) "
            "VALUES (%s, %s, %s, %s)", (kind, object_id, group_key, origin))


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/twin/types")
def twin_types():
    caller, err, code = require_caller()
    if err:
        return err, code
    _name, tenant_id, _origin = caller
    conn = get_conn(tenant_id)
    if conn is None:
        return ("this tenant has no twin schema yet -- install an app that "
                "contributes or consumes a type first"), 409
    try:
        with conn:
            bindings = _bindings(conn, _name)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.key, d.kind FROM oaap_model.type_definitions d "
                    "JOIN oaap_model.activations a ON a.type_key = d.key "
                    "WHERE a.tenant_id = %s ORDER BY d.key", (tenant_id,))
                active = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"active": active, "bindings": bindings})


@app.post("/twin/objects")
def create_object():
    caller, err, code = require_caller()
    if err:
        return err, code
    name, tenant_id, origin = caller
    body = request.get_json(silent=True) or {}
    type_key = (body.get("type") or "").strip()
    title = (body.get("title") or "").strip()
    if not type_key or not title:
        return "body needs 'type' and 'title'", 400
    group = body.get("group") or {}
    group_key = (group.get("key") or "").strip()
    if not group_key:
        return "body needs 'group.key' -- the owner's core group", 400
    conn = get_conn(tenant_id)
    if conn is None:
        return ("this tenant has no twin schema yet (oaap.data.model 0.1: "
                "install registers and binds it)"), 409
    try:
        with conn:
            bindings = _bindings(conn, name)
            owns = any(b["type_key"] == type_key and b["direction"] == "contributes"
                       and b["role"] == "owner" for b in bindings)
            if not owns:
                return (f"'{name}' does not contribute '{type_key}' as owner "
                        "-- see 'oaap data model bindings'"), 403
            if _type_active(conn, tenant_id, type_key) is None:
                return f"type '{type_key}' is not active for this tenant", 404
            obj_id = str(uuid.uuid4())
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO objects (id, type_key, title, owner_origin, "
                    "recorded_by) VALUES (%s, %s, %s, %s, %s)",
                    (obj_id, type_key, title, origin, name))
                source_key = (body.get("source_key") or "").strip()
                if source_key:
                    cur.execute(
                        "INSERT INTO source_keys (object_id, origin, "
                        "source_key, recorded_by) VALUES (%s, %s, %s, %s)",
                        (obj_id, origin, source_key, name))
                cur.execute(
                    "INSERT INTO groups (object_id, group_key, origin, "
                    "created_by) VALUES (%s, %s, %s, %s)",
                    (obj_id, group_key, origin, name))
            _write_group_content(conn, obj_id, group_key, origin, name, group)
            _record_event(conn, "object.created", obj_id, group_key, origin)
    finally:
        conn.close()
    return jsonify({"id": f"urn:oaap:obj:{obj_id}"}), 201


def _write_group_content(conn, obj_id, group_key, origin, recorded_by, payload):
    """Append the attribute/relation/activity rows a group PUT (or the
    creating POST) carries. Superseding only compares to the CURRENT
    row of the same slot -- a value repeated unchanged writes nothing,
    keeping the history honest (RFC-0031 §3.4)."""
    with conn.cursor() as cur:
        for key, value in (payload.get("attributes") or {}).items():
            if not KEY_RE.match(key):
                continue
            cur.execute(
                "SELECT id, value FROM current_attributes "
                "WHERE object_id = %s AND group_key = %s AND attr_key = %s",
                (obj_id, group_key, key))
            current = cur.fetchone()
            if current and json.dumps(current["value"], sort_keys=True) == json.dumps(value, sort_keys=True):
                continue
            cur.execute(
                "INSERT INTO attributes (object_id, group_key, attr_key, "
                "value, valid_from, valid_to, recorded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (obj_id, group_key, key, json.dumps(value),
                 payload.get("valid_from"), payload.get("valid_to"), recorded_by))
            new_id = cur.fetchone()["id"]
            if current:
                cur.execute("UPDATE attributes SET superseded_by = %s WHERE id = %s",
                           (new_id, current["id"]))
        for rel in (payload.get("relations") or []):
            rel_key = (rel.get("key") or "").strip()
            target = (rel.get("target") or "").strip()
            m = OBJ_ID_RE.match(target)
            if not rel_key or not m or not KEY_RE.match(rel_key):
                continue
            target_id = m.group(1)
            cur.execute(
                "SELECT id FROM current_relations WHERE object_id = %s AND "
                "group_key = %s AND rel_key = %s AND target_id = %s",
                (obj_id, group_key, rel_key, target_id))
            current = cur.fetchone()
            cur.execute(
                "INSERT INTO relations (object_id, group_key, rel_key, "
                "target_id, valid_from, valid_to, recorded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (obj_id, group_key, rel_key, target_id,
                 rel.get("valid_from"), rel.get("valid_to"), recorded_by))
            new_id = cur.fetchone()["id"]
            if current:
                cur.execute("UPDATE relations SET superseded_by = %s WHERE id = %s",
                           (new_id, current["id"]))
        for act in (payload.get("activities") or []):
            activity_key = (act.get("key") or "").strip()
            if not activity_key or not KEY_RE.match(activity_key):
                continue
            target = act.get("target") or ""
            m = OBJ_ID_RE.match(target) if target else None
            cur.execute(
                "INSERT INTO activities (object_id, group_key, activity_key, "
                "target_id, status, planned_start, planned_end, started_at, "
                "finished_at, recorded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (obj_id, group_key, activity_key, m.group(1) if m else None,
                 act.get("status", "open"), act.get("planned_start"),
                 act.get("planned_end"), act.get("started_at"),
                 act.get("finished_at"), recorded_by))


@app.get("/twin/objects/<obj_id>")
def read_object(obj_id):
    caller, err, code = require_caller()
    if err:
        return err, code
    name, tenant_id, _origin = caller
    m = OBJ_ID_RE.match(obj_id)
    if not m:
        return "malformed object id", 400
    obj_id = m.group(1)
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM objects WHERE id = %s", (obj_id,))
                obj = cur.fetchone()
            if obj is None:
                return "no such object", 404
            bindings = _bindings(conn, name)
            allowed = any(b["type_key"] == obj["type_key"] for b in bindings)
            if not allowed:
                return (f"'{name}' neither contributes nor consumes "
                        f"'{obj['type_key']}' -- see 'oaap data model "
                        "bindings' (RFC-0031 D7)"), 403
            with conn.cursor() as cur:
                cur.execute("SELECT group_key, origin FROM groups "
                           "WHERE object_id = %s", (obj_id,))
                group_rows = cur.fetchall()
                groups = {}
                for g in group_rows:
                    groups[g["group_key"]] = {"origin": g["origin"],
                                              "attributes": {}, "relations": [],
                                              "activities": []}
                cur.execute("SELECT group_key, attr_key, value, valid_from, "
                           "valid_to FROM current_attributes WHERE object_id = %s",
                           (obj_id,))
                for r in cur.fetchall():
                    if r["group_key"] in groups:
                        groups[r["group_key"]]["attributes"][r["attr_key"]] = {
                            "value": r["value"], "valid_from": r["valid_from"],
                            "valid_to": r["valid_to"]}
                cur.execute("SELECT group_key, rel_key, target_id, valid_from, "
                           "valid_to FROM current_relations WHERE object_id = %s",
                           (obj_id,))
                for r in cur.fetchall():
                    if r["group_key"] in groups:
                        groups[r["group_key"]]["relations"].append({
                            "key": r["rel_key"],
                            "target": f"urn:oaap:obj:{r['target_id']}",
                            "valid_from": r["valid_from"], "valid_to": r["valid_to"]})
                cur.execute("SELECT group_key, activity_key, target_id, status, "
                           "planned_start, planned_end, started_at, finished_at "
                           "FROM current_activities WHERE object_id = %s", (obj_id,))
                for r in cur.fetchall():
                    if r["group_key"] in groups:
                        groups[r["group_key"]]["activities"].append({
                            "key": r["activity_key"],
                            "target": (f"urn:oaap:obj:{r['target_id']}"
                                      if r["target_id"] else None),
                            "status": r["status"]})
    finally:
        conn.close()
    return jsonify({"id": f"urn:oaap:obj:{obj_id}", "type": obj["type_key"],
                    "title": obj["title"], "owner": obj["owner_origin"],
                    "groups": groups})


@app.put("/twin/objects/<obj_id>/groups/<group_key>")
def write_group(obj_id, group_key):
    caller, err, code = require_caller()
    if err:
        return err, code
    name, tenant_id, origin = caller
    m = OBJ_ID_RE.match(obj_id)
    if not m or not KEY_RE.match(group_key.replace(".", "")):
        return "malformed object id or group key", 400
    obj_id = m.group(1)
    body = request.get_json(silent=True) or {}
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT type_key FROM objects WHERE id = %s", (obj_id,))
                obj = cur.fetchone()
            if obj is None:
                return "no such object", 404
            bindings = _bindings(conn, name)
            writes = any(b["type_key"] == obj["type_key"] and b["direction"] == "contributes"
                        for b in bindings)
            if not writes:
                return (f"'{name}' does not contribute to '{obj['type_key']}' "
                        "-- see 'oaap data model bindings' (RFC-0031 D4)"), 403
            with conn.cursor() as cur:
                cur.execute("SELECT origin FROM groups WHERE object_id = %s "
                           "AND group_key = %s", (obj_id, group_key))
                existing = cur.fetchone()
            if existing is None:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO groups (object_id, group_key, origin, "
                        "created_by) VALUES (%s, %s, %s, %s)",
                        (obj_id, group_key, origin, name))
            elif existing["origin"] != origin:
                # "Nobody else writes there" (RFC-0031 §3.3) -- enforced
                # here, not by trusting the caller to behave.
                return (f"group '{group_key}' belongs to '{existing['origin']}' "
                        f"-- '{origin}' may not write it"), 403
            _write_group_content(conn, obj_id, group_key, origin, name, body)
            _record_event(conn, "group.written", obj_id, group_key, origin)
    finally:
        conn.close()
    return "", 204
