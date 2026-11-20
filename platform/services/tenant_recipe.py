"""oaap.core.tenant -- setup recipes for a tenant that already exists (RFC-0109).

A recipe is a short list of the same steps a profile uses (RFC-0055,
RFC-0104), without the steps that create or shape a tenant, run against an
EXISTING tenant. This file is the PURE half, like tenant_build: judging a
recipe, turning it into the profile shape the engine already drives,
choosing the instances it needs, the preview, and the scope a tenant_admin
may act in. Everything that touches the node is appctl's.

Rules that carry the design:

* A recipe never creates or shapes a tenant: no tenant.create, tenant.face,
  tenant.policy, address.*, idp.*.
* It names the APPS it needs; the instances are this tenant's, chosen by
  the person or pre-selected when the tenant has exactly one. A name that
  is not an instance of that app IN THIS TENANT is refused (S2).
* An app is installed only when the recipe says so for that need
  (`install: true`); the recipe itself never carries an app.install step.
* A run is preceded by a preview of the same values; a conflict in the
  preview stops the run before anything happens (D6).
"""
import json

import tenant_build as tb

RECIPE_FORMAT = "oaap.tenant-recipe/1"
TOP_KEYS = ("recipe", "id", "version", "title", "description", "needs",
            "params", "steps")
# what a recipe may do -- the profile's vocabulary minus everything that
# makes or shapes the tenant; app.install comes only from `needs`
STEP_TYPES = ("app.config", "destination.ensure", "app.setup", "manual",
              "backup.check")
PARAM_KINDS = ("text", "word", "bool", "color")
RESERVED = ("label", "tenant", tb.SELECTION_REF)
KEY_SUFFIX = "_key"
NEED_KEYS = ("app", "label", "install", "name")
NEEDS_MAX = 12
TITLE_MAX = 120


class Refusal(tb.Refusal):
    """A rule said no; one sentence for a human."""


# ------------------------------------------------------------ the recipe

def need_names(doc):
    return [n for n in ((doc or {}).get("needs") or {})]


def recipe_problems(doc):
    """Every reason this recipe cannot run, as sentences. [] = fine."""
    if not isinstance(doc, dict):
        return ["a recipe is a JSON object"]
    out = []
    if doc.get("recipe") != RECIPE_FORMAT:
        out.append(f"'recipe' must say {RECIPE_FORMAT}")
    for k in doc:
        if k not in TOP_KEYS:
            out.append(f"'{k}' is not a field of a recipe")
    if not tb.ID_RE.match(str(doc.get("id") or "")):
        out.append("'id' is a short lowercase word")
    if "version" in doc and not tb.VERSION_RE.match(str(doc.get("version") or "")):
        out.append("'version' is x.y.z (e.g. 1.0.0), optionally -pre")
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
        out.append(f"'title' is a text of 1 to {TITLE_MAX} characters")
    if "description" in doc and not isinstance(doc.get("description"), str):
        out.append("'description' is a text")

    needs = doc.get("needs")
    if not isinstance(needs, dict) or not needs or len(needs) > NEEDS_MAX:
        out.append(f"'needs' names 1 to {NEEDS_MAX} apps the recipe works with")
        needs = {}
    params = doc.get("params", {})
    if not isinstance(params, dict):
        out.append("'params' must be an object")
        params = {}
    taken = set()
    for n, spec in needs.items():
        if (not tb.PARAM_RE.match(n) or n in RESERVED or n.endswith(KEY_SUFFIX)):
            out.append(f"need '{n}': the name is not allowed")
        taken.update((n, n + KEY_SUFFIX))
        if not isinstance(spec, dict):
            out.append(f"need '{n}' must be an object")
            continue
        for k in spec:
            if k not in NEED_KEYS:
                out.append(f"need '{n}': '{k}' is not a field of a need")
        if not tb.WORD_RE.match(str(spec.get("app") or "")):
            out.append(f"need '{n}' names an app id ('app')")
        if "label" in spec and not isinstance(spec["label"], str):
            out.append(f"need '{n}': 'label' is a text")
        if "install" in spec and not isinstance(spec["install"], bool):
            out.append(f"need '{n}': 'install' is true or false")
        if "name" in spec and (spec.get("install") is not True
                               or not tb.WORD_RE.match(str(spec["name"]))):
            out.append(f"need '{n}': 'name' is the proposed instance name of an "
                       "app the recipe may install (install: true)")
    for name, spec in params.items():
        if not tb.PARAM_RE.match(name) or name in RESERVED or name in taken:
            out.append(f"parameter '{name}': the name is not allowed (it is "
                       "reserved or a need's)")
        if not isinstance(spec, dict) or spec.get("kind") not in PARAM_KINDS:
            out.append(f"parameter '{name}' needs a kind of "
                       + "/".join(PARAM_KINDS))

    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        return out + ["'steps' must be a non-empty list"]
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            continue                      # said by the profile check below
        typ = st.get("type")
        if typ == "app.install":
            out.append(f"step {i + 1}: a recipe installs an app only through "
                       "its 'needs' (install: true), never in a step")
        elif typ in tb.STEP_TYPES and typ not in STEP_TYPES:
            out.append(f"step {i + 1} ('{st.get('id', '')}'): a recipe works "
                       f"in a tenant that exists; '{typ}' is not one of its "
                       "steps")
    if out:
        return out
    # The rest is the profile's own judgement, on the shape the engine runs.
    return out + tb.profile_problems(as_profile(doc))


def _install_step_id(need):
    return "install-" + need.replace("_", "-")


def as_profile(doc):
    """The recipe in the shape tenant_build drives: the needs and `tenant`
    become parameters, a need with install: true becomes an app.install in
    front. Pure and repeatable, so continue and rollback find the same
    steps again."""
    params = {"label": {"kind": "label", "required": True},
              "tenant": {"kind": "word", "required": True}}
    steps = []
    for n, spec in (doc.get("needs") or {}).items():
        params[n] = {"kind": "word", "required": True}
        params[n + KEY_SUFFIX] = {"kind": "word", "required": True}
        if isinstance(spec, dict) and spec.get("install") is True:
            steps.append({"id": _install_step_id(n), "type": "app.install",
                          "app": spec.get("app"), "name": "{" + n + "}"})
    params.update(doc.get("params") or {})
    return {"profile": tb.PROFILE_FORMAT, "id": doc.get("id"),
            "version": doc.get("version", ""), "title": doc.get("title", ""),
            "params": params, "steps": steps + list(doc.get("steps") or [])}


def read_recipe_file(path):
    """-> (recipe, digest, raw). Raises Refusal with one sentence."""
    try:
        with open(path, "rb") as f:
            raw = f.read(tb.MAX_PROFILE_BYTES + 1)
    except OSError as exc:
        raise Refusal(f"the recipe cannot be read ({type(exc).__name__})")
    if len(raw) > tb.MAX_PROFILE_BYTES:
        raise Refusal(f"the recipe file is larger than "
                      f"{tb.MAX_PROFILE_BYTES // 1024} KB")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refusal("the recipe is not valid JSON")
    problems = recipe_problems(doc)
    if problems:
        raise Refusal("the recipe is refused: " + "; ".join(problems[:5])
                      + (f" (and {len(problems) - 5} more)"
                         if len(problems) > 5 else ""))
    return doc, tb.digest_of(raw), raw


# ------------------------------------------------- the instances (D2, D3)

def resolve_needs(doc, instances, given, planned_key):
    """-> (values, problems, chosen).

    `instances`: [(key, local name, app id)] of THIS tenant only -- the
    caller filters by tenant, and nothing outside the list can be chosen
    (S2). `given`: {need: local name or key}. `planned_key(local)`: the key
    an instance installed under that name would get.

    values: {need: local name, need_key: key}; chosen: {need: {"app",
    "name", "key", "install": bool}} for the preview.
    """
    values, problems, chosen = {}, [], {}
    for n, spec in (doc.get("needs") or {}).items():
        app = spec.get("app")
        mine = [(k, loc) for k, loc, a in instances if a == app]
        g = str(given.get(n) or "").strip()
        if g:
            hit = next(((k, loc) for k, loc in mine if g in (k, loc)), None)
            if not hit:
                problems.append(f"'{g}' ist keine Instanz der App {app} in diesem "
                                "Mandanten" + (f" (es gibt: "
                                               f"{', '.join(loc for _k, loc in mine)})"
                                               if mine else ""))
                continue
        elif len(mine) == 1:
            hit = mine[0]
        elif mine:
            problems.append(f"der Mandant hat mehrere Instanzen der App {app} "
                            f"({', '.join(loc for _k, loc in mine)}) -- wähle eine "
                            f"mit --param {n}=<name>")
            continue
        elif spec.get("install") is True:
            loc = spec.get("name") or n.replace("_", "-")
            clash = [k for k, l2, a in instances if l2 == loc and a != app]
            if clash:
                problems.append(f"die App {app} würde als '{loc}' installiert, "
                                f"aber '{loc}' ist im Mandanten schon eine andere App")
                continue
            values[n], values[n + KEY_SUFFIX] = loc, planned_key(loc)
            chosen[n] = {"app": app, "name": loc, "key": values[n + KEY_SUFFIX],
                         "install": True}
            continue
        else:
            problems.append(f"dieser Mandant hat keine App {app} "
                            f"({spec.get('label') or n}); das Rezept installiert "
                            "sie nicht")
            continue
        values[n], values[n + KEY_SUFFIX] = hit[1], hit[0]
        chosen[n] = {"app": app, "name": hit[1], "key": hit[0], "install": False}
    for n in given:
        if n not in (doc.get("needs") or {}):
            problems.append(f"'{n}' ist keine benötigte App dieses Rezepts")
    return values, problems, chosen


# ------------------------------------------------------ the preview (D6)

def preview(profile, values, drivers, ctx):
    """Every step's check, nothing done -> [{id, type, verdict, note}].

    verdict: present | absent | conflict | skipped | refused (the step
    cannot even be rendered or read). The setup doors answer their own dry
    run inside the check (RFC-0104 D2)."""
    rows = []
    for st in tb.steps_of(profile, values):
        row = {"id": st["id"], "type": st["type"]}
        if not tb.step_active(st, values):
            rows.append(dict(row, verdict="skipped", note="nicht gewünscht"))
            continue
        args, bad = tb._safe(tb.render_step, st, values)
        if args is None:
            rows.append(dict(row, verdict="refused", note=bad))
            continue
        res, bad = tb._safe(drivers.check, st["type"], args, dict(ctx, step=row))
        if res is None:
            rows.append(dict(row, verdict="refused", note=bad))
            continue
        rows.append(dict(row, verdict=res[0], note=tb._sentence(res[1])))
    return rows


def preview_blockers(rows):
    """The rows that stop a run before it starts: a conflict, or a step
    that cannot be rendered or read."""
    return [r for r in rows if r["verdict"] in (tb.CONFLICT, "refused")]


SAY = {tb.PRESENT: "vorhanden", tb.ABSENT: "wird angelegt",
       tb.CONFLICT: "Konflikt", "skipped": "nicht gewünscht",
       "refused": "abgelehnt"}


# ------------------------------------------------- tenant_admin scope (§5)

def tenant_admin_scope_problems(plan, config_is_secret=None):
    """Why a tenant_admin may not run these filled-in steps -> sentences.

    `plan`: [(id, type, args)] AFTER the placeholders are filled (S5: a
    value cannot widen what the recipe does). `config_is_secret(name, key)`
    asks the app's manifest; without it a key is judged by its name."""
    out = []
    for sid, typ, a in plan:
        if typ in ("app.setup", "manual", "backup.check", "app.install"):
            continue
        if typ == "app.config":
            key = str(a.get("key") or "")
            secret = (config_is_secret(a.get("name", ""), key) if config_is_secret
                      else any(w in key.lower() for w in tb.SECRET_WORDS))
            if secret or a.get("generate") is True:
                out.append(f"Schritt '{sid}': {key} ist ein geheimer oder "
                           "erzeugter Wert -- das tut nur der Betreiber")
            continue
        if typ == "destination.ensure":
            if (a.get("auth") or "none") != "none":
                out.append(f"Schritt '{sid}': eine Destination mit Zugangsdaten "
                           "legt nur der Betreiber an")
            continue
        out.append(f"Schritt '{sid}': '{typ}' liegt außerhalb dessen, was ein "
                   "tenant_admin selbst darf")
    return out


# --------------------------------------------------------------- the run

def new_run_state(doc, digest, values, chosen, by, now=None):
    """A run's state: a build's state with kind 'recipe' and id r-…, so the
    engine drives, continues and rolls it back unchanged."""
    profile = as_profile(doc)
    state = tb.new_state(profile, digest, values, by, now=now)
    state["id"] = tb.new_build_id(state["label"], now, prefix="r")
    state["kind"] = "recipe"
    state["recipe"] = doc["id"]
    state["title"] = str(doc.get("title") or "")[:TITLE_MAX]
    state["needs"] = chosen
    return state


def open_run_for(root, label):
    """The unfinished run in this tenant, or None (one at a time)."""
    return tb.open_build_for(root, label)
