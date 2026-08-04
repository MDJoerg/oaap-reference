"""OAAP web portal (oaap.core.portal, skeleton).

Serves the first-run wizard (/setup, protected by the one-time setup
token, validated by the identity service) and a minimal dashboard.
Authentication is entirely the gateway's job: the portal trusts the
X-OAAP-User / X-OAAP-Roles headers set after forward auth.
"""

import json
import os

import requests
from flask import Flask, redirect, render_template_string, request

IDENTITY = "http://identity:8000"
VERSION = os.environ.get("OAAP_VERSION", "unknown")
REGISTRY = "/apps-registry/registry.json"


def launchpad_tiles(user_roles, host):
    """Role-filtered app tiles from the instance registry (spec 2.5).

    The filter is UX only — the gateway enforces the roles on every
    request regardless of what the portal shows.
    """
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            instances = json.load(f).get("instances", {})
    except (OSError, ValueError):
        return []
    tiles = []
    for name, inst in sorted(instances.items()):
        allowed = set(inst.get("roles") or [])
        if allowed and "admin" not in user_roles and not user_roles & allowed:
            continue
        tiles.append({
            "name": inst.get("app_name", name),
            "instance": name,
            "version": inst.get("version", "?"),
            "channel": inst.get("channel", "production"),
            "description": inst.get("description", ""),
            "url": f"http://{host}:{inst['port']}/",
        })
    return tiles

app = Flask(__name__)

STYLE = """
<style>
  body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#1f2328}
  main{max-width:36rem;margin:3rem auto;background:#fff;padding:2rem;
       border-radius:.5rem;box-shadow:0 1px 4px rgba(0,0,0,.1)}
  h1{font-size:1.3rem;margin-top:0}
  input{width:100%;box-sizing:border-box;margin:.25rem 0 1rem;padding:.5rem}
  button{padding:.6rem 1.2rem;border:0;border-radius:.25rem;background:#2563eb;color:#fff;font-size:1rem}
  .err{color:#b91c1c}.ok{color:#15803d}.muted{color:#555;font-size:.9rem}
  dt{font-weight:600}dd{margin:0 0 .75rem}
</style>
"""

DASHBOARD = STYLE + """
<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OAAP Portal</title>
<style>
  .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(14rem,1fr));gap:1rem;margin:1rem 0 2rem}
  .tile{display:block;background:#f8fafc;border:1px solid #e2e8f0;border-radius:.5rem;
        padding:1rem;text-decoration:none;color:inherit}
  .tile:hover{border-color:#2563eb}
  .tile h3{margin:0 0 .25rem;font-size:1.05rem}
  .badge{font-size:.75rem;padding:.1rem .5rem;border-radius:1rem;background:#e2e8f0}
  .badge.test{background:#fef3c7}
</style>
<main>
  <h1>OAAP Portal</h1>
  {% if tiles %}
    <h2 style="font-size:1.05rem">Apps</h2>
    <div class="tiles">
      {% for t in tiles %}
      <a class="tile" href="{{ t.url }}">
        <h3>{{ t.name }}</h3>
        {% if t.description %}<p class="muted">{{ t.description }}</p>{% endif %}
        <span class="muted">v{{ t.version }}</span>
        <span class="badge {{ t.channel }}">{{ t.channel }}</span>
      </a>
      {% endfor %}
    </div>
  {% else %}
    <p class="muted">No apps installed yet (or none visible for your
    roles). Install apps with <code>oaap app install</code>.</p>
  {% endif %}
  <dl>
    <dt>Signed in as</dt><dd>{{ user }}</dd>
    <dt>Roles</dt><dd>{{ roles }}</dd>
    <dt>Platform version</dt><dd>{{ version }}</dd>
  </dl>
  <p>
    {% if is_admin %}<a href="/users">Manage users</a> ·{% endif %}
    <a href="/auth/password">Change password</a>
  </p>
  <form method="post" action="/auth/logout"><button>Sign out</button></form>
</main>
"""

USERS_PAGE = STYLE + """
<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OAAP Users</title>
<style>
  main{max-width:52rem}
  table{width:100%;border-collapse:collapse;margin:1rem 0}
  th,td{text-align:left;padding:.5rem;border-bottom:1px solid #e2e8f0;vertical-align:top}
  td form{margin:0}
  .roles label{display:inline-block;margin-right:.6rem;font-size:.9rem}
  .roles input,.rowcheck input{width:auto;margin:0 .25rem 0 0}
  td input[type=text],td input[type=password]{margin:0;padding:.35rem}
  button.small{padding:.35rem .8rem;font-size:.9rem}
  .inactive td{color:#94a3b8}
  fieldset{border:1px solid #e2e8f0;border-radius:.5rem;margin:1.5rem 0;padding:1rem}
  legend{font-weight:600}
</style>
<main>
  <h1>Users</h1>
  <p><a href="/">← Portal</a></p>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  {% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
  <table>
    <tr><th>Username</th><th>Display name</th><th>Roles</th><th>Active</th><th></th><th>New password</th></tr>
    {% for u in users %}
    {# row-spanning forms are invalid HTML — inputs reference the form
       below the table via the form attribute instead #}
    <tr class="{{ '' if u.active else 'inactive' }}">
      <td>{{ u.username }}</td>
      <td><input type="text" name="display_name" value="{{ u.display_name }}"
                 form="upd-{{ u.username }}"></td>
      <td class="roles">
        {% for r in all_roles %}
        <label><input type="checkbox" name="roles" value="{{ r }}"
               form="upd-{{ u.username }}" {{ 'checked' if r in u.roles }}>{{ r }}</label>
        {% endfor %}
      </td>
      <td class="rowcheck"><label><input type="checkbox" name="active"
          form="upd-{{ u.username }}" {{ 'checked' if u.active }}>active</label></td>
      <td><button class="small" form="upd-{{ u.username }}">Save</button></td>
      <td>
        <form method="post" action="/users/{{ u.username }}/password">
          <input type="password" name="password" minlength="8" required
                 autocomplete="new-password" placeholder="min. 8 chars">
          <button class="small">Set</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% for u in users %}
  <form id="upd-{{ u.username }}" method="post" action="/users/{{ u.username }}/update"></form>
  {% endfor %}
  <fieldset>
    <legend>Create user</legend>
    <form method="post" action="/users/create">
      <label>Username <input type="text" name="username" required
             pattern="[a-z0-9][a-z0-9._-]{1,39}"
             title="lowercase letters/digits/._- (2–40 chars)"></label>
      <label>Display name <input type="text" name="display_name"></label>
      <label>Initial password <input type="password" name="password"
             minlength="8" required autocomplete="new-password"></label>
      <p class="roles">
        {% for r in all_roles %}
        <label><input type="checkbox" name="roles" value="{{ r }}"
               {{ 'checked' if r == 'user' }}>{{ r }}</label>
        {% endfor %}
      </p>
      <button>Create</button>
    </form>
  </fieldset>
  <p class="muted">Users cannot be deleted, only deactivated — apps may
  reference them in their records. Roles take effect on the user's next
  request.</p>
</main>
"""

SETUP_PAGE = STYLE + """
<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OAAP Setup</title>
<main>
  <h1>Welcome to OAAP</h1>
  {% if done %}
    <p class="ok">Setup is already completed.</p>
    <p><a href="/">Go to the portal</a> (you will be asked to sign in).</p>
  {% else %}
    <p class="muted">Create the first administrator. You need the setup
    token printed by the installer.</p>
    {% if error %}<p class="err">{{ error }}</p>{% endif %}
    <form method="post" action="/setup">
      <label>Setup token <input name="token" required></label>
      <label>Admin username <input name="username" required autocomplete="username"></label>
      <label>Password (min. 8 characters)
        <input name="password" type="password" minlength="8" required autocomplete="new-password"></label>
      <button>Create administrator</button>
    </form>
  {% endif %}
</main>
"""


def setup_done() -> bool:
    return requests.get(f"{IDENTITY}/internal/status", timeout=5).json()["setup_done"]


ALL_ROLES = ("admin", "keyuser", "user", "guest", "partner")


def caller_roles():
    """Verified roles from the gateway (forward auth, RFC-0002)."""
    return set(filter(None, request.headers.get("X-OAAP-Roles", "").split(",")))


@app.get("/")
def dashboard():
    roles = request.headers.get("X-OAAP-Roles", "")
    return render_template_string(
        DASHBOARD,
        user=request.headers.get("X-OAAP-User", "?"),
        roles=roles or "?",
        version=VERSION,
        is_admin="admin" in caller_roles(),
        tiles=launchpad_tiles(set(filter(None, roles.split(","))),
                              request.host.split(":")[0]),
    )


# ---------------------------------------------------------------------------
# User management (spec oaap.core.identity 2.4) — admin only. The
# gateway has already authenticated the caller; the portal checks the
# admin role and delegates the operations to identity's internal API.

def users_page(error=None, msg=None, status=200):
    users = requests.get(f"{IDENTITY}/internal/users", timeout=5).json()["users"]
    return render_template_string(
        USERS_PAGE, users=users, all_roles=ALL_ROLES, error=error, msg=msg
    ), status


def require_admin():
    if "admin" not in caller_roles():
        return "Forbidden: user management needs the admin role", 403
    return None


@app.get("/users")
def users_list():
    return require_admin() or users_page()


@app.post("/users/create")
def users_create():
    denied = require_admin()
    if denied:
        return denied
    resp = requests.post(f"{IDENTITY}/internal/users", json={
        "username": request.form.get("username", "").strip(),
        "display_name": request.form.get("display_name", ""),
        "password": request.form.get("password", ""),
        "roles": request.form.getlist("roles"),
    }, timeout=5)
    if resp.status_code == 201:
        return users_page(msg=f"User '{request.form.get('username', '').strip()}' created.")
    return users_page(error=resp.json().get("error", "Create failed."), status=resp.status_code)


@app.post("/users/<username>/update")
def users_update(username):
    denied = require_admin()
    if denied:
        return denied
    resp = requests.put(f"{IDENTITY}/internal/users/{username}", json={
        "display_name": request.form.get("display_name", ""),
        "roles": request.form.getlist("roles"),
        "active": request.form.get("active") == "on",
    }, timeout=5)
    if resp.status_code == 200:
        return users_page(msg=f"User '{username}' updated.")
    return users_page(error=resp.json().get("error", "Update failed."), status=resp.status_code)


@app.post("/users/<username>/password")
def users_password(username):
    denied = require_admin()
    if denied:
        return denied
    resp = requests.post(f"{IDENTITY}/internal/users/{username}/password", json={
        "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 200:
        return users_page(msg=f"Password for '{username}' set.")
    return users_page(error=resp.json().get("error", "Password change failed."), status=resp.status_code)


@app.get("/setup")
def setup_form():
    return render_template_string(SETUP_PAGE, done=setup_done(), error=None)


@app.post("/setup")
def setup_submit():
    if setup_done():
        return render_template_string(SETUP_PAGE, done=True, error=None)
    resp = requests.post(f"{IDENTITY}/internal/setup", json={
        "token": request.form.get("token", "").strip(),
        "username": request.form.get("username", ""),
        "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 201:
        return redirect("/auth/login", code=303)
    error = resp.json().get("error", f"Setup failed (HTTP {resp.status_code}).")
    return render_template_string(SETUP_PAGE, done=False, error=error), resp.status_code
