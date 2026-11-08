#!/usr/bin/env python3
"""Portal-Navigation (Design-Leitlinien §5 und 6.2.3, nav_view.py).

Gemessen wird:

  * die Seitenleiste zeigt genau die Eintraege, die die Rollen frueher in
    der Kopfzeile zeigten -- nur in Gruppen; leere Gruppen fehlen;
  * ein einfacher Benutzer (nur "Apps") bekommt KEINE Seitenleiste;
  * eine lange Seite ohne Reiter bekommt Sprungmarken aus ihren
    Kartenueberschriften, eine Seite mit Reitern nicht (EINE fixierte
    Leiste je Seite), eine kurze Seite auch nicht;
  * eine Warnkarte (anstehende Entscheidung) und h2.section bekommen
    keine Marke; gleiche Ueberschriften bekommen verschiedene Ziele.

Braucht nur die Standardbibliothek.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import nav_view as nv                                         # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def labels(groups):
    return [l["label"] for g in groups for l in g["links"]]


# --- Seitenleiste -----------------------------------------------------------
print("Seitenleiste")
plain = nv.nav_groups({}, "apps")
ok("einfacher Benutzer: nur Apps", labels(plain) == ["Apps"], plain)
ok("einfacher Benutzer: keine Seitenleiste", not nv.show_side(plain))

server_admin = {"is_user_admin": True, "can_health": True, "can_store": True,
                "can_build": True, "has_cohorts": True}
g = nv.nav_groups(server_admin, "health")
ok("Serververwalter auf Einzelknoten: alles ausser Zwilling und Mandant",
   labels(g) == ["Apps", "Instanzen", "Gesundheit", "Aufbau", "Datenträger",
                 "Benutzer", "Zugänge", "Kohorten", "Store", "Nachrichten", "Destinationen"], labels(g))
ok("Gruppen in fester Reihenfolge, leere fehlen nicht hier",
   [x["title"] for x in g] == ["Betrieb", "Menschen & Zugänge",
                               "Katalog & Daten", "Kommunikation"])
ok("genau ein Eintrag aktiv, der richtige",
   [l["label"] for x in g for l in x["links"] if l["active"]] == ["Gesundheit"])
ok("Seitenleiste erscheint", nv.show_side(g))

ten = nv.nav_groups({"is_user_admin": True, "can_store": True, "can_twin": True,
                     "show_tenant_admin": True}, "tenant")
ok("Mandantenverwalter: Mandant und Zwilling, kein Aufbau/Gesundheit",
   "Mandant" in labels(ten) and "Zwilling" in labels(ten)
   and "Aufbau" not in labels(ten) and "Gesundheit" not in labels(ten), labels(ten))

support = nv.nav_groups({"can_health": True}, "health")
ok("support: Apps + Gesundheit, eine Gruppe",
   labels(support) == ["Apps", "Gesundheit"] and len(support) == 1, support)

user = nv.nav_groups({"can_twin": True}, "twin")
ok("Benutzer mit Zwilling: zwei Gruppen, leere fehlen",
   [x["title"] for x in user] == ["Betrieb", "Katalog & Daten"], user)

ok("unbekanntes Merkmal blendet aus statt ein",
   labels(nv.nav_groups({"irgendwas": True}, "apps")) == ["Apps"])


# --- Sprungmarken -----------------------------------------------------------
print("\nSprungmarken")


def card(title, cls="card"):
    return f'<div class="{cls}">\n  <h2>{title}</h2>\n  <p>x</p>\n</div>\n'


head = '<div class="pagehead"><h1>Gesundheit</h1></div>\n'
long_page = head + "".join(card(t) for t in
                           ("Knoten", "Pflegestand", "Kernservices", "Apps",
                            "Tunnel (Connector)"))
html, marks = nv.jump_sections(long_page)
ok("lange Seite: fuenf Marken", [l for _, l in marks] ==
   ["Knoten", "Pflegestand", "Kernservices", "Apps", "Tunnel (Connector)"], marks)
ok("jede Marke hat ihr Ziel im Dokument",
   all(f'<h2 id="{i}">' in html for i, _ in marks), html)
ok("Ziel-IDs lesbar", marks[4][0] == "abschnitt-tunnel-connector", marks)
ok("Leiste steht hinter dem Seitenkopf, vor der ersten Karte",
   html.index("</h1>") < html.index('<nav class="jumps"') < html.index('<div class="card"'))
ok("sonst unveraendert", html.replace(html[html.index('<nav class="jumps"'):
                                       html.index("</nav>\n") + 7], "")
   .replace(' id="abschnitt-knoten"', "").count("<h2") == 5)

short = head + "".join(card(t) for t in ("A", "B", "C"))
ok("drei Abschnitte: keine Marken", nv.jump_sections(short) == (short, []))

tabs = long_page + '<nav class="tabs"><a href="?tab=x">x</a></nav>'
ok("Seite mit Reitern: keine Marken (eine Leiste je Seite)",
   nv.jump_sections(tabs) == (tabs, []))

warn = card("Bitte bestätigen", "card warn") + long_page
html, marks = nv.jump_sections(warn)
ok("Warnkarte bekommt keine Marke", "Bitte bestätigen" not in [l for _, l in marks], marks)
ok("Warnkarte steht VOR der Leiste",
   html.index("Bitte bestätigen") < html.index('<nav class="jumps"'))

sect = '<h2 class="section">Gruppe</h2>' + long_page
ok("h2.section bekommt keine Marke",
   "Gruppe" not in [l for _, l in nv.jump_sections(sect)[1]])

dup = "".join(card(t) for t in ("Status", "Status", "Größe & Ort", "D"))
html, marks = nv.jump_sections(dup)
ok("doppelte Ueberschrift: zwei Ziele",
   marks[0][0] == "abschnitt-status" and marks[1][0] == "abschnitt-status-2", marks)
ok("Umlaute und Entitaeten im Ziel aufgeloest",
   marks[2][0] == "abschnitt-groesse-ort", marks)

tagged = "".join(card(t) for t in ('Apps <span class="badge">3</span>', "B", "C", "D"))
ok("Auszeichnung im Titel faellt aus der Marke",
   nv.jump_sections(tagged)[1][0][1] == "Apps 3")

print(f"\n{'OK' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
