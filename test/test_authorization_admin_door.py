#!/usr/bin/env python3
"""Die Verwaltungs-Tuer fuer Apps (oaap.core.authorization 0.3, RFC-0045 A7).

Bis 0.2 oeffnete die Verwaltung nur dem Schluessel des Hosts. Jetzt gibt
es EINE weitere Tuer fuer eine App, die der Betreiber bestaetigt hat. Das
ist eine privilegierte Tuer, und dieser Test fragt, was sie NICHT tut:

    - ohne Schluessel, mit dem Schluessel der Antwort-Tuer (oaap.authz),
      mit einem Schluessel ohne Bereich, mit einer Instanz, der der Host die
      Tuer nicht freigegeben hat: nichts kommt hinein;
    - die PERSON wird von Identity geprueft, nicht der App geglaubt: ein
      normaler Benutzer, ein tenant_admin eines ANDEREN Mandanten, ein
      Maschinenkonto, ein gesperrter Admin -- alle 403, nichts geschrieben;
    - der Mandant ist der des Schluessels, auch fuer einen server_admin, und
      auch wenn der Koerper einen anderen nennt;
    - `granted_by` und das Protokoll nennen die Person, nicht die App;
    - `register`, `instance` und alles, was nicht in der Liste steht, gibt
      es hier nicht (404, nichts angelegt);
    - eine Plattformrolle kommt nirgends vor und bleibt unberuehrt;
    - `retire` loescht nichts und lehnt ab, solange etwas Lebendiges darauf
      steht -- und nennt, was.

Aufruf: python3 test/test_authorization_admin_door.py
"""
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
        print(f"      {str(detail)[:600]}")


try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-authz-door-test-")
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


def user(uid, name, roles, tenant, kind="human", active=True):
    return {"id": uid, "username": name, "display_name": name.title(),
            "password_hash": "geheim-hash", "kind": kind, "roles": roles,
            "groups": [], "tenant": tenant, "active": active,
            "session_epoch": 0}


USERS = [
    user("u-op", "joerg", ["server_admin", "admin", "user"], "t-default"),
    user("u-vp-chef", "vp_chef", ["tenant_admin", "user"], "t-vp"),
    user("u-vp-alt", "vp_alt", ["tenant_admin", "user"], "t-vp", active=False),
    user("u-vp-ben", "ben", ["user"], "t-vp"),
    user("u-vp-carla", "carla", ["user"], "t-vp"),
    user("u-sgl-chef", "sgl_chef", ["tenant_admin", "user"], "t-sgl"),
    user("u-sgl-max", "max", ["user"], "t-sgl"),
    user("u-inst-vp", "instance:vp-prod", ["user"], "t-vp", "machine"),
    user("u-inst-adm", "instance:rollen-rechte", ["user"], "t-vp", "machine"),
    user("u-inst-zu", "instance:zu", ["user"], "t-vp", "machine"),
    user("u-inst-boss", "instance:boss", ["tenant_admin", "user"], "t-vp",
         "machine"),
]
with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(USERS, f)
PLATFORM_ROLES = {u["username"]: list(u["roles"]) for u in USERS}

c = m.app.test_client()
H = {"X-OAAP-Internal-Key": "test-internal-key"}

DECL = {
    "objects": [
        {"key": "news", "title": "News", "activities": ["read", "publish"],
         "fields": [{"key": "area", "values": ["news", "sponsoring"]}]},
    ],
    "role_templates": [
        {"key": "bereich", "title": "Bereich",
         "grants": [{"object": "news", "activities": ["read", "publish"],
                     "area": "$value"}]},
    ],
}
r = c.post("/internal/authz/register", headers=H, json={
    "app": "vereinsportal", "version": "0.1.0", "declaration": DECL})
assert r.status_code == 200, r.get_json()

users = m.load_users()
with m.authz_rw() as st:
    st["instances"]["vp-prod"] = {"app": "vereinsportal", "tenant": "t-vp"}
    st["instances"]["rollen-rechte"] = {"app": "rollen-rechte",
                                        "tenant": "t-vp", "administer": True}
    st["instances"]["zu"] = {"app": "rollen-rechte", "tenant": "t-vp"}
    m.save_authz(st)
_r, KEY = m.issue_key(users, "instance:rollen-rechte", ["user"],
                      "oaap.authz.admin", "admin", 90, "root")
_r, KEY_ZU = m.issue_key(users, "instance:zu", ["user"],
                         "oaap.authz.admin", "admin", 90, "root")
_r, KEY_READ = m.issue_key(users, "instance:rollen-rechte", ["user"],
                           "oaap.authz", "authz", 90, "root")
_r, KEY_NOSCOPE = m.issue_key(users, "instance:rollen-rechte", ["user"], "",
                              "ohne Bereich", 90, "root")


def door(method, path, who="u-vp-chef", key=KEY, body=None, args=""):
    """Ein Aufruf durch die Tuer, wie ihn die App macht."""
    h = {"Authorization": "Bearer " + key} if key else {}
    url = "/authz/admin/" + path
    if method == "get":
        q = (f"on_behalf_of={who}" if who else "") + args
        return c.get(url + ("?" + q if q else ""), headers=h)
    b = dict(body or {})
    if who:
        b["on_behalf_of"] = who
    return getattr(c, method)(url, headers=h, json=b)


def audit_lines():
    try:
        with open(m.AUDIT_LOG, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def state():
    return json.loads(json.dumps(m.load_authz()))


print("Wer hineinkommt: der Schluessel")
before = state()
r = door("get", "roles", key=None)
ok("ohne Schluessel: 401, kein Umweg ueber eine Anmeldeseite",
   r.status_code == 401, r.status_code)
r = door("get", "roles", key=KEY_READ)
ok("KOEDER: der Schluessel der Antwort-Tuer (oaap.authz) kommt nicht hinein",
   r.status_code in (401, 403), r.status_code)
r = door("get", "roles", key=KEY_NOSCOPE)
ok("KOEDER: ein Schluessel ohne Bereich kommt nicht hinein",
   r.status_code in (401, 403), r.status_code)
r = door("get", "roles", key=KEY_ZU)
ok("KOEDER: eine Instanz, der der Host die Tuer NICHT freigab, kommt nicht "
   "hinein (auch mit Schluessel des richtigen Bereichs)",
   r.status_code == 403, r.get_json())
r = c.get("/authz/effective?user=u-vp-ben",
          headers={"Authorization": "Bearer " + KEY})
ok("KOEDER: und der Verwaltungsschluessel beantwortet keine Rechtefrage",
   r.status_code in (401, 403), r.status_code)
ok("nichts wurde geschrieben", state() == before)

print("Wer handelt: die Person, von Identity geprueft")
r = door("get", "roles", who=None)
ok("ohne on_behalf_of: 400", r.status_code == 400, r.get_json())
for who, label in (("u-vp-ben", "ein normaler Benutzer"),
                   ("u-sgl-chef", "der tenant_admin eines ANDEREN Mandanten"),
                   ("u-inst-vp", "ein Maschinenkonto"),
                   ("u-inst-boss", "ein Maschinenkonto, das selbst tenant_admin traegt"),
                   ("u-vp-alt", "ein gesperrter tenant_admin"),
                   ("u-gibt-es-nicht", "eine erfundene ID")):
    n_audit = len(audit_lines())
    r = door("post", "roles", who=who, body={
        "app": "vereinsportal", "template": "bereich", "name": "Schleichweg",
        "values": {"area": ["news"]}})
    ok(f"KOEDER: {label} -- 403 und nichts geschrieben",
       r.status_code == 403 and state() == before
       and len(audit_lines()) == n_audit, r.get_json())

print("Was der Mandanten-Admin sieht")
r = door("get", "declarations")
ok("die Deklarationen der Apps des Mandanten", r.status_code == 200
   and [d["app"] for d in r.get_json()["declarations"]] == ["vereinsportal"],
   r.get_json())
r = door("get", "users")
us = r.get_json()["users"] if r.status_code == 200 else []
ok("die Personen des Mandanten, sonst keine", {u["username"] for u in us}
   == {"vp_chef", "vp_alt", "ben", "carla"}, us)
ok("keine Maschinenkonten, kein Hash, keine Plattformrolle, keine Mail",
   all(set(u) == {"id", "username", "display_name", "active"} for u in us)
   and "geheim" not in json.dumps(us))

print("Was er baut -- und mit wessen Namen")
r = door("post", "roles", body={
    "app": "vereinsportal", "template": "bereich", "name": "News-Redaktion",
    "values": {"area": ["news"]}, "granted_by": "boeser"})
ok("eine Rolle anlegen", r.status_code == 201, r.get_json())
rid = r.get_json()["role"]["id"]
ok("die Rolle gehoert dem Mandanten des Schluessels",
   state()["roles"][rid]["tenant"] == "t-vp")
r = door("post", "collections", body={"name": "Redaktion",
                                       "roles": ["News-Redaktion"]})
cid = r.get_json()["collection"]["id"]
r = door("post", "assignments", body={
    "collection": "Redaktion", "user": "u-vp-ben", "granted_by": "boeser",
    "tenant": "t-sgl"})
ok("eine Zuordnung geben", r.status_code == 201, r.get_json())
aid = r.get_json()["assignment"]["id"]
a = state()["assignments"][aid]
ok("granted_by ist die Person, nicht der Koerper und nicht die App",
   a["granted_by"] == "vp_chef", a)
ok("der Mandant im Koerper ('t-sgl') wurde ignoriert", a["tenant"] == "t-vp")
last = [e for e in audit_lines() if e["action"] == "authz.assign"][-1]
ok("das Protokoll nennt die Person und die Instanz",
   last["who"] == "vp_chef" and last["role"] == "tenant_admin"
   and "rollen-rechte" in last.get("detail", ""), last)
r = door("post", "assignments", body={"collection": "Redaktion",
                                       "user": "u-sgl-max"})
ok("KOEDER: eine Person eines anderen Mandanten bekommt nichts",
   r.status_code == 404, r.get_json())

print("Ein server_admin ist hier eine Person, kein Weg in einen anderen Mandanten")
r = door("post", "roles", who="u-op", body={
    "app": "vereinsportal", "template": "bereich", "name": "Vom Betreiber",
    "values": {"area": ["sponsoring"]}, "tenant": "t-sgl"})
ok("er darf (Person der Plattform)", r.status_code == 201, r.get_json())
ok("... aber im Mandanten des Schluessels, nicht im genannten und nicht im "
   "eigenen", state()["roles"][r.get_json()["role"]["id"]]["tenant"] == "t-vp")

print("Was es durch diese Tuer nicht gibt")
n_inst = len(state()["instances"])
for method, path in (("post", "register"), ("post", "instance"),
                     ("get", "keys"), ("post", "users"),
                     ("delete", "roles/" + rid)):
    r = door(method, path, body={"app": "x", "tenant": "t-vp",
                                 "instance": "neu", "administer": True,
                                 "declaration": DECL})
    ok(f"{method.upper()} /{path}: 404", r.status_code == 404, r.status_code)
ok("nichts angelegt, keine Instanz freigegeben, Deklarationen unveraendert",
   len(state()["instances"]) == n_inst
   and state()["declarations"] == before["declarations"])

print("Wer darf was -- mit Herkunft")
r = door("get", "effective", args="&user=u-vp-ben")
g = r.get_json() if r.status_code == 200 else {}
ok("Bens Rechte je App, aufgeloest", r.status_code == 200 and any(
    x["app"] == "vereinsportal" and any(
        y["object"] == "news" and y["fields"] == {"area": ["news"]}
        for y in x["grants"]) for x in g["apps"]), g)
row = next((x for x in g.get("assignments", []) if x["id"] == aid), {})
ok("und woher: Sammlung, Rollen, Geber, 'lebt'",
   row.get("collection_name") == "Redaktion"
   and row.get("roles") == ["News-Redaktion"]
   and row.get("granted_by") == "vp_chef" and row.get("live") is True, row)
r = door("get", "effective", args="&user=u-sgl-max")
ok("KOEDER: eine Person eines anderen Mandanten ist unbekannt (404)",
   r.status_code == 404, r.get_json())
r = door("get", "log")
lg = r.get_json()["log"] if r.status_code == 200 else []
ok("das Protokoll: nur authz.*, nur dieser Mandant, neueste zuerst",
   lg and all(e["action"].startswith("authz.") and e["tenant"] == "t-vp"
              for e in lg) and lg[0]["when"] >= lg[-1]["when"], lg[:2])

print("retire: nichts wird geloescht, und was noch gebraucht wird, bleibt")
r = door("post", "roles/News-Redaktion/retire")
ok("KOEDER: eine Rolle in einer Sammlung im Gebrauch wird abgelehnt und "
   "nennt die Sammlung", r.status_code == 409
   and "Redaktion" in r.get_json()["error"], r.get_json())
r = door("post", "collections/Redaktion/retire")
ok("KOEDER: eine Sammlung mit lebender Zuordnung wird abgelehnt und nennt sie",
   r.status_code == 409 and "live assignment" in r.get_json()["error"],
   r.get_json())
r = door("post", "mappings", body={"group": "Verein/Redaktion",
                                    "collection": "Redaktion"})
ok("eine Abbildung auf die Sammlung", r.status_code == 201, r.get_json())
mid = r.get_json()["mapping"]["id"]
r = door("delete", "assignments/" + aid)
ok("die Zuordnung beenden", r.status_code == 200, r.get_json())
r = door("post", "collections/Redaktion/retire")
ok("KOEDER: mit einer Abbildung bleibt die Sammlung (nennt die Gruppe)",
   r.status_code == 409 and "Verein/Redaktion" in r.get_json()["error"],
   r.get_json())
door("delete", "mappings/" + mid)
r = door("post", "collections/Redaktion/retire")
ok("jetzt geht es", r.status_code == 200 and r.get_json()["changed"] is True,
   r.get_json())
ok("der Datensatz bleibt, markiert", state()["collections"][cid]["retired"]
   ["by"] == "vp_chef" and cid in state()["collections"])
ok("noch einmal: ohne Folgen", door("post", "collections/Redaktion/retire")
   .get_json()["changed"] is False)
r = door("post", "assignments", body={"collection": "Redaktion",
                                       "user": "u-vp-carla"})
ok("KOEDER: eine ausgeblendete Sammlung wird nicht mehr vergeben",
   r.status_code == 400 and "retired" in r.get_json()["error"], r.get_json())
r = door("post", "mappings", body={"group": "Verein/X",
                                    "collection": "Redaktion"})
ok("KOEDER: und nicht mehr abgebildet",
   r.status_code == 400 and "retired" in r.get_json()["error"], r.get_json())
r = door("post", "roles/News-Redaktion/retire")
ok("jetzt darf die Rolle gehen", r.status_code == 200, r.get_json())
r = door("post", "collections", body={"name": "Neu",
                                       "roles": ["News-Redaktion"]})
ok("KOEDER: eine ausgeblendete Rolle kommt in keine neue Sammlung",
   r.status_code == 400 and "retired" in r.get_json()["error"], r.get_json())
r = door("post", "roles", body={
    "app": "vereinsportal", "template": "bereich", "name": "News-Redaktion",
    "values": {"area": ["news"]}})
ok("KOEDER: der Name einer ausgeblendeten Rolle bleibt vergeben -- und die "
   "Meldung SAGT, dass sie ausgeblendet ist", r.status_code == 400
   and "retired" in r.get_json()["error"], r.get_json())
r = door("post", "collections", body={"name": "Redaktion",
                                       "roles": ["Vom Betreiber"]})
ok("dasselbe fuer eine ausgeblendete Sammlung", r.status_code == 400
   and "retired" in r.get_json()["error"], r.get_json())
r = door("post", "roles/gibt-es-nicht/retire")
ok("eine unbekannte Rolle: 404", r.status_code == 404)
r = door("post", "roles/" + rid + "/retire", who="u-vp-ben")
ok("KOEDER: ein normaler Benutzer blendet nichts aus", r.status_code == 403)
r = c.post("/internal/authz/roles/Vom Betreiber/retire", headers=H,
           json={"actor": "root", "operator": True, "tenant": "t-vp"})
ok("auch der Host kann es (dieselbe Funktion)", r.status_code == 200,
   r.get_json())
ok("im Protokoll: authz.role-retire und authz.collection-retire mit Person",
   any(e["action"] == "authz.collection-retire" and e["who"] == "vp_chef"
       for e in audit_lines())
   and any(e["action"] == "authz.role-retire" and e["who"] == "vp_chef"
           for e in audit_lines()))

print("Die Tuer geht wieder zu")
r = c.post("/internal/authz/instance", headers=H, json={
    "instance": "rollen-rechte", "app": "rollen-rechte", "tenant": "t-vp"})
ok("der Host traegt die Instanz OHNE Freigabe neu ein", r.status_code == 200)
r = door("get", "roles")
ok("KOEDER: dieselbe Instanz, derselbe Schluessel -- jetzt zu (403)",
   r.status_code == 403, r.get_json())

print("Plattformrollen")
ok("keine einzige Plattformrolle hat sich durch all das geaendert",
   {u["username"]: list(u["roles"]) for u in m.load_users()}
   == PLATFORM_ROLES)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
