#!/usr/bin/env python3
"""Vorlagen für Assistenten (RFC-0117 Stufe 1): die Maschine und der Knoten.

Gemessen wird, was das RFC verspricht:

  * die drei mitgelieferten Vorlagen (Hilfe, Recherche, Leer) sind gültige
    Rezepte mit `offers: "assistant"`;
  * `prose`: mehrzeiliger Text für Begrüßung und Anweisungen -- Zeilen
    bleiben Zeilen, jedes andere Steuerzeichen und Überlänge werden
    abgelehnt, `text` bleibt einzeilig;
  * §6.3 KÖDER: ein `prose`-Wert in einer Adresse, einem Namen, dem Titel
    -- abgelehnt, nur greeting und system des Assistenten;
  * §6.4 KÖDER: die Karte sagt grün, die Schritte setzen rot -- abgelehnt;
    genau ein Assistent je Vorlage, eine Karte nur an einer Vorlage;
  * D4: frei ist nur eine mitgelieferte Vorlage, so wie sie geliefert ist,
    ohne Installation und ohne rot -- Hilfe und Leer ja, Recherche nein,
    eine veränderte Hilfe nein;
  * D7: die Datei des Betreibers gewinnt bei gleicher Version, die
    mitgelieferte bei höherer; die Liste nennt beide Ordner;
  * der Riegel: ein tenant_admin ohne Freigabe darf die freie Vorlage,
    nicht Recherche, nicht die veränderte Hilfe; die Sicht fürs Portal
    trägt offers, card und free.

Aufruf: python3 test/test_assistant_templates.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-assist-tpl-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
import tenant_build as tb                                     # noqa: E402
import tenant_recipe as tr                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


SHIPPED = os.path.join(HERE, "..", "platform", "recipes")


def load(rid):
    with open(os.path.join(SHIPPED, rid + ".json"), encoding="utf-8") as f:
        return json.load(f)


HILFE, RECH, LEER = load("hilfe-assistent"), load("recherche"), load("assistent-leer")


def refused(doc, word):
    p = tr.recipe_problems(doc)
    return any(word in x for x in p), p


print("=== die mitgelieferten Vorlagen ===")
for d in (HILFE, RECH, LEER):
    ok(f"{d['id']} {d['version']}: gültig, bietet einen Assistenten an",
       tr.recipe_problems(d) == [] and d["offers"] == "assistant"
       and len(tr.assistant_items(d)) == 1, tr.recipe_problems(d))
ok("formbar sind Name, Kennung, Begrüßung, Anweisungen, Modell -- nie Sicht oder Ampel",
   all(sorted(n for n, _s in tr.shape_params(d)) == ["aid", "greeting", "model", "system", "title"]
       for d in (HILFE, RECH, LEER)))
ok("die Kopien in oaap-docs sind dieselben Dateien",
   all(json.load(open(os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "rezepte",
                                   d["id"] + ".json"), encoding="utf-8")) == d
       for d in (HILFE, RECH, LEER)))
ok("Hilfe: Begrüßung und Anweisungen wie die Fassung 1.1.0 sie anlegte (schon da = vorhanden)",
   HILFE["params"]["system"]["default"].startswith("Du bist der Assistent „Hilfe“. Antworte knapp "
                                                   "und nutze die Werkzeuge der Sicht hilfe")
   and HILFE["version"].startswith("1."))

print("=== prose ===")
prof = tr.as_profile(HILFE)
base = {"label": "kunde", "tenant": "kunde", "help": "hilfe", "help_key": "kunde-hilfe",
        "tools": "werkzeuge", "tools_key": "kunde-werkzeuge", "models": "modelle",
        "models_key": "kunde-modelle", "chat": "chat", "chat_key": "kunde-chat",
        "model": "standard", "ceiling": "yellow"}
text = "Zeile eins\n- Punkt\tmit Tab\nZeile drei"
vals, probs = tb.param_values(prof, dict(base, system=text))
ok("mehrzeilige Anweisungen gehen durch, Zeilen bleiben Zeilen",
   not probs and vals["system"] == text, probs)
step = next(s for s in HILFE["steps"] if s["id"] == "werkzeuge")
item = next(i for i in tb.render_step(step, vals)["items"] if i["kind"] == "assistant")
ok("…und stehen so im Assistenten-Eintrag", item["system"] == text and item["id"] == "hilfe", item)
_v, probs = tb.param_values(prof, dict(base, system="a\x07b"))
ok("ein anderes Steuerzeichen: abgelehnt", any("control" in p for p in probs), probs)
_v, probs = tb.param_values(prof, dict(base, greeting="x" * 601))
ok("Begrüßung über 600 Zeichen: abgelehnt", any("longer" in p for p in probs), probs)
_v, probs = tb.param_values(prof, dict(base, title="zwei\nZeilen"))
ok("ein text bleibt einzeilig", any("control" in p for p in probs), probs)
d = copy.deepcopy(HILFE)
d["params"]["system"]["max"] = 9000
r, p = refused(d, "max")
ok("prose höchstens 8000 Zeichen", r, p)

print("=== §6.3 KÖDER: prose nur in den Text des Assistenten ===")
d = copy.deepcopy(HILFE)
d["steps"][0]["target"] = "https://{system}.example"
r, p = refused(d, "may only fill")
ok("prose in der Adresse einer Destination: abgelehnt", r, p)
d = copy.deepcopy(HILFE)
next(i for i in d["steps"][3]["items"] if i["kind"] == "assistant")["title"] = "{greeting}"
r, p = refused(d, "not 'title'")
ok("prose im Titel des Assistenten: abgelehnt", r, p)
d = copy.deepcopy(HILFE)
d["steps"][3]["items"][3]["id"] = "{system}"
r, p = refused(d, "may only fill")
ok("prose als Kennung eines Profils: abgelehnt", r, p)
d = copy.deepcopy(HILFE)
del d["offers"], d["card"]
d["steps"][0]["name"] = "{system}"
r, p = refused(d, "may only fill")
ok("auch in einem Rezept, das keine Vorlage ist", r, p)

print("=== §6.4 KÖDER: die Karte sagt die Ampel ===")
d = copy.deepcopy(RECH)
d["card"]["light"] = "green"
r, p = refused(d, "steps set red")
ok("Karte grün, Profil rot: abgelehnt", r, p)
d = copy.deepcopy(HILFE)
d["steps"][3]["items"].append(dict(d["steps"][3]["items"][-1], id="zweiter"))
r, p = refused(d, "exactly one assistant")
ok("zwei Assistenten in einer Vorlage: abgelehnt", r, p)
d = copy.deepcopy(HILFE)
del d["card"]
r, p = refused(d, "card")
ok("eine Vorlage ohne Karte: abgelehnt", r, p)
d = copy.deepcopy(HILFE)
del d["offers"]
r, p = refused(d, "'card' belongs")
ok("eine Karte ohne offers: abgelehnt", r, p)
d = copy.deepcopy(HILFE)
d["offers"] = "agent"
r, p = refused(d, "'offers'")
ok("offers kennt nur assistant", r, p)

print("=== D4: frei ohne Freigabe ===")
ok("Hilfe, wie geliefert: frei", tr.free_problems(HILFE, HILFE) == [])
ok("Leer, wie geliefert: frei", tr.free_problems(LEER, LEER) == [])
p = tr.free_problems(RECH, RECH)
ok("Recherche: nicht frei (installiert, rot, Adresse aus einem Wert)",
   len(p) == 3 and any("installs" in x for x in p) and any("red" in x for x in p), p)
d = copy.deepcopy(HILFE)
d["params"]["system"]["default"] += " Und noch etwas."
ok("eine veränderte Hilfe: nicht frei", tr.free_problems(d, HILFE)
   == ["not shipped by the platform as it is"])
ok("eine Vorlage, die die Plattform nicht liefert: nicht frei",
   tr.free_problems(HILFE, None) == ["not shipped by the platform as it is"])
d = copy.deepcopy(HILFE)
d["steps"].append({"id": "frag", "type": "manual", "text": "x"})
ok("ein Schritt außer app.setup und destination.ensure: nicht frei",
   any("manual" in x for x in tr.free_problems(d, d)), tr.free_problems(d, d))

print("=== D7: welche Datei ===")
own_dir = a.RECIPE_DIR
os.makedirs(own_dir, exist_ok=True)
ship = tempfile.mkdtemp(prefix="oaap-shipped-")
a.SHIPPED_RECIPE_DIR = ship
for d in (HILFE, RECH, LEER):
    json.dump(d, open(os.path.join(ship, d["id"] + ".json"), "w", encoding="utf-8"),
              ensure_ascii=False)
ok("nur geliefert: die gelieferte", a.tenant_recipe_path("hilfe-assistent")
   == os.path.join(ship, "hilfe-assistent.json"))
old = dict(copy.deepcopy(HILFE), version="1.1.0")
json.dump(old, open(os.path.join(own_dir, "hilfe-assistent.json"), "w", encoding="utf-8"))
ok("eine ältere Kopie des Betreibers verdeckt die gelieferte nicht",
   a.tenant_recipe_path("hilfe-assistent") == os.path.join(ship, "hilfe-assistent.json"))
same = copy.deepcopy(HILFE)
same["params"]["system"]["default"] = "Eigene Anweisung des Betreibers."
json.dump(same, open(os.path.join(own_dir, "hilfe-assistent.json"), "w", encoding="utf-8"))
ok("gleiche Version: die des Betreibers gewinnt",
   a.tenant_recipe_path("hilfe-assistent") == os.path.join(own_dir, "hilfe-assistent.json"))
json.dump(dict(same, version="2.0.0"),
          open(os.path.join(own_dir, "hilfe-assistent.json"), "w", encoding="utf-8"))
ok("höhere Version des Betreibers: seine",
   a.tenant_recipe_path("hilfe-assistent") == os.path.join(own_dir, "hilfe-assistent.json"))
ok("die Liste nennt die gelieferten und die eigenen",
   [(r[0], r[2]) for r in a.tenant_recipes()]
   == [("assistent-leer", "1.0.0"), ("hilfe-assistent", "2.0.0"), ("recherche", "1.1.0")],
   a.tenant_recipes())

print("=== der Riegel des tenant_admin ===")
T = {"label": "kunde", "tenant_admin_may": []}


def gate(doc):
    try:
        a._recipe_tenant_gate("tenant_admin", "t-kunde", "t-kunde", T, doc)
        return ""
    except tb.Refusal as exc:
        return str(exc)


ok("ohne Freigabe: die gelieferte Hilfe geht", gate(HILFE) == "", gate(HILFE))
ok("ohne Freigabe: Leer geht", gate(LEER) == "")
ok("ohne Freigabe: Recherche nicht -- mit dem Weg zum Betreiber",
   "not released" in gate(RECH), gate(RECH))
ok("ohne Freigabe: die Hilfe des Betreibers (verändert) nicht", "not released" in gate(same))
T["tenant_admin_may"] = ["recipe:recherche@1"]
ok("mit Freigabe: Recherche geht", gate(RECH) == "")
try:
    a._recipe_tenant_gate("tenant_admin", "t-pxx", "t-kunde", T, HILFE)
    other = ""
except tb.Refusal as exc:
    other = str(exc)
ok("frei heißt nicht fremd: der tenant_admin von pxx nicht in kunde", "own tenant" in other, other)

os.remove(os.path.join(own_dir, "hilfe-assistent.json"))
a.tenant_recipe_view_write()
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
rows = {r["id"]: r for r in view["recipes"]}
ok("die Sicht fürs Portal: offers, Karte, frei",
   rows["hilfe-assistent"]["offers"] == "assistant" and rows["hilfe-assistent"]["free"] is True
   and rows["assistent-leer"]["free"] is True and rows["recherche"]["free"] is False
   and rows["recherche"]["card"]["light"] == "red" and rows["hilfe-assistent"]["shipped"] is True,
   {k: (v.get("offers"), v.get("free"), v.get("shipped")) for k, v in rows.items()})
ok("…und die Vorgabe der Anweisungen steht mehrzeilig darin",
   "\n- " in rows["recherche"]["params"]["system"]["default"])

shutil.rmtree(ship, ignore_errors=True)
print(f"\nFAIL: {fails}" if fails else "\nalle grün")
sys.exit(1 if fails else 0)
