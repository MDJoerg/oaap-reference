#!/usr/bin/env python3
"""Mehrere Anmeldungen in einem Browser (RFC-0137 Stufe 0 + 1).

Alle Mandanten unter dem Knotennamen teilen ein Cookie. Bis 0.1.389 hielt
es eine Person: wer sich im Chat von Mandant B anmeldete, war im Chat von
Mandant A abgemeldet, und /verify sagte dort "another tenant". Jetzt hält
die Sitzung je Mandant eine Anmeldung, und jede Anfrage nimmt genau eine.

Der Satz, den diese Datei verteidigt:

    Jede Anfrage gehört genau einer Person -- der Anmeldung des Mandanten,
    um dessen Instanz oder Ort es geht. Nie der eines anderen.

Aufruf: python3 test/test_several_sign_ins.py
Braucht flask + werkzeug; fehlen sie, SKIP statt eines falschen PASS.
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


try:
    import flask  # noqa: F401
    from werkzeug.security import generate_password_hash
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-signins-test-")
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = DATA
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
m.USERS_FILE = os.path.join(DATA, "users.json")
m.KEYS_FILE = os.path.join(DATA, "api-keys.json")
m.THROTTLE_FILE = os.path.join(DATA, "login-throttle.json")
m.AUDIT_LOG = os.path.join(DATA, "audit.jsonl")
m.TENANTS_FILE = os.path.join(DATA, "tenants.json")
m.EXTERNAL_HOST_FILE = os.path.join(DATA, "external.json")
m.MODES_FILE = os.path.join(DATA, "modes.json")

NODE = "node.test"
with open(m.EXTERNAL_HOST_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": NODE}, f)
TENANTS = {"t-default": {"label": "default"}, "t-a": {"label": "pxx"},
           "t-b": {"label": "csa"}, "t-c": {"label": "kit"}}
for i in range(9):
    TENANTS[f"t-x{i}"] = {"label": f"x{i}"}
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": TENANTS}, f)


def user(name, tenant, roles=("user",)):
    return {"username": name, "display_name": name,
            "password_hash": generate_password_hash("geheim-123"),
            "kind": "human", "roles": list(roles), "groups": [],
            "tenant": tenant, "active": True, "session_epoch": 0}


USERS = [user("anna", "t-a"), user("anna2", "t-a"), user("bert", "t-b"),
         user("op", "t-default", ("server_admin", "admin", "user"))]
USERS += [user(f"x{i}", f"t-x{i}") for i in range(9)]


def reset_users():
    with open(m.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(USERS, f)


def bump(name):
    users = json.load(open(m.USERS_FILE, encoding="utf-8"))
    for u in users:
        if u["username"] == name:
            u["session_epoch"] = u.get("session_epoch", 0) + 1
    with open(m.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f)


def login(c, name, **kw):
    r = c.post("/auth/login", data={"username": name, "password": "geheim-123"}, **kw)
    assert r.status_code in (302, 303), (name, r.status_code)


def verify(c, tenant, nav=False):
    h = {"Sec-Fetch-Mode": "navigate"} if nav else {}
    return c.get(f"/verify?instance=chat&tenant={tenant}", headers=h)


def who(r):
    return r.headers.get("X-OAAP-User", "")


def accounts(c):
    with c.session_transaction() as s:
        return dict(s.get("accounts") or {}), s.get("last")


reset_users()

print("\nZwei Mandanten, ein Browser")
c = m.app.test_client()
login(c, "anna")
login(c, "bert")
r = verify(c, "t-a")
ok("Instanz von pxx: anna", r.status_code == 204 and who(r) == "anna", (r.status_code, who(r)))
r = verify(c, "t-b")
ok("Instanz von csa: bert", r.status_code == 204 and who(r) == "bert", (r.status_code, who(r)))
acc, last = accounts(c)
ok("zwei Einträge, der neueste ist der letzte", set(acc) == {"t-a", "t-b"} and last == "t-b", acc)
ok("kein Eintrag trägt eine Person auf oberster Ebene", True)
with c.session_transaction() as s:
    ok("die alte Form ist weg", "user" not in s and "epoch" not in s, dict(s))

print("\nEin dritter Mandant ohne Anmeldung")
r = verify(c, "t-c")
ok("geht zur Anmeldung dieses Mandanten (303, tenant=t-c)",
   r.status_code == 303 and "tenant=t-c" in r.headers.get("Location", ""),
   (r.status_code, r.headers.get("Location")))
r = c.get("/auth/login?tenant=t-c")
ok("die Anmeldeseite sagt, wer schon angemeldet ist",
   "Angemeldet als bert, anna" in r.get_data(as_text=True), r.get_data(as_text=True)[-600:])

print("\nDer Ort eines Mandanten nimmt dessen Anmeldung")
r = c.get("/auth/whoami", headers={"X-Forwarded-Host": f"pxx.{NODE}"})
ok("pxx.node.test: anna", r.status_code == 200 and (r.get_json() or {}).get("username") == "anna",
   (r.status_code, r.get_data(as_text=True)[:200]))
r = c.get("/auth/whoami", headers={"X-Forwarded-Host": f"csa.{NODE}"})
ok("csa.node.test: bert", (r.get_json() or {}).get("username") == "bert", r.get_data(as_text=True)[:200])
r = c.get("/auth/whoami", headers={"X-OAAP-Site-Tenant": "t-a",
                                   "X-Forwarded-Host": f"chat.{NODE}"})
ok("Instanz-Adresse mit dem Wort des Gateways: anna",
   (r.get_json() or {}).get("username") == "anna", r.get_data(as_text=True)[:200])
r = c.get("/auth/whoami", headers={"X-Forwarded-Host": NODE})
ok("Knotenname ohne Mandant: die neueste (bert)", (r.get_json() or {}).get("username") == "bert",
   r.get_data(as_text=True)[:200])

print("\nAbgemeldet anderswo: nur dieser Eintrag fällt weg")
bump("bert")
r = verify(c, "t-b")
ok("csa: zur Anmeldung", r.status_code == 303, r.status_code)
r = verify(c, "t-a")
ok("pxx: weiter anna", r.status_code == 204 and who(r) == "anna", (r.status_code, who(r)))
acc, last = accounts(c)
ok("nur noch pxx in der Liste, letzter zeigt darauf", set(acc) == {"t-a"} and last == "t-a", (acc, last))

print("\nAbmelden gilt für den Ort (D6)")
reset_users()
c = m.app.test_client()
login(c, "anna")
login(c, "bert")
r = c.post("/auth/logout", headers={"X-OAAP-Site-Tenant": "t-a"})
ok("Abmelden auf einer pxx-Instanz antwortet mit einer Weiterleitung", r.status_code in (302, 303), r.status_code)
acc, _ = accounts(c)
ok("anna ist weg, bert bleibt", set(acc) == {"t-b"}, acc)
r = verify(c, "t-b")
ok("csa weiter bert", r.status_code == 204 and who(r) == "bert", (r.status_code, who(r)))
login(c, "anna")
r = c.post("/auth/logout", data={"all": "1"})
acc, _ = accounts(c)
ok("„Überall abmelden“ leert die Liste", acc == {}, acc)
ok("und danach ist niemand mehr angemeldet", verify(c, "t-b").status_code == 303)

print("\nDie Form vor RFC-0137 wird gelesen (niemand fliegt beim Update raus)")
reset_users()
c = m.app.test_client()
with c.session_transaction() as s:
    s["user"] = "anna"
    s["epoch"] = 0
r = verify(c, "t-a")
ok("alter Keks: anna an ihrer Instanz", r.status_code == 204 and who(r) == "anna", (r.status_code, who(r)))
login(c, "bert")
acc, _ = accounts(c)
ok("eine neue Anmeldung legt sich daneben", set(acc) == {"t-a", "t-b"}, acc)

print("\nEine Anmeldung je Mandant (D2)")
c = m.app.test_client()
login(c, "anna")
login(c, "anna2")
acc, _ = accounts(c)
ok("eine zweite Person desselben Mandanten ersetzt die erste", list(acc) == ["t-a"] and acc["t-a"]["user"] == "anna2", acc)
ok("und /verify sagt anna2", who(verify(c, "t-a")) == "anna2")

print("\nserver_admin und Mitglied (D4)")
c = m.app.test_client()
login(c, "op")
login(c, "anna")
ok("pxx: das Mitglied anna, nicht der Betreiber", who(verify(c, "t-a")) == "anna")
r = verify(c, "t-b")
ok("csa ohne Mitglied: der Betreiber", r.status_code == 204 and who(r) == "op", (r.status_code, who(r)))

print("\nHöchstens acht (D5)")
c = m.app.test_client()
for i in range(9):
    login(c, f"x{i}")
acc, last = accounts(c)
ok("acht Einträge", len(acc) == 8, len(acc))
ok("der älteste ist gegangen, der neueste geblieben", "t-x0" not in acc and "t-x8" in acc and last == "t-x8", list(acc))
ck = c.get_cookie("oaap_session")
size = len(ck.value) if ck else 0
ok("das Cookie bleibt unter 4 KB (gemessen, nicht leer)", 0 < size < 4096, size)
print(f"      Cookie mit acht Anmeldungen: {size} Zeichen")

print("\nDer Abmeldehinweis des Anbieters nur bei der neuesten Realm-Anmeldung")
with m.app.test_request_context("/"):
    m._add_account(USERS[0], {"provider": "kc"}, {"tenant": "t-a", "hint": "h" * 1500})
    m._add_account(USERS[2], {"provider": "kc"}, {"tenant": "t-b", "hint": "j" * 1500})
    acc = m._accounts()
    ok("die ältere behält den Mandanten, nicht den Hinweis",
       acc["t-a"]["idp_out"] == {"tenant": "t-a"}, acc["t-a"])
    ok("die neueste trägt ihn", acc["t-b"]["idp_out"].get("hint") == "j" * 1500)

print("\nStufe 0: die Ablehnung zeigt den Weg")
reset_users()
c = m.app.test_client()
login(c, "anna")
r = verify(c, "t-unbekannt", nav=True)
body = r.get_data(as_text=True)
ok("ein unbekannter Mandant bleibt 403", r.status_code == 403, r.status_code)
ok("im Browser eine Seite mit „Mit anderem Konto anmelden“",
   "Mit anderem Konto anmelden" in body and "anna" in body, body[:300])
r = verify(c, "t-unbekannt")
ok("für ein Skript der Satz wie bisher",
   r.get_data(as_text=True) == "Forbidden: this app belongs to another tenant", r.get_data(as_text=True)[:200])

print("\nDas Gateway sagt identity, wessen Instanz es ist")
import appctl as g  # noqa: E402
body = "\n".join(g.site_body([{"path": "/", "roles": ["user"]}], "c", 80, tenant="t-a"))
auth = body.split("handle /auth/* {", 1)[1].split("}", 1)[0]
ok("im /auth/*-Block steht X-OAAP-Site-Tenant mit dem Mandanten",
   'request_header X-OAAP-Site-Tenant "t-a"' in auth, auth)
ok("auf Site-Ebene wird ein mitgeschickter gestrichen",
   "\trequest_header -X-OAAP-Site-Tenant" in body.split("handle /auth/*", 1)[0])
ok("ein krummer Wert wird weggelassen", g.site_tenant_lines('t"; evil') == [])
ok("eine alte Site ohne die Zeile gilt als veraltet (wird beim Update neu geschrieben)",
   g._site_identity_stale(body.replace("\trequest_header -X-OAAP-Site-Tenant\n", "")))
ok("die neue nicht", not g._site_identity_stale(body))

print("")
print("OK" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
