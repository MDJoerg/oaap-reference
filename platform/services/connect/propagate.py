"""The signed-in person, carried through (oaap.net.destinations 2.9,
RFC-0066 stage 1).

What the SAP Cloud Connector does for BTP, done by this node for its
own apps: the app proves on whose behalf it calls (the assertion
identity signed for exactly this instance), this module names the
backend user, issues a client certificate for that user from the
tenant's own CA, and builds the TLS context that presents it -- to a
server whose certificate was pinned when the destination was made, and
to nothing else.

Everything here is a plain function, so test_propagation.py can run the
rules instead of reading them; app.py only moves bytes.

The tenant part of a certificate's subject is written HERE, from the
destination's tenant, never from the app or the person. The target's
mapping rule filters on it (measured on a4h, 2026-10-27: CERTRULE's
subject filter), so that one line is the tenant border inside the
target.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import ssl
import tempfile
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ASSERTION_HEADER = "X-OAAP-User-Assertion"
CA_YEARS = 3
LEAF_BITS = 2048
CA_BITS = 3072
LIFETIME_MIN, LIFETIME_MAX = 1, 60
# MUST match services/idp.py PROPAGATE_CLAIM_RE -- test_propagation.py
# compares them.
PROPAGATE_CLAIM_RE = r"sap_user(_[a-z0-9]{1,16})?"
NAME_CHECKS = {"sap": r"[A-Z0-9_]{1,12}"}


# --------------------------------------------------------- the assertion
# A copy of services/assertion.py `check` -- this image is built from its
# own directory. test_propagation.py mints with the one and checks with
# the other.

_HEAD = {"alg": "HS256", "typ": "oaap-assertion"}


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_assertion(key, token, audience, now=None):
    """(claims, refusal)."""
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
    if head != _HEAD or not isinstance(claims, dict) or claims.get("v") != 1:
        return None, "the assertion is of a kind this node does not issue"
    now = int(time.time() if now is None else now)
    if not isinstance(claims.get("exp"), int) or claims["exp"] <= now:
        return None, "the assertion has expired"
    if claims.get("aud") != audience:
        return None, "the assertion was made for another instance"
    return claims, ""


def read_key(path):
    try:
        with open(path, encoding="ascii") as f:
            raw = bytes.fromhex(f.read().strip())
    except (OSError, ValueError):
        return b""
    return raw if len(raw) >= 32 else b""


# ------------------------------------------------------- the backend user

def backend_user(rules, propagate, name_check=""):
    """(name, refusal). The first rule that yields a name wins.

    No rule, no name -- and never a technical user instead (RFC-0065
    D8): a person who cannot be carried through is refused, with the
    reason, because a fallback would let them act with rights that are
    not theirs and nobody in the target would see it.
    """
    prop = propagate if isinstance(propagate, dict) else {}
    for r in rules or []:
        if not isinstance(r, dict) or r.get("rule") != "claim":
            continue
        claim = str(r.get("name") or "")
        if not re.fullmatch(PROPAGATE_CLAIM_RE, claim):
            continue
        value = prop.get(claim)
        if not isinstance(value, str) or not value:
            continue
        name = value.upper() if name_check == "sap" else value
        pattern = NAME_CHECKS.get(name_check)
        if pattern and not re.fullmatch(pattern, name):
            return "", (f"the user name in '{claim}' is not a valid "
                        f"{name_check.upper()} user name")
        return name, ""
    named = ", ".join(str(r.get("name")) for r in rules or []
                      if isinstance(r, dict)) or "-"
    return "", (f"no backend user is known for this person ({named} not "
                f"set) -- an administrator of the sign-in service maintains it")


# ------------------------------------------------------------------- CA

def _name(tenant_label, cn):
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OAAP"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, tenant_label),
        x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def ca_paths(ca_dir, tenant_id):
    return (os.path.join(ca_dir, f"{tenant_id}.key"),
            os.path.join(ca_dir, f"{tenant_id}.crt"))


def _write_private(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def ensure_ca(ca_dir, tenant_id, tenant_label, now=None):
    """(cert, key, created). One CA per tenant, made once.

    Kept like a destination secret (spec 2.9.3): 0600, in a directory
    only this service mounts, not in the backup. A lost CA is made again
    and imported again; a copied one would be a door into every target
    that trusts it.
    """
    key_path, crt_path = ca_paths(ca_dir, tenant_id)
    if os.path.exists(key_path) and os.path.exists(crt_path):
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), None)
        with open(crt_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        return cert, key, False
    os.makedirs(ca_dir, exist_ok=True)
    try:
        os.chmod(ca_dir, 0o700)
    except OSError:
        pass
    now = now or datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=CA_BITS)
    name = _name(tenant_label, f"OAAP Propagation {tenant_label}")
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=365 * CA_YEARS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False), True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                key.public_key()), False)
            .sign(key, hashes.SHA256()))
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    with open(crt_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key, True


def issue(ca_cert, ca_key, tenant_label, user, user_id, minutes, now=None):
    """(cert_pem, key_pem, serial_hex, not_after). The person's certificate.

    `O=OAAP, OU=<tenant>, CN=<user>`, the person's UUID as a SAN URI for
    the target's own audit trail, client authentication only, valid for
    `minutes` -- a minute back for clocks that disagree.
    """
    minutes = max(LIFETIME_MIN, min(LIFETIME_MAX, int(minutes or 10)))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=LEAF_BITS)
    not_after = now + datetime.timedelta(minutes=minutes)
    b = (x509.CertificateBuilder()
         .subject_name(_name(tenant_label, user))
         .issuer_name(ca_cert.subject)
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(minutes=1))
         .not_valid_after(not_after)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
         .add_extension(x509.KeyUsage(
             digital_signature=True, content_commitment=False,
             key_encipherment=True, data_encipherment=False,
             key_agreement=False, key_cert_sign=False, crl_sign=False,
             encipher_only=False, decipher_only=False), True)
         .add_extension(x509.ExtendedKeyUsage(
             [ExtendedKeyUsageOID.CLIENT_AUTH]), False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
             ca_key.public_key()), False))
    if user_id:
        b = b.add_extension(x509.SubjectAlternativeName(
            [x509.UniformResourceIdentifier(f"urn:oaap:user:{user_id}")]),
            False)
    cert = b.sign(ca_key, hashes.SHA256())
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()),
            format(cert.serial_number, "x"), not_after)


# ------------------------------------------------------- the pinned server

def pem_sha256(pem):
    """Hex SHA-256 of the DER of a PEM certificate -- what an operator
    compares with what the target's administrator sees in STRUST."""
    cert = x509.load_pem_x509_certificate(
        pem.encode() if isinstance(pem, str) else pem)
    return hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def weak_key(pem):
    cert = x509.load_pem_x509_certificate(
        pem.encode() if isinstance(pem, str) else pem)
    pub = cert.public_key()
    return isinstance(pub, rsa.RSAPublicKey) and pub.key_size < 2048


def client_context(cert_pem, key_pem, server_pem, tmp_dir=None):
    """The TLS context for one person toward one pinned server.

    Trusts the pinned certificate and nothing else: it is the only
    anchor, so a different server -- even one with a publicly valid
    certificate for the same name -- fails the handshake. The host name
    is not compared: the pin is the stronger statement, and SAP's
    default certificate names `*.dummy.nodomain`. A key shorter than
    2048 bits is accepted by lowering OpenSSL's security level for THIS
    context only, and only because the certificate is pinned (spec
    2.9.2) -- never by switching verification off.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if weak_key(server_pem):
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.load_verify_locations(cadata=server_pem if isinstance(server_pem, str)
                              else server_pem.decode())
    # load_cert_chain reads files only. The key exists on disk for the
    # length of this call, 0600, in a directory of this process, and is
    # gone before the context is used.
    fd, path = tempfile.mkstemp(prefix="pp-", suffix=".pem", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(cert_pem + b"\n" + key_pem)
        ctx.load_cert_chain(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return ctx


class CertCache:
    """One certificate per (destination, person), reused until shortly
    before it expires. Keyed by the person's id AND the backend name, so
    a changed `sap_user` gets a new certificate at once."""

    MARGIN = datetime.timedelta(minutes=1)

    def __init__(self):
        self.items = {}

    def get(self, key, now=None):
        now = now or datetime.datetime.now(datetime.timezone.utc)
        hit = self.items.get(key)
        if hit and hit["not_after"] - self.MARGIN > now:
            return hit
        self.items.pop(key, None)
        return None

    def put(self, key, ctx, serial, not_after):
        self.items[key] = {"ctx": ctx, "serial": serial,
                           "not_after": not_after}
        if len(self.items) > 2000:
            oldest = sorted(self.items, key=lambda k: self.items[k]["not_after"])
            for k in oldest[:500]:
                self.items.pop(k, None)
