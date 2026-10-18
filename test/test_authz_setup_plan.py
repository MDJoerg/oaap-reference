#!/usr/bin/env python3
"""Einrichten aus den Vorlagen: der Plan (oaap.core.authorization 0.4, 2.11).

Der Plan ist eine reine Funktion. Jede Regel des Abschnitts hat hier einen
Koeder, denn ein Plan, der zu viel verspricht, legt zu viel an, und einer, der zu
wenig verspricht, sieht von aussen aus wie ein Plan, der "schon alles da" meldet:

    - Vorlage mit $value: uebersprungen; mit $context: Rolle + Sammlung, KEINE
      Gruppe; mit may_grant: dito; sonst alles;
    - der Gruppenpfad: --group-for vor suggested_group vor nichts;
    - ein zweiter Lauf nach dem Anlegen: alles "present", nichts zu tun;
    - ein belegter Name ist ein Konflikt NUR dieser Vorlage, die anderen laufen;
    - Vorhandenes wird auch unter anderem Namen erkannt;
    - eine ausgeblendete Rolle wird nie wiederverwendet;
    - unbekannte Schluessel (--only/--skip/--group-for) und kaputte Pfade
      schreiben nichts;
    - ein anderer Mandant sieht seine Dinge nicht als "vorhanden".

Braucht weder Docker noch Flask.

Aufruf: python3 test/test_authz_setup_plan.py
"""
import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import authorization as az                                     # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def refuses(label, fn, needle=""):
    try:
        fn()
    except az.AuthzError as e:
        ok(label, needle in str(e), str(e))
        return
    ok(label, False, "no refusal")


DECL = {
    "objects": [
        {"key": "team", "title": "Mannschaft", "activities": ["coach", "assist"],
         "fields": [{"key": "team", "context": "Mannschaft"}]},
        {"key": "venue", "title": "Halle", "activities": ["announce"]},
        {"key": "news", "title": "Meldungen", "activities": ["publish"],
         "fields": [{"key": "area", "values": ["verein", "jugend"]}]},
    ],
    "role_templates": [
        {"key": "announcer", "title": "Hallensprecher/in",
         "suggested_group": "Verein/Hallensprecher",
         "grants": [{"object": "venue", "activities": ["announce"]}]},
        {"key": "official", "title": "Betreuer/in",
         "grants": [{"object": "venue", "activities": ["announce"]}]},
        {"key": "trainer", "title": "Trainer/in",
         "suggested_group": "Verein/Trainer",
         "grants": [{"object": "team", "activities": ["coach"],
                     "team": "$context"}]},
        {"key": "bereich", "title": "Bereich", "suggested_group": "Verein/Bereich",
         "grants": [{"object": "news", "activities": ["publish"],
                     "area": "$value"}]},
        {"key": "fest", "title": "Jugendredaktion",
         "suggested_group": "Verein/Jugend",
         "grants": [{"object": "news", "activities": ["publish"],
                     "area": "jugend"}]},
        {"key": "chef", "title": "Obmann", "suggested_group": "Verein/Obmann",
         "may_grant": ["official"],
         "grants": [{"object": "venue", "activities": ["announce"]}]},
    ],
}
APP, T1, T2 = "hib", "t-eins", "t-zwei"


def fresh():
    st = az.empty_state()
    az.register(st, APP, "1.0.0", copy.deepcopy(DECL))
    return st


def rows(plan):
    return {r["template"]: r for r in plan["rows"]}


def apply(st, plan, tenant=T1):
    """Was die Befehlszeile tut, mit den Funktionen des Kerns."""
    for r in plan["rows"]:
        if r["state"] != "ready":
            continue
        rid = (r["role"].get("id") if r["role"]["action"] == "present"
               else az.create_role(st, tenant, APP, r["template"],
                                   r["role"]["name"]))
        cid = (r["collection"].get("id") if r["collection"]["action"] == "present"
               else az.create_collection(st, tenant, r["collection"]["name"],
                                         [rid]))
        if r["mapping"]["action"] == "create":
            az.add_mapping(st, tenant, r["group"], cid)


print("Die Vorlagen")
st = fresh()
p = az.plan_setup(st, T1, APP)
r = rows(p)
ok("alle sechs Vorlagen stehen im Plan, in der Reihenfolge der Erklaerung",
   [x["template"] for x in p["rows"]] ==
   ["announcer", "official", "trainer", "bereich", "fest", "chef"], p["rows"])
ok("ohne Kontext und mit Vorschlag: alles, mit Gruppe",
   r["announcer"]["state"] == "ready" and r["announcer"]["group"] == "Verein/Hallensprecher"
   and r["announcer"]["group_source"] == "manifest"
   and [r["announcer"][k]["action"] for k in ("role", "collection", "mapping")]
   == ["create"] * 3, r["announcer"])
ok("ohne Vorschlag: Rolle und Sammlung, keine Gruppe, und der Plan sagt es",
   r["official"]["mapping"]["action"] == "none" and r["official"]["group"] == ""
   and any("--group-for official" in n for n in r["official"]["notes"]),
   r["official"])
ok("KOEDER $context: Rolle und Sammlung ja, Gruppe NEIN, trotz Vorschlag",
   r["trainer"]["role"]["action"] == "create"
   and r["trainer"]["collection"]["action"] == "create"
   and r["trainer"]["mapping"]["action"] == "none" and r["trainer"]["group"] == ""
   and any("context" in n for n in r["trainer"]["notes"]), r["trainer"])
ok("KOEDER $value: uebersprungen, nichts angelegt",
   r["bereich"]["state"] == "skipped" and r["bereich"]["role"]["action"] == "none"
   and r["bereich"]["collection"]["action"] == "none", r["bereich"])
ok("ein fester Wert (area: jugend) ist KEIN $value: alles",
   r["fest"]["state"] == "ready" and r["fest"]["mapping"]["action"] == "create",
   r["fest"])
ok("KOEDER may_grant: Rolle und Sammlung, keine Gruppe",
   r["chef"]["mapping"]["action"] == "none" and r["chef"]["group"] == ""
   and any("may_grant" in n for n in r["chef"]["notes"]), r["chef"])
ok("die Gruppen, die der Realm braucht, sind genau die abgebildeten",
   p["groups"] == ["Verein/Hallensprecher", "Verein/Jugend"], p["groups"])

print("Der Gruppenpfad")
p = az.plan_setup(st, T1, APP, group_for={"announcer": "Mannschaft/Sprecher",
                                          "official": "Verein/Betreuer"})
r = rows(p)
ok("--group-for geht vor dem Vorschlag",
   r["announcer"]["group"] == "Mannschaft/Sprecher"
   and r["announcer"]["group_source"] == "tenant", r["announcer"])
ok("--group-for gibt einer Vorlage ohne Vorschlag einen Pfad",
   r["official"]["mapping"]["action"] == "create"
   and r["official"]["group"] == "Verein/Betreuer", r["official"])
p = az.plan_setup(st, T1, APP, group_for={"trainer": "Verein/Trainer"})
ok("KOEDER: --group-for macht aus $context keine Gruppe",
   rows(p)["trainer"]["mapping"]["action"] == "none", rows(p)["trainer"])
p = az.plan_setup(st, T1, APP, group_for={"announcer": "/Verein/Sprecher"})
ok("ein Pfad, wie ihn der Realm zeigt (mit fuehrendem /), wird ohne ihn gefuehrt",
   rows(p)["announcer"]["group"] == "Verein/Sprecher", rows(p)["announcer"])
refuses("ein leeres Segment im Pfad wird abgelehnt",
        lambda: az.plan_setup(st, T1, APP, group_for={"announcer": "a//b"}),
        "1 to 3 segments")
refuses("vier Segmente sind zu viel",
        lambda: az.plan_setup(st, T1, APP, group_for={"announcer": "a/b/c/d"}),
        "1 to 3 segments")

print("Auswahl")
p = az.plan_setup(st, T1, APP, only=["announcer"])
ok("--only: nur diese Vorlage laeuft",
   [x["template"] for x in p["rows"] if x["state"] == "ready"] == ["announcer"],
   p["rows"])
p = az.plan_setup(st, T1, APP, skip=["announcer"])
ok("--skip nimmt eine Vorlage aus",
   rows(p)["announcer"]["state"] == "skipped"
   and rows(p)["announcer"]["role"]["action"] == "none", rows(p)["announcer"])
refuses("KOEDER: ein unbekannter --only-Schluessel schreibt nichts, er wird abgelehnt",
        lambda: az.plan_setup(st, T1, APP, only=["announcr"]), "announcr")
refuses("auch --skip", lambda: az.plan_setup(st, T1, APP, skip=["nope"]), "nope")
refuses("auch --group-for", lambda: az.plan_setup(st, T1, APP,
                                                   group_for={"nope": "a/b"}), "nope")
refuses("eine App ohne Erklaerung", lambda: az.plan_setup(st, T1, "gibtsnicht"), "")

print("Wiederholbar")
st = fresh()
apply(st, az.plan_setup(st, T1, APP))
n_roles, n_coll, n_map = len(st["roles"]), len(st["collections"]), len(st["mappings"])
ok("nach dem ersten Lauf: 5 Rollen (ohne $value), 5 Sammlungen, 2 Abbildungen",
   (n_roles, n_coll, n_map) == (5, 5, 2), (n_roles, n_coll, n_map))
p2 = az.plan_setup(st, T1, APP)
r2 = rows(p2)
ok("der zweite Plan: alles vorhanden, nichts zu tun",
   all(r2[k]["state"] == "present" for k in ("announcer", "official", "trainer",
                                              "fest", "chef"))
   and r2["bereich"]["state"] == "skipped", [(k, v["state"]) for k, v in r2.items()])
apply(st, p2)
ok("und ein zweiter Lauf aendert nichts",
   (len(st["roles"]), len(st["collections"]), len(st["mappings"]))
   == (n_roles, n_coll, n_map))

print("Vorhandenes unter anderem Namen")
st = fresh()
rid = az.create_role(st, T1, APP, "official", "Betreuer von Hand")
cid = az.create_collection(st, T1, "Betreuerpaket", [rid])
r = rows(az.plan_setup(st, T1, APP))
ok("Rolle und Sammlung werden erkannt, auch mit eigenem Namen",
   r["official"]["role"]["action"] == "present"
   and r["official"]["collection"]["action"] == "present"
   and r["official"]["role"]["name"] == "Betreuer von Hand", r["official"])
ok("und der Plan legt dafuer nichts an", r["official"]["state"] == "present",
   r["official"])
rid2 = az.create_role(st, T1, APP, "announcer", "Sprecher")
r = rows(az.plan_setup(st, T1, APP))
ok("eine Rolle ohne Sammlung: die Sammlung wird angelegt, die Rolle nicht",
   r["announcer"]["role"]["action"] == "present"
   and r["announcer"]["collection"]["action"] == "create", r["announcer"])
cid2 = az.create_collection(st, T1, "Sprecherpaket", [rid2])
az.add_mapping(st, T1, "Verein/Hallensprecher", cid2)
r = rows(az.plan_setup(st, T1, APP))
ok("eine vorhandene Abbildung ist 'present'",
   r["announcer"]["mapping"]["action"] == "present"
   and r["announcer"]["state"] == "present", r["announcer"])
ok("die Gruppe einer vorhandenen Abbildung braucht der Realm trotzdem",
   "Verein/Hallensprecher" in az.plan_setup(st, T1, APP)["groups"])

print("Konflikt")
st = fresh()
other = az.create_role(st, T1, APP, "announcer", "Betreuer/in")
r = rows(az.plan_setup(st, T1, APP))
ok("der Name 'Betreuer/in' ist von einer anderen Rolle belegt: Konflikt NUR dort",
   r["official"]["state"] == "conflict"
   and r["official"]["role"]["action"] == "none"
   and r["announcer"]["state"] == "ready", (r["official"], r["announcer"]))
ok("der Konflikt sagt, dass der Name belegt ist",
   any("taken" in n for n in r["official"]["notes"]), r["official"])
st = fresh()
az.create_collection(st, T1, "Obmann", [az.create_role(st, T1, APP, "official", "x")])
r = rows(az.plan_setup(st, T1, APP))
ok("ein belegter Sammlungsname ist ein Konflikt, und die Rolle wird nicht angelegt",
   r["chef"]["state"] == "conflict" and r["chef"]["role"]["action"] == "none",
   r["chef"])
st = fresh()
rid = az.create_role(st, T1, APP, "announcer", "Hallensprecher/in")
az.retire_role(st, T1, rid)
r = rows(az.plan_setup(st, T1, APP))
ok("KOEDER: eine ausgeblendete Rolle wird nie wiederverwendet, ihr Name bleibt belegt",
   r["announcer"]["state"] == "conflict", r["announcer"])

print("Eigener Name (--name-for)")
st = fresh()
other = az.create_role(st, T1, APP, "announcer", "Betreuer/in")
r = rows(az.plan_setup(st, T1, APP))
ok("Ausgangslage: der Titel 'Betreuer/in' ist belegt -> Konflikt",
   r["official"]["state"] == "conflict", r["official"])
p = az.plan_setup(st, T1, APP, name_for={"official": "Betreuer (Mannschaft)"})
r = rows(p)
ok("--name-for loest den Konflikt: Rolle UND Sammlung tragen den gewaehlten Namen",
   r["official"]["state"] == "ready"
   and r["official"]["role"]["name"] == "Betreuer (Mannschaft)"
   and r["official"]["collection"]["name"] == "Betreuer (Mannschaft)", r["official"])
apply(st, p)
ok("angelegt unter diesem Namen",
   any(x["name"] == "Betreuer (Mannschaft)" for x in st["roles"].values())
   and any(x["name"] == "Betreuer (Mannschaft)" for x in st["collections"].values()))
ok("und der naechste Plan sagt 'present' fuer diese Vorlage, auch ohne --name-for",
   rows(az.plan_setup(st, T1, APP))["official"]["state"] == "present")
st = fresh()
az.create_collection(st, T1, "Obmann", [az.create_role(st, T1, APP, "official", "x")])
r = rows(az.plan_setup(st, T1, APP, name_for={"chef": "Obmann des Vereins"}))
ok("auch ein belegter Sammlungsname laesst sich so umgehen",
   r["chef"]["state"] == "ready"
   and r["chef"]["collection"]["name"] == "Obmann des Vereins", r["chef"])
st = fresh()
rid = az.create_role(st, T1, APP, "announcer", "Sprecher von Hand")
az.create_collection(st, T1, "Sprecherpaket", [rid])
r = rows(az.plan_setup(st, T1, APP, name_for={"announcer": "Sprecherin"}))
ok("KOEDER: eine vorhandene Rolle wird NICHT umbenannt, der Plan sagt, dass --name-for nicht benutzt wurde",
   r["announcer"]["role"]["name"] == "Sprecher von Hand"
   and r["announcer"]["role"]["action"] == "present"
   and any("not used" in n for n in r["announcer"]["notes"]), r["announcer"])
for bad, why in (("", "leer"), (" x", "Leerzeichen vorn"), ("x" * 81, "81 Zeichen"),
                 ("ab", "Steuerzeichen")):
    refuses(f"--name-for mit einem unbrauchbaren Namen ({why}) wird abgelehnt",
            lambda b=bad: az.plan_setup(fresh(), T1, APP, name_for={"official": b}),
            "--name-for")
refuses("--name-for mit unbekanntem Schluessel wird abgelehnt",
        lambda: az.plan_setup(fresh(), T1, APP, name_for={"nope": "X"}), "nope")
p = az.plan_setup(fresh(), T1, APP, name_for={"official": "Betreuung"},
                  skip=["official"])
ok("eine abgewaehlte Vorlage bleibt abgewaehlt, auch mit --name-for",
   rows(p)["official"]["state"] == "skipped", rows(p)["official"])

print("Mandanten")
st = fresh()
apply(st, az.plan_setup(st, T1, APP))
r = rows(az.plan_setup(st, T2, APP))
ok("KOEDER: was ein anderer Mandant angelegt hat, ist hier nicht 'vorhanden'",
   all(x["role"]["action"] in ("create", "none") for x in r.values()), r)
ok("und die Namen kollidieren nicht ueber Mandanten hinweg",
   r["announcer"]["state"] == "ready", r["announcer"])

print("Der Vorschlag im Manifest")
bad = copy.deepcopy(DECL)
bad["role_templates"][0]["suggested_group"] = "/Verein/X"
ok("ein Pfad mit fuehrendem Schraegstrich besteht die Pruefung nicht",
   az.validate_declaration(bad) != [], az.validate_declaration(bad))
bad["role_templates"][0]["suggested_group"] = "a/b/c/d"
ok("vier Segmente auch nicht", az.validate_declaration(bad) != [])
bad["role_templates"][0]["suggested_group"] = ["Verein"]
ok("eine Liste auch nicht", az.validate_declaration(bad) != [])
good = copy.deepcopy(DECL)
good["role_templates"][0]["suggested_group"] = "Verein/Hallen sprecher"
ok("ein Leerzeichen in der Mitte ist erlaubt",
   az.validate_declaration(good) == [], az.validate_declaration(good))
a, b = copy.deepcopy(DECL), copy.deepcopy(DECL)
b["role_templates"][0]["suggested_group"] = "Anders/Gruppe"
ok("eine geaenderte Empfehlung ist 'unchanged' (kein Versprechen)",
   az.compare(a, b)[0] == "unchanged", az.compare(a, b))
del b["role_templates"][0]["suggested_group"]
ok("auch ein entfernter Vorschlag ist nicht zerstoererisch",
   az.compare(a, b)[0] == "unchanged", az.compare(a, b))

print("\nALLE PRUEFUNGEN BESTANDEN" if not fails
      else f"\n{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
