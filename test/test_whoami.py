#!/usr/bin/env python3
"""Was eine App ueber den Menschen davor erfahren darf (oaap.core.identity 2.7).

Bis 0.1.75 bekam eine App zwei Kopfzeilen und sonst nichts. Wer oben
rechts ein Profilmenue zeigen wollte, konnte den Benutzernamen
hinschreiben -- nicht den Anzeigenamen, und die beiden Handlungen
"Passwort aendern" und "Abmelden" musste er selbst wissen.

`/auth/whoami` gibt der Seite dieselbe Antwort, die `/verify` dem
Gateway gibt. Der Satz, den diese Datei verteidigt, ist deshalb:

    whoami ist eine zweite LESUNG einer Wahrheit, nie eine zweite
    Wahrheit.

Konkret: `roles` muss zeichengleich das sein, was in derselben Lage in
X-OAAP-Roles steht. Liefe das auseinander, waere die Seite eine
Auskunft, die *fast* stimmt -- und das ist die schlimmere Sorte falsch,
weil sie beim Nachsehen recht zu haben scheint.

Und was NICHT drinsteht, wird hier genauso geprueft wie das, was
drinsteht: kein Mandant, keine Gruppen. Beides ist eine Entscheidung
(Grenze zieht das Gateway; Sichtbarkeit ist ein Plattform-Schalter),
und eine Entscheidung, die niemand nachprueft, ist eine Absicht.

Aufruf: python3 test/test_whoami.py
Braucht flask + werkzeug (wie der Identity-Dienst selbst). Fehlen sie,
meldet die Datei SKIP statt eines falschen PASS.
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
    from werkzeug.security import generate_password_hash
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(0)


def load_identity(data_dir):
    os.environ["SESSION_SECRET"] = "test-session-secret"
    os.environ["SETUP_TOKEN"] = "test-setup-token"
    os.environ["INTERNAL_API_KEY"] = "test-internal-key"
    os.environ["OAAP_IDENTITY_DATA_DIR"] = data_dir
    sys.path.insert(0, IDENTITY_DIR)
    sys.modules.pop("app", None)
    m = importlib.reload(importlib.import_module("app"))
    m.USERS_FILE = os.path.join(data_dir, "users.json")
    m.KEYS_FILE = os.path.join(data_dir, "api-keys.json")
    m.THROTTLE_FILE = os.path.join(data_dir, "login-throttle.json")
    m.AUDIT_LOG = os.path.join(data_dir, "audit.jsonl")
    m.TENANTS_FILE = os.path.join(data_dir, "tenants.json")
    return m


def user(name, roles, display="", kind="human", tenant="", groups=None,
         password="geheim-123"):
    return {"username": name, "display_name": display,
            "password_hash": generate_password_hash(password) if password else "",
            "kind": kind, "roles": roles, "groups": groups or [],
            "tenant": tenant, "active": True, "session_epoch": 0}


DATA = tempfile.mkdtemp(prefix="oaap-whoami-test-")
m = load_identity(DATA)
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"},
                           "t-cls": {"label": "cls"}}}, f)
with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump([
        user("joerg", ["server_admin", "admin", "user"], display="Jörg",
             tenant="t-default", groups=["leitung"]),
        user("bernd", ["admin", "user"], tenant="t-cls", groups=["werkstatt"]),
        user("terminal-3", ["user"], kind="machine", tenant="t-cls",
             password=""),
    ], f)


def signed_in(username, password="geheim-123"):
    """Eine echte Anmeldung, kein untergeschobenes Cookie.

    Der Umweg ist Absicht: geprueft werden soll die Lage, in der eine
    App tatsaechlich steht -- ein Mensch hat sich angemeldet, und die
    Seite fragt nach.
    """
    c = m.app.test_client()
    r = c.post("/auth/login", data={"username": username,
                                    "password": password})
    assert r.status_code in (302, 303), r.status_code
    return c


print("")
print("Die Antwort beschreibt den Aufrufer -- und niemanden sonst")

c = signed_in("joerg")
r = c.get("/auth/whoami")
me = r.get_json()
ok("sie kommt mit 200", r.status_code == 200, r.status_code)
ok("und nennt den Benutzernamen", me["username"] == "joerg", me)
ok("und den Anzeigenamen", me["display_name"] == "Jörg", me)
ok("und die Art des Prinzipals", me["kind"] == "human", me)
ok("und wie er sich ausgewiesen hat", me["method"] == "session", me)

print("")
print("Es gibt keinen Parameter, mit dem man nach jemand anderem fragt")

r2 = c.get("/auth/whoami", query_string={"username": "bernd",
                                         "user": "bernd"})
ok("ein untergeschobener Name aendert die Antwort nicht",
   r2.get_json()["username"] == "joerg", r2.get_json())

print("")
print("Zweite Lesung, nicht zweite Wahrheit")

# Der Kern. /verify beantwortet dieselbe Frage fuer das Gateway; die
# beiden duerfen nicht auseinanderlaufen -- auch nicht in der
# Reihenfolge, denn eine App, die auf die Zeichenkette vergleicht,
# wuerde das merken und wir nicht.
v = c.get("/verify")
ok("verify laesst dieselbe Sitzung durch", v.status_code == 204, v.status_code)
ok("und whoami nennt genau die Rollen, die in X-OAAP-Roles stehen",
   ",".join(me["roles"]) == v.headers.get("X-OAAP-Roles"),
   f"whoami={me['roles']} header={v.headers.get('X-OAAP-Roles')}")
ok("auch derselbe Benutzername",
   me["username"] == v.headers.get("X-OAAP-User"), v.headers)

print("")
print("Was NICHT drinsteht -- und das ist der Sinn der Sache")

body = json.dumps(me, ensure_ascii=False)
ok("kein Mandant: die Grenze zieht das Gateway, nicht die App",
   "tenant" not in me and "t-default" not in body, body)
ok("keine Gruppen: Sichtbarkeit ist ein Plattform-Schalter",
   "groups" not in me and "leitung" not in body, body)
ok("und ganz sicher kein Passwort-Hash",
   "hash" not in body and "pbkdf2" not in body and "scrypt" not in body, body)

print("")
print("Die Antwort beschreibt DIESE Sitzung, also darf sie niemand aufheben")

ok("sie traegt Cache-Control: no-store",
   "no-store" in (r.headers.get("Cache-Control") or ""),
   r.headers.get("Cache-Control"))

print("")
print("Die Adressen fuers Menue")

ok("Passwort aendern steht drin", me["links"]["password"] == "/auth/password", me)
ok("Abmelden steht drin", me["links"]["logout"] == "/auth/logout", me)
ok("Abmelden ist ein POST -- ein GET wuerde gar nicht erst angenommen",
   m.app.test_client().get("/auth/logout").status_code == 405,
   "sonst meldet jede fremde Seite den Benutzer per Bild-Tag ab")

print("")
print("Ein Anzeigename wird nie erfunden")

b = signed_in("bernd")
mb = b.get("/auth/whoami").get_json()
ok("ohne Anzeigenamen bleibt das Feld leer", mb["display_name"] == "", mb)
ok("und wird nicht aus dem Benutzernamen gebastelt",
   mb["display_name"] != "Bernd", mb)

print("")
print("Wer nicht angemeldet ist, bekommt eine Antwort -- kein Formular")

anon = m.app.test_client()
ra = anon.get("/auth/whoami")
ok("401, nicht 303 zum Anmeldeformular", ra.status_code == 401, ra.status_code)
ok("und nichts, was jemanden beschreibt",
   "joerg" not in ra.get_data(as_text=True), ra.get_data(as_text=True)[:200])
ok("mit WWW-Authenticate, damit ein Skript weiss, was fehlt",
   "Bearer" in (ra.headers.get("WWW-Authenticate") or ""),
   ra.headers.get("WWW-Authenticate"))

print("")
print("Ein Schluessel kann nur einschraenken -- auch hier")

rec, secret = m.issue_key(m.load_users(), "bernd", ["user"], "",
                          "Kasse", 90, "joerg")
k = m.app.test_client()
mk = k.get("/auth/whoami",
           headers={"Authorization": "Bearer " + secret}).get_json()
ok("whoami nennt die Rollen DES SCHLUESSELS, nicht die des Prinzipals",
   mk["roles"] == ["user"], f"{mk['roles']} (Prinzipal hat auch admin)")
vk = k.get("/verify", headers={"Authorization": "Bearer " + secret})
ok("und wieder zeichengleich mit der Kopfzeile",
   ",".join(mk["roles"]) == vk.headers.get("X-OAAP-Roles"),
   f"{mk['roles']} vs {vk.headers.get('X-OAAP-Roles')}")
ok("der Weg steht dabei", mk["method"] == "key", mk)

print("")
print("Eine Maschine bekommt keinen Link auf ein Passwortformular")

mrec, msecret = m.issue_key(m.load_users(), "terminal-3", ["user"], "",
                            "Packstation", 90, "joerg")
mm = m.app.test_client().get(
    "/auth/whoami", headers={"Authorization": "Bearer " + msecret}).get_json()
ok("sie ist als Maschine erkennbar", mm["kind"] == "machine", mm)
ok("Abmelden ja", mm["links"]["logout"] == "/auth/logout", mm)
ok("Passwort aendern nein -- sie hat keins",
   "password" not in mm["links"], mm)

print("")
print("Ein kaputter Schluessel bekommt die Maschinen-Antwort, kein Redirect")

rb = m.app.test_client().get("/auth/whoami",
                             headers={"Authorization": "Bearer kaputt"})
ok("401 statt 303", rb.status_code == 401, rb.status_code)

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
