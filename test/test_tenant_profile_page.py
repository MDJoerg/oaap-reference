#!/usr/bin/env python3
"""Profile hoch- und herunterladen im Portal (RFC-0055 §14, oaap.core.portal 2.11).

  * Vorlage und Profile lassen sich herunterladen (JSON, als Anhang, nicht
    gespeichert); eine Kennung, die keine ist, oder ein abgelehntes Profil
    ergibt 404;
  * Hochladen: nur die Anfrage `tenant-profile`/`put` mit dem Text der Datei
    kommt in den Spool -- keine Datei, ein Nicht-JSON, eine zu grosse Datei und
    eine fremde Herkunft nie; Loeschen nur, was in der Sicht steht;
  * 403 fuer jeden ausser dem Betreiber am Knoten selbst, 404 am Ort eines
    Mandanten, an jeder neuen Tuer;
  * ein Parameter der Art bool ist ein Haken im Startformular und im
    Einladungsformular; ein nicht gesetzter Haken wird als "nein" gesendet.

Aufruf: python3 test/test_tenant_profile_page.py
"""
import io
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import build_view as bv                                       # noqa: E402
import tenant_build as tb                                     # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


print("=== die Vorlage ===")
ok("die Vorlage besteht die Pruefung des Knotens und die Regeln fuer Hochgeladenes",
   tb.profile_problems(bv.TEMPLATE) == []
   and tb.upload_problems(bv.TEMPLATE, {"webseite"}) == [])
ok("...und ist als Text wieder dieselbe Vorlage", json.loads(bv.template_text()) == bv.TEMPLATE)
ok("die Vorlage erklaert sich selbst (Schrittarten, bool, when, App aus dem Katalog)",
   all(w in bv.TEMPLATE["description"] for w in ("bool", "when", "app.install", "Katalog")))
t, pb = bv.upload_text(b"")
ok("eine leere Datei wird benannt", t is None and pb)
t, pb = bv.upload_text(b"x" * (bv.PROFILE_FILE_MAX + 1))
ok("eine zu grosse Datei wird benannt", t is None and "KB" in pb)
t, pb = bv.upload_text(b"\xff\xfe\x00 kein utf8")
ok("Bytes, die kein UTF-8 sind, werden benannt", t is None and pb)
t, pb = bv.upload_text(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode())
ok("ein JSON mit Byte-Order-Mark (Windows-Editor) wird gelesen", pb is None and json.loads(t) == {"a": 1})
pr = {"id": "v", "params": {"label": {"kind": "label", "required": True},
                            "web": {"kind": "bool", "default": True, "label": "Mit Webseite"}},
      "steps": [{"id": "a", "type": "tenant.create"},
                {"id": "w", "type": "app.install", "when": "web"}]}
ok("Haken gesetzt -> true, nicht gesetzt -> false (ein Browser sendet ihn dann gar nicht)",
   bv.start_params(pr, {"label": "x", "web": "1"})[0]["web"] == "true"
   and bv.start_params(pr, {"label": "x"})[0]["web"] == "false")
ok("der Plan nennt, wovon ein Schritt abhaengt",
   bv.plan(pr)[1]["when"] == "Mit Webseite" and bv.plan(pr)[0]["when"] == "")
ok("ein uebersprungener Schritt zaehlt beim Fortschritt als erledigt",
   bv.detail({"state": "waiting", "steps": [{"id": "a", "type": "tenant.create", "state": "done"},
                                            {"id": "w", "type": "app.install", "state": "skipped"},
                                            {"id": "m", "type": "manual", "state": "waiting"}]})
   ["progress"] == "2 von 3")

print("=== die Seiten ===")
import app as portal                                          # noqa: E402

PROFILE = {"id": "verein", "title": "Verein", "problem": "", "doc": bv.TEMPLATE,
           "params": bv.TEMPLATE["params"],
           "steps": [{"id": s["id"], "type": s["type"], "when": s.get("when", "")}
                     for s in bv.TEMPLATE["steps"]]}
BAD = {"id": "kaputt", "title": "", "problem": "step 9: unknown step type 'x'"}
TMP = tempfile.mkdtemp(prefix="oaap-profile-page-")
portal.BUILD_VIEW_FILE = os.path.join(TMP, "build-view.json")
portal.REQUEST_VIEW_FILE = os.path.join(TMP, "request-view.json")
json.dump({"profiles": [PROFILE, BAD], "builds": []}, open(portal.BUILD_VIEW_FILE, "w"))
json.dump({}, open(portal.REQUEST_VIEW_FILE, "w"))
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []
mg.CTX["queue"] = lambda rid, name, payload, wait: QUEUED.append(dict(payload, _rid=rid))
WHO = {"role": "server_admin", "roles": {"server_admin"}, "tenant": "", "host": None}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}

r = c.get("/aufbau/vorlage.json", headers=H)
ok("die Vorlage kommt als JSON-Anhang, nicht gespeichert",
   r.status_code == 200 and r.headers["Content-Type"].startswith("application/json")
   and "attachment" in r.headers["Content-Disposition"]
   and r.headers["Cache-Control"] == "no-store" and json.loads(r.data) == bv.TEMPLATE)
r = c.get("/aufbau/profile/verein.json", headers=H)
ok("ein Profil kommt so, wie der Knoten es haelt", r.status_code == 200
   and json.loads(r.data) == bv.TEMPLATE and 'filename="verein.json"' in r.headers["Content-Disposition"])
codes = [c.get(u, headers=H).status_code for u in
         ("/aufbau/profile/kaputt.json", "/aufbau/profile/nope.json",
          "/aufbau/profile/..%2Fx.json", "/aufbau/profile/Gross.json")]
ok("abgelehntes, unbekanntes und falsch geschriebenes Profil: 404", codes == [404] * 4, codes)
t = c.get("/aufbau", headers=H).get_data(as_text=True)
ok("die Liste hat Hochladen, Vorlage, Herunterladen (nur wo das Profil gilt) und Loeschen",
   'action="/aufbau/profile/hochladen"' in t and 'enctype="multipart/form-data"' in t
   and 'href="/aufbau/vorlage.json"' in t and t.count('/aufbau/profile/verein.json') == 1
   and '/aufbau/profile/kaputt.json' not in t and 'action="/aufbau/profile/kaputt/loeschen"' in t)

print("=== wer, und wo ===")
for who, role, roles in (("Verwalter", "tenant_admin", {"tenant_admin"}),
                         ("Mitglied", "", {"user"}), ("niemand", "", set())):
    WHO.update(role=role, roles=roles, tenant="t-a")
    codes = [c.get("/aufbau/vorlage.json", headers=H).status_code,
             c.get("/aufbau/profile/verein.json", headers=H).status_code,
             c.post("/aufbau/profile/hochladen", headers=H,
                    data={"datei": (io.BytesIO(b"{}"), "p.json")}).status_code,
             c.post("/aufbau/profile/verein/loeschen", headers=H).status_code]
    ok(f"{who}: 403 an jeder neuen Tuer, nichts im Spool", codes == [403] * 4 and not QUEUED, codes)
WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host="t-kunde")
codes = [c.get("/aufbau/vorlage.json", headers=H).status_code,
         c.get("/aufbau/profile/verein.json", headers=H).status_code,
         c.post("/aufbau/profile/hochladen", headers=H,
                data={"datei": (io.BytesIO(b"{}"), "p.json")}).status_code,
         c.post("/aufbau/profile/verein/loeschen", headers=H).status_code]
ok("am Ort eines Mandanten gibt es die Tueren nicht (404)", codes == [404] * 4 and not QUEUED, codes)
WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host=None)

print("=== Hochladen und Loeschen ===")


def up(data, name="p.json", headers=H):
    d = {} if data is None else {"datei": (io.BytesIO(data), name)}
    return c.post("/aufbau/profile/hochladen", headers=headers, data=d,
                  content_type="multipart/form-data")


for label, resp in (("keine Datei", up(None)), ("leere Datei", up(b"")),
                    ("kein JSON", up(b"das ist kein json")),
                    ("zu gross", up(b"{" + b" " * (bv.PROFILE_FILE_MAX + 10) + b"}")),
                    ("fremde Herkunft", up(b"{}", headers={**H, "Origin": "https://boese.example"}))):
    ok(f"{label}: nichts im Spool", not QUEUED and resp.status_code in (302, 403), (resp.status_code, QUEUED))
body = bv.template_text().encode()
r = up(body)
ok("eine Datei wird als Anfrage mit ihrem Text gestellt -- die Pruefung macht der Knoten",
   r.status_code == 302 and QUEUED and QUEUED[0]["action"] == "tenant-profile"
   and QUEUED[0]["op"] == "put" and QUEUED[0]["args"] == {"content": body.decode()}, QUEUED)
QUEUED.clear()
ok("Loeschen eines unbekannten, falsch geschriebenen Profils: nichts im Spool",
   c.post("/aufbau/profile/nope/loeschen", headers=H).status_code == 302
   and c.post("/aufbau/profile/..%2Fx/loeschen", headers=H).status_code in (302, 404) and not QUEUED)
r = c.post("/aufbau/profile/verein/loeschen", headers=H)
ok("Loeschen eines bekannten Profils stellt die Anfrage", r.status_code == 302 and QUEUED
   and QUEUED[0]["op"] == "delete" and QUEUED[0]["args"] == {"id": "verein"}, QUEUED)
QUEUED.clear()
r = c.post("/aufbau/profile/verein/loeschen", headers={**H, "Origin": "https://boese.example"})
ok("Loeschen mit fremder Herkunft: 403", r.status_code == 403 and not QUEUED)

print("=== der Haken im Formular ===")
t = c.get("/aufbau/neu?profil=verein", headers=H).get_data(as_text=True)
tag = re.search(r'<input type="checkbox" name="with_web"[^>]*>', t)
ok("ein bool ist ein Haken, standardmaessig gesetzt, mit seiner Beschriftung",
   tag and "checked" in tag.group(0) and "Mit Webseite" in t, t[-1200:])
ok("der Plan nennt den Schritt, der vom Haken abhaengt", "nur mit „Mit Webseite“" in t)
r = c.post("/aufbau/neu", headers=H, data={"profil": "verein", "label": "vneu", "title": "T"})
ok("ohne Haken geht ausdruecklich 'false' in die Anfrage",
   QUEUED and QUEUED[-1]["args"]["params"]["with_web"] == "false", QUEUED)
QUEUED.clear()
r = c.post("/aufbau/neu", headers=H, data={"profil": "verein", "label": "vneu", "title": "T",
                                           "with_web": "1"})
ok("mit Haken 'true'", QUEUED and QUEUED[-1]["args"]["params"]["with_web"] == "true", QUEUED)

import hashlib                                               # noqa: E402
LINK = "Zk3vQ9xT2mLw8RnB5cYd1HfJ7aPe4UsG6oVi0KtNqXc"
json.dump({"live": {hashlib.sha256(LINK.encode()).hexdigest(): "verein"}},
          open(portal.REQUEST_VIEW_FILE, "w"))
QUEUED.clear()
t = c.get(f"/anfrage?t={LINK}").get_data(as_text=True)
tag = re.search(r'<input type="checkbox" name="with_web"[^>]*>', t)
ok("auch das Formular des Interessenten zeigt den Haken, standardmaessig gesetzt",
   tag and "checked" in tag.group(0) and "Mit Webseite" in t, t[-800:])
c.post(f"/anfrage?t={LINK}", data={"label": "vneu", "title": "T", "contact": "a@b.example"})
ok("...und sendet ohne Haken 'false' an den Knoten",
   QUEUED and QUEUED[-1]["args"]["params"]["with_web"] == "false", QUEUED)

print()
print("FAILURES" if fails else "ALL PASS", fails)
sys.exit(1 if fails else 0)
