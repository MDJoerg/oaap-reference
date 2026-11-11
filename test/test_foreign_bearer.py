#!/usr/bin/env python3
"""Ein fremdes Bearer-Token neben einer Sitzung (I-44).

Bis 0.1.249 las `/verify` JEDES `Authorization: Bearer ...` als
OAAP-Schluessel und lehnte ab ("malformed key"), auch wenn eine gueltige
Sitzung mitkam. Jede App, die im Browser eigene Tokens benutzt (die
Keycloak-Konsole, eine SPA mit OIDC), scheiterte so hinter einer
angemeldeten Route -- Keycloak 0.1.6 zeigte nur "Manage realms".

Die Regel, die diese Datei verteidigt:

    Ein Token in UNSERER Form (oaapk_...) ist immer ein Schluessel --
    gueltig oder nicht. Ein fremdes Token mit Sitzung: die Sitzung
    entscheidet. Ein fremdes Token ohne Sitzung: die Maschinen-Antwort
    401, nie eine Umleitung.

Der Koeder an der richtigen Tuer: ein KAPUTTER oaapk_-Schluessel neben
einer gueltigen Sitzung darf nicht durchgehen -- sonst rettete ein Cookie,
das zufaellig mitkommt, einen falschen Schluessel.

Aufruf: python3 test/test_foreign_bearer.py
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich hier nicht laden.")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-foreign-bearer-")
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
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-cls": {"label": "cls"}, "t-x": {"label": "x"}}}, f)


def user(name, roles, kind="human", tenant="t-cls", **kw):
    u = {"username": name, "display_name": "", "password_hash": "", "kind": kind,
         "roles": roles, "groups": [], "tenant": tenant, "active": True,
         "session_epoch": 0}
    u.update(kw)
    return u


with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump([user("anna", ["admin", "user"]),
               user("bert", ["user"], tenant="t-x"),
               user("maschine", ["user"], kind="machine")], f)

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJrYyJ9.c2lnbmF0dXJl"   # irgendein fremdes Token


def client(as_user=None):
    c = m.app.test_client()
    if as_user:
        with c.session_transaction() as s:
            s["user"] = as_user
            s["epoch"] = 0
    return c


def verify(c, token=None, **params):
    h = {"Authorization": "Bearer " + token} if token else {}
    params.setdefault("roles", "user")
    return c.get("/verify", query_string=params, headers=h)


print("")
print("Fremdes Token MIT Sitzung: die Sitzung entscheidet")
r = verify(client("anna"), JWT)
ok("kommt durch (204)", r.status_code == 204, f"HTTP {r.status_code} {r.get_data(as_text=True)[:120]}")
ok("als die Person der Sitzung", r.headers.get("X-OAAP-User") == "anna", dict(r.headers))
r = verify(client("anna"), JWT, roles="support")
ok("die Rollen der Route gelten weiter (403 ohne support)", r.status_code == 403, r.status_code)
r = verify(client("bert"), JWT, tenant="t-cls")
ok("die Mandantengrenze gilt weiter (403)", r.status_code == 403, r.status_code)
ok("auch 'bearer' klein geschrieben",
   client("anna").get("/verify", query_string={"roles": "user"},
                      headers={"Authorization": "bearer " + JWT}).status_code == 204)

print("")
print("Fremdes Token OHNE Sitzung: die Maschinen-Antwort bleibt")
r = verify(client(), JWT)
ok("401, keine Umleitung", r.status_code == 401 and not r.headers.get("Location"),
   f"HTTP {r.status_code} {r.headers.get('Location')}")
ok("mit WWW-Authenticate", "Bearer" in (r.headers.get("WWW-Authenticate") or ""))

print("")
print("Unsere Form bleibt ein Schluessel -- der Koeder")
bad = "oaapk_deadbeef_" + "x" * 30
r = verify(client("anna"), bad)
ok("ein kaputter oaapk_-Schluessel NEBEN gueltiger Sitzung: 401", r.status_code == 401,
   f"HTTP {r.status_code} -- sonst rettete das Cookie den falschen Schluessel")
r = verify(client("anna"), "oaapk_kaputt")
ok("auch in falscher Form, solange er mit oaapk_ beginnt: 401", r.status_code == 401, r.status_code)
rec, secret = m.issue_key(m.load_users(), "maschine", ["user"], "", "t", 30, "anna")
r = verify(client("anna"), secret)
ok("ein gueltiger Schluessel neben einer Sitzung: der SCHLUESSEL zaehlt",
   r.status_code == 204 and r.headers.get("X-OAAP-User") == "maschine",
   f"{r.status_code} {r.headers.get('X-OAAP-User')}")

print("")
print("Eine Sitzung, die nicht mehr gilt, rettet das fremde Token nicht")
c = m.app.test_client()
with c.session_transaction() as s:
    s["user"] = "anna"
    s["epoch"] = 7          # abgemeldet ueberall seither
r = verify(c, JWT)
ok("keine 204", r.status_code != 204, r.status_code)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
