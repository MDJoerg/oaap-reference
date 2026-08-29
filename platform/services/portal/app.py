"""OAAP web portal (oaap.core.portal, skeleton).

Serves the first-run wizard (/setup, protected by the one-time setup
token, validated by the identity service), the role-and-group-filtered
launchpad, user management (server_admin only, list report + object
page floorplans), app-instance visibility (RFC-0007) and configuration
(both server_admin only) and the platform health page
(server_admin/partner).
Authentication is entirely the gateway's job: the portal trusts the
X-OAAP-User / X-OAAP-Roles headers set after forward auth.

Look & feel follows oaap-design/docs/design-guidelines.md v0.1 —
blue palette, hexagon mark, German UI, floorplans, no external
resources.
"""

import json
import os
from datetime import date, datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, g, redirect, render_template_string, request
from markupsafe import Markup

import fleet_view

IDENTITY = "http://identity:8000"
GATEWAY = "http://gateway:80"
# Internal health-probe listener on the gateway (RFC-0016): the portal
# cannot reach isolated app containers directly, so it asks the gateway
# — the only core service on every app network — to probe them. Only
# reachable container-to-container on the platform network.
GATEWAY_HEALTH = "http://gateway:8099"

# Every call to identity's /internal/* API carries this key (RFC-0015
# addendum A4). Reaching identity over the container network is no longer
# proof of anything — every app instance sits on that same network — so
# the portal now proves it is the portal. The key is only in the env of
# these two containers; app instances get their own env file and never
# see it. Identity refuses /internal/* without it.
INTERNAL = requests.Session()
INTERNAL.headers[
    "X-OAAP-Internal-Key"] = os.environ.get("INTERNAL_API_KEY", "")
VERSION = os.environ.get("OAAP_VERSION", "unknown")
REGISTRY = "/apps-registry/registry.json"

ALL_ROLES = ("server_admin", "tenant_admin", "admin", "keyuser", "user",
             "guest", "partner")
# Roles whose authority reaches past a tenant: server_admin administers
# the node, and partner sees the health page — which lists every
# instance on the machine. A tenant_admin may hand out neither, or the
# boundary has a second door (oaap.core.tenant 2.3 rule 1). Kept in
# step with the same list in identity, which does the refusing.
NODE_WIDE_ROLES = ("server_admin", "partner")
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
  /* a card that is asking for a decision, not merely reporting */
  .card.warn{border-color:var(--warn);border-left-width:4px}
  /* Objektkopf + Reiter (design guidelines 6.2.1/6.2.2): a page with a
     dozen cards is a scroll hunt. The head answers "what is this?", the
     tabs group the rest. Tabs are LINKS and the server picks the active
     one — no JavaScript, and every section stays in the document so a
     missing stylesheet makes the page long, not broken. */
  .objhead{background:var(--oaap-surface);border:1px solid var(--oaap-border);
       border-radius:.6rem;padding:1.1rem 1.4rem;margin-bottom:1rem;
       box-shadow:0 1px 3px rgba(23,37,84,.06)}
  .objhead .titleline{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .objhead h1{margin:0}
  .objhead .sub{color:var(--oaap-muted);font-size:.9rem;margin:.35rem 0 0}
  .facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(12rem,1fr));
       gap:.7rem 1.4rem;margin-top:1rem;border-top:1px solid var(--oaap-border);
       padding-top:.9rem}
  .facts .k{display:block;font-size:.72rem;text-transform:uppercase;
       letter-spacing:.04em;color:var(--oaap-muted)}
  .facts .v{display:block;font-size:.95rem;word-break:break-word}
  .tabs{display:flex;gap:.2rem;flex-wrap:wrap;margin-bottom:1.2rem;
       border-bottom:1px solid var(--oaap-border);position:sticky;top:0;
       background:var(--oaap-bg);z-index:5}
  .tabs a{display:flex;align-items:center;padding:.7rem 1rem;min-height:44px;
       text-decoration:none;color:var(--oaap-muted);
       border-bottom:3px solid transparent}
  .tabs a:hover{color:var(--oaap-text);background:rgba(37,99,235,.06)}
  .tabs a.active{color:var(--oaap-blue-700);font-weight:600;
       border-bottom-color:var(--oaap-blue-600)}
  .tabs a.danger.active{color:var(--err);border-bottom-color:var(--err)}
  .panel{display:none}
  .panel.active{display:block}
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
  .badge.todo{background:#fee2e2;color:#991b1b}
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
    {% if is_user_admin %}<a href="/users" class="{{ 'active' if active == 'users' }}">Benutzer</a>{% endif %}
    {% if can_health %}<a href="/health" class="{{ 'active' if active == 'health' }}">Gesundheit</a>{% endif %}
    {% if is_server_admin %}<a href="/store" class="{{ 'active' if active == 'store' }}">Store</a>{% endif %}
    {% if is_user_admin %}<a href="/instances" class="{{ 'active' if active == 'instances' }}">Instanzen</a>{% endif %}
    {% if show_tenant and is_user_admin %}<a href="/tenant" class="{{ 'active' if active == 'tenant' }}">Mandant</a>{% endif %}
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
    {# Apps öffnen im neuen Tab (Jörg, 2026-08-23): das Portal bleibt
       als Ausgangspunkt offen — der "Rückweg" ist der Tab-Wechsel,
       ohne Eingriff in fremde App-Seiten. Direkteinstieg per URL
       bleibt unberührt. #}
    <a class="tile" href="{{ t.url }}" target="_blank" rel="noopener">
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
{% if hidden_count %}
<p class="muted">{{ hidden_count }}
   {{ "weitere Instanz läuft" if hidden_count == 1 else "weitere Instanzen laufen" }}
   ohne Kachel — Hintergrunddienste, die von anderer Software benutzt
   werden. Zu sehen und umzustellen unter
   <a href="/instances">Instanzen</a>.</p>
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
{% if scope_note %}<p class="muted">Mandant <strong>{{ scope_note }}</strong> —
   Du siehst und verwaltest die Benutzer dieses Mandanten.</p>{% endif %}
<div class="card" style="overflow-x:auto;padding:.4rem 1.4rem">
<table>
  <tr><th>Benutzername</th><th>Anzeigename</th>{% if show_tenant %}<th>Mandant</th>{% endif %}<th>Rollen</th><th>Gruppen</th><th>Status</th><th></th></tr>
  {% for u in users %}
  <tr class="rowlink">
    <td><a class="rowaction" href="/users/{{ u.username }}">{{ u.username }}</a></td>
    <td>{{ u.display_name }}</td>
    {% if show_tenant %}<td class="muted">{{ labels.get(u.tenant or default_tenant, "?") }}</td>{% endif %}
    <td>{{ u.roles|join(", ") }}</td>
    <td class="muted">{{ u.groups|join(", ") if u.groups else "–" }}</td>
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
    {% if tenant_of %}
    <p class="muted">Mandant: <strong>{{ tenant_of }}</strong> — steht beim
       Anlegen fest und ändert sich nicht. Jemanden in einen anderen Mandanten
       zu versetzen heißt, ihn zu einem anderen Kunden zu versetzen; die
       ehrliche Form davon ist ein neues Konto.</p>
    {% endif %}
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
    <h2>Gruppen</h2>
    <label>Sichtbarkeits-Gruppen (kommagetrennt)
      <input type="text" name="groups" value="{{ u.groups|join(', ') }}"
             placeholder="z. B. buero, finanzen"></label>
    <p class="muted">Freie Stichworte — steuern zusätzlich zur Rolle, welche
      App-Instanzen mit eingeschränkter Sichtbarkeit dieser Benutzer sieht
      (Instanzen-Seite). server_admin sieht immer alles.</p>
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
    {% if tenants %}
    <label>Mandant
      <select name="tenant">
        {% for t in tenants %}
        <option value="{{ t.id }}" {{ 'selected' if t.id == form.tenant }}>{{ t.name }}{% if t.name != t.label %} ({{ t.label }}){% endif %}</option>
        {% endfor %}
      </select></label>
    <p class="muted">Der Mandant steht mit dem Anlegen fest und lässt sich
       danach nicht mehr ändern.</p>
    {% endif %}
  </div>
  <div class="card">
    <h2>Rollen</h2>
    <p class="roles">
      {% for r in all_roles %}
      <label><input type="checkbox" name="roles" value="{{ r }}"
             {{ 'checked' if r in form.roles }}>{{ r }}</label>
      {% endfor %}
    </p>
  </div>
  <div class="card">
    <h2>Gruppen</h2>
    <label>Sichtbarkeits-Gruppen (kommagetrennt)
      <input type="text" name="groups" value="{{ form.groups|join(', ') if form.groups else '' }}"
             placeholder="z. B. buero, finanzen"></label>
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
{% if dns and dns.rows %}
<div class="card">
  <h2>Veröffentlichte Namen</h2>
  <table>
    <tr><th>Name</th><th>Gehört zu</th><th>Löst auf nach</th><th>Status</th></tr>
    {% for d in dns.rows %}
    <tr><td>{{ d.name }}</td>
        <td class="muted">{{ d.what }}</td>
        <td class="muted">{{ d.resolved }}</td>
        <td><span class="dot {{ d.state }}"></span>{{ d.label }}</td></tr>
    {% endfor %}
  </table>
  {% if dns.note %}<p class="muted">{{ dns.note }}</p>{% endif %}
  {% if dns.public_ip %}
  <p class="muted">Öffentliche Adresse dieses Knotens: <strong>{{ dns.public_ip }}</strong>
     — ermittelt über {{ dns.source }}. Diese eine Anfrage nach außen ist
     nötig, um „zeigt der Name noch hierher?" beantworten zu können; sie
     entfällt, sobald der Knoten keinen Namen mehr veröffentlicht.</p>
  {% elif not dns.note %}
  <p class="muted">Die eigene öffentliche Adresse war nicht zu ermitteln —
     ohne sie lässt sich nur sagen, ob die Namen überhaupt auflösen.</p>
  {% endif %}
  <p class="muted">Zuletzt geprüft: {{ dns.when[:19].replace("T", " ") }} UTC
     (höchstens halbstündlich). „Zeigt woanders hin" heißt fast immer:
     Die öffentliche Adresse hat sich geändert und der DynDNS-Eintrag
     hinkt hinterher.</p>
</div>
{% endif %}
{% if reach and reach.rows %}
<div class="card">
  <h2>Direkte Ports (Erreichbarkeit)</h2>
  <table>
    <tr><th>Endpunkt</th><th>Instanz</th><th>Port</th><th>Prüfung</th><th>Status</th></tr>
    {% for r in reach.rows %}
    <tr><td>{{ r.name or "–" }}</td>
        <td class="muted">{{ r.instance }}</td>
        <td class="muted">{{ r.host_port }}/{{ r.protocol }}</td>
        <td class="muted">{% if r.how %}{{ r.how }}{% else %}–{% endif %}</td>
        <td><span class="dot {{ r.state }}"></span>{{ r.label }}</td></tr>
    {% endfor %}
  </table>
  {% if reach.note %}<p class="muted">{{ reach.note }}</p>{% endif %}
  <p class="muted">Geprüft wird per <strong>Selbstverbindung</strong> zur eigenen
     öffentlichen Adresse{% if reach.public_ip %} ({{ reach.public_ip }}, ermittelt
     über {{ reach.source }}){% endif %} — TCP direkt, UDP per STUN-Anfrage
     (die jeder WebRTC-Medienserver beantwortet). <strong>„Erreichbar"</strong>
     heißt: Der Port antwortet an der öffentlichen Adresse. <strong>„Von hier
     nicht bestätigt"</strong> ist bewusst <em>kein</em> Fehler — ein Router,
     der Verbindungen zur eigenen öffentlichen Adresse nicht zurückschleift
     (kein Hairpin), lässt eine funktionierende Freigabe von hier aus stumm
     erscheinen. Die endgültige Bestätigung von außen liefert erst der
     Reflektor (RFC-0015, Stufe 2); bis dahin gilt: mit einem echten Client
     gegentesten.</p>
  <p class="muted">Zuletzt geprüft: {{ reach.when[:19].replace("T", " ") }} UTC
     (höchstens halbstündlich).</p>
</div>
{% endif %}
{% if braked %}
<div class="card">
  <h2>Gebremste Anfragen ({{ braked.hours }} Stunden)</h2>
  {% if braked.error %}
  <p class="muted">Der Zähler war nicht abrufbar.</p>
  {% else %}
  <table>
    <tr><th>Instanz</th><th>Abgewiesen</th><th>Zuletzt</th></tr>
    {% for b in braked.rows %}
    <tr><td>{{ b.instance }}</td>
        <td>{% if b.count %}<span class="dot warn"></span>{{ b.count }}{% else %}<span class="dot ok"></span>0{% endif %}</td>
        <td class="muted">{{ b.last }}</td></tr>
    {% endfor %}
  </table>
  {% endif %}
  <p class="muted">Gezählt wird, wie oft die Mengenbremse eine Anfrage an
     eine öffentliche Route abgewiesen hat (HTTP 429). Das ist eine
     Mengenbremse und <strong>keine Zugangskontrolle</strong> — dauerhaft
     hohe Zahlen heißen: nachsehen, wer da klopft, und die App selbst
     absichern. Einstellbar je Instanz auf deren Objektseite.</p>
</div>
{% endif %}
{% if deploys %}
<div class="card">
  <h2>KI-Deployments (Deploy-Hook)</h2>
  <table>
    <tr><th>Zeit (UTC)</th><th>Instanz</th><th>Version</th><th>Commit</th><th>Ergebnis</th></tr>
    {% for d in deploys %}
    <tr><td>{{ d.when }}</td>
        <td>{{ d.instance }}</td>
        <td>{{ d.version }}</td>
        <td class="muted">{{ d.revision or "–" }}</td>
        <td><span class="dot {{ 'ok' if d.ok else 'err' }}"></span>{{ d.message }}</td></tr>
    {% endfor %}
  </table>
</div>
{% endif %}
{% if ext %}
<div class="card">
  <h2>Externer Zugriff</h2>
  <p><strong>{{ ext.host }}</strong> <span class="muted">— Portal:
  https://{{ ext.host }}/ · Apps: https://&lt;instanz&gt;.{{ ext.host }}/</span></p>
  {% if ext.last %}
  <p><span class="dot ok"></span>Letzter Aufruf: <strong>{{ ext.last.when }}</strong>
     <span class="muted">— {{ ext.last.host }} (HTTP {{ ext.last.status }})
     von {{ ext.last.ip }}</span></p>
  {% else %}
  <p><span class="dot unknown"></span><span class="muted">Noch kein Aufruf
  protokolliert — Zertifikate und Erreichbarkeit brauchen die
  Portfreigaben 80 und 443 im Router auf diesen Knoten.</span></p>
  {% endif %}
</div>
{% endif %}
<p class="muted">Geprüft wird aus Sicht des Portals über das interne
Netz. Landschafts-Gesundheit (Worker-Knoten) folgt mit RFC-0003.</p>
"""

# Floorplan "Listenbericht" — one catalogue across all enabled sources,
# with the source and its trust class on every entry (RFC-0012 §6).
STORE_BODY = """
<style>
  .filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));
       gap:.2rem .8rem;align-items:end}
  .filters label{font-size:.82rem;color:var(--oaap-muted);display:block}
  .filters select,.filters input{margin:.2rem 0 .6rem}
  .storecard{display:flex;flex-direction:column}
  .storecard .badges{display:flex;gap:.3rem;flex-wrap:wrap;margin:.5rem 0 0}
  .storecard .src{margin-top:auto;padding-top:.6rem;font-size:.78rem;
       color:var(--oaap-muted)}
  .icon{width:2rem;height:2rem;border-radius:.35rem;object-fit:contain;flex:none}
</style>
<div class="pagehead"><h1>Store</h1>
  <span><span class="muted" style="margin-right:.8rem">{{ apps|length }} von
    {{ total }} Apps</span><a class="btn" href="/store/sources">Quellen</a></span></div>
{% if msg %}
<div class="card"><p class="{{ 'ok' if msg_ok else 'err' }}" style="margin:0">{{ msg }}</p></div>
{% endif %}
{% for e in errors %}
<div class="card"><p class="err" style="margin:0"><strong>{{ e.name }}</strong>
  nicht lesbar: {{ e.error }}</p>
  <p class="muted" style="margin:.3rem 0 0">{{ e.url }}</p></div>
{% endfor %}

<form class="card" method="get" action="/store">
  <div class="filters">
    <div style="grid-column:1/-1"><label for="q">Suche</label>
      <input id="q" name="q" value="{{ f.q }}"
             placeholder="Name, Kurztext, Beschreibung oder Hashtag"></div>
    <div><label for="categories">Kategorie</label>
      <select id="categories" name="categories"><option value="">alle</option>
      {% for o in opt_categories %}<option value="{{ o.value }}"
        {{ 'selected' if f.categories == o.value }}>{{ o.label }}</option>{% endfor %}
      </select></div>
    <div><label for="app_class">Art</label>
      <select id="app_class" name="app_class"><option value="">alle</option>
      {% for o in opt_class %}<option value="{{ o.value }}"
        {{ 'selected' if f.app_class == o.value }}>{{ o.label }}</option>{% endfor %}
      </select></div>
    <div><label for="audience">Zielgruppe</label>
      <select id="audience" name="audience"><option value="">alle</option>
      {% for o in opt_audience %}<option value="{{ o.value }}"
        {{ 'selected' if f.audience == o.value }}>{{ o.label }}</option>{% endfor %}
      </select></div>
    <div><label for="maturity">Reifegrad</label>
      <select id="maturity" name="maturity"><option value="">alle</option>
      {% for o in opt_maturity %}<option value="{{ o.value }}"
        {{ 'selected' if f.maturity == o.value }}>{{ o.label }}</option>{% endfor %}
      </select></div>
    <div><label for="trust">Vertrauen</label>
      <select id="trust" name="trust"><option value="">alle</option>
      {% for o in trusts %}<option value="{{ o.value }}"
        {{ 'selected' if f.trust == o.value }}>{{ o.label }}</option>{% endfor %}
      </select></div>
    <div><label for="source">Quelle</label>
      <select id="source" name="source"><option value="">alle</option>
      {% for o in sources %}<option value="{{ o.value }}"
        {{ 'selected' if f.source == o.value }}>{{ o.label }}</option>{% endfor %}
      </select></div>
    <div><label for="license">Lizenz</label>
      <select id="license" name="license"><option value="">alle</option>
      {% for l in licenses %}<option value="{{ l }}"
        {{ 'selected' if f.license == l }}>{{ l }}</option>{% endfor %}
      </select></div>
    <div><label for="installed">Installiert</label>
      <select id="installed" name="installed"><option value="">egal</option>
        <option value="yes" {{ 'selected' if f.installed == 'yes' }}>ja</option>
        <option value="no" {{ 'selected' if f.installed == 'no' }}>nein</option>
      </select></div>
  </div>
  <label class="checkline"><input type="checkbox" name="all_profiles" value="1"
    {{ 'checked' if f.all_profiles }}> Auch Apps zeigen, die ein Profil
    erwarten, das dieser Knoten nicht hat{% if hidden_profile %}
    ({{ hidden_profile }}){% endif %}</label>
  <label class="checkline"><input type="checkbox" name="all_status" value="1"
    {{ 'checked' if f.all_status }}> Auch archivierte Apps zeigen{% if hidden_archived %}
    ({{ hidden_archived }}){% endif %}</label>
  <button>Filtern</button>
  {% if f.q or filtered %}<a class="rowaction" href="/store"
     style="margin-left:.8rem">Zurücksetzen</a>{% endif %}
</form>

{% if not apps %}
<div class="card"><p class="muted" style="margin:0">
  {% if total %}Kein Treffer — mit „Zurücksetzen" siehst Du wieder alles.
  {% else %}Keine App gefunden. Store-Quellen verwaltet die Administration
  auf der Maschine: <code>sudo oaap store list</code>.{% endif %}</p></div>
{% endif %}
<div class="tiles">
  {% for a in apps %}
  <a class="tile storecard" href="/store/{{ a.source.id }}/{{ a.id }}">
    <div class="top">
      {% if a.icon %}<img class="icon" src="{{ a.icon }}" alt="">
      {% else %}
      <svg class="hexdot" viewBox="0 0 100 100" width="24" height="24" aria-hidden="true">
        <polygon points="50,6 71,18 71,42 50,54 29,42 29,18" fill="#2563eb"/>
        <polygon points="28,44 49,56 49,80 28,92 7,80 7,56" fill="none" stroke="#2563eb" stroke-width="5"/>
        <polygon points="72,44 93,56 93,80 72,92 51,80 51,56" fill="none" stroke="#2563eb" stroke-width="5"/></svg>
      {% endif %}
      <h3>{{ a.name }}</h3>
    </div>
    <p>{{ a.summary }}</p>
    <div class="badges">
      <span class="badge">v{{ a.version }}</span>
      {% if a.is_new %}<span class="badge">neu</span>{% endif %}
      {% if a.installed %}<span class="badge test">installiert ({{ a.installed }})</span>{% endif %}
      {% if a.maturity and a.maturity != 'stable' %}<span class="badge test">{{ a.maturity_label }}</span>{% endif %}
      {% if a.status == 'deprecated' %}<span class="badge test">veraltet</span>{% endif %}
      {% if a.status == 'archived' %}<span class="badge off">archiviert</span>{% endif %}
      {% if a.expert %}<span class="badge test">nur für Experten</span>{% endif %}
      {% if a.app_class == 'service' %}<span class="badge off">{{ a.class_label }}</span>{% endif %}
      {% if not a.profile_fit %}<span class="badge off">Profil {{ a.profiles|join(', ') }}</span>{% endif %}
      {% for c in a.category_labels %}<span class="badge off">{{ c }}</span>{% endfor %}
    </div>
    <div class="src">{{ a.source.name }} · {{ a.source.trust_label }}</div>
  </a>
  {% endfor %}
</div>
<p class="muted">Nennen mehrere Quellen dieselbe App, gewinnt die höchste
Vertrauensklasse — nicht die zuerst eingetragene Quelle (RFC-0012 §3).
Quellen verwaltet die Administration auf der Maschine mit
<code>sudo oaap store list|add-source|remove-source|enable|disable|trust</code>.</p>
"""

# Floorplan "Listenbericht" — store sources (RFC-0012 §7). server_admin
# only; every change goes through the host-side worker, like every other
# portal-side registry write.
STORE_SOURCES_BODY = """
<a class="back" href="/store">← Zurück zum Store</a>
<h1>Store-Quellen</h1>
{% if msg %}
<div class="card"><p class="{{ 'ok' if msg_ok else 'err' }}" style="margin:0">{{ msg }}</p></div>
{% endif %}
<div class="card">
  <table>
    <tr><th>Quelle</th><th>Vertrauen</th><th>Status</th><th></th></tr>
    {% for s in sources %}
    <tr>
      <td><strong>{{ s.name }}</strong>
        {% if s.shipped %}<span class="badge off">mitgeliefert</span>{% endif %}
        {% if s.review %}<span class="badge test">bitte prüfen</span>{% endif %}
        <br><span class="muted">{{ s.url }}</span>
        {% if s.origin %}<br><span class="muted">Herkunft: {{ s.origin }}</span>{% endif %}
        <br><span class="muted"><code>{{ s.id }}</code></span></td>
      <td><span class="badge {{ 'test' if s.trust != 'platform' }}">{{ s.trust_label }}</span></td>
      <td><span class="badge {{ '' if s.enabled else 'off' }}">{{ 'an' if s.enabled else 'aus' }}</span></td>
      <td>
        <form method="post" action="/store/sources" style="display:inline">
          <input type="hidden" name="op" value="{{ 'disable' if s.enabled else 'enable' }}">
          <input type="hidden" name="source_id" value="{{ s.id }}">
          <button style="padding:.35rem .8rem;min-height:0;font-size:.85rem">
            {{ 'Ausschalten' if s.enabled else 'Einschalten' }}</button>
        </form>
        <details style="margin-top:.4rem"><summary class="muted">Ändern</summary>
          <form method="post" action="/store/sources" style="margin:.5rem 0 0">
            <input type="hidden" name="op" value="rename">
            <input type="hidden" name="source_id" value="{{ s.id }}">
            <label>Anzeigename <input type="text" name="name" value="{{ s.name }}"></label>
            <button style="padding:.35rem .8rem;min-height:0;font-size:.85rem">Umbenennen</button>
          </form>
          {% if s.trust != 'platform' %}
          <form method="post" action="/store/sources" style="margin:.8rem 0 0">
            <input type="hidden" name="op" value="trust">
            <input type="hidden" name="source_id" value="{{ s.id }}">
            <label>Vertrauensklasse
              <select name="trust">
                <option value="verified" {{ 'selected' if s.trust == 'verified' }}>geprüft</option>
                <option value="unverified" {{ 'selected' if s.trust == 'unverified' }}>muss bestätigt werden</option>
              </select></label>
            <p class="muted" style="margin:0 0 .5rem">Auf „geprüft" zu heben
              heißt: Apps von hier installieren <strong>ohne Bestätigung</strong>,
              und diese Quelle sticht ungeprüfte, wenn mehrere dieselbe App
              führen. „Von uns" bleibt dem vorbehalten, was die Installation
              mitbringt.</p>
            <button style="padding:.35rem .8rem;min-height:0;font-size:.85rem">Klasse setzen</button>
          </form>
          {% else %}
          <p class="muted" style="margin:.5rem 0 0">„Von uns" ist nicht
            umstellbar — die Klasse gehört zur Auslieferung. Ausschalten oder
            entfernen geht.</p>
          {% endif %}
          <form method="post" action="/store/sources" style="margin:.8rem 0 0"
                onsubmit="return confirm('Quelle {{ s.id }} entfernen?')">
            <input type="hidden" name="op" value="remove">
            <input type="hidden" name="source_id" value="{{ s.id }}">
            <input type="hidden" name="confirm" value="{{ s.id }}">
            <button style="padding:.35rem .8rem;min-height:0;font-size:.85rem">Entfernen</button>
            {% if s.shipped %}<p class="muted" style="margin:.3rem 0 0">Diese
              Quelle wird mitgeliefert; die Entfernung wird gemerkt und von
              Updates nicht rückgängig gemacht.</p>{% endif %}
          </form>
        </details>
      </td>
    </tr>
    {% else %}
    <tr><td colspan="4" class="muted">Noch keine Quelle eingetragen.</td></tr>
    {% endfor %}
  </table>
</div>

<form method="post" action="/store/sources">
  <div class="card">
    <h2>Quelle hinzufügen</h2>
    <input type="hidden" name="op" value="add">
    <label>URL der Liste <input type="text" name="url" required
           placeholder="https://…/oaap-store.json"></label>
    <label>Anzeigename <input type="text" name="name"
           placeholder="z. B. Liste von Firma X"></label>
    <label>Herkunft <input type="text" name="origin"
           placeholder="wer sie veröffentlicht — wird unverändert angezeigt"></label>
    <label>Vertrauensklasse
      <select name="trust">
        <option value="unverified">muss bestätigt werden (Standard)</option>
        <option value="verified">geprüft — ich stehe für diese Liste ein</option>
      </select></label>
    <button>Hinzufügen</button>
  </div>
</form>
<p class="muted">Führen mehrere Quellen dieselbe App, gewinnt die höchste
Vertrauensklasse — innerhalb einer Klasse die Reihenfolge oben (RFC-0012 §3).
Dieselben Regeln prüft der Server noch einmal selbst; dieselbe Verwaltung
gibt es auf der Maschine mit <code>sudo oaap store …</code>.</p>
"""

# Floorplan "Objektseite" — one app in full. This page is the reason the
# list format carries presentation fields at all; without it they would
# be decoration (RFC-0012 §6).
STORE_APP_BODY = """
<style>
  .shots{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:.8rem}
  .shots figure{margin:0}
  .shots img{width:100%;border:1px solid var(--oaap-border);border-radius:.4rem}
  .shots figcaption{font-size:.8rem;color:var(--oaap-muted);margin-top:.3rem}
  .facts{display:grid;grid-template-columns:auto 1fr;gap:.45rem 1rem;font-size:.92rem}
  .facts dt{color:var(--oaap-muted)}
  .facts dd{margin:0}
  .icon-lg{width:3rem;height:3rem;object-fit:contain;flex:none}
</style>
<a class="back" href="/store">← Zurück zum Store</a>
{% if msg %}
<div class="card"><p class="{{ 'ok' if msg_ok else 'err' }}" style="margin:0">{{ msg }}</p></div>
{% endif %}
<div class="card">
  <div style="display:flex;gap:.9rem;align-items:flex-start">
    {% if a.icon %}<img class="icon-lg" src="{{ a.icon }}" alt="">{% endif %}
    <div style="flex:1">
      <h1 style="margin:0">{{ a.name }}</h1>
      <p class="muted" style="margin:.3rem 0 0">{{ a.summary }}</p>
      <div style="display:flex;gap:.3rem;flex-wrap:wrap;margin-top:.7rem">
        <span class="badge">v{{ a.version }}</span>
        {% if a.is_new %}<span class="badge">neu</span>{% endif %}
        {% if a.installed %}<span class="badge test">installiert ({{ a.installed }})</span>{% endif %}
        {% if a.maturity %}<span class="badge {{ 'test' if a.maturity != 'stable' }}">{{ a.maturity_label }}</span>{% endif %}
        {% if a.status != 'active' %}<span class="badge test">{{ a.status_label }}</span>{% endif %}
        {% if a.expert %}<span class="badge test">nur für Experten</span>{% endif %}
        {% for c in a.category_labels %}<span class="badge off">{{ c }}</span>{% endfor %}
      </div>
    </div>
  </div>
</div>

{% if not a.profile_fit %}
<div class="card"><p style="margin:0"><span class="badge test">Hinweis</span>
  Diese App erwartet ein Knoten-Profil ({{ a.profiles|join(', ') }}), das dieser
  Knoten nicht hat{% if profiles %} (hier: {{ profiles|join(', ') }}){% else %}
  (hier: keines){% endif %}. Der Store <strong>warnt nur</strong> — installieren
  kannst Du sie trotzdem (RFC-0011).</p></div>
{% endif %}
{% if a.status == 'deprecated' %}
<div class="card"><p style="margin:0"><span class="badge test">Veraltet</span>
  Diese App wird nicht mehr empfohlen. Installierbar bleibt sie.</p></div>
{% endif %}

<div class="card">
  <h2>Installieren</h2>
  {% if a.pending %}
  <p class="muted" style="margin:0">⏳ Installation läuft — das Ergebnis
  erscheint hier und im Deploy-Protokoll auf der Gesundheitsseite.</p>
  {% elif a.installed == a.version %}
  <p class="ok" style="margin:0">Auf dem aktuellen Stand (v{{ a.version }}).</p>
  {% else %}
  <form method="post" action="/store/install"
        onsubmit="this.querySelector('button').disabled=true;
                  this.querySelector('button').textContent='Wird installiert …'">
    <input type="hidden" name="app_id" value="{{ a.id }}">
    <input type="hidden" name="source_id" value="{{ a.source.id }}">
    {% if a.source.trust == 'unverified' %}
    <p class="muted">„{{ a.source.name }}" ist eine <strong>ungeprüfte
    Quelle</strong>. Die Bestätigung wird mit Deinem Namen protokolliert.</p>
    <label class="checkline"><input type="checkbox" name="confirm_source"
           value="{{ a.source.id }}" required>
      Ich installiere bewusst aus dieser Quelle.</label>
    {% endif %}
    {% if a.expert %}
    <p class="muted">Diese App ist als <strong>nur für Experten</strong>
    gekennzeichnet — sie verlangt Wissen, das über die Bedienung hinausgeht.</p>
    {% endif %}
    <button>{{ ('Aktualisieren auf v' + a.version) if a.installed else 'Installieren' }}</button>
  </form>
  <p class="muted" style="margin:.6rem 0 0">Installiert wird auf dem
  Produktions-Kanal. Welches Paket geholt wird, entscheidet der Server
  selbst anhand der eingetragenen Quellen — nicht diese Seite.</p>
  {% endif %}
</div>

{% if a.description and a.description != a.summary %}
<div class="card"><h2>Beschreibung</h2>
  <p style="margin:0;white-space:pre-line">{{ a.description }}</p></div>
{% endif %}

{% if a.screenshots %}
<div class="card"><h2>Bilder</h2>
  <div class="shots">
    {% for s in a.screenshots %}
    <figure><img src="{{ s.src }}" alt="{{ s.caption }}">
      {% if s.caption %}<figcaption>{{ s.caption }}</figcaption>{% endif %}</figure>
    {% endfor %}
  </div></div>
{% endif %}

<div class="card">
  <h2>Fakten</h2>
  <dl class="facts">
    <dt>Kennung</dt><dd><code>{{ a.id }}</code></dd>
    <dt>Version</dt><dd>{{ a.version }}{% if a.released %} vom {{ a.released }}{% endif %}</dd>
    {% if a.type %}<dt>Verpackung</dt><dd>{{ a.type }}</dd>{% endif %}
    {% if a.class_label %}<dt>Art</dt><dd>{{ a.class_label }}{% if a.app_class == 'service' %}
      <br><span class="muted">Wird von anderer Software benutzt und bekommt
      deshalb keine Kachel im Launchpad. Umstellen lässt sich das nach der
      Installation auf der Instanzseite.</span>{% endif %}</dd>{% endif %}
    {% if a.audience_labels %}<dt>Zielgruppe</dt><dd>{{ a.audience_labels|join(', ') }}</dd>{% endif %}
    {% if a.roles %}<dt>Rollen</dt><dd>{{ a.roles|join(', ') }}</dd>{% endif %}
    {% if a.license %}<dt>Lizenz</dt><dd>{{ a.license }}</dd>{% endif %}
    <dt>Quelle</dt><dd>{{ a.source.name }} — {{ a.source.trust_label }}{% if a.source.origin %}
      <br><span class="muted">Herkunft: {{ a.source.origin }}</span>{% endif %}</dd>
    {% if a.also_in %}<dt>Auch gelistet in</dt>
      <dd>{{ a.also_in|join(', ') }} <span class="muted">— installiert wird
      aus der Quelle mit der höchsten Vertrauensklasse</span></dd>{% endif %}
    <dt>Paket</dt><dd>
      {% if a.pinned %}festgelegt auf <code>{{ a.ref }}</code>
      {% else %}<span class="badge test">nicht festgelegt</span>
      <span class="muted">— installiert wird, was der Standard-Zweig gerade
      enthält</span>{% endif %}</dd>
    {% if a.tags %}<dt>Hashtags</dt><dd class="muted">{{ a.tags|join(' · ') }}</dd>{% endif %}
  </dl>
</div>

{% if a.links %}
<div class="card"><h2>Links</h2>
  <ul style="margin:0;padding-left:1.1rem">
    {% for l in a.links %}
    <li><a href="{{ l.url }}" target="_blank" rel="noopener">{{ l.label }} ↗</a></li>
    {% endfor %}
  </ul></div>
{% endif %}

{% if a.command %}
<div class="card"><h2>Installation von Hand</h2>
  <code style="display:block;background:#f8fafc;border:1px solid var(--oaap-border);
               border-radius:.4rem;padding:.5rem .7rem;overflow-x:auto;white-space:pre">{{ a.command }}</code>
</div>
{% endif %}
"""

# Floorplan "Listenbericht" — installed app instances and their
# visibility setting (RFC-0007). server_admin only.
# Floorplan "Listenbericht". Shown only where there is more than one
# tenant — the menu entry that leads here is hidden otherwise, and the
# route says the same thing again for anyone who typed the address.
TENANT_BODY = """
<h1>{{ "Mandanten" if is_server_admin else "Mandant" }}</h1>
{% if not is_server_admin %}
<div class="card">
  <h2>{{ me.name }}</h2>
  <p class="muted">Kürzel <code>{{ me.label }}</code>, angelegt {{ me.created }}.</p>
  <p>{{ me.users }} Benutzer, {{ me.instances }} Instanz(en).</p>
  {% if host %}
  <p class="muted">Apps dieses Mandanten sind erreichbar unter
     <code>&lt;instanz&gt;.{{ me.label }}.{{ host }}</code>.</p>
  {% endif %}
</div>
{% else %}
<div class="card" style="overflow-x:auto;padding:.4rem 1.4rem">
<table>
  <tr><th>Kürzel</th><th>Name</th><th>Benutzer</th><th>Instanzen</th><th>Angelegt</th></tr>
  {% for t in tenants %}
  <tr><td><code>{{ t.label }}</code></td><td>{{ t.name }}</td>
      <td>{{ t.users }}</td><td>{{ t.instances }}</td>
      <td class="muted">{{ t.created }}</td></tr>
  {% endfor %}
</table>
</div>
<p class="muted">Angelegt und umbenannt werden Mandanten an der Maschine:
   <code>sudo oaap tenant create &lt;kürzel&gt;</code>. Das Kürzel steht im
   Hostnamen und damit im öffentlichen Certificate-Transparency-Log — für
   einen vertraulichen Kunden also eines wählen, das nichts verrät.</p>
{% endif %}

<h2>Protokoll</h2>
<p class="muted">Jede Zustandsänderung an {{ "einem Mandanten" if is_server_admin
   else "diesem Mandanten" }} — wer, wann, was, mit welchem Ergebnis.
   <strong>Auch die des Betreibers.</strong> Ein <code>server_admin</code> darf
   auf diesem Knoten alles; das Gegengewicht ist nicht eine Schranke, die es
   nicht gibt, sondern dass hier steht, was getan wurde. Lesen wird nicht
   protokolliert, nur Ändern.</p>
{% if entries %}
<div class="card" style="overflow-x:auto;padding:.4rem 1.4rem">
<table>
  <tr><th>Wann</th>{% if is_server_admin %}<th>Mandant</th>{% endif %}<th>Wer</th><th>Rolle</th><th>Was</th><th>Betrifft</th><th>Ergebnis</th></tr>
  {% for e in entries %}
  <tr>
    <td class="muted">{{ e.when }}</td>
    {% if is_server_admin %}<td>{{ e.tenant_label or "–" }}</td>{% endif %}
    <td>{{ e.who }}</td><td class="muted">{{ e.role }}</td>
    <td><code>{{ e.action }}</code></td>
    <td>{{ e.subject }}</td>
    <td>{{ e.result }}{% if e.detail %} <span class="muted">— {{ e.detail }}</span>{% endif %}</td>
  </tr>
  {% endfor %}
</table>
</div>
{% else %}
<div class="card"><p class="muted">Noch keine Einträge.</p></div>
{% endif %}
"""

INSTANCES_LIST_BODY = """
<h1>Instanzen</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
{% if can_create %}
<p style="display:flex;align-items:center;gap:.8rem;flex-wrap:wrap">
   <a class="btn" href="/instances/new">Test-Instanz anlegen</a>
   <span class="muted">möglich, weil dieser Knoten das Profil
   <code>dev</code> trägt (RFC-0011)</span></p>
{% endif %}
{% if scope_note %}<p class="muted">Mandant <strong>{{ scope_note }}</strong> —
   Du siehst und verwaltest die Instanzen dieses Mandanten.</p>{% endif %}
{% if instances %}
<div class="card" style="overflow-x:auto;padding:.4rem 1.4rem">
<table>
  <tr><th>Instanz</th><th>App</th>{% if show_tenant %}<th>Mandant</th>{% endif %}<th>Kanal</th><th>Sichtbarkeit</th><th>Kachel</th><th></th></tr>
  {% for i in instances %}
  <tr class="rowlink">
    <td><a class="rowaction" href="/instances/{{ i.name }}">{{ i.name }}</a></td>
    <td>{{ i.app_name }} <span class="muted">v{{ i.version }}</span></td>
    {% if show_tenant %}<td class="muted">{{ i.tenant or "?" }}</td>{% endif %}
    <td><span class="badge {{ i.channel }}">{{ i.channel_label }}</span></td>
    <td>{{ i.visibility_label }}</td>
    <td>{{ "ja" if i.tile_visible else "nein" }}{% if i.tile_mode != "auto" %}
        <span class="muted">(fest)</span>{% endif %}</td>
    <td><a class="rowaction" href="/instances/{{ i.name }}">Bearbeiten</a></td>
  </tr>
  {% endfor %}
</table>
</div>
{% else %}
<div class="card"><p class="muted">Noch keine Apps installiert.</p></div>
{% endif %}
<p class="muted">Sichtbarkeit schränkt zusätzlich zur Rolle ein, wer eine
installierte Instanz sehen und öffnen darf (RFC-0007) — <code>server_admin</code>
sieht immer alle Instanzen, unabhängig davon.</p>
<p class="muted"><strong>Kachel</strong> sagt nur, ob die Instanz im Launchpad
erscheint. Hintergrunddienste bekommen von sich aus keine; „fest" heißt, dass
jemand das ausdrücklich umgestellt hat. Das ist reine Anzeige — Adresse,
Routen und Rollen der Instanz bleiben davon unberührt.</p>
{% if can_create %}
<div class="card">
  <h2>Anlege-Erlaubnis fürs Studio</h2>
  <p class="muted">Eine Test-Instanz legst Du oben selbst an. Soll sie
     stattdessen <strong>aus dem Studio heraus</strong> entstehen — dort liegt
     das Paket schon —, dann stellst Du hier eine <strong>einmalige
     Erlaubnis</strong> für <em>einen</em> Instanznamen aus. Sie gilt
     {{ grant_minutes }} Minuten, wird beim Anlegen verbraucht und lässt sich
     bis dahin widerrufen. Das Studio bewahrt sie nicht auf — wie beim
     Deploy-Token gibt der Mensch sie im Augenblick der Handlung ein
     (RFC-0019).</p>
  {% if grants %}
  <table class="mini">
    <tr><th>Offen für</th>{% if show_tenant %}<th>Mandant</th>{% endif %}<th>Läuft ab in</th><th></th></tr>
    {% for gr in grants %}
    <tr>
      <td><code>{{ gr.instance }}</code></td>
      {% if show_tenant %}<td class="muted">{{ gr.tenant or "?" }}</td>{% endif %}
      <td>{{ gr.minutes }} Minuten</td>
      <td><form method="post" action="/instances/grant">
        <input type="hidden" name="op" value="revoke">
        <input type="hidden" name="name" value="{{ gr.instance }}">
        <button class="secondary">Widerrufen</button>
      </form></td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}
  <form method="post" action="/instances/grant">
    <label>Name der künftigen Instanz <input type="text" name="name"
           placeholder="z. B. bdt-app-test" autocomplete="off"></label>
    <button>Erlaubnis ausstellen</button>
  </form>
  <p class="muted">Die Erlaubnis gilt <strong>nur für diesen Namen</strong> und
     <strong>nur für den Test-Kanal</strong>. Sie erlaubt genau ein Anlegen —
     kein Ausrollen einer bestehenden Instanz, keinen Zugriff auf Daten. Für
     spätere Aktualisierungen erzeugst Du danach ein normales Deploy-Token auf
     der Instanzseite.</p>
</div>
{% endif %}
"""

# Floorplan "Dialogseite": like the deploy token, the grant is readable
# exactly once, and deliberately NOT after a redirect — a redirect would
# put it in a URL, and the gateway logs full URIs including their query.
GRANT_SHOW_BODY = """
<a class="back" href="/instances">← Zurück zur Liste</a>
<h1>Anlege-Erlaubnis für {{ name }}</h1>
<div class="card">
  <p class="ok">Erlaubnis ausgestellt. <strong>Sie wird nur dieses eine Mal
     angezeigt.</strong> Gespeichert ist davon nur eine Prüfsumme.</p>
  <p><code style="display:block;padding:.7rem;word-break:break-all;font-size:1.05rem">{{ grant }}</code></p>
  <p>Gültig {{ minutes }} Minuten, <strong>einmal verwendbar</strong>, gebunden
     an den Namen <code>{{ name }}</code> und den Test-Kanal.</p>
  <h2>So wird sie benutzt</h2>
  <p class="muted">Im Studio beim Vorhaben: Instanzname <code>{{ name }}</code>,
     Hook-Adresse <code>{{ hook_url }}</code>, Paket wählen, und diese Erlaubnis
     in das Token-Feld eintragen. Das Studio meldet das Paket an und lädt es
     hoch — dieselben drei Schritte wie bei jedem Deployment.</p>
  <p class="muted">Danach ist die Erlaubnis verbraucht. Für spätere
     Aktualisierungen erzeugst Du auf der Instanzseite ein Deploy-Token.</p>
  <p><a href="/instances">Fertig — zurück zur Liste</a></p>
</div>
"""

# Floorplan "Dialogseite" — create a test instance. Only reachable on a
# node with profile `dev` (RFC-0011); the store's one-click install
# remains the way to production instances on every node.
INSTANCE_NEW_BODY = """
<a class="back" href="/instances">← Zurück zur Liste</a>
<h1>Test-Instanz anlegen</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<div class="card">
  <p><span class="badge test">Profil dev</span> Dieser Knoten ist als
  <strong>Entwicklungsknoten</strong> gekennzeichnet. Deshalb darf hier
  aus dem Portal heraus angelegt werden — auf einem Produktivknoten
  bleibt das der Kommandozeile vorbehalten (RFC-0011).</p>
</div>
<form method="post" action="/instances/new" enctype="multipart/form-data">
  <div class="card">
    <h2>Was soll installiert werden?</h2>
    <label class="checkline"><input type="radio" name="from" value="store" checked>
      Eine App aus dem Store</label>
    <label>App
      <select name="app_id">
        {% for a in store_apps %}
        <option value="{{ a.id }}">{{ a.name }} (v{{ a.version }}) — {{ a.source }} [{{ a.trust_label }}]</option>
        {% else %}
        <option value="">— keine Store-Quelle lesbar —</option>
        {% endfor %}
      </select></label>
    {% if store_apps | selectattr('trust', 'equalto', 'unverified') | list %}
    <label class="checkline"><input type="checkbox" name="confirm_source" value="yes">
      Ich installiere bewusst aus einer ungeprüften Quelle (nur nötig, wenn
      die gewählte App aus einer solchen stammt).</label>
    {% endif %}
    <label class="checkline"><input type="radio" name="from" value="git">
      Aus einem Git-Repository, das noch in keiner Liste steht</label>
    <label>Git-URL <input type="text" name="url"
           placeholder="https://github.com/… oder git@github.com:…"></label>
    <label>Pfad im Repository (optional) <input type="text" name="path"
           placeholder="z. B. apps/hub"></label>
    <label>Branch oder Tag (optional) <input type="text" name="ref"
           placeholder="z. B. v0.3.0 — leer = Standardbranch"></label>
    <p class="muted">Der freie Git-Weg ist der Grund für das Profil: Eine
    brandneue App steht in keiner Store-Liste. Auf einem Knoten ohne
    <code>dev</code> gilt weiterhin, dass nur installiert wird, was eine
    konfigurierte Quelle listet.</p>
    <label class="checkline"><input type="radio" name="from" value="artifact">
      Aus einem hochgeladenen Paket (ZIP)</label>
    <label>Paket <input type="file" name="artifact" accept=".zip"></label>
    <label>Pfad im Paket (optional) <input type="text" name="artifact_path"
           placeholder="leer = Wurzel oder einziger Oberordner"></label>
    <p class="muted">Für Quellen, auf die dieser Knoten keinen Zugriff hat
    oder haben soll: ein privates Repository, ein Rechner ohne Internet,
    eine Datei vom Stick. Der Knoten bekommt damit ein <strong>Paket statt
    eines Zugangsrechts</strong> — er muss keine fremden Zugangsdaten
    speichern (RFC-0019). Das Paket bleibt hier liegen, damit spätere
    Deployments und ein Rückschritt eine echte Quelle haben.</p>
  </div>
  <div class="card">
    <h2>Name der Instanz</h2>
    <label>Instanzname (optional) <input type="text" name="name"
           placeholder="leer = Kennung der App"></label>
    <p class="muted">Der Name ist die Adresse der Instanz und lässt sich
    später nicht ändern. Kleinbuchstaben, Ziffern und Bindestriche.</p>
    <p class="muted"><strong>Kanal: Test.</strong> Aus dem Portal
    angelegte Instanzen landen immer auf dem Test-Kanal — sie dürfen ein
    Deploy-Token bekommen, und ein erneutes Deployment derselben Version
    ist erlaubt. Produktiv installiert weiterhin der Store.</p>
    <button>Anlegen</button>
  </div>
</form>
"""

# Floorplan "Objektseite" mit Objektkopf und Reitern (design guidelines
# 6.2.1/6.2.2). Diese Seite trägt ein Dutzend Karten — ohne Gruppierung
# ist sie eine Scroll-Jagd. Reihenfolge der Reiter: erst lesen
# (Überblick), dann das Tägliche, zuletzt das Gefährliche.
#
# Alle Abschnitte stehen IMMER im Dokument, nur der gewählte ist
# sichtbar. Das kostet ein paar Kilobyte und spart JavaScript, einen
# zweiten Zustand und die Frage, was ohne Stylesheet passiert.
INSTANCE_EDIT_BODY = """
<a class="back" href="/instances">← Zurück zur Liste</a>
<div class="objhead">
  <div class="titleline">
    <h1>{{ i.name }}</h1>
    <span class="badge {{ 'test' if i.is_test else 'off' }}">{{ i.channel_label }}</span>
    {% if i.pending %}<span class="badge todo">Bestätigung offen</span>{% endif %}
  </div>
  <p class="sub">{{ i.app_name }} {{ i.version }} · Kennung <code>{{ i.app_id }}</code></p>
  <div class="facts">
    <div><span class="k">Adresse</span><span class="v">
      {% if i.address_url %}<a href="{{ i.address_url }}">{{ i.address_host }}</a>
      {% else %}— <span class="muted">(kein externer Name)</span>{% endif %}</span></div>
    <div><span class="k">Sichtbar für</span><span class="v">{{ i.visibility_label }}</span></div>
    <div><span class="k">Kachel</span><span class="v">{{ "im Launchpad" if i.tile_visible else "ausgeblendet" }}</span></div>
    <div><span class="k">Herkunft</span><span class="v">{{ i.source_label }}</span></div>
    <div><span class="k">Deploy-Token</span><span class="v">
      {% if not i.is_test %}<span class="muted">nur für Test-Instanzen</span>
      {% elif i.token_created %}seit {{ i.token_created }}
      {% else %}<span class="muted">keins</span>{% endif %}</span></div>
    <div><span class="k">Verbindungen</span><span class="v">
      {% if i.links %}{{ i.links|length }} zu {{ i.links|join(", ") }}
      {% else %}<span class="muted">keine — isoliert</span>{% endif %}</span></div>
  </div>
</div>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
{% if i.pending %}
{# Über den Reitern, nicht in einem: eine anstehende Entscheidung darf
   die Gruppierung nicht verstecken (design guidelines 6.2.2). #}
<div class="card warn">
  <h2>Deployment wartet auf Bestätigung</h2>
  <p>Eine Anmeldung für <strong>Version {{ i.pending.version }}</strong> würde
     erweitern, was diese Instanz erreichen darf oder wer sie erreichen darf.
     Deshalb ist sie abgelehnt worden, bis Du zustimmst:</p>
  <ul>{% for r in i.pending.reasons %}<li>{{ r }}</li>{% endfor %}</ul>
  <p class="muted">Die Bestätigung gilt für <em>genau dieses</em> Manifest, nicht
     für das nächste Deployment. Nach dem Zustimmen meldet die KI dieselbe
     Version erneut an und lädt hoch.</p>
  <form method="post" action="/instances/{{ i.name }}/envelope" style="display:inline">
    <input type="hidden" name="tab" value="{{ tab }}">
    <input type="hidden" name="op" value="confirm">
    <input type="hidden" name="manifest_sha" value="{{ i.pending.manifest_sha }}">
    <button>Erweiterung bestätigen</button>
  </form>
  <form method="post" action="/instances/{{ i.name }}/envelope" style="display:inline">
    <input type="hidden" name="tab" value="{{ tab }}">
    <input type="hidden" name="op" value="reject">
    <button class="secondary">Verwerfen</button>
  </form>
</div>
{% endif %}
<nav class="tabs">
  {% for key, label in tabs %}
  <a href="/instances/{{ i.name }}?tab={{ key }}"
     class="{{ 'active' if key == tab }}{{ ' danger' if key == 'verwaltung' }}">{{ label }}</a>
  {% endfor %}
</nav>

<section class="panel {{ 'active' if tab == 'ueberblick' }}">
<div class="card">
  <h2>Was die App mitbringt</h2>
  {% if i.description %}<p>{{ i.description }}</p>{% endif %}
  <p class="muted">Das hier ist die Selbstauskunft der App aus ihrem Manifest —
     sie kann sich mit dem nächsten Deployment ändern. Was <em>Du</em>
     entscheidest, steht in den anderen Reitern.</p>
  <h3>Routen</h3>
  <table class="mini">
    <tr><th>Pfad</th><th>Wer darf hinein</th></tr>
    {% for r in i.route_rows %}
    <tr><td><code>{{ r.path }}</code></td><td>{{ r.who }}</td></tr>
    {% endfor %}
  </table>
  <p class="muted">Die Rollen erzwingt das Gateway bei jeder Anfrage — nicht
     diese Seite und nicht die App.</p>
  {% if i.services|length > 1 %}
  <h3>Dienste</h3>
  <ul class="muted">
    {% for s in i.services %}
    <li><code>{{ s.service or "(einziger)" }}</code> — Port {{ s.port }}{{ " (Hauptdienst)" if loop.first }}</li>
    {% endfor %}
  </ul>
  {% endif %}
  <h3>Datenablage</h3>
  {% if i.storage %}
  <ul class="muted">
    {% for s in i.storage %}<li><code>{{ s.name }}</code> im Container unter <code>{{ s.mount }}</code></li>{% endfor %}
  </ul>
  <p class="muted">Nur hier darf die App schreiben, und nur das ist im Backup.</p>
  {% else %}
  <p class="muted">Diese App erklärt keine Ablage — sie hält nichts fest, was
     einen Neustart überlebt.</p>
  {% endif %}
</div>
<div class="card">
  <h2>Herkunft</h2>
  <p>{{ i.source_label }}</p>
  {% if i.source_lines %}
  <ul class="muted">{% for l in i.source_lines %}<li>{{ l }}</li>{% endfor %}</ul>
  {% endif %}
  <p class="muted">Aus dieser Quelle wird die Instanz neu ausgerollt und
     wiederhergestellt. Kanal <strong>{{ i.channel_label }}</strong>:
     {% if i.is_test %}Test-Instanzen dürfen ein Deploy-Token bekommen, und
     dieselbe Version darf erneut ausgerollt werden.
     {% else %}Produktiv-Instanzen bekommen kein Deploy-Token; sie werden über
     den Store aktualisiert.{% endif %}</p>
</div>
</section>

<section class="panel {{ 'active' if tab == 'zugang' }}">
<form method="post" action="/instances/{{ i.name }}/visibility">
  <input type="hidden" name="tab" value="zugang">
  <div class="card">
    <h2>Sichtbarkeit</h2>
    <label class="checkline"><input type="radio" name="mode" value="all"
           {{ 'checked' if not i.groups }}>Für alle mit passender Rolle sichtbar (Standard)</label>
    <label class="checkline"><input type="radio" name="mode" value="groups"
           {{ 'checked' if i.groups }}>Nur für bestimmte Gruppen</label>
    <label>Gruppen (kommagetrennt) <input type="text" name="groups"
           value="{{ i.groups|join(', ') }}" placeholder="z. B. buero, finanzen"></label>
    <p class="muted"><code>server_admin</code> sieht diese Instanz immer,
       unabhängig von dieser Einstellung. Rollen legt weiterhin das
       App-Manifest fest ({{ i.roles|join(", ") if i.roles else "keine Einschränkung" }}).</p>
    <button>Speichern</button>
  </div>
</form>
<form method="post" action="/instances/{{ i.name }}/tile">
  <input type="hidden" name="tab" value="zugang">
  <div class="card">
    <h2>Kachel im Launchpad</h2>
    <p class="muted">{{ i.tile_reason }}</p>
    <label class="checkline"><input type="radio" name="mode" value="auto"
           {{ 'checked' if i.tile_mode == 'auto' }}>Der App folgen (Standard)</label>
    <label class="checkline"><input type="radio" name="mode" value="on"
           {{ 'checked' if i.tile_mode == 'on' }}>Immer zeigen</label>
    <label class="checkline"><input type="radio" name="mode" value="off"
           {{ 'checked' if i.tile_mode == 'off' }}>Nie zeigen</label>
    <p class="muted">Das ist reine Anzeige und <strong>keine
       Zugriffskontrolle</strong>: Die Instanz behält ihre Adresse, ihre Routen
       und ihre Rollen, und das Gateway prüft weiter wie bisher. Wer eine App
       vor bestimmten Leuten verbergen will, nimmt die Sichtbarkeit oben.</p>
    <button>Speichern</button>
  </div>
</form>
{% if i.has_public_route %}
<form method="post" action="/instances/{{ i.name }}/throttle">
  <input type="hidden" name="tab" value="zugang">
  <div class="card">
    <h2>Drosselung öffentlicher Routen</h2>
    <p class="muted">Diese App hat mindestens eine Route, die <strong>ohne
       Anmeldung</strong> erreichbar ist. Dort begrenzt die Plattform die
       Anfragen je Client-Adresse — ein gemeinsames Budget über alle Zugänge
       dieser Instanz.</p>
    <label class="checkline"><input type="radio" name="mode" value="default"
           {{ 'checked' if i.throttle_mode == 'default' }}>Standard ({{ i.throttle_default }})</label>
    <label class="checkline"><input type="radio" name="mode" value="custom"
           {{ 'checked' if i.throttle_mode == 'custom' }}>Eigener Wert</label>
    <label>Anfragen pro Sekunden <input type="text" name="rate" value="{{ i.throttle_rate }}"
           placeholder="z. B. 600/60 für 600 Anfragen pro Minute"></label>
    <label class="checkline"><input type="radio" name="mode" value="off"
           {{ 'checked' if i.throttle_mode == 'off' }}>Aus — keine Bremse</label>
    <p class="muted">Das ist eine Mengenbremse, keine Zugangskontrolle: Sie
       begrenzt pro Adresse und hält niemanden auf, der viele Adressen hat.
       Die App muss ihre eigenen Zugangsschlüssel weiterhin selbst schützen.
       Alle Geräte hinter einem Internetanschluss zählen als ein Client.</p>
    <button>Speichern</button>
  </div>
</form>
{% endif %}
</section>

<section class="panel {{ 'active' if tab == 'netz' }}">
<div class="card">
  <h2>Eigene Adresse</h2>
  <p class="muted">Automatisch erreichbar unter
     <code>{{ i.auto_address or "— (dieser Knoten hat keinen externen Namen)" }}</code>.
     Zusätzlich kann diese Instanz eigene öffentliche Namen tragen —
     einen <strong>Hauptnamen</strong> (den man in ausgelieferte Software
     einbaut und der einen Umzug überlebt) und beliebig viele
     <strong>Aliasse</strong>, die gleichwertig erreichbar sind. Alle Namen
     stehen unter demselben Schutz — ein Alias ist kein Schlupfloch.</p>
  <form method="post" action="/instances/{{ i.name }}/address">
    <input type="hidden" name="tab" value="netz">
    <label>Hauptname <input type="text" name="hostname" value="{{ i.address }}"
           placeholder="z. B. hub.meine-domain.de"></label>
    <p class="muted">Der Name muss selbst auf diesen Knoten zeigen (DNS-Eintrag
       und Portfreigabe bleiben Deine Sache). Das Zertifikat holt die Plattform
       beim ersten Zugriff. Die automatische Adresse bleibt gültig —
       <strong>sie gehört nicht in dieses Feld</strong>: Namen unter dem
       Knotennamen entstehen von selbst und werden hier abgelehnt.
       Hierher gehört ein <em>eigener</em> Name (z. B. eine Produkt-Domain).</p>
    <button>Hauptnamen speichern</button>
    {% if i.address and not i.aliases %}<button name="op" value="remove" class="secondary"
        onclick="return confirm('Hauptnamen {{ i.address }} wirklich entfernen? Dieser Name kann in ausgelieferte Software eingebaut sein — er ist danach sofort nicht mehr erreichbar. Die automatische Adresse bleibt bestehen.')">Hauptnamen entfernen</button>{% endif %}
  </form>
  {% if i.address %}
  <h3>Aliasse</h3>
  {% if i.aliases %}
  <ul class="muted">
    {% for a in i.aliases %}
    <li><code>{{ a }}</code>
      <form method="post" action="/instances/{{ i.name }}/address" style="display:inline">
        <input type="hidden" name="tab" value="netz">
        <input type="hidden" name="op" value="alias-remove">
        <input type="hidden" name="hostname" value="{{ a }}">
        <button class="secondary">entfernen</button>
      </form>
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="muted">Noch keine Aliasse.</p>
  {% endif %}
  <form method="post" action="/instances/{{ i.name }}/address">
    <input type="hidden" name="tab" value="netz">
    <input type="hidden" name="op" value="alias-add">
    <label>Alias hinzufügen <input type="text" name="hostname"
           placeholder="z. B. bdt-hub-test.joomp.de"></label>
    <button>Alias hinzufügen</button>
  </form>
  {% else %}
  <p class="muted">Aliasse sind erst möglich, wenn ein Hauptname gesetzt ist.</p>
  {% endif %}
</div>
{% if i.endpoints %}
<div class="card">
  <h2>Direkter Port (ohne Gateway)</h2>
  <p class="muted">Diese App möchte einen <strong>Nicht-HTTP-Port</strong> —
     etwa für Echtzeit-Medien. Ein solcher Port läuft <strong>am Gateway
     vorbei</strong>: <strong>keine Anmeldung, keine Rollenprüfung, keine
     Drosselung, kein Zugriffsprotokoll</strong>. Wer hereinkommt, entscheidet
     allein die App. Das ist die bewusste Ausnahme — Du gibst sie frei, nicht
     die App.</p>
  {% for e in i.endpoints %}
  <div class="subcard">
    <p><strong>{{ e.name }}</strong> — {{ e.protocol }}, App-Port {{ e.container_port }}{% if e.fixed %} <span class="muted">(fester Port {{ e.container_port }})</span>{% endif %}</p>
    <p class="muted">Begründung der App: {{ e.reason or '—' }}{% if e.fixed %} Dieser Port ist <strong>fest</strong>: Die App bewirbt ihn selbst bei ihren Clients, deshalb wird er unverändert veröffentlicht — ist er belegt, scheitert die Freigabe (statt einen anderen zu nehmen).{% endif %}</p>
    {% if e.granted %}
    <p>Freigegeben auf <strong>Host-Port {{ e.host_port }}</strong>. Leite diesen
       Port auf Deinem Router auf diesen Knoten weiter ({{ e.protocol }}
       {{ e.host_port }}). Die Adresse ist knotenlokal — der Edge trägt sie nicht,
       und eine Wiederherstellung auf einer anderen Maschine bringt sie nicht mit.</p>
    <form method="post" action="/instances/{{ i.name }}/endpoint">
      <input type="hidden" name="tab" value="netz">
      <input type="hidden" name="op" value="deny">
      <input type="hidden" name="endpoint" value="{{ e.name }}">
      <button class="secondary">Port schließen</button>
    </form>
    {% elif i.node_exposed %}
    <form method="post" action="/instances/{{ i.name }}/endpoint"
          onsubmit="return confirm('Dieser Port läuft am Gateway vorbei — keine Anmeldung, keine Drosselung, kein Protokoll. Wirklich freigeben?');">
      <input type="hidden" name="tab" value="netz">
      <input type="hidden" name="op" value="allow">
      <input type="hidden" name="endpoint" value="{{ e.name }}">
      <button>Port freigeben</button>
    </form>
    {% else %}
    <p class="muted">Dieser Knoten hat das Profil <code>exposed</code> nicht —
       einen solchen Port kann er nicht freigeben. Setze es bewusst auf der
       Maschine: <code>sudo oaap node add-profile exposed</code>.</p>
    {% endif %}
  </div>
  {% endfor %}
</div>
{% else %}
<div class="card">
  <h2>Direkter Port (ohne Gateway)</h2>
  <p class="muted">Diese App will keinen Port am Gateway vorbei. Alles, was sie
     anbietet, läuft über die Adresse oben — mit Anmeldung, Rollenprüfung,
     Drosselung und Protokoll.</p>
</div>
{% endif %}
<div class="card">
  <h2>Verbindungen zu anderen Apps</h2>
  <p class="muted">Standardmäßig ist jede App für sich — sie erreicht keine
     andere. Hier kannst Du dieser Instanz ausdrücklich erlauben, eine andere
     zu erreichen (über ein eigenes, getrenntes Netz). Das ist die einzige Art,
     wie zwei Apps miteinander sprechen, und sie ist jederzeit widerrufbar.</p>
  {% if i.links %}
  <table class="mini">
    <tr><th>Diese App darf erreichen</th><th></th></tr>
    {% for t in i.links %}
    <tr>
      <td><code>{{ t }}</code></td>
      <td><form method="post" action="/instances/{{ i.name }}/link">
        <input type="hidden" name="tab" value="netz">
        <input type="hidden" name="op" value="remove">
        <input type="hidden" name="target" value="{{ t }}">
        <button class="secondary">Trennen</button>
      </form></td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">Keine Verbindung — diese App ist vollständig isoliert.</p>
  {% endif %}
  {% if i.link_candidates %}
  <form method="post" action="/instances/{{ i.name }}/link">
    <input type="hidden" name="tab" value="netz">
    <input type="hidden" name="op" value="add">
    <label>Verbindung erlauben zu
      <select name="target">
        {% for c in i.link_candidates %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
      </select>
    </label>
    <button>Verbinden</button>
    <p class="muted">Die Ziel-App sieht dadurch nur den vereinbarten Draht, nicht
       das Innenleben dieser App. Die Verbindung übersteht ein Neu-Ausrollen.</p>
  </form>
  {% endif %}
</div>
</section>

<section class="panel {{ 'active' if tab == 'deployment' }}">
{% if i.deploy_now %}
<div class="card">
  <h2>{% if i.deploy_now.state == "running" %}Deployment läuft
      {% elif i.deploy_now.state == "stale" %}Deployment abgebrochen
      {% else %}Deployment wartet{% endif %}</h2>
  <p>{% if i.deploy_now.state == "running" %}⏳ Seit
     <strong>{{ i.deploy_now.ago }}</strong> in Arbeit
     ({{ i.deploy_now.label }}). Das Ergebnis erscheint im Deploy-Protokoll
     auf der Gesundheitsseite.
     {% elif i.deploy_now.state == "stale" %}⚠️ Vor
     <strong>{{ i.deploy_now.ago }}</strong> angefangen
     ({{ i.deploy_now.label }}) und nie abgeschlossen — der Deploy-Worker ist
     mittendrin ausgefallen. Beim nächsten Deployment wird das als
     fehlgeschlagen verbucht; es läuft nichts mehr.
     {% else %}⏳ Seit <strong>{{ i.deploy_now.ago }}</strong> in der
     Warteschlange ({{ i.deploy_now.label }}) — der Deploy-Worker hat sie
     noch nicht angefasst.{% endif %}</p>
  {% if i.deploy_now.state == "queued" %}
  <form method="post" action="/instances/{{ i.name }}/deploy/cancel">
    <input type="hidden" name="tab" value="deployment">
    <input type="hidden" name="deployment" value="{{ i.deploy_now.id }}">
    <button class="secondary">Abbrechen</button>
  </form>
  {% elif i.deploy_now.state == "running" %}
  <p class="muted">Ein angelaufenes Deployment wird nicht abgebrochen — ein
     halb gebauter Stand ist schlimmer als warten. Es endet spätestens nach
     {{ i.deploy_limit }} Minuten, dann steht das Ergebnis im Protokoll.</p>
  {% endif %}
</div>
{% endif %}
{% if i.is_test %}
<div class="card">
  <h2>Deploy-Token</h2>
  {% if i.token_created %}
  <p>Ein Token für diese Instanz besteht seit <strong>{{ i.token_created }}</strong>.
     Der Wert selbst ist nirgends gespeichert — nur seine Prüfsumme. Wenn er
     verloren ging, erzeuge einen neuen; der alte gilt dann nicht mehr.</p>
  {% else %}
  <p class="muted">Für diese Instanz besteht kein Token.</p>
  {% endif %}
  <p class="muted">Adresse für den Hook (an die KI weitergeben, sie ist nicht
     geheim):<br><code>{{ i.hook_url }}</code></p>
  <form method="post" action="/instances/{{ i.name }}/token" style="display:inline">
    <input type="hidden" name="tab" value="deployment">
    <input type="hidden" name="op" value="create">
    <button>{{ "Neues Token erzeugen" if i.token_created else "Token erzeugen" }}</button>
  </form>
  {% if i.token_created %}
  <form method="post" action="/instances/{{ i.name }}/token" style="display:inline">
    <input type="hidden" name="tab" value="deployment">
    <input type="hidden" name="op" value="revoke">
    <button class="secondary">Widerrufen</button>
  </form>
  {% endif %}
  <p class="muted">Ein Deploy-Token erlaubt genau eines: diese Test-Instanz aus
     ihrer hinterlegten Quelle neu zu deployen. Keine Anmeldung, kein Zugriff
     auf Daten, keine Änderung an Routen oder Rollen. Produktiv-Instanzen
     bekommen grundsätzlich kein Token.</p>
  <p class="muted">Bringt das Deployment sein Paket selbst mit (RFC-0019), sind
     es zwei Schritte mit demselben Token:<br>
     <code>POST {{ i.hook_url }}/announce</code> mit Manifest, Prüfsumme und
     Größe — die Antwort enthält ein Einmal-Token —, dann
     <code>PUT {{ i.hook_url }}/artifact</code> mit dem Paket.</p>
  <p class="muted">Dauert der Bau länger als zwei Minuten, antwortet der Hook
     mit <code>202</code> und nennt im Feld <code>deployment</code> die
     Kennung dieses Deployments. Damit fragt die KI den Ausgang ab:<br>
     <code>GET {{ i.hook_url }}/status?deployment=&lt;Kennung&gt;</code> —
     die Antwort trägt <code>state</code> mit <code>queued</code>,
     <code>running</code> oder <code>done</code>. <strong>Ohne</strong>
     Kennung antwortet derselbe Aufruf über die Instanz, meldet aber ebenfalls
     ein noch laufendes Deployment als solches statt das vorige Ergebnis.<br>
     Ein Deployment, das noch wartet, lässt sich zurückziehen mit
     <code>POST {{ i.hook_url }}/cancel?deployment=&lt;Kennung&gt;</code>;
     ein angelaufenes nicht (RFC-0024).</p>
</div>
{% else %}
<div class="card">
  <h2>Deploy-Token</h2>
  <p class="muted">Diese Instanz läuft auf dem Kanal <strong>Produktiv</strong>
     und bekommt deshalb grundsätzlich kein Deploy-Token: Was hier läuft,
     wechselt über den Store, nicht auf Zuruf einer Maschine. Zum Erproben
     einer neuen Fassung gehört eine Test-Instanz.</p>
</div>
{% endif %}
{% if i.promote %}
<div class="card">
  <h2>Nach Produktiv übernehmen</h2>
  <p class="muted">Diese Test-Instanz läuft aus einem hochgeladenen Paket
     (Version <strong>{{ i.version }}</strong>). „Übernehmen" installiert
     <strong>genau dieses Paket</strong> in einer Produktiv-Instanz:
     <strong>dieselben Bytes</strong>, keine erneute Übertragung, keine
     Zugangsdaten. Was produktiv geht, ist damit nachweislich das, was Du
     getestet hast (RFC-0020).</p>
  <form method="post" action="/instances/{{ i.name }}/promote">
    <input type="hidden" name="tab" value="deployment">
    {% if i.promote.targets %}
    <label>Bestehende Produktiv-Instanz
      <select name="target">
        <option value="">— neue Instanz anlegen —</option>
        {% for t in i.promote.targets %}
        <option value="{{ t.name }}">{{ t.name }} (läuft {{ t.version }})</option>
        {% endfor %}
      </select>
    </label>
    {% endif %}
    <label>Oder neuer Name für die Produktiv-Instanz
      <input type="text" name="new_target" placeholder="{{ i.promote.suggestion }}"
             autocomplete="off"></label>
    <label class="checkline"><input type="checkbox" name="confirm" value="1">
      Rahmenerweiterung bestätigen — nur ankreuzen, wenn der erste Versuch
      eine gemeldet hat</label>
    <button>Übernehmen</button>
  </form>
  <p class="muted">Erweitert das Paket den Rahmen der Produktiv-Instanz, bricht
     der erste Versuch ab und <strong>nennt jeden Grund</strong>. Erst dann
     kreuzt Du an und übernimmst. So steht die Erweiterung vor der Zustimmung,
     nicht dahinter.</p>
  <p class="muted">Die Produktiv-Instanz behält dabei
     <strong>ihre eigenen Daten</strong>, ihre Konfigurationswerte, ihre
     Adresse, Gruppen und Freigaben — übernommen wird das Paket, sonst nichts.
     Aus dem Test wandert nichts mit. Die vorige Fassung bleibt aufgehoben:
     der Weg zurück ist der Rückschritt auf der Produktiv-Instanz.</p>
  <p class="muted">Produktiv nimmt nur eine <strong>höhere Version</strong> an,
     und eine Erweiterung des Rahmens (neue öffentliche Route, neuer Speicher,
     neuer Port) wird oben angezeigt und muss ausdrücklich bestätigt werden.
     Ein Deploy-Token bekommt die Produktiv-Instanz dadurch nicht.</p>
</div>
{% endif %}
{% if i.artifacts %}
<div class="card">
  <h2>Hochgeladene Pakete</h2>
  <p class="muted">Diese Instanz läuft aus einem hochgeladenen Paket. Es bleibt
     hier liegen — deshalb kann sie neu ausgerollt und zurückgesetzt werden,
     und deshalb ist ihr Backup vollständig.</p>
  <table>
    <tr><th>Paket</th><th>Empfangen</th><th></th></tr>
    {% for a in i.artifacts %}
    <tr>
      <td><code>{{ a.file }}</code>{% if a.running %}
          <span class="badge">in Betrieb</span>{% endif %}</td>
      <td>{{ a.received }}</td>
      <td>
        <form method="post" action="/instances/{{ i.name }}/rollback"
              style="display:inline">
          <input type="hidden" name="tab" value="deployment">
          <input type="hidden" name="artifact" value="{{ a.file }}">
          <button class="secondary">{{ "Erneut ausrollen" if a.running
                                       else "Hierauf zurück" }}</button>
        </form>
        {% if not a.running %}
        <form method="post" action="/instances/{{ i.name }}/artifact-delete"
              style="display:inline"
              onsubmit="return confirm('{{ a.file }} endgültig löschen? Auf dieses Paket kann danach nicht mehr zurückgesetzt werden.');">
          <input type="hidden" name="tab" value="deployment">
          <input type="hidden" name="artifact" value="{{ a.file }}">
          <button class="secondary">Löschen</button>
        </form>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  <p class="muted"><strong>In Betrieb</strong> heißt: aus diesem Paket läuft
     die Instanz gerade. „Erneut ausrollen" baut genau diese Bytes noch
     einmal — kein neues Paket, keine neue Übertragung. Gelöscht werden kann
     jedes Paket außer dem in Betrieb: Backup, Rückschritt und die Übernahme
     nach Produktiv lesen genau diese Datei.</p>
</div>
{% endif %}
</section>

<section class="panel {{ 'active' if tab == 'konfiguration' }}">
{% if i.config %}
<form method="post" action="/instances/{{ i.name }}/config">
  <input type="hidden" name="tab" value="konfiguration">
  <div class="card">
    <h2>Konfiguration</h2>
    {% for c in i.config %}
    <label>{{ c.label }}
      {% if c.multiline %}
      <textarea name="cfg-{{ c.key }}" rows="4" spellcheck="false"
                style="width:100%;font-family:ui-monospace,monospace;font-size:.9rem"
                placeholder="{{ ('gesetzt — leer lassen, um ihn zu behalten' if c.is_set else 'noch nicht gesetzt') if c.secret else 'eine Angabe je Zeile' }}">{{ c.value }}</textarea>
      {% elif c.secret %}
      <input type="password" name="cfg-{{ c.key }}" value="" autocomplete="new-password"
             placeholder="{{ 'gesetzt — leer lassen, um ihn zu behalten' if c.is_set else 'noch nicht gesetzt' }}">
      {% else %}
      <input type="text" name="cfg-{{ c.key }}" value="{{ c.value }}">
      {% endif %}
    </label>
    <p class="muted"><code>{{ c.key }}</code>{% if c.multiline %} — eine Angabe
       je Zeile{% endif %}{% if c.secret %} — vertraulich,
       wird nie angezeigt{% endif %}</p>
    {% endfor %}
    <p class="muted">Diese Werte deklariert die App in ihrem Manifest; andere
       lassen sich hier nicht setzen. Beim Speichern wird der Container mit
       den neuen Werten neu erzeugt — die App ist dabei kurz nicht
       erreichbar. Daten, Adresse und Version bleiben unverändert.</p>
    <button>Speichern</button>
  </div>
</form>
{% else %}
<div class="card">
  <h2>Konfiguration</h2>
  <p class="muted">Diese App erklärt in ihrem Manifest keine Konfigurationswerte
     — es gibt hier nichts einzustellen. Andere Werte lassen sich nicht
     nachreichen: Was eine App braucht, sagt sie selbst.</p>
</div>
{% endif %}
</section>

<section class="panel {{ 'active' if tab == 'verwaltung' }}">
<form method="post" action="/instances/{{ i.name }}/remove">
  <input type="hidden" name="tab" value="verwaltung">
  <div class="card">
    <h2>Instanz entfernen</h2>
    <p>Entfernt Container, Adresse und Kachel dieser Instanz. Der Vorgang
       lässt sich nicht rückgängig machen — eine erneute Installation legt
       eine frische Instanz an.</p>
    <label class="checkline"><input type="radio" name="purge" value=""
           checked>Daten behalten (liegen weiter auf dem Server und werden
           bei einer Neuinstallation gleichen Namens wiederverwendet)</label>
    <label class="checkline"><input type="radio" name="purge" value="1">Daten
           ebenfalls <strong>unwiderruflich löschen</strong></label>
    <label>Zum Bestätigen den Instanznamen eintippen: <code>{{ i.name }}</code>
      <input type="text" name="confirm" autocomplete="off" placeholder="{{ i.name }}"></label>
    <button class="secondary">Entfernen</button>
  </div>
</form>
</section>
"""

# Floorplan "Dialogseite": the one and only time the token is readable.
# Deliberately NOT a redirect — a Post/Redirect/Get would have to carry
# the token in the URL, and the gateway logs full URIs including their
# query string.
TOKEN_SHOW_BODY = """
<a class="back" href="/instances/{{ name }}">← Zurück zur Instanz</a>
<h1>Deploy-Token für {{ name }}</h1>
<div class="card">
  <p class="ok">Token erzeugt. <strong>Es wird nur dieses eine Mal
     angezeigt.</strong> Die Plattform speichert davon nur eine Prüfsumme —
     wir können es Dir später nicht noch einmal zeigen.</p>
  <p><code style="display:block;padding:.7rem;word-break:break-all;font-size:1.05rem">{{ token }}</code></p>
  <p>Weitergabe an die KI: über den vereinbarten Postkasten oder einen
     anderen Kanal, den nur ihr beide lest — <strong>nicht ins
     Repository, nicht in einen Brief, nicht in ein Ticket.</strong></p>
  <h2>So wird es benutzt</h2>
  <p class="muted">Nach jedem Push auf die hinterlegte Quelle:</p>
  <p><code style="display:block;padding:.7rem;word-break:break-all">curl -X POST {{ hook_url }} -H "Authorization: Bearer &lt;token&gt;"</code></p>
  <p class="muted">Antwort 202 heißt „läuft noch"; den Ausgang danach unter
     <code>{{ hook_url }}/status</code> abfragen.</p>
  <p><a href="/instances/{{ name }}">Fertig — zurück zur Instanz</a></p>
</div>
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
      <p class="muted" style="margin:1.2rem 0 .2rem">Wofür ist dieser Knoten da?</p>
      <label class="checkline"><input type="checkbox" name="profile_dev" value="1">
        Entwicklungsknoten (Profil <code>dev</code>)</label>
      <p class="muted" style="margin:.2rem 0 0">Auf einem Entwicklungsknoten
      darf das Portal Test-Instanzen anlegen — auch aus einem Repository,
      das in keiner Store-Liste steht. Für eine Maschine mit echten
      Daten: nicht ankreuzen. Später änderbar mit
      <code>sudo oaap node add-profile dev</code>.</p>
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


def caller_groups():
    """The caller's own visibility groups (RFC-0007).

    Deliberately NOT a header — the App Deployment Contract stays
    unchanged (no X-OAAP-Groups). Looked up from identity by the
    verified username instead, same trust boundary as roles.
    """
    username = request.headers.get("X-OAAP-User", "")
    if not username:
        return set()
    u = next((x for x in identity_users() if x["username"] == username), None)
    return set(u.get("groups") or []) if u else set()


# Accounts and tenants of this node (oaap.core.tenant). The portal
# READS both files and writes neither: the registry mount is read-only,
# and the audit log is mounted read-only on purpose -- the portal shows
# it, appctl and identity write it.
TENANTS_FILE = "/apps-registry/tenants.json"
AUDIT_LOG = "/audit/tenant-log.jsonl"


def caller_name():
    return request.headers.get("X-OAAP-User", "")


def load_tenants():
    try:
        with open(TENANTS_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("tenants") or {}
    except (OSError, ValueError):
        return {}


def default_tenant_id():
    for tid, t in sorted(load_tenants().items()):
        if t.get("label") == "default":
            return tid
    return ""


def resolve_tenant(ref):
    """Absent means the default tenant; UNKNOWN never does (spec 2.5)."""
    ref = (ref or "").strip()
    if not ref:
        return default_tenant_id() or ""
    return ref if ref in load_tenants() else None


def tenant_label(tid):
    return (load_tenants().get(tid or "") or {}).get("label", "")


def tenant_name(tid):
    t = load_tenants().get(tid or "") or {}
    return t.get("name") or t.get("label", "")


def multi_tenant():
    """Whether this node uses tenants at all.

    Everything tenant-shaped in this file asks first. On a node with one
    tenant nothing may appear: no column, no menu entry, no sentence.
    That is not politeness, it is the acceptance criterion the
    capability was built against.
    """
    return len(load_tenants()) > 1


def caller_record():
    """The calling user's own record, from identity.

    Scoped by identity itself: a caller who is no kind of administrator
    gets exactly one record back, their own.
    """
    name = caller_name()
    if not name:
        return None
    return next((u for u in identity_users() if u["username"] == name), None)


def caller_scope():
    """(role, tenant) for the caller -- the two facts every page needs.

    role is "server_admin" (the node), "tenant_admin" (one tenant) or ""
    (neither). The tenant always comes from the caller's OWN record, so
    there is no request in which a caller can name a different one.
    """
    roles = caller_roles()
    u = caller_record() or {}
    mine = resolve_tenant(u.get("tenant")) or ""
    if "server_admin" in roles:
        return "server_admin", mine
    if "tenant_admin" in roles:
        return "tenant_admin", mine
    return "", mine


def require_user_admin():
    """User administration: the node's administrator, or a tenant's."""
    if not (caller_roles() & {"server_admin", "tenant_admin"}):
        return ("Zugriff verweigert: erfordert die Rolle server_admin oder "
                "tenant_admin."), 403
    return None


def visible_instances():
    """The instances this caller may administer.

    A server_admin sees the node. A tenant_admin sees their own tenant
    and learns nothing about the existence of any other -- not a count,
    not a name. Everyone else sees nothing here; the launchpad is a
    different question and asks it separately.
    """
    role, mine = caller_scope()
    everything = load_instances()
    if role == "server_admin":
        return everything
    if role != "tenant_admin":
        return {}
    return {n: i for n, i in everything.items()
            if (resolve_tenant(i.get("tenant")) or "") == mine}


def read_audit(tenant=None, limit=200):
    """The tenant audit log (oaap.core.tenant 1.7), newest first.

    A damaged line is skipped rather than fatal: this is a record, and
    a record that refuses to be read because of one bad byte protects
    nobody.
    """
    out = []
    try:
        with open(AUDIT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if tenant is not None and e.get("tenant") != tenant:
                    continue
                out.append(e)
    except OSError:
        return []
    return list(reversed(out[-limit:]))


def page(body_template, title, active, status=200, **ctx):
    body = render_template_string(body_template, **ctx)
    roles = request.headers.get("X-OAAP-Roles", "")
    caller = caller_roles()
    multi = multi_tenant()
    return render_template_string(
        LAYOUT,
        title=title, active=active, body=Markup(body), logo=LOGO_SVG,
        user=request.headers.get("X-OAAP-User", "?"), roles=roles or "?",
        is_server_admin="server_admin" in caller,
        # Who may reach user administration and the instance list — the
        # node's administrator or a tenant's. On a single-tenant node
        # nobody holds tenant_admin, so this reads exactly as before.
        is_user_admin=bool(caller & {"server_admin", "tenant_admin"}),
        show_tenant=multi,
        can_health=bool(caller & {"server_admin", "partner"}),
        version=VERSION,
    ), status


def load_instances():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f).get("instances", {})
    except (OSError, ValueError):
        return {}


EXTERNAL_FILE = "/apps-registry/external.json"
EDGE_FILE = "/apps-registry/edge.json"
NODE_FILE = "/apps-registry/node.json"
ACCESS_LOG = "/gateway-logs/external-access.log"

# What this node is for (RFC-0011). The portal READS profiles and never
# writes them — the one exception is the first-run wizard, which is
# authorised by the setup token. Anything else would defeat the point:
# a profile that the portal can grant itself is not a per-node decision.
# ACHTUNG: Diese Tabelle muss dieselben Schlüssel tragen wie `PROFILES`
# in appctl.py — `node_profiles()` verwirft alles, was hier fehlt. Ein
# hier vergessenes Profil ist deshalb kein Schönheitsfehler: Der Knoten
# hat es, das Portal sieht es nicht, und jede App, die es erwartet,
# bleibt im Store gefiltert. `test_store_view.py` prüft den Abgleich.
PROFILE_LABELS = {
    "dev": "Entwicklungsknoten — das Portal darf Test-Instanzen anlegen "
           "und aus einer noch nicht gelisteten Quelle installieren",
    "exposed": "Exponierter Knoten — der Betreiber darf einer App einen "
               "Nicht-HTTP-Port am Gateway vorbei freigeben (RFC-0015). "
               "Sinnvoll nur, wo die Portweiterleitung des Routers steht.",
}


def node_profiles():
    try:
        with open(NODE_FILE, encoding="utf-8") as f:
            stored = json.load(f).get("profiles") or []
    except (OSError, ValueError):
        return []
    return sorted(p for p in stored if p in PROFILE_LABELS)


def external_host():
    try:
        with open(EXTERNAL_FILE, encoding="utf-8") as f:
            return json.load(f).get("host", "")
    except (OSError, ValueError):
        return ""


@app.get("/edge/tls-ask")
def edge_tls_ask():
    """Approve on-demand TLS for edge-routed names (gateway spec, edge).

    The edge terminates TLS for foreign platforms' hostnames, but their
    app subdomains are unknown here and wildcard certificates are not
    obtainable via HTTP challenge — so the gateway asks per requested
    name at handshake time. Approved: a routed hostname or any name
    below it. Only the gateway can reach this endpoint (internal
    network; /edge/* is no declared client route).
    """
    domain = (request.args.get("domain") or "").lower().strip(".")
    if not domain:
        return "missing domain", 400
    try:
        with open(EDGE_FILE, encoding="utf-8") as f:
            routes = json.load(f).get("routes", [])
    except (OSError, ValueError):
        routes = []
    for r in routes:
        h = r.get("host", "")
        if h and (domain == h or domain.endswith("." + h)):
            return "ok", 200
    return "not an edge-routed name", 404


# Whether an installed instance belongs on the launchpad is an RFC
# decision (RFC-0012 §1.2 with its §1.3 addendum, oaap.apps.runtime
# 2.10), so it lives outside this file for the same reason the catalogue
# rules do — readable and testable without Flask. Imported as a module:
# it carries a CLASS_LABEL of its own, about instances rather than store
# entries.
import instance_view as iv  # noqa: E402


def launchpad_tiles(user_roles, user_groups, host, user_tenant=None):
    """Role- and group-filtered app tiles from the instance registry
    (spec 2.5, RFC-0007). The filter is UX only — the gateway enforces
    both on every request regardless of what the portal shows (mirrored
    exactly: no bypass here that the gateway's /verify does not also
    grant, and vice versa — server_admin bypasses the group check only,
    same as /verify).

    Returns (tiles, hidden): `hidden` counts the instances this caller
    passed both filters for but which carry no tile (runtime spec 2.10).
    The launchpad reports that number to a server_admin, because a page
    that is empty for a good reason looks exactly like one that is empty
    for a bad one.
    """
    # Where is the caller? The LAN listener ports only work from inside;
    # from outside only 80/443 are forwarded. Deciding this by "is the
    # host exactly our external name" was too narrow: a platform can be
    # entered under any public name that resolves here — an instance's
    # own hostname (RFC-0009), an operator's CNAME — and every one of
    # those got LAN-port links that are dead from outside. Ask the
    # opposite question instead: does this look like a LAN address?
    ext = external_host()
    on_lan = not ext or _looks_like_lan(host)
    is_server_admin = "server_admin" in user_roles
    tiles, hidden = [], 0
    for name, inst in sorted(load_instances().items()):
        # The tenant boundary, before role and group: an app of another
        # tenant is not "hidden" from this caller, it is none of their
        # business, and the gateway would refuse them anyway (spec 3.1).
        # A server_admin sees the node, as everywhere (RFC-0022 D5).
        if (user_tenant is not None and not is_server_admin
                and (resolve_tenant(inst.get("tenant")) or "") != user_tenant):
            continue
        allowed = set(inst.get("roles") or [])
        if allowed and not user_roles & allowed:
            continue
        vis_groups = set((inst.get("visibility") or {}).get("groups") or [])
        if vis_groups and not is_server_admin and not user_groups & vis_groups:
            continue
        # Counted AFTER the two filters above, never before: the number
        # must not tell a caller that instances exist which they may not
        # see anyway. Display only — nothing here grants or denies access.
        if not iv.tile_visible(inst):
            hidden += 1
            continue
        channel = inst.get("channel", "production")
        tiles.append({
            "name": inst.get("app_name", name),
            "instance": name,
            "version": inst.get("version", "?"),
            "channel": channel,
            "channel_label": CHANNEL_LABELS.get(channel, channel),
            "description": inst.get("description", ""),
            "url": _tile_url(name, inst, host, ext, on_lan),
        })
    return tiles, hidden


_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".oaap.internal")


def _looks_like_lan(host):
    """Rough check: is this host a LAN-only way to reach the node?

    A bare IP address or a single-label name (`oaap-demo`) can only come
    from inside; anything with a public-looking domain is treated as
    "from outside", where LAN listener ports are not forwarded. Erring
    towards "outside" is the safe direction: an external link shown on
    the LAN still works, the reverse does not.
    """
    host = host.lower()
    if host.startswith("[") or ":" in host:          # IPv6 literal
        return True
    if host.replace(".", "").isdigit():              # IPv4 literal
        return True
    return "." not in host or host.endswith(_LAN_SUFFIXES)


def _tile_url(name, inst, host, ext, on_lan):
    if on_lan:
        return f"http://{host}:{inst['port']}/"
    # entered from outside: prefer the instance's own public hostname
    # (RFC-0009), otherwise its subdomain of this node
    if inst.get("address"):
        return f"https://{inst['address']}/"
    return f"https://{name}.{ext}/"


def setup_done() -> bool:
    return INTERNAL.get(f"{IDENTITY}/internal/status", timeout=5).json()["setup_done"]


@app.get("/")
def dashboard():
    roles = caller_roles()
    # Which tenant this caller belongs to — asked once, here, and only
    # where it can change what is shown. On a node with one tenant it
    # is the same answer for everybody and changes nothing.
    mine = caller_scope()[1] if multi_tenant() else None
    tiles, hidden = launchpad_tiles(roles, caller_groups(),
                                    request.host.split(":")[0],
                                    user_tenant=mine)
    # Only a server_admin is told about tileless instances: they are the
    # only ones who can do anything about it, and everybody else would
    # be told to miss something they were never meant to operate.
    return page(DASHBOARD_BODY, "Apps", "apps", tiles=tiles,
                hidden_count=hidden if "server_admin" in roles else 0)


# ---------------------------------------------------------------------------
# User management (spec oaap.core.identity 2.4) — server_admin for the
# node, tenant_admin for one tenant (RFC-0008 + oaap.core.tenant 2.3),
# floorplans "Listenbericht" and "Objektseite". The gateway has already
# authenticated the caller; the portal checks the role and delegates the
# operations to identity's internal API — which checks the tenant
# boundary again, on its own store, because that is where the actor's
# record is and a rule kept in two places eventually disagrees.

def require_instance_admin(name):
    """The node's administrator, or the tenant_admin of THIS instance.

    An instance of another tenant is answered as one that does not
    exist. "Forbidden" would confirm that the name is taken on this
    node, and that is already an answer across the boundary
    (oaap.core.tenant 2.3 rule 2).
    """
    if "server_admin" in caller_roles():
        return None
    if "tenant_admin" not in caller_roles():
        return ("Zugriff verweigert: erfordert die Rolle server_admin oder "
                "tenant_admin."), 403
    if name in visible_instances():
        return None
    return "Instanz nicht gefunden.", 404


def identity_users():
    """The users this caller may see — identity does the filtering.

    The actor travels with the request because identity, not the portal,
    owns the boundary: a tenant_admin gets their tenant, a server_admin
    gets the node, anybody else gets their own record. Filtering here
    instead would put the same rule in two places, and the day the two
    disagree the more generous one wins.
    """
    name = caller_name()
    if not name:
        # No verified caller (a public surface): there is nobody to
        # scope to, and asking identity would only get a 400.
        return []
    # Cached for the duration of ONE request: several helpers on the
    # same page ask this question, and the answer cannot change halfway
    # through rendering.
    cached = g.get("identity_users")
    if cached is None:
        cached = INTERNAL.get(f"{IDENTITY}/internal/users",
                              params={"actor": name},
                              timeout=5).json().get("users", [])
        g.identity_users = cached
    return cached


def _parse_groups(raw):
    """Free-form group tags (RFC-0007) from a comma-separated form field."""
    return sorted({g.strip().lower() for g in raw.split(",") if g.strip()})


def _role_choices():
    """Which roles this caller may hand out.

    A tenant_admin never sees the node-wide roles in the list — not as
    a disabled checkbox either. Identity refuses them regardless (spec
    2.3 rule 1); leaving them out of the form is so that nobody is
    invited to try.
    """
    if "server_admin" in caller_roles():
        return ALL_ROLES
    return tuple(r for r in ALL_ROLES if r not in NODE_WIDE_ROLES)


def _tenant_choices():
    """The tenants a new user may be created into.

    Empty on a single-tenant node and for a tenant_admin: in both cases
    there is exactly one answer and no question to ask. Only a
    server_admin on a node with several tenants gets a choice, and
    identity checks the answer again.
    """
    role, _mine = caller_scope()
    if role != "server_admin" or not multi_tenant():
        return []
    return sorted(({"id": tid, "label": t.get("label", ""),
                    "name": t.get("name") or t.get("label", "")}
                   for tid, t in load_tenants().items()),
                  key=lambda t: t["label"])


def _new_user_form():
    return {"username": "", "display_name": "", "roles": ["user"],
            "groups": [], "tenant": ""}


@app.get("/users")
def users_list():
    denied = require_user_admin()
    if denied:
        return denied
    role, mine = caller_scope()
    users = identity_users()
    return page(USERS_LIST_BODY, "Benutzer", "users", users=users,
                # Named only where there is more than one to name.
                show_tenant=multi_tenant() and role == "server_admin",
                labels={t: tenant_label(t) for t in load_tenants()},
                default_tenant=default_tenant_id(),
                scope_note=(tenant_name(mine) if role == "tenant_admin"
                            and multi_tenant() else ""),
                msg=request.args.get("msg"), error=request.args.get("err"))


@app.get("/users/new")
def users_new():
    denied = require_user_admin()
    if denied:
        return denied
    return page(USER_NEW_BODY, "Benutzer anlegen", "users",
                all_roles=_role_choices(), tenants=_tenant_choices(),
                error=None, form=_new_user_form())


@app.post("/users/create")
def users_create():
    denied = require_user_admin()
    if denied:
        return denied
    form = {
        "username": request.form.get("username", "").strip(),
        "display_name": request.form.get("display_name", ""),
        "roles": request.form.getlist("roles"),
        "groups": _parse_groups(request.form.get("groups", "")),
        "tenant": request.form.get("tenant", ""),
    }
    # The tenant travels as a WISH. Identity decides: a server_admin may
    # name one, anybody else is put in their own whatever this form
    # says. The rule lives there, once, where the actor's own record is.
    resp = INTERNAL.post(f"{IDENTITY}/internal/users", json={
        **form, "actor": caller_name(),
        "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 201:
        created = quote("Benutzer " + form["username"] + " wurde angelegt.")
        return redirect(f"/users?msg={created}", code=303)
    # Validation error: stay on the page, keep the inputs (guidelines 6.2)
    return page(USER_NEW_BODY, "Benutzer anlegen", "users", status=resp.status_code,
                all_roles=_role_choices(), tenants=_tenant_choices(), form=form,
                error=resp.json().get("error", "Anlegen fehlgeschlagen."))


@app.get("/users/<username>")
def users_detail(username):
    denied = require_user_admin()
    if denied:
        return denied
    # identity_users() is already scoped to what this caller may see, so
    # a user of another tenant is simply not in the list — the same
    # answer as one who does not exist, deliberately.
    u = next((x for x in identity_users() if x["username"] == username), None)
    if not u:
        return redirect(f"/users?err={quote('Benutzer nicht gefunden.')}", code=303)
    return page(USER_EDIT_BODY, f"Benutzer {username}", "users", u=u,
                all_roles=_role_choices(),
                tenant_of=(tenant_name(resolve_tenant(u.get("tenant")))
                           if multi_tenant() else ""),
                msg=request.args.get("msg"), error=request.args.get("err"))


@app.post("/users/<username>/update")
def users_update(username):
    denied = require_user_admin()
    if denied:
        return denied
    resp = INTERNAL.put(f"{IDENTITY}/internal/users/{username}", json={
        "display_name": request.form.get("display_name", ""),
        "roles": request.form.getlist("roles"),
        "groups": _parse_groups(request.form.get("groups", "")),
        "active": request.form.get("active") == "on",
        "actor": caller_name(),
    }, timeout=5)
    if resp.status_code == 200:
        return redirect(f"/users/{username}?msg={quote('Gespeichert.')}", code=303)
    return redirect(f"/users/{username}?err={quote(resp.json().get('error', 'Speichern fehlgeschlagen.'))}", code=303)


@app.post("/users/<username>/password")
def users_password(username):
    denied = require_user_admin()
    if denied:
        return denied
    resp = INTERNAL.post(f"{IDENTITY}/internal/users/{username}/password", json={
        "password": request.form.get("password", ""),
        "actor": caller_name(),
    }, timeout=5)
    if resp.status_code == 200:
        return redirect(f"/users/{username}?msg={quote('Passwort wurde gesetzt.')}", code=303)
    return redirect(f"/users/{username}?err={quote(resp.json().get('error', 'Passwort setzen fehlgeschlagen.'))}", code=303)


# --- the tenant page (oaap.core.tenant 2.1/1.7) ----------------------------
# Two readings of one page: a tenant_admin sees their own tenant and its
# log; a server_admin sees every tenant and every entry. Nobody sees a
# tenant they are not in — not its name, not its size, not that it
# exists.

@app.get("/tenant")
def tenant_page():
    denied = require_user_admin()
    if denied:
        return denied
    if not multi_tenant():
        # Typed by hand on a node that has no tenants in use. Saying
        # "there is nothing here" is the honest answer and keeps the
        # invisibility rule intact.
        return redirect("/", code=303)
    role, mine = caller_scope()
    tenants = load_tenants()
    users = identity_users()
    instances = load_instances()

    def counts(tid):
        return (sum(1 for u in users
                    if (resolve_tenant(u.get("tenant")) or "") == tid),
                sum(1 for i in instances.values()
                    if (resolve_tenant(i.get("tenant")) or "") == tid))

    if role == "server_admin":
        rows = []
        for tid, t in sorted(tenants.items(), key=lambda kv: kv[1].get("label", "")):
            n_users, n_inst = counts(tid)
            rows.append({"label": t.get("label", "?"),
                         "name": t.get("name") or "—",
                         "created": t.get("created", "?"),
                         "users": n_users, "instances": n_inst})
        return page(TENANT_BODY, "Mandanten", "tenant", tenants=rows, me=None,
                    is_server_admin=True, host=external_host(),
                    entries=read_audit())
    t = tenants.get(mine) or {}
    n_users, n_inst = counts(mine)
    me = {"label": t.get("label", "?"), "name": t.get("name") or t.get("label", "?"),
          "created": t.get("created", "?"), "users": n_users, "instances": n_inst}
    # The tenant is taken from the caller's own record, so the log they
    # get is theirs by construction — there is no parameter to tamper
    # with and therefore no other tenant's log to ask for.
    return page(TENANT_BODY, "Mandant", "tenant", tenants=[], me=me,
                is_server_admin=False, host=external_host(),
                entries=read_audit(mine))


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
    # RFC-0011: nobody should have to guess why this node behaves
    # differently from its neighbour.
    profiles = node_profiles()
    rows.append({
        "name": "Knoten-Profil", "state": "ok",
        "label": ", ".join(profiles) if profiles else "(keins)",
        "detail": "; ".join(PROFILE_LABELS[p] for p in profiles) if profiles
                  else "verhält sich wie ein normaler Produktivknoten; "
                       "gesetzt wird das auf der Maschine mit "
                       "'sudo oaap node add-profile'",
    })
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


def external_access():
    """Registered external hostname + the most recent gateway hit."""
    host = external_host()
    if not host:
        return None
    info = {"host": host, "last": None}
    try:
        with open(ACCESS_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
    except OSError:
        return info
    for line in reversed(lines):
        try:
            e = json.loads(line)
            when = datetime.fromtimestamp(float(e["ts"]), timezone.utc)
        except (ValueError, TypeError, KeyError):
            continue
        req = e.get("request", {})
        info["last"] = {
            "when": when.strftime("%d.%m.%Y %H:%M:%S UTC"),
            "host": req.get("host", "?"),
            "ip": req.get("remote_ip", "?"),
            "status": e.get("status", "?"),
        }
        break
    return info


# --------------------------------------------------------------------------
# Do the published names still point here? (RFC-0009 decision 2)
#
# The DuckDNS incident is what this is for: a node was off the internet
# for days and nothing said so — its DynDNS entry quietly kept an old
# address. With an instance address baked into shipped clients
# (RFC-0009) the consequence is worse.
#
# The price is stated rather than hidden: to compare, the node must ask
# an OUTSIDE service for its own public address. So this runs only when
# the node actually publishes a name — a LAN-only platform never
# reaches out, and the installer's promise of an offline-capable
# platform stands. Which service was asked is shown on the page.

DNS_CACHE = "/deploy-spool/.dns-check.json"  # SPOOL_DIR, defined further down
DNS_CHECK_TTL = 1800  # seconds; one outside request per half hour at most
PUBLIC_IP_SERVICES = ("https://api.ipify.org", "https://checkip.amazonaws.com")


def published_names():
    """Names this node has in DNS, with their origin — the node hostname
    plus every instance's REGISTERED canonical name and its aliases
    (RFC-0018).

    Deliberately without the automatic names `<instance>.<node>`: they
    are served by the wildcard record that already covers the node
    hostname, so a lookup on one of them answers for every name under
    it — including names nobody ever installed. There is nothing to
    compare, and a green verdict would be worthless. What can actually
    fail for an automatic name is the ROUTE, and that is reported per
    instance as `auto_state` in the fleet document (spec 0.3, rule 7).
    """
    names = []
    host = external_host()
    if host:
        names.append({"name": host, "what": "Knoten"})
    for inst_name, inst in sorted(load_instances().items()):
        canon = inst.get("address")
        if not canon:
            continue
        names.append({"name": canon, "what": f"Instanz {inst_name}"})
        for alias in inst.get("aliases") or []:
            if alias and alias != canon:
                names.append({"name": alias,
                              "what": f"Instanz {inst_name} (Alias)"})
    return names


def _behind_edge():
    try:
        with open(EXTERNAL_FILE, encoding="utf-8") as f:
            return json.load(f).get("edge", "")
    except (OSError, ValueError):
        return ""


def _public_ip():
    for url in PUBLIC_IP_SERVICES:
        try:
            r = requests.get(url, timeout=4)
            r.raise_for_status()
            ip = r.text.strip()
            if _re.fullmatch(r"[0-9.]{7,15}", ip):
                return ip, url
        except (requests.RequestException, ValueError):
            continue
    return "", ""


def _resolve(name):
    """All addresses, IPv4 AND IPv6 (AF_UNSPEC). A name that resolves only
    over IPv6 — e.g. a Fritzbox rebind-protected CNAME seen from inside the
    LAN, which strips the IPv4 answer — must not read as 'does not resolve'.
    That IPv4-only false negative is exactly what this dual-stack lookup
    fixes (RFC-0009 follow-up)."""
    import socket
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_UNSPEC)})
    except OSError:
        return []


def _dns_check_run():
    names = published_names()
    now = datetime.now(timezone.utc)
    result = {"when": now.isoformat(), "rows": [], "public_ip": "",
              "source": "", "note": ""}
    if not names:
        return result
    if _behind_edge():
        # The names resolve to the EDGE's public address, which this
        # node cannot know. Saying "unknown" is honest; guessing is not.
        result["note"] = ("Dieser Knoten steht hinter einem Edge-Knoten — die "
                          "veröffentlichten Namen zeigen auf dessen öffentliche "
                          "Adresse, die von hier aus nicht feststellbar ist. "
                          "Geprüft wird nur, ob sie überhaupt auflösen.")
    else:
        result["public_ip"], result["source"] = _public_ip()
    for entry in names:
        ips = _resolve(entry["name"])
        v4 = [a for a in ips if ":" not in a]
        row = dict(entry, resolved=", ".join(ips) or "–")
        if not ips:
            row["state"], row["label"] = "err", "Löst nicht auf"
        elif result["note"]:
            row["state"], row["label"] = "unknown", "Löst auf"
        elif not result["public_ip"]:
            row["state"], row["label"] = "unknown", "Nicht vergleichbar"
        elif result["public_ip"] in ips:
            row["state"], row["label"] = "ok", "Zeigt hierher"
        elif not v4:
            # Resolves only over IPv6. Our public address is IPv4 (ipify),
            # so there is nothing to compare against — say so honestly
            # instead of raising a false "points elsewhere" alarm.
            row["state"], row["label"] = "unknown", "Nur IPv6 (nicht vergleichbar)"
        else:
            row["state"], row["label"] = "warn", "Zeigt woanders hin"
        result["rows"].append(row)
    return result


def dns_check():
    """Cached verdict; refreshed at most every DNS_CHECK_TTL seconds."""
    try:
        age = _time.time() - os.path.getmtime(DNS_CACHE)
        if age < DNS_CHECK_TTL:
            with open(DNS_CACHE, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    result = _dns_check_run()
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        tmp = DNS_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f)
        os.replace(tmp, DNS_CACHE)
    except OSError:
        pass
    return result


# --------------------------------------------------------------------------
# Do the granted direct ports still reach here? (RFC-0015 decision Q4)
#
# The sibling of the DNS watchdog above, and it shares that mechanism as
# RFC-0015 asked: RFC-0009 asks "does the published NAME still point here",
# this asks "does the published PORT still reach here". The forgotten
# router forward is the failure it exists for — silent, and until now
# indistinguishable from a broken app.
#
# Two honest limits, both stated rather than hidden:
#   * A home router that does not hairpin (loop a connection to its own
#     public address back inside) makes a working forward look unreachable
#     FROM HERE. So a failed probe is never a red "broken" — it is a grey
#     "could not confirm from here". Only a SUCCESS is asserted.
#   * This is stage 1 (RFC-0015): the probe runs from the node itself, so a
#     router that answers the public address locally can show "reachable"
#     even with no forward. The stage-2 reflector (an outside vantage) is
#     the one that removes both caveats. The page says which one produced
#     the verdict.
#
# UDP cannot be confirmed by a bare datagram — answered and dropped look
# the same. For UDP we send a STUN binding request (RFC 5389): every WebRTC
# media server (LiveKit, mediasoup, coturn) answers it by standard, which
# is exactly what a declared UDP endpoint carries. A STUN reply is real
# proof; its absence is the same grey "not confirmed".

REACH_CACHE = "/deploy-spool/.reach-check.json"
REACH_CHECK_TTL = 1800  # seconds; matches the DNS watchdog's outside cadence
REACH_TIMEOUT = 3  # seconds per probe


def granted_endpoints():
    """Direct ports handed to the world, with instance and protocol."""
    eps = []
    for inst_name, inst in sorted(load_instances().items()):
        for ep in inst.get("endpoints") or []:
            if ep.get("host_port"):
                eps.append({"instance": inst_name, "name": ep.get("name", ""),
                            "protocol": ep.get("protocol", "tcp"),
                            "host_port": ep["host_port"]})
    return eps


def _probe_tcp(ip, port):
    """True only if a connection completes — a fact that cannot be faked."""
    import socket
    try:
        with socket.create_connection((ip, port), timeout=REACH_TIMEOUT):
            return True
    except OSError:
        return False


def _probe_stun(ip, port):
    """Send a STUN binding request; a reply on the transaction proves the
    UDP port reaches a WebRTC media server. RFC 5389: 20-byte header, type
    0x0001, magic cookie 0x2112A442, 12-byte transaction id echoed back."""
    import socket
    import struct
    txid = os.urandom(12)
    req = struct.pack(">HHI", 0x0001, 0, 0x2112A442) + txid
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(REACH_TIMEOUT)
    try:
        s.sendto(req, (ip, port))
        data, _ = s.recvfrom(1024)
    except OSError:
        return False
    finally:
        s.close()
    if len(data) < 20:
        return False
    _mtype, _mlen, cookie = struct.unpack(">HHI", data[:8])
    # Any answer carrying our magic cookie and transaction id is a STUN
    # speaker on that port — success or error class alike proves reach.
    return cookie == 0x2112A442 and data[8:20] == txid


def _reach_check_run():
    eps = granted_endpoints()
    now = datetime.now(timezone.utc)
    result = {"when": now.isoformat(), "rows": [], "public_ip": "",
              "source": "", "vantage": "self", "note": ""}
    if not eps:
        return result
    if _behind_edge():
        result["note"] = ("Dieser Knoten steht hinter einem Edge-Knoten — die "
                          "direkten Ports werden dort weitergeleitet und sind "
                          "von hier aus nicht prüfbar.")
        for ep in eps:
            result["rows"].append(dict(ep, state="unknown", label="Nicht prüfbar"))
        return result
    result["public_ip"], result["source"] = _public_ip()
    if not result["public_ip"]:
        for ep in eps:
            result["rows"].append(dict(ep, state="unknown", label="Nicht vergleichbar"))
        return result
    ip = result["public_ip"]
    for ep in eps:
        proto, port = ep["protocol"], ep["host_port"]
        ok_tcp = _probe_tcp(ip, port) if proto in ("tcp", "both") else False
        ok_udp = _probe_stun(ip, port) if proto in ("udp", "both") else False
        if ok_tcp or ok_udp:
            how = "STUN" if ok_udp and not ok_tcp else "TCP"
            result["rows"].append(dict(ep, state="ok", label="Erreichbar", how=how))
        else:
            result["rows"].append(dict(ep, state="unknown",
                                       label="Von hier nicht bestätigt", how=""))
    return result


def reach_check():
    """Cached verdict; refreshed at most every REACH_CHECK_TTL seconds."""
    try:
        age = _time.time() - os.path.getmtime(REACH_CACHE)
        if age < REACH_CHECK_TTL:
            with open(REACH_CACHE, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    result = _reach_check_run()
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        tmp = REACH_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f)
        os.replace(tmp, REACH_CACHE)
    except OSError:
        pass
    return result


def braked_requests():
    """Per-instance count of throttled requests (RFC-0010 decision 2)."""
    instances = load_instances()
    public = {n for n, i in instances.items()
              if any("public" in (r.get("roles") or []) for r in (i.get("routes") or []))}
    if not public:
        return None
    try:
        data = INTERNAL.get(f"{IDENTITY}/internal/throttle-braked", timeout=3).json()
    except (requests.RequestException, ValueError):
        return {"hours": 24, "rows": [], "error": True}
    counts = data.get("instances", {})
    rows = []
    for name in sorted(public):
        c = counts.get(name, {})
        rows.append({"instance": name, "count": c.get("count", 0),
                     "last": datetime.fromtimestamp(c["last_hour"] * 3600, timezone.utc)
                                     .strftime("%d.%m.%Y %H:00 UTC") if c else "–"})
    return {"hours": data.get("hours", 24), "rows": rows, "error": False}


def _probe(url, ok_status=200, via=requests):
    """Health probe. `via` is the keyed session for /internal/* targets —
    without it identity answers 401 and a healthy node reads as broken."""
    try:
        r = via.get(url, timeout=2, allow_redirects=False)
    except requests.RequestException as e:
        return "err", "Nicht erreichbar", type(e).__name__
    if r.status_code == ok_status:
        return "ok", "Gesund", f"HTTP {r.status_code}"
    return "warn", "Antwortet unerwartet", f"HTTP {r.status_code}"


def _core_states():
    """Core-service probes — shared by the health page and /fleet/status."""
    core = []
    state, label, detail = _probe(f"{IDENTITY}/internal/status", via=INTERNAL)
    core.append({"name": "Identity", "state": state, "label": label, "detail": detail})
    # Full chain: gateway proxies the login page to identity.
    state, label, detail = _probe(f"{GATEWAY}/auth/login")
    core.append({"name": "Gateway", "state": state, "label": label, "detail": detail})
    core.append({"name": "Portal", "state": "ok", "label": "Gesund",
                 "detail": "liefert diese Seite"})
    core.append(deploy_worker_state())
    return core


def _instance_probe(name, inst):
    """State/label/detail of one instance — shared by the health page
    and /fleet/status.

    RFC-0016: apps are isolated on their own networks and the portal
    can no longer reach them by container name. The gateway is the
    one core service on every app network, so we probe THROUGH it,
    via its internal health endpoint (appctl write_internal_health_
    caddy, gateway:8099/h/<name> -> the app's health path, no auth).
    """
    container, svc_port = inst.get("container"), inst.get("svc_port")
    health_path = inst.get("health_path")
    if container and svc_port and health_path:
        state, label, detail = _probe(f"{GATEWAY_HEALTH}/h/{name}")
        # Wrapped apps often answer their root with a redirect —
        # any response below 400 counts as alive.
        if state == "warn" and detail.startswith("HTTP 3"):
            state, label = "ok", "Gesund"
    elif container and svc_port:
        state, label, detail = _probe(f"{GATEWAY_HEALTH}/h/{name}")
        if state == "warn":  # any HTTP answer counts as reachable here
            state, label = "ok", "Erreichbar"
        if state == "ok":
            detail += ", App ohne erfassten Healthcheck"
    else:
        state, label = "unknown", "Unbekannt"
        detail = "vor der Gesundheits-Erfassung installiert — bei erneutem 'oaap app install' verfügbar"
    return state, label, detail


@app.get("/health")
def health():
    if not caller_roles() & {"server_admin", "partner"}:
        return "Zugriff verweigert: Gesundheit erfordert die Rolle server_admin oder partner.", 403

    core = _core_states()

    apps = []
    for name, inst in sorted(load_instances().items()):
        state, label, detail = _instance_probe(name, inst)
        channel = inst.get("channel", "production")
        apps.append({
            "name": inst.get("app_name", name), "instance": name,
            "version": inst.get("version", "?"), "channel": channel,
            "channel_label": CHANNEL_LABELS.get(channel, channel),
            "state": state, "label": label, "detail": detail,
        })
    return page(HEALTH_BODY, "Gesundheit", "health", node=node_values(),
                core=core, apps=apps, ext=external_access(),
                dns=dns_check(), reach=reach_check(), braked=braked_requests(),
                deploys=recent_deploys())


# --------------------------------------------- fleet status (RFC-0021)
# One read-only, machine-readable status document per node, guarded by
# a revocable fleet key instead of a session — the fleet overview app
# on the operator's inner node polls this. The key grants EXACTLY this
# one GET; the gateway strips identity headers on /fleet/* like on the
# deploy hook. Facts only, never secrets — the whitelist lives in
# fleet_view.instance_row.

FLEET_KEYS = "/apps-registry/fleet-keys.json"


def _fleet_keys():
    try:
        with open(FLEET_KEYS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@app.get("/fleet/status")
def fleet_status():
    # Same brake as the deploy hook (5 failures in 5 min → 1/min), with
    # its own throttle namespace so fleet guesses and deploy guesses
    # are counted apart.
    key = f"fleet:{_client_ip()}"
    if _deploy_blocked(key):
        return {"error": "too many attempts — wait a minute"}, 429
    auth = request.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not fleet_view.valid_key(presented, _fleet_keys()):
        _deploy_failed(key)
        # One indistinguishable answer for every failure — no chatter
        # about which keys exist (same rule as the deploy hook).
        return {"error": "denied"}, 403
    _deploy_succeeded(key)

    instances, pending = [], []
    # The node's registered external hostname — the automatic names of
    # all instances hang off it (schema 0.3). Empty on a LAN-only node;
    # then instance rows carry no automatic name at all.
    ext = external_host()
    for name, inst in sorted(load_instances().items()):
        state, _label, _detail = _instance_probe(name, inst)
        instances.append(fleet_view.instance_row(name, inst, state, ext))
        if _pending_envelope(name):
            pending.append(name)
    dns = dns_check() or {}
    return fleet_view.build_document(
        node=ext or request.host.split(":")[0],
        version=VERSION,
        profiles=node_profiles(),
        now_iso=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        core=_core_states(),
        instances=instances,
        dns_rows=dns.get("rows"),
        pending_names=pending,
        public_ip=dns.get("public_ip", ""),
    )


# ---------------------------------------------------------------------------
# Deploy hook (oaap.apps.runtime 0.2 §2.5) — the protected channel for a
# project's AI coding agent: POST /deploy/<instance> with the instance's
# bearer token redeploys the TEST instance from its recorded package
# source. No session, no identity headers; the gateway strips them.
# The portal only validates and queues — the host-side worker
# (appctl.py process-deploys, triggered by a systemd path unit on the
# spool directory) does the actual docker work.

import hashlib
import hmac
import secrets
import re as _re
import time as _time
import uuid as _uuid

SPOOL_DIR = "/deploy-spool"
SPOOL_QUEUE = os.path.join(SPOOL_DIR, "queue")
SPOOL_RESULTS = os.path.join(SPOOL_DIR, "results")
# A request the worker has taken up, moved out of the queue by the
# worker itself (RFC-0024 §5). Reading it is how this side can say
# "running" rather than "not finished yet" — and how withdrawing a
# request that has not started can be offered without racing the worker.
SPOOL_CLAIMS = os.path.join(SPOOL_DIR, "claims")
DEPLOY_THROTTLE = os.path.join(SPOOL_DIR, ".throttle.json")
DEPLOY_TOKENS = "/apps-registry/deploy-tokens.json"
DEPLOY_LOG = "/apps-registry/deploy-log.jsonl"
DEPLOY_WAIT_SECONDS = 120
DENIED = {"error": "unknown instance or invalid token"}


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "?"


# --------------------------------------- deployment state (RFC-0024 §2)
# The rules live in deploy_state.py, without Flask around them: what
# counts as a deployment, when a claim belongs to a worker that died,
# and what may still be withdrawn. Here only the paths of THIS node are
# put in front of them.
from deploy_state import (ACTION_LABEL, DEPLOY_ACTIONS,  # noqa: E402,F401
                          DEPLOY_MAX_MINUTES, TIMED_OUT, ago as _ago)
import deploy_state as _ds  # noqa: E402


def _in_flight(name=""):
    return _ds.in_flight(SPOOL_QUEUE, SPOOL_CLAIMS, name)


def _deploy_in_flight(name):
    """The one deployment this instance has under way, if any."""
    return _ds.deployment(_in_flight(name))


def _deploy_now(name):
    """The same, dressed for the instance page."""
    return _ds.for_page(_in_flight(name))


def _withdraw(name, rid):
    return _ds.withdraw(SPOOL_QUEUE, SPOOL_UPLOADS, name, rid,
                        _in_flight(name))


def _log_entry(name, rid=""):
    """The deploy-log line for one request id, or the instance's last."""
    for entry in recent_deploys(limit=200 if rid else 50):
        if name and entry.get("instance") != name:
            continue
        if not rid:
            return entry
        if entry.get("id") == rid:
            return entry
    return None


def _throttle_load():
    try:
        with open(DEPLOY_THROTTLE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _throttle_save(state):
    os.makedirs(SPOOL_DIR, exist_ok=True)
    tmp = DEPLOY_THROTTLE + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, DEPLOY_THROTTLE)


def _deploy_blocked(key):
    entry = _throttle_load().get(key)
    return bool(entry) and entry.get("blocked_until", 0) > _time.time()


def _deploy_failed(key):
    """Like login throttling: 5 failures in 5 minutes → 1 attempt/minute."""
    state = _throttle_load()
    now = _time.time()
    entry = state.get(key) or {"fails": []}
    entry["fails"] = [t for t in entry["fails"] if now - t < 300] + [now]
    if len(entry["fails"]) >= 5:
        entry["blocked_until"] = now + 60
    state[key] = entry
    _throttle_save(state)


def _deploy_succeeded(key):
    state = _throttle_load()
    if key in state:
        del state[key]
        _throttle_save(state)


def _valid_deploy_token(name):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    try:
        with open(DEPLOY_TOKENS, encoding="utf-8") as f:
            entry = json.load(f).get(name) or {}
    except (OSError, ValueError):
        return False
    digest = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    return hmac.compare_digest(entry.get("digest", ""), digest)


def _valid_creation_grant(name):
    """Is the presented bearer a live creation grant for this name?

    Only a pre-check, exactly like _valid_upload_grant: the host
    re-checks it and is the only place that SPENDS it. Returns the
    digest so the announcement can name the permission it is using.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    digest = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    try:
        with open(ARTIFACT_GRANTS, encoding="utf-8") as f:
            entry = json.load(f).get(digest) or {}
    except (OSError, ValueError):
        return ""
    if entry.get("kind") != "create" or entry.get("instance") != name:
        return ""
    if entry.get("expires", 0) < _time.time():
        return ""
    return digest


def _deploy_auth(name, allow_creation=False):
    """One indistinguishable answer for every failure (spec test 13).

    With `allow_creation` a live instance creation grant is accepted in
    place of a deploy token — the case where the instance does not exist
    yet and therefore cannot have a token (RFC-0019, Studio section).
    Returns (instance-or-{}, None) on success; the caller learns which
    of the two carried it from `g.creation_digest`.
    """
    key = _client_ip()
    if _deploy_blocked(key):
        return {"error": "too many attempts — wait a minute"}, 429
    named = bool(_re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""))
    inst = load_instances().get(name) if named else None
    g.creation_digest = ""
    if inst and inst.get("channel") == "test" and _valid_deploy_token(name):
        _deploy_succeeded(key)
        return inst, None
    if allow_creation and named and not inst:
        digest = _valid_creation_grant(name)
        if digest:
            _deploy_succeeded(key)
            g.creation_digest = digest
            return {}, None
    _deploy_failed(key)
    return DENIED, 403


def _entry_url(name, inst):
    ext = external_host()
    if ext:
        return f"https://{name}.{ext}/"
    # `inst` can be empty when a creation failed — then there is no port
    # and no address to name, and saying so beats a 500
    port = (inst or {}).get("port")
    return (f"http://{request.host.split(':')[0]}:{port}/" if port else "")


def recent_deploys(limit=5):
    try:
        with open(DEPLOY_LOG, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@app.post("/deploy/<name>")
def deploy_hook(name):
    inst, err = _deploy_auth(name)
    if err:
        return inst, err
    rid = _uuid.uuid4().hex
    os.makedirs(SPOOL_QUEUE, exist_ok=True)
    tmp = os.path.join(SPOOL_DIR, f".req-{rid}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": name,
                   "requested": datetime.now(timezone.utc).isoformat()}, f)
    os.replace(tmp, os.path.join(SPOOL_QUEUE, f"{rid}.json"))
    res_path = os.path.join(SPOOL_RESULTS, f"{rid}.json")
    deadline = _time.time() + DEPLOY_WAIT_SECONDS
    while _time.time() < deadline:
        if os.path.exists(res_path):
            try:
                with open(res_path, encoding="utf-8") as f:
                    res = json.load(f)
            finally:
                os.remove(res_path)
            body = {"ok": res.get("ok", False), "instance": name,
                    "deployment": rid, "state": "done",
                    "version": res.get("version", ""),
                    "revision": res.get("revision", ""),
                    "message": res.get("message", ""),
                    "url": _entry_url(name, inst)}
            return body, (200 if res.get("ok") else 502)
        _time.sleep(2)
    # The id is what makes the poll below answer about THIS deployment
    # rather than about whatever this instance did last (RFC-0024 §1).
    return {"ok": None, "instance": name, "deployment": rid, "state": "running",
            "message": "deployment is still running — poll GET "
                       f"/deploy/{name}/status?deployment={rid}"}, 202


# --------------------------------------- artifact deployment (RFC-0019)
# The deploy hook above fetches a recorded source. This pair lets a
# deployment bring its own package instead — for a private repository,
# that is the difference between the platform holding a foreign
# credential in cleartext and holding nothing at all.
#
# Three phases: announce (manifest + checksum + size) → the HOST decides
# and an upload grant is issued → the package is admitted only against
# that grant. The portal mints the upload token and sends only its
# digest to the host, exactly as it does for deploy tokens: the secret
# never reaches the spool.

ARTIFACT_GRANTS = "/apps-registry/artifact-grants.json"
ARTIFACT_MAX_BYTES = 256 * 1024 * 1024
SPOOL_UPLOADS = os.path.join(SPOOL_DIR, "uploads")


def _hook_base():
    ext = external_host()
    return f"https://{ext}" if ext else request.host_url.rstrip("/")


def _valid_upload_grant(name):
    """Is the presented bearer a live upload grant for this instance?

    Checked here only to refuse a stranger before 256 MB are streamed to
    disk. The authoritative check runs on the host, where the grant is
    also SPENT — the spool is data, not trust.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    digest = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    try:
        with open(ARTIFACT_GRANTS, encoding="utf-8") as f:
            entry = json.load(f).get(digest) or {}
    except (OSError, ValueError):
        return ""
    if entry.get("kind") != "upload" or entry.get("instance") != name:
        return ""
    if entry.get("expires", 0) < _time.time():
        return ""
    return digest


@app.post("/deploy/<name>/announce")
def deploy_announce(name):
    # The one place that also accepts an instance creation grant: before
    # the instance exists there is no deploy token, so the operator's
    # single-use permission stands in for one (RFC-0019, Studio
    # section). Everything after this line is identical for both — the
    # creation is the same handshake, one level up.
    inst, err = _deploy_auth(name, allow_creation=True)
    if err:
        return inst, err
    data = request.get_json(silent=True) or {}
    manifest = data.get("manifest") or ""
    if not manifest.strip():
        return {"refused": "no_manifest",
                "message": "announce the complete oaap-app.yaml as 'manifest' "
                           "— the announcement is the contract the upload is "
                           "checked against"}, 422
    try:
        size = int(data.get("artifact_bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0 or size > ARTIFACT_MAX_BYTES:
        return {"refused": "bad_size",
                "message": f"'artifact_bytes' must be the real ZIP size, at "
                           f"most {ARTIFACT_MAX_BYTES // (1024 * 1024)} MB"}, 422
    token = secrets.token_urlsafe(32)
    res = _queue_and_wait(name, {
        "action": "announce", "manifest": manifest,
        "artifact_sha256": (data.get("artifact_sha256") or "").strip().lower(),
        "artifact_bytes": size,
        "digest": hashlib.sha256(token.encode()).hexdigest(),
        "create_digest": g.get("creation_digest", ""),
    }, DEPLOY_WAIT_SECONDS)
    if res is None:
        return {"refused": "timeout",
                "message": "the node did not answer in time — try again"}, 504
    if not res.get("ok"):
        return {"refused": "rejected", "message": res.get("message", "")}, 422
    return {"ok": True,
            "upload_token": token,
            "upload_url": f"{_hook_base()}/deploy/{name}/artifact",
            "expires_in": 900,
            "message": res.get("message", "")}, 200


@app.put("/deploy/<name>/artifact")
def deploy_artifact(name):
    """Phase 3 — the package itself, admitted only by an upload grant."""
    key = _client_ip()
    if _deploy_blocked(key):
        return {"error": "too many attempts — wait a minute"}, 429
    digest = (_valid_upload_grant(name)
              if _re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or "") else "")
    if not digest:
        _deploy_failed(key)
        return DENIED, 403
    _deploy_succeeded(key)
    declared = request.content_length or 0
    if declared > ARTIFACT_MAX_BYTES:
        return {"ok": False,
                "message": f"artifact exceeds "
                           f"{ARTIFACT_MAX_BYTES // (1024 * 1024)} MB"}, 413
    rid = _uuid.uuid4().hex
    os.makedirs(SPOOL_UPLOADS, exist_ok=True)
    up = os.path.join(SPOOL_UPLOADS, f"{rid}.zip")
    written = 0
    fd = os.open(up, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = request.stream.read(1 << 16)
                if not chunk:
                    break
                written += len(chunk)
                if written > ARTIFACT_MAX_BYTES:
                    raise ValueError("too large")
                out.write(chunk)
    except (ValueError, OSError):
        os.remove(up)
        return {"ok": False,
                "message": f"artifact exceeds "
                           f"{ARTIFACT_MAX_BYTES // (1024 * 1024)} MB"}, 413
    res = _queue_with_id(rid, name, {"action": "artifact", "digest": digest,
                                     "path": (request.args.get("path") or "").strip()},
                         DEPLOY_WAIT_SECONDS)
    if res is None:
        return {"ok": None, "instance": name, "deployment": rid,
                "state": "running",
                "message": "deployment is still running — poll GET "
                           f"/deploy/{name}/status?deployment={rid}"}, 202
    body = {"ok": res.get("ok", False), "instance": name,
            "deployment": rid, "state": "done",
            "version": res.get("version", ""),
            "revision": res.get("revision", ""),
            "message": res.get("message", ""),
            "url": _entry_url(name, load_instances().get(name) or {})}
    return body, (200 if res.get("ok") else 422)


@app.get("/deploy/<name>/status")
def deploy_status(name):
    """What became of a deployment (RFC-0024 §2).

    With `?deployment=<id>` this answers about THAT request. Without it,
    about the instance — but either way it now carries an explicit
    `state`, and a deployment still in flight is reported as such.

    That last part is the fix for a silent wrong answer: this endpoint
    used to return the instance's most recent log line, so a client
    polling after a 202 was handed the PREVIOUS deployment's success
    while its own was still building. It read `ok: true` and stopped
    looking.
    """
    inst, err = _deploy_auth(name)
    if err:
        return inst, err
    rid = (request.args.get("deployment") or "").strip()
    if rid and not _re.fullmatch(r"[0-9a-f]{6,64}", rid):
        return {"instance": name, "state": "unknown",
                "message": "malformed deployment id"}, 400

    for e in _in_flight(name):
        if rid and e["id"] != rid:
            continue
        if not rid and e["action"] not in DEPLOY_ACTIONS:
            continue
        if e["state"] == "stale":
            return {"instance": name, "deployment": e["id"], "ok": False,
                    "state": "done", "message": TIMED_OUT}, 200
        return {"instance": name, "deployment": e["id"], "state": e["state"],
                "since": e["since"],
                "message": ("deployment is running" if e["state"] == "running"
                            else "deployment is queued")}, 200

    entry = _log_entry(name, rid)
    if entry is None:
        return {"instance": name, "deployment": rid,
                "state": "unknown" if rid else "none",
                "message": ("no deployment with that id on this node" if rid
                            else "no deployment recorded yet")}, 200
    entry["state"] = "done"
    entry["url"] = _entry_url(name, inst)
    return entry, 200


@app.post("/deploy/<name>/cancel")
def deploy_cancel_hook(name):
    """Withdraw a deployment that has not started (RFC-0024 §5)."""
    inst, err = _deploy_auth(name)
    if err:
        return inst, err
    rid = (request.args.get("deployment") or "").strip()
    if not _re.fullmatch(r"[0-9a-f]{6,64}", rid or ""):
        return {"instance": name, "message": "name the deployment to withdraw "
                                             "as ?deployment=<id>"}, 400
    withdrawn, why, _started = _withdraw(name, rid)
    return ({"ok": withdrawn, "instance": name, "deployment": rid,
             "message": why}, 200 if withdrawn else 409)




# ---------------------------------------------------------------------------
# Store (admin only) — reads the configured source lists and offers
# one-click installation (runtime spec 2.6): the portal queues only the
# app id; the host-side worker resolves it against the configured
# sources itself and installs from what that lookup returns.

STORE_SOURCES_FILE = "/apps-registry/store-sources.json"
INSTALL_WAIT_SECONDS = 120  # < gunicorn --timeout (150s)

# Store list rules live in store_view.py — merging, vocabulary, image
# paths and the filter defaults are decisions RFC-0012 makes, and they
# are readable and testable there without Flask around them.
from store_view import (AUDIENCE_LABEL, CATEGORY_LABEL, CLASS_LABEL,
                        MATURITY_LABEL, TRUST_LABEL,
                        matches, merge_catalogue, options)

def all_sources():
    """Every configured store source, in the object form of RFC-0012 §2.

    appctl.py is the authority — it migrates old {url, name} entries into
    this form and writes them back. The portal only reads, and reads
    tolerantly: an entry it cannot classify counts as unverified, which
    is the cautious direction to fall in.
    """
    try:
        with open(STORE_SOURCES_FILE, encoding="utf-8") as f:
            raw = json.load(f).get("sources", [])
    except (OSError, ValueError):
        return []
    out = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict) or not s.get("url"):
            continue
        trust = s.get("trust") if s.get("trust") in TRUST_LABEL else "unverified"
        out.append({"id": s.get("id") or f"quelle-{i}", "url": s["url"],
                    "name": s.get("name") or "", "trust": trust,
                    "trust_label": TRUST_LABEL[trust],
                    "origin": s.get("origin") or "",
                    "enabled": bool(s.get("enabled", True)),
                    "shipped": bool(s.get("shipped")),
                    "review": bool(s.get("review"))})
    return out


def configured_sources():
    """The enabled ones — what the store actually reads from."""
    return [s for s in all_sources() if s["enabled"]]


# A queued request is normally gone in a second or two; an install may
# take minutes while images are pulled. Anything still lying there after
# this long means nothing is draining the queue.
WORKER_STUCK_SECONDS = 600


def deploy_worker_state():
    """Is anything actually processing the spool? (oaap.core.host §2.4)

    The worker runs on the machine as a systemd path unit, so this
    container cannot ask systemd about it. It can see the symptom, and
    the symptom is the better test anyway: it catches a failed unit, a
    host without systemd, and a worker that dies on every request alike.

    Found on oaap-test, 2026-08-09: a burst of portal actions tripped
    systemd's start rate limit, the path unit went to 'failed', and the
    node kept accepting requests while processing none of them. Nothing
    anywhere said so — every page looked healthy, actions just quietly
    had no effect.
    """
    name = "Deploy-Worker"
    if not os.path.isdir(SPOOL_QUEUE):
        return {"name": name, "state": "unknown", "label": "Unbekannt",
                "detail": "Warteschlange nicht lesbar"}
    entries = _in_flight()
    if not entries:
        return {"name": name, "state": "ok", "label": "Gesund",
                "detail": "Warteschlange leer"}
    # Waiting and working are different symptoms and get different
    # verdicts (RFC-0024 §3). A request nobody picks up means the worker
    # is gone — that is the failure this check was built for. A build
    # that has been running for twelve minutes means the worker is fine
    # and busy; calling that "Steht" would send the operator to
    # systemctl for no reason.
    waiting = [e for e in entries if e["state"] == "queued"]
    running = [e for e in entries if e["state"] == "running"]
    stale = [e for e in entries if e["state"] == "stale"]
    if stale:
        return {"name": name, "state": "error", "label": "Steht",
                "detail": (f"{len(stale)} Anfrage(n) wurden angefangen und nie "
                           "abgeschlossen — der Worker ist mittendrin "
                           "ausgefallen. Sie werden beim nächsten Lauf als "
                           "fehlgeschlagen verbucht; auf der Maschine prüfen "
                           "mit 'systemctl status oaap-deployd.service'")}
    oldest_wait = max((e["since"] for e in waiting), default=0)
    if oldest_wait > WORKER_STUCK_SECONDS and not running:
        return {
            "name": name, "state": "error", "label": "Steht",
            "detail": (f"{len(waiting)} Anfrage(n) warten, die älteste seit "
                       f"{oldest_wait // 60} Minuten, und keine ist in "
                       "Arbeit — auf der Maschine prüfen mit 'systemctl "
                       "status oaap-deployd.path'; wieder in Gang bringen "
                       "mit 'systemctl reset-failed oaap-deployd.service "
                       "oaap-deployd.path && systemctl start "
                       "oaap-deployd.path'")}
    # Say WHAT is going on, not just how much: a running deployment is
    # the answer to "warum tut sich gerade nichts" (RFC-0024 §3).
    if running:
        r = running[0]
        detail = (f"{r['instance'] or 'Knoten'}: {r['action']} läuft seit "
                  f"{_ago(r['since'])}")
        if waiting:
            detail += f", {len(waiting)} weitere warten"
    else:
        detail = f"{len(waiting)} Anfrage(n) warten"
    return {"name": name, "state": "ok", "label": "Arbeitet", "detail": detail}


def pending_installs():
    """App ids with a queued/running store install (spool not yet done).

    Without this the store page offers "Installieren" again while the
    worker is still pulling images — a second click then fails
    (Jörgs Befund 2026-08-06)."""
    # Both directories: since RFC-0024 the worker moves a request it
    # has taken up out of the queue, so looking only there would call an
    # install "finished" the moment it actually STARTS.
    return {e["instance"] for e in _in_flight() if e["action"] == "install"}




def store_catalogue():
    """Fetch every enabled source and merge it into one catalogue."""
    fetched, errors = [], []
    for src in configured_sources():
        try:
            r = requests.get(src["url"], timeout=4)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            # Show the cause, not just the class — "ConnectionError"
            # alone hides whether it is DNS, routing, or TLS.
            errors.append({"name": src["name"] or src["id"], "url": src["url"],
                           "error": f"{type(e).__name__}: {str(e)[:300]}"})
            continue
        fetched.append((dict(src, name=src["name"] or data.get("name") or src["id"]),
                        data))
    installed = {inst.get("app_id"): inst.get("version")
                 for inst in load_instances().values()}
    return merge_catalogue(fetched, installed, pending_installs(),
                           node_profiles()), errors


def store_page(msg=None, msg_ok=True, status=200):
    entries, errors = store_catalogue()
    f = {
        "q": request.args.get("q", "").strip().lower(),
        "categories": request.args.get("categories", ""),
        "app_class": request.args.get("app_class", ""),
        "audience": request.args.get("audience", ""),
        "maturity": request.args.get("maturity", ""),
        "trust": request.args.get("trust", ""),
        "source": request.args.get("source", ""),
        "license": request.args.get("license", ""),
        "installed": request.args.get("installed", ""),
        "all_status": request.args.get("all_status") == "1",
        "all_profiles": request.args.get("all_profiles") == "1",
    }
    shown = [e for e in entries if matches(e, f)]
    hidden_profile = sum(1 for e in entries if not e["profile_fit"])
    hidden_archived = sum(1 for e in entries if e["status"] == "archived")
    return page(STORE_BODY, "Store", "store", status=status,
                apps=shown, total=len(entries), errors=errors, f=f,
                filtered=any(v for k, v in f.items()
                             if k not in ("all_status", "all_profiles")),
                hidden_profile=hidden_profile, hidden_archived=hidden_archived,
                opt_categories=options(entries, "categories", CATEGORY_LABEL),
                opt_class=options(entries, "app_class", CLASS_LABEL),
                opt_audience=options(entries, "audience", AUDIENCE_LABEL),
                opt_maturity=options(entries, "maturity", MATURITY_LABEL),
                trusts=[{"value": t, "label": TRUST_LABEL[t]}
                        for t in ("platform", "verified", "unverified")
                        if any(e["source"]["trust"] == t for e in entries)],
                sources=[{"value": s["id"], "label": s["name"]}
                         for s in {e["source"]["id"]: e["source"]
                                   for e in entries}.values()],
                licenses=sorted({e["license"] for e in entries if e["license"]}),
                profiles=node_profiles(),
                msg=msg or request.args.get("msg"),
                msg_ok=msg_ok and request.args.get("err") is None)


@app.get("/store")
def store():
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: der Store erfordert die Rolle server_admin.", 403
    return store_page()


SOURCE_WAIT_SECONDS = 20   # a small JSON file, no docker work


@app.get("/store/sources")
def store_sources():
    """Store sources in the portal (RFC-0012 §7) — closes step 4 of
    `portal-statt-cli.md`. The CLI keeps working unchanged."""
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: der Store erfordert die Rolle server_admin.", 403
    return page(STORE_SOURCES_BODY, "Store-Quellen", "store",
                sources=all_sources(), msg=request.args.get("msg"),
                msg_ok=request.args.get("err") is None)


@app.post("/store/sources")
def store_sources_change():
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: der Store erfordert die Rolle server_admin.", 403
    op = request.form.get("op", "")
    if op not in ("add", "remove", "enable", "disable", "rename", "trust"):
        return redirect("/store/sources?err=1&msg="
                        + quote("Unbekannte Aktion."), code=303)
    source_id = request.form.get("source_id", "").strip()
    # Removal names the source it claims to remove — a misdirected or
    # replayed request must not throw out a different list than the one
    # the operator was looking at (same rule as removing an instance).
    if op == "remove" and request.form.get("confirm", "") != source_id:
        return redirect("/store/sources?err=1&msg="
                        + quote("Entfernen nicht bestätigt."), code=303)
    res = _queue_and_wait("", {
        "action": "source", "op": op, "source_id": source_id,
        "url": request.form.get("url", "").strip(),
        "name": request.form.get("name", "").strip(),
        "origin": request.form.get("origin", "").strip(),
        "trust": request.form.get("trust", "").strip(),
    }, SOURCE_WAIT_SECONDS)
    if res is None:
        return redirect("/store/sources?err=1&msg="
                        + quote("Der Server hat nicht rechtzeitig geantwortet — "
                                "die Liste oben zeigt den tatsächlichen Stand."),
                        code=303)
    text = res.get("message", "unbekanntes Ergebnis")
    return redirect(f"/store/sources?msg={quote(text[0].upper() + text[1:])}"
                    + ("" if res.get("ok") else "&err=1"), code=303)


@app.get("/store/<source_id>/<app_id>")
def store_app(source_id, app_id):
    """Object page for one app (§6) — the reason the format carries
    presentation fields at all; without it they would be decoration."""
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: der Store erfordert die Rolle server_admin.", 403
    entries, _errors = store_catalogue()
    a = next((e for e in entries
              if e["id"] == app_id and e["source"]["id"] == source_id), None)
    if not a:
        return redirect("/store?err=1&msg="
                        + quote("Diese App steht in keiner eingetragenen "
                                "Quelle (mehr)."), code=303)
    return page(STORE_APP_BODY, a["name"], "store", a=a,
                profiles=node_profiles(),
                msg=request.args.get("msg"),
                msg_ok=request.args.get("err") is None)


@app.post("/store/install")
def store_install():
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: der Store erfordert die Rolle server_admin.", 403
    app_id = request.form.get("app_id", "").strip()
    source_id = request.form.get("source_id", "").strip()

    def back(text, ok=True):
        where = f"/store/{source_id}/{app_id}" if source_id and app_id else "/store"
        return redirect(f"{where}?msg={quote(text)}"
                        + ("" if ok else "&err=1"), code=303)

    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]*", app_id):
        return store_page("Ungültige App-Kennung.", msg_ok=False, status=400)
    # Queue for the host worker. The request names the app id and the
    # source the user was looking at — never a package URL or a version:
    # the worker resolves both against the CONFIGURED sources on the
    # host (spec 2.6, RFC-0012 §3). Picking among the sources the
    # server_admin already chose is all a request can do; the spool is
    # data, not trust.
    confirm = request.form.get("confirm_source", "").strip()
    src = next((s for s in configured_sources() if s["id"] == source_id), None)
    if source_id and not src:
        return store_page("Diese Store-Quelle ist auf diesem Knoten nicht "
                          "(mehr) eingetragen.", msg_ok=False, status=400)
    if src and src["trust"] == "unverified" and confirm != source_id:
        return back("Nicht bestätigt: " + (src["name"] or source_id)
                    + " ist eine ungeprüfte Quelle. Bitte das Häkchen setzen.",
                    ok=False)
    rid = _uuid.uuid4().hex
    os.makedirs(SPOOL_QUEUE, exist_ok=True)
    tmp = os.path.join(SPOOL_DIR, f".req-{rid}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": app_id, "action": "install",
                   "source_id": source_id, "confirm_source": confirm,
                   "by": request.headers.get("X-OAAP-User", "?"),
                   "requested": datetime.now(timezone.utc).isoformat()}, f)
    os.replace(tmp, os.path.join(SPOOL_QUEUE, f"{rid}.json"))
    res_path = os.path.join(SPOOL_RESULTS, f"{rid}.json")
    deadline = _time.time() + INSTALL_WAIT_SECONDS
    while _time.time() < deadline:
        if os.path.exists(res_path):
            try:
                with open(res_path, encoding="utf-8") as f:
                    res = json.load(f)
            finally:
                os.remove(res_path)
            if res.get("ok"):
                v = res.get("version", "")
                return back(f"'{app_id}' wurde installiert"
                            + (f" (Version {v})" if v else "")
                            + " — die Kachel erscheint im Launchpad.")
            return back(f"Installation von '{app_id}' fehlgeschlagen: "
                        f"{res.get('message', 'unbekannter Fehler')}", ok=False)
        _time.sleep(2)
    return back(f"Die Installation von '{app_id}' läuft noch — das Ergebnis "
                "erscheint im Deploy-Protokoll auf der Gesundheitsseite.")


# ---------------------------------------------------------------------------
# App-instance visibility (RFC-0007) and configuration (spec 2.3/2.4.3)
# — server_admin only. /apps-registry
# is mounted read-only in this container (like the store install above),
# so a change is queued to the host-side worker (appctl.py
# process-deploys), which updates the registry, regenerates that
# instance's Caddy site(s) and reloads the gateway.

VISIBILITY_WAIT_SECONDS = 20  # registry+Caddy+reload only, no docker work
LINK_WAIT_SECONDS = 30  # creates/removes a network and connects containers
ENDPOINT_WAIT_SECONDS = 90  # recreates the instance's containers with a port map


def _instance_groups(inst):
    return (inst.get("visibility") or {}).get("groups") or []


RESERVED_ENV = {"OAAP_APP_SECRET"}  # platform-owned, never operator-editable


def _instance_env(name):
    """Current config values of an instance (read-only mount)."""
    try:
        with open(f"/apps-registry/{name}/instance.env", encoding="utf-8") as f:
            return dict(l.strip().split("=", 1) for l in f if "=" in l)
    except OSError:
        return {}


def _instance_config(name, inst):
    """Declared config keys with their current values (spec 2.4.3).

    Mirrors appctl.config_entries: instances installed before config
    recording fall back to the keys in instance.env and are treated as
    secret, so an unclassified value is never rendered into a page.
    """
    env = _instance_env(name)
    declared = inst.get("config")
    if declared is None:
        declared = [{"key": k, "label": k, "secret": True}
                    for k in env if k not in RESERVED_ENV]
    rows = []
    for c in declared:
        key = c["key"]
        if key in RESERVED_ENV:
            continue
        secret = bool(c.get("secret"))
        multiline = bool(c.get("multiline"))
        stored = env.get(key, "")
        rows.append({
            "key": key, "label": c.get("label") or key, "secret": secret,
            "multiline": multiline,
            "is_set": bool(env.get(key)),
            # a secret value never leaves the server, not even prefilled
            "value": "" if secret else
                     (iv.value_to_lines(stored) if multiline else stored),
        })
    return rows


@app.get("/instances")
def instances_list():
    denied = require_user_admin()
    if denied:
        return denied
    role, mine = caller_scope()
    rows = []
    for name, inst in sorted(visible_instances().items()):
        groups = _instance_groups(inst)
        channel = inst.get("channel", "production")
        rows.append({
            "name": name, "app_name": inst.get("app_name", name),
            "version": inst.get("version", "?"),
            "channel": channel, "channel_label": CHANNEL_LABELS.get(channel, channel),
            "visibility_label": "Alle" if not groups else "Gruppen: " + ", ".join(groups),
            "tile_visible": iv.tile_visible(inst),
            "tile_mode": iv.tile_mode(inst),
            "tenant": tenant_label(resolve_tenant(inst.get("tenant"))),
        })
    return page(INSTANCES_LIST_BODY, "Instanzen", "instances", instances=rows,
                can_create="dev" in node_profiles(),
                show_tenant=multi_tenant() and role == "server_admin",
                scope_note=(tenant_name(mine) if role == "tenant_admin"
                            and multi_tenant() else ""),
                grants=_open_creation_grants(),
                grant_minutes=CREATE_GRANT_MINUTES,
                msg=request.args.get("msg"), error=request.args.get("err"))


# --- instance creation grant (RFC-0019, Studio section) --------------------
# Before an instance exists there is no deploy token, so the one step
# Studio cannot do with a pasted token is the first one. Instead of
# giving Studio a standing permission, `server_admin` hands it a
# single-use one for exactly one name — spendable, not held.

CREATE_GRANT_MINUTES = 30  # mirrors appctl.CREATE_GRANT_TTL


def _open_creation_grants():
    """Which creation grants are open, for the list page.

    Names and remaining time only — never the digest. What is shown here
    is what an administrator has to be able to revoke, not a credential.
    """
    try:
        with open(ARTIFACT_GRANTS, encoding="utf-8") as f:
            grants = json.load(f)
    except (OSError, ValueError):
        return []
    now = _time.time()
    role, mine = caller_scope()
    out = []
    for entry in grants.values():
        if entry.get("kind") != "create" or entry.get("expires", 0) <= now:
            continue
        # A permit names the tenant it was issued for (spec 1.4), which
        # is exactly what makes it filterable here: a tenant_admin must
        # not learn that a name is spoken for in somebody else's tenant.
        held = resolve_tenant((entry.get("payload") or {}).get("tenant"))
        if role != "server_admin" and held != mine:
            continue
        out.append({"instance": entry.get("instance", ""),
                    "minutes": max(1, int((entry["expires"] - now) // 60)),
                    "tenant": tenant_label(held)})
    return sorted(out, key=lambda e: e["instance"])


@app.post("/instances/grant")
def instance_grant():
    denied = require_user_admin()
    if denied:
        return denied
    name = (request.form.get("name") or "").strip().lower()
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return redirect("/instances?err="
                        + quote("Instanzname: Kleinbuchstaben, Ziffern, Bindestriche."),
                        code=303)
    if request.form.get("op") == "revoke":
        res = _queue_and_wait(name, {"action": "grant", "op": "revoke"},
                              TOKEN_WAIT_SECONDS)
        if res is None:
            return redirect("/instances?err="
                            + quote("Der Widerruf läuft noch — bitte gleich erneut prüfen."),
                            code=303)
        return redirect(f"/instances?{'msg' if res.get('ok') else 'err'}="
                        + quote(res.get("message", "")), code=303)
    if name in load_instances():
        # Instance names are unique per NODE, not per tenant — a name
        # already taken cannot be hidden from a tenant_admin without
        # failing later and more confusingly. So it is said plainly, and
        # without saying whose it is: the collision is the fact, the
        # owner is not.
        if name in visible_instances():
            err = (f"Eine Instanz namens „{name}“ gibt es schon — "
                   "sie braucht ein Deploy-Token, keine Anlege-Erlaubnis.")
        else:
            err = (f"Der Name „{name}“ ist auf diesem Knoten schon vergeben — "
                   "bitte einen anderen wählen.")
        return redirect("/instances?err=" + quote(err), code=303)
    # Same shape as the deploy token: the portal mints the secret and
    # hands the host only its digest, so the readable value never
    # touches the spool or the filesystem.
    grant = secrets.token_urlsafe(32)
    # The tenant travels as a wish and is decided on the host from the
    # issuer's own record — the same rule as everywhere else, checked
    # where the user store actually is (spec 2.3 rule 3).
    res = _queue_and_wait(name, {
        "action": "grant", "op": "create",
        "digest": hashlib.sha256(grant.encode()).hexdigest(),
        "tenant": request.form.get("tenant", ""),
    }, TOKEN_WAIT_SECONDS)
    if res is None:
        return redirect("/instances?err="
                        + quote("Die Ausstellung läuft noch — bitte gleich erneut prüfen."),
                        code=303)
    if not res.get("ok"):
        return redirect("/instances?err="
                        + quote(res.get("message", "Erlaubnis konnte nicht ausgestellt werden.")),
                        code=303)
    # Rendered directly, NOT via Post/Redirect/Get — see TOKEN_SHOW_BODY.
    return page(GRANT_SHOW_BODY, f"Anlege-Erlaubnis {name}", "instances",
                name=name, grant=grant, minutes=CREATE_GRANT_MINUTES,
                hook_url=_hook_url(name))


# --- creating a test instance from the portal (RFC-0011, profile `dev`) ---
# The store's one-click install (2.6) stays untouched and available on
# every node. What the profile adds here is the development case: a test
# instance, optionally from a repository no store list carries yet.

CREATE_WAIT_SECONDS = 120  # clone + build + start, like a store install


def _store_apps():
    """Every app the configured store sources list, for the create form.

    Same catalogue as the store page, so both agree with the host on
    which source wins for an app id (RFC-0012 §3). Showing two entries
    for one id would only invite a choice the host would then overrule.
    """
    return [{"id": e["id"], "name": e["name"], "version": e["version"],
             "source": e["source"]["name"], "source_id": e["source"]["id"],
             "trust": e["source"]["trust"],
             "trust_label": e["source"]["trust_label"]}
            for e in store_catalogue()[0]]


def _require_dev_node():
    """Guard for the create path — the profile is checked twice.

    Here for a readable answer, and again on the host, where the
    decision actually is: the spool is data, not trust.

    A tenant_admin may create instances too (oaap.core.tenant 2.3) --
    the host puts the new one in THEIR tenant, so a workbench node with
    several tenants does not become a way into somebody else's.
    """
    denied = require_user_admin()
    if denied:
        return denied
    if "dev" not in node_profiles():
        return ("Dieser Knoten hat kein Profil 'dev'. Instanzen aus dem "
                "Portal anzulegen ist eine Entwicklungshandlung und deshalb "
                "an das Profil gebunden (RFC-0011) — setzen mit "
                "'sudo oaap node add-profile dev' auf der Maschine.", 403)
    return None


@app.get("/instances/new")
def instance_new_form():
    denied = _require_dev_node()
    if denied:
        return denied
    return page(INSTANCE_NEW_BODY, "Instanz anlegen", "instances",
                store_apps=_store_apps(), error=request.args.get("err"))


@app.post("/instances/new")
def instance_new():
    denied = _require_dev_node()
    if denied:
        return denied
    from_ = request.form.get("from", "store")
    app_id = request.form.get("app_id", "").strip()
    url = request.form.get("url", "").strip()
    name = request.form.get("name", "").strip().lower()
    if from_ == "store" and not app_id:
        return _new_error("Bitte eine App aus dem Store wählen.")
    if from_ == "git" and not url:
        return _new_error("Bitte die Git-URL des Repositories angeben.")
    upload = request.files.get("artifact") if from_ == "artifact" else None
    if from_ == "artifact":
        if not upload or not upload.filename:
            return _new_error("Bitte ein ZIP-Paket auswählen.")
        if not name:
            # the manifest inside would answer this, but reading it here
            # would mean unpacking an untrusted archive in the portal —
            # that job belongs to the host, which is why it asks instead
            return _new_error("Für den Paket-Weg bitte einen Instanznamen angeben.")
    # An instance name derived from a Git URL would be a guess; the app
    # id from the manifest is the honest default, so the host decides it
    # when the field is left empty.
    if name and not _re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return _new_error("Instanzname: Kleinbuchstaben, Ziffern, Bindestriche.")
    if name and name in load_instances():
        return _new_error(f"Eine Instanz namens '{name}' gibt es bereits.")
    if not name and from_ == "store":
        name = app_id
    if not name:
        return _new_error("Für den Git-Weg bitte einen Instanznamen angeben.")
    # Which list the chosen app comes from, resolved the same way the
    # host will resolve it (RFC-0012 §3). An unverified source costs a
    # confirmation here too — the dev profile relaxes where a package
    # may come from, not whether the operator is told.
    source_id, confirm = "", ""
    if from_ == "store":
        chosen = next((a for a in _store_apps() if a["id"] == app_id), None)
        if chosen:
            source_id = chosen["source_id"]
            if chosen["trust"] == "unverified":
                if request.form.get("confirm_source") != "yes":
                    return _new_error(
                        f"'{chosen['name']}' stammt aus der ungeprüften Quelle "
                        f"{chosen['source']} — bitte die Bestätigung setzen.")
                confirm = source_id
    rid = _uuid.uuid4().hex
    if upload is not None:
        os.makedirs(SPOOL_UPLOADS, exist_ok=True)
        dest = os.path.join(SPOOL_UPLOADS, f"{rid}.zip")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = upload.stream.read(1 << 16)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > ARTIFACT_MAX_BYTES:
                        raise ValueError("too large")
                    out.write(chunk)
        except (ValueError, OSError):
            os.remove(dest)
            return _new_error(
                f"Das Paket ist größer als "
                f"{ARTIFACT_MAX_BYTES // (1024 * 1024)} MB.")
    res = _queue_with_id(rid, name, {
        "action": "create", "from": from_, "app_id": app_id,
        "source_id": source_id, "confirm_source": confirm,
        "url": url, "path": (request.form.get("artifact_path", "").strip()
                             if from_ == "artifact"
                             else request.form.get("path", "").strip()),
        "ref": request.form.get("ref", "").strip(),
    }, CREATE_WAIT_SECONDS)
    if res is None:
        return redirect(f"/instances?msg={quote(f'Die Installation von {name} läuft noch — das Ergebnis steht im Deploy-Protokoll auf der Gesundheitsseite.')}", code=303)
    if res.get("ok"):
        return redirect(f"/instances/{name}?msg={quote('Test-Instanz angelegt.')}",
                        code=303)
    return _new_error(f"Anlegen fehlgeschlagen: "
                      f"{res.get('message', 'unbekannter Fehler')}")


def _new_error(text):
    return page(INSTANCE_NEW_BODY, "Instanz anlegen", "instances", status=400,
                store_apps=_store_apps(), error=text)


def _tab(default=""):
    """Which section the caller is in — from the link (?tab=) or from the
    hidden field a form carried along, so a save comes back where it was
    triggered instead of dropping the user at the top of the page."""
    return iv.valid_tab(request.values.get("tab"), default)


def _inst_back(name, msg="", err=""):
    """Post/Redirect/Get back to the instance page, same section."""
    q = []
    if msg:
        q.append("msg=" + quote(msg))
    if err:
        q.append("err=" + quote(err))
    tab = _tab()
    if tab:
        q.append("tab=" + tab)
    return redirect(f"/instances/{name}" + ("?" + "&".join(q) if q else ""),
                    code=303)


@app.get("/instances/<name>")
def instance_detail(name):
    denied = require_instance_admin(name)
    if denied:
        return denied
    inst = load_instances().get(name)
    if not inst:
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    groups = _instance_groups(inst)
    address = inst.get("address", "")
    auto = f"{name}.{external_host()}" if external_host() else ""
    source_label, source_lines = iv.source_view(inst)
    i = {"name": name, "app_name": inst.get("app_name", name),
         "version": inst.get("version", "?"),
         # Objektkopf (design guidelines 6.2.1): the answers one would
         # otherwise have to hunt for across six sections
         "app_id": inst.get("app_id", ""),
         "description": inst.get("description", ""),
         "channel_label": CHANNEL_LABELS.get(inst.get("channel", ""),
                                             inst.get("channel", "?")),
         "address_host": address or auto,
         "address_url": f"https://{address or auto}" if (address or auto) else "",
         "visibility_label": iv.visibility_label(inst),
         "tile_visible": iv.tile_visible(inst),
         "source_label": source_label, "source_lines": source_lines,
         "route_rows": iv.route_rows(inst),
         "storage": inst.get("storage") or [],
         "services": inst.get("services") or [],
         "groups": groups, "roles": inst.get("roles") or [],
         "config": _instance_config(name, inst),
         "is_test": inst.get("channel") == "test",
         "token_created": _token_created(name),
         "artifacts": _artifacts(name, inst),
         "deploy_now": _deploy_now(name),
         "deploy_limit": DEPLOY_MAX_MINUTES,
         "promote": _promote_view(name, inst),
         "pending": _pending_envelope(name),
         "hook_url": _hook_url(name),
         "address": address,
         "aliases": inst.get("aliases") or [],
         "auto_address": auto,
         "has_public_route": any("public" in (r.get("roles") or [])
                                 for r in inst.get("routes") or []),
         "tile_mode": iv.tile_mode(inst),
         "tile_reason": iv.tile_reason(inst),
         # app-to-app links (RFC-0016): the instances this one may reach,
         # and the other instances still available to link to
         "links": inst.get("links") or [],
         "link_candidates": sorted(n for n in load_instances()
                                   if n != name
                                   and n not in (inst.get("links") or [])),
         # non-HTTP endpoints (RFC-0015): what the app declares, which are
         # granted, and whether this node is even allowed to grant them
         "endpoints": _endpoint_view(inst),
         "node_exposed": "exposed" in node_profiles(),
         **_throttle_view(inst)}
    return page(INSTANCE_EDIT_BODY, f"Instanz {name}", "instances", i=i,
                tabs=iv.TABS, tab=_tab(iv.DEFAULT_TAB),
                msg=request.args.get("msg"), error=request.args.get("err"))


def _endpoint_view(inst):
    """Declared endpoints merged with their grant status, for the portal
    card (RFC-0015)."""
    granted = {e["name"]: e for e in (inst.get("endpoints") or [])}
    rows = []
    for d in inst.get("declared_endpoints") or []:
        g = granted.get(d["name"])
        rows.append({
            "name": d["name"], "protocol": d["protocol"],
            "container_port": d["container_port"], "wish": d.get("wish"),
            "fixed": bool(d.get("fixed")),
            "reason": (d.get("reason") or "").strip(),
            "granted": bool(g), "host_port": g["host_port"] if g else None,
        })
    return rows


@app.post("/instances/<name>/endpoint")
def instance_endpoint(name):
    """Grant or revoke a non-HTTP endpoint (RFC-0015). server_admin only;
    queued through the spool worker, which re-checks the 'exposed' node
    profile — the button is not the boundary."""
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    op = request.form.get("op", "allow")
    ep = (request.form.get("endpoint") or "").strip()
    if not ep:
        return _inst_back(name, err="Kein Endpunkt angegeben.")
    return _queue_and_redirect(name, {"action": "endpoint", "op": op, "endpoint": ep},
                               ENDPOINT_WAIT_SECONDS)


@app.post("/instances/<name>/link")
def instance_link(name):
    """Declare or drop an app-to-app link (RFC-0016). server_admin only;
    queued through the spool worker like every other instance write."""
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    op = request.form.get("op", "add")
    target = (request.form.get("target") or "").strip()
    if not target:
        return _inst_back(name, err="Bitte eine Ziel-Instanz wählen.")
    return _queue_and_redirect(name, {"action": "link", "op": op, "target": target},
                               LINK_WAIT_SECONDS)


@app.post("/instances/<name>/visibility")
def instance_visibility(name):
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    mode = request.form.get("mode", "all")
    groups = _parse_groups(request.form.get("groups", "")) if mode == "groups" else []
    if mode == "groups" and not groups:
        return _inst_back(
            name, err="Bitte mindestens eine Gruppe angeben oder Alle wählen.")
    return _queue_and_redirect(name, {"action": "visibility", "groups": groups},
                               VISIBILITY_WAIT_SECONDS)


@app.post("/instances/<name>/tile")
def instance_tile(name):
    """Launchpad tile override (runtime spec 2.10).

    Queued like every other write from here — the registry mount is
    read-only — even though this one touches nothing but one registry
    field. The host re-checks the mode: the spool is data, not trust.
    """
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    mode = request.form.get("mode", "auto")
    if mode not in iv.TILE_MODES:
        return _inst_back(name, err="Unbekannte Einstellung für die Kachel.")
    return _queue_and_redirect(name, {"action": "tile", "mode": mode},
                               TILE_WAIT_SECONDS)


TILE_WAIT_SECONDS = 20  # one registry field, no container and no gateway work


# Recreating the container takes noticeably longer than a registry edit.
CONFIG_WAIT_SECONDS = 90
TOKEN_WAIT_SECONDS = 20    # writes one small file, no container work
ADDRESS_WAIT_SECONDS = 30  # registry + Caddy regeneration + reload


def _token_created(name):
    """When the instance's deploy token was issued ('' = none)."""
    try:
        with open(DEPLOY_TOKENS, encoding="utf-8") as f:
            stamp = (json.load(f).get(name) or {}).get("created", "")
    except (OSError, ValueError):
        return ""
    return stamp.replace("T", " ").rstrip("Z")


def _artifacts(name, inst):
    """Retained packages of an artifact-deployed instance, newest first."""
    src = inst.get("source") or {}
    if src.get("kind") != "artifact":
        return []
    d = os.path.join("/apps-registry", name, "artifacts")
    running = src.get("stored", "")
    try:
        files = [f for f in os.listdir(d) if f.endswith(".zip")]
    except OSError:
        return []
    files.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)
    out = []
    for f in files:
        stamp = datetime.fromtimestamp(os.path.getmtime(os.path.join(d, f)),
                                       timezone.utc)
        out.append({"file": f, "running": f == running,
                    "received": stamp.strftime("%Y-%m-%d %H:%M")})
    return out


def _pending_envelope(name):
    """An announcement held back because it would widen the envelope."""
    try:
        with open(ARTIFACT_GRANTS, encoding="utf-8") as f:
            entry = json.load(f).get(f"pending:{name}") or {}
    except (OSError, ValueError):
        return None
    payload = entry.get("payload") or {}
    if not payload.get("reasons") or payload.get("confirmed"):
        return None
    return payload


DEFAULT_THROTTLE = {"limit": 300, "window": 60}  # mirrors appctl.DEFAULT_THROTTLE


def _throttle_view(inst):
    """Radio state and rate field for the throttle card (RFC-0010)."""
    t = inst.get("throttle")
    default = (f"{DEFAULT_THROTTLE['limit']} Anfragen pro "
               f"{DEFAULT_THROTTLE['window']} Sekunden")
    if t is None:                      # kein Override -> Plattform-Standard
        mode, rate = "default", ""
    elif not t:                        # ausdrücklich abgeschaltet
        mode, rate = "off", ""
    else:
        mode, rate = "custom", f"{t['limit']}/{t['window']}"
    return {"throttle_mode": mode, "throttle_rate": rate,
            "throttle_default": default}


@app.post("/instances/<name>/address")
def instance_address(name):
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    op = (request.form.get("op") or "").strip()
    hostname = (request.form.get("hostname") or "").strip()
    # Canonical name: an explicit remove, or an empty field, clears it.
    if op == "remove" or (not op and not hostname):
        return _queue_and_redirect(name, {"action": "address", "op": "remove"},
                                   ADDRESS_WAIT_SECONDS)
    if op in ("alias-add", "alias-remove"):
        if not hostname:
            return _inst_back(name, err="Kein Aliasname angegeben.")
        return _queue_and_redirect(name, {"action": "address", "op": op,
                                          "hostname": hostname},
                                   ADDRESS_WAIT_SECONDS)
    return _queue_and_redirect(name, {"action": "address", "hostname": hostname},
                               ADDRESS_WAIT_SECONDS)


@app.post("/instances/<name>/throttle")
def instance_throttle(name):
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    return _queue_and_redirect(name, {
        "action": "throttle",
        "mode": request.form.get("mode", "default"),
        "rate": request.form.get("rate", ""),
    }, ADDRESS_WAIT_SECONDS)


REMOVE_WAIT_SECONDS = 60  # stops a container and rewrites gateway config


@app.post("/instances/<name>/remove")
def instance_remove(name):
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    # Typing the name is the guard against a misclick on the one
    # destructive control in this UI; the host checks it a second time.
    if (request.form.get("confirm") or "").strip() != name:
        return _inst_back(name,
                          err="Zum Entfernen bitte den Instanznamen eintippen.")
    res = _queue_and_wait(name, {"action": "remove",
                                 "confirm": name,
                                 "purge": bool(request.form.get("purge"))},
                          REMOVE_WAIT_SECONDS)
    if res is None:
        return redirect(
            f"/instances?err={quote('Das Entfernen läuft noch — bitte die Liste gleich erneut prüfen.')}",
            code=303)
    if not res.get("ok"):
        return _inst_back(name, err=res.get("message", "Entfernen fehlgeschlagen."))
    # back to the list: the object page this came from no longer exists
    return redirect(f"/instances?msg={quote(res.get('message', 'Entfernt.'))}", code=303)


def _hook_url(name):
    """Where the deploy hook answers for this instance.

    The hook lives on the platform apex, not on an instance's own
    address — so this is always the node's external hostname, or the
    host the caller is using when the node has none.
    """
    ext = external_host()
    return f"https://{ext}/deploy/{name}" if ext else f"http://{request.host}/deploy/{name}"


@app.post("/instances/<name>/envelope")
def instance_envelope(name):
    """Confirm or discard a held-back deployment (RFC-0019 decision 5).

    Confirmation is deliberately bound to the announced manifest, not to
    the instance: it says yes to THIS change, never to whatever the next
    upload turns out to contain.
    """
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    op = "reject" if request.form.get("op") == "reject" else "confirm"
    return _queue_and_redirect(
        name, {"action": "envelope", "op": op,
               "manifest_sha": request.form.get("manifest_sha", "").strip()},
        TOKEN_WAIT_SECONDS)


def _promote_view(name, inst):
    """The promotion offer for a test instance (RFC-0020), or None.

    Only for an instance that runs from a retained artifact — that is
    the only case where "the same bytes" is provable, which is the whole
    promise. The candidate list is deliberately narrow: production
    instances of the SAME app, because promoting into a different app is
    the mistake this page must not make easy.
    """
    if inst.get("channel") != "test":
        return None
    if (inst.get("source") or {}).get("kind") != "artifact":
        return None
    app_id = inst.get("app_id", "")
    targets = [{"name": n, "version": i.get("version", "?")}
               for n, i in sorted(load_instances().items())
               if i.get("channel") == "production" and i.get("app_id") == app_id]
    suggestion = name[:-5] if name.endswith("-test") else f"{app_id}-prod"
    return {"targets": targets, "suggestion": suggestion}


PROMOTE_WAIT_SECONDS = 180  # builds an image, like any other installation


@app.post("/instances/<name>/promote")
def instance_promote(name):
    """Ship this test instance's tested artifact to production (RFC-0020).

    `server_admin` only, and deliberately no spendable permission: this
    is the one decision the grants of RFC-0019 are careful not to
    include. The portal only forwards it — the host re-checks channel,
    app id, version and envelope before it installs.
    """
    denied = require_instance_admin(name)
    if denied:
        return denied
    inst = load_instances().get(name)
    if not inst:
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    target = (request.form.get("target") or "").strip()
    new_target = (request.form.get("new_target") or "").strip().lower()
    if target and new_target and target != new_target:
        return _inst_back(name, err="Bitte entweder eine bestehende "
                                    "Produktiv-Instanz wählen oder einen neuen "
                                    "Namen eintragen — nicht beides.")
    target = target or new_target
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]*", target):
        return _inst_back(name, err="Ziel fehlt oder ist kein gültiger "
                                    "Instanzname (Kleinbuchstaben, Ziffern, "
                                    "Bindestriche).")
    if target == name:
        return _inst_back(name, err="Eine Instanz kann nicht in sich selbst "
                                    "übernommen werden.")
    # queued on the TARGET: that is the instance that changes, and the
    # log, the result and the registry are all keyed on it
    res = _queue_and_wait(target, {"action": "promote", "from": name,
                                   "confirmed": bool(request.form.get("confirm"))},
                          PROMOTE_WAIT_SECONDS)
    if res is None:
        return _inst_back(name, err="Die Übernahme läuft noch — sieh gleich in "
                                    "der Instanzliste nach, ob sie durch ist.")
    if not res.get("ok"):
        return _inst_back(name, err=res.get("message", "Übernahme fehlgeschlagen."))
    return redirect(f"/instances/{target}?msg="
                    + quote(res.get("message", "Übernommen.")), code=303)


@app.post("/instances/<name>/rollback")
def instance_rollback(name):
    """Reinstall a retained package (RFC-0019 §4)."""
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    artifact = request.form.get("artifact", "").strip()
    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.zip", artifact):
        return _inst_back(name, err="Unbekanntes Paket.")
    return _queue_and_redirect(name, {"action": "rollback", "artifact": artifact},
                               CREATE_WAIT_SECONDS)


@app.post("/instances/<name>/artifact-delete")
def instance_artifact_delete(name):
    """Delete one retained package — never the one in service (RFC-0024 §6)."""
    denied = require_instance_admin(name)
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    artifact = request.form.get("artifact", "").strip()
    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.zip", artifact):
        return _inst_back(name, err="Unbekanntes Paket.")
    # The host decides: /apps-registry is mounted read-only here, and the
    # rule "not the one in service" has to be re-checked where the
    # registry is authoritative anyway.
    return _queue_and_redirect(name, {"action": "artifact-remove",
                                      "artifact": artifact},
                               VISIBILITY_WAIT_SECONDS)


@app.post("/instances/<name>/deploy/cancel")
def instance_deploy_cancel(name):
    """Withdraw a deployment that is still waiting (RFC-0024 §5)."""
    denied = require_instance_admin(name)
    if denied:
        return denied
    rid = request.form.get("deployment", "").strip()
    if not _re.fullmatch(r"[0-9a-f]{6,64}", rid or ""):
        return _inst_back(name, err="Unbekanntes Deployment.")
    done, _why, started = _withdraw(name, rid)
    if done:
        return _inst_back(name, msg="Das Deployment wurde zurückgezogen, "
                                    "bevor es angefangen hat.")
    return _inst_back(
        name,
        err=("Das Deployment ist bereits angelaufen und wird nicht "
             "abgebrochen — ein halb gebauter Stand ist schlimmer als "
             f"warten. Es endet spätestens nach {DEPLOY_MAX_MINUTES} Minuten."
             if started else
             "Dieses Deployment wartet nicht (mehr) — sieh im Deploy-Protokoll "
             "auf der Gesundheitsseite nach, wie es ausgegangen ist."))


@app.post("/instances/<name>/token")
def instance_token(name):
    denied = require_instance_admin(name)
    if denied:
        return denied
    inst = load_instances().get(name)
    if not inst:
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    if inst.get("channel") != "test":
        return _inst_back(name, err="Deploy-Token gibt es nur für Test-Instanzen.")
    if request.form.get("op") == "revoke":
        return _queue_and_redirect(name, {"action": "token", "op": "revoke"},
                                   TOKEN_WAIT_SECONDS)
    # The portal mints the token and hands the host only its digest, so
    # the readable value never touches the filesystem — not the spool,
    # not the token store. Creating one is no more privilege than this
    # page already has: a token authorizes exactly the redeploy the
    # portal can trigger anyway (runtime spec 2.5).
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    res = _queue_and_wait(name, {"action": "token", "op": "create", "digest": digest},
                          TOKEN_WAIT_SECONDS)
    if res is None:
        return _inst_back(
            name, err="Die Ausstellung läuft noch — bitte gleich erneut prüfen.")
    if not res.get("ok"):
        return _inst_back(name,
                          err=res.get("message", "Token konnte nicht erzeugt werden."))
    # Rendered directly, NOT via Post/Redirect/Get: a redirect would put
    # the token into a URL, and the gateway logs full URIs.
    return page(TOKEN_SHOW_BODY, f"Deploy-Token {name}", "instances",
                name=name, token=token, hook_url=_hook_url(name))


@app.post("/instances/<name>/config")
def instance_config(name):
    denied = require_instance_admin(name)
    if denied:
        return denied
    inst = load_instances().get(name)
    if not inst:
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    values = {}
    for c in _instance_config(name, inst):
        submitted = request.form.get(f"cfg-{c['key']}")
        if submitted is None:
            continue
        # an empty secret field means "keep the stored value" -- there is
        # nothing to prefill it with, so blank cannot mean "clear it"
        if c["secret"] and submitted.strip() == "":
            continue
        if c.get("multiline"):
            # Zeilen -> gespeicherte Listenform. Ein Eintrag mit ';' wird
            # abgelehnt statt zerschnitten (instance_view.lines_to_value).
            joined, err = iv.lines_to_value(submitted)
            if err:
                return _inst_back(name, err=f"{c['label']}: {err}")
            submitted = joined
        values[c["key"]] = submitted
    if not values:
        return _inst_back(name, msg="Keine Änderung.")
    return _queue_and_redirect(name, {"action": "config", "values": values},
                               CONFIG_WAIT_SECONDS)


def _queue_and_redirect(name, payload, wait_seconds):
    """Queue a change and turn the worker's verdict into a redirect."""
    res = _queue_and_wait(name, payload, wait_seconds)
    if res is None:
        return _inst_back(
            name, err="Die Änderung läuft noch — bitte gleich erneut prüfen.")
    if res.get("ok"):
        return _inst_back(name, msg="Gespeichert.")
    return _inst_back(name, err=res.get("message", "Speichern fehlgeschlagen."))


def _queue_and_wait(name, payload, wait_seconds):
    """Hand a change to the host-side worker and wait for its verdict.

    Returns the worker's result dict, or None if it did not answer in
    time. The request file may carry configuration values, so it is
    written 0600 -- it lives in the spool only until the worker
    consumes it.
    """
    return _queue_with_id(_uuid.uuid4().hex, name, payload, wait_seconds)


def _queue_with_id(rid, name, payload, wait_seconds):
    """Same, with the request id decided by the caller.

    An artifact upload has to write its package next to the request
    under that id (RFC-0019), so it cannot let the queue invent one.
    """
    os.makedirs(SPOOL_QUEUE, exist_ok=True)
    tmp = os.path.join(SPOOL_DIR, f".req-{rid}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": name,
                   "by": request.headers.get("X-OAAP-User", "?"),
                   "requested": datetime.now(timezone.utc).isoformat(),
                   **payload}, f)
    os.replace(tmp, os.path.join(SPOOL_QUEUE, f"{rid}.json"))
    res_path = os.path.join(SPOOL_RESULTS, f"{rid}.json")
    deadline = _time.time() + wait_seconds
    while _time.time() < deadline:
        if os.path.exists(res_path):
            try:
                with open(res_path, encoding="utf-8") as f:
                    return json.load(f)
            finally:
                os.remove(res_path)
        _time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# First-run wizard

@app.get("/setup")
def setup_form():
    return render_template_string(SETUP_PAGE, done=setup_done(), error=None)


SETUP_PROFILE_WAIT_SECONDS = 15  # writes one small file, no docker work


@app.post("/setup")
def setup_submit():
    if setup_done():
        return render_template_string(SETUP_PAGE, done=True, error=None)
    token = request.form.get("token", "").strip()
    # The node profile is written BEFORE the admin is created, because
    # the host worker accepts it only while setup is still open (and
    # only against the real setup token, which it checks itself). Doing
    # it afterwards would race with the very flag that guards it.
    if request.form.get("profile_dev"):
        res = _queue_and_wait("", {"action": "node", "profiles": ["dev"],
                                   "setup_token": token},
                              SETUP_PROFILE_WAIT_SECONDS)
        if res is None or not res.get("ok"):
            # Stop before creating the admin: a half-applied first run is
            # worse than a repeated one, and the form can simply be sent
            # again (the setup token is still valid).
            why = res.get("message", "") if res else \
                "der Dienst auf dem Server hat nicht rechtzeitig geantwortet"
            return render_template_string(
                SETUP_PAGE, done=False,
                error=f"Das Knoten-Profil konnte nicht gesetzt werden ({why}). "
                      "Bitte erneut absenden — oder ohne Haken fortfahren und "
                      "das Profil später mit 'sudo oaap node add-profile dev' "
                      "setzen."), 503
    resp = INTERNAL.post(f"{IDENTITY}/internal/setup", json={
        "token": token,
        "username": request.form.get("username", ""),
        "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 201:
        return redirect("/auth/login", code=303)
    error = resp.json().get("error", f"Einrichtung fehlgeschlagen (HTTP {resp.status_code}).")
    return render_template_string(SETUP_PAGE, done=False, error=error), resp.status_code
