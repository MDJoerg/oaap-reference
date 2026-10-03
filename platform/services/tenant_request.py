"""Einladungen und Anträge für den Mandanten-Aufbau (RFC-0055 Stufe 4).

Ein Interessent baut nichts. Er bekommt einen **Einladungslink** (einmal
benutzbar, mit Ablauf, an ein Profil gebunden) und füllt damit ein Formular
aus; heraus kommt ein **Antrag**, den ein angemeldeter `server_admin` freigibt
oder ablehnt. Erst die Freigabe startet den Aufbau -- mit der Rolle des
Menschen, der freigibt (die Betreiber-API kennt keinen Schlüssel, RFC-0055
§11.1).

Diese Datei ist der reine Kern ohne Docker und ohne Flask (wie
`tenant_build.py`): Dateien unter einem Wurzelordner, Zeit als Parameter.
Der Einladungslink wird NIE gespeichert, nur sein SHA-256; wer den Ordner
liest, kann also keinen Antrag stellen.
"""
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

INVITE_DAYS_DEFAULT = 14
INVITE_DAYS_MAX = 60
MAX_OPEN_INVITES = 50
MAX_PENDING_REQUESTS = 50
KEEP_DECIDED_DAYS = 30            # wie lange ein Entschiedenes sichtbar bleibt
KEEP_EXPIRED_DAYS = 7
CONTACT_MAX = 160
NOTE_MAX = 80
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
ID_RE = re.compile(r"^(inv|req)-[0-9a-f]{12}$")
MAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[^@\s<>\"',;]{1,120}\.[^@\s<>\"',;]{2,}$")


class Refusal(Exception):
    """Ein Satz, der dem Menschen sagt, warum nicht."""


def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(iso):
    try:
        return datetime.fromisoformat(str(iso))
    except ValueError:
        return None


def hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def new_token():
    return secrets.token_urlsafe(32)


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _read_all(root, prefix):
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        if not (n.startswith(prefix + "-") and n.endswith(".json")):
            continue
        try:
            with open(os.path.join(root, n), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and ID_RE.match(str(doc.get("id", ""))):
            out.append(doc)
    return out


def _path(root, rid):
    if not ID_RE.match(str(rid or "")):
        raise Refusal(f"'{rid}' ist keine gültige Kennung")
    return os.path.join(root, rid + ".json")


def invites(root):
    return _read_all(root, "inv")


def requests(root):
    return _read_all(root, "req")


def get(root, rid):
    try:
        with open(_path(root, rid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _expired(inv, now):
    exp = _parse(inv.get("expires"))
    return exp is None or exp <= now


def invite_state(inv, now):
    """open | used | revoked | expired -- the state a person would name."""
    if inv.get("state") in ("used", "revoked"):
        return inv["state"]
    return "expired" if _expired(inv, now) else "open"


# --------------------------------------------------------------- invitations

def create_invite(root, token, profile, note, days, by, now):
    """Speichert die Einladung (nur den Hash) -> die Einladung."""
    if not TOKEN_RE.match(str(token or "")):
        raise Refusal("der Einladungslink ist zu kurz oder enthält unzulässige Zeichen")
    note = " ".join(str(note or "").split())[:NOTE_MAX]
    try:
        days = INVITE_DAYS_DEFAULT if days in (None, "") else int(days)
    except (TypeError, ValueError):
        raise Refusal("die Gültigkeit muss eine Zahl in Tagen sein")
    if not 1 <= days <= INVITE_DAYS_MAX:
        raise Refusal(f"die Einladung gilt 1 bis {INVITE_DAYS_MAX} Tage")
    open_now = [i for i in invites(root) if invite_state(i, now) == "open"]
    if len(open_now) >= MAX_OPEN_INVITES:
        raise Refusal(f"es gibt schon {MAX_OPEN_INVITES} offene Einladungen; "
                      "widerrufe oder verbrauche zuerst welche")
    inv = {"id": "inv-" + secrets.token_hex(6), "hash": hash_token(token),
           "profile": str(profile), "note": note, "state": "open",
           "created": _iso(now), "by": str(by),
           "expires": _iso(now + timedelta(days=days)), "request": ""}
    _write(_path(root, inv["id"]), inv)
    return inv


def revoke_invite(root, rid, now):
    inv = get(root, rid)
    if not inv or not str(rid).startswith("inv-"):
        raise Refusal("diese Einladung gibt es nicht")
    if inv.get("state") != "open":
        raise Refusal("diese Einladung ist schon " + invite_state(inv, now))
    inv["state"] = "revoked"
    inv["closed"] = _iso(now)
    _write(_path(root, rid), inv)
    return inv


def find_by_token(root, token, now):
    """Die OFFENE Einladung zu diesem Link, sonst None. Ein Vergleich über
    den Hash, in gleichbleibender Zeit."""
    if not TOKEN_RE.match(str(token or "")):
        return None
    h = hash_token(token)
    found = None
    for inv in invites(root):
        if secrets.compare_digest(str(inv.get("hash", "")), h):
            found = inv
    if found and invite_state(found, now) == "open":
        return found
    return None


# ----------------------------------------------------------------- requests

def valid_contact(contact):
    c = str(contact or "").strip()
    if not c or len(c) > CONTACT_MAX or not MAIL_RE.match(c):
        raise Refusal("die Kontakt-Adresse sieht nicht wie eine E-Mail-Adresse aus")
    return c


def submit(root, token, values, contact, now):
    """Aus einer offenen Einladung wird ein Antrag. `values` sind die schon
    geprüften Parameter des Profils (das tut der Aufrufer mit der Engine).
    Die Einladung ist danach verbraucht -- auch bei einem zweiten Absenden
    desselben Links: dann bleibt es beim ersten Antrag."""
    inv = find_by_token(root, token, now)
    if not inv:
        raise Refusal("diese Einladung gilt nicht (mehr)")
    contact = valid_contact(contact)
    pending = [r for r in requests(root) if r.get("state") == "pending"]
    if len(pending) >= MAX_PENDING_REQUESTS:
        raise Refusal("es liegen schon zu viele unentschiedene Anträge vor")
    label = str(values.get("label") or "")
    if any(r.get("label") == label for r in pending):
        raise Refusal(f"für das Kürzel '{label}' liegt schon ein Antrag vor")
    req = {"id": "req-" + secrets.token_hex(6), "profile": inv["profile"],
           "label": label, "params": dict(values), "contact": contact,
           "invite": inv["id"], "note": inv.get("note", ""),
           "state": "pending", "created": _iso(now), "decided": "", "by": "",
           "build": "", "reason": ""}
    # erst die Einladung verbrauchen: schlägt das fehl, gibt es keinen Antrag
    inv["state"] = "used"
    inv["closed"] = _iso(now)
    inv["request"] = req["id"]
    _write(_path(root, inv["id"]), inv)
    _write(_path(root, req["id"]), req)
    return req


def pending_label_taken(root, label):
    return any(r.get("state") == "pending" and r.get("label") == label
               for r in requests(root))


def reject(root, rid, by, reason, now):
    """Ablehnen: der Antrag bleibt als Vermerk, die Kontakt-Adresse nicht."""
    req = get(root, rid)
    if not req or not str(rid).startswith("req-"):
        raise Refusal("diesen Antrag gibt es nicht")
    if req.get("state") != "pending":
        raise Refusal("dieser Antrag ist schon entschieden")
    req.update(state="rejected", decided=_iso(now), by=str(by), contact="",
               reason=" ".join(str(reason or "").split())[:200])
    _write(_path(root, rid), req)
    return req


def approve(root, rid, by, build_id, now):
    """Freigegeben: der Antrag merkt sich den Aufbau. Die Adresse bleibt,
    bis der Antrag nach KEEP_DECIDED_DAYS verfällt -- der Mensch, der den
    ersten Verwalter einrichtet, braucht sie."""
    req = get(root, rid)
    if not req or not str(rid).startswith("req-"):
        raise Refusal("diesen Antrag gibt es nicht")
    if req.get("state") != "pending":
        raise Refusal("dieser Antrag ist schon entschieden")
    req.update(state="approved", decided=_iso(now), by=str(by),
               build=str(build_id))
    _write(_path(root, rid), req)
    return req


def prune(root, now):
    """Abgelaufene Einladungen und alte Entscheidungen verschwinden -- mit
    der Kontakt-Adresse darin."""
    gone = []
    for inv in invites(root):
        end = _parse(inv.get("closed")) or _parse(inv.get("expires"))
        if end and now - end > timedelta(days=KEEP_EXPIRED_DAYS) \
                and invite_state(inv, now) != "open":
            gone.append(inv["id"])
    for req in requests(root):
        end = _parse(req.get("decided"))
        if req.get("state") != "pending" and end \
                and now - end > timedelta(days=KEEP_DECIDED_DAYS):
            gone.append(req["id"])
    for rid in gone:
        try:
            os.remove(_path(root, rid))
        except OSError:
            pass
    return gone


# ------------------------------------------------------------------- views

def view(root, now):
    """Was das Portal lesen darf: keine Hashes, keine Links. Die Adresse
    steht nur bei offenen und freigegebenen Anträgen."""
    inv_rows = [{"id": i["id"], "profile": i.get("profile", ""),
                 "note": i.get("note", ""), "state": invite_state(i, now),
                 "created": i.get("created", ""), "expires": i.get("expires", ""),
                 "by": i.get("by", ""), "request": i.get("request", "")}
                for i in invites(root)]
    req_rows = [{k: r.get(k, "") for k in
                 ("id", "profile", "label", "params", "contact", "note",
                  "state", "created", "decided", "by", "build", "reason")}
                for r in requests(root)]
    inv_rows.sort(key=lambda r: r["created"], reverse=True)
    req_rows.sort(key=lambda r: r["created"], reverse=True)
    # the hashes of the OPEN invitations, so the portal can tell a dead link
    # from a live one before it queues anything; a hash gives no link back
    live = {i["hash"]: i["profile"] for i in invites(root)
            if invite_state(i, now) == "open"}
    return {"schema": "0.1", "written": _iso(now), "invites": inv_rows,
            "requests": req_rows, "live": live}
