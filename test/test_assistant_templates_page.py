#!/usr/bin/env python3
"""Die Seite „Assistenten“ (RFC-0117 Stufe 1) im Portal.

Gemessen wird:

  * dieselben Menschen wie Einrichten: der tenant_admin am eigenen Ort, der
    Betreiber am Ort des Mandanten; ein Mitglied 403, ein fremder
    tenant_admin 403; der Menüpunkt steht dort;
  * Schritt 1: Karten der Vorlagen mit Ampel; die freie Vorlage hat
    „Weiter“, Recherche ohne Freigabe „braucht Freigabe“ und „Freigabe
    anfragen“ (die Anfrage von Einrichten);
  * Schritte 2 und 3: was gebraucht wird (Instanzen, die Adresse des
    Suchdienstes nur dort, wo sie fehlt), Anpassen mit großen Feldern für
    Anweisungen; Sicht und Ampel stehen nicht zur Wahl;
  * Schritt 4: „Vorschau“ ist ein Auftrag `tenant-recipe` mit dem eigenen
    Mandanten, die Anweisungen mehrzeilig (CRLF des Browsers → LF), ein
    unbekanntes Feld geht nicht mit; „Anlegen“ steht erst nach einer
    bereiten Vorschau da und trägt genau ihre Werte; ein Konflikt nennt
    den Ausweg „andere Kennung“; fremde Herkunft kommt nie in den Spool;
  * eine nicht freigegebene Vorlage lässt sich auch über die Adresse nicht
    starten.

Braucht Flask; der Knoten ist ersetzt (Sicht-Datei und Spool sind Attrappen).
Aufruf: python3 test/test_assistant_templates_page.py
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


def row(rid, free, released=()):
    """A recipe row as the host writes it into recipe-view.json."""
    with open(os.path.join(HERE, "..", "platform", "recipes", rid + ".json"),
              encoding="utf-8") as f:
        d = json.load(f)
    return {"id": rid, "title": d["title"], "version": d["version"], "major": 1, "problem": "",
            "description": d["description"],
            "needs": {n: {"app": s["app"], "label": s.get("label") or n,
                          "install": s.get("install") is True} for n, s in d["needs"].items()},
            "params": d["params"], "offers": d["offers"], "card": d["card"], "free": free,
            "shipped": True, "steps": [{"id": s["id"], "type": s["type"]} for s in d["steps"]],
            "released_for": list(released), "releases": {}}


HILFE, RECH, LEER = row("hilfe-assistent", True), row("recherche", False), row("assistent-leer", True)
PLAIN = {"id": "sap-sso", "title": "SAP SSO", "version": "1.0.0", "major": 1, "problem": "",
         "needs": {}, "params": {}, "steps": [], "released_for": ["kunde"], "releases": {}}
VIEW = {"tenants": [{"label": "kunde", "name": "Kunde"}, {"label": "pxx", "name": ""}],
        "recipes": [HILFE, RECH, LEER, PLAIN], "runs": []}

print("=== die reinen Regeln ===")
cards = rv.templates_for(VIEW, "kunde", "tenant_admin")
ok("Karten: nur Vorlagen, startbare zuerst, Recherche mit Ampel rot und „installiert“",
   [(c["id"], c["usable"]) for c in cards]
   == [("hilfe-assistent", True), ("assistent-leer", True), ("recherche", False)]
   and cards[2]["light"] == "red" and cards[2]["installs"] == ["Recherche"], cards)
ok("der Betreiber am Ort darf jede", all(c["usable"] for c in rv.templates_for(VIEW, "kunde", "server_admin")))
ok("eine freie Vorlage erscheint auch auf Einrichten",
   "hilfe-assistent" in [r["id"] for r in rv.recipes_for(VIEW, "pxx", "tenant_admin")])
ok("ein Rezept ohne offers ist keine Vorlage",
   rv.find_template(VIEW, "sap-sso", "kunde", "tenant_admin") is None
   and rv.find_recipe(VIEW, "sap-sso", "kunde", "tenant_admin") is not None)
needs, asked, shaped = rv.wizard_parts(RECH)
ok("Recherche (1.2.0): nichts gefragt -- der eigene Suchdienst; geformt die fünf Werte",
   [f["name"] for f in asked] == []
   and [f["name"] for f in shaped] == ["title", "aid", "greeting", "system", "model"]
   and [f["name"] for f in needs] == ["recherche", "tools", "chat"], (asked, shaped))
_n, asked, shaped = rv.wizard_parts(HILFE)
ok("Hilfe: nichts gefragt, die Ampel nicht zum Formen",
   asked == [] and "ceiling" not in [f["name"] for f in shaped])
sp = rv.start_params(HILFE, {"system": "eins\r\nzwei\r\n", "greeting": "  ", "title": "Hilfe A",
                             "boese": "x", "ceiling": "red"})
ok("Formular: Zeilen als LF, leere weg, unbekannte weg",
   sp == {"system": "eins\nzwei", "title": "Hilfe A", "ceiling": "red"}, sp)
sp = rv.start_params(HILFE, {"greeting": "x" * 5000})
ok("eine zu lange Begrüßung geht eins zu lang zum Knoten (er sagt warum)",
   len(sp["greeting"]) == 601)

# ----------------------------------------------------------- the pages
import app as portal                                          # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-assist-page-")
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


def as_(role, tenant, host):
    WHO.update(role=role, roles={role} if role else {"user"}, tenant=tenant, host=host)


def job(op, result):
    rid = os.urandom(16).hex()
    jd = os.path.join(TMP, "jobs", rid)
    os.makedirs(jd)
    json.dump({"id": rid, "op": op, "tenant": WHO["tenant"] if WHO["role"] == "tenant_admin"
               else WHO["host"], "role": WHO["role"]}, open(os.path.join(jd, "meta.json"), "w"))
    json.dump(dict({"id": rid, "op": op}, **result), open(os.path.join(jd, "result.json"), "w"))
    return rid


print("=== wer, und wo ===")
r = c.get("/assistenten", headers=H)
t = r.get_data(as_text=True)
ok("tenant_admin von kunde: die Karten, Hilfe mit „Weiter“, Recherche mit „Freigabe anfragen“",
   r.status_code == 200 and 'href="/assistenten/neu/hilfe-assistent"' in t
   and 'href="/assistenten/neu/recherche"' not in t and "Freigabe anfragen" in t
   and 'value="recherche"' in t and "SAP SSO" not in t, t[-1500:])
ok("der Menüpunkt Assistenten steht da", 'href="/assistenten"' in t)
as_("tenant_admin", "t-pxx", "t-kunde")
codes = [c.get("/assistenten", headers=H).status_code,
         c.get("/assistenten/neu/hilfe-assistent", headers=H).status_code,
         c.post("/assistenten/neu/hilfe-assistent/vorschau", headers=H).status_code]
ok("tenant_admin von pxx am Ort von kunde: 403, nichts im Spool", codes == [403] * 3 and not QUEUED, codes)
as_("", "t-kunde", "t-kunde")
ok("ein Mitglied: 403", c.get("/assistenten", headers=H).status_code == 403)
as_("server_admin", "t-default", "t-kunde")
t = c.get("/assistenten", headers=H).get_data(as_text=True)
ok("Betreiber am Ort von kunde: auch Recherche mit „Weiter“", 'href="/assistenten/neu/recherche"' in t)

print("=== der Assistent ===")
as_("tenant_admin", "t-kunde", "t-kunde")
r = c.get("/assistenten/neu/recherche", headers=H)
ok("Recherche ohne Freigabe über die Adresse: zurück mit Hinweis, nichts im Spool",
   r.status_code == 302 and "err=" in r.headers["Location"] and not QUEUED)
r = c.post("/assistenten/neu/recherche/vorschau", headers=dict(H, Origin="http://localhost"),
           data={"search_url": "http://x"})
ok("…auch nicht als Vorschau", r.status_code == 302 and not QUEUED)
r = c.get("/assistenten/neu/hilfe-assistent", headers=H)
t = r.get_data(as_text=True)
ok("Hilfe: was gebraucht wird, Anpassen mit Textfeldern, noch kein „Anlegen“",
   r.status_code == 200 and "Was gebraucht wird" in t and '<textarea id="f-system"' in t
   and 'name="aid"' in t and 'name="ceiling"' not in t
   and 'action="/assistenten/neu/hilfe-assistent/anlegen"' not in t, t[-1500:])
r = c.post("/assistenten/neu/hilfe-assistent/vorschau", headers=dict(H, Origin="http://boese.example"),
           data={"system": "x"})
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and not QUEUED)
r = c.post("/assistenten/neu/hilfe-assistent/vorschau", headers=dict(H, Origin="http://localhost"),
           data={"title": "Hilfe Vertrieb", "aid": "hilfe-vertrieb",
                 "system": "Antworte für den Vertrieb.\r\n- kurz\r\n- mit Quelle", "boese": "x",
                 "help": ""})
q = QUEUED[-1] if QUEUED else {}
ok("Vorschau: ein Auftrag tenant-recipe, eigener Mandant, Anweisungen mehrzeilig, nichts Fremdes",
   r.status_code == 302 and "/assistenten/neu/hilfe-assistent?job=" in r.headers["Location"]
   and q.get("action") == "tenant-recipe" and q.get("op") == "preview"
   and q["args"]["tenant"] == "kunde" and q["args"]["recipe"] == "hilfe-assistent"
   and q["args"]["params"] == {"title": "Hilfe Vertrieb", "aid": "hilfe-vertrieb",
                               "system": "Antworte für den Vertrieb.\n- kurz\n- mit Quelle"}, q)

PV = {"recipe": "hilfe-assistent", "tenant": "kunde", "problems": [],
      "needs": {"help": {"app": "help", "name": "hilfe", "key": "kunde-hilfe", "install": False},
                "chat": {"app": "chat", "name": "chat", "key": "kunde-chat", "install": False}},
      "choices": {"model": {"need": "models", "item_kind": "alias", "value": "standard",
                            "items": [{"id": "standard", "title": "x", "light": "yellow"}], "note": ""}},
      "params": {"title": "Hilfe Vertrieb", "aid": "hilfe-vertrieb", "model": "standard",
                 "system": "Antworte für den Vertrieb.\n- kurz", "ceiling": "yellow"},
      "rows": [{"id": "werkzeuge", "type": "app.setup", "verdict": "absent", "note": "neu"}]}
jid = job("preview", {"ok": True, "message": "Vorschau fertig", "preview": PV})
t = c.get(f"/assistenten/neu/hilfe-assistent?job={jid}", headers=H).get_data(as_text=True)
ok("bereite Vorschau: „Anlegen“ mit genau ihren Werten, die Anweisungen bleiben im Feld",
   'action="/assistenten/neu/hilfe-assistent/anlegen"' in t
   and 'name="aid" value="hilfe-vertrieb"' in t and 'name="help" value="hilfe"' in t
   and "Antworte für den Vertrieb.\n- kurz</textarea>" in t
   and '<option value="standard" selected>' in t, t[-2500:])
blk = dict(PV, rows=[{"id": "werkzeuge", "type": "app.setup", "verdict": "conflict",
                      "note": "Den Assistenten gibt es schon (@1), anders in: system."}])
jid = job("preview", {"ok": True, "message": "Halt", "preview": blk})
t = c.get(f"/assistenten/neu/hilfe-assistent?job={jid}", headers=H).get_data(as_text=True)
ok("Konflikt: kein „Anlegen“, der Ausweg andere Kennung",
   'hilfe-assistent/anlegen"' not in t and "andere Kennung" in t)
n0 = len(QUEUED)
r = c.post("/assistenten/neu/hilfe-assistent/anlegen", headers=dict(H, Origin="http://localhost"),
           data=dict(PV["params"], help="hilfe", chat="chat"))
q = QUEUED[-1] if len(QUEUED) > n0 else {}
ok("Anlegen: ein Auftrag run, danach die Seite Assistenten mit dem Auftrag",
   r.status_code == 302 and "/assistenten?job=" in r.headers["Location"]
   and q.get("op") == "run" and q["args"]["params"]["aid"] == "hilfe-vertrieb"
   and q["args"]["params"]["help"] == "hilfe", q)
print(f"\nFAIL: {fails}" if fails else "\nalle grün")
sys.exit(1 if fails else 0)
