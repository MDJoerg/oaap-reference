#!/usr/bin/env python3
"""Das Asset-Universum des Mandanten (RFC-0088 Stufe 1).

A  das Modell (services/twin/assets_model.py) besteht die Prüfung, die
   `oaap assets` vor dem Anmelden der Typen macht;
B  der Mandanten-Leser (assets_twin.read_tenant_answer) nimmt nur, was die
   Whitelist nennt -- nie Schlüsselwerte, nie Personen (Stufe 1);
C  was hängt wovon ab: Quelle -> Modellname -> Schlüssel -> Agent;
D  die Seite: Reihenfolge je Art, Entferntes zuletzt, ohne `affected`;
E  der Plan des Hosts: nur Instanzen mit Tür, ein schlafende wird nicht
   gefragt, ein Mandant mit "aus" fehlt; der Schalter über den Spool.

Ohne Docker, ohne Datenbank. Aufruf: python3 test/test_assets_tenant.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import assets_model as am                                      # noqa: E402
import assets_twin as at                                       # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-assets-")
import appctl as a                                             # noqa: E402

print("=== A: Modell ===")
dm = am.data_model()
errs = a.validate_data_model_sections({"data_model": dm})
ok("Modell besteht die Prüfung der Typen", not errs, errs)
keys = [e["key"] for k in a.MODEL_KINDS for e in dm.get(k) or []]
ok("kein Schlüssel doppelt", len(keys) == len(set(keys)))
ok("Attribut- und Relationsschlüssel tragen das Präfix 'assets'",
   all(e["key"].startswith("assets") for k in ("attribute_types", "relation_types")
       for e in dm[k]))
fleet = a._fleet_model_modules()[1].data_model()
fkeys = {e["key"] for k in a.MODEL_KINDS for e in fleet.get(k) or []}
ok("keine Überschneidung mit den Typen der Flotte", not (set(keys) & fkeys),
   set(keys) & fkeys)
ok("Gruppen nur aus Kleinbuchstaben (Regel der Registry)",
   all(a._MODEL_GROUP_KEY_RE.fullmatch(g["key"]) for g in dm["group_types"]))
ok("Wertetypen bekannt", all(e["value_type"] in a.MODEL_VALUE_TYPES
                             for e in dm["attribute_types"]))

print("=== B: Whitelist des Mandanten-Lesers ===")
DOOR = {"v": 1, "assets": [
    {"kind": "source", "name": "gaming-pc", "state": "down", "destination": "gaming",
     "light": "red", "checked": {"at": "2026-11-15T16:00:00Z", "ok": False},
     "models": ["llama3", "qwen", 7], "address": "http://10.0.0.5:11434", "secret": "x"},
    {"kind": "source", "name": "anbieter", "state": "ok"},
    {"kind": "model", "name": "chat-gaming", "state": "down", "uses": ["gaming-pc", "nix"],
     "upstream": [{"source": "gaming-pc", "model": "llama3:8b"}, {"source": "", "model": "x"}],
     "prompt": "geheim"},
    {"kind": "model", "name": "chat", "state": "ok", "uses": ["gaming-pc", "anbieter"]},
    {"kind": "key", "name": "Schulung Mai", "expires": "2026-11-20T00:00:00Z", "budget": 10.5,
     "used": 9.6, "models": ["chat-gaming", "unbekannt"], "owner": "Erika Muster",
     "value": "sk-geheim", "hash": "abc"},
    {"kind": "cohort", "name": "kurs-1", "expires": "kein Datum", "seats": 12},
    {"kind": "agent", "name": "helfer", "uses": ["chat-gaming", "suche"], "acts_for": "uuid-1"},
    {"kind": "tool", "name": "suche", "source": "anbieter"},
    {"kind": "signin", "name": "drive", "source": "anbieter", "expires": "2026-12-01"},
    {"kind": "secret", "name": "nein"},
    {"kind": "model", "name": "a/b", "state": "ok"},
    {"kind": "model", "name": "chat", "state": "down"},
]}
items = at.read_tenant_answer(DOOR)
by = {(i["kind"], i["name"]): i for i in items}
ok("keine Antwort ohne v:1", at.read_tenant_answer({"assets": []}) is None
   and at.read_tenant_answer({"v": 2, "assets": []}) is None)
ok("unbekannte Art und Name mit '/' fallen weg",
   ("secret", "nein") not in by and ("model", "a/b") not in by)
ok("dasselbe (Art, Name) nur einmal, das erste gilt",
   by[("model", "chat")]["values"]["state"] == "ok"
   and sum(1 for i in items if i["kind"] == "model" and i["name"] == "chat") == 1)
flat = json.dumps(items, ensure_ascii=False)
ok("nie Schlüsselwert, Prüfsumme, Adresse, Prompt",
   not any(x in flat for x in ("sk-geheim", "\"abc\"", "10.0.0.5", "geheim\"", "prompt")), flat)
ok("Stufe 1: keine Personen (owner, acts_for)",
   "Erika" not in flat and "uuid-1" not in flat)
src = by[("source", "gaming-pc")]["values"]
ok("Quelle: Zustand, Ampel, Prüfung, Destination, Zahl der Modelle",
   src == {"name": "gaming-pc", "kind": "source", "state": "down", "light": "red",
           "checked": "2026-11-15T16:00:00Z", "checkedOk": False, "modelCount": 2,
           "destination": "gaming"}, src)
ok("Modellname: nutzt nur Quellen dieser Antwort",
   by[("model", "chat-gaming")]["rel"] == {"uses": [("source", "gaming-pc")]})
ok("Modellname: Kennung bei der Quelle, ohne unvollständige Zeilen",
   by[("model", "chat-gaming")]["values"]["upstream"] == "gaming-pc: llama3:8b")
k = by[("key", "Schulung Mai")]
ok("Schlüssel: Ende, Budget, Verbrauch; darf nur bekannte Modellnamen",
   k["values"].get("expires") == "2026-11-20T00:00:00Z" and k["values"].get("budget") == 10.5
   and k["values"].get("used") == 9.6 and k["rel"] == {"mayUse": [("model", "chat-gaming")]}, k)
ok("Kohorte: ein Ende, das kein Datum ist, fällt weg; Plätze bleiben",
   "expires" not in by[("cohort", "kurs-1")]["values"]
   and by[("cohort", "kurs-1")]["values"]["seats"] == 12)
ok("Agent arbeitet mit Modellname und Werkzeug",
   by[("agent", "helfer")]["rel"] == {"agentUses": [("model", "chat-gaming"), ("tool", "suche")]})
ok("Werkzeug gehört zu einer Quelle, Anmeldung öffnet eine",
   by[("tool", "suche")]["rel"] == {"toolOf": [("source", "anbieter")]}
   and by[("signin", "drive")]["rel"] == {"opens": [("source", "anbieter")]}
   and by[("signin", "drive")]["values"]["expires"] == "2026-12-01")
many = {"v": 1, "assets": [{"kind": "model", "name": f"m{i}"} for i in range(700)]}
ok(f"höchstens {at.ITEMS_MAX} Assets", len(at.read_tenant_answer(many)) == at.ITEMS_MAX)
ok("Quellschlüssel <instanz>/<art>/<name>",
   at.item_key("gw", "key", "Schulung Mai") == "gw/key/Schulung Mai"
   and at.kind_of("gw/key/Schulung Mai") == "key")

print("=== C: was hängt wovon ab ===")
ids = {(i["kind"], i["name"]): f"id-{n}" for n, i in enumerate(items)}
objs = {ids[(i["kind"], i["name"])]: {"kind": i["kind"], "name": i["name"], "instance": "gw"}
        for i in items}
rels = [(ids[(i["kind"], i["name"])], rk, ids[t]) for i in items
        for rk, ts in i["rel"].items() for t in ts]
aff = at.affected(rels, objs, ids[("source", "gaming-pc")])
names = [(r["kind"], r["name"], r["depth"]) for r in aff]
ok("Quelle aus: Modellnamen, dann Schlüssel und Agent",
   names == [("model", "chat", 1), ("model", "chat-gaming", 1),
             ("agent", "helfer", 2), ("key", "Schulung Mai", 2)], names)
ok("jedes Objekt einmal", len({r["id"] for r in aff}) == len(aff))
ok("Werkzeug -> Agent über die Quelle des Werkzeugs",
   [(r["kind"], r["name"]) for r in at.affected(rels, objs, ids[("source", "anbieter")])]
   == [("model", "chat"), ("signin", "drive"), ("tool", "suche"), ("agent", "helfer")])
ok("ein Schlüssel hat nichts, was von ihm abhängt",
   at.affected(rels, objs, ids[("key", "Schulung Mai")]) == [])

print("=== D: die Seite ===")
objects = {"app1": {"type": am.APP, "source": "gw", "title": "gw",
                    "values": {"app": "ai-gateway", "appState": "ok"}}}
for i in items:
    oid = ids[(i["kind"], i["name"])]
    objects[oid] = {"type": am.ITEM, "source": at.item_key("gw", i["kind"], i["name"]),
                    "title": i["name"], "values": dict(i["values"])}
objects[ids[("model", "chat")]]["values"]["state"] = am.REMOVED
u = at.universe(objects, rels)
order = [(r["kind"], r["name"]) for r in u["items"]]
ok("Reihenfolge der Arten wie im Modell", [k for k, _n in order][:2] == ["source", "source"]
   and order.index(("key", "Schulung Mai")) > order.index(("model", "chat-gaming")))
ok("Entferntes zuletzt innerhalb seiner Art, ohne 'hängt davon ab'",
   order.index(("model", "chat")) > order.index(("model", "chat-gaming"))
   and next(r for r in u["items"] if r["name"] == "chat")["affected"] == [])
ok("eine App mit ihrer Antwort", u["apps"] == [{"id": "app1", "instance": "gw",
                                                 "app": "ai-gateway", "state": "ok"}])
row = next(r for r in u["items"] if r["name"] == "chat-gaming")
ok("Zeile nennt ihre Beziehungen", row["rel"] == [("uses", "source", "gaming-pc")], row["rel"])

print("=== E: Plan des Hosts und Schalter ===")
a.load_tenants = lambda: {"t1": {"label": "default"}, "t2": {"label": "verein"},
                          "t3": {"label": "aus"}}
a.default_tenant_id = lambda: "t1"
REG = {"instances": {
    "gw": {"app_id": "ai-gateway", "assets": {"path": "/oaap/assets"}},
    "gw2": {"app": "ai-gateway", "assets": {"path": "/oaap/assets"}, "tenant": "t2",
            "state": "standby"},
    "gw3": {"app": "ai-gateway", "assets": {"path": "/oaap/assets"}, "tenant": "t3"},
    "gw4": {"app": "ai-gateway", "assets": {"path": "/oaap/assets"}, "tenant": "t2",
            "maintenance": True},
    "web": {"app": "web"}}}
plan = a.assets_plan(REG, {"tenants": {"t3": "off"}})
ok("nur Instanzen mit Tür, nach Mandant; ohne Mandant der Vorgabemandant",
   sorted(plan) == ["t1", "t2"] and list(plan["t1"]) == ["gw"], plan)
ok("schlafend oder in Wartung: nicht gefragt",
   plan["t2"]["gw2"]["ask"] is False and plan["t2"]["gw4"]["ask"] is False
   and plan["t1"]["gw"]["ask"] is True)
ok("die App heißt nach ihrer Kennung (app_id der Registry)", plan["t1"]["gw"]["app"] == "ai-gateway")
ok("D2: an, bis der Mandant ausschaltet", a.assets_on("t1", {"tenants": {}})
   and not a.assets_on("t3", {"tenants": {"t3": "off"}}))
a.audit_tenant = lambda *x, **k: None
a.tenant_by_label = lambda label, include_former=False: (
    next((t for t, r in a.load_tenants().items() if r["label"] == label), None), None)
ok_, msg = a.assets_set_job({"op": "off", "args": {"tenant": "verein"}}, "t1", "tenant_admin", "u")
ok("tenant_admin schaltet nur den eigenen Mandanten (der genannte zählt nicht)",
   ok_ and a.assets_settings_load()["tenants"] == {"t1": "off"}, msg)
ok_, msg = a.assets_set_job({"op": "off", "args": {"tenant": "verein"}}, "t1", "server_admin", "r")
ok("server_admin schaltet einen genannten",
   ok_ and a.assets_settings_load()["tenants"] == {"t1": "off", "t2": "off"}, msg)
ok_, _m = a.assets_set_job({"op": "on", "args": {}}, "t1", "tenant_admin", "u")
ok("wieder an: der Eintrag verschwindet", ok_ and "t1" not in a.assets_settings_load()["tenants"])
ok("eine andere Rolle darf nicht", not a.assets_set_job({"op": "on"}, "t1", "", "u")[0])
ok("unbekannter Schritt", not a.assets_set_job({"op": "delete"}, "t1", "server_admin", "r")[0])
ok("die Einstellung ist für das Portal lesbar (0644)",
   oct(os.stat(a.ASSETS_SETTINGS).st_mode & 0o777) == "0o644"
   or os.name == "nt")

print(f"\n{'OK' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
