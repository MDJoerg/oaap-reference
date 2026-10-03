#!/usr/bin/env python3
"""Der Install einer App, die die Rechte ihres Mandanten VERWALTEN will
(oaap.core.authorization 0.3 §2.10, RFC-0045 A7).

Der Schluessel dieser App ist ein Werkzeug des Mandanten-Admins; der Betreiber
sagt einmal ja, bevor irgendetwas gebaut wird. Geprueft wird (ohne Docker):

    - `administer` gehoert nicht zur Deklaration: eine App, die nur verwaltet,
      registriert nichts; ein Abschnitt ohne den Schluessel bleibt, wie er war;
    - ohne Bestaetigung bricht der Install ab -- und sagt, was der Schluessel
      kann UND was er nie kann; mit Bestaetigung geht er durch; ein
      Neu-Ausrollen einer schon freigegebenen Instanz fragt nicht noch einmal;
    - keine Bestaetigung noetig, wenn die App nichts verwalten will;
    - das Manifest prueft den Wahrheitswert;
    - beim Entfernen schliesst sich die Tuer (und ein Fehler dabei wird laut);
    - die beiden Stellen im Install/Entfernen, die das tun, SIND da und in
      der richtigen Reihenfolge (das kann man nur lesen).

Aufruf: python3 test/test_authz_administer_install.py
"""
import contextlib
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


print("Der Abschnitt")
decl, adm = appctl.authz_split_section({"administer": True})
ok("nur 'administer': keine Deklaration, aber verwalten",
   decl is None and adm is True, (decl, adm))
decl, adm = appctl.authz_split_section({
    "administer": True, "objects": [{"key": "a"}], "role_templates": []})
ok("mit Deklaration: administer wird abgezogen, der Rest bleibt",
   decl == {"objects": [{"key": "a"}], "role_templates": []} and adm is True,
   decl)
decl, adm = appctl.authz_split_section({"administer": False, "objects": []})
ok("administer: false verwaltet nicht", adm is False)
sec = {"objects": []}
decl, adm = appctl.authz_split_section(sec)
ok("ein Abschnitt ohne den Schluessel bleibt wie er war (auch leer)",
   decl == sec and adm is False)
ok("keine Sektion: nichts", appctl.authz_split_section(None) == (None, False))
ok("das Manifest nimmt 'administer' an",
   appctl.validate_authorization_section({"authorization": {
       "administer": True}}) == [])
ok("KOEDER: ein Wort statt eines Wahrheitswerts wird abgelehnt",
   appctl.validate_authorization_section({"authorization": {
       "administer": "ja"}}) != [])

print("Die Schranke")
why = appctl.authz_admin_refusal("rollen-rechte", True, False, False)
ok("ohne Bestaetigung: Abbruch, mit Grund", bool(why))
ok("der Grund sagt, was der Schluessel KANN ...",
   "roles" in why and "assignments" in why and "ONE tenant" in why, why)
ok("... und was er NIE kann",
   "platform role" in why and "another tenant" in why
   and "user" in why, why)
ok("... und wie man zustimmt", "--confirm-administer" in why)
ok("mit Bestaetigung: durch",
   appctl.authz_admin_refusal("rollen-rechte", True, False, True) == "")
ok("eine schon freigegebene Instanz (Neu-Ausrollen) fragt nicht noch einmal",
   appctl.authz_admin_refusal("rollen-rechte", True, True, False) == "")
ok("eine App, die nichts verwalten will, braucht keine Bestaetigung",
   appctl.authz_admin_refusal("partnerverwaltung", False, False, False) == "")

print("Die Tuer schliesst sich beim Entfernen")
calls = []
real = (appctl._authz_instance_record, appctl._authz_set_administer)
try:
    appctl._authz_instance_record = lambda n: {
        "app": "rollen-rechte", "tenant": "t-vp", "administer": True}
    appctl._authz_set_administer = lambda *a: calls.append(a)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        appctl._authz_close_admin_door("rr")
    ok("eine offene Tuer wird geschlossen, fuer genau diese Instanz",
       calls == [("rr", "t-vp", "rollen-rechte", False)], calls)
    ok("und es wird gesagt", "Closed" in buf.getvalue())

    calls.clear()
    appctl._authz_instance_record = lambda n: {"app": "x", "tenant": "t"}
    appctl._authz_close_admin_door("rr")
    ok("eine nie geoeffnete: nichts zu tun", calls == [], calls)

    def boom(*a):
        raise SystemExit("identity ist nicht erreichbar")
    appctl._authz_instance_record = lambda n: {
        "app": "rollen-rechte", "tenant": "t-vp", "administer": True}
    appctl._authz_set_administer = boom
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            appctl._authz_close_admin_door("rr")
        raised = False
    except BaseException:                          # noqa: BLE001
        raised = True
    ok("ein Fehler beim Schliessen laesst das Entfernen nicht scheitern ...",
       not raised)
    ok("... wird aber laut gesagt (die Tuer koennte offen geblieben sein)",
       "WARNING" in buf.getvalue(), buf.getvalue())
finally:
    appctl._authz_instance_record, appctl._authz_set_administer = real

print("Im Install und beim Entfernen (nur lesbar)")
src = open(os.path.join(HERE, "..", "platform", "appctl.py"),
           encoding="utf-8").read()
i_gate = src.index("authz_admin_refusal(\n            app[\"id\"]")
i_reg = src.index("_authz_register_declaration(\n                app[\"id\"]")
ok("die Schranke steht VOR der Registrierung (vor allem, was gebaut wird)",
   i_gate < i_reg)
ok("der Schluessel wird nie fuer eine Probe-Instanz oder ein Artefakt gemacht",
   re.search(r"if authz_admin and not is_artefact and not rehearsal:", src))
i_rm = src.index("def remove_instance(")
ok("remove_instance schliesst die Tuer",
   "_authz_close_admin_door(name)" in src[i_rm:i_rm + 4000])

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
