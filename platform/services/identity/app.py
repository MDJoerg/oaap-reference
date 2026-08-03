"""OAAP built-in minimal identity provider (oaap.core.identity, skeleton).

Local user store on disk, username + password, session cookie. The
gateway calls /verify on every request (forward auth); /auth/* is the
only identity surface exposed through the gateway. /internal/* is
reachable only on the internal container network.
"""

import json
import os
import secrets
import tempfile

from flask import Flask, redirect, render_template_string, request, session
from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = "/data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

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


@app.get("/verify")
def verify():
    """Forward-auth endpoint for the gateway (RFC-0002 default deny).

    Optional ?roles=a,b restricts the route to sessions holding at
    least one of the given roles (route-level authorization from the
    app manifest, spec oaap.apps.runtime 2.4).
    """
    user = session.get("user")
    if not user:
        return redirect("/auth/login", code=303)
    required = request.args.get("roles", "")
    if required:
        if not set(required.split(",")) & set(user["roles"]):
            return "Forbidden: missing role", 403
    return "", 204, {
        "X-OAAP-User": user["username"],
        "X-OAAP-Roles": ",".join(user["roles"]),
    }


@app.get("/auth/login")
def login_form():
    users = _load(USERS_FILE, [])
    return render_template_string(LOGIN_PAGE, error=None, has_users=bool(users))


@app.post("/auth/login")
def login():
    users = _load(USERS_FILE, [])
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    for u in users:
        if u["username"] == username and check_password_hash(u["password_hash"], password):
            session["user"] = {"username": u["username"], "roles": u["roles"]}
            return redirect("/", code=303)
    return render_template_string(
        LOGIN_PAGE, error="Invalid username or password.", has_users=bool(users)
    ), 401


@app.post("/auth/logout")
def logout():
    session.clear()
    return redirect("/auth/login", code=303)


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
    users = _load(USERS_FILE, [])
    if users:
        return {"error": "Users already exist."}, 409

    body = request.get_json(force=True)
    if not secrets.compare_digest(body.get("token", ""), os.environ["SETUP_TOKEN"]):
        return {"error": "Invalid setup token."}, 403
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or len(password) < 8:
        return {"error": "Username required; password must have at least 8 characters."}, 400

    _save(USERS_FILE, [{
        "username": username,
        "password_hash": generate_password_hash(password),
        "roles": ["admin", "keyuser"],
    }])
    _save(STATE_FILE, {"setup_done": True})
    return {"ok": True}, 201
