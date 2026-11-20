#!/usr/bin/env python3
"""Die Seite Einrichten (RFC-0109 Stufe 2) und die Rezepte auf Aufbau.

Gemessen wird, was das RFC verspricht:

  * Einrichten gibt es im Zusammenhang eines Mandanten: für seinen
    tenant_admin (nur der eigene Mandant, nie am Ort eines anderen) und für
    den Betreiber am Ort des Mandanten; am Knoten selbst 404, für andere 403;
    der Menüpunkt steht genau dort;
  * ein tenant_admin sieht nur Rezepte, die für SEINEN Mandanten
    freigegeben sind, und nur die Läufe seines Mandanten;
  * die Seite schreibt nichts selbst: „Was fehlt noch?“, „Einrichten“,
    „Rückgängig“ stellen einen Auftrag `tenant-recipe` mit dem Kürzel des
    eigenen Mandanten; ein Feld, das das Rezept nicht kennt, geht nicht mit;
    fremde Herkunft, ein unbestätigtes Rückgängig kommen nie in den Spool;
  * „Einrichten“ steht erst da, wenn die Vorschau bereit ist, und trägt
    genau ihre Werte; mit Konflikt oder offener Wahl gibt es den Knopf nicht;
  * die Werte aus der App stehen als Auswahl da, die Ampel mit Vorschlag;
  * auf Aufbau sieht der Betreiber die Rezepte mit „Freigegeben für“, und
    die Freigabe ist ein Auftrag; ein tenant_admin kommt nicht hin.

Braucht Flask; der Knoten ist ersetzt (Sicht-Datei und Spool sind Attrappen).
Aufruf: python3 test/test_tenant_recipe_page.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import recipe_view as rv                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


HILFE = {"id": "hilfe-assistent", "title": "Hilfe-Assistent im Chat", "version": "1.1.0",
         "major": 1, "problem": "", "description": "Der Chat bekommt den Assistenten.",
         "needs": {"help": {"app": "help", "label": "Hilfe", "install": False},
                   "models": {"app": "ai-gateway", "label": "Modelle", "install": False}},
         "params": {"model": {"kind": "setup_item", "need": "models", "item_kind": "alias",
                              "default": "standard", "label": "Modellname"},
                    "ceiling": {"kind": "light", "from": "model", "label": "Ampel"}},
         "steps": [{"id": "freigabe", "type": "app.setup"}],
         "released_for": ["kunde"], "releases": {"kunde": [1]}}
GEHEIM = dict(HILFE, id="nur-betreiber", title="Nur Betreiber", released_for=[], releases={})
KAPUTT = {"id": "kaputt", "title": "", "version": "", "problem": "not valid JSON"}
RUN_K = {"id": "r-20261120t100000-kunde", "kind": "recipe", "recipe": "hilfe-assistent",
         "title": "Hilfe-Assistent im Chat", "label": "kunde", "state": "done",
         "created": "2026-11-20T10:00:00+00:00", "by": "kv", "version": "1.1.0",
         "steps": [{"id": "freigabe", "type": "app.setup", "state": "done",
                    "note": "made", "made": ["setup:modelle:client:kunde-chat"]}]}
RUN_P = dict(RUN_K, id="r-20261120t100100-pxx", label="pxx", by="px")
VIEW = {"tenants": [{"label": "kunde", "name": "Kunde"}, {"label": "pxx", "name": ""}],
        "recipes": [HILFE, GEHEIM, KAPUTT], "runs": [RUN_P, RUN_K]}

print("=== die reinen Regeln ===")
ok("tenant_admin von kunde: nur das freigegebene Rezept",
   [r["id"] for r in rv.recipes_for(VIEW, "kunde", "tenant_admin")] == ["hilfe-assistent"])
ok("tenant_admin von pxx: nichts", rv.recipes_for(VIEW, "pxx", "tenant_admin") == [])
ok("Betreiber: alle, das kaputte nicht ausführbar",
   [(r["id"], r["runnable"]) for r in rv.recipes_for(VIEW, "pxx", "server_admin")]
   == [("hilfe-assistent", True), ("nur-betreiber", True), ("kaputt", False)])
ok("Läufe nur des eigenen Mandanten", [r["id"] for r in rv.runs_for(VIEW, "kunde")] == [RUN_K["id"]])
ok("ein fremder Lauf wird nicht gefunden, ein Pfad auch nicht",
   rv.find_run(VIEW, RUN_P["id"], "kunde") is None and rv.find_run(VIEW, "../x", "kunde") is None
   and rv.find_run(VIEW, RUN_K["id"], "kunde") is RUN_K)
ok("ein nicht freigegebenes Rezept findet der tenant_admin nicht",
   rv.find_recipe(VIEW, "nur-betreiber", "kunde", "tenant_admin") is None
   and rv.find_recipe(VIEW, "hilfe-assistent", "kunde", "tenant_admin")["id"] == "hilfe-assistent")
sp = rv.start_params(HILFE, {"model": " chat-default ", "ceiling": "", "help": "",
                             "boese": "x", "models": "modelle"})
ok("Formular: nur bekannte Namen, leere weg, gekürzt", sp == {"model": "chat-default",
                                                              "models": "modelle"}, sp)
PV_READY = {"recipe": "hilfe-assistent", "tenant": "kunde", "problems": [],
            "needs": {"help": {"app": "help", "name": "hilfe", "key": "kunde-hilfe",
                               "install": False},
                      "models": {"app": "ai-gateway", "name": "modelle", "key": "kunde-modelle",
                                 "install": False}},
            "choices": {"model": {"need": "models", "item_kind": "alias", "value": "chat-default",
                                  "items": [{"id": "chat-default", "title": "openrouter:x",
                                             "light": "red"},
                                            {"id": "lokal", "title": "ollama:y", "light": "green"}],
                                  "note": ""},
                        "ceiling": {"light": True, "value": "red", "suggested": "red"}},
            "params": {"model": "chat-default", "ceiling": "red"},
            "rows": [{"id": "freigabe", "type": "app.setup", "verdict": "absent", "note": "fehlt"}]}
PV_CHOOSE = dict(PV_READY, problems=["bitte wählen: Modellname"], rows=[], params={})
PV_BLOCK = dict(PV_READY, rows=[{"id": "freigabe", "type": "app.setup", "verdict": "conflict",
                                 "note": "ein Mensch"}])
PV_DONE = dict(PV_READY, rows=[{"id": "freigabe", "type": "app.setup", "verdict": "present",
                                "note": "da"}])
ok("Zustand der Vorschau: bereit / wählen / Halt / alles da",
   [rv.preview_state(x) for x in (PV_READY, PV_CHOOSE, PV_BLOCK, PV_DONE, None)]
   == ["ready", "choose", "blocked", "complete", "none"])
f = {x["name"]: x for x in rv.fields(HILFE, PV_READY)}
ok("Felder: die Liste der App als Auswahl, die Ampel mit Vorschlag, die Instanz der Vorschau",
   [i["id"] for i in f["model"]["entries"]] == ["chat-default", "lokal"]
   and f["model"]["value"] == "chat-default"
   and any(o["selected"] and o["value"] == "red" and "vorgeschlagen" in o["text"]
           for o in f["ceiling"]["options"]) and f["models"]["value"] == "modelle", f)

# ----------------------------------------------------------- the pages
import app as portal                                          # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-recipe-page-")
VF = os.path.join(TMP, "recipe-view.json")
portal.RECIPE_VIEW_FILE = VF
json.dump({"schema": "0.1", **VIEW}, open(VF, "w"))
portal.BUILD_VIEW_FILE = os.path.join(TMP, "build-view.json")
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []


def fake_queue(rid, name, payload, wait):
    QUEUED.append(dict(payload, _rid=rid))


mg.CTX["queue"] = fake_queue
TENANTS = {"t-kunde": {"label": "kunde"}, "t-pxx": {"label": "pxx"},
           "t-default": {"label": "node"}}
portal.load_tenants = lambda: TENANTS
WHO = {"role": "tenant_admin", "roles": {"tenant_admin"}, "tenant": "t-kunde", "host": "t-kunde"}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "kv", "X-OAAP-Roles": "tenant_admin"}


def as_(role, tenant, host):
    WHO.update(role=role, roles={role} if role else {"user"}, tenant=tenant, host=host)


def job(op, result):
    """A finished job in the spool, as the host would leave it."""
    rid = os.urandom(16).hex()
    jd = os.path.join(TMP, "jobs", rid)
    os.makedirs(jd)
    json.dump({"id": rid, "op": op, "tenant": WHO["tenant"] if WHO["role"] == "tenant_admin"
               else WHO["host"], "role": WHO["role"]}, open(os.path.join(jd, "meta.json"), "w"))
    json.dump(dict({"id": rid, "op": op}, **result), open(os.path.join(jd, "result.json"), "w"))
    return rid


print("=== wer, und wo ===")
r = c.get("/einrichten", headers=H)
t = r.get_data(as_text=True)
ok("tenant_admin von kunde an seinem Ort: das freigegebene Rezept, seine Läufe, der Menüpunkt",
   r.status_code == 200 and "Hilfe-Assistent im Chat" in t and "Nur Betreiber" not in t
   and RUN_K["id"] in t and RUN_P["id"] not in t and 'href="/einrichten"' in t, t[-800:])
as_("tenant_admin", "t-pxx", "t-pxx")
t = c.get("/einrichten", headers=H).get_data(as_text=True)
ok("tenant_admin von pxx: kein Rezept, der Weg zum Betreiber", "noch kein Rezept freigegeben" in t
   and "Hilfe-Assistent" not in t.split("Läufe")[0], t[-600:])
as_("tenant_admin", "t-pxx", "t-kunde")
codes = [c.get("/einrichten", headers=H).status_code,
         c.get("/einrichten/rezept/hilfe-assistent", headers=H).status_code,
         c.post("/einrichten/rezept/hilfe-assistent/vorschau", headers=H).status_code]
ok("tenant_admin von pxx am Ort von kunde: 403, nichts im Spool", codes == [403] * 3 and not QUEUED,
   codes)
as_("server_admin", "t-default", None)
r = c.get("/einrichten", headers=H)
mt = c.get("/kohorten", headers=H).get_data(as_text=True)
ok("Betreiber am Knoten: 404 mit Hinweis aufs Menü, kein Menüpunkt",
   r.status_code == 404 and "Mandanten wählen" in r.get_data(as_text=True)
   and 'href="/einrichten"' not in mt)
as_("server_admin", "t-default", "t-pxx")
t = c.get("/einrichten", headers=H).get_data(as_text=True)
ok("Betreiber am Ort von pxx: jedes Rezept, mit dem Hinweis auf fehlende Freigabe",
   "Nur Betreiber" in t and "nicht freigegeben" in t and "abgelehnt: not valid JSON" in t, t[-900:])
as_("", "t-kunde", "t-kunde")
r = c.get("/einrichten", headers=H)
ok("ein Mitglied: 403", r.status_code == 403)

print("=== Vorschau und Lauf ===")
as_("tenant_admin", "t-kunde", "t-kunde")
r = c.get("/einrichten/rezept/nur-betreiber", headers=H)
ok("ein nicht freigegebenes Rezept: zurück mit Hinweis", r.status_code == 302
   and "err=" in r.headers["Location"])
r = c.get("/einrichten/rezept/hilfe-assistent", headers=H)
t = r.get_data(as_text=True)
ok("das Formular: Bedarfe und Werte, noch ohne „Einrichten“",
   r.status_code == 200 and 'name="model"' in t and 'name="help"' in t
   and "Was fehlt noch?" in t and 'action="/einrichten/rezept/hilfe-assistent/lauf"' not in t)
r = c.post("/einrichten/rezept/hilfe-assistent/vorschau", headers=dict(H, Origin="http://boese.example"),
           data={"model": "x"})
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and not QUEUED)
r = c.post("/einrichten/rezept/hilfe-assistent/vorschau", headers=H,
           data={"model": "chat-default", "boese": "x", "tenant": "pxx"})
q = QUEUED[-1] if QUEUED else {}
ok("Was fehlt noch?: ein Auftrag tenant-recipe/preview mit dem eigenen Kürzel, ohne fremde Felder",
   r.status_code == 302 and q.get("action") == "tenant-recipe" and q.get("op") == "preview"
   and q["args"] == {"recipe": "hilfe-assistent", "tenant": "kunde",
                     "params": {"model": "chat-default"}}, q)
rid = job("preview", {"ok": True, "message": "Vorschau fertig", "preview": PV_READY})
t = c.get(f"/einrichten/rezept/hilfe-assistent?job={rid}", headers=H).get_data(as_text=True)
ok("Vorschau bereit: Zeilen, Auswahl aus der App, „Einrichten“ mit genau ihren Werten",
   "wird angelegt" in t and 'value="lokal"' in t
   and 'action="/einrichten/rezept/hilfe-assistent/lauf"' in t
   and 'name="model" value="chat-default"' in t and 'name="ceiling" value="red"' in t
   and 'name="models" value="modelle"' in t, t[-1500:])
rid = job("preview", {"ok": True, "message": "Bitte wählen", "preview": PV_CHOOSE})
t = c.get(f"/einrichten/rezept/hilfe-assistent?job={rid}", headers=H).get_data(as_text=True)
ok("offene Wahl: der Satz, die Auswahl, kein „Einrichten“", "bitte wählen: Modellname" in t
   and 'action="/einrichten/rezept/hilfe-assistent/lauf"' not in t)
rid = job("preview", {"ok": True, "message": "Halt", "preview": PV_BLOCK})
t = c.get(f"/einrichten/rezept/hilfe-assistent?job={rid}", headers=H).get_data(as_text=True)
ok("Konflikt: kein „Einrichten“", "Konflikt" in t
   and 'action="/einrichten/rezept/hilfe-assistent/lauf"' not in t)
rid = job("preview", {"ok": True, "message": "x", "preview": dict(PV_READY, tenant="pxx")})
t = c.get(f"/einrichten/rezept/hilfe-assistent?job={rid}", headers=H).get_data(as_text=True)
ok("eine Vorschau für einen anderen Mandanten wird nicht gezeigt",
   'action="/einrichten/rezept/hilfe-assistent/lauf"' not in t)
n = len(QUEUED)
r = c.post("/einrichten/rezept/hilfe-assistent/lauf", headers=H,
           data={"model": "chat-default", "ceiling": "red", "models": "modelle"})
q = QUEUED[-1]
ok("Einrichten: ein Auftrag run, zurück zur Liste mit dem Auftrag",
   len(QUEUED) == n + 1 and q["op"] == "run" and q["args"]["params"]["ceiling"] == "red"
   and "/einrichten?job=" in r.headers["Location"], q)
rid = job("run", {"ok": True, "message": "Lauf r-…: done", "run": RUN_K["id"]})
t = c.get(f"/einrichten?job={rid}", headers=H).get_data(as_text=True)
ok("der Auftrag ist fertig: Knopf zum Lauf", f'href="/einrichten/lauf/{RUN_K["id"]}"' in t)

print("=== ein Lauf ===")
r = c.get(f"/einrichten/lauf/{RUN_P['id']}", headers=H)
ok("ein Lauf eines anderen Mandanten: zurück zur Liste", r.status_code == 302)
t = c.get(f"/einrichten/lauf/{RUN_K['id']}", headers=H).get_data(as_text=True)
ok("der eigene Lauf: Schritte und Rückgängig", "freigabe" in t and "Rückgängig" in t)
n = len(QUEUED)
r = c.post(f"/einrichten/lauf/{RUN_K['id']}/rollback", headers=H, data={})
ok("Rückgängig ohne Haken: nichts im Spool", len(QUEUED) == n and "err=" in r.headers["Location"])
r = c.post(f"/einrichten/lauf/{RUN_K['id']}/rollback", headers=H, data={"confirm": "1"})
q = QUEUED[-1]
ok("Rückgängig mit Haken: ein Auftrag rollback für genau diesen Lauf",
   len(QUEUED) == n + 1 and q["op"] == "rollback" and q["args"] == {"run": RUN_K["id"],
                                                                    "tenant": "kunde"}, q)
r = c.post(f"/einrichten/lauf/{RUN_P['id']}/rollback", headers=H, data={"confirm": "1"})
ok("Köder: Rückgängig eines fremden Laufs kommt nicht in den Spool", len(QUEUED) == n + 1)
r = c.post(f"/einrichten/lauf/{RUN_K['id']}/confirm", headers=H, data={"step": "freigabe"})
ok("Bestätigen, wo nichts wartet: nichts im Spool", len(QUEUED) == n + 1)

print("=== Rezepte auf Aufbau ===")
as_("server_admin", "t-default", None)
t = c.get("/aufbau", headers=H).get_data(as_text=True)
ok("der Betreiber sieht die Rezepte mit „Freigegeben für“",
   "Rezepte" in t and "Hilfe-Assistent im Chat" in t and "niemandem (nur der Betreiber)" in t
   and 'action="/aufbau/rezepte/freigabe"' in t, t[-1500:])
n = len(QUEUED)
r = c.post("/aufbau/rezepte/freigabe", headers=H,
           data={"recipe": "nur-betreiber", "tenant": "pxx", "op": "release"})
q = QUEUED[-1]
ok("Freigeben: ein Auftrag release", len(QUEUED) == n + 1 and q["action"] == "tenant-recipe"
   and q["op"] == "release" and q["args"] == {"recipe": "nur-betreiber", "tenant": "pxx"}, q)
r = c.post("/aufbau/rezepte/freigabe", headers=H,
           data={"recipe": "nur-betreiber", "tenant": "gibts-nicht", "op": "release"})
ok("ein unbekannter Mandant: nichts im Spool", len(QUEUED) == n + 1)
as_("tenant_admin", "t-kunde", None)
r = c.post("/aufbau/rezepte/freigabe", headers=H,
           data={"recipe": "nur-betreiber", "tenant": "kunde", "op": "release"})
ok("ein tenant_admin gibt nichts frei: 403", r.status_code == 403 and len(QUEUED) == n + 1)

print("")
print("PASS: alles" if not fails else f"FAIL: {fails}")
sys.exit(1 if fails else 0)
