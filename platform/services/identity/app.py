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

from flask import Flask, redirect, render_template_string, request, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = "/data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Standard roles a user account may hold (RFC-0002 + RFC-0008; `public`
# is a route marker, not a role). server_admin is platform authority
# (users, groups, edge/external routing, backup, visibility bypass) and
# is never forwarded to apps as something app-specific — see RFC-0008.
# admin is unchanged: an app-facing role only, carrying no platform
# authority by itself.
ASSIGNABLE_ROLES = ("server_admin", "admin", "keyuser", "user", "guest", "partner")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
# Free-form visibility tags (RFC-0007) — no registry, a group exists
# the moment any user carries it. Kept short and simple like usernames.
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

app = Flask(__name__)
app.secret_key = os.environ["SESSION_SECRET"]
app.config.update(
    SESSION_COOKIE_NAME="oaap_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

EXTERNAL_HOST_FILE = "/platform-apps/external.json"


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
    return users


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


def find_user(users, username):
    return next((u for u in users if u["username"] == username), None)


def session_username():
    user = session.get("user")
    if isinstance(user, dict):  # session format before user management
        return user.get("username")
    return user


def public_user(u):
    """User record for list/UI use — never the password hash (spec 5.7)."""
    return {"username": u["username"], "display_name": u["display_name"],
            "roles": u["roles"], "groups": u["groups"], "active": u["active"]}


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
    """
    username = session_username()
    user = find_user(load_users(), username) if username else None
    if (not user or not user["active"]
            or session.get("epoch") != user.get("session_epoch", 0)):
        session.clear()
        return redirect("/auth/login", code=303)
    required = request.args.get("roles", "")
    if required and not set(required.split(",")) & set(user["roles"]):
        return "Forbidden: missing role", 403
    required_groups = request.args.get("groups", "")
    if (required_groups and "server_admin" not in user["roles"]
            and not set(required_groups.split(",")) & set(user["groups"])):
        return "Forbidden: not in a visibility group for this app", 403
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
    if u and u["active"] and check_password_hash(u["password_hash"], password):
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


@app.post("/auth/logout")
def logout():
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
# Internal API — only reachable on the container network (spec 4.3).
# The portal is responsible for admin authorization of its callers.

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


@app.get("/internal/users")
def users_list():
    return {"users": [public_user(u) for u in load_users()]}


@app.post("/internal/users")
def users_create():
    body = request.get_json(force=True)
    users = load_users()
    username = (body.get("username") or "").strip()
    if not USERNAME_RE.fullmatch(username):
        return {"error": "Benutzername: Kleinbuchstaben/Ziffern/._- (2–40 Zeichen)."}, 400
    if find_user(users, username):
        return {"error": f"Benutzer '{username}' existiert bereits."}, 409
    if len(body.get("password") or "") < 8:
        return {"error": "Das Passwort braucht mindestens 8 Zeichen."}, 400
    try:
        roles = _validated_roles(body.get("roles"))
        groups = _validated_groups(body.get("groups"))
    except ValueError as e:
        return {"error": str(e)}, 400
    users.append({
        "username": username,
        "display_name": (body.get("display_name") or "").strip(),
        "password_hash": generate_password_hash(body["password"]),
        "roles": roles,
        "groups": groups,
        "active": True,
    })
    _save(USERS_FILE, users)
    return {"ok": True}, 201


@app.put("/internal/users/<username>")
def users_update(username):
    body = request.get_json(force=True)
    users = load_users()
    u = find_user(users, username)
    if not u:
        return {"error": "Benutzer nicht gefunden."}, 404
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
    u["roles"] = roles
    u["groups"] = groups
    u["active"] = active
    u["display_name"] = (body.get("display_name") or "").strip()
    _save(USERS_FILE, users)
    return {"ok": True}


@app.post("/internal/users/<username>/password")
def users_set_password(username):
    body = request.get_json(force=True)
    users = load_users()
    u = find_user(users, username)
    if not u:
        return {"error": "Benutzer nicht gefunden."}, 404
    if len(body.get("password") or "") < 8:
        return {"error": "Das Passwort braucht mindestens 8 Zeichen."}, 400
    u["password_hash"] = generate_password_hash(body["password"])
    _save(USERS_FILE, users)
    return {"ok": True}
