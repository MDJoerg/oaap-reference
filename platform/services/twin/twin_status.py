"""Status as a declared transition (RFC-0120 stage 4, D7; oaap.data.twin 0.15 §2.27,
oaap.data.model 0.5 §2.11).

An object type may declare `status: {values, initial?, transitions: [{from, to, by?}]}`
in the registry. A change goes only along a declared transition, is appended to
`statuses` with who, when and why (never updated), and records the event
`status.changed`. The table `states` keeps its relay meaning; this is `status`.

`by` names who may make a transition: `owner` (the object's owner origin),
`writers` (an app or client that may write the type), `persons` (a person who
may write the tenant's groups). Without `by`, all three may.
"""
import json
import re

import twin_write as tw

VALUE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
BY = ("owner", "writers", "persons")
VALUES_MAX = 50
TRANSITIONS_MAX = 200
HISTORY_MAX = 200


def _refuse(msg, code=400):
    raise tw.Refused(msg, code)


def _iso(v):
    return v.isoformat().replace("+00:00", "Z") if v is not None else None


# ------------------------------------------------------------------ the declaration
def check_declaration(st):
    """The declaration, normalised: {values, initial, transitions}. Refused
    with a sentence when its shape is wrong."""
    if not isinstance(st, dict) or set(st) - {"values", "initial", "transitions"}:
        _refuse("'status' is {values, initial?, transitions}")
    vals = st.get("values")
    if not isinstance(vals, list) or not vals or len(vals) > VALUES_MAX \
            or not all(isinstance(v, str) and VALUE_RE.match(v) for v in vals) \
            or len(set(vals)) != len(vals):
        _refuse(f"status 'values' are 1..{VALUES_MAX} different keys [a-z][a-z0-9_]{{0,39}}")
    initial = st.get("initial", vals[0])
    if initial not in vals:
        _refuse(f"status 'initial' is one of its values, not '{initial}'")
    trs = st.get("transitions") or []
    if not isinstance(trs, list) or len(trs) > TRANSITIONS_MAX:
        _refuse(f"status 'transitions' is a list of at most {TRANSITIONS_MAX} {{from, to, by?}}")
    out, seen = [], set()
    for t in trs:
        if not isinstance(t, dict) or set(t) - {"from", "to", "by"}:
            _refuse("every transition is {from, to, by?}")
        f, to = t.get("from"), t.get("to")
        if f not in vals or to not in vals or f == to:
            _refuse(f"transition {f} -> {to}: two different values of the status")
        if (f, to) in seen:
            _refuse(f"transition {f} -> {to} twice")
        seen.add((f, to))
        tr = {"from": f, "to": to}
        if "by" in t:
            by = t["by"]
            if not isinstance(by, list) or not by or any(b not in BY for b in by):
                _refuse(f"transition {f} -> {to}: 'by' is a list of {', '.join(BY)}")
            tr["by"] = sorted(set(by), key=BY.index)
        out.append(tr)
    return {"values": list(vals), "initial": initial, "transitions": out}


def merge_declaration(old, new):
    """(merged, changed) -- `new` added to `old`: values and transitions
    appended, never removed; `initial` and a transition's `by` stay (409)."""
    if old is None:
        return new, True
    old = check_declaration(old)
    if new["initial"] != old["initial"]:
        _refuse(f"status 'initial' is '{old['initial']}' -- it does not change", 409)
    vals = old["values"] + [v for v in new["values"] if v not in old["values"]]
    have = {(t["from"], t["to"]): t for t in old["transitions"]}
    trs = list(old["transitions"])
    for t in new["transitions"]:
        o = have.get((t["from"], t["to"]))
        if o is None:
            trs.append(t)
        elif o.get("by") != t.get("by"):
            _refuse(f"transition {t['from']} -> {t['to']} is declared with by="
                    f"{o.get('by') or 'all'} -- it does not change", 409)
    merged = {"values": vals, "initial": old["initial"], "transitions": trs}
    return merged, merged != old


def declaration(conn, type_key):
    """The type's status declaration, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT definition FROM oaap_model.type_definitions WHERE key = %s",
                    (type_key,))
        r = cur.fetchone()
    if not r:
        return None
    d = r["definition"]
    d = json.loads(d) if isinstance(d, str) else (d or {})
    st = d.get("status")
    if st is None:
        return None
    try:
        return check_declaration(st)
    except tw.Refused as e:
        _refuse(f"the status declared on '{type_key}' is malformed: {e}", 409)


def allowed_from(decl, current, kinds):
    """The values one may go to from `current`, as one of these kinds."""
    return [t["to"] for t in decl["transitions"] if t["from"] == current
            and set(t.get("by") or BY) & set(kinds)]


# ------------------------------------------------------------------ reading
def history(conn, oid, at=None, limit=HISTORY_MAX):
    with conn.cursor() as cur:
        if at is None:
            cur.execute("SELECT * FROM statuses WHERE object_id = %s ORDER BY id DESC LIMIT %s",
                        (oid, limit))
        else:
            cur.execute("SELECT * FROM statuses WHERE object_id = %s AND recorded_at <= %s "
                        "ORDER BY id DESC LIMIT %s", (oid, at, limit))
        return cur.fetchall()


def current(conn, oid, decl, at=None):
    """{value, since, by} -- since/by None while it is the initial one."""
    rows = history(conn, oid, at, 1)
    if not rows:
        return {"value": decl["initial"], "since": None, "by": None}
    r = rows[0]
    return {"value": r["to_status"], "since": _iso(r["recorded_at"]), "by": r["origin"]}


def status_of(conn, oid, type_key, at=None):
    """The object's status, or None when its type declares none."""
    decl = declaration(conn, type_key)
    return current(conn, oid, decl, at) if decl else None


def entry(r):
    return {"from": r["from_status"], "to": r["to_status"], "at": _iso(r["recorded_at"]),
            "by": r["origin"], "recorded_by": r["recorded_by"], "reason": r["reason"]}


# ------------------------------------------------------------------ changing
def change(conn, oid, type_key, owner_origin, to, reason, origin, recorded_by, kinds):
    """Change the status along a declared transition. `kinds` are the
    caller's: a subset of owner, writers, persons. Returns
    {id, from, to, at, written}."""
    decl = declaration(conn, type_key)
    if decl is None:
        _refuse(f"type '{type_key}' declares no status", 409)
    if not isinstance(to, str) or to not in decl["values"]:
        _refuse(f"'{to}' is not a status of '{type_key}' -- it has: "
                + ", ".join(decl["values"]))
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        _refuse("'reason' is a text of at most 500 characters")
    with conn.cursor() as cur:
        cur.execute("SELECT retired_at FROM retirements WHERE object_id = %s LIMIT 1", (oid,))
        if cur.fetchone():
            _refuse("the object is retired -- its status does not change", 409)
        # one change at a time per object: two callers never both step from the same status
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"status:{oid}",))
    cur_st = current(conn, oid, decl)["value"]
    if to == cur_st:
        return {"id": f"urn:oaap:obj:{oid}", "from": cur_st, "to": to, "at": None,
                "written": False}
    tr = next((t for t in decl["transitions"] if t["from"] == cur_st and t["to"] == to), None)
    if tr is None:
        allowed = [t["to"] for t in decl["transitions"] if t["from"] == cur_st]
        _refuse(f"'{cur_st}' -> '{to}' is not a declared transition of '{type_key}' -- from "
                f"'{cur_st}' it goes to: " + (", ".join(allowed) or "nothing"), 409)
    by = tr.get("by") or list(BY)
    if not set(by) & set(kinds):
        _refuse(f"'{cur_st}' -> '{to}' is made by {', '.join(by)} -- not by {origin}", 403)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO events (kind, object_id, group_key, origin) "
                    "VALUES ('status.changed', %s, '', %s) RETURNING id", (oid, origin))
        eid = cur.fetchone()["id"]
        cur.execute("INSERT INTO statuses (object_id, from_status, to_status, reason, origin, "
                    "recorded_by, event_id) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING recorded_at",
                    (oid, cur_st, to, (reason or "").strip() or None, origin, recorded_by, eid))
        at = cur.fetchone()["recorded_at"]
    return {"id": f"urn:oaap:obj:{oid}", "from": cur_st, "to": to, "at": _iso(at),
            "written": True}


def by_event(conn, event_ids):
    """{event id: {from, to, reason}} for status events (the feed, the relay)."""
    ids = [int(i) for i in event_ids]
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT event_id, from_status, to_status, reason FROM statuses "
                    "WHERE event_id = ANY(%s)", (ids,))
        return {int(r["event_id"]): {"from": r["from_status"], "to": r["to_status"],
                                     "reason": r["reason"]} for r in cur.fetchall()}
