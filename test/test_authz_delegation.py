#!/usr/bin/env python3
"""Weitergabe und Agenten (oaap.core.authorization 0.5, RFC-0067).

Die Tuer steht jedem offen, der einen gewoehnlichen Schluessel einer App hat.
Dieser Test fragt deshalb, was sie NICHT tut:

    - ohne Nachweis, mit abgelaufenem, fremdem, verfaelschtem Nachweis: 401,
      nichts geschrieben; ein `on_behalf_of` im Koerper zaehlt nicht;
    - niemand gibt weiter, was er nicht haelt: Trainer von mB nicht in mC, keine
      ungelistete Vorlage, keine fremde App, nicht ueber das eigene Ende hinaus;
    - KOEDER: ein kontextgebundener Halter gibt kein kontextfreies Paket weiter,
      und keinen Wert, den er selbst nicht hat;
    - niemand vergibt sich selbst etwas;
    - eine Maschine bekommt nie ein Paket mit may_grant, an KEINER Tuer;
    - Beenden: im eigenen Kontext auch Fremdes, ausserhalb nichts, und was eine
      Gruppe gab nicht (der naechste Login braechte es zurueck);
    - das Geloeschte: ein geloeschter Benutzer haelt nichts Lebendiges mehr;
    - ein API-Schluessel einer PERSON bekommt keine Zusicherung (Spec 2.11);
    - Plattformrollen bleiben unberuehrt.

Aufruf: python3 test/test_authz_delegation.py
"""
import importlib
import json
import os
import sys
import tempfile
import time

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

DATA = tempfile.mkdtemp(prefix="oaap-authz-delegate-test-")
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
m.ASSERTION_KEY_FILE = os.path.join(DATA, "assertion.key")
with open(m.ASSERTION_KEY_FILE, "w", encoding="ascii") as f:
    f.write("ab" * 32)
PROOF_KEY = bytes.fromhex("ab" * 32)
az = m.authz
assertion = m.assertion

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
    user("u-chef", "vp_chef", ["tenant_admin", "user"], "t-vp"),
    user("u-ben", "ben", ["user"], "t-vp"),
    user("u-carla", "carla", ["user"], "t-vp"),
    user("u-dora", "dora", ["user"], "t-vp"),
    user("u-gesperrt", "gesperrt", ["user"], "t-vp", active=False),
    user("u-max", "max", ["user"], "t-sgl"),
    user("u-agent", "agent-a", ["user"], "t-vp", "machine"),
    user("u-inst", "instance:vp-prod", ["user"], "t-vp", "machine"),
    user("u-inst2", "instance:anders", ["user"], "t-vp", "machine"),
    user("u-inst3", "instance:rechte", ["user"], "t-vp", "machine"),
]
with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(USERS, f)
PLATFORM_ROLES = {u["username"]: list(u["roles"]) for u in USERS}

c = m.app.test_client()
H = {"X-OAAP-Internal-Key": "test-internal-key"}

DECL = {
    "objects": [
        {"key": "team", "title": "Mannschaft",
         "activities": ["read", "edit"],
         "fields": [{"key": "team", "context": "Mannschaft"}]},
        {"key": "news", "title": "News", "activities": ["read", "publish"],
         "fields": [{"key": "area", "values": ["news", "sponsoring"]}]},
    ],
    "role_templates": [
        {"key": "trainer", "title": "Trainer",
         "grants": [{"object": "team", "activities": ["read", "edit"],
                     "team": "$context"}],
         "may_grant": ["co", "leser", "chef2"]},
        {"key": "co", "title": "Co",
         "grants": [{"object": "team", "activities": ["read"],
                     "team": "$context"}]},
        {"key": "chef2", "title": "Unter-Trainer", "may_grant": ["co"],
         "grants": [{"object": "team", "activities": ["read"],
                     "team": "$context"}]},
        {"key": "leser", "title": "Leser ohne Kontext",
         "grants": [{"object": "team", "activities": ["read"]}]},
        {"key": "fremd", "title": "Nicht gelistet",
         "grants": [{"object": "team", "activities": ["edit"],
                     "team": "$context"}]},
        {"key": "ressort", "title": "Ressort", "may_grant": ["hilfe"],
         "grants": [{"object": "news", "activities": ["publish"],
                     "area": "$value"}]},
        {"key": "hilfe", "title": "Ressorthilfe",
         "grants": [{"object": "news", "activities": ["read"],
                     "area": "$value"}]},
    ],
}
r = c.post("/internal/authz/register", headers=H, json={
    "app": "vereinsportal", "version": "0.1.0", "declaration": DECL})
assert r.status_code == 200, r.get_json()
ANDERE = {"objects": [{"key": "x", "title": "X", "activities": ["read"],
                       "fields": [{"key": "team", "context": "Mannschaft"}]}],
          "role_templates": [{"key": "co", "title": "Co anderswo",
                              "grants": [{"object": "x", "activities": ["read"],
                                          "team": "$context"}]}]}
r = c.post("/internal/authz/register", headers=H, json={
    "app": "andere-app", "version": "0.1.0", "declaration": ANDERE})
assert r.status_code == 200, r.get_json()

users = m.load_users()
with m.authz_rw() as st:
    st["instances"]["vp-prod"] = {"app": "vereinsportal", "tenant": "t-vp"}
    st["instances"]["anders"] = {"app": "andere-app", "tenant": "t-vp"}
    st["instances"]["rechte"] = {"app": "rollen-rechte", "tenant": "t-vp",
                                 "administer": True}
    C = {}
    for tpl, name, vals, app in (
            ("trainer", "Trainer", {}, "vereinsportal"),
            ("co", "Co", {}, "vereinsportal"),
            ("chef2", "Untertrainer", {}, "vereinsportal"),
            ("leser", "Leser", {}, "vereinsportal"),
            ("fremd", "Fremd", {}, "vereinsportal"),
            ("ressort", "Ressort News", {"area": ["news"]}, "vereinsportal"),
            ("hilfe", "Hilfe News", {"area": ["news"]}, "vereinsportal"),
            ("hilfe", "Hilfe Sponsoring", {"area": ["sponsoring"]},
             "vereinsportal"),
            ("co", "Co anderswo", {}, "andere-app")):
        rid = az.create_role(st, "t-vp", app, tpl, "R " + name, vals, "test")
        C[name] = az.create_collection(st, "t-vp", name, [rid], "test")
    A_BEN = az.assign(st, "t-vp", C["Trainer"], "u-ben", {"team": "mB"},
                      valid_to="2099-06-30", granted_by="vp_chef")
    az.assign(st, "t-vp", C["Ressort News"], "u-dora", granted_by="vp_chef")
    m.save_authz(st)
_r, KEY = m.issue_key(users, "instance:vp-prod", ["user"], "oaap.authz",
                      "authz", 90, "root")
_r, KEY2 = m.issue_key(users, "instance:anders", ["user"], "oaap.authz",
                       "authz", 90, "root")
_r, KEY_NOSCOPE = m.issue_key(users, "instance:vp-prod", ["user"], "",
                              "ohne Bereich", 90, "root")
_r, KEY_ADMIN = m.issue_key(users, "instance:rechte", ["user"],
                            "oaap.authz.admin", "admin", 90, "root")


def proof(uid="u-ben", name="ben", tenant="t-vp", aud="vp-prod", now=None,
          key=PROOF_KEY):
    return assertion.mint(key, uid, name, tenant, aud, now=now)


def door(method, path, prf=None, key=KEY, body=None):
    h = {"Authorization": "Bearer " + key} if key else {}
    h[m.AUTHZ_PROOF_HEADER] = proof() if prf is None else prf
    return getattr(c, method)("/authz/delegate/" + path, headers=h, json=body)


def assignments():
    return m.load_authz()["assignments"]


def give(coll, target="u-carla", ctx=None, end=None, **kw):
    return door("post", "assignments", body={
        "collection": C.get(coll, coll), "user": target,
        "context": {"team": "mB"} if ctx is None else ctx,
        "valid_to": end}, **kw)


# --- wer kommt hinein -------------------------------------------------------
n = len(assignments())
r = c.get("/authz/delegate/grantable",
          headers={m.AUTHZ_PROOF_HEADER: proof()})
ok("ohne Schluessel: 401", r.status_code == 401, r.get_json())
ok("Schluessel ohne Bereich: 403",
   door("get", "grantable", key=KEY_NOSCOPE).status_code == 403)
ok("Schluessel der Verwaltungstuer: 403",
   door("get", "grantable", key=KEY_ADMIN).status_code == 403)
ok("ohne Nachweis: 401", give("Co", prf="").status_code == 401)
ok("abgelaufener Nachweis: 401",
   give("Co", prf=proof(now=time.time() - 3600)).status_code == 401)
ok("Nachweis fuer eine andere Instanz: 401",
   give("Co", prf=proof(aud="anders")).status_code == 401)
ok("Nachweis eines anderen Mandanten: 401",
   give("Co", prf=proof("u-max", "max", "t-sgl")).status_code == 401)
ok("Nachweis mit fremdem Schluessel: 401",
   give("Co", prf=proof(key=b"x" * 32)).status_code == 401)
t = proof().split(".")
ok("verfaelschter Nachweis: 401",
   give("Co", prf=t[0] + "." + t[1] + "A." + t[2]).status_code == 401)
ok("gesperrte Person: 403",
   give("Co", prf=proof("u-gesperrt", "gesperrt")).status_code == 403)
ok("Maschine mit Nachweis (kann es gar nicht geben): 403",
   give("Co", prf=proof("u-agent", "agent-a")).status_code == 403)
r = door("post", "assignments", prf="", body={
    "collection": C["Co"], "user": "u-carla", "context": {"team": "mB"},
    "on_behalf_of": "u-ben"})
ok("KOEDER: on_behalf_of ohne Nachweis zaehlt nicht: 401", r.status_code == 401)
ok("nichts davon hat geschrieben", len(assignments()) == n)
ok("andere Pfade gibt es nicht: 404",
   door("post", "roles", body={}).status_code == 404)

# --- was weitergegeben werden darf ------------------------------------------
r = door("get", "grantable")
g = r.get_json()["grantable"]
names = {x["name"] for row in g for x in row["collections"]}
ok("grantable nennt die gelisteten Pakete mit Kontext",
   {"Co", "Untertrainer"} <= names, g)
ok("grantable nennt Kontextfreies und Ungelistetes nicht",
   not names & {"Leser", "Fremd", "Trainer", "Co anderswo"}, names)
ok("grantable nennt Kontext und Ende des Halters",
   g[0]["context"] == {"team": ["mB"]} and g[0]["valid_to"] == "2099-06-30", g)
ok("wer nichts weitergeben darf, bekommt eine leere Liste",
   door("get", "grantable", prf=proof("u-carla", "carla")).get_json()
   == {"grantable": []})

r = give("Co", ctx={"team": "mC"})
ok("Trainer von mB nicht in mC: 403", r.status_code == 403, r.get_json())
r = give("Co", ctx={"team": ["mB", "mC"]})
ok("KOEDER: mB und mC zusammen auch nicht: 403", r.status_code == 403)
ok("nicht gelistete Vorlage: 403", give("Fremd").status_code == 403)
r = give("Leser", ctx={})
ok("KOEDER: kontextfreies Paket vom kontextgebundenen Halter: 403",
   r.status_code == 403 and "further" in r.get_json()["error"], r.get_json())
ok("Paket einer anderen App: 403", give("Co anderswo").status_code == 403)
ok("Ende hinter dem eigenen: 403",
   give("Co", end="2099-07-01").status_code == 403)
ok("sich selbst: 403", give("Co", target="u-ben").status_code == 403)
ok("Ziel in einem anderen Mandanten: 404",
   give("Co", target="u-max").status_code == 404)
ok("unbekanntes Paket: 403", give("gibt-es-nicht").status_code == 403)
ok("ohne Kontext, wo das Paket einen braucht: 403",
   give("Co", ctx={}).status_code == 403)
ok("Person ohne may_grant, gueltiger Nachweis: 403",
   give("Co", prf=proof("u-carla", "carla"), target="u-dora").status_code == 403)
ok("die App spielt keine Rolle: dieselbe Person an der anderen App: 403",
   door("post", "assignments", key=KEY2, prf=proof(aud="anders"), body={
       "collection": C["Co anderswo"], "user": "u-carla",
       "context": {"team": "mB"}}).status_code == 403)
ok("nichts davon hat geschrieben (2)", len(assignments()) == n)

r = give("Co", end="2099-01-31")
a1 = r.get_json().get("assignment") or {}
ok("Trainer von mB gibt Co in mB weiter: 201",
   r.status_code == 201 and a1["context"] == {"team": ["mB"]}, r.get_json())
ok("granted_by ist die Person, nicht die App",
   a1.get("granted_by") == "ben" and a1.get("granted_by_id") == "u-ben", a1)
ok("Herkunft delegated", a1.get("source") == "delegated", a1)
ok("eigenes Ende bleibt, und die Antwort behauptet nichts anderes",
   a1.get("valid_to") == "2099-01-31"
   and r.get_json()["end_set_by_holder"] is False, r.get_json())
r = give("Co", target="u-dora")
a2 = r.get_json()["assignment"]
ok("ohne Ende gilt das des Halters, und die Antwort sagt es",
   a2["valid_to"] == "2099-06-30" and r.get_json()["end_set_by_holder"] is True,
   r.get_json())
with open(m.AUDIT_LOG, encoding="utf-8") as f:
    log = f.read()
ok("das Protokoll nennt die Person und die Instanz",
   "authz.delegate-assign" in log and "ben" in log and "vp-prod" in log, log[-300:])

ok("wirkt sofort: effective zeigt mB",
   any(x["fields"].get("team") == ["mB"] for x in az.effective(
       m.load_authz(), "t-vp", "vereinsportal", "u-carla")))

# Kette: so tief wie erklaert
r = give("Untertrainer")
ok("gelistetes may_grant darf weiter", r.status_code == 201, r.get_json())
r = give("Co", target="u-dora", prf=proof("u-carla", "carla"))
ok("Kette: die Untertrainerin gibt Co in mB weiter",
   r.status_code == 201, r.get_json())
r = give("Untertrainer", target="u-dora", prf=proof("u-carla", "carla"))
ok("Kette endet, wo die Erklaerung endet: Untertrainer gibt keinen Untertrainer",
   r.status_code == 403, r.get_json())

# Werte, nicht nur Kontexte
r = door("post", "assignments", prf=proof("u-dora", "dora"), body={
    "collection": C["Hilfe News"], "user": "u-carla", "context": {}})
ok("Ressort News gibt Hilfe News weiter", r.status_code == 201, r.get_json())
r = door("post", "assignments", prf=proof("u-dora", "dora"), body={
    "collection": C["Hilfe Sponsoring"], "user": "u-carla", "context": {}})
ok("KOEDER: Ressort News gibt keine Hilfe Sponsoring weiter",
   r.status_code == 403 and "value" in r.get_json()["error"], r.get_json())
names = {x["name"] for row in door("get", "grantable",
                                   prf=proof("u-dora", "dora")).get_json()
         ["grantable"] for x in row["collections"]}
ok("grantable sagt dasselbe wie die Tuer",
   "Hilfe News" in names and "Hilfe Sponsoring" not in names, names)

# --- Maschinen --------------------------------------------------------------
r = give("Untertrainer", target="u-agent")
ok("Maschine bekommt an dieser Tuer kein may_grant: 403",
   r.status_code == 403 and "Maschinen" in r.get_json()["error"], r.get_json())
r = c.post("/internal/authz/assignments", headers=H, json={
    "actor": "vp_chef", "collection": C["Untertrainer"], "user": "u-agent",
    "context": {"team": "mB"}})
ok("KOEDER: auch nicht an der Tuer des Hosts: 403", r.status_code == 403,
   r.get_json())
r = give("Co", target="u-agent")
ok("ein einfaches Paket darf ein Agent bekommen: 201",
   r.status_code == 201, r.get_json())
A_AGENT = r.get_json()["assignment"]["id"]
gr = az.effective(m.load_authz(), "t-vp", "vereinsportal", "u-agent")
ok("Agent: effective zeigt nur mB",
   az.may(gr, "team.read", team="mB") and not az.may(gr, "team.read", team="mC")
   and not az.may(gr, "team.read"), gr)

ADMIN_H = {"Authorization": "Bearer " + KEY_ADMIN}
r = c.get("/authz/admin/users?on_behalf_of=u-chef", headers=ADMIN_H)
ok("Verwaltung: Standard listet nur Menschen",
   r.status_code == 200 and all("kind" not in u for u in r.get_json()["users"])
   and "agent-a" not in [u["username"] for u in r.get_json()["users"]], r.get_json())
r = c.get("/authz/admin/users?on_behalf_of=u-chef&kind=machine", headers=ADMIN_H)
got = [u["username"] for u in r.get_json()["users"]]
ok("Verwaltung: ?kind=machine zeigt den Agenten, keine Instanz-Prinzipale",
   got == ["agent-a"], got)

# --- Beenden ----------------------------------------------------------------
r = door("delete", "assignments/" + a2["id"], prf=proof("u-carla", "carla"))
ok("im eigenen Kontext auch Fremdes beenden: Co von Ben durch Carla",
   r.status_code == 200 and r.get_json()["ended"] is True, r.get_json())
ok("zweimal beenden aendert nichts",
   door("delete", "assignments/" + a2["id"]).get_json()["ended"] is False)
with m.authz_rw() as st:
    aid_mc = az.assign(st, "t-vp", C["Co"], "u-dora", {"team": "mC"},
                       granted_by="vp_chef")
    aid_idp = az.assign(st, "t-vp", C["Co"], "u-dora", {"team": "mB"},
                        granted_by="idp:x", source="idp")
    aid_fremd = az.assign(st, "t-vp", C["Fremd"], "u-dora", {"team": "mB"},
                          granted_by="vp_chef")
    m.save_authz(st)
ok("Zuordnung in mC bleibt fuer Trainer von mB zu: 403",
   door("delete", "assignments/" + aid_mc).status_code == 403)
ok("nicht gelistete Vorlage: 403",
   door("delete", "assignments/" + aid_fremd).status_code == 403)
ok("KOEDER: was eine Gruppe gab, nicht: 403",
   door("delete", "assignments/" + aid_idp).status_code == 403)
ok("nicht vorhanden: dieselbe Antwort",
   door("delete", "assignments/nix").status_code == 403)
left = assignments()
ok("nichts davon wurde beendet",
   not any(left[x]["ended"] for x in (aid_mc, aid_idp, aid_fremd)))

# --- verwaist und geloescht -------------------------------------------------
with m.authz_rw() as st:
    az.revoke(st, "t-vp", A_BEN, who="vp_chef")
    m.save_authz(st)
ok("Der Trainer geht: was er vergab, bleibt (A2)",
   not assignments()[a1["id"]]["ended"])
ok("und er kann nichts mehr weitergeben", give("Co").status_code == 403)

with m.users_rw() as us:
    res = m._delete_user("carla", us, "vp_chef", "tenant_admin", "t-vp")
ok("Benutzer geloescht", (res.get("ok") if isinstance(res, dict) else False), res)
ok("KOEDER: seine Zuordnungen sind beendet, nicht mehr lebend",
   all(a["ended"] for a in assignments().values() if a["subject"] == "u-carla"))

# --- Zusicherung und Schluessel einer Person --------------------------------
with m.users_rw() as us:
    _rec, KEY_BEN = m.issue_key(us, "ben", ["user"], "vp-prod", "Bens", 1, "root")
r = c.get("/verify?roles=user&instance=vp-prod",
          headers={"Authorization": "Bearer " + KEY_BEN})
ok("Schluessel einer Person: durchgelassen, Zusicherung leer (Spec 2.11)",
   r.status_code == 204 and r.headers.get(assertion.HEADER, "x") == "", r.headers)
ok("und der Name stimmt trotzdem", r.headers.get("X-OAAP-User") == "ben")

# --- Plattformrollen --------------------------------------------------------
after = {u["username"]: list(u["roles"]) for u in m.load_users()}
PLATFORM_ROLES.pop("carla")
ok("Plattformrollen unberuehrt", after == PLATFORM_ROLES)

print("---")
print("FAILED" if fails else "ALL PASSED", f"({fails} failed)")
sys.exit(1 if fails else 0)
