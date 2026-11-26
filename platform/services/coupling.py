"""Coupling -- one hand-over for every key (RFC-0136).

The pure part, shared by the host (appctl), identity and the portal: the
ticket `OAAP-COUPLE1.<base64url(JSON)>`, the redemption code and the browser
link (both only ever stored as their SHA-256), the states of a ticket, and
the two forms of trust a ticket for a non-public address carries (§2, F1):
`pin` as curl takes it (`sha256//<base64 of the SHA-256 of the public key>`)
and `ca`, the certificate as PEM (Node-RED, SAP STRUST).

Nothing here is a secret by itself: a code is worth something once and only
within its window, and the key is born at redemption, in identity.
"""
import base64
import hashlib
import json
import re
import secrets
import ssl
import uuid
from datetime import datetime, timezone

PREFIX = "OAAP-COUPLE1."
VERSION = 1
KINDS = ("gate-access",)               # stage 1 (D6); platform-key, fleet-node later
WINDOW_DEFAULT_MIN = 15                # D3
WINDOW_MAX_MIN = 7 * 24 * 60           # D3: up to 7 days for a hand-over by mail
WINDOW_CHOICES = ((15, "15 Minuten"), (60, "1 Stunde"), (24 * 60, "1 Tag"),
                  (7 * 24 * 60, "7 Tage"))
KEEP_DAYS = 30                         # §5: ended tickets stay listed 30 days
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^c1_[A-Za-z0-9_-]{40,60}$")
LINK_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# §3: one sentence for everything that is over, one for everything unknown
GONE = "Diese Kopplung ist abgelaufen oder schon eingelöst — bitte eine neue anfordern."
UNKNOWN = "Diese Kopplung gibt es hier nicht."
STATE_TITLES = {"open": "wartet auf Einlösung", "redeemed": "eingelöst",
                "expired": "abgelaufen", "withdrawn": "zurückgezogen"}


def new_code():
    """A redemption code: 32 random bytes, base64url, with a marker."""
    return "c1_" + secrets.token_urlsafe(32)


def new_link():
    """The browser link's id (§3): a random UUID."""
    return str(uuid.uuid4())


def digest(s):
    """What identity stores of a code or a link."""
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def iso(t):
    """A UTC timestamp, from epoch seconds or a datetime."""
    if isinstance(t, datetime):
        t = t.timestamp()
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_minutes(v):
    """The window a person chose -> minutes, or raises ValueError."""
    if v in (None, ""):
        return WINDOW_DEFAULT_MIN
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("window: minutes, a number")
    if not 1 <= n <= WINDOW_MAX_MIN:
        raise ValueError(f"window: 1 to {WINDOW_MAX_MIN} minutes (7 days)")
    return n


def state(rec, now_iso):
    """open / redeemed / withdrawn / expired of one ticket record."""
    st = rec.get("state") or "open"
    if st != "open":
        return st
    return "expired" if (rec.get("until") or "") <= now_iso else "open"


def public(rec, now_iso):
    """A ticket record without its hashes -- for lists and the portal."""
    out = {k: v for k, v in rec.items() if k not in ("code_hash", "link_hash")}
    out["state"] = state(rec, now_iso)
    return out


def prune(recs, now_epoch):
    """Drop tickets that ended more than KEEP_DAYS ago (§5)."""
    cut = iso(now_epoch - KEEP_DAYS * 86400)
    keep = []
    for r in recs:
        ended = r.get("redeemed") or r.get("withdrawn") or r.get("until") or ""
        if (r.get("state") or "open") == "open" and (r.get("until") or "") > cut:
            keep.append(r)
        elif ended > cut:
            keep.append(r)
    return keep


# --- the ticket -------------------------------------------------------------

def encode(doc):
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(text):
    """A ticket line (or the JSON file's content) -> dict. Whitespace is
    ignored -- a mail wraps the line (as fleet_pair.decode)."""
    raw = str(text or "").strip()
    s = "".join(raw.split())
    if raw.startswith("{"):
        doc = json.loads(raw)      # the file: its strings keep their spaces
    else:
        if not s.startswith(PREFIX):
            raise ValueError("not a coupling ticket (OAAP-COUPLE1.)")
        body = s[len(PREFIX):]
        body += "=" * (-len(body) % 4)
        try:
            doc = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("the ticket is damaged")
    if not isinstance(doc, dict) or doc.get("v") != VERSION:
        raise ValueError("a coupling ticket of another version")
    return doc


def gate_ticket(detail, code, link):
    """The ticket of a gate access (§2) from what the host answered when it
    issued it (`detail`) and the code and link the issuer made itself."""
    addr = detail.get("address") or ""
    doc = {"v": VERSION, "kind": "gate-access", "address": addr,
           "label": detail.get("label") or "", "user": detail.get("user") or "",
           "redeem": detail.get("redeem") or "", "code": code,
           "until": detail.get("until") or "", "key_days": detail.get("key_days"),
           "links": {"ping": addr, "template": addr + "/template?form=placeholders" if addr else "",
                     "browser": (detail.get("browser") or "") + link},
           "issuer": detail.get("issuer") or "", "issued": detail.get("issued") or ""}
    if detail.get("pin"):
        doc["pin"] = detail["pin"]
    if detail.get("ca"):
        doc["ca"] = detail["ca"]
    return doc


# --- trust for a non-public address (§2, D8, F1) ----------------------------

def _tlv(b, i):
    """(start of the content, end) of the DER element at i."""
    n = b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    return i, i + n


def spki(der):
    """The SubjectPublicKeyInfo of a DER certificate (the element curl pins)."""
    _s0, _e0 = _tlv(der, 0)            # Certificate
    i, _e1 = _tlv(der, _s0)            # tbsCertificate
    if der[i] == 0xA0:                 # [0] version
        i = _tlv(der, i)[1]
    for _ in range(5):                 # serial, signature, issuer, validity, subject
        i = _tlv(der, i)[1]
    return der[i:_tlv(der, i)[1]]


def pin_of(der):
    """`sha256//<base64>` of the public key -- what `curl --pinnedpubkey` takes;
    it survives a renewed certificate with the same key."""
    return "sha256//" + base64.b64encode(hashlib.sha256(spki(der)).digest()).decode("ascii")


def pem_of(der):
    return ssl.DER_cert_to_PEM_cert(der)


def trust_of(der, publicly_trusted):
    """What a ticket carries about the server's certificate: nothing for a
    publicly trusted address, else pin and ca."""
    if publicly_trusted or not der:
        return {}
    return {"pin": pin_of(der), "ca": pem_of(der)}
