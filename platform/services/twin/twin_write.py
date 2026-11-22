"""The twin's write rules, in one place (oaap.data.twin §2.5/§2.7).

Used by the API (app.py, for an app's instance credential) and by the
platform's own writer, the fleet service (fleet.py, origin
`platform:fleet`, RFC-0085 amendment A2). Two doors, one rule: a group
belongs to the origin that created it, a value repeated unchanged writes
nothing, every write appends one `events` row. What differs between the
doors is only WHO is allowed to call -- bindings for an app, the
platform's own role for the fleet -- and that check stays at the door.

`conn` is a psycopg2 connection on the tenant's own schema with a dict
cursor (app.get_conn); nothing here commits -- the caller's `with conn:`
does, so one call is one transaction.
"""
import json
import logging
import re
import uuid

import twin_values as tv

log = logging.getLogger("twin.write")

OBJ_ID_RE = re.compile(r"^(?:urn:oaap:obj:)?([0-9a-fA-F-]{36})$")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


class Refused(Exception):
    """A write the rules forbid; `code` is the HTTP answer the API gives."""

    def __init__(self, msg, code=403):
        super().__init__(msg)
        self.code = code


def canonical_of(conn, obj_id):
    """The id everyone should use instead, if 'obj_id' was merged away
    -- or 'obj_id' itself, unchanged, for the overwhelming majority of
    objects that were never part of a merge. D3: "a merge keeps both
    platform IDs resolvable forever; one becomes canonical.\""""
    with conn.cursor() as cur:
        cur.execute("SELECT canonical_id FROM aliases WHERE alias_id = %s "
                    "AND unmerged_at IS NULL", (obj_id,))
        row = cur.fetchone()
    return str(row["canonical_id"]) if row else obj_id


def record_event(conn, kind, object_id, group_key, origin):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (kind, object_id, group_key, origin) "
            "VALUES (%s, %s, %s, %s)", (kind, object_id, group_key, origin))


def _same(a, b):
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def checked_attributes(conn, origin, attrs):
    """The attributes of one write, checked against the registry BEFORE
    anything is written (RFC-0120 stage 2, oaap.data.twin 0.12 §2.19): a value
    of the wrong type and a periodic attribute (written only as a period,
    §2.20) refuse the whole write -- for an app or a person. The platform's
    own writers (origin 'platform:...') are not refused for one bad value:
    that value is left out and logged, so one odd reading of one node never
    stops the fleet's twin."""
    if not isinstance(attrs, dict):
        raise Refused("'attributes' is an object {key: value}", 400)
    keys = [k for k in attrs if isinstance(k, str) and KEY_RE.match(k)]
    defs = tv.attribute_types(conn, keys)
    lenient = origin.startswith("platform:")
    out = {}
    for key in keys:
        try:
            if tv.periodic(defs.get(key)):
                raise tv.Wrong(f"attribute '{key}' has periods -- write it with "
                               "POST .../groups/<group>/periods (oaap.data.twin §2.20)")
            out[key] = tv.check(key, defs.get(key), attrs[key])
        except tv.Wrong as e:
            if not lenient:
                raise Refused(str(e), 400)
            log.warning("%s: %s -- left out", origin, e)
    return out


def write_group_content(conn, obj_id, group_key, origin, recorded_by, payload):
    """Append the attribute/relation/activity rows a group PUT (or the
    creating POST) carries. Superseding only compares to the CURRENT
    row of the same slot -- a value repeated unchanged writes nothing,
    keeping the history honest (RFC-0031 §3.4). Returns how many rows
    were appended.

    Relations too since 0.5: before, an unchanged relation appended a new
    row on every PUT, against §2.7's own sentence -- harmless for an app
    that writes once, a row a minute for the fleet (found 2026-11-14)."""
    n = 0
    attrs = checked_attributes(conn, origin, payload.get("attributes") or {})
    with conn.cursor() as cur:
        for key, value in attrs.items():
            cur.execute(
                "SELECT id, value FROM current_attributes "
                "WHERE object_id = %s AND group_key = %s AND attr_key = %s",
                (obj_id, group_key, key))
            current = cur.fetchone()
            if current and _same(current["value"], value):
                continue
            cur.execute(
                "INSERT INTO attributes (object_id, group_key, attr_key, "
                "value, valid_from, valid_to, recorded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (obj_id, group_key, key, json.dumps(value),
                 payload.get("valid_from"), payload.get("valid_to"), recorded_by))
            new_id = cur.fetchone()["id"]
            n += 1
            if current:
                cur.execute("UPDATE attributes SET superseded_by = %s WHERE id = %s",
                            (new_id, current["id"]))
        for rel in (payload.get("relations") or []):
            rel_key = (rel.get("key") or "").strip()
            target = (rel.get("target") or "").strip()
            m = OBJ_ID_RE.match(target)
            if not rel_key or not m or not KEY_RE.match(rel_key):
                continue
            target_id = canonical_of(conn, m.group(1))
            cur.execute(
                "SELECT id, valid_from, valid_to FROM current_relations WHERE object_id = %s "
                "AND group_key = %s AND rel_key = %s AND target_id = %s",
                (obj_id, group_key, rel_key, target_id))
            current = cur.fetchone()
            if current and _same(current["valid_from"], rel.get("valid_from")) \
                    and _same(current["valid_to"], rel.get("valid_to")):
                continue
            cur.execute(
                "INSERT INTO relations (object_id, group_key, rel_key, "
                "target_id, valid_from, valid_to, recorded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (obj_id, group_key, rel_key, target_id,
                 rel.get("valid_from"), rel.get("valid_to"), recorded_by))
            new_id = cur.fetchone()["id"]
            n += 1
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
                 canonical_of(conn, m.group(1)) if m else None,
                 act.get("status", "open"), act.get("planned_start"),
                 act.get("planned_end"), act.get("started_at"),
                 act.get("finished_at"), recorded_by))
            n += 1
    return n


def write_group(conn, obj_id, group_key, origin, recorded_by, payload):
    """Write into one group of an existing object: create the group (the
    first writer becomes its origin) or append to it -- refused when the
    group belongs to another origin (RFC-0031 §3.3). One `events` row
    only when something was actually written."""
    obj_id = canonical_of(conn, obj_id)
    with conn.cursor() as cur:
        cur.execute("SELECT origin FROM groups WHERE object_id = %s "
                    "AND group_key = %s", (obj_id, group_key))
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                "INSERT INTO groups (object_id, group_key, origin, "
                "created_by) VALUES (%s, %s, %s, %s)",
                (obj_id, group_key, origin, recorded_by))
        elif existing["origin"] != origin:
            raise Refused(f"group '{group_key}' belongs to '{existing['origin']}' "
                          f"-- '{origin}' may not write it")
    n = write_group_content(conn, obj_id, group_key, origin, recorded_by, payload)
    if n or existing is None:
        record_event(conn, "group.written", obj_id, group_key, origin)
    return n


def own_group(conn, obj_id, group_key, origin, recorded_by):
    """True if the group was created now (the first writer becomes its
    origin), False if it exists and is this origin's -- Refused otherwise."""
    with conn.cursor() as cur:
        cur.execute("SELECT origin FROM groups WHERE object_id = %s "
                    "AND group_key = %s", (obj_id, group_key))
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                "INSERT INTO groups (object_id, group_key, origin, "
                "created_by) VALUES (%s, %s, %s, %s)",
                (obj_id, group_key, origin, recorded_by))
            return True
    if existing["origin"] != origin:
        raise Refused(f"group '{group_key}' belongs to '{existing['origin']}' "
                      f"-- '{origin}' may not write it")
    return False


def write_period(conn, obj_id, group_key, origin, recorded_by, body, event_origin=None):
    """One period of a periodic attribute (oaap.data.twin 0.12 §2.20): a value
    valid from `valid_from` to `valid_to` (exclusive), with unit, source and
    quality. Periods of one attribute COEXIST; they must not overlap, except
    that the same bounds again supersede the period written before (a
    correction). `value: null` withdraws a period. Append-only like
    everything else. Returns {"written": bool, "period": {...}}."""
    try:
        attr, value, frm, to, unit, source, quality = tv.period_body(body)
        adef = tv.attribute_types(conn, [attr]).get(attr)
        if adef is None:
            raise tv.Wrong(f"attribute '{attr}' is not registered on this node")
        if not tv.periodic(adef):
            raise tv.Wrong(f"attribute '{attr}' has no periods -- write it with the group "
                           "(PUT .../groups/<group>); a type declares periodic attributes "
                           "('periodic': true)")
        value = tv.check(attr, adef, value)
    except tv.Wrong as e:
        raise Refused(str(e), 400)
    with conn.cursor() as cur:
        cur.execute("SELECT definition FROM oaap_model.type_definitions WHERE key = %s "
                    "AND kind = 'group_types'", (group_key,))
        g = cur.fetchone()
    gdef = tv.defn(g["definition"]) if g else {}
    if gdef.get("attributes") and attr not in gdef["attributes"]:
        raise Refused(f"group '{group_key}' has no attribute '{attr}' -- it has: "
                      + ", ".join(gdef["attributes"]), 400)
    obj_id = canonical_of(conn, obj_id)
    with conn.cursor() as cur:
        # one writer at a time per attribute of an object: the overlap check
        # and the insert must see the same periods
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"period:{obj_id}:{group_key}:{attr}",))
    own_group(conn, obj_id, group_key, origin, recorded_by)
    unit = unit if unit is not None else (adef.get("unit") or None)
    with conn.cursor() as cur:
        cur.execute("SELECT id, value, valid_from, valid_to, unit, source, quality "
                    "FROM current_periods WHERE object_id = %s AND group_key = %s "
                    "AND attr_key = %s AND valid_from < %s AND valid_to > %s",
                    (obj_id, group_key, attr, to, frm))
        rows = cur.fetchall()
        same = next((r for r in rows if r["valid_from"] == frm and r["valid_to"] == to), None)
        clash = [r for r in rows if r is not same and r["value"] is not None]
        if clash:
            c = clash[0]
            raise Refused(f"'{attr}' already has a period {tv.iso(c['valid_from'])} .. "
                          f"{tv.iso(c['valid_to'])} that overlaps -- periods of one attribute "
                          "must not overlap (the same bounds again correct a period)", 409)
        if value is None and (same is None or same["value"] is None):
            raise Refused(f"'{attr}' has no period {tv.iso(frm)} .. {tv.iso(to)} to withdraw",
                          404)
        row = {"value": value, "valid_from": frm, "valid_to": to, "unit": unit,
               "source": source, "quality": quality}
        if same is not None and all(_same(same[k], row[k]) for k in
                                    ("value", "unit", "source", "quality")):
            return {"written": False, "period": tv.period_json(row, with_meta=False)}
        cur.execute(
            "INSERT INTO periods (object_id, group_key, attr_key, value, valid_from, valid_to, "
            "unit, source, quality, recorded_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id", (obj_id, group_key, attr, json.dumps(value), frm, to, unit, source,
                             quality, recorded_by))
        new_id = cur.fetchone()["id"]
        if same is not None:
            cur.execute("UPDATE periods SET superseded_by = %s WHERE id = %s",
                        (new_id, same["id"]))
    record_event(conn, "period.written", obj_id, group_key, event_origin or origin)
    return {"written": True, "period": tv.period_json(row, with_meta=False)}


def find_by_source_key(conn, origin, source_key):
    """The canonical id of the object this origin knows as `source_key`."""
    with conn.cursor() as cur:
        cur.execute("SELECT object_id FROM source_keys WHERE origin = %s AND "
                    "source_key = %s ORDER BY recorded_at LIMIT 1", (origin, source_key))
        row = cur.fetchone()
    return canonical_of(conn, str(row["object_id"])) if row else None


def ensure_object(conn, type_key, source_key, title, origin, recorded_by, core_group):
    """(id, created) of the object this origin knows as `source_key`;
    created with `core_group` as the owner's core group when none exists
    (the same rows POST /twin/objects writes)."""
    found = find_by_source_key(conn, origin, source_key)
    if found:
        return found, False
    obj_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO objects (id, type_key, title, owner_origin, recorded_by) "
            "VALUES (%s, %s, %s, %s, %s)", (obj_id, type_key, title, origin, recorded_by))
        cur.execute(
            "INSERT INTO source_keys (object_id, origin, source_key, recorded_by) "
            "VALUES (%s, %s, %s, %s)", (obj_id, origin, source_key, recorded_by))
        cur.execute(
            "INSERT INTO groups (object_id, group_key, origin, created_by) "
            "VALUES (%s, %s, %s, %s)", (obj_id, core_group, origin, recorded_by))
    record_event(conn, "object.created", obj_id, core_group, origin)
    return obj_id, True
