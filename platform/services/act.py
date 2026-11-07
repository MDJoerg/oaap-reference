"""Checking an act token: a provider's access token addressed to the platform.

RFC-0073 §4 (procedure I). An MCP gateway of a tenant exchanges the token a
person's client gave it for one whose audience is the platform's own client
of the tenant's realm, and hands it to the platform door on its own site.
Identity then checks a token addressed to itself -- what a resource server
does -- and finds the person by the binding `{provider, sub}`.

Checked completely, in this order, and nothing is taken on trust:

1. Shape: three parts, header `alg` = `RS256` and a `kid`. Never `none`,
   never a symmetric algorithm (its key would be a secret the platform has
   no business holding).
2. Signature against the issuer's published key with that `kid`, at least
   2048 bits.
3. `iss` exactly the tenant's issuer, `aud` contains the platform's client,
   `exp`/`nbf`/`iat` with 30 s leeway, `sub` present.

RS256 by hand with `pow()`: the standard library has no RSA, and VERIFYING
a PKCS #1 v1.5 signature (RFC 8017 §8.2.2) is one exponentiation and a
comparison with the expected encoding. No secret is involved -- this only
verifies, it never signs. The same construction as the MCP gateway's
`signin.py`, measured against the real Keycloak on 2026-11-07.

The keys come from the issuer's discovery document; its `jwks_uri` is
fetched only when it has the issuer's own origin (never follow an address
an answer names). No token and no part of one ever lands in a message.

Pure: the fetching is injected, so a test needs neither Flask nor a network.
"""
import base64
import hashlib
import hmac
import json
import threading
import time
from urllib.parse import urlsplit

LEEWAY = 30
MIN_BITS = 2048
REFETCH_S = 60
HEADER = "X-OAAP-Act-Token"
# DigestInfo for SHA-256 (RFC 8017 §9.2, note 1).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class Invalid(Exception):
    """The token is no good. The reason is a sentence without the token."""


def b64(part):
    part = part + "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part.encode("ascii"))


def b64int(part):
    return int.from_bytes(b64(part), "big")


def verify_rs256(n, e, signed, signature):
    """RSASSA-PKCS1-v1_5 with SHA-256. `n`, `e` integers; bytes otherwise."""
    k = (n.bit_length() + 7) // 8
    if n.bit_length() < MIN_BITS:
        raise Invalid("the issuer's key is shorter than 2048 bits")
    if len(signature) != k:
        raise Invalid("signature has the wrong length")
    s = int.from_bytes(signature, "big")
    if s >= n:
        raise Invalid("signature out of range")
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_PREFIX + hashlib.sha256(signed).digest()
    if k < len(t) + 11:
        raise Invalid("the issuer's key is too short")
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    if not hmac.compare_digest(em, expected):
        raise Invalid("signature does not match")


def same_origin(a, b):
    pa, pb = urlsplit(a), urlsplit(b)

    def port(p):
        return p.port or (443 if p.scheme == "https" else 80)
    return (pa.scheme, (pa.hostname or "").lower(), port(pa)) == \
        (pb.scheme, (pb.hostname or "").lower(), port(pb))


class Keys:
    """One issuer's keys: fetched once, remembered, and refetched for an
    unknown `kid` at most once a minute -- made-up tokens must not turn the
    platform into a way of flooding the provider.

    `fetch()` returns the JWKS document or raises Invalid; the caller wires
    it to its own back channel (discovery -> same-origin jwks_uri).
    """

    def __init__(self, fetch, clock=time.monotonic):
        self._fetch = fetch
        self._clock = clock
        self._keys = {}
        self._last = None
        self._lock = threading.Lock()

    def get(self, kid):
        with self._lock:
            if kid in self._keys:
                return self._keys[kid]
            if self._last is not None and self._clock() - self._last < REFETCH_S:
                raise Invalid("unknown key id" if self._keys
                              else "the issuer's keys are not readable right now")
            self._last = self._clock()
            doc = self._fetch()
            found = {}
            for jwk in (doc or {}).get("keys") or ():
                if isinstance(jwk, dict) and jwk.get("kty") == "RSA" \
                        and isinstance(jwk.get("kid"), str) \
                        and jwk.get("use", "sig") == "sig" \
                        and jwk.get("n") and jwk.get("e"):
                    found[jwk["kid"]] = (b64int(jwk["n"]), b64int(jwk["e"]))
            self._keys = found
            if kid not in found:
                raise Invalid("unknown key id")
            return found[kid]


def check(token, keys, issuer, audience, now=None):
    """Check an act token completely. Returns its claims; raises Invalid."""
    now = time.time() if now is None else now
    if not isinstance(token, str) or len(token) > 16384:
        raise Invalid("not a JWT")
    try:
        head_b, body_b, sig_b = token.split(".")
        head = json.loads(b64(head_b))
        claims = json.loads(b64(body_b))
        signature = b64(sig_b)
    except (ValueError, TypeError):
        raise Invalid("not a JWT") from None
    if not isinstance(head, dict) or not isinstance(claims, dict):
        raise Invalid("not a JWT")
    if head.get("alg") != "RS256":
        raise Invalid(f"algorithm {str(head.get('alg'))[:20]!r} is not accepted")
    if not isinstance(head.get("kid"), str):
        raise Invalid("no key id")
    n, e = keys.get(head["kid"])
    verify_rs256(n, e, f"{head_b}.{body_b}".encode("ascii"), signature)
    if not issuer or claims.get("iss") != issuer:
        raise Invalid("issued by another provider than this tenant's")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not audience or audience not in auds:
        raise Invalid("not addressed to the platform's client of this realm")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + LEEWAY <= now:
        raise Invalid("expired")
    for field in ("nbf", "iat"):
        value = claims.get(field)
        if isinstance(value, (int, float)) and value - LEEWAY > now:
            raise Invalid(f"{field} lies in the future")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise Invalid("no subject")
    return claims


def gateway_of(scope, prefix="oaap.mcp.sources:"):
    """The gateway instance a door scope names, or '' for anything else."""
    if not isinstance(scope, str) or not scope.startswith(prefix):
        return ""
    name = scope[len(prefix):]
    return name if name and all(c.isalnum() or c in "-_." for c in name) else ""
