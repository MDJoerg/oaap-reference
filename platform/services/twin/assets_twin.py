"""The tenant reader of an app's `assets` door (RFC-0088 §2, §3) -- pure.

The fleet reader (portal/fleet_links.read_assets_answer) takes exactly
kind, name, state, uses and destination, and the hub sees them as
pseudonyms. This reader takes what only the instance's own tenant may see,
and still only by whitelist (oaap.fleet.status rule 1, applied here too):
a field that is not named below never reaches the twin, whatever the app
sends. Never in any field: a key's value or hash, a credential, a prompt, a
tool's arguments or results, an address. A key is named by its label.

Stage 1 deliberately leaves out the persons (`owner`, `acts_for`, D3: they
come with stage 3 as person objects visible to the tenant_admin only).

Nothing here reads a file, a clock or a database.
"""
import re

import assets_model as am

ITEMS_MAX = 500
MODELS_MAX = 300
TEXT_MAX = 400
# a name: printable, no '/', since it is a part of the source key
_NAME = re.compile(r"^[^\x00-\x1f\x7f/]{1,80}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_LIGHT = re.compile(r"^[a-z]{1,16}$")
# kind -> {relation short name: (answer field, kinds the target may have)}
REL_FIELDS = {
    "model": {"uses": ("uses", ("source",))},
    "key": {"mayUse": ("models", ("model",))},
    "tool": {"toolOf": ("source", ("source",))},
    "signin": {"opens": ("source", ("source",))},
    "agent": {"agentUses": ("uses", ("model", "tool"))},
}
REL_KEYS = ("assetOf", "uses", "mayUse", "toolOf", "opens", "agentUses")
# who depends on whom: `a --rel--> b` means a depends on b
DEPENDS = ("uses", "mayUse", "toolOf", "opens", "agentUses")


def _name(v):
    if not isinstance(v, str):
        return ""
    v = v.strip()
    return v if _NAME.match(v) else ""


def _iso(v):
    return v if isinstance(v, str) and _ISO.match(v) else None


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
        return None
    return v


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _join(items, limit=TEXT_MAX):
    text = ", ".join(str(i) for i in items if i not in (None, ""))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _values(kind, a):
    """The whitelisted attributes of one asset, short names."""
    v = {"name": _name(a.get("name")), "kind": kind}
    if kind in ("source", "model"):
        v["state"] = a.get("state") if a.get("state") in am.STATES else "unknown"
    else:
        v["state"] = "present"
    if kind == "source":
        if isinstance(a.get("light"), str) and _LIGHT.match(a["light"]):
            v["light"] = a["light"]
        chk = a.get("checked")
        if isinstance(chk, dict):
            v["checked"] = _iso(chk.get("at"))
            if isinstance(chk.get("ok"), bool):
                v["checkedOk"] = chk["ok"]
        if isinstance(a.get("models"), list):
            v["modelCount"] = len([m for m in a["models"][:MODELS_MAX] if isinstance(m, str)])
        if _name(a.get("destination")):
            v["destination"] = _name(a["destination"])
    if kind == "model" and isinstance(a.get("upstream"), list):
        ups = [u for u in a["upstream"] if isinstance(u, dict)]
        v["upstream"] = _join(f"{_name(u.get('source'))}: {_name(u.get('model'))}"
                              for u in ups if _name(u.get("source")) and _name(u.get("model")))
    if kind in ("key", "cohort", "signin"):
        v["expires"] = _iso(a.get("expires"))
    if kind == "key":
        v["budget"], v["used"] = _num(a.get("budget")), _num(a.get("used"))
    if kind == "cohort":
        v["seats"] = _int(a.get("seats"))
    return {k: x for k, x in v.items() if x not in (None, "")}


def read_tenant_answer(doc):
    """The app's answer at its `assets` door -> [{"kind", "name", "values",
    "rel": {short relation: [(kind, name)]}}], or None when it is no answer.
    At most ITEMS_MAX assets; a relation keeps only targets of this answer
    with a kind the relation allows; every (kind, name) once."""
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("assets"), list):
        return None
    raw = [a for a in doc["assets"] if isinstance(a, dict)][:ITEMS_MAX]
    items, seen = [], set()
    for a in raw:
        kind, name = a.get("kind"), _name(a.get("name"))
        if kind not in am.KINDS or not name or (kind, name) in seen:
            continue
        seen.add((kind, name))
        items.append((kind, name, a))
    out = []
    for kind, name, a in items:
        rel = {}
        for rk, (field, kinds) in REL_FIELDS.get(kind, {}).items():
            got = a.get(field)
            names = got if isinstance(got, list) else ([got] if isinstance(got, str) else [])
            targets = sorted({(k, _name(n)) for n in names for k in kinds
                              if _name(n) and (k, _name(n)) in seen})
            if targets:
                rel[rk] = targets
        out.append({"kind": kind, "name": name, "values": _values(kind, a), "rel": rel})
    return out


def app_key(instance):
    return instance


def item_key(instance, kind, name):
    """Source key of an asset in the tenant's twin: `<instance>/<kind>/<name>`."""
    return f"{instance}/{kind}/{name}"


def kind_of(source_key):
    parts = (source_key or "").split("/", 2)
    return parts[1] if len(parts) == 3 else ""


# --- the read for the page (RFC-0088 §4/§7: affected per row) ---------------

def affected(rels, objects, start, limit=200):
    """Who depends on `start`, breadth first: a source down -> the model
    names that use it -> the keys allowed to use those -> the agents.
    rels: [(source id, short relation, target id)] -- open relations;
    objects: {id: {"kind", "name", "instance"}}. -> [{"id", "kind", "name",
    "instance", "depth", "via"}], nearest first, every object once."""
    deps = {}
    for s, rk, t in rels:
        if rk in DEPENDS:
            deps.setdefault(t, []).append((s, rk))
    out, seen, frontier, depth = [], {start}, [start], 0
    while frontier and len(out) < limit:
        depth += 1
        nxt = []
        for cur in frontier:
            for s, rk in sorted(deps.get(cur, []),
                                key=lambda x: ((objects.get(x[0]) or {}).get("name", "").lower(), x[1])):
                if s in seen or s not in objects or len(out) >= limit:
                    continue
                seen.add(s)
                o = objects[s]
                out.append({"id": s, "kind": o.get("kind", ""), "name": o.get("name", ""),
                            "instance": o.get("instance", ""), "depth": depth, "via": rk})
                nxt.append(s)
        frontier = nxt
    return out


def universe(objects, rels):
    """The page's data: {"apps": [...], "items": [...]} from the twin rows.
    objects: {id: {"type", "source", "title", "values": {short: value}}};
    rels: [(source id, short relation, target id)]. Removed assets are
    listed last within their kind, with their state; a row's `affected` is
    computed only for what is not removed."""
    by_kind = {k: [] for k in am.KINDS}
    apps, items = [], {}
    for oid, o in objects.items():
        if o.get("type") == am.APP:
            apps.append({"id": oid, "instance": o.get("source", ""),
                         "app": (o.get("values") or {}).get("app", ""),
                         "state": (o.get("values") or {}).get("appState", "")})
        elif o.get("type") == am.ITEM:
            v = dict(o.get("values") or {})
            src = o.get("source", "")
            items[oid] = {"kind": v.get("kind") or kind_of(src),
                          "name": v.get("name") or o.get("title", ""),
                          "instance": src.split("/", 1)[0]}
    out_items = []
    for oid, o in objects.items():
        if o.get("type") != am.ITEM:
            continue
        v = dict(o.get("values") or {})
        it = items[oid]
        row = {"id": oid, **it, "values": v,
               "rel": sorted((rk, items[t]["kind"], items[t]["name"])
                             for s, rk, t in rels if s == oid and t in items)}
        row["affected"] = ([] if v.get("state") == am.REMOVED
                           else affected(rels, items, oid))
        out_items.append(row)
    for row in out_items:
        by_kind.setdefault(row["kind"], []).append(row)
    ordered = []
    for k in list(am.KINDS) + sorted(set(by_kind) - set(am.KINDS)):
        rows = by_kind.get(k) or []
        rows.sort(key=lambda r: (r["values"].get("state") == am.REMOVED, r["instance"], r["name"]))
        ordered += rows
    apps.sort(key=lambda a: a["instance"])
    return {"apps": apps, "items": ordered}
