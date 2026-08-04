"""OAAP web portal (oaap.core.portal, skeleton).

Serves the first-run wizard (/setup, protected by the one-time setup
token, validated by the identity service), the role-filtered launchpad,
user management (admin only, list report + object page floorplans),
and the platform health page (admin/partner). Authentication is
entirely the gateway's job: the portal trusts the X-OAAP-User /
X-OAAP-Roles headers set after forward auth.

Look & feel follows oaap-design/docs/design-guidelines.md v0.1 —
blue palette, hexagon mark, German UI, floorplans, no external
resources.
"""

import json
import os
from urllib.parse import quote

import requests
from flask import Flask, redirect, render_template_string, request
from markupsafe import Markup

IDENTITY = "http://identity:8000"
GATEWAY = "http://gateway:80"
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
    --ok:#15803d; --err:#b91c1c; --warn:#b45309;
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
  h2{font-size:1.02rem;margin:0 0 .8rem}
  .pagehead{display:flex;align-items:center;justify-content:space-between;gap:1rem;
       flex-wrap:wrap;margin-bottom:1rem}
  .pagehead h1{margin:0}
  .back{display:inline-block;margin-bottom:.8rem;color:var(--oaap-blue-600);text-decoration:none}
  .back:hover{text-decoration:underline}
  .card{background:var(--oaap-surface);border:1px solid var(--oaap-border);
       border-radius:.6rem;padding:1.4rem;box-shadow:0 1px 3px rgba(23,37,84,.06);
       margin-bottom:1.2rem}
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
       background:var(--oaap-blue-100);color:var(--oaap-blue-900);white-space:nowrap}
  .badge.test{background:#fef3c7;color:#92400e}
  .badge.off{background:#f3f4f6;color:#6b7280}
  .dot{display:inline-block;width:.65rem;height:.65rem;border-radius:50%;margin-right:.45rem}
  .dot.ok{background:var(--ok)} .dot.err{background:var(--err)}
  .dot.warn{background:var(--warn)} .dot.unknown{background:#9ca3af}
  a.btn{display:inline-block;padding:.6rem 1.3rem;border-radius:.4rem;
       background:var(--oaap-blue-600);color:#fff;text-decoration:none;min-height:44px}
  a.btn:hover{background:var(--oaap-blue-700)}
  input,select{width:100%;padding:.55rem;margin:.25rem 0 1rem;
       border:1px solid var(--oaap-border);border-radius:.4rem;font-size:.95rem}
  button{padding:.6rem 1.3rem;border:0;border-radius:.4rem;background:var(--oaap-blue-600);
       color:#fff;font-size:.95rem;cursor:pointer;min-height:44px}
  button:hover{background:var(--oaap-blue-700)}
  .err{color:var(--err)}.ok{color:var(--ok)}.muted{color:var(--oaap-muted);font-size:.9rem}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:.6rem .5rem;border-bottom:1px solid var(--oaap-border);vertical-align:middle}
  th{font-size:.82rem;text-transform:uppercase;letter-spacing:.04em;color:var(--oaap-muted)}
  tr.rowlink:hover td{background:#f8fafc}
  td a.rowaction{color:var(--oaap-blue-600);text-decoration:none;white-space:nowrap}
  td a.rowaction:hover{text-decoration:underline}
  .roles label{display:inline-block;margin-right:.8rem;font-size:.95rem;white-space:nowrap}
  .roles input,.checkline input{width:auto;margin:0 .3rem 0 0}
  .checkline{display:block;margin:.3rem 0 1rem}
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
    {% if can_health %}<a href="/health" class="{{ 'active' if active == 'health' }}">Gesundheit</a>{% endif %}
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

# Floorplan "Listenbericht" (design guidelines 6.1): read-only list,
# one global action, one row action (navigate to the object page).
USERS_LIST_BODY = """
<div class="pagehead">
  <h1>Benutzer</h1>
  <a class="btn" href="/users/new">Benutzer anlegen</a>
</div>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
<div class="card" style="overflow-x:auto;padding:.4rem 1.4rem">
<table>
  <tr><th>Benutzername</th><th>Anzeigename</th><th>Rollen</th><th>Status</th><th></th></tr>
  {% for u in users %}
  <tr class="rowlink">
    <td><a class="rowaction" href="/users/{{ u.username }}">{{ u.username }}</a></td>
    <td>{{ u.display_name }}</td>
    <td>{{ u.roles|join(", ") }}</td>
    <td><span class="badge {{ '' if u.active else 'off' }}">{{ 'aktiv' if u.active else 'inaktiv' }}</span></td>
    <td><a class="rowaction" href="/users/{{ u.username }}">Bearbeiten</a></td>
  </tr>
  {% endfor %}
</table>
</div>
<p class="muted">Benutzer werden nicht gelöscht, sondern deaktiviert —
Apps können sie in ihren Daten referenzieren.</p>
"""

# Floorplan "Objektseite" (design guidelines 6.2).
USER_EDIT_BODY = """
<a class="back" href="/users">← Zurück zur Liste</a>
<div class="pagehead">
  <h1>{{ u.username }}{% if u.display_name %} <span class="muted">({{ u.display_name }})</span>{% endif %}</h1>
  <span class="badge {{ '' if u.active else 'off' }}">{{ 'aktiv' if u.active else 'inaktiv' }}</span>
</div>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
<form method="post" action="/users/{{ u.username }}/update">
  <div class="card">
    <h2>Stammdaten</h2>
    <label>Anzeigename <input type="text" name="display_name" value="{{ u.display_name }}"></label>
  </div>
  <div class="card">
    <h2>Rollen</h2>
    <p class="roles">
      {% for r in all_roles %}
      <label><input type="checkbox" name="roles" value="{{ r }}"
             {{ 'checked' if r in u.roles }}>{{ r }}</label>
      {% endfor %}
    </p>
    <p class="muted">Rollenänderungen wirken ab der nächsten Anfrage des Benutzers.</p>
  </div>
  <div class="card">
    <h2>Status</h2>
    <label class="checkline"><input type="checkbox" name="active"
        {{ 'checked' if u.active }}>Benutzer ist aktiv (abwählen deaktiviert die Anmeldung sofort)</label>
    <button>Speichern</button>
  </div>
</form>
<div class="card">
  <h2>Passwort setzen</h2>
  <form method="post" action="/users/{{ u.username }}/password">
    <label>Neues Passwort (mind. 8 Zeichen)
      <input type="password" name="password" minlength="8" required
             autocomplete="new-password"></label>
    <button>Passwort setzen</button>
  </form>
</div>
"""

USER_NEW_BODY = """
<a class="back" href="/users">← Zurück zur Liste</a>
<h1>Benutzer anlegen</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<form method="post" action="/users/create">
  <div class="card">
    <h2>Stammdaten</h2>
    <label>Benutzername <input type="text" name="username" required
           value="{{ form.username }}" pattern="[a-z0-9][a-z0-9._-]{1,39}"
           title="Kleinbuchstaben/Ziffern/._- (2–40 Zeichen)"></label>
    <label>Anzeigename <input type="text" name="display_name" value="{{ form.display_name }}"></label>
    <label>Startpasswort (mind. 8 Zeichen) <input type="password" name="password"
           minlength="8" required autocomplete="new-password"></label>
  </div>
  <div class="card">
    <h2>Rollen</h2>
    <p class="roles">
      {% for r in all_roles %}
      <label><input type="checkbox" name="roles" value="{{ r }}"
             {{ 'checked' if r in form.roles }}>{{ r }}</label>
      {% endfor %}
    </p>
    <button>Anlegen</button>
  </div>
</form>
"""

HEALTH_BODY = """
<h1>Gesundheit</h1>
<div class="card">
  <h2>Knoten</h2>
  <table>
    <tr><th>Wert</th><th>Status</th><th>Details</th></tr>
    {% for n in node %}
    <tr><td>{{ n.name }}</td>
        <td><span class="dot {{ n.state }}"></span>{{ n.label }}</td>
        <td class="muted">{{ n.detail }}</td></tr>
    {% endfor %}
  </table>
</div>
<div class="card">
  <h2>Kernservices</h2>
  <table>
    <tr><th>Service</th><th>Status</th><th>Details</th></tr>
    {% for s in core %}
    <tr><td>{{ s.name }}</td>
        <td><span class="dot {{ s.state }}"></span>{{ s.label }}</td>
        <td class="muted">{{ s.detail }}</td></tr>
    {% endfor %}
  </table>
</div>
<div class="card">
  <h2>Apps</h2>
  {% if apps %}
  <table>
    <tr><th>App</th><th>Instanz</th><th>Kanal</th><th>Status</th><th>Details</th></tr>
    {% for a in apps %}
    <tr><td>{{ a.name }} <span class="muted">v{{ a.version }}</span></td>
        <td>{{ a.instance }}</td>
        <td><span class="badge {{ a.channel }}">{{ a.channel_label }}</span></td>
        <td><span class="dot {{ a.state }}"></span>{{ a.label }}</td>
        <td class="muted">{{ a.detail }}</td></tr>
    {% endfor %}
  </table>
  {% else %}<p class="muted">Keine Apps installiert.</p>{% endif %}
</div>
<p class="muted">Geprüft wird aus Sicht des Portals über das interne
Netz. Knoten-Werte (Festplatte, Updates) folgen mit der
Betriebs-Capability.</p>
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
    caller = caller_roles()
    return render_template_string(
        LAYOUT,
        title=title, active=active, body=Markup(body), logo=LOGO_SVG,
        user=request.headers.get("X-OAAP-User", "?"), roles=roles or "?",
        is_admin="admin" in caller, can_health=bool(caller & {"admin", "partner"}),
        version=VERSION,
    ), status


def load_instances():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f).get("instances", {})
    except (OSError, ValueError):
        return {}


def launchpad_tiles(user_roles, host):
    """Role-filtered app tiles from the instance registry (spec 2.5).

    The filter is UX only — the gateway enforces the roles on every
    request regardless of what the portal shows.
    """
    tiles = []
    for name, inst in sorted(load_instances().items()):
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
    return page(
        DASHBOARD_BODY, "Apps", "apps",
        tiles=launchpad_tiles(caller_roles(), request.host.split(":")[0]),
    )


# ---------------------------------------------------------------------------
# User management (spec oaap.core.identity 2.4) — admin only, floorplans
# "Listenbericht" and "Objektseite". The gateway has already
# authenticated the caller; the portal checks the admin role and
# delegates the operations to identity's internal API.

def require_admin():
    if "admin" not in caller_roles():
        return "Zugriff verweigert: Benutzerverwaltung erfordert die Rolle admin.", 403
    return None


def identity_users():
    return requests.get(f"{IDENTITY}/internal/users", timeout=5).json()["users"]


@app.get("/users")
def users_list():
    denied = require_admin()
    if denied:
        return denied
    return page(USERS_LIST_BODY, "Benutzer", "users", users=identity_users(),
                msg=request.args.get("msg"), error=request.args.get("err"))


@app.get("/users/new")
def users_new():
    denied = require_admin()
    if denied:
        return denied
    return page(USER_NEW_BODY, "Benutzer anlegen", "users",
                all_roles=ALL_ROLES, error=None,
                form={"username": "", "display_name": "", "roles": ["user"]})


@app.post("/users/create")
def users_create():
    denied = require_admin()
    if denied:
        return denied
    form = {
        "username": request.form.get("username", "").strip(),
        "display_name": request.form.get("display_name", ""),
        "roles": request.form.getlist("roles"),
    }
    resp = requests.post(f"{IDENTITY}/internal/users", json={
        **form, "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 201:
        created = quote("Benutzer " + form["username"] + " wurde angelegt.")
        return redirect(f"/users?msg={created}", code=303)
    # Validation error: stay on the page, keep the inputs (guidelines 6.2)
    return page(USER_NEW_BODY, "Benutzer anlegen", "users", status=resp.status_code,
                all_roles=ALL_ROLES, form=form,
                error=resp.json().get("error", "Anlegen fehlgeschlagen."))


@app.get("/users/<username>")
def users_detail(username):
    denied = require_admin()
    if denied:
        return denied
    u = next((x for x in identity_users() if x["username"] == username), None)
    if not u:
        return redirect(f"/users?err={quote('Benutzer nicht gefunden.')}", code=303)
    return page(USER_EDIT_BODY, f"Benutzer {username}", "users", u=u,
                all_roles=ALL_ROLES, msg=request.args.get("msg"),
                error=request.args.get("err"))


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
        return redirect(f"/users/{username}?msg={quote('Gespeichert.')}", code=303)
    return redirect(f"/users/{username}?err={quote(resp.json().get('error', 'Speichern fehlgeschlagen.'))}", code=303)


@app.post("/users/<username>/password")
def users_password(username):
    denied = require_admin()
    if denied:
        return denied
    resp = requests.post(f"{IDENTITY}/internal/users/{username}/password", json={
        "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 200:
        return redirect(f"/users/{username}?msg={quote('Passwort wurde gesetzt.')}", code=303)
    return redirect(f"/users/{username}?err={quote(resp.json().get('error', 'Passwort setzen fehlgeschlagen.'))}", code=303)


# ---------------------------------------------------------------------------
# Health (design guidelines: visible for admin and partner) — checked
# live from the portal over the internal container network.

def _gb(n_bytes):
    return f"{n_bytes / 1024**3:.1f}".replace(".", ",") + " GB"


def node_values():
    """Host-level readings, visible from inside the container.

    Disk: statvfs on the registry mount — it lives on the platform's
    data filesystem. Memory/load/uptime: /proc is not namespaced for
    these readings, so they are host values.
    """
    rows = []
    rows.append({"name": "Plattformversion", "state": "ok", "label": VERSION,
                 "detail": ""})
    try:
        up = float(open("/proc/uptime").read().split()[0])
        d, rest = divmod(int(up), 86400)
        h, rest = divmod(rest, 3600)
        rows.append({"name": "Betriebszeit", "state": "ok",
                     "label": f"{d} Tage, {h} Std., {rest // 60} Min.", "detail": ""})
    except OSError:
        pass
    try:
        l1, l5, l15 = open("/proc/loadavg").read().split()[:3]
        rows.append({"name": "CPU-Last", "state": "ok",
                     "label": f"{l1} / {l5} / {l15}",
                     "detail": "Durchschnitt über 1 / 5 / 15 Minuten"})
    except OSError:
        pass
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.strip().split()[0]) * 1024
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        if total:
            state = "warn" if avail < total * 0.1 else "ok"
            rows.append({"name": "Arbeitsspeicher", "state": state,
                         "label": f"{_gb(avail)} von {_gb(total)} verfügbar",
                         "detail": "wird knapp" if state == "warn" else ""})
    except OSError:
        pass
    try:
        vs = os.statvfs("/apps-registry")
        free, total = vs.f_frsize * vs.f_bavail, vs.f_frsize * vs.f_blocks
        state = "warn" if free < 2 * 1024**3 else "ok"
        rows.append({"name": "Datenträger", "state": state,
                     "label": f"{_gb(free)} von {_gb(total)} frei",
                     "detail": "Dateisystem des Plattform-Datenverzeichnisses"
                               + ("; unter 2 GB frei" if state == "warn" else "")})
    except OSError:
        pass
    return rows


def _probe(url, ok_status=200):
    try:
        r = requests.get(url, timeout=2, allow_redirects=False)
    except requests.RequestException as e:
        return "err", "Nicht erreichbar", type(e).__name__
    if r.status_code == ok_status:
        return "ok", "Gesund", f"HTTP {r.status_code}"
    return "warn", "Antwortet unerwartet", f"HTTP {r.status_code}"


@app.get("/health")
def health():
    if not caller_roles() & {"admin", "partner"}:
        return "Zugriff verweigert: Gesundheit erfordert die Rolle admin oder partner.", 403

    core = []
    state, label, detail = _probe(f"{IDENTITY}/internal/status")
    core.append({"name": "Identity", "state": state, "label": label, "detail": detail})
    # Full chain: gateway proxies the login page to identity.
    state, label, detail = _probe(f"{GATEWAY}/auth/login")
    core.append({"name": "Gateway", "state": state, "label": label, "detail": detail})
    core.append({"name": "Portal", "state": "ok", "label": "Gesund",
                 "detail": "liefert diese Seite"})

    apps = []
    for name, inst in sorted(load_instances().items()):
        container, svc_port = inst.get("container"), inst.get("svc_port")
        health_path = inst.get("health_path")
        if container and svc_port and health_path:
            state, label, detail = _probe(f"http://{container}:{svc_port}{health_path}")
        elif container and svc_port:
            state, label, detail = _probe(f"http://{container}:{svc_port}/")
            if state == "warn":  # any HTTP answer counts as reachable here
                state, label = "ok", "Erreichbar"
            if state == "ok":
                detail += ", App ohne erfassten Healthcheck"
        else:
            state, label = "unknown", "Unbekannt"
            detail = "vor der Gesundheits-Erfassung installiert — bei erneutem 'oaap app install' verfügbar"
        channel = inst.get("channel", "production")
        apps.append({
            "name": inst.get("app_name", name), "instance": name,
            "version": inst.get("version", "?"), "channel": channel,
            "channel_label": CHANNEL_LABELS.get(channel, channel),
            "state": state, "label": label, "detail": detail,
        })
    return page(HEALTH_BODY, "Gesundheit", "health", node=node_values(),
                core=core, apps=apps)


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
