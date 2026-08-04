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

from flask import Flask, redirect, render_template_string, request, session
from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = "/data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Standard roles a user account may hold (RFC-0002; `public` is a route
# marker, not a role).
ASSIGNABLE_ROLES = ("admin", "keyuser", "user", "guest", "partner")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")

app = Flask(__name__)
app.secret_key = os.environ["SESSION_SECRET"]
app.config.update(
    SESSION_COOKIE_NAME="oaap_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


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
    return users


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
            "roles": u["roles"], "active": u["active"]}


def other_active_admin_exists(users, username):
    return any(u["active"] and "admin" in u["roles"] and u["username"] != username
               for u in users)


LOGIN_PAGE = """
<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OAAP Login</title>
<style>
  body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f4f5f7}
  form{background:#fff;padding:2rem;border-radius:.5rem;box-shadow:0 1px 4px rgba(0,0,0,.1);width:20rem}
  h1{font-size:1.2rem;margin-top:0}
  input{width:100%;box-sizing:border-box;margin:.25rem 0 1rem;padding:.5rem}
  button{width:100%;padding:.6rem;border:0;border-radius:.25rem;background:#2563eb;color:#fff;font-size:1rem}
  .err{color:#b91c1c}.hint{color:#555;font-size:.9rem}
</style>
<form method="post" action="/auth/login">
  <h1>OAAP Login</h1>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  {% if not has_users %}
    <p class="hint">No users exist yet — finish the platform setup first
    (see the URL and token printed by the installer).</p>
  {% endif %}
  <label>Username <input name="username" autofocus autocomplete="username"></label>
  <label>Password <input name="password" type="password" autocomplete="current-password"></label>
  <button>Sign in</button>
</form>
"""

PASSWORD_PAGE = """
<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Change password</title>
<style>
  body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f4f5f7}
  form{background:#fff;padding:2rem;border-radius:.5rem;box-shadow:0 1px 4px rgba(0,0,0,.1);width:20rem}
  h1{font-size:1.2rem;margin-top:0}
  input{width:100%;box-sizing:border-box;margin:.25rem 0 1rem;padding:.5rem}
  button{width:100%;padding:.6rem;border:0;border-radius:.25rem;background:#2563eb;color:#fff;font-size:1rem}
  .err{color:#b91c1c}.ok{color:#15803d}
  a{color:#2563eb}
</style>
<form method="post" action="/auth/password">
  <h1>Change password</h1>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  {% if done %}<p class="ok">Password changed.</p><p><a href="/">Back to the portal</a></p>{% else %}
  <label>Current password <input name="current" type="password" required autocomplete="current-password"></label>
  <label>New password (min. 8 characters)
    <input name="new" type="password" minlength="8" required autocomplete="new-password"></label>
  <button>Change password</button>
  <p><a href="/">Back to the portal</a></p>
  {% endif %}
</form>
"""


@app.get("/verify")
def verify():
    """Forward-auth endpoint for the gateway (RFC-0002 default deny).

    Optional ?roles=a,b restricts the route to users holding at least
    one of the given roles (route-level authorization from the app
    manifest, spec oaap.apps.runtime 2.4). Roles always come from the
    current user store, never from the session (spec 2.3).
    """
    username = session_username()
    user = find_user(load_users(), username) if username else None
    if not user or not user["active"]:
        session.clear()
        return redirect("/auth/login", code=303)
    required = request.args.get("roles", "")
    if required and not set(required.split(",")) & set(user["roles"]):
        return "Forbidden: missing role", 403
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
    u = find_user(users, username)
    # Generic error either way — no username enumeration (spec 4.4).
    if u and u["active"] and check_password_hash(u["password_hash"], password):
        session["user"] = u["username"]
        return redirect("/", code=303)
    return render_template_string(
        LOGIN_PAGE, error="Invalid username or password.", has_users=bool(users)
    ), 401


@app.post("/auth/logout")
def logout():
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
            PASSWORD_PAGE, error="The current password is not correct.", done=False), 403
    new = request.form.get("new", "")
    if len(new) < 8:
        return render_template_string(
            PASSWORD_PAGE, error="The new password needs at least 8 characters.", done=False), 400
    u["password_hash"] = generate_password_hash(new)
    _save(USERS_FILE, users)
    return render_template_string(PASSWORD_PAGE, error=None, done=True)


# ---------------------------------------------------------------------------
# Internal API — only reachable on the container network (spec 4.3).
# The portal is responsible for admin authorization of its callers.

@app.get("/internal/status")
def internal_status():
    state = _load(STATE_FILE, {})
    return {"setup_done": bool(state.get("setup_done"))}


@app.post("/internal/setup")
def internal_setup():
    """Create the first admin. Called by the portal's first-run wizard."""
    state = _load(STATE_FILE, {})
    if state.get("setup_done"):
        return {"error": "Setup is already completed; the token is no longer valid."}, 410
    users = load_users()
    if users:
        return {"error": "Users already exist."}, 409

    body = request.get_json(force=True)
    if not secrets.compare_digest(body.get("token", ""), os.environ["SETUP_TOKEN"]):
        return {"error": "Invalid setup token."}, 403
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not USERNAME_RE.fullmatch(username) or len(password) < 8:
        return {"error": "Username: lowercase letters/digits/._- (2–40 chars); "
                         "password must have at least 8 characters."}, 400

    _save(USERS_FILE, [{
        "username": username,
        "display_name": "",
        "password_hash": generate_password_hash(password),
        "roles": ["admin", "keyuser"],
        "active": True,
    }])
    _save(STATE_FILE, {"setup_done": True})
    return {"ok": True}, 201


def _validated_roles(raw):
    roles = [r for r in (raw or []) if r in ASSIGNABLE_ROLES]
    if not roles:
        raise ValueError("At least one valid role is required.")
    return sorted(set(roles))


@app.get("/internal/users")
def users_list():
    return {"users": [public_user(u) for u in load_users()]}


@app.post("/internal/users")
def users_create():
    body = request.get_json(force=True)
    users = load_users()
    username = (body.get("username") or "").strip()
    if not USERNAME_RE.fullmatch(username):
        return {"error": "Username: lowercase letters/digits/._- (2–40 chars)."}, 400
    if find_user(users, username):
        return {"error": f"User '{username}' already exists."}, 409
    if len(body.get("password") or "") < 8:
        return {"error": "Password must have at least 8 characters."}, 400
    try:
        roles = _validated_roles(body.get("roles"))
    except ValueError as e:
        return {"error": str(e)}, 400
    users.append({
        "username": username,
        "display_name": (body.get("display_name") or "").strip(),
        "password_hash": generate_password_hash(body["password"]),
        "roles": roles,
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
        return {"error": "User not found."}, 404
    try:
        roles = _validated_roles(body.get("roles"))
    except ValueError as e:
        return {"error": str(e)}, 400
    active = bool(body.get("active", True))
    # Last-admin protection (spec 2.4): the platform must keep at least
    # one active admin.
    loses_admin = "admin" in u["roles"] and u["active"] and \
                  ("admin" not in roles or not active)
    if loses_admin and not other_active_admin_exists(users, username):
        return {"error": "This is the last active administrator — "
                         "assign admin to someone else first."}, 409
    u["roles"] = roles
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
        return {"error": "User not found."}, 404
    if len(body.get("password") or "") < 8:
        return {"error": "Password must have at least 8 characters."}, 400
    u["password_hash"] = generate_password_hash(body["password"])
    _save(USERS_FILE, users)
    return {"ok": True}
