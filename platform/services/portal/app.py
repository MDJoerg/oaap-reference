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
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, redirect, render_template_string, request
from markupsafe import Markup

IDENTITY = "http://identity:8000"
GATEWAY = "http://gateway:80"
VERSION = os.environ.get("OAAP_VERSION", "unknown")
REGISTRY = "/apps-registry/registry.json"

ALL_ROLES = ("server_admin", "admin", "keyuser", "user", "guest", "partner")
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
    {% if is_server_admin %}<a href="/users" class="{{ 'active' if active == 'users' }}">Benutzer</a>{% endif %}
    {% if can_health %}<a href="/health" class="{{ 'active' if active == 'health' }}">Gesundheit</a>{% endif %}
    {% if is_server_admin %}<a href="/store" class="{{ 'active' if active == 'store' }}">Store</a>{% endif %}
    {% if is_server_admin %}<a href="/instances" class="{{ 'active' if active == 'instances' }}">Instanzen</a>{% endif %}
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
  <tr><th>Benutzername</th><th>Anzeigename</th><th>Rollen</th><th>Gruppen</th><th>Status</th><th></th></tr>
  {% for u in users %}
  <tr class="rowlink">
    <td><a class="rowaction" href="/users/{{ u.username }}">{{ u.username }}</a></td>
    <td>{{ u.display_name }}</td>
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

STORE_BODY = """
<h1>Store</h1>
{% if msg %}
<div class="card"><p class="{{ 'ok' if msg_ok else 'err' }}" style="margin:0">{{ msg }}</p></div>
{% endif %}
{% if not sources %}
<div class="card">
  <p class="muted">Noch keine Store-Quelle eingetragen. Eine Quelle ist
  eine URL auf eine <code>oaap-store.json</code>-Liste — hinzufügen mit:</p>
  <p><code>sudo oaap store add-source &lt;url&gt;</code></p>
</div>
{% endif %}
{% for src in sources %}
<div class="card">
  <h2>{{ src.title }} <span class="badge {{ 'test' if src.trust != 'platform' }}">{{ src.trust_label }}</span></h2>
  <p class="muted">{{ src.url }}{% if src.origin %} — Herkunft: {{ src.origin }}{% endif %}</p>
  {% if src.confirm %}
  <p class="muted">Diese Liste ist nicht geprüft. Eine Installation daraus
  muss einzeln bestätigt werden und wird mit Deinem Namen protokolliert.</p>
  {% endif %}
  {% if src.error %}
    <p class="err">Quelle nicht lesbar: {{ src.error }}</p>
  {% elif not src.apps %}
    <p class="muted">Diese Liste enthält keine Apps.</p>
  {% else %}
    {% for a in src.apps %}
    <div style="border-top:1px solid var(--oaap-border);padding:.9rem 0">
      <div style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap">
        <svg viewBox="0 0 100 100" width="20" height="20" aria-hidden="true">
          <polygon points="50,6 71,18 71,42 50,54 29,42 29,18" fill="#2563eb"/>
          <polygon points="28,44 49,56 49,80 28,92 7,80 7,56" fill="none" stroke="#2563eb" stroke-width="5"/>
          <polygon points="72,44 93,56 93,80 72,92 51,80 51,56" fill="none" stroke="#2563eb" stroke-width="5"/></svg>
        <strong>{{ a.name }}</strong>
        <span class="badge">{{ a.type }}</span>
        <span class="muted">v{{ a.version }}</span>
        {% if a.installed %}<span class="badge test">installiert ({{ a.installed }})</span>{% endif %}
        {% if a.homepage %}<a class="muted" href="{{ a.homepage }}" target="_blank" rel="noopener">Homepage ↗</a>{% endif %}
        {% if a.license %}<span class="muted">Lizenz: {{ a.license }}</span>{% endif %}
      </div>
      <p class="muted" style="margin:.4rem 0">{{ a.description }}</p>
      {% if a.command and a.id %}
        {% if a.pending %}
        <p class="muted" style="margin:.2rem 0 0">⏳ Installation läuft — das
        Ergebnis erscheint hier und im Deploy-Protokoll (Gesundheitsseite).</p>
        {% elif a.installed == a.version %}
        <p class="ok" style="margin:.2rem 0 0">Auf dem aktuellen Stand.</p>
        {% else %}
        <form method="post" action="/store/install" style="margin:.2rem 0 0"
              onsubmit="this.querySelector('button').disabled=true;
                        this.querySelector('button').textContent='Wird installiert …'">
          <input type="hidden" name="app_id" value="{{ a.id }}">
          <input type="hidden" name="source_id" value="{{ src.id }}">
          {% if src.confirm %}
          <label class="checkline"><input type="checkbox" name="confirm_source"
                 value="{{ src.id }}" required>
            Ich installiere bewusst aus der ungeprüften Quelle „{{ src.title }}".</label>
          {% endif %}
          <button>{{ ('Aktualisieren auf v' + a.version) if a.installed else 'Installieren' }}</button>
        </form>
        {% endif %}
      <details style="margin:.4rem 0 0"><summary class="muted">Installation von Hand (CLI)</summary>
      <code style="display:block;background:#f8fafc;border:1px solid var(--oaap-border);
                   border-radius:.4rem;padding:.5rem .7rem;overflow-x:auto;white-space:pre">{{ a.command }}</code>
      </details>
      {% else %}
      <p class="muted">Paketquelle wird von diesem Durchstich noch nicht unterstützt.</p>
      {% endif %}
    </div>
    {% endfor %}
  {% endif %}
</div>
{% endfor %}
<p class="muted">Ein Klick auf „Installieren" installiert die App auf dem
Produktions-Kanal; der Server löst die App dabei selbst gegen die
konfigurierten Quellen auf. Nennen mehrere Quellen dieselbe App, gewinnt
die höchste Vertrauensklasse — nicht die zuerst eingetragene Quelle
(RFC-0012 §3). Quellen verwaltet die Administration mit
<code>sudo oaap store list|add-source|remove-source|enable|disable|trust</code>.</p>
"""

# Floorplan "Listenbericht" — installed app instances and their
# visibility setting (RFC-0007). server_admin only.
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
{% if instances %}
<div class="card" style="overflow-x:auto;padding:.4rem 1.4rem">
<table>
  <tr><th>Instanz</th><th>App</th><th>Kanal</th><th>Sichtbarkeit</th><th></th></tr>
  {% for i in instances %}
  <tr class="rowlink">
    <td><a class="rowaction" href="/instances/{{ i.name }}">{{ i.name }}</a></td>
    <td>{{ i.app_name }} <span class="muted">v{{ i.version }}</span></td>
    <td><span class="badge {{ i.channel }}">{{ i.channel_label }}</span></td>
    <td>{{ i.visibility_label }}</td>
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
<form method="post" action="/instances/new">
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

# Floorplan "Objektseite" (design guidelines 6.2).
INSTANCE_EDIT_BODY = """
<a class="back" href="/instances">← Zurück zur Liste</a>
<h1>{{ i.name }} <span class="muted">({{ i.app_name }} v{{ i.version }})</span></h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
<form method="post" action="/instances/{{ i.name }}/visibility">
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
<form method="post" action="/instances/{{ i.name }}/address">
  <div class="card">
    <h2>Eigene Adresse</h2>
    <p class="muted">Automatisch erreichbar unter
       <code>{{ i.auto_address or "— (dieser Knoten hat keinen externen Namen)" }}</code>.
       Zusätzlich kann diese Instanz einen eigenen öffentlichen Namen tragen —
       sinnvoll, wenn die Adresse in ausgelieferte Software eingebaut wird und
       einen späteren Umzug überleben soll.</p>
    <label>Eigener Name <input type="text" name="hostname" value="{{ i.address }}"
           placeholder="z. B. hub.meine-domain.de"></label>
    <p class="muted">Der Name muss selbst auf diesen Knoten zeigen (DNS-Eintrag
       und Portfreigabe bleiben Deine Sache). Das Zertifikat holt die Plattform
       beim ersten Zugriff. Die automatische Adresse bleibt gültig.</p>
    <button>Speichern</button>
    {% if i.address %}<button name="op" value="remove" class="secondary">Namen entfernen</button>{% endif %}
  </div>
</form>
{% if i.has_public_route %}
<form method="post" action="/instances/{{ i.name }}/throttle">
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
    <input type="hidden" name="op" value="create">
    <button>{{ "Neues Token erzeugen" if i.token_created else "Token erzeugen" }}</button>
  </form>
  {% if i.token_created %}
  <form method="post" action="/instances/{{ i.name }}/token" style="display:inline">
    <input type="hidden" name="op" value="revoke">
    <button class="secondary">Widerrufen</button>
  </form>
  {% endif %}
  <p class="muted">Ein Deploy-Token erlaubt genau eines: diese Test-Instanz aus
     ihrer hinterlegten Quelle neu zu deployen. Keine Anmeldung, kein Zugriff
     auf Daten, keine Änderung an Routen oder Rollen. Produktiv-Instanzen
     bekommen grundsätzlich kein Token.</p>
</div>
{% endif %}
{% if i.config %}
<form method="post" action="/instances/{{ i.name }}/config">
  <div class="card">
    <h2>Konfiguration</h2>
    {% for c in i.config %}
    <label>{{ c.label }}
      {% if c.secret %}
      <input type="password" name="cfg-{{ c.key }}" value="" autocomplete="new-password"
             placeholder="{{ 'gesetzt — leer lassen, um ihn zu behalten' if c.is_set else 'noch nicht gesetzt' }}">
      {% else %}
      <input type="text" name="cfg-{{ c.key }}" value="{{ c.value }}">
      {% endif %}
    </label>
    <p class="muted"><code>{{ c.key }}</code>{% if c.secret %} — vertraulich,
       wird nie angezeigt{% endif %}</p>
    {% endfor %}
    <p class="muted">Diese Werte deklariert die App in ihrem Manifest; andere
       lassen sich hier nicht setzen. Beim Speichern wird der Container mit
       den neuen Werten neu erzeugt — die App ist dabei kurz nicht
       erreichbar. Daten, Adresse und Version bleiben unverändert.</p>
    <button>Speichern</button>
  </div>
</form>
{% endif %}
<form method="post" action="/instances/{{ i.name }}/remove">
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


def page(body_template, title, active, status=200, **ctx):
    body = render_template_string(body_template, **ctx)
    roles = request.headers.get("X-OAAP-Roles", "")
    caller = caller_roles()
    return render_template_string(
        LAYOUT,
        title=title, active=active, body=Markup(body), logo=LOGO_SVG,
        user=request.headers.get("X-OAAP-User", "?"), roles=roles or "?",
        is_server_admin="server_admin" in caller,
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
PROFILE_LABELS = {
    "dev": "Entwicklungsknoten — das Portal darf Test-Instanzen anlegen "
           "und aus einer noch nicht gelisteten Quelle installieren",
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


def launchpad_tiles(user_roles, user_groups, host):
    """Role- and group-filtered app tiles from the instance registry
    (spec 2.5, RFC-0007). The filter is UX only — the gateway enforces
    both on every request regardless of what the portal shows (mirrored
    exactly: no bypass here that the gateway's /verify does not also
    grant, and vice versa — server_admin bypasses the group check only,
    same as /verify).
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
    tiles = []
    for name, inst in sorted(load_instances().items()):
        allowed = set(inst.get("roles") or [])
        if allowed and not user_roles & allowed:
            continue
        vis_groups = set((inst.get("visibility") or {}).get("groups") or [])
        if vis_groups and not is_server_admin and not user_groups & vis_groups:
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
    return tiles


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
    return requests.get(f"{IDENTITY}/internal/status", timeout=5).json()["setup_done"]


@app.get("/")
def dashboard():
    return page(
        DASHBOARD_BODY, "Apps", "apps",
        tiles=launchpad_tiles(caller_roles(), caller_groups(), request.host.split(":")[0]),
    )


# ---------------------------------------------------------------------------
# User management (spec oaap.core.identity 2.4) — server_admin only
# (RFC-0008: this operates on the server itself, not on one app's own
# data), floorplans "Listenbericht" and "Objektseite". The gateway has
# already authenticated the caller; the portal checks the server_admin
# role and delegates the operations to identity's internal API.

def require_server_admin():
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: erfordert die Rolle server_admin.", 403
    return None


def identity_users():
    return requests.get(f"{IDENTITY}/internal/users", timeout=5).json()["users"]


def _parse_groups(raw):
    """Free-form group tags (RFC-0007) from a comma-separated form field."""
    return sorted({g.strip().lower() for g in raw.split(",") if g.strip()})


@app.get("/users")
def users_list():
    denied = require_server_admin()
    if denied:
        return denied
    return page(USERS_LIST_BODY, "Benutzer", "users", users=identity_users(),
                msg=request.args.get("msg"), error=request.args.get("err"))


@app.get("/users/new")
def users_new():
    denied = require_server_admin()
    if denied:
        return denied
    return page(USER_NEW_BODY, "Benutzer anlegen", "users",
                all_roles=ALL_ROLES, error=None,
                form={"username": "", "display_name": "", "roles": ["user"], "groups": []})


@app.post("/users/create")
def users_create():
    denied = require_server_admin()
    if denied:
        return denied
    form = {
        "username": request.form.get("username", "").strip(),
        "display_name": request.form.get("display_name", ""),
        "roles": request.form.getlist("roles"),
        "groups": _parse_groups(request.form.get("groups", "")),
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
    denied = require_server_admin()
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
    denied = require_server_admin()
    if denied:
        return denied
    resp = requests.put(f"{IDENTITY}/internal/users/{username}", json={
        "display_name": request.form.get("display_name", ""),
        "roles": request.form.getlist("roles"),
        "groups": _parse_groups(request.form.get("groups", "")),
        "active": request.form.get("active") == "on",
    }, timeout=5)
    if resp.status_code == 200:
        return redirect(f"/users/{username}?msg={quote('Gespeichert.')}", code=303)
    return redirect(f"/users/{username}?err={quote(resp.json().get('error', 'Speichern fehlgeschlagen.'))}", code=303)


@app.post("/users/<username>/password")
def users_password(username):
    denied = require_server_admin()
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
    """Names this node hands out to the world, with their origin."""
    names = []
    host = external_host()
    if host:
        names.append({"name": host, "what": "Knoten"})
    for inst_name, inst in sorted(load_instances().items()):
        if inst.get("address"):
            names.append({"name": inst["address"], "what": f"Instanz {inst_name}"})
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
    import socket
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)})
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
        row = dict(entry, resolved=", ".join(ips) or "–")
        if not ips:
            row["state"], row["label"] = "err", "Löst nicht auf"
        elif result["note"]:
            row["state"], row["label"] = "unknown", "Löst auf"
        elif not result["public_ip"]:
            row["state"], row["label"] = "unknown", "Nicht vergleichbar"
        elif result["public_ip"] in ips:
            row["state"], row["label"] = "ok", "Zeigt hierher"
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


def braked_requests():
    """Per-instance count of throttled requests (RFC-0010 decision 2)."""
    instances = load_instances()
    public = {n for n, i in instances.items()
              if any("public" in (r.get("roles") or []) for r in (i.get("routes") or []))}
    if not public:
        return None
    try:
        data = requests.get(f"{IDENTITY}/internal/throttle-braked", timeout=3).json()
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
    if not caller_roles() & {"server_admin", "partner"}:
        return "Zugriff verweigert: Gesundheit erfordert die Rolle server_admin oder partner.", 403

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
            # Wrapped apps often answer their root with a redirect —
            # any response below 400 counts as alive.
            if state == "warn" and detail.startswith("HTTP 3"):
                state, label = "ok", "Gesund"
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
                core=core, apps=apps, ext=external_access(),
                dns=dns_check(), braked=braked_requests(),
                deploys=recent_deploys())


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
DEPLOY_THROTTLE = os.path.join(SPOOL_DIR, ".throttle.json")
DEPLOY_TOKENS = "/apps-registry/deploy-tokens.json"
DEPLOY_LOG = "/apps-registry/deploy-log.jsonl"
DEPLOY_WAIT_SECONDS = 120
DENIED = {"error": "unknown instance or invalid token"}


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "?"


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


def _deploy_auth(name):
    """One indistinguishable answer for every failure (spec test 13)."""
    key = _client_ip()
    if _deploy_blocked(key):
        return {"error": "too many attempts — wait a minute"}, 429
    inst = (load_instances().get(name)
            if _re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or "") else None)
    if not inst or inst.get("channel") != "test" or not _valid_deploy_token(name):
        _deploy_failed(key)
        return DENIED, 403
    _deploy_succeeded(key)
    return inst, None


def _entry_url(name, inst):
    ext = external_host()
    if ext:
        return f"https://{name}.{ext}/"
    return f"http://{request.host.split(':')[0]}:{inst['port']}/"


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
                    "version": res.get("version", ""),
                    "revision": res.get("revision", ""),
                    "message": res.get("message", ""),
                    "url": _entry_url(name, inst)}
            return body, (200 if res.get("ok") else 502)
        _time.sleep(2)
    return {"ok": None, "instance": name,
            "message": "deployment is still running — poll GET "
                       f"/deploy/{name}/status"}, 202


@app.get("/deploy/<name>/status")
def deploy_status(name):
    inst, err = _deploy_auth(name)
    if err:
        return inst, err
    for entry in recent_deploys(limit=50):
        if entry.get("instance") == name:
            entry["url"] = _entry_url(name, inst)
            return entry, 200
    return {"instance": name, "message": "no deployment recorded yet"}, 200


# ---------------------------------------------------------------------------
# Store (admin only) — reads the configured source lists and offers
# one-click installation (runtime spec 2.6): the portal queues only the
# app id; the host-side worker resolves it against the configured
# sources itself and installs from what that lookup returns.

STORE_SOURCES_FILE = "/apps-registry/store-sources.json"
INSTALL_WAIT_SECONDS = 120  # < gunicorn --timeout (150s)

# Trust classes (RFC-0012 §3). appctl.py is the authority — it migrates
# old {url, name} entries into this form and writes them back. The
# portal only reads, and reads tolerantly: an entry it cannot classify
# counts as unverified, which is the cautious direction to fall in.
TRUST_LABEL = {"platform": "von uns", "verified": "geprüft",
               "unverified": "muss bestätigt werden"}


def configured_sources():
    """Enabled store sources, in the object form of RFC-0012 §2."""
    try:
        with open(STORE_SOURCES_FILE, encoding="utf-8") as f:
            raw = json.load(f).get("sources", [])
    except (OSError, ValueError):
        return []
    out = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict) or not s.get("url") or not s.get("enabled", True):
            continue
        trust = s.get("trust") if s.get("trust") in TRUST_LABEL else "unverified"
        out.append({"id": s.get("id") or f"quelle-{i}", "url": s["url"],
                    "name": s.get("name") or "", "trust": trust,
                    "origin": s.get("origin") or ""})
    return out


def pending_installs():
    """App ids with a queued/running store install (spool not yet done).

    Without this the store page offers "Installieren" again while the
    worker is still pulling images — a second click then fails
    (Jörgs Befund 2026-08-06)."""
    pending = set()
    try:
        for fn in os.listdir(SPOOL_QUEUE):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SPOOL_QUEUE, fn), encoding="utf-8") as f:
                    req = json.load(f)
            except (OSError, ValueError):
                continue
            if req.get("action") == "install":
                pending.add(req.get("instance"))
    except OSError:
        pass
    return pending


def store_page(msg=None, msg_ok=True, status=200):
    configured = configured_sources()
    pending = pending_installs()
    installed = {inst.get("app_id"): inst.get("version")
                 for inst in load_instances().values()}
    sources = []
    for src in configured:
        entry = {"url": src["url"], "title": src["name"] or "Store-Quelle",
                 "id": src["id"], "trust": src["trust"],
                 "trust_label": TRUST_LABEL[src["trust"]],
                 "origin": src["origin"],
                 "confirm": src["trust"] == "unverified",
                 "apps": [], "error": None}
        try:
            r = requests.get(entry["url"], timeout=4)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            # Show the cause, not just the class — "ConnectionError"
            # alone hides whether it is DNS, routing, or TLS.
            entry["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            sources.append(entry)
            continue
        entry["title"] = src.get("name") or data.get("name") or "Store-Quelle"
        for a in data.get("apps", []):
            pkg = a.get("package") or {}
            command = None
            if pkg.get("git"):
                command = f"sudo oaap app install {pkg['git']}"
                if pkg.get("path"):
                    command += f" --path {pkg['path']}"
            entry["apps"].append({
                "id": a.get("id", ""),
                "name": a.get("name", a.get("id", "?")),
                "description": a.get("description", ""),
                "type": a.get("type", "?"), "version": a.get("version", "?"),
                "license": a.get("license", ""), "homepage": a.get("homepage", ""),
                "installed": installed.get(a.get("id")),
                "pending": a.get("id") in pending,
                "command": command,
            })
        sources.append(entry)
    return page(STORE_BODY, "Store", "store", status=status,
                sources=sources, msg=msg, msg_ok=msg_ok)


@app.get("/store")
def store():
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: der Store erfordert die Rolle server_admin.", 403
    return store_page()


@app.post("/store/install")
def store_install():
    if "server_admin" not in caller_roles():
        return "Zugriff verweigert: der Store erfordert die Rolle server_admin.", 403
    app_id = request.form.get("app_id", "").strip()
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]*", app_id):
        return store_page("Ungültige App-Kennung.", msg_ok=False, status=400)
    # Queue for the host worker. The request names the app id and the
    # source the user was looking at — never a package URL or a version:
    # the worker resolves both against the CONFIGURED sources on the
    # host (spec 2.6, RFC-0012 §3). Picking among the sources the
    # server_admin already chose is all a request can do; the spool is
    # data, not trust.
    source_id = request.form.get("source_id", "").strip()
    confirm = request.form.get("confirm_source", "").strip()
    src = next((s for s in configured_sources() if s["id"] == source_id), None)
    if source_id and not src:
        return store_page("Diese Store-Quelle ist auf diesem Knoten nicht "
                          "(mehr) eingetragen.", msg_ok=False, status=400)
    if src and src["trust"] == "unverified" and confirm != source_id:
        label = src["name"] or source_id
        return store_page("Nicht bestätigt: " + label + " ist eine ungeprüfte "
                          "Quelle. Bitte das Häkchen setzen.",
                          msg_ok=False, status=400)
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
                return store_page(f"'{app_id}' wurde installiert"
                                  + (f" (Version {v})" if v else "")
                                  + " — die Kachel erscheint im Launchpad.")
            return store_page(f"Installation von '{app_id}' fehlgeschlagen: "
                              f"{res.get('message', 'unbekannter Fehler')}",
                              msg_ok=False, status=502)
        _time.sleep(2)
    return store_page(f"Die Installation von '{app_id}' läuft noch — das "
                      "Ergebnis erscheint im Deploy-Protokoll auf der "
                      "Gesundheitsseite.", status=202)


# ---------------------------------------------------------------------------
# App-instance visibility (RFC-0007) and configuration (spec 2.3/2.4.3)
# — server_admin only. /apps-registry
# is mounted read-only in this container (like the store install above),
# so a change is queued to the host-side worker (appctl.py
# process-deploys), which updates the registry, regenerates that
# instance's Caddy site(s) and reloads the gateway.

VISIBILITY_WAIT_SECONDS = 20  # registry+Caddy+reload only, no docker work


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
        rows.append({
            "key": key, "label": c.get("label") or key, "secret": secret,
            "is_set": bool(env.get(key)),
            # a secret value never leaves the server, not even prefilled
            "value": "" if secret else env.get(key, ""),
        })
    return rows


@app.get("/instances")
def instances_list():
    denied = require_server_admin()
    if denied:
        return denied
    rows = []
    for name, inst in sorted(load_instances().items()):
        groups = _instance_groups(inst)
        channel = inst.get("channel", "production")
        rows.append({
            "name": name, "app_name": inst.get("app_name", name),
            "version": inst.get("version", "?"),
            "channel": channel, "channel_label": CHANNEL_LABELS.get(channel, channel),
            "visibility_label": "Alle" if not groups else "Gruppen: " + ", ".join(groups),
        })
    return page(INSTANCES_LIST_BODY, "Instanzen", "instances", instances=rows,
                can_create="dev" in node_profiles(),
                msg=request.args.get("msg"), error=request.args.get("err"))


# --- creating a test instance from the portal (RFC-0011, profile `dev`) ---
# The store's one-click install (2.6) stays untouched and available on
# every node. What the profile adds here is the development case: a test
# instance, optionally from a repository no store list carries yet.

CREATE_WAIT_SECONDS = 120  # clone + build + start, like a store install


TRUST_RANK = {"platform": 3, "verified": 2, "unverified": 1}


def _store_apps():
    """Every app the configured store sources list, for the create form.

    One entry per app id, resolved the same way the host resolves it
    (RFC-0012 §3): highest trust class first, configured order within a
    class. Showing two entries for one id would only invite a choice the
    host would then overrule.
    """
    apps = {}
    for pos, src in enumerate(configured_sources()):
        try:
            r = requests.get(src["url"], timeout=4)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            continue
        title = src["name"] or data.get("name") or "Store-Quelle"
        for a in data.get("apps", []):
            if not a.get("id") or not (a.get("package") or {}).get("git"):
                continue
            cand = {"id": a["id"], "name": a.get("name", a["id"]),
                    "version": a.get("version", "?"), "source": title,
                    "source_id": src["id"], "trust": src["trust"],
                    "trust_label": TRUST_LABEL[src["trust"]], "pos": pos}
            best = apps.get(a["id"])
            if best is None or (TRUST_RANK[cand["trust"]], -cand["pos"]) \
                    > (TRUST_RANK[best["trust"]], -best["pos"]):
                apps[a["id"]] = cand
    return sorted(apps.values(), key=lambda a: (a["name"].lower(), a["source"]))


def _require_dev_node():
    """Guard for the create path — the profile is checked twice.

    Here for a readable answer, and again on the host, where the
    decision actually is: the spool is data, not trust.
    """
    denied = require_server_admin()
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
    res = _queue_and_wait(name, {
        "action": "create", "from": from_, "app_id": app_id,
        "source_id": source_id, "confirm_source": confirm,
        "url": url, "path": request.form.get("path", "").strip(),
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


@app.get("/instances/<name>")
def instance_detail(name):
    denied = require_server_admin()
    if denied:
        return denied
    inst = load_instances().get(name)
    if not inst:
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    i = {"name": name, "app_name": inst.get("app_name", name),
         "version": inst.get("version", "?"),
         "groups": _instance_groups(inst), "roles": inst.get("roles") or [],
         "config": _instance_config(name, inst),
         "is_test": inst.get("channel") == "test",
         "token_created": _token_created(name),
         "hook_url": _hook_url(name),
         "address": inst.get("address", ""),
         "auto_address": f"{name}.{external_host()}" if external_host() else "",
         "has_public_route": any("public" in (r.get("roles") or [])
                                 for r in inst.get("routes") or []),
         **_throttle_view(inst)}
    return page(INSTANCE_EDIT_BODY, f"Instanz {name}", "instances", i=i,
                msg=request.args.get("msg"), error=request.args.get("err"))


@app.post("/instances/<name>/visibility")
def instance_visibility(name):
    denied = require_server_admin()
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    mode = request.form.get("mode", "all")
    groups = _parse_groups(request.form.get("groups", "")) if mode == "groups" else []
    if mode == "groups" and not groups:
        return redirect(
            f"/instances/{name}?err={quote('Bitte mindestens eine Gruppe angeben oder Alle wählen.')}",
            code=303)
    return _queue_and_redirect(name, {"action": "visibility", "groups": groups},
                               VISIBILITY_WAIT_SECONDS)


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
    denied = require_server_admin()
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    hostname = (request.form.get("hostname") or "").strip()
    if request.form.get("op") == "remove" or not hostname:
        return _queue_and_redirect(name, {"action": "address", "op": "remove"},
                                   ADDRESS_WAIT_SECONDS)
    return _queue_and_redirect(name, {"action": "address", "hostname": hostname},
                               ADDRESS_WAIT_SECONDS)


@app.post("/instances/<name>/throttle")
def instance_throttle(name):
    denied = require_server_admin()
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
    denied = require_server_admin()
    if denied:
        return denied
    if not load_instances().get(name):
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    # Typing the name is the guard against a misclick on the one
    # destructive control in this UI; the host checks it a second time.
    if (request.form.get("confirm") or "").strip() != name:
        return redirect(
            f"/instances/{name}?err={quote('Zum Entfernen bitte den Instanznamen eintippen.')}",
            code=303)
    res = _queue_and_wait(name, {"action": "remove",
                                 "confirm": name,
                                 "purge": bool(request.form.get("purge"))},
                          REMOVE_WAIT_SECONDS)
    if res is None:
        return redirect(
            f"/instances?err={quote('Das Entfernen läuft noch — bitte die Liste gleich erneut prüfen.')}",
            code=303)
    if not res.get("ok"):
        return redirect(
            f"/instances/{name}?err={quote(res.get('message', 'Entfernen fehlgeschlagen.'))}",
            code=303)
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


@app.post("/instances/<name>/token")
def instance_token(name):
    denied = require_server_admin()
    if denied:
        return denied
    inst = load_instances().get(name)
    if not inst:
        return redirect(f"/instances?err={quote('Instanz nicht gefunden.')}", code=303)
    if inst.get("channel") != "test":
        return redirect(
            f"/instances/{name}?err={quote('Deploy-Token gibt es nur für Test-Instanzen.')}",
            code=303)
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
        return redirect(
            f"/instances/{name}?err={quote('Die Ausstellung läuft noch — bitte gleich erneut prüfen.')}",
            code=303)
    if not res.get("ok"):
        return redirect(
            f"/instances/{name}?err={quote(res.get('message', 'Token konnte nicht erzeugt werden.'))}",
            code=303)
    # Rendered directly, NOT via Post/Redirect/Get: a redirect would put
    # the token into a URL, and the gateway logs full URIs.
    return page(TOKEN_SHOW_BODY, f"Deploy-Token {name}", "instances",
                name=name, token=token, hook_url=_hook_url(name))


@app.post("/instances/<name>/config")
def instance_config(name):
    denied = require_server_admin()
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
        if c["secret"] and submitted == "":
            continue
        values[c["key"]] = submitted
    if not values:
        return redirect(f"/instances/{name}?msg={quote('Keine Änderung.')}", code=303)
    return _queue_and_redirect(name, {"action": "config", "values": values},
                               CONFIG_WAIT_SECONDS)


def _queue_and_redirect(name, payload, wait_seconds):
    """Queue a change and turn the worker's verdict into a redirect."""
    res = _queue_and_wait(name, payload, wait_seconds)
    if res is None:
        return redirect(
            f"/instances/{name}?err={quote('Die Änderung läuft noch — bitte gleich erneut prüfen.')}",
            code=303)
    if res.get("ok"):
        return redirect(f"/instances/{name}?msg={quote('Gespeichert.')}", code=303)
    return redirect(
        f"/instances/{name}?err={quote(res.get('message', 'Speichern fehlgeschlagen.'))}",
        code=303)


def _queue_and_wait(name, payload, wait_seconds):
    """Hand a change to the host-side worker and wait for its verdict.

    Returns the worker's result dict, or None if it did not answer in
    time. The request file may carry configuration values, so it is
    written 0600 -- it lives in the spool only until the worker
    consumes it.
    """
    rid = _uuid.uuid4().hex
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
    resp = requests.post(f"{IDENTITY}/internal/setup", json={
        "token": token,
        "username": request.form.get("username", ""),
        "password": request.form.get("password", ""),
    }, timeout=5)
    if resp.status_code == 201:
        return redirect("/auth/login", code=303)
    error = resp.json().get("error", f"Einrichtung fehlgeschlagen (HTTP {resp.status_code}).")
    return render_template_string(SETUP_PAGE, done=False, error=error), resp.status_code
