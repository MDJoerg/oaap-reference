"""Reads of the door /twin/v1 (RFC-0120 stage 1, oaap.data.twin 0.11 §2.18).

What a program outside the platform needs to look at a tenant's twin: filtered
lists with a cursor, one object, the neighbourhood of an object, the shortest
paths between two objects, an export of nodes and edges (JSON, GraphML) and a
change feed. Everything here only READS, and everything answers as the twin
looked at one moment:

- **`at`** is a date-time (or a date, meaning the end of that day, UTC). Rows
  count that were *recorded* by then and not yet superseded by then (the
  twin's recorded time, RFC-0031 §3.4), and whose *validity* contains that
  day. Without `at` the moment is now -- so a relation the fleet ended
  (`valid_to` set) is gone, as it should be for a viewer.
- **Visibility:** only objects of the types the caller is bound to (in either
  direction, the rule of every twin read). An edge appears only when both its
  ends are visible -- an id of a type the caller may not read never leaks
  through a relation.
- **Merges:** an id merged away answers as its canonical object, as of `at`.

Graph walks run in Python over ONE load of the edges among the visible
objects (D5: in PostgreSQL, no second store; no recursive SQL yet, measured
with the fleet twin -- revisit if it gets slow). Every answer is bounded and
says so (`truncated`).
"""
import base64
import datetime
import json
import re
from collections import deque

UTC = datetime.timezone.utc
OBJ_ID_RE = re.compile(r"^(?:urn:oaap:obj:)?([0-9a-fA-F-]{36})$")
GROUP_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

LIST_DEFAULT = 100
LIST_MAX = 500
SCAN_BATCH = 500
SCAN_MAX = 20000          # rows one filtered page looks at before it answers
GRAPH_DEPTH_MAX = 5
GRAPH_NODES_MAX = 2000
PATH_LEN_MAX = 6
PATHS_MAX = 10
EXPORT_NODES_MAX = 20000
CHANGES_DEFAULT = 200
CHANGES_MAX = 1000
# an event becomes part of the feed only this long after it was recorded: a
# transaction that took a lower id but commits later must not be skipped
FEED_LAG_S = 5
OPS = (">=", "<=", "!=", "=", "<", ">")


class Bad(Exception):
    """A request the caller can fix -- 400 with this sentence."""


# ------------------------------------------------------------------ inputs
def parse_at(raw):
    """(moment, day) -- `moment` an aware datetime, `day` the date validity is
    checked on. None for now. A date alone means the end of that day."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            d = datetime.date.fromisoformat(raw)
            return datetime.datetime.combine(d, datetime.time(23, 59, 59, 999999), UTC)
        t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise Bad("'at' is a date (2026-11-22) or a date-time (2026-11-22T10:00:00Z)")
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def now():
    return datetime.datetime.now(UTC)


def obj_id(raw):
    m = OBJ_ID_RE.match((raw or "").strip())
    if not m:
        raise Bad(f"'{raw}' is not an object id (urn:oaap:obj:<uuid>)")
    return m.group(1).lower()


def urn(i):
    return f"urn:oaap:obj:{i}"


def int_arg(raw, default, lo, hi, name):
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except ValueError:
        raise Bad(f"'{name}' is a number")
    return max(lo, min(hi, v))


def csv_arg(raw):
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def encode_cursor(title, oid):
    return base64.urlsafe_b64encode(json.dumps([title, oid]).encode()).decode().rstrip("=")


def decode_cursor(raw):
    try:
        pad = "=" * (-len(raw) % 4)
        title, oid = json.loads(base64.urlsafe_b64decode(raw + pad))
        assert isinstance(title, str) and OBJ_ID_RE.match(oid)
        return title, oid
    except Exception:
        raise Bad("'cursor' is not one this door gave out -- start without it")


# ---------------------------------------------------------------- registry
def _defn(d):
    if isinstance(d, str):
        try:
            return json.loads(d)
        except ValueError:
            return {}
    return d or {}


def registry(conn, tenant_id):
    """What the reads need from the model registry: attribute value types,
    personal attributes (D10), group definitions, object types' semantic ids
    -- for the tenant's active types and every attribute/group type."""
    with conn.cursor() as cur:
        cur.execute("SELECT key, kind, definition FROM oaap_model.type_definitions "
                    "WHERE kind IN ('attribute_types', 'group_types', 'object_types', "
                    "'relation_types')")
        rows = cur.fetchall()
    reg = {"attribute": {}, "group": {}, "object": {}, "relation": {}}
    for r in rows:
        d = _defn(r["definition"])
        reg[r["kind"].split("_")[0]][r["key"]] = d
    return reg


# ----------------------------------------------------------------- periods
def _period_rows(conn, ids, moment, as_of):
    """Period rows of these member ids as recorded at `moment` (`as_of`
    False: the current rows), withdrawn ones left out."""
    p = {"ids": list(ids), "t": moment}
    cols = ("r.object_id, r.group_key, r.attr_key, r.value, r.valid_from, r.valid_to, "
            "r.unit, r.source, r.quality, r.recorded_at, r.recorded_by")
    if not as_of:
        sql = (f"SELECT {cols} FROM current_periods r WHERE r.object_id = ANY(%(ids)s::uuid[]) "
               "AND r.value <> 'null'::jsonb")
    else:
        sql = (f"SELECT {cols} FROM periods r LEFT JOIN periods s ON s.id = r.superseded_by "
               "WHERE r.object_id = ANY(%(ids)s::uuid[]) AND r.recorded_at <= %(t)s "
               "AND (s.id IS NULL OR s.recorded_at > %(t)s) AND r.value <> 'null'::jsonb")
    with conn.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchall()


def periods_at(conn, ids, moment, as_of):
    """{(member id, group, attr): row} -- the value of each periodic
    attribute at `moment` (oaap.data.twin 0.12 §2.20): the period that
    contains it; if none does, the latest period that ended before it (the
    last known value -- its bounds go with it, so nobody mistakes 2025 for
    now). A period that begins later does not count."""
    best = {}
    for r in _period_rows(conn, ids, moment, as_of):
        if r["valid_from"] > moment:
            continue
        k = (str(r["object_id"]), r["group_key"], r["attr_key"])
        inside = r["valid_to"] > moment
        cur = best.get(k)
        if cur is None:
            best[k] = (inside, r)
        elif inside and not cur[0]:
            best[k] = (inside, r)
        elif inside == cur[0] and r["valid_to"] > cur[1]["valid_to"]:
            best[k] = (inside, r)
    return {k: v[1] for k, v in best.items()}


def period_meta(r):
    out = {"valid_from": _iso(r["valid_from"]), "valid_to": _iso(r["valid_to"])}
    for k in ("unit", "source", "quality"):
        if r.get(k):
            out[k] = r[k]
    return out


def _iso(t):
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z") if t else None


def list_periods(conn, mem, moment, as_of, group=None, attr=None, frm=None, to=None):
    """Every period of these objects ({member id: canonical id}), oldest
    first, as recorded at `moment`; `group`/`attr` narrow it, `frm`/`to`
    keep the periods that overlap that window."""
    out = []
    for r in _period_rows(conn, mem, moment, as_of):
        if group and r["group_key"] != group:
            continue
        if attr and r["attr_key"] != attr:
            continue
        if frm and r["valid_to"] <= frm:
            continue
        if to and r["valid_from"] >= to:
            continue
        row = {"object": urn(mem[str(r["object_id"])]), "group": r["group_key"],
               "attribute": r["attr_key"], "value": r["value"], **period_meta(r),
               "recorded_at": _iso(r["recorded_at"]), "recorded_by": r["recorded_by"]}
        out.append(row)
    out.sort(key=lambda x: (x["group"], x["attribute"], x["valid_from"], x["valid_to"]))
    return out


# ---------------------------------------------------------------- snapshot
class Snapshot:
    """The visible part of one tenant's twin at one moment: objects of the
    bound types, the alias map, and (loaded on demand) the edges."""

    def __init__(self, conn, types, at=None):
        self.conn = conn
        self.types = sorted(types)
        self.at = at
        self.moment = at or now()
        self.day = self.moment.date()
        self._edges = None
        with conn.cursor() as cur:
            if at is None:
                cur.execute("SELECT alias_id, canonical_id FROM aliases WHERE unmerged_at IS NULL")
            else:
                cur.execute("SELECT alias_id, canonical_id FROM aliases WHERE merged_at <= %s "
                            "AND (unmerged_at IS NULL OR unmerged_at > %s)", (at, at))
            self.alias = {str(r["alias_id"]): str(r["canonical_id"]) for r in cur.fetchall()}
            cur.execute("SELECT id, type_key, title, owner_origin FROM objects "
                        "WHERE type_key = ANY(%s) AND recorded_at <= %s",
                        (self.types, self.moment))
            self.objects = {}
            for r in cur.fetchall():
                i = str(r["id"])
                if i in self.alias:
                    continue
                self.objects[i] = {"type": r["type_key"], "title": r["title"],
                                   "owner": r["owner_origin"]}

    def canonical(self, i):
        seen = set()
        while i in self.alias and i not in seen:
            seen.add(i)
            i = self.alias[i]
        return i

    def members(self, ids):
        """{member id: canonical id} for these canonical ids and every id
        merged into them (as of the moment)."""
        want = set(ids)
        out = {i: i for i in want}
        for a in self.alias:
            c = self.canonical(a)
            if c in want:
                out[a] = c
        return out

    def _valid(self, alias="r"):
        return (f" AND ({alias}.valid_from IS NULL OR {alias}.valid_from <= %(day)s)"
                f" AND ({alias}.valid_to IS NULL OR {alias}.valid_to > %(day)s)")

    def _rows(self, table, cols, ids):
        """Rows of attributes/relations of these member ids as of the moment."""
        p = {"ids": list(ids), "day": self.day, "t": self.moment}
        if self.at is None:
            sql = (f"SELECT {cols} FROM current_{table} r WHERE r.object_id = ANY(%(ids)s::uuid[])"
                   + self._valid())
        else:
            sql = (f"SELECT {cols} FROM {table} r LEFT JOIN {table} s ON s.id = r.superseded_by "
                   "WHERE r.object_id = ANY(%(ids)s::uuid[]) AND r.recorded_at <= %(t)s "
                   "AND (s.id IS NULL OR s.recorded_at > %(t)s)" + self._valid())
        with self.conn.cursor() as cur:
            cur.execute(sql, p)
            return cur.fetchall()

    def groups(self, ids, only=None, leave_out=()):
        """{canonical id: {group: {origin, attributes: {k: v}, relations: [...]}}}
        -- relations only to visible objects. `only` limits the groups,
        `leave_out` drops attribute keys (personal ones in an export)."""
        mem = self.members(ids)
        out = {i: {} for i in ids}
        if not ids:
            return out
        with self.conn.cursor() as cur:
            cur.execute("SELECT object_id, group_key, origin FROM groups "
                        "WHERE object_id = ANY(%s::uuid[]) AND created_at <= %s",
                        (list(mem), self.moment))
            grows = cur.fetchall()
        slot = {}
        taken = {}
        for g in sorted(grows, key=lambda g: (str(g["object_id"]) != mem[str(g["object_id"])],
                                              str(g["object_id"]))):
            m, k = str(g["object_id"]), g["group_key"]
            if only is not None and k not in only:
                continue
            c = mem[m]
            disp = k if (c, k) not in taken else f"{k}+{m[:8]}"
            taken[(c, k)] = True
            out[c][disp] = {"origin": g["origin"], "attributes": {}, "relations": []}
            slot[(m, k)] = (c, disp)
        for r in self._rows("attributes", "r.object_id, r.group_key, r.attr_key, r.value",
                            mem):
            s = slot.get((str(r["object_id"]), r["group_key"]))
            if s and r["attr_key"] not in leave_out:
                out[s[0]][s[1]]["attributes"][r["attr_key"]] = r["value"]
        for (m, k, a), r in periods_at(self.conn, mem, self.moment, self.at is not None).items():
            s = slot.get((m, k))
            if s and a not in leave_out:
                g = out[s[0]][s[1]]
                g["attributes"][a] = r["value"]
                g.setdefault("periods", {})[a] = period_meta(r)
        for r in self._rows("relations", "r.object_id, r.group_key, r.rel_key, r.target_id",
                            mem):
            s = slot.get((str(r["object_id"]), r["group_key"]))
            t = self.canonical(str(r["target_id"]))
            if s and t in self.objects:
                out[s[0]][s[1]]["relations"].append({"key": r["rel_key"], "target": urn(t)})
        return out

    def edges(self):
        """[(source, key, target, group)] among the visible objects."""
        if self._edges is None:
            mem = self.members(self.objects)
            seen = set()
            out = []
            for r in self._rows("relations",
                                "r.object_id, r.group_key, r.rel_key, r.target_id", mem):
                s = mem.get(str(r["object_id"]))
                t = self.canonical(str(r["target_id"]))
                if s is None or t not in self.objects or s == t:
                    continue
                e = (s, r["rel_key"], t, r["group_key"])
                if e not in seen:
                    seen.add(e)
                    out.append(e)
            out.sort()
            self._edges = out
        return self._edges

    def node(self, i):
        o = self.objects[i]
        return {"id": urn(i), "type": o["type"], "title": o["title"]}

    def obj(self, i, groups):
        o = self.objects[i]
        return {"id": urn(i), "type": o["type"], "title": o["title"], "owner": o["owner"],
                "groups": groups}

    def stamp(self):
        return self.moment.isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------- where
def parse_where(raw, reg):
    """(group, attr, op, value-or-values, value_type) of one `where=` term:
    `<group>.<attr><op><value>` with op = != < <= > >=, or
    `<group>.<attr> in <v1>,<v2>` or `<group>.<attr> has`. Checked against the
    registry: the attribute must exist, belong to the group if the group says
    which attributes it has, and the value must fit the attribute's type."""
    raw = raw.strip()
    m = re.match(r"^(\S+)\s+(in|has)(?:\s+(.*))?$", raw)
    if m:
        path, op, val = m.group(1), m.group(2), (m.group(3) or "")
        if op == "has" and val:
            raise Bad(f"'{raw}': 'has' takes no value")
        if op == "in" and not val:
            raise Bad(f"'{raw}': 'in' needs values, separated by commas")
    else:
        mm = re.match(r"^([A-Za-z0-9.]+?)(>=|<=|!=|=|<|>)(.*)$", raw)
        if not mm:
            raise Bad(f"'{raw}' is not a filter -- write <group>.<attribute><op><value> "
                      "(op = != < <= > >=), '<group>.<attribute> in a,b' or "
                      "'<group>.<attribute> has'")
        path, op, val = mm.group(1), mm.group(2), mm.group(3)
    group, _, attr = path.rpartition(".")
    if not GROUP_RE.match(group or "") or not KEY_RE.match(attr or ""):
        raise Bad(f"'{path}' is not <group>.<attribute> (a group key has a dot: oaap.software)")
    adef = reg["attribute"].get(attr)
    if adef is None:
        raise Bad(f"attribute '{attr}' is not registered on this node")
    gdef = reg["group"].get(group)
    if gdef and gdef.get("attributes") and attr not in gdef["attributes"]:
        raise Bad(f"group '{group}' has no attribute '{attr}' -- it has: "
                  + ", ".join(gdef["attributes"]))
    vt = adef.get("value_type") or "text"
    if op == "has":
        return group, attr, op, None, vt
    vals = [v.strip() for v in val.split(",")] if op == "in" else [val]
    conv = [_typed(v, vt, raw) for v in vals]
    if vt == "bool" and op not in ("=", "!=", "in"):
        raise Bad(f"'{raw}': a yes/no attribute compares only with = and !=")
    return group, attr, op, conv if op == "in" else conv[0], vt


def _typed(v, vt, raw):
    try:
        if vt in ("int", "decimal"):
            return float(v)
        if vt == "bool":
            if v.lower() not in ("true", "false"):
                raise ValueError
            return v.lower() == "true"
        if vt == "date":
            return datetime.date.fromisoformat(v).isoformat()
        if vt == "datetime":
            datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return v
    except ValueError:
        raise Bad(f"'{raw}': '{v}' is not a value of type {vt}")
    return v


def _matches(groups, term):
    group, attr, op, want, vt = term
    have = None
    for k, g in groups.items():
        if k == group or k.startswith(group + "+"):
            if attr in g["attributes"]:
                have = g["attributes"][attr]
                break
    if op == "has":
        return have is not None
    if have is None:
        return False
    try:
        if vt in ("int", "decimal"):
            have = float(have)
        elif vt == "bool":
            have = have if isinstance(have, bool) else str(have).lower() == "true"
        else:
            have = str(have)
    except (TypeError, ValueError):
        return False
    if op == "in":
        return have in want
    return {"=": have == want, "!=": have != want, "<": have < want,
            "<=": have <= want, ">": have > want, ">=": have >= want}[op]


# ------------------------------------------------------------------- reads
def list_objects(snap, type_key, where, limit, cursor, fields):
    """One page of objects of `type_key`, filtered, ordered by (title, id).
    Returns (objects, next_cursor)."""
    rows = sorted(((o["title"], i) for i, o in snap.objects.items() if o["type"] == type_key))
    if cursor:
        rows = [r for r in rows if r > cursor]
    out = []
    scanned = 0
    pos = 0
    only = None if fields is None else set(fields) | {t[0] for t in where}

    def shown(g):
        if fields is None:
            return g
        return {k: v for k, v in g.items() if k.split("+")[0] in fields}

    while pos < len(rows) and len(out) < limit + 1 and scanned < SCAN_MAX:
        batch = rows[pos:pos + (SCAN_BATCH if where else limit + 1 - len(out))]
        pos += len(batch)
        scanned += len(batch)
        gs = snap.groups([i for _t, i in batch], only=only)
        for title, i in batch:
            if all(_matches(gs[i], t) for t in where):
                out.append((title, i, shown(gs[i])))
                if len(out) == limit + 1:
                    break
    if len(out) > limit:
        last = out[limit - 1]
        return [snap.obj(i, g) for _t, i, g in out[:limit]], encode_cursor(last[0], last[1])
    if pos < len(rows):            # the scan stopped before the end: go on from there
        t, i = rows[pos - 1]
        return [snap.obj(i, g) for _t, i, g in out], encode_cursor(t, i)
    return [snap.obj(i, g) for _t, i, g in out], None


def _adjacency(snap, relations, direction):
    adj = {}
    for s, k, t, g in snap.edges():
        if relations and k not in relations:
            continue
        if direction in ("out", "both"):
            adj.setdefault(s, []).append((t, (s, k, t, g)))
        if direction in ("in", "both"):
            adj.setdefault(t, []).append((s, (s, k, t, g)))
    return adj


def neighbourhood(snap, start, depth, relations, direction, types):
    """Nodes and edges within `depth` hops of `start`; `types` limits which
    nodes the walk enters (the start always counts)."""
    adj = _adjacency(snap, relations, direction)
    seen = {start: 0}
    q = deque([start])
    edges = set()
    truncated = False
    while q:
        cur = q.popleft()
        if seen[cur] >= depth:
            continue
        for nxt, e in adj.get(cur, ()):
            if types and snap.objects[nxt]["type"] not in types:
                continue
            if nxt not in seen:
                if len(seen) >= GRAPH_NODES_MAX:
                    truncated = True
                    continue
                seen[nxt] = seen[cur] + 1
                q.append(nxt)
            edges.add(e)
    nodes = [dict(snap.node(i), depth=d) for i, d in sorted(seen.items(), key=lambda x: (x[1], x[0]))]
    return nodes, [edge_json(e) for e in sorted(edges)], truncated


def paths(snap, a, b, relations, direction, limit, max_len):
    """Up to `limit` shortest paths from a to b, each at most `max_len` hops."""
    adj = _adjacency(snap, relations, direction)
    dist = {a: 0}
    parents = {a: []}
    q = deque([a])
    while q:
        cur = q.popleft()
        if cur == b or dist[cur] >= max_len:
            continue
        for nxt, e in adj.get(cur, ()):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                parents[nxt] = [(cur, e)]
                q.append(nxt)
            elif dist[nxt] == dist[cur] + 1:
                parents[nxt].append((cur, e))
    if b not in dist:
        return []
    out = []

    def walk(node, tail_nodes, tail_edges):
        if len(out) >= limit:
            return
        if node == a:
            out.append({"length": len(tail_edges), "nodes": [urn(n) for n in [a] + tail_nodes],
                        "edges": [edge_json(e) for e in tail_edges]})
            return
        for p, e in parents[node]:
            walk(p, [node] + tail_nodes, [e] + tail_edges)

    walk(b, [], [])
    return out


def edge_json(e):
    s, k, t, g = e
    return {"source": urn(s), "key": k, "target": urn(t), "group": g}


def export(snap, types, relations, with_attributes, reg):
    """(nodes, edges, truncated) of the chosen types -- attributes marked
    personal (`personal: true` on the attribute type) left out (D10)."""
    ids = sorted(i for i, o in snap.objects.items() if not types or o["type"] in types)
    truncated = len(ids) > EXPORT_NODES_MAX
    ids = ids[:EXPORT_NODES_MAX]
    keep = set(ids)
    personal = {k for k, d in reg["attribute"].items() if d.get("personal")}
    nodes = []
    if with_attributes:
        gs = {}
        for n in range(0, len(ids), 2000):
            gs.update(snap.groups(ids[n:n + 2000], leave_out=personal))
        for i in ids:
            node = snap.obj(i, {k: dict({"origin": g["origin"], "attributes": g["attributes"]},
                                        **({"periods": g["periods"]} if g.get("periods") else {}))
                                for k, g in gs[i].items()})
            nodes.append(node)
    else:
        nodes = [dict(snap.node(i), owner=snap.objects[i]["owner"]) for i in ids]
    edges = [edge_json(e) for e in snap.edges()
             if e[0] in keep and e[2] in keep and (not relations or e[1] in relations)]
    return nodes, edges, truncated


def graphml(snap, nodes, edges, reg):
    """GraphML 1.0 of an export: node data type, title, owner and every
    attribute as `<group>.<attr>` (a string); edge data key and group."""
    from xml.sax.saxutils import escape, quoteattr
    keys = {}
    for n in nodes:
        for g, gd in (n.get("groups") or {}).items():
            for a in gd["attributes"]:
                keys.setdefault(f"{g}.{a}", f"a{len(keys)}")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '  <key id="type" for="node" attr.name="type" attr.type="string"/>',
           '  <key id="title" for="node" attr.name="title" attr.type="string"/>',
           '  <key id="owner" for="node" attr.name="owner" attr.type="string"/>',
           '  <key id="semantic" for="node" attr.name="semantic_iri" attr.type="string"/>',
           '  <key id="rel" for="edge" attr.name="key" attr.type="string"/>',
           '  <key id="group" for="edge" attr.name="group" attr.type="string"/>']
    for name, kid in keys.items():
        out.append(f'  <key id="{kid}" for="node" attr.name={quoteattr(name)} attr.type="string"/>')
    out.append(f'  <graph id="twin" edgedefault="directed"><desc>{escape(snap.stamp())}</desc>')
    import semantic
    for n in nodes:
        out.append(f'    <node id={quoteattr(n["id"])}>')
        out.append(f'      <data key="type">{escape(n["type"])}</data>')
        out.append(f'      <data key="title">{escape(n["title"])}</data>')
        if n.get("owner"):
            out.append(f'      <data key="owner">{escape(n["owner"])}</data>')
        sid = (reg["object"].get(n["type"]) or {}).get("semantic_id")
        if sid:
            out.append(f'      <data key="semantic">{escape(semantic.expand(sid))}</data>')
        for g, gd in (n.get("groups") or {}).items():
            for a, v in gd["attributes"].items():
                txt = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                out.append(f'      <data key="{keys[f"{g}.{a}"]}">{escape(txt)}</data>')
        out.append('    </node>')
    for k, e in enumerate(edges):
        out.append(f'    <edge id="e{k}" source={quoteattr(e["source"])} '
                   f'target={quoteattr(e["target"])}>')
        out.append(f'      <data key="rel">{escape(e["key"])}</data>')
        out.append(f'      <data key="group">{escape(e["group"])}</data>')
        out.append('    </edge>')
    out.append('  </graph>')
    out.append('</graphml>')
    return "\n".join(out) + "\n"


def changes(conn, types, since, limit):
    """(changes, next, more) -- events of objects of these types after the
    event id `since`, in order. `since` = 'now' starts at the latest event."""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) AS m FROM events "
                    "WHERE recorded_at < now() - %s * interval '1 second'", (FEED_LAG_S,))
        settled = int(cur.fetchone()["m"])
        if since == "now":
            return [], settled, False
        try:
            since = max(0, int(since or 0))
        except ValueError:
            raise Bad("'since' is the 'next' of the last answer, 0, or 'now'")
        cur.execute("SELECT alias_id, canonical_id FROM aliases WHERE unmerged_at IS NULL")
        alias = {str(r["alias_id"]): str(r["canonical_id"]) for r in cur.fetchall()}
        cur.execute("SELECT e.id, e.recorded_at, e.kind, e.object_id, e.group_key, e.origin, "
                    "o.type_key FROM events e JOIN objects o ON o.id = e.object_id "
                    "WHERE e.id > %s AND e.id <= %s AND o.type_key = ANY(%s) "
                    "ORDER BY e.id LIMIT %s", (since, settled, sorted(types), limit + 1))
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    out = []
    for r in rows:
        oid = str(r["object_id"])
        seen = set()
        while oid in alias and oid not in seen:
            seen.add(oid)
            oid = alias[oid]
        c = {"id": int(r["id"]), "at": r["recorded_at"].isoformat().replace("+00:00", "Z"),
             "kind": r["kind"], "object": urn(oid), "type": r["type_key"],
             "group": r["group_key"] or None, "origin": r["origin"]}
        if oid != str(r["object_id"]):
            c["merged_from"] = urn(str(r["object_id"]))
        out.append(c)
    nxt = out[-1]["id"] if more else max(since, settled)
    return out, nxt, more
