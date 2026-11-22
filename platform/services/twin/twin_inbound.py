"""Inbound webhooks: receivers with a mapping (RFC-0120 D13, oaap.data.twin 0.14 §2.26).

A system that only speaks webhooks posts to `/twin/v1/inbound/<client>/<name>`,
signed with the receiver's secret. The receiver's **mapping** (JSON, no code,
nothing fetched) turns the body into the operations of a batch (§2.24), which
runs through `twin_sync.Door.batch` -- the client's rights, the client's
origin, one transaction, the same write rule as the client's own writes.

- A **path** is a dotted string into the current item (`cost.amount`,
  `tags.0`); `$.` at its start reads from the body's root. `{"value": x}` is a
  constant.
- `type` and group keys are literal: what a mapping may write is visible in
  the mapping, never taken from a payload.
- The delivery id (a path, `{"header": ...}`, or the hash of the body) names
  the batch: the same delivery again writes nothing.
"""
import hashlib
import hmac
import json
import re
import secrets
import time

import twin_sync as tsy
import twin_write as tw

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SCHEMES = ("oaap", "hmac-sha256", "token")
RECEIVERS_MAX = 5
MAPPING_MAX = 16 * 1024
RULES_MAX = 20
BODY_MAX = 1024 * 1024
TOLERANCE_S = 300


def _refuse(msg, code=400):
    raise tw.Refused(msg, code)


# ------------------------------------------------------------------ the mapping
def _value_spec(v, where, header_ok=False):
    """A path, {"value": x} or (only for the id) {"header": name}."""
    if isinstance(v, str):
        if not v.strip() or len(v) > 200:
            _refuse(f"{where}: a path is 1..200 characters")
        return
    if isinstance(v, dict) and set(v) == {"value"}:
        if isinstance(v["value"], (dict, list)):
            _refuse(f"{where}: a constant is a string, number, true, false or null")
        return
    if header_ok and isinstance(v, dict) and set(v) == {"header"} \
            and HEADER_RE.match(str(v["header"])):
        return
    _refuse(f"{where}: a path (string) or {{\"value\": constant}}"
            + (" or {\"header\": name}" if header_ok else ""))


def check_mapping(m, writable):
    """The mapping, checked: its shape, its keys, and that every type it
    writes is one the client writes now. Raises Refused with a sentence."""
    if not isinstance(m, dict):
        _refuse("'mapping' is an object {id?, items?, rules}")
    if len(json.dumps(m)) > MAPPING_MAX:
        _refuse(f"a mapping is at most {MAPPING_MAX // 1024} KB")
    extra = set(m) - {"id", "items", "rules"}
    if extra:
        _refuse(f"a mapping has id, items, rules -- not '{sorted(extra)[0]}'")
    if "id" in m:
        _value_spec(m["id"], "id", header_ok=True)
        if isinstance(m["id"], dict) and "value" in m["id"]:
            _refuse("id: a constant would make every delivery the same one")
    if "items" in m and (not isinstance(m["items"], str) or not m["items"].strip()):
        _refuse("items: a path to the list the body carries")
    rules = m.get("rules")
    if not isinstance(rules, list) or not rules:
        _refuse("'rules' is a list of at least one rule")
    if len(rules) > RULES_MAX:
        _refuse(f"a mapping has at most {RULES_MAX} rules")
    for i, r in enumerate(rules, 1):
        w = f"rule {i}"
        if not isinstance(r, dict):
            _refuse(f"{w}: an object {{when?, upsert | retire}}")
        acts = [k for k in ("upsert", "retire", "status") if k in r]
        if len(acts) != 1 or set(r) - {"when", "upsert", "retire", "status"}:
            _refuse(f"{w}: exactly one of upsert, retire, status, and an optional when")
        when = r.get("when", {})
        if not isinstance(when, dict):
            _refuse(f"{w}: 'when' maps paths to a value or a list of values")
        for p, v in when.items():
            _value_spec(p, f"{w} when")
            if isinstance(v, dict) or (isinstance(v, list)
                                       and any(isinstance(x, (dict, list)) for x in v)):
                _refuse(f"{w} when '{p}': a value or a list of values")
        if "status" in r:
            a = r["status"]
            if not isinstance(a, dict) or "key" not in a or "to" not in a                     or set(a) - {"key", "to", "reason"}:
                _refuse(f"{w} status: {{key, to, reason?}}")
            for f in ("key", "to", "reason"):
                if f in a:
                    _value_spec(a[f], f"{w} status {f}")
            continue
        if "retire" in r:
            a = r["retire"]
            if not isinstance(a, dict) or "key" not in a or set(a) - {"key", "reason"}:
                _refuse(f"{w} retire: {{key, reason?}}")
            _value_spec(a["key"], f"{w} retire key")
            if "reason" in a:
                _value_spec(a["reason"], f"{w} retire reason")
            continue
        a = r["upsert"]
        if not isinstance(a, dict) or "key" not in a \
                or set(a) - {"key", "type", "title", "groups"}:
            _refuse(f"{w} upsert: {{key, type?, title?, groups?}}")
        _value_spec(a["key"], f"{w} upsert key")
        if "title" in a:
            _value_spec(a["title"], f"{w} upsert title")
        if "type" in a:
            t = a["type"]
            if not isinstance(t, str) or not t:
                _refuse(f"{w} upsert type: the type's key, literal")
            if t not in writable:
                _refuse(f"{w} upsert type: this client does not write '{t}' -- it writes: "
                        + (", ".join(sorted(writable)) or "nothing"), 400)
        groups = a.get("groups", {})
        if not isinstance(groups, dict):
            _refuse(f"{w} upsert groups: {{group key: {{attributes, relations}}}}")
        for g, content in groups.items():
            if not tsy.GROUP_KEY_RE.match(g):
                _refuse(f"{w}: '{g}' is not a group key (literal, namespace.name)")
            if not isinstance(content, dict) or set(content) - {"attributes", "relations"}:
                _refuse(f"{w} group '{g}': {{attributes?, relations?}}")
            attrs = content.get("attributes", {})
            if not isinstance(attrs, dict):
                _refuse(f"{w} group '{g}' attributes: {{attribute key: path or constant}}")
            for k, v in attrs.items():
                if not KEY_RE.match(k):
                    _refuse(f"{w} group '{g}': '{k}' is not an attribute key")
                _value_spec(v, f"{w} group '{g}' attribute '{k}'")
            rels = content.get("relations", [])
            if not isinstance(rels, list):
                _refuse(f"{w} group '{g}' relations: a list of {{key, target_key}}")
            for rel in rels:
                if not isinstance(rel, dict) or set(rel) != {"key", "target_key"} \
                        or not KEY_RE.match(str(rel["key"])):
                    _refuse(f"{w} group '{g}': a relation is {{key, target_key}}")
                _value_spec(rel["target_key"], f"{w} group '{g}' relation '{rel['key']}'")


_MISSING = object()


def _walk(node, path):
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part, _MISSING)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
        if node is _MISSING:
            return _MISSING
    return node


def _get(spec, item, root, headers=None):
    if isinstance(spec, dict):
        if "value" in spec:
            return spec["value"]
        return (headers or {}).get(spec["header"], _MISSING) if headers is not None else _MISSING
    if spec.startswith("$."):
        return _walk(root, spec[2:])
    return _walk(item, spec)


def _holds(when, item, root):
    for p, want in when.items():
        got = _get(p, item, root)
        if got is _MISSING:
            return False
        options = want if isinstance(want, list) else [want]
        if got not in options and str(got) not in [str(x) for x in options]:
            return False
    return True


def _key(spec, item, root, where):
    v = _get(spec, item, root)
    if v is _MISSING or v is None or isinstance(v, (dict, list)) or str(v).strip() == "":
        _refuse(f"{where}: no key at '{spec if isinstance(spec, str) else spec}'", 422)
    return str(v)


def operations(mapping, root):
    """(operations, skipped) -- the batch a body makes under a mapping."""
    if "items" in mapping:
        items = _get(mapping["items"] if mapping["items"].startswith("$.")
                     else "$." + mapping["items"], None, root)
        if items is _MISSING:
            _refuse(f"the body has nothing at '{mapping['items']}'", 422)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            _refuse(f"'{mapping['items']}' is not a list or an object", 422)
    else:
        items = [root]
    ops, skipped = [], 0
    for n, item in enumerate(items, 1):
        rule = next((r for r in mapping["rules"] if _holds(r.get("when", {}), item, root)), None)
        if rule is None:
            skipped += 1
            continue
        where = f"item {n}"
        if "status" in rule:
            a = rule["status"]
            to = _get(a["to"], item, root)
            if to is _MISSING or to is None or isinstance(to, (dict, list)):
                _refuse(f"{where}: no status at '{a['to']}'", 422)
            op = {"op": "status", "key": _key(a["key"], item, root, where), "to": str(to)}
            if "reason" in a:
                r = _get(a["reason"], item, root)
                if r is not _MISSING and r is not None:
                    op["reason"] = str(r)
            ops.append(op)
            continue
        if "retire" in rule:
            a = rule["retire"]
            op = {"op": "retire", "key": _key(a["key"], item, root, where)}
            if "reason" in a:
                r = _get(a["reason"], item, root)
                if r is not _MISSING and r is not None:
                    op["reason"] = str(r)
            ops.append(op)
            continue
        a = rule["upsert"]
        op = {"op": "upsert", "key": _key(a["key"], item, root, where)}
        if "type" in a:
            op["type"] = a["type"]
        if "title" in a:
            t = _get(a["title"], item, root)
            if t is not _MISSING and t is not None:
                op["title"] = str(t)
        groups = {}
        for g, content in (a.get("groups") or {}).items():
            attrs = {}
            for k, spec in (content.get("attributes") or {}).items():
                v = _get(spec, item, root)
                if v is not _MISSING:
                    attrs[k] = v
            rels = []
            for rel in content.get("relations") or []:
                v = _get(rel["target_key"], item, root)
                if v is not _MISSING and v is not None and str(v).strip():
                    rels.append({"key": rel["key"], "target_key": str(v)})
            groups[g] = {"attributes": attrs, "relations": rels}
        op["groups"] = groups
        ops.append(op)
    if len(ops) > tsy.BATCH_MAX:
        _refuse(f"the delivery makes {len(ops)} operations -- a batch holds at most "
                f"{tsy.BATCH_MAX}", 422)
    return ops, skipped


def delivery_id(mapping, raw, root, headers):
    """The batch id: in:<receiver>:<hash>, from the declared id or the body."""
    spec = mapping.get("id")
    v = _MISSING
    if spec is not None:
        v = _get(spec, root, root, headers)
    src = raw if v is _MISSING or v is None or str(v) == "" else ("id:" + str(v)).encode()
    return hashlib.sha256(src).hexdigest()[:40]


# ------------------------------------------------------------------ the signature
def check_signature(rec, headers, raw, now=None):
    """True when the request carries the receiver's signature. `headers`
    answers .get(name) case-insensitively (Flask's do)."""
    secret = rec["secret"]
    scheme = rec["scheme"]
    if scheme == "oaap":
        got = headers.get("X-OAAP-Signature") or ""
        try:
            parts = dict(p.split("=", 1) for p in got.split(","))
            ts = int(parts["t"])
        except (ValueError, KeyError):
            return False
        if abs((now or time.time()) - ts) > TOLERANCE_S:
            return False
        mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"t={ts},v1={mac}", got)
    got = headers.get(rec["header"] or "") or ""
    if scheme == "hmac-sha256":
        mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest((rec["prefix"] or "") + mac, got)
    if scheme == "token":
        return hmac.compare_digest(secret, got)
    return False


def _signature(sig):
    """(scheme, header, prefix) from the registration's 'signature'."""
    sig = sig if sig is not None else {}
    if not isinstance(sig, dict) or set(sig) - {"scheme", "header", "prefix"}:
        _refuse("'signature' is {scheme: oaap | hmac-sha256 | token, header?, prefix?}")
    scheme = sig.get("scheme") or "oaap"
    if scheme not in SCHEMES:
        _refuse(f"signature scheme is one of {', '.join(SCHEMES)}")
    if scheme == "oaap":
        if sig.get("header") or sig.get("prefix"):
            _refuse("the scheme oaap uses X-OAAP-Signature -- no header or prefix")
        return scheme, None, None
    header = sig.get("header") or ("X-Signature-256" if scheme == "hmac-sha256" else "")
    if not HEADER_RE.match(header):
        _refuse(f"the scheme {scheme} names its 'header'")
    prefix = sig.get("prefix") or ""
    if scheme == "token" and prefix:
        _refuse("the scheme token has no prefix")
    if len(prefix) > 20 or any(ord(c) < 33 for c in prefix):
        _refuse("a prefix is up to 20 visible characters, e.g. 'sha256='")
    return scheme, header, prefix


# ------------------------------------------------------------------ records
def receiver_json(r, label=None):
    state = "removed" if r.get("removed_at") else ("failing" if r.get("last_error") and (
        not r.get("last_ok_at") or (r.get("last_error_at") and r["last_error_at"] > r["last_ok_at"]))
        else "active")
    out = {"name": r["name"], "client": r["principal"].split(":", 1)[1], "state": state,
           "signature": {"scheme": r["scheme"]} | ({"header": r["header"]} if r["header"] else {})
           | ({"prefix": r["prefix"]} if r["prefix"] else {}),
           "mapping": r["mapping"], "received": r["received"], "written": r["written"],
           "refused": r["refused"],
           "last_ok_at": r["last_ok_at"].isoformat() if r.get("last_ok_at") else None,
           "last_error": r.get("last_error"),
           "last_error_at": r["last_error_at"].isoformat() if r.get("last_error_at") else None}
    out["url"] = f"/twin/v1/inbound/{out['client']}/{r['name']}"
    return out


def receivers_of(conn, principal=None):
    with conn.cursor() as cur:
        if principal:
            cur.execute("SELECT * FROM receivers WHERE removed_at IS NULL AND principal = %s "
                        "ORDER BY id", (principal,))
        else:
            cur.execute("SELECT * FROM receivers WHERE removed_at IS NULL ORDER BY id")
        return [receiver_json(r) for r in cur.fetchall()]


def find(conn, principal, name):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM receivers WHERE removed_at IS NULL AND principal = %s "
                    "AND name = %s", (principal, name))
        return cur.fetchone()


def add(conn, principal, writable, body):
    name = body.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        _refuse("'name' is [a-z0-9][a-z0-9-]{0,38} -- it is part of the receiver's URL")
    scheme, header, prefix = _signature(body.get("signature"))
    check_mapping(body.get("mapping"), writable)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"receivers:{principal}",))
        cur.execute("SELECT count(*) AS n FROM receivers WHERE removed_at IS NULL "
                    "AND principal = %s", (principal,))
        if cur.fetchone()["n"] >= RECEIVERS_MAX:
            _refuse(f"a client has at most {RECEIVERS_MAX} receivers", 409)
        if find(conn, principal, name):
            _refuse(f"a receiver '{name}' exists -- PUT replaces its mapping", 409)
        secret = secrets.token_urlsafe(32)
        cur.execute("INSERT INTO receivers (principal, name, scheme, header, prefix, secret, "
                    "mapping) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (principal, name, scheme, header, prefix, secret,
                     json.dumps(body["mapping"])))
        r = cur.fetchone()
    return dict(receiver_json(r), secret=secret)


def replace(conn, principal, writable, name, body):
    r = find(conn, principal, name)
    if r is None:
        _refuse(f"no receiver '{name}' of this client", 404)
    if "signature" in body:
        scheme, header, prefix = _signature(body.get("signature"))
    else:
        scheme, header, prefix = r["scheme"], r["header"], r["prefix"]
    check_mapping(body.get("mapping"), writable)
    with conn.cursor() as cur:
        cur.execute("UPDATE receivers SET scheme = %s, header = %s, prefix = %s, mapping = %s, "
                    "last_error = NULL, last_error_at = NULL WHERE id = %s RETURNING *",
                    (scheme, header, prefix, json.dumps(body["mapping"]), r["id"]))
        return receiver_json(cur.fetchone())


def remove(conn, principal, name):
    r = find(conn, principal, name)
    if r is None:
        _refuse(f"no receiver '{name}' of this client", 404)
    with conn.cursor() as cur:
        cur.execute("UPDATE receivers SET removed_at = now() WHERE id = %s", (r["id"],))


def note(conn, rid, *, refused=False, written=None, error=None):
    """The receiver's counters and last answer -- bookkeeping, its own
    transaction (a refused delivery's batch was rolled back)."""
    with conn.cursor() as cur:
        if refused:
            cur.execute("UPDATE receivers SET refused = refused + 1 WHERE id = %s", (rid,))
        elif error is not None:
            cur.execute("UPDATE receivers SET received = received + 1, last_error = %s, "
                        "last_error_at = now() WHERE id = %s", (error[:300], rid))
        else:
            cur.execute("UPDATE receivers SET received = received + 1, written = written + %s, "
                        "last_ok_at = now() WHERE id = %s", (written or 0, rid))
