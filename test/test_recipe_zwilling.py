#!/usr/bin/env python3
"""Die Plattform-Vorlage „Zwilling“ (RFC-0119 3c, D16 mit Nachtrag vom 2026-11-22).

Gefunden in bec auf oaapx01: Die Vorlage 1.0.0 setzte keine Ampel für die Quelle
`twin`. Neue Quellen beginnen im MCP-Gateway rot, das Profil des Chats reichte bis gelb
-- das Gateway lehnte jede Sitzung ab. Gemessen wird:

  * die Vorlage ist ein gültiges Rezept und frei (mitgeliefert, nichts rot, nur app.setup);
  * sie fragt die Ampel der Quelle `twin`, ohne Vorgabe und ohne Vorschlag;
  * der Schritt `server_light` steht nach dem `refresh` und vor der Sicht;
  * Quelle rot: die Frage bleibt offen, kein Vorschlag (die Vorlage entscheidet nicht);
  * die Person wählt gelb: der Plan setzt gelb für `twin`;
  * ein Mensch hat die Quelle schon gelb gesetzt: das gilt, nichts zu fragen;
  * KÖDER eine Quelle `twin`, die es noch nicht gibt (kein Eintrag): die Frage bleibt offen.

Aufruf: python3 test/test_recipe_zwilling.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-zwilling-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
import tenant_recipe as tr                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


DOC = a.shipped_recipe_doc("zwilling")
print("=== die Vorlage ===")
ok("ein gültiges Rezept, eine Vorlage für einen Assistenten, Version 1.1.0",
   DOC and tr.recipe_problems(DOC) == [] and DOC["offers"] == "assistant"
   and DOC["version"] == "1.1.0", tr.recipe_problems(DOC) if DOC else "fehlt")
ok("frei für jeden tenant_admin (mitgeliefert, nichts rot, nur app.setup)",
   tr.free_problems(DOC, a.shipped_recipe_doc("zwilling")) == [],
   tr.free_problems(DOC, a.shipped_recipe_doc("zwilling")))
q = DOC["params"].get("quelle_ampel") or {}
ok("fragt die Ampel der Quelle: ein Wert ohne Vorgabe",
   q.get("kind") == "light" and "default" not in q, q)
items = DOC["steps"][0]["items"]
kinds = [(i["kind"], i.get("id")) for i in items]
ok("server_light für twin steht nach dem refresh und vor der Sicht",
   kinds[:3] == [("refresh", "twin"), ("server_light", "twin"), ("view", "zwilling")]
   and items[1]["light"] == "{quelle_ampel}", kinds)

print("=== die Ampel der Quelle beim Einrichten ===")
tenants = a.load_tenants()
tenants.update({"t-kunde": {"label": "kunde", "name": "Kunde"}})
a.save_tenants(tenants)
a._recipe_instances = lambda tid: [("kunde-werkzeuge", "werkzeuge", "mcp-gateway"),
                                   ("kunde-modelle", "modelle", "ai-gateway"),
                                   ("kunde-chat", "chat", "chat")] if tid == "t-kunde" else []
a._recipe_versions = lambda tid: {"kunde-werkzeuge": "0.44.0"} if tid == "t-kunde" else {}


def reader(light):
    def read(key, kind):
        if kind == "alias":
            return [{"id": "standard", "title": "Standard", "light": "yellow"}]
        if kind == "server_light":
            rows = [{"id": "kunde-hilfe", "title": "Hilfe", "light": "green"}]
            if light:
                rows.append({"id": "twin", "title": "Zwilling", "light": light})
            return rows
        return []
    return read


def prepare(given, light):
    return a._recipe_prepare("zwilling", "kunde", given, "tenant_admin", actor_tid="t-kunde",
                             read_list=reader(light), soft=True)


_d, _g, prof, vals, _c, choices, probs = prepare({}, "red")
ch = choices.get("quelle_ampel") or {}
ok("Quelle rot: die Frage bleibt offen, kein Vorschlag (die Vorlage entscheidet nicht)",
   any("Ampel der Quelle" in p for p in probs) and not ch.get("suggested")
   and ch.get("current") == "red", (probs, ch))
_d, _g, prof, vals, _c, choices, probs = prepare({"quelle_ampel": "yellow"}, "red")
plan = tr.filled_plan(prof, vals) if prof else []
lights = [i for _i, _t, ar in plan for i in ar.get("items") or []
          if i.get("kind") == "server_light"]
ok("die Person wählt gelb: der Plan setzt gelb für twin",
   probs == [] and lights == [{"kind": "server_light", "id": "twin", "light": "yellow"}],
   (probs, lights))
_d, _g, prof, vals, _c, choices, probs = prepare({}, "yellow")
ok("ein Mensch hat twin schon gelb gesetzt: das gilt, nichts zu fragen",
   probs == [] and vals["quelle_ampel"] == "yellow"
   and choices["quelle_ampel"].get("current") == "yellow", (probs, choices.get("quelle_ampel")))
_d, _g, prof, vals, _c, choices, probs = prepare({}, "")
ok("KÖDER die Quelle twin ist im Gateway noch nicht eingelesen: die Frage bleibt offen",
   any("Ampel der Quelle" in p for p in probs), probs)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
