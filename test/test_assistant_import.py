#!/usr/bin/env python3
"""Export `from` und „Aus Datei“ (RFC-0117 Stufe 2, 0.1.338).

Gemessen wird:

  A. Der Rezeptlauf nennt sich der Einrichtungstür: `setup_recipe_ref` gibt
     `<id>@<major>` (sonst ''), das Aufrufskript setzt `X-OAAP-Setup-Recipe` nur
     mit einem gültigen Wert, Vorschau und Lauf geben ihn den Treibern mit;
     `recipe_assistant_map` nennt je Vorlage, welcher formbare Wert welches Feld
     des Assistenten füllt (die drei gelieferten Vorlagen).
  B. `import_plan`: mit `from` und startbarer Vorlage gleicher Hauptversion
     vorausgefüllt (nur Felder der Zuordnung); ohne `from` ein Satz mit der
     Sicht; unbekannte Vorlage, andere Hauptversion, fehlende Freigabe je ein
     Satz; keine Austauschdatei, zu viele Assistenten: ein Problem.
  C. `fields` übernimmt Werte aus der Datei nur für formbare Werte, gekürzt,
     Zeilen als LF; eine Vorschau gewinnt.
  D. Die Seiten: „Aus Datei“ steht auf der Liste; die Datei wird gelesen und
     zeigt je Assistent den Weg; „Vorlage öffnen“ füllt den Assistenten per
     POST vor (Anweisungen mehrzeilig); fremde Herkunft 403; ein fremder
     tenant_admin 403; nichts landet im Spool.

Braucht Flask; der Knoten ist ersetzt (Sicht-Datei und Spool sind Attrappen).
Aufruf: python3 test/test_assistant_import.py
"""
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-assist-import-")
os.environ["OAAP_DATA_DIR"] = os.path.join(DATA, "appctl")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
import recipe_view as rv                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def load(rid):
    with open(os.path.join(HERE, "..", "platform", "recipes", rid + ".json"), encoding="utf-8") as f:
        return json.load(f)


print("=== A. Der Lauf nennt seine Vorlage ===")
ok("setup_recipe_ref: id@major", a.setup_recipe_ref("hilfe-assistent", "1.2.0") == "hilfe-assistent@1")
ok("… ohne Version oder mit kaputter Kennung: leer",
   a.setup_recipe_ref("hilfe-assistent", "") == "" and a.setup_recipe_ref("Böse Id", "1.0.0") == "")
ok("das Aufrufskript setzt die Kopfzeile nur mit Wert",
   'if q.get("recipe"):' in a.SETUP_CALL_SCRIPT and "X-OAAP-Setup-Recipe" in a.SETUP_CALL_SCRIPT)
src = open(a.__file__, encoding="utf-8").read()
ok("setup_call gibt nur einen gültigen Wert weiter",
   '"recipe": recipe if SETUP_RECIPE_RE.match(recipe or "") else ""' in src)
ok("Vorschau und Lauf geben den Treibern die Vorlage",
   src.count('drivers.recipe = setup_recipe_ref(doc["id"], doc.get("version"))') == 2
   and 'drivers.recipe = setup_recipe_ref(state.get("recipe", ""), state.get("version"))' in src)
d = a._BuildDrivers("kunde")
ok("ein Treiber beginnt ohne Vorlage", d.recipe == "")
maps = {rid: a.recipe_assistant_map(load(rid)) for rid in ("hilfe-assistent", "recherche", "assistent-leer")}
ok("Hilfe: Kennung, Titel, Begrüßung, Anweisungen, Modell aus formbaren Werten",
   maps["hilfe-assistent"] == {"id": "aid", "title": "title", "greeting": "greeting",
                               "system": "system", "model": "model"}, maps)
ok("Leer und Recherche haben eine Zuordnung (nicht leer, nur formbare Werte)",
   all(maps[r] and all(load(r)["params"][p].get("shape") is True for p in maps[r].values())
       for r in ("recherche", "assistent-leer")), maps)
ok("ein Rezept ohne Assistent: keine Zuordnung", a.recipe_assistant_map({"steps": []}) == {})


def row(rid, free, released=(), major=1):
    doc = load(rid)
    return {"id": rid, "title": doc["title"], "version": doc["version"], "major": major, "problem": "",
            "description": doc["description"],
            "needs": {n: {"app": s["app"], "label": s.get("label") or n,
                          "install": s.get("install") is True} for n, s in doc["needs"].items()},
            "params": doc["params"], "offers": doc["offers"], "card": doc["card"], "free": free,
            "shipped": True, "steps": [{"id": s["id"], "type": s["type"]} for s in doc["steps"]],
            "released_for": list(released), "releases": {}, "assistant_map": maps[rid]}


HILFE, RECH, LEER = row("hilfe-assistent", True), row("recherche", False), row("assistent-leer", True, major=2)
VIEW = {"tenants": [{"label": "kunde", "name": "Kunde"}], "recipes": [HILFE, RECH, LEER], "runs": []}
SYSTEM = "Zeile eins\nZeile zwei\nZeile drei"
FILE = {"v": 1, "assistants": [
    {"id": "hilfe-b", "title": "Hilfe B", "view": "hilfe", "model": "standard", "system": SYSTEM,
     "greeting": "Hallo", "visibility": {"scope": "tenant"}, "owner": "-",
     "from": "recipe:hilfe-assistent@1"},
    {"id": "frei", "title": "Frei", "view": "crm", "model": "standard", "system": "x",
     "visibility": {"scope": "tenant"}, "owner": "-"},
    {"id": "web", "title": "Web", "view": "recherche", "model": "standard", "system": "x",
     "visibility": {"scope": "tenant"}, "owner": "-", "from": "recipe:recherche@1"},
    {"id": "leer", "title": "Leer", "model": "standard", "system": "x",
     "visibility": {"scope": "tenant"}, "owner": "-", "from": "recipe:assistent-leer@1"},
    {"id": "weg", "title": "Weg", "model": "standard", "system": "x",
     "visibility": {"scope": "tenant"}, "owner": "-", "from": "recipe:gibt-es-nicht@1"},
]}

print("=== B. import_plan ===")
rows, prob = rv.import_plan(VIEW, FILE, "kunde", "tenant_admin")
by = {r["id"]: r for r in rows}
ok("keine Problemzeile, fünf Zeilen", prob == "" and len(rows) == 5, (prob, rows))
ok("Hilfe B: Vorlage, vorausgefüllt mit genau den Feldern der Zuordnung",
   by["hilfe-b"]["rid"] == "hilfe-assistent" and by["hilfe-b"]["prefill"]
   == {"aid": "hilfe-b", "title": "Hilfe B", "greeting": "Hallo", "system": SYSTEM, "model": "standard"},
   by["hilfe-b"])
ok("ohne from: Satz mit der Sicht, keine Vorlage",
   not by["frei"]["rid"] and "Sicht „crm“" in by["frei"]["sentence"], by["frei"])
ok("Recherche ohne Freigabe: Satz „Freigabe“", not by["web"]["rid"] and "Freigabe" in by["web"]["sentence"])
ok("andere Hauptversion: Satz, nicht vorausgefüllt",
   not by["leer"]["rid"] and "Hauptversion 1" in by["leer"]["sentence"], by["leer"])
ok("unbekannte Vorlage: Satz", not by["weg"]["rid"] and "gibt es auf diesem Knoten nicht" in by["weg"]["sentence"])
rows, prob = rv.import_plan(VIEW, FILE, "kunde", "server_admin")
ok("der Betreiber am Ort: Recherche vorausgefüllt",
   {r["id"]: r for r in rows}["web"]["rid"] == "recherche")
ok("keine Austauschdatei: ein Problem", rv.import_plan(VIEW, {"x": 1}, "kunde", "tenant_admin")[1] != "")
ok("zu viele: ein Problem",
   "Höchstens" in rv.import_plan(VIEW, {"v": 1, "assistants": [{"id": "x"}] * 21}, "kunde", "tenant_admin")[1])
ok("eine Datei mit Rechten wird nicht gespeichert (es gibt keinen Speicherweg hier)",
   not hasattr(rv, "import_store"))

print("=== C. fields ===")
fs = {f["name"]: f for f in rv.fields(HILFE, None, {"title": "T", "system": "a\r\nb", "ceiling": "red",
                                                   "aid": "x" * 500})}
ok("formbare Werte aus der Datei, Zeilen als LF", fs["title"]["value"] == "T" and fs["system"]["value"] == "a\nb")
ok("… ein nicht formbarer Wert (ceiling) nicht", fs["ceiling"]["value"] != "red")
ok("… gekürzt auf die Länge eines Werts", len(fs["aid"]["value"]) == rv.VALUE_MAX)
fs = {f["name"]: f for f in rv.fields(HILFE, {"params": {"title": "aus der Vorschau"}}, {"title": "T"})}
ok("eine Vorschau gewinnt", fs["title"]["value"] == "aus der Vorschau")

print("=== D. Seiten ===")
import app as portal                                          # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-assist-import-page-")
VF = os.path.join(TMP, "recipe-view.json")
portal.RECIPE_VIEW_FILE = VF
json.dump({"schema": "0.2", **VIEW}, open(VF, "w", encoding="utf-8"), ensure_ascii=False)
portal.BUILD_VIEW_FILE = os.path.join(TMP, "build-view.json")
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []
mg.CTX["queue"] = lambda rid, name, payload, wait: QUEUED.append(dict(payload, _rid=rid))
TENANTS = {"t-kunde": {"label": "kunde"}, "t-pxx": {"label": "pxx"}, "t-default": {"label": "node"}}
portal.load_tenants = lambda: TENANTS
WHO = {"role": "tenant_admin", "roles": {"tenant_admin"}, "tenant": "t-kunde", "host": "t-kunde"}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "kv", "X-OAAP-Roles": "tenant_admin"}
HO = dict(H, Origin="http://localhost")


def upload(doc, headers=HO):
    data = {"datei": (io.BytesIO(json.dumps(doc, ensure_ascii=False).encode("utf-8")), "a.json")}
    return c.post("/assistenten/aus-datei", headers=headers, data=data,
                  content_type="multipart/form-data")


t = c.get("/assistenten", headers=H).get_data(as_text=True)
ok("die Liste hat „Aus Datei“ und den Hinweis auf die Übernahme im Chat",
   'action="/assistenten/aus-datei"' in t and "Ins MCP-Gateway übernehmen" in t)
r = upload(FILE)
t = r.get_data(as_text=True)
ok("die Datei wird gelesen: Hilfe B mit „Vorlage öffnen“, die anderen mit Satz",
   r.status_code == 200 and 'action="/assistenten/neu/hilfe-assistent"' in t
   and "Vorlage öffnen" in t and "Sicht „crm“" in t and "gibt es auf diesem Knoten nicht" in t, t[-2000:])
ok("… die Anweisungen reisen mehrzeilig im Formular", "Zeile eins\nZeile zwei" in t)
ok("nichts im Spool", not QUEUED)
r = c.post("/assistenten/neu/hilfe-assistent", headers=HO,
           data={"aid": "hilfe-b", "title": "Hilfe B", "system": SYSTEM, "model": "standard"})
t = r.get_data(as_text=True)
ok("„Vorlage öffnen“: der Assistent vorausgefüllt (Titel, Kennung, Anweisungen)",
   r.status_code == 200 and 'value="Hilfe B"' in t and 'value="hilfe-b"' in t and "Zeile drei" in t, t[-2500:])
ok("… und noch immer nichts im Spool", not QUEUED)
ok("fremde Herkunft: 403 (Datei und Vorlage)",
   upload(FILE, dict(H, Origin="http://boese.example")).status_code == 403
   and c.post("/assistenten/neu/hilfe-assistent", headers=dict(H, Origin="http://boese.example"),
              data={"title": "x"}).status_code == 403)
r = upload({"v": 2})
ok("keine Austauschdatei: ein Satz", r.status_code == 200 and "keine Austauschdatei" in r.get_data(as_text=True))
r = c.post("/assistenten/aus-datei", headers=HO, data={}, content_type="multipart/form-data")
ok("ohne Datei: ein Satz", "Keine Datei" in r.get_data(as_text=True))
WHO.update(role="tenant_admin", roles={"tenant_admin"}, tenant="t-pxx", host="t-kunde")
ok("ein fremder tenant_admin: 403", upload(FILE).status_code == 403)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
