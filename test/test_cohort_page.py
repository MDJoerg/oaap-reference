#!/usr/bin/env python3
"""Die Kohorten-Seite (oaap.core.portal 0.3.17, RFC-0046 Stufe 3).

Gemessen wird, was die Spezifikation verspricht:

  * die Seite liest nur: dieselbe Datei wie die API, kein Schreibweg;
  * der Mandant ist der des Aufrufers; ein fremder Name ist 404;
  * ein Benutzer ohne Verwalterrolle bekommt 403;
  * das Menue zeigt "Kohorten" nur, wo es eine Kohorte gibt;
  * Termine stehen exakt UND relativ; ein Platz mit Problem faellt auf.

Braucht Flask; der Knoten ist ersetzt (die Sichtdatei ist eine Attrappe).
"""
import json
import os
import sys
import tempfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import cohort_view as cv                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


# --- die reinen Regeln ------------------------------------------------------
T = date(2026, 10, 1)
ok("Datum: in 23 Tagen", cv.when("2026-10-24", T) == "2026-10-24 (in 23 Tagen)")
ok("Datum: heute / morgen / gestern",
   [cv.when(d, T) for d in ("2026-10-01", "2026-10-02", "2026-09-30")]
   == ["2026-10-01 (heute)", "2026-10-02 (morgen)", "2026-09-30 (gestern)"])
ok("Datum: vorbei", cv.when("2026-09-20", T) == "2026-09-20 (vor 11 Tagen)")
ok("kein Datum / kaputtes Datum -> Strich", cv.when("", T) == "–" and cv.when("x", T) == "–"
   and cv.when(None, T) == "–")
ok("Instanzzustand gemischt ist nicht 'ok'",
   cv.instance_tone("running") == "ok" and cv.instance_tone("exited,running") == "off"
   and cv.instance_tone("missing") == "warn")
ok("Instanzzustand: Klartext", cv.instance_label("exited,running") == "angehalten, läuft"
   and cv.instance_label("") == "unbekannt")

C = {"name": "kurs-a", "state": "running", "created": "2026-09-30T10:00:00+00:00",
     "ended": "", "lifetime": {"ends": "2026-10-24", "deactivate_at": "2026-11-23",
                               "delete_at": "2027-01-22"},
     "seats": [
         {"id": "01", "user": "kurs-a-01", "label": "Anna", "waiting": False, "refused": "",
          "instances": [{"app": "ide", "key": "k1", "state": "running",
                         "address": "https://ide-01.example/"}]},
         {"id": "02", "user": "kurs-a-02", "label": "", "waiting": True, "refused": "",
          "instances": []},
         {"id": "03", "user": "kurs-a-03", "label": "", "waiting": False,
          "refused": "kein Platz", "instances": [{"app": "ide", "key": "k3", "state": "missing",
                                                 "address": ""}]}]}
r = cv.row(C, T)
ok("Zeile: Zustand, Plaetze, Problemplaetze, Ende",
   r["label"] == "Läuft" and r["seats"] == 3 and r["problem_seats"] == 2
   and r["ends"] == "2026-10-24 (in 23 Tagen)", r)
ok("beendete Kohorte heisst Beendet",
   cv.row({**C, "ended": "2026-10-25"}, T)["label"] == "Beendet")
d = cv.detail(C, T)
ok("Detail: Termine und Plaetze",
   d["delete"].startswith("2027-01-22") and len(d["seat_list"]) == 3
   and d["seat_list"][1]["note"] == "wartet auf eine freie Instanz"
   and d["seat_list"][2]["note"] == "abgelehnt: kein Platz", d)
ok("leere Eingabe bricht nicht", cv.rows(None) == [] and cv.rows({}) == [])

# --- die Seite ------------------------------------------------------------
import app as portal                                          # noqa: E402

VIEW = os.path.join(tempfile.mkdtemp(prefix="oaap-cohort-page-"), "cohort-view.json")
portal.COHORT_VIEW_FILE = VIEW
json.dump({"schema": "0.1", "cohorts": {"t-a": {"kurs-a": C}, "t-b": {"fremd": {
    "name": "fremd", "state": "running", "seats": [], "lifetime": {}}}}}, open(VIEW, "w"))

WHO = {"role": "tenant_admin", "tenant": "t-a", "roles": {"tenant_admin"}, "host": None}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "trainer", "X-OAAP-Roles": "tenant_admin"}

r = c.get("/kohorten", headers=H)
t = r.get_data(as_text=True)
ok("Liste: 200, eigene Kohorte, nicht die fremde",
   r.status_code == 200 and "kurs-a" in t and "fremd" not in t)
ok("Liste nennt den Weg zum Aendern (API und CLI), aendert selbst nichts",
   "/api/v1/tenant/cohorts" in t and "oaap cohort" in t and t.count("<form") == 1)  # nur Abmelden
ok("Menue zeigt Kohorten, wo es welche gibt", 'href="/kohorten"' in t)
r = c.get("/kohorten/kurs-a", headers=H)
t = r.get_data(as_text=True)
ok("Detail: Plaetze, Adresse, Termin, Problemhinweis",
   r.status_code == 200 and "kurs-a-01" in t and "https://ide-01.example/" in t
   and "wartet auf eine freie Instanz" in t and "abgelehnt: kein Platz" in t
   and "2027-01-22" in t, t[-900:])
ok("Detail: kein Formular ausser Abmelden", t.count("<form") == 1)
r = c.get("/kohorten/fremd", headers=H)
ok("fremder Mandant: 404, nichts davon in der Antwort",
   r.status_code == 404 and "fremd" not in r.get_data(as_text=True).replace("Kohorten", ""))
r = c.get("/kohorten/..%2f..", headers=H)
ok("Pfadspiel: 404", r.status_code == 404)

WHO.update(role="", roles={"user"})
ok("ohne Verwalterrolle: 403", c.get("/kohorten", headers=H).status_code == 403
   and c.get("/kohorten/kurs-a", headers=H).status_code == 403)

WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-c")
r = c.get("/kohorten", headers=H)
ok("Mandant ohne Kohorte: leere Seite, ohne Menueeintrag",
   r.status_code == 200 and "Noch keine Kohorte" in r.get_data(as_text=True)
   and 'href="/kohorten"' not in r.get_data(as_text=True))

WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-a", host="t-b")
ok("Host eines anderen Mandanten: 403 fuer den tenant_admin",
   c.get("/kohorten", headers=H).status_code == 403)

WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host="t-b")
ok("server_admin am Mandantenort sieht dessen Kohorten",
   "fremd" in c.get("/kohorten", headers=H).get_data(as_text=True))

os.remove(VIEW)
WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-a", host=None)
r = c.get("/kohorten", headers=H)
ok("Sichtdatei fehlt: leere Seite statt Fehler",
   r.status_code == 200 and "Noch keine Kohorte" in r.get_data(as_text=True))

print("")
print("ALLE BESTANDEN" if not fails else f"FAILED ({fails} Fehler)")
sys.exit(1 if fails else 0)
