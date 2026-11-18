#!/usr/bin/env python3
"""Eine Plattformrolle je Realm-Gruppe (oaap.core.tenant 1.9 2.8.1,
RFC-0091 Stufe 2, Konformitaetstest 30).

    Nur drei Rollen  guest, partner, user -- alles andere abgelehnt
    Bei jeder Anmeldung  gegeben, was die Gruppe traegt; beendet, was die
                     Gruppe nicht mehr traegt
    Von Hand ist von Hand  eine Rolle ohne Herkunft fasst der Abgleich
                     nie an
    Gewaehrend       eine role_map zaehlt fuer die K7-/Broker-Ablehnung
                     wie `role`
    Der echte Weg    _idp_principal des Identitaetsdienstes, Protokoll
                     user.idp-role-sync

Aufruf: python3 test/test_idp_role_map.py
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "platform", "services")
PLATFORM = os.path.join(HERE, "..", "platform")
IDENTITY_DIR = os.path.join(SERVICES, "identity")
DATA = tempfile.mkdtemp(prefix="oaap-rolemap-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, SERVICES)

import idp  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


POL = {"role_map": {"partner": "partner", "/mitarbeiter": "user"}}

print("=== die Tabelle ===")
p = idp.policy_of({"idp_policy": POL})
ok("Pfade ohne fuehrenden Schraegstrich",
   p["role_map"] == {"partner": "partner", "mitarbeiter": "user"}, p["role_map"])
ok("ein von Hand eingetragenes admin wird beim Lesen verworfen",
   idp.policy_of({"idp_policy": {"role_map": {"chefs": "admin"}}})["role_map"] == {})
for bad in ("admin", "keyuser", "tenant_admin", "server_admin", "support"):
    why = idp.role_map_refusal({"chefs": bad})
    ok(f"eine Abbildung auf {bad} wird abgelehnt", "only guest, partner, user"
       in why, why)
ok("guest/partner/user gehen", not idp.role_map_refusal(
    {"a": "guest", "b": "partner", "c": "user"}))
ok("eine leere Gruppe wird abgelehnt", idp.role_map_refusal({" ": "user"}))
ok("policy_refusal nennt die Abbildung",
   "cannot come from a realm group" in idp.policy_refusal(
       "eingang", "", False, role_map={"x": "admin"}))

print("\n=== gewaehrend wie `role` ===")
OPEN_REALM = {"read": "2026-11-18T08:00:00Z", "space": "t",
              "brokers": [{"alias": "google", "product": "google",
                           "enabled": True, "population": "any"}]}
why = idp.policy_refusal("eingang", "", False, realm=OPEN_REALM,
                         role_map={"partner": "partner"})
ok("offener Broker + role_map ohne Begruendung: abgelehnt",
   "role per realm group" in why, why)
ok("... mit Begruendung: geht", not idp.policy_refusal(
    "eingang", "", False, realm=OPEN_REALM, role_map={"partner": "partner"},
    reason="Partner X meldet sich nur ueber Google an"))
ok("... ohne role_map bleibt eingang frei", not idp.policy_refusal(
    "eingang", "", False, realm=OPEN_REALM))
ok("Selbstregistrierung + role_map ohne Begruendung: abgelehnt",
   "self-registration together with a role per realm group" in
   idp.policy_refusal("eingang", "", True, role_map={"partner": "partner"}))
drift = idp.drift_lines({"first_login": "eingang", "realm": OPEN_REALM,
                         "role_map": {"partner": "partner"}, "reason": ""})
ok("die Abweichung nennt einen spaeter geoeffneten Broker",
   any("roles per realm group" in d for d in drift), drift)

print("\n=== der Abgleich (rein) ===")


def sync(roles, source, groups, pol=POL):
    return idp.role_sync({"roles": roles, "role_source": source}, pol,
                         {"groups": groups})


r, s, c = sync([], {}, ["/partner"])
ok("erste Anmeldung mit /partner: partner mit Herkunft",
   r == ["partner"] and s == {"partner": "partner"}
   and c == [("added", "partner", "partner")], (r, s, c))
r, s, c = sync(["partner"], {"partner": "partner"}, [])
ok("naechste Anmeldung ohne die Gruppe: beendet",
   r == [] and s == {} and c == [("ended", "partner", "partner")], (r, s, c))
r, s, c = sync(["partner"], {}, [])
ok("von Hand gegebenes partner bleibt, ohne Eintrag", r == ["partner"]
   and s == {} and c == [], (r, s, c))
r, s, c = sync(["partner"], {}, ["/partner"])
ok("... auch wenn die Gruppe es auch traegt: keine Herkunft, keine Aenderung",
   r == ["partner"] and s == {} and c == [], (r, s, c))
r, s, c = sync(["user", "partner"], {"partner": "partner"}, ["/partner"])
ok("unveraendert: keine Zeile", c == [] and sorted(r) == ["partner", "user"])
r, s, c = sync(["partner"], {"partner": "partner"}, ["/partner"], pol={})
ok("Abbildung entfernt: die Rolle aus der Abbildung endet",
   r == [] and c == [("ended", "partner", "partner")], (r, s, c))
r, s, c = sync([], {"partner": "partner"}, ["/partner"])
ok("eine von Hand entfernte Rolle kommt zurueck, solange die Gruppe sie traegt",
   r == ["partner"] and c == [("added", "partner", "partner")], (r, s, c))
r, s, c = sync([], {}, ["/admin", "/server_admin", "/irgendwas"])
ok("nicht abgebildete Gruppen geben nichts", r == [] and c == [], (r, s, c))
r, s, c = sync([], {}, ["/partner"], pol={"role_map": {"partner": "admin"}})
ok("ein admin im gespeicherten Satz gibt beim Abgleich nichts", r == [], r)
ok("die Zeile fuers Protokoll", idp.role_changes_text(
    [("added", "partner", "partner"), ("ended", "user", "mitarbeiter")])
   == "partner erhalten (partner) · user beendet (mitarbeiter)")

print("\n=== der echte Weg: _idp_principal ===")
try:
    import flask  # noqa: F401
except ImportError as e:
    print(f"SKIP  Flask nicht importierbar ({e}).")
    sys.exit(1 if fails else 0)

IDATA = tempfile.mkdtemp(prefix="oaap-rolemap-id-")
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["OAAP_IDENTITY_DATA_DIR"] = IDATA
os.environ.pop("INTERNAL_API_KEY", None)
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
app = importlib.reload(importlib.import_module("app"))
tfile = os.path.join(IDATA, "tenants.json")


def tenants(policy):
    with open(tfile, "w", encoding="utf-8") as f:
        json.dump({"tenants": {"t-default": {"label": "default"},
                               "t-kunde": {"label": "kunde",
                                           "idp_policy": policy}}}, f)


app.TENANTS_FILE = tfile
tenants({"first_login": "eingang", "role_map": {"partner": "partner"}})
audited = []
app.audit = lambda *a, **k: audited.append(a + (k.get("detail", ""),))
PROV = {"kind": "keycloak", "issuer": "https://auth.example/realms/kunde",
        "client_id": "oaap"}


def login(sub, groups, name="anna"):
    u, err = app._idp_principal("t-kunde", PROV, {
        "sub": sub, "preferred_username": name, "groups": groups})
    return u, err


def rec(name):
    return next(u for u in app.load_users() if u["username"] == name)


u, err = login("s-anna", ["/partner"])
ok("Erstanmeldung mit /partner: Rolle partner", u and u["roles"] == ["partner"],
   (u, err))
ok("... mit Herkunft im Datensatz", rec(u["username"]).get("role_source")
   == {"partner": "partner"})
ok("... und einer Zeile user.idp-role-sync",
   any(a[0] == "user.idp-role-sync" and "partner erhalten" in str(a)
       for a in audited), audited)
ok("public_user zeigt die Herkunft", app.public_user(rec(u["username"]))
   ["role_source"] == {"partner": "partner"})
name = u["username"]
audited.clear()
u, _ = login("s-anna", [])
ok("naechste Anmeldung ohne Gruppe: Rolle beendet", rec(name)["roles"] == []
   and "role_source" not in rec(name), rec(name))
ok("... im Protokoll", any(a[0] == "user.idp-role-sync" and "beendet" in str(a)
                           for a in audited), audited)

# Bert hat partner von Hand.
users = app.load_users()
users.append({"username": "kunde.bert", "password_hash": "", "roles":
              ["partner"], "groups": [], "active": True, "kind": "human",
              "tenant": "t-kunde", "id": "u-bert", "session_epoch": 0,
              "idp": {"provider": idp.provider_key(PROV),
                      "subject": "s-bert"}})
app.save_users(users)
audited.clear()
login("s-bert", [], name="bert")
ok("von Hand gegebenes partner bleibt ohne Gruppe", rec("kunde.bert")["roles"]
   == ["partner"])
login("s-bert", ["/partner"], name="bert")
ok("... und wird auch mit Gruppe nicht beansprucht",
   "role_source" not in rec("kunde.bert"))
ok("das Protokoll nennt fuer Bert keine Rollen-Aenderung",
   not any(a[0] == "user.idp-role-sync" for a in audited), audited)

IDENT = open(os.path.join(IDENTITY_DIR, "app.py"), encoding="utf-8").read()
ok("eine Aenderung von Hand behaelt Herkunft nur fuer gehaltene Rollen",
   'if r in roles}\n    if source:\n        u["role_source"] = source' in IDENT)

print("\n=== die Kommandozeile: oaap tenant policy --role-map ===")
sys.path.insert(0, PLATFORM)
import appctl as m  # noqa: E402

m.ensure_default_tenant()
tid, _t = m.tenant_create("partnerx", name="Partner X")
okk, msg = m.tenant_set_policy(tid, role_map={"partner": "admin"})
ok("admin abgelehnt", not okk and "only guest, partner, user" in msg, msg)
okk, msg = m.tenant_set_policy(tid, role_map={"/partner": "partner"})
ok("partner gespeichert", okk and m.load_tenants()[tid]["idp_policy"]
   ["role_map"] == {"partner": "partner"}, msg)
log = open(m.TENANT_LOG,
           encoding="utf-8").read()
ok("im Mandantenlog", "Rollen je Realm-Gruppe: partner=partner" in log)
okk, msg = m.tenant_set_policy(tid, second_factor="off")
ok("eine andere Aenderung laesst die Tabelle stehen",
   okk and m.load_tenants()[tid]["idp_policy"]["role_map"]
   == {"partner": "partner"})
okk, msg = m.tenant_set_policy(tid, role_map={})
ok("{} leert sie", okk and m.load_tenants()[tid]["idp_policy"]["role_map"]
   == {})

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
