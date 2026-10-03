#!/usr/bin/env python3
"""Fachliche Berechtigungen im Identity-Dienst (oaap.core.authorization 0.1).

Der Kern (test_authorization_core.py) weiss, was ein Recht ist. Hier geht
es darum, wer fragen und wer aendern darf -- und dass eine App nur ihre
eigenen Rechte sieht, in ihrem eigenen Mandanten, mit ihrem eigenen
Schluessel.

Geprueft wird (echter Identity-Dienst im Testclient, ohne Docker):
    - Registrierung: eine zerstoerende Aenderung wird NICHT geschrieben,
      solange niemand bestaetigt, und nennt die betroffenen Rollen;
    - Verwaltung: nur tenant_admin/server_admin, und nur im eigenen
      Mandanten; ein server_admin muss den Mandanten nennen;
    - `granted_by` ist der angemeldete Aufrufer, was der Koerper auch sagt;
    - die Zuordnung geht nur an eine Benutzer-ID DES Mandanten;
    - `effective`: nur mit dem Schluessel der Instanz (Bereich oaap.authz),
      nur die Rechte der eigenen App, nur Personen des eigenen Mandanten
      (fremde: 404, nicht 'leer'); Schluessel einer anderen App oder ohne
      Bereich kommen nicht durch; eine Sitzung kommt nicht durch;
    - Widerruf wirkt sofort; ein Mandant erreicht den anderen nie;
    - Plattformrollen bleiben unberuehrt;
    - jede Aenderung steht im Protokoll des Mandanten.

Aufruf: python3 test/test_authorization_identity.py
"""
import copy
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
SERVICES = os.path.join(HERE, "..", "platform", "services")
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-authz-test-")
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = DATA
sys.path.insert(0, SERVICES)
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
m.USERS_FILE = os.path.join(DATA, "users.json")
m.KEYS_FILE = os.path.join(DATA, "api-keys.json")
m.THROTTLE_FILE = os.path.join(DATA, "login-throttle.json")
m.AUDIT_LOG = os.path.join(DATA, "audit.jsonl")
m.TENANTS_FILE = os.path.join(DATA, "tenants.json")
m.AUTHZ_FILE = os.path.join(DATA, "authorization.json")
m.AUTHZ_LOCK_FILE = os.path.join(DATA, "authorization.lock")

with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"},
                           "t-vp": {"label": "vp"},
                           "t-sgl": {"label": "sgl"}}}, f)


def user(uid, name, roles, tenant, kind="human"):
    return {"id": uid, "username": name, "display_name": "",
            "password_hash": "", "kind": kind, "roles": roles, "groups": [],
            "tenant": tenant, "active": True, "session_epoch": 0}


USERS = [
    user("u-op", "joerg", ["server_admin", "admin", "user"], "t-default"),
    user("u-vp-chef", "vp_chef", ["tenant_admin", "user"], "t-vp"),
    user("u-vp-ben", "ben", ["user"], "t-vp"),
    user("u-vp-carla", "carla", ["user"], "t-vp"),
    user("u-sgl-chef", "sgl_chef", ["tenant_admin", "user"], "t-sgl"),
    user("u-sgl-max", "max", ["user"], "t-sgl"),
    user("u-inst-vp", "instance:vp-prod", ["user"], "t-vp", "machine"),
    user("u-inst-x", "instance:andere-app", ["user"], "t-vp", "machine"),
    user("u-inst-sgl", "instance:sgl-prod", ["user"], "t-sgl", "machine"),
]
with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(USERS, f)

c = m.app.test_client()
H = {"X-OAAP-Internal-Key": "test-internal-key"}

DECL = {
    "objects": [
        {"key": "team", "title": "Mannschaft",
         "activities": ["read", "edit_lineup"],
         "fields": [{"key": "team", "context": "Mannschaft"}]},
        {"key": "news", "title": "News", "activities": ["read", "publish"],
         "fields": [{"key": "area", "values": ["news", "sponsoring"]}]},
    ],
    "role_templates": [
        {"key": "trainer", "title": "Trainer/in",
         "grants": [{"object": "team", "activities": ["read", "edit_lineup"],
                     "team": "$context"}]},
        {"key": "bereich", "title": "Bereich",
         "grants": [{"object": "news", "activities": ["read", "publish"],
                     "area": "$value"}]},
    ],
}


def post(path, body, who=None, method="post"):
    body = dict(body)
    if who:
        body["actor"] = who
    return getattr(c, method)(path, headers=H, json=body)


def audit_lines():
    try:
        with open(m.AUDIT_LOG, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


print("Registrierung (Abschnitt 2.2)")
r = post("/internal/authz/register", {"app": "vereinsportal", "version": "0.1.0",
                                      "declaration": DECL})
ok("die erste Registrierung gelingt", r.status_code == 200
   and r.get_json()["kind"] == "new", r.get_json())
r = post("/internal/authz/register", {"app": "vereinsportal", "version": "0.1.0",
                                      "declaration": DECL})
ok("dieselbe noch einmal ist unveraendert",
   r.get_json()["kind"] == "unchanged")
r = post("/internal/authz/register", {"app": "vereinsportal", "version": "0.1",
                                      "declaration": {"objects": 5}})
ok("eine unsaubere Erklaerung wird abgelehnt", r.status_code == 400)
r = c.post("/internal/authz/register", json={})
ok("ohne den internen Schluessel kommt niemand hinein", r.status_code == 401)

print("Verwaltung (Abschnitt 2.3, 2.6)")
r = post("/internal/authz/roles", {"app": "vereinsportal",
                                   "template": "trainer",
                                   "name": "Trainer Jugend"}, "ben")
ok("ein einfacher Benutzer darf keine Rolle bauen", r.status_code == 403)
r = post("/internal/authz/roles", {"app": "vereinsportal",
                                   "template": "trainer",
                                   "name": "Trainer Jugend"}, "joerg")
ok("ein server_admin muss den Mandanten nennen", r.status_code == 400,
   r.get_json())
r = post("/internal/authz/roles", {"app": "vereinsportal",
                                   "template": "trainer",
                                   "name": "Trainer Jugend"}, "vp_chef")
ok("der Mandantenverwalter baut die Rolle", r.status_code == 201, r.get_json())
role_trainer = r.get_json()["role"]
r = post("/internal/authz/roles", {"app": "vereinsportal",
                                   "template": "bereich", "name": "News",
                                   "values": {"area": ["news"]}}, "vp_chef")
role_news = r.get_json()["role"]
ok("eine Rolle mit Werten (IDs)", r.status_code == 201
   and role_news["values"] == {"area": ["news"]})
r = c.get("/internal/authz/roles?actor=sgl_chef", headers=H)
ok("der andere Mandant sieht die Rollen nicht",
   r.get_json()["roles"] == [], r.get_json())
r = c.get("/internal/authz/roles?actor=vp_chef", headers=H)
ok("der eigene Mandant sieht sie",
   len(r.get_json()["roles"]) == 2)
r = c.get("/internal/authz/roles?actor=joerg&tenant=vp", headers=H)
ok("ein server_admin sieht sie, wenn er den Mandanten nennt",
   len(r.get_json()["roles"]) == 2, r.get_json())
r = post("/internal/authz/collections", {"name": "Trainerteam",
                                         "roles": [role_trainer["id"],
                                                   "News"]}, "vp_chef")
ok("eine Sammlung aus einer Rolle per ID und einer per Name",
   r.status_code == 201, r.get_json())
coll = r.get_json()["collection"]
r = post("/internal/authz/collections", {"name": "Fremd",
                                         "roles": [role_trainer["id"]]},
         "sgl_chef")
ok("der andere Mandant kann die Rolle nicht in eine Sammlung nehmen",
   r.status_code == 400, r.get_json())

print("Zuordnung")
before_roles = {u["username"]: list(u["roles"]) for u in m.load_users()}
r = post("/internal/authz/assignments",
         {"collection": coll["id"], "user": "u-vp-ben",
          "context": {"team": "mB"}, "granted_by": "u-op"}, "vp_chef")
ok("Ben bekommt das Trainerteam fuer mB", r.status_code == 201, r.get_json())
a_ben = r.get_json()["assignment"]
ok("granted_by ist der Aufrufer, nicht der Koerper",
   a_ben["granted_by"] == "vp_chef" and a_ben["granted_by_id"] == "u-vp-chef",
   a_ben)
r = post("/internal/authz/assignments",
         {"collection": coll["id"], "user": "u-sgl-max",
          "context": {"team": "mB"}}, "vp_chef")
ok("an eine Benutzer-ID eines ANDEREN Mandanten: 404", r.status_code == 404)
r = post("/internal/authz/assignments",
         {"collection": coll["id"], "user": "ben",
          "context": {"team": "mB"}}, "vp_chef")
ok("ein Benutzername ist keine Benutzer-ID: 404", r.status_code == 404)
r = post("/internal/authz/assignments",
         {"collection": coll["id"], "user": "u-vp-carla"}, "vp_chef")
ok("ohne den geforderten Kontext: 400", r.status_code == 400, r.get_json())
r = post("/internal/authz/assignments",
         {"collection": coll["id"], "user": "u-vp-ben",
          "context": {"team": "mB"}}, "sgl_chef")
ok("eine fremde Sammlung kann der andere Mandant nicht zuordnen",
   r.status_code in (400, 404), r.get_json())
ok("Plattformrollen blieben unberuehrt",
   {u["username"]: list(u["roles"]) for u in m.load_users()}
   == before_roles)

print("Schluessel und effective (Abschnitt 2.4)")
users = m.load_users()
m.AUTHZ_SCOPE
with m.authz_rw() as st:
    st["instances"]["vp-prod"] = {"app": "vereinsportal", "tenant": "t-vp"}
    st["instances"]["andere-app"] = {"app": "andere", "tenant": "t-vp"}
    m.save_authz(st)
rec, key_vp = m.issue_key(users, "instance:vp-prod", ["user"], "oaap.authz",
                          "authz", 90, "root")
rec, key_x = m.issue_key(users, "instance:andere-app", ["user"],
                         "oaap.authz", "authz", 90, "root")
rec, key_noscope = m.issue_key(users, "instance:vp-prod", ["user"], "",
                               "ohne Bereich", 90, "root")
rec, key_twin = m.issue_key(users, "instance:vp-prod", ["user"], "oaap.twin",
                            "twin", 90, "root")
rec, key_unreg = m.issue_key(users, "instance:sgl-prod", ["user"],
                             "oaap.authz", "authz", 90, "root")


def eff(uid, key=None, **kw):
    h = {"Authorization": "Bearer " + key} if key else {}
    return c.get("/authz/effective?user=" + uid, headers=h, **kw)


r = eff("u-vp-ben", key_vp)
g = r.get_json()
ok("die App bekommt Bens aufgeloeste Rechte", r.status_code == 200
   and any(x["object"] == "team" and x["fields"] == {"team": ["mB"]}
           and "edit_lineup" in x["activities"] for x in g["grants"])
   and any(x["object"] == "news" and x["fields"] == {"area": ["news"]}
           for x in g["grants"]), g)
ok("die Antwort nennt App, Mandant und die 30 Sekunden",
   g["app"] == "vereinsportal" and g["tenant"] == "t-vp"
   and g["fresh_for"] == 30)
ok("keine Platzhalter und kein Grund in der Antwort",
   "$context" not in json.dumps(g) and "collection" not in json.dumps(g)
   and "Trainerteam" not in json.dumps(g))
ok("eine Person ohne Zuordnung: leere Liste",
   eff("u-vp-carla", key_vp).get_json()["grants"] == [])
r = eff("u-sgl-max", key_vp)
ok("KOEDER: eine Person eines ANDEREN Mandanten ist unbekannt, nicht leer",
   r.status_code == 404, r.get_json())
r = eff("u-vp-ben", key_x)
ok("der Schluessel einer anderen App sieht nur die Rechte DIESER App",
   r.status_code == 200 and r.get_json()["grants"] == [], r.get_json())
r = eff("u-vp-ben", key_noscope)
ok("ein Schluessel ohne den Bereich kommt nicht durch",
   r.status_code == 403, r.get_json())
r = eff("u-vp-ben", key_twin)
ok("der Schluessel des Zwillings (oaap.twin) kommt nicht durch",
   r.status_code == 403, r.get_json())
r = eff("u-vp-ben", key_unreg)
ok("eine nicht eingetragene Instanz kommt nicht durch",
   r.status_code == 403, r.get_json())
ok("ohne Schluessel: 401 (kein Umweg ueber eine Anmeldeseite)",
   eff("u-vp-ben").status_code == 401)
ok("ein unbekannter Benutzer: 404",
   eff("nix", key_vp).status_code == 404)

print("Widerruf, Protokoll")
r = post("/internal/authz/assignments/" + a_ben["id"], {}, "sgl_chef",
         method="delete")
ok("der andere Mandant kann die Zuordnung nicht beenden",
   r.status_code == 404)
ok("... sie gilt weiter",
   eff("u-vp-ben", key_vp).get_json()["grants"] != [])
r = post("/internal/authz/assignments/" + a_ben["id"], {}, "vp_chef",
         method="delete")
ok("der eigene Mandantenverwalter beendet sie", r.status_code == 200
   and r.get_json()["ended"] is True)
ok("danach hat Ben nichts mehr (sofort)",
   eff("u-vp-ben", key_vp).get_json()["grants"] == [])
ok("die Zuordnung ist beendet markiert, nicht geloescht",
   any(a["id"] == a_ben["id"] and a["ended"] for a in
       c.get("/internal/authz/assignments?actor=vp_chef", headers=H)
       .get_json()["assignments"]))
acts = [e["action"] for e in audit_lines()]
for want in ("authz.role-create", "authz.collection-create", "authz.assign",
             "authz.revoke"):
    ok(f"im Protokoll steht {want}", want in acts, acts)
ok("die Eintraege stehen im Protokoll des Mandanten vp",
   all(e["tenant"] == "t-vp" for e in audit_lines()
       if e["action"].startswith("authz.") and e["action"] != "authz.register"))

print("Zerstoerende Aenderung (Abschnitt 2.2)")
a_ben = post("/internal/authz/assignments",
             {"collection": coll["id"], "user": "u-vp-ben",
              "context": {"team": "mB"}}, "vp_chef").get_json()["assignment"]
d2 = copy.deepcopy(DECL)
d2["objects"][0]["activities"].remove("edit_lineup")
d2["role_templates"][0]["grants"][0]["activities"] = ["read"]
r = post("/internal/authz/register", {"app": "vereinsportal",
                                      "version": "0.2.0", "declaration": d2})
j = r.get_json()
ok("ohne Bestaetigung: 409, nichts geschrieben, Rolle und Zahl genannt",
   r.status_code == 409 and j["impact"]["t-vp"]["roles"] == ["Trainer Jugend"]
   and j["impact"]["t-vp"]["assignments"] == 1, j)
ok("die alte Erklaerung gilt weiter",
   m.load_authz()["declarations"]["vereinsportal"]["version"] == "0.1.0")
r = post("/internal/authz/register", {"app": "vereinsportal",
                                      "version": "0.2.0", "declaration": d2,
                                      "confirm": True})
ok("mit Bestaetigung wird geschrieben", r.status_code == 200
   and m.load_authz()["declarations"]["vereinsportal"]["version"] == "0.2.0")
ok("das Protokoll nennt die betroffene Rolle",
   any(e["action"] == "authz.register" and "Trainer Jugend"
       in e.get("detail", "") for e in audit_lines()))
ok("Ben darf nun nicht mehr aufstellen (nur noch lesen)",
   all("edit_lineup" not in x["activities"] for x in
       eff("u-vp-ben", key_vp).get_json()["grants"]
       if x["object"] == "team"))

print("Die Gruppen des Anbieters (RFC-0045 Abschnitt 5)")
PROVIDER = {"kind": "oidc", "issuer": "https://kc.example/realms/vp",
            "client_id": "oaap-x", "connector": "kc", "space": "vp"}
tj = json.load(open(m.TENANTS_FILE, encoding="utf-8"))
tj["tenants"]["t-vp"]["idp"] = dict(PROVIDER)
json.dump(tj, open(m.TENANTS_FILE, "w", encoding="utf-8"))
r = post("/internal/authz/mappings", {"group": "Verein/Hallenwart",
                                      "collection": "Trainerteam"}, "vp_chef")
ok("KOEDER: eine Sammlung mit Kontext kann keine Gruppe geben (400)",
   r.status_code == 400 and "context" in r.get_json()["error"], r.get_json())
r = post("/internal/authz/roles", {"app": "vereinsportal",
                                   "template": "bereich", "name": "News lesen",
                                   "values": {"area": ["sponsoring"]}}, "vp_chef")
r = post("/internal/authz/collections", {"name": "Hallenwarte",
                                         "roles": ["News lesen"]}, "vp_chef")
coll_hw = r.get_json()["collection"]
r = post("/internal/authz/mappings", {"group": "/Verein/Hallenwart",
                                      "collection": "Hallenwarte"}, "ben")
ok("ein einfacher Benutzer darf nichts abbilden", r.status_code == 403)
r = post("/internal/authz/mappings", {"group": "/Verein/Hallenwart",
                                      "collection": "Hallenwarte"}, "vp_chef")
ok("der Mandantenverwalter bildet die Gruppe ab", r.status_code == 201
   and r.get_json()["mapping"]["group"] == "Verein/Hallenwart", r.get_json())
mapping = r.get_json()["mapping"]
r = c.get("/internal/authz/mappings?actor=sgl_chef", headers=H)
ok("der andere Mandant sieht die Abbildung nicht",
   r.get_json()["mappings"] == [], r.get_json())
r = post("/internal/authz/mappings/" + mapping["id"], {}, "sgl_chef",
         method="delete")
ok("... und kann sie nicht entfernen", r.status_code == 404)


def login(sub, groups):
    claims = {"sub": sub, "preferred_username": sub, "email": sub + "@x.example",
              "email_verified": True, "name": sub.title()}
    if groups is not None:
        claims["groups"] = groups
    u, bad = m._idp_principal("t-vp", PROVIDER, claims)
    return u, bad


u1, bad = login("hanna", ["/Verein/Hallenwart", "/Egal"])
ok("die erste Anmeldung ueber den Anbieter gelingt", u1 and not bad, bad)
rec, key_vp2 = m.issue_key(m.load_users(), "instance:vp-prod", ["user"],
                           "oaap.authz", "authz", 90, "root")


def grants_of(uid):
    return eff(uid, key_vp2).get_json()["grants"]


ok("schon beim ERSTEN Login gilt die Gruppe: die App sieht das Recht",
   any(x["object"] == "news" and x["fields"] == {"area": ["sponsoring"]}
       for x in grants_of(u1["id"])), grants_of(u1["id"]))
roles_first = list(u1["roles"])
u1b, _ = login("hanna", ["/Verein/Hallenwart"])
ok("zweiter Login mit derselben Gruppe: eine Zuordnung, nicht zwei",
   sum(1 for a in m.load_authz()["assignments"].values()
       if a["subject"] == u1["id"] and not a["ended"]) == 1)
u1c, _ = login("hanna", ["/Andere"])
ok("KOEDER: Gruppe verlassen -> beim naechsten Login ist das Recht weg",
   grants_of(u1["id"]) == [])
ok("PLATTFORMROLLEN bleiben, wie die Richtlinie sie gab -- keine Gruppe aendert sie",
   list(u1c["roles"]) == roles_first
   and "tenant_admin" not in u1c["roles"]
   and "server_admin" not in u1c["roles"], (roles_first, u1c["roles"]))
u1d, _ = login("hanna", None)
ok("ein Token ohne groups-Anspruch gibt kein Recht",
   grants_of(u1["id"]) == [])
login("hanna", ["/Verein/Hallenwart"])
ok("KOEDER: ein Benutzer, der sich Gruppennamen ausdenkt, die niemand abbildete, bekommt nichts",
   grants_of(login("mallory", ["/Verein/Hallenwart2", "tenant_admin",
                               "server_admin", "/tenant_admin"])[0]["id"])
   == [])
um, _ = login("mallory", ["tenant_admin", "server_admin"])
ok("... und seine Plattformrollen sind nicht die Namen der Gruppen",
   "tenant_admin" not in um["roles"] and "server_admin" not in um["roles"],
   um["roles"])
acts = [e["action"] for e in audit_lines()]
ok("im Protokoll: Abbildung und Abgleich beim Login",
   "authz.mapping-add" in acts and "authz.idp-sync" in acts, acts)
ok("der Abgleich steht im Protokoll des Mandanten vp",
   all(e["tenant"] == "t-vp" for e in audit_lines()
       if e["action"] == "authz.idp-sync"))

real_sync = m.authz.sync_login


def boom(*a, **k):
    raise RuntimeError("Platte voll")


m.authz.sync_login = boom
uu, bad = login("hanna", ["/Verein/Hallenwart"])
m.authz.sync_login = real_sync
ok("ein Fehler beim Abgleich sperrt die Anmeldung NICHT aus", uu and not bad,
   bad)

r = post("/internal/authz/mappings/" + mapping["id"], {}, "vp_chef",
         method="delete")
ok("die Abbildung entfernen beendet sofort, was sie gab", r.status_code == 200
   and r.get_json()["ended"] >= 1 and grants_of(u1["id"]) == [], r.get_json())

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
