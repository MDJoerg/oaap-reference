#!/usr/bin/env python3
"""Das Portal-Geruest (Design-Leitlinien 0.2 §5 und 6.2.3, oaap.core.portal 0.3.33).

Gerendert wird page() selbst, fuer verschiedene Rollen:

  * die Kopfzeile traegt nur Marke und Benutzermenue (Profil, Passwort,
    Abmelden) -- kein Verwaltungspunkt mehr darin;
  * die Seitenleiste zeigt dem Serververwalter die Gruppen, dem einfachen
    Benutzer gar nichts (nur "Apps" = keine Leiste, kein Menueknopf);
  * eine lange Seite ohne Reiter bekommt Sprungmarken samt Skript, eine
    Seite mit Reitern nicht;
  * das einzige Formular des Geruests ist Abmelden (POST).

Aufruf (im Portal-Abbild): python3 test/test_portal_layout.py
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-layout-")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import app as portal  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:800]}")


ROLES = {"r": set()}
portal.caller_roles = lambda: ROLES["r"]
portal._can_build = lambda: "server_admin" in ROLES["r"]
portal._has_cohorts = lambda: False
portal.multi_tenant = lambda: False


def render(body, active, roles):
    ROLES["r"] = set(roles)
    with portal.app.test_request_context(
            "/", headers={"X-OAAP-User": "jo", "X-OAAP-Roles": ",".join(roles)}):
        html, status = portal.page(body, "Test", active)
    return html


def part(html, start, end):
    i = html.find(start)
    return html[i:html.find(end, i)] if i >= 0 else ""


SHORT = '<div class="card"><h2>Eins</h2></div>'
LONG = ('<div class="pagehead"><h1>Gesundheit</h1></div>'
        + "".join(f'<div class="card"><h2>{t}</h2><p>x</p></div>'
                  for t in ("Knoten", "Pflegestand", "Kernservices", "Apps")))
TABS = LONG + '<nav class="tabs"><a href="?tab=a" class="active">A</a></nav>'

print("Serververwalter")
h = render(SHORT, "health", ["server_admin"])
header = part(h, '<header class="oaap">', "</header>")
side = part(h, '<nav class="side"', "</nav>")
ok("Kopfzeile: keine Verwaltungspunkte mehr",
   not re.search(r'href="/(users|health|store|instances|keys|aufbau)', header), header)
ok("Kopfzeile: Benutzermenue mit Profil, Passwort, Abmelden",
   'class="usermenu"' in header and 'href="/auth/profile"' in header
   and 'href="/auth/password"' in header and 'action="/auth/logout"' in header)
ok("Abmelden ist ein POST und das einzige Formular des Geruests",
   h.count("<form") == 1 and '<form method="post" action="/auth/logout">' in h)
ok("Seitenleiste mit Gruppen",
   all(f"<summary>{g}</summary>" in side for g in ("Betrieb", "Menschen &amp; Zugänge",
                                                    "Katalog &amp; Daten", "Kommunikation")), side)
# 0.1.246: Gruppen klappen ein (<details>, ohne JavaScript offen); die Gruppe
# der aktiven Seite traegt data-active und bleibt offen.
ok("Gruppen einklappbar, ohne Skript offen", side.count("<details open") == 4, side)
ok("nur die Gruppe der aktiven Seite ist markiert",
   re.findall(r'data-group="([^"]+)" data-active="1"', side) == ["Betrieb"], side)
ok("Gesundheit ist aktiv, sonst keiner",
   re.findall(r'<a href="([^"]+)" class="active"', side) == ["/health"], side)
ok("kein Zwilling ohne Mandantenrolle", 'href="/zwilling"' not in side)
ok("Menueknopf fuer schmale Bildschirme", 'for="navtoggle"' in h and 'id="navtoggle"' in h)
# 0.1.246 (Joergs Browsertest 08.11.): die Eintraege scrollen fuer sich, die
# Plattformversion steht als Fusszeile der Leiste, die Kopfhoehe wird gemessen.
ok("Seitenleiste: Eintraege in eigenem Scrollbereich", '<div class="links">' in side, side)
ok("Seitenleiste: Plattformversion als Fusszeile",
   re.search(r'<div class="navfoot">OAAP-Plattform [^<]+</div>', side) is not None, side)
ok("Kopfhoehe gemessen statt angenommen", "--hdr-h',h.offsetHeight" in h)

print("\nEinfacher Benutzer")
h = render(SHORT, "apps", ["user"])
ok("Zwilling + Apps = zwei Eintraege: Seitenleiste", '<nav class="side"' in h)
h = render(SHORT, "apps", ["guest"])
ok("nur Apps: keine Seitenleiste", '<nav class="side"' not in h, h[-1200:])
ok("nur Apps: kein Menueknopf", 'id="navtoggle"' not in h and 'for="navtoggle"' not in h)
ok("Benutzermenue bleibt", 'class="usermenu"' in h and "Abmelden" in h)

print("\nSprungmarken")
h = render(LONG, "health", ["server_admin"])
jumps = part(h, '<nav class="jumps"', "</nav>")
ok("lange Seite: vier Marken", jumps.count("<a href=\"#abschnitt-") == 4, jumps)
ok("die Ziele stehen im Dokument", 'id="abschnitt-kernservices"' in h)
ok("Skript zum Mitleuchten dabei", "IntersectionObserver" in h)
ok("Leiste im Arbeitsbereich, nach dem Seitenkopf",
   h.index("<main>") < h.index("</h1>") < h.index('<nav class="jumps"'))
h = render(TABS, "instances", ["server_admin"])
ok("Seite mit Reitern: keine Sprungmarken, kein Skript",
   '<nav class="jumps"' not in h and "IntersectionObserver" not in h)
h = render(SHORT, "health", ["server_admin"])
ok("kurze Seite: keine Sprungmarken", '<nav class="jumps"' not in h)

print(f"\n{'OK' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
