"""oaap.core.tenant -- setup recipes for a tenant that already exists (RFC-0109).

A recipe is a short list of the same steps a profile uses (RFC-0055,
RFC-0104), without the steps that create or shape a tenant, run against an
EXISTING tenant. This file is the PURE half, like tenant_build: judging a
recipe, turning it into the profile shape the engine already drives,
choosing the instances it needs, the preview, and the scope a tenant_admin
may act in. Everything that touches the node is appctl's.

Rules that carry the design:

* A recipe never creates or shapes a tenant: no tenant.create, tenant.face,
  tenant.policy, address.*, idp.provision, idp.admin_group. Stage 3
  (section 12): `idp.claim` works INSIDE the realm the tenant already has
  (an attribute only administrators edit, a client scope), and
  `destination.bind` binds with "acts as the person" after the chain test.
* It names the APPS it needs; the instances are this tenant's, chosen by
  the person or pre-selected when the tenant has exactly one. A name that
  is not an instance of that app IN THIS TENANT is refused (S2).
* An app is installed only when the recipe says so for that need
  (`install: true`); the recipe itself never carries an app.install step.
* A run is preceded by a preview of the same values; a conflict in the
  preview stops the run before anything happens (D6).
* Stage 2 (RFC-0109 sections 4, 5): a value can be CHOSEN from what an app
  has (`setup_item`, read through the app's setup door), a light can follow
  the chosen item (`light`), and the operator RELEASES a recipe for a
  tenant (`recipe:<id>@<major>` in the tenant's `tenant_admin_may`), after
  which that tenant's tenant_admin runs it -- own tenant, filled steps
  inside the scope of section 5.
"""
import json
import re

import tenant_build as tb

RECIPE_FORMAT = "oaap.tenant-recipe/1"
TOP_KEYS = ("recipe", "id", "version", "title", "description", "needs",
            "params", "steps")
# what a recipe may do -- the profile's vocabulary minus everything that
# makes or shapes the tenant; app.install comes only from `needs`
STEP_TYPES = ("app.config", "destination.ensure", "app.setup", "manual",
              "backup.check", "idp.claim", "destination.bind")
PARAM_KINDS = ("text", "word", "bool", "color", "setup_item", "light")
LIGHTS = ("green", "yellow", "red")
LIGHT_RANK = {"green": 0, "yellow": 1, "red": 2}
LIGHT_DE = {"green": "grün", "yellow": "gelb", "red": "rot"}
SETUP_ITEM_MAX = 128
# a release in the tenant record: the recipe's id and its MAJOR version --
# a new major version needs a new release (section 5)
RELEASE_RE = re.compile(r"^recipe:([a-z0-9][a-z0-9-]{0,62})@([0-9]{1,4})$")
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
            continue
        if spec["kind"] == "setup_item":
            if spec.get("need") not in needs:
                out.append(f"parameter '{name}': 'need' names one of the recipe's "
                           "needs (the app whose setup door lists the choices)")
            if not tb.WORD_RE.match(str(spec.get("item_kind") or "")):
                out.append(f"parameter '{name}': 'item_kind' is the kind the setup "
                           "door lists (e.g. alias)")
        elif spec["kind"] == "light":
            src = spec.get("from")
            if src is not None and (src not in params or not isinstance(params[src], dict)
                                    or params[src].get("kind") != "setup_item"):
                out.append(f"parameter '{name}': 'from' names a parameter of kind "
                           "setup_item")
            if "default" in spec and spec["default"] not in LIGHTS:
                out.append(f"parameter '{name}': the default is green, yellow or red")
        elif "need" in spec or "item_kind" in spec or "from" in spec:
            out.append(f"parameter '{name}': 'need', 'item_kind' and 'from' belong "
                       "to the kinds setup_item and light")

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
    for name, spec in (doc.get("params") or {}).items():
        params[name] = _engine_param(spec)
    return {"profile": tb.PROFILE_FORMAT, "id": doc.get("id"),
            "version": doc.get("version", ""), "title": doc.get("title", ""),
            "params": params, "steps": steps + list(doc.get("steps") or [])}


def _engine_param(spec):
    """A recipe parameter as the engine judges it. A chosen item is a text
    (an id of the app, judged against the app's own list before the run);
    a light is a word, judged against the three lights there too."""
    if not isinstance(spec, dict) or spec.get("kind") not in ("setup_item", "light"):
        return spec
    out = {k: v for k, v in spec.items() if k in ("label", "default", "required")}
    if spec["kind"] == "setup_item":
        out.update(kind="text", max=SETUP_ITEM_MAX)
    else:
        out["kind"] = "word"
    return out


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


# ------------------------------------------ values from an app (section 4)

def item_params(doc):
    """[(name, spec)] of the parameters chosen from an app's setup door."""
    return [(n, sp) for n, sp in ((doc or {}).get("params") or {}).items()
            if isinstance(sp, dict) and sp.get("kind") == "setup_item"]


def judge_choices(doc, given, lists):
    """-> (values, problems, choices). Fills and judges the parameters of
    kind setup_item and light (RFC-0109 section 4); the rest passes.

    `lists`: {param: [items] or a sentence why the door gave no list}. An
    item is {"id", "title", "light"?, "light_min"?} as the door says it.

    * setup_item: a given id must be in the list; nothing given -> the
      default when it is in the list (or the only item), else the person
      must choose. When the door gave no list (an older app), the given
      value or the default stands as in stage 1, and the choice says so.
    * light with `from`: nothing given -> the chosen item's light (the
      worst it can reach, the honest ceiling). A ceiling stricter than the
      best the item can reach is refused: the chat could never use it.
    """
    values, problems, choices = dict(given or {}), [], {}
    params = (doc or {}).get("params") or {}
    picked = {}
    for name, spec in item_params(doc):
        got = lists.get(name)
        val = str(values.get(name) or "").strip()
        ch = {"need": spec.get("need"), "item_kind": spec.get("item_kind"),
              "label": spec.get("label") or name, "items": [], "note": ""}
        if not isinstance(got, list):
            ch["note"] = str(got or "keine Liste")
            val = val or str(spec.get("default") or "")
            if val:
                values[name] = val
            choices[name] = dict(ch, value=val)
            continue
        items = [i for i in got if isinstance(i, dict) and isinstance(i.get("id"), str)]
        ch["items"] = [{k: i[k] for k in ("id", "title", "light", "light_min") if k in i}
                       for i in items]
        ids = [i["id"] for i in items]
        if val and val not in ids:
            problems.append(f"'{val}' gibt es bei {spec.get('need')} nicht als "
                            f"{spec.get('item_kind')}"
                            + (f" (es gibt: {', '.join(ids[:12])})" if ids else
                               " (dort gibt es noch keinen)"))
            val = ""
        elif not val:
            dft = str(spec.get("default") or "")
            if dft in ids:
                val = dft
            elif len(ids) == 1:
                val = ids[0]
            else:
                problems.append(f"bitte wählen: {spec.get('label') or name}"
                                + ("" if ids else f" -- bei {spec.get('need')} gibt es "
                                   f"noch keinen {spec.get('item_kind')}"))
        if val:
            values[name] = val
            picked[name] = next(i for i in items if i["id"] == val)
        else:
            values.pop(name, None)
        choices[name] = dict(ch, value=val)
    for name, spec in params.items():
        if not isinstance(spec, dict) or spec.get("kind") != "light":
            continue
        item = picked.get(spec.get("from") or "")
        val = str(values.get(name) or "").strip()
        if not val:
            val = (item or {}).get("light") or spec.get("default") or ""
        ch = {"light": True, "from": spec.get("from") or "",
              "label": spec.get("label") or name,
              "suggested": (item or {}).get("light") or ""}
        if val not in LIGHTS:
            problems.append(f"{spec.get('label') or name}: green, yellow oder red")
            values.pop(name, None)
            choices[name] = dict(ch, value="")
            continue
        floor = (item or {}).get("light_min") or (item or {}).get("light")
        if floor in LIGHTS and LIGHT_RANK[val] < LIGHT_RANK[floor]:
            problems.append(
                f"{spec.get('label') or name}: {LIGHT_DE[val]} ist strenger als alles, "
                f"was '{values.get(spec.get('from'))}' erreichen kann ({LIGHT_DE[floor]}) "
                "-- der Chat könnte es nie benutzen")
        values[name] = val
        choices[name] = dict(ch, value=val)
    return values, problems, choices


# ------------------------------------------------- releases (section 5)

def major_of(version):
    m = re.match(r"^([0-9]{1,4})\.", str(version or ""))
    return int(m.group(1)) if m else 0


def release_word(rid, version_or_major):
    major = (version_or_major if isinstance(version_or_major, int)
             else major_of(version_or_major))
    return f"recipe:{rid}@{major}"


def released(may, rid, version):
    """Is this recipe, in this major version, released in `may` (the
    tenant's `tenant_admin_may` list)?"""
    return release_word(rid, version) in (may or [])


def releases_of(may, rid=None):
    """[(id, major)] of the recipe releases in a `tenant_admin_may` list."""
    out = []
    for w in may or []:
        m = RELEASE_RE.match(str(w))
        if m and (rid is None or m.group(1) == rid):
            out.append((m.group(1), int(m.group(2))))
    return out


def filled_plan(profile, values):
    """[(id, type, args)] of the active steps AFTER the placeholders are
    filled -- what the tenant_admin scope is judged on (S5)."""
    out = []
    for st in tb.steps_of(profile, values):
        if not tb.step_active(st, values):
            continue
        out.append((st["id"], st["type"], tb.render_step(st, values)))
    return out


# ------------------------------------------------------ the preview (D6)

def preview(profile, values, drivers, ctx):
    """Every step's check, nothing done -> [{id, type, verdict, note}].

    verdict: present | absent | conflict | skipped | refused (the step
    cannot even be rendered or read). The setup doors answer their own dry
    run inside the check (RFC-0104 D2).

    Stage 3 (measured on oaap-test, 2026-11-20): behind a manual step that
    still waits for a human, what the apps say NOW about a later step may
    depend on that human (a catalog only a person can read, a server made
    two steps earlier). A conflict or refusal there is `later` -- shown
    with its sentence, not a blocker; the run checks the step again before
    it does it, and stops there."""
    rows = []
    waits_for = ""
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
            rows.append(dict(row, verdict="later" if waits_for else "refused",
                             note=(f"erst nach '{waits_for}' prüfbar: " if waits_for else "")
                             + str(bad)))
            continue
        verdict, note = res[0], tb._sentence(res[1])
        if waits_for and verdict == tb.CONFLICT:
            verdict, note = "later", f"erst nach '{waits_for}' prüfbar: {note}"
        rows.append(dict(row, verdict=verdict, note=note))
        if st["type"] == "manual" and verdict != tb.PRESENT and not waits_for:
            waits_for = st["id"]
    return rows


def preview_blockers(rows):
    """The rows that stop a run before it starts: a conflict, or a step
    that cannot be rendered or read."""
    return [r for r in rows if r["verdict"] in (tb.CONFLICT, "refused")]


SAY = {tb.PRESENT: "vorhanden", tb.ABSENT: "wird angelegt",
       tb.CONFLICT: "Konflikt", "skipped": "nicht gewünscht",
       "refused": "abgelehnt", "later": "erst danach prüfbar"}


# ------------------------------------------------- tenant_admin scope (§5)

def tenant_admin_scope_problems(plan, config_is_secret=None):
    """Why a tenant_admin may not run these filled-in steps -> sentences.

    `plan`: [(id, type, args)] AFTER the placeholders are filled (S5: a
    value cannot widen what the recipe does). `config_is_secret(name, key)`
    asks the app's manifest; without it a key is judged by its name."""
    out = []
    for sid, typ, a in plan:
        # stage 3 (section 12): the operator's release of a recipe is the
        # moment he says "this tenant may connect its own SAP system" -- an
        # attribute in its own realm, a bind in its own tenant
        if typ in ("app.setup", "manual", "backup.check", "app.install",
                   "idp.claim", "destination.bind"):
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
            # principal propagation carries no secret (the issuing key never
            # leaves the node) and is pinned to what a human compared
            if (a.get("auth") or "none") not in ("none", "principal_propagation"):
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


# ------------------------------------------------------- recipe.apply (D9)
# RFC-0109 D9 (stage 3): a profile step `recipe.apply` is made of the same
# pieces a tenant_admin can repeat later. It is EXPANDED when the profile is
# read -- before the profile's own checks -- into the recipe's steps, with
# ids prefixed by the step's id, the needs named as instances of the build,
# `{tenant}` as the build's `{label}`, and the recipe's parameters from
# `with` (or their defaults). The build keeps the digest of every recipe it
# expanded, so continue and rollback expand the same file again.

PAIR_RE = re.compile(r"^\s*([a-z][a-z0-9_]{0,30})\s*=\s*(.*?)\s*$")
_REF = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _pairs(text, what):
    out, bad = {}, []
    for part in [x for x in str(text or "").split(",") if x.strip()]:
        m = PAIR_RE.match(part)
        if not m:
            bad.append(f"{what}: '{part.strip()}' is not name=value")
            continue
        out[m.group(1)] = m.group(2)
    return out, bad


def _subst(value, mapping, missing):
    if isinstance(value, str):
        def one(m):
            if m.group(1) in mapping:
                return mapping[m.group(1)]
            missing.add(m.group(1))
            return m.group(0)
        return _REF.sub(one, value)
    if isinstance(value, list):
        return [_subst(v, mapping, missing) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, mapping, missing) for k, v in value.items()}
    return value


def expand_apply(profile, load):
    """-> (profile with every recipe.apply replaced, {recipe id: digest},
    problems). `load(rid)` -> (recipe doc, digest) or raises Refusal."""
    steps = (profile or {}).get("steps")
    if not isinstance(steps, list) or not any(
            isinstance(s, dict) and s.get("type") == "recipe.apply" for s in steps):
        return profile, {}, []
    out_steps, used, problems = [], {}, []
    for st in steps:
        if not isinstance(st, dict) or st.get("type") != "recipe.apply":
            out_steps.append(st)
            continue
        sid = str(st.get("id") or "")
        where = f"step '{sid}' (recipe.apply)"
        extra = sorted(set(st) - {"id", "type", "recipe", "needs", "with", "when"})
        if extra:
            problems.append(f"{where}: {', '.join(extra)} is not an argument of recipe.apply")
            continue
        rid = str(st.get("recipe") or "")
        try:
            doc, digest = load(rid)
        except tb.Refusal as exc:
            problems.append(f"{where}: {exc}")
            continue
        bad = recipe_problems(doc)
        if bad:
            problems.append(f"{where}: the recipe '{rid}' is refused: {bad[0]}")
            continue
        used[rid] = digest
        needs, b1 = _pairs(st.get("needs"), f"{where} needs")
        given, b2 = _pairs(st.get("with"), f"{where} with")
        problems += b1 + b2
        mapping = {"tenant": "{label}"}
        for n, spec in (doc.get("needs") or {}).items():
            if isinstance(spec, dict) and spec.get("install") is True:
                problems.append(f"{where}: the recipe installs '{spec.get('app')}' itself "
                                "(install: true) -- in a profile the app.install is the "
                                "profile's own step")
            if not needs.get(n):
                problems.append(f"{where}: needs names the instance for '{n}' "
                                f"(app {(spec or {}).get('app')}), e.g. {n}=<instance>")
            mapping[n] = needs.get(n, "")
            # the node-wide key of an instance of the tenant being built:
            # `<label>-<name>` (instance_key, RFC-0025 section 8.1)
            mapping[n + KEY_SUFFIX] = "{label}-" + needs.get(n, "")
        for n in needs:
            if n not in (doc.get("needs") or {}):
                problems.append(f"{where}: '{n}' is not a need of the recipe '{rid}'")
        params = doc.get("params") or {}
        for n in given:
            if n not in params:
                problems.append(f"{where}: '{n}' is not a parameter of the recipe '{rid}'")
        for n, spec in params.items():
            if n in given:
                val = given[n]
            elif "default" in (spec or {}):
                val = str(spec["default"])
            elif (spec or {}).get("required"):
                problems.append(f"{where}: the recipe needs '{n}' (with: {n}=...)")
                continue
            else:
                val = ""
            if (spec or {}).get("kind") == "light" and not _REF.search(val) \
                    and val not in LIGHTS:
                problems.append(f"{where}: '{n}' is green, yellow or red")
            mapping[n] = val
        ids = {s["id"]: f"{sid}-{s['id']}" for s in doc.get("steps") or []
               if isinstance(s, dict) and s.get("id")}
        for rs in doc.get("steps") or []:
            missing = set()
            new = {k: (v if k in ("id", "type") else _subst(v, mapping, missing))
                   for k, v in rs.items()}
            new["id"] = ids.get(rs.get("id"), sid)
            if "pin_step" in new:
                new["pin_step"] = ids.get(rs.get("pin_step"), new["pin_step"])
            if st.get("when"):
                new["when"] = st["when"]
            if not tb.ID_RE.match(new["id"]):
                problems.append(f"{where}: '{new['id']}' is too long for a step id -- "
                                "a shorter id for the recipe.apply step")
            if missing:
                problems.append(f"{where}: '{rid}' step '{rs.get('id')}' needs "
                                + ", ".join(sorted(missing)))
            out_steps.append(new)
    return dict(profile, steps=out_steps), used, problems
