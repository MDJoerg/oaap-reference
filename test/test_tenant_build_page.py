#!/usr/bin/env python3
"""Die Aufbau-Seiten (oaap.core.portal 2.9, RFC-0055 Stufe 3): der Assistent.

Gemessen wird, was die Spezifikation verspricht:

  * die Seiten gibt es nur fuer den server_admin am Knoten selbst; ein
    Verwalter bekommt 403, am Ort eines Mandanten gibt es sie nicht (404),
    und der Menuepunkt erscheint genau dort;
  * das Formular entsteht aus den Parametern des Profils, das Kuerzel traegt
    den Hinweis, dass es oeffentlich ist, ein Menschenschritt ist gekennzeichnet;
  * die Seiten schreiben nichts selbst: jede Schaltflaeche stellt die Anfrage
    der API (Aktion `tenant-build`); ein fehlendes Pflichtfeld, eine fremde
    Herkunft, ein Zurueckbauen ohne getipptes Kuerzel kommen nie in den Spool;
  * was auf einen Menschen wartet, steht ueber allem; das Folgenschwere ganz
    unten; die Daten der Instanzen nur bei gesetztem Haken.

Braucht Flask; der Knoten ist ersetzt (Sicht-Datei und Spool sind Attrappen).
Aufruf: python3 test/test_tenant_build_page.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import build_view as bv                                       # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


# ---------------------------------------------------------- the pure rules
PROFILE = {"id": "verein", "title": "Verein", "problem": "",
           "params": {"label": {"kind": "label", "required": True,
                                "label": "Kürzel"},
                      "title": {"kind": "text", "required": True, "max": 60},
                      "color": {"kind": "color", "default": None}},
           "steps": [{"id": "tenant", "type": "tenant.create"},
                     {"id": "admin", "type": "manual"},
                     {"id": "web", "type": "app.install"}]}
BAD = {"id": "kaputt", "title": "", "problem": "unknown step type 'x'"}
B_WAIT = {"id": "b-20261003t100000-vwait", "profile": "verein", "label": "vwait",
          "state": "waiting", "created": "2026-10-03T10:00:00+00:00",
          "by": "betreiber",
          "steps": [{"id": "tenant", "type": "tenant.create", "state": "done",
                     "note": "the tenant was created", "made": ["tenant:t1"]},
                    {"id": "admin", "type": "manual", "state": "waiting",
                     "note": "Create the admin.", "text": "Create the admin.",
                     "done_when": "confirmed", "made": []},
                    {"id": "web", "type": "app.install", "state": "pending",
                     "note": "", "made": []}]}
B_FAIL = {"id": "b-20261003t100100-vfail", "profile": "verein", "label": "vfail",
          "state": "failed", "created": "2026-10-03T10:01:00+00:00", "by": "betreiber",
          "steps": [{"id": "tenant", "type": "tenant.create", "state": "done",
                     "note": "", "made": ["tenant:t2"]},
                    {"id": "web", "type": "app.install", "state": "failed",
                     "note": "no such package", "made": ["instance:vfail-web"]}]}
B_HAND = {**B_WAIT, "id": "b-20261003t100200-vhand", "label": "vhand",
          "steps": [{**B_WAIT["steps"][0]},
                    {**B_WAIT["steps"][1], "done_when": "role.tenant_admin"},
                    {**B_WAIT["steps"][2]}]}
B_DONE = {**B_WAIT, "id": "b-20261003t100300-vdone", "label": "vdone",
          "state": "rolled-back"}
VIEW = {"profiles": [PROFILE, BAD],
        "builds": [B_WAIT, B_FAIL, B_HAND, B_DONE]}

print("=== die reinen Regeln ===")
r = bv.rows(VIEW)
ok("Liste: Zustand in Klartext, Zeit ohne Sekunden",
   r[0]["state"] == "Wartet auf einen Menschen" and r[0]["created"] == "2026-10-03 10:00"
   and r[3]["state"] == "Zurückgebaut", r)
ok("ein vom Host abgelehntes Profil traegt den Grund und ist nicht startbar",
   bv.profiles(VIEW)[1]["problem"] and bv.find_profile(VIEW, "kaputt") is None
   and bv.find_profile(VIEW, "verein")["id"] == "verein")
ok("eine Kennung, die keine ist, findet nichts (auch kein '..')",
   bv.find_build(VIEW, "../../x") is None and bv.find_build(VIEW, "nope") is None
   and bv.find_build(VIEW, B_WAIT["id"]) is B_WAIT)
ok("ein unfertiger Aufbau eines Kuerzels wird gefunden, ein zurueckgebauter nicht",
   bv.open_for(VIEW, "vwait") is B_WAIT and bv.open_for(VIEW, "vdone") is None)
f = bv.fields(PROFILE)
ok("Formularfelder in der Reihenfolge der Datei, mit eigener Beschriftung",
   [x["name"] for x in f] == ["label", "title", "color"] and f[0]["label"] == "Kürzel"
   and f[1]["label"] == "title" and f[1]["max"] == 60, f)
p = bv.plan(PROFILE)
ok("die Schritte in Klartext, der Menschenschritt gekennzeichnet",
   p[0]["text"] == "Mandanten anlegen" and p[1]["human"] and not p[0]["human"], p)
par, miss = bv.start_params(PROFILE, {"label": " vneu ", "title": "Verein Neu",
                                      "color": "#112233"})
ok("Werte werden getrimmt uebernommen", par == {"label": "vneu", "title": "Verein Neu",
                                                  "color": "#112233"} and not miss, par)
par, miss = bv.start_params(PROFILE, {"label": "vneu", "title": "T",
                                      "color": "#112233", "color__none": "1"})
ok("'keine Farbe' laesst die Farbe aus der Anfrage weg", "color" not in par, par)
par, miss = bv.start_params(PROFILE, {"label": "", "title": "  "})
ok("fehlende Pflichtfelder werden genannt, ein leeres optionales fehlt nicht",
   miss == ["label", "title"] and "color" not in par, (par, miss))
d = bv.detail(B_WAIT)
ok("Objektseite: Fortschritt, wartender Schritt mit Text, bestaetigbar",
   d["progress"] == "1 von 3" and d["waiting"]["text"] == "Create the admin."
   and d["waiting"]["confirmable"] and d["failed"] is None, d)
ok("ein Wartepunkt, der auf eine LESUNG wartet, ist nicht zu bestaetigen",
   bv.detail(B_HAND)["waiting"]["confirmable"] is False)
d = bv.detail(B_FAIL)
ok("ein fehlgeschlagener Schritt wird genannt; Instanzen sind erkannt",
   d["failed"]["id"] == "web" and d["made_instances"] and d["unfinished"], d)
ok("ein zurueckgebauter Aufbau ist nicht noch einmal zurueckzubauen",
   bv.detail(B_DONE)["rollbackable"] is False and bv.detail(B_WAIT)["rollbackable"])
ok("leere Eingaben brechen nicht", bv.rows(None) == [] and bv.profiles({}) == []
   and bv.find_profile(None, "x") is None)

# ----------------------------------------------------------- the pages
import app as portal                                          # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-build-page-")
VF = os.path.join(TMP, "build-view.json")
portal.BUILD_VIEW_FILE = VF
json.dump({"schema": "0.1", **VIEW}, open(VF, "w"))
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []


def fake_queue(rid, name, payload, wait):
    QUEUED.append(dict(payload, _rid=rid))


mg.CTX["queue"] = fake_queue

WHO = {"role": "server_admin", "roles": {"server_admin"}, "tenant": "",
       "host": None}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}

print("=== wer, und wo ===")
r = c.get("/aufbau", headers=H)
t = r.get_data(as_text=True)
ok("der Betreiber am Knoten sieht Profile und Aufbauten",
   r.status_code == 200 and "verein" in t and "vwait" in t and "vfail" in t, t[-600:])
ok("der Menuepunkt Aufbau steht da", 'href="/aufbau"' in t)
ok("ein abgelehntes Profil zeigt den Grund und keinen Knopf",
   "abgelehnt: unknown step type" in t and t.count("Aufbau starten") == 1, t.count("Aufbau starten"))
ok("die Liste hat Formulare nur fuer Profile (hochladen, loeschen je Profil) und die Abmeldung",
   t.count("<form") == 1 + 1 + 2 and t.count('action="/aufbau/profile/') == 3
   and 'action="/aufbau/neu"' not in t, t.count("<form"))
for who, role, roles in (("Verwalter", "tenant_admin", {"tenant_admin"}),
                         ("Mitglied", "", {"user"})):
    WHO.update(role=role, roles=roles, tenant="t-a")
    codes = [c.get(u, headers=H).status_code for u in
             ("/aufbau", "/aufbau/neu?profil=verein", "/aufbau/" + B_WAIT["id"])]
    codes.append(c.post("/aufbau/neu", headers=H, data={"profil": "verein"}).status_code)
    codes.append(c.post("/aufbau/" + B_WAIT["id"] + "/continue", headers=H).status_code)
    mt = c.get("/kohorten", headers=H).get_data(as_text=True) if role else ""
    ok(f"{who}: 403 an jeder Tuer, nichts im Spool, kein Menuepunkt",
       codes == [403] * 5 and not QUEUED and 'href="/aufbau"' not in mt, codes)
WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host="t-kunde")
codes = [c.get("/aufbau", headers=H).status_code,
         c.post("/aufbau/neu", headers=H, data={"profil": "verein"}).status_code,
         c.post("/aufbau/" + B_WAIT["id"] + "/continue", headers=H).status_code]
ok("am Ort eines Mandanten gibt es die Seiten nicht (404), auch nicht fuer den Betreiber",
   codes == [404] * 3 and not QUEUED, codes)
t = c.get("/kohorten", headers=H).get_data(as_text=True)
ok("...und dort steht auch kein Menuepunkt", 'href="/aufbau"' not in t)
WHO.update(host=None)

print("=== das Formular ===")
r = c.get("/aufbau/neu?profil=verein", headers=H)
t = r.get_data(as_text=True)
ok("Formular aus den Parametern: Kuerzel (Pflicht, begrenzt), Titel, Farbe",
   r.status_code == 200 and 'name="label"' in t and 'name="title"' in t
   and 'type="color"' in t and 'name="color__none"' in t and 'maxlength="31"' in t
   and 'maxlength="60"' in t and "required" in t, t[-1500:])
ok("das Kuerzel traegt den Hinweis, dass es oeffentlich ist",
   "öffentlich" in t and "Zertifikatsprotokollen" in t)
ok("die Schritte in Klartext, der Menschenschritt gekennzeichnet",
   "Mandanten anlegen" in t and "Anwendung installieren" in t
   and t.count("ein Mensch") >= 1)
ok("ein unbekanntes oder abgelehntes Profil: 404, kein Formular",
   c.get("/aufbau/neu?profil=kaputt", headers=H).status_code == 404
   and c.get("/aufbau/neu?profil=../x", headers=H).status_code == 404
   and c.get("/aufbau/neu", headers=H).status_code == 404)

print("=== Starten: nur die Anfrage der API ===")
r = c.post("/aufbau/neu", headers=H, data={"profil": "verein", "label": "",
                                             "title": ""})
ok("fehlende Pflichtfelder: 400, nichts im Spool, die Eingabe bleibt",
   r.status_code == 400 and not QUEUED and "Bitte ausfüllen" in r.get_data(as_text=True))
r = c.post("/aufbau/neu", headers=H, data={"profil": "verein", "label": "vkeep",
                                             "title": ""})
ok("...mit den schon eingegebenen Werten", 'value="vkeep"' in r.get_data(as_text=True))
r = c.post("/aufbau/neu", headers=dict(H, Origin="http://boese.example"),
           data={"profil": "verein", "label": "vx", "title": "x"})
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and not QUEUED)
r = c.post("/aufbau/neu", headers=dict(H, **{"Sec-Fetch-Site": "cross-site"}),
           data={"profil": "verein", "label": "vx", "title": "x"})
ok("Sec-Fetch-Site cross-site: 403", r.status_code == 403 and not QUEUED)
r = c.post("/aufbau/neu", headers=H, data={"profil": "kaputt", "label": "vx",
                                             "title": "x"})
ok("ein vom Host abgelehntes Profil laesst sich nicht starten",
   r.status_code == 404 and not QUEUED)
r = c.post("/aufbau/neu", headers=H, data={"profil": "verein", "label": "vneu",
                                             "title": "Verein Neu",
                                             "color": "#112233",
                                             "boese": "1", "role": "server_admin"})
ok("ein gueltiger Start: Weiterleitung mit Auftrag", r.status_code == 302
   and "/aufbau?job=" in r.headers["Location"] and len(QUEUED) == 1, r.headers)
q = QUEUED[0]
ok("die Anfrage ist die der API: Aktion, Operation, Profil, nur die "
   "Parameter des Profils (nichts Fremdes, keine Rolle)",
   q["action"] == "tenant-build" and q["op"] == "start"
   and q["args"] == {"profile": "verein", "params": {
       "label": "vneu", "title": "Verein Neu", "color": "#112233"}}
   and "role" not in q and "role" not in q["args"], q)
meta = json.load(open(os.path.join(TMP, "jobs", q["_rid"], "meta.json")))
ok("der Auftragssatz traegt den Betreiber", meta["role"] == "server_admin")

print("=== der Auftrag auf der Seite ===")
rid = q["_rid"]
json.dump({}, open(os.path.join(TMP, "queue", rid + ".json"), "w"))
t = c.get("/aufbau?job=" + rid, headers=H).get_data(as_text=True)
ok("wartend: der Hinweis und das Neuladen", "wartet auf dem Knoten" in t
   and "location.reload" in t)
os.makedirs(os.path.join(TMP, "claims"), exist_ok=True)
json.dump({}, open(os.path.join(TMP, "claims", rid + ".json"), "w"))
os.remove(os.path.join(TMP, "queue", rid + ".json")) if os.path.exists(
    os.path.join(TMP, "queue", rid + ".json")) else None
t = c.get("/aufbau?job=" + rid, headers=H).get_data(as_text=True)
ok("laeuft: der Hinweis", "läuft auf dem Knoten" in t)
json.dump({"id": rid, "ok": True, "build": B_WAIT["id"],
           "message": "build X is waiting"},
          open(os.path.join(TMP, "jobs", rid, "result.json"), "w"))
t = c.get("/aufbau?job=" + rid, headers=H).get_data(as_text=True)
ok("fertig: die Worte des Hosts und der Weg zum Aufbau, kein Neuladen",
   "build X is waiting" in t and f'/aufbau/{B_WAIT["id"]}' in t
   and "location.reload" not in t)
json.dump({"id": rid, "ok": False, "message": "the label 'vneu' is taken"},
          open(os.path.join(TMP, "jobs", rid, "result.json"), "w"))
t = c.get("/aufbau?job=" + rid, headers=H).get_data(as_text=True)
ok("abgelehnt: die Ablehnung in den Worten des Hosts, als Fehler",
   "the label &#39;vneu&#39; is taken" in t or "the label 'vneu' is taken" in t)
ok("eine Auftragskennung, die keine ist, zeigt nichts",
   "wartet auf dem Knoten" not in c.get("/aufbau?job=../../x", headers=H).get_data(as_text=True))

print("=== die Objektseite ===")
QUEUED.clear()
r = c.get("/aufbau/" + B_WAIT["id"], headers=H)
t = r.get_data(as_text=True)
ok("Objektseite: Kopf, Schritte, Fortschritt", r.status_code == 200 and "vwait" in t
   and "1 von 3" in t and "Mandanten anlegen" in t, t[:200])
ok("der Wartepunkt steht UEBER dem Kopf, mit seinem Text und beiden Knoepfen",
   t.index("Hier ist ein Mensch gefragt") < t.index('class="objhead"')
   and "Create the admin." in t and "Weiter prüfen" in t and "Bestätigt" in t)
ok("das Zurueckbauen steht ganz unten, nach den Schritten",
   t.index("Schritte") < t.index("Zurückbauen"))
ok("ohne angelegte Instanz gibt es den Haken fuer die Daten NICHT",
   "purge_instances" not in t)
t = c.get("/aufbau/" + B_HAND["id"], headers=H).get_data(as_text=True)
ok("wartet er auf eine Lesung, gibt es nur 'Weiter pruefen', kein 'Bestaetigt'",
   "Weiter prüfen" in t and "Bestätigt" not in t)
t = c.get("/aufbau/" + B_FAIL["id"], headers=H).get_data(as_text=True)
ok("fehlgeschlagen: der Schritt mit seinem Grund und 'Fortsetzen'",
   "Ein Schritt ist fehlgeschlagen" in t and "no such package" in t
   and "Fortsetzen" in t)
ok("mit angelegter Instanz gibt es den Haken, nicht vorgehakt, und er sagt, was er loescht",
   'name="purge_instances"' in t and "checked" not in t.split('name="purge_instances"')[1][:40]
   and "nicht umkehrbar" in t)
t = c.get("/aufbau/" + B_DONE["id"], headers=H).get_data(as_text=True)
ok("ein zurueckgebauter Aufbau bietet kein Zurueckbauen mehr", "Zurückbauen" not in t
   and "Hier ist ein Mensch" not in t.split("Schritte")[0] or "Zurückbauen" not in t)
ok("eine unbekannte Kennung: 404", c.get("/aufbau/b-20250101t000000-x", headers=H).status_code == 404
   and c.get("/aufbau/nope", headers=H).status_code == 404)

B_STUCK = {**B_FAIL, "id": "b-20261003t100400-vstuck", "label": "vstuck",
           "rolling_back": True,
           "steps": [{"id": "tenant", "type": "tenant.create", "state": "failed",
                      "note": "rollback stopped: - 1 user account(s)",
                      "made": ["tenant:t9"]}]}
json.dump({"schema": "0.1", **VIEW, "builds": VIEW["builds"] + [B_STUCK]}, open(VF, "w"))
t = c.get("/aufbau/" + B_STUCK["id"], headers=H).get_data(as_text=True)
ok("ein stehengebliebener RUECKBAU wird so genannt, nicht als Fehlschlag eines Schritts",
   "Der Rückbau ist stehengeblieben" in t and "Ein Schritt ist fehlgeschlagen" not in t
   and "enthält noch etwas" in t and "Rückbau fortsetzen" in t, t[:600])
ok("...mit dem Grund des Knotens und dem Weg (Konten entfernen, dann fortsetzen)",
   "1 user account(s)" in t and "letzte" in t and "zuerst die Rolle" in t)
r = c.post(f"/aufbau/{B_STUCK['id']}/continue", headers=H)
ok("'Rückbau fortsetzen' stellt die Anfrage 'continue' ohne Datenloeschung",
   QUEUED[-1]["op"] == "continue" and QUEUED[-1]["args"]["purge_instances"] is False)
json.dump({"schema": "0.1", **VIEW}, open(VF, "w"))

print("=== Fortsetzen, Bestaetigen, Zurueckbauen ===")
bid = B_WAIT["id"]
r = c.post(f"/aufbau/{bid}/continue", headers=H)
ok("Fortsetzen: Auftrag ohne Datenloeschung",
   r.status_code == 302 and QUEUED[-1]["op"] == "continue"
   and QUEUED[-1]["args"] == {"build": bid, "purge_instances": False}, QUEUED[-1:])
r = c.post(f"/aufbau/{bid}/confirm", headers=H, data={"step": "admin"})
ok("Bestaetigen des wartenden Schritts: Auftrag mit dem Schritt",
   QUEUED[-1]["op"] == "confirm" and QUEUED[-1]["args"] == {"build": bid, "step": "admin"})
n = len(QUEUED)
r = c.post(f"/aufbau/{bid}/confirm", headers=H, data={"step": "tenant"})
ok("Bestaetigen eines Schritts, der nicht wartet: abgewiesen, nichts im Spool",
   r.status_code == 302 and "err=" in r.headers["Location"] and len(QUEUED) == n)
r = c.post(f"/aufbau/{B_HAND['id']}/confirm", headers=H, data={"step": "admin"})
ok("Bestaetigen, wo eine Lesung entscheidet: abgewiesen", len(QUEUED) == n
   and "err=" in r.headers["Location"])
r = c.post(f"/aufbau/{bid}/rollback", headers=H, data={"confirm": "falsch"})
ok("Zurueckbauen mit falschem Kuerzel: abgewiesen, nichts im Spool",
   len(QUEUED) == n and "err=" in r.headers["Location"])
r = c.post(f"/aufbau/{bid}/rollback", headers=H, data={})
ok("Zurueckbauen ganz ohne Eingabe: abgewiesen", len(QUEUED) == n)
r = c.post(f"/aufbau/{bid}/rollback", headers=H, data={"confirm": "vwait"})
ok("Zurueckbauen mit dem Kuerzel: Auftrag, die Daten der Instanzen bleiben",
   QUEUED[-1]["op"] == "rollback" and QUEUED[-1]["args"] == {
       "build": bid, "purge_instances": False}, QUEUED[-1])
r = c.post(f"/aufbau/{B_FAIL['id']}/rollback", headers=H,
           data={"confirm": "vfail", "purge_instances": "1"})
ok("...nur mit dem gesetzten Haken gehen sie mit",
   QUEUED[-1]["args"]["purge_instances"] is True)
r = c.post(f"/aufbau/{B_FAIL['id']}/rollback", headers=H,
           data={"confirm": "vfail", "purge_instances": "ja bitte"})
ok("ein anderer Wert als der des Hakens ist keine Einwilligung",
   QUEUED[-1]["args"]["purge_instances"] is False)
n = len(QUEUED)
r = c.post(f"/aufbau/{bid}/teleport", headers=H)
ok("eine unbekannte Handlung: zurueck zur Liste, nichts im Spool",
   r.status_code == 302 and len(QUEUED) == n)
r = c.post("/aufbau/b-20250101t000000-x/continue", headers=H)
ok("ein unbekannter Aufbau: zurueck zur Liste, nichts im Spool",
   r.status_code == 302 and len(QUEUED) == n)
r = c.post(f"/aufbau/{bid}/continue", headers=dict(H, Origin="http://boese.example"))
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and len(QUEUED) == n)

print("=== eine fehlende Sichtdatei ist eine leere Liste ===")
os.remove(VF)
r = c.get("/aufbau", headers=H)
ok("keine Sichtdatei: 200, leere Listen, der Weg zur Anleitung",
   r.status_code == 200 and "Noch kein Aufbau" in r.get_data(as_text=True)
   and "noch kein Profil" in r.get_data(as_text=True))

print("")
print("FAIL" if fails else "PASS", fails, "failure(s)")
sys.exit(1 if fails else 0)
