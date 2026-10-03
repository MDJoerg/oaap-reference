#!/usr/bin/env python3
"""Fachliche Berechtigungen, der Kern (oaap.core.authorization 0.1, RFC-0045).

Ein Berechtigungssystem, das eine Tuer offen laesst, sieht von aussen aus
wie eines, das funktioniert. Darum sind die Koeder hier die eigentlichen
Pruefungen: ein Trainer von mC darf nicht in mB aufstellen, eine Frage
ohne Mannschaft darf nicht mit "ja" beantwortet werden, weil er EINE
Mannschaft hat, ein abgelaufenes Recht ist weg, ein fremder Mandant sieht
nichts.

Geprueft wird:
    - die Erklaerung: jede Regel aus Abschnitt 2.1 hat ein Gegenbeispiel;
    - der Vergleich: hinzufuegen ist unkritisch, Entfernen und Umdeuten
      nicht, und die betroffenen Rollen werden genannt;
    - Rollen, Sammlungen, Zuordnungen: Werte sind IDs, Kontext ist Pflicht
      wo die Vorlage ihn verlangt, ein Mandant erreicht nur sich selbst;
    - `effective`: nur die Objekte DIESER App, nur gueltige Zuordnungen;
    - `may`: schliesst in beide Richtungen.

Braucht weder Docker noch Flask.

Aufruf: python3 test/test_authorization_core.py
"""
import copy
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import authorization as az                                     # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def refuses(label, fn, needle=""):
    try:
        fn()
    except az.AuthzError as e:
        ok(label, needle in str(e), str(e))
        return
    ok(label, False, "no refusal")


DECL = {
    "objects": [
        {"key": "team", "title": "Mannschaft",
         "activities": ["read", "edit_lineup", "manage_members"],
         "fields": [{"key": "team", "context": "Mannschaft"}]},
        {"key": "news", "title": "News", "activities": ["read", "publish"],
         "fields": [{"key": "area",
                     "values": ["sponsoring", "news", "hallenzeiten"]}]},
    ],
    "role_templates": [
        {"key": "trainer", "title": "Trainer/in",
         "grants": [{"object": "team",
                     "activities": ["read", "edit_lineup"],
                     "team": "$context"}],
         "may_grant": ["spieler"]},
        {"key": "spieler", "title": "Spieler/in",
         "grants": [{"object": "team", "activities": ["read"],
                     "team": "$context"}]},
        {"key": "bereich", "title": "Bereich",
         "grants": [{"object": "news", "activities": ["read", "publish"],
                     "area": "$value"}]},
    ],
}

print("Die Erklaerung (Abschnitt 2.1)")
ok("die gueltige Erklaerung besteht", az.validate_declaration(DECL) == [],
   az.validate_declaration(DECL))


def bad(label, mutate, needle):
    d = copy.deepcopy(DECL)
    mutate(d)
    errs = az.validate_declaration(d)
    ok(label, any(needle in e for e in errs), errs)


bad("Schluessel muss dem Muster entsprechen",
    lambda d: d["objects"][0].update(key="Team!"), "must match")
bad("zwei gleiche Objekte", lambda d: d["objects"].append(
    copy.deepcopy(d["objects"][0])), "declared twice")
bad("zwei gleiche Aktivitaeten",
    lambda d: d["objects"][0]["activities"].append("read"), "twice")
bad("Objekt ohne Aktivitaeten",
    lambda d: d["objects"][0].update(activities=[]), "activities")
bad("Feld mit Kontext UND Werten",
    lambda d: d["objects"][1]["fields"][0].update(context="X"), "exactly one")
bad("Feld ohne Quelle",
    lambda d: d["objects"][1]["fields"][0].pop("values"), "exactly one")
bad("doppelte Werte",
    lambda d: d["objects"][1]["fields"][0].update(values=["a", "a"]),
    "distinct")
bad("Vorlage nennt ein nicht erklaertes Objekt",
    lambda d: d["role_templates"][0]["grants"][0].update(object="nix"),
    "not declared")
bad("Vorlage nennt eine nicht erklaerte Aktivitaet",
    lambda d: d["role_templates"][0]["grants"][0].update(activities=["fly"]),
    "activities")
bad("Vorlage nennt ein nicht erklaertes Feld",
    lambda d: d["role_templates"][0]["grants"][0].update(nix="$context"),
    "field 'nix'")
bad("$context auf einem Feld mit Werteliste",
    lambda d: d["role_templates"][2]["grants"][0].update(area="$context"),
    "not a context field")
bad("$value auf einem Kontextfeld",
    lambda d: d["role_templates"][0]["grants"][0].update(team="$value"),
    "no value list")
bad("fest vorgegebener Wert, der nicht erklaert ist",
    lambda d: d["role_templates"][2]["grants"][0].update(area=["gibtsnicht"]),
    "not declared")
bad("may_grant nennt eine unbekannte Vorlage",
    lambda d: d["role_templates"][0].update(may_grant=["nix"]), "may_grant")
bad("unbekannter Schluessel im Objekt",
    lambda d: d["objects"][0].update(extra=1), "unknown key")
bad("unbekannter Schluessel im Abschnitt",
    lambda d: d.update(extra=1), "unknown key")
bad("Titel fehlt", lambda d: d["objects"][0].pop("title"), "title")
ok("eine Erklaerung, die kein Objekt ist, wird abgelehnt",
   az.validate_declaration([]) != [])

print("Der Vergleich (Abschnitt 2.2)")
ok("erste Registrierung heisst 'new'", az.compare(None, DECL)[0] == "new")
ok("dieselbe Erklaerung ist unveraendert",
   az.compare(DECL, copy.deepcopy(DECL))[0] == "unchanged")
d2 = copy.deepcopy(DECL)
d2["objects"][0]["activities"].append("export")
d2["role_templates"].append({"key": "gast", "title": "Gast",
                             "grants": [{"object": "news",
                                         "activities": ["read"]}]})
ok("Hinzufuegen ist additiv", az.compare(DECL, d2)[0] == "additive")
d3 = copy.deepcopy(DECL)
d3["objects"][0]["title"] = "Team"
ok("nur ein Titel anders ist unveraendert",
   az.compare(DECL, d3)[0] == "unchanged")
d4 = copy.deepcopy(DECL)
d4["objects"][0]["activities"].remove("edit_lineup")
d4["role_templates"][0]["grants"][0]["activities"] = ["read"]
kind, removed, _ = az.compare(DECL, d4)
ok("eine entfernte Aktivitaet ist zerstoerend und wird genannt",
   kind == "destructive" and "activity:team.edit_lineup" in removed, removed)
d5 = copy.deepcopy(DECL)
d5["objects"][1]["fields"][0]["values"].remove("news")
d5["role_templates"][2]["grants"][0]["area"] = "$value"
ok("ein entfernter Wert ist zerstoerend",
   az.compare(DECL, d5)[0] == "destructive")
d6 = copy.deepcopy(DECL)
f = d6["objects"][1]["fields"][0]
f.pop("values")
f["context"] = "Bereich"
d6["role_templates"][2]["grants"][0]["area"] = "$context"
ok("eine Feldquelle von Werten auf Kontext ist zerstoerend",
   az.compare(DECL, d6)[0] == "destructive")
d7 = copy.deepcopy(DECL)
d7["role_templates"] = [t for t in d7["role_templates"] if t["key"] != "spieler"]
d7["role_templates"][0]["may_grant"] = []
ok("eine entfernte Vorlage ist zerstoerend",
   az.compare(DECL, d7)[0] == "destructive")

print("Rollen, Sammlungen, Zuordnungen (Abschnitt 2.3)")
S = az.empty_state()
az.register(S, "vereinsportal", "0.1.0", DECL)
refuses("eine Rolle fuer eine App ohne Erklaerung",
        lambda: az.create_role(S, "T1", "andere", "trainer", "X"),
        "no authorization")
refuses("eine Rolle fuer eine nicht erklaerte Vorlage",
        lambda: az.create_role(S, "T1", "vereinsportal", "nix", "X"),
        "not declared")
refuses("eine $value-Vorlage ohne Werte",
        lambda: az.create_role(S, "T1", "vereinsportal", "bereich", "News"),
        "needs")
refuses("ein Wert, der nicht erklaert ist",
        lambda: az.create_role(S, "T1", "vereinsportal", "bereich", "News",
                               {"area": ["gibtsnicht"]}), "needs")
refuses("ein Feld, das die Vorlage nicht kennt",
        lambda: az.create_role(S, "T1", "vereinsportal", "trainer", "Tr",
                               {"team": ["mB"]}), "no field")
trainer = az.create_role(S, "T1", "vereinsportal", "trainer", "Trainer Jugend",
                         who="chef")
spieler = az.create_role(S, "T1", "vereinsportal", "spieler", "Spieler",
                         who="chef")
news = az.create_role(S, "T1", "vereinsportal", "bereich", "Bereich News",
                      {"area": ["news"]}, who="chef")
refuses("zwei Rollen gleichen Namens im Mandanten",
        lambda: az.create_role(S, "T1", "vereinsportal", "trainer",
                               "Trainer Jugend"), "already")
other = az.create_role(S, "T2", "vereinsportal", "trainer", "Trainer Jugend")
ok("derselbe Name in einem anderen Mandanten ist erlaubt", bool(other))
refuses("eine Sammlung mit der Rolle eines anderen Mandanten",
        lambda: az.create_collection(S, "T1", "Mix", [trainer, other]),
        "not known")
refuses("eine leere Sammlung",
        lambda: az.create_collection(S, "T1", "Leer", []), "at least")
coll = az.create_collection(S, "T1", "Trainerteam", [trainer, news], who="chef")
refuses("Zuordnung ohne Kontext, obwohl die Vorlage ihn verlangt",
        lambda: az.assign(S, "T1", coll, "u-ben", {}, granted_by="chef"),
        "needed")
refuses("Zuordnung mit einem Kontextfeld, das es nicht gibt",
        lambda: az.assign(S, "T1", coll, "u-ben", {"team": ["mB"], "x": "y"},
                          granted_by="chef"), "no context field")
refuses("Zuordnung mit unzulaessigem Kontextwert",
        lambda: az.assign(S, "T1", coll, "u-ben", {"team": ["<script>"]},
                          granted_by="chef"), "twin object")
refuses("Zuordnung einer fremden Sammlung",
        lambda: az.assign(S, "T2", coll, "u-ben", {"team": ["mB"]},
                          granted_by="x"), "no such collection")
refuses("Gueltigkeit rueckwaerts",
        lambda: az.assign(S, "T1", coll, "u-ben", {"team": ["mB"]},
                          "2026-12-31", "2026-01-01", "chef"), "before")
a_ben = az.assign(S, "T1", coll, "u-ben", {"team": ["mB"]}, granted_by="chef")
ok("granted_by steht in der Zuordnung",
   S["assignments"][a_ben]["granted_by"] == "chef")

print("effective (Abschnitt 2.4)")
today = date(2026, 10, 3)
g = az.effective(S, "T1", "vereinsportal", "u-ben", today)
ok("Ben bekommt aufgeloeste Rechte: Aufstellung in mB und News",
   any(x["object"] == "team" and "edit_lineup" in x["activities"]
       and x["fields"] == {"team": ["mB"]} for x in g)
   and any(x["object"] == "news" and x["fields"] == {"area": ["news"]}
           for x in g), g)
ok("eine andere Person hat nichts",
   az.effective(S, "T1", "vereinsportal", "u-carla", today) == [])
ok("der andere Mandant sieht Bens Rechte nicht",
   az.effective(S, "T2", "vereinsportal", "u-ben", today) == [])
ok("eine andere App sieht Bens Rechte nicht",
   az.effective(S, "T1", "andere", "u-ben", today) == [])
a_old = az.assign(S, "T1", coll, "u-tim", {"team": ["mC"]},
                  "2025-01-01", "2025-12-31", "chef")
ok("abgelaufene Zuordnung zaehlt nicht",
   az.effective(S, "T1", "vereinsportal", "u-tim", today) == [])
a_fut = az.assign(S, "T1", coll, "u-lea", {"team": ["mC"]},
                  "2027-01-01", "", "chef")
ok("noch nicht gueltige Zuordnung zaehlt nicht",
   az.effective(S, "T1", "vereinsportal", "u-lea", today) == [])
ok("am letzten Tag gilt sie noch",
   az.effective(S, "T1", "vereinsportal", "u-tim", date(2025, 12, 31)) != [])
ok("Widerruf beendet die Zuordnung und loescht sie nicht",
   az.revoke(S, "T1", a_ben, "chef") and a_ben in S["assignments"]
   and az.effective(S, "T1", "vereinsportal", "u-ben", today) == [])
ok("ein zweiter Widerruf aendert nichts",
   az.revoke(S, "T1", a_ben, "chef") is False)
refuses("Widerruf einer fremden Zuordnung",
        lambda: az.revoke(S, "T2", a_old, "x"), "no such assignment")
a_ben = az.assign(S, "T1", coll, "u-ben", {"team": ["mB"]}, granted_by="chef")
g = az.effective(S, "T1", "vereinsportal", "u-ben", today)

print("may (die Koeder)")
ok("Ben darf in mB aufstellen", az.may(g, "team.edit_lineup", team="mB"))
ok("KOEDER: Ben darf NICHT in mC aufstellen",
   not az.may(g, "team.edit_lineup", team="mC"))
ok("KOEDER: ohne Mannschaft zu nennen heisst es nein",
   not az.may(g, "team.edit_lineup"))
ok("... und 'irgendwo' sagt man ausdruecklich",
   az.may_any(g, "team.edit_lineup"))
ok("Ben darf die Mitglieder nicht verwalten (nicht in der Vorlage)",
   not az.may(g, "team.manage_members", team="mB"))
ok("unbekanntes Objekt: nein", not az.may(g, "kasse.read"))
ok("unbekannte Aktivitaet: nein", not az.may(g, "team.fly", team="mB"))
ok("Ben darf News veroeffentlichen, aber nur im Bereich news",
   az.may(g, "news.publish", area="news")
   and not az.may(g, "news.publish", area="sponsoring"))
ok("kein Recht: nein", not az.may([], "team.read", team="mB"))
ok("Platzhalter ($context) steht nie in der Antwort",
   "$context" not in str(g) and "$value" not in str(g))

print("Die Erklaerung wird ersetzt")
S2 = copy.deepcopy(S)
kind, removed, impact = az.register(S2, "vereinsportal", "0.2.0", d4)
ok("zerstoerende Aenderung ohne Bestaetigung: nichts geschrieben",
   kind == "destructive"
   and S2["declarations"]["vereinsportal"]["version"] == "0.1.0")
ok("... und die betroffene Rolle samt Zuordnungen wird genannt",
   impact.get("T1", {}).get("roles") == ["Trainer Jugend"]
   and impact["T1"]["assignments"] >= 1
   and "T2" in impact, impact)
kind, removed, impact = az.register(S2, "vereinsportal", "0.2.0", d4,
                                    confirm=True)
ok("mit Bestaetigung wird geschrieben",
   S2["declarations"]["vereinsportal"]["version"] == "0.2.0")
ok("eine additive Aenderung braucht keine Bestaetigung",
   az.register(copy.deepcopy(S), "vereinsportal", "0.1.1", d2)[0]
   == "additive")
kind, removed, impact = az.withdraw(copy.deepcopy(S), "vereinsportal")
ok("ein Paket ohne den Abschnitt wird gezeigt, nicht still entfernt",
   kind == "destructive" and "T1" in impact)
refuses("eine unsaubere Erklaerung wird nicht registriert",
        lambda: az.register(copy.deepcopy(S), "x", "1", {"objects": 5}),
        "not sound")

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
