"""OAAP web portal (oaap.core.portal, skeleton).

Serves the first-run wizard (/setup, protected by the one-time setup
token, validated by the identity service) and a minimal dashboard.
Authentication is entirely the gateway's job: the portal trusts the
X-OAAP-User / X-OAAP-Roles headers set after forward auth.
"""

import os

import requests
from flask import Flask, redirect, render_template_string, request

IDENTITY = "http://identity:8000"
VERSION = os.environ.get("OAAP_VERSION", "unknown")

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
<main>
  <h1>OAAP Portal</h1>
  <p class="muted">Walking skeleton — administration features arrive with
  the full <code>oaap.core.portal</code> specification.</p>
  <dl>
    <dt>Signed in as</dt><dd>{{ user }}</dd>
    <dt>Roles</dt><dd>{{ roles }}</dd>
    <dt>Platform version</dt><dd>{{ version }}</dd>
  </dl>
  <form method="post" action="/auth/logout"><button>Sign out</button></form>
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


@app.get("/")
def dashboard():
    return render_template_string(
        DASHBOARD,
        user=request.headers.get("X-OAAP-User", "?"),
        roles=request.headers.get("X-OAAP-Roles", "?"),
        version=VERSION,
    )


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
