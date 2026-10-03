#!/usr/bin/env python3
"""Die Seiten zu Einladung und Antrag (RFC-0055 Stufe 4, oaap.core.portal 2.10).

  * die oeffentliche Seite `/anfrage?t=<link>` antwortet ohne Anmeldung, aber nur
    auf einen offenen Link; ein toter, ein erfundener und ein Link am Ort eines
    Mandanten ergeben DIESELBE Antwort (404), die nichts verraet;
  * das Formular entsteht aus dem Profil des Links, nicht aus der Anfrage; ein
    fremdes Profil kann man nicht waehlen;
  * Koeder am Absenden: fehlende Pflichtfelder, ein Kuerzel in falscher Form,
    eine Adresse, die keine ist, ein zu grosser Koerper, eine fremde Herkunft --
    nichts davon kommt in den Spool; zweimal absenden stellt nur EINE Anfrage;
    mehr als zehn in der Minute werden gebremst;
  * die Seiten des Betreibers: 403 fuer jeden anderen, 404 am Ort eines Mandanten;
    der Link wird nur in der Antwort auf das Erzeugen gezeigt, nie wieder;
    Freigeben/Ablehnen/Widerrufen stellen die Anfrage, die der Host noch einmal
    prueft, und nur fuer das, was offen ist.

Braucht Flask; der Knoten ist ersetzt (Sichtdateien und Spool sind Attrappen).
Aufruf: python3 test/test_tenant_request_page.py
"""
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import build_view as bv                                       # noqa: E402
import tenant_request as tr                                   # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def h(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


PROFILE = {"id": "verein", "title": "Verein", "problem": "",
           "params": {"label": {"kind": "label", "required": True, "label": "Kürzel"},
                      "title": {"kind": "text", "required": True, "max": 60,
                                "label": "Name des Vereins"},
                      "color": {"kind": "color", "default": None}},
           "steps": [{"id": "tenant", "type": "tenant.create"}]}
OTHER = {**PROFILE, "id": "intern", "params": {"label": {"kind": "label", "required": True},
                                              "geheim": {"kind": "text", "required": True}}}
LIVE = tr.new_token()
LIVE2 = tr.new_token()
DEAD = tr.new_token()

print("=== die reinen Regeln ===")
RV = {"live": {h(LIVE): "verein", h(LIVE2): "intern"},
      "invites": [{"id": "inv-aaaaaaaaaaaa", "profile": "verein", "state": "open",
                   "created": "2026-10-03T10:00:00+00:00", "expires": "2026-10-17T10:00:00+00:00",
                   "by": "betreiber", "note": "Handball"},
                  {"id": "inv-bbbbbbbbbbbb", "profile": "verein", "state": "used",
                   "created": "2026-10-01T10:00:00+00:00", "expires": "2026-10-15T10:00:00+00:00",
                   "by": "betreiber", "note": ""}],
      "requests": [{"id": "req-cccccccccccc", "profile": "verein", "label": "vneu",
                    "params": {"label": "vneu", "title": "Verein Neu"},
                    "contact": "vorstand@verein.example", "note": "Handball",
                    "state": "pending", "created": "2026-10-03T11:00:00+00:00",
                    "decided": "", "by": "", "build": "", "reason": ""},
                   {"id": "req-dddddddddddd", "profile": "verein", "label": "valt",
                    "params": {"label": "valt", "title": "Alt"}, "contact": "",
                    "state": "rejected", "created": "2026-10-02T11:00:00+00:00",
                    "decided": "2026-10-02T12:00:00+00:00", "by": "betreiber",
                    "build": "", "reason": "doppelt"}]}
ok("ein offener Link nennt sein Profil, ein toter, ein kurzer oder ein erfundener nichts",
   bv.token_profile(RV, LIVE) == "verein" and bv.token_profile(RV, DEAD) is None
   and bv.token_profile(RV, "kurz") is None and bv.token_profile(RV, "../" * 16) is None
   and bv.token_profile({}, LIVE) is None and bv.token_profile(None, LIVE) is None)
ok("Antraege und Einladungen in Klartext; offene zaehlen",
   bv.pending_count(RV) == 1 and bv.request_rows(RV)[0]["state"] == "Wartet auf Entscheidung"
   and bv.invite_rows(RV)[1]["state"] == "Benutzt"
   and bv.request_rows(RV)[0]["params"][1] == ("title", "Verein Neu"))
ok("eine Kennung, die keine ist, findet nichts",
   bv.find_item(RV, "../../x") is None and bv.find_item(RV, "req-cccccccccccc")["label"] == "vneu"
   and bv.find_item(RV, "inv-aaaaaaaaaaaa")["note"] == "Handball")
form = {"label": " VNeu ", "title": "Verein Neu", "contact": "vorstand@verein.example"}
par, con, err = bv.public_params(PROFILE, form)
ok("das Formular des Interessenten: Kuerzel klein, Adresse getrennt, kein Fehler",
   par == {"label": "vneu", "title": "Verein Neu"} and con == "vorstand@verein.example"
   and not err, (par, con, err))
for label, f in (("fehlendes Pflichtfeld", {"label": "vneu", "contact": "a@b.de"}),
                 ("Kuerzel mit Leerzeichen", {"label": "v neu", "title": "T", "contact": "a@b.de"}),
                 ("Kuerzel zu lang", {"label": "v" * 40, "title": "T", "contact": "a@b.de"}),
                 ("Adresse fehlt", {"label": "vneu", "title": "T"}),
                 ("Adresse keine", {"label": "vneu", "title": "T", "contact": "a@b"}),
                 ("Titel zu lang", {"label": "vneu", "title": "T" * 400, "contact": "a@b.de"})):
    ok(f"Fehler benannt: {label}", bool(bv.public_params(PROFILE, f)[2]), f)

print("=== die Seiten ===")
import app as portal                                          # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-request-page-")
portal.BUILD_VIEW_FILE = os.path.join(TMP, "build-view.json")
portal.REQUEST_VIEW_FILE = os.path.join(TMP, "request-view.json")
json.dump({"schema": "0.1", "profiles": [PROFILE, OTHER], "builds": []},
          open(portal.BUILD_VIEW_FILE, "w"))
json.dump(RV, open(portal.REQUEST_VIEW_FILE, "w"))
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []


def fake_queue(rid, name, payload, wait):
    QUEUED.append(dict(payload, _rid=rid))


mg.CTX["queue"] = fake_queue
WHO = {"role": "", "roles": set(), "tenant": "", "host": None}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
portal.caller_name = lambda: WHO.get("name", "")
c = portal.app.test_client()
H = {}                                                       # the gateway strips identity headers
OP = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}

r = c.get(f"/anfrage?t={LIVE}", headers=H)
t = r.get_data(as_text=True)
ok("ein offener Link zeigt das Formular, ohne Anmeldung",
   r.status_code == 200 and 'name="label"' in t and 'name="title"' in t
   and 'name="contact"' in t and 'type="email"' in t and "Name des Vereins" in t, t[-800:])
ok("das Kuerzel traegt den Hinweis, dass es oeffentlich ist",
   "öffentlich" in t)
ok("die Seite hat kein Menue, keine Abmeldung und verraet keinen Benutzer",
   "Abmelden" not in t and "/users" not in t and 'href="/aufbau' not in t)
ok("sie wird nicht gespeichert und gibt den Link nicht weiter",
   r.headers.get("Cache-Control") == "no-store" and 'name="referrer" content="no-referrer"' in t
   and "noindex" in t)
ok("das Formular schickt an den eigenen Link", f'action="/anfrage?t={LIVE}"' in t)
r2 = c.get(f"/anfrage?t={LIVE2}", headers=H)
t2 = r2.get_data(as_text=True)
ok("das Formular kommt aus dem Profil DES LINKS (ein anderes, ein anderes Formular)",
   'name="geheim"' in t2 and 'name="geheim"' not in t)
dead = [c.get(f"/anfrage?t={x}", headers=H) for x in (DEAD, "kurz", "a" * 40)]
dead.append(c.get("/anfrage", headers=H))
same = {(x.status_code, x.get_data(as_text=True)) for x in dead}
ok("ein toter, ein kurzer, ein erfundener und gar kein Link: dieselbe Antwort, 404",
   len(same) == 1 and dead[0].status_code == 404 and "gilt nicht" in dead[0].get_data(as_text=True))
WHO["host"] = "t-kunde"
ok("am Ort eines Mandanten gibt es die Seite nicht, auch nicht mit offenem Link",
   c.get(f"/anfrage?t={LIVE}", headers=H).status_code == 404
   and c.post(f"/anfrage?t={LIVE}", headers=H, data=form).status_code == 404 and not QUEUED)
WHO["host"] = None

print("=== Koeder am Absenden ===")
good = {"label": "vneu", "title": "Verein Neu", "contact": "vorstand@verein.example",
        "color__none": "1"}
for label, data in (("ohne Titel", {**good, "title": ""}),
                    ("Kuerzel mit Gross und Sonderzeichen", {**good, "label": "V!neu"}),
                    ("ohne Adresse", {**good, "contact": ""}),
                    ("mit falscher Adresse", {**good, "contact": "nein"})):
    r = c.post(f"/anfrage?t={LIVE}", headers=H, data=data)
    ok(f"{label}: 400, das Formular bleibt mit dem Eingegebenen, nichts im Spool",
       r.status_code == 400 and not QUEUED
       and (data["title"] == "" or "Verein Neu" in r.get_data(as_text=True)),
       (r.status_code, QUEUED))
r = c.post(f"/anfrage?t={LIVE}", headers={**H, "Origin": "https://boese.example"}, data=good)
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and not QUEUED)
r = c.post(f"/anfrage?t={LIVE}", headers=H, data={**good, "title": "x" * 9000})
ok("zu grosser Koerper: 413, nichts im Spool", r.status_code == 413 and not QUEUED, r.status_code)
r = c.post(f"/anfrage?t={DEAD}", headers=H, data=good)
ok("toter Link: 404, nichts im Spool", r.status_code == 404 and not QUEUED)
r = c.post(f"/anfrage?t={LIVE}", headers=H, data={**good, "shell": "rm -rf /", "profil": "intern"})
ok("zusaetzliche Felder (auch ein anderes Profil) aendern nichts am Profil des Links",
   r.status_code == 200 and len(QUEUED) == 1
   and set(QUEUED[0]["args"]["params"]) == {"label", "title"}, QUEUED)
q = QUEUED[0]
ok("der Spool bekommt genau die Aktion des Interessenten: Link, Parameter, Adresse",
   q["action"] == "tenant-request-submit" and q["op"] == "submit"
   and q["args"]["token"] == LIVE and q["args"]["contact"] == "vorstand@verein.example"
   and q["args"]["params"] == {"label": "vneu", "title": "Verein Neu"}, q)
t = r.get_data(as_text=True)
ok("die Antwort sagt, was geschieht (ein Mensch prueft), und zeigt das Formular nicht mehr",
   "abgeschickt" in t and "Ein Mensch prüft" in t and 'name="label"' not in t
   and LIVE not in t)
r = c.post(f"/anfrage?t={LIVE}", headers=H, data=good)
ok("zweimal absenden mit demselben Link: gleiche Antwort, aber nur EINE Anfrage im Spool",
   r.status_code == 200 and len(QUEUED) == 1, len(QUEUED))

# the brake: ten per window over the whole portal
QUEUED.clear()
portal._public_log.clear()
toks = [tr.new_token() for _ in range(12)]
RV["live"].update({h(x): "verein" for x in toks})
json.dump(RV, open(portal.REQUEST_VIEW_FILE, "w"))
codes = [c.post(f"/anfrage?t={x}", headers=H, data={**good, "label": "v" + str(i)}).status_code
         for i, x in enumerate(toks)]
ok("zehn je Minute gehen durch, die naechsten werden gebremst (429) und bleiben aus dem Spool",
   codes[:10] == [200] * 10 and codes[10:] == [429, 429] and len(QUEUED) == 10, codes)
portal._public_log.clear()

print("=== die Seiten des Betreibers ===")
QUEUED.clear()
for who, role, roles in (("Verwalter", "tenant_admin", {"tenant_admin"}),
                         ("Mitglied", "", {"user"}), ("niemand", "", set())):
    WHO.update(role=role, roles=roles, tenant="t-a")
    codes = [c.get("/aufbau/anfragen", headers=OP).status_code,
             c.post("/aufbau/anfragen/einladen", headers=OP, data={"profil": "verein"}).status_code,
             c.post("/aufbau/anfragen/req-cccccccccccc/freigeben", headers=OP).status_code,
             c.post("/aufbau/anfragen/inv-aaaaaaaaaaaa/widerrufen", headers=OP).status_code]
    ok(f"{who}: 403 an jeder Tuer, nichts im Spool", codes == [403] * 4 and not QUEUED, codes)
WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host="t-kunde")
codes = [c.get("/aufbau/anfragen", headers=OP).status_code,
         c.post("/aufbau/anfragen/req-cccccccccccc/freigeben", headers=OP).status_code]
ok("am Ort eines Mandanten gibt es die Seiten nicht (404)", codes == [404, 404] and not QUEUED, codes)
WHO.update(host=None)
r = c.get("/aufbau/anfragen", headers=OP)
t = r.get_data(as_text=True)
ok("der Betreiber sieht Antraege (mit Angaben und Adresse), Einladungen und das Formular",
   r.status_code == 200 and "Verein Neu" in t and "vorstand@verein.example" in t
   and "Freigeben und aufbauen" in t and "Widerrufen" in t and 'name="profil"' in t, t[-900:])
ok("ein abgelehnter Antrag hat keine Knoepfe und keine Adresse; der Grund steht da",
   "doppelt" in t and t.count("Freigeben und aufbauen") == 1)
ok("die Einladung zeigt nie einen Link", "/anfrage?" not in t)
ok("die Liste der Aufbauten verweist auf die Antraege, mit Zahl",
   "Einladungen und Anträge (1 offen)" in c.get("/aufbau", headers=OP).get_data(as_text=True))

r = c.post("/aufbau/anfragen/einladen", headers={**OP, "Origin": "https://boese.example"},
           data={"profil": "verein"})
ok("Einladen mit fremder Herkunft: 403, nichts im Spool", r.status_code == 403 and not QUEUED)
for label, data in (("ein Profil, das es nicht gibt", {"profil": "nope"}),
                    ("Gueltigkeit keine Zahl", {"profil": "verein", "days": "x"}),
                    ("Gueltigkeit 0", {"profil": "verein", "days": "0"}),
                    ("Gueltigkeit 61", {"profil": "verein", "days": "61"})):
    r = c.post("/aufbau/anfragen/einladen", headers=OP, data=data)
    ok(f"Einladen mit {label}: abgelehnt, nichts im Spool", r.status_code in (400, 404) and not QUEUED,
       (r.status_code, QUEUED))
r = c.post("/aufbau/anfragen/einladen", headers=OP,
           data={"profil": "verein", "note": "Handball", "days": "10"})
t = r.get_data(as_text=True)
q = QUEUED[0]
link = q["args"]["token"]
ok("Einladen stellt die Anfrage mit Profil, Notiz, Tagen und einem frischen Link",
   r.status_code == 200 and q["action"] == "tenant-request" and q["op"] == "invite"
   and q["args"]["profile"] == "verein" and q["args"]["days"] == 10
   and len(link) >= 32, q)
ok("der Link steht in der Antwort -- genau dort, und mit der Adresse des Knotens",
   f"http://localhost/anfrage?t={link}" in t and "nur dieses eine Mal" in t, t[-600:])
ok("...und er steht nirgends sonst: weder im Verlauf noch in der Liste beim naechsten Aufruf",
   link not in c.get("/aufbau/anfragen", headers=OP).get_data(as_text=True)
   and link not in c.get("/aufbau", headers=OP).get_data(as_text=True))
ok("die Antwort mit dem Link wird nicht gespeichert",
   r.headers.get("Cache-Control") == "no-store")

QUEUED.clear()
r = c.post("/aufbau/anfragen/req-cccccccccccc/freigeben", headers=OP)
ok("Freigeben stellt die Anfrage; der Host prueft Rolle und Antrag noch einmal",
   r.status_code == 302 and QUEUED[0]["op"] == "approve"
   and QUEUED[0]["args"] == {"id": "req-cccccccccccc"}
   and QUEUED[0]["action"] == "tenant-request", (r.status_code, QUEUED))
QUEUED.clear()
r = c.post("/aufbau/anfragen/req-cccccccccccc/ablehnen", headers=OP, data={"reason": "  passt   nicht "})
ok("Ablehnen nimmt den Grund bereinigt mit", QUEUED[0]["op"] == "reject"
   and QUEUED[0]["args"]["reason"] == "passt nicht", QUEUED)
QUEUED.clear()
for label, path in (("einen entschiedenen Antrag freigeben", "/aufbau/anfragen/req-dddddddddddd/freigeben"),
                    ("eine benutzte Einladung widerrufen", "/aufbau/anfragen/inv-bbbbbbbbbbbb/widerrufen"),
                    ("einen Antrag widerrufen", "/aufbau/anfragen/req-cccccccccccc/widerrufen"),
                    ("eine Einladung freigeben", "/aufbau/anfragen/inv-aaaaaaaaaaaa/freigeben"),
                    ("eine unbekannte Kennung", "/aufbau/anfragen/req-ffffffffffff/freigeben"),
                    ("eine Kennung mit Pfad", "/aufbau/anfragen/..%2F..%2Fx/freigeben"),
                    ("ein unbekanntes Verb", "/aufbau/anfragen/req-cccccccccccc/loeschen")):
    r = c.post(path, headers=OP)
    ok(f"{label}: nichts im Spool", not QUEUED, (path, r.status_code, QUEUED))
r = c.post("/aufbau/anfragen/inv-aaaaaaaaaaaa/widerrufen", headers=OP)
ok("eine offene Einladung widerrufen stellt die Anfrage", QUEUED and QUEUED[0]["op"] == "revoke", QUEUED)

print()
print("FAILURES" if fails else "ALL PASS", fails)
sys.exit(1 if fails else 0)
