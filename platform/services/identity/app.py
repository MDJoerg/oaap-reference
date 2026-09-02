"""OAAP built-in minimal identity provider (oaap.core.identity, skeleton).

Local user store on disk, username + password, session cookie. The
gateway calls /verify on every request (forward auth); /auth/* is the
only identity surface exposed through the gateway. /internal/* is
reachable only on the internal container network.

Spec oaap.core.identity 2.3: /verify evaluates the current user store
on every call — sessions carry only the username, never roles — so
role changes and deactivation act on the user's next request.
"""

import json
import os
import re
import secrets
import tempfile
import time
from datetime import timedelta

from flask import Flask, redirect, render_template_string, request, session
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
ASSIGNABLE_ROLES = ("server_admin", "tenant_admin", "admin", "keyuser",
                    "user", "guest", "partner")
# Roles whose authority reaches past a tenant. server_admin administers
# the node; partner reads the health page, which lists every instance on
# the machine. A tenant_admin may grant neither -- otherwise the role is
# a two-step path out of its own tenant (oaap.core.tenant 2.3 rule 1).
NODE_WIDE_ROLES = frozenset({"server_admin", "partner"})
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
# Free-form visibility tags (RFC-0007) — no registry, a group exists
# the moment any user carries it. Kept short and simple like usernames.
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

# API keys (RFC-0027). A key is the second way to answer the one
# question /verify asks -- WHICH PRINCIPAL IS THIS -- and nothing after
# that answer knows the difference: roles, visibility groups, tenant and
# the two headers are the same code for a cookie and for a key.
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


def load_users():
    users = _load(USERS_FILE, [])
    for u in users:
        u.setdefault("display_name", "")
        u.setdefault("active", True)
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
    users = load_users()
    changed = False
    for u in users:
        if not u.get("tenant"):
            u["tenant"] = tid
            changed = True
    if changed:
        _save(USERS_FILE, users)
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
    users = load_users()
    changed = False
    for u in users:
        if "admin" in u["roles"] and "server_admin" not in u["roles"]:
            u["roles"] = sorted(set(u["roles"]) | {"server_admin"})
            changed = True
    if changed:
        _save(USERS_FILE, users)
        print(f"RFC-0008 migration: granted server_admin to "
              f"{sum(1 for u in users if 'server_admin' in u['roles'])} existing admin(s)",
              flush=True)
    state["server_admin_migrated"] = True
    _save(STATE_FILE, state)


_migrate_server_admin_once()
_migrate_tenant_once()


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
    """
    return {"username": u["username"], "display_name": u["display_name"],
            "roles": u["roles"], "groups": u["groups"], "active": u["active"],
            "tenant": u.get("tenant", ""), "kind": u.get("kind", "human")}


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
        return None, "session", redirect("/auth/login", code=303)
    kid = session.get("terminal_key")
    if kid and not _terminal_key_ok(kid):
        session.clear()
        return None, "session", redirect("/auth/login", code=303)
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
    return "", 204, {
        "X-OAAP-User": user["username"],
        "X-OAAP-Roles": ",".join(user["roles"]),
    }


@app.get("/auth/login")
def login_form():
    return render_template_string(LOGIN_PAGE, error=None, has_users=bool(load_users()))


@app.post("/auth/login")
def login():
    users = load_users()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    throttle_key = f"{_client_ip()}|{username}"
    if _login_blocked(throttle_key):
        return render_template_string(
            LOGIN_PAGE, error="Zu viele Fehlversuche — bitte eine Minute warten.",
            has_users=bool(users)), 429
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
        return redirect("/", code=303)
    _login_failed(throttle_key)
    print(f"login failed: '{username}' from {_client_ip()}", flush=True)
    return render_template_string(
        LOGIN_PAGE, error="Benutzername oder Passwort ist falsch.", has_users=bool(users)
    ), 401


def _revoke_sessions(username):
    """Bump a user's session epoch, invalidating every copy of their
    cookie immediately (see load_users()/verify()) — used by logout and
    password change, since a signed cookie cannot otherwise be revoked.
    """
    users = load_users()
    u = find_user(users, username)
    if u:
        u["session_epoch"] = u.get("session_epoch", 0) + 1
        _save(USERS_FILE, users)


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
    return redirect("/auth/login", code=303)


@app.get("/auth/password")
def password_form():
    if not session_username():
        return redirect("/auth/login", code=303)
    return render_template_string(PASSWORD_PAGE, error=None, done=False)


@app.post("/auth/password")
def password_change():
    """Self-service password change (spec 2.4)."""
    users = load_users()
    u = find_user(users, session_username() or "")
    if not u or not u["active"]:
        return redirect("/auth/login", code=303)
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
    _save(USERS_FILE, users)
    print(f"password changed: {u['username']} (other sessions revoked)", flush=True)
    return render_template_string(PASSWORD_PAGE, error=None, done=True)


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
    """Create the first admin. Called by the portal's first-run wizard."""
    state = _load(STATE_FILE, {})
    if state.get("setup_done"):
        return {"error": "Die Einrichtung ist bereits abgeschlossen; das Token ist nicht mehr gültig."}, 410
    users = load_users()
    if users:
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
        "display_name": "",
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
    _save(STATE_FILE, {"setup_done": True, "server_admin_migrated": True})
    return {"ok": True}, 201


def _validated_roles(raw):
    roles = [r for r in (raw or []) if r in ASSIGNABLE_ROLES]
    if not roles:
        raise ValueError("Mindestens eine gültige Rolle ist erforderlich.")
    return sorted(set(roles))


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
    users = load_users()
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
    # create an account, give it server_admin (the node) or partner
    # (the health page, which names every instance on the machine),
    # sign in as it. Granting tenant_admin IS allowed, because `tenant`
    # above is already forced to the actor's own: the new administrator
    # cannot land anywhere else.
    if role == "tenant_admin" and NODE_WIDE_ROLES & set(roles):
        return {"error": "Ein tenant_admin darf server_admin und partner "
                         "nicht vergeben."}, 403
    # server_admin on a machine is refused for the same reason a key
    # may not carry it (RFC-0027 D2): nothing that authenticates from a
    # config file should hold the node.
    if kind == "machine" and "server_admin" in roles:
        return {"error": "Ein Maschinen-Prinzipal darf server_admin nicht "
                         "halten (RFC-0027 D2)."}, 400
    users.append({
        "username": username,
        "display_name": (body.get("display_name") or "").strip(),
        "password_hash": ("" if kind == "machine"
                          else generate_password_hash(body["password"])),
        "kind": kind,
        "roles": roles,
        "groups": groups,
        "tenant": tenant,
        "active": True,
    })
    _save(USERS_FILE, users)
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
    users = load_users()
    u = find_user(users, username)
    # "Not found", not "forbidden", for a user of another tenant: the
    # difference between the two answers tells a tenant_admin that the
    # name exists somewhere on this node, which is already information
    # across the boundary (spec 2.3 rule 2).
    if not u or not may_see(role, actor_tenant, u):
        return {"error": "Benutzer nicht gefunden."}, 404
    if role == "tenant_admin" and NODE_WIDE_ROLES & set(body.get("roles") or []):
        return {"error": "Ein tenant_admin darf server_admin und partner "
                         "nicht vergeben."}, 403
    try:
        roles = _validated_roles(body.get("roles"))
        groups = _validated_groups(body.get("groups"))
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
    u["roles"] = roles
    u["groups"] = groups
    u["active"] = active
    u["display_name"] = (body.get("display_name") or "").strip()
    # The tenant is deliberately NOT settable here (spec 2.2): moving a
    # user between tenants is moving a person between customers, and the
    # honest form of that is a new account, not a field edit.
    _save(USERS_FILE, users)
    detail = "roles: " + ",".join(roles)
    if set(was_roles) != set(roles):
        detail += " (was " + ",".join(was_roles) + ")"
    if was_active != active:
        detail += "; deactivated" if not active else "; reactivated"
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
    users = load_users()
    u = find_user(users, username)
    if not u or not may_see(role, actor_tenant, u):
        return {"error": "Benutzer nicht gefunden."}, 404
    if len(body.get("password") or "") < 8:
        return {"error": "Das Passwort braucht mindestens 8 Zeichen."}, 400
    u["password_hash"] = generate_password_hash(body["password"])
    _save(USERS_FILE, users)
    audit("user.password", resolve_tenant(u.get("tenant")) or "", username,
          who=actor_name, role=role)
    return {"ok": True}
