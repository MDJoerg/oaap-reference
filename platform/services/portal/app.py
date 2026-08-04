"""OAAP web portal (oaap.core.portal, skeleton).

Serves the first-run wizard (/setup, protected by the one-time setup
token, validated by the identity service), the role-filtered launchpad,
and the user management UI (admin only). Authentication is entirely
the gateway's job: the portal trusts the X-OAAP-User / X-OAAP-Roles
headers set after forward auth.

Look & feel follows oaap-design/docs/design-guidelines.md v0.1 —
blue palette, hexagon mark, German UI, no external resources.
"""

import json
import os

import requests
from flask import Flask, redirect, render_template_string, request
from markupsafe import Markup

IDENTITY = "http://identity:8000"
VERSION = os.environ.get("OAAP_VERSION", "unknown")
REGISTRY = "/apps-registry/registry.json"

ALL_ROLES = ("admin", "keyuser", "user", "guest", "partner")
CHANNEL_LABELS = {"test": "Test", "production": "Produktiv"}

# Hexagon mark per design guidelines (assets/logo.svg, white for the
# dark header) — inline, because OAAP UIs load nothing from outside.
LOGO_SVG = Markup(
    '<svg viewBox="0 0 100 100" width="34" height="34" aria-hidden="true">'
    '<polygon points="50,4 90,27 90,73 50,96 10,73 10,27" fill="none" '
    'stroke="#ffffff" stroke-width="6" stroke-linejoin="round"/>'
    '<polygon points="50,28 69,39 69,61 50,72 31,61 31,39" fill="#ffffff"/></svg>'
)
FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Cpolygon points='50,4 90,27 90,73 50,96 10,73 10,27' fill='%232563eb'/%3E%3C/svg%3E"
)

STYLE = """
<style>
  :root{
    --oaap-blue-950:#172554; --oaap-blue-900:#1e3a8a; --oaap-blue-700:#1d4ed8;
    --oaap-blue-600:#2563eb; --oaap-blue-100:#dbeafe;
    --oaap-bg:#f4f6fa; --oaap-surface:#fff; --oaap-text:#1f2937;
    --oaap-muted:#6b7280; --oaap-border:#e5e7eb;
    --ok:#15803d; --err:#b91c1c;
  }
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       margin:0;background:var(--oaap-bg);color:var(--oaap-text)}
  header.oaap{background:linear-gradient(135deg,var(--oaap-blue-900),var(--oaap-blue-950));
       color:#fff;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;
       padding:.6rem 1.2rem}
  .brand{display:flex;align-items:center;gap:.6rem;text-decoration:none;color:#fff}
  .brand b{font-size:1.15rem;letter-spacing:.08em}
  .brand small{display:block;font-size:.62rem;opacity:.75;letter-spacing:.02em}
  nav.main{display:flex;gap:.25rem;margin-left:1rem;flex:1}
  nav.main a{color:#fff;text-decoration:none;padding:.55rem .9rem;border-radius:.4rem;
       opacity:.85;border-bottom:3px solid transparent}
  nav.main a:hover{background:rgba(255,255,255,.12);opacity:1}
  nav.main a.active{border-bottom-color:var(--oaap-blue-100);opacity:1;font-weight:600}
  .userbox{display:flex;align-items:center;gap:.7rem;font-size:.9rem}
  .userbox .who{text-align:right;line-height:1.2}
  .userbox .who small{opacity:.75}
  .userbox a{color:var(--oaap-blue-100)}
  .userbox form{margin:0}
  .userbox button{background:rgba(255,255,255,.14);color:#fff;border:1px solid rgba(255,255,255,.35);
       border-radius:.4rem;padding:.45rem .9rem;font-size:.85rem;cursor:pointer}
  .userbox button:hover{background:rgba(255,255,255,.25)}
  main{max-width:62rem;margin:1.6rem auto;padding:0 1.2rem}
  h1{font-size:1.35rem;margin:.2rem 0 1rem}
  h2{font-size:1.05rem}
  .card{background:var(--oaap-surface);border:1px solid var(--oaap-border);
       border-radius:.6rem;padding:1.4rem;box-shadow:0 1px 3px rgba(23,37,84,.06)}
  .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr));gap:1rem;margin:1rem 0}
  .tile{display:block;background:var(--oaap-surface);border:1px solid var(--oaap-border);
       border-radius:.6rem;padding:1rem 1.1rem;text-decoration:none;color:inherit;
       box-shadow:0 1px 3px rgba(23,37,84,.06);min-height:44px}
  .tile:hover{border-color:var(--oaap-blue-600);box-shadow:0 2px 8px rgba(37,99,235,.18)}
  .tile .top{display:flex;align-items:center;gap:.55rem}
  .tile h3{margin:0;font-size:1.02rem;flex:1}
  .tile p{color:var(--oaap-muted);font-size:.9rem;margin:.5rem 0}
  .tile .meta{color:var(--oaap-muted);font-size:.8rem}
  .hexdot{flex:none}
  .badge{font-size:.72rem;padding:.15rem .55rem;border-radius:1rem;
       background:var(--oaap-blue-100);color:var(--oaap-blue-900)}
  .badge.test{background:#fef3c7;color:#92400e}
  input,select{width:100%;padding:.55rem;margin:.25rem 0 1rem;
       border:1px solid var(--oaap-border);border-radius:.4rem;font-size:.95rem}
  button{padding:.6rem 1.3rem;border:0;border-radius:.4rem;background:var(--oaap-blue-600);
       color:#fff;font-size:.95rem;cursor:pointer;min-height:44px}
  button:hover{background:var(--oaap-blue-700)}
  button.small{padding:.4rem .9rem;font-size:.85rem;min-height:36px}
  .err{color:var(--err)}.ok{color:var(--ok)}.muted{color:var(--oaap-muted);font-size:.9rem}
  table{width:100%;border-collapse:collapse;margin:1rem 0}
  th,td{text-align:left;padding:.55rem .5rem;border-bottom:1px solid var(--oaap-border);vertical-align:top}
  th{font-size:.82rem;text-transform:uppercase;letter-spacing:.04em;color:var(--oaap-muted)}
  td form{margin:0}
  td input{margin:0;padding:.4rem}
  .roles label{display:inline-block;margin-right:.6rem;font-size:.9rem;white-space:nowrap}
  .roles input,.rowcheck input{width:auto;margin:0 .25rem 0 0}
  .inactive td{color:#9ca3af}
  fieldset{border:1px solid var(--oaap-border);border-radius:.6rem;margin:1.5rem 0;padding:1.2rem;
       background:var(--oaap-surface)}
  legend{font-weight:600;padding:0 .4rem}
  footer.oaap{max-width:62rem;margin:2rem auto 1.2rem;padding:0 1.2rem;
       color:var(--oaap-muted);font-size:.8rem;display:flex;gap:.5rem;align-items:center}
  @media (max-width:640px){
    nav.main{order:3;flex-basis:100%;margin-left:0}
    .userbox .who{display:none}
  }
</style>
"""

LAYOUT = STYLE + """
<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href=\"""" + FAVICON + """\">
<title>{{ title }} — OAAP</title>
<header class="oaap">
  <a class="brand" href="/">{{ logo }}
    <span><b>OAAP</b><small>Open Application &amp; Automation Platform</small></span>
  </a>
  <nav class="main">
    <a href="/" class="{{ 'active' if active == 'apps' }}">Apps</a>
    {% if is_admin %}<a href="/users" class="{{ 'active' if active == 'users' }}">Benutzer</a>{% endif %}
  </nav>
  <div class="userbox">
    <span class="who">{{ user }}<br><small>{{ roles }}</small></span>
    <a href="/auth/password" title="Passwort ändern">Passwort</a>
    <form method="post" action="/auth/logout"><button>Abmelden</button></form>
  </div>
</header>
<main>{{ body }}</main>
<footer class="oaap">
  <svg viewBox="0 0 100 100" width="14" height="14" aria-hidden="true">
    <polygon points="50,4 90,27 90,73 50,96 10,73 10,27" fill="#2563eb"/></svg>
  OAAP-Plattform {{ version }}
</footer>
</html>
"""

DASHBOARD_BODY = """
<h1>Apps</h1>
{% if tiles %}
  <div class="tiles">
    {% for t in tiles %}
    <a class="tile" href="{{ t.url }}">
      <div class="top">
        <svg class="hexdot" viewBox="0 0 100 100" width="20" height="20" aria-hidden="true">
          <polygon points="50,4 90,27 90,73 50,96 10,73 10,27" fill="none"
                   stroke="#2563eb" stroke-width="8" stroke-linejoin="round"/></svg>
        <h3>{{ t.name }}</h3>
        <span class="badge {{ t.channel }}">{{ t.channel_label }}</span>
      </div>
      {% if t.description %}<p>{{ t.description }}</p>{% endif %}
      <span class="meta">Version {{ t.version }}</span>
    </a>
    {% endfor %}
  </div>
{% else %}
  <div class="card"><p class="muted">Noch keine Apps installiert — oder für
  Ihre Rollen ist keine sichtbar. Apps installiert die Administration mit
  <code>oaap app install</code>.</p></div>
{% endif %}
"""

USERS_BODY = """
<h1>Benutzer</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
<div class="card" style="overflow-x:auto">
<table>
  <tr><th>Benutzername</th><th>Anzeigename</th><th>Rollen</th><th>Aktiv</th><th></th><th>Neues Passwort</th></tr>
  {% for u in users %}
  {# Zeilenübergreifende Formulare sind ungültiges HTML — die Felder
     referenzieren das Formular unter der Tabelle via form-Attribut #}
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
        form="upd-{{ u.username }}" {{ 'checked' if u.active }}>aktiv</label></td>
    <td><button class="small" form="upd-{{ u.username }}">Speichern</button></td>
    <td>
      <form method="post" action="/users/{{ u.username }}/password">
        <input type="password" name="password" minlength="8" required
               autocomplete="new-password" placeholder="mind. 8 Zeichen">
        <button class="small">Setzen</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
</div>
{% for u in users %}
<form id="upd-{{ u.username }}" method="post" action="/users/{{ u.username }}/update"></form>
{% endfor %}
<fieldset>
  <legend>Benutzer anlegen</legend>
  <form method="post" action="/users/create">
    <label>Benutzername <input type="text" name="username" required
           pattern="[a-z0-9][a-z0-9._-]{1,39}"
           title="Kleinbuchstaben/Ziffern/._- (2–40 Zeichen)"></label>
    <label>Anzeigename <input type="text" name="display_name"></label>
    <label>Startpasswort <input type="password" name="password"
           minlength="8" required autocomplete="new-password"></label>
    <p class="roles">
      {% for r in all_roles %}
      <label><input type="checkbox" name="roles" value="{{ r }}"
             {{ 'checked' if r == 'user' }}>{{ r }}</label>
      {% endfor %}
    </p>
    <button>Anlegen</button>
  </form>
</fieldset>
<p class="muted">Benutzer werden nicht gelöscht, sondern deaktiviert —
Apps können sie in ihren Daten referenzieren. Rollenänderungen wirken ab
der nächsten Anfrage des Benutzers.</p>
"""

SETUP_PAGE = STYLE + """
<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href=\"""" + FAVICON + """\">
<title>Einrichtung — OAAP</title>
<body style="display:grid;place-items:center;min-height:100vh">
<main style="width:min(26rem,92vw);margin:0">
  <div class="card">
  <p style="text-align:center">
    <svg viewBox="0 0 100 100" width="52" height="52" aria-hidden="true">
      <polygon points="50,4 90,27 90,73 50,96 10,73 10,27" fill="none"
               stroke="#2563eb" stroke-width="6" stroke-linejoin="round"/>
      <polygon points="50,28 69,39 69,61 50,72 31,61 31,39" fill="#2563eb"/></svg>
  </p>
  <h1 style="text-align:center">Willkommen bei OAAP</h1>
  {% if done %}
    <p class="ok">Die Einrichtung ist bereits abgeschlossen.</p>
    <p><a href="/">Zum Portal</a> (Anmeldung erforderlich).</p>
  {% else %}
    <p class="muted">Legen Sie das erste Administrator-Konto an. Dazu
    brauchen Sie das Setup-Token aus der Installationsausgabe.</p>
    {% if error %}<p class="err">{{ error }}</p>{% endif %}
    <form method="post" action="/setup">
      <label>Setup-Token <input name="token" required></label>
      <label>Admin-Benutzername <input name="username" required autocomplete="username"></label>
      <label>Passwort (mind. 8 Zeichen)
        <input name="password" type="password" minlength="8" required autocomplete="new-password"></label>
      <button style="width:100%">Administrator anlegen</button>
    </form>
  {% endif %}
  </div>
</main></body></html>
"""

app = Flask(__name__)


def caller_roles():
    """Verified roles from the gateway (forward auth, RFC-0002)."""
    return set(filter(None, request.headers.get("X-OAAP-Roles", "").split(",")))


def page(body_template, title, active, status=200, **ctx):
    body = render_template_string(body_template, **ctx)
    roles = request.headers.get("X-OAAP-Roles", "")
    return render_template_string(
        LAYOUT,
        title=title, active=active, body=Markup(body), logo=LOGO_SVG,
        user=request.headers.get("X-OAAP-User", "?"), roles=roles or "?",
        is_admin="admin" in caller_roles(), version=VERSION,
    ), status


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
        channel = inst.get("channel", "production")
        tiles.append({
            "name": inst.get("app_name", name),
            "instance": name,
            "version": inst.get("version", "?"),
            "channel": channel,
            "channel_label": CHANNEL_LABELS.get(channel, channel),
            "description": inst.get("description", ""),
            "url": f"http://{host}:{inst['port']}/",
        })
    return tiles


def setup_done() -> bool:
    return requests.get(f"{IDENTITY}/internal/status", timeout=5).json()["setup_done"]


@app.get("/")
def dashboard():
    roles = set(filter(None, request.headers.get("X-OAAP-Roles", "").split(",")))
    return page(
        DASHBOARD_BODY, "Apps", "apps",
        tiles=launchpad_tiles(roles, request.host.split(":")[0]),
    )


# ---------------------------------------------------------------------------
# User management (spec oaap.core.identity 2.4) — admin only. The
# gateway has already authenticated the caller; the portal checks the
# admin role and delegates the operations to identity's internal API.

def users_page(error=None, msg=None, status=200):
    users = requests.get(f"{IDENTITY}/internal/users", timeout=5).json()["users"]
    return page(USERS_BODY, "Benutzer", "users", status=status,
                users=users, all_roles=ALL_ROLES, error=error, msg=msg)


def require_admin():
    if "admin" not in caller_roles():
        return "Zugriff verweigert: Benutzerverwaltung erfordert die Rolle admin.", 403
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
        return users_page(msg=f"Benutzer '{request.form.get('username', '').strip()}' angelegt.")
    return users_page(error=resp.json().get("error", "Anlegen fehlgeschlagen."), status=resp.status_code)


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
        return users_page(msg=f"Benutzer '{username}' aktualisiert.")
    return users_page(error=resp.json().get("error", "Aktualisieren fehlgeschlagen."), status=resp.status_code)


@app.post("/users/<username>/password")
def users_password(username):
    denied = require_admin()
    if denied:
        return denied
    resp = requests.post(f"{IDENTITY}/internal/users/{username}/password", json={
        "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 200:
        return users_page(msg=f"Passwort für '{username}' gesetzt.")
    return users_page(error=resp.json().get("error", "Passwort setzen fehlgeschlagen."), status=resp.status_code)


# ---------------------------------------------------------------------------
# First-run wizard

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
    error = resp.json().get("error", f"Einrichtung fehlgeschlagen (HTTP {resp.status_code}).")
    return render_template_string(SETUP_PAGE, done=False, error=error), resp.status_code
