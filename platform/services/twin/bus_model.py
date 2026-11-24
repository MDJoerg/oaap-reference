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
    if si.startswith("receiver:"):
        # 0.9: an event that came in through a client's receiver
        label = si[len("receiver:"):].split("/", 1)[0]
        src, origin = f"urn:oaap:tenant:{tenant}:{si}", f"client:{label}"
        schema = dataschema("receiver", row["type"])
    elif si.startswith("platform:"):
        src, origin = f"urn:oaap:tenant:{tenant}:{si[9:]}", si
        # the twin's events carry the origin as the twin names it (RFC-0127 §3)
        if si == "platform:twin" and isinstance(row.get("data"), dict)                 and row["data"].get("origin"):
            origin = row["data"]["origin"]
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
    ext = row.get("ext")
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except ValueError:
            ext = None
    if isinstance(ext, dict):
        for k in ("source", "type", "id"):
            if ext.get(k):
                ev["oaapext" + k] = ext[k]
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
# RFC-0127 D10, step 2.2: the twin in the envelope. Audience `tenant`: an
# app holds a grant from the tenant admin like for any other type. The relay
# copies a twin event into the store only while some instance of the tenant
# holds a live grant for its type (Jörg, 2026-11-24) -- the twin's own
# journal stays the record, the store is not a second copy of it.
_OBJ = {"type": {"type": "string", "maxLength": 120},
        "group": {"type": "string", "maxLength": 120},
        "origin": {"type": "string", "maxLength": 200}}
_CHANGES = ["group.written", "period.written", "object.renamed",
            "object.merged", "object.unmerged"]


def _twin_type(what, title, props, required=("type",)):
    return {"publisher": "twin", "audience": "tenant", "subject": "object",
            "title": title, "semantic_id": f"urn:oaap:concept:twin:object:{what}",
            "schema": {"type": "object", "properties": props,
                       "required": list(required), "additionalProperties": False}}


PLATFORM_TYPES.update({
    "oaap.twin.object.created.v1": _twin_type("created", "Objekt im Zwilling angelegt", dict(_OBJ)),
    "oaap.twin.object.changed.v1": _twin_type(
        "changed", "Objekt im Zwilling geändert",
        dict(_OBJ, change={"type": "string", "enum": _CHANGES}), ("type", "change")),
    "oaap.twin.object.retired.v1": _twin_type("retired", "Objekt im Zwilling stillgelegt", dict(_OBJ)),
    "oaap.twin.object.statuschanged.v1": _twin_type(
        "statuschanged", "Status eines Objekts im Zwilling gewechselt",
        dict(_OBJ, **{"from": {"type": ["string", "null"], "maxLength": 80},
                      "to": {"type": "string", "maxLength": 80}}), ("type", "to")),
})
for _t, _p in PLATFORM_TYPES.items():
    _p.update(type=_t, listed=[], personal=False)

_TWIN_KIND = {"object.created": "oaap.twin.object.created.v1",
              "object.retired": "oaap.twin.object.retired.v1",
              "status.changed": "oaap.twin.object.statuschanged.v1"}
_TWIN_KIND.update({k: "oaap.twin.object.changed.v1" for k in _CHANGES})


def twin_event(row, type_key, status=None):
    """(type, subject, data) for one row of the twin's `events` table with
    its object's type key, or None when it has no event: `type.created`,
    no object, an unknown kind. `status` = {from, to} for status.changed."""
    t = _TWIN_KIND.get(row.get("kind"))
    if t is None or not row.get("object_id") or not type_key:
        return None
    data = {"type": type_key}
    if row.get("group_key"):
        data["group"] = row["group_key"]
    if row.get("origin"):
        data["origin"] = row["origin"]
    if t.endswith(".changed.v1"):
        data["change"] = row["kind"]
    if t.endswith(".statuschanged.v1"):
        st = status or {}
        data["from"], data["to"] = st.get("from"), st.get("to") or ""
    return t, f"urn:oaap:obj:{row['object_id']}", data


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
    if (PLATFORM_TYPES.get(t) or {}).get("audience") == "tenant":
        return ""
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

# ------------------------------------- integration clients (0.8)
#
# RFC-0127 D7, stage 3: the way out. An integration client (`client:<label>`,
# oaap.data.twin §2.17) may be granted event types like an instance; its
# webhook in the CloudEvents format carries them, beside its twin changes.
# A grant to a client is the tenant admin's permission for that target, also
# for a `personal` type (Jörg, 2026-11-24) -- said when granting.

CLIENT_PREFIX = "client:"
CLIENT_RE = re.compile(r"^client:([a-z0-9][a-z0-9-]{0,38})$")


def client_grant_error(catalogue, tenant, clients, principal, t):
    """'' when integration client `principal` ('client:<label>') of `tenant`
    may be granted type `t`, else why. `clients` = twin-clients.json's
    {label: {tenant, ...}}."""
    m = CLIENT_RE.match(principal or "")
    c = (clients or {}).get(m.group(1)) if m else None
    if not c or c.get("tenant") != tenant:
        return f"'{principal}' is not an integration client of this tenant"
    plat = PLATFORM_TYPES.get(t)
    if plat and plat["publisher"] == "twin":
        return (f"{t}: a client receives the twin's changes through its read rights "
                "-- in its webhook (format cloudevents), no grant")
    if plat:
        if plat["audience"] == "addressed":
            return f"{t} is addressed to the inviting app; it does not leave the tenant"
        return ""
    pubs = [find_type(e, t) for n, e in ((catalogue or {}).get("instances") or {}).items()
            if e.get("tenant") == tenant and find_type(e, t)]
    if not pubs:
        return f"no instance of this tenant publishes {t}"
    if any(p["audience"] == "tenant" for p in pubs):
        return ""
    if all(p["audience"] == "addressed" for p in pubs):
        return f"{t} is addressed: it reaches only the instance it names"
    return f"{t} is listed for named apps only"


def client_conditions(grants, me):
    """(sql, args) of what a client's webhook carries from the store: the
    events of its live grants **from the moment of each grant on** -- never
    one stored before the tenant admin said yes (measured 2026-11-24: a grant
    opened what was already in the store, including events from a time
    without any grant). `grants` = [{type, subject_prefix, granted_at}].
    None when it holds none."""
    conds, args = [], []
    for g in grants:
        c, a = grant_condition(g)
        conds.append(c)
        args += a
    if not conds:
        return None
    return f"source_instance <> %s AND ({' OR '.join(conds)})", [me] + args


def grant_condition(g):
    """(sql, args) of what one live grant opens -- the one rule for every
    receiver, instance or client (RFC-0127 D13, Jörg 2026-11-24): the
    granted type, not addressed, under its subject prefix, **from the moment
    of the grant on** (`time >= granted_at`). The store's past stays closed;
    it is read in the twin. A grant without `granted_at` is a fault, not an
    open door: KeyError."""
    c = "(oaapto IS NULL AND type = %s AND time >= %s"
    args = [g["type"], g["granted_at"]]
    if g.get("subject_prefix"):
        c += " AND starts_with(subject, %s)"
        args.append(g["subject_prefix"])
    return c + ")", args


def is_personal(catalogue, tenant, t):
    for e in ((catalogue or {}).get("instances") or {}).values():
        p = find_type(e, t) if e.get("tenant") == tenant else None
        if p and p.get("personal"):
            return True
    return bool((PLATFORM_TYPES.get(t) or {}).get("personal"))


def client_types(catalogue, tenant):
    """The types a client of `tenant` could be granted at all (sorted)."""
    out = set()
    for e in ((catalogue or {}).get("instances") or {}).values():
        if e.get("tenant") == tenant:
            out |= {p["type"] for p in e.get("publishes") or [] if p.get("audience") == "tenant"}
    out |= {t for t, p in PLATFORM_TYPES.items()
            if p["audience"] == "tenant" and p["publisher"] != "twin"}
    return sorted(out)


def twin_change_event(change, tenant):
    """The CloudEvent of one entry of the twin's change feed (twin_read.
    changes: {id, at, kind, object, type, group, origin, status?}), or None
    for a kind with no event. `id` is the twin's event id, unique per this
    source."""
    row = {"kind": change.get("kind"), "object_id": (change.get("object") or "").rsplit(":", 1)[-1],
           "group_key": change.get("group"), "origin": change.get("origin")}
    ev = twin_event(row, change.get("type"), change.get("status"))
    if ev is None:
        return None
    t, subj, data = ev
    out = {"specversion": SPECVERSION, "id": f"twin-{change['id']}", "type": t,
           "source": f"urn:oaap:tenant:{tenant}:twin", "subject": subj,
           "time": change.get("at"), "datacontenttype": "application/json",
           "dataschema": dataschema("platform", t), "oaaptenant": tenant,
           "oaaporigin": change.get("origin") or "platform:twin", "data": data}
    if change.get("merged_from"):
        out["oaapmergedfrom"] = change["merged_from"]
    return out

# ------------------------------------------- receivers in (0.9)
#
# RFC-0127 D8, stage 3.2: a client's receiver may declare event types
# `ext.<client>.<entity>.<what>.v<major>`; a CloudEvent posted to it (SAP's
# form: structured, batched or binary) becomes an event of the declared type
# its sender's type maps to (`from`; Jörg 2026-11-24: our own type, the
# sender's kept as `oaapexttype`, its source as `oaapextsource`, its id as
# `oaapextid`). Audience `tenant`: an instance or a client receives it after
# the tenant admin's grant.

RECEIVER_TYPES_MAX = 20
EXT_FROM_MAX = 200


def ext_prefix(label):
    return "ext." + label.replace("-", "") + "."


def receiver_events_error(ev, label):
    """'' when a receiver's `events` declaration fits, else the sentence.
    ev = {"types": [{type, title, from?, schema?, personal?}]}."""
    if not isinstance(ev, dict) or set(ev) - {"types"}:
        return "'events' is {types: [{type, title, from?, schema?, personal?}]}"
    ts = ev.get("types")
    if not isinstance(ts, list) or not ts or len(ts) > RECEIVER_TYPES_MAX:
        return f"events.types: 1..{RECEIVER_TYPES_MAX} entries"
    seen, froms = set(), set()
    for i, t in enumerate(ts):
        w = f"events.types[{i}]"
        if not isinstance(t, dict) or set(t) - {"type", "title", "from", "schema", "personal"}:
            return f"{w}: {{type, title, from?, schema?, personal?}}"
        err = type_error(t.get("type"))
        if err:
            return f"{w}.type: {err}"
        if not t["type"].startswith(ext_prefix(label)):
            return f"{w}.type: a receiver of client '{label}' declares types under '{ext_prefix(label)}…'"
        if t["type"] in seen:
            return f"{w}.type: {t['type']} twice"
        seen.add(t["type"])
        if not isinstance(t.get("title"), str) or not t["title"].strip() or len(t["title"]) > 120:
            return f"{w}.title: required, at most 120 characters"
        f = t.get("from", t["type"])
        if not isinstance(f, str) or not f or len(f) > EXT_FROM_MAX or any(ord(c) < 33 for c in f):
            return f"{w}.from: the sender's type, 1..{EXT_FROM_MAX} visible characters"
        if f in froms:
            return f"{w}.from: {f} maps to two types"
        froms.add(f)
        if "schema" in t:
            if not isinstance(t["schema"], dict) or t["schema"].get("type") not in (None, "object"):
                return f"{w}.schema: an object schema"
            err = schema_error(t["schema"])
            if err:
                return f"{w}.schema: {err}"
        if "personal" in t and not isinstance(t["personal"], bool):
            return f"{w}.personal: true or false"
    return ""


def with_receivers(catalogue, tenant, receivers):
    """The catalogue plus one entry per receiver with declared types --
    `receiver:<label>/<name>` publishing them with audience `tenant` -- so
    grants, the overview and the export treat them like any publisher.
    receivers = [{principal: 'client:<label>', name, events}]."""
    out = {"instances": dict((catalogue or {}).get("instances") or {})}
    for r in receivers or []:
        ev = r.get("events")
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except ValueError:
                ev = None
        if not isinstance(ev, dict):
            continue
        label = str(r.get("principal", "")).split(":", 1)[-1]
        pubs = [{"type": t["type"], "title": t.get("title", t["type"]), "subject": "external",
                 "semantic_id": None, "audience": "tenant", "listed": [],
                 "personal": t.get("personal") is True,
                 "schema": t.get("schema") or {"type": "object"}, "from": t.get("from", t["type"])}
                for t in ev.get("types") or [] if isinstance(t, dict) and t.get("type")]
        out["instances"][f"receiver:{label}/{r.get('name')}"] = {
            "tenant": tenant, "app_id": "receiver", "version": "", "publishes": pubs,
            "subscribes": [], "endpoint": ""}
    return out


def parse_cloudevents(raw, headers):
    """[CloudEvent dict] from a request: `application/cloudevents-batch+json`
    (a JSON array), `application/cloudevents+json` or a JSON body with
    `specversion` (structured), or the binary mode (`ce-*` headers, the body is
    `data`). (None, sentence) when it is none of them."""
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if headers.get("ce-specversion") or headers.get("ce-type"):
        ev = {k: headers.get("ce-" + k) for k in ("specversion", "id", "source", "type",
                                                  "subject", "time", "dataschema")
              if headers.get("ce-" + k)}
        try:
            ev["data"] = json.loads(raw) if raw else None
        except ValueError:
            return None, "binary mode: the body (data) is JSON"
        return [ev], ""
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "the body is not JSON"
    if ctype == "application/cloudevents-batch+json":
        if not isinstance(doc, list) or not doc or len(doc) > 50:
            return None, "a batch is a JSON array of 1..50 CloudEvents"
        return doc, ""
    if isinstance(doc, dict) and "specversion" in doc:
        return [doc], ""
    return None, "not a CloudEvent: structured (specversion in the body), batched or binary (ce-* headers)"


def map_cloudevent(ev, declared):
    """(accepted, sentence) for one incoming CloudEvent against a receiver's
    declared types. accepted = {type, subject, data, ext}."""
    if not isinstance(ev, dict):
        return None, "a CloudEvent is a JSON object"
    if ev.get("specversion") != "1.0":
        return None, "specversion: 1.0"
    for k in ("id", "source", "type"):
        v = ev.get(k)
        if not isinstance(v, str) or not v or len(v) > 400 or any(ord(c) < 32 for c in v):
            return None, f"{k}: required, a printable string"
    t = next((d for d in declared if d.get("from", d["type"]) == ev["type"]), None)
    if t is None:
        return None, f"{ev['type']} is not a type this receiver declares (events.types[].from)"
    data = ev.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        data = {"value": data}
    if len(json.dumps(data, separators=(",", ":")).encode()) > MAX_DATA_BYTES:
        return None, f"data is larger than {MAX_DATA_BYTES // 1024} KB"
    if t.get("schema"):
        bad = validate(data, t["schema"])
        if bad:
            return None, f"{bad[0]}: {bad[1]}"
    subj = ev.get("subject") if isinstance(ev.get("subject"), str) and ev.get("subject") else ev["id"]
    return {"type": t["type"], "subject": subj[:SUBJECT_MAX], "data": data,
            "ext": {"source": ev["source"], "type": ev["type"], "id": ev["id"]}}, ""

# ------------------------------------------- the portal page (0.6)


def overview(catalogue, registry, tenant, grants, deliveries, clients=None):
    """What the portal page "Ereignisse" shows for one tenant (pure;
    tested): every instance with an events block, what it publishes, what
    it wants and in which state, its push; and the platform's types.
    `grants` = [{instance, type, subject_prefix, granted_by, granted_at}]
    (live ones), `deliveries` = {instance: {delivered, failures,
    stopped_at, last_error, last_ok_at}}."""
    live = {(g["instance"], g["type"]): g for g in grants}
    insts = []
    recv = []
    for name, e in sorted(((catalogue or {}).get("instances") or {}).items()):
        if e.get("tenant") != tenant:
            continue
        if name.startswith("receiver:"):
            recv += [{"receiver": name[len("receiver:"):], "type": p["type"], "title": p["title"],
                      "from": p.get("from", p["type"]), "personal": p.get("personal", False)}
                     for p in e.get("publishes") or []]
            continue
        subs = []
        for sb in e.get("subscribes") or []:
            t, g = sb["type"], live.get((name, sb["type"]))
            plat = PLATFORM_TYPES.get(t) or {}
            if g:
                state = "granted"
            elif sb.get("addressed_only"):
                state = "addressed"
            elif plat.get("audience") == "addressed":
                state = "addressed_platform"
            else:
                state = "waiting"
            subs.append({"type": t, "why": sb.get("why", ""), "state": state,
                         "grantable": state == "waiting" and not grant_error(catalogue, tenant, name, t),
                         "refusal": grant_error(catalogue, tenant, name, t) if state == "waiting" else "",
                         "publisher": plat.get("publisher", ""),
                         "subject_prefix": (g or {}).get("subject_prefix") or "",
                         "granted_by": (g or {}).get("granted_by") or "",
                         "granted_at": (g or {}).get("granted_at") or ""})
        reg = ((registry or {}).get("instances") or {}).get(name) or {}
        d = (deliveries or {}).get(name)
        push = None
        if e.get("endpoint"):
            push = {"endpoint": e["endpoint"], "started": d is not None}
            if d is not None:
                push.update({k: d.get(k) for k in ("delivered", "failures", "stopped_at",
                                                     "last_error", "last_ok_at")})
        insts.append({"instance": name, "app_id": e.get("app_id", ""),
                      "version": e.get("version") or reg.get("version", ""),
                      "publishes": [{k: p.get(k) for k in ("type", "title", "audience", "personal",
                                                           "semantic_id")}
                                    for p in e.get("publishes") or []],
                      "subscribes": subs, "push": push})
    plat = [{"type": t, "title": p["title"], "publisher": p["publisher"],
             "audience": p["audience"], "semantic_id": p["semantic_id"]}
            for t, p in sorted(PLATFORM_TYPES.items())]
    # 0.8: the tenant's integration clients -- what leaves the tenant
    cls, offer = [], client_types(catalogue, tenant)
    for label, c in sorted((clients or {}).items()):
        if c.get("tenant") != tenant:
            continue
        p = CLIENT_PREFIX + label
        held = [{"type": g["type"], "subject_prefix": g.get("subject_prefix") or "",
                 "granted_by": g.get("granted_by") or "", "personal": is_personal(catalogue, tenant, g["type"])}
                for g in grants if g["instance"] == p]
        have = {h["type"] for h in held}
        cls.append({"client": p, "label": label, "grants": held,
                    "grantable": [{"type": t, "personal": is_personal(catalogue, tenant, t)}
                                  for t in offer if t not in have]})
    return {"instances": insts, "platform_types": plat, "clients": cls, "receiver_types": recv}

# ------------------------------------------------ AsyncAPI export (0.7)
#
# RFC-0127 D9: the catalogue outward as AsyncAPI 3.1.0 (released
# 2026-01-31, asyncapi.com), per app and per tenant. One channel per event
# type (its address is the type: a logical channel, the platform routes);
# one message per type, its payload the CloudEvents envelope of §2.1 with
# the type's schema as `data`; operations from the app's side -- `send` for
# what it publishes (through its door), `receive` for what it wants (pushed
# to its endpoint or read from its feed). OAAP's own facts ride as `x-oaap-*`
# specification extensions.

ASYNCAPI_VERSION = "3.1.0"
_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")


def _akey(t):
    return _KEY_RE.sub("_", t)


def _envelope_schema(t, data_schema, addressed):
    props = {"specversion": {"const": SPECVERSION},
             "id": {"type": "string", "description": "ULID, set by the platform"},
             "source": {"type": "string", "description": "urn:oaap:tenant:<id>:<publisher>, set by the platform"},
             "type": {"const": t},
             "subject": {"type": "string", "maxLength": SUBJECT_MAX},
             "time": {"type": "string", "format": "date-time"},
             "datacontenttype": {"const": "application/json"},
             "dataschema": {"type": "string"},
             "oaaptenant": {"type": "string"},
             "oaaporigin": {"type": "string"},
             "oaapsemantic": {"type": "string"},
             "data": data_schema or {"type": "object"}}
    req = ["specversion", "id", "source", "type", "time", "data"]
    if addressed:
        props["oaapto"] = {"type": "string", "pattern": "^instance:[a-z0-9][a-z0-9-]{0,80}$"}
        req.append("oaapto")
    return {"type": "object", "properties": props, "required": req}


def _type_index(catalogue, tenant):
    """{type: (declaration, publisher)} of every type published in the
    tenant: the platform's and every instance's."""
    idx = {t: (p, "platform:" + p["publisher"]) for t, p in PLATFORM_TYPES.items()}
    for name, e in sorted(((catalogue or {}).get("instances") or {}).items()):
        if e.get("tenant") == tenant:
            for p in e.get("publishes") or []:
                idx.setdefault(p["type"], (p, name if name.startswith("receiver:")
                                           else "instance:" + name))
    return idx


def _message(t, decl, publisher):
    decl = decl or {}
    m = {"name": t, "title": decl.get("title") or t,
         "contentType": "application/cloudevents+json",
         "payload": _envelope_schema(t, decl.get("schema"), decl.get("audience") == "addressed"),
         "x-oaap-publisher": publisher or "unknown in this tenant",
         "x-oaap-audience": decl.get("audience", "")}
    if decl.get("semantic_id"):
        m["x-oaap-semantic-id"] = decl["semantic_id"]
    if decl.get("personal"):
        m["x-oaap-personal"] = True
    return m


def asyncapi(catalogue, registry, tenant, instance=None, tenant_label=""):
    """The AsyncAPI 3.1.0 document of one instance (`instance` given) or of
    the whole tenant, as a dict (pure; tested). None when the instance is
    not one of the tenant's with an events block."""
    insts = {n: e for n, e in ((catalogue or {}).get("instances") or {}).items()
             if e.get("tenant") == tenant}
    if instance is not None and instance not in insts:
        return None
    idx = _type_index(catalogue, tenant)
    who = {instance: insts[instance]} if instance else insts
    channels, operations, messages = {}, {}, {}

    def channel(t):
        k = _akey(t)
        if k not in channels:
            decl, pub = idx.get(t, (None, None))
            messages[k] = _message(t, decl, pub)
            channels[k] = {"address": t, "title": (decl or {}).get("title") or t,
                           "messages": {k: {"$ref": f"#/components/messages/{k}"}}}
        return k

    for name, e in sorted(who.items()):
        pre = "" if instance else _akey(name) + "__"
        for p in e.get("publishes") or []:
            k = channel(p["type"])
            operations[f"{pre}send__{k}"] = {
                "action": "send", "channel": {"$ref": f"#/channels/{k}"},
                "title": f"{name} veröffentlicht",
                "description": "POST to the instance's own door /platform/events/ "
                               "with OAAP_EVENTS_KEY; the platform sets the envelope.",
                "messages": [{"$ref": f"#/channels/{k}/messages/{k}"}]}
        for sb in e.get("subscribes") or []:
            k = channel(sb["type"])
            op = {"action": "receive", "channel": {"$ref": f"#/channels/{k}"},
                  "title": f"{name} empfängt", "summary": sb.get("why", ""),
                  "description": ((f"Pushed to {e['endpoint']}, signed X-OAAP-Signature "
                                   "(HMAC-SHA256, OAAP_EVENTS_SECRET); ")
                                  if e.get("endpoint") else "")
                  + "readable from /platform/events/feed. Delivered only after a grant, "
                    "unless addressed to this instance.",
                  "messages": [{"$ref": f"#/channels/{k}/messages/{k}"}]}
            if sb.get("addressed_only"):
                op["x-oaap-addressed-only"] = True
            operations[f"{pre}receive__{k}"] = op
    e = insts.get(instance) or {}
    reg = ((registry or {}).get("instances") or {}).get(instance or "") or {}
    title = (f"OAAP-Ereignisse der Instanz {instance}" if instance
             else f"OAAP-Ereignisse des Mandanten {tenant_label or tenant}")
    doc = {"asyncapi": ASYNCAPI_VERSION,
           "info": {"title": title,
                    "version": (e.get("version") or reg.get("version") or "0") if instance else "0.7",
                    "description": "Generated by the platform from the manifests' `events` blocks "
                                   "and the platform catalogue (oaap.events.bus, RFC-0127). "
                                   "Every message is a CloudEvent 1.0 in the structured JSON form."},
           "defaultContentType": "application/cloudevents+json",
           "servers": {"platform": {
               "host": "{site}", "protocol": "http", "pathname": "/platform/events",
               "description": "The instance's own events door (OAAP_EVENTS_URL); "
                              "a push arrives at the instance's declared endpoint.",
               "variables": {"site": {"default": "gateway",
                                      "description": "host:port of OAAP_EVENTS_URL"}}}},
           "channels": channels, "operations": operations,
           "components": {"messages": messages},
           "x-oaap-tenant": tenant}
    if instance:
        doc["x-oaap-instance"] = instance
        doc["x-oaap-app"] = e.get("app_id", "")
    return doc

# --------------------------------------------------- what reaches whom


def pending_conditions(grants, wanted, me):
    """(sql, args) of the WHERE part both the feed and the push use: the
    events of the tenant that `me` holds a live grant for (not addressed),
    plus those addressed to `me` of a type it declares (0.2), never its
    own. A grant opens its type from `granted_at` on (grant_condition, D13).
    `grants` = [{type, subject_prefix, granted_at}], `wanted` = its
    subscribed types. None when nothing can reach it."""
    conds, args = [], []
    for g in grants:
        c, a = grant_condition(g)
        conds.append(c)
        args += a
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
