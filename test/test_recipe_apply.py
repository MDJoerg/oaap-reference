#!/usr/bin/env python3
"""recipe.apply in einem Profil (RFC-0109 D9, Stufe 3).

Die Sätze, die diese Datei verteidigt:

    Ein Profil besteht aus denselben Stücken, die ein tenant_admin später
    wiederholt: `recipe.apply` wird beim Lesen des Profils zu den Schritten
    des Rezepts, mit vorangestellter Kennung, den Instanzen des Aufbaus,
    `{tenant}` als `{label}` und den Werten aus `with` (sonst den Vorgaben).
    Danach prüft das Profil wie immer -- ein Platzhalter, den das Profil
    nicht hat, ist eine Ablehnung.
    Was fehlt (eine Instanz, ein Pflichtwert), was nicht zum Rezept gehört
    und ein Rezept, das sich selbst eine App installiert, wird abgelehnt.
    Der Aufbau merkt sich die Prüfsumme jeder Rezeptdatei; „Weiter“ nimmt
    dieselbe Fassung, auch wenn die Datei inzwischen eine neuere ist.

Aufruf: python3 test/test_recipe_apply.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-recipe-apply-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
# RFC-0109 semantics: without the templates the platform ships (RFC-0117 D4
# makes a shipped template free; test_assistant_templates.py measures that)
a.SHIPPED_RECIPE_DIR = tempfile.mkdtemp(prefix="oaap-no-shipped-")
import tenant_build as tb                                     # noqa: E402
import tenant_recipe as tr                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


RZ = os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "rezepte")
HILFE = json.load(open(os.path.join(RZ, "hilfe-assistent.json"), encoding="utf-8"))
SAP = json.load(open(os.path.join(RZ, "sap-sso.json"), encoding="utf-8"))
LOAD = {"hilfe-assistent": (HILFE, "d1"), "sap-sso": (SAP, "d2")}


def load(rid):
    if rid not in LOAD:
        raise tb.Refusal(f"there is no recipe '{rid}'")
    return LOAD[rid]


PROFILE = {
    "profile": tb.PROFILE_FORMAT, "id": "ki-platz", "version": "1.0.0",
    "params": {"label": {"kind": "label", "required": True},
               "modell": {"kind": "text", "default": "chat-default"}},
    "steps": [
        {"id": "neu", "type": "tenant.create", "label": "{label}"},
        {"id": "hilfe", "type": "app.install", "app": "help", "name": "hilfe"},
        {"id": "ha", "type": "recipe.apply", "recipe": "hilfe-assistent",
         "needs": "help=hilfe, tools=werkzeuge, models=modelle, chat=chat",
         "with": "model={modell}, ceiling=red"},
    ]}

print("=== Ausdehnen ===")
exp, used, probs = tr.expand_apply(PROFILE, load)
ids = [s["id"] for s in exp["steps"]]
ok("recipe.apply wird zu den vier Schritten des Rezepts, mit vorangestellter Kennung",
   not probs and ids == ["neu", "hilfe", "ha-docs-origin", "ha-hilfe-holen", "ha-freigabe",
                         "ha-werkzeuge"], (ids, probs))
ok("die Prüfsumme der Rezeptdatei wird genannt", used == {"hilfe-assistent": "d1"}, used)
fr = next(s for s in exp["steps"] if s["id"] == "ha-freigabe")
item = fr["items"][0]
ok("Instanzen, {tenant} als {label}, Werte aus with -- auch ein Platzhalter des Profils",
   fr["name"] == "modelle" and item["id"] == "{label}-chat" and item["aliases"] == ["{modell}"]
   and item["ceiling"] == "red" and item["tenant"] == "{label}", fr)
ok("das ausgedehnte Profil besteht die Prüfung des Profils", tb.profile_problems(exp) == [],
   tb.profile_problems(exp))
p2 = copy.deepcopy(PROFILE)
p2["steps"][2]["when"] = "assistent"
p2["params"]["assistent"] = {"kind": "bool", "default": "true"}
exp2, _u, _p = tr.expand_apply(p2, load)
ok("when des Schritts gilt für jeden Schritt des Rezepts",
   all(s.get("when") == "assistent" for s in exp2["steps"] if s["id"].startswith("ha-")))


def problems_of(**change):
    p = copy.deepcopy(PROFILE)
    p["steps"][2].update(change)
    for k, v in list(change.items()):
        if v is None:
            p["steps"][2].pop(k)
    return tr.expand_apply(p, load)[2]


pr = problems_of(needs="help=hilfe, tools=werkzeuge, models=modelle")
ok("eine fehlende Instanz: abgelehnt", any("'chat'" in x for x in pr), pr)
pr = problems_of(needs=PROFILE["steps"][2]["needs"] + ", boese=x")
ok("eine Instanz, die das Rezept nicht braucht: abgelehnt", any("boese" in x for x in pr), pr)
pr = problems_of(**{"with": "ceiling=lila, gibts=1"})
ok("ein unbekannter Wert und eine falsche Ampel: abgelehnt",
   any("gibts" in x for x in pr) and any("green, yellow or red" in x for x in pr), pr)
pr = problems_of(recipe="gibts-nicht")
ok("ein Rezept, das es nicht gibt: abgelehnt", any("no recipe" in x for x in pr), pr)
pr = problems_of(skript="x")
ok("ein fremdes Argument: abgelehnt", any("skript" in x for x in pr), pr)
inst = copy.deepcopy(HILFE)
inst["needs"]["help"].update(install=True, name="hilfe")
LOAD["mit-install"] = (inst, "d3")
pr = problems_of(recipe="mit-install")
ok("ein Rezept, das sich eine App installiert: abgelehnt (das ist der Schritt des Profils)",
   any("install" in x for x in pr), pr)
sap_prof = copy.deepcopy(PROFILE)
sap_prof["steps"][2] = {"id": "sap", "type": "recipe.apply", "recipe": "sap-sso",
                        "needs": "tools=werkzeuge, chat=chat, models=modelle",
                        "with": "mcp_path=zmcp2/bc/access-support"}
pr = tr.expand_apply(sap_prof, load)[2]
ok("ein Pflichtwert des Rezepts fehlt (sap_url): abgelehnt", any("sap_url" in x for x in pr), pr)
sap_prof["steps"][2]["with"] += ", sap_url={sap}"
sap_prof["params"]["sap"] = {"kind": "text", "required": True}
exp3, _u, pr = tr.expand_apply(sap_prof, load)
pin = next(s for s in exp3["steps"] if s["id"] == "sap-ziel")
ok("sap-sso im Profil: auch der Pin-Schritt bekommt die neue Kennung, das Profil ist gültig",
   not pr and pin["pin_step"] == "sap-zertifikat" and tb.profile_problems(exp3) == [],
   (pr, pin, tb.profile_problems(exp3)))
ok("ein Rezept enthält nie recipe.apply (kein Rezept im Rezept)",
   any("recipe.apply" in x for x in tr.recipe_problems(
       dict(HILFE, steps=HILFE["steps"] + [{"id": "x", "type": "recipe.apply",
                                            "recipe": "sap-sso"}]))))

print("")
print("=== auf dem Knoten: Profil lesen, Aufbau, Weiter ===")
os.makedirs(a.RECIPE_DIR, exist_ok=True)
os.makedirs(a.PROFILE_DIR, exist_ok=True)
shutil.copy(os.path.join(RZ, "hilfe-assistent.json"), a.tenant_recipe_path("hilfe-assistent"))
ppath = a.tenant_build_profile_path("ki-platz")
json.dump(PROFILE, open(ppath, "w", encoding="utf-8"))
doc, dg = tb.read_profile_file(ppath)
_d0, rdig, _r = tr.read_recipe_file(a.tenant_recipe_path("hilfe-assistent"))
ok("das Profil wird beim Lesen ausgedehnt und nennt die Rezeptdatei",
   [s["id"] for s in doc["steps"]][2] == "ha-docs-origin"
   and doc["_recipes"] == {"hilfe-assistent": rdig}, doc.get("_recipes"))
ok("…und die Rezeptdatei liegt unter ihrer Prüfsumme im Verlauf",
   tb.history_path(a.RECIPE_DIR, "hilfe-assistent", rdig) is not None)
newer = copy.deepcopy(HILFE)
newer["version"] = "1.2.0"
newer["steps"][0]["id"] = "doku-quelle"
json.dump(newer, open(a.tenant_recipe_path("hilfe-assistent"), "w", encoding="utf-8"))
doc2, _ = tb.read_profile_file(ppath, pins={"hilfe-assistent": rdig})
ok("mit der Prüfsumme des Aufbaus: dieselbe Fassung, obwohl die Datei neuer ist",
   [s["id"] for s in doc2["steps"]][2] == "ha-docs-origin", [s["id"] for s in doc2["steps"]])
doc3, _ = tb.read_profile_file(ppath)
ok("ohne Prüfsumme (ein neuer Aufbau): die neue Fassung",
   [s["id"] for s in doc3["steps"]][2] == "ha-doku-quelle", [s["id"] for s in doc3["steps"]])
try:
    tb.read_profile_file(ppath, pins={"hilfe-assistent": "0" * 64})
    why = ""
except tb.Refusal as exc:
    why = str(exc)
ok("eine Prüfsumme, deren Datei es nicht mehr gibt: abgelehnt, mit dem Weg (zurückrollen)",
   "roll the build back" in why, why)
a.tenant_label_error = lambda lb, what: ""
a.tenant_build_apps_refusal = lambda profile, values: ""
plan = a.tenant_build_start("ki-platz", {"label": "neukunde"}, "root", "root", dry_run=True)
ok("der Plan eines Aufbaus zeigt die Schritte des Rezepts, gefüllt",
   any(s[0] == "ha-freigabe" if isinstance(s, (list, tuple)) else s.get("id") == "ha-freigabe"
       for s in plan["plan"]), plan["plan"][:3])

print("")
print("PASS: alles" if not fails else f"FAIL: {fails}")
sys.exit(1 if fails else 0)
