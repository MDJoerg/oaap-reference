"""oaap.core.tenant -- a tenant's requests to the operator (RFC-0109 section 13).

A tenant_admin who needs a recipe that is not released for their tenant, a
step outside their scope, or something no recipe does yet, presses "Beim
Betreiber anfragen" on Einrichten. The PLATFORM writes the request as a
case into the node's request inbox (a `vorgaenge` instance of the default
tenant, named by the operator) through that app's node door -- the tenant
needs no destination, no key and no account there. Without an inbox the
request goes as a message to the node's owners (RFC-0080). The state and
the operator's replies are read back and shown on Einrichten.

This file is the PURE half, like tenant_recipe: the request record, its
checks, the words of the case, the files under a root, and what a reading
of the case changes. Everything that touches the node is appctl's.

Rules that carry the design:

* A request carries nothing the tenant did not type or the preview did not
  show them: the recipe, the values they gave, their text, and the
  preview's rows (or the sentence that refused it). No secret: a value
  under a name with a secret word is refused (S3).
* The tenant comes from the actor's record, never from the request (S2).
* A tenant has at most MAX_OPEN open requests; a request is never sent
  twice (the case is found again by its id, the door's `ref`).
"""
import json
import os
import re
from datetime import datetime, timezone

ID_RE = re.compile(r"^q-[0-9]{8}t[0-9]{6}-[a-z0-9-]{1,63}(-[0-9]{1,3})?$")
RECIPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
SECRET_WORDS = ("key", "token", "secret", "password", "credentials")
TEXT_MAX = 2000
VALUE_MAX = 200
VALUES_MAX = 30
ROWS_MAX = 20
NOTE_MAX = 240
NAME_MAX = 80
REPLIES_KEEP = 12
REPLY_MAX = 1000
MAX_OPEN = 10
# what the request's own state says: where it went
DELIVERY = ("sent", "message", "failed")
DELIVERY_DE = {"sent": "als Vorgang beim Betreiber",
               "message": "als Nachricht an den Betreiber",
               "failed": "nicht zugestellt"}
# a case in one of these is over (vorgaenge: closed, rejected)
CASE_ENDED = ("closed", "rejected")
# the operator's own answer on Aufbau (works without an inbox too)
VERDICTS = ("done", "rejected")
VERDICT_DE = {"done": "erledigt", "rejected": "abgelehnt"}
ANSWER_MAX = 1000


class Refusal(Exception):
    """A rule said no; one sentence for a human."""


def iso_now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc) \
        .isoformat(timespec="seconds")


def new_id(label, now=None):
    t = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"q-{t.strftime('%Y%m%dt%H%M%S')}-{label}"


def _clip(text, n):
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def values_problems(values):
    if values is None:
        return []
    if not isinstance(values, dict):
        return ["the values are an object of texts"]
    out = []
    if len(values) > VALUES_MAX:
        out.append(f"at most {VALUES_MAX} values")
    for k, v in values.items():
        if not isinstance(k, str) or not PARAM_RE.match(k):
            out.append(f"'{k}' is not a value's name")
        elif any(w in k.lower() for w in SECRET_WORDS):
            out.append(f"'{k}' sounds like a secret -- a request never carries "
                       "one (RFC-0109 S3)")
        if not isinstance(v, (str, int)) or len(str(v)) > VALUE_MAX:
            out.append(f"'{k}': a text of up to {VALUE_MAX} characters")
    return out


def preview_summary(preview=None, refused=""):
    """What the tenant saw when they asked: the preview's rows, its open
    choices, or the sentence that refused it."""
    if refused:
        return {"refused": _clip(refused, 600)}
    p = preview or {}
    return {"rows": [{"id": str(r.get("id", ""))[:64], "verdict": str(r.get("verdict", ""))[:16],
                      "note": _clip(r.get("note", ""), NOTE_MAX)}
                     for r in (p.get("rows") or [])[:ROWS_MAX]],
            "problems": [_clip(x, NOTE_MAX) for x in (p.get("problems") or [])[:ROWS_MAX]]}


def new_request(label, by, by_name, *, recipe="", version="", title="",
                values=None, text="", seen=None, now=None):
    """A request record, not yet delivered. Refuses what may not go."""
    label = str(label or "").strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", label):
        raise Refusal("a request comes from a tenant")
    recipe = str(recipe or "").strip()
    if recipe and not RECIPE_RE.match(recipe):
        raise Refusal(f"'{recipe}' is not a recipe's name")
    text = str(text or "").strip()
    if len(text) > TEXT_MAX:
        raise Refusal(f"the text has at most {TEXT_MAX} characters")
    if not recipe and not text:
        raise Refusal("say what you need -- a recipe, or a few words")
    bad = values_problems(values)
    if bad:
        raise Refusal("; ".join(bad))
    return {"id": new_id(label, now), "kind": "recipe-request", "label": label,
            "by": str(by or "")[:NAME_MAX], "by_name": _clip(by_name or by, NAME_MAX),
            "recipe": recipe, "version": str(version or "")[:32],
            "title": _clip(title, 120), "values": {k: str(v) for k, v in (values or {}).items()},
            "text": text, "seen": seen or {}, "created": iso_now(now),
            "delivery": "", "inbox": "", "case": "", "note": "",
            "case_status": "", "case_label": "", "replies": [], "read": "",
            "released": ""}


def case_title(req):
    what = req.get("title") or req.get("recipe") or _clip(req.get("text"), 60)
    return _clip(f"Anfrage aus {req['label']}: {what}", 200)


def case_body(req, node=""):
    """The case's text: who asks (as the record says), what, and what they
    saw. Plain lines; the operator reads it in the cases app."""
    lines = [f"Mandant {req['label']} fragt beim Betreiber an"
             + (f" (Knoten {node})" if node else "") + ".",
             f"Von: {req.get('by_name') or req.get('by')} ({req.get('by')})"]
    if req.get("recipe"):
        lines.append(f"Rezept: {req['recipe']} {req.get('version') or ''}".rstrip()
                     + (f" -- {req['title']}" if req.get("title") else ""))
    if req.get("values"):
        lines.append("Werte: " + ", ".join(f"{k}={v}" for k, v in sorted(req["values"].items())))
    if req.get("text"):
        lines += ["", req["text"]]
    seen = req.get("seen") or {}
    if seen.get("refused"):
        lines += ["", "Die Seite Einrichten hat gesagt: " + seen["refused"]]
    elif seen.get("rows") or seen.get("problems"):
        lines += ["", "Vorschau, wie der Mandant sie sah:"]
        lines += [f"- {x}" for x in seen.get("problems") or []]
        lines += [f"- {r['id']}: {r['verdict']} {r['note']}".rstrip() for r in seen.get("rows") or []]
    lines += ["", f"Anfrage {req['id']}. Freigeben: Aufbau, Karte „Rezepte“, oder "
                  f"oaap tenant allow {req['label']} recipe:<id>. Antworten hier im Vorgang "
                  "sieht der Mandant auf seiner Seite Einrichten."]
    return "\n".join(lines)[:9000]


def message_text(req, node=""):
    return case_body(req, node)[:1500]


def apply_reading(req, reading, now=None):
    """What a reading of the case (vorgaenge `op: read`) changes in the
    record -> True when something changed."""
    if not isinstance(reading, dict) or not reading.get("ok"):
        return False
    replies = [{"at": str(r.get("at", ""))[:32], "by": _clip(r.get("by", ""), NAME_MAX),
                "status_label": str(r.get("status_label", ""))[:40],
                "text": _clip(r.get("text", ""), REPLY_MAX)}
               for r in (reading.get("replies") or [])
               if isinstance(r, dict)][-REPLIES_KEEP:]
    new = {"case_status": str(reading.get("status", ""))[:32],
           "case_label": str(reading.get("status_label", ""))[:40], "replies": replies}
    changed = any(req.get(k) != v for k, v in new.items())
    req.update(new)
    req["read"] = iso_now(now)
    return changed


def close(req, verdict, text, by_name, now=None):
    """The operator answers a request on Aufbau: done or rejected, with a
    sentence the tenant reads on Einrichten. A rejection needs the
    sentence. Raises Refusal."""
    if verdict not in VERDICTS:
        raise Refusal("the answer is done or rejected")
    if req.get("closed"):
        raise Refusal(f"request {req['id']} is already answered "
                      f"({VERDICT_DE.get(req.get('verdict'), req.get('verdict'))})")
    text = str(text or "").strip()
    if len(text) > ANSWER_MAX:
        raise Refusal(f"the answer has at most {ANSWER_MAX} characters")
    if verdict == "rejected" and not text:
        raise Refusal("a rejection says why, in one sentence")
    at = iso_now(now)
    req.update(closed=at, verdict=verdict)
    req["replies"] = (list(req.get("replies") or []) + [{
        "at": at, "by": _clip(by_name, NAME_MAX), "status_label": VERDICT_DE[verdict],
        "text": _clip(text, REPLY_MAX)}])[-REPLIES_KEEP:]
    return req


def is_open(req):
    if req.get("closed"):
        return False
    if req.get("delivery") == "failed":
        return False
    if req.get("delivery") == "message":
        return not req.get("released")
    return req.get("case_status", "") not in CASE_ENDED


def state_text(req):
    """One phrase for the list on Einrichten."""
    if req.get("closed"):
        return VERDICT_DE.get(req.get("verdict"), "beantwortet")
    if req.get("delivery") == "failed":
        return "nicht zugestellt"
    if req.get("released"):
        return "freigegeben"
    if req.get("delivery") == "message":
        return "als Nachricht beim Betreiber"
    return req.get("case_label") or "beim Betreiber"


# ------------------------------------------------------------ the files

def path_of(root, rid):
    if not ID_RE.match(str(rid or "")):
        raise Refusal(f"'{rid}' is not a request")
    return os.path.join(root, rid + ".json")


def save(root, req):
    os.makedirs(root, mode=0o750, exist_ok=True)
    p = path_of(root, req["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o640)
    os.replace(tmp, p)


def load(root, rid):
    try:
        with open(path_of(root, rid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise Refusal(f"there is no request '{rid}'")


def all_requests(root):
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and ID_RE.match(n[:-5]):
            try:
                with open(os.path.join(root, n), encoding="utf-8") as f:
                    doc = json.load(f)
                if isinstance(doc, dict) and doc.get("kind") == "recipe-request":
                    out.append(doc)
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda r: r.get("created", ""), reverse=True)


def unique_id(root, req):
    """A second request of the same tenant in the same second gets -2."""
    base, n = req["id"], 1
    while os.path.exists(path_of(root, req["id"])):
        n += 1
        req["id"] = f"{base}-{n}"
    return req


def open_of(root, label):
    return [r for r in all_requests(root) if r.get("label") == label and is_open(r)]
