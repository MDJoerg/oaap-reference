"""Values checked against the registry, on every door (RFC-0120 stage 2,
oaap.data.twin 0.12 §2.19), and the periods of a periodic attribute (§2.20).

One function decides whether a value fits an attribute's type, and what is
stored: `check(adef, value)` -> the value as stored, or `Wrong` with the
sentence. The app door, the person door, the fleet and the period writes all
call it through twin_write -- one rule, not one per door.

What fits (a value of `None` always does -- it clears the attribute; so does
an empty string for every type but text, which is how a form and the fleet
say "none"):

| value_type | accepted                                         | stored        |
|------------|--------------------------------------------------|---------------|
| text       | a string                                         | as given      |
| int        | an integer, or a string of digits                | an integer    |
| decimal    | a number, or a string that is one ("1.5", "1,5") | a number      |
| bool       | true/false, or the strings true/false            | true/false    |
| date       | 2026-11-22                                       | as given      |
| datetime   | 2026-11-22T10:00:00Z (with a zone or without)    | as given      |
| enum       | a string, one of `values` when the type has them | as given      |
| ref        | urn:oaap:obj:<uuid>                              | as given      |

A string where a number is expected is turned into the number: the portal's
form sends text, and a value written as "5" by an app before this rule is the
same value as 5. An attribute that is not registered is not checked (its key
still has to look like a key) -- the registry says what it knows, nothing
more.
"""
import datetime
import json
import re

UTC = datetime.timezone.utc
REF_RE = re.compile(r"^urn:oaap:obj:[0-9a-fA-F-]{36}$")
INT_RE = re.compile(r"^[+-]?\d{1,18}$")
DEC_RE = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?$")
QUALITIES = ("measured", "calculated", "estimated", "reported")
TEXT_MAX = 500           # unit, source


class Wrong(ValueError):
    """A value or a period the rules refuse -- 400 with this sentence."""


def defn(d):
    if isinstance(d, str):
        try:
            return json.loads(d)
        except ValueError:
            return {}
    return d or {}


def attribute_types(conn, keys):
    """{key: definition} of the registered attribute types among `keys`."""
    keys = sorted(set(keys))
    if not keys:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT key, definition FROM oaap_model.type_definitions "
                    "WHERE kind = 'attribute_types' AND key = ANY(%s)", (keys,))
        return {r["key"]: defn(r["definition"]) for r in cur.fetchall()}


def check(key, adef, value):
    """The value as stored, or Wrong. `adef` None: not registered, not checked."""
    if value is None or adef is None:
        return value
    vt = adef.get("value_type") or "text"
    if vt != "text" and isinstance(value, str) and not value.strip():
        return None              # an empty field is no value (the fleet writes "" for none)
    bad = Wrong(f"attribute '{key}' is {vt}: {json.dumps(value, ensure_ascii=False)[:80]} "
                f"is not a value of that type")
    if isinstance(value, (dict, list)):
        raise bad
    if vt == "text":
        if not isinstance(value, str):
            raise bad
        return value
    if vt == "int":
        if isinstance(value, bool):
            raise bad
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and INT_RE.match(value.strip()):
            return int(value.strip())
        raise bad
    if vt == "decimal":
        if isinstance(value, bool):
            raise bad
        if isinstance(value, (int, float)):
            return value
        s = value.strip() if isinstance(value, str) else ""
        if INT_RE.match(s):
            return int(s)
        if DEC_RE.match(s):
            return float(s.replace(",", "."))
        raise bad
    if vt == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise bad
    if not isinstance(value, str):
        raise bad
    if vt == "date":
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            raise bad
        return value
    if vt == "datetime":
        try:
            datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise bad
        return value
    if vt == "enum":
        allowed = adef.get("values")
        if isinstance(allowed, list) and allowed and value not in allowed:
            raise Wrong(f"attribute '{key}' is one of {', '.join(map(str, allowed))} -- "
                        f"not '{value[:80]}'")
        return value
    if vt == "ref":
        if not REF_RE.match(value):
            raise bad
        return value
    return value


def periodic(adef):
    return bool(adef and adef.get("periodic"))


# ------------------------------------------------------------------ periods
def moment(raw, name, end=False):
    """A period bound: a date (00:00 UTC of that day; with `end`, the next
    day's 00:00 -- a period 'until 2025-12-31' ends when 2026 begins) or a
    date-time with a zone."""
    if isinstance(raw, datetime.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    raw = (raw or "").strip() if isinstance(raw, str) else ""
    if not raw:
        raise Wrong(f"'{name}' is needed (2025-01-01 or 2025-01-01T00:00:00Z)")
    try:
        if len(raw) == 10:
            d = datetime.date.fromisoformat(raw)
            if end:
                d = d + datetime.timedelta(days=1)
            return datetime.datetime.combine(d, datetime.time(0), UTC)
        t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise Wrong(f"'{name}' is a date (2025-01-01) or a date-time (2025-01-01T00:00:00Z)")
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def period_body(body):
    """(attr, value, from, to, unit, source, quality) of a period write.
    `valid_to` is exclusive as a date-time; as a date it is the last day the
    period still covers (`until_inclusive` reading, what a form asks)."""
    attr = (body.get("attribute") or "").strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9]*$", attr):
        raise Wrong("'attribute' is the attribute's key (e.g. co2e)")
    if "value" not in body:
        raise Wrong("'value' is needed (null withdraws the period)")
    frm = moment(body.get("valid_from"), "valid_from")
    to = moment(body.get("valid_to"), "valid_to", end=True)
    if to <= frm:
        raise Wrong("the period ends before it begins -- 'valid_to' after 'valid_from'")
    unit = body.get("unit")
    source = body.get("source")
    quality = body.get("quality")
    for name, v in (("unit", unit), ("source", source)):
        if v is not None and (not isinstance(v, str) or len(v) > TEXT_MAX):
            raise Wrong(f"'{name}' is a short text")
    if quality not in (None, "") and quality not in QUALITIES:
        raise Wrong(f"'quality' is one of {', '.join(QUALITIES)}")
    return (attr, body.get("value"), frm, to, (unit or None), (source or None),
            (quality or None))


def iso(t):
    if t is None:
        return None
    if isinstance(t, datetime.datetime):
        return t.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return t.isoformat()


def period_json(r, with_meta=True):
    out = {"value": r["value"], "valid_from": iso(r["valid_from"]),
           "valid_to": iso(r["valid_to"])}
    for k in ("unit", "source", "quality"):
        if r.get(k):
            out[k] = r[k]
    if with_meta:
        out["recorded_at"] = iso(r.get("recorded_at"))
        out["recorded_by"] = r.get("recorded_by")
    return out
