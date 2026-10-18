#!/usr/bin/env python3
"""Der Aufbau-Schritt `authz.setup` (RFC-0055, oaap.core.authorization 0.4 §2.11).

Ein Schritt gilt erst als erledigt, wenn seine PRUEFUNG es bestaetigt (RFC-0055 §3).
Darum steht hier die Pruefung im Mittelpunkt:

    - das Profil: der Schritt braucht `app`, kennt nur `only`/`skip`/`groups`,
      alles andere wird vor dem ersten Schritt abgelehnt;
    - die Pruefung sagt ABSENT, solange etwas zu tun ist, PRESENT, wenn nichts
      mehr zu tun ist -- auch wenn der Mandant Vorlagen bewusst offen laesst
      ($value) -- und CONFLICT, wenn ein Name anderweitig belegt ist;
    - nach dem Lauf sagt dieselbe Pruefung PRESENT (der Schritt wird nicht
      durch den Erfolg des Befehls erledigt, sondern durch das, was da ist);
    - der Befehl bekommt seine Argumente als LISTE (kein Textbaustein).

Aufruf: python3 test/test_authz_setup_step.py
"""
import copy
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-authz-step-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m                                             # noqa: E402
import authorization as az                                     # noqa: E402
import tenant_build as tb                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


TID, LABEL, APP = "t-v", "verein", "hib"
DECL = {
    "objects": [{"key": "venue", "title": "Halle", "activities": ["announce"]},
                {"key": "news", "title": "News", "activities": ["publish"],
                 "fields": [{"key": "area", "values": ["a", "b"]}]}],
    "role_templates": [
        {"key": "announcer", "title": "Hallensprecher/in",
         "suggested_group": "Verein/Hallensprecher",
         "grants": [{"object": "venue", "activities": ["announce"]}]},
        {"key": "bereich", "title": "Bereich",
         "grants": [{"object": "news", "activities": ["publish"],
                     "area": "$value"}]},
    ],
}
STATE = az.empty_state()
az.register(STATE, APP, "1.0.0", copy.deepcopy(DECL))
SEEN = {"argv": None}


def fake_route(method, path, body=None):
    route = path.split("?")[0]
    mine = lambda d: [dict(x) for x in d.values() if x["tenant"] == TID]  # noqa: E731
    if method == "GET":
        if route.endswith("/declarations"):
            return 200, {"declarations": [
                {"app": a, "version": d["version"],
                 "declaration": d["declaration"], "roles": 0}
                for a, d in STATE["declarations"].items()]}
        key = route.rsplit("/", 1)[1]
        return 200, {key: mine(STATE[key])}
    try:
        if route.endswith("/roles"):
            rid = az.create_role(STATE, TID, body["app"], body["template"],
                                 body["name"], body.get("values"))
            return 201, {"role": STATE["roles"][rid]}
        if route.endswith("/collections"):
            cid = az.create_collection(STATE, TID, body["name"], body["roles"])
            return 201, {"collection": STATE["collections"][cid]}
        if route.endswith("/mappings"):
            mid = az.add_mapping(STATE, TID, body["group"], body["collection"])
            return 201, {"mapping": STATE["mappings"][mid]}
    except az.AuthzError as e:
        return 400, {"error": str(e)}
    return 404, {}


m._authz_call = fake_route
m.tenant_by_label = lambda label, include_former=True: (
    (TID, {"label": LABEL, "name": "Verein"}) if label == LABEL else (None, None))
m.audit_tenant = lambda *a, **k: None
m.os.geteuid = lambda: 0
real_run = m._run_verb


def run_verb(argv):
    SEEN["argv"] = list(argv)
    return real_run(argv)


m._run_verb = run_verb
drv = m._BuildDrivers(LABEL)

print("Das Profil")
prof = {"profile": tb.PROFILE_FORMAT, "id": "p", "title": "P",
        "params": {"label": {"kind": "label", "required": True}},
        "steps": [{"id": "s", "type": "authz.setup", "app": "hib"}]}
ok("ein Schritt mit nur `app` ist gueltig", tb.profile_problems(prof) == [],
   tb.profile_problems(prof))
prof["steps"][0].update(only="announcer", skip="bereich",
                        groups="announcer=Verein/Sprecher")
ok("mit only, skip, groups auch", tb.profile_problems(prof) == [],
   tb.profile_problems(prof))
del prof["steps"][0]["app"]
ok("KOEDER: ohne `app` abgelehnt", any("needs 'app'" in p
                                        for p in tb.profile_problems(prof)),
   tb.profile_problems(prof))
prof["steps"][0].update(app="hib", shell="rm -rf /")
ok("KOEDER: ein fremdes Argument wird abgelehnt, vor dem ersten Schritt",
   any("'shell' is not an argument" in p for p in tb.profile_problems(prof)),
   tb.profile_problems(prof))

print("Die Pruefung")
a = {"app": APP}
verdict, note = drv.check("authz.setup", a, {})
ok("vorher: ABSENT, und sie nennt die Vorlage", verdict == tb.ABSENT
   and "announcer" in note, (verdict, note))
verdict, note = drv.check("authz.setup", {"app": "gibtsnicht"}, {})
ok("eine App ohne Erklaerung: ABSENT mit dem Hinweis, sie zuerst zu installieren",
   verdict == tb.ABSENT and "install" in note, (verdict, note))
verdict, note = drv.check("authz.setup", {"app": APP, "only": "nope"}, {})
ok("ein unbekannter Schluessel: CONFLICT, nicht ein stiller Erfolg",
   verdict == tb.CONFLICT and "nope" in note, (verdict, note))

print("Der Lauf")
ok_, note, made = drv.do("authz.setup", {"app": APP,
                                          "groups": "announcer=Verein/Sprecher"}, {})
ok("der Befehl lief", ok_, note)
ok("er bekam seine Argumente als Liste, mit --group-for",
   SEEN["argv"] == ["authz", "setup", "--tenant", LABEL, "--app", APP,
                    "--group-for", "announcer=Verein/Sprecher"], SEEN["argv"])
ok("angelegt: eine Rolle, eine Sammlung, eine Abbildung -- die Vorlage mit $value NICHT",
   (len(STATE["roles"]), len(STATE["collections"]), len(STATE["mappings"]))
   == (1, 1, 1), (len(STATE["roles"]), len(STATE["mappings"])))
ok("der Schritt macht nichts rueckgaengig (nichts loescht): keine 'made'-Eintraege",
   made == [], made)
verdict, note = drv.check("authz.setup", {"app": APP,
                                           "groups": "announcer=Verein/Sprecher"}, {})
ok("danach sagt dieselbe Pruefung PRESENT -- auch mit der offenen $value-Vorlage",
   verdict == tb.PRESENT, (verdict, note))

print("only und skip erreichen den Befehl")
drv.do("authz.setup", {"app": APP, "only": "announcer", "skip": "bereich"}, {})
ok("--only und --skip stehen in der Argumentliste",
   "--only" in SEEN["argv"] and SEEN["argv"][SEEN["argv"].index("--only") + 1]
   == "announcer" and "--skip" in SEEN["argv"]
   and SEEN["argv"][SEEN["argv"].index("--skip") + 1] == "bereich", SEEN["argv"])

drv.do("authz.setup", {"app": APP, "names": "announcer=Sprecherin,bereich=Bereichsleitung"}, {})
ok("`names` erreicht den Befehl als --name-for, je Paar einmal",
   SEEN["argv"].count("--name-for") == 2
   and "announcer=Sprecherin" in SEEN["argv"]
   and "bereich=Bereichsleitung" in SEEN["argv"], SEEN["argv"])
verdict, note = drv.check("authz.setup", {"app": APP, "names": "nope=X"}, {})
ok("und die Pruefung kennt `names`: ein unbekannter Schluessel ist ein CONFLICT",
   verdict == tb.CONFLICT and "nope" in note, (verdict, note))
prof["steps"][0].update(app="hib", names="announcer=Sprecherin")
prof["steps"][0].pop("shell", None)
ok("das Profil erlaubt `names`", tb.profile_problems(prof) == [], tb.profile_problems(prof))

print("Konflikt")
STATE["roles"].clear(), STATE["collections"].clear(), STATE["mappings"].clear()
az.create_role(STATE, TID, APP, "announcer", "Hallensprecher/in")
# dieselbe Vorlage, aber die Rolle ist ausgeblendet -> ihr Name bleibt belegt
rid = next(iter(STATE["roles"]))
az.retire_role(STATE, TID, rid)
verdict, note = drv.check("authz.setup", {"app": APP}, {})
ok("eine ausgeblendete Rolle mit dem Namen: CONFLICT, der Aufbau haelt an",
   verdict == tb.CONFLICT and "announcer" in note, (verdict, note))

print("\nALLE PRUEFUNGEN BESTANDEN" if not fails
      else f"\n{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
