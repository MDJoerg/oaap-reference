"""oaap.data.twin 0.2 — the digital twin (RFC-0031 Schritt 3 + Schritt 5).

The only service an app talks to for shared tenant data. It never
learns a tenant or an origin from a request -- both come from the
caller's own credential (RFC-0031 §8), resolved the same way every
other protected route on this platform resolves a caller: the gateway
verifies the presented API key against `identity` and hands this
service the result as trusted headers (`X-OAAP-User`, `X-OAAP-Roles`),
exactly as it hands them to an app. This service additionally requires
`X-OAAP-User` to be a MACHINE principal named `instance:<name>` (RFC-
0027 3.1, minted at install time -- see appctl.py's `_twin_issue_
instance_key`); a human session reaching `/twin/*` through the gateway,
however it got there, is refused, because nothing on THAT route is
meant for a browser.

0.1 built RFC-0031 §9's own minimum (steps 1-3): create an object as
its owner, with its owner's core group; read an object and every group
its type is bound to; a contributor writes into its own group on a
foreign object. Recorded time always; validity carried but unfiltered.

**0.2 (RFC-0031 Bauplan Schritt 5, the twin browser) adds:**

- `?at=` validity filtering on every read (§9 step 7's first half),
  shared by the app-facing and the person-facing routes alike.
- Merge and unmerge (§9 step 6, §3.6) -- an `aliases` table (appctl.py
  provisions it), duplicate-candidate detection, and transparent
  resolution on every read/write: an id that was merged away keeps
  answering, forever (D3), and groups of BOTH objects are kept and
  shown together.
- A second authentication path, `/internal/*`, for the portal ONLY
  (never reachable through the gateway's `/twin/*` route, never gated
  by RFC-0027 at all) -- the exact posture `identity`'s own
  `/internal/*` guard already uses (RFC-0015 addendum A4): being
  reachable on the platform network is not proof of anything, only the
  shared `INTERNAL_API_KEY` is. The PORTAL is responsible for
  authenticating the actual person (its own login, its own gateway
  forward-auth); this service only re-checks their ROLE per action.
  This is RFC-0031 §6's own sentence, taken literally: "the twin
  browser uses the same API with a person's session instead of an
  instance credential" -- except the person's session never reaches
  this container directly, the portal relays it, the same way it
  already relays nothing of the kind to `identity` (it calls
  `/internal/*` there too, for the same reason).
- Tenant type creation (Bauplan Schritt 5: "Typen des Mandanten
  anlegen") -- scoped deliberately narrow, to RFC-0031 §9 step 5's own
  example and nothing wider: a tenant_admin may add a new GROUP type
  (plus the attribute types it needs) onto an object type that already
  exists and is already active. Never a new OBJECT type from the
  browser in 0.2 -- that would need the same additive/destructive
  version diffing `oaap.data.model`'s CLI path already has and this
  service does not reimplement (see the module comment at
  `twin_create_type` for why "create only" is enough for this step).
- A tree (`GET .../tree`), built by repeated reads in Python, not a
  recursive CTE -- correct at this scale, revisited only if it becomes
  slow (named honestly, not hidden).

Concretely NOT built yet, named here rather than silently missing:

- **Restricted groups** (D7's second half) -- every group of a type an
  instance is bound to is still readable; nothing marks one
  `restricted` to named readers.
- **`/twin/references`** (fuzzy search, D6) -- still nothing to search.
- **The rehearsal's own schema copy** (D8) -- unchanged from 0.1: a
  rehearsal gets no twin credential at all, deliberately.
- **The outbox reader** (RFC-0032) -- every write still appends one
  `events` row; nothing reads it yet.

Every write still appends one row to `events`.
"""
import datetime
import json
import os
import re
import secrets
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

# Mirrors appctl.py's _MODEL_KEY_RE / _MODEL_GROUP_KEY_RE / MODEL_VALUE_TYPES
# exactly (oaap.data.model 0.1 §2.2) -- this service has no import path to
# that module (a different container, a different codebase directory), so
# the pattern is duplicated here, not shared. A change to one without the
# other is a bug, not a variant -- the same drift warning this file already
# carries for TWIN_KEY_SCOPE-shaped constants below.
MODEL_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
MODEL_GROUP_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")
MODEL_VALUE_TYPES = ("text", "int", "decimal", "bool", "date", "datetime",
                    "enum", "ref")

# The portal's proof that a caller of '/internal/*' IS the portal (RFC-0015
# addendum A4's posture, applied to this service for the first time: the
# platform network no longer proves anything by itself). Held by 'identity'
# and 'portal' already; this container is the third and last holder.
INTERNAL_KEY = os.environ.get("INTERNAL_API_KEY", "")
INTERNAL_HEADER = "X-OAAP-Internal-Key"


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
                       "not a person (RFC-0031 §8) -- a browser reaches this "
                       "service through the portal's own '/twin' pages, "
                       "never here"), 403
    name = user[len("instance:"):]
    resolved = resolve_instance(name)
    if resolved is None:
        return None, f"'{name}' is not a registered instance", 403
    return (name, resolved["tenant"], resolved["origin"]), None, None


@app.before_request
def _guard_internal_api():
    """Every '/internal/*' route needs the shared platform key -- exactly
    identity's own guard (RFC-0015 addendum A4), copied rather than
    imported for the same reason the key regexes above are copied: no
    shared module between these containers. '/twin/*' below is
    UNCHANGED by this -- it is reached through the gateway and gated by
    RFC-0027 as before; this guard only ever sees '/internal/*'."""
    if not request.path.startswith("/internal/"):
        return None
    if not INTERNAL_KEY:
        return jsonify(error="internal API key is not configured on this "
                             "node -- run 'sudo oaap update'"), 503
    if not secrets.compare_digest(
            request.headers.get(INTERNAL_HEADER, ""), INTERNAL_KEY):
        return jsonify(error="internal API requires the platform key"), 401
    return None


def require_person():
    """(username, tenant_id, roles) for a call the PORTAL makes on
    behalf of an already-verified person (RFC-0031 §6). Reachable only
    under '/internal/*', already gated above to callers holding
    INTERNAL_API_KEY. This function only assembles who is asking --
    whether that person's ROLE permits the specific action is each
    route's own job below, the same split identity's own comment
    describes for its '/internal/*' guard."""
    username = request.headers.get("X-OAAP-Person-User", "").strip()
    tenant_id = request.headers.get("X-OAAP-Person-Tenant", "").strip()
    roles = set(filter(None, request.headers.get(
        "X-OAAP-Person-Roles", "").split(",")))
    if not username or not tenant_id:
        return None, "missing person context (X-OAAP-Person-User/-Tenant)", 400
    return (username, tenant_id, roles), None, None


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


# ---------------------------------------------------------------- validity
def _parse_at(raw):
    """A date the caller asked to see the twin as of, or None for 'now'
    -- the only shape §3.4's validity axis needs (a day, not a moment:
    the date slider Schritt 5 asks for offers exactly that). An
    unparsable '?at=' is treated as none, not as an error -- the same
    "tolerant of a bad filter" posture the rest of this platform takes
    for a store-list field it does not recognise."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def _row_open(row, at):
    """Whether one attribute/relation row was valid at 'at' (None = no
    filter, 0.1's unfiltered behaviour, unchanged for every caller that
    never passes '?at=')."""
    if at is None:
        return True
    vf, vt = row.get("valid_from"), row.get("valid_to")
    if vf and vf > at:
        return False
    if vt and vt <= at:
        return False
    return True


# -------------------------------------------------------------- merge (D3)
def _canonical_of(conn, obj_id):
    """The id everyone should use instead, if 'obj_id' was merged away
    -- or 'obj_id' itself, unchanged, for the overwhelming majority of
    objects that were never part of a merge. D3: "a merge keeps both
    platform IDs resolvable forever; one becomes canonical.\""""
    with conn.cursor() as cur:
        cur.execute("SELECT canonical_id FROM aliases WHERE alias_id = %s "
                   "AND unmerged_at IS NULL", (obj_id,))
        row = cur.fetchone()
    return str(row["canonical_id"]) if row else obj_id


def _aliases_of(conn, canonical_id):
    """Every id currently merged INTO canonical_id -- may be several,
    never negative: a third duplicate can always merge into an
    already-merged pair."""
    with conn.cursor() as cur:
        cur.execute("SELECT alias_id FROM aliases WHERE canonical_id = %s "
                   "AND unmerged_at IS NULL", (canonical_id,))
        return [str(r["alias_id"]) for r in cur.fetchall()]


def _load_object(conn, requested_id, at=None):
    """The object's header plus every group recorded under it OR any id
    merged into it (§3.6: "groups of both objects are kept ... nothing
    is overwritten. An app that stored the alias never breaks.") --
    unfiltered by who may READ which group; each caller filters that on
    top (an instance by its own bindings, a person by nothing -- D7's
    simple half plus "the tenant sees its own twin" (§3.3)). None if no
    such object exists at all, even after resolving a merge.

    A group_key collision between the merged-together objects' OWN
    groups is possible in theory (two DIFFERENT origins, each valid on
    their own object, happen to have chosen the identical group_key) --
    "nobody else writes there" (§3.3) only ever protected ONE live
    object, never a pair being merged. Handled, not ignored: the
    SECOND row sharing a key is suffixed with a short form of its own
    object id, so nothing is silently dropped. This never fires for the
    99% of objects that were never merged -- the plain key is untouched.
    """
    canonical_id = _canonical_of(conn, requested_id)
    member_ids = [canonical_id] + _aliases_of(conn, canonical_id)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM objects WHERE id = %s", (canonical_id,))
        obj = cur.fetchone()
    if obj is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT object_id, group_key, origin FROM groups "
                   "WHERE object_id = ANY(%s::uuid[])", (member_ids,))
        group_rows = cur.fetchall()
    by_key = {}
    for g in group_rows:
        by_key.setdefault(g["group_key"], []).append(g)
    groups = {}
    slot_of = {}  # (object_id, group_key) -> the display key used below
    for key, rows in by_key.items():
        for g in rows:
            disp = key if len(rows) == 1 else f"{key}+{str(g['object_id'])[:8]}"
            groups[disp] = {"origin": g["origin"], "object_id": str(g["object_id"]),
                           "attributes": {}, "relations": [], "activities": []}
            slot_of[(str(g["object_id"]), key)] = disp
    with conn.cursor() as cur:
        cur.execute("SELECT object_id, group_key, attr_key, value, valid_from, "
                   "valid_to FROM current_attributes "
                   "WHERE object_id = ANY(%s::uuid[])", (member_ids,))
        for r in cur.fetchall():
            if not _row_open(r, at):
                continue
            disp = slot_of.get((str(r["object_id"]), r["group_key"]))
            if disp:
                groups[disp]["attributes"][r["attr_key"]] = {
                    "value": r["value"], "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"]}
    with conn.cursor() as cur:
        cur.execute("SELECT object_id, group_key, rel_key, target_id, valid_from, "
                   "valid_to FROM current_relations "
                   "WHERE object_id = ANY(%s::uuid[])", (member_ids,))
        for r in cur.fetchall():
            if not _row_open(r, at):
                continue
            disp = slot_of.get((str(r["object_id"]), r["group_key"]))
            if disp:
                # A relation's target may itself have been merged away
                # since it was written -- resolve forward, so the tree
                # and the object page always point at the live object.
                groups[disp]["relations"].append({
                    "key": r["rel_key"],
                    "target": f"urn:oaap:obj:{_canonical_of(conn, str(r['target_id']))}",
                    "valid_from": r["valid_from"], "valid_to": r["valid_to"]})
    with conn.cursor() as cur:
        cur.execute("SELECT object_id, group_key, activity_key, target_id, status, "
                   "planned_start, planned_end, started_at, finished_at "
                   "FROM current_activities "
                   "WHERE object_id = ANY(%s::uuid[])", (member_ids,))
        for r in cur.fetchall():
            disp = slot_of.get((str(r["object_id"]), r["group_key"]))
            if disp:
                groups[disp]["activities"].append({
                    "key": r["activity_key"],
                    "target": (f"urn:oaap:obj:{r['target_id']}"
                              if r["target_id"] else None),
                    "status": r["status"], "planned_start": r["planned_start"],
                    "planned_end": r["planned_end"], "started_at": r["started_at"],
                    "finished_at": r["finished_at"]})
    return {"canonical_id": canonical_id, "member_ids": member_ids,
           "type_key": obj["type_key"], "title": obj["title"],
           "owner": obj["owner_origin"], "groups": groups}


@app.get("/healthz")
def healthz():
    return "ok", 200


# =========================================================== app-facing API
# Everything below this line, up to the '/internal/*' section, is
# UNCHANGED in shape from 0.1 (RFC-0031 §9 steps 1-3): an instance
# authenticates with require_caller(), never a person. The additions in
# 0.2 are additive only -- '?at=' and alias resolution -- nothing here
# narrows what an app could already do.

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
            target_id = _canonical_of(conn, m.group(1))
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
                (obj_id, group_key, activity_key,
                 _canonical_of(conn, m.group(1)) if m else None,
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
    at = _parse_at(request.args.get("at"))
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            loaded = _load_object(conn, obj_id, at)
            if loaded is None:
                return "no such object", 404
            bindings = _bindings(conn, name)
            allowed = any(b["type_key"] == loaded["type_key"] for b in bindings)
            if not allowed:
                return (f"'{name}' neither contributes nor consumes "
                        f"'{loaded['type_key']}' -- see 'oaap data model "
                        "bindings' (RFC-0031 D7)"), 403
    finally:
        conn.close()
    result = {"id": f"urn:oaap:obj:{loaded['canonical_id']}", "type": loaded["type_key"],
              "title": loaded["title"], "owner": loaded["owner"],
              "groups": loaded["groups"]}
    if loaded["canonical_id"] != obj_id:
        # D3: the id the caller stored still answers, forever -- it just
        # no longer names a separate object.
        result["merged_from"] = f"urn:oaap:obj:{obj_id}"
    return jsonify(result)


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
            obj_id = _canonical_of(conn, obj_id)
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


# ========================================================= person-facing API
# Reached ONLY from the portal, over the platform's internal network,
# gated by _guard_internal_api() above -- never through the gateway,
# never with an RFC-0027 key. Every route below authenticates with
# require_person(), never require_caller(). Roles are as Bauplan
# Schritt 5 names them: 'user' sees; 'admin'/'keyuser' additionally
# maintain the tenant's own groups; 'tenant_admin' additionally merges,
# unmerges and creates types.

@app.get("/internal/twin/types")
def twin_types_for_person():
    """Every type active for the caller's tenant -- unlike '/twin/types',
    not filtered by any binding, because a person has none: RFC-0031
    §3.3 treats the tenant as an origin like any other, and this is its
    own read of its own registry. The full definition rides along so
    the portal can render "which attributes does this group carry"
    without a second round trip."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, _roles = person
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"types": []})
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.key, d.kind, d.origin, d.definition FROM "
                    "oaap_model.type_definitions d JOIN oaap_model.activations a "
                    "ON a.type_key = d.key WHERE a.tenant_id = %s "
                    "ORDER BY d.kind, d.key", (tenant_id,))
                rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        definition = r["definition"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        out.append({"key": r["key"], "kind": r["kind"], "origin": r["origin"],
                   "title": definition.get("title") or r["key"],
                   "definition": definition})
    return jsonify({"types": out})


@app.get("/internal/twin/objects")
def twin_objects_by_type():
    """The tree's root list: every object of one type, tenant-wide, no
    binding filter (Bauplan Schritt 5: "Baum je Objekttyp"). A merged-
    away alias is excluded -- reachable from the duplicates page and
    from the canonical object it now answers as, never as its own row
    here."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, _roles = person
    type_key = (request.args.get("type") or "").strip()
    if not type_key:
        return "query needs '?type='", 400
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"objects": []})
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, owner_origin FROM objects WHERE "
                    "type_key = %s AND id NOT IN (SELECT alias_id FROM "
                    "aliases WHERE unmerged_at IS NULL) ORDER BY title",
                    (type_key,))
                rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"objects": [{"id": f"urn:oaap:obj:{r['id']}",
                                "title": r["title"], "owner": r["owner_origin"]}
                               for r in rows]})


@app.get("/internal/twin/objects/<obj_id>")
def twin_object_detail(obj_id):
    """The object page's own data: header, every group regardless of
    origin, the date slider applied if '?at=' is given, and which
    other ids answer as this one (a merge's aftermath, shown, not
    hidden)."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, _roles = person
    m = OBJ_ID_RE.match(obj_id)
    if not m:
        return "malformed object id", 400
    obj_id = m.group(1)
    at = _parse_at(request.args.get("at"))
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            loaded = _load_object(conn, obj_id, at)
            if loaded is None:
                return "no such object", 404
            aliases = _aliases_of(conn, loaded["canonical_id"])
    finally:
        conn.close()
    result = {"id": f"urn:oaap:obj:{loaded['canonical_id']}", "type": loaded["type_key"],
              "title": loaded["title"], "owner": loaded["owner"],
              "groups": loaded["groups"],
              "merged_aliases": [f"urn:oaap:obj:{a}" for a in aliases]}
    if loaded["canonical_id"] != obj_id:
        result["requested_as_alias_of"] = f"urn:oaap:obj:{obj_id}"
    return jsonify(result)


@app.get("/internal/twin/objects/<obj_id>/tree")
def twin_tree(obj_id):
    """The navigation tree from one object, following relations
    outward, depth-limited, '?at=' applied at every hop (Bauplan
    Schritt 5: "Baum je Objekttyp", RFC-0031 §6's own
    '/twin/objects/{id}/tree?at=&depth='). Built by repeated calls to
    _load_object rather than a recursive CTE -- correct, not yet fast;
    fine at the size this platform runs at today."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, _roles = person
    m = OBJ_ID_RE.match(obj_id)
    if not m:
        return "malformed object id", 400
    obj_id = m.group(1)
    at = _parse_at(request.args.get("at"))
    try:
        depth = max(0, min(5, int(request.args.get("depth", "2"))))
    except ValueError:
        depth = 2
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            root = _load_object(conn, obj_id, at)
            if root is None:
                return "no such object", 404
            root_node = {"id": f"urn:oaap:obj:{root['canonical_id']}",
                        "type": root["type_key"], "title": root["title"],
                        "children": []}
            nodes_by_id = {root["canonical_id"]: root_node}
            frontier = [root["canonical_id"]]
            visited = {root["canonical_id"]}
            for _ in range(depth):
                if not frontier:
                    break
                next_frontier = []
                for cid in frontier:
                    loaded = _load_object(conn, cid, at)
                    if loaded is None:
                        continue
                    for g in loaded["groups"].values():
                        for rel in g["relations"]:
                            tm = OBJ_ID_RE.match(rel["target"])
                            if not tm:
                                continue
                            target_id = tm.group(1)
                            if target_id in visited:
                                continue
                            trow = _load_object(conn, target_id, at)
                            if trow is None:
                                continue
                            node = {"id": f"urn:oaap:obj:{trow['canonical_id']}",
                                   "type": trow["type_key"], "title": trow["title"],
                                   "via": rel["key"], "children": []}
                            nodes_by_id[cid]["children"].append(node)
                            visited.add(target_id)
                            nodes_by_id[trow["canonical_id"]] = node
                            next_frontier.append(trow["canonical_id"])
                frontier = next_frontier
    finally:
        conn.close()
    return jsonify(root_node)


@app.get("/internal/twin/candidates")
def twin_candidates():
    """Duplicate candidates for this tenant (§3.6): the SAME normalised
    title, created by DIFFERENT origins, neither already merged. A
    platform-computed hint, nothing more -- §3.6: "A detection produces
    a candidate pair, visible in the twin browser ... nothing else."

    Deliberately NOT scoped to one type: the reference scenario this
    step exists to resolve (Bauplan Schritt 4, Mitarbeiterverwaltung
    2026-09-10) is exactly "Anna Müller" as a `Kontaktperson` (owner
    `app:partnerverwaltung`) and, independently, as a `Mitarbeiter`
    (owner `app:mitarbeiterverwaltung`) -- two DIFFERENT types, the same
    real person. Restricting detection to one type (an earlier reading
    of the RFC's own illustrative wording) would have made this service
    unable to surface the very duplicate it was built to resolve --
    found live on oaap-test while verifying this step, before the
    candidate list had ever been shown to a person."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, roles = person
    if "tenant_admin" not in roles:
        return "viewing merge candidates needs role tenant_admin (RFC-0031 §3.6)", 403
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"candidates": []})
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, type_key, title, owner_origin FROM objects "
                    "WHERE id NOT IN (SELECT alias_id FROM aliases WHERE "
                    "unmerged_at IS NULL) ORDER BY title")
                rows = cur.fetchall()
    finally:
        conn.close()
    buckets = {}
    for r in rows:
        norm = re.sub(r"\s+", " ", (r["title"] or "").strip().lower())
        buckets.setdefault(norm, []).append(r)
    candidates = []
    for _norm, members in buckets.items():
        if len(members) < 2 or len({m["owner_origin"] for m in members}) < 2:
            # the SAME origin naming a thing twice is that app's own
            # bug, not a twin duplicate -- not shown here
            continue
        candidates.append({
            "title": members[0]["title"],
            "objects": [{"id": f"urn:oaap:obj:{m['id']}", "title": m["title"],
                        "type": m["type_key"], "owner": m["owner_origin"]}
                       for m in members]})
    return jsonify({"candidates": candidates})


@app.get("/internal/twin/merges")
def twin_merges():
    """Every currently-active merge for this tenant, with both objects'
    titles -- the duplicates page's own "already merged, undo?" list.
    Not paged: a tenant does not merge often enough for this to grow
    past what one page can show."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, roles = person
    if "tenant_admin" not in roles:
        return "viewing merges needs role tenant_admin (RFC-0031 §3.6)", 403
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"merges": []})
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT a.alias_id, a.canonical_id, a.merged_at, a.merged_by, "
                    "o1.title AS alias_title, o2.title AS canonical_title "
                    "FROM aliases a "
                    "JOIN objects o1 ON o1.id = a.alias_id "
                    "JOIN objects o2 ON o2.id = a.canonical_id "
                    "WHERE a.unmerged_at IS NULL ORDER BY a.merged_at DESC")
                rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"merges": [
        {"alias": f"urn:oaap:obj:{r['alias_id']}",
        "canonical": f"urn:oaap:obj:{r['canonical_id']}",
        "alias_title": r["alias_title"], "canonical_title": r["canonical_title"],
        "merged_at": r["merged_at"].isoformat() if r["merged_at"] else None,
        "merged_by": r["merged_by"]} for r in rows]})


@app.post("/internal/twin/merge")
def twin_merge():
    """A human act (§3.6), audited: 'keep' stays the canonical id,
    'drop' becomes an alias that answers forever (D3). Deliberately
    allowed ACROSS different object types -- §3.6 never restricts merge
    to one type, only the automatic DETECTION heuristic's illustrative
    wording did, and the reference duplicate this step exists to
    resolve (Anna as `Kontaktperson` AND, independently, as
    `Mitarbeiter`) is exactly a cross-type case. The canonical object's
    OWN type is what every future read reports; the dropped object's
    groups are kept and shown alongside it regardless (§2.9's loader
    already unions by object id, never by type)."""
    person, err, code = require_person()
    if err:
        return err, code
    username, tenant_id, roles = person
    if "tenant_admin" not in roles:
        return "merge is a tenant_admin action (RFC-0031 §3.6)", 403
    body = request.get_json(silent=True) or {}
    keep = OBJ_ID_RE.match((body.get("keep") or "").strip())
    drop = OBJ_ID_RE.match((body.get("drop") or "").strip())
    if not keep or not drop:
        return "body needs 'keep' and 'drop' (object ids)", 400
    keep_id, drop_id = keep.group(1), drop.group(1)
    if keep_id == drop_id:
        return "'keep' and 'drop' name the same object", 400
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, type_key FROM objects WHERE id IN (%s, %s)",
                           (keep_id, drop_id))
                found = {str(r["id"]): r["type_key"] for r in cur.fetchall()}
            if keep_id not in found or drop_id not in found:
                return "no such object", 404
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM aliases WHERE alias_id = %s AND "
                           "unmerged_at IS NULL", (drop_id,))
                if cur.fetchone():
                    return f"'{drop_id}' is already merged into another object", 409
                cur.execute(
                    "INSERT INTO aliases (alias_id, canonical_id, merged_at, "
                    "merged_by) VALUES (%s, %s, now(), %s)",
                    (drop_id, keep_id, username))
            _record_event(conn, "object.merged", keep_id, "", f"tenant:{username}")
    finally:
        conn.close()
    return "", 204


@app.post("/internal/twin/unmerge")
def twin_unmerge():
    """Undoes one merge -- possible always, because groups are re-
    parented, never merged (§3.6): 'drop' goes back to answering as
    its own object; 'keep' loses nothing it did not already have on
    its own."""
    person, err, code = require_person()
    if err:
        return err, code
    username, tenant_id, roles = person
    if "tenant_admin" not in roles:
        return "unmerge is a tenant_admin action (RFC-0031 §3.6)", 403
    body = request.get_json(silent=True) or {}
    drop = OBJ_ID_RE.match((body.get("drop") or "").strip())
    if not drop:
        return "body needs 'drop' (the merged-away object id)", 400
    drop_id = drop.group(1)
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE aliases SET unmerged_at = now(), "
                           "unmerged_by = %s WHERE alias_id = %s AND "
                           "unmerged_at IS NULL RETURNING canonical_id",
                           (username, drop_id))
                row = cur.fetchone()
            if row is None:
                return f"'{drop_id}' is not currently merged", 404
            _record_event(conn, "object.unmerged", str(row["canonical_id"]), "",
                          f"tenant:{username}")
    finally:
        conn.close()
    return "", 204


@app.put("/internal/twin/objects/<obj_id>/groups/<group_key>")
def twin_write_group_person(obj_id, group_key):
    """A person writes into a TENANT-origin group -- Bauplan Schritt 5:
    "admin/keyuser sehen und pflegen Mandantengruppen." Origin is
    always the literal string 'tenant' (never one per person): every
    tenant group lives in that tenant's OWN schema already, so there is
    no cross-tenant namespace question here the way there is for the
    shared type registry (see twin_create_type). "Nobody else writes
    there" (§3.3) applies exactly as it does to an app: a group already
    owned by an app cannot be taken over by the tenant either."""
    person, err, code = require_person()
    if err:
        return err, code
    username, tenant_id, roles = person
    if not (roles & {"admin", "keyuser", "tenant_admin"}):
        return ("writing into a tenant group needs role admin, keyuser or "
                "tenant_admin (RFC-0031 Bauplan Schritt 5)"), 403
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
            obj_id = _canonical_of(conn, obj_id)
            with conn.cursor() as cur:
                cur.execute("SELECT type_key FROM objects WHERE id = %s", (obj_id,))
                obj = cur.fetchone()
            if obj is None:
                return "no such object", 404
            # A person has no bindings (only an instance does) -- this
            # replaces that check: the group type must exist, be active
            # for this tenant, and attach to THIS object's own type.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.definition FROM oaap_model.type_definitions d "
                    "JOIN oaap_model.activations a ON a.type_key = d.key "
                    "WHERE a.tenant_id = %s AND d.key = %s AND d.kind = 'group_types'",
                    (tenant_id, group_key))
                gt = cur.fetchone()
            if gt is None:
                return f"group type '{group_key}' is not active for this tenant", 404
            gdef = gt["definition"]
            if isinstance(gdef, str):
                gdef = json.loads(gdef)
            if gdef.get("on") != obj["type_key"]:
                return (f"group type '{group_key}' attaches to "
                        f"'{gdef.get('on')}', not '{obj['type_key']}'"), 400
            with conn.cursor() as cur:
                cur.execute("SELECT origin FROM groups WHERE object_id = %s "
                           "AND group_key = %s", (obj_id, group_key))
                existing = cur.fetchone()
            if existing is None:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO groups (object_id, group_key, origin, "
                        "created_by) VALUES (%s, %s, %s, %s)",
                        (obj_id, group_key, "tenant", username))
            elif existing["origin"] != "tenant":
                return (f"group '{group_key}' belongs to '{existing['origin']}' "
                        "-- the tenant may not overwrite another origin's "
                        "group (RFC-0031 §3.3)"), 403
            _write_group_content(conn, obj_id, group_key, "tenant", username, body)
            _record_event(conn, "group.written", obj_id, group_key, f"tenant:{username}")
    finally:
        conn.close()
    return "", 204


@app.post("/internal/twin/types")
def twin_create_type():
    """Bauplan Schritt 5: "Typen des Mandanten anlegen" -- deliberately
    narrow, to RFC-0031 §9 step 5's own example and nothing wider: a
    NEW group type (plus the attribute types it needs), attached to an
    OBJECT type that already exists and is already active. Never a new
    object type here, and never a CHANGE to a type that already exists
    -- both would need the same additive/destructive version diffing
    `oaap.data.model`'s CLI path already has
    (appctl.py's model_register_data_model / type_change_kind), and
    this service does not reimplement that here: a brand-new key is
    accepted, an existing one is refused outright, with a plain reason.
    That is enough for "a person adds one group to an object type," the
    one case the conformance scenario actually names, and honest about
    what is NOT built rather than a partial version of it.

    Origin is 'tenant:<tenant-id>', not the bare word 'tenant' the
    CLI's own 'oaap data model register' used to write (fixed
    alongside this, appctl.py) -- oaap_model.type_definitions is a
    single, NODE-WIDE table (D2), so two tenants on the same node each
    calling their group 'notiz' must not silently become one shared
    type. The object TYPE they attach to is unaffected: it is looked
    up by its existing, unqualified key, exactly as an app's own
    contributes/consumes already does.
    """
    person, err, code = require_person()
    if err:
        return err, code
    username, tenant_id, roles = person
    if "tenant_admin" not in roles:
        return "creating a type needs role tenant_admin", 403
    body = request.get_json(silent=True) or {}
    on_type = (body.get("on") or "").strip()
    group_key = (body.get("group_key") or "").strip()
    group_title = (body.get("group_title") or group_key).strip()
    attrs = body.get("attributes") or []   # [{"key","title","value_type"}]
    if not on_type or not group_key or not attrs:
        return "body needs 'on', 'group_key' and at least one 'attributes' entry", 400
    if not MODEL_GROUP_KEY_RE.match(group_key):
        return "'group_key' must look like 'namespace.name' (lowercase)", 400
    for a in attrs:
        if not MODEL_KEY_RE.match((a.get("key") or "")):
            return f"attribute key '{a.get('key')}' is not a valid type key", 400
        if a.get("value_type", "text") not in MODEL_VALUE_TYPES:
            return (f"attribute '{a['key']}': value_type must be one of "
                    f"{' | '.join(MODEL_VALUE_TYPES)}"), 400
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM oaap_model.type_definitions d JOIN "
                    "oaap_model.activations a ON a.type_key = d.key WHERE "
                    "a.tenant_id = %s AND d.key = %s AND d.kind = 'object_types'",
                    (tenant_id, on_type))
                if cur.fetchone() is None:
                    return (f"object type '{on_type}' is not active for this "
                            "tenant -- a group attaches to a type that already "
                            "exists (RFC-0031 §3.3)"), 404
                new_keys = [group_key] + [a["key"] for a in attrs]
                cur.execute("SELECT key FROM oaap_model.type_definitions "
                           "WHERE key = ANY(%s)", (new_keys,))
                clashes = [r["key"] for r in cur.fetchall()]
                if clashes:
                    return (f"type key(s) already registered: "
                            f"{', '.join(clashes)} -- pick different names"), 409
            origin = f"tenant:{tenant_id}"
            with conn.cursor() as cur:
                for a in attrs:
                    cur.execute(
                        "INSERT INTO oaap_model.type_definitions (key, kind, "
                        "origin, package, version, definition) VALUES "
                        "(%s, 'attribute_types', %s, %s, '1.0.0', %s)",
                        (a["key"], origin, tenant_id, json.dumps(
                            {"key": a["key"], "title": a.get("title") or a["key"],
                             "value_type": a.get("value_type", "text")})))
                    cur.execute(
                        "INSERT INTO oaap_model.activations (tenant_id, type_key) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING", (tenant_id, a["key"]))
                cur.execute(
                    "INSERT INTO oaap_model.type_definitions (key, kind, origin, "
                    "package, version, definition) VALUES "
                    "(%s, 'group_types', %s, %s, '1.0.0', %s)",
                    (group_key, origin, tenant_id, json.dumps(
                        {"key": group_key, "title": group_title, "on": on_type,
                         "attributes": [a["key"] for a in attrs],
                         "relations": [], "activities": []})))
                cur.execute(
                    "INSERT INTO oaap_model.activations (tenant_id, type_key) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING", (tenant_id, group_key))
            _record_event(conn, "type.created", None, group_key, f"tenant:{username}")
    finally:
        conn.close()
    return jsonify({"key": group_key}), 201
