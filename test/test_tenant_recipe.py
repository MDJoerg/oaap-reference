#!/usr/bin/env python3
"""Rezepte für einen bestehenden Mandanten (RFC-0109, Stufe 1).

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
a.tenant_by_label = lambda label, include_former=False: (
    {"kunde": ("t-kunde", {"label": "kunde"}), "pxx": ("t-pxx", {"label": "pxx"})}
    .get(label, (None, None)))
a.instance_key = lambda tid, loc: tid[2:] + "-" + loc
os.makedirs(a.RECIPE_DIR, exist_ok=True)
shutil.copy(DOCS, a.tenant_recipe_path("hilfe-assistent"))

rows = a.tenant_recipes()
ok("die Liste nennt das Rezept mit Version", rows == [("hilfe-assistent",
                                                       "Hilfe-Assistent im Chat", "1.0.0", "")], rows)
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
              drivers=Node())
ok("Stufe 1: ein tenant_admin führt noch kein Rezept aus", "server_admin" in why, why)
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
print("PASS: alles" if not fails else f"FAIL: {fails}")
sys.exit(1 if fails else 0)
