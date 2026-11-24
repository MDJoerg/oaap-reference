"""The event bus's pure half (oaap.events.bus 0.1, RFC-0127 stage 1).

Two readers, one file: the node (appctl.py loads it by path, to check an
`events` block at install and write the catalogue) and the twin service
(bus_door.py, to check a publish against that catalogue and to build the
envelope). Nothing here opens a file, a socket or a database: a manifest, a
schema, a body go in, a verdict or an event comes out -- so every rule is
tested without Flask, Postgres or a node.
"""
import hashlib
import hmac
import json
import os
import re
import time

SPECVERSION = "1.0"
MAX_TYPES = 50
MAX_SCHEMA_BYTES = 32 * 1024
MAX_DATA_BYTES = 64 * 1024
RATE_PER_MINUTE = 120
FEED_LIMIT_DEFAULT = 100
FEED_LIMIT_MAX = 200
RETENTION_DAYS = 30
AUDIENCES = ("addressed", "tenant", "listed")
# The push (0.3): one event per POST, in store order, at least once.
PUSH_PER_PASS = 50
PUSH_TIMEOUT_S = 5
PUSH_FAILURES_MAX = 10
PUSH_BACKOFF_MAX_S = 3600
ENDPOINT_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,199}$")

TYPE_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+){3,7}$")
MAJOR_RE = re.compile(r"\.v[0-9]+$")
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
SUBJECT_MAX = 200
# CloudEvents attributes the platform sets; a publisher's value is replaced.
PLATFORM_ATTRS = ("specversion", "id", "source", "time", "datacontenttype",
                  "dataschema", "oaaptenant", "oaaporigin", "oaapsemantic")

# ------------------------------------------------------------------ names


def app_type_prefix(app_id):
    """An app's own types start with `app.<id>.` -- the id with its dashes
    dropped, since a type level is [a-z0-9]."""
    return "app." + app_id.replace("-", "") + "."


def type_error(t, app_id=None):
    """'' when `t` is a type name an app may declare, else the sentence."""
    if not isinstance(t, str) or len(t) > 120 or not TYPE_RE.match(t):
        return (f"{t!r} is not an event type name: <namespace>.<entity>.<what>.v<major>, "
                "lower case a-z0-9, 4-8 levels, at most 120 characters")
    if not MAJOR_RE.search(t):
        return f"{t!r}: the last level is the major version, e.g. '.v1'"
    return ""


def own_type_error(t, app_id):
    err = type_error(t)
    if err:
        return err
    if t.startswith(("oaap.", "ext.")):
        return f"{t!r}: 'oaap.' is the platform's namespace and 'ext.' the receivers'"
    if not t.startswith(app_type_prefix(app_id)):
        return f"{t!r}: an app publishes under its own namespace '{app_type_prefix(app_id)}…'"
    return ""

# ----------------------------------------------------------------- schema

_SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items",
                "enum", "const", "minLength", "maxLength", "minimum", "maximum",
                "maxItems", "pattern", "description", "title", "$schema", "$id"}
_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


def schema_error(s, path="$"):
    """'' when `s` uses only the subset of §2.3, else the sentence."""
    if not isinstance(s, dict):
        return f"{path}: a schema is an object"
    extra = sorted(set(s) - _SCHEMA_KEYS)
    if extra:
        return (f"{path}: {', '.join(extra)} is not in the subset the platform checks "
                "(oaap.events.bus §2.3)")
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not ts or any(x not in _TYPES for x in ts):
            return f"{path}.type: one of {', '.join(_TYPES)} or a list of them"
    if "properties" in s:
        if not isinstance(s["properties"], dict):
            return f"{path}.properties: an object"
        for k, sub in s["properties"].items():
            err = schema_error(sub, f"{path}.{k}")
            if err:
                return err
    if "required" in s and (not isinstance(s["required"], list)
                            or any(not isinstance(x, str) for x in s["required"])):
        return f"{path}.required: a list of names"
    ap = s.get("additionalProperties")
    if ap is not None and ap is not False and ap is not True:
        err = schema_error(ap, f"{path}.additionalProperties")
        if err:
            return err
    if "items" in s:
        err = schema_error(s["items"], f"{path}[]")
        if err:
            return err
    if "enum" in s and (not isinstance(s["enum"], list) or not s["enum"]):
        return f"{path}.enum: a non-empty list"
    for k in ("minLength", "maxLength", "maxItems"):
        if k in s and (not isinstance(s[k], int) or isinstance(s[k], bool) or s[k] < 0):
            return f"{path}.{k}: a whole number from 0"
    for k in ("minimum", "maximum"):
        if k in s and (not isinstance(s[k], (int, float)) or isinstance(s[k], bool)):
            return f"{path}.{k}: a number"
    if "pattern" in s:
        try:
            re.compile(s["pattern"])
        except (re.error, TypeError):
            return f"{path}.pattern: not a regular expression"
    return ""


def _is(v, t):
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    return v is None


def validate(v, s, path="data"):
    """None when `v` fits schema `s` (the subset), else (path, sentence) of
    the first place where it does not."""
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(v, x) for x in ts):
            return path, f"expected {' or '.join(ts)}"
    if "const" in s and v != s["const"]:
        return path, f"must be {json.dumps(s['const'])}"
    if "enum" in s and v not in s["enum"]:
        return path, "not one of " + ", ".join(json.dumps(x) for x in s["enum"][:10])
    if isinstance(v, str):
        if "minLength" in s and len(v) < s["minLength"]:
            return path, f"at least {s['minLength']} characters"
        if "maxLength" in s and len(v) > s["maxLength"]:
            return path, f"at most {s['maxLength']} characters"
        if "pattern" in s and not re.search(s["pattern"], v):
            return path, f"does not match {s['pattern']}"
    if _is(v, "number"):
        if "minimum" in s and v < s["minimum"]:
            return path, f"at least {s['minimum']}"
        if "maximum" in s and v > s["maximum"]:
            return path, f"at most {s['maximum']}"
    if isinstance(v, list):
        if "maxItems" in s and len(v) > s["maxItems"]:
            return path, f"at most {s['maxItems']} items"
        if "items" in s:
            for i, x in enumerate(v):
                r = validate(x, s["items"], f"{path}[{i}]")
                if r:
                    return r
    if isinstance(v, dict):
        for k in s.get("required") or []:
            if k not in v:
                return f"{path}.{k}", "required"
        props = s.get("properties") or {}
        for k, x in v.items():
            if k in props:
                r = validate(x, props[k], f"{path}.{k}")
                if r:
                    return r
            else:
                ap = s.get("additionalProperties", True)
                if ap is False:
                    return f"{path}.{k}", "not allowed here"
                if isinstance(ap, dict):
                    r = validate(x, ap, f"{path}.{k}")
                    if r:
                        return r
    return None

# --------------------------------------------------------------- manifest


def declaration(m, read_file):
    """Check a manifest's `events` block. Returns (error, entry): error ''
    and the catalogue entry ({publishes, subscribes}) or None when the
    manifest has no block; or the sentence and None.

    `read_file(path)` returns the bytes of a file in the package, or None
    when there is none (the node's job, not this module's)."""
    if "events" not in m:
        return "", None
    ev = m["events"]
    app_id = str((m.get("app") or {}).get("id", ""))
    if not isinstance(ev, dict):
        return "events: object expected", None
    extra = sorted(set(ev) - {"publishes", "subscribes", "endpoint"})
    if extra:
        return ("events: only 'publishes', 'subscribes' and 'endpoint' are known "
                f"(found {', '.join(extra)})"), None
    endpoint = ev.get("endpoint")
    if endpoint is not None and (not isinstance(endpoint, str) or not ENDPOINT_RE.match(endpoint)
                                 or ".." in endpoint.split("/")):
        return ("events.endpoint: a path of the app, e.g. /oaap/events "
                "(letters, digits, . _ ~ - /, no query)"), None
    pubs, subs = ev.get("publishes") or [], ev.get("subscribes") or []
    if not isinstance(pubs, list) or not isinstance(subs, list):
        return "events.publishes / events.subscribes: lists expected", None
    if len(pubs) > MAX_TYPES or len(subs) > MAX_TYPES:
        return f"events: at most {MAX_TYPES} types per list", None
    out_p, seen = [], set()
    for i, p in enumerate(pubs):
        where = f"events.publishes[{i}]"
        if not isinstance(p, dict):
            return f"{where}: object expected", None
        unknown = sorted(set(p) - {"type", "title", "subject", "schema", "semantic_id",
                                   "audience", "listed", "personal"})
        if unknown:
            return f"{where}: unknown key {', '.join(unknown)}", None
        err = own_type_error(p.get("type"), app_id)
        if err:
            return f"{where}.type: {err}", None
        if p["type"] in seen:
            return f"{where}.type: {p['type']} twice", None
        seen.add(p["type"])
        title = p.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            return f"{where}.title: required, at most 120 characters", None
        subj = p.get("subject", "")
        if not isinstance(subj, str) or len(subj) > 40:
            return f"{where}.subject: at most 40 characters", None
        sem = p.get("semantic_id")
        if sem is not None and (not isinstance(sem, str) or not sem or len(sem) > 300):
            return f"{where}.semantic_id: a string, at most 300 characters", None
        aud = p.get("audience", "tenant")
        if aud not in AUDIENCES:
            return f"{where}.audience: one of {', '.join(AUDIENCES)}", None
        listed = p.get("listed")
        if aud == "listed":
            if not isinstance(listed, list) or not listed or \
                    any(not isinstance(x, str) or not APP_ID_RE.match(x) for x in listed):
                return f"{where}.listed: the app ids that may be granted it", None
        elif listed is not None:
            return f"{where}.listed: only with audience 'listed'", None
        if "personal" in p and not isinstance(p["personal"], bool):
            return f"{where}.personal: true or false", None
        path = p.get("schema")
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
            return f"{where}.schema: a relative path to a JSON file in the package", None
        raw = read_file(path)
        if raw is None:
            return f"{where}.schema: {path} is not in the package", None
        if len(raw) > MAX_SCHEMA_BYTES:
            return f"{where}.schema: {path} is larger than {MAX_SCHEMA_BYTES // 1024} KB", None
        try:
            schema = json.loads(raw)
        except ValueError as e:
            return f"{where}.schema: {path} is not JSON ({e})", None
        err = schema_error(schema)
        if err:
            return f"{where}.schema: {path}: {err}", None
        if schema.get("type") not in (None, "object"):
            return f"{where}.schema: {path}: `data` is an object at the top", None
        out_p.append({"type": p["type"], "title": title.strip(), "subject": subj,
                      "semantic_id": sem, "audience": aud, "listed": listed or [],
                      "personal": p.get("personal") is True, "schema": schema})
    out_s, seen = [], set()
    for i, s in enumerate(subs):
        where = f"events.subscribes[{i}]"
        if not isinstance(s, dict):
            return f"{where}: object expected", None
        unknown = sorted(set(s) - {"type", "why", "addressed_only"})
        if unknown:
            return f"{where}: unknown key {', '.join(unknown)}", None
        err = type_error(s.get("type"))
        if err:
            return f"{where}.type: {err}", None
        if s["type"].startswith("oaap.") and s["type"] not in PLATFORM_TYPES:
            return (f"{where}.type: {s['type']} is not a platform type -- known are "
                    + ", ".join(sorted(PLATFORM_TYPES))), None
        if s["type"] in seen:
            return f"{where}.type: {s['type']} twice", None
        seen.add(s["type"])
        why = s.get("why")
        if not isinstance(why, str) or not why.strip() or len(why) > 200:
            return f"{where}.why: required, at most 200 characters", None
        if "addressed_only" in s and not isinstance(s["addressed_only"], bool):
            return f"{where}.addressed_only: true or false", None
        out_s.append({"type": s["type"], "why": why.strip(),
                      "addressed_only": s.get("addressed_only") is True})
    if endpoint and not out_s:
        return "events.endpoint: only an app that subscribes receives a push", None
    return "", {"publishes": out_p, "subscribes": out_s, "endpoint": endpoint or ""}

# --------------------------------------------------------------- envelope

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now_ms=None, rand=None):
    """A ULID: 48 bits of milliseconds, 80 random bits, Crockford base32."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    r = int.from_bytes(os.urandom(10) if rand is None else rand, "big")
    n = (ms << 80) | r
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def source(tenant, instance):
    return f"urn:oaap:tenant:{tenant}:instance:{instance}"


def dataschema(app_id, t):
    return f"urn:oaap:events:schema:{app_id}:{t}"


def find_type(catalogue_entry, t):
    for p in (catalogue_entry or {}).get("publishes") or []:
        if p.get("type") == t:
            return p
    return None


OAAPTO_RE = __import__("re").compile(r"^instance:([a-z0-9][a-z0-9-]{0,80})$")


def check_publish(body, entry):
    """(status, sentence, accepted) for a publish body against the
    publisher's catalogue entry. `accepted` = {type, subject, data, to}
    when status is 202 -- whatever else the body carried is dropped. `to`
    is the addressee's instance name for an addressed type (0.2), else
    None; whether the publisher may address it is the door's question
    (it needs the store's record)."""
    if not isinstance(body, dict):
        return 400, "the body is a CloudEvent: a JSON object", None
    t = body.get("type")
    if not isinstance(t, str) or not t:
        return 400, "type: required", None
    p = find_type(entry, t)
    if p is None:
        return 403, f"{t} is not a type this app declares under events.publishes", None
    to = None
    if p.get("audience") == "addressed":
        m = OAAPTO_RE.match(str(body.get("oaapto") or ""))
        if not m:
            return 400, (f"{t} is addressed: oaapto names the receiving instance, "
                         "'instance:<name>'"), None
        to = m.group(1)
    elif "oaapto" in body:
        return 400, f"{t} is not an addressed type; it takes no oaapto", None
    subj = body.get("subject")
    if not isinstance(subj, str) or not subj or len(subj) > SUBJECT_MAX or \
            any(ord(c) < 32 for c in subj):
        return 400, f"subject: 1-{SUBJECT_MAX} printable characters", None
    data = body.get("data", {})
    if not isinstance(data, dict):
        return 400, "data: a JSON object", None
    if len(json.dumps(data, separators=(",", ":")).encode()) > MAX_DATA_BYTES:
        return 413, f"data is larger than {MAX_DATA_BYTES // 1024} KB", None
    bad = validate(data, p["schema"])
    if bad:
        return 422, f"{bad[0]}: {bad[1]}", None
    return 202, "", {"type": t, "subject": subj, "data": data, "to": to}


def addressee_error(catalogue_entry_of_to, tenant, registry_inst_of_to, t):
    """'' when the named instance can receive addressed type `t`: it exists
    in the same tenant and declares `t` under events.subscribes. The record
    (did it give the publisher something to address) is checked apart."""
    if not registry_inst_of_to or registry_inst_of_to.get("tenant") != tenant:
        return "oaapto names no instance of this tenant"
    if not any(sb.get("type") == t for sb in (catalogue_entry_of_to or {}).get("subscribes") or []):
        return f"the addressed instance does not declare {t} under events.subscribes"
    return ""


def envelope(row, tenant, app_id, semantic=None):
    """The CloudEvent for a stored row ({id, type, source_instance, subject,
    time, data}). A platform publisher is stored as `platform:<name>`
    (0.4) -- a value no instance name can take -- and gets its own
    source, origin and schema URN."""
    tm = row["time"]
    if not isinstance(tm, str):
        tm = tm.astimezone(__import__("datetime").timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
    si = row["source_instance"]
    if si.startswith("platform:"):
        src, origin = f"urn:oaap:tenant:{tenant}:{si[9:]}", si
        schema = dataschema("platform", row["type"])
    else:
        src, origin = source(tenant, si), f"app:{app_id}"
        schema = dataschema(app_id, row["type"])
    ev = {"specversion": SPECVERSION, "id": row["id"], "type": row["type"],
          "source": src, "subject": row["subject"], "time": tm,
          "datacontenttype": "application/json", "dataschema": schema,
          "oaaptenant": tenant, "oaaporigin": origin,
          "data": row["data"]}
    if semantic or row.get("semantic"):
        ev["oaapsemantic"] = semantic or row.get("semantic")
    if row.get("oaapto"):
        ev["oaapto"] = "instance:" + row["oaapto"]
    return ev

# ------------------------------------------------- platform types (0.4)
#
# RFC-0127 stage 2: the platform publishes too. Its types live here, not in
# a manifest; an app subscribes to them like to any other. The identity
# types are addressed to the instance that invited the person (RFC-0126):
# the platform's own record (`invited_by`) is the relation, so they need no
# grant (§6.1's second case) and reach nobody else.

_PERSON = {"user_id": {"type": "string", "maxLength": 64},
           "ref": {"type": "string", "maxLength": 200}}
_BY = {"type": "string", "enum": ["app", "admin"]}

PLATFORM_TYPES = {
    "oaap.identity.person.activated.v1": {
        "publisher": "identity", "audience": "addressed", "subject": "user",
        "title": "Eingeladene Person hat sich zum ersten Mal angemeldet",
        "semantic_id": "urn:oaap:concept:identity:person:activated",
        "schema": {"type": "object", "properties": dict(_PERSON),
                   "required": ["user_id"], "additionalProperties": False}},
    "oaap.identity.person.locked.v1": {
        "publisher": "identity", "audience": "addressed", "subject": "user",
        "title": "Eingeladene Person wurde gesperrt",
        "semantic_id": "urn:oaap:concept:identity:person:locked",
        "schema": {"type": "object", "properties": dict(_PERSON, by=_BY),
                   "required": ["user_id", "by"], "additionalProperties": False}},
    "oaap.identity.person.unlocked.v1": {
        "publisher": "identity", "audience": "addressed", "subject": "user",
        "title": "Eingeladene Person wurde entsperrt",
        "semantic_id": "urn:oaap:concept:identity:person:unlocked",
        "schema": {"type": "object", "properties": dict(_PERSON, by=_BY),
                   "required": ["user_id", "by"], "additionalProperties": False}},
}
for _t, _p in PLATFORM_TYPES.items():
    _p.update(type=_t, listed=[], personal=False)


def check_platform_publish(body):
    """(status, sentence, accepted) for a platform publisher's body
    {publisher, type, subject, data, to}. Same shape as check_publish;
    whether `to` exists and wants the type is addressee_error's question."""
    if not isinstance(body, dict):
        return 400, "a JSON object", None
    t, who = body.get("type"), body.get("publisher")
    p = PLATFORM_TYPES.get(t)
    if p is None:
        return 403, f"{t!r} is not a platform type", None
    if who != p["publisher"]:
        return 403, f"{t} is published by {p['publisher']}, not {who!r}", None
    m = OAAPTO_RE.match("instance:" + str(body.get("to") or ""))
    if p["audience"] == "addressed" and not m:
        return 400, f"{t} is addressed: `to` names the instance", None
    subj = body.get("subject")
    if not isinstance(subj, str) or not subj or len(subj) > SUBJECT_MAX:
        return 400, f"subject: 1-{SUBJECT_MAX} characters", None
    data = body.get("data")
    if not isinstance(data, dict):
        return 400, "data: a JSON object", None
    bad = validate(data, p["schema"])
    if bad:
        return 422, f"{bad[0]}: {bad[1]}", None
    return 202, "", {"type": t, "subject": subj, "data": data,
                     "to": m.group(1) if m else None, "publisher": who}

# ----------------------------------------------------------------- grants


def grant_error(catalogue, tenant, instance, t):
    """'' when `instance` (of `tenant`) may be granted type `t`, else why.
    `catalogue` is the node's events-catalog.json."""
    insts = (catalogue or {}).get("instances") or {}
    me = insts.get(instance)
    if not me or me.get("tenant") != tenant:
        return f"'{instance}' is not an instance of this tenant with an events block"
    if not any(s.get("type") == t for s in me.get("subscribes") or []):
        return f"'{instance}' does not declare {t} under events.subscribes"
    if (PLATFORM_TYPES.get(t) or {}).get("audience") == "addressed":
        return f"{t} is addressed: it reaches only the instance it names, no grant needed"
    pubs =[(n, e, find_type(e, t)) for n, e in insts.items()
            if e.get("tenant") == tenant and find_type(e, t)]
    if not pubs:
        return f"no instance of this tenant publishes {t}"
    for _n, _e, p in pubs:
        if p["audience"] == "tenant":
            return ""
        if p["audience"] == "listed" and me.get("app_id") in p.get("listed", []):
            return ""
    if all(p["audience"] == "addressed" for _n, _e, p in pubs):
        return f"{t} is addressed: it reaches only the instance it names, no grant needed"
    return f"{t} is listed for other apps, not for '{me.get('app_id')}'"

# --------------------------------------------------- what reaches whom


def pending_conditions(grants, wanted, me):
    """(sql, args) of the WHERE part both the feed and the push use: the
    events of the tenant that `me` holds a live grant for (not addressed),
    plus those addressed to `me` of a type it declares (0.2), never its
    own. `grants` = [{type, subject_prefix}], `wanted` = its subscribed
    types. None when nothing can reach it."""
    conds, args = [], []
    for g in grants:
        if g.get("subject_prefix"):
            conds.append("(oaapto IS NULL AND type = %s AND starts_with(subject, %s))")
            args += [g["type"], g["subject_prefix"]]
        else:
            conds.append("(oaapto IS NULL AND type = %s)")
            args.append(g["type"])
    if wanted:
        conds.append("(oaapto = %s AND type = ANY(%s))")
        args += [me, list(wanted)]
    if not conds:
        return None
    return f"source_instance <> %s AND ({' OR '.join(conds)})", [me] + args

# ------------------------------------------------------------- the push


def sign(secret, ts, body):
    """X-OAAP-Signature, the twin webhooks' form: t=<unix>,v1=HMAC-SHA256
    over '<t>.<body>' with the instance's OAAP_EVENTS_SECRET."""
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret, header, body, now=None, tolerance=300):
    """What a receiving app does: True when the signature fits and is fresh."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((time.time() if now is None else now) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), f"t={ts},v1={parts.get('v1', '')}")


def backoff(failures):
    """Seconds to wait after the n-th failure in a row: 2^n, at most an hour."""
    return min(2 ** max(1, failures), PUSH_BACKOFF_MAX_S)
