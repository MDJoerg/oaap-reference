#!/usr/bin/env python3
"""Eine Ablehnung des Gateways muss LESBAR sein (RFC-0038 Folgebefund).

Der Satz, den diese Datei verteidigt:

    Wer von einer anderen Herkunft (Origin) anfragt und abgelehnt wird,
    soll im Browser den WAHREN Grund sehen -- nicht "CORS-Fehler".

Das ist kein Schoenheitsfehler. Am 15.09. hat Joerg Stunden in die
falsche Frage gesteckt: Der Browser meldete CORS, tatsaechlich fehlte
dem einen Aufruf (/config.json) der API-Schluessel. Das Gateway
antwortete 303 auf die Anmeldeseite, ohne CORS-Kopfzeilen -- und damit
war der Status fuer das Skript unsichtbar.

Geprueft werden vier Regeln und eine Nicht-Regel:

    Fremde Herkunft + Ablehnung  -> Access-Control-Allow-Origin.
    Fremde Herkunft + Skriptruf  -> 401 statt 303 (ein Skript kann mit
                                    einem Anmeldeformular nichts tun).
    Fremde Herkunft + Navigation -> die Umleitung bleibt (dort ist das
                                    Formular genau richtig).
    Gleiche Herkunft             -> unveraendert, keine CORS-Kopfzeile.
    NIE Access-Control-Allow-Credentials -- sonst koennte jede fremde
    Seite ausmessen, ob ihr Besucher hier angemeldet ist.

Aufruf: python3 test/test_cors_refusal.py
Braucht flask + werkzeug (wie der Identity-Dienst selbst).
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


try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-cors-test-")
m = load_identity(DATA)
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"},
                           "t-cls": {"label": "cls"}}}, f)
with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "display_name": "", "password_hash": "",
                "kind": "human", "roles": ["user"], "groups": [],
                "tenant": "t-default", "active": True, "session_epoch": 0}], f)

SITE = "gliss-viewer.cls.oaap.joomp.de"
FREMD = "http://localhost:8080"
c = m.app.test_client()


def call(path="/verify", origin=FREMD, mode="cors", dest="empty",
         host=SITE, proto="https", roles="user", auth=None):
    """Ein forward_auth-Aufruf, wie das Gateway ihn stellt.

    Genau die Kopfzeilen, die Caddy setzt: den Namen, den der Aufrufer
    getippt hat, in X-Forwarded-Host -- request.host ist hier immer
    identity:8000 und damit nutzlos.
    """
    h = {}
    if origin:
        h["Origin"] = origin
    if mode:
        h["Sec-Fetch-Mode"] = mode
    if dest:
        h["Sec-Fetch-Dest"] = dest
    if host:
        h["X-Forwarded-Host"] = host
    if proto:
        h["X-Forwarded-Proto"] = proto
    if auth:
        h["Authorization"] = auth
    qs = {"roles": roles} if roles else {}
    return c.get(path, query_string=qs, headers=h)


print("")
print("Die Herkunft erkennen -- ohne sich auf den eigenen Host zu verlassen")

with m.app.test_request_context(
        "/verify", headers={"Origin": f"https://{SITE}",
                            "X-Forwarded-Host": SITE,
                            "X-Forwarded-Proto": "https"}):
    ok("gleiche Herkunft gilt als eigene", m.foreign_origin() == "",
       "sonst bekaeme jeder Aufruf der App an sich selbst CORS-Kopfzeilen")
    ok("die Seite kennt ihre eigene Herkunft",
       m.site_origin() == f"https://{SITE}")
with m.app.test_request_context(
        "/verify", headers={"Origin": FREMD, "X-Forwarded-Host": SITE,
                            "X-Forwarded-Proto": "https"}):
    ok("fremde Herkunft wird als solche erkannt",
       m.foreign_origin() == FREMD)
with m.app.test_request_context(
        "/verify", headers={"Origin": "null", "X-Forwarded-Host": SITE}):
    ok("die Herkunft 'null' kann man nicht zurueckspiegeln",
       m.foreign_origin() == "",
       "ein undurchsichtiges Dokument -- zurueckspiegeln hilft ihm nicht")

print("")
print("Der Fall vom 15.09.: ein Skriptruf ohne jeden Nachweis")

r = call()
ok("keine Umleitung mehr, sondern eine Antwort", r.status_code == 401,
   f"war {r.status_code} -> {r.headers.get('Location')}")
ok("der Browser darf sie lesen",
   r.headers.get("Access-Control-Allow-Origin") == FREMD)
ok("die Antwort haengt an der Herkunft, also Vary",
   "Origin" in (r.headers.get("Vary") or ""))
ok("sie sagt, was zu tun ist", "Bearer" in r.get_data(as_text=True)
   and "RFC-0027" in r.get_data(as_text=True), r.get_data(as_text=True)[:200])
ok("WWW-Authenticate ist fuer das Skript sichtbar",
   "WWW-Authenticate" in (r.headers.get("Access-Control-Expose-Headers") or "")
   and r.headers.get("WWW-Authenticate"))
ok("KEIN Allow-Credentials",
   "Access-Control-Allow-Credentials" not in r.headers,
   "sonst koennte jede fremde Seite den Anmeldestand ihres Besuchers "
   "ausmessen -- und genau das soll eine Fehlermeldung nicht kaufen")
ok("kein Location mehr, das ins Leere fuehrt", not r.headers.get("Location"))

print("")
print("Ein falscher Schluessel: Status bleibt, Lesbarkeit kommt hinzu")

r = call(auth="Bearer nicht-die-vereinbarte-form")
ok("die Ablehnung behaelt ihren Status", r.status_code == 401)
ok("und wird lesbar", r.headers.get("Access-Control-Allow-Origin") == FREMD)
ok("der Grund steht drin", "malformed key" in r.get_data(as_text=True),
   r.get_data(as_text=True)[:120])

print("")
print("Was NICHT angefasst wird")

r = call(origin=f"https://{SITE}")
ok("gleiche Herkunft: die Umleitung bleibt", r.status_code == 303,
   f"war {r.status_code}")
ok("gleiche Herkunft: keine CORS-Kopfzeile",
   "Access-Control-Allow-Origin" not in r.headers)

r = call(mode="navigate", dest="document")
ok("fremde Herkunft, aber eine NAVIGATION: Umleitung bleibt",
   r.status_code == 303,
   "ein Mensch, der auf einen Link klickt, soll das Anmeldeformular sehen")

r = call(origin="")
ok("ohne Origin bleibt alles wie zuvor",
   r.status_code == 303 and "Access-Control-Allow-Origin" not in r.headers)

c2 = m.app.test_client()
r = c2.get("/auth/login", headers={"Origin": FREMD, "X-Forwarded-Host": SITE})
ok("nur die Gateway-Endpunkte sind betroffen, nicht /auth/*",
   "Access-Control-Allow-Origin" not in r.headers,
   "die Anmeldeseite ist keine Ablehnung des Gateways")

print("")
print("Die Bremse (RFC-0010) liegt auf demselben Weg")

m._RATE.clear()
# Die Zaehlung der gebremsten Anfragen benutzt fcntl (POSIX) und ist
# nicht, was hier geprueft wird -- auf einer Entwicklermaschine ohne
# fcntl wuerde sie den 429 in einen 500 verwandeln und die Regel
# verdecken. Also stillgelegt, nicht uebersprungen: die Regel gilt auf
# jedem Rechner, auf dem diese Datei laeuft.
m._braked_note = lambda scope: None
last = None
for _ in range(4):
    last = c.get("/throttle", query_string={"scope": "x", "limit": "2",
                                            "window": "60"},
                 headers={"Origin": FREMD, "X-Forwarded-Host": SITE,
                          "Sec-Fetch-Mode": "cors"})
ok("auch 429 wird lesbar",
   last.status_code == 429
   and last.headers.get("Access-Control-Allow-Origin") == FREMD,
   f"{last.status_code} / {last.headers.get('Access-Control-Allow-Origin')}")
ok("ein 429 bleibt ein 429 -- kein 401 daraus",
   last.status_code == 429,
   "nur eine UMLEITUNG wird umgedeutet, kein anderer Status")

print("")
print("Erfolg wird nicht angefasst -- dort antwortet die App selbst")

r = c.get("/verify", query_string={"roles": "user"},
          headers={"Origin": FREMD, "X-Forwarded-Host": SITE})
ok("ohne Sitzung ist 204 hier ohnehin nicht zu haben",
   r.status_code != 204)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
