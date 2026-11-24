"""oaap.data.twin 0.3 — the digital twin (RFC-0031 Schritt 3 + Schritt 5,
RFC-0032 build order step 2).

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
**0.3 (RFC-0032 build order step 2) adds the outbox's other end:**

- `relay.py`, the SAME image run by the compose service `relay` (node
  profile `broker`): publishes every `events` row to the broker and
  writes a group's snapshot into `states` -- see that file.
- `GET /internal/twin/outbox` (end of this file): per tenant, how many
  events wait and when the relay last reported -- what the portal's
  health page reads.

Every write still appends one row to `events`; this service itself
never publishes anything.

**oaap.data.timeseries 0.1 (RFC-0086) is served here too**, under
'/twin/series' and '/twin/points' -- the same caller, the same bindings:
writing a series needs `contributes` on its object's type, reading needs
any binding, a remote reader only reads. The points live in the tenant's
own schema `ts_<tenant-id>` with its own role (credential in the same
secrets file, written by `oaap data timeseries enable`); the driver is
timeseries.py, the minutely job ts_admin.py. See the section at the end.
"""
import datetime
import functools
import json
import os
import re
import secrets
import time
import uuid

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request

import assets_model
import assets_twin
import fleet_affected
import mcp_door
import semantic
import timeseries as ts
import twin_inbound as tsi
import bus_model as bm
import twin_read as tr
import twin_status as tst
import twin_sync as tsy
import twin_values as tv
import twin_write as tw

app = Flask(__name__)

PLATFORM_APPS_DIR = "/platform-apps"
REGISTRY_FILE = os.path.join(PLATFORM_APPS_DIR, "registry.json")
SECRETS_FILE = os.path.join(PLATFORM_APPS_DIR, "twin-secrets.json")
# oaap.data.twin 0.4 §2.14: readers on ANOTHER node, reached through a
# tunnel (RFC-0033 §6, D10). Written by appctl only, like the registry;
# no secret in it.
READERS_FILE = os.path.join(PLATFORM_APPS_DIR, "twin-readers.json")
REMOTE_PREFIX = "remote:"
# RFC-0120 stage 0: integration clients -- programs outside the platform, one
# tenant each, reading exactly the types the operator (later the tenant
# admin) bound them to. Written by appctl only; no secret in it.
CLIENTS_FILE = os.path.join(PLATFORM_APPS_DIR, "twin-clients.json")
CLIENT_PREFIX = "client:"
# the tenants' labels (the namespace of a tenant's object types, RFC-0120 D9)
# and whether a tenant's outbound traffic is closed (RFC-0068: no webhooks)
TENANTS_FILE = os.path.join(PLATFORM_APPS_DIR, "tenants.json")
# hosts a webhook may reach although they are not on the internet (a test
# receiver in the LAN) -- the operator's setting, the same one hooks.py reads
HOOK_ALLOW = tuple(h.strip().lower() for h in os.environ.get("OAAP_TWIN_HOOK_ALLOW", "")
                   .split(",") if h.strip())
API_VERSION = "v1"
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


def resolve_remote(label):
    """{'tenant', 'origin', 'reads'} for a remote reader (§2.14), or
    None. The same shape of trust as resolve_instance: the NAME comes
    from identity's verification, everything else from a file only the
    operator writes."""
    r = (_load_json(READERS_FILE, {}).get("readers") or {}).get(label)
    if not r or not r.get("tenant"):
        return None
    return {"tenant": r["tenant"], "origin": REMOTE_PREFIX + label,
            "reads": [t for t in r.get("reads") or [] if isinstance(t, str)]}


def is_remote(name):
    return name.startswith(REMOTE_PREFIX)


def resolve_client(label):
    """{'tenant', 'origin', 'reads', 'writes', 'models'} of an integration
    client (RFC-0120), or None -- the same shape of trust as resolve_remote:
    the NAME from identity's verification, the rest from a file only the
    host writes (the operator's CLI, the tenant admin's job). Stage 3:
    `writes` (types it creates objects of and writes its own groups on) and
    `models` (it may create types of its tenant, D8)."""
    c = (_load_json(CLIENTS_FILE, {}).get("clients") or {}).get(label)
    if not c or not c.get("tenant"):
        return None
    return {"tenant": c["tenant"], "origin": CLIENT_PREFIX + label,
            "reads": [t for t in c.get("reads") or [] if isinstance(t, str)],
            "writes": [t for t in c.get("writes") or [] if isinstance(t, str)],
            "models": c.get("models") is True}


def tenant_record(tenant_id):
    """The tenant's record in tenants.json ({"tenants": {id: record}}, the
    host's file) -- its label, whether its outbound traffic is closed."""
    return tsy.tenant_of(_load_json(TENANTS_FILE, {}), tenant_id)


def is_client(name):
    return name.startswith(CLIENT_PREFIX)


def reads_only(name):
    """Not for the app routes: a remote reader reads only (§2.14); an
    integration client writes through its own door, /twin/v1 (RFC-0120
    stage 3), never through an app's routes."""
    return is_remote(name) or is_client(name)


def require_caller():
    """(instance_name, tenant, origin) or None -- writes the 401/403
    onto Flask's request-local error path via abort-by-return.

    X-OAAP-User is set by identity's own /verify, forwarded by the
    gateway's forward_auth (Caddyfile '/twin/*') -- never something a
    client can set itself (the gateway strips/overwrites it on every
    route, per the deployment contract every other protected route
    already relies on)."""
    user = request.headers.get("X-OAAP-User", "")
    if is_client(user):
        resolved = resolve_client(user[len(CLIENT_PREFIX):])
        if resolved is None:
            return None, f"'{user}' is not an integration client of this node", 403
        return (user, resolved["tenant"], resolved["origin"]), None, None
    if is_remote(user):
        # oaap.data.twin 0.4 §2.14 -- the caller's NAME is the full
        # principal ('remote:<label>'), so no binding row of a local
        # instance can ever be mistaken for it
        resolved = resolve_remote(user[len(REMOTE_PREFIX):])
        if resolved is None:
            return None, f"'{user}' is not a remote reader of this node", 403
        return (user, resolved["tenant"], resolved["origin"]), None, None
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
    """The caller's bindings. A remote reader has none in the model
    registry -- it has no manifest -- so it gets one `consumes` row per
    type the operator gave it, and nothing that could write (§2.14)."""
    if is_remote(instance):
        r = resolve_remote(instance[len(REMOTE_PREFIX):]) or {}
        return [{"type_key": t, "direction": "consumes", "role": None,
                 "group_key": None, "declared_as": "remote reader"}
                for t in r.get("reads") or []]
    if is_client(instance):
        c = resolve_client(instance[len(CLIENT_PREFIX):])
        if c is None:
            return []
        return tsy.client_bindings(conn, c["tenant"], instance, c)
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


_record_event = tw.record_event
_NOT_HERE = ("a remote reader reads only (oaap.data.twin 0.4 §2.14); an integration client "
             "writes through its own door /twin/v1 (RFC-0120 stage 3) -- these routes are "
             "an app's")


@app.errorhandler(tw.Refused)
def _refused(e):
    """A write the rules refuse, raised anywhere inside a route's `with conn:`
    (which rolls the transaction back): its sentence and its code."""
    return str(e), e.code


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
# The rules live in twin_write.py (one rule for the API and the fleet).
_canonical_of = tw.canonical_of
# the objects not retired (RFC-0120 stage 3) -- for the lists of now
NOT_RETIRED = "id NOT IN (SELECT object_id FROM retirements)"


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
    # RFC-0120 stage 3: a retired object is gone from every current read; a
    # read for a day before it was retired still sees it
    when = tsy.retired_at(conn, canonical_id)
    if when is not None and (at is None or when.date() <= at):
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
    # periodic attributes (oaap.data.twin 0.12 §2.20): the period of the day
    # asked for (its end, UTC), or of now -- with its bounds, unit, source
    moment = (datetime.datetime.combine(at, datetime.time(23, 59, 59, 999999),
                                        datetime.timezone.utc) if at else tr.now())
    for (m, k, a), r in tr.periods_at(conn, member_ids, moment, False).items():
        disp = slot_of.get((m, k))
        if disp:
            groups[disp]["attributes"][a] = {"value": r["value"], "periodic": True,
                                             **tr.period_meta(r)}
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


# ------------------------------------------------ RFC-0120: the door /twin/v1
# One versioned path for programs outside the platform (D1, D3), behind the
# same gateway block '/twin/*' and the same key check as the app routes; the
# app routes above stay as they are. Stage 0: what the caller may read, and a
# description of the door (OpenAPI 3.1) generated from it.

def _v1_types(conn, tenant_id, name):
    bound = {}
    for b in _bindings(conn, name):
        bound.setdefault(b["type_key"], set()).add(
            "read" if b["direction"] == "consumes" else "write")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.key, d.kind, d.definition FROM oaap_model.type_definitions d "
            "JOIN oaap_model.activations a ON a.type_key = d.key "
            "WHERE a.tenant_id = %s ORDER BY d.key", (tenant_id,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        if r["key"] not in bound:
            continue
        d = r["definition"] or {}
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                d = {}
        t = {"key": r["key"], "kind": r["kind"], "access": sorted(bound[r["key"]]),
             "title": d.get("title") or d.get("label") or r["key"]}
        if d.get("semantic_id"):
            t["semantic_iri"] = semantic.expand(d["semantic_id"])
        for k in ("groups", "attributes", "relations"):
            if d.get(k):
                t[k] = d[k]
        out.append(t)
    return out


def _v1_caller():
    caller, err, code = require_caller()
    if err:
        return None, None, (jsonify({"error": err}), code)
    name, tenant_id, _origin = caller
    conn = get_conn(tenant_id)
    if conn is None:
        return None, None, (jsonify({"error": "this tenant has no twin yet"}), 409)
    return name, (tenant_id, conn), None


@app.get("/twin/v1/types")
def v1_types():
    name, tc, fail = _v1_caller()
    if fail:
        return fail
    tenant_id, conn = tc
    try:
        with conn:
            types = _v1_types(conn, tenant_id, name)
    finally:
        conn.close()
    return jsonify({"v": 1, "api": API_VERSION, "caller": name, "types": types})


def openapi_doc(types):
    """OpenAPI 3.1 for the door as THIS caller sees it -- the types it is
    bound to, nothing it may not reach (RFC-0120 §4.1)."""
    obj = {"type": "object", "properties": {
        "id": {"type": "string", "description": "urn:oaap:obj:<uuid>"},
        "type": {"type": "string"}, "title": {"type": "string"},
        "groups": {"type": "object"}}}
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "OAAP twin", "version": API_VERSION,
                 "description": "The tenant's digital twin (oaap.data.twin, RFC-0120). "
                                "Every call carries a bearer key; the tenant comes from "
                                "the key, never from the request. Reads answer as the "
                                "twin looked at one moment ('at'), only objects of the "
                                "types this key may read, an edge only when both ends "
                                "are visible."},
        "servers": [{"url": "/twin/v1"}],
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                           "description": "oaapk_<id>_<secret>"}},
            "schemas": {"Object": obj, "Type": {"type": "object", "properties": {
                "key": {"type": "string"}, "kind": {"type": "string"},
                "access": {"type": "array", "items": {"enum": ["read", "write"]}},
                "semantic_iri": {"type": "string"}}}}},
        "security": [{"bearer": []}],
        "paths": {
            "/types": {"get": {"summary": "The types this key may read or write",
                               "responses": {"200": {"description": "types"}}}},
            "/openapi.json": {"get": {"summary": "This document",
                                      "responses": {"200": {"description": "OpenAPI 3.1"}}}}},
        "x-oaap-types": [{"key": t["key"], "access": t["access"]} for t in types],
        "x-oaap-stage": "RFC-0120 stage 3 -- reads, graph, export, change feed, "
                        "periods and series; types of the tenant, upsert by key, batch, "
                        "retire, webhooks; receivers for inbound webhooks (D13)",
    }
    doc["paths"].update(_V1_READ_PATHS)
    for path, ops in _V1_WRITE_PATHS.items():
        doc["paths"].setdefault(path, {}).update(ops)
    return doc


def _q(name, desc, required=False, schema=None):
    return {"name": name, "in": "query", "required": required, "description": desc,
            "schema": schema or {"type": "string"}}


_AT = _q("at", "the moment to answer for: a date-time, or a date (= end of that day, UTC); "
               "default now")
_REL = _q("relations", "relation keys to follow, comma-separated; default all")
_DIR = _q("direction", "out, in or both", schema={"enum": ["out", "in", "both"]})
_V1_READ_PATHS = {
    "/objects": {"get": {
        "summary": "Objects of one type, filtered, one page at a time (ordered by title)",
        "parameters": [
            _q("type", "the object type", True), _AT,
            _q("where", "repeatable: <group>.<attr><op><value> (op = != < <= > >=), "
                        "'<group>.<attr> in a,b' or '<group>.<attr> has'; checked against "
                        "the attribute's value type"),
            _q("fields", "groups to answer, comma-separated; default all"),
            _q("limit", "at most 500, default 100", schema={"type": "integer"}),
            _q("cursor", "the 'next' of the previous page")],
        "responses": {"200": {"description": "{v, at, objects, next}"}}}},
    "/objects/{id}": {"get": {
        "summary": "One object with its groups",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}, _AT],
        "responses": {"200": {"description": "the object"}, "404": {"description": "none"}}}},
    "/objects/{id}/graph": {"get": {
        "summary": "The neighbourhood of an object: nodes and edges within depth hops",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}},
                       _q("depth", "1..5, default 2", schema={"type": "integer"}),
                       _REL, _DIR, _q("types", "node types the walk enters"), _AT],
        "responses": {"200": {"description": "{v, at, nodes, edges, truncated}"}}}},
    "/paths": {"get": {
        "summary": "The shortest paths between two objects",
        "parameters": [_q("from", "object id", True), _q("to", "object id", True), _REL, _DIR,
                       _q("max", "how many paths, 1..10, default 3",
                          schema={"type": "integer"}),
                       _q("length", "most hops, 1..6, default 6", schema={"type": "integer"}),
                       _AT],
        "responses": {"200": {"description": "{v, at, paths}"}}}},
    "/export": {"get": {
        "summary": "Nodes and edges in one document -- what a graph viewer loads",
        "parameters": [_q("types", "object types, comma-separated; default all it may read"),
                       _REL, _AT,
                       _q("format", "json or graphml", schema={"enum": ["json", "graphml"]}),
                       _q("attributes", "1 (default) or 0", schema={"enum": ["0", "1"]})],
        "responses": {"200": {"description": "JSON {v, at, types, nodes, edges, truncated} "
                                             "or GraphML"}}}},
    "/changes": {"get": {
        "summary": "Every change after a cursor, in order -- for a sync that missed nothing",
        "parameters": [_q("since", "the 'next' of the last answer, 0, or 'now'"),
                       _q("types", "object types, comma-separated"),
                       _q("limit", "at most 1000, default 200", schema={"type": "integer"})],
        "responses": {"200": {"description": "{v, changes, next, more}"}}}},
    "/objects/{id}/periods": {"get": {
        "summary": "The periods of an object's periodic attributes (a KPI per year, ...)",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}},
                       _q("attribute", "<group>.<attr>; default every periodic attribute"),
                       _q("from", "keep periods that end after this (date or date-time)"),
                       _q("to", "keep periods that begin before this"), _AT],
        "responses": {"200": {"description": "{v, at, object, periods: [{group, attribute, "
                                             "value, valid_from, valid_to, unit, source, "
                                             "quality, recorded_at, recorded_by}]}"}}}},
    "/series": {"get": {
        "summary": "The time series of an object (measured values)",
        "parameters": [_q("object", "object id", True)],
        "responses": {"200": {"description": "{object, series}"}}}},
    "/series/{id}/range": {"get": {
        "summary": "Points of one series between two times (the tier is chosen for you)",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "integer"}},
                       _q("from", "seconds or ISO-8601 with zone", True),
                       _q("to", "seconds or ISO-8601 with zone", True),
                       _q("step", "seconds per point, optional", schema={"type": "integer"})],
        "responses": {"200": {"description": "{series, tier, points}"}}}},
    "/series/{id}/latest": {"get": {
        "summary": "The latest point of one series",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "{series, latest}"}}}},
}


def _p(name, kind="string"):
    return {"name": name, "in": "path", "required": True, "schema": {"type": kind}}


def _w(summary, desc, params=(), codes=None):
    return {"summary": summary, "description": desc, "parameters": list(params),
            "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            "responses": codes or {"200": {"description": "done"},
                                   "400": {"description": "a request to fix, with a sentence"},
                                   "403": {"description": "not this client's to write"},
                                   "409": {"description": "in the way: retired, overlapping, "
                                                          "registered otherwise"}}}


_V1_WRITE_PATHS = {
    "/types": {"post": _w(
        "Register types of the tenant (needs 'models')",
        "{object_type?: {key, title, semantic_id?}, on?: <type>, groups: [{key, title, "
        "attributes: [{key, title, value_type, values?, unit?, periodic?, personal?}], "
        "relations: [{key, title, to}]}]} -- the object type becomes t.<tenant>.<key>; "
        "attribute, relation and group keys are reused when registered with the same "
        "meaning; additive only; 201 when something was registered, else 200")},
    "/objects/by-key/{key}": {
        "put": _w("Create or find the object this client knows as {key}, write its groups",
                  "{type, title, groups: {<group>: {attributes: {...}, relations: [{key, "
                  "target | target_key}]}}} -- the first group of a new object is its "
                  "owner's core group; a new title renames (owner only); 201 created, "
                  "200 found", [_p("key")]),
        "get": {"summary": "The object this client knows as {key}", "parameters": [_p("key")],
                "responses": {"200": {"description": "the object"},
                              "404": {"description": "none"}}}},
    "/objects/{id}/groups/{group}": {"put": _w(
        "Write this client's own group", "{attributes, relations} -- a group another origin "
        "owns is refused with its owner's name", [_p("id"), _p("group")])},
    "/objects/{id}/groups/{group}/periods": {"post": _w(
        "Write one period of a periodic attribute into this client's own group",
        "{attribute, value, valid_from, valid_to, unit?, source?, quality?}",
        [_p("id"), _p("group")])},
    "/objects/{id}/retire": {"post": _w(
        "Retire an object this client owns", "{reason?} -- it stays in the history and "
        "leaves every current read", [_p("id")])},
    "/objects/{id}/status": {
        "post": _w("Change the object's status along a declared transition",
                   "{to, reason?} -- a transition its type does not declare is refused (409) "
                   "naming the values allowed from here", [_p("id")]),
        "get": {"summary": "The object's status and its history, newest first",
                "parameters": [_p("id")],
                "responses": {"200": {"description": "{status, history, declaration}"},
                              "404": {"description": "none, or its type declares no status"}}}},
    "/batch": {"post": _w(
        "Several writes in one transaction, all or nothing",
        "{id, operations: [{op: upsert|group|period|retire|status, ...}]} -- the same id again "
        "answers the first run and writes nothing ('replayed': true)")},
    "/webhooks": {
        "post": _w("Register a webhook", "{url (https), types?, kinds?, own?} -- the answer "
                   "carries the secret once; every POST is signed X-OAAP-Signature: "
                   "t=<unix>,v1=hex(HMAC-SHA256(secret, '<t>.<body>'))",
                   codes={"201": {"description": "{id, secret, ...}"}}),
        "get": {"summary": "This client's webhooks and their state",
                "responses": {"200": {"description": "{webhooks}"}}}},
    "/webhooks/{id}": {"delete": {"summary": "Remove a webhook", "parameters": [
        _p("id", "integer")], "responses": {"204": {"description": "removed"}}}},
    "/webhooks/{id}/resume": {"post": _w(
        "Resume a stopped webhook from where it stopped", "", [_p("id", "integer")])},
    "/receivers": {
        "post": _w("Register a receiver for inbound webhooks (D13)",
                   "{name, signature?: {scheme: oaap|hmac-sha256|token, header?, prefix?}, "
                   "mapping: {id?, items?, rules: [{when?, upsert | retire | status}]}} -- the answer "
                   "carries the receiver's URL and its secret once",
                   codes={"201": {"description": "{name, url, secret, ...}"}}),
        "get": {"summary": "This client's receivers and their state",
                "responses": {"200": {"description": "{receivers}"}}}},
    "/receivers/{name}": {
        "put": _w("Replace a receiver's mapping (the secret stays)",
                  "{signature?, mapping}", [_p("name")]),
        "delete": {"summary": "Remove a receiver", "parameters": [_p("name")],
                   "responses": {"204": {"description": "removed"}}}},
    "/inbound/{client}/{name}": {"post": {
        "summary": "Where the sending system posts -- no key, the receiver's signature",
        "security": [], "parameters": [_p("client"), _p("name")],
        "responses": {"200": {"description": "{batch, replayed, operations, skipped}"},
                      "401": {"description": "signature"},
                      "404": {"description": "no such receiver"},
                      "422": {"description": "the mapping or the write rule refused it; "
                                             "nothing written"}}}},
}


# ------------------------------------------------------- RFC-0120 stage 1: reads
def v1_read(fn):
    """The frame of every read of the door: the caller, its tenant's
    connection, the types it may read (any binding, active for the tenant),
    the moment; a request it can fix answers 400 with the sentence."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        name, tc, fail = _v1_caller()
        if fail:
            return fail
        tenant_id, conn = tc
        try:
            with conn:
                types = _v1_types(conn, tenant_id, name)
                readable = {t["key"] for t in types if t["kind"] == "object_types"}
                return fn(conn, tenant_id, name, readable, *a, **k)
        except (tr.Bad, tv.Wrong) as e:
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()
    return wrapper


def _need_type(readable, key, name):
    if key not in readable:
        raise tr.Bad(f"'{name}' may not read type '{key}' -- it may read: "
                     + (", ".join(sorted(readable)) or "nothing"))


def _types_arg(readable, name):
    want = tr.csv_arg(request.args.get("types"))
    for t in want:
        _need_type(readable, t, name)
    return set(want)


def _visible(snap, raw):
    i = snap.canonical(tr.obj_id(raw))
    return i if i in snap.objects else None


@app.get("/twin/v1/objects")
@v1_read
def v1_objects(conn, tenant_id, name, readable):
    type_key = (request.args.get("type") or "").strip()
    if not type_key:
        raise tr.Bad("query needs '?type='")
    _need_type(readable, type_key, name)
    reg = tr.registry(conn, tenant_id)
    where = [tr.parse_where(w, reg) for w in request.args.getlist("where")][:8]
    limit = tr.int_arg(request.args.get("limit"), tr.LIST_DEFAULT, 1, tr.LIST_MAX, "limit")
    cursor = tr.decode_cursor(request.args["cursor"]) if request.args.get("cursor") else None
    fields = tr.csv_arg(request.args.get("fields")) if "fields" in request.args else None
    snap = tr.Snapshot(conn, readable, tr.parse_at(request.args.get("at")))
    objs, nxt = tr.list_objects(snap, type_key, where, limit, cursor, fields)
    return jsonify({"v": 1, "at": snap.stamp(), "objects": objs, "next": nxt})


@app.get("/twin/v1/objects/<oid>")
@v1_read
def v1_object(conn, tenant_id, name, readable, oid):
    snap = tr.Snapshot(conn, readable, tr.parse_at(request.args.get("at")))
    raw = tr.obj_id(oid)
    i = _visible(snap, oid)
    if i is None:
        return jsonify({"error": "no such object, or not of a type this key may read"}), 404
    out = dict(snap.obj(i, snap.groups([i])[i]), at=snap.stamp())
    if i != raw:
        out["merged_from"] = tr.urn(raw)
    st = _status_or_none(conn, i, snap.objects[i]["type"], snap.at)
    if st is not None:
        out["status"] = st
    return jsonify(out)


def _status_or_none(conn, oid, type_key, at=None):
    """The status for a read -- None without a declaration; a malformed
    declaration is not the reader's fault and reads as none."""
    try:
        return tst.status_of(conn, oid, type_key, at)
    except tw.Refused:
        return None


@app.get("/twin/v1/objects/<oid>/status")
@v1_read
def v1_object_status(conn, tenant_id, name, readable, oid):
    """The status and its history, newest first (oaap.data.twin 0.15 §2.27)."""
    snap = tr.Snapshot(conn, readable, tr.parse_at(request.args.get("at")))
    i = _visible(snap, oid)
    if i is None:
        return jsonify({"error": "no such object, or not of a type this key may read"}), 404
    type_key = snap.objects[i]["type"]
    try:
        decl = tst.declaration(conn, type_key)
    except tw.Refused as e:
        return jsonify({"error": str(e)}), 409
    if decl is None:
        return jsonify({"error": f"type '{type_key}' declares no status"}), 404
    return jsonify({"id": tr.urn(i), "at": snap.stamp(), "declaration": decl,
                    "status": tst.current(conn, i, decl, snap.at),
                    "history": [tst.entry(r) for r in tst.history(conn, i, snap.at)]})


def _window():
    frm = tv.moment(request.args["from"], "from") if request.args.get("from") else None
    to = tv.moment(request.args["to"], "to", end=True) if request.args.get("to") else None
    return frm, to


def _attribute_arg():
    raw = (request.args.get("attribute") or "").strip()
    if not raw:
        return None, None
    group, _, attr = raw.rpartition(".")
    if not tr.GROUP_RE.match(group or "") or not tr.KEY_RE.match(attr or ""):
        raise tr.Bad(f"'{raw}' is not <group>.<attribute> (e.g. esg.kpi.co2e)")
    return group, attr


@app.get("/twin/v1/objects/<oid>/periods")
@v1_read
def v1_periods(conn, tenant_id, name, readable, oid):
    at = tr.parse_at(request.args.get("at"))
    snap = tr.Snapshot(conn, readable, at)
    i = _visible(snap, oid)
    if i is None:
        return jsonify({"error": "no such object, or not of a type this key may read"}), 404
    group, attr = _attribute_arg()
    frm, to = _window()
    rows = tr.list_periods(conn, snap.members([i]), snap.moment, at is not None,
                           group, attr, frm, to)
    return jsonify({"v": 1, "at": snap.stamp(), "object": tr.urn(i), "periods": rows})


def _ts_read(tenant_id, fn):
    """A read of the tenant's time series for a person or a client: the
    driver opened and closed, its refusals as answers."""
    try:
        drv = get_ts(tenant_id)
    except psycopg2.Error:
        return jsonify({"error": "the store does not answer"}), 503
    if drv is None:
        return jsonify({"error": TS_OFF}), 409
    try:
        return fn(drv)
    except ts.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ts.TimeseriesError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        drv.conn.close()


def _object_type(conn, raw):
    """(canonical id, type key) of an object of this twin, or (None, None)."""
    m = OBJ_ID_RE.match(raw or "")
    if not m:
        raise tr.Bad(f"'{raw}' is not an object id (urn:oaap:obj:<uuid>)")
    oid = _canonical_of(conn, m.group(1).lower())
    with conn.cursor() as cur:
        cur.execute("SELECT type_key FROM objects WHERE id = %s", (oid,))
        r = cur.fetchone()
    return (oid, r["type_key"]) if r else (None, None)


def _series_list(conn, drv, oid):
    ids = [oid] + _aliases_of(conn, oid)
    return jsonify({"object": _urn(oid),
                    "series": [_ts_public(x) for i in ids for x in drv.list(i)]})


def _series_range(drv, sid):
    frm, to = _qtime(request.args.get("from")), _qtime(request.args.get("to"))
    if not frm or not to:
        raise tr.Bad("'from' and 'to' are needed (seconds or ISO-8601 with zone)")
    step = request.args.get("step")
    try:
        step = int(step) if step else None
    except ValueError:
        raise tr.Bad("step: whole seconds")
    return jsonify(drv.range(sid, frm, to, step, time.time()))


NO_OBJECT = "no such object, or not of a type this key may read"


@app.get("/twin/v1/series")
@v1_read
def v1_series(conn, tenant_id, name, readable):
    if not request.args.get("object"):
        raise tr.Bad("query needs '?object='")
    oid, type_key = _object_type(conn, request.args["object"])
    if oid is None or type_key not in readable:
        return jsonify({"error": NO_OBJECT}), 404
    return _ts_read(tenant_id, lambda drv: _series_list(conn, drv, oid))


def _v1_series_of(conn, readable, drv, sid):
    s = drv.get_series(sid)
    oid, type_key = _object_type(conn, s["object"])
    if oid is None or type_key not in readable:
        raise ts.NotFound(f"no series {sid}")
    return s


@app.get("/twin/v1/series/<int:sid>/range")
@v1_read
def v1_series_range(conn, tenant_id, name, readable, sid):
    def go(drv):
        _v1_series_of(conn, readable, drv, sid)
        return _series_range(drv, sid)
    return _ts_read(tenant_id, go)


@app.get("/twin/v1/series/<int:sid>/latest")
@v1_read
def v1_series_latest(conn, tenant_id, name, readable, sid):
    def go(drv):
        _v1_series_of(conn, readable, drv, sid)
        return jsonify({"series": sid, "latest": drv.latest(sid)})
    return _ts_read(tenant_id, go)


def _direction():
    d = (request.args.get("direction") or "").strip() or None
    if d not in (None, "out", "in", "both"):
        raise tr.Bad("'direction' is out, in or both")
    return d


@app.get("/twin/v1/objects/<oid>/graph")
@v1_read
def v1_graph(conn, tenant_id, name, readable, oid):
    depth = tr.int_arg(request.args.get("depth"), 2, 1, tr.GRAPH_DEPTH_MAX, "depth")
    types = _types_arg(readable, name)
    rels = set(tr.csv_arg(request.args.get("relations")))
    snap = tr.Snapshot(conn, readable, tr.parse_at(request.args.get("at")))
    i = _visible(snap, oid)
    if i is None:
        return jsonify({"error": "no such object, or not of a type this key may read"}), 404
    nodes, edges, cut = tr.neighbourhood(snap, i, depth, rels, _direction() or "both", types)
    return jsonify({"v": 1, "at": snap.stamp(), "start": tr.urn(i), "depth": depth,
                    "nodes": nodes, "edges": edges, "truncated": cut})


@app.get("/twin/v1/paths")
@v1_read
def v1_paths(conn, tenant_id, name, readable):
    if not request.args.get("from") or not request.args.get("to"):
        raise tr.Bad("query needs '?from=' and '?to='")
    rels = set(tr.csv_arg(request.args.get("relations")))
    limit = tr.int_arg(request.args.get("max"), 3, 1, tr.PATHS_MAX, "max")
    length = tr.int_arg(request.args.get("length"), tr.PATH_LEN_MAX, 1, tr.PATH_LEN_MAX, "length")
    snap = tr.Snapshot(conn, readable, tr.parse_at(request.args.get("at")))
    a, b = _visible(snap, request.args["from"]), _visible(snap, request.args["to"])
    if a is None or b is None:
        return jsonify({"error": "no such object, or not of a type this key may read"}), 404
    found = tr.paths(snap, a, b, rels, _direction() or "both", limit, length)
    nodes = sorted({n for p in found for n in p["nodes"]})
    return jsonify({"v": 1, "at": snap.stamp(), "from": tr.urn(a), "to": tr.urn(b),
                    "paths": found,
                    "nodes": [snap.node(tr.obj_id(n)) for n in nodes]})


@app.get("/twin/v1/export")
@v1_read
def v1_export(conn, tenant_id, name, readable):
    types = _types_arg(readable, name) or set(readable)
    rels = set(tr.csv_arg(request.args.get("relations")))
    fmt = (request.args.get("format") or "json").strip()
    if fmt not in ("json", "graphml"):
        raise tr.Bad("'format' is json or graphml")
    attrs = (request.args.get("attributes") or "1") != "0"
    reg = tr.registry(conn, tenant_id)
    snap = tr.Snapshot(conn, readable, tr.parse_at(request.args.get("at")))
    nodes, edges, cut = tr.export(snap, types, rels, attrs, reg)
    if fmt == "graphml":
        return app.response_class(tr.graphml(snap, nodes, edges, reg),
                                  mimetype="application/graphml+xml")
    tdocs = []
    for t in sorted(types):
        d = {"key": t}
        sid = (reg["object"].get(t) or {}).get("semantic_id")
        if sid:
            d["semantic_iri"] = semantic.expand(sid)
        tdocs.append(d)
    return jsonify({"v": 1, "at": snap.stamp(), "types": tdocs, "nodes": nodes,
                    "edges": edges, "truncated": cut})


@app.get("/twin/v1/changes")
@v1_read
def v1_changes(conn, tenant_id, name, readable):
    types = _types_arg(readable, name) or set(readable)
    limit = tr.int_arg(request.args.get("limit"), tr.CHANGES_DEFAULT, 1, tr.CHANGES_MAX, "limit")
    out, nxt, more = tr.changes(conn, types, (request.args.get("since") or "0").strip(), limit)
    return jsonify({"v": 1, "changes": out, "next": nxt, "more": more})


@app.get("/twin/v1/openapi.json")
def v1_openapi():
    name, tc, fail = _v1_caller()
    if fail:
        return fail
    tenant_id, conn = tc
    try:
        with conn:
            types = _v1_types(conn, tenant_id, name)
    finally:
        conn.close()
    return jsonify(openapi_doc(types))


# ------------------------------------------------ RFC-0120 stage 3: sync writes
def v1_write(fn):
    """The frame of every write of the door: an integration client only (an
    app writes through its own routes, a remote reader reads), its tenant's
    connection, one transaction -- a refusal anywhere rolls all of it back
    and answers its sentence as JSON."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        caller, err, code = require_caller()
        if err:
            return jsonify({"error": err}), code
        name, tenant_id, origin = caller
        if not is_client(name):
            return jsonify({"error": "the writes of /twin/v1 are an integration client's; an "
                                     "app writes through /twin/objects, a remote reader "
                                     "reads only"}), 403
        conn = get_conn(tenant_id)
        if conn is None:
            return jsonify({"error": "this tenant has no twin yet"}), 409
        try:
            with conn:
                door = tsy.Door(conn, tenant_id, name, origin, _bindings(conn, name))
                return fn(door, *a, **k)
        except tw.Refused as e:
            return jsonify({"error": str(e)}), e.code
        except (tr.Bad, tv.Wrong) as e:
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()
    return wrapper


def _body():
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        raise tw.Refused("the body is a JSON object", 400)
    return b


@app.post("/twin/v1/types")
@v1_write
def v1_create_types(door):
    """Types of the tenant (D8, D9): a client with `models` registers an object
    type `t.<tenant>.<Key>` with its groups, or adds groups to a type it
    writes. Additive only; the same body again changes nothing."""
    c = resolve_client(door.principal[len(CLIENT_PREFIX):]) or {}
    if not c.get("models"):
        raise tw.Refused(f"'{door.principal}' may not create types -- the tenant admin gives "
                         "a client 'models' (RFC-0120 D8)", 403)
    label = tenant_record(door.tenant_id).get("label") or door.tenant_id
    res = tsy.create_types(door.conn, door.tenant_id, label, door.principal, _body(),
                           lambda t: t in door.writable)
    return jsonify(res), (201 if res["created"] else 200)


@app.put("/twin/v1/objects/by-key/<path:key>")
@v1_write
def v1_upsert(door, key):
    res = door.upsert(key, _body())
    return jsonify(res), (201 if res["created"] else 200)


@app.get("/twin/v1/objects/by-key/<path:key>")
@v1_write
def v1_by_key(door, key):
    oid, _o = door.resolve(None, key=key)
    snap = tr.Snapshot(door.conn, door.readable, None)
    if oid not in snap.objects:
        return jsonify({"error": "no such object, or not of a type this key may read"}), 404
    return jsonify(dict(snap.obj(oid, snap.groups([oid])[oid]), at=snap.stamp(), key=key))


@app.put("/twin/v1/objects/<oid>/groups/<group_key>")
@v1_write
def v1_write_group(door, oid, group_key):
    i, o = door.resolve(oid)
    if not tr.GROUP_RE.match(group_key):
        raise tw.Refused("malformed group key", 400)
    return jsonify({"id": tr.urn(i), "written": door.write_group(i, o, group_key, _body())})


@app.post("/twin/v1/objects/<oid>/groups/<group_key>/periods")
@v1_write
def v1_write_period(door, oid, group_key):
    i, o = door.resolve(oid)
    if not tr.GROUP_RE.match(group_key):
        raise tw.Refused("malformed group key", 400)
    res = door.write_period(i, o, group_key, _body())
    return jsonify(res), (201 if res["written"] else 200)


@app.post("/twin/v1/objects/<oid>/retire")
@v1_write
def v1_retire(door, oid):
    i, o = door.resolve(oid)
    b = request.get_json(silent=True) or {}
    return jsonify(door.retire(i, o, b.get("reason") if isinstance(b, dict) else ""))


@app.post("/twin/v1/objects/<oid>/status")
@v1_write
def v1_set_status(door, oid):
    """A status change along a declared transition (§2.27)."""
    i, o = door.resolve(oid)
    b = _body()
    return jsonify(door.status(i, o, b.get("to"), b.get("reason")))


@app.post("/twin/v1/batch")
@v1_write
def v1_batch(door):
    b = _body()
    return jsonify(door.batch(b.get("id"), b.get("operations")))


@app.post("/twin/v1/webhooks")
@v1_write
def v1_add_hook(door):
    closed = tsy.egress_closed(tenant_record(door.tenant_id))
    res = tsy.add_hook(door.conn, door.principal, door.readable, _body(), HOOK_ALLOW, closed)
    return jsonify(res), 201


@app.get("/twin/v1/webhooks")
@v1_write
def v1_hooks(door):
    return jsonify({"webhooks": tsy.hooks_of(door.conn, door.principal)})


@app.delete("/twin/v1/webhooks/<int:hid>")
@v1_write
def v1_remove_hook(door, hid):
    tsy.remove_hook(door.conn, door.principal, hid)
    return "", 204


@app.post("/twin/v1/webhooks/<int:hid>/resume")
@v1_write
def v1_resume_hook(door, hid):
    return jsonify(tsy.resume_hook(door.conn, door.principal, hid))


# ------------------------------------- RFC-0120 D13: inbound webhooks (0.14 §2.26)
def _may_receive(door, body=None):
    c = resolve_client(door.principal[len(CLIENT_PREFIX):]) or {}
    if isinstance(body, dict) and body.get("mapping") is None and body.get("events") is not None:
        return          # 0.9: CloudEvents only -- nothing is written into the twin
    if not (c.get("writes") or c.get("models")):
        raise tw.Refused(f"'{door.principal}' writes nothing -- a receiver writes as its "
                         "client, so the tenant admin gives it 'writes' first", 403)


@app.post("/twin/v1/receivers")
@v1_write
def v1_add_receiver(door):
    body = _body()
    _may_receive(door, body)
    return jsonify(tsi.add(door.conn, door.principal, door.writable, body)), 201


@app.get("/twin/v1/receivers")
@v1_write
def v1_receivers(door):
    return jsonify({"receivers": tsi.receivers_of(door.conn, door.principal)})


@app.put("/twin/v1/receivers/<name>")
@v1_write
def v1_replace_receiver(door, name):
    body = _body()
    _may_receive(door, body)
    return jsonify(tsi.replace(door.conn, door.principal, door.writable, name, body))


@app.delete("/twin/v1/receivers/<name>")
@v1_write
def v1_remove_receiver(door, name):
    tsi.remove(door.conn, door.principal, name)
    return "", 204


@app.post("/twin/v1/inbound/<label>/<name>")
def v1_inbound(label, name):
    """Where a system that only speaks webhooks posts. No key: the gateway
    passes this path unverified with every identity header stripped
    (Caddyfile), and only the receiver's signature counts. The body becomes
    a batch under the client's rights and origin; the delivery id names it."""
    nothing = jsonify({"error": "no such receiver"}), 404
    if not tsi.NAME_RE.match(name) or not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", label):
        return nothing
    if (request.content_length or 0) > tsi.BODY_MAX:
        return jsonify({"error": f"a delivery is at most {tsi.BODY_MAX // 1024} KB"}), 413
    raw = request.get_data(cache=False)
    if len(raw) > tsi.BODY_MAX:
        return jsonify({"error": f"a delivery is at most {tsi.BODY_MAX // 1024} KB"}), 413
    client = resolve_client(label)
    if client is None:
        return nothing
    principal = CLIENT_PREFIX + label
    conn = get_conn(client["tenant"])
    if conn is None:
        return nothing
    try:
        with conn:
            rec = tsi.find(conn, principal, name)
        if rec is None:
            return nothing
        if not tsi.check_signature(rec, request.headers, raw):
            with conn:
                tsi.note(conn, rec["id"], refused=True)
            return jsonify({"error": "the signature does not fit"}), 401
        if rec.get("events") and (rec.get("mapping") in (None, {}) or
                                  request.headers.get("ce-specversion") or "cloudevents" in
                                  (request.headers.get("Content-Type") or "")):
            # 0.9 (RFC-0127 D8): CloudEvents in -- every event of the delivery
            # maps to a declared type, or none is taken
            evs, why = bm.parse_cloudevents(raw, request.headers)
            accepted = []
            for ev in evs or []:
                a, why = bm.map_cloudevent(ev, (rec["events"] or {}).get("types") or [])
                if a is None:
                    break
                accepted.append(a)
            if evs is None or why:
                with conn:
                    tsi.note(conn, rec["id"], error=why)
                return jsonify({"error": why}), 422
            with conn:
                stored, replayed = tsi.store_events(conn, rec, label, accepted)
            with conn:
                tsi.note(conn, rec["id"], written=stored)
            return jsonify({"accepted": len(accepted), "stored": stored, "replayed": replayed}), 202
        try:
            root = json.loads(raw)
        except ValueError:
            with conn:
                tsi.note(conn, rec["id"], error="the body is not JSON")
            return jsonify({"error": "the body is not JSON"}), 400
        bid = f"in:{name}:{tsi.delivery_id(rec['mapping'], raw, root, request.headers)}"
        try:
            with conn:
                ops, skipped = tsi.operations(rec["mapping"], root)
                if ops:
                    door = tsy.Door(conn, client["tenant"], principal, client["origin"],
                                    _bindings(conn, principal))
                    res = door.batch(bid, ops)
                else:
                    res = {"replayed": False}
        except tw.Refused as e:
            msg = str(e)
        except (tr.Bad, tv.Wrong) as e:
            msg = str(e)
        else:
            msg = None
        if msg is not None:
            with conn:
                tsi.note(conn, rec["id"], error=msg)
            return jsonify({"error": msg}), 422
        with conn:
            tsi.note(conn, rec["id"], written=0 if res.get("replayed") else len(ops))
        return jsonify({"batch": bid if ops else None, "replayed": bool(res.get("replayed")),
                        "operations": len(ops), "skipped": skipped})
    except psycopg2.errors.UndefinedTable:
        return nothing
    finally:
        conn.close()


@app.post("/twin/objects")
def create_object():
    caller, err, code = require_caller()
    if err:
        return err, code
    name, tenant_id, origin = caller
    if reads_only(name):
        return _NOT_HERE, 403
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


_write_group_content = tw.write_group_content

# oaap.data.twin 0.7 §2.6a: the most objects one list answers. A fleet of a
# few nodes has a few hundred instances at most; a list longer than this
# says so (`truncated`) rather than cutting silently.
LIST_MAX = 500


def _title_like(words):
    """(' AND title ILIKE %s ...', patterns) -- every word in the title,
    `%`, `_` and backslash taken literally. One rule for the app list
    (§2.6a) and the MCP search (§2.16)."""
    like = "".join(" AND title ILIKE %s" for _ in words)
    pats = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for w in words]
    return like, pats


@app.get("/twin/objects")
def list_objects():
    """oaap.data.twin 0.7 §2.6a (RFC-0085 §10): every object of one type,
    each answered as §2.6 answers it -- for an instance whose bindings name
    the type in either direction, exactly the rule of the single read. The
    fleet app reads its tenant's fleet with this; without it an app could
    only read objects whose ids it already held. A merged-away alias is no
    row of its own (it answers as its canonical object)."""
    caller, err, code = require_caller()
    if err:
        return err, code
    name, tenant_id, _origin = caller
    type_key = (request.args.get("type") or "").strip()
    if not type_key:
        return "query needs '?type='", 400
    at = _parse_at(request.args.get("at"))
    # RFC-0118 P6 (oaap.data.twin 0.8 §2.6a): an app's list report searches
    # and pages here instead of reading all and cutting at LIST_MAX --
    # `q` matches words in the title, `offset` skips that many rows
    words = [w for w in (request.args.get("q") or "").strip()[:200].split() if w][:8]
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        return "'offset' is a number", 400
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            bindings = _bindings(conn, name)
            if not any(b["type_key"] == type_key for b in bindings):
                return (f"'{name}' neither contributes nor consumes "
                        f"'{type_key}' -- see 'oaap data model "
                        "bindings' (RFC-0031 D7)"), 403
            with conn.cursor() as cur:
                like, pats = _title_like(words)
                cur.execute(
                    "SELECT id FROM objects WHERE type_key = %s AND id NOT IN "
                    "(SELECT alias_id FROM aliases WHERE unmerged_at IS NULL)" + like +
                    " ORDER BY title, id LIMIT %s OFFSET %s",
                    (type_key, *pats, LIST_MAX + 1, offset))
                ids = [str(r["id"]) for r in cur.fetchall()]
            out = []
            for oid in ids[:LIST_MAX]:
                loaded = _load_object(conn, oid, at)
                if loaded is not None:
                    out.append({"id": f"urn:oaap:obj:{loaded['canonical_id']}",
                                "type": loaded["type_key"], "title": loaded["title"],
                                "owner": loaded["owner"], "groups": loaded["groups"]})
    finally:
        conn.close()
    return jsonify({"objects": out, "truncated": len(ids) > LIST_MAX, "offset": offset})


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
    if reads_only(name):
        return _NOT_HERE, 403
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
            try:
                # "Nobody else writes there" (RFC-0031 §3.3) -- enforced
                # in twin_write, not by trusting the caller to behave.
                tw.write_group(conn, obj_id, group_key, origin, name, body)
            except tw.Refused as exc:
                return str(exc), exc.code
    finally:
        conn.close()
    return "", 204


@app.post("/twin/objects/<obj_id>/groups/<group_key>/periods")
def write_period(obj_id, group_key):
    """An app writes one period of a periodic attribute into its own group
    (oaap.data.twin 0.12 §2.20) -- the binding and the write rule of a group
    PUT."""
    caller, err, code = require_caller()
    if err:
        return err, code
    name, tenant_id, origin = caller
    if reads_only(name):
        return _NOT_HERE, 403
    m = OBJ_ID_RE.match(obj_id)
    if not m or not tr.GROUP_RE.match(group_key):
        return "malformed object id or group key", 400
    body = request.get_json(silent=True) or {}
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            oid = _canonical_of(conn, m.group(1))
            with conn.cursor() as cur:
                cur.execute("SELECT type_key FROM objects WHERE id = %s", (oid,))
                obj = cur.fetchone()
            if obj is None:
                return "no such object", 404
            if not any(b["type_key"] == obj["type_key"] and b["direction"] == "contributes"
                       for b in _bindings(conn, name)):
                return (f"'{name}' does not contribute to '{obj['type_key']}' "
                        "-- see 'oaap data model bindings' (RFC-0031 D4)"), 403
            res = tw.write_period(conn, oid, group_key, origin, name, body)
    finally:
        conn.close()
    return jsonify(res), (201 if res["written"] else 200)


def _periods_answer(conn, raw_id, at=None):
    """{object, periods} of one object of this twin (every periodic attribute,
    or `?attribute=<group>.<attr>`), or None if there is no such object."""
    m = OBJ_ID_RE.match(raw_id)
    if not m:
        raise tr.Bad("malformed object id")
    oid = _canonical_of(conn, m.group(1).lower())
    with conn.cursor() as cur:
        cur.execute("SELECT type_key FROM objects WHERE id = %s", (oid,))
        obj = cur.fetchone()
    if obj is None:
        return None, None
    group, attr = _attribute_arg()
    frm, to = _window()
    mem = {i: oid for i in [oid] + _aliases_of(conn, oid)}
    rows = tr.list_periods(conn, mem, at or tr.now(), at is not None, group, attr, frm, to)
    return obj["type_key"], {"object": tr.urn(oid), "periods": rows}


@app.get("/twin/objects/<obj_id>/periods")
def read_periods(obj_id):
    caller, err, code = require_caller()
    if err:
        return err, code
    name, tenant_id, _origin = caller
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            type_key, out = _periods_answer(conn, obj_id)
            if out is None:
                return "no such object", 404
            if not any(b["type_key"] == type_key for b in _bindings(conn, name)):
                return f"'{name}' neither contributes nor consumes '{type_key}'", 403
    except (tr.Bad, tv.Wrong) as e:
        return str(e), 400
    finally:
        conn.close()
    return jsonify(out)


# ============================================== the MCP source (0.9 §2.16)
# Reached ONLY through the door blocks appctl writes on a consuming MCP
# gateway's own site (RFC-0073 §4/§16): forward_auth at identity carries
# the person (or, at the discovery door, the gateway for the catalog),
# then the block SETS X-OAAP-Twin-Tenant to the gateway's tenant. '/mcp'
# is not under '/twin/*', so the public route never reaches it. The
# JSON-RPC rules live in mcp_door.py; this only binds them to a tenant.
TWIN_TENANT_HEADER = "X-OAAP-Twin-Tenant"
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class _McpStore:
    """What the tools read and write, for ONE tenant (the door's). The writes
    (oaap.data.twin 0.16 §2.28) go through the portal's own functions, as the
    person; a refusal becomes the tool's sentence."""

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    def _conn(self):
        conn = get_conn(self.tenant_id)
        if conn is None:
            raise mcp_door.Refused("Dieser Mandant hat noch keinen Zwilling.")
        return conn

    def types(self):
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT d.key, d.kind, d.definition FROM "
                        "oaap_model.type_definitions d JOIN oaap_model.activations a "
                        "ON a.type_key = d.key WHERE a.tenant_id = %s "
                        "ORDER BY d.kind, d.key", (self.tenant_id,))
                    rows = cur.fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            definition = r["definition"]
            if isinstance(definition, str):
                definition = json.loads(definition)
            row = {"key": r["key"], "kind": r["kind"],
                   "title": definition.get("title") or r["key"]}
            if definition.get("semantic_id"):
                row["semantic_iri"] = semantic.expand(definition["semantic_id"])
            out.append(row)
        return out

    def search(self, type_key, words, offset, limit):
        like, pats = _title_like(words)
        where = " AND type_key = %s" if type_key else ""
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, type_key, title, owner_origin FROM objects WHERE "
                        "id NOT IN (SELECT alias_id FROM aliases WHERE unmerged_at IS NULL)"
                        " AND " + NOT_RETIRED + where + like + " ORDER BY title, id LIMIT %s OFFSET %s",
                        ((type_key,) if type_key else ()) + (*pats, limit, offset))
                    rows = cur.fetchall()
        finally:
            conn.close()
        return [{"id": f"urn:oaap:obj:{r['id']}", "type": r["type_key"],
                 "title": r["title"], "owner": r["owner_origin"]} for r in rows]

    def object(self, obj_id, at):
        conn = self._conn()
        try:
            with conn:
                loaded = _load_object(conn, obj_id, _parse_at(at))
                if loaded is None:
                    return None
                aliases = _aliases_of(conn, loaded["canonical_id"])
                # each relation's target with its title -- an assistant
                # should not have to read every target to name it
                targets = {rel["target"][len("urn:oaap:obj:"):]
                           for g in loaded["groups"].values() for rel in g["relations"]}
                titles = {}
                if targets:
                    with conn.cursor() as cur:
                        cur.execute("SELECT id, title FROM objects WHERE id = ANY(%s::uuid[])",
                                    (sorted(targets),))
                        titles = {str(r["id"]): r["title"] for r in cur.fetchall()}
        finally:
            conn.close()
        for g in loaded["groups"].values():
            for rel in g["relations"]:
                t = titles.get(rel["target"][len("urn:oaap:obj:"):])
                if t is not None:
                    rel["target_title"] = t
        result = {"id": f"urn:oaap:obj:{loaded['canonical_id']}", "type": loaded["type_key"],
                  "title": loaded["title"], "owner": loaded["owner"],
                  "groups": loaded["groups"],
                  "merged_aliases": [f"urn:oaap:obj:{a}" for a in aliases]}
        if loaded["canonical_id"] != obj_id:
            result["requested_as_alias_of"] = f"urn:oaap:obj:{obj_id}"
        return result


def _mcp_write(store, fn):
    conn = store._conn()
    try:
        with conn:
            return fn(conn)
    except (tw.Refused, tr.Bad, tv.Wrong) as e:
        raise mcp_door.Refused(str(e)) from None
    finally:
        conn.close()


def _mcp_set_group(self, username, obj_id, group_key, attrs):
    if not tr.GROUP_RE.match(group_key or ""):
        raise mcp_door.Refused("group ist der Schlüssel einer Gruppe (namespace.name).")
    oid = _mcp_write(self, lambda c: person_group_write(c, self.tenant_id, username, obj_id,
                                                       group_key, {"attributes": attrs}))
    return {"id": f"urn:oaap:obj:{oid}", "group": group_key, "written": sorted(attrs)}


def _mcp_add_period(self, username, obj_id, group_key, body):
    if not tr.GROUP_RE.match(group_key or ""):
        raise mcp_door.Refused("group ist der Schlüssel einer Gruppe (namespace.name).")
    return _mcp_write(self, lambda c: person_period_write(c, self.tenant_id, username, obj_id,
                                                         group_key, body))


def _mcp_set_status(self, username, obj_id, to, reason):
    return _mcp_write(self, lambda c: person_status_change(c, username, obj_id, to, reason))


_McpStore.set_group = _mcp_set_group
_McpStore.add_period = _mcp_add_period
_McpStore.set_status = _mcp_set_status


@app.post("/mcp")
def mcp_source():
    tenant_id = (request.headers.get(TWIN_TENANT_HEADER) or "").strip()
    if not TENANT_ID_RE.match(tenant_id):
        return jsonify(error="the twin's MCP door is reached only through an MCP "
                             "gateway's door (oaap.data.twin §2.16)"), 403
    if not (request.content_type or "").startswith("application/json"):
        return jsonify(error="MCP spricht JSON."), 415
    if (request.content_length or 0) > 1024 * 1024:
        return jsonify(error="Rumpf zu groß"), 413
    try:
        msg = json.loads(request.get_data(cache=False) or b"{}")
    except ValueError:
        return jsonify({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}})
    out = mcp_door.answer(msg, mcp_door.who_from(request.headers), _McpStore(tenant_id))
    if out is None:
        return "", 202
    resp = app.response_class(json.dumps(out, ensure_ascii=False, default=str),
                              mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/mcp")
def mcp_source_get():
    return "", 405, {"Allow": "POST"}


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
        row = {"key": r["key"], "kind": r["kind"], "origin": r["origin"],
               "title": definition.get("title") or r["key"],
               "definition": definition}
        if definition.get("semantic_id"):
            # stored short, answered in full (oaap.data.model 0.2 §2.9)
            row["semantic_iri"] = semantic.expand(definition["semantic_id"])
        out.append(row)
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
                    "aliases WHERE unmerged_at IS NULL) AND " + NOT_RETIRED +
                    " ORDER BY title",
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
            status = _person_status(conn, loaded["canonical_id"], loaded["type_key"], at,
                                    _roles)
    finally:
        conn.close()
    result = {"id": f"urn:oaap:obj:{loaded['canonical_id']}", "type": loaded["type_key"],
              "title": loaded["title"], "owner": loaded["owner"],
              "groups": loaded["groups"],
              "merged_aliases": [f"urn:oaap:obj:{a}" for a in aliases]}
    if status is not None:
        result["status"] = status
    if loaded["canonical_id"] != obj_id:
        result["requested_as_alias_of"] = f"urn:oaap:obj:{obj_id}"
    return jsonify(result)


PERSON_WRITERS = {"admin", "keyuser", "tenant_admin"}


def _person_status(conn, oid, type_key, at, roles):
    """The object page's status: current, the values this person may go to
    from here, the history (oaap.data.twin 0.15 §2.27). None without a
    declaration."""
    try:
        decl = tst.declaration(conn, type_key)
    except tw.Refused:
        return None
    if decl is None:
        return None
    moment = None
    if at is not None:
        moment = datetime.datetime.combine(at, datetime.time(23, 59, 59, 999999),
                                           datetime.timezone.utc)
    cur = tst.current(conn, oid, decl, moment)
    may = bool(roles & PERSON_WRITERS)
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM retirements WHERE object_id = %s LIMIT 1", (oid,))
        retired = c.fetchone() is not None
    return dict(cur, values=decl["values"],
                allowed=[] if (retired or at is not None or not may)
                else tst.allowed_from(decl, cur["value"], ["persons"]),
                history=[tst.entry(r) for r in tst.history(conn, oid, moment, 50)])


@app.post("/internal/twin/objects/<obj_id>/status")
def twin_set_status_person(obj_id):
    """A person changes the status on the object page -- the same rule as a
    client (§2.27), as `persons`; the origin is tenant:<sign-in name>."""
    person, err, code = require_person()
    if err:
        return err, code
    username, tenant_id, roles = person
    if not (roles & PERSON_WRITERS):
        return ("changing a status needs role admin, keyuser or tenant_admin "
                "(oaap.data.twin 0.15 §2.27)"), 403
    m = OBJ_ID_RE.match(obj_id)
    if not m:
        return "malformed object id", 400
    body = request.get_json(silent=True) or {}
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            res = person_status_change(conn, username, m.group(1), body.get("to"),
                                       body.get("reason"))
    except tw.Refused as e:
        return str(e), e.code
    finally:
        conn.close()
    return jsonify(res)


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


@app.get("/internal/twin/objects/<obj_id>/affected")
def twin_affected(obj_id):
    """RFC-0087 §6, stage 2: what depends on this object -- the relations
    of the fleet read BACKWARDS (uses, reaches, behind, bindsTo), each row
    with its tenant (heldBy, destOf). Today's state only; the walk is
    fleet_affected.affected, the same the hub's matter uses. FleetView
    draws it later."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, _roles = person
    m = OBJ_ID_RE.match(obj_id)
    if not m:
        return "malformed object id", 400
    obj_id = m.group(1)
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            rels, objects = fleet_affected.load_graph(conn)
    finally:
        conn.close()
    result = fleet_affected.affected(rels, objects, obj_id)
    if result is None:
        return "no such fleet object", 404
    for r in [result["start"]] + result["affected"]:
        for k in ("id", "from"):
            if r.get(k):
                r[k] = f"urn:oaap:obj:{r[k]}"
    return jsonify(result)


@app.get("/internal/twin/assets")
def twin_assets():
    """RFC-0088 stage 1: the tenant's own asset universe for the portal page
    "Assets" -- every app and asset the node wrote under `platform:assets`,
    each with its current values, its relations and what depends on it
    (assets_twin.affected). The tenant_admin's read (D3: the whole universe
    is theirs); the server_admin's for the tenant the portal names."""
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, roles = person
    if not roles & {"tenant_admin", "server_admin"}:
        return "the asset universe is the tenant_admin's to read", 403
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"apps": [], "items": [], "twin": False})
    rk = {assets_model.ak(r): r for r in assets_twin.REL_KEYS}
    ak = {assets_model.ak(a[0]): a[0] for a in assets_model._ATTRS}
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT o.id, o.type_key, o.title, s.source_key FROM objects o "
                            "JOIN source_keys s ON s.object_id = o.id AND s.origin = %s "
                            "WHERE o.type_key = ANY(%s)",
                            (assets_model.ORIGIN, [assets_model.APP, assets_model.ITEM]))
                objects = {str(r["id"]): {"type": r["type_key"], "title": r["title"],
                                          "source": r["source_key"], "values": {}}
                           for r in cur.fetchall()}
                ids = list(objects)
                rels = []
                if ids:
                    cur.execute("SELECT object_id, attr_key, value FROM current_attributes "
                                "WHERE object_id = ANY(%s::uuid[]) AND group_key = ANY(%s) "
                                "AND valid_to IS NULL",
                                (ids, ["oaap.assetapp", "oaap.assetitem"]))
                    for r in cur.fetchall():
                        short = ak.get(r["attr_key"])
                        if short:
                            objects[str(r["object_id"])]["values"][short] = r["value"]
                    cur.execute("SELECT object_id, rel_key, target_id FROM current_relations "
                                "WHERE object_id = ANY(%s::uuid[]) AND valid_to IS NULL "
                                "AND rel_key = ANY(%s)", (ids, list(rk)))
                    rels = [(str(r["object_id"]), rk[r["rel_key"]], str(r["target_id"]))
                            for r in cur.fetchall()]
    finally:
        conn.close()
    out = assets_twin.universe(objects, rels)
    out["twin"] = True
    return jsonify(out)


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
                    "unmerged_at IS NULL) AND " + NOT_RETIRED + " ORDER BY title")
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
                    # 'alias_id' is the table's PRIMARY KEY (appctl.py's
                    # own comment: "one object merged away at most once
                    # AT A TIME") -- but an id that was unmerged and is
                    # now merging again is the SAME alias_id with an
                    # OLD, historical row already sitting there
                    # (unmerge sets unmerged_at, never deletes, on
                    # purpose). A plain INSERT hits that row's PK and
                    # fails with a 500 -- found live on oaap-test,
                    # 2026-09-10, re-merging the very Anna duplicate
                    # this step exists to resolve, right after proving
                    # unmerge works. ON CONFLICT turns the history back
                    # into an active merge instead of colliding with it.
                    "INSERT INTO aliases (alias_id, canonical_id, merged_at, "
                    "merged_by) VALUES (%s, %s, now(), %s) "
                    "ON CONFLICT (alias_id) DO UPDATE SET "
                    "canonical_id = EXCLUDED.canonical_id, merged_at = now(), "
                    "merged_by = EXCLUDED.merged_by, unmerged_at = NULL, "
                    "unmerged_by = NULL",
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


def _tenant_group_type(conn, tenant_id, group_key, type_key):
    """A person has no bindings (only an instance does) -- this replaces that
    check: the group type must exist, be active for this tenant, and attach to
    THIS object's own type. None if it does, else (sentence, code)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.definition FROM oaap_model.type_definitions d "
            "JOIN oaap_model.activations a ON a.type_key = d.key "
            "WHERE a.tenant_id = %s AND d.key = %s AND d.kind = 'group_types'",
            (tenant_id, group_key))
        gt = cur.fetchone()
    if gt is None:
        return f"group type '{group_key}' is not active for this tenant", 404
    gdef = tv.defn(gt["definition"])
    if gdef.get("on") != type_key:
        return (f"group type '{group_key}' attaches to "
                f"'{gdef.get('on')}', not '{type_key}'"), 400
    return None


TWIN_WRITERS = {"admin", "keyuser", "tenant_admin"}


@app.post("/internal/twin/objects/<obj_id>/groups/<group_key>/periods")
def twin_write_period_person(obj_id, group_key):
    """A person enters one period into a TENANT group (the portal's form
    "Werte mit Zeitraum", RFC-0120 stage 2) -- the roles and the rule of the
    group write below; the group is created if the tenant has none yet."""
    person, err, code = require_person()
    if err:
        return err, code
    username, tenant_id, roles = person
    if not (roles & TWIN_WRITERS):
        return "writing into a tenant group needs role admin, keyuser or tenant_admin", 403
    m = OBJ_ID_RE.match(obj_id)
    if not m or not tr.GROUP_RE.match(group_key):
        return "malformed object id or group key", 400
    body = request.get_json(silent=True) or {}
    conn = get_conn(tenant_id)
    if conn is None:
        return "this tenant has no twin schema yet", 409
    try:
        with conn:
            res = person_period_write(conn, tenant_id, username, m.group(1), group_key, body)
    finally:
        conn.close()
    return jsonify(res), (201 if res["written"] else 200)


def person_period_write(conn, tenant_id, username, obj_id, group_key, body):
    """One period into a TENANT group -- the portal's form and the MCP tool
    twin_add_period (§2.28) both call this."""
    oid, type_key, _owner = _person_object(conn, obj_id)
    refused = _tenant_group_type(conn, tenant_id, group_key, type_key)
    if refused:
        raise tw.Refused(refused[0], refused[1])
    return tw.write_period(conn, oid, group_key, "tenant", username, body,
                           event_origin=f"tenant:{username}")


def person_status_change(conn, username, obj_id, to, reason):
    """A status change as `persons` -- the portal's card and the MCP tool
    twin_set_status (§2.28) both call this."""
    oid = _canonical_of(conn, obj_id)
    with conn.cursor() as cur:
        cur.execute("SELECT type_key, owner_origin FROM objects WHERE id = %s", (oid,))
        obj = cur.fetchone()
    if obj is None:
        raise tw.Refused("no such object", 404)
    origin = f"tenant:{username}"      # the sign-in name, never the body's
    return tst.change(conn, oid, obj["type_key"], obj["owner_origin"], to, reason,
                      origin, username, ["persons"])


@app.get("/internal/twin/objects/<obj_id>/periods")
def twin_periods_person(obj_id):
    person, err, code = require_person()
    if err:
        return err, code
    _username, tenant_id, _roles = person
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"periods": []})
    try:
        with conn:
            _type, out = _periods_answer(conn, obj_id)
    except (tr.Bad, tv.Wrong) as e:
        return str(e), 400
    finally:
        conn.close()
    if out is None:
        return "no such object", 404
    return jsonify(out)


@app.get("/internal/twin/series")
def twin_series_person():
    """The series of one object, for the portal's object page (RFC-0120 stage
    2: series for persons) -- a person reads the tenant's whole twin."""
    person, err, code = require_person()
    if err:
        return err, code
    tenant_id = person[1]
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"series": []})
    try:
        with conn:
            oid, _type = _object_type(conn, request.args.get("object") or "")
            if oid is None:
                return "no such object", 404
            return _ts_read(tenant_id, lambda drv: _series_list(conn, drv, oid))
    except tr.Bad as e:
        return str(e), 400
    finally:
        conn.close()


@app.get("/internal/twin/series/<int:sid>/range")
def twin_series_range_person(sid):
    person, err, code = require_person()
    if err:
        return err, code
    try:
        return _ts_read(person[1], lambda drv: (drv.get_series(sid), _series_range(drv, sid))[1])
    except tr.Bad as e:
        return str(e), 400


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
            person_group_write(conn, tenant_id, username, obj_id, group_key, body)
    finally:
        conn.close()
    return "", 204


def _person_object(conn, obj_id):
    """(canonical id, type key, owner) of an object a person writes -- 404
    when there is none, 409 when it is retired (§2.24)."""
    oid = _canonical_of(conn, obj_id)
    with conn.cursor() as cur:
        cur.execute("SELECT type_key, owner_origin FROM objects WHERE id = %s", (oid,))
        obj = cur.fetchone()
        if obj is None:
            raise tw.Refused("no such object", 404)
        cur.execute("SELECT 1 FROM retirements WHERE object_id = %s LIMIT 1", (oid,))
        if cur.fetchone():
            raise tw.Refused("the object is retired -- it is not written again", 409)
    return oid, obj["type_key"], obj["owner_origin"]


def person_group_write(conn, tenant_id, username, obj_id, group_key, body):
    """A person's write into a TENANT group -- the portal's route and the MCP
    tool twin_set_group (oaap.data.twin 0.16 §2.28) both call this; inside
    the caller's transaction, refusals raise tw.Refused."""
    oid, type_key, _owner = _person_object(conn, obj_id)
    refused = _tenant_group_type(conn, tenant_id, group_key, type_key)
    if refused:
        raise tw.Refused(refused[0], refused[1])
    with conn.cursor() as cur:
        cur.execute("SELECT origin FROM groups WHERE object_id = %s "
                    "AND group_key = %s", (oid, group_key))
        existing = cur.fetchone()
    if existing is None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO groups (object_id, group_key, origin, "
                "created_by) VALUES (%s, %s, %s, %s)",
                (oid, group_key, "tenant", username))
    elif existing["origin"] != "tenant":
        raise tw.Refused(f"group '{group_key}' belongs to '{existing['origin']}' "
                         "-- the tenant may not overwrite another origin's "
                         "group (RFC-0031 §3.3)", 403)
    _write_group_content(conn, oid, group_key, "tenant", username, body)
    _record_event(conn, "group.written", oid, group_key, f"tenant:{username}")
    return oid


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
        if a.get("unit") is not None and (not isinstance(a["unit"], str)
                                          or len(a["unit"]) > 40):
            return f"attribute '{a['key']}': 'unit' is a short text", 400
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
                             "value_type": a.get("value_type", "text"),
                             # RFC-0120 stage 2: values with a period (D4)
                             **({"periodic": True} if a.get("periodic") else {}),
                             **({"unit": a["unit"].strip()} if (a.get("unit") or "").strip()
                                else {})})))
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


@app.get("/internal/twin/webhooks")
def twin_webhooks_person():
    """The webhooks of the tenant's clients with their state -- the portal's
    page *Integration* (RFC-0120 stage 3: "stops after a limit, which the
    portal shows"). Never a secret."""
    person, err, code = require_person()
    if err:
        return err, code
    _u, tenant_id, roles = person
    if not (roles & {"admin", "tenant_admin", "server_admin"}):
        return "the webhooks of the clients are the tenant admin's page", 403
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"webhooks": []})
    try:
        with conn:
            return jsonify({"webhooks": tsy.hooks_of(conn)})
    except psycopg2.errors.UndefinedTable:
        return jsonify({"webhooks": []})
    finally:
        conn.close()


@app.get("/internal/twin/receivers")
def twin_receivers_person():
    """The receivers of the tenant's clients with their state -- the portal's
    page *Integration* (RFC-0120 D13). Never a secret."""
    person, err, code = require_person()
    if err:
        return err, code
    _u, tenant_id, roles = person
    if not (roles & {"admin", "tenant_admin", "server_admin"}):
        return "the receivers of the clients are the tenant admin's page", 403
    conn = get_conn(tenant_id)
    if conn is None:
        return jsonify({"receivers": []})
    try:
        with conn:
            return jsonify({"receivers": tsi.receivers_of(conn)})
    except psycopg2.errors.UndefinedTable:
        return jsonify({"receivers": []})
    finally:
        conn.close()


# ============================================================ outbox (0.3)
@app.get("/internal/twin/outbox")
def twin_outbox():
    """How far the outbox relay (relay.py, RFC-0032 §1.5) has come, per
    tenant -- the portal's health page turns this into "the outbox grows
    and nobody collects it" (oaap.core.portal 2.5).

    The one '/internal/twin/*' route WITHOUT require_person(): it answers
    a node-wide operator question, not a person's question about their
    own tenant, and it returns counts and ages only -- never an object,
    a group or a value. The prefix guard above still demands
    INTERNAL_API_KEY; the portal additionally shows the result only to
    'server_admin'/'support', the same audience as the rest of that page.

    Ages come from Postgres's own clock (now() - checked_at), so a clock
    difference between containers cannot make a live relay look silent.
    """
    out = []
    for schema in sorted(load_secrets()):
        if not schema.startswith("twin_"):
            continue
        tenant_id = schema[len("twin_"):]
        entry = {"tenant": tenant_id}
        try:
            conn = get_conn(tenant_id)
        except psycopg2.Error:
            entry["error"] = "store not reachable"
            out.append(entry)
            continue
        if conn is None:
            continue
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT w.last_event_id, w.last_error, "
                        "EXTRACT(EPOCH FROM now() - w.checked_at) AS checked_age, "
                        "(SELECT count(*) FROM events e WHERE e.id > w.last_event_id) "
                        "AS pending FROM relay_watermark w WHERE w.id = 1")
                    row = cur.fetchone()
            if row is None:
                entry["error"] = "relay watermark row missing"
            else:
                entry.update(
                    pending=int(row["pending"]),
                    watermark=int(row["last_event_id"]),
                    checked_age=(float(row["checked_age"])
                                 if row["checked_age"] is not None else None),
                    last_error=row["last_error"])
        except psycopg2.Error:
            # A tenant schema provisioned before 0.3 and not yet migrated
            # ('oaap data store migrate-twin', run by every update).
            entry["error"] = "relay tables missing"
        finally:
            conn.close()
        out.append(entry)
    return jsonify({"tenants": out})


# ======================================================= time series (0.1)
# oaap.data.timeseries 0.1 §3 (RFC-0086). The caller is require_caller()'s,
# exactly as for objects: the tenant comes from the credential, the right
# from the caller's bindings to the OBJECT's type -- a series has no
# rights of its own, it is a property of its object (RFC-0086 D2).

TS_OFF = ("time series are not enabled for this tenant -- the operator runs "
          "'oaap data timeseries enable <tenant>' (oaap.data.timeseries 0.1)")
# points per minute and tenant (oaap.data.timeseries 0.1 §3); a tenant that
# needs more says so -- there is no switch for it in 0.1
TS_POINTS_PER_MINUTE = ts.DEFAULT_POINTS_PER_MINUTE
TS_READ_ONLY = ("a remote reader reads only (oaap.data.twin 0.4 §2.14) -- "
                "writing belongs to an app on the node that holds the data")


def get_ts(tenant_id):
    """The driver on the tenant's OWN time-series schema and role, or None
    when the operator has not enabled time series for it. A plain cursor:
    the driver asks for a dict cursor where it wants one."""
    schema = f"ts_{tenant_id}"
    creds = load_secrets().get(schema)
    if creds is None:
        return None
    conn = psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                            user=creds["role"], password=creds["password"],
                            connect_timeout=5)
    return ts.PostgresDriver(conn, schema)


def _urn(obj_id):
    return f"urn:oaap:obj:{obj_id}"


def _ts_public(s):
    return {**s, "object": _urn(s["object"])}


class _Refused(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def _ts_object(tenant_id, name, raw_id, write):
    """(canonical object id, type key) of an object of the caller's twin
    the caller may read -- or, with `write`, contribute to. Raises
    _Refused otherwise. An id merged away resolves to its canonical one,
    so a series never forks along a merge (RFC-0031 D3)."""
    m = OBJ_ID_RE.match(raw_id or "") if isinstance(raw_id, str) else None
    if not m:
        raise _Refused("malformed object id", 400)
    conn = get_conn(tenant_id)
    if conn is None:
        raise _Refused("this tenant has no twin schema yet -- a series belongs "
                       "to a twin object (RFC-0086 D2)", 409)
    try:
        with conn:
            try:
                oid = _canonical_of(conn, m.group(1))
                with conn.cursor() as cur:
                    cur.execute("SELECT type_key FROM objects WHERE id = %s", (oid,))
                    obj = cur.fetchone()
            except psycopg2.DataError:
                raise _Refused("malformed object id", 400)
            if obj is None:
                raise _Refused("no such object in this tenant's twin", 404)
            bindings = _bindings(conn, name)
    finally:
        conn.close()
    if write:
        ok = any(b["type_key"] == obj["type_key"] and b["direction"] == "contributes"
                 for b in bindings)
        verb = "does not contribute to"
    else:
        ok = any(b["type_key"] == obj["type_key"] for b in bindings)
        verb = "neither contributes nor consumes"
    if not ok:
        raise _Refused(f"'{name}' {verb} '{obj['type_key']}' -- see 'oaap data "
                       "model bindings'", 403)
    return oid, obj["type_key"]


def ts_route(write):
    """Caller, driver, refusals -> HTTP, and the connection closed on every
    path. A refusal of the driver is the caller's error (400); a missing
    series 404; the minute's allowance 429."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            caller, err, code = require_caller()
            if err:
                return err, code
            if write and reads_only(caller[0]):
                return TS_READ_ONLY, 403
            try:
                drv = get_ts(caller[1])
            except psycopg2.Error:
                return "the store does not answer", 503
            if drv is None:
                return TS_OFF, 409
            try:
                return fn(caller, drv, *a, **kw)
            except _Refused as e:
                return str(e), e.code
            except ts.NotFound as e:
                return str(e), 404
            except ts.RateLimited as e:
                return str(e), 429
            except ts.TimeseriesError as e:
                return str(e), 400
            except psycopg2.Error as e:
                # the store's fault, not the caller's: said, never a bare 500
                app.logger.warning("timeseries: %s: %s", type(e).__name__, e)
                return (f"the time-series store failed ({type(e).__name__}) -- "
                        "the operator sees it in 'oaap data timeseries show'"), 503
            finally:
                drv.conn.close()
        return wrapper
    return deco


@app.put("/twin/series")
@ts_route(write=True)
def ts_ensure(caller, drv):
    name, tenant_id, _origin = caller
    body = request.get_json(silent=True) or {}
    raw_id = body.get("object")
    oid, _type = _ts_object(tenant_id, name, raw_id, write=True)
    s, created = drv.ensure_series(
        oid, body.get("measure"), body.get("kind", "number"), body.get("labels"),
        body.get("unit", ""), body.get("policy", "standard"), body.get("expected_s"))
    return jsonify({"series": _ts_public(s), "created": created}), (201 if created else 200)


@app.get("/twin/series")
@ts_route(write=False)
def ts_list(caller, drv):
    name, tenant_id, _origin = caller
    raw_id = request.args.get("object")
    oid, _type = _ts_object(tenant_id, name, raw_id, write=False)
    conn = get_conn(tenant_id)
    try:
        with conn:
            ids = [oid] + _aliases_of(conn, oid)
    finally:
        conn.close()
    out = [s for i in ids for s in drv.list(i)]
    return jsonify({"object": _urn(oid), "series": [_ts_public(s) for s in out]})


def _ts_series(caller, drv, series_id, write):
    s = drv.get_series(series_id)
    _ts_object(caller[1], caller[0], s["object"], write)
    return s


@app.post("/twin/series/<int:series_id>/points")
@ts_route(write=True)
def ts_write(caller, drv, series_id):
    _ts_series(caller, drv, series_id, write=True)
    points = (request.get_json(silent=True) or {}).get("points")
    if not isinstance(points, list):
        return "body needs 'points': [[time, value], ...]", 400
    if len(points) > ts.MAX_BATCH:
        return f"at most {ts.MAX_BATCH} points per call", 400
    now = time.time()
    drv.admit(len(points), now, TS_POINTS_PER_MINUTE)
    return jsonify(drv.write(series_id, points, now))


@app.post("/twin/points")
@ts_route(write=True)
def ts_write_many(caller, drv):
    """Several series in one call (the fleet service's form). Every series
    is checked first; one the caller may not write is answered with its
    reason and the others are written -- never all-or-nothing on rights,
    because a sender cannot fix a revoked binding by resending."""
    body = (request.get_json(silent=True) or {}).get("series")
    if not isinstance(body, dict):
        return "body needs 'series': {\"<id>\": [[time, value], ...]}", 400
    if sum(len(v) for v in body.values() if isinstance(v, list)) > ts.MAX_BATCH:
        return f"at most {ts.MAX_BATCH} points per call", 400
    out, todo, allowed = {}, [], {}
    for key, points in body.items():
        try:
            sid = int(key)
            if not isinstance(points, list):
                raise _Refused("points: a list of [time, value]", 400)
            s = drv.get_series(sid)
            if s["object"] not in allowed:
                try:
                    _ts_object(caller[1], caller[0], s["object"], write=True)
                    allowed[s["object"]] = None
                except _Refused as e:
                    allowed[s["object"]] = str(e)
            if allowed[s["object"]]:
                raise _Refused(allowed[s["object"]], 403)
            todo.append((sid, points))
        except (ValueError, _Refused) as e:   # NotFound is a ValueError too
            out[key] = {"error": str(e)}
    now = time.time()
    if todo:
        drv.admit(sum(len(p) for _sid, p in todo), now, TS_POINTS_PER_MINUTE)
    for sid, points in todo:
        try:
            out[str(sid)] = drv.write(sid, points, now)
        except ts.TimeseriesError as e:
            out[str(sid)] = {"error": str(e)}
    return jsonify({"series": out})


def _qtime(v):
    """A time from the query string: seconds arrive as text there, and the
    driver takes seconds as a number or ISO-8601 as text (§2)."""
    if v and re.fullmatch(r"\d{1,12}(\.\d+)?", v):
        return float(v)
    return v


@app.get("/twin/series/<int:series_id>/range")
@ts_route(write=False)
def ts_range(caller, drv, series_id):
    _ts_series(caller, drv, series_id, write=False)
    frm, to = _qtime(request.args.get("from")), _qtime(request.args.get("to"))
    if not frm or not to:
        return "'from' and 'to' are needed (seconds or ISO-8601 with zone)", 400
    step = request.args.get("step")
    try:
        step = int(step) if step else None
    except ValueError:
        return "step: whole seconds", 400
    return jsonify(drv.range(series_id, frm, to, step, time.time()))


@app.get("/twin/series/<int:series_id>/latest")
@ts_route(write=False)
def ts_latest(caller, drv, series_id):
    _ts_series(caller, drv, series_id, write=False)
    return jsonify({"series": series_id, "latest": drv.latest(series_id)})


@app.delete("/twin/series/<int:series_id>")
@ts_route(write=True)
def ts_delete(caller, drv, series_id):
    """Confirmed by naming the series a second time (`?confirm=<id>`), so
    a DELETE that reached the wrong path removes nothing; audited in the
    tenant's own schema (who, which series, how many points)."""
    _ts_series(caller, drv, series_id, write=True)
    if request.args.get("confirm") != str(series_id):
        return (f"deleting removes the series and all its points -- confirm "
                f"with '?confirm={series_id}'"), 409
    s = drv.drop_series(series_id, by=caller[0])
    return jsonify({"deleted": _ts_public(s)})


# The event bus's door (oaap.events.bus 0.1, RFC-0127 stage 1): the gateway
# passes /platform/events/* of an instance's site here as /bus/v1/*.
import bus_door  # noqa: E402
bus_door.register(app, get_conn, PLATFORM_APPS_DIR)
