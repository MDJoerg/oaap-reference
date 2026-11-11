#!/usr/bin/env python3
"""Kleinigkeiten an der Aufbau-Seite (I-45, 0.1.250).

Jörgs Befunde beim Aufbau von svl (09.11.): Farbfeld zeigt die Farbe
nicht, Ankreuzfelder sitzen falsch, keine Schrittnummern, der Text des
Wartepunkts endet mitten im Wort („…danach neu anmelde“), die Hinweise
sind englisch auf einer deutschen Seite.

Aufruf: python3 test/test_build_page_details.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import tenant_build as tb                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:300]}")


print("Der Text eines Wartepunkts wird nicht abgeschnitten")
long_text = ("Lege im Anmeldedienst den ersten Verwalter an und nimm ihn in die "
             "Verwaltergruppe auf. " * 6 + "Er muss sich danach neu anmelden.")
ok("ein Text ueber 240 Zeichen bleibt ganz",
   tb._sentence(long_text, tb.TEXT_LIMIT).endswith("neu anmelden."),
   tb._sentence(long_text, tb.TEXT_LIMIT)[-40:])
ok("eine Maschinen-Notiz bleibt kurz", len(tb._sentence("x " * 400)) <= 240)


src = open(tb.__file__, encoding="utf-8").read()
ok("der wartende Schritt traegt den langen Text (TEXT_LIMIT im _stop)",
   'TEXT_LIMIT if kind == "waiting"' in src)
ok("und der gespeicherte Text des Schritts ebenso",
   src.count("TEXT_LIMIT)") >= 3, src.count("TEXT_LIMIT)"))

print("")
print("Die Hinweise sind deutsch")
app = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
block = app[app.index("class _BuildDrivers:"):app.index("PRESENT_, ABSENT_, CONFLICT_ = (tenant_build.PRESENT")]
english = [s for s in ("the tenant exists", "no tenant with this label yet",
                       "was installed", "waiting for a human", "has nothing to undo",
                       "the verb stopped with", "is as wished")
           if s in block]
ok("keine der bekannten englischen Notizen mehr im Treiber", not english, english)
for s in ('"already in place', '"rollback stopped', '"not asked for"', '"rolled back"'):
    ok(f"die Engine sagt nicht mehr '{s}'", s not in src)

print("")
print("Die Seite")
portal = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
              encoding="utf-8").read()
css = portal[:portal.index("</style>")]
ok("seatopt gilt auf jeder Seite (globales Stylesheet)", ".seatopt{" in css)
ok("Ankreuzfelder sind nicht 100 % breit", "input[type=checkbox],input[type=radio]{width:auto}" in css)
ok("das Farbfeld hat eine sichtbare Groesse", re.search(r"input\[type=color\]\{width:4\.5rem", css))
ok("die Schrittliste nummeriert", "<th>#</th><th>Schritt</th>" in portal
   and "{{ loop.index }}" in portal)

print("")
print("Das Profil handballverein fragt zwei Farben")
import json  # noqa: E402
prof = json.load(open(os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "profile",
                                   "handballverein.json"), encoding="utf-8")) \
    if os.path.isfile(os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "profile",
                                   "handballverein.json")) else None
if prof is None:
    print("SKIP  oaap-docs liegt nicht neben oaap-reference")
else:
    ok("zweiter Farbparameter", prof["params"].get("color2", {}).get("kind") == "color")
    face = next(s for s in prof["steps"] if s["type"] == "tenant.face")
    ok("und das Gesicht bekommt ihn als Akzent", face.get("color_accent") == "{color2}")
    ok("das Profil bleibt gueltig", tb.profile_problems(prof) == [], tb.profile_problems(prof))

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
