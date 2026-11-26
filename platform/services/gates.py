"""Gates -- the way in for other systems (RFC-0135, stage 1).

One module, three readers: appctl (writes gates.json and the gateway's
blocks), identity (ping and templates) and -- as a verbatim copy -- the
`vorgaenge` app (checks a delivery against the same field list). The
templates and the check come from ONE list, so what a gate describes and
what it accepts cannot drift apart (RFC-0135 §5).

Nothing here touches the network or the disk; the store is a dict the
caller loads and saves.

    COPY: oaap-apps/apps/vorgaenge/gates.py is this file, byte for byte.
    Change both, or the template and the check disagree.
"""
import json
import re

FORMAT = 1
# what a gate's address may be called: /gate/<name> (RFC-0135 §2)
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# what a `ref` may contain: it is read back as /gate/<name>/<ref>
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
TYPES = ("text", "number", "timestamp", "date", "yesno", "choice")
# where a value goes in a case (RFC-0135 §6); everything else is technical
TARGETS = ("ref", "title", "text", "tech")
MAX_FIELDS = 40
TEXT_MAX = 10000          # vorgaenge BODY_MAX
BODY_MAX_KB = 64
SCOPE_PREFIX = "oaap.gate:"
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
YES = ("true", "ja", "x", "1", "yes")
NO = ("false", "nein", "", "0", "no")

DEFAULT_FIELDS = [
    {"name": "ref", "type": "text", "max": 120, "required": True, "to": "ref",
     "description": "eigene Referenz des Absenders; dieselbe ref zweimal ist eine Lieferung",
     "sample": "ZSUP-000815"},
    {"name": "title", "type": "text", "max": 200, "required": True, "to": "title",
     "description": "Kurzbeschreibung", "sample": "ME21N: keine Berechtigung"},
    {"name": "text", "type": "text", "max": TEXT_MAX, "required": True, "to": "text",
     "description": "was die Person fragte, was das System weiss",
     "sample": "Benutzer MMUSTER kann keine Bestellung anlegen."},
    {"name": "system", "type": "text", "max": 40, "required": False, "to": "tech",
     "description": "System und Mandant", "sample": "A4H/001"},
    {"name": "occurred", "type": "timestamp", "required": False, "to": "tech",
     "description": "wann es geschah (UTC)", "sample": "2026-11-26T09:14:03Z"},
]


def scope(tenant, name):
    """The key scope of a gate's accesses (RFC-0135 §3.3)."""
    return f"{SCOPE_PREFIX}{tenant}/{name}"


def gate_key(tenant, name):
    return f"{tenant}/{name}"


def empty_store():
    return {"v": FORMAT, "gates": {}, "processes": {}}


def check_fields(fields):
    """A field list as the administrator wrote it -> the normalized list,
    or ValueError with a sentence. Exactly one field goes to `ref` and one
    to `title`; at most one to `text`."""
    if not isinstance(fields, list) or not fields:
        raise ValueError("fields: a non-empty list")
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"fields: at most {MAX_FIELDS}")
    out, seen = [], set()
    for f in fields:
        if not isinstance(f, dict):
            raise ValueError("fields: each one an object")
        name = str(f.get("name") or "")
        if not FIELD_RE.match(name):
            raise ValueError(f"field name {name!r}: a-z, 0-9, _ (starting with a letter)")
        if name in seen:
            raise ValueError(f"field {name!r} twice")
        seen.add(name)
        typ = str(f.get("type") or "text")
        if typ not in TYPES:
            raise ValueError(f"field {name}: type one of {', '.join(TYPES)}")
        to = str(f.get("to") or "tech")
        if to not in TARGETS:
            raise ValueError(f"field {name}: to one of {', '.join(TARGETS)}")
        if to != "tech" and typ != "text":
            raise ValueError(f"field {name}: only a text goes to {to}")
        g = {"name": name, "type": typ, "required": bool(f.get("required")), "to": to,
             "description": str(f.get("description") or "")[:200]}
        if typ == "text":
            try:
                g["max"] = int(f.get("max") or (TEXT_MAX if to == "text" else 200))
            except (TypeError, ValueError):
                raise ValueError(f"field {name}: max is a number")
            limit = 120 if to == "ref" else TEXT_MAX
            if not 1 <= g["max"] <= limit:
                raise ValueError(f"field {name}: max 1..{limit}")
        if typ == "choice":
            ch = [str(c) for c in (f.get("choices") or []) if str(c)]
            if not ch:
                raise ValueError(f"field {name}: a choice needs choices")
            g["choices"] = ch[:30]
        if f.get("sample") not in (None, ""):
            g["sample"] = str(f.get("sample"))[:200]
        out.append(g)
    tos = [g["to"] for g in out]
    for t in ("ref", "title"):
        if tos.count(t) != 1:
            raise ValueError(f"fields: exactly one field goes to {t}")
    if tos.count("text") > 1:
        raise ValueError("fields: at most one field goes to text")
    for g in out:
        if g["to"] in ("ref", "title") and not g["required"]:
            raise ValueError(f"field {g['name']}: the {g['to']} is required")
    return out


def format_of(f):
    t = f["type"]
    if t == "text":
        return f"<= {f.get('max', 200)} Zeichen"
    if t == "number":
        return "Zahl (Punkt als Dezimaltrenner)"
    if t == "timestamp":
        return "YYYY-MM-DDThh:mm:ssZ (UTC)"
    if t == "date":
        return "YYYY-MM-DD"
    if t == "yesno":
        return "X oder leer (auch true/false)"
    return "|".join(f.get("choices") or [])


def _sample(f):
    if f.get("sample"):
        return f["sample"]
    return {"text": "...", "number": "1", "timestamp": "2026-01-31T12:00:00Z",
            "date": "2026-01-31", "yesno": "X"}.get(f["type"]) or (f.get("choices") or [""])[0]


def template(fields, form):
    """The three forms of RFC-0135 §5, generated from the field list."""
    if form == "fields":
        rows = ["Feld\tTyp\tFormat\tPflicht\tBeschreibung"]
        rows += ["\t".join((f["name"], f["type"], format_of(f),
                            "ja" if f["required"] else "nein", f.get("description", "")))
                 for f in fields]
        return "\n".join(rows) + "\n"
    if form == "placeholders":
        head = ("# Platzhalter {feld} ersetzen. Vorher im Wert maskieren: \" -> \\\", "
                "\\ -> \\\\, Zeilenumbruch -> \\n.\n"
                "# Oder dieselben Felder als Formularfelder senden "
                "(application/x-www-form-urlencoded) -- dann ohne Maskieren.\n"
                "# Zeichensatz UTF-8; sonst im Content-Type charset=iso-8859-1 angeben.\n")
        body = ",\n".join(f'  "{f["name"]}": "{{{f["name"]}}}"' for f in fields)
        return head + "{\n" + body + "\n}\n"
    return json.dumps({f["name"]: _sample(f) for f in fields}, ensure_ascii=False, indent=2) + "\n"


def validate(fields, doc):
    """(values, errors): a delivery checked against the field list
    (RFC-0135 D5). `doc` is the parsed JSON object or the form fields.
    Unknown fields are refused with their names; values come back as
    strings, trimmed, a yes/no as "X" or ""."""
    if not isinstance(doc, dict):
        return {}, ["JSON-Objekt oder Formularfelder erwartet"]
    known = {f["name"]: f for f in fields}
    errors = []
    unknown = sorted(k for k in doc if k not in known)
    if unknown:
        errors.append("unbekannte Felder: " + ", ".join(unknown))
    values, missing = {}, []
    for f in fields:
        raw = doc.get(f["name"])
        if isinstance(raw, bool):
            raw = "X" if raw else ""
        elif isinstance(raw, (int, float)):
            raw = repr(raw) if isinstance(raw, float) else str(raw)
        elif raw is None:
            raw = ""
        elif not isinstance(raw, str):
            errors.append(f"{f['name']}: ein einzelner Wert erwartet")
            continue
        v = raw.strip()
        if not v and f["type"] != "yesno":
            if f["required"]:
                missing.append(f["name"])
            continue
        t = f["type"]
        bad = ""
        if t == "text":
            if len(v) > f.get("max", 200):
                bad = f"höchstens {f.get('max', 200)} Zeichen"
            elif f["to"] == "ref" and not REF_RE.match(v):
                bad = "Buchstaben, Ziffern und . _ : - (höchstens 120)"
        elif t == "number":
            bad = "" if NUMBER_RE.match(v) else "eine Zahl"
        elif t == "timestamp":
            bad = "" if TIMESTAMP_RE.match(v) else "YYYY-MM-DDThh:mm:ssZ (UTC)"
        elif t == "date":
            bad = "" if DATE_RE.match(v) else "YYYY-MM-DD"
        elif t == "yesno":
            if v.lower() in YES:
                v = "X"
            elif v.lower() in NO:
                v = ""
            else:
                bad = "X oder leer"
        elif t == "choice":
            bad = "" if v in f.get("choices", []) else "eines von " + "|".join(f.get("choices", []))
        if bad:
            errors.append(f"{f['name']}: {bad}")
            continue
        if v or t == "yesno":
            values[f["name"]] = v
    if missing:
        errors.append("Pflichtfelder fehlen: " + ", ".join(missing))
    return values, errors


def mapped(fields, values):
    """{'ref','title','text','tech'} from checked values."""
    out = {"ref": "", "title": "", "text": "", "tech": {}}
    for f in fields:
        if f["name"] not in values:
            continue
        if f["to"] == "tech":
            out["tech"][f["name"]] = values[f["name"]]
        else:
            out[f["to"]] = values[f["name"]]
    return out


def ping_text(gate, tenant_label, access_label, expires, process_line, now, platform=""):
    """The ping of RFC-0135 §4: nothing secret, everything the person at
    SM59 needs when it does not work."""
    req = sum(1 for f in gate["fields"] if f["required"])
    lines = [f"OAAP-Tor: {gate['name']} (Mandant {tenant_label})",
             f"Zugang: {access_label or '-'} - Schluessel gueltig bis {(expires or '-')[:10]}",
             f"Prozess: {process_line}",
             f"Felder: {len(gate['fields'])} ({req} Pflicht) - Vorlage: "
             f"/gate/{gate['name']}/template?form=placeholders",
             f"Annahme: JSON oder Formularfelder, UTF-8 (oder charset=iso-8859-1) - "
             f"hoechstens {BODY_MAX_KB} KB",
             f"Serverzeit: {now}" + (f" - Plattform {platform}" if platform else "")]
    return "\n".join(lines) + "\n"


# the attested description of a gate the gateway hands the target
# (RFC-0135 §6): base64url of compact JSON, so it fits a header unquoted
def pack(doc):
    import base64
    raw = json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def unpack(text):
    import base64
    text = (text or "").strip()
    try:
        return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode())
    except (ValueError, UnicodeDecodeError):
        return None
