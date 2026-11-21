#!/usr/bin/env python3
"""Rezepte für einen bestehenden Mandanten (RFC-0109, Stufen 1 und 2).

Die Sätze, die diese Datei verteidigt:

    Ein Rezept legt nie einen Mandanten an und formt ihn nicht; es
    installiert eine App nur über `needs` mit install: true.
    Es arbeitet nur mit den Instanzen DIESES Mandanten (S2): eine fremde
    Instanz lässt sich nicht nennen, auch nicht über ihren Schlüssel.
    Hat der Mandant genau eine Instanz einer App, ist sie gewählt.
    Vor dem Lauf steht die Vorschau; ein Konflikt hält den Lauf an, bevor
    etwas geschieht, und es entsteht kein Lauf.
    Ein zweiter Lauf findet alles vorhanden und legt nichts an.
    Rückgängig nimmt nur, was DIESER Lauf anlegte.
    Was ein tenant_admin darf, wird an den ausgefüllten Schritten geprüft (S5).
    Das Protokoll sagt tenant.recipe.*, nicht tenant.build.*.
    Stufe 2: Ein Wert wird aus der Liste der App gewählt (setup_item), die
    Ampel folgt ihm (light) und wird nie strenger als das Modell erlaubt.
    Ein tenant_admin führt nur aus, was der Betreiber für SEINEN Mandanten
    in dieser Hauptversion freigegeben hat, und nur im eigenen Mandanten;
    ein Schritt außerhalb seines Bereichs lehnt das ganze Rezept ab.

Aufruf: python3 test/test_tenant_recipe.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-recipe-")
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
        print(f"      {str(detail)[:600]}")


DOCS = os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "rezepte",
                    "hilfe-assistent.json")
with open(DOCS, encoding="utf-8") as f:
    HILFE = json.load(f)


def refused(doc, word):
    probs = tr.recipe_problems(doc)
    return any(word in p for p in probs), probs


print("Das Rezept selbst")
ok("hilfe-assistent aus oaap-docs ist ein gültiges Rezept",
   tr.recipe_problems(HILFE) == [], tr.recipe_problems(HILFE))
d = copy.deepcopy(HILFE)
d["steps"].insert(0, {"id": "neu", "type": "tenant.create", "label": "{tenant}"})
r, p = refused(d, "tenant.create")
ok("tenant.create ist kein Schritt eines Rezepts", r, p)
d = copy.deepcopy(HILFE)
d["steps"].append({"id": "realm", "type": "idp.provision", "connector": "auth"})
r, p = refused(d, "idp.provision")
ok("idp.provision auch nicht", r, p)
d = copy.deepcopy(HILFE)
d["steps"].append({"id": "inst", "type": "app.install", "app": "help", "name": "x"})
r, p = refused(d, "needs")
ok("app.install als Schritt: nein, nur über needs", r, p)
d = copy.deepcopy(HILFE)
d["params"]["help_key"] = {"kind": "text"}
r, p = refused(d, "help_key")
ok("ein Parameter darf nicht wie der Schlüssel eines Bedarfs heißen", r, p)
d = copy.deepcopy(HILFE)
d["params"]["tenant"] = {"kind": "word"}
r, p = refused(d, "tenant")
ok("'tenant' setzt der Knoten", r, p)
d = copy.deepcopy(HILFE)
d["needs"]["x_key"] = {"app": "help"}
r, p = refused(d, "x_key")
ok("ein Bedarf endet nicht auf _key", r, p)
d = copy.deepcopy(HILFE)
d["skript"] = "rm -rf /"
r, p = refused(d, "skript")
ok("ein unbekanntes Feld wird abgelehnt", r, p)
d = copy.deepcopy(HILFE)
d["steps"][2]["items"][0]["token"] = "abc"
r, p = refused(d, "secret")
ok("ein Geheimnis in einem Einrichtungseintrag wird abgelehnt", r, p)
d = copy.deepcopy(HILFE)
d["steps"][0]["instance"] = "{unbekannt}"
r, p = refused(d, "unbekannt")
ok("ein nicht erklärter Platzhalter wird abgelehnt", r, p)
d = copy.deepcopy(HILFE)
d["needs"]["help"]["name"] = "hilfe"
r, p = refused(d, "install")
ok("'name' eines Bedarfs nur mit install: true", r, p)

d = copy.deepcopy(HILFE)
d["needs"]["help"].update(install=True, name="hilfe")
prof = tr.as_profile(d)
ok("install: true wird ein app.install vor den Schritten",
   prof["steps"][0] == {"id": "install-help", "type": "app.install",
                        "app": "help", "name": "{help}"}, prof["steps"][0])
ok("…und das Rezept bleibt gültig", tr.recipe_problems(d) == [], tr.recipe_problems(d))

print("")
print("Die Instanzen des Mandanten (D2, D3, S2)")
MINE = [("kunde-hilfe", "hilfe", "help"), ("kunde-werkzeuge", "werkzeuge", "mcp-gateway"),
        ("kunde-modelle", "modelle", "ai-gateway"), ("kunde-chat", "chat", "chat")]
pk = lambda loc: "kunde-" + loc                                # noqa: E731
vals, probs, chosen = tr.resolve_needs(HILFE, MINE, {}, pk)
ok("je eine Instanz: vorgewählt, Name und Schlüssel",
   not probs and vals["help"] == "hilfe" and vals["help_key"] == "kunde-hilfe"
   and vals["chat_key"] == "kunde-chat", (vals, probs))
two = MINE + [("kunde-chat2", "chat2", "chat")]
vals, probs, _c = tr.resolve_needs(HILFE, two, {}, pk)
ok("zwei Chats: der Mensch muss wählen", any("mehrere" in p for p in probs), probs)
vals, probs, _c = tr.resolve_needs(HILFE, two, {"chat": "chat2"}, pk)
ok("gewählt über den Namen", not probs and vals["chat_key"] == "kunde-chat2", (vals, probs))
vals, probs, _c = tr.resolve_needs(HILFE, two, {"chat": "kunde-chat2"}, pk)
ok("…oder über den Schlüssel", not probs and vals["chat"] == "chat2", (vals, probs))
vals, probs, _c = tr.resolve_needs(HILFE, MINE, {"chat": "pxx-chat"}, pk)
ok("eine Instanz eines anderen Mandanten lässt sich nicht nennen (S2)",
   any("keine Instanz" in p for p in probs), probs)
vals, probs, _c = tr.resolve_needs(HILFE, MINE, {"chat": "werkzeuge"}, pk)
ok("eine Instanz einer anderen App auch nicht", any("keine Instanz" in p for p in probs), probs)
vals, probs, _c = tr.resolve_needs(HILFE, MINE[1:], {}, pk)
ok("fehlt die App: Halt, nicht installieren (D3)",
   any("keine App help" in p for p in probs), probs)
d = copy.deepcopy(HILFE)
d["needs"]["help"].update(install=True, name="hilfe")
vals, probs, chosen = tr.resolve_needs(d, MINE[1:], {}, pk)
ok("mit install: true: geplanter Name und Schlüssel",
   not probs and vals["help"] == "hilfe" and vals["help_key"] == "kunde-hilfe"
   and chosen["help"]["install"], (vals, probs))
d["needs"]["help"]["name"] = "chat"
vals, probs, _c = tr.resolve_needs(d, MINE[1:], {}, pk)
ok("…aber nicht unter dem Namen einer anderen App", any("andere App" in p for p in probs), probs)
vals, probs, _c = tr.resolve_needs(HILFE, MINE, {"vertipper": "x"}, pk)
ok("ein vertippter Bedarf wird genannt", any("vertipper" in p for p in probs), probs)

print("")
print("Was ein tenant_admin selbst darf (§5, S5)")
plan_ok = [("a", "app.setup", {}), ("b", "manual", {}), ("c", "destination.ensure",
                                                       {"auth": "none"}),
           ("d", "app.config", {"name": "chat", "key": "CHAT_DEFAULT_LIGHT", "value": "yellow"})]
ok("Einrichtungstür, manuell, Destination ohne Zugangsdaten, offener Wert: erlaubt",
   tr.tenant_admin_scope_problems(plan_ok) == [], tr.tenant_admin_scope_problems(plan_ok))
bad = tr.tenant_admin_scope_problems([("x", "destination.ensure", {"auth": "basic"}),
                                      ("y", "app.config", {"key": "AIGW_MASTER_KEY",
                                                           "generate": True}),
                                      ("z", "authz.setup", {})])
ok("Zugangsdaten, erzeugter Schlüssel, fremder Schritt: je abgelehnt", len(bad) == 3, bad)
ok("die Frage an das Manifest gewinnt über den Namen",
   tr.tenant_admin_scope_problems([("d", "app.config", {"name": "chat", "key": "MODE",
                                                         "value": "x"})],
                                  config_is_secret=lambda n, k: True) != [])
# S5: the check runs on the FILLED steps -- a parameter cannot smuggle auth
d = copy.deepcopy(HILFE)
d["params"]["art"] = {"kind": "word", "default": "none"}
d["steps"][0]["auth"] = "{art}"
prof = tr.as_profile(d)
vals, _p, _c = tr.resolve_needs(d, MINE, {}, pk)
values, probs = tb.param_values(prof, dict(vals, label="kunde", tenant="kunde",
                                           art="bearer"))
ok("über einen Parameter gesetzte Zugangsdaten fallen in der ausgefüllten Fassung auf",
   not probs and tr.tenant_admin_scope_problems(tb.plan(prof, values)) != [], probs)

print("")
print("Vorschau, Lauf, zweiter Lauf, Rückgängig -- am Knoten")


class Node:
    """Fake drivers: what is on the node is a set; a conflict is marked."""

    def __init__(self):
        self.there, self.conflict, self.events = set(), set(), []

    @staticmethod
    def key(typ, a):
        if typ == "app.setup":
            return (typ, a["name"], tuple((i["kind"], i["id"]) for i in a["items"]))
        return (typ, json.dumps(a, sort_keys=True))

    def check(self, typ, a, ctx):
        k = self.key(typ, a)
        if k in self.conflict:
            return tb.CONFLICT, "ein Mensch hat das anders angelegt"
        return (tb.PRESENT, "da") if k in self.there else (tb.ABSENT, "fehlt")

    def do(self, typ, a, ctx):
        k = self.key(typ, a)
        self.there.add(k)
        return True, "angelegt", [f"x:{len(self.there)}:{typ}"]

    def undo(self, typ, a, made, ctx):
        self.there.discard(self.key(typ, a))
        return True, "zurück"

    def audit(self, event, step, result, detail=""):
        self.events.append(event)


REG = {"instances": {
    "kunde-hilfe": {"tenant": "t-kunde", "app_id": "help", "name": "hilfe"},
    "kunde-werkzeuge": {"tenant": "t-kunde", "app_id": "mcp-gateway", "name": "werkzeuge"},
    "kunde-modelle": {"tenant": "t-kunde", "app_id": "ai-gateway", "name": "modelle"},
    "kunde-chat": {"tenant": "t-kunde", "app_id": "chat", "name": "chat"},
    "pxx-chat": {"tenant": "t-pxx", "app_id": "chat", "name": "chat"},
}}
a.load_registry = lambda: REG
a.resolve_tenant = lambda t: t
a.is_rehearsal = lambda inst: False
TENANTS = {"t-kunde": {"label": "kunde"}, "t-pxx": {"label": "pxx"},
           "t-default": {"label": "node"}}
AUDIT = []
a.load_tenants = lambda: TENANTS
a.save_tenants = lambda t: TENANTS.update(t)
a.default_tenant_id = lambda: "t-default"
a.audit_tenant = lambda action, tenant, **kw: AUDIT.append((action, tenant, kw.get("subject")))


def _by_label(label, include_former=False):
    for tid, t in TENANTS.items():
        if t.get("label") == label:
            return tid, t
    return None, None


a.tenant_by_label = _by_label
a.instance_key = lambda tid, loc: tid[2:] + "-" + loc
os.makedirs(a.RECIPE_DIR, exist_ok=True)
shutil.copy(DOCS, a.tenant_recipe_path("hilfe-assistent"))

rows = a.tenant_recipes()
ok("die Liste nennt das Rezept mit Version", rows == [("hilfe-assistent",
                                                       "Hilfe-Assistent im Chat", HILFE["version"], "")], rows)
node = Node()
P = {"model": "chat-default", "ceiling": "red"}
pv = a.tenant_recipe_preview("hilfe-assistent", "kunde", P, "root", "root", drivers=node)
ok("Vorschau: vier Schritte, alle 'wird angelegt'",
   [r["verdict"] for r in pv["rows"]] == ["absent"] * 4, pv["rows"])
ok("Vorschau nennt die gewählten Instanzen", pv["needs"]["chat"]["key"] == "kunde-chat", pv["needs"])
ok("Vorschau ändert nichts", node.there == set() and node.events == [], node.there)
st = a.tenant_recipe_run("hilfe-assistent", "kunde", P, "anna", "root", drivers=node)
ok("Lauf: fertig, Kennung r-…", st["state"] == "done" and st["id"].startswith("r-")
   and st["kind"] == "recipe", (st["state"], st["id"]))
ok("die Freigabe nennt den Schlüssel des Chats, Modellname und Ampel",
   any(k[0] == "app.setup" and k[1] == "modelle" for k in node.there), node.there)
ok("das Protokoll sagt tenant.recipe.*", node.events and all(
   e.startswith("tenant.recipe.") for e in node.events), node.events)
pv = a.tenant_recipe_preview("hilfe-assistent", "kunde", P, "root", "root", drivers=node)
ok("zweite Vorschau: alles vorhanden", all(r["verdict"] == "present" for r in pv["rows"]),
   pv["rows"])
st2 = a.tenant_recipe_run("hilfe-assistent", "kunde", P, "anna", "root", drivers=node)
ok("zweiter Lauf: fertig und legt nichts an",
   st2["state"] == "done" and not any(r["made"] for r in st2["steps"]), st2["steps"])
ok("die Läufe stehen in ihrem eigenen Ordner, nicht bei den Aufbauten",
   len(tb.list_states(a.RECIPE_RUN_DIR)) == 2 and tb.list_states(a.BUILD_DIR) == [])
before = set(node.there)
st3 = a.tenant_recipe_rollback(st2["id"], "anna", "root", drivers=node)
ok("Rückgängig des zweiten Laufs nimmt nichts weg (er legte nichts an)",
   st3["state"] == "rolled-back" and node.there == before, node.there)
st4 = a.tenant_recipe_rollback(st["id"], "anna", "root", drivers=node)
ok("Rückgängig des ersten Laufs nimmt seine vier Schritte zurück",
   st4["state"] == "rolled-back" and node.there == set(), node.there)

print("")
print("Was einen Lauf verhindert, bevor er etwas tut")
node2 = Node()
node2.conflict.add(Node.key("app.setup", {"name": "modelle", "items": [
    {"kind": "client", "id": "kunde-chat"}]}))
n_before = len(tb.list_states(a.RECIPE_RUN_DIR))
try:
    a.tenant_recipe_run("hilfe-assistent", "kunde", P, "anna", "root", drivers=node2)
    ok("ein Konflikt in der Vorschau hält an", False)
except tb.Refusal as exc:
    ok("ein Konflikt in der Vorschau hält an, mit dem Schritt", "freigabe" in str(exc), exc)
ok("…und es entsteht kein Lauf, nichts wurde angelegt",
   len(tb.list_states(a.RECIPE_RUN_DIR)) == n_before and node2.there == set())


def refusal(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except tb.Refusal as exc:
        return str(exc)
    return ""


why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "kunde",
              dict(P, chat="pxx-chat"), "root", "root", drivers=Node())
ok("die Instanz eines anderen Mandanten wird abgelehnt (S2)", "keine Instanz" in why, why)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "neu", P, "root", "root",
              drivers=Node())
ok("ein Mandant, den es nicht gibt: ein Profil baut ihn", "profile" in why, why)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "pxx", P, "root", "root",
              drivers=Node())
ok("pxx hat nur einen Chat: die übrigen Apps fehlen, Halt", "keine App" in why, why)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "kunde",
              dict(P, chat_key="pxx-chat"), "root", "root", drivers=Node())
ok("den Schlüssel eines Bedarfs setzt der Knoten, nicht der Aufruf", "set by the node" in why,
   why)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "kunde", P, "kv", "tenant_admin",
              drivers=Node(), actor_tid="t-kunde")
ok("ein tenant_admin: ohne Freigabe kein Rezept, mit dem Weg zum Betreiber",
   "not released" in why and "operator" in why, why)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "kunde", P, "kv", "keyuser",
              drivers=Node())
ok("eine andere Rolle: abgelehnt", "server_admin" in why, why)
why = refusal(a.tenant_recipe_preview, "../profile", "kunde", P, "root", "root",
              drivers=Node())
ok("ein Pfad ist kein Rezeptname", "not a recipe name" in why, why)

print("")
print("Ein offener Lauf hält den nächsten im selben Mandanten an")
node3 = Node()
d = copy.deepcopy(HILFE)
d["id"] = "mit-warten"
d["steps"].append({"id": "pruefen", "type": "manual", "text": "Bitte prüfen.",
                   "done_when": "confirmed"})
with open(a.tenant_recipe_path("mit-warten"), "w", encoding="utf-8") as f:
    json.dump(d, f)
st = a.tenant_recipe_run("mit-warten", "kunde", P, "anna", "root", drivers=node3)
ok("ein manueller Schritt lässt den Lauf warten", st["state"] == "waiting", st["state"])
why = refusal(a.tenant_recipe_run, "hilfe-assistent", "kunde", P, "anna", "root",
              drivers=node3)
ok("der nächste Lauf wird mit dem offenen genannt", st["id"] in why, why)

print("")
print("Stufe 2: Werte aus der App (§4)")
ALIASES = [{"id": "chat-default", "title": "openrouter:x", "light": "red", "light_min": "red"},
           {"id": "lokal", "title": "ollama:y", "light": "green", "light_min": "green"},
           {"id": "misch", "title": "a, b", "light": "red", "light_min": "yellow"}]
v, probs, ch = tr.judge_choices(HILFE, {}, {"model": ALIASES})
ok("nichts gewählt, der Vorgabename fehlt in der Liste: bitte wählen",
   any("bitte wählen" in x for x in probs) and "model" not in v
   and [i["id"] for i in ch["model"]["items"]] == ["chat-default", "lokal", "misch"], (probs, ch))
v, probs, ch = tr.judge_choices(HILFE, {"model": "chat-default"}, {"model": ALIASES})
ok("gewählt: die Ampel folgt dem Modell (rot)",
   not probs and v["ceiling"] == "red" and ch["ceiling"]["suggested"] == "red", (probs, v))
v, probs, ch = tr.judge_choices(HILFE, {"model": "chat-default", "ceiling": "yellow"},
                                {"model": ALIASES})
ok("strenger als das Modell (gelb bei rot): abgelehnt, der Chat könnte es nie benutzen",
   any("nie benutzen" in x for x in probs), probs)
v, probs, ch = tr.judge_choices(HILFE, {"model": "lokal", "ceiling": "red"}, {"model": ALIASES})
ok("weiter als nötig (rot bei grün) darf die Person wählen", not probs and v["ceiling"] == "red",
   probs)
v, probs, ch = tr.judge_choices(HILFE, {"model": "misch", "ceiling": "yellow"}, {"model": ALIASES})
ok("Ausweichgruppe gelb..rot: gelb reicht (light_min), vorgeschlagen rot",
   not probs and ch["ceiling"]["suggested"] == "red", probs)
v, probs, ch = tr.judge_choices(HILFE, {"model": "gibts-nicht"}, {"model": ALIASES})
ok("ein Name, den die App nicht hat: abgelehnt, mit der Liste",
   any("gibt es bei models nicht" in x and "chat-default" in x for x in probs), probs)
v, probs, ch = tr.judge_choices(HILFE, {"model": "lokal", "ceiling": "lila"}, {"model": ALIASES})
ok("eine Ampel, die keine ist: abgelehnt", any("green, yellow oder red" in x for x in probs), probs)
v, probs, ch = tr.judge_choices(HILFE, {}, {"model": "die App nennt keine Liste"})
ok("ältere App ohne Liste: Vorgabe gilt wie in Stufe 1, die Wahl sagt warum",
   not probs and v["model"] == "standard" and v["ceiling"] == "yellow"
   and "keine Liste" in ch["model"]["note"], (probs, v, ch))
v, probs, ch = tr.judge_choices(HILFE, {}, {"model": [ALIASES[1]]})
ok("genau ein Eintrag: er ist gewählt", not probs and v["model"] == "lokal"
   and v["ceiling"] == "green", (probs, v))
d = copy.deepcopy(HILFE)
d["params"]["model"]["need"] = "gibts"
ok("setup_item muss einen Bedarf des Rezepts nennen", refused(d, "'need' names")[0])
d = copy.deepcopy(HILFE)
d["params"]["ceiling"]["from"] = "ceiling"
ok("light from muss ein setup_item nennen", refused(d, "'from' names")[0])
d = copy.deepcopy(HILFE)
d["params"]["x"] = {"kind": "text", "need": "help"}
ok("need/item_kind/from gehören nur zu setup_item und light", refused(d, "belong")[0])
ok("die Engine sieht einen Text und ein Wort", tr.as_profile(HILFE)["params"]["model"]["kind"] == "text"
   and tr.as_profile(HILFE)["params"]["ceiling"]["kind"] == "word"
   and "need" not in tr.as_profile(HILFE)["params"]["model"])

LISTS = {"kunde-modelle": ALIASES}
READ = lambda key, kind: LISTS.get(key) if kind == "alias" else "?"  # noqa: E731
pv = a.tenant_recipe_preview("hilfe-assistent", "kunde", {}, "root", "root",
                             drivers=Node(), read_list=READ)
ok("Vorschau ohne Wahl: keine Zeilen, die Liste und 'bitte wählen'",
   pv["rows"] == [] and pv["problems"] and pv["choices"]["model"]["items"], pv)
pv = a.tenant_recipe_preview("hilfe-assistent", "kunde", {"model": "chat-default"}, "root",
                             "root", drivers=Node(), read_list=READ)
ok("Vorschau mit Wahl: vier Zeilen, die Ampel rot vom Modell, Werte ohne Instanzen",
   len(pv["rows"]) == 4 and pv["params"]["ceiling"] == "red" and "chat" not in pv["params"]
   and "chat_key" not in pv["params"], pv["params"])
why = refusal(a.tenant_recipe_run, "hilfe-assistent", "kunde", {"model": "nix"}, "anna", "root",
              drivers=Node(), read_list=READ)
ok("ein Lauf mit einem Namen, den die App nicht hat: abgelehnt, kein Lauf",
   "gibt es bei models nicht" in why, why)

print("")
print("Stufe 2: Freigabe durch den Betreiber (§5)")
_open = tr.open_run_for(a.RECIPE_RUN_DIR, "kunde")
if _open:
    a.tenant_recipe_rollback(_open["id"], "root", "root", drivers=node3)
ok("Freigabe-Wort: Kennung und Hauptversion", tr.release_word("hilfe-assistent", "1.1.0")
   == "recipe:hilfe-assistent@1" and tr.released(["recipe:hilfe-assistent@1"], "hilfe-assistent", "1.9.0")
   and not tr.released(["recipe:hilfe-assistent@1"], "hilfe-assistent", "2.0.0"))
ok, why2 = a.tenant_recipe_release("t-kunde", "recipe:hilfe-assistent", True, "kv", "tenant_admin")
ok_ = ok
del ok


def ok(label, cond, detail=""):  # noqa: F811 -- the checker again
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


ok("ein tenant_admin gibt sich nichts selbst frei", ok_ is False and "server_admin" in why2, why2)
r = a.tenant_recipe_release("t-default", "recipe:hilfe-assistent", True, "op", "server_admin")
ok("für den Mandanten des Knotens gibt es nichts freizugeben", r[0] is False, r)
r = a.tenant_recipe_release("t-kunde", "recipe:hilfe-assistent", True, "op", "server_admin")
ok("der Betreiber gibt frei: Hauptversion der Datei, ins Protokoll",
   r[0] and TENANTS["t-kunde"]["tenant_admin_may"] == ["recipe:hilfe-assistent@1"]
   and ("tenant.recipe.release", "t-kunde", "recipe:hilfe-assistent@1") in AUDIT, (r, AUDIT))
r = a.tenant_set_admin_may("t-kunde", "recipe:hilfe-assistent", True, "op", "server_admin")
ok("über den Schalter (oaap tenant allow) derselbe Weg, unverändert", r[0] and "unchanged" in r[1], r)
node5 = Node()
pv = a.tenant_recipe_preview("hilfe-assistent", "kunde", {"model": "chat-default"}, "kv",
                             "tenant_admin", drivers=node5, actor_tid="t-kunde", read_list=READ)
ok("freigegeben: der tenant_admin sieht die Vorschau im eigenen Mandanten",
   len(pv["rows"]) == 4, pv)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "kunde", {"model": "chat-default"},
              "px", "tenant_admin", drivers=Node(), actor_tid="t-pxx", read_list=READ)
ok("S2: der tenant_admin eines anderen Mandanten nennt 'kunde': abgelehnt",
   "own tenant" in why, why)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "kunde",
              {"model": "chat-default", "chat": "pxx-chat"}, "kv", "tenant_admin",
              drivers=Node(), actor_tid="t-kunde", read_list=READ)
ok("S2: auch freigegeben lässt sich keine fremde Instanz nennen", "keine Instanz" in why, why)
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "pxx", {}, "px", "tenant_admin",
              drivers=Node(), actor_tid="t-pxx", read_list=READ)
ok("in pxx nicht freigegeben: abgelehnt", "not released for 'pxx'" in why, why)
st = a.tenant_recipe_run("hilfe-assistent", "kunde", {"model": "chat-default"}, "kv",
                         "tenant_admin", drivers=node5, actor_tid="t-kunde", read_list=READ)
ok("der tenant_admin führt aus: fertig, der Lauf nennt ihn",
   st["state"] == "done" and st["by"] == "kv", (st["state"], st.get("by")))
why = refusal(a.tenant_recipe_rollback, st["id"], "px", "tenant_admin", drivers=node5,
              actor_tid="t-pxx")
ok("Rückgängig durch den tenant_admin eines anderen Mandanten: abgelehnt", "own tenant" in why, why)
a.tenant_recipe_release("t-kunde", "recipe:hilfe-assistent", False, "op", "server_admin")
ok("Freigabe zurückgenommen: aus der Liste, ins Protokoll",
   TENANTS["t-kunde"]["tenant_admin_may"] == []
   and ("tenant.recipe.unrelease", "t-kunde", "recipe:hilfe-assistent@1") in AUDIT, AUDIT[-2:])
st2 = a.tenant_recipe_rollback(st["id"], "kv", "tenant_admin", drivers=node5, actor_tid="t-kunde")
ok("...der eigene Lauf lässt sich trotzdem zurücknehmen", st2["state"] == "rolled-back"
   and node5.there == set(), st2["state"])
why = refusal(a.tenant_recipe_preview, "hilfe-assistent", "kunde", {"model": "chat-default"},
              "kv", "tenant_admin", drivers=Node(), actor_tid="t-kunde", read_list=READ)
ok("...aber kein neuer Lauf", "not released" in why, why)

print("")
print("Stufe 2: der Bereich des tenant_admin an den ausgefüllten Schritten (S5)")
d = copy.deepcopy(HILFE)
d["id"] = "mit-zugang"
d["version"] = "1.0.0"
d["params"]["art"] = {"kind": "word", "default": "none"}
d["steps"][0]["auth"] = "{art}"
with open(a.tenant_recipe_path("mit-zugang"), "w", encoding="utf-8") as f:
    json.dump(d, f)
a.tenant_recipe_release("t-kunde", "recipe:mit-zugang", True, "op", "server_admin")
pv = a.tenant_recipe_preview("mit-zugang", "kunde", {"model": "chat-default"}, "kv",
                             "tenant_admin", drivers=Node(), actor_tid="t-kunde", read_list=READ)
ok("freigegeben, auth none: im Bereich", len(pv["rows"]) == 4, pv.get("problems"))
why = refusal(a.tenant_recipe_preview, "mit-zugang", "kunde",
              {"model": "chat-default", "art": "basic"}, "kv", "tenant_admin", drivers=Node(),
              actor_tid="t-kunde", read_list=READ)
ok("S5: ein Wert, der den Schritt erweitert (auth basic), lehnt das Rezept vor der Vorschau ab",
   "may not run alone" in why and "docs-origin" in why, why)
ok("...der Betreiber darf dasselbe", len(a.tenant_recipe_preview(
    "mit-zugang", "kunde", {"model": "chat-default", "art": "basic"}, "op", "server_admin",
    drivers=Node(), read_list=READ)["rows"]) == 4)
d2 = copy.deepcopy(HILFE)
d2["id"] = "mit-geheimnis"
d2["steps"].insert(0, {"id": "schluessel", "type": "app.config", "name": "{chat}",
                       "key": "CHAT_API_KEY", "value": "x"})
with open(a.tenant_recipe_path("mit-geheimnis"), "w", encoding="utf-8") as f:
    json.dump(d2, f)
a.tenant_recipe_release("t-kunde", "recipe:mit-geheimnis", True, "op", "server_admin")
why = refusal(a.tenant_recipe_preview, "mit-geheimnis", "kunde", {"model": "chat-default"},
              "kv", "tenant_admin", drivers=Node(), actor_tid="t-kunde", read_list=READ)
ok("S5: ein Konfigurationswert, den das Manifest nicht als offen erklärt: abgelehnt",
   "schluessel" in why, why)
d3 = copy.deepcopy(HILFE)
d3["id"] = "neue-fassung"
d3["version"] = "2.0.0"
with open(a.tenant_recipe_path("neue-fassung"), "w", encoding="utf-8") as f:
    json.dump(d3, f)
TENANTS["t-kunde"]["tenant_admin_may"] = TENANTS["t-kunde"]["tenant_admin_may"] + [
    "recipe:neue-fassung@1"]
why = refusal(a.tenant_recipe_preview, "neue-fassung", "kunde", {"model": "chat-default"},
              "kv", "tenant_admin", drivers=Node(), actor_tid="t-kunde", read_list=READ)
ok("eine neue Hauptversion braucht eine neue Freigabe", "not released" in why, why)

print("")
print("Stufe 2: der Auftrag aus dem Portal und die Sicht")
ok_, msg, ex = a.tenant_recipe_job({"op": "preview", "args": {
    "recipe": "mit-zugang", "tenant": "kunde", "params": {"model": "chat-default"}}},
    "t-pxx", "tenant_admin", "px")
ok("Auftrag: Rolle und Mandant aus dem Datensatz -- der tenant_admin von pxx nennt 'kunde': nein",
   ok_ is False and "own tenant" in msg and not ex, msg)
ok_, msg, ex = a.tenant_recipe_job({"op": "release", "args": {
    "recipe": "hilfe-assistent", "tenant": "kunde"}}, "t-kunde", "tenant_admin", "kv")
ok("Auftrag: ein tenant_admin kann nicht freigeben", ok_ is False and "server_admin" in msg, msg)
ok_, msg, ex = a.tenant_recipe_job({"op": "nix", "args": {}}, "t-kunde", "tenant_admin", "kv")
ok("Auftrag: eine unbekannte Operation", ok_ is False and "unknown" in msg, msg)
ok_, msg, ex = a.tenant_recipe_job({"op": "preview", "args": {
    "recipe": "hilfe-assistent", "tenant": "kunde", "params": {"model": ["x"]}}},
    "t-default", "server_admin", "op")
ok("Auftrag: Werte sind Texte", ok_ is False and "text values" in msg, msg)
a.tenant_recipe_release("t-kunde", "recipe:hilfe-assistent", True, "op", "server_admin")
ok("die Sicht für das Portal: Rezepte, Freigaben, Mandanten, Läufe",
   a.tenant_recipe_view_write())
with open(a.RECIPE_VIEW, encoding="utf-8") as f:
    view = json.load(f)
hv = next(x for x in view["recipes"] if x["id"] == "hilfe-assistent")
ok("...hilfe-assistent ist für kunde freigegeben, Hauptversion 1",
   hv["released_for"] == ["kunde"] and hv["major"] == 1 and hv["releases"] == {"kunde": [1]}
   and "node" not in [t["label"] for t in view["tenants"]] and view["runs"], hv)
ok("...und keine Werte mit Geheimnis-Namen in der Sicht",
   not any(w in json.dumps(view["runs"]).lower() for w in ("password", "secret\"", "token\"")))

print("")
print("PASS: alles" if not fails else f"FAIL: {fails}")
sys.exit(1 if fails else 0)
