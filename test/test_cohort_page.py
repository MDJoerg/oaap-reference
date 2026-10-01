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
ok("Liste nennt API und CLI fuer Anlegen/Entfernen, hat selbst kein Formular",
   "/api/v1/tenant/cohorts" in t and "oaap cohort" in t and t.count("<form") == 1)  # nur Abmelden
ok("Menue zeigt Kohorten, wo es welche gibt", 'href="/kohorten"' in t)
r = c.get("/kohorten/kurs-a", headers=H)
t = r.get_data(as_text=True)
ok("Detail: Plaetze, Adresse, Termin, Problemhinweis",
   r.status_code == 200 and "kurs-a-01" in t and "https://ide-01.example/" in t
   and "wartet auf eine freie Instanz" in t and "abgelehnt: kein Platz" in t
   and "2027-01-22" in t, t[-900:])
ok("Detail: Anhalten und Verlängern (Starten erst, wenn angehalten)",
   'action="/kohorten/kurs-a/stop"' in t and 'action="/kohorten/kurs-a/extend"' in t
   and "/kohorten/kurs-a/start" not in t)
r = c.get("/kohorten/fremd", headers=H)
ok("fremder Mandant: 404, nichts davon in der Antwort",
   r.status_code == 404 and "fremd" not in r.get_data(as_text=True).replace("Kohorten", ""))
r = c.get("/kohorten/..%2f..", headers=H)
ok("Pfadspiel: abgewiesen (404 oder 405)", r.status_code in (404, 405))

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

# --- Schaltflaechen: dieselbe Uebergabe wie die API ------------------------
import management_api as mapi                                 # noqa: E402

SP = tempfile.mkdtemp(prefix="oaap-cohort-spool-")
for sub in ("queue", "claims", "jobs"):
    os.makedirs(os.path.join(SP, sub))
mapi.SPOOL_DIR = SP
QUEUED = []
def _queue(rid, name, payload, wait):
    QUEUED.append((rid, payload))
    open(os.path.join(SP, "queue", rid + ".json"), "w").write("{}")


mapi.CTX["queue"] = _queue
mapi.CTX["caller_name"] = lambda: "trainer"
WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-a", host=None)

r = c.post("/kohorten/kurs-a/stop", headers=H)
loc = r.headers.get("Location", "")
ok("Anhalten: Auftrag in den Spool, Weiterleitung mit Auftrag",
   r.status_code == 302 and "?job=" in loc and len(QUEUED) == 1
   and QUEUED[0][1] == {"action": "cohort", "op": "stop", "args": {"cohort": "kurs-a"}}, (loc, QUEUED))
rid = QUEUED[0][0]
meta = json.load(open(os.path.join(SP, "jobs", rid, "meta.json")))
ok("der Auftrag traegt Mandant, Rolle und Handelnden",
   meta["tenant"] == "t-a" and meta["role"] == "tenant_admin" and meta["by"] == "trainer", meta)
t = c.get(loc, headers=H).get_data(as_text=True)
ok("Banner: Auftrag wartet, Seite laedt neu", "wartet auf dem Knoten" in t and "location.reload" in t)
os.rename(os.path.join(SP, "queue", rid + ".json"), os.path.join(SP, "claims", rid + ".json"))
ok("Banner: laeuft", "läuft auf dem Knoten" in c.get(loc, headers=H).get_data(as_text=True))
json.dump({"id": rid, "ok": True, "message": "Cohort 'kurs-a' stopped."},
          open(os.path.join(SP, "jobs", rid, "result.json"), "w"))
t = c.get(loc, headers=H).get_data(as_text=True)
ok("Banner: Ergebnis des Knotens, kein Neuladen mehr",
   "Cohort &#39;kurs-a&#39; stopped." in t and "location.reload" not in t, t[-600:])
json.dump({"id": rid, "ok": False, "message": "refused: nope"},
          open(os.path.join(SP, "jobs", rid, "result.json"), "w"))
ok("Banner: Mehrzeiliges bleibt mehrzeilig (pre-wrap)", "white-space:pre-wrap" in c.get(loc, headers=H).get_data(as_text=True))
ok("Banner: Ablehnung des Knotens wird als Fehler gezeigt",
   '>refused: nope<' in c.get(loc, headers=H).get_data(as_text=True))
WHO.update(tenant="t-c")
os.makedirs(os.path.join(SP, "jobs", "f" * 32))
json.dump({"id": "f" * 32, "tenant": "t-a"}, open(os.path.join(SP, "jobs", "f" * 32, "meta.json"), "w"))
json.dump({"ok": True, "message": "GEHEIM"}, open(os.path.join(SP, "jobs", "f" * 32, "result.json"), "w"))
WHO.update(tenant="t-a")
WHO["host"] = None

n0 = len(QUEUED)
r = c.post("/kohorten/kurs-a/extend", headers=H, data={"ends": "2026-11-07"})
ok("Verlaengern: Auftrag mit Datum",
   r.status_code == 302 and QUEUED[-1][1]["args"] == {"cohort": "kurs-a", "ends": "2026-11-07", "dry_run": False}
   and len(QUEUED) == n0 + 1, QUEUED[-1])
n0 = len(QUEUED)
r = c.post("/kohorten/kurs-a/extend", headers=H, data={"ends": "morgen; rm -rf"})
ok("Verlaengern ohne gueltiges Datum: nichts im Spool, Hinweis",
   len(QUEUED) == n0 and "err=" in r.headers["Location"])
r = c.post("/kohorten/fremd/stop", headers=H)
ok("fremde Kohorte anhalten: nichts im Spool", len(QUEUED) == n0 and r.status_code == 302
   and r.headers["Location"] == "/kohorten")
r = c.post("/kohorten/kurs-a/remove", headers=H)
ok("entfernen gibt es hier nicht", len(QUEUED) == n0)
r = c.post("/kohorten/kurs-a/stop", headers={**H, "Origin": "https://boese.example"})
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and len(QUEUED) == n0)
WHO.update(role="", roles={"user"})
r = c.post("/kohorten/kurs-a/stop", headers=H)
ok("ohne Verwalterrolle: 403, nichts im Spool", r.status_code == 403 and len(QUEUED) == n0)
WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-b")
r = c.get(f"/kohorten/fremd?job={'f' * 32}", headers=H)
ok("Auftrag eines anderen Mandanten: sein Ergebnis steht nicht im Banner",
   "GEHEIM" not in r.get_data(as_text=True))
WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-a", host=None)
# eine angehaltene Kohorte bietet Starten an
json.dump({"cohorts": {"t-a": {"kurs-a": {**C, "state": "stopped"}}}}, open(VIEW, "w"))
t = c.get("/kohorten/kurs-a", headers=H).get_data(as_text=True)
ok("angehalten: Starten statt Anhalten",
   'action="/kohorten/kurs-a/start"' in t and "/kohorten/kurs-a/stop" not in t)

# --- Anlegen, Handout, Zuruecksetzen ---------------------------------------
import io                                                     # noqa: E402
import zipfile                                                # noqa: E402

json.dump({"cohorts": {"t-a": {"kurs-a": C}}}, open(VIEW, "w"))


def zipbytes(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


GOOD = zipbytes({"cohort.yaml": "oaap_cohort: '0.1'\nname: neu\n"})
r = c.get("/kohorten-anlegen", headers=H)
t = r.get_data(as_text=True)
ok("Anlegen-Seite: Formular mit Datei, ohne Git-Adresse",
   r.status_code == 200 and 'type="file"' in t and 'enctype="multipart/form-data"' in t
   and 'name="git"' not in t)
n0 = len(QUEUED)
r = c.post("/kohorten-anlegen", headers=H, data={"archive": (io.BytesIO(GOOD), "v.zip")},
           content_type="multipart/form-data")
loc = r.headers.get("Location", "")
rid = loc.split("job=")[-1]
ok("Anlegen: Vorlage entpackt, Auftrag create mit Mandant und Rolle",
   r.status_code == 302 and len(QUEUED) == n0 + 1 and QUEUED[-1][1]["op"] == "create"
   and os.path.isfile(os.path.join(SP, "jobs", rid, "template", "cohort.yaml"))
   and json.load(open(os.path.join(SP, "jobs", rid, "meta.json")))["tenant"] == "t-a", (loc, QUEUED[-1]))
n0 = len(QUEUED)
for label, data in (("kein ZIP", b"nur text"),
                    ("Pfad nach oben", zipbytes({"cohort.yaml": "x", "../boese": "x"})),
                    ("ohne cohort.yaml", zipbytes({"a.txt": "x"}))):
    r = c.post("/kohorten-anlegen", headers=H, data={"archive": (io.BytesIO(data), "v.zip")},
               content_type="multipart/form-data")
    ok(f"Anlegen mit schlechtem Archiv ({label}): Hinweis, nichts im Spool",
       r.status_code == 302 and "err=" in r.headers["Location"] and len(QUEUED) == n0,
       r.headers.get("Location"))
r = c.post("/kohorten-anlegen", headers=H, data={}, content_type="multipart/form-data")
ok("Anlegen ohne Datei: Hinweis", r.status_code == 302 and "err=" in r.headers["Location"])
r = c.post("/kohorten-anlegen", headers={**H, "Sec-Fetch-Site": "cross-site"},
           data={"archive": (io.BytesIO(GOOD), "v.zip")}, content_type="multipart/form-data")
ok("Anlegen von einer fremden Seite: 403, nichts im Spool", r.status_code == 403 and len(QUEUED) == n0)
WHO.update(role="", roles={"user"})
r = c.post("/kohorten-anlegen", headers=H, data={"archive": (io.BytesIO(GOOD), "v.zip")},
           content_type="multipart/form-data")
ok("Anlegen ohne Verwalterrolle: 403", r.status_code == 403 and len(QUEUED) == n0)
WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-a")

# der Auftrag ist fertig -> Banner mit Link und Handout
jdir = os.path.join(SP, "jobs", rid)
os.rename(os.path.join(SP, "queue", rid + ".json"), os.path.join(SP, "claims", rid + ".json"))
CSV = b"user,password\nneu-tn-01,geheim-123\n"
open(os.path.join(jdir, "handout.csv"), "wb").write(CSV)
json.dump({"id": rid, "ok": True, "cohort": "neu", "message": "cohort 'neu' created"},
          open(os.path.join(jdir, "result.json"), "w"))
t = c.get(loc, headers=H).get_data(as_text=True)
ok("fertiger Auftrag: Link zur Kohorte und Handout-Formular",
   'href="/kohorten/neu"' in t and f'action="/kohorten-handout/{rid}"' in t and 'type="password"' in t)
mapi.CTX["caller_name"] = lambda: "kollege"
portal.caller_name = lambda: "kollege"
t = c.get(loc, headers=H).get_data(as_text=True)
ok("ein Kollege sieht das Handout-Formular nicht",
   "/kohorten-handout/" not in t and 'href="/kohorten/neu"' in t)
r = c.post(f"/kohorten-handout/{rid}", headers=H, data={})
ok("ein Kollege bekommt das Handout nicht: 403, Datei bleibt",
   r.status_code == 403 and os.path.isfile(os.path.join(jdir, "handout.csv")))
portal.caller_name = lambda: "trainer"
mapi.CTX["caller_name"] = lambda: "trainer"
t = c.get(loc, headers=H).get_data(as_text=True)
ok("der Starter sieht das Handout-Formular", f'action="/kohorten-handout/{rid}"' in t)
r = c.post(f"/kohorten-handout/{rid}", headers=H, data={"password": "kurz"})
ok("zu kurzes Passwort: Hinweis, Handout NICHT verbraucht",
   r.status_code == 302 and "err=" in r.headers["Location"]
   and os.path.isfile(os.path.join(jdir, "handout.csv")))
r = c.post(f"/kohorten-handout/{rid}", headers={**H, "Origin": "https://boese.example"},
           data={"password": "langes-passwort"})
ok("Handout von einer fremden Seite: 403, Datei bleibt",
   r.status_code == 403 and os.path.isfile(os.path.join(jdir, "handout.csv")))
nq = len(QUEUED)
r = c.post(f"/kohorten-handout/{rid}", headers=H, data={"password": "langes-passwort"})
ok("Handout mit Passwort: ZIP, Datei vernichtet, Vermerk im Spool",
   r.status_code == 200 and r.mimetype == "application/zip" and "X-OAAP-Handout" not in r.headers
   and not os.path.exists(os.path.join(jdir, "handout.csv"))
   and QUEUED[-1][1]["op"] == "handout-note" and len(QUEUED) == nq + 1)
ok("das ZIP ist wirklich verschluesselt",
   zipfile.ZipFile(io.BytesIO(r.data)).infolist()[0].flag_bits & 1 == 1)
r = c.post(f"/kohorten-handout/{rid}", headers=H, data={"password": "langes-passwort"})
ok("zweiter Abruf: nichts mehr, mit Hinweis", r.status_code == 302 and "err=" in r.headers["Location"])
ok("danach zeigt der Banner kein Handout mehr",
   "/kohorten-handout/" not in c.get(loc, headers=H).get_data(as_text=True))
rid2 = "c" * 32
os.makedirs(os.path.join(SP, "jobs", rid2))
json.dump({"id": rid2, "op": "create", "by": "trainer", "tenant": "t-a", "cohort": "x"},
          open(os.path.join(SP, "jobs", rid2, "meta.json"), "w"))
json.dump({"id": rid2, "ok": True, "cohort": "x", "message": "ok"},
          open(os.path.join(SP, "jobs", rid2, "result.json"), "w"))
open(os.path.join(SP, "jobs", rid2, "handout.csv"), "wb").write(CSV)
r = c.post(f"/kohorten-handout/{rid2}", headers=H, data={})
ok("ohne Passwort: offenes ZIP, als unencrypted gekennzeichnet",
   r.status_code == 200 and r.headers.get("X-OAAP-Handout") == "unencrypted"
   and zipfile.ZipFile(io.BytesIO(r.data)).read("handout.csv") == CSV)

# Zuruecksetzen
t = c.get("/kohorten/kurs-a", headers=H).get_data(as_text=True)
ok("Platz: Zuruecksetzen mit Bestaetigung, Dateien behalten vorgewaehlt",
   'action="/kohorten/kurs-a/seats/02/reset"' in t and 'name="keep_home" value="1" checked' in t
   and 'name="sure" value="1" required' in t)
n0 = len(QUEUED)
r = c.post("/kohorten/kurs-a/seats/02/reset", headers=H, data={"keep_home": "1"})
ok("Zuruecksetzen ohne Bestaetigung: nichts im Spool, Hinweis",
   len(QUEUED) == n0 and "err=" in r.headers["Location"])
r = c.post("/kohorten/kurs-a/seats/02/reset", headers=H, data={"sure": "1", "keep_home": "1"})
ok("Zuruecksetzen, Dateien behalten", r.status_code == 302 and QUEUED[-1][1] == {
    "action": "cohort", "op": "reset",
    "args": {"cohort": "kurs-a", "seat": "02", "keep_home": True}}, QUEUED[-1])
r = c.post("/kohorten/kurs-a/seats/02/reset", headers=H, data={"sure": "1"})
ok("Zuruecksetzen ohne 'Dateien behalten': keep_home falsch",
   QUEUED[-1][1]["args"]["keep_home"] is False)
n0 = len(QUEUED)
for sid in ("99", "..", "01;rm"):
    c.post(f"/kohorten/kurs-a/seats/{sid}/reset", headers=H, data={"sure": "1"})
c.post("/kohorten/fremd/seats/01/reset", headers=H, data={"sure": "1"})
ok("unbekannter Platz oder fremde Kohorte: nichts im Spool", len(QUEUED) == n0)
r = c.post("/kohorten/kurs-a/seats/02/reset", headers={**H, "Sec-Fetch-Site": "cross-site"},
           data={"sure": "1"})
ok("Zuruecksetzen von einer fremden Seite: 403", r.status_code == 403 and len(QUEUED) == n0)
WHO.update(role="", roles={"user"})
ok("Zuruecksetzen ohne Verwalterrolle: 403",
   c.post("/kohorten/kurs-a/seats/02/reset", headers=H, data={"sure": "1"}).status_code == 403)
WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-a")

os.remove(VIEW)
r = c.get("/kohorten", headers=H)
ok("Sichtdatei fehlt: leere Seite statt Fehler",
   r.status_code == 200 and "Noch keine Kohorte" in r.get_data(as_text=True))

print("")
print("ALLE BESTANDEN" if not fails else f"FAILED ({fails} Fehler)")
sys.exit(1 if fails else 0)
