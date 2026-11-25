"""oaap.core.tenant -- the tenant build profile (RFC-0055).

A profile is a versioned JSON file naming parameters and an ordered list of
steps; a build is one run of a profile with concrete values. This file is the
PURE half: reading and judging a profile, the state file, the order in which
steps run, what "continue" and "roll back" mean. It knows no verb. Everything
that touches the node (the tenant store, the gateway, the realm) is a
`drivers` object handed in by appctl, so the rules here can be tested
anywhere, with a fake node, and the real node is touched through exactly one
door per step: the verb the operator would have typed.

Three rules carry the whole design and none of them is a setting:

* A step is DONE when its CHECK says the result is on the node, not when the
  verb returned 0. A verb that returned 0 and left nothing is a failed step.
* A build touches only what it made. `made` lists it, step by step, and a
  rollback walks that list backwards and nothing else.
* The profile is a description, not a program. The step types are a closed
  list, a value is a value (never a command), and a profile that names
  something outside the list is refused whole, before anything runs.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

PROFILE_FORMAT = "oaap.tenant-profile/1"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
WORD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# the attributes the platform propagates (idp.PROPAGATE_CLAIM_RE, oaap.core.identity 2.11)
CLAIM_RE = re.compile(r"sap_user(_[a-z0-9]{1,16})?")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# b- a build (RFC-0055), r- a run of a recipe (RFC-0109): one engine, one
# state format, two folders.
BUILD_ID_RE = re.compile(r"^[br]-[0-9]{8}t[0-9]{6}-[a-z0-9-]{1,63}$")
TEMPLATE_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

PARAM_KINDS = ("label", "text", "color", "connector", "word", "bool", "apps")
# kind `apps` (RFC-0055 section 15): the apps chosen at the start, as ids of
# the catalogue. An `app.install` whose `app` is exactly "{p}" of this kind
# becomes one step per chosen app before anything runs; inside that step
# `{app}` is the id of the app at hand.
APPS_MAX_DEFAULT = 12
APPS_MAX = 30
SELECTION_REF = "app"
TRUE_WORDS = ("true", "1", "yes", "on")
FALSE_WORDS = ("false", "0", "no", "off", "")

# step type -> (required keys, optional keys). A key outside both is a
# refusal: a profile cannot smuggle an argument the verb never promised.
STEP_TYPES = {
    "tenant.create":  (("label",), ("title",)),
    "address.ensure": ((), ()),
    "address.wait":   ((), ("timeout",)),
    "idp.provision":  (("connector",), ("idp_label",)),
    "tenant.policy":  ((), ("first_login", "default_role",
                            "self_registration")),
    "tenant.face":    ((), ("title", "color_primary", "color_accent")),
    # Exactly one of `source` (a path or URL, for a profile the operator wrote
    # on the node) and `app` (an id in a configured store source, the only
    # form an UPLOADED profile may use -- see upload_problems).
    # `confirm_administer` (oaap.core.authorization 2.10): "{p}" naming the
    # SAME bool parameter as the step's `when`. One tick then installs an app
    # that administers its tenant's grants AND is the operator's confirmation;
    # a profile can never confirm in advance (no literal "true").
    "app.install":    (("name",), ("source", "app", "source_id", "channel",
                                   "path", "ref", "confirm_administer")),
    # oaap.core.authorization 0.4 section 2.11: roles, collections, mappings
    # and realm groups from the templates of one app. `only`/`skip` are
    # comma-separated template keys, `groups` is KEY=PATH[,KEY=PATH] and
    # `names` is KEY=NAME[,KEY=NAME] (a name holds no comma).
    "authz.setup":    (("app",), ("only", "skip", "groups", "names")),
    # I-42: one configuration value of an instance this build installs
    # (the operating mode, a contact address, an operator's name) -- after
    # its app.install, so the instance exists. One key per step: each is
    # checked, repeated and reported on its own, and a value stays text.
    # The value never appears in a note or the audit trail.
    # RFC-0079: `generate: true` instead of `value` -- the node produces the
    # value, of the kind the APP's manifest declares, and only into an empty
    # key. Exactly one of the two.
    "app.config":     (("name", "key"), ("value", "generate")),
    # RFC-0056 inside a build: the group the tenant's own administrator
    # belongs in, in the tenant's realm (a group, never a person).
    "idp.admin_group": ((), ()),
    # Portal 2.20 / tenant 2.9.2: the app's OWN client in the tenant's realm
    # (identity.oidc_client) -- what `oaap idp app` does, made by the
    # connector that made the tenant's realm. Nothing to choose.
    "idp.app":        (("name",), ()),
    # RFC-0104 D1: an http destination of the tenant, assigned to an
    # instance this build installs (`need` defaults to `name`). Never a
    # secret: one that needs it is made without, and a `manual` step with
    # done_when "destination.secret" waits for a human to enter it.
    "destination.ensure": (("name", "instance", "target"),
                           ("auth", "user", "header", "need",
                            # RFC-0109 section 12 (stage 3): principal
                            # propagation, pinned to the fingerprint a human
                            # compared and confirmed in an earlier manual
                            # step (done_when server.compared) -- `pin_step`
                            # names it. Never a fingerprint in the profile.
                            "pin_step", "claim", "name_check", "check_path")),
    # RFC-0109 section 12: inside a realm the tenant already has -- the
    # attribute the platform propagates (only administrators may edit it),
    # a client scope that puts it into the token, on the platform's client.
    # Made by the tenant's connector; nothing is ever deleted there.
    "idp.claim":      (("claim",), ("scope",)),
    # RFC-0109 section 12: bind an instance to a destination of its tenant,
    # with `propagate: "true"` acting as the signed-in person -- a step of
    # its own, after the chain test, not part of destination.ensure.
    "destination.bind": (("name", "destination"), ("need", "propagate")),
    # RFC-0104 D2: what an instance's setup door should have in place --
    # a list of items {kind, id, ...}; the app creates them by its own
    # rules and never overwrites a human's work.
    "app.setup":      (("name", "items"), ()),
    # the arguments of the two reads that need them (MANUAL_READ_KEYS)
    "manual":         (("text", "done_when"), ("destination", "instance",
                                               "kind", "item", "target",
                                               # 0.1.329: what the page offers
                                               # to download next to the step
                                               "offer")),
    "backup.check":   ((), ()),
    # RFC-0109 D9 (stage 3): a recipe's steps inside a profile. Expanded when
    # the profile is read (RECIPE_EXPANDER), never driven as itself: `needs`
    # is NEED=INSTANCE[,...], `with` is PARAM=VALUE[,...] (values may hold
    # the profile's own placeholders).
    "recipe.apply":   (("recipe",), ("needs", "with")),
}
# set by appctl: (profile, pins) -> (expanded profile, {recipe: digest},
# problems); None leaves recipe.apply as it is (and nothing can drive it)
RECIPE_EXPANDER = None
# `when` names a bool parameter: the step is skipped unless it is true.
COMMON_KEYS = ("id", "type", "label_text", "when")
WHEN_MAX = 4
DONE_WHEN = ("user.exists", "role.tenant_admin", "confirmed",
             "destination.secret", "setup.ready",
             # RFC-0109 section 12: the target's server certificate, read by
             # the node, compared by a human and confirmed WITH the
             # fingerprint they compared; and the chain test (RFC-0096)
             "server.compared", "destination.check")
# done_when -> the step keys it needs; no other done_when takes any
MANUAL_READ_KEYS = {"destination.secret": ("destination", "instance"),
                    "setup.ready": ("instance", "kind", "item"),
                    "server.compared": ("target",),
                    "destination.check": ("destination",)}
# a waiting step of these says what the node read beside its text (the
# fingerprint to compare, the link of the chain that broke)
MANUAL_SHOWS_READING = ("server.compared", "destination.check")
# 0.1.329 (Jörg on oaapx01): files a person needs for a manual step, offered
# on the run page right at the step -- the tenant's CA (SAP: STRUST) and a
# sample certificate of it (SAP: CERTRULE). Public halves, never a key.
MANUAL_OFFERS = ("ca", "sample")
PROPAGATION_KEYS = ("pin_step", "claim", "name_check", "check_path")
SETUP_ITEMS_MAX = 40
SECRET_WORDS = ("key", "token", "secret", "password", "credentials")
# what a destination.ensure may say about its authentication: no mailbox.
# Principal propagation (RFC-0066) only with `pin_step`: the pinned
# certificate is never in the profile, a human confirms it during the run
# (RFC-0109 section 12).
ENSURE_AUTH = ("none", "basic", "bearer", "header", "principal_propagation")

STEP_STATES = ("pending", "running", "done", "failed", "waiting", "skipped")
BUILD_STATES = ("running", "waiting", "failed", "done", "rolled-back")
OPEN_STATES = ("running", "waiting", "failed")

MAX_PROFILE_BYTES = 64 * 1024
LOCK_STALE_SECONDS = 3600
PRESENT, ABSENT, CONFLICT = "present", "absent", "conflict"


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Refusal(Exception):
    """A rule said no. The message is one sentence for a human."""


# ----------------------------------------------------------- the profile

def digest_of(raw):
    return hashlib.sha256(raw).hexdigest()


# ------------------------------------------------- versions (RFC-0079)

VERSION_RE = re.compile(r"^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}(-[0-9A-Za-z.]{1,20})?$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
HISTORY = ".history"


def profile_version(doc):
    """The profile's version, or "" for one without (shown as such)."""
    v = str((doc or {}).get("version") or "")
    return v if VERSION_RE.match(v) else ""


def version_key(v):
    """Numeric ordering: 1.2.10 after 1.2.9, 1.0.0 after 1.0.0-rc1 -- the
    rule the marketplace orders releases by. Build metadata after '+'
    (semver: `15.0.11+oaap.1`) does not order and is dropped."""
    core, _, pre = str(v).partition("+")[0].partition("-")
    nums = tuple(int(x) for x in core.split("."))
    parts = []
    for p in pre.split(".") if pre else []:
        parts.append((0, int(p), "") if p.isdigit() else (1, 0, p))
    return nums + ((1,) if not pre else (0, tuple(parts)),)


def upload_decision(new_doc, new_digest, cur_doc, cur_digest):
    """What an upload of `new_doc` does next to the profile the node holds
    under the same id -> "add" | "replace" | "same". Raises Refusal.

    Same version, same bytes: nothing changes. Same version, other bytes:
    refused -- one version names one file, or "built with 1.1.0" means
    nothing. Lower: refused. Without a version over one with: refused.
    """
    if cur_doc is None:
        return "add"
    if new_digest == cur_digest:
        return "same"
    nv, cv = profile_version(new_doc), profile_version(cur_doc)
    pid = new_doc.get("id", "?")
    if not nv:
        if cv:
            raise Refusal(f"profile '{pid}' is held in version {cv}; an "
                          "upload without a version cannot replace it -- "
                          "give the file a higher 'version'")
        return "replace"
    if not cv:
        return "replace"
    if nv == cv:
        raise Refusal(f"version {nv} of profile '{pid}' is already on the "
                      "node with a different content -- raise the version")
    if version_key(nv) < version_key(cv):
        raise Refusal(f"profile '{pid}' is held in version {cv}; {nv} is "
                      "older and is not taken")
    return "replace"


def history_dir(root, pid):
    if not ID_RE.match(str(pid or "")):
        raise Refusal(f"'{pid}' is not a profile name")
    return os.path.join(root, HISTORY, pid)


def history_put(root, pid, raw, by, now=None):
    """Keep this file of profile `pid` under its digest -> digest.

    Idempotent: a file already kept keeps its first record (who, when)."""
    d = history_dir(root, pid)
    os.makedirs(d, exist_ok=True)
    dg = digest_of(raw)
    path = os.path.join(d, dg + ".json")
    if os.path.isfile(path):
        return dg
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        doc = {}
    meta = {"digest": dg, "version": profile_version(doc),
            "title": str(doc.get("title") or "")[:200],
            "kept": now or iso_now(), "by": str(by or "")}
    for name, data in ((dg + ".meta.json",
                        json.dumps(meta).encode("utf-8")), (dg + ".json", raw)):
        tmp = os.path.join(d, name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(d, name))
    return dg


def history_list(root, pid):
    """[meta] of every kept file of `pid`, newest version first."""
    try:
        d = history_dir(root, pid)
        names = os.listdir(d)
    except (OSError, Refusal):
        return []
    out = []
    for n in names:
        if not n.endswith(".meta.json") or not DIGEST_RE.match(n[:-10]):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("digest") == n[:-10]:
            out.append(m)

    def order(m):
        v = m.get("version") or ""
        if v:
            return (1, version_key(v), m.get("kept", ""))
        return (0, (), m.get("kept", ""))
    return sorted(out, key=order, reverse=True)


def history_path(root, pid, digest):
    """The kept file of `pid` with this digest, or None."""
    if not DIGEST_RE.match(str(digest or "")):
        return None
    p = os.path.join(history_dir(root, pid), digest + ".json")
    return p if os.path.isfile(p) else None


def history_drop(root, pid):
    """Remove every kept file of `pid` (deleting the profile)."""
    d = history_dir(root, pid)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass
    try:
        os.rmdir(d)
    except OSError:
        pass


def upload_problems(doc, known_apps=None):
    """Extra reasons a profile may not be UPLOADED through the portal.

    A profile written on the node by the operator may name any package
    source; one that arrives through a web page names apps of the configured
    catalogue only -- a catalogue entry is something a server_admin chose to
    trust, a path or URL typed into an upload is not. `known_apps` (ids) is
    checked when given."""
    out = []
    for i, st in enumerate((doc or {}).get("steps") or []):
        if isinstance(st, dict) and st.get("type") == "app.install":
            sid = st.get("id", i + 1)
            if "source" in st or "path" in st or "ref" in st:
                out.append(f"step '{sid}': an uploaded profile names an app of "
                           "the catalogue ('app'), not a 'source', 'path' or 'ref'")
            app = st.get("app")
            # RFC-0133 §4 (D4): an app named only under a tick (`when`) is not
            # a refusal here -- the page greys the tick, the start refuses it
            # when set (ticked_unknown_apps); without `when` nothing changes
            if isinstance(app, str) and not TEMPLATE_RE.search(app) \
                    and known_apps is not None and app not in known_apps \
                    and not when_names(st):
                out.append(f"step '{sid}': '{app}' is not in any configured "
                           "store source")
    return out


def read_profile_file(path, pins=None):
    """-> (profile dict, digest). Raises Refusal with one sentence.

    RFC-0109 D9: a `recipe.apply` is expanded here, before the profile's
    checks, by RECIPE_EXPANDER; `pins` ({recipe: digest}, from a build's
    state) makes continue and rollback expand the same recipe files. The
    expanded profile says which it used under `_recipes`."""
    try:
        size = os.path.getsize(path)
        if size > MAX_PROFILE_BYTES:
            raise Refusal(f"the profile file is larger than "
                          f"{MAX_PROFILE_BYTES // 1024} KB")
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise Refusal(f"the profile cannot be read ({type(exc).__name__})")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refusal("the profile is not valid JSON")
    if RECIPE_EXPANDER is not None and isinstance(doc, dict):
        doc, used, problems = RECIPE_EXPANDER(doc, pins)
        if problems:
            raise Refusal("the profile is refused: " + "; ".join(problems[:5]))
        if used:
            doc["_recipes"] = used
    problems = profile_problems(doc)
    if problems:
        raise Refusal("the profile is refused: " + "; ".join(problems[:5])
                      + (f" (and {len(problems) - 5} more)"
                         if len(problems) > 5 else ""))
    return doc, digest_of(raw)


def profile_problems(doc):
    """Every reason this profile cannot run, as sentences. [] = fine.

    Judged whole and before anything runs: a profile with an unknown step
    type in step nine must not create the tenant in step one.
    """
    out = []
    if not isinstance(doc, dict):
        return ["a profile is a JSON object"]
    if doc.get("profile") != PROFILE_FORMAT:
        out.append(f"'profile' must say {PROFILE_FORMAT}")
    if not ID_RE.match(str(doc.get("id") or "")):
        out.append("'id' is a short lowercase word")
    if "version" in doc and not VERSION_RE.match(str(doc.get("version") or "")):
        out.append("'version' is x.y.z (e.g. 1.1.0), optionally -pre")
    params = doc.get("params")
    if not isinstance(params, dict):
        out.append("'params' must be an object")
        params = {}
    for name, spec in params.items():
        if not PARAM_RE.match(name) or name == SELECTION_REF:
            out.append(f"parameter name '{name}' is not allowed")
        if not isinstance(spec, dict) or spec.get("kind") not in PARAM_KINDS:
            out.append(f"parameter '{name}' needs a kind of "
                       + "/".join(PARAM_KINDS))
        elif spec["kind"] == "apps":
            out += _apps_spec_problems(name, spec)
        if isinstance(spec, dict) and "distinct" in spec and (
                spec.get("kind") not in ("word", "text")
                or not WORD_RE.match(str(spec.get("distinct") or ""))):
            out.append(f"parameter '{name}': 'distinct' names a group (a short "
                       "lowercase word) and goes with kind word or text")
    if "label" not in params or (params.get("label") or {}).get("kind") != "label":
        out.append("a profile needs a parameter 'label' of kind label")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        return out + ["'steps' must be a non-empty list"]
    seen = set()
    for i, st in enumerate(steps):
        where = f"step {i + 1}"
        if not isinstance(st, dict):
            out.append(f"{where} must be an object")
            continue
        sid = str(st.get("id") or "")
        if not ID_RE.match(sid):
            out.append(f"{where} needs an 'id' (short lowercase word)")
        elif sid in seen:
            out.append(f"{where}: the id '{sid}' is used twice")
        seen.add(sid)
        typ = st.get("type")
        if typ not in STEP_TYPES:
            out.append(f"{where} ('{sid}'): unknown step type '{typ}'")
            continue
        req, opt = STEP_TYPES[typ]
        for k in req:
            if k not in st:
                out.append(f"{where} ('{sid}', {typ}) needs '{k}'")
        for k in st:
            if k not in req + opt + COMMON_KEYS:
                out.append(f"{where} ('{sid}', {typ}): '{k}' is not an "
                           f"argument of this step type")
        chooser = selection_param(st, params)
        if typ == "app.setup":
            out += _setup_items_problems(where, sid, st.get("items"), params)
        for k, v in st.items():
            if k in ("id", "type") or (typ == "app.setup" and k == "items")                     or (k == "when" and isinstance(v, list)):
                continue          # a list in `when` is judged below
            if isinstance(v, str):
                for ref in TEMPLATE_RE.findall(v):
                    if ref == SELECTION_REF and chooser and k != "app":
                        continue
                    if ref not in params:
                        out.append(f"{where} ('{sid}'): '{{{ref}}}' is not a "
                                   f"declared parameter")
                    elif (params[ref] or {}).get("kind") == "apps" \
                            and not (chooser == ref and k == "app"):
                        out.append(f"{where} ('{sid}'): '{{{ref}}}' is a choice "
                                   "of apps; it stands only as the whole 'app' "
                                   "of an app.install")
            elif k == "generate":
                if v is not True:
                    out.append(f"{where} ('{sid}'): generate is true or absent")
            elif k == "timeout":
                if not (isinstance(v, int) and not isinstance(v, bool)
                        and 1 <= v <= 900):
                    out.append(f"{where} ('{sid}'): timeout is 1 to 900 seconds")
            elif v is not None:
                out.append(f"{where} ('{sid}'): '{k}' must be text")
        if "when" in st:
            # 0.1.375: one bool parameter, or a list of up to four that must
            # ALL be ticked (an app that needs the realm: with_wiki and
            # with_realm)
            w = st["when"]
            names = w if isinstance(w, list) else [w]
            if not names or len(names) > WHEN_MAX or not all(
                    isinstance(n, str) and n in params
                    and (params[n] or {}).get("kind") == "bool" for n in names) \
                    or len(set(names)) != len(names):
                out.append(f"{where} ('{sid}'): 'when' must name a parameter "
                           f"of kind bool, or a list of up to {WHEN_MAX} of them")
        if typ == "app.install":
            if ("source" in st) == ("app" in st):
                out.append(f"{where} ('{sid}'): app.install needs exactly one "
                           "of 'source' and 'app'")
            if chooser and SELECTION_REF not in TEMPLATE_RE.findall(
                    str(st.get("name") or "")):
                out.append(f"{where} ('{sid}'): an app.install over a choice "
                           "of apps needs '{app}' in its 'name' -- every "
                           "chosen app gets its own instance")
            if chooser and "confirm_administer" in st:
                out.append(f"{where} ('{sid}'): an app that administers its "
                           "tenant's grants is not chosen from a list; it "
                           "gets its own step with its own tick")
            elif "confirm_administer" in st:
                m = TEMPLATE_RE.fullmatch(str(st["confirm_administer"]))
                if not m or m.group(1) not in when_names(st):
                    out.append(f"{where} ('{sid}'): confirm_administer is "
                               "'{p}' naming the same bool parameter as "
                               "'when' -- the tick that installs is the "
                               "confirmation, a profile never confirms in "
                               "advance")
        if "generate" in st and st["generate"] is not True \
                and not isinstance(st["generate"], (bool, int)):
            # text is not caught by the type check above (text is allowed
            # everywhere else); the KIND belongs to the manifest, never here
            out.append(f"{where} ('{sid}'): generate is true or absent")
        if typ == "app.config" and ("value" in st) == ("generate" in st):
            out.append(f"{where} ('{sid}', app.config) needs exactly one of "
                       "'value' and 'generate'")
        if typ == "manual" and st.get("done_when") not in DONE_WHEN:
            out.append(f"{where} ('{sid}'): done_when is one of "
                       + ", ".join(DONE_WHEN))
        if typ == "manual":
            need = MANUAL_READ_KEYS.get(st.get("done_when"), ())
            for k in need:
                if not st.get(k):
                    out.append(f"{where} ('{sid}'): done_when {st.get('done_when')} "
                               f"needs '{k}'")
            offers = [x.strip() for x in str(st.get("offer") or "").split(",") if x.strip()]
            bad = [x for x in offers if x not in MANUAL_OFFERS]
            if bad:
                out.append(f"{where} ('{sid}'): offer is a list of "
                           + ", ".join(MANUAL_OFFERS))
            for k in ("destination", "instance", "kind", "item", "target"):
                if k in st and k not in need:
                    out.append(f"{where} ('{sid}'): '{k}' is no argument of "
                               f"done_when {st.get('done_when')}")
        if typ == "destination.ensure":
            auth = st.get("auth", "none")
            if isinstance(auth, str) and not TEMPLATE_RE.search(auth)                     and auth not in ENSURE_AUTH:
                out.append(f"{where} ('{sid}'): auth is one of "
                           + ", ".join(ENSURE_AUTH))
            if auth == "basic" and "user" not in st:
                out.append(f"{where} ('{sid}'): auth basic needs 'user'")
            if auth == "header" and "header" not in st:
                out.append(f"{where} ('{sid}'): auth header needs 'header'")
            for k in ("secret", "password", "token"):
                if k in st:
                    out.append(f"{where} ('{sid}'): a profile never holds a secret")
            if auth == "principal_propagation":
                pin = st.get("pin_step")
                earlier = {x.get("id"): x for x in steps[:i] if isinstance(x, dict)}
                ref = earlier.get(pin) or {}
                if not pin or ref.get("type") != "manual" \
                        or ref.get("done_when") != "server.compared":
                    out.append(f"{where} ('{sid}'): principal_propagation needs "
                               "'pin_step' naming an EARLIER manual step with "
                               "done_when server.compared -- the certificate is "
                               "pinned to what a human compared, never to a "
                               "value in the profile")
                elif ref.get("target") != st.get("target"):
                    out.append(f"{where} ('{sid}'): the pin step '{pin}' compares "
                               "another target")
                if st.get("name_check") not in (None, "", "sap"):
                    out.append(f"{where} ('{sid}'): name_check is 'sap' or absent")
            elif any(k in st for k in PROPAGATION_KEYS):
                out.append(f"{where} ('{sid}'): "
                           + ", ".join(k for k in PROPAGATION_KEYS if k in st)
                           + " belong to auth principal_propagation")
        if typ == "idp.claim":
            claim = st.get("claim")
            if isinstance(claim, str) and not TEMPLATE_RE.search(claim) \
                    and not CLAIM_RE.fullmatch(claim):
                out.append(f"{where} ('{sid}'): claim is sap_user or sap_user_<x> "
                           "(the attributes the platform propagates)")
            scope = st.get("scope", "sap")
            if isinstance(scope, str) and not TEMPLATE_RE.search(scope) \
                    and not WORD_RE.match(scope):
                out.append(f"{where} ('{sid}'): scope is a short lowercase word")
        if typ == "destination.bind":
            prop = st.get("propagate", "false")
            if isinstance(prop, str) and not TEMPLATE_RE.search(prop) \
                    and prop not in ("true", "false"):
                out.append(f"{where} ('{sid}'): propagate is \"true\" or \"false\"")
    return out


def _setup_items_problems(where, sid, items, params):
    """An app.setup's items: a list of objects with a text `kind` and `id`;
    values are text (parameters allowed), true/false, or lists of text --
    since RFC-0133 also a list of flat objects of text (a view's fixed tool
    list: [{"server", "tool", "effect"}, ...]). Never a field that names a
    secret (RFC-0104 §3.1)."""
    out = []
    if not isinstance(items, list) or not items or len(items) > SETUP_ITEMS_MAX:
        return [f"{where} ('{sid}'): items is a list of 1 to {SETUP_ITEMS_MAX} objects"]
    for n, it in enumerate(items):
        at = f"{where} ('{sid}') item {n + 1}"
        if not isinstance(it, dict):
            out.append(f"{at} must be an object")
            continue
        for k in ("kind", "id"):
            if not isinstance(it.get(k), str) or not it.get(k):
                out.append(f"{at} needs a text '{k}'")
        for k, v in it.items():
            if k.lower() in SECRET_WORDS:
                out.append(f"{at}: a profile never holds a secret ('{k}')")
                continue
            texts = v if isinstance(v, list) else [v]
            if isinstance(v, list) and all(isinstance(x, dict) for x in v) and v:
                flat = [y for x in v for y in x.values()]
                if not all(isinstance(y, str) for y in flat) or any(
                        str(a).lower() in SECRET_WORDS for x in v for a in x):
                    out.append(f"{at}: '{k}' is a list of objects of text")
                    continue
                texts = flat
            elif isinstance(v, list) and not all(isinstance(x, str) for x in v):
                out.append(f"{at}: '{k}' is a list of text (or of objects of text)")
                continue
            if isinstance(v, dict):
                if not all(isinstance(x, str) for x in v.values()):
                    out.append(f"{at}: '{k}' holds text only")
                    continue
                texts = list(v.values())
            elif not isinstance(v, (str, bool, list)):
                out.append(f"{at}: '{k}' is text, true/false or a list of text")
                continue
            for t in texts:
                for ref in TEMPLATE_RE.findall(t) if isinstance(t, str) else ():
                    if ref not in params:
                        out.append(f"{at}: '{{{ref}}}' is not a declared parameter")
                    elif (params[ref] or {}).get("kind") == "apps":
                        out.append(f"{at}: '{{{ref}}}' is a choice of apps")
    return out


def _render_text(text, values, sid, where):
    whole = TEMPLATE_RE.fullmatch(text)
    if whole:
        val = values.get(whole.group(1))
        return None if val is None else val

    def sub(m):
        val = values.get(m.group(1))
        if val is None:
            raise Refusal(f"step '{sid}': {where} needs the parameter "
                          f"'{m.group(1)}', which has no value")
        return val
    return TEMPLATE_RE.sub(sub, text)


def render_items(items, values, sid):
    """An app.setup's items with the parameters filled in. A field that is
    exactly an unset "{p}" is dropped, as in render_step."""
    out = []
    for it in items or []:
        row = {}
        for k, v in it.items():
            if isinstance(v, str):
                r = _render_text(v, values, sid, f"'{k}'")
                if r is not None:
                    row[k] = r
            elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                row[k] = [{a: r for a, r in ((a, _render_text(t, values, sid, f"'{k}'"))
                                             for a, t in x.items()) if r is not None}
                          for x in v]
            elif isinstance(v, list):
                row[k] = [x for x in (_render_text(t, values, sid, f"'{k}'")
                                      for t in v) if x is not None]
            elif isinstance(v, dict):
                row[k] = {a: r for a, r in ((a, _render_text(t, values, sid, f"'{k}'"))
                                            for a, t in v.items()) if r is not None}
            else:
                row[k] = v
        out.append(row)
    return out


def _apps_spec_problems(name, spec):
    out = []
    mx = spec.get("max", APPS_MAX_DEFAULT)
    if not (isinstance(mx, int) and not isinstance(mx, bool)
            and 1 <= mx <= APPS_MAX):
        out.append(f"parameter '{name}': max is 1 to {APPS_MAX}")
    ch = spec.get("choices")
    if ch is not None and (not isinstance(ch, list) or not ch
                           or len(ch) > 200
                           or not all(isinstance(c, str) and WORD_RE.match(c)
                                      for c in ch)):
        out.append(f"parameter '{name}': choices is a list of app ids")
    d = spec.get("default")
    if d is not None:
        ids, bad = _apps_list(d)
        if bad:
            out.append(f"parameter '{name}': default {bad}")
        elif isinstance(ch, list) and any(i not in ch for i in ids):
            out.append(f"parameter '{name}': default names an app outside "
                       "its choices")
    return out


def _apps_list(val):
    """A choice of apps as given -> ([ids in order, no repeats], problem).
    Accepts a list or a comma-separated text; empty is no app."""
    items = val if isinstance(val, list) else str(val).split(",")
    ids = []
    for it in items:
        if not isinstance(it, str):
            return [], "is not a list of app ids"
        it = it.strip().lower()
        if not it:
            continue
        if not WORD_RE.match(it):
            return [], f"'{it[:40]}' is not an app id"
        if it not in ids:
            ids.append(it)
    return ids, ""


def selection_param(step, params):
    """The `apps` parameter this step is spread over, or None: an
    app.install whose `app` is exactly "{p}" with p of kind apps."""
    if not isinstance(step, dict) or step.get("type") != "app.install":
        return None
    m = TEMPLATE_RE.fullmatch(str(step.get("app") or ""))
    if m and ((params or {}).get(m.group(1)) or {}).get("kind") == "apps":
        return m.group(1)
    return None


def selected_apps(profile, values):
    """{parameter: [app ids]} for every `apps` parameter (judged values)."""
    out = {}
    for name, spec in (profile.get("params") or {}).items():
        if isinstance(spec, dict) and spec.get("kind") == "apps":
            out[name] = [i for i in str(values.get(name) or "").split(",") if i]
    return out


def steps_of(profile, values):
    """The profile's steps with every choice of apps spread out.

    A step over a choice becomes one step per chosen app, in the order
    chosen, with the id '<step>-<app>', `app` set to the id and `{app}`
    filled in its other texts. Pure and repeatable: a build keeps its
    values, so continue and rollback find the same steps again. Nothing
    chosen is no step at all. An id that collides with another step is a
    refusal before anything runs.
    """
    params = profile.get("params") or {}
    out, seen = [], set()
    for st in profile["steps"]:
        p = selection_param(st, params)
        if not p:
            out.append(st)
            continue
        for app in [i for i in str(values.get(p) or "").split(",") if i]:
            one = {}
            for k, v in st.items():
                if k == "app":
                    one[k] = app
                elif isinstance(v, str) and k not in ("id", "type", "when"):
                    one[k] = v.replace("{" + SELECTION_REF + "}", app)
                else:
                    one[k] = v
            one["id"] = f"{st['id']}-{app}"
            out.append(one)
    for st in out:
        if st["id"] in seen:
            raise Refusal(f"the step id '{st['id']}' occurs twice once the "
                          "chosen apps are spread out; rename a step")
        seen.add(st["id"])
    return out


def administer_params(profile):
    """The bool parameters whose tick confirms an app that administers its
    tenant's grants (oaap.core.authorization 2.10) -> set of names. Only a
    server_admin starting a build may give them; an applicant's form never."""
    out = set()
    for st in (profile or {}).get("steps") or []:
        if isinstance(st, dict) and st.get("type") == "app.install":
            m = TEMPLATE_RE.fullmatch(str(st.get("confirm_administer") or ""))
            if m:
                out.add(m.group(1))
    return out


def param_values(profile, given, label_check=None):
    """-> (values, problems). Every value is judged by its kind.

    A parameter nobody gave and that has no default is a problem; a
    parameter nobody declared is a problem too (a typo must not be silently
    dropped -- it would have been the colour the operator meant).
    """
    params = profile.get("params") or {}
    values, problems = {}, []
    for k in given:
        if k not in params:
            problems.append(f"'{k}' is not a parameter of this profile")
    for name, spec in params.items():
        if name in given and given[name] is not None:
            val = str(given[name])
        elif "default" in spec:
            val = spec["default"]
        elif spec.get("required"):
            problems.append(f"parameter '{name}' is required")
            continue
        else:
            val = None
        if spec.get("kind") == "apps":
            ids, bad = _apps_list(val if val is not None else "")
            ch = spec.get("choices")
            if not bad and isinstance(ch, list):
                outside = [i for i in ids if i not in ch]
                if outside:
                    bad = ("names an app this profile does not offer: "
                           + ", ".join(outside[:5]))
            mx = spec.get("max", APPS_MAX_DEFAULT)
            if not bad and isinstance(mx, int) and len(ids) > mx:
                bad = f"at most {mx} apps"
            if not bad and spec.get("required") and not ids:
                bad = "choose at least one app"
            if bad:
                problems.append(f"parameter '{name}': {bad}")
            else:
                values[name] = ",".join(ids)
            continue
        if spec.get("kind") == "bool":
            word = str(val).strip().lower() if val is not None else ""
            if word in TRUE_WORDS:
                values[name] = "true"
            elif word in FALSE_WORDS:
                values[name] = "false"
            else:
                problems.append(f"parameter '{name}': is not yes or no")
            continue
        if val is None:
            values[name] = None
            continue
        bad = _kind_problem(spec, str(val), label_check)
        if bad:
            problems.append(f"parameter '{name}': {bad}")
        else:
            values[name] = str(val)
    # `distinct`: parameters of one group must differ -- two instance names
    # of a profile that came out the same would make the second install
    # meet the first (RFC-0104, the names an operator sets before the start).
    groups = {}
    for name, spec in params.items():
        grp = spec.get("distinct") if isinstance(spec, dict) else None
        val = values.get(name)
        if grp and val is not None:
            groups.setdefault(grp, {}).setdefault(val.strip().lower(), []).append(name)
    for grp, by_val in groups.items():
        for val, names in by_val.items():
            if len(names) > 1:
                problems.append(f"parameters {', '.join(repr(n) for n in names)}: "
                                f"'{val}' twice -- these must differ")
    return values, problems


def _kind_problem(spec, val, label_check):
    kind = spec["kind"]
    if kind == "label":
        if label_check:
            return label_check(val.strip().lower()) or ""
        return "" if WORD_RE.match(val) else "not a valid label"
    if kind == "text":
        mx = spec.get("max", 120)
        if not val.strip():
            return "is empty"
        if len(val) > mx:
            return f"is longer than {mx} characters"
        # RFC-0117: a recipe's `prose` (instructions of an assistant) may
        # break lines; every other control character stays refused
        keep = "\n\t" if spec.get("multiline") is True else ""
        if any(ord(c) < 32 and c not in keep for c in val):
            return "contains control characters"
        return ""
    if kind == "color":
        return "" if COLOR_RE.match(val) else "is not a colour like #1a2b3c"
    if kind in ("connector", "word"):
        return "" if WORD_RE.match(val) else "is not a short lowercase word"
    return "has an unknown kind"


def render_step(step, values):
    """The step's arguments with the parameters filled in, as plain strings.

    "{name}" alone with no value drops the key (an unset optional colour);
    "{name}" inside a longer text with no value is a refusal. A value is
    never interpreted: this returns data for a verb's argument LIST.
    """
    out = {}
    for k, v in step.items():
        if k in ("id", "type", "label_text", "when"):
            continue
        if k == "items" and step.get("type") == "app.setup":
            out[k] = render_items(v, values, step["id"])
            continue
        if isinstance(v, str):
            whole = TEMPLATE_RE.fullmatch(v)
            if whole:
                val = values.get(whole.group(1))
                if val is None:
                    continue
                out[k] = val
                continue

            def sub(m):
                val = values.get(m.group(1))
                if val is None:
                    raise Refusal(f"step '{step['id']}': '{k}' needs the "
                                  f"parameter '{m.group(1)}', which has no value")
                return val
            out[k] = TEMPLATE_RE.sub(sub, v)
        elif v is not None:
            out[k] = v
    return out


def plan(profile, values):
    """[(id, type, rendered args)] -- what a build would do, nothing done."""
    return [(s["id"], s["type"], render_step(s, values))
            for s in steps_of(profile, values) if step_active(s, values)]


def when_names(step):
    """The bool parameters a step's `when` names (none, one or a list)."""
    w = step.get("when")
    if w is None:
        return []
    return [n for n in (w if isinstance(w, list) else [w]) if isinstance(n, str)]


def step_active(step, values):
    """False for a step whose `when` parameters are not ALL true."""
    return all(values.get(n) == "true" for n in when_names(step))


def ticked_unknown_apps(profile, values, known_apps):
    """RFC-0133 §4: the literal apps of ACTIVE `app.install` steps with a
    `when` that no configured source lists -> [(step id, app, tick names)].
    `known_apps` None (no source readable) -> []: the general check says it."""
    if known_apps is None:
        return []
    out = []
    for st in (profile or {}).get("steps") or []:
        if not isinstance(st, dict) or st.get("type") != "app.install":
            continue
        app = st.get("app")
        if not isinstance(app, str) or TEMPLATE_RE.search(app) or not when_names(st):
            continue
        if step_active(st, values) and app not in known_apps:
            out.append((st.get("id", ""), app, when_names(st)))
    return out


# ------------------------------------------------------------- the state

def new_build_id(label, now=None, prefix="b"):
    t = (now or datetime.now(timezone.utc)).strftime("%Y%m%dt%H%M%S")
    return f"{prefix}-{t}-{label}"


def _new_step(s, values):
    rec = {"id": s["id"], "type": s["type"], "state": "pending",
           "note": "", "started": "", "finished": "", "made": []}
    if s["type"] == "manual":
        # What a person is asked to do and what ends the wait, kept WITH the
        # step: a page shows it without the profile file (which may have
        # changed since), and a waiting step is then a complete question.
        try:
            rec["text"] = _lines(render_step(s, values).get("text", ""), TEXT_LIMIT)
        except Refusal:
            rec["text"] = _lines(s.get("text", ""), TEXT_LIMIT)
        rec["done_when"] = s.get("done_when", "")
        if s.get("offer"):
            rec["offer"] = [x.strip() for x in str(s["offer"]).split(",")
                            if x.strip() in MANUAL_OFFERS]
    return rec


def new_state(profile, digest, values, by, now=None):
    label = values["label"].strip().lower()
    return {
        "id": new_build_id(label, now), "profile": profile["id"],
        "version": profile_version(profile),
        "digest": digest, "label": label, "params": dict(values),
        "by": by, "created": iso_now(), "state": "running",
        "steps": [_new_step(s, values) for s in steps_of(profile, values)],
    }


def state_path(root, bid):
    if not BUILD_ID_RE.match(str(bid or "")):
        raise Refusal("that is not a build id")
    return os.path.join(root, bid + ".json")


def save_state(root, state):
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = state_path(root, state["id"])
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_state(root, bid):
    try:
        with open(state_path(root, bid), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise Refusal(f"no build '{bid}'")
    except (OSError, ValueError):
        raise Refusal(f"the state of build '{bid}' cannot be read")


def list_states(root):
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and BUILD_ID_RE.match(n[:-5]):
            try:
                out.append(load_state(root, n[:-5]))
            except Refusal:
                continue
    return out


def open_build_for(root, label):
    """The unfinished build of this label, or None (one at a time)."""
    for st in list_states(root):
        if st.get("label") == label and st.get("state") in OPEN_STATES:
            return st
    return None


class RunLock:
    """One process drives one build. A lock file with a time on it; an old
    one is stale (a killed process must not lock the label for ever)."""

    def __init__(self, root, bid):
        self.path = os.path.join(root, bid + ".run")
        self.have = False

    def _alive(self):
        """Is the process that wrote the lock still there? A build killed
        in the middle (SIGKILL, power) must not lock itself for an hour --
        found by killing one on a real node. Where processes cannot be
        asked (not POSIX) only age decides."""
        try:
            age = time.time() - os.path.getmtime(self.path)
            with open(self.path, encoding="utf-8") as f:
                pid = int((f.read().split() or ["0"])[0])
        except (OSError, ValueError):
            return False
        if age >= LOCK_STALE_SECONDS:
            return False
        if os.name != "posix" or pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
            except FileExistsError:
                if self._alive():
                    raise Refusal("this build is being driven by another "
                                  "process right now")
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {iso_now()}")
            self.have = True
            return self
        raise Refusal("the build lock could not be taken")

    def __exit__(self, *exc):
        if self.have:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


# ------------------------------------------------------------ the engine
#
# `drivers` supplies, per step type:
#   check(type, args, ctx) -> (PRESENT|ABSENT|CONFLICT, one sentence)
#   do(type, args, ctx)    -> (ok, one sentence, [made, ...])
#   undo(type, args, made, ctx) -> (ok, one sentence)
# ctx = {"label", "started", "state", "values"}; `audit(event, step, result,
# detail)` is optional and is where a build leaves its trace.

def _save(root, state, drivers):
    """Save, then tell the driver (the portal reads a view of every build
    and must see each step as it finishes, not only the end)."""
    save_state(root, state)
    fn = getattr(drivers, "saved", None)
    if fn:
        try:
            fn(state)
        except Exception:  # noqa: BLE001 -- a view never undoes a step
            pass


def _event(state, what):
    """tenant.build.<what>, or tenant.recipe.<what> for a recipe's run: the
    tenant's log says which of the two acted in its name."""
    return f"tenant.{'recipe' if state.get('kind') == 'recipe' else 'build'}.{what}"


def _audit(drivers, event, step, result, detail=""):
    fn = getattr(drivers, "audit", None)
    if fn:
        try:
            fn(event, step, result, detail)
        except Exception:  # noqa: BLE001 -- a log line never undoes a step
            pass


def _sentence(text, limit=240):
    text = " ".join(str(text or "").split())
    return text[:limit]


def _lines(text, limit=2000):
    """A person's text with its line breaks kept (0.1.329): spaces inside a
    line collapse, empty lines stay one empty line."""
    out, blank = [], False
    for ln in str(text or "").replace("\r", "").split("\n"):
        ln = " ".join(ln.split())
        if not ln:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()[:limit]


# What a person is asked to do is the profile author's text, written for a
# person: it is not cut like a machine's note (I-45 -- "...danach neu
# anmelde" was the 240th character).
TEXT_LIMIT = 2000


def _safe(call, *a):
    try:
        return call(*a), ""
    except Refusal as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 -- the class name, not the message
        return None, f"der Schritt brach ab ({type(exc).__name__})"


def run(state, profile, drivers, root, now=iso_now):
    """Drive the build from its first unfinished step. Saves after every
    change of a step. Stops at the first step that fails or waits.

    A step already `done` is read again (cheap): if its result is no longer
    on the node the build stops there and says so. It is NOT done over --
    somebody may have removed it on purpose, and a second create is not
    what the operator asked for.
    """
    values = state["params"]
    steps = {s["id"]: s for s in steps_of(profile, values)}
    ctx = {"label": state["label"], "started": state["created"],
           "state": state, "values": values}
    state["state"] = "running"
    _save(root, state, drivers)
    for rec in state["steps"]:
        step = steps[rec["id"]]
        typ = rec["type"]
        if not step_active(step, values):
            # not asked for: decided before the step is even rendered, so a
            # parameter only that step needs cannot fail the build
            if rec["state"] != "skipped":
                rec["state"], rec["note"] = "skipped", "nicht gewünscht"
                _save(root, state, drivers)
            continue
        args, bad = _safe(render_step, step, values)
        if args is None:
            return _stop(state, rec, "failed", bad, root, drivers, now)
        ctx["step"] = rec
        if rec["state"] == "done":
            res, bad = _safe(drivers.check, typ, args, ctx)
            if res is None or res[0] != PRESENT:
                return _stop(state, rec, "failed",
                             "war erledigt und ist nicht mehr vorhanden: "
                             + _sentence(bad or res[1]), root, drivers, now)
            continue
        was_running = rec["state"] == "running"
        rec["state"], rec["started"] = "running", now()
        _save(root, state, drivers)
        res, bad = _safe(drivers.check, typ, args, ctx)
        if res is None:
            return _stop(state, rec, "failed", bad, root, drivers, now)
        verdict, note = res
        if verdict == CONFLICT:
            return _stop(state, rec, "failed", _sentence(note), root,
                         drivers, now)
        if verdict == PRESENT:
            if was_running and hasattr(drivers, "claim"):
                # This build's own earlier process was interrupted inside
                # this step, after the verb did its work and before the
                # `made` line was written (found by killing one on a real
                # node: the instance was there, the list was empty, and a
                # rollback would have left it behind). The driver says
                # what of the present result is provably this build's.
                got, _bad = _safe(drivers.claim, typ, args, ctx)
                if got:
                    rec["made"] = list(rec.get("made") or []) + [
                        g for g in got if g not in (rec.get("made") or [])]
                    _save(root, state, drivers)
            _finish(state, rec, "schon vorhanden: " + _sentence(note), root,
                    drivers, now)
            continue
        if typ == "manual":
            said = _lines(args["text"], TEXT_LIMIT)
            if args.get("done_when") in MANUAL_SHOWS_READING and note:
                said += " -- " + _sentence(note, 600)
            return _stop(state, rec, "waiting", said, root, drivers, now)
        res, bad = _safe(drivers.do, typ, args, ctx)
        if res is None:
            return _stop(state, rec, "failed", bad, root, drivers, now)
        ok, note, made = res
        # What was made is written down BEFORE the verdict on the result:
        # a step that made something and then failed its check still has
        # something a rollback must find.
        rec["made"] = list(rec.get("made") or []) + list(made or [])
        _save(root, state, drivers)
        if not ok:
            return _stop(state, rec, "failed", _sentence(note), root,
                         drivers, now)
        res, bad = _safe(drivers.check, typ, args, ctx)
        if res is None:
            return _stop(state, rec, "failed", bad, root, drivers, now)
        verdict, after = res
        if verdict != PRESENT:
            return _stop(state, rec, "failed",
                         "der Schritt lief durch, aber sein Ergebnis ist "
                         "nicht auf dem Knoten: " + _sentence(after), root, drivers, now)
        _finish(state, rec, _sentence(note) or _sentence(after), root,
                drivers, now)
    state["state"] = "done"
    _save(root, state, drivers)
    _audit(drivers, _event(state, "done"), "", "ok", state["id"])
    return state


def _finish(state, rec, note, root, drivers, now):
    rec["state"], rec["note"], rec["finished"] = "done", note, now()
    _save(root, state, drivers)
    _audit(drivers, _event(state, "step"), rec["id"], "ok", note)


def _stop(state, rec, kind, note, root, drivers, now):
    rec["state"], rec["note"] = kind, _sentence(
        note, TEXT_LIMIT if kind == "waiting" else 240)
    rec["finished"] = now() if kind == "failed" else ""
    state["state"] = "waiting" if kind == "waiting" else "failed"
    _save(root, state, drivers)
    _audit(drivers, _event(state, "step"), rec["id"], kind, rec["note"])
    return state


def confirm(state, step_id, root, value=""):
    """A human says a `manual` step with done_when 'confirmed' is done.

    RFC-0109 section 12: a step with done_when 'server.compared' is
    confirmed WITH the fingerprint the human compared (`value`); the
    driver's next read pins exactly that, and only while the target still
    presents it."""
    for rec in state["steps"]:
        if rec["id"] == step_id:
            if rec["type"] != "manual":
                raise Refusal(f"step '{step_id}' is not a manual step")
            if rec["state"] != "waiting":
                raise Refusal(f"step '{step_id}' is not waiting "
                              f"(it is {rec['state']})")
            if rec.get("done_when") == "server.compared":
                fp = re.sub(r"[^0-9a-f]", "", str(value or "").lower())
                if len(fp) != 64:
                    raise Refusal(f"step '{step_id}' is confirmed with the SHA-256 "
                                  "fingerprint you compared (64 hex digits)")
                if rec.get("offered_sha256") and fp != rec["offered_sha256"]:
                    raise Refusal("that is not the fingerprint the target showed; "
                                  "nothing was confirmed")
                rec["accepted_sha256"] = fp
            rec["confirmed"] = True
            save_state(root, state)
            return state
    raise Refusal(f"no step '{step_id}' in this build")


def rollback(state, profile, drivers, root, now=iso_now):
    """Undo what THIS build made, in reverse, one safe verb each.

    An entry in `made` is the only thing touched. A step with an undo that
    refuses (a tenant that holds something) stops the rollback there and
    says so; the remaining `made` entries stay for a later continue.
    """
    values = state["params"]
    steps = {s["id"]: s for s in steps_of(profile, values)}
    ctx = {"label": state["label"], "started": state["created"],
           "state": state, "values": values}
    # A rollback that is certain to stop half way is refused BEFORE it
    # starts: a half-undone build is a worse place to be than the one the
    # operator asked to leave (found on a real node: the instance went, its
    # data stayed, the tenant could not follow, and the checkbox that would
    # have deleted the data no longer had an instance to act on).
    ask = getattr(drivers, "rollback_refusal", None)
    if ask and not state.get("rolling_back"):
        why = ask(state)
        if why:
            raise Refusal(why)
    state["rolling_back"] = True
    state["state"] = "running"
    _save(root, state, drivers)
    for rec in reversed(state["steps"]):
        if not rec.get("made"):
            continue
        args, bad = _safe(render_step, steps[rec["id"]], values)
        if args is None:
            return _stop(state, rec, "failed", bad, root, drivers, now)
        ctx["step"] = rec
        for item in list(reversed(rec["made"])):
            res, bad = _safe(drivers.undo, rec["type"], args, [item], ctx)
            if res is None or not res[0]:
                return _stop(state, rec, "failed",
                             "Rückbau angehalten: "
                             + _sentence(bad or res[1]), root, drivers, now)
            rec["made"].remove(item)
            _save(root, state, drivers)
            _audit(drivers, _event(state, "rollback"), rec["id"], "ok",
                   _sentence(res[1]))
        rec["state"], rec["note"] = "pending", "zurückgebaut"
        _save(root, state, drivers)
    state["state"] = "rolled-back"
    state.pop("rolling_back", None)
    _save(root, state, drivers)
    return state
