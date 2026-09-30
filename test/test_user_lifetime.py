#!/usr/bin/env python3
"""RFC-0046 §6 (identity 0.6): the lifetime of a person on the node.

Four things were added, and each can fail without anybody noticing:

    THE FORCED CHANGE MUST HOLD ON EVERY WAY IN. A flag that says "this
    session reaches the password page and nothing else" is worth what
    its weakest reader is worth. The session answer lives in ONE place
    (`_by_session`, which /verify and /auth/whoami both read), and the
    test asks all three doors: verify, whoami, and the login itself. The
    change must also produce a DIFFERENT password -- "change it to what
    it already is" would clear the flag and leave the handout's password
    standing.

    THE DATES MUST NOT FIRE FROM A REQUEST, AND MUST NOT BE CLEARED BY
    SILENCE. The portal's edit form does not send them; an update that
    says nothing about them keeps them. A moment in the past is refused,
    not fired. server_admin gets none.

    DELETION MUST REFUSE WHAT IT MUST REFUSE. server_admin, the last
    tenant_admin of a tenant, the actor themselves, a user with a key
    that still works, and a user of another tenant (as "not found", the
    answer that does not confirm the name exists). The audit line keeps
    the name and the id.

    ONE IMPLEMENTATION (RFC-0046 §4). `oaap user add|delete|schedule`
    and the cohort tool call the same functions in appctl, which run
    identity's own. The test runs the REAL scripts appctl would send into
    the identity container, in this process, against a throwaway data
    directory -- so a script that only looks right cannot pass.

Run: python3 test/test_user_lifetime.py   (needs flask + werkzeug)
"""
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.join(HERE, "..", "platform")
IDENTITY_DIR = os.path.join(PLATFORM_DIR, "services", "identity")

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
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(1)

d = tempfile.mkdtemp(prefix="oaap-lifetime-test-")
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = d
with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
    json.dump([], f)
with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
    json.dump({"setup_done": True, "server_admin_migrated": True,
               "support_migrated": True, "tenant_migrated": True}, f)
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
m.USERS_FILE = os.path.join(d, "users.json")
m.STATE_FILE = os.path.join(d, "state.json")
m.KEYS_FILE = os.path.join(d, "api-keys.json")
m.AUDIT_LOG = os.path.join(d, "audit.jsonl")
TENANTS = os.path.join(d, "tenants.json")
with open(TENANTS, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-op": {"label": "default"},
                           "t-schule": {"label": "schule"}}}, f)
m.TENANTS_FILE = TENANTS
m.THROTTLE_FILE = os.path.join(d, "throttle.json")
H = {"X-OAAP-Internal-Key": "test-internal-key"}
PW = "Anfang-1234"


def users_of():
    with open(m.USERS_FILE, encoding="utf-8") as f:
        return {u["username"]: u for u in json.load(f)}


def audit_lines():
    if not os.path.isfile(m.AUDIT_LOG):
        return []
    with open(m.AUDIT_LOG, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def seed(name, roles, tenant, **extra):
    from werkzeug.security import generate_password_hash
    us = list(users_of().values())
    us.append(dict({"username": name, "display_name": "",
                    "password_hash": generate_password_hash(PW),
                    "kind": "human", "roles": roles, "groups": [],
                    "tenant": tenant, "active": True,
                    "session_epoch": 0}, **extra))
    with open(m.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(us, f)


seed("chefin", ["server_admin"], "t-op")
seed("trainer", ["tenant_admin", "user"], "t-schule")
seed("zweiter-trainer", ["tenant_admin"], "t-schule")
seed("fremd", ["user"], "t-op")

c = m.app.test_client()


def create(actor, name, **kw):
    body = {"actor": actor, "username": name, "password": PW,
            "roles": ["user"], "groups": [], **kw}
    return c.post("/internal/users", json=body, headers=H)


# --------------------------------------------------------------------------
print("Anlegen: der Zwang steht, und er haelt an jeder Tuer")

r = create("trainer", "kurs-tn-01", display_name="Teilnehmer 01")
ok("ein tenant_admin legt an", r.status_code == 201, r.get_json())
u = users_of()["kurs-tn-01"]
ok("der Zwang steht beim Anlegen (Vorgabe: ja)",
   u["must_change_password"] is True, u)
ok("das Konto liegt im Mandanten des Handelnden", u["tenant"] == "t-schule")

r = create("trainer", "kurs-tn-keep", must_change_password=False)
ok("wer es ausdruecklich anders will, bekommt es anders",
   users_of()["kurs-tn-keep"]["must_change_password"] is False)

r = create("trainer", "maschine-1", kind="machine", roles=["user"])
ok("eine Maschine hat keinen Zwang (sie hat kein Passwort)",
   users_of()["maschine-1"]["must_change_password"] is False)

pc = m.app.test_client()          # der Teilnehmer, mit eigenem Cookie
r = pc.post("/auth/login", data={"username": "kurs-tn-01", "password": PW})
ok("die Anmeldung gelingt", r.status_code == 303, r.status_code)
ok("und schickt SOFORT auf die Passwortseite",
   r.headers.get("Location", "").startswith("/auth/password"),
   r.headers.get("Location"))

r = pc.get("/verify", headers={"Sec-Fetch-Mode": "cors"})
ok("verify laesst eine Sitzung mit Zwang NICHT durch (Skript: 403)",
   r.status_code == 403, r.status_code)
ok("und nennt den Grund", b"password" in r.data.lower(), r.data)
r = pc.get("/verify", headers={"Sec-Fetch-Mode": "navigate",
                               "X-Forwarded-Uri": "/tabelle?x=1"})
ok("eine Navigation wird auf die Passwortseite geschickt, mit Ziel",
   r.status_code == 303 and "/auth/password?next=" in r.headers["Location"],
   (r.status_code, r.headers.get("Location")))
r = pc.get("/auth/whoami")
ok("auch whoami (derselbe Weg) gibt einer Sitzung mit Zwang nichts",
   r.status_code == 401, r.status_code)
r = pc.get("/auth/password?next=%2Ftabelle")
ok("die Passwortseite ist erreichbar und sagt, warum",
   r.status_code == 200 and "jemand anderes" in r.get_data(as_text=True))

r = pc.post("/auth/password", data={"current": PW, "new": PW})
ok("dasselbe Passwort noch einmal loest den Zwang NICHT",
   r.status_code == 400 and users_of()["kurs-tn-01"]["must_change_password"],
   r.status_code)
r = pc.post("/auth/password", data={"current": "falsch", "new": "Eigenes-9876"})
ok("ein falsches aktuelles Passwort loest ihn auch nicht",
   r.status_code == 403 and users_of()["kurs-tn-01"]["must_change_password"])
r = pc.post("/auth/password", data={"current": PW, "new": "Eigenes-9876",
                                    "next": "//boese.example/"})
ok("das Aendern gelingt und loest den Zwang",
   r.status_code == 303 and
   users_of()["kurs-tn-01"]["must_change_password"] is False, r.status_code)
ok("das Ziel ist ein Ort dieser Plattform, nie ein fremder",
   r.headers.get("Location") == "/", r.headers.get("Location"))
r = pc.get("/verify")
ok("danach ist die Sitzung eine gewoehnliche", r.status_code == 204,
   r.status_code)
ok("und das Aendern steht im Protokoll",
   any(e["action"] == "user.password-changed" and e["subject"] == "kurs-tn-01"
       for e in audit_lines()))

# --------------------------------------------------------------------------
print("")
print("Ein gesetztes Passwort ist wieder ein fremdes")

r = c.post("/internal/users/kurs-tn-01/password", headers=H,
           json={"actor": "trainer", "password": "Neu-vom-Trainer-1"})
ok("der Trainer setzt ein Passwort", r.status_code == 200, r.get_json())
ok("der Zwang steht wieder",
   users_of()["kurs-tn-01"]["must_change_password"] is True)
r = pc.get("/verify", headers={"Sec-Fetch-Mode": "cors"})
ok("und die alte Sitzung ist beendet", r.status_code in (303, 403),
   r.status_code)
r = c.post("/internal/users/maschine-1/password", headers=H,
           json={"actor": "trainer", "password": "Irgendwas-12345"})
ok("eine Maschine bekommt kein Passwort", r.status_code == 400, r.get_json())

# --------------------------------------------------------------------------
print("")
print("Die Termine: gespeichert, nie ausgeloest, nie durch Schweigen geloescht")

future = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 40 * 86400))
later = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 100 * 86400))
past = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))

r = create("trainer", "kurs-tn-02", deactivate_at=future, delete_at=later,
           schedule_reason="Kohorte kurs-2026-10")
ok("Termine beim Anlegen", r.status_code == 201, r.get_json())
u = users_of()["kurs-tn-02"]
ok("ein Datum wird zum Beginn dieses Tages (UTC) gelesen",
   u["deactivate_at"] == future + "T00:00:00Z", u["deactivate_at"])
ok("der Grund bleibt dabei", u["schedule_reason"] == "Kohorte kurs-2026-10")
ok("angelegt heisst nicht abgelaufen: das Konto ist aktiv", u["active"])

r = create("trainer", "kurs-tn-03", deactivate_at=past)
ok("ein Zeitpunkt in der Vergangenheit wird abgelehnt, nicht ausgeloest",
   r.status_code == 400 and "Zukunft" in r.get_json()["error"], r.get_json())
r = create("trainer", "kurs-tn-03", deactivate_at=later, delete_at=future)
ok("die Loeschung muss nach der Deaktivierung liegen",
   r.status_code == 400, r.get_json())
r = create("trainer", "kurs-tn-03", deactivate_at="31.12.2030")
ok("ein Datum in anderer Schreibweise wird nicht geraten",
   r.status_code == 400, r.get_json())
r = create("chefin", "noch-ein-admin", roles=["server_admin"],
           delete_at=future, tenant="t-op")
ok("ein server_admin bekommt keine Termine",
   r.status_code == 400 and "server_admin" in r.get_json()["error"],
   r.get_json())

# Das Portal bearbeitet mit vollem Koerper, aber OHNE die Termine.
r = c.put("/internal/users/kurs-tn-02", headers=H, json={
    "actor": "trainer", "roles": ["user"], "groups": ["kurs-02"],
    "display_name": "Neu", "email": "", "active": True})
ok("ein Speichern ohne Termin-Felder laesst die Termine stehen",
   r.status_code == 200 and users_of()["kurs-tn-02"]["delete_at"]
   == later + "T00:00:00Z", r.get_json())
r = c.put("/internal/users/kurs-tn-02", headers=H, json={
    "actor": "trainer", "roles": ["user"], "groups": [], "active": True,
    "delete_at": ""})
ok("ausdruecklich leer loescht einen Termin",
   users_of()["kurs-tn-02"]["delete_at"] == ""
   and users_of()["kurs-tn-02"]["deactivate_at"] == future + "T00:00:00Z")
ok("die Aenderung steht im Protokoll, mit Grund",
   any(e["action"] == "user.change" and "delete_at" in e.get("detail", "")
       for e in audit_lines()))

r = c.get("/internal/users?actor=trainer", headers=H)
row = next(x for x in r.get_json()["users"] if x["username"] == "kurs-tn-02")
ok("die Liste traegt die Termine (die Benutzerseite zeigt sie)",
   row["deactivate_at"] and row["schedule_reason"] == "Kohorte kurs-2026-10",
   row)

# --------------------------------------------------------------------------
print("")
print("Loeschen: die Verweigerungen, und was bleibt")

ok("es gibt zwei tenant_admins im Mandanten (Ausgangslage)",
   sum("tenant_admin" in u["roles"] and u["tenant"] == "t-schule"
       for u in users_of().values()) == 2)
uid = users_of()["kurs-tn-02"]["id"]


def delete(actor, name):
    return c.delete(f"/internal/users/{name}", headers=H,
                    json={"actor": actor})


r = delete("trainer", "kurs-tn-02")
ok("ein tenant_admin loescht einen Teilnehmer", r.status_code == 200,
   r.get_json())
ok("der Datensatz ist weg", "kurs-tn-02" not in users_of())
ok("die Antwort nennt die Kennung",  r.get_json().get("id") == uid)
line = next((e for e in audit_lines() if e["action"] == "user.delete"), {})
ok("das Protokoll behaelt Name UND Kennung als Text",
   line.get("subject") == "kurs-tn-02" and uid in line.get("detail", ""),
   line)
ok("ein zweites Loeschen ist ein 404, kein Fehler im Dienst",
   delete("trainer", "kurs-tn-02").status_code == 404)

ok("ein server_admin wird nicht geloescht",
   delete("chefin", "chefin").status_code == 409)
seed("weiterer-chef", ["server_admin"], "t-op")
r = delete("chefin", "weiterer-chef")
ok("auch nicht, wenn ein zweiter da ist -- erst die Rolle abgeben",
   r.status_code == 409 and "server_admin" in r.get_json()["error"],
   r.get_json())
ok("das eigene Konto loescht man nicht selbst",
   delete("trainer", "trainer").status_code == 409)
r = delete("chefin", "zweiter-trainer")
ok("der zweite tenant_admin darf gehen", r.status_code == 200, r.get_json())
r = delete("chefin", "trainer")
ok("der LETZTE tenant_admin eines Mandanten nicht",
   r.status_code == 409 and "letzte" in r.get_json()["error"], r.get_json())
ok("ein tenant_admin sieht einen Nutzer eines anderen Mandanten nicht "
   "(404, nicht 403)",
   delete("trainer", "fremd").status_code == 404)

seed("mit-schluessel", ["user"], "t-schule")
users = m.load_users()
rec, secret = m.issue_key(users, "mit-schluessel", ["user"], "", "test", 30,
                          "chefin")
r = delete("trainer", "mit-schluessel")
ok("ein Benutzer mit gueltigem Schluessel wird nicht geloescht",
   r.status_code == 409 and rec["id"] in r.get_json()["error"], r.get_json())
m.revoke_key(rec["id"])
r = delete("trainer", "mit-schluessel")
ok("nach dem Entziehen geht es", r.status_code == 200, r.get_json())
r = c.delete("/internal/users/fremd", headers=H, json={"actor": "kurs-tn-01"})
ok("wer kein Verwalter ist, loescht nichts", r.status_code == 403,
   r.status_code)
r = c.delete("/internal/users/fremd", json={"actor": "chefin"})
ok("und ohne den Plattformschluessel kommt man gar nicht an die Tuer",
   r.status_code in (401, 403), r.status_code)

# --------------------------------------------------------------------------
print("")
print("Dieselben Funktionen an der Kommandozeile (appctl, echte Skripte)")

sys.path.insert(0, PLATFORM_DIR)
import appctl  # noqa: E402


def fake_exec(script, env=None):
    """What `docker exec identity python3 -c` would do, in this process."""
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(script, "<identity-exec>", "exec"),
                 {"__name__": "__exec__"})
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return buf.getvalue()


appctl._identity_exec = fake_exec
os.environ["SUDO_USER"] = "jörg"

code, res = appctl.identity_user_create({
    "username": "cli-01", "roles": ["user"], "groups": ["kurs-01"],
    "tenant": "t-schule", "password": PW, "display_name": "CLI 01",
    "must_change_password": True})
ok("`user add` legt an, im genannten Mandanten", code == 201
   and users_of()["cli-01"]["tenant"] == "t-schule", (code, res))
ok("mit Zwang", users_of()["cli-01"]["must_change_password"])
ok("und das Protokoll nennt den Bediener", any(
    e["action"] == "user.create" and e["subject"] == "cli-01"
    and e["who"] == "jörg" for e in audit_lines()))

code, res = appctl.identity_user_create({
    "username": "cli-admin", "roles": ["server_admin"], "password": PW})
ok("server_admin wird an dieser Tuer nicht vergeben", code == 403
   and "cli-admin" not in users_of(), (code, res))
code, res = appctl.identity_user_create({
    "username": "cli-01", "roles": ["user"], "password": PW})
ok("ein vergebener Name ist ein 409", code == 409, (code, res))
code, res = appctl.identity_user_create({
    "username": "cli-kurz", "roles": ["user"], "password": "kurz"})
ok("ein zu kurzes Passwort wird abgelehnt", code == 400, (code, res))

pw = appctl.generate_password()
ok("ein erzeugtes Passwort ist lesbar und lang genug",
   len(pw) >= 14 and not set(pw) & set("0OIl1"), pw)
ok("und zwei erzeugte sind nicht gleich", pw != appctl.generate_password())

code, res = appctl.identity_user_schedule(
    "cli-01", {"deactivate_at": future, "schedule_reason": "cohort test"})
u = users_of()["cli-01"]
ok("`user schedule` setzt einen Termin",
   code == 200 and u["deactivate_at"] == future + "T00:00:00Z"
   and u["roles"] == ["user"] and u["groups"] == ["kurs-01"], (code, res, u))
code, res = appctl.identity_user_schedule("cli-01", {"deactivate_at": past})
ok("und lehnt die Vergangenheit ab", code == 400, (code, res))
code, res = appctl.identity_user_schedule(
    "cli-01", {"deactivate_at": "", "delete_at": "", "schedule_reason": ""})
ok("`--clear` nimmt beide Termine weg",
   code == 200 and not users_of()["cli-01"]["deactivate_at"], (code, res))
code, res = appctl.identity_user_schedule("gibt-es-nicht",
                                          {"delete_at": future})
ok("ein unbekannter Benutzer ist ein 404", code == 404, (code, res))

code, res = appctl.identity_user_set_password("cli-01", "Vom-Bediener-55")
ok("`user password` setzt den Zwang wieder",
   code == 200 and users_of()["cli-01"]["must_change_password"], (code, res))
code, res = appctl.identity_user_set_password("cli-01", "Vom-Bediener-56",
                                              must_change=False)
ok("`--keep-password` laesst ihn weg",
   code == 200 and not users_of()["cli-01"]["must_change_password"])

code, res = appctl.identity_user_delete("cli-01")
ok("`user delete` loescht", code == 200 and "cli-01" not in users_of(),
   (code, res))
code, res = appctl.identity_user_delete("chefin")
ok("und verweigert einen server_admin auch dem Bediener", code == 409,
   (code, res))

print("")
print("ALLE BESTANDEN" if not fails else f"FAILED ({fails} Fehler)")
sys.exit(1 if fails else 0)
