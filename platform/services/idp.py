"""A tenant's identity provider: what may be configured, who may change
it, and what a foreign login is allowed to become.

RFC-0041 (K1-K7). The same reason `place.py` exists: more than one
program needs the same judgement, and this project has paid for the
alternative six times. `identity` performs the login, `appctl`
configures the provider and the policy, the portal shows both.

If those disagreed about, say, whether an incoming login may be matched
to an existing record by e-mail, the result would not be a cosmetic
bug -- it is account takeover by collision, which RFC-0041 K4 forbids
in so many words.

So: **pure functions, no file access, no framework, no network.** Each
caller reads its own files and holds its own HTTP client, and then asks
here. What is shared is the judgement, not the plumbing.

Two rules in this file are not settings and never become settings:

* an incoming login is matched ONLY on `(provider, subject, tenant)` --
  never on e-mail, never on username (`find_binding`);
* nothing a provider asserts becomes an OAAP role (`first_login_grant`).

Both live in functions rather than in a branch at the call site, which
is the lesson 0.1.117 paid for: a rule in a branch can only be READ by
a test, and a text check goes green the moment somebody changes the
condition and leaves the sentence standing.
"""

import base64
import binascii
import ipaddress
import json
import re

# ---------------------------------------------------------------------------
# The provider object (K2)
#
# It is a URL and four fields, and nothing in the platform may say "the
# local Keycloak". An instance may sit on this very node and the
# provider object still names it the way anybody else would. That one
# property is what makes K6's move an edit instead of a project.

PROVIDER_KINDS = ("oidc",)

# K3: the integration ages against somebody else's product, and that
# kind of ageing is invisible until it breaks. So the version the
# provider object was built against is recorded WITH it, and the admin
# path checks it against what the server says about itself
# (/admin/serverinfo -> systemInfo.version). Measured 2026-09-22.
KEYCLOAK_PINNED = "26.7.4"
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:" + KEYCLOAK_PINNED

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECRET_MIN = 16


def _host_of(url):
    """The host of an absolute http(s) URL, lowercased, or ''."""
    m = re.match(r"^https?://([^/?#\s]+)", (url or "").strip(), re.I)
    if not m:
        return ""
    hostport = m.group(1)
    if "@" in hostport:            # userinfo in the URL is never ours
        return ""
    if hostport.startswith("["):   # [::1]:8080
        return hostport[1:hostport.find("]")].lower() if "]" in hostport else ""
    return hostport.split(":")[0].lower()


def unroutable_host(host):
    """True when this host cannot be reached from the internet.

    Loopback, the private ranges, link-local -- and a BARE name with no
    dot in it, which on a node means a container on a Docker network or
    a LAN short name. Everything else is treated as reachable, which is
    the safe direction: a name we cannot classify is classified as
    public.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an address. A bare label (no dot) is a container or a LAN
        # short name; anything with a dot could be anywhere.
        return "." not in host
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def issuer_refusal(issuer):
    """Why this issuer URL may not be used, or '' if it may.

    THE CHANNEL RULE, and it carries more weight here than it looks.
    The ID token is fetched over the back channel and its signature is
    deliberately NOT verified (see `jwt_claims`) -- OIDC Core 3.1.3.7
    allows that precisely because TLS to the token endpoint already
    proves who answered. Take the TLS away and nothing proves it: a
    JWKS document fetched over plain http is as forgeable as the token
    it would have verified. So the transport is not a hardening knob
    here, it IS the authentication of the issuer.

    Hence: `https` everywhere, with ONE exception that is a statement
    of fact rather than a relaxation -- an `http` issuer whose host
    cannot be reached from the internet at all (loopback, a private
    address, a container name). Traffic that never leaves the machine
    or the club's own cable has exactly the exposure the node's own
    portal already has on such a network; refusing it there would be
    theatre, and refusing it anywhere else is not.
    """
    issuer = (issuer or "").strip()
    if not issuer:
        return "an identity provider needs an issuer URL"
    if not re.match(r"^https?://", issuer, re.I):
        return "the issuer must be an absolute http(s) URL"
    if issuer != issuer.rstrip("/"):
        return "the issuer must be written without a trailing slash"
    if any(c in issuer for c in "?# "):
        return "the issuer must carry no query and no fragment"
    host = _host_of(issuer)
    if not host:
        return "the issuer names no host"
    if issuer.lower().startswith("http://") and not unroutable_host(host):
        return (f"'{host}' can be reached from the internet, so the issuer "
                "must be https: the client secret and the identity token "
                "travel this connection, and nothing else proves who "
                "answered it (RFC-0041 3)")
    return ""


def provider_refusal(kind, issuer, client_id, client_secret):
    """The ONE sentence that refuses a provider object.

    One place, because there are two doors to this (the CLI and, later,
    the portal) and the 0.1.115 lesson says the refusal is what has to
    be single-sourced: as long as the sentence can only be made here,
    a third door is countable instead of merely hoped for.
    """
    if kind not in PROVIDER_KINDS:
        return (f"unknown provider kind '{kind}' -- this version speaks "
                f"{', '.join(PROVIDER_KINDS)} (RFC-0041 4)")
    bad = issuer_refusal(issuer)
    if bad:
        return bad
    if not CLIENT_ID_RE.match((client_id or "").strip()):
        return "the client id is missing or malformed"
    if len((client_secret or "").strip()) < SECRET_MIN:
        return (f"the client secret is missing or shorter than {SECRET_MIN} "
                "characters")
    return ""


def provider_of(tenant):
    """The tenant's provider object, normalised, or {} when it has none.

    NEVER carries the client secret: this reads `tenants.json`, which is
    world-readable on the node and travels in a tenant archive. The
    secret lives in a 0600 file of its own and is looked up by tenant
    id -- RFC-0041 3, the same posture as the twin's schema passwords.
    """
    p = ((tenant or {}).get("idp") or {})
    issuer = (p.get("issuer") or "").strip()
    if not issuer:
        return {}
    return {
        "kind": (p.get("kind") or "oidc").strip().lower(),
        "issuer": issuer,
        "client_id": (p.get("client_id") or "").strip(),
        "version": (p.get("version") or "").strip(),
        "label": (p.get("label") or "").strip(),
        "added": (p.get("added") or "").strip(),
    }


def provider_key(provider):
    """The first half of a binding: which provider said this.

    `kind|issuer`, exactly as configured. Repointing a tenant at a
    DIFFERENT issuer therefore breaks every binding it has, loudly and
    by construction -- which is right, because a different issuer is a
    different authority and the subjects it names are its own.

    K6's move is the case that must NOT break, and it does not: the
    club's realm is exported and imported, so the issuer keeps its
    name while the machine behind it changes. That is the whole reason
    the provider object is a URL.
    """
    p = provider or {}
    return f"{(p.get('kind') or 'oidc').lower()}|{p.get('issuer') or ''}"


def discovery_url(issuer):
    """Where an OIDC issuer describes itself."""
    return (issuer or "").rstrip("/") + "/.well-known/openid-configuration"


def endpoints_refusal(doc, issuer):
    """Whether a discovery document may be used for THIS issuer.

    The `issuer` in the document must equal the one we configured, to
    the character. Without that check a redirect or a mistyped path
    would silently hand the login to a different authority, and every
    binding made afterwards would name the wrong one.
    """
    if not isinstance(doc, dict):
        return "the provider's configuration document is not an object"
    said = (doc.get("issuer") or "").strip()
    if said != (issuer or "").strip():
        return (f"the provider calls itself '{said}', not '{issuer}' -- "
                "refusing rather than guessing (RFC-0041 K3)")
    for field in ("authorization_endpoint", "token_endpoint"):
        url = (doc.get(field) or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return f"the provider names no usable {field}"
        if issuer_refusal(url.split("?")[0].rstrip("/")):
            return (f"the provider's {field} is not on a channel this node "
                    "accepts (RFC-0041 3)")
    return ""


def endpoints_of(doc):
    """The three addresses a login needs, from a checked document."""
    return {
        "authorize": (doc.get("authorization_endpoint") or "").strip(),
        "token": (doc.get("token_endpoint") or "").strip(),
        "userinfo": (doc.get("userinfo_endpoint") or "").strip(),
        "end_session": (doc.get("end_session_endpoint") or "").strip(),
    }


# ---------------------------------------------------------------------------
# The policy: what a first login BECOMES (K4, K4b, K7)
#
# The binding is a platform rule and is not configurable. The
# consequence of a first login belongs to the tenant, because a club
# that administers its own realm wants something different from a
# customer whose people the operator admits by hand -- and because
# Joerg was right that there are real cases for all three.

FIRST_LOGIN_VALUES = ("eingang", "role", "groups")
FIRST_LOGIN_DEFAULT = "eingang"

# Which roles a first login may hand out at all. Deliberately a list of
# what IS allowed and not of what is forbidden: a role added to the
# platform later is then not automatically grantable by a foreign login
# that nobody re-read this list for.
#
# server_admin, support (node-wide, RFC-0039) and tenant_admin (the
# whole tenant, oaap.core.tenant 2.3) are absent on purpose. A door that
# opens on somebody else's assertion must not be able to open the node.
GRANTABLE_AT_FIRST_LOGIN = ("admin", "keyuser", "user", "guest", "partner")
NEVER_AT_FIRST_LOGIN = ("server_admin", "support", "tenant_admin")

GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
REALM_GROUP_MAX = 120
REASON_MIN = 10


def policy_of(tenant):
    """The tenant's identity policy -- ALWAYS a complete answer.

    A tenant that has never been configured reads as the default, so
    there is one code path and not a configured and an unconfigured
    one. `set` says whether anybody actually chose any of it, which is
    what the portal shows a tenant_admin.
    """
    p = ((tenant or {}).get("idp_policy") or {})
    first = (p.get("first_login") or "").strip().lower()
    if first not in FIRST_LOGIN_VALUES:
        first = FIRST_LOGIN_DEFAULT
    raw_map = p.get("group_map")
    group_map = {}
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            key, val = str(k).strip(), str(v).strip().lower()
            if key and GROUP_RE.match(val):
                group_map[key] = val
    return {
        "first_login": first,
        "default_role": (p.get("default_role") or "").strip().lower(),
        "group_map": group_map,
        "self_registration": bool(p.get("self_registration")),
        "reason": (p.get("reason") or "").strip(),
        "set": bool(p),
    }


def policy_target(role, own_tenant, wanted=""):
    """Whose identity policy this caller may change: (tenant_id, refusal).

    K4b, and it is the one place in this codebase where `tenant_admin`
    gets LESS than it does elsewhere. The reason is the shared node: a
    tenant that could open itself would be opening a door on a machine
    that carries other customers, and the operator would learn about it
    afterwards. So the tenant SEES the setting and the operator moves
    it, and every move is an entry in that tenant's own log.

    A function rather than a condition at each call site, because a
    rule in a branch can only be read by a test and a text check goes
    green the moment the condition changes underneath the sentence.
    """
    if role != "server_admin":
        return "", ("who gets into a tenant through a provider is the "
                    "operator's switch, not the tenant's: this needs "
                    "server_admin (RFC-0041 K4b)")
    tid = (wanted or own_tenant or "").strip()
    if not tid:
        return "", "no tenant named"
    return tid, ""


def policy_refusal(first_login, default_role, self_registration,
                   group_map=None, reason=""):
    """The ONE sentence that refuses an identity policy.

    The combination that has to be caught here is `role`/`groups`
    together with self-registration: each is defensible alone, and
    together they hand rights to anybody who can reach the page while
    nothing else in the system would notice. It is refused unless it is
    set DELIBERATELY, with a reason -- and the reason is what lands in
    the tenant's log, so that the decision has an author.
    """
    first = (first_login or "").strip().lower()
    if first not in FIRST_LOGIN_VALUES:
        return (f"unknown first-login policy '{first_login}' -- one of "
                f"{', '.join(FIRST_LOGIN_VALUES)} (RFC-0041 K4)")
    role = (default_role or "").strip().lower()
    if first == "eingang":
        if role:
            return ("'eingang' grants nothing, so it takes no default role "
                    "-- say 'role' if a role is meant")
    else:
        if not role:
            return f"'{first}' needs a default role, or it grants nothing"
        if role in NEVER_AT_FIRST_LOGIN:
            return (f"'{role}' is never granted by a first login: a door "
                    "that opens on somebody else's assertion must not be "
                    "able to open the node or the whole tenant "
                    "(RFC-0041 K4)")
        if role not in GRANTABLE_AT_FIRST_LOGIN:
            return (f"'{role}' is not a role this platform hands out at "
                    f"first login ({', '.join(GRANTABLE_AT_FIRST_LOGIN)})")
    if first != "groups" and group_map:
        return "a group mapping only means something with 'groups'"
    for realm_group, oaap_group in (group_map or {}).items():
        if not str(realm_group).strip() or len(str(realm_group)) > REALM_GROUP_MAX:
            return f"realm group '{realm_group}' is empty or too long"
        if not GROUP_RE.match(str(oaap_group).strip().lower()):
            return (f"'{oaap_group}' is not a visibility group name "
                    "([a-z0-9][a-z0-9._-]*)")
    if self_registration and first != "eingang":
        if len((reason or "").strip()) < REASON_MIN:
            return (f"self-registration together with '{first}' hands a role "
                    "to anybody who can reach the registration page. It is "
                    "allowed only deliberately: say why, and the reason goes "
                    "into this tenant's log (RFC-0041 K7)")
    return ""


def first_login_grant(policy, claims):
    """What a FIRST login becomes: (roles, groups).

    This is the second rule that is not a setting. A provider may
    assert anything it likes -- `realm_access.roles`, a `roles` claim, a
    group named `server_admin` -- and none of it is read here. The
    roles come from the tenant's own policy and the groups from a
    mapping the OPERATOR wrote; an unmapped realm group grants nothing,
    so a club cannot widen its own rights by inventing a group.

    Written as a function precisely so a test can hand it a hostile
    assertion and watch nothing happen.
    """
    # Normalised again here, never trusted as handed in: this function
    # is the last gate before rights exist, and it reads the same keys
    # whether it was given a stored record or an already-normalised one.
    p = policy_of({"idp_policy": policy or {}})
    first = p["first_login"]
    role = (p.get("default_role") or "").strip().lower()
    if first == "eingang" or not role or role in NEVER_AT_FIRST_LOGIN \
            or role not in GRANTABLE_AT_FIRST_LOGIN:
        roles = []
    else:
        roles = [role]
    groups = []
    if first == "groups":
        mapping = p.get("group_map") or {}
        for asserted in realm_groups(claims):
            mapped = mapping.get(asserted)
            if mapped and mapped not in groups:
                groups.append(mapped)
    return roles, sorted(groups)


def realm_groups(claims):
    """The groups the provider asserted, as plain strings.

    Read ONLY so they can be looked up in the operator's mapping. A
    name in this list is never used for anything else -- not as a role,
    not as a group, not as a tenant.
    """
    raw = (claims or {}).get("groups")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for g in raw:
        g = str(g).strip().lstrip("/")
        if g and len(g) <= REALM_GROUP_MAX and g not in out:
            out.append(g)
    return out


# ---------------------------------------------------------------------------
# The binding (K4): (provider, subject) and nothing else


def binding_of(user):
    """(provider_key, subject) of a user record, or ('', '')."""
    b = ((user or {}).get("idp") or {})
    return (b.get("provider") or "").strip(), (b.get("subject") or "").strip()


def find_binding(users, pkey, subject, tenant):
    """The local record this incoming login IS, or None.

    THE RULE, and it is not a setting: an incoming login is matched on
    `(provider, subject, tenant)` and on nothing else. Not on e-mail --
    that is account takeover by collision, and the address an
    unverified provider asserts is worth exactly nothing. Not on
    username -- the same, with extra steps.

    The tenant is part of the key rather than a consequence of it,
    because RFC-0022 D3 says the same human in two tenants is two
    principals. Two tenants pointed at one realm is unusual and
    perfectly legal, and it must produce two records, not one that
    straddles the boundary.
    """
    pkey = (pkey or "").strip()
    subject = (subject or "").strip()
    tenant = (tenant or "").strip()
    if not pkey or not subject:
        return None
    for u in users or []:
        have_p, have_s = binding_of(u)
        if have_p == pkey and have_s == subject \
                and (u.get("tenant") or "").strip() == tenant:
            return u
    return None


def has_local_password(user):
    """Whether this record can be logged in with a password HERE.

    RFC-0041 3: a login never falls back to a local password for a
    realm-backed identity. That sentence is only true if the fallback
    does not exist -- a record a provider introduced carries no
    password hash, and the local form must say so rather than leave it
    to what a hashing library happens to do with an empty string. The
    same guard already had to be written out for the machine
    principals of RFC-0027; this is that rule, asked once.

    A record MAY hold both a binding and a password: a tenant keeps
    local users alongside a realm (K2), and an operator may deliberately
    give a realm-backed person a local credential too. What is refused
    is the fallback nobody chose, not the credential somebody set.
    """
    h = str((user or {}).get("password_hash") or "")
    return bool(h) and "$" in h


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


def local_username(claims, taken, fallback="user"):
    """A local name for a person a provider just introduced.

    The provider SUGGESTS a name; it never dictates one. A suggestion
    that is already taken gets a number, because the alternative --
    reusing the record that has it -- is exactly the takeover
    `find_binding` refuses one function above. The name is a name
    (RFC-0040): what anything anchors on is the record's own id.
    """
    taken = {str(t).strip().lower() for t in (taken or ())}
    wish = ""
    for key in ("preferred_username", "email", "name"):
        raw = str((claims or {}).get(key) or "").strip().lower()
        if not raw:
            continue
        if key == "email":
            raw = raw.split("@")[0]
        cleaned = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-._")
        cleaned = re.sub(r"-{2,}", "-", cleaned)[:40]
        if USERNAME_RE.match(cleaned):
            wish = cleaned
            break
    if not wish:
        sub = re.sub(r"[^a-z0-9]+", "", str((claims or {}).get("sub") or ""))
        wish = (fallback + "-" + sub)[:40] if sub else fallback
        if not USERNAME_RE.match(wish):
            wish = "user-neu"
    if wish not in taken:
        return wish
    for n in range(2, 1000):
        cand = f"{wish[:36]}-{n}"
        if cand not in taken:
            return cand
    return ""


# ---------------------------------------------------------------------------
# Reading a token, and what may be believed of it


def jwt_claims(token):
    """The claims of a JWS compact token, WITHOUT checking its signature.

    That is deliberate and it is allowed -- OIDC Core 3.1.3.7: a token
    received through direct communication with the token endpoint may
    be validated by the TLS server validation in place of the
    signature. This implementation only ever obtains ID tokens that
    way, over a channel `issuer_refusal` has already ruled acceptable.

    Which is why that rule up there is load-bearing and not cosmetic:
    remove it and this function believes whatever answered the socket.
    Everything else a token must satisfy is checked in
    `claims_refusal`, which is not optional.
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


CLOCK_SKEW = 120


def claims_refusal(claims, issuer, client_id, nonce, now_epoch):
    """Why these claims may not become a session, or ''."""
    if not isinstance(claims, dict):
        return "the provider returned no readable identity token"
    if (claims.get("iss") or "").strip() != (issuer or "").strip():
        return (f"the token names the issuer '{claims.get('iss')}', not "
                f"'{issuer}'")
    aud = claims.get("aud")
    aud = [aud] if isinstance(aud, str) else list(aud or ())
    if (client_id or "") not in aud:
        return "the token was not issued for this node"
    if not str(claims.get("sub") or "").strip():
        return "the token names no subject, so there is nothing to bind to"
    if (claims.get("nonce") or "") != (nonce or ""):
        return "the token does not answer this login attempt"
    try:
        exp = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    if exp and exp + CLOCK_SKEW < int(now_epoch):
        return "the token has expired"
    return ""


def second_factor(claims):
    """What the provider said about a second factor, as one short string.

    OAAP does not enforce 2FA and must not claim to (K7): Keycloak
    decides, and OAAP only learns that the login succeeded. What it CAN
    do is record what was asserted, so that an audit entry is able to
    say it. '' means the provider said nothing, which is not the same
    as 'no second factor' and is written down as such.
    """
    amr = (claims or {}).get("amr")
    amr = [amr] if isinstance(amr, str) else list(amr or ())
    amr = [str(a).strip() for a in amr if str(a).strip()]
    acr = str((claims or {}).get("acr") or "").strip()
    parts = []
    if amr:
        parts.append("amr=" + ",".join(amr[:5]))
    if acr:
        parts.append("acr=" + acr)
    return " ".join(parts)


def profile_from(claims):
    """Display name and e-mail a provider offered, for a local record.

    `email_verified` is carried across ONLY when the provider asserted
    it -- RFC-0040's flag says whether anybody proved the address, and
    copying an unproven address with the flag set would make the
    platform assert something nobody checked.
    """
    c = claims or {}
    name = str(c.get("name") or "").strip()
    if not name:
        given = str(c.get("given_name") or "").strip()
        family = str(c.get("family_name") or "").strip()
        name = " ".join(p for p in (given, family) if p)
    return {
        "display_name": name[:80],
        "email": str(c.get("email") or "").strip()[:254],
        "email_verified": bool(c.get("email_verified")),
    }
