"""oaap.core.authorization 0.1 -- business authorization (RFC-0045 stages 1+2).

Pure functions over a plain dict, so the three places that need them
(the manifest check in appctl, the identity service that holds the state,
and the tests) cannot disagree about what a declaration or a grant means.
Nothing here reads a file, opens a socket or knows Flask.

The state is one dict:

    {"declarations": {app: {"version", "registered", "declaration"}},
     "roles":        {id: {tenant, app, template, name, values, ...}},
     "collections":  {id: {tenant, name, roles: [role id], ...}},
     "assignments":  {id: {tenant, collection, subject, context,
                           valid_from, valid_to, granted_by, ended, ...}}}

The rule that runs through all of it is RFC-0045 section 1: a business
grant is not a platform role. Nothing in this file mentions one, on
purpose -- there is no code path from here to `server_admin`, or to any
other platform role, to find.
"""

import re
import uuid
from datetime import date, datetime, timezone

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
NAME_RE = re.compile(r"^[^\x00-\x1f]{1,80}$")

FRESH_FOR = 30            # seconds; RFC-0045 A4

CONTEXT = "$context"
VALUE = "$value"

OBJECT_KEYS = {"key", "title", "activities", "fields"}
FIELD_KEYS = {"key", "context", "values"}
TEMPLATE_KEYS = {"key", "title", "grants", "may_grant"}
GRANT_RESERVED = {"object", "activities"}


class AuthzError(ValueError):
    """A refusal with a sentence a human can act on."""


def empty_state():
    return {"declarations": {}, "roles": {}, "collections": {},
            "assignments": {}, "instances": {}, "mappings": {}}


def new_id():
    return uuid.uuid4().hex


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The declaration (manifest section `authorization`)

def _text(x):
    return isinstance(x, str) and bool(x.strip())


def _keys_unique(items, label, errs):
    seen = set()
    for it in items:
        k = it.get("key") if isinstance(it, dict) else None
        if k in seen:
            errs.append(f"{label} '{k}' is declared twice")
        seen.add(k)


def validate_declaration(decl):
    """List of errors; empty when the declaration is sound (spec 2.1)."""
    errs = []
    if not isinstance(decl, dict):
        return ["authorization: an object with 'objects' and "
                "'role_templates' expected"]
    for k in decl:
        if k not in ("objects", "role_templates"):
            errs.append(f"authorization: unknown key '{k}'")
    objects = decl.get("objects", [])
    templates = decl.get("role_templates", [])
    if not isinstance(objects, list):
        return errs + ["authorization.objects: a list expected"]
    if not isinstance(templates, list):
        return errs + ["authorization.role_templates: a list expected"]
    _keys_unique(objects, "object", errs)
    _keys_unique(templates, "role template", errs)
    shape = {}
    for o in objects:
        if not isinstance(o, dict):
            errs.append("authorization.objects: an object expected")
            continue
        k = o.get("key")
        where = f"authorization.objects[{k}]"
        for extra in set(o) - OBJECT_KEYS:
            errs.append(f"{where}: unknown key '{extra}'")
        if not (isinstance(k, str) and KEY_RE.match(k)):
            errs.append(f"{where}: 'key' must match {KEY_RE.pattern}")
            continue
        if not _text(o.get("title")):
            errs.append(f"{where}: 'title' is required")
        acts = o.get("activities")
        if not (isinstance(acts, list) and acts
                and all(isinstance(a, str) and KEY_RE.match(a) for a in acts)):
            errs.append(f"{where}: 'activities' must be a non-empty list of "
                        "keys")
            acts = []
        elif len(set(acts)) != len(acts):
            errs.append(f"{where}: an activity is declared twice")
        fields = o.get("fields", [])
        fshape = {}
        if not isinstance(fields, list):
            errs.append(f"{where}: 'fields' must be a list")
            fields = []
        _keys_unique(fields, f"{where} field", errs)
        for f in fields:
            if not isinstance(f, dict):
                errs.append(f"{where}.fields: an object expected")
                continue
            fk = f.get("key")
            for extra in set(f) - FIELD_KEYS:
                errs.append(f"{where}.fields[{fk}]: unknown key '{extra}'")
            if not (isinstance(fk, str) and KEY_RE.match(fk)):
                errs.append(f"{where}.fields: 'key' must match "
                            f"{KEY_RE.pattern}")
                continue
            has_ctx, has_vals = "context" in f, "values" in f
            if has_ctx == has_vals:
                errs.append(f"{where}.fields[{fk}]: exactly one of "
                            "'context' and 'values'")
                continue
            if has_ctx:
                if not _text(f["context"]):
                    errs.append(f"{where}.fields[{fk}]: 'context' names a "
                                "twin object type")
                fshape[fk] = ("context", None)
            else:
                vals = f["values"]
                if not (isinstance(vals, list) and vals and all(
                        isinstance(v, str) and VALUE_RE.match(v)
                        for v in vals)) or len(set(vals)) != len(vals):
                    errs.append(f"{where}.fields[{fk}]: 'values' must be a "
                                "non-empty list of distinct value ids")
                    vals = []
                fshape[fk] = ("values", tuple(vals))
        shape[k] = (tuple(acts), fshape)
    tkeys = {t.get("key") for t in templates if isinstance(t, dict)}
    for t in templates:
        if not isinstance(t, dict):
            errs.append("authorization.role_templates: an object expected")
            continue
        k = t.get("key")
        where = f"authorization.role_templates[{k}]"
        for extra in set(t) - TEMPLATE_KEYS:
            errs.append(f"{where}: unknown key '{extra}'")
        if not (isinstance(k, str) and KEY_RE.match(k)):
            errs.append(f"{where}: 'key' must match {KEY_RE.pattern}")
            continue
        if not _text(t.get("title")):
            errs.append(f"{where}: 'title' is required")
        grants = t.get("grants")
        if not (isinstance(grants, list) and grants):
            errs.append(f"{where}: 'grants' must be a non-empty list")
            grants = []
        for g in grants:
            if not isinstance(g, dict):
                errs.append(f"{where}.grants: an object expected")
                continue
            ob = g.get("object")
            if ob not in shape:
                errs.append(f"{where}: grants object '{ob}' that is not "
                            "declared")
                continue
            acts, fshape = shape[ob]
            ga = g.get("activities")
            if not (isinstance(ga, list) and ga
                    and all(a in acts for a in ga)):
                errs.append(f"{where}: grant on '{ob}' names activities "
                            f"that are not declared (declared: "
                            f"{', '.join(acts)})")
            for fk, spec in g.items():
                if fk in GRANT_RESERVED:
                    continue
                if fk not in fshape:
                    errs.append(f"{where}: grant on '{ob}' names field "
                                f"'{fk}' that is not declared")
                    continue
                kind, vals = fshape[fk]
                if spec == CONTEXT:
                    if kind != "context":
                        errs.append(f"{where}: '{fk}' is not a context "
                                    "field")
                elif spec == VALUE:
                    if kind != "values":
                        errs.append(f"{where}: '{fk}' has no value list, "
                                    "$value cannot fill it")
                else:
                    want = spec if isinstance(spec, list) else [spec]
                    if kind != "values" or not want or not all(
                            isinstance(w, str) and w in vals for w in want):
                        errs.append(f"{where}: '{fk}' names values that "
                                    "are not declared")
        mg = t.get("may_grant", [])
        if not (isinstance(mg, list) and all(
                isinstance(x, str) and x in tkeys for x in mg)):
            errs.append(f"{where}: 'may_grant' names templates that are "
                        "not declared")
    return errs


# ---------------------------------------------------------------------------
# Comparing two declarations (spec 2.2)

def _index(decl):
    """Everything a declaration promises, as a set of tokens."""
    toks = {}
    for o in (decl or {}).get("objects", []) or []:
        k = o["key"]
        toks[f"object:{k}"] = None
        for a in o.get("activities", []):
            toks[f"activity:{k}.{a}"] = None
        for f in o.get("fields", []) or []:
            src = "context" if "context" in f else "values"
            toks[f"field:{k}.{f['key']}"] = src
            if src == "values":
                for v in f.get("values", []):
                    toks[f"value:{k}.{f['key']}.{v}"] = None
    for t in (decl or {}).get("role_templates", []) or []:
        k = t["key"]
        toks[f"template:{k}"] = None
        for g in t.get("grants", []):
            ob = g["object"]
            for a in g.get("activities", []):
                toks[f"grant:{k}:{ob}.{a}"] = None
            for fk, spec in g.items():
                if fk in GRANT_RESERVED:
                    continue
                toks[f"restriction:{k}:{ob}.{fk}"] = (
                    spec if isinstance(spec, str) else tuple(sorted(spec)))
        for m in t.get("may_grant", []) or []:
            toks[f"may_grant:{k}>{m}"] = None
    return toks


def compare(old, new):
    """(kind, removed, added). kind: new / unchanged / additive / destructive.

    Removed is destructive. A field whose source changed counts as
    removed AND added: a role that stored a value id must not silently
    mean something else afterwards. A restriction that CHANGED counts as
    removed (the old promise is gone) -- narrowing and widening are both
    something a tenant must hear about before the install, because the
    roles built on the template change what they grant.
    """
    if old is None:
        return "new", [], sorted(_index(new))
    a, b = _index(old), _index(new)
    removed = sorted(t for t in a if t not in b)
    added = sorted(t for t in b if t not in a)
    for t in a:
        if t in b and a[t] != b[t] and t.split(":")[0] in (
                "field", "restriction"):
            removed.append(t)
            added.append(t)
    removed, added = sorted(set(removed)), sorted(set(added))
    if removed:
        return "destructive", removed, added
    return ("additive" if added else "unchanged"), [], added


def affected(state, app, removed):
    """Roles (and their assignments) that lose something, per tenant.

    `removed` is the destructive token list of `compare`. A role is hit
    when its template is gone, when a grant its template had is gone, or
    when it stores a value id that is no longer declared.
    """
    gone_t = {t.split(":", 1)[1] for t in removed if t.startswith("template:")}
    hit_t = set(gone_t)
    for t in removed:
        head, _, rest = t.partition(":")
        if head in ("grant", "restriction", "may_grant"):
            hit_t.add(rest.split(":")[0])
    gone_v = {t.split(":", 1)[1] for t in removed if t.startswith("value:")}
    gone_f = {t.split(":", 1)[1] for t in removed if t.startswith("field:")}
    out = {}
    for rid, r in state.get("roles", {}).items():
        if r.get("app") != app:
            continue
        hit = r.get("template") in hit_t
        if not hit and (gone_v or gone_f):
            for fk, vs in (r.get("values") or {}).items():
                if any(k.endswith("." + fk) for k in gone_f):
                    hit = True
                for v in vs:
                    if any(g.endswith(f".{fk}.{v}") for g in gone_v):
                        hit = True
        if not hit:
            continue
        coll = [cid for cid, c in state["collections"].items()
                if rid in c.get("roles", [])]
        n = sum(1 for a in state["assignments"].values()
                if a.get("collection") in coll and not a.get("ended"))
        row = out.setdefault(r["tenant"], {"roles": [], "assignments": 0})
        row["roles"].append(r["name"])
        row["assignments"] += n
    return out


def register(state, app, version, declaration, confirm=False):
    """(kind, removed, impact). Stores the declaration, all or nothing.

    A destructive change is not written unless `confirm` is true; the
    caller gets the impact back to show first (spec 2.2). The same
    declaration again is `unchanged` and writes nothing.
    """
    if not (isinstance(app, str) and app):
        raise AuthzError("an app id is needed")
    errs = validate_declaration(declaration)
    if errs:
        raise AuthzError("the declaration is not sound: " + "; ".join(errs))
    old = (state["declarations"].get(app) or {}).get("declaration")
    kind, removed, _added = compare(old, declaration)
    impact = affected(state, app, removed) if kind == "destructive" else {}
    if kind == "destructive" and not confirm:
        return kind, removed, impact
    if kind in ("new", "additive", "destructive") or (
            kind == "unchanged" and
            state["declarations"].get(app, {}).get("version") != version):
        state["declarations"][app] = {
            "version": str(version or ""), "registered": now_iso(),
            "declaration": declaration}
    return kind, removed, impact


def withdraw(state, app, confirm=False):
    """A package that no longer has the section: everything it had is removed."""
    old = (state["declarations"].get(app) or {}).get("declaration")
    if old is None:
        return "unchanged", [], {}
    removed = sorted(_index(old))
    impact = affected(state, app, removed)
    if not confirm:
        return "destructive", removed, impact
    del state["declarations"][app]
    return "destructive", removed, impact


# ---------------------------------------------------------------------------
# What the tenant builds

def _decl(state, app):
    d = (state["declarations"].get(app) or {}).get("declaration")
    if d is None:
        raise AuthzError(f"the app '{app}' has registered no authorization "
                         "declaration")
    return d


def _template(decl, key):
    for t in decl.get("role_templates", []) or []:
        if t["key"] == key:
            return t
    raise AuthzError(f"the template '{key}' is not declared")


def _object(decl, key):
    for o in decl.get("objects", []) or []:
        if o["key"] == key:
            return o
    return {}


def value_fields(decl, template):
    """{(object, field): declared values} the tenant must fill ($value)."""
    out = {}
    for g in template.get("grants", []):
        for fk, spec in g.items():
            if spec == VALUE:
                fld = next(f for f in _object(decl, g["object"])["fields"]
                           if f["key"] == fk)
                out[(g["object"], fk)] = list(fld["values"])
    return out


def context_fields(decl, template):
    """[(object, field)] the assignment must fill ($context)."""
    return [(g["object"], fk) for g in template.get("grants", [])
            for fk, spec in g.items() if spec == CONTEXT]


def create_role(state, tenant, app, template, name, values=None, who=""):
    decl = _decl(state, app)
    tpl = _template(decl, template)
    if not NAME_RE.match(name or ""):
        raise AuthzError("a role needs a name of up to 80 characters")
    if any(r["tenant"] == tenant and r["name"] == name
           for r in state["roles"].values()):
        raise AuthzError(f"this tenant has a role '{name}' already")
    values = values or {}
    need = {fk: vs for (_o, fk), vs in value_fields(decl, tpl).items()}
    for fk in values:
        if fk not in need:
            raise AuthzError(f"the template '{template}' has no field "
                             f"'{fk}' to fill")
    out = {}
    for fk, declared in need.items():
        got = values.get(fk)
        if not (isinstance(got, list) and got and all(
                isinstance(v, str) and v in declared for v in got)):
            raise AuthzError(f"'{fk}' needs one or more of: "
                             f"{', '.join(declared)} (value ids, not labels)")
        out[fk] = sorted(set(got))
    rid = new_id()
    state["roles"][rid] = {"id": rid, "tenant": tenant, "app": app,
                           "template": template, "name": name, "values": out,
                           "created": now_iso(), "by": who}
    return rid


def create_collection(state, tenant, name, role_ids, who=""):
    if not NAME_RE.match(name or ""):
        raise AuthzError("a collection needs a name of up to 80 characters")
    if not role_ids:
        raise AuthzError("a collection needs at least one role")
    if any(c["tenant"] == tenant and c["name"] == name
           for c in state["collections"].values()):
        raise AuthzError(f"this tenant has a collection '{name}' already")
    for rid in role_ids:
        r = state["roles"].get(rid)
        if not r or r["tenant"] != tenant:
            raise AuthzError("a role of the collection is not known to "
                             "this tenant")
    cid = new_id()
    state["collections"][cid] = {"id": cid, "tenant": tenant, "name": name,
                                 "roles": list(dict.fromkeys(role_ids)),
                                 "created": now_iso(), "by": who}
    return cid


def _day(s, label):
    if s in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(s)).date() if len(str(s)) > 10 \
            else date.fromisoformat(str(s))
    except ValueError:
        raise AuthzError(f"{label} is not a date (YYYY-MM-DD)")


def collection_contexts(state, collection):
    """[(app, template, object, field)] an assignment of it must fill."""
    out = []
    for rid in collection["roles"]:
        r = state["roles"][rid]
        decl = _decl(state, r["app"])
        for ob, fk in context_fields(decl, _template(decl, r["template"])):
            out.append((r["app"], r["template"], ob, fk))
    return out


def assign(state, tenant, collection_id, subject, context=None,
           valid_from=None, valid_to=None, granted_by="", source="", via=""):
    """Create an assignment. `granted_by` is whoever the caller authenticated
    as -- the caller of THIS function passes it, never the request body."""
    c = state["collections"].get(collection_id)
    if not c or c["tenant"] != tenant:
        raise AuthzError("this tenant has no such collection")
    if not (isinstance(subject, str) and subject.strip()):
        raise AuthzError("an assignment names a user id")
    need = collection_contexts(state, c)
    context = context or {}
    names = {fk for (_a, _t, _o, fk) in need}
    for k in context:
        if k not in names:
            raise AuthzError(f"the collection has no context field '{k}'")
    ctx = {}
    for fk in sorted(names):
        v = context.get(fk)
        vs = v if isinstance(v, list) else ([v] if v else [])
        if not vs or not all(isinstance(x, str) and CONTEXT_RE.match(x)
                             for x in vs):
            raise AuthzError(f"the context '{fk}' is needed: the id of a "
                             "twin object")
        ctx[fk] = sorted(set(vs))
    vf, vt = _day(valid_from, "valid_from"), _day(valid_to, "valid_to")
    if vf and vt and vt < vf:
        raise AuthzError("valid_to is before valid_from")
    aid = new_id()
    state["assignments"][aid] = {
        "id": aid, "tenant": tenant, "collection": collection_id,
        "subject": subject.strip(), "context": ctx,
        "valid_from": vf.isoformat() if vf else "",
        "valid_to": vt.isoformat() if vt else "",
        "granted_by": granted_by, "granted_at": now_iso(), "ended": "",
        "source": source, "via": via}
    return aid


def revoke(state, tenant, assignment_id, who=""):
    a = state["assignments"].get(assignment_id)
    if not a or a["tenant"] != tenant:
        raise AuthzError("this tenant has no such assignment")
    if a["ended"]:
        return False
    a["ended"] = now_iso()
    a["ended_by"] = who
    return True


# ---------------------------------------------------------------------------
# What the app asks for

def _live(a, today):
    if a.get("ended"):
        return False
    vf, vt = a.get("valid_from"), a.get("valid_to")
    if vf and today < date.fromisoformat(vf):
        return False
    if vt and today > date.fromisoformat(vt):
        return False
    return True


def effective(state, tenant, app, user, today=None):
    """The grants `user` holds NOW in `app`, resolved (spec 2.4).

    Only this app's own objects. A person with no live assignment gets an
    empty list; a caller that wants "no such person" asks identity first
    (the route does).
    """
    today = today or datetime.now(timezone.utc).date()
    grants = []
    decl = (state["declarations"].get(app) or {}).get("declaration")
    if decl is None:
        return grants
    for a in state["assignments"].values():
        if a["tenant"] != tenant or a["subject"] != user \
                or not _live(a, today):
            continue
        coll = state["collections"].get(a["collection"])
        if not coll:
            continue
        for rid in coll["roles"]:
            r = state["roles"].get(rid)
            if not r or r["app"] != app or r["tenant"] != tenant:
                continue
            try:
                tpl = _template(decl, r["template"])
            except AuthzError:
                continue
            for g in tpl.get("grants", []):
                fields = {}
                for fk, spec in g.items():
                    if fk in GRANT_RESERVED:
                        continue
                    if spec == CONTEXT:
                        fields[fk] = list(a["context"].get(fk, []))
                    elif spec == VALUE:
                        fields[fk] = list(r["values"].get(fk, []))
                    else:
                        fields[fk] = list(spec) if isinstance(spec, list) \
                            else [spec]
                row = {"object": g["object"],
                       "activities": list(g["activities"]),
                       "fields": fields}
                if a.get("valid_to"):
                    row["valid_to"] = a["valid_to"]
                grants.append(row)
    return grants


def may(grants, permission, **fields):
    """Whether resolved grants allow `object.activity` for these field values.

    Fails closed in both directions:

    * a field the CALLER names must be allowed by the grant -- asking for
      context mB must not pass because the person is trainer of mC;
    * a field the GRANT restricts must be NAMED by the caller -- asking
      "may he edit the line-up?" without saying which team must not pass
      because he may edit one. Use `may_any` when "any context" is meant.

    A field absent from a grant means unrestricted.
    """
    ob, _, act = (permission or "").partition(".")
    for g in grants or ():
        if g.get("object") != ob or act not in g.get("activities", ()):
            continue
        ok = True
        for fk, have in (g.get("fields") or {}).items():
            if fk not in fields or fields[fk] not in have:
                ok = False
                break
        if ok:
            for fk, want in fields.items():
                have = (g.get("fields") or {}).get(fk)
                if have is not None and want not in have:
                    ok = False
                    break
        if ok:
            return True
    return False


def may_any(grants, permission):
    """Whether the person may do this in SOME context (for a menu, not a door)."""
    ob, _, act = (permission or "").partition(".")
    return any(g.get("object") == ob and act in g.get("activities", ())
               and all(len(v) > 0 for v in (g.get("fields") or {}).values())
               for g in grants or ())


# ---------------------------------------------------------------------------
# The provider's groups (RFC-0045 section 5, stage 3)
#
# The provider says WHO a person is, OAAP says what they may do. A group
# of the tenant's realm can stand for a role collection through a mapping
# the tenant writes; a group nobody mapped grants nothing. Evaluated at
# EVERY login, so that leaving the group takes the right away at the next
# sign-in -- weaker than "the next request", and said so wherever it is
# shown.
#
# What a group can never be: a platform role (nothing in this file knows
# one), a context (a group says "is a trainer", not "of team mB" -- so a
# collection that needs a context cannot be mapped), a way to hand rights
# on (a collection with `may_grant` cannot be mapped either: a delegation
# chain must not start in somebody else's console).

GROUP_PATH_RE = re.compile(r"^[^\x00-\x1f/][^\x00-\x1f]{0,127}$")


def group_path(raw):
    """The form a group is compared in: the realm's path without the
    leading slash, exactly as `idp.realm_groups` hands it over."""
    return str(raw or "").strip().lstrip("/")


def collection_blockers(state, collection):
    """Why a collection cannot be given by a group, or ''."""
    need = collection_contexts(state, collection)
    if need:
        return ("it needs a context ("
                + ", ".join(sorted({fk for (_a, _t, _o, fk) in need}))
                + "): a group says 'is a trainer', not 'of which team'")
    for rid in collection["roles"]:
        r = state["roles"][rid]
        tpl = _template(_decl(state, r["app"]), r["template"])
        if tpl.get("may_grant"):
            return (f"the role '{r['name']}' may hand rights on "
                    "(may_grant): a delegation chain must not start in the "
                    "provider's console")
    return ""


def add_mapping(state, tenant, group, collection_id, who=""):
    grp = group_path(group)
    if not GROUP_PATH_RE.match(grp):
        raise AuthzError("a group is named by its path in the realm, "
                         "e.g. 'Verein/Hallenwart'")
    c = state["collections"].get(collection_id)
    if not c or c["tenant"] != tenant:
        raise AuthzError("this tenant has no such collection")
    why = collection_blockers(state, c)
    if why:
        raise AuthzError(f"the collection '{c['name']}' cannot be given by "
                         f"a group: {why}")
    if any(m["tenant"] == tenant and m["group"] == grp
           and m["collection"] == collection_id
           for m in state["mappings"].values()):
        raise AuthzError("this group already gives this collection")
    mid = new_id()
    state["mappings"][mid] = {"id": mid, "tenant": tenant, "group": grp,
                              "collection": collection_id,
                              "created": now_iso(), "by": who}
    return mid


def remove_mapping(state, tenant, mapping_id, who=""):
    """Remove a mapping AND end what it gave, at once.

    The people it reached keep nothing: a rule that is gone must not leave
    its results standing until each person happens to sign in again.
    Returns the number of assignments ended.
    """
    m = state["mappings"].get(mapping_id)
    if not m or m["tenant"] != tenant:
        raise AuthzError("this tenant has no such mapping")
    del state["mappings"][mapping_id]
    n = 0
    for a in state["assignments"].values():
        if (a["tenant"] == tenant and a.get("source") == "idp"
                and a.get("via") == m["group"]
                and a["collection"] == m["collection"] and not a["ended"]):
            a["ended"] = now_iso()
            a["ended_by"] = "mapping removed" + (f" by {who}" if who else "")
            n += 1
    return n


def sync_login(state, tenant, user_id, groups):
    """Bring a person's provider-given assignments in line with the groups
    the provider asserts NOW. (added, ended) as lists of (collection, group).

    Touches only assignments this function made (`source == 'idp'`); a
    right somebody gave by hand stays as it is. A collection that has meanwhile
    become unmappable is ended, not given. No `groups` claim at all (a client
    without the mapper) is "no groups": the rights drop, they do not stay.
    """
    held = {group_path(g) for g in groups or ()}
    wanted = {}
    for m in state["mappings"].values():
        if m["tenant"] != tenant or m["group"] not in held:
            continue
        c = state["collections"].get(m["collection"])
        if not c or c["tenant"] != tenant or collection_blockers(state, c):
            continue
        wanted.setdefault(m["collection"], m["group"])
    live = {a["collection"]: a for a in state["assignments"].values()
            if a["tenant"] == tenant and a["subject"] == user_id
            and a.get("source") == "idp" and not a["ended"]}
    added, ended = [], []
    for cid, grp in sorted(wanted.items()):
        if cid not in live:
            assign(state, tenant, cid, user_id, {}, granted_by="idp:" + grp,
                   source="idp", via=grp)
            added.append((cid, grp))
    for cid, a in sorted(live.items()):
        if cid not in wanted:
            a["ended"] = now_iso()
            a["ended_by"] = "login: group no longer asserted"
            ended.append((cid, a.get("via", "")))
    return added, ended
