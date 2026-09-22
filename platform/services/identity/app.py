"""OAAP built-in minimal identity provider (oaap.core.identity, skeleton).

Local user store on disk, username + password, session cookie. The
gateway calls /verify on every request (forward auth); /auth/* is the
only identity surface exposed through the gateway. /internal/* is
reachable only on the internal container network.

Spec oaap.core.identity 2.3: /verify evaluates the current user store
on every call — sessions carry only the username, never roles — so
role changes and deactivation act on the user's next request.
"""

import contextlib
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from datetime import timedelta
from urllib.parse import quote

from flask import (Flask, make_response, redirect, render_template_string,
                   request, session)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

# The mount inside the container. Overridable only so that a test can
# drive this service without inventing a /data on the developer's
# machine -- the variable is never set in production, and appctl reads
# its own data directory the same way.
DATA_DIR = os.environ.get("OAAP_IDENTITY_DATA_DIR", "/data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Standard roles a user account may hold (RFC-0002 + RFC-0008; `public`
# is a route marker, not a role). server_admin is platform authority
# (users, groups, edge/external routing, backup, visibility bypass) and
# is never forwarded to apps as something app-specific — see RFC-0008.
# admin is unchanged: an app-facing role only, carrying no platform
# authority by itself. tenant_admin (oaap.core.tenant 2.3) is the half
# RFC-0008 left open: platform authority INSIDE ONE TENANT -- the
# tenant of the holder's own record, never one named in a request.
# support (RFC-0039) is the read-only counterpart to server_admin: the
# service provider who looks after this node and reads its node-wide
# status surfaces, changing nothing.
ASSIGNABLE_ROLES = ("server_admin", "tenant_admin", "support", "admin",
                    "keyuser", "user", "guest", "partner")
# Roles whose authority reaches past a tenant. server_admin administers
# the node; support reads the health page, which lists every instance on
# the machine. A tenant_admin may grant neither -- otherwise the role is
# a two-step path out of its own tenant (oaap.core.tenant 2.3 rule 1).
#
# RFC-0039: `partner` used to stand here, carrying that node-wide read
# while RFC-0002 published it as "external partner organization" -- an
# app-facing classification. One word, two meanings, and the unsafe one
# was the one nobody wrote down. The privilege moved out to `support`;
# `partner` stayed and now means only what RFC-0002 always said, which
# is why a tenant_admin may hand it out like any other app-facing role.
NODE_WIDE_ROLES = frozenset({"server_admin", "support"})
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")

# RFC-0040: the person behind the name.
#
# THE IDENTITY. Every user record carries a UUID that is assigned once,
# never changed and never reused -- not even after deactivation. It is
# what an app anchors on; `username` goes back to being what it always
# was, a name. UUID rather than the short hex RFC-0026 gave instances,
# because this identifier will be mapped one-to-one onto a foreign
# provider's subject claim (RFC-0040 §6) and it leaves the node.
#
# THE ADDRESS. `email` plus `email_verified`. The platform sends no
# mail in this version, so the flag is set only by an administrator
# asserting it or, later, by the verification flow a foreign identity
# provider brings. Setting a DIFFERENT address clears the flag -- an
# address that changed is an address nobody proved.
#
# Deliberately loose validation: one '@', something on either side of
# it, no spaces, no control characters, 254 characters at most. The
# platform is not the arbiter of address syntax (RFC 5322 allows more
# than anyone wants to implement), and the flag, not the regex, is what
# says whether an address is real.
EMAIL_MAX = 254
EMAIL_RE = re.compile(r"^[^\s@,;<>\"]+@[^\s@,;<>\".]+(\.[^\s@,;<>\".]+)+$")
DISPLAY_NAME_MAX = 80
# Free-form visibility tags (RFC-0007) — no registry, a group exists
# the moment any user carries it. Kept short and simple like usernames.
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

# API keys (RFC-0027). A key is the second way to answer the one
# question /verify asks -- WHICH PRINCIPAL IS THIS -- and nothing after
# that answer knows the difference: roles, visibility groups, tenant and
# the identity headers are the same code for a cookie and for a key.
#
# Presented as `Authorization: Bearer oaapk_<id>_<secret>`. The id
# travels in clear so the audit log and the portal can name a key
# without ever holding its secret; only a hash of the secret is stored.
KEYS_FILE = os.path.join(DATA_DIR, "api-keys.json")
KEY_TOKEN_RE = re.compile(r"^oaapk_([0-9a-f]{8})_([A-Za-z0-9_-]{22,})$")
KEY_DEFAULT_DAYS, KEY_MAX_DAYS = 90, 365
# RFC-0027 D2: no key ever carries platform authority. A leaked
# server_admin key is the whole node, and unlike a password it lives in
# a config file, a CI variable, a screenshot. Enforced twice -- refused
# at issue, and filtered again at use, because a role can be added to a
# principal after its key was written.
KEY_FORBIDDEN_ROLES = frozenset({"server_admin"})
# RFC-0028: a terminal session. A browser cannot put an Authorization
# header on an ordinary navigation, so a kiosk cannot present a key the
# way a script does -- it needs a cookie. Enrolment exchanges the key
# for one, ONCE, and the session keeps naming the key: every request
# re-checks it, so `oaap key revoke` kills the terminal within seconds
# and deactivating the principal does the same. The cookie is the
# carrier; the key remains the credential.
TERMINAL_SESSION_DAYS = 365

app = Flask(__name__)
app.secret_key = os.environ["SESSION_SECRET"]
app.permanent_session_lifetime = timedelta(days=TERMINAL_SESSION_DAYS)
app.config.update(
    SESSION_COOKIE_NAME="oaap_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

EXTERNAL_HOST_FILE = "/platform-apps/external.json"
# Accounts and tenants of this node (oaap.core.tenant 0.1). Identity
# READS this file and never writes it — appctl on the host owns it. The
# mount is read-only, which makes that structural rather than a promise.
TENANTS_FILE = "/platform-apps/tenants.json"


def default_tenant_id():
    """The default tenant's UUID, or '' when the node has none yet.

    Empty is a normal state, not an error: identity may start before
    the host-side migration has run. Everything here then reads the
    absent tenant as the default one (spec 2.2), which is exactly what
    a single-tenant node means anyway.
    """
    try:
        with open(TENANTS_FILE, encoding="utf-8") as f:
            tenants = (json.load(f) or {}).get("tenants") or {}
    except (OSError, ValueError):
        return ""
    for tid, t in sorted(tenants.items()):
        if t.get("label") == "default":
            return tid
    return ""


def known_tenants():
    try:
        with open(TENANTS_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("tenants") or {}
    except (OSError, ValueError):
        return {}


def resolve_tenant(ref):
    """Spec 2.5, and the difference between the two rules is the whole
    safety argument: ABSENT means the default tenant (that is how every
    record written before tenants existed reads), UNKNOWN means None --
    refused, never healed onto the default one. Mapping an unknown
    tenant onto `default` would move a customer's user into the
    OPERATOR's tenant, which is a data leak wearing the clothes of
    robustness.
    """
    ref = (ref or "").strip()
    if not ref:
        return default_tenant_id() or ""
    return ref if ref in known_tenants() else None


def single_tenant():
    """While true, nothing about tenants may be visible anywhere."""
    return len(known_tenants()) <= 1


AUDIT_LOG = "/audit/tenant-log.jsonl"


def audit(action, tenant, subject, result="ok", who="?", role="-", detail=""):
    """One line in the tenant audit log (oaap.core.tenant 1.7).

    Identity writes here because user administration is the one state
    change that never passes through the host -- appctl writes
    everything else into the same file. Both only ever APPEND single
    short lines, and nothing rewrites the file, which is what makes two
    writers safe. A failure to write must not fail the operation: the
    log is a record, not a lock. It is reported instead.
    """
    import datetime
    entry = {"when": datetime.datetime.now(datetime.timezone.utc)
                              .isoformat(timespec="seconds"),
             "who": who or "?", "role": role or "-", "action": action,
             "tenant": tenant or "", "tenant_label": _label_of(tenant),
             "subject": subject, "result": result}
    if detail:
        entry["detail"] = detail
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARNING: could not write the tenant audit log: {e}", flush=True)


def _label_of(tid):
    return (known_tenants().get(tid or "") or {}).get("label", "")


def authority(actor_name):
    """What an actor may do, and where (spec 2.3).

    Returns (role, tenant, error). `role` is "server_admin" (everything,
    RFC-0022 D5), "tenant_admin" (their own tenant and nothing else) or
    "" (nothing). The tenant comes from the ACTOR'S OWN RECORD -- a
    tenant that arrives in a request is a tenant the caller chose, and a
    caller who chooses their own tenant has no boundary.
    """
    u = find_user(load_users(), actor_name or "")
    if not u or not u.get("active", True):
        return "", "", "Der handelnde Benutzer ist unbekannt oder inaktiv."
    roles = set(u.get("roles") or [])
    if "server_admin" in roles:
        return "server_admin", resolve_tenant(u.get("tenant")) or "", ""
    if "tenant_admin" not in roles:
        return "", "", "Benutzerverwaltung erfordert server_admin oder tenant_admin."
    own = resolve_tenant(u.get("tenant"))
    if own is None:
        return ("tenant_admin", "",
                "Dein Konto nennt einen Mandanten, den dieser Knoten nicht hat.")
    return "tenant_admin", own, ""


def may_see(role, actor_tenant, u):
    """Whether an actor may see/act on one user record.

    A tenant_admin sees their own tenant only. Everyone else who got
    this far is a server_admin, who sees everything (D5) -- and whose
    every action lands in the customer's own audit log, which is the
    counterweight.
    """
    if role != "tenant_admin":
        return True
    return (resolve_tenant(u.get("tenant")) or "") == actor_tenant


def _external_host():
    try:
        with open(EXTERNAL_HOST_FILE, encoding="utf-8") as f:
            return json.load(f).get("host", "").lower()
    except (OSError, ValueError):
        return ""


class DomainAwareSessionInterface(SecureCookieSessionInterface):
    """Widen the session cookie to the registered external hostname.

    Externally the apps live on <instance>.<host> (RFC-0005 level 3);
    a host-only cookie set on the portal apex would never reach them.
    On LAN requests (IP + ports share one host) the default host-only
    behavior remains.
    """

    def get_cookie_domain(self, app):
        ext = _external_host()
        if ext:
            host = request.host.split(":")[0].lower()
            if host == ext or host.endswith("." + ext):
                return ext
        return super().get_cookie_domain(app)


app.session_interface = DomainAwareSessionInterface()


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _save(path, data):
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# THE WRITE LOCK ON THE USER FILE (RFC-0040 §4, D6)
#
# Every change to a user goes read -> modify -> write-whole-file. Two of
# those at the same time lose one of the changes, and lose it silently:
# the second writer's file is complete and valid, it simply does not
# contain what the first one did.
#
# Today that is nearly unreachable -- an administrator creates accounts
# one at a time through the portal. It stops being unreachable the
# moment records are created by INCOMING TRAFFIC, which is exactly what
# a foreign identity provider brings (RFC-0040 §6) and what
# self-registration needs. RFC-0040 fixes it now rather than in the RFC
# that needs it, because by then the bug is live.
#
# The service already knew how: _braked_note() takes an exclusive flock
# for precisely this reason. It was simply never applied to the file
# that matters most.
#
# THE LOCK MUST SPAN THE READ. A lock around the write alone fixes
# nothing -- the stale copy was read before it was taken. So the unit is
# `with users_rw() as users:`, which holds the lock across load, change
# and save. Reads (verify, whoami, authority) stay lock-free: _save()
# swaps the file in with os.replace(), so a reader sees the old file or
# the new one, never half of either.
USERS_LOCK_FILE = os.path.join(DATA_DIR, "users.lock")


@contextlib.contextmanager
def _users_lock():
    """Exclusive lock for a read-modify-write of the user file.

    A lock FILE of its own, never users.json itself: _save() replaces
    that file, so a lock held on it would be a lock on an inode nobody
    writes to any more.

    On a platform without fcntl (a developer's Windows machine running
    the tests) this is a no-op -- said out loud rather than hidden,
    because the guarantee it makes is then absent. The service itself
    only ever runs in a Linux container.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    fd = os.open(USERS_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def users_rw():
    """The user store, open for change: lock, load, hand over.

    The caller mutates the list and calls save_users() on it INSIDE the
    block -- or returns without saving, which is a legitimate outcome
    (a validation error changes nothing). Either way the lock is held
    from before the read until after the write.
    """
    with _users_lock():
        yield load_users()


def save_users(users):
    """Write the user store back. Only ever called inside users_rw().

    Assigns an `id` to any record that lacks one, so a creation path
    that forgets cannot produce a record without an identity. That has
    happened four times in this codebase with other identifiers: a path
    was rebuilt and one writer stayed on the old one. Here the missing
    field would not be an error anybody sees -- it would be an app
    anchoring its permissions on an empty string.

    Takes no lock of its own: flock is per file descriptor, so opening
    the lock file a second time in the same process would wait for a
    lock this process already holds. The lock belongs to users_rw().
    """
    for u in users:
        if not u.get("id"):
            u["id"] = new_user_id()
    _save(USERS_FILE, users)


def new_user_id():
    """A fresh user identity (RFC-0040 D1): a UUID, never reused."""
    return str(uuid.uuid4())


def load_users():
    users = _load(USERS_FILE, [])
    for u in users:
        u.setdefault("display_name", "")
        u.setdefault("active", True)
        # RFC-0040 §3.1: the identity, as opposed to the name. Read as
        # "" when absent so that no reader crashes on a hand-edited
        # file; _backfill_user_ids() below makes absent a state that
        # does not survive a restart, and save_users() one that does
        # not survive a write.
        u.setdefault("id", "")
        # RFC-0040 §3.2: the address, and whether anybody proved it.
        u.setdefault("email", "")
        u.setdefault("email_verified", False)
        # Sessions are stateless (signed cookie, no server-side store) —
        # logout alone cannot invalidate a copy of the cookie held
        # elsewhere (another tab/window, browser history). This counter,
        # bumped on logout, is what actually revokes it (see verify()).
        u.setdefault("session_epoch", 0)
        # RFC-0007: free-form visibility group tags.
        u.setdefault("groups", [])
        # RFC-0027 D1 (Joerg, 2026-09-02): machine principals are
        # users with a kind, not a parallel species -- so tenant,
        # roles, visibility groups, deactivation and audit all apply
        # without a second implementation, and the tenant check stays
        # in ONE place. A machine has no password and cannot use the
        # login form; it authenticates by key only.
        u.setdefault("kind", "human")
        # oaap.core.tenant 1.1: which tenant this user belongs to.
        # Empty means the default tenant — that is the reading rule for
        # every record written before tenants existed, and it is why
        # this migration cannot break a running node.
        u.setdefault("tenant", "")
    return users


def _migrate_tenant_once():
    """oaap.core.tenant 1.5 step 2: every existing user joins the
    default tenant.

    Strictly speaking this changes no behaviour — an absent tenant
    already READS as the default one (spec 2.2). It is done anyway so
    that the stored data says what is true, and so `oaap tenant check`
    can tell "belongs to the default tenant" apart from "was never
    asked". Nothing is displayed: on a one-tenant node the user must
    not learn that tenants exist.

    Skipped without a flag while the tenant store is missing — identity
    can start before the host migration ran, and the next start does
    it. Never guesses an id.
    """
    state = _load(STATE_FILE, {})
    if state.get("tenant_migrated"):
        return
    tid = default_tenant_id()
    if not tid:
        return
    with users_rw() as users:
        changed = False
        for u in users:
            if not u.get("tenant"):
                u["tenant"] = tid
                changed = True
        if changed:
            save_users(users)
    state["tenant_migrated"] = True
    _save(STATE_FILE, state)


def _migrate_server_admin_once():
    """RFC-0008, one-time upgrade step: every existing `admin` holder
    also becomes `server_admin`, so nobody presently trusted with the
    server loses access when the two roles split apart. Runs once per
    installation (STATE_FILE flag), not on every load — after this,
    the two roles are granted independently.
    """
    state = _load(STATE_FILE, {})
    if state.get("server_admin_migrated"):
        return
    with users_rw() as users:
        changed = False
        for u in users:
            if "admin" in u["roles"] and "server_admin" not in u["roles"]:
                u["roles"] = sorted(set(u["roles"]) | {"server_admin"})
                changed = True
        if changed:
            save_users(users)
            print(f"RFC-0008 migration: granted server_admin to "
                  f"{sum(1 for u in users if 'server_admin' in u['roles'])} existing admin(s)",
                  flush=True)
    state["server_admin_migrated"] = True
    _save(STATE_FILE, state)


def _migrate_support_once():
    """RFC-0039, one-time upgrade step: every existing `partner` holder
    also becomes `support`, so nobody who looks after this node loses
    the health page when the node-wide half of `partner` moves out.

    The same shape as the RFC-0008 migration above, and for the same
    reason: today's `partner` holders ARE service providers -- that is
    the only meaning the role was usable for, because it was the only
    one the platform enforced. So this preserves exactly the access
    they have and grants nobody anything new.

    `partner` is deliberately NOT removed. Taking a role away is the
    riskier direction (an app route may require it), and the platform
    cannot tell a service provider from a genuine external company --
    guessing would either strip a real partner or leave a technician
    mislabelled. Sorting that out is an operator task, named in the
    release note.
    """
    state = _load(STATE_FILE, {})
    if state.get("support_migrated"):
        return
    with users_rw() as users:
        changed = False
        for u in users:
            if "partner" in u["roles"] and "support" not in u["roles"]:
                u["roles"] = sorted(set(u["roles"]) | {"support"})
                changed = True
        if changed:
            save_users(users)
            print(f"RFC-0039 migration: granted support to "
                  f"{sum(1 for u in users if 'support' in u['roles'])} existing "
                  f"partner(s) -- review who should keep 'partner'",
                  flush=True)
    state["support_migrated"] = True
    _save(STATE_FILE, state)


def _backfill_user_ids():
    """RFC-0040 §7: every existing user receives an `id` on the first
    start after the update, written once.

    DELIBERATELY WITHOUT A STATE FLAG, unlike the three migrations
    above. Those change what a record MEANS (a role granted, a tenant
    joined), so repeating them would undo an operator's cleanup -- the
    flag is what makes them safe. Filling in a missing identity changes
    no meaning and is idempotent: a record that has one is left alone.

    Without the flag this also heals the cases a flag would miss -- a
    users.json restored from a backup older than the update, a file
    edited by hand on the machine, a creation path nobody thought of.
    Together with save_users() it means a user record without an
    identity survives neither a write nor a restart.
    """
    with users_rw() as users:
        missing = [u for u in users if not u.get("id")]
        if not missing:
            return
        save_users(users)
        print(f"RFC-0040: assigned a stable id to {len(missing)} existing "
              f"user record(s)", flush=True)


_migrate_server_admin_once()
_migrate_support_once()
_migrate_tenant_once()
_backfill_user_ids()


def find_user(users, username):
    return next((u for u in users if u["username"] == username), None)


def session_username():
    user = session.get("user")
    if isinstance(user, dict):  # session format before user management
        return user.get("username")
    return user


def public_user(u):
    """User record for list/UI use — never the password hash (spec 5.7).

    Carries the tenant since oaap.core.tenant 0.2: the portal has to be
    able to show a tenant_admin their own tenant and nobody else's, and
    it cannot filter by something it is not told. This record goes to
    the portal over the key-protected internal API and to `oaap user
    list` on the machine — never to an app, and never into a header.

    Carries the RFC-0040 identity and address since 0.4.0. `id` so the
    portal can show an administrator the thing apps anchor on --
    otherwise the one field a support question is about would be
    visible nowhere. `email` WITH its verification flag, always
    together: an address shown without it invites the reader to believe
    it, which is the same mistake D2 keeps out of the headers.
    """
    return {"username": u["username"], "display_name": u["display_name"],
            "roles": u["roles"], "groups": u["groups"], "active": u["active"],
            "tenant": u.get("tenant", ""), "kind": u.get("kind", "human"),
            "id": u.get("id", ""), "email": u.get("email", ""),
            "email_verified": bool(u.get("email_verified"))}


def _header_value(value):
    """A header value an app can always percent-decode (RFC-0040 D4).

    HTTP header values are not a safe place for arbitrary Unicode, and
    the failure is not a clean error -- it is mojibake in one app and a
    dropped header in another, found in production. A display name is
    "Jörg Müller"; an e-mail local part can be worse.

    Plain whenever the value is printable ASCII WITHOUT a percent sign
    -- the common case, and it stays readable for whoever is looking at
    it. Otherwise UTF-8 percent-encoded.

    The percent sign is what makes the rule total. An ASCII name
    containing one ("100% sicher") would be indistinguishable from an
    escape sequence, so it is encoded as well -- which is why the
    instruction to apps can be the simple one: ALWAYS percent-decode.
    A rule with an exception is a rule half the apps will get wrong.
    """
    if value and all(32 <= ord(c) < 127 for c in value) and "%" not in value:
        return value
    return quote(value, safe="", encoding="utf-8")


def identity_headers(user):
    """The trusted headers the gateway copies onto the app's request.

    ALWAYS all five, empty where there is no value, never a shorter
    list. The gateway's anti-spoofing guarantee works by OVERWRITING
    what the client sent (deployment contract guarantee 1); a header
    this answer leaves out is a header whose client-sent value has
    nothing to overwrite it. So absent is expressed as empty, not as
    missing, and apps are told to read the two the same way.

    `X-OAAP-Email` carries a PROVEN address or nothing (RFC-0040 D2).
    An address in a platform header will be treated as proven whatever
    flag stands next to it, so an unverified one is not handed over at
    all. hbsha asked for the address plus a flag; this is the narrower
    answer, and it can be widened later without breaking anybody.
    """
    return {
        "X-OAAP-User": user["username"],
        "X-OAAP-Roles": ",".join(user["roles"]),
        "X-OAAP-User-Id": user.get("id", ""),
        "X-OAAP-Display-Name": _header_value(user.get("display_name") or ""),
        "X-OAAP-Email": (_header_value(user.get("email") or "")
                         if user.get("email_verified") else ""),
    }


# The originally requested address, carried through the login (RFC-0040
# §5). An invitation link is the ordinary case this fixes: today the
# path and query are lost and only the instance survives, so a person
# who follows an invitation lands on the app's start page and the token
# in the link is gone.
RETURN_MAX = 512

# Paths that are never a place to send somebody back to.
#
# /auth/login and /auth/logout are the obvious ones: the flow that just
# ran is not the destination.
#
# /verify and /throttle are the SAFETY NET for the day the gateway stops
# telling us the original address. _requested_uri() then falls back to
# the request identity actually sees -- which on a forward-auth call is
# the verify call itself. Without this line that fallback would produce
# a return target of "/verify?roles=user": a login that lands on a
# 204 with no page. Named here rather than guarded at the call site,
# because the fallback is exactly the path nobody will be watching.
NOT_A_RETURN = frozenset({"/auth/login", "/auth/logout", "/auth/terminal",
                          "/verify", "/throttle"})


def _return_target(raw):
    """A place inside this platform, or "" (RFC-0040 D5).

    A return target taken from a URL is the classic open-redirect hole:
    a link to our own login page that sends the visitor to somebody
    else's site AFTER they signed in, which is where a convincing
    phishing page belongs. So only a local path passes, and the rule is
    written here once rather than trusted to a code review.

    Refused: anything not starting with a single "/" (a scheme, a bare
    word), "//host" and "/\\host" -- the second because browsers have
    historically read a backslash as a slash, so it is protocol-relative
    to a browser and local-looking to a regex. Control characters,
    because a header cannot carry them and a splitter might. And the
    platform's own auth plumbing (NOT_A_RETURN below), because bouncing
    back into the flow that just ran is a loop, not a return.
    """
    if not raw or len(raw) > RETURN_MAX:
        return ""
    if not raw.startswith("/") or raw[:2] in ("//", "/\\"):
        return ""
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return ""
    if raw.split("?", 1)[0].rstrip("/") in NOT_A_RETURN:
        return ""
    return raw



def _requested_uri():
    """Where the caller was actually going.

    On a forward-auth call that is the gateway's X-Forwarded-Uri -- the
    only place the original address still exists, because the request
    identity sees was rewritten to /verify. Read whole, NOT through
    _forwarded(): that helper takes the first comma-separated element,
    and a URI may legitimately contain a comma.

    On a direct call (identity's own pages, reached through the
    reserved /auth/* handler) the request itself is the answer.
    """
    fwd = request.headers.get("X-Forwarded-Uri", "")
    if fwd:
        return fwd
    return request.full_path[:-1] if request.full_path.endswith("?") \
        else request.full_path


def login_redirect():
    """Send an unauthenticated visitor to the login form -- and remember
    where they were going (RFC-0040 §5).
    """
    target = _return_target(_requested_uri())
    if not target or target == "/":
        return redirect("/auth/login", code=303)
    return redirect("/auth/login?next=" + quote(target, safe=""), code=303)


def other_active_server_admin_exists(users, username):
    """RFC-0008: the platform must keep at least one active server_admin
    (losing the last one would lock everyone out of user/edge/store
    management — the same protection RFC-0002 gave `admin` originally,
    now attached to the role that actually carries platform authority).
    """
    return any(u["active"] and "server_admin" in u["roles"] and u["username"] != username
               for u in users)


# Login throttling (first hardening step for exposed setups): after 5
# failures per client+username within 5 minutes, one attempt per
# minute. State lives in /data so it is shared across gunicorn workers
# (in-process memory would give every worker its own counter).
THROTTLE_FILE = os.path.join(DATA_DIR, "login-throttle.json")
_LOCK_THRESHOLD, _LOCK_WINDOW, _LOCK_SECONDS = 5, 300, 60


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


def _throttle_state(key):
    """Pruned failure timestamps for key plus the full (pruned) table."""
    now = time.time()
    table = _load(THROTTLE_FILE, {})
    table = {k: hits for k, hits in
             ((k, [t for t in v if now - t < _LOCK_WINDOW]) for k, v in table.items())
             if hits}
    return table.get(key, []), table


def _login_blocked(key):
    hits, _ = _throttle_state(key)
    return len(hits) >= _LOCK_THRESHOLD and time.time() - hits[-1] < _LOCK_SECONDS


def _login_failed(key):
    hits, table = _throttle_state(key)
    table[key] = hits + [time.time()]
    _save(THROTTLE_FILE, table)


def _login_succeeded(key):
    hits, table = _throttle_state(key)
    if key in table:
        del table[key]
        _save(THROTTLE_FILE, table)


# ----------------------------------------------------------- API keys

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _in_days_iso(days):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + days * 86400))


def load_keys():
    keys = _load(KEYS_FILE, [])
    for k in keys:
        k.setdefault("instance", "")
        k.setdefault("label", "")
        k.setdefault("revoked", False)
        k.setdefault("last_used", "")
        k.setdefault("terminal", False)
    return keys


def public_key(k):
    """A key record without its hash -- for the portal and the CLI.

    Same rule as public_user: the secret never leaves this module, and
    it left exactly once, at creation.
    """
    return {"id": k["id"], "principal": k["principal"],
            "tenant": k.get("tenant", ""), "roles": k["roles"],
            "instance": k.get("instance", ""), "label": k.get("label", ""),
            "created": k.get("created", ""), "expires": k.get("expires", ""),
            "last_used": k.get("last_used", ""),
            "created_by": k.get("created_by", ""),
            "terminal": bool(k.get("terminal")),
            "revoked": bool(k.get("revoked"))}


def issue_key(users, principal, roles, instance, label, days, created_by,
              terminal=False):
    """Mint one key. Returns (record, secret) -- the secret is the only
    time the caller ever sees it.

    Two ceilings, both checked here and the second one again at use
    (RFC-0027 3.3): a key may not carry more than its PRINCIPAL holds,
    and the caller may not grant more than they hold themselves.
    """
    u = find_user(users, principal)
    if not u:
        raise ValueError(f"Diesen Prinzipal gibt es nicht: '{principal}'.")
    if not u["active"]:
        raise ValueError(f"'{principal}' ist deaktiviert.")
    wanted = {r for r in (roles or []) if r in ASSIGNABLE_ROLES}
    if not wanted:
        raise ValueError("Mindestens eine gueltige Rolle ist erforderlich.")
    if wanted & KEY_FORBIDDEN_ROLES:
        raise ValueError("Ein Schluessel darf server_admin nicht tragen "
                         "(RFC-0027 D2).")
    if not wanted <= set(u["roles"]):
        raise ValueError(f"'{principal}' haelt diese Rollen selbst nicht: "
                         + ",".join(sorted(wanted - set(u["roles"]))))
    # `days or DEFAULT` would read 0 as "unset" and quietly hand out 90
    # days to a caller who meant "never". Absent and zero are different
    # answers, and only one of them is a request we refuse out loud.
    if days is None or days == "":
        days = KEY_DEFAULT_DAYS
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("Gueltigkeit in Tagen muss eine Zahl sein.")
    if not 1 <= days <= KEY_MAX_DAYS:
        raise ValueError(f"Gueltigkeit: 1 bis {KEY_MAX_DAYS} Tage "
                         "(RFC-0027 D3 -- 'nie' gibt es nicht).")
    keys = load_keys()
    while True:
        kid = secrets.token_hex(4)
        if not any(k["id"] == kid for k in keys):
            break
    secret = secrets.token_urlsafe(32)
    rec = {"id": kid, "principal": principal, "tenant": u.get("tenant", ""),
           "roles": sorted(wanted), "instance": (instance or "").strip(),
           "label": (label or "").strip(),
           "hash": generate_password_hash(secret),
           "created": _now_iso(), "expires": _in_days_iso(days),
           "last_used": "", "created_by": created_by, "revoked": False,
           # Only a label for the list -- a terminal key is an ordinary
           # key in every way that matters, which is why enrolling one
           # needed no new store (RFC-0028 4.1: the device IS the key).
           "terminal": bool(terminal)}
    keys.append(rec)
    _save(KEYS_FILE, keys)
    return rec, f"oaapk_{kid}_{secret}"


def revoke_key(kid):
    """Immediate, by the same reasoning that produced session_epoch: a
    credential you cannot withdraw within seconds is one you do not
    really control. The record is kept -- a revoked key that vanished
    would take its own history with it."""
    keys = load_keys()
    rec = next((k for k in keys if k["id"] == kid), None)
    if rec is None:
        return None
    rec["revoked"] = True
    rec["revoked_at"] = _now_iso()
    _save(KEYS_FILE, keys)
    return rec


def _touch_key(kid):
    """Record that a key was used -- at most once an hour.

    last_used answers "is this key still needed" without archaeology,
    which is what makes rotation possible at all. Writing it on every
    request would put a file write in front of every single call, so
    the resolution is deliberately coarse.
    """
    now = _now_iso()
    keys = load_keys()
    rec = next((k for k in keys if k["id"] == kid), None)
    if rec is None or rec["last_used"][:13] == now[:13]:
        return
    rec["last_used"] = now
    _save(KEYS_FILE, keys)


def _key_refusal(code, detail, status=401):
    """A machine gets an answer, never a redirect to a login form.

    That is the one place where the two methods must NOT look alike: a
    script following a 303 to /auth/login would receive an HTML page
    with status 200 and call it success.
    """
    return (detail, status,
            {"WWW-Authenticate": 'Bearer error="' + code
                                 + '", error_description="' + detail + '"'})


def _by_key(token, instance):
    """Method `key` (RFC-0027 3.2): which principal is this bearer?"""
    m = KEY_TOKEN_RE.fullmatch(token)
    if not m:
        return None, "key", _key_refusal("invalid_token",
                                         "malformed key")
    kid, secret = m.group(1), m.group(2)
    # Two brakes (RFC-0010 reused): one on the key id, one on the
    # client. The second is the one that matters -- an attacker
    # enumerating ids never repeats one, so a per-id brake alone would
    # never fire.
    brakes = ["key|" + kid, "keyclient|" + _client_ip()]
    if any(_login_blocked(b) for b in brakes):
        return None, "key", _key_refusal("invalid_token",
                                         "too many failed attempts", 429)
    rec = next((k for k in load_keys() if k["id"] == kid), None)
    if (rec is None or rec["revoked"]
            or not check_password_hash(rec["hash"], secret)):
        for b in brakes:
            _login_failed(b)
        return None, "key", _key_refusal("invalid_token",
                                         "unknown or revoked key")
    if rec.get("expires") and rec["expires"] <= _now_iso():
        return None, "key", _key_refusal(
            "invalid_token", "the key expired on " + rec["expires"][:10])
    # Instance scoping (RFC-0027 D5). Fail CLOSED where the gateway did
    # not say which instance this is: a site generated before this
    # version passes no `instance`, and a scoped key must refuse there
    # rather than silently reach everything.
    if rec.get("instance") and rec["instance"] != instance:
        return None, "key", _key_refusal(
            "insufficient_scope",
            "this key is limited to instance '" + rec["instance"] + "'", 403)
    user = find_user(load_users(), rec["principal"])
    if not user or not user["active"]:
        return None, "key", _key_refusal(
            "invalid_token", "the principal is gone or deactivated")
    for b in brakes:
        _login_succeeded(b)
    _touch_key(kid)
    # Roles come from the LIVE user store on every request, exactly as
    # for a session (spec 2.3). A key can only ever narrow: taking a
    # role off the principal takes it off every key it ever issued.
    effective = sorted((set(rec["roles"]) & set(user["roles"]))
                       - KEY_FORBIDDEN_ROLES)
    return dict(user, roles=effective), "key", None


def _terminal_key_ok(kid):
    """Is the key behind a terminal session still good?

    Checked on EVERY request, not once at enrolment. Without this the
    cookie would outlive the credential it came from, and revoking a
    key would leave the screen it was issued for running until somebody
    rebooted it -- which is the same as not revoking it.
    """
    rec = next((k for k in load_keys() if k["id"] == kid), None)
    return bool(rec and not rec["revoked"]
                and (not rec.get("expires") or rec["expires"] > _now_iso()))


def _by_session():
    """Method `session` (RFC-0027 3.2): the browser cookie."""
    username = session_username()
    user = find_user(load_users(), username) if username else None
    if (not user or not user["active"]
            or session.get("epoch") != user.get("session_epoch", 0)):
        session.clear()
        return None, "session", login_redirect()
    kid = session.get("terminal_key")
    if kid and not _terminal_key_ok(kid):
        session.clear()
        return None, "session", login_redirect()
    return user, "session", None


def resolve_principal(instance=""):
    """WHICH PRINCIPAL IS THIS? -- and nothing else (RFC-0027 3.2).

    An ordered list of methods, not a branch. Everything after this
    answer -- roles, visibility groups, the tenant boundary, the two
    headers -- is written once and shared. That is what lets a
    customer's own identity provider become a third method later
    instead of a rewrite.

    Returns (user, method, refusal). Exactly one of user and refusal is
    set.
    """
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return _by_key(auth[7:].strip(), instance)
    return _by_session()


# Look & feel per oaap-design/docs/design-guidelines.md v0.1 (blue,
# hexagon mark, German UI, no external resources). Kept in sync with
# the portal's stylesheet by hand — the guidelines file is the source
# of truth.
_CARD_STYLE = """
<style>
  :root{--blue-600:#2563eb;--blue-700:#1d4ed8;--bg:#f4f6fa;--text:#1f2937;
        --muted:#6b7280;--border:#e5e7eb;--err:#b91c1c;--ok:#15803d}
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       display:grid;place-items:center;min-height:100vh;margin:0;
       background:var(--bg);color:var(--text)}
  .card{background:#fff;padding:2rem;border-radius:.6rem;border:1px solid var(--border);
       box-shadow:0 1px 3px rgba(23,37,84,.06);width:min(22rem,92vw)}
  .mark{text-align:center;margin-bottom:.4rem}
  h1{font-size:1.2rem;margin:.2rem 0 1rem;text-align:center}
  .wordmark{text-align:center;letter-spacing:.08em;font-weight:700;color:var(--blue-600)}
  input{width:100%;padding:.55rem;margin:.25rem 0 1rem;border:1px solid var(--border);
       border-radius:.4rem;font-size:.95rem}
  button{width:100%;padding:.65rem;border:0;border-radius:.4rem;background:var(--blue-600);
       color:#fff;font-size:1rem;cursor:pointer;min-height:44px}
  button:hover{background:var(--blue-700)}
  .err{color:var(--err)}.ok{color:var(--ok)}.hint{color:var(--muted);font-size:.9rem}
  a{color:var(--blue-600)}
</style>
"""

_MARK_SVG = """
<p class="mark"><svg viewBox="0 0 100 100" width="46" height="46" aria-hidden="true">
  <polygon points="50,4 90,27 90,73 50,96 10,73 10,27" fill="none"
           stroke="#2563eb" stroke-width="6" stroke-linejoin="round"/>
  <polygon points="50,28 69,39 69,61 50,72 31,61 31,39" fill="#2563eb"/></svg></p>
"""

_FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "viewBox='0 0 100 100'%3E%3Cpolygon points='50,4 90,27 90,73 50,96 "
            "10,73 10,27' fill='%232563eb'/%3E%3C/svg%3E")

_HEAD = ('<!doctype html><html lang="de"><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width, initial-scale=1">'
         f'<link rel="icon" href="{_FAVICON}">')

LOGIN_PAGE = _HEAD + "<title>Anmelden — OAAP</title>" + _CARD_STYLE + _MARK_SVG.join([
    "<body><div class='card'>",
    """<div class="wordmark">OAAP</div>
<h1>Anmelden</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if not has_users %}
  <p class="hint">Es gibt noch keine Benutzer — bitte zuerst die
  Einrichtung abschließen (URL und Token stehen in der
  Installationsausgabe).</p>
{% endif %}
<form method="post" action="/auth/login">
{% if next %}<input type="hidden" name="next" value="{{ next }}">{% endif %}
  <label>Benutzername <input name="username" autofocus autocomplete="username"></label>
  <label>Passwort <input name="password" type="password" autocomplete="current-password"></label>
  <button>Anmelden</button>
</form>
</div></body></html>"""])

PASSWORD_PAGE = _HEAD + "<title>Passwort ändern — OAAP</title>" + _CARD_STYLE + _MARK_SVG.join([
    "<body><div class='card'>",
    """<h1>Passwort ändern</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if done %}
  <p class="ok">Das Passwort wurde geändert.</p>
  <p><a href="/">Zurück zum Portal</a></p>
{% else %}
<form method="post" action="/auth/password">
  <label>Aktuelles Passwort <input name="current" type="password" required autocomplete="current-password"></label>
  <label>Neues Passwort (mind. 8 Zeichen)
    <input name="new" type="password" minlength="8" required autocomplete="new-password"></label>
  <button>Passwort ändern</button>
</form>
<p><a href="/">Zurück zum Portal</a></p>
{% endif %}
</div></body></html>"""])


PROFILE_PAGE = _HEAD + "<title>Profil — OAAP</title>" + _CARD_STYLE + _MARK_SVG.join([
    "<body><div class='card'>",
    """<h1>Profil</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if done %}
  <p class="ok">Der Anzeigename wurde geändert.</p>
{% endif %}
<form method="post" action="/auth/profile">
  <label>Anzeigename (max. 80 Zeichen)
    <input name="display_name" maxlength="80" value="{{ display_name }}"></label>
  <button>Speichern</button>
</form>
<p><a href="/">Zurück zum Portal</a></p>
</div></body></html>"""])


@app.get("/auth/profile")
def profile_form():
    if not session_username():
        return login_redirect()
    users = load_users()
    u = find_user(users, session_username() or "")
    if not u or not u["active"]:
        return login_redirect()
    return render_template_string(
        PROFILE_PAGE, error=None, done=False,
        display_name=u.get("display_name") or "")


@app.post("/auth/profile")
def profile_change():
    """Self-service display-name change (RFC-0036 Teil B).

    Deliberately the only field this route touches. Roles, groups,
    tenant, active status and the username itself stay admin-only
    (spec 2.2/2.3) -- this is the one thing about themselves that was
    previously stuck behind "ask an admin" for no security reason: a
    misspelled or outdated display name carries no privilege.
    """
    name = (request.form.get("display_name") or "").strip()
    with users_rw() as users:
        u = find_user(users, session_username() or "")
        if not u or not u["active"]:
            return login_redirect()
        if len(name) > DISPLAY_NAME_MAX:
            return render_template_string(
                PROFILE_PAGE,
                error=f"Der Anzeigename darf höchstens {DISPLAY_NAME_MAX} "
                      "Zeichen haben.",
                done=False, display_name=name[:DISPLAY_NAME_MAX]), 400
        u["display_name"] = name
        save_users(users)
    print(f"profile changed: {u['username']} (display_name)", flush=True)
    return render_template_string(PROFILE_PAGE, error=None, done=True,
                                  display_name=name)


# ---------------------------------------------------------------------------
# CORS ON A REFUSAL (RFC-0038 §"Open for later" follow-up, Jörgs Befund
# 2026-09-15/17)
#
# WHY THIS EXISTS. A page on another origin calls an app on this node.
# The gateway refuses it -- no session (303 -> /auth/login), no or a
# wrong API key (401/403), too many requests (429) -- and the refusal
# carries no CORS header, because nothing ever put one there. So the
# browser does not report "not authenticated". It reports a CORS error,
# and the status it actually received is invisible both to the script
# and to the person debugging it. That is the wrong question, and it
# cost Jörg the 15.09.: the true answer ("this one call carries no
# key") sat in the gateway's access log the whole time.
#
# ONE PLACE, because there is only one. Every refusal the gateway can
# produce on an app route comes from this service: /verify answers
# session, key, role, group and tenant, /throttle answers the brake
# (appctl.site_body and _throttle_block are the only two forward_auth
# calls a generated site makes). A rule written here therefore cannot
# be forgotten at the ninth call site -- the failure mode this codebase
# keeps finding.
#
# WHAT IS DELIBERATELY NOT DONE: no `Access-Control-Allow-Credentials`.
# A cookie-bearing cross-origin call still cannot read this answer, so
# no foreign page can use a refusal to probe whether its visitor has a
# session on this node. The case this serves is the one that is MEANT
# to work -- an API key in `Authorization` (RFC-0027), which is not a
# credential in the CORS sense -- and there it replaces a lie with the
# truth. A caller that insists on cookies across origins keeps the CORS
# error, and that is the honest answer: cookies are not the way in from
# another origin.
CORS_REFUSAL_PATHS = ("/verify", "/throttle")

# Only what a browser needs to READ a refusal, and nothing that would
# let it act on one. `Vary` is added, never assigned: the answer depends
# on the caller's Origin, so a cache must not hand one caller's copy to
# the next.
CORS_REFUSAL_HINT = ("not authenticated: a call from another origin cannot "
                     "use the browser login of this platform. Present an "
                     "OAAP API key as 'Authorization: Bearer <key>' "
                     "(RFC-0027), issued in the portal under „Zugänge“.")


def _forwarded(header, default=""):
    """The first value of a hop-by-hop list header, or `default`."""
    return request.headers.get(header, "").split(",")[0].strip() or default


def site_origin():
    """The origin of the site the gateway is protecting, as the browser
    sees it -- never this container's own host.

    forward_auth calls arrive at identity:8000, so `request.host` is
    useless here; the gateway's X-Forwarded-* headers carry the name the
    caller typed. Absent (a direct call to /verify on the container
    network) yields "", and then every Origin counts as foreign -- the
    harmless direction: the only thing that follows is a header on a
    refusal.
    """
    host = _forwarded("X-Forwarded-Host")
    return f'{_forwarded("X-Forwarded-Proto", "https")}://{host}' if host else ""


def foreign_origin():
    """The Origin of a cross-origin browser call, or "".

    "" for a same-origin call (whose refusal needs no CORS header, and
    whose redirect to the login form is exactly right) and for the
    literal `null` an opaque document sends, which no reflected origin
    can help.
    """
    origin = request.headers.get("Origin", "").strip()
    if not origin or origin.lower() == "null":
        return ""
    return "" if origin.lower() == site_origin().lower() else origin


def is_navigation():
    """Is the browser NAVIGATING here, rather than a script calling?

    Fetch metadata, which every current browser sends and no script can
    forge (the Sec- prefix makes it browser-owned). The distinction
    decides whether a refusal may stay a redirect: for a navigation --
    a cross-origin form post, a link -- the login form IS the answer,
    and turning it into a 401 would break a sign-in that works today.
    For a fetch() it is useless: the browser follows the 303, the login
    page answers 200 without CORS headers, and the script reports a CORS
    error on a URL it never called.

    A browser too old to send these headers and posting a form across
    origins is judged a script call and gets a 401. Accepted knowingly:
    it is a narrow case, the message says what to do, and the other
    direction would keep the misleading answer for everybody.
    """
    return (request.headers.get("Sec-Fetch-Mode", "").lower() == "navigate"
            or request.headers.get("Sec-Fetch-Dest", "").lower() == "document")


@app.after_request
def cors_on_refusal(resp):
    """Let a cross-origin caller READ why the gateway refused it."""
    if request.path not in CORS_REFUSAL_PATHS or resp.status_code < 300:
        return resp
    origin = foreign_origin()
    if not origin:
        return resp
    if resp.status_code in (301, 302, 303, 307, 308) and not is_navigation():
        # A machine gets an answer, never a redirect to a login form --
        # the same rule _key_refusal() already holds for a bad key, now
        # applied where the caller presented NO credential at all.
        resp = make_response(CORS_REFUSAL_HINT, 401)
        resp.headers["WWW-Authenticate"] = (
            'Bearer error="invalid_token", error_description="no session and '
            'no API key"')
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.vary.add("Origin")
    resp.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/verify")
def verify():
    """Forward-auth endpoint for the gateway (RFC-0002 default deny).

    Optional ?roles=a,b restricts the route to users holding at least
    one of the given roles (route-level authorization from the app
    manifest, spec oaap.apps.runtime 2.4). Optional ?groups=a,b is an
    ADDITIONAL restriction from the instance's visibility setting
    (RFC-0007): the caller needs at least one of the listed groups,
    unless they hold server_admin (RFC-0008's platform-wide bypass —
    the true administrator sees every instance regardless of
    visibility). Roles and groups always come from the current user
    store, never from the session (spec 2.3).

    Since RFC-0027 the caller may present a session cookie OR an API
    key; resolve_principal() answers which principal it is, and
    EVERYTHING BELOW IS UNCHANGED for both. Optional ?instance=<key>
    lets a key be limited to one app.
    """
    user, _method, refusal = resolve_principal(
        request.args.get("instance", ""))
    if refusal is not None:
        return refusal
    required = request.args.get("roles", "")
    if required and not set(required.split(",")) & set(user["roles"]):
        return "Forbidden: missing role", 403
    required_groups = request.args.get("groups", "")
    if (required_groups and "server_admin" not in user["roles"]
            and not set(required_groups.split(",")) & set(user["groups"])):
        return "Forbidden: not in a visibility group for this app", 403
    # The tenant boundary (oaap.core.tenant 3.1), enforced here and
    # nowhere else: the instance's tenant arrives as a parameter from
    # the generated gateway config, and a session from another tenant is
    # refused BEFORE the app is reached. An app that filtered by tenant
    # itself would be one bug away from a leak between customers.
    #
    # server_admin passes (RFC-0022 D5: the operator may reach
    # everything, and the audit log is the counterweight, not a barrier
    # that would be a lie). Comparison is on the RESOLVED value on both
    # sides, so an instance naming a tenant this node does not have
    # matches nobody -- fail closed, exactly as spec 2.5 requires.
    required_tenant = request.args.get("tenant", "")
    if required_tenant and "server_admin" not in user["roles"]:
        want = resolve_tenant(required_tenant)
        mine = resolve_tenant(user.get("tenant"))
        if want is None or mine is None or want != mine:
            return "Forbidden: this app belongs to another tenant", 403
    return "", 204, identity_headers(user)


@app.get("/auth/login")
def login_form():
    return render_template_string(
        LOGIN_PAGE, error=None, has_users=bool(load_users()),
        next=_return_target(request.args.get("next", "")))


@app.post("/auth/login")
def login():
    users = load_users()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    # Validated again on the way out, not only on the way in: the form
    # travels through the visitor's browser, so what comes back is an
    # input like any other (RFC-0040 D5).
    target = _return_target(request.form.get("next", ""))
    throttle_key = f"{_client_ip()}|{username}"
    if _login_blocked(throttle_key):
        return render_template_string(
            LOGIN_PAGE, error="Zu viele Fehlversuche — bitte eine Minute warten.",
            has_users=bool(users), next=target), 429
    u = find_user(users, username)
    # Generic error either way — no username enumeration (spec 4.4).
    # A machine principal has no password (RFC-0027 3.1) and must not
    # be able to acquire a session by any route -- said explicitly here
    # rather than relying on check_password_hash refusing an empty
    # stored hash, which is a library behaviour, not a decision of ours.
    if (u and u["active"] and u.get("kind", "human") != "machine"
            and check_password_hash(u["password_hash"], password)):
        _login_succeeded(throttle_key)
        session["user"] = u["username"]
        session["epoch"] = u.get("session_epoch", 0)
        print(f"login ok: {u['username']} from {_client_ip()}", flush=True)
        # Back to where they were going, or the start page (RFC-0040
        # §5). The instance always survived, because the browser stays
        # on the same hostname; the path and query did not, which is
        # what turned every invitation link into a landing on "/".
        return redirect(target or "/", code=303)
    _login_failed(throttle_key)
    print(f"login failed: '{username}' from {_client_ip()}", flush=True)
    return render_template_string(
        LOGIN_PAGE, error="Benutzername oder Passwort ist falsch.",
        has_users=bool(users), next=target
    ), 401


def _revoke_sessions(username):
    """Bump a user's session epoch, invalidating every copy of their
    cookie immediately (see load_users()/verify()) — used by logout and
    password change, since a signed cookie cannot otherwise be revoked.
    """
    with users_rw() as users:
        u = find_user(users, username)
        if u:
            u["session_epoch"] = u.get("session_epoch", 0) + 1
            save_users(users)


TERMINAL_DONE = """
<!doctype html><meta charset="utf-8"><title>Terminal eingerichtet</title>
<body style="font-family:system-ui;max-width:40rem;margin:4rem auto;padding:1rem">
<h1>Dieses Gerät ist jetzt ein Terminal</h1>
<p>Es meldet sich ab sofort selbst an, auch nach einem Neustart, als
   <strong>{{ who }}</strong>.</p>
<p>Es meldet sich <em>nicht</em> mehr ab. Beendet wird das im Portal unter
   Zugänge, indem der Schlüssel dieses Geräts entzogen wird — das wirkt
   sofort, auch wenn niemand am Gerät ist.</p>
<p><a href="/">Weiter</a></p>
</body>"""


@app.post("/auth/terminal")
def terminal_enrol():
    """Exchange a key for a long-lived session (RFC-0028 4.2).

    Why this exists at all: a browser puts no Authorization header on an
    ordinary navigation, so a kiosk cannot present a key the way a
    script does. It gets a cookie instead -- and the cookie keeps naming
    the key, so nothing about revocation changes.

    The key arrives as a form field because the page that carries it was
    rendered exactly once, by the portal, for the administrator standing
    at the machine. It must never travel in a URL.
    """
    token = (request.form.get("key") or "").strip()
    user, _method, refusal = _by_key(token, "")
    if refusal is not None:
        return refusal
    if user.get("kind") != "machine":
        return ("Nur ein Maschinen-Prinzipal kann ein Terminal sein "
                "(RFC-0028 4.1)."), 403
    session.clear()
    session.permanent = True
    session["user"] = user["username"]
    session["epoch"] = user.get("session_epoch", 0)
    session["terminal_key"] = KEY_TOKEN_RE.fullmatch(token).group(1)
    audit("terminal.enrol", user.get("tenant", ""), user["username"],
          who=user["username"], role="-",
          detail="key " + session["terminal_key"])
    print(f"terminal enrolled: {user['username']} from {_client_ip()}",
          flush=True)
    return render_template_string(TERMINAL_DONE, who=user["username"])


@app.post("/auth/logout")
def logout():
    # A terminal has no one to log out. Offering it the button anyway
    # would mean one stray tap in a warehouse leaves a dead screen until
    # somebody with an administrator password walks over -- and the
    # person who tapped it has no way to know that is what happened.
    if session.get("terminal_key"):
        return ("Ein Terminal meldet sich nicht ab. Beendet wird das im "
                "Portal unter Zugänge, indem der Schlüssel dieses Geräts "
                "entzogen wird."), 403
    username = session_username()
    if username:
        _revoke_sessions(username)
        print(f"logout: {username}", flush=True)
    session.clear()
    # No return target here, deliberately (RFC-0040 §5 is about a
    # refused request, not a deliberate sign-out): somebody who signs
    # out asked to LEAVE the page they were on, and sending them back
    # to it after the next login would undo that.
    return redirect("/auth/login", code=303)


@app.get("/auth/password")
def password_form():
    if not session_username():
        return login_redirect()
    return render_template_string(PASSWORD_PAGE, error=None, done=False)


@app.post("/auth/password")
def password_change():
    """Self-service password change (spec 2.4)."""
    with users_rw() as users:
        u = find_user(users, session_username() or "")
        if not u or not u["active"]:
            return login_redirect()
        if not check_password_hash(u["password_hash"], request.form.get("current", "")):
            return render_template_string(
                PASSWORD_PAGE, error="Das aktuelle Passwort stimmt nicht.", done=False), 403
        new = request.form.get("new", "")
        if len(new) < 8:
            return render_template_string(
                PASSWORD_PAGE, error="Das neue Passwort braucht mindestens 8 Zeichen.", done=False), 400
        u["password_hash"] = generate_password_hash(new)
        # Standard practice: a password change signs out every OTHER copy of
        # this user's cookie. Keep this browser signed in by advancing its
        # own session to match (else the request right after this one would
        # find itself logged out too).
        u["session_epoch"] = u.get("session_epoch", 0) + 1
        session["epoch"] = u["session_epoch"]
        save_users(users)
    print(f"password changed: {u['username']} (other sessions revoked)", flush=True)
    return render_template_string(PASSWORD_PAGE, error=None, done=True)


@app.get("/auth/whoami")
def whoami():
    """What an app may know about the person in front of it (spec 2.7).

    The same question /verify answers, in a form a page can read. Three
    rules make it safe to expose on every instance entry point:

    1. It answers about THE CALLER and nobody else. There is no
       parameter, so there is no request in which a caller can ask
       about someone else.
    2. `roles` is the SAME list /verify puts in X-OAAP-Roles -- read
       from the same resolve_principal() result, not computed again.
       whoami is a second reading of one truth, never a second truth.
    3. No tenant and no groups. The tenant boundary is enforced at the
       gateway before the app is reached (oaap.core.tenant 3.1);
       handing the app its caller's tenant would invite it to filter by
       that itself -- a weaker second enforcement, one bug away from a
       leak between customers -- and on a single-tenant node it would
       make tenants visible where nothing may be. Groups are absent for
       the same reason no X-OAAP-Groups header exists (spec 2.6).

    An unauthenticated caller gets 401, never the 303 to the login form
    that a browser route would give: whoever calls this is a script
    inside a page, and a script following a redirect would receive an
    HTML login page with status 200 and call it success.
    """
    user, method, refusal = resolve_principal(request.args.get("instance", ""))
    if refusal is not None:
        # A key refusal is already a proper machine answer; a failed
        # session is a redirect, which has to be turned into one.
        return refusal if method == "key" else (
            {"error": "not signed in"}, 401,
            {"WWW-Authenticate": 'Bearer error="invalid_token", '
                                 'error_description="not signed in"'})
    # A machine principal has no password, so it is not offered the
    # page for changing one -- an app rendering this menu would show a
    # link that leads to a form the caller can never fill in.
    links = {"logout": "/auth/logout"}
    if user.get("kind", "human") != "machine":
        links["password"] = "/auth/password"
    # RFC-0040: the same three values the headers carry, for the same
    # reason `roles` is here -- a page that can read one truth should
    # not have to guess the other. NOT percent-encoded: this is JSON,
    # which carries Unicode natively, and D4's encoding exists only
    # because HTTP headers do not. `email` follows D2 exactly: a proven
    # address or an empty string, never an unproven one with a flag.
    return ({"username": user["username"],
             "id": user.get("id", ""),
             "display_name": user.get("display_name") or "",
             "email": ((user.get("email") or "")
                       if user.get("email_verified") else ""),
             "roles": list(user["roles"]),
             "kind": user.get("kind", "human"),
             "method": method,
             "links": links},
            200,
            # Describes THIS session. A shared cache holding it would
            # hand one person's name to the next.
            {"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Request throttling for public routes (RFC-0010).
#
# Public routes carry no authentication at all — the platform hands the
# request straight to the app. This is the one gateway-side brake it can
# still apply: requests per client address per instance. It is a volume
# brake, not an authentication substitute (see the RFC).
#
# Counters live in process memory on purpose: this runs in the hot path
# of every public request, and the login throttle's file-per-request
# approach would be far too expensive. Consequence, documented rather
# than hidden: each gunicorn worker counts on its own, so the effective
# ceiling is the configured limit times the worker count, and a restart
# forgets the counters. Both are acceptable for a coarse abuse brake.

_RATE = {}
_RATE_MAX_KEYS = 20000


def _throttle_client():
    """The client address the gateway vouches for.

    Never derived from X-Forwarded-For here: on a directly exposed
    route a client can send that header itself. The gateway sets
    X-OAAP-Client per site, because only it knows whether the peer is
    the real client (direct) or the edge (behind-edge mode), and the
    edge overwrites X-Forwarded-For with the true peer.
    """
    return (request.headers.get("X-OAAP-Client", "").split(",")[0].strip()
            or request.remote_addr or "?")


# How often the brake actually engaged (RFC-0010 decision 2). Without
# this a 429 leaves nothing but a line in an access log nobody reads,
# and abuse stays invisible until somebody goes looking.
#
# Two properties this has to have, and neither is free:
#   - it must be COMPLETE across gunicorn workers, or the number is a
#     lie in the same way the effective limit is (each worker counts on
#     its own). So the file is the shared state, and any worker can
#     answer for all of them.
#   - it must not become an amplifier: one line per braked request is
#     exactly what an attacker would like to trigger. So counts go into
#     HOURLY BUCKETS, pruned to 24 hours — bounded by instances times
#     24, no matter how hard anyone knocks — and are flushed at most
#     every few seconds.

BRAKED_FILE = os.path.join(DATA_DIR, "throttle-braked.json")
BRAKED_HOURS = 24


def _braked_note(scope):
    """Record one braked request, straight through to the shared file.

    Batching in process memory was tried first and produces a wrong
    number, not merely a late one: a reader can force a flush only in
    the gunicorn worker that happens to answer it, so whatever the
    OTHER workers still hold is missing — and after a short burst
    nothing follows to settle it. Measured on a real node: 14 braked
    requests showed up as 2.

    Writing per braked request is affordable because the file does not
    grow with traffic — one integer per instance per hour, pruned to 24
    hours. The extra cost is a small locked read-modify-write on a page
    already in cache, next to the full HTTP request the brake performs
    anyway. So a flood cannot inflate this into a disk problem; it can
    only make the number it produces larger.
    """
    import fcntl
    hour = int(time.time() // 3600)
    oldest = hour - BRAKED_HOURS + 1
    try:
        fd = os.open(BRAKED_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = json.load(f)
            except ValueError:
                data = {}
            buckets = {k: v for k, v in (data.get(scope) or {}).items()
                       if str(k).isdigit() and int(k) >= oldest}
            buckets[str(hour)] = buckets.get(str(hour), 0) + 1
            data[scope] = buckets
            # drop instances whose last brake fell out of the window
            data = {s: b for s, b in data.items()
                    if any(str(k).isdigit() and int(k) >= oldest for k in b)}
            f.seek(0)
            json.dump(data, f)
            f.truncate()
    except OSError:
        return


@app.get("/throttle")
def throttle():
    scope = request.args.get("scope", "")
    try:
        limit = max(1, int(request.args.get("limit", "300")))
        window = max(1, int(request.args.get("window", "60")))
    except ValueError:
        limit, window = 300, 60
    key = f"{scope}|{_throttle_client()}"
    now = time.time()
    hits = [t for t in _RATE.get(key, ()) if now - t < window]
    if len(hits) >= limit:
        _RATE[key] = hits
        _braked_note(scope)
        retry = max(1, int(window - (now - hits[0])))
        return ("Too many requests", 429,
                {"Retry-After": str(retry), "Cache-Control": "no-store"})
    hits.append(now)
    if key not in _RATE and len(_RATE) >= _RATE_MAX_KEYS:
        # keep memory bounded under a spray of distinct addresses; the
        # oldest bucket is the least interesting one to lose
        _RATE.pop(min(_RATE, key=lambda k: _RATE[k][-1]), None)
    _RATE[key] = hits
    return "", 204


# ---------------------------------------------------------------------------
# Internal API — the portal is responsible for admin authorization of its
# callers; this layer only establishes that the caller IS the portal.
#
# WHY THIS GUARD EXISTS (RFC-0015 addendum A4, found 2026-08-11). The
# only protection used to be "reachable on the container network" — and
# every app instance ran on that same flat network. So any code inside
# any app container could POST /internal/users with
# `roles: ["server_admin"]` and hand itself the platform. The gateway
# enforces RFC-0002 perfectly on the way IN; there was nothing sideways.
# RFC-0016 (0.1.30) closed that structurally by giving each app its own
# network, so an app can no longer reach identity at all. This key stays
# as defence in depth: if a future mistake reconnects something, the
# internal API still refuses a caller that is not the portal.
#
# Checked in one place, by path prefix, rather than per route: the
# failure we are fixing is precisely the kind where someone adds an
# endpoint and forgets the decorator. A new /internal/* route is covered
# the moment it exists.
#
# Deliberately FAIL CLOSED when the key is missing. A node whose key was
# never generated then loses portal user administration and says why —
# loudly, in a way somebody fixes. Failing open would restore exactly the
# hole this closes, invisibly and forever. Login, /verify and app traffic
# do not pass through here and keep working either way.
INTERNAL_KEY = os.environ.get("INTERNAL_API_KEY", "")
INTERNAL_HEADER = "X-OAAP-Internal-Key"


@app.before_request
def _guard_internal_api():
    if not request.path.startswith("/internal/"):
        return None
    if not INTERNAL_KEY:
        return {"error": "internal API key is not configured on this node — "
                         "run 'sudo oaap update' to generate it"}, 503
    if not secrets.compare_digest(
            request.headers.get(INTERNAL_HEADER, ""), INTERNAL_KEY):
        return {"error": "internal API requires the platform key"}, 401
    return None


# ------------------------------------------------- MQTT broker auth (RFC-0032 D2)
# oaap.events.broker 0.1: mosquitto-go-auth's http backend calls these
# two routes for every CONNECT (getuser) and every publish or subscribe
# (aclcheck) on the broker gated by the 'broker' node profile
# (services/broker/mosquitto.conf.template). Deliberately NOT under
# '/internal/' -- that prefix's guard above checks a HEADER
# (X-OAAP-Internal-Key), and the plugin has no configuration option to
# set one (checked against its own documentation while building this).
# The one exception to that convention: the same shared secret travels
# in the query string instead. Safe here because this call never leaves
# the compose network -- mosquitto.conf.template's auth_opt_http_host is
# the container name 'identity', never something a browser or a device
# can reach or observe.
MQTT_AUTH_PARAM = "k"

# The platform's own outbox relay (oaap.data.twin 0.3, RFC-0032 build
# order step 2) -- Jörg's decision of 2026-09-12: a node secret of its
# own, not one RFC-0027 key per tenant. A key is confined to its own
# tenant's tree (_topic_allowed below); the relay publishes for EVERY
# tenant on the node. The dot makes RELAY_USER a name no key id
# ([0-9a-f]{8}, KEY_TOKEN_RE) can ever be, so the two paths can never be
# mistaken for each other. Must match services/twin/relay.py exactly.
RELAY_USER = "oaap.relay"
RELAY_KEY = os.environ.get("BROKER_RELAY_KEY", "")
# mosquitto-go-auth's 'acc': 1 read, 2 write (publish), 3 read+write,
# 4 subscribe. The relay only ever publishes.
MQTT_ACC_WRITE = 2
# Exactly the shape relay.topic_for() builds -- tenant, type, object,
# optionally group; literal levels only, never a wildcard.
_RELAY_TOPIC_RE = re.compile(
    r"^oaap/([0-9a-f-]{36})/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"
    r"(?:/[A-Za-z0-9._-]+)?$")


def _relay_login_ok(password):
    """Fail closed: a node whose relay secret was never generated has no
    relay principal at all -- not even for an empty password."""
    return bool(RELAY_KEY) and secrets.compare_digest(
        (password or "").encode("utf-8"), RELAY_KEY.encode("utf-8"))


def _relay_topic_ok(topic, acc):
    """Publish only, and only into the tree of a tenant this node knows."""
    try:
        acc = int(acc)
    except (TypeError, ValueError):
        return False
    m = _RELAY_TOPIC_RE.fullmatch(topic or "")
    return (bool(RELAY_KEY) and acc == MQTT_ACC_WRITE and m is not None
            and m.group(1) in known_tenants())


def _mqtt_auth_ok():
    if not INTERNAL_KEY:
        return False
    return secrets.compare_digest(
        request.args.get(MQTT_AUTH_PARAM, ""), INTERNAL_KEY)


def _topic_allowed(topic, tenant):
    """Is `topic` inside this principal's own 'oaap/<tenant>/...' tree?

    RFC-0032 §1.1: tenant first, so one rule scopes a client to
    everything it owns and nothing else -- the same boundary-at-the-edge
    principle RFC-0022 uses at the gateway. Applies identically to a
    literal publish topic and a subscription filter (mosquitto passes
    both through aclcheck the same way, wildcards included): a filter
    like 'oaap/<tenant>/#' still starts with 'oaap/<tenant>/', and one
    reaching outside it (a bare '#', or another tenant's prefix) does
    not.
    """
    if not tenant:
        return False
    prefix = f"oaap/{tenant}"
    return topic == prefix or topic.startswith(prefix + "/")


@app.post("/mqtt-auth/getuser")
def mqtt_auth_getuser():
    """Which principal is this MQTT client? RFC-0027 method 'key', reused:

    the client's MQTT password carries the full token
    ('oaapk_<id>_<secret>'); its username must be that same id, in the
    clear -- a copy-paste/ordering mistake refuses rather than silently
    authenticating as the wrong half of the pair. Only an UNSCOPED key
    is accepted (instance=None): an instance-scoped key like the twin's
    own is for one app's HTTP calls through '/twin/*', not for this.
    """
    if not _mqtt_auth_ok():
        return {"Ok": False, "Error": "denied"}, 200
    body = request.get_json(force=True, silent=True) or {}
    if body.get("username", "") == RELAY_USER:
        if _relay_login_ok(body.get("password", "")):
            return {"Ok": True, "Error": ""}, 200
        return {"Ok": False, "Error": "invalid credentials"}, 200
    token = body.get("password", "")
    m = KEY_TOKEN_RE.fullmatch(token)
    if not m or body.get("username", "") != m.group(1):
        return {"Ok": False, "Error": "invalid credentials"}, 200
    user, _method, err = _by_key(token, instance=None)
    if err or not user:
        return {"Ok": False, "Error": "invalid credentials"}, 200
    return {"Ok": True, "Error": ""}, 200


@app.post("/mqtt-auth/aclcheck")
def mqtt_auth_aclcheck():
    """May this already-authenticated client touch this topic?

    Runs after getuser already accepted the CONNECT, so this only looks
    the key back up by id (the MQTT username) for its tenant -- no
    secret to check again, the same way a session is trusted for the
    rest of its lifetime once /verify has accepted it once.
    """
    if not _mqtt_auth_ok():
        return {"Ok": False, "Error": "denied"}, 200
    body = request.get_json(force=True, silent=True) or {}
    kid = body.get("username", "")
    if kid == RELAY_USER:
        # Publish only, never subscribe or read, and only into a known
        # tenant's tree -- the relay has no reason to hear anything.
        if _relay_topic_ok(body.get("topic", ""), body.get("acc")):
            return {"Ok": True, "Error": ""}, 200
        return {"Ok": False,
                "Error": "the relay may only publish into a known tenant's tree"}, 200
    rec = next((k for k in load_keys()
                if k["id"] == kid and not k["revoked"]), None)
    if not rec:
        return {"Ok": False, "Error": "unknown key"}, 200
    if _topic_allowed(body.get("topic", ""), rec.get("tenant", "")):
        return {"Ok": True, "Error": ""}, 200
    return {"Ok": False, "Error": "topic outside this principal's tenant"}, 200


@app.get("/internal/status")
def internal_status():
    state = _load(STATE_FILE, {})
    return {"setup_done": bool(state.get("setup_done"))}


@app.get("/internal/throttle-braked")
def internal_throttle_braked():
    """Per-instance count of braked requests in the last 24 hours.

    Answers for ALL workers, not just this one: the file is the state,
    written through on every braked request, so no worker holds a count
    that this answer would miss.
    """
    try:
        with open(BRAKED_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    oldest = int(time.time() // 3600) - BRAKED_HOURS + 1
    out = {}
    for scope, buckets in data.items():
        fresh = {int(h): c for h, c in buckets.items()
                 if str(h).isdigit() and int(h) >= oldest}
        if fresh:
            out[scope] = {"count": sum(fresh.values()),
                          "last_hour": max(fresh)}
    return {"hours": BRAKED_HOURS, "instances": out}


@app.post("/internal/setup")
def internal_setup():
    """Create the first admin. Called by the portal's first-run wizard.

    Under the user-file lock (RFC-0040 D6) like every other write: two
    wizard submissions arriving together would both find the store
    empty, and the second would overwrite the first -- leaving a node
    whose one administrator cannot sign in with the password its
    operator just chose.
    """
    with _users_lock():
        return _first_admin()


def _first_admin():
    state = _load(STATE_FILE, {})
    if state.get("setup_done"):
        return {"error": "Die Einrichtung ist bereits abgeschlossen; das Token ist nicht mehr gültig."}, 410
    if load_users():
        return {"error": "Es existieren bereits Benutzer."}, 409

    body = request.get_json(force=True)
    if not secrets.compare_digest(body.get("token", ""), os.environ["SETUP_TOKEN"]):
        return {"error": "Das Setup-Token ist ungültig."}, 403
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not USERNAME_RE.fullmatch(username) or len(password) < 8:
        return {"error": "Benutzername: Kleinbuchstaben/Ziffern/._- (2–40 Zeichen); "
                         "das Passwort braucht mindestens 8 Zeichen."}, 400

    _save(USERS_FILE, [{
        "username": username,
        # RFC-0040 §3.1: the first user gets an identity like every
        # other one. Written here rather than left to the backfill so
        # that the very first record on a node is complete.
        "id": new_user_id(),
        "display_name": "",
        "email": "",
        "email_verified": False,
        "password_hash": generate_password_hash(password),
        # RFC-0008: the initial user gets both server_admin (platform
        # authority — can designate further server admins) and admin
        # (app-facing, unchanged) — no behavior change for the common
        # single-operator install.
        "roles": ["server_admin", "admin", "keyuser"],
        "groups": [],
        # The first user of a fresh node joins its default tenant. May
        # be empty here when the node is being set up before the first
        # `oaap update`; the migration above fills it in later, and the
        # reading rule covers the gap in the meantime.
        "tenant": default_tenant_id(),
        "active": True,
    }])
    # Both migrations are marked done on a fresh node: there is nothing
    # to upgrade, and the first user deliberately gets neither `partner`
    # nor `support` -- a node's own operator holds server_admin, which
    # already sees everything (RFC-0039 §3.7).
    _save(STATE_FILE, {"setup_done": True, "server_admin_migrated": True,
                       "support_migrated": True})
    return {"ok": True}, 201


def _validated_roles(raw):
    roles = [r for r in (raw or []) if r in ASSIGNABLE_ROLES]
    if not roles:
        raise ValueError("Mindestens eine gültige Rolle ist erforderlich.")
    return sorted(set(roles))


def _validated_email(raw):
    """An address, or "" (RFC-0040 §3.2). Never a guess."""
    value = (raw or "").strip()
    if not value:
        return ""
    if len(value) > EMAIL_MAX or not EMAIL_RE.fullmatch(value):
        raise ValueError("Das sieht nicht wie eine E-Mail-Adresse aus "
                         "(name@beispiel.de).")
    return value


def _validated_groups(raw):
    """Free-form visibility tags (RFC-0007) — no registry, just a
    filtered, deduplicated list of short lowercase tokens."""
    groups = [g.strip().lower() for g in (raw or []) if g and g.strip()]
    bad = [g for g in groups if not GROUP_RE.fullmatch(g)]
    if bad:
        raise ValueError(f"Ungültige Gruppen-Stichworte: {', '.join(bad)} "
                          "(Kleinbuchstaben/Ziffern/._-, max. 40 Zeichen).")
    return sorted(set(groups))


def _actor(body_or_args):
    """The user this call is made ON BEHALF OF, from the portal.

    Required, and deliberately so: without it identity cannot tell a
    server_admin from a tenant_admin, and would have to assume the more
    powerful one. The internal API is key-protected, but a boundary
    that depends on the caller remembering to mention itself is not a
    boundary.
    """
    return str(body_or_args.get("actor") or "").strip()


def _key_visible(role, actor_tenant, actor_name, k):
    """Who may see a key: the node (server_admin), one tenant
    (tenant_admin), or one's own (anybody else).

    Same three answers as users_list, and for the same reason -- a
    portal that cannot ask "which of these are mine" would have to be
    told, and being told is not a boundary.
    """
    if role == "server_admin":
        return True
    if role == "tenant_admin":
        return resolve_tenant(k.get("tenant")) == actor_tenant
    return k.get("principal") == actor_name


@app.get("/internal/keys")
def keys_list():
    actor_name = _actor(request.args)
    if not actor_name:
        return {"error": "actor fehlt."}, 400
    role, actor_tenant, _err = authority(actor_name)
    keys = [k for k in load_keys()
            if _key_visible(role, actor_tenant, actor_name, k)]
    return {"keys": [public_key(k) for k in keys],
            "scope": role or "self"}


@app.post("/internal/keys")
def keys_create():
    """Issue one key. The secret is in this response and nowhere else,
    ever again (RFC-0027 3.3)."""
    body = request.get_json(force=True)
    actor_name = _actor(body)
    role, actor_tenant, err = authority(actor_name)
    if not role:
        return {"error": err or "Nicht berechtigt."}, 403
    users = load_users()
    principal = (body.get("principal") or "").strip()
    target = find_user(users, principal)
    if not target:
        return {"error": f"Diesen Prinzipal gibt es nicht: '{principal}'."}, 404
    # RFC-0027 D4: a tenant_admin issues inside their own tenant and
    # nowhere else. Checked against the ACTOR'S record, never against a
    # tenant named in the request.
    if not may_see(role, actor_tenant, target):
        return {"error": "Dieser Prinzipal gehoert zu einem anderen "
                         "Mandanten."}, 403
    actor = find_user(users, actor_name)
    wanted = {r for r in (body.get("roles") or []) if r in ASSIGNABLE_ROLES}
    # The second ceiling (RFC-0027 3.3): nobody hands out authority they
    # do not hold. Without this a tenant_admin could mint a key with a
    # role they cannot otherwise grant.
    excess = wanted - set((actor or {}).get("roles") or [])
    if excess:
        return {"error": "Sie koennen keine Rolle vergeben, die Sie selbst "
                         "nicht halten: " + ",".join(sorted(excess))}, 403
    try:
        rec, secret = issue_key(
            users, principal, sorted(wanted), body.get("instance") or "",
            body.get("label") or "", body.get("days"), actor_name,
            terminal=bool(body.get("terminal")))
    except ValueError as e:
        return {"error": str(e)}, 400
    audit("key.issue", rec["tenant"], principal, who=actor_name, role=role,
          detail=f"key {rec['id']}, roles: " + ",".join(rec["roles"])
                 + (f", instance {rec['instance']}" if rec["instance"] else "")
                 + f", expires {rec['expires'][:10]}")
    return {"ok": True, "key": public_key(rec), "secret": secret}, 201


@app.post("/internal/keys/<kid>/revoke")
def keys_revoke(kid):
    body = request.get_json(force=True, silent=True) or {}
    actor_name = _actor(body)
    role, actor_tenant, err = authority(actor_name)
    if not role:
        return {"error": err or "Nicht berechtigt."}, 403
    rec = next((k for k in load_keys() if k["id"] == kid), None)
    if rec is None:
        return {"error": f"Diesen Schluessel gibt es nicht: '{kid}'."}, 404
    if not _key_visible(role, actor_tenant, actor_name, rec):
        return {"error": "Dieser Schluessel gehoert zu einem anderen "
                         "Mandanten."}, 403
    if rec["revoked"]:
        return {"ok": True, "already": True}
    revoke_key(kid)
    audit("key.revoke", rec.get("tenant", ""), rec["principal"],
          who=actor_name, role=role, detail=f"key {kid}")
    return {"ok": True}


@app.get("/internal/users")
def users_list():
    """The users this actor may see (spec 2.4).

    Three answers, not two: a server_admin sees the node, a
    tenant_admin sees their own tenant, and anybody else sees only
    themselves -- which is what the portal needs to look up its own
    caller's visibility groups without being an administration call.
    """
    actor_name = _actor(request.args)
    if not actor_name:
        return {"error": "actor fehlt."}, 400
    role, actor_tenant, _err = authority(actor_name)
    users = load_users()
    if role == "server_admin":
        visible = users
    elif role == "tenant_admin":
        visible = [u for u in users if may_see(role, actor_tenant, u)]
    else:
        visible = [u for u in users if u["username"] == actor_name]
    return {"users": [public_user(u) for u in visible],
            "scope": role or "self",
            "tenant": actor_tenant if role == "tenant_admin" else ""}


@app.post("/internal/users")
def users_create():
    body = request.get_json(force=True)
    actor_name = _actor(body)
    role, actor_tenant, err = authority(actor_name)
    if not role:
        return {"error": err or "Nicht berechtigt."}, 403
    # Under the lock from here (RFC-0040 D6): the duplicate-name check
    # and the append have to be one indivisible step, or two requests
    # naming the same user both find it free and one of them is lost.
    # That is unreachable while an administrator types names in the
    # portal, and ordinary the moment records appear through incoming
    # traffic (RFC-0040 §4).
    with users_rw() as users:
        return _create_user(body, users, actor_name, role, actor_tenant)


def _create_user(body, users, actor_name, role, actor_tenant):
    username = (body.get("username") or "").strip()
    if not USERNAME_RE.fullmatch(username):
        return {"error": "Benutzername: Kleinbuchstaben/Ziffern/._- (2–40 Zeichen)."}, 400
    if find_user(users, username):
        return {"error": f"Benutzer '{username}' existiert bereits."}, 409
    # A machine principal (RFC-0027 3.1) has no password at all -- it
    # authenticates by key and cannot use the login form. Giving it an
    # unusable password instead would leave a hash nobody can explain.
    kind = "machine" if (body.get("kind") == "machine") else "human"
    if kind == "human" and len(body.get("password") or "") < 8:
        return {"error": "Das Passwort braucht mindestens 8 Zeichen."}, 400
    try:
        roles = _validated_roles(body.get("roles"))
        groups = _validated_groups(body.get("groups"))
        email = _validated_email(body.get("email"))
    except ValueError as e:
        return {"error": str(e)}, 400
    # Which tenant the account is created into (spec 2.2). A
    # server_admin may name one; anyone else gets their own, whatever
    # the request says -- rule 3 of oaap.core.tenant 2.3, and the
    # reason the role cannot walk out of its tenant.
    if role == "server_admin":
        tenant = resolve_tenant(body.get("tenant") or "")
        if tenant is None:
            return {"error": "Diesen Mandanten gibt es auf diesem Knoten nicht."}, 400
    else:
        tenant = actor_tenant
    # Rule 1 of spec 2.3: a tenant_admin may not grant a node-wide role.
    # Without that the role is a two-step path out of its tenant --
    # create an account, give it server_admin (the node) or support
    # (the health page, which names every instance on the machine),
    # sign in as it. Granting tenant_admin IS allowed, because `tenant`
    # above is already forced to the actor's own: the new administrator
    # cannot land anywhere else.
    if role == "tenant_admin" and NODE_WIDE_ROLES & set(roles):
        return {"error": "Ein tenant_admin darf server_admin und support "
                         "nicht vergeben."}, 403
    # server_admin on a machine is refused for the same reason a key
    # may not carry it (RFC-0027 D2): nothing that authenticates from a
    # config file should hold the node.
    if kind == "machine" and "server_admin" in roles:
        return {"error": "Ein Maschinen-Prinzipal darf server_admin nicht "
                         "halten (RFC-0027 D2)."}, 400
    users.append({
        "username": username,
        # RFC-0040 D1: assigned here, at creation, and never again.
        "id": new_user_id(),
        "display_name": (body.get("display_name") or "").strip()[:DISPLAY_NAME_MAX],
        # An address an administrator types is not thereby proven. The
        # flag is set in a separate, deliberate step (users_update), so
        # that asserting "this address is real" is never something that
        # happens as a side effect of filling in a form.
        "email": email,
        "email_verified": False,
        "password_hash": ("" if kind == "machine"
                          else generate_password_hash(body["password"])),
        "kind": kind,
        "roles": roles,
        "groups": groups,
        "tenant": tenant,
        "active": True,
    })
    save_users(users)
    audit("user.create", tenant, username, who=actor_name, role=role,
          detail=("machine, " if kind == "machine" else "")
                 + "roles: " + ",".join(roles))
    return {"ok": True}, 201


@app.put("/internal/users/<username>")
def users_update(username):
    body = request.get_json(force=True)
    actor_name = _actor(body)
    role, actor_tenant, err = authority(actor_name)
    if not role:
        return {"error": err or "Nicht berechtigt."}, 403
    with users_rw() as users:
        return _update_user(username, body, users, actor_name, role, actor_tenant)


def _update_user(username, body, users, actor_name, role, actor_tenant):
    u = find_user(users, username)
    # "Not found", not "forbidden", for a user of another tenant: the
    # difference between the two answers tells a tenant_admin that the
    # name exists somewhere on this node, which is already information
    # across the boundary (spec 2.3 rule 2).
    if not u or not may_see(role, actor_tenant, u):
        return {"error": "Benutzer nicht gefunden."}, 404
    if role == "tenant_admin" and NODE_WIDE_ROLES & set(body.get("roles") or []):
        return {"error": "Ein tenant_admin darf server_admin und support "
                         "nicht vergeben."}, 403
    try:
        roles = _validated_roles(body.get("roles"))
        groups = _validated_groups(body.get("groups"))
        email = _validated_email(body.get("email"))
    except ValueError as e:
        return {"error": str(e)}, 400
    active = bool(body.get("active", True))
    # Last-server_admin protection (RFC-0008): the platform must keep
    # at least one active server_admin, or nobody could manage users,
    # edge routes, external hostnames or the store any more.
    loses_server_admin = "server_admin" in u["roles"] and u["active"] and \
                         ("server_admin" not in roles or not active)
    if loses_server_admin and not other_active_server_admin_exists(users, username):
        return {"error": "Das ist der letzte aktive server_admin — "
                         "bitte zuerst jemand anderem server_admin geben."}, 409
    was_roles, was_active = list(u["roles"]), u["active"]
    was_email, was_verified = u.get("email", ""), bool(u.get("email_verified"))
    u["roles"] = roles
    u["groups"] = groups
    u["active"] = active
    u["display_name"] = (body.get("display_name") or "").strip()[:DISPLAY_NAME_MAX]
    # RFC-0040 §3.2: a CHANGED address is an address nobody proved, so
    # the flag falls with it -- the caller cannot keep an old assertion
    # alive under a new address by simply not mentioning the flag. An
    # unchanged address keeps whatever the request says, which is how an
    # administrator asserts one ("I know this person, this is their
    # address"), the only writer of the flag until an identity provider
    # brings a verification flow (§6).
    u["email"] = email
    u["email_verified"] = (bool(body.get("email_verified"))
                           and bool(email) and email == was_email)
    # The tenant is deliberately NOT settable here (spec 2.2): moving a
    # user between tenants is moving a person between customers, and the
    # honest form of that is a new account, not a field edit.
    # The `id` is not settable at all, by anybody, ever (RFC-0040 D1) --
    # it is not in this list, and there is no request that puts it there.
    save_users(users)
    detail = "roles: " + ",".join(roles)
    if set(was_roles) != set(roles):
        detail += " (was " + ",".join(was_roles) + ")"
    if was_active != active:
        detail += "; deactivated" if not active else "; reactivated"
    if was_email != email:
        detail += "; e-mail set" if email else "; e-mail removed"
    if u["email_verified"] != was_verified:
        detail += ("; e-mail asserted as verified" if u["email_verified"]
                   else "; e-mail no longer verified")
    elif bool(body.get("email_verified")) and not u["email_verified"]:
        # Said in the log, not swallowed: the caller asked for the
        # assertion and did not get it, because the address changed in
        # the same request. A silent "no" here is how an administrator
        # comes to believe an address was proven.
        detail += "; verification refused (the address changed)"
    audit("user.change", resolve_tenant(u.get("tenant")) or "", username,
          who=actor_name, role=role, detail=detail)
    return {"ok": True}


@app.post("/internal/users/<username>/password")
def users_set_password(username):
    body = request.get_json(force=True)
    actor_name = _actor(body)
    role, actor_tenant, err = authority(actor_name)
    if not role:
        return {"error": err or "Nicht berechtigt."}, 403
    with users_rw() as users:
        u = find_user(users, username)
        if not u or not may_see(role, actor_tenant, u):
            return {"error": "Benutzer nicht gefunden."}, 404
        if len(body.get("password") or "") < 8:
            return {"error": "Das Passwort braucht mindestens 8 Zeichen."}, 400
        u["password_hash"] = generate_password_hash(body["password"])
        save_users(users)
    audit("user.password", resolve_tenant(u.get("tenant")) or "", username,
          who=actor_name, role=role)
    return {"ok": True}
