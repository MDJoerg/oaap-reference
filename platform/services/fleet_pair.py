"""Pairing a node with the fleet hub (RFC-0085 §4, stage 3, amendment A3).

Pure rules, no Docker, no files of the host: appctl hands in the state and
the functions that touch identity, and keeps what this module returns. The
portal uses the same module to read a pairing file for its preview.

Two pairing files, one text form (``OAAP-PAIR1.<base64url(json)>``), each
going to the party that makes the call:

* **entry offer** (entry -> hub, once per entry): the hub calls the entry
  with the offer's one-time token and receives an operator key for its
  bridge and the pairing secret with which it signs node codes;
* **node code** (hub -> node): the node calls the entry (or the hub itself,
  for a direct pairing) with the code and receives a node key for its
  sender. The code is signed with the pairing secret the entry shares with
  the hub, valid 15 minutes, usable once.

Every call verifies the SHA-256 of the HTTPS certificate named in the file
(pinning), not a CA chain: the file was handed over by a person, so it is
the trust anchor, and a self-signed gateway works the same as a public one.
"""
import base64
import calendar
import hashlib
import hmac
import http.client
import json
import re
import secrets
import socket
import ssl
import time

PREFIX = "OAAP-PAIR1."
TTL = 15 * 60                   # D4: a code lives 15 minutes
REVOKE_SKEW = 5 * 60            # a revoke call older than this is refused
CLASSES = ("alive", "metrics", "status", "events")
REQUIRED_CLASSES = ("alive",)   # without alive "silent" and "removed" look alike
DEFAULT_CLASSES = CLASSES       # D5
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")     # = mqtt_acl.NODE_RE
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
FP_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_LABEL = "pair:"             # node keys issued by a pairing carry "pair:<hub>"
MAX_TEXT = 8192


class Refusal(Exception):
    """A pairing step that is refused; the message is for the operator."""


# ------------------------------------------------------------- the text form

def encode(obj):
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(text):
    """The object in a pairing file or pasted text. Whitespace and line
    breaks (a mail client wraps) are ignored."""
    s = "".join((text or "").split())
    if len(s) > MAX_TEXT:
        raise Refusal("Das ist keine Kopplungsdatei (zu groß).")
    if not s.startswith(PREFIX):
        raise Refusal("Das ist keine Kopplungsdatei (sie beginnt mit OAAP-PAIR1.).")
    body = s[len(PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refusal("Die Kopplungsdatei ist beschädigt.") from None
    if not isinstance(obj, dict) or obj.get("v") != 1 or \
            obj.get("type") not in ("node-code", "entry-offer"):
        raise Refusal("Diese Art Kopplungsdatei kennt dieser Knoten nicht.")
    return obj


def canonical(obj):
    return json.dumps({k: v for k, v in obj.items() if k != "mac"},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def mac(obj, secret):
    return hmac.new(secret.encode("utf-8"), canonical(obj), hashlib.sha256).hexdigest()


def digest(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ------------------------------------------------------------ making files

def _check_place(https, broker, fp):
    if not HOST_RE.match(https or ""):
        raise Refusal("Dieser Knoten hat keinen öffentlichen Namen "
                      "('oaap external set'); ohne ihn kann ihn niemand erreichen.")
    if not (broker or "").startswith("mqtts://"):
        raise Refusal("Der Broker braucht TLS (mqtts://).")
    if not FP_RE.match(fp or ""):
        raise Refusal("Das HTTPS-Zertifikat dieses Knotens ließ sich nicht lesen.")


def make_entry_offer(entry, https, broker, fp, now, address=""):
    """-> (text, record to keep at the entry). The token is in the text
    only; the entry keeps its digest."""
    _check_place(https, broker, fp)
    token = secrets.token_urlsafe(24)
    obj = {"v": 1, "type": "entry-offer", "entry": entry, "https": https,
           "broker": broker, "fp": fp, "token": token,
           "expires": _iso(now + TTL)}
    if address:
        obj["address"] = address
    return encode(obj), {"token": digest(token), "expires": int(now + TTL)}


def make_node_code(hub, entry, place, secret, node, now, replace=False,
                   address="", broker_address=""):
    """-> (text, record to keep at the hub). `place` is the hub's record of
    the entry: https, broker, fp."""
    node = (node or "").strip().lower()
    if not NODE_RE.match(node):
        raise Refusal("Name des Knotens: 2–40 Zeichen, Kleinbuchstaben, Ziffern, '-'.")
    _check_place(place.get("https"), place.get("broker"), place.get("fp"))
    nonce = secrets.token_urlsafe(18)
    obj = {"v": 1, "type": "node-code", "hub": hub, "entry": entry,
           "https": place["https"], "broker": place["broker"], "fp": place["fp"],
           "node": node, "replace": bool(replace), "nonce": nonce,
           "expires": _iso(now + TTL)}
    if address:
        obj["address"] = address
    if broker_address:
        obj["broker_address"] = broker_address
    obj["mac"] = mac(obj, secret)
    return encode(obj), {"nonce": digest(nonce), "node": node, "entry": entry,
                         "expires": int(now + TTL), "replace": bool(replace)}


def _expiry(obj):
    try:
        return calendar.timegm(time.strptime(obj.get("expires", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def expired(obj, now):
    return _expiry(obj) <= now


def preview(obj):
    """What a person must see before confirming -- no secret is in it."""
    out = {k: obj.get(k) for k in ("type", "hub", "entry", "https", "broker", "node",
                                     "replace", "expires", "address")
           if obj.get(k) not in (None, "")}
    return out


def classes_from(values):
    """The classes a node offers: the required ones always, unknown ones never."""
    got = [c for c in CLASSES if c in set(values or ()) or c in REQUIRED_CLASSES]
    return got


# ------------------------------------------------------------- the entry

def _prune(rows, now):
    return [r for r in rows or [] if int(r.get("expires", 0)) > now]


def claim_node(state, body, now, self_name, active_keys, issue, revoke):
    """The entry judges a node's call (POST /fleet/pair/claim).

    `state` is the entry's part of the pairing file: {"hubs": {hub: {"secret"}},
    "used": [{nonce, expires}], "nodes": {node: {...}}}; it is changed in
    place. `active_keys(node)` -> [key ids of active node keys for it];
    `issue(node, hub)` -> (key id, secret); `revoke(kid)`.
    Returns the answer for the node (with the secret) -- or raises Refusal.
    """
    try:
        obj = decode(body.get("code", ""))
    except Refusal:
        raise Refusal("Der Code ist ungültig.") from None
    if obj.get("type") != "node-code":
        raise Refusal("Der Code ist ungültig.")
    if obj.get("entry") != self_name:
        raise Refusal("Dieser Code ist für einen anderen Eingang.")
    hub = obj.get("hub", "")
    known = (state.get("hubs") or {}).get(hub) or {}
    if not known.get("secret") or not hmac.compare_digest(
            mac(obj, known["secret"]), str(obj.get("mac", ""))):
        # forged, or signed by a hub this entry does not (or no longer) know
        raise Refusal("Der Code ist ungültig.")
    if expired(obj, now):
        raise Refusal("Der Code ist abgelaufen. An der Zentrale einen neuen erzeugen.")
    state["used"] = _prune(state.get("used"), now)
    n_digest = digest(obj.get("nonce", ""))
    if any(u.get("nonce") == n_digest for u in state["used"]):
        raise Refusal("Der Code wurde schon benutzt. An der Zentrale einen neuen erzeugen.")
    node = obj.get("node", "")
    if not NODE_RE.match(node) or body.get("node", node) != node:
        raise Refusal("Der Code ist für einen anderen Knoten.")
    # spent from here on, also when the rest refuses: a code is one try
    state["used"].append({"nonce": n_digest, "expires": int(_expiry(obj))})
    old = list(active_keys(node))
    if old and not obj.get("replace"):
        raise Refusal(f"Ein Knoten '{node}' ist hier schon gekoppelt. An der Zentrale "
                      "einen Code „als Ersatz“ erzeugen, wenn er neu installiert wurde.")
    for kid in old:
        revoke(kid)
    kid, secret = issue(node, hub)
    classes = classes_from(body.get("classes") or DEFAULT_CLASSES)
    state.setdefault("nodes", {})[node] = {"key": kid, "hub": hub, "at": _iso(now),
                                           "classes": classes,
                                           "replaced": old}
    return {"ok": True, "user": kid, "secret": secret, "node": node,
            "broker": obj["broker"], "hub": hub, "entry": self_name}


def claim_entry(state, body, now, self_name, issue_operator, broker):
    """The entry judges the hub's call with an entry offer (POST
    /fleet/pair/entry). `state["offers"]`: [{token, expires}].
    `issue_operator(hub)` -> (key id, secret). Returns the answer with the
    operator key and the pairing secret."""
    token = str(body.get("token", ""))
    hub = str(body.get("hub", ""))
    if not NODE_RE.match(hub):
        raise Refusal("Der Name der Zentrale ist ungültig.")
    state["offers"] = _prune(state.get("offers"), now)
    t_digest = digest(token)
    offer = next((o for o in state["offers"]
                  if token and hmac.compare_digest(o.get("token", ""), t_digest)), None)
    if offer is None:
        raise Refusal("Das Angebot ist ungültig, abgelaufen oder schon benutzt. Am "
                      "Eingang ein neues erzeugen.")
    state["offers"].remove(offer)
    kid, op_secret = issue_operator(hub)
    pair_secret = secrets.token_urlsafe(32)
    hubs = state.setdefault("hubs", {})
    hubs[hub] = {"secret": pair_secret, "since": _iso(now), "operator": kid}
    return {"ok": True, "entry": self_name, "operator_user": kid,
            "operator_secret": op_secret, "pair_secret": pair_secret,
            "broker": broker}


def revoke_mac(secret, hub, node, ts, nonce):
    return hmac.new(secret.encode("utf-8"),
                    f"revoke\n{hub}\n{node}\n{int(ts)}\n{nonce}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def revoke_node(state, body, now, active_keys, revoke):
    """The entry judges the hub's revoke (POST /fleet/pair/revoke): HMAC with
    the pairing secret, fresh, once. Revokes the node keys that a pairing by
    THIS hub issued; a hand-made key stays (the hub never made it)."""
    hub, node = str(body.get("hub", "")), str(body.get("node", ""))
    known = (state.get("hubs") or {}).get(hub) or {}
    try:
        ts = int(body.get("ts", 0))
    except (TypeError, ValueError):
        ts = 0
    nonce = str(body.get("nonce", ""))
    if not known.get("secret") or not nonce or not hmac.compare_digest(
            revoke_mac(known["secret"], hub, node, ts, nonce), str(body.get("mac", ""))):
        raise Refusal("Nicht berechtigt.")
    if abs(now - ts) > REVOKE_SKEW:
        raise Refusal("Die Anfrage ist zu alt (Uhrzeit der Zentrale prüfen).")
    state["used"] = _prune(state.get("used"), now)
    n_digest = digest("revoke:" + nonce)
    if any(u.get("nonce") == n_digest for u in state["used"]):
        raise Refusal("Diese Anfrage wurde schon bearbeitet.")
    state["used"].append({"nonce": n_digest, "expires": int(now + 2 * REVOKE_SKEW)})
    gone = []
    for kid in active_keys(node, KEY_LABEL + hub):
        revoke(kid)
        gone.append(kid)
    rec = (state.get("nodes") or {}).pop(node, None)
    return {"ok": True, "node": node, "revoked": len(gone), "known": rec is not None}


# ---------------------------------------------------------- the hub's bridge

def bridge_conf(entry, hub, broker, user, secret, cafile="/etc/ssl/certs/ca-certificates.crt"):
    """The hub's bridge to an entry, as RFC-0085 §5.3 and stage 1 wrote it by
    hand (bridge-oaapx01.conf, 2026-11-12). Holds a key: write it 0600."""
    hostport = broker[len("mqtts://"):]
    if ":" not in hostport:
        hostport += ":8883"
    for v in (entry, hub, user, secret, hostport):
        if not re.match(r"^[\x21-\x7e]{1,256}$", v or ""):
            raise Refusal("Die Antwort des Eingangs ist unbrauchbar.")
    return (f"# RFC-0085 stage 3 (A3): the hub {hub} opens the bridge to the entry\n"
            f"# {entry}, written by the pairing. Holds a key: 0600.\n"
            f"connection entry-{entry}\n"
            f"address {hostport}\n"
            f"bridge_cafile {cafile}\n"
            f"remote_username {user}\n"
            f"remote_password {secret}\n"
            f"remote_clientid oaap-hub-{hub}\n"
            f"local_clientid bridge-{entry}\n"
            "cleansession false\n"
            "keepalive_interval 30\n"
            "restart_timeout 10 120\n"
            f"topic # in 1 oaap-via/{entry}/ oaap-node/\n"
            f"topic # out 1 oaap-fleet/control/{entry}/ oaap-fleet/control/{entry}/\n"
            "notifications true\n"
            "notifications_local_only false\n"
            f"notification_topic oaap-fleet/bridge/{entry}/{hub}\n")


# ------------------------------------------------------- HTTPS with a pin

def peer_fingerprint(host, port=443, address="", timeout=5):
    """SHA-256 of the leaf certificate `host` presents (SNI = host), reached
    at `address` when given."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((address or host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            return hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, fp, address="", timeout=40):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE         # the pin below replaces the chain
        super().__init__(host, 443, timeout=timeout, context=ctx)
        self._fp, self._address = fp, address

    def connect(self):
        raw = socket.create_connection((self._address or self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        got = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(got, self._fp):
            self.sock.close()
            raise Refusal(f"{self.host} zeigt ein anderes Zertifikat als in der "
                          "Kopplungsdatei. Ist es inzwischen erneuert worden, an der "
                          "Gegenseite eine neue Datei erzeugen; sonst nicht fortfahren.")


def post(host, path, body, fp, address="", timeout=40):
    """POST JSON to https://host/path, pinned to `fp`. -> (status, dict)."""
    conn = _PinnedConnection(host, fp, address, timeout)
    try:
        conn.request("POST", path, json.dumps(body).encode("utf-8"),
                     {"Content-Type": "application/json", "X-OAAP-API": "1"})
        resp = conn.getresponse()
        raw = resp.read(65536)
    except (OSError, http.client.HTTPException) as exc:
        raise Refusal(f"{host} ist nicht erreichbar ({type(exc).__name__}).") from None
    finally:
        conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = {}
    return resp.status, data if isinstance(data, dict) else {}
