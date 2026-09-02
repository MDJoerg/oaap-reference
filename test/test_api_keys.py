#!/usr/bin/env python3
"""Ein Schluessel ist ein zweiter Weg zu derselben Antwort (RFC-0027).

`/verify` beantwortet als einzige Stelle der Plattform die Frage "wer
bist du und was darfst du". Bis 0.1.61 konnte sie nur ein Cookie
lesen. Jetzt liest sie auch einen Schluessel -- und der Satz, den diese
Datei verteidigt, ist:

    Vorne ein anderer Nachweis, hinten dieselben zwei Kopfzeilen.

Deshalb steht hier auffallend wenig ueber Rollen, Sichtbarkeitsgruppen
und die Mandantengrenze: die sind gemeinsamer Code und werden in
test_tenant_boundary.py geprueft. Geprueft wird hier, was neu ist und
was schiefgehen kann:

    Der Schluessel kann nur einschraenken, nie erweitern.
    Eine Maschine bekommt eine Antwort, nie ein Anmeldeformular.
    Ein begrenzter Schluessel scheitert geschlossen, wo das Gateway
    nicht sagt, um welche Instanz es geht.

Der letzte ist der wichtigste: Eine Site, die vor dieser Fassung
erzeugt wurde, nennt keine Instanz. Wuerde ein begrenzter Schluessel
dort durchgehen, waere die Begrenzung genau dort wirkungslos, wo
niemand nachsieht.

Aufruf: python3 test/test_api_keys.py
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


def load_identity(data_dir):
    """Identity mit einem Datenverzeichnis, das uns gehoert.

    DATA_DIR ist im Dienst eine feste Konstante ('/data', der Mount im
    Container). Die Funktionen lesen sie ueber die Modul-Globale, also
    genuegt es, sie nach dem Import umzuhaengen -- das ist ehrlicher
    als den Dienst fuer den Test umzubauen.
    """
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


def put_users(m, users):
    with open(m.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f)


def user(name, roles, kind="human", tenant="", active=True, groups=None):
    return {"username": name, "display_name": "", "password_hash": "",
            "kind": kind, "roles": roles, "groups": groups or [],
            "tenant": tenant, "active": active, "session_epoch": 0}


try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-keys-test-")
m = load_identity(DATA)
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"},
                           "t-cls": {"label": "cls"}}}, f)

put_users(m, [
    user("joerg", ["server_admin", "admin", "user"], tenant="t-default"),
    user("cls_admin", ["tenant_admin", "admin", "keyuser", "user"],
         tenant="t-cls"),
    user("terminal-3", ["user"], kind="machine", tenant="t-cls",
         groups=["packstation"]),
    user("gesperrt", ["user"], kind="machine", tenant="t-cls", active=False),
])

print("")
print("Ein Schluessel entsteht -- und sein Geheimnis genau einmal")

rec, secret = m.issue_key(m.load_users(), "terminal-3", ["user"], "",
                          "Packstation 3", 90, "cls_admin")
ok("er traegt den Mandanten seines Prinzipals", rec["tenant"] == "t-cls")
ok("das Geheimnis hat die vereinbarte Form",
   secret.startswith("oaapk_" + rec["id"] + "_"), secret[:20])
ok("gespeichert wird nur ein Hash",
   rec["hash"] and secret.split("_", 2)[2] not in json.dumps(m.load_keys()),
   "sonst waere die Zusage 'nur einmal sichtbar' eine Behauptung")
ok("die oeffentliche Fassung fuehrt den Hash nicht mit",
   "hash" not in m.public_key(rec))
ok("er laeuft ab", rec["expires"] > rec["created"])

print("")
print("Was beim Ausstellen abgelehnt wird")


def refused(**kw):
    args = {"users": m.load_users(), "principal": "terminal-3",
            "roles": ["user"], "instance": "", "label": "", "days": 90,
            "created_by": "cls_admin"}
    args.update(kw)
    try:
        m.issue_key(**args)
        return ""
    except ValueError as e:
        return str(e)


ok("ein Prinzipal, den es nicht gibt", "gibt es nicht" in refused(
    principal="niemand"))
ok("ein deaktivierter Prinzipal", "deaktiviert" in refused(
    principal="gesperrt"))
ok("server_admin -- niemals per Schluessel",
   "server_admin" in refused(principal="joerg", roles=["server_admin"]),
   "ein durchgesickerter server_admin-Schluessel ist der ganze Knoten")
ok("eine Rolle, die der Prinzipal selbst nicht haelt",
   "haelt diese Rollen selbst nicht" in refused(roles=["admin"]))
ok("gar keine gueltige Rolle", "Rolle" in refused(roles=["erfunden"]))
ok("'nie' als Gueltigkeit gibt es nicht", "1 bis 365" in refused(days=0))
ok("und laenger als ein Jahr auch nicht", "1 bis 365" in refused(days=400))

print("")
print("Vorne ein Schluessel, hinten dieselben zwei Kopfzeilen")

c = m.app.test_client()


def verify(token=None, **params):
    headers = {"Authorization": "Bearer " + token} if token else {}
    return c.get("/verify", query_string=params, headers=headers)


r = verify(secret, roles="user")
ok("eine gueltige Anfrage kommt durch", r.status_code == 204,
   f"HTTP {r.status_code}: {r.get_data(as_text=True)[:200]}")
ok("und nennt den Prinzipal, nicht den Schluessel",
   r.headers.get("X-OAAP-User") == "terminal-3", dict(r.headers))
ok("die Rollen stehen wie bei einer Sitzung darin",
   r.headers.get("X-OAAP-Roles") == "user", dict(r.headers))
ok("keine App erfaehrt, dass es ein Schluessel war",
   not any("key" in h.lower() or "bearer" in str(v).lower()
           for h, v in r.headers.items()),
   "das ist die ganze Zusage von RFC-0027: hinten ist es dasselbe")

print("")
print("Die Mandantengrenze gilt unveraendert")

ok("der eigene Mandant kommt durch",
   verify(secret, roles="user", tenant="t-cls").status_code == 204)
ok("ein fremder Mandant wird abgewiesen",
   verify(secret, roles="user", tenant="t-default").status_code == 403,
   "geprueft wird der Prinzipal, nicht der Schluessel")
ok("eine Rolle, die der Schluessel nicht hat, wird abgewiesen",
   verify(secret, roles="admin").status_code == 403)
ok("die Sichtbarkeitsgruppe des Prinzipals gilt",
   verify(secret, roles="user", groups="packstation").status_code == 204)
ok("eine fremde Gruppe wird abgewiesen",
   verify(secret, roles="user", groups="wareneingang").status_code == 403)

print("")
print("Eine Maschine bekommt eine Antwort, nie ein Anmeldeformular")

r = verify("oaapk_deadbeef_" + "x" * 30, roles="user")
ok("ein unbekannter Schluessel -> 401, keine Umleitung", r.status_code == 401,
   f"HTTP {r.status_code}")
ok("und sagt im Kopf, warum",
   "Bearer" in r.headers.get("WWW-Authenticate", ""),
   dict(r.headers))
ok("ein missgestalteter Schluessel -> 401",
   verify("voelliger-unsinn", roles="user").status_code == 401)
ok("ganz ohne Nachweis bleibt es bei der Umleitung zur Anmeldung",
   verify(roles="user").status_code == 303,
   "fuer Menschen ist die Umleitung richtig -- nur fuer Maschinen nicht")

print("")
print("Der Schluessel kann nur einschraenken, nie erweitern")

weit, weit_secret = m.issue_key(m.load_users(), "cls_admin",
                                ["admin", "user"], "", "", 30, "joerg")
r = verify(weit_secret, roles="admin")
ok("ein Schluessel mit zwei Rollen traegt beide",
   r.status_code == 204 and set(r.headers["X-OAAP-Roles"].split(","))
   == {"admin", "user"}, dict(r.headers))
put_users(m, [u if u["username"] != "cls_admin"
              else user("cls_admin", ["user"], tenant="t-cls")
              for u in m.load_users()])
r = verify(weit_secret, roles="admin")
ok("nimmt man dem Prinzipal eine Rolle, verliert der Schluessel sie sofort",
   r.status_code == 403,
   "Rollen kommen bei jeder Anfrage aus dem Benutzerspeicher, nie aus "
   "dem Schluessel")
ok("die verbliebene Rolle traegt er weiter",
   verify(weit_secret, roles="user").status_code == 204)

print("")
print("Begrenzung auf eine Instanz -- und was passiert, wenn niemand fragt")

eng, eng_secret = m.issue_key(m.load_users(), "terminal-3", ["user"],
                              "cls-viewer", "", 30, "joerg")
ok("in der genannten Instanz kommt er durch",
   verify(eng_secret, roles="user", instance="cls-viewer").status_code == 204)
ok("in einer anderen nicht",
   verify(eng_secret, roles="user", instance="cls-anders").status_code == 403)
ok("und ohne Instanzangabe AUCH NICHT",
   verify(eng_secret, roles="user").status_code == 403,
   "eine Site von vor dieser Fassung nennt keine Instanz; ein begrenzter "
   "Schluessel muss dort scheitern, nicht durchrutschen")
ok("ein unbegrenzter Schluessel stoert sich an nichts davon",
   verify(secret, roles="user", instance="cls-viewer").status_code == 204)

print("")
print("Entziehen wirkt sofort")

ok("vor dem Entzug geht er", verify(secret, roles="user").status_code == 204)
m.revoke_key(rec["id"])
ok("danach nicht mehr", verify(secret, roles="user").status_code == 401)
ok("der Datensatz bleibt stehen",
   any(k["id"] == rec["id"] for k in m.load_keys()),
   "ein entzogener Schluessel, der verschwindet, nimmt seine Geschichte mit")

print("")
print("Der Prinzipal ist die Reissleine")

put_users(m, [u if u["username"] != "terminal-3"
              else user("terminal-3", ["user"], kind="machine",
                        tenant="t-cls", active=False)
              for u in m.load_users()])
ok("ein deaktivierter Prinzipal entwertet jeden seiner Schluessel",
   verify(eng_secret, roles="user", instance="cls-viewer").status_code == 401,
   "ohne einen einzigen Schluessel anzufassen")

print("")
print("Eine Maschine kommt nicht ans Anmeldeformular")

put_users(m, [user("terminal-3", ["user"], kind="machine", tenant="t-cls")])
r = c.post("/auth/login", data={"username": "terminal-3", "password": ""})
ok("die Anmeldung mit leerem Passwort scheitert", r.status_code == 401)
r = c.post("/auth/login", data={"username": "terminal-3", "password": "x"})
ok("und mit irgendeinem auch", r.status_code == 401)

print("")
print("Was zuletzt benutzt wurde, wird notiert -- aber nicht bei jedem Aufruf")

put_users(m, [user("terminal-3", ["user"], kind="machine", tenant="t-cls")])
frisch, frisch_secret = m.issue_key(m.load_users(), "terminal-3", ["user"],
                                    "", "", 30, "joerg")
verify(frisch_secret, roles="user")
gespeichert = next(k for k in m.load_keys() if k["id"] == frisch["id"])
ok("nach dem ersten Aufruf steht ein Zeitpunkt darin",
   bool(gespeichert["last_used"]), gespeichert)
vorher = json.dumps(m.load_keys())
verify(frisch_secret, roles="user")
ok("ein zweiter Aufruf in derselben Stunde schreibt nicht erneut",
   json.dumps(m.load_keys()) == vorher,
   "sonst stuende vor jedem einzelnen Aufruf ein Dateischreibvorgang")

print("")
print("Ein abgelaufener Schluessel ist ein toter Schluessel")

keys = m.load_keys()
next(k for k in keys if k["id"] == frisch["id"])["expires"] = \
    "2000-01-01T00:00:00Z"
with open(m.KEYS_FILE, "w", encoding="utf-8") as f:
    json.dump(keys, f)
r = verify(frisch_secret, roles="user")
ok("er wird abgewiesen", r.status_code == 401)
ok("und die Antwort sagt, dass er abgelaufen ist",
   "expired" in r.get_data(as_text=True), r.get_data(as_text=True)[:200])

print("")
print("Aus einem Schluessel wird ein Terminal (RFC-0028)")

# Der Grund fuer diesen ganzen Umweg: Ein Browser setzt bei einer
# normalen Navigation keine Authorization-Kopfzeile. Ein Kiosk kann
# einen Schluessel also nicht so vorzeigen wie ein Skript -- er braucht
# ein Cookie. Die Einrichtung tauscht den Schluessel einmal dagegen,
# und das Cookie nennt weiterhin den Schluessel.
put_users(m, [user("terminal-3", ["user"], kind="machine", tenant="t-cls"),
              user("joerg", ["server_admin", "user"], tenant="t-default")])
t = m.app.test_client()
term, term_secret = m.issue_key(m.load_users(), "terminal-3", ["user"], "",
                                "Packstation 3", 365, "joerg", terminal=True)
r = t.post("/auth/terminal", data={"key": term_secret})
ok("die Einrichtung nimmt den Schluessel an", r.status_code == 200,
   f"HTTP {r.status_code}")
ok("und sagt, dass sich das Geraet ab jetzt selbst anmeldet",
   "Neustart" in r.get_data(as_text=True))

r = t.get("/verify", query_string={"roles": "user"})
ok("danach kommt der Browser OHNE Kopfzeile durch", r.status_code == 204,
   f"HTTP {r.status_code}")
ok("und ist der Maschinen-Prinzipal, nicht der Einrichter",
   r.headers.get("X-OAAP-User") == "terminal-3", dict(r.headers))

r = t.post("/auth/logout")
ok("ein Terminal kann sich nicht abmelden", r.status_code == 403,
   "ein Fehltipp in der Halle darf keinen toten Bildschirm hinterlassen")
ok("und die Antwort sagt, wo es stattdessen beendet wird",
   "Zug" in r.get_data(as_text=True))
ok("es laeuft danach weiter",
   t.get("/verify", query_string={"roles": "user"}).status_code == 204)

m.revoke_key(term["id"])
ok("den Schluessel zu entziehen beendet das Terminal sofort",
   t.get("/verify", query_string={"roles": "user"}).status_code == 303,
   "sonst ueberlebte das Cookie den Nachweis, aus dem es entstand -- und "
   "Entziehen waere dasselbe wie Nichtstun, bis jemand neu startet")

t2 = m.app.test_client()
ok("ein entzogener Schluessel richtet auch kein neues Terminal mehr ein",
   t2.post("/auth/terminal", data={"key": term_secret}).status_code == 401)

mensch, mensch_secret = m.issue_key(m.load_users(), "joerg", ["user"], "",
                                    "", 30, "joerg")
r = t2.post("/auth/terminal", data={"key": mensch_secret})
ok("ein Schluessel eines MENSCHEN wird als Terminal abgelehnt",
   r.status_code == 403,
   "sonst haenge die Anmeldung einer Person dauerhaft an einem Bildschirm")

zweit, zweit_secret = m.issue_key(m.load_users(), "terminal-3", ["user"], "",
                                  "Packstation 4", 365, "joerg", terminal=True)
t3 = m.app.test_client()
t3.post("/auth/terminal", data={"key": zweit_secret})
ok("ein zweites Geraet am selben Prinzipal laeuft eigenstaendig",
   t3.get("/verify", query_string={"roles": "user"}).status_code == 204,
   "geteilt wird der Prinzipal, nie das Geheimnis -- deshalb hat das "
   "Entziehen des ersten Geraets dieses hier nicht beruehrt")

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
