"""Modelling and sync through the door /twin/v1 (RFC-0120 stage 3,
oaap.data.twin 0.13 §2.22-2.25).

What a program outside the platform -- an enterprise modelling tool, a ticket
system -- needs to keep its own objects in a tenant's twin without ever
overwriting what the platform, an app or a person owns:

- **Types of the tenant** (D8, D9): a client with `models` creates object
  types, their group types, attribute and relation types. An object type gets
  the tenant's namespace, `t.<tenant label>.<Key>`; attribute, relation and
  group keys stay node-wide and are **reused** when the same key is already
  registered with the same meaning (value type, values, unit, periodic; from
  and to), refused when it means something else. Additive only: a group type
  of this tenant may gain attributes and relations, never lose or change one.
- **Upsert by the source's own key**: the client's key of an object
  (`source_keys`, origin `client:<label>`) finds it or creates it -- the same
  call twice makes one object. A relation may name its target by the client's
  key instead of the twin's id.
- **Batches**: up to BATCH_MAX operations in one transaction, all or nothing,
  under an id the client chooses -- the same id again answers what the first
  run answered and writes nothing.
- **Retire** instead of delete: the owner ends an object; it stays in the
  history (a read with an earlier `at` sees it), disappears from every
  current read, and is never written again.
- **Webhooks** (registration here, delivery in hooks.py): a client names a URL;
  every change it may read is posted there, signed.

The write rule is the twin's own (twin_write): a group belongs to the origin
that created it, and the origin of everything a client writes is
`client:<label>` -- a group a person or an app created is refused with a
sentence that names its owner. This is the sync rule: nobody overwrites
anybody.

`conn` is the tenant's connection (dict cursor); nothing here commits.
"""
import json
import re
import secrets as _secrets

import twin_read as tr
import twin_status as tst
import twin_values as tv
import twin_write as tw

MODEL_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,62}$")
GROUP_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")
TENANT_TYPE_RE = re.compile(r"^t\.([a-z0-9][a-z0-9-]{0,62})\.([A-Za-z][A-Za-z0-9]{0,62})$")
VALUE_TYPES = ("text", "int", "decimal", "bool", "date", "datetime", "enum", "ref")
SOURCE_KEY_MAX = 200
TITLE_MAX = 300
BATCH_MAX = 500
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
HOOKS_PER_CLIENT = 5
HOOK_KINDS = ("object.created", "group.written", "period.written", "object.retired",
              "object.renamed", "object.merged", "object.unmerged", "status.changed")
# what an attribute type means -- the same key with the same meaning is reused
_ATTR_MEANING = ("value_type", "values", "unit", "periodic")


def _refuse(msg, code=400):
    raise tw.Refused(msg, code)


def tenant_of(doc, tenant_id):
    """A tenant's record from tenants.json, which the host writes as
    {"tenants": {id: record}} (appctl.save_tenants) -- one reading for the
    twin (label, closed at registration) and hooks.py (closed at delivery)."""
    t = doc.get("tenants") if isinstance(doc, dict) else None
    return ((t if isinstance(t, dict) else {}).get(tenant_id)) or {}


def egress_closed(record):
    return bool((record.get("egress") or {}).get("closed"))


def tenant_type_key(label, key):
    return f"t.{label}.{key}"


def _title(raw, fallback):
    t = raw if isinstance(raw, str) else ""
    t = " ".join(t.split())[:TITLE_MAX]
    return t or fallback


# ------------------------------------------------------------------ rights
def created_types(conn, tenant_id, principal):
    """Object types this principal created in this tenant (D8: a modelling
    client owns what it modelled, without the operator naming it again)."""
    with conn.cursor() as cur:
        cur.execute("SELECT d.key FROM oaap_model.type_definitions d "
                    "JOIN oaap_model.activations a ON a.type_key = d.key "
                    "WHERE a.tenant_id = %s AND d.kind = 'object_types' "
                    "AND d.origin = %s AND d.definition::jsonb->>'created_by' = %s",
                    (tenant_id, f"tenant:{tenant_id}", principal))
        return [r["key"] for r in cur.fetchall()]


def client_bindings(conn, tenant_id, principal, record):
    """The bindings of an integration client: `reads` consume, `writes`
    contribute as owner (create objects, write its own groups), and every
    object type it created itself contributes as owner too."""
    out = {}
    for t in record.get("reads") or []:
        out[t] = "consumes"
    for t in (record.get("writes") or []) + (created_types(conn, tenant_id, principal)
                                             if record.get("models") else []):
        out[t] = "contributes"
    return [{"type_key": t, "direction": d, "role": "owner" if d == "contributes" else None,
             "group_key": None, "declared_as": "integration client"}
            for t, d in sorted(out.items())]


# ------------------------------------------------------------------ types
def _type_row(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT key, kind, origin, definition FROM oaap_model.type_definitions "
                    "WHERE key = %s", (key,))
        r = cur.fetchone()
    return (r, tv.defn(r["definition"])) if r else (None, None)


def _activate(conn, tenant_id, key):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO oaap_model.activations (tenant_id, type_key) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (tenant_id, key))


def _register(conn, tenant_id, kind, key, definition):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO oaap_model.type_definitions (key, kind, origin, package, "
                    "version, definition) VALUES (%s, %s, %s, %s, '1.0.0', %s)",
                    (key, kind, f"tenant:{tenant_id}", tenant_id, json.dumps(definition)))
    _activate(conn, tenant_id, key)


def _replace(conn, key, definition):
    """The one UPDATE of the registry this module makes: an additive change of
    a group type of this tenant (new attributes or relations appended) -- the
    same as `oaap data model register` of a higher version, which overwrites
    the definition row too."""
    with conn.cursor() as cur:
        cur.execute("UPDATE oaap_model.type_definitions SET definition = %s WHERE key = %s",
                    (json.dumps(definition), key))


def _attr_definition(a, principal):
    if not isinstance(a, dict):
        _refuse("every attribute is an object {key, value_type, ...}")
    key = a.get("key") or ""
    if not isinstance(key, str) or not MODEL_KEY_RE.match(key):
        _refuse(f"attribute key '{key}' is not a key ([A-Za-z][A-Za-z0-9]*)")
    vt = a.get("value_type", "text")
    if vt not in VALUE_TYPES:
        _refuse(f"attribute '{key}': value_type is one of {', '.join(VALUE_TYPES)}")
    d = {"key": key, "title": _title(a.get("title"), key), "value_type": vt,
         "created_by": principal}
    if a.get("values") is not None:
        if vt != "enum" or not isinstance(a["values"], list) or not all(
                isinstance(v, str) and v for v in a["values"]) or len(a["values"]) > 200:
            _refuse(f"attribute '{key}': 'values' is a list of texts, for an enum")
        d["values"] = list(a["values"])
    if a.get("unit") is not None:
        if not isinstance(a["unit"], str) or len(a["unit"]) > 40:
            _refuse(f"attribute '{key}': 'unit' is a short text")
        if a["unit"].strip():
            d["unit"] = a["unit"].strip()
    for flag in ("periodic", "personal"):
        if a.get(flag) is True:
            d[flag] = True
    return d


def _same_meaning(old, new, fields):
    return all(old.get(f) == new.get(f) for f in fields)


def create_types(conn, tenant_id, label, principal, body, may_write):
    """Register (or find) an object type of this tenant with its groups, or
    groups on an object type the caller may write (`may_write(type_key)`).
    Returns {object_type, created: [...], reused: [...], unchanged: [...]}.
    Refused (400/403/409) with a sentence before anything is written."""
    if not isinstance(body, dict):
        _refuse("the body is an object {object_type?, on?, groups}")
    groups = body.get("groups") or []
    if not isinstance(groups, list) or len(groups) > 50:
        _refuse("'groups' is a list of at most 50 group types")
    created, reused, unchanged = [], [], []
    ot = body.get("object_type")
    if ot is not None:
        if not isinstance(ot, dict) or not MODEL_KEY_RE.match(str(ot.get("key") or "")):
            _refuse("'object_type.key' is a key ([A-Za-z][A-Za-z0-9]*); the tenant's "
                    "namespace is added (t.<tenant>.<key>)")
        type_key = tenant_type_key(label, ot["key"])
        row, _d = _type_row(conn, type_key)
        if row is None:
            d = {"key": type_key, "title": _title(ot.get("title"), ot["key"]),
                 "created_by": principal}
            if ot.get("title_plural"):
                d["title_plural"] = _title(ot["title_plural"], d["title"])
            if isinstance(ot.get("semantic_id"), str) and ot["semantic_id"].strip():
                d["semantic_id"] = ot["semantic_id"].strip()[:300]
            if ot.get("status") is not None:
                d["status"] = tst.check_declaration(ot["status"])
            _register(conn, tenant_id, "object_types", type_key, d)
            created.append(type_key)
        elif row["kind"] != "object_types" or row["origin"] != f"tenant:{tenant_id}":
            _refuse(f"'{type_key}' is registered otherwise", 409)
        else:
            _activate(conn, tenant_id, type_key)
            unchanged.append(type_key)
    else:
        type_key = str(body.get("on") or "").strip()
        if not type_key:
            _refuse("the body names a new 'object_type' or the type its groups go 'on'")
        row, _d = _type_row(conn, type_key)
        if row is None or row["kind"] != "object_types":
            _refuse(f"there is no object type '{type_key}'", 404)
        if not may_write(type_key):
            _refuse(f"'{principal}' may not write type '{type_key}' -- a group goes onto a "
                    "type it may write", 403)
    if type_key not in created and not may_write(type_key) and ot is not None:
        # an existing namespaced type another principal of this tenant made
        _refuse(f"'{type_key}' was made by "
                f"{(_type_row(conn, type_key)[1] or {}).get('created_by') or 'someone else'}"
                f" -- '{principal}' may not change it", 403)
    if ot is not None and type_key not in created and ot.get("status") is not None:
        # status on a type this client made: additive (oaap.data.model 0.5 §2.11)
        _row, d = _type_row(conn, type_key)
        merged, changed = tst.merge_declaration(d.get("status"),
                                                tst.check_declaration(ot["status"]))
        if changed:
            _replace(conn, type_key, dict(d, status=merged))
            created.append(f"{type_key} (status)")
            unchanged.remove(type_key)
    for g in groups:
        if not isinstance(g, dict) or not GROUP_KEY_RE.match(str(g.get("key") or "")):
            _refuse(f"group key '{(g or {}).get('key') if isinstance(g, dict) else g}' "
                    "looks like 'namespace.name' (lowercase)")
        attr_keys, rel_keys = [], []
        for a in g.get("attributes") or []:
            d = _attr_definition(a, principal)
            row, old = _type_row(conn, d["key"])
            if row is None:
                _register(conn, tenant_id, "attribute_types", d["key"], d)
                created.append(d["key"])
            elif row["kind"] != "attribute_types" or not _same_meaning(old, d, _ATTR_MEANING):
                _refuse(f"attribute '{d['key']}' is already registered as "
                        f"{old.get('value_type') or row['kind']} -- pick another key, or the "
                        "same meaning", 409)
            else:
                _activate(conn, tenant_id, d["key"])
                reused.append(d["key"])
            attr_keys.append(d["key"])
        for r in g.get("relations") or []:
            if not isinstance(r, dict) or not MODEL_KEY_RE.match(str(r.get("key") or "")):
                _refuse("every relation is an object {key, to, title?} with a key "
                        "([A-Za-z][A-Za-z0-9]*)")
            to = str(r.get("to") or "")
            if MODEL_KEY_RE.match(to) and _type_row(conn, to)[0] is None:
                to = tenant_type_key(label, to)          # a type of this same model
            trow, _x = _type_row(conn, to)
            if trow is None or trow["kind"] != "object_types":
                _refuse(f"relation '{r['key']}': there is no object type '{r.get('to')}'", 404)
            d = {"key": r["key"], "from": type_key, "to": to,
                 "title": _title(r.get("title"), r["key"]), "created_by": principal}
            if r.get("title_inverse"):
                d["title_inverse"] = _title(r["title_inverse"], "")
            row, old = _type_row(conn, d["key"])
            if row is None:
                _register(conn, tenant_id, "relation_types", d["key"], d)
                created.append(d["key"])
            elif row["kind"] != "relation_types" or not _same_meaning(old, d, ("from", "to")):
                _refuse(f"relation '{d['key']}' is already registered from "
                        f"'{old.get('from')}' to '{old.get('to')}' -- pick another key", 409)
            else:
                _activate(conn, tenant_id, d["key"])
                reused.append(d["key"])
            rel_keys.append(d["key"])
        gkey = g["key"]
        row, old = _type_row(conn, gkey)
        new = {"key": gkey, "title": _title(g.get("title"), gkey), "on": type_key,
               "attributes": attr_keys, "relations": rel_keys, "activities": [],
               "created_by": principal}
        if row is None:
            _register(conn, tenant_id, "group_types", gkey, new)
            created.append(gkey)
            continue
        if row["kind"] != "group_types" or row["origin"] != f"tenant:{tenant_id}" \
                or old.get("on") != type_key:
            _refuse(f"group type '{gkey}' is already registered "
                    f"({row['origin']}, on '{old.get('on')}') -- pick another key", 409)
        if old.get("created_by") != principal:
            _refuse(f"group type '{gkey}' was made by "
                    f"{old.get('created_by') or 'a person in the portal'} -- '{principal}' "
                    "may not change it", 403)
        more_a = [k for k in attr_keys if k not in (old.get("attributes") or [])]
        more_r = [k for k in rel_keys if k not in (old.get("relations") or [])]
        if more_a or more_r:
            old = dict(old, attributes=(old.get("attributes") or []) + more_a,
                       relations=(old.get("relations") or []) + more_r)
            _replace(conn, gkey, old)
            created.append(f"{gkey} (+{', '.join(more_a + more_r)})")
        else:
            unchanged.append(gkey)
        _activate(conn, tenant_id, gkey)
    if created:
        tw.record_event(conn, "type.created", None, type_key, principal)
    return {"object_type": type_key, "created": created, "reused": reused,
            "unchanged": unchanged}


# ------------------------------------------------------------------ objects
def source_key(raw):
    k = raw if isinstance(raw, str) else ""
    if not k.strip() or len(k) > SOURCE_KEY_MAX or any(ord(ch) < 32 for ch in k):
        _refuse(f"a key is 1..{SOURCE_KEY_MAX} characters, no control characters")
    return k


def retired_at(conn, oid):
    with conn.cursor() as cur:
        cur.execute("SELECT retired_at FROM retirements WHERE object_id = %s "
                    "ORDER BY id LIMIT 1", (oid,))
        r = cur.fetchone()
    return r["retired_at"] if r else None


def _object(conn, oid):
    with conn.cursor() as cur:
        cur.execute("SELECT id, type_key, title, owner_origin FROM objects WHERE id = %s", (oid,))
        return cur.fetchone()


class Door:
    """What one client may do in one call: its principal, origin, tenant and
    the types it reads and writes (from client_bindings)."""

    def __init__(self, conn, tenant_id, principal, origin, bindings):
        self.conn, self.tenant_id = conn, tenant_id
        self.principal, self.origin = principal, origin
        self.readable = {b["type_key"] for b in bindings}
        self.writable = {b["type_key"] for b in bindings if b["direction"] == "contributes"}
        self.owns = {b["type_key"] for b in bindings
                     if b["direction"] == "contributes" and b["role"] == "owner"}

    # -- finding objects
    def resolve(self, ref, key=None):
        """The canonical id of an object named by `ref` (urn) or by this
        client's own `key`, visible to it -- or a 404/400 sentence."""
        if key is not None:
            oid = tw.find_by_source_key(self.conn, self.origin, source_key(key))
            if oid is None:
                _refuse(f"no object has the key '{key}' of {self.origin}", 404)
        else:
            try:
                oid = tw.canonical_of(self.conn, tr.obj_id(ref))
            except tr.Bad as e:
                _refuse(str(e))
        o = _object(self.conn, oid)
        if o is None or o["type_key"] not in self.readable:
            _refuse("no such object, or not of a type this key may read", 404)
        return oid, o

    def _writable_object(self, oid, o):
        if o["type_key"] not in self.writable:
            _refuse(f"'{self.principal}' may not write type '{o['type_key']}' -- it may "
                    "write: " + (", ".join(sorted(self.writable)) or "nothing"), 403)
        when = retired_at(self.conn, oid)
        if when is not None:
            _refuse(f"the object was retired on {tv.iso(when)} -- it is not written again", 409)

    # -- groups
    def _group_type(self, gkey, type_key):
        row, d = _type_row(self.conn, gkey)
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM oaap_model.activations WHERE tenant_id = %s "
                        "AND type_key = %s", (self.tenant_id, gkey))
            active = cur.fetchone() is not None
        if row is None or row["kind"] != "group_types" or not active:
            _refuse(f"group type '{gkey}' is not active for this tenant -- a client writes "
                    "groups its tenant has registered (POST /twin/v1/types)", 404)
        if d.get("on") != type_key:
            _refuse(f"group type '{gkey}' attaches to '{d.get('on')}', not '{type_key}'")
        return d

    def _relations(self, rels, gdef):
        """Relations with the target named by id or by this client's key,
        each resolved to a visible object."""
        if not isinstance(rels, list):
            _refuse("'relations' is a list of {key, target | target_key}")
        out = []
        for r in rels:
            if not isinstance(r, dict) or not tw.KEY_RE.match(str(r.get("key") or "")):
                _refuse("every relation is an object {key, target | target_key}")
            if gdef.get("relations") and r["key"] not in gdef["relations"]:
                _refuse(f"group '{gdef.get('key')}' has no relation '{r['key']}' -- it has: "
                        + ", ".join(gdef["relations"]))
            if "target_key" in r:
                tid, _t = self.resolve(None, key=r["target_key"])
            else:
                tid, _t = self.resolve(r.get("target"))
            out.append({k: v for k, v in r.items() if k in ("key", "valid_from", "valid_to")}
                       | {"target": tr.urn(tid)})
        return out

    def write_group(self, oid, o, gkey, content):
        if not isinstance(content, dict):
            _refuse(f"group '{gkey}': its content is an object {{attributes, relations}}")
        self._writable_object(oid, o)
        gdef = self._group_type(gkey, o["type_key"])
        attrs = content.get("attributes") or {}
        if not isinstance(attrs, dict):
            _refuse("'attributes' is an object {key: value}")
        if gdef.get("attributes"):
            extra = [k for k in attrs if k not in gdef["attributes"]]
            if extra:
                _refuse(f"group '{gkey}' has no attribute '{extra[0]}' -- it has: "
                        + ", ".join(gdef["attributes"]))
        payload = {"attributes": attrs,
                   "relations": self._relations(content.get("relations") or [], gdef)}
        for k in ("valid_from", "valid_to"):
            if content.get(k) is not None:
                payload[k] = content[k]
        return tw.write_group(self.conn, oid, gkey, self.origin, self.principal, payload)

    def write_period(self, oid, o, gkey, body):
        self._writable_object(oid, o)
        self._group_type(gkey, o["type_key"])
        return tw.write_period(self.conn, oid, gkey, self.origin, self.principal, body)

    # -- upsert
    def upsert(self, key, body):
        """{id, created, written, renamed} -- finds the object this client
        knows as `key` or creates it (owner of its type needed), renames it
        when the owner sends another title, writes the groups given."""
        key = source_key(key)
        if not isinstance(body, dict):
            _refuse("the body is an object {type, title, groups}")
        type_key = str(body.get("type") or "").strip()
        groups = body.get("groups") or {}
        if not isinstance(groups, dict):
            _refuse("'groups' is an object {group key: {attributes, relations}}")
        oid = tw.find_by_source_key(self.conn, self.origin, key)
        created = renamed = False
        if oid is None:
            if not type_key:
                _refuse("a new object needs 'type'")
            if type_key not in self.owns:
                _refuse(f"'{self.principal}' may not create objects of '{type_key}' -- it "
                        "creates objects of: " + (", ".join(sorted(self.owns)) or "nothing"),
                        403)
            title = _title(body.get("title"), "")
            if not title:
                _refuse("a new object needs 'title'")
            if not groups:
                _refuse("a new object needs at least one group -- its owner's core group")
            core = next(iter(groups))
            self._group_type(core, type_key)
            # one writer per key at a time: two calls with the same key make one object
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"key:{self.origin}:{key}",))
            oid = tw.find_by_source_key(self.conn, self.origin, key)
            if oid is None:
                oid, created = tw.ensure_object(self.conn, type_key, key, title, self.origin,
                                                self.principal, core)
        o = _object(self.conn, oid)
        if type_key and o["type_key"] != type_key:
            _refuse(f"the key '{key}' names an object of type '{o['type_key']}', not "
                    f"'{type_key}'", 409)
        self._writable_object(oid, o)
        title = _title(body.get("title"), "")
        if not created and title and title != o["title"]:
            if o["owner_origin"] != self.origin:
                _refuse(f"the title belongs to the owner '{o['owner_origin']}'", 403)
            rename(self.conn, oid, o["title"], title, self.origin, self.principal)
            renamed = True
        n = 0
        for gkey, content in groups.items():
            n += self.write_group(oid, o, gkey, content)
        return {"id": tr.urn(oid), "created": created, "written": n, "renamed": renamed}

    def status(self, oid, o, to, reason=None):
        """A status change by this client (oaap.data.twin 0.15 §2.27): owner
        when it owns the object, writer when it may write the type."""
        kinds = [k for k, yes in (("owner", o["owner_origin"] == self.origin),
                                  ("writers", o["type_key"] in self.writable)) if yes]
        if not kinds:
            _refuse(f"'{self.principal}' may not write type '{o['type_key']}'", 403)
        return tst.change(self.conn, oid, o["type_key"], o["owner_origin"], to, reason,
                          self.origin, self.principal, kinds)

    def retire(self, oid, o, reason=""):
        if o["type_key"] not in self.writable:
            _refuse(f"'{self.principal}' may not write type '{o['type_key']}'", 403)
        if o["owner_origin"] != self.origin:
            _refuse(f"the object belongs to '{o['owner_origin']}' -- only its owner retires it",
                    403)
        when = retired_at(self.conn, oid)
        if when is not None:
            return {"id": tr.urn(oid), "retired": tv.iso(when), "written": False}
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO retirements (object_id, origin, reason, retired_by) "
                        "VALUES (%s, %s, %s, %s) RETURNING retired_at",
                        (oid, self.origin, _title(reason, "") or None, self.principal))
            when = cur.fetchone()["retired_at"]
        tw.record_event(self.conn, "object.retired", oid, "", self.origin)
        return {"id": tr.urn(oid), "retired": tv.iso(when), "written": True}

    # -- batch
    def batch(self, batch_id, ops):
        """All or nothing, under an id: the same id again answers the first
        run's result and writes nothing ('replayed': true)."""
        if not isinstance(batch_id, str) or not BATCH_ID_RE.match(batch_id):
            _refuse("'id' names the batch: 1..100 of [A-Za-z0-9._:-] -- the same id again "
                    "writes nothing")
        if not isinstance(ops, list) or not ops:
            _refuse("'operations' is a list of at least one operation")
        if len(ops) > BATCH_MAX:
            _refuse(f"a batch holds at most {BATCH_MAX} operations")
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"batch:{self.principal}:{batch_id}",))
            cur.execute("SELECT result FROM batches WHERE principal = %s AND batch_id = %s",
                        (self.principal, batch_id))
            done = cur.fetchone()
        if done:
            return dict(tv.defn(done["result"]), replayed=True)
        results = []
        for i, op in enumerate(ops, 1):
            try:
                results.append(self._op(op))
            except tw.Refused as e:
                raise tw.Refused(f"operation {i}: {e} -- nothing of this batch was written",
                                 e.code)
        result = {"id": batch_id, "results": results}
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO batches (principal, batch_id, result) VALUES (%s, %s, %s)",
                        (self.principal, batch_id, json.dumps(result)))
        return dict(result, replayed=False)

    def _op(self, op):
        if not isinstance(op, dict):
            _refuse("every operation is an object {op, ...}")
        kind = op.get("op")
        if kind == "upsert":
            return dict(self.upsert(op.get("key"), op), op="upsert")
        if kind not in ("group", "period", "retire", "status"):
            _refuse(f"op is one of upsert, group, period, retire, status -- not '{kind}'")
        if not op.get("object") and op.get("key") is None:
            _refuse(f"op '{kind}' names its object: 'object' (id) or 'key' (this client's key)")
        oid, o = self.resolve(op.get("object"), key=op.get("key"))
        if kind == "retire":
            return dict(self.retire(oid, o, op.get("reason") or ""), op="retire")
        if kind == "status":
            return dict(self.status(oid, o, op.get("to"), op.get("reason")), op="status")
        gkey = str(op.get("group") or "")
        if not GROUP_KEY_RE.match(gkey):
            _refuse(f"op '{kind}' names its 'group'")
        if kind == "group":
            n = self.write_group(oid, o, gkey, op)
            return {"op": "group", "id": tr.urn(oid), "written": n}
        res = self.write_period(oid, o, gkey, op)
        return {"op": "period", "id": tr.urn(oid), "written": res["written"]}


def rename(conn, oid, old, new, origin, recorded_by):
    """A new title by the owner: the old one is kept in `renames` (a read with
    an earlier `at` answers it), the object row carries the current one."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO renames (object_id, old_title, new_title, recorded_by) "
                    "VALUES (%s, %s, %s, %s)", (oid, old, new, recorded_by))
        cur.execute("UPDATE objects SET title = %s WHERE id = %s", (new, oid))
    tw.record_event(conn, "object.renamed", oid, "", origin)


# ------------------------------------------------------------------ webhooks
def hook_problem(url, allowed_private=()):
    """'' or the sentence why this URL cannot be a webhook target. https
    only; a name or address of this platform's own network never (the
    delivery checks every address again before it connects)."""
    import ipaddress
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 500:
        return "'url' is an https URL of at most 500 characters"
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        if u.port == 0:          # a bad port raises ValueError here
            return "'url' names no port"
    except ValueError:
        return "'url' is not a URL"
    if u.username or u.password:
        return "a webhook URL carries no user or password -- the signature proves the sender"
    if not host:
        return "'url' names no host"
    if host in allowed_private:
        return "" if u.scheme in ("http", "https") else "'url' is http(s)"
    if u.scheme != "https":
        return "a webhook URL is https"
    if "." not in host:
        return f"'{host}' is a name of this platform's network, not a host outside"
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return f"{host} is not an address on the internet"
    except ValueError:
        if host.endswith((".local", ".internal", ".localhost", ".lan", ".home.arpa")):
            return f"'{host}' is a local name, not a host outside"
    return ""


def add_hook(conn, principal, readable, body, allowed_private=(), closed=False):
    """Register a webhook -> its record with the secret, shown this once."""
    if closed:
        _refuse("this tenant's outbound traffic is closed (RFC-0068) -- it posts no webhooks",
                409)
    if not isinstance(body, dict):
        _refuse("the body is an object {url, types?, kinds?, own?}")
    why = hook_problem(body.get("url"), allowed_private)
    if why:
        _refuse(why)
    types = body.get("types") or []
    kinds = body.get("kinds") or []
    if not isinstance(types, list) or not isinstance(kinds, list):
        _refuse("'types' and 'kinds' are lists")
    for t in types:
        if t not in readable:
            _refuse(f"'{principal}' may not read type '{t}' -- it may read: "
                    + (", ".join(sorted(readable)) or "nothing"))
    for k in kinds:
        if k not in HOOK_KINDS:
            _refuse(f"kind '{k}' is not one of: {', '.join(HOOK_KINDS)}")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM webhooks WHERE principal = %s "
                    "AND removed_at IS NULL", (principal,))
        if cur.fetchone()["n"] >= HOOKS_PER_CLIENT:
            _refuse(f"a client has at most {HOOKS_PER_CLIENT} webhooks -- remove one first", 409)
        cur.execute("SELECT coalesce(max(id), 0) AS m FROM events")
        start = int(cur.fetchone()["m"])
        secret = "whsec_" + _secrets.token_urlsafe(32)
        cur.execute("INSERT INTO webhooks (principal, url, types, kinds, own, secret, cursor) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (principal, body["url"], types, kinds, body.get("own") is True, secret,
                     start))
        row = cur.fetchone()
    return dict(hook_json(row), secret=secret)


def hook_json(r):
    def t(v):
        return tv.iso(v) if v else None
    state = ("removed" if r.get("removed_at") else "stopped" if r.get("stopped_at")
             else "retrying" if r.get("failures") else "active")
    return {"id": int(r["id"]), "client": r["principal"], "url": r["url"],
            "types": list(r.get("types") or []), "kinds": list(r.get("kinds") or []),
            "own": bool(r.get("own")), "state": state, "since_event": int(r["cursor"]),
            "delivered": int(r.get("delivered") or 0), "failures": int(r.get("failures") or 0),
            "last_ok": t(r.get("last_ok_at")), "last_error": r.get("last_error"),
            "next_try": t(r.get("next_try_at")), "stopped": t(r.get("stopped_at")),
            "created": t(r.get("created_at"))}


def hooks_of(conn, principal=None):
    with conn.cursor() as cur:
        if principal:
            cur.execute("SELECT * FROM webhooks WHERE principal = %s AND removed_at IS NULL "
                        "ORDER BY id", (principal,))
        else:
            cur.execute("SELECT * FROM webhooks WHERE removed_at IS NULL ORDER BY principal, id")
        return [hook_json(r) for r in cur.fetchall()]


def _own_hook(conn, principal, hid):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM webhooks WHERE id = %s AND principal = %s "
                    "AND removed_at IS NULL", (hid, principal))
        r = cur.fetchone()
    if r is None:
        _refuse("no such webhook of this client", 404)
    return r


def remove_hook(conn, principal, hid):
    _own_hook(conn, principal, hid)
    with conn.cursor() as cur:
        cur.execute("UPDATE webhooks SET removed_at = now() WHERE id = %s", (hid,))


def resume_hook(conn, principal, hid):
    r = _own_hook(conn, principal, hid)
    with conn.cursor() as cur:
        cur.execute("UPDATE webhooks SET stopped_at = NULL, failures = 0, next_try_at = NULL "
                    "WHERE id = %s RETURNING *", (r["id"],))
        return hook_json(cur.fetchone())
