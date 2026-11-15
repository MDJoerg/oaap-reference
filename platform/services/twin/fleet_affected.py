"""Affected, not alarmed (RFC-0087 §6, stage 2).

From a broken link -- a tunnel, an external end, an instance -- read the
fleet's relations BACKWARDS and list what depends on it: the destinations
that use it, the instances bound to those, the tunnels that reach those
instances, and on, with the tenant each one belongs to. One broken tunnel
is then one matter that names everything behind it, instead of one alarm
per thing that stopped working.

The walk itself is pure (`affected`, `link_report`); `load_graph` is the one
read from the twin schema both callers share -- the fleet service, which
writes the hub's report every flush, and the twin service's read
`/internal/twin/objects/<id>/affected`, which FleetView will draw.

What the walk deliberately over-approximates: a tunnel that REACHES an
affected instance is listed as affected as a whole, and with it every
destination that uses it -- the twin does not know which offer of a tunnel
a destination calls. "Possibly affected" is the honest reading of such a
row; the `via` of every row says how it was reached.
"""
import time

import fleet_model as fm

# who depends on whom: `source --rel--> target` means source depends on target
BACK = ("usesTunnel", "usesExternal", "behind", "reaches", "bindsTo")
# whose it is: instance --heldBy--> tenant, destination --destOf--> tenant
OWNER = ("heldBy", "destOf")
# the tunnel's two ends, for the matter's text
ENDS = ("servedBy", "dialedBy")
KINDS = {fm.TUNNEL: "tunnel", fm.DESTINATION: "destination", fm.INSTANCE: "instance",
         fm.EXTERNAL: "external", fm.TENANT: "tenant", fm.NODE: "node"}
LIMIT = 200            # a walk stops here and says so; the fleet is far smaller
TUNNEL_DOWN = ("down-inner", "down-outer")
# an external end whose tunnel app says so (RFC-0087 §8.1)
EXTERNAL_DOWN = ("down-tunnel", "down-target")
DOWN = TUNNEL_DOWN + EXTERNAL_DOWN


def short(rel_key):
    """`fleetUsesTunnel` -> `usesTunnel`; a short key stays as it is."""
    if rel_key.startswith("fleet") and len(rel_key) > 5:
        return rel_key[5].lower() + rel_key[6:]
    return rel_key


def _node_of(obj, ends):
    """Where an object lives: an instance `node/inst`, a destination
    `dest:node/name`, an external end `ext:node/name`; a tunnel has two
    ends and no single place."""
    src = obj.get("source") or ""
    for prefix in ("dest:", "ext:"):
        if src.startswith(prefix):
            return src[len(prefix):].split("/", 1)[0]
    if obj.get("type") == fm.INSTANCE and "/" in src:
        return src.split("/", 1)[0]
    return ""


def affected(rels, objects, start, limit=LIMIT):
    """rels: [(source id, rel key, target id)] -- the OPEN relations;
    objects: {id: {"type", "title", "source"}}; start: an object id.

    -> {"start": row, "affected": [row, ...], "tenants": [title, ...],
        "truncated": bool} or None when `start` is no object. A row is
    {"id", "kind", "title", "node", "tenant", "depth", "via", "from"}: `via`
    is the relation that made it depend on `from` (the row before it).
    Breadth first, so the nearest come first; every object once."""
    if start not in objects:
        return None
    deps, owner, ends, behind = {}, {}, {}, {}
    for s, rk, t in rels:
        rk = short(rk)
        if rk == "behind":
            behind.setdefault(s, t)
        if rk in BACK:
            deps.setdefault(t, []).append((s, rk))
        elif rk in OWNER:
            owner.setdefault(s, t)
        elif rk in ENDS:
            ends.setdefault(s, {})[rk] = t

    def title(oid):
        return (objects.get(oid) or {}).get("title") or oid

    def row(oid, depth, via, frm):
        o = objects.get(oid) or {}
        ten = owner.get(oid)
        return {"id": oid, "kind": KINDS.get(o.get("type"), o.get("type") or "?"),
                "title": title(oid), "node": _node_of(o, ends.get(oid)),
                "tenant": title(ten) if ten else "", "depth": depth, "via": via, "from": frm}

    out, seen, tenants = [], {start}, []
    frontier, truncated = [start], False
    depth = 0
    while frontier and not truncated:
        depth += 1
        nxt = []
        for cur in frontier:
            for s, rk in sorted(deps.get(cur, []), key=lambda x: (title(x[0]), x[1])):
                if s in seen:
                    continue
                if len(out) >= limit:
                    truncated = True
                    break
                seen.add(s)
                r = row(s, depth, rk, cur)
                out.append(r)
                nxt.append(s)
            if truncated:
                break
        frontier = nxt
    first = row(start, 0, "", "")
    e = ends.get(start) or {}
    first["outer"] = title(e["servedBy"]) if e.get("servedBy") else ""
    first["inner"] = title(e["dialedBy"]) if e.get("dialedBy") else ""
    # an external end: the tunnel app it lies behind
    first["behind"] = title(behind[start]) if behind.get(start) else ""
    for r in [first] + out:
        if r["tenant"] and r["tenant"] not in tenants:
            tenants.append(r["tenant"])
    return {"start": first, "affected": out, "tenants": tenants, "truncated": truncated}


def link_report(states, rels, objects, previous, now):
    """The hub's report for its minute job (fleet_watch.link_conditions):
    every tunnel whose joined state is down, and every external end whose
    tunnel app says it is down (§8.1), with what depends on it.

    states: {object id: state} -- a tunnel's joined state
    (fleet_twin.joined_state), an external end's `extState`; previous: the
    last report -- a link's `since` is kept while its state does not
    change. -> {"written", "links": {name: {"id", "kind", "state", "since",
    "outer", "inner", "behind", "node", "affected", "tenants",
    "truncated"}}}. A tunnel is named by its title (its id), an external
    end by `<node>/<instance>/<target>` -- its title alone (a target's
    name) may repeat on another node."""
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    prev = (previous or {}).get("links") or {}
    links = {}
    for oid, st in sorted(states.items()):
        if st not in DOWN or oid not in objects:
            continue
        a = affected(rels, objects, oid)
        src = objects[oid].get("source") or ""
        ext = objects[oid].get("type") == fm.EXTERNAL
        name = src[len("ext:"):] if ext and src.startswith("ext:") else a["start"]["title"]
        p = prev.get(name) or {}
        links[name] = {"id": oid, "kind": "external" if ext else "tunnel", "state": st,
                       "since": p["since"] if p.get("state") == st and p.get("since") else iso,
                       "outer": a["start"]["outer"], "inner": a["start"]["inner"],
                       "behind": a["start"]["behind"], "node": a["start"]["node"],
                       "affected": a["affected"], "tenants": a["tenants"],
                       "truncated": a["truncated"]}
    return {"written": iso, "links": links}


def load_graph(conn):
    """(rels, objects) of the fleet from the twin schema `conn` points at:
    the open relations the walk follows, and every fleet object with its
    type, title and source key."""
    keys = [fm.ak(k) for k in BACK + OWNER + ENDS]
    with conn.cursor() as cur:
        cur.execute("SELECT object_id, rel_key, target_id FROM current_relations "
                    "WHERE valid_to IS NULL AND rel_key = ANY(%s)", (keys,))
        rels = [(str(r["object_id"]), r["rel_key"], str(r["target_id"])) for r in cur.fetchall()]
        cur.execute("SELECT o.id, o.type_key, o.title, s.source_key FROM objects o "
                    "LEFT JOIN source_keys s ON s.object_id = o.id AND s.origin = %s "
                    "WHERE o.type_key = ANY(%s)", (fm.ORIGIN, list(KINDS)))
        objects = {str(r["id"]): {"type": r["type_key"], "title": r["title"],
                                  "source": r["source_key"] or ""} for r in cur.fetchall()}
    return rels, objects
