"""The configuration of an instance as a file, and the search over it
(portal 0.3.61, 2.19).

Works on the rows the page already builds (_instance_config): key, label,
secret, multiline, file, stored. Those rows never carry a secret value --
the host's view has none -- so the export cannot leak one even by mistake.
No Flask here, so the tests load it directly.
"""
import json

SCHEMA = "oaap.instance-config/0.1"
MAX_BYTES = 256 * 1024
LIST_SEPARATOR = ";"


def _lines(stored):
    return [p.strip() for p in (stored or "").split(LIST_SEPARATOR) if p.strip()]


def export(name, inst, rows, now):
    """The JSON document for the download: every value that is neither
    secret nor a file; the others named in `omitted` with the reason."""
    values, omitted = {}, []
    for c in rows:
        if c.get("secret"):
            omitted.append({"key": c["key"], "reason": "secret"})
        elif c.get("file"):
            omitted.append({"key": c["key"], "reason": "file"})
        elif c.get("multiline"):
            values[c["key"]] = _lines(c.get("stored"))
        else:
            values[c["key"]] = str(c.get("stored") or "")
    return {"schema": SCHEMA, "instance": name,
            "app_id": inst.get("app_id", ""), "version": inst.get("version", ""),
            "exported": now, "values": values, "omitted": omitted}


def parse(raw):
    """(values, error) from an uploaded file. `values` maps key -> str or
    list of str; the error is a sentence for the page."""
    if raw is None or len(raw) == 0:
        return None, "Keine Datei gewählt."
    if len(raw) > MAX_BYTES:
        return None, f"Die Datei ist größer als {MAX_BYTES // 1024} KiB."
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None, "Die Datei ist kein JSON."
    vals = doc.get("values") if isinstance(doc, dict) else None
    if not isinstance(vals, dict):
        return None, "In der Datei fehlt das Objekt „values“."
    out = {}
    for k, v in vals.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v if x is not None]
        elif v is None:
            out[str(k)] = ""
        elif isinstance(v, (str, int, float, bool)):
            out[str(k)] = ("true" if v is True else "false" if v is False else str(v))
        else:
            return None, f"Der Wert von {k} ist weder Text noch Liste."
    return out, ""


def to_form(rows, values):
    """What the upload puts into the form, without saving.

    Returns (typed, changed, unknown): `typed` the field contents by key
    (the page's `typed` mechanism), `changed` the keys whose value differs
    from what is stored (a secret given in the file always counts -- it
    cannot be compared), `unknown` the file's keys this app does not declare.
    A file field is never filled from a file of this kind.
    """
    declared = {c["key"]: c for c in rows}
    typed, changed = {}, []
    for k, v in values.items():
        c = declared.get(k)
        if c is None or c.get("file"):
            continue
        if c.get("multiline"):
            lines = v if isinstance(v, list) else _lines(v) if LIST_SEPARATOR in v \
                else [x.strip() for x in str(v).splitlines() if x.strip()]
            text = "\n".join(x.strip() for x in lines if x.strip())
            same = text == "\n".join(_lines(c.get("stored")))
        else:
            text = " ".join(v) if isinstance(v, list) else str(v)
            same = text == str(c.get("stored") or "")
        if c.get("secret"):
            if text.strip() == "":
                continue          # empty means "keep", as in the form
            same = False
        typed[k] = text
        if not same:
            changed.append(k)
    unknown = sorted(k for k in values if k not in declared
                     or declared[k].get("file"))
    return typed, changed, unknown


def search(rows, q):
    """The declared values matching `q` (case-insensitive) in key, label or
    -- where not secret -- value; each with where it matched."""
    q = (q or "").strip().lower()
    if not q:
        return []
    hits = []
    for c in rows:
        where = []
        if q in c["key"].lower():
            where.append("Schlüssel")
        if q in str(c.get("label") or "").lower():
            where.append("Bezeichnung")
        if not c.get("secret") and not c.get("file") and \
                q in str(c.get("stored") or "").lower():
            where.append("Wert")
        if where:
            hits.append({"key": c["key"], "label": c.get("label") or c["key"],
                         "where": ", ".join(where), "secret": bool(c.get("secret"))})
    return hits
