"""The person, signed, for the way out (oaap.core.identity 2.11, RFC-0066).

Identity mints; the service that issues a backend credential checks
(services/connect/propagate.py keeps its own copy of `check` -- its
image is built from its own directory -- and test_propagation.py runs
one against the other, so the two cannot drift apart unnoticed).

A compact JWS with HS256, written out by hand: three base64url parts,
the HMAC over the first two. No library, because the two sides are two
platform services on one node sharing one key, and the whole format is
twenty lines either side can read.
"""
import base64
import hashlib
import hmac
import json
import time

VERSION = 1
LIFETIME = 300            # seconds -- spec 2.11: five minutes
HEADER = "X-OAAP-User-Assertion"
_HEAD = {"alg": "HS256", "typ": "oaap-assertion"}


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint(key, user_id, username, tenant, audience, propagate=None, now=None):
    """The header value, or "" when it must not exist.

    No key, no audience, no person: empty. An assertion nobody can check,
    or one that says "for any instance", is worse than none.
    """
    if not key or not audience or not user_id or not tenant:
        return ""
    now = int(time.time() if now is None else now)
    claims = {"v": VERSION, "sub": user_id, "usr": username,
              "ten": tenant, "aud": audience, "iat": now,
              "exp": now + LIFETIME, "prop": dict(propagate or {})}
    head = _b64(json.dumps(_HEAD, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":"),
                           sort_keys=True).encode())
    sig = hmac.new(key, f"{head}.{body}".encode("ascii"),
                   hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def check(key, token, audience, now=None):
    """(claims, refusal). The claims only when every check held."""
    if not key:
        return None, "this node has no assertion key"
    parts = (token or "").strip().split(".")
    if len(parts) != 3:
        return None, "no assertion of the person came with the call"
    want = hmac.new(key, f"{parts[0]}.{parts[1]}".encode("ascii"),
                    hashlib.sha256).digest()
    try:
        got = _unb64(parts[2])
        head = json.loads(_unb64(parts[0]))
        claims = json.loads(_unb64(parts[1]))
    except (ValueError, TypeError):
        return None, "the assertion is not readable"
    if not hmac.compare_digest(want, got):
        return None, "the assertion's signature does not hold"
    if head != _HEAD or not isinstance(claims, dict) \
            or claims.get("v") != VERSION:
        return None, "the assertion is of a kind this node does not issue"
    now = int(time.time() if now is None else now)
    if not isinstance(claims.get("exp"), int) or claims["exp"] <= now:
        return None, "the assertion has expired"
    if claims.get("aud") != audience:
        return None, "the assertion was made for another instance"
    return claims, ""
