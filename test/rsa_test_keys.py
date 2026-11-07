#!/usr/bin/env python3
"""Nur für Tests: RSA-Schlüssel erzeugen und Tokens unterschreiben, mit der Standardbibliothek.

Ein Ersatz-Anmeldedienst für `test_mcp_act.py` (Kopie aus oaap-apps/apps/mcp-gateway). Die
Primzahlsuche (Miller-Rabin) ist für Tests gut genug und für nichts
sonst gedacht.

Kein eigenes Testprogramm; nur Hilfsmodul.
"""
import base64
import hashlib
import json
import secrets
import time

SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _probably_prime(n, rounds=32):
    if n < 4:
        return n in (2, 3)
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if _probably_prime(c):
            return c


def keypair(bits=2048, e=65537):
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            n = p * q
            if n.bit_length() == bits:
                return n, e, pow(e, -1, phi)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64int(n):
    return b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def jwk(n, e, kid):
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": b64int(n), "e": b64int(e)}


def sign(n, d, data):
    k = (n.bit_length() + 7) // 8
    t = SHA256_PREFIX + hashlib.sha256(data).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def token(key, claims, kid="k1", alg="RS256", head=None):
    n, _, d = key
    h = {"alg": alg, "typ": "JWT", "kid": kid}
    h.update(head or {})
    signed = b64(json.dumps(h).encode()) + "." + b64(json.dumps(claims).encode())
    return signed + "." + b64(sign(n, d, signed.encode("ascii")))


def claims(issuer, audience, sub="person-1", groups=(), lifetime=300, **extra):
    now = int(time.time())
    c = {"iss": issuer, "aud": audience, "sub": sub, "iat": now, "exp": now + lifetime,
         "groups": list(groups), "azp": "mcp-claude"}
    c.update(extra)
    return c


