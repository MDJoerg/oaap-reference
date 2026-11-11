"""Values the platform produces for a key the app itself defines.

`oaap.apps.runtime` 2.8 (`generate:` in a manifest's config entry) and
RFC-0079 (`generate: true` in a build step). One module for the portal's
"Erzeugen" button AND the host's build step, so both produce the same kind
of value by the same rule.

The KIND always comes from the app's manifest, never from a caller: only the
app knows what its key holds, and a value generated into a field meant for
somebody else's credential is a wrong answer, not a convenience.

Every value is url-safe base64 without padding: no ';', '=', quote or
whitespace, so it is safe in the line-based env file, in a ';'-list, in a
URL and on a command line.
"""
import base64
import secrets

KINDS = ("token", "vapid")

TOKEN_BYTES = 32

# The order of the NIST P-256 group (FIPS 186-4, D.1.2.3). A private key is
# an integer 1 <= k < n; 32 random bytes fall outside with a probability of
# about 2**-32, and are then simply drawn again.
P256_ORDER = int(
    "ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16)


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def kind_of(entry_generate):
    """The manifest's `generate` value -> a known kind or "" (no)."""
    k = str(entry_generate or "").strip().lower()
    return k if k in KINDS else ""


def generate(kind):
    """A fresh value of this kind. Raises ValueError for an unknown kind."""
    if kind == "token":
        return secrets.token_urlsafe(TOKEN_BYTES)
    if kind == "vapid":
        # The format of FAQ F37 and of the usual Web Push libraries: the
        # 32-byte big-endian private scalar of a P-256 key, base64url. The
        # app derives the public key from it.
        while True:
            raw = secrets.token_bytes(32)
            k = int.from_bytes(raw, "big")
            if 1 <= k < P256_ORDER:
                return _b64url(raw)
    raise ValueError(f"unknown kind of generated value '{kind}'")


def valid(kind, value):
    """True when `value` has the form this kind produces (for checks)."""
    v = str(value or "")
    if kind == "vapid":
        try:
            raw = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
        except (ValueError, TypeError):
            return False
        return len(raw) == 32 and 1 <= int.from_bytes(raw, "big") < P256_ORDER
    if kind == "token":
        return len(v) >= 32 and all(c.isalnum() or c in "-_" for c in v)
    return False
