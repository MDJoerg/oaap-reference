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
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
BUILD_ID_RE = re.compile(r"^b-[0-9]{8}t[0-9]{6}-[a-z0-9-]{1,63}$")
TEMPLATE_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

PARAM_KINDS = ("label", "text", "color", "connector", "word")

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
    "app.install":    (("source", "name"), ("channel", "path", "ref")),
    "manual":         (("text", "done_when"), ()),
    "backup.check":   ((), ()),
}
COMMON_KEYS = ("id", "type", "label_text")
DONE_WHEN = ("user.exists", "role.tenant_admin", "confirmed")

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


def read_profile_file(path):
    """-> (profile dict, digest). Raises Refusal with one sentence."""
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
    params = doc.get("params")
    if not isinstance(params, dict):
        out.append("'params' must be an object")
        params = {}
    for name, spec in params.items():
        if not PARAM_RE.match(name):
            out.append(f"parameter name '{name}' is not allowed")
        if not isinstance(spec, dict) or spec.get("kind") not in PARAM_KINDS:
            out.append(f"parameter '{name}' needs a kind of "
                       + "/".join(PARAM_KINDS))
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
        for k, v in st.items():
            if k in ("id", "type"):
                continue
            if isinstance(v, str):
                for ref in TEMPLATE_RE.findall(v):
                    if ref not in params:
                        out.append(f"{where} ('{sid}'): '{{{ref}}}' is not a "
                                   f"declared parameter")
            elif k == "timeout":
                if not (isinstance(v, int) and not isinstance(v, bool)
                        and 1 <= v <= 900):
                    out.append(f"{where} ('{sid}'): timeout is 1 to 900 seconds")
            elif v is not None:
                out.append(f"{where} ('{sid}'): '{k}' must be text")
        if typ == "manual" and st.get("done_when") not in DONE_WHEN:
            out.append(f"{where} ('{sid}'): done_when is one of "
                       + ", ".join(DONE_WHEN))
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
        if val is None:
            values[name] = None
            continue
        bad = _kind_problem(spec, str(val), label_check)
        if bad:
            problems.append(f"parameter '{name}': {bad}")
        else:
            values[name] = str(val)
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
        if any(ord(c) < 32 for c in val):
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
        if k in ("id", "type", "label_text"):
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
            for s in profile["steps"]]


# ------------------------------------------------------------- the state

def new_build_id(label, now=None):
    t = (now or datetime.now(timezone.utc)).strftime("%Y%m%dt%H%M%S")
    return f"b-{t}-{label}"


def _new_step(s, values):
    rec = {"id": s["id"], "type": s["type"], "state": "pending",
           "note": "", "started": "", "finished": "", "made": []}
    if s["type"] == "manual":
        # What a person is asked to do and what ends the wait, kept WITH the
        # step: a page shows it without the profile file (which may have
        # changed since), and a waiting step is then a complete question.
        try:
            rec["text"] = _sentence(render_step(s, values).get("text", ""))
        except Refusal:
            rec["text"] = _sentence(s.get("text", ""))
        rec["done_when"] = s.get("done_when", "")
    return rec


def new_state(profile, digest, values, by, now=None):
    label = values["label"].strip().lower()
    return {
        "id": new_build_id(label, now), "profile": profile["id"],
        "digest": digest, "label": label, "params": dict(values),
        "by": by, "created": iso_now(), "state": "running",
        "steps": [_new_step(s, values) for s in profile["steps"]],
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


def _audit(drivers, event, step, result, detail=""):
    fn = getattr(drivers, "audit", None)
    if fn:
        try:
            fn(event, step, result, detail)
        except Exception:  # noqa: BLE001 -- a log line never undoes a step
            pass


def _sentence(text):
    text = " ".join(str(text or "").split())
    return text[:240]


def _safe(call, *a):
    try:
        return call(*a), ""
    except Refusal as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 -- the class name, not the message
        return None, f"the step stopped with {type(exc).__name__}"


def run(state, profile, drivers, root, now=iso_now):
    """Drive the build from its first unfinished step. Saves after every
    change of a step. Stops at the first step that fails or waits.

    A step already `done` is read again (cheap): if its result is no longer
    on the node the build stops there and says so. It is NOT done over --
    somebody may have removed it on purpose, and a second create is not
    what the operator asked for.
    """
    steps = {s["id"]: s for s in profile["steps"]}
    values = state["params"]
    ctx = {"label": state["label"], "started": state["created"],
           "state": state, "values": values}
    state["state"] = "running"
    _save(root, state, drivers)
    for rec in state["steps"]:
        step = steps[rec["id"]]
        typ = rec["type"]
        args, bad = _safe(render_step, step, values)
        if args is None:
            return _stop(state, rec, "failed", bad, root, drivers, now)
        ctx["step"] = rec
        if rec["state"] == "done":
            res, bad = _safe(drivers.check, typ, args, ctx)
            if res is None or res[0] != PRESENT:
                return _stop(state, rec, "failed",
                             "it was done, and is no longer in place: "
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
            _finish(state, rec, "already in place: " + _sentence(note), root,
                    drivers, now)
            continue
        if typ == "manual":
            return _stop(state, rec, "waiting", _sentence(args["text"]),
                         root, drivers, now)
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
                         "the step finished but its result is not on the "
                         "node: " + _sentence(after), root, drivers, now)
        _finish(state, rec, _sentence(note) or _sentence(after), root,
                drivers, now)
    state["state"] = "done"
    _save(root, state, drivers)
    _audit(drivers, "tenant.build.done", "", "ok", state["id"])
    return state


def _finish(state, rec, note, root, drivers, now):
    rec["state"], rec["note"], rec["finished"] = "done", note, now()
    _save(root, state, drivers)
    _audit(drivers, "tenant.build.step", rec["id"], "ok", note)


def _stop(state, rec, kind, note, root, drivers, now):
    rec["state"], rec["note"] = kind, _sentence(note)
    rec["finished"] = now() if kind == "failed" else ""
    state["state"] = "waiting" if kind == "waiting" else "failed"
    _save(root, state, drivers)
    _audit(drivers, "tenant.build.step", rec["id"], kind, rec["note"])
    return state


def confirm(state, step_id, root):
    """A human says a `manual` step with done_when 'confirmed' is done."""
    for rec in state["steps"]:
        if rec["id"] == step_id:
            if rec["type"] != "manual":
                raise Refusal(f"step '{step_id}' is not a manual step")
            if rec["state"] != "waiting":
                raise Refusal(f"step '{step_id}' is not waiting "
                              f"(it is {rec['state']})")
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
    steps = {s["id"]: s for s in profile["steps"]}
    values = state["params"]
    ctx = {"label": state["label"], "started": state["created"],
           "state": state, "values": values}
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
                             "rollback stopped: "
                             + _sentence(bad or res[1]), root, drivers, now)
            rec["made"].remove(item)
            _save(root, state, drivers)
            _audit(drivers, "tenant.build.rollback", rec["id"], "ok",
                   _sentence(res[1]))
        rec["state"], rec["note"] = "pending", "rolled back"
        _save(root, state, drivers)
    state["state"] = "rolled-back"
    state.pop("rolling_back", None)
    _save(root, state, drivers)
    return state
