#!/usr/bin/env python3
"""Der Anmeldedienst eines Mandanten (RFC-0041, Schritte 2/3/5).

Der Anbieter beantwortet *wer*, OAAP beantwortet *was jemand darf*.
Geprueft wird, was teuer ist, wenn es bricht -- und zwei Regeln werden
absichtlich AUSGEFUEHRT statt gelesen, weil der Mutationstest vom
23.09.2026 gezeigt hat, dass eine Regel in einem Zweig nur gelesen
werden kann und eine Textpruefung gruen bleibt, sobald jemand die
Bedingung aendert und den Satz stehen laesst:

    EIN Urteil        Portal, Anmeldedienst und `appctl` fragen
                      services/idp.py, und beide Images kopieren die
                      Datei wirklich mit.
    Die Bindung       Eine eingehende Anmeldung wird NUR ueber
                      (Anbieter, sub, Mandant) zugeordnet. Nicht ueber
                      E-Mail, nicht ueber den Benutzernamen -- das ist
                      Kontouebernahme durch Namensgleichheit.
    Keine Rolle       Keine Behauptung eines Anbieters wird je zu einer
                      OAAP-Rolle. Auch nicht, wenn die Gruppe
                      "server_admin" heisst.
    Der Kanal         Ein `http`-Aussteller, der aus dem Internet
                      erreichbar ist, wird abgelehnt: darueber reisen
                      das Client-Geheimnis und das Token, und nichts
                      sonst beweist, wer geantwortet hat.
    Die Vollmacht     Nur `server_admin` legt den Schalter um, der
                      entscheidet, was ein erster Login bedeutet (K4b).
    Das Geheimnis     Steht nie in tenants.json, nie in einer Sicht des
                      Portals, und liegt 0600 in einem Verzeichnis, das
                      nur der Anmeldedienst einhaengt -- lesend.
    Kein Rueckfall    Ein Konto, das ein Anbieter angelegt hat, kann
                      sich lokal nicht anmelden.
    Der ganze Weg     Ein erfundener Anbieter, ein echter Durchlauf:
                      /auth/oidc/start -> Weiterleitung -> Token ->
                      Sitzung -> Benutzersatz.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_tenant_idp.py
"""
import ast
import base64
import io as _io
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-idp-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, SERVICES)
sys.path.insert(0, PLATFORM)

import idp                                                     # noqa: E402
import place                                                   # noqa: E402

HOST = "oaap.joomp.de"
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
print("Ein Urteil in drei Programmen")

IDP_SRC = read(SERVICES, "idp.py")
IDENTITY_SRC = read(SERVICES, "identity", "app.py")
PORTAL_SRC = read(SERVICES, "portal", "app.py")
APPCTL_SRC = read(PLATFORM, "appctl.py")

for name, src in (("identity", IDENTITY_SRC), ("portal", PORTAL_SRC),
                  ("appctl", APPCTL_SRC)):
    ok(f"{name} fragt services/idp.py", re.search(r"^import idp", src, re.M),
       src[:200])

for svc in ("portal", "identity"):
    dockerfile = read(SERVICES, svc, "Dockerfile")
    ok(f"das {svc}-Image kopiert idp.py wirklich mit",
       re.search(r"^COPY .*\bidp\.py\b", dockerfile, re.M), dockerfile)

# Die beiden Saetze, die eine Ablehnung ausmachen, duerfen nur an EINER
# Stelle entstehen koennen. Solange das gilt, ist eine dritte Tuer
# zaehlbar, statt nur gehofft (die Lehre aus 0.1.115).
for fn in ("def provider_refusal", "def policy_refusal", "def issuer_refusal",
           "def find_binding", "def first_login_grant", "def policy_target"):
    here = IDP_SRC.count(fn)
    elsewhere = (IDENTITY_SRC + PORTAL_SRC + APPCTL_SRC).count(fn)
    ok(f"{fn[4:]} gibt es genau einmal, und zwar in idp.py",
       here == 1 and elsewhere == 0, f"idp.py {here}, sonst {elsewhere}")

# ---------------------------------------------------------------------------
print("\nDie Bindung: (Anbieter, sub, Mandant) und sonst nichts")

PKEY = "oidc|https://auth.example.org/realms/hbvp"
USERS = [
    {"username": "mueller", "tenant": "T-HBVP", "email": "m@example.org",
     "idp": {"provider": PKEY, "subject": "S-1"}},
    # Derselbe Mensch, anderer Mandant: zwei Prinzipale (RFC-0022 D3).
    {"username": "mueller-cls", "tenant": "T-CLS", "email": "m@example.org",
     "idp": {"provider": PKEY, "subject": "S-1"}},
    # Gleiche E-Mail, gleicher Name, KEINE Bindung.
    {"username": "s-1", "tenant": "T-HBVP", "email": "m@example.org"},
]

ok("die Bindung findet den richtigen Satz",
   (idp.find_binding(USERS, PKEY, "S-1", "T-HBVP") or {}).get("username")
   == "mueller")
ok("derselbe sub in einem anderen Mandanten ist ein ANDERER Prinzipal",
   (idp.find_binding(USERS, PKEY, "S-1", "T-CLS") or {}).get("username")
   == "mueller-cls")
ok("ein unbekannter sub findet nichts, auch bei gleicher E-Mail",
   idp.find_binding(USERS, PKEY, "S-NEU", "T-HBVP") is None)
ok("ein Satz ohne Bindung wird nie getroffen, auch wenn er so HEISST "
   "wie der sub",
   idp.find_binding(USERS, PKEY, "s-1", "T-HBVP") is None)
ok("ein anderer Anbieter mit demselben sub findet nichts",
   idp.find_binding(USERS, "oidc|https://woanders/realms/x", "S-1",
                    "T-HBVP") is None)
ok("ein leerer sub bindet an gar nichts",
   idp.find_binding(USERS, PKEY, "", "T-HBVP") is None)

# ---------------------------------------------------------------------------
print("\nKeine Behauptung wird zu einer Rolle")

HOSTILE = {"sub": "S-9", "preferred_username": "eindringling",
           "roles": ["server_admin"],
           "realm_access": {"roles": ["server_admin", "tenant_admin"]},
           "groups": ["server_admin", "/tenant_admin", "vorstand"]}

for first in ("eingang", "role", "groups"):
    policy = {"first_login": first, "default_role": "user",
              "group_map": {"vorstand": "leitung"}}
    roles, groups = idp.first_login_grant(policy, HOSTILE)
    ok(f"'{first}': keine knotenweite Rolle aus einer Behauptung",
       not (set(roles) & set(idp.NEVER_AT_FIRST_LOGIN)), roles)
    ok(f"'{first}': keine Gruppe ohne ausdrueckliche Abbildung",
       "server_admin" not in groups and "tenant_admin" not in groups, groups)

ok("'eingang' gibt gar nichts",
   idp.first_login_grant({"first_login": "eingang"}, HOSTILE) == ([], []))
ok("'role' gibt genau die hinterlegte Rolle",
   idp.first_login_grant({"first_login": "role", "default_role": "user"},
                         HOSTILE) == (["user"], []))
ok("'groups' bildet nur ab, was der Betreiber geschrieben hat",
   idp.first_login_grant(
       {"first_login": "groups", "default_role": "user",
        "group_map": {"vorstand": "leitung"}}, HOSTILE)
   == (["user"], ["leitung"]))
# Und der Riegel dahinter: selbst wenn jemand eine verbotene Rolle in
# die gespeicherte Richtlinie schriebe, gibt sie diese Funktion nicht
# aus. Die Pruefung beim Setzen ist die erste Schranke, nicht die
# einzige.
ok("eine verbotene Rolle in der gespeicherten Richtlinie wirkt trotzdem "
   "nicht",
   idp.first_login_grant({"first_login": "role",
                          "default_role": "server_admin"}, HOSTILE)
   == ([], []))

# ---------------------------------------------------------------------------
print("\nDer Kanal zum Aussteller")

for issuer, want_ok in (
        ("https://auth.example.org/realms/hbvp", True),
        ("http://127.0.0.1:8080/realms/hbvp", True),
        ("http://10.10.10.96:8101/realms/hbvp", True),
        ("http://192.168.1.5/realms/hbvp", True),
        ("http://keycloak:8080/realms/hbvp", True),
        ("http://localhost:8080/realms/hbvp", True),
        ("http://auth.example.org/realms/hbvp", False),
        ("http://8.8.8.8/realms/hbvp", False),
        ("https://auth.example.org/realms/hbvp/", False),
        ("https://auth.example.org/realms/hbvp?x=1", False),
        ("ftp://auth.example.org/realms/hbvp", False),
        ("https://user:pw@auth.example.org/realms/x", False),
        ("", False)):
    got = not idp.issuer_refusal(issuer)
    ok(f"{'angenommen' if want_ok else 'abgelehnt'}: {issuer or '(leer)'}",
       got == want_ok, idp.issuer_refusal(issuer))

ok("ein Anbieter-Objekt ohne Geheimnis wird abgelehnt",
   idp.provider_refusal("oidc", "https://a.example/realms/x", "c", "kurz"))
ok("ein unbekannter Anbieter-Typ wird abgelehnt",
   idp.provider_refusal("saml", "https://a.example/realms/x", "c", "x" * 32))
ok("ein vollstaendiges Anbieter-Objekt wird angenommen",
   not idp.provider_refusal("oidc", "https://a.example/realms/x", "oaap-node",
                            "x" * 32))
# Kein Anbieter ist kein Anbieter namens "". Gemessen auf oaap-test:
# ohne diese Unterscheidung meldete die ALLERERSTE Anbindung "ISSUER
# CHANGED, every binding it had is void" -- und schlimmer als die
# Formulierung: "oidc|" ist ein Schluessel, und ein Schluessel trifft.
ok("ohne Anbieter gibt es keinen Schluessel", idp.provider_key({}) == ""
   and idp.provider_key({"issuer": ""}) == "", idp.provider_key({}))
ok("und ein leerer Schluessel bindet an nichts",
   idp.find_binding([{"username": "x", "tenant": "T",
                      "idp": {"provider": "", "subject": "S"}}],
                    idp.provider_key({}), "S", "T") is None)

# Das Entdeckungsdokument muss sich selbst so nennen, wie wir es
# konfiguriert haben -- sonst uebernaehme eine Weiterleitung lautlos die
# Anmeldung.
GOOD_DOC = {"issuer": "https://a.example/realms/x",
            "authorization_endpoint": "https://a.example/realms/x/auth",
            "token_endpoint": "https://a.example/realms/x/token"}
ok("ein stimmiges Entdeckungsdokument wird angenommen",
   not idp.endpoints_refusal(GOOD_DOC, "https://a.example/realms/x"))
ok("ein Dokument, das sich anders nennt, wird abgelehnt",
   idp.endpoints_refusal(dict(GOOD_DOC, issuer="https://woanders/realms/x"),
                         "https://a.example/realms/x"))
ok("ein Token-Endpunkt auf einem unsicheren Kanal wird abgelehnt",
   idp.endpoints_refusal(
       dict(GOOD_DOC, token_endpoint="http://a.example/realms/x/token"),
       "https://a.example/realms/x"))

# ---------------------------------------------------------------------------
print("\nWas ein Token sagen muss")

NOW = 2_000_000_000


def jwt(payload):
    def seg(d):
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return seg({"alg": "RS256"}) + "." + seg(payload) + ".unterschrift"


GOOD = {"iss": "https://a.example/realms/x", "aud": "oaap-node",
        "sub": "S-1", "nonce": "N", "exp": NOW + 300}
ok("ein stimmiges Token wird angenommen",
   not idp.claims_refusal(GOOD, GOOD["iss"], "oaap-node", "N", NOW))
for label, claims in (
        ("ein fremder Aussteller", dict(GOOD, iss="https://woanders")),
        ("ein fremder Empfaenger", dict(GOOD, aud="jemand-anders")),
        ("kein sub", dict(GOOD, sub="")),
        ("ein fremdes nonce", dict(GOOD, nonce="ANDERS")),
        ("ein abgelaufenes Token", dict(GOOD, exp=NOW - 3600))):
    ok(f"abgelehnt: {label}",
       idp.claims_refusal(claims, GOOD["iss"], "oaap-node", "N", NOW))
ok("ein aud als Liste wird verstanden",
   not idp.claims_refusal(dict(GOOD, aud=["x", "oaap-node"]), GOOD["iss"],
                          "oaap-node", "N", NOW))
ok("aus einem stimmigen Token werden die Angaben gelesen",
   idp.jwt_claims(jwt(GOOD)) == GOOD)
for junk in ("", "abc", "a.b", "a.!!!.c"):
    ok(f"kein Token: {junk or '(leer)'}", idp.jwt_claims(junk) is None)

ok("ein behaupteter zweiter Faktor wird notiert",
   "otp" in idp.second_factor({"amr": ["pwd", "otp"]}))
ok("schweigt der Anbieter, wird nichts behauptet",
   idp.second_factor({}) == "")

# ---------------------------------------------------------------------------
print("\nWer den Schalter umlegen darf (K4b)")

ok("ein server_admin darf",
   idp.policy_target("server_admin", "T-OP", "T-HBVP") == ("T-HBVP", ""))
ok("ein server_admin ohne Ziel nimmt den eigenen Mandanten",
   idp.policy_target("server_admin", "T-OP", "") == ("T-OP", ""))
for role in ("tenant_admin", "support", "admin", "user", ""):
    tid, refusal = idp.policy_target(role, "T-HBVP", "T-HBVP")
    ok(f"'{role or '(keine Rolle)'}' darf nicht", tid == "" and refusal,
       (tid, refusal))

# ---------------------------------------------------------------------------
print("\nWas eine Richtlinie sein darf")

ok("die Vorgabe eines unkonfigurierten Mandanten ist 'eingang'",
   idp.policy_of({})["first_login"] == "eingang"
   and idp.policy_of({})["default_role"] == ""
   and idp.policy_of({})["set"] is False)
ok("'eingang' ist vollstaendig ohne Rolle",
   not idp.policy_refusal("eingang", "", False))
ok("'role' ohne Rolle wird abgelehnt", idp.policy_refusal("role", "", False))
for role in idp.NEVER_AT_FIRST_LOGIN:
    ok(f"'{role}' wird nie bei einem ersten Login vergeben",
       idp.policy_refusal("role", role, False))
ok("eine app-seitige Rolle geht", not idp.policy_refusal("role", "user", False))
ok("Selbstregistrierung mit 'eingang' ist unbedenklich",
   not idp.policy_refusal("eingang", "", True))
ok("Selbstregistrierung mit 'role' wird ohne Begruendung abgelehnt",
   idp.policy_refusal("role", "user", True))
ok("... und mit Begruendung angenommen",
   not idp.policy_refusal("role", "user", True, None,
                          "Der Verein prueft die Aufnahme selbst"))
ok("eine Gruppenabbildung ohne 'groups' wird abgelehnt",
   idp.policy_refusal("role", "user", False, {"vorstand": "leitung"}))
ok("ein unbrauchbarer Gruppenname wird abgelehnt",
   idp.policy_refusal("groups", "user", False, {"vorstand": "LEITUNG!!"}))
# Die Liste der vergebbaren Rollen ist eine ERLAUBNIS-Liste. Eine Rolle,
# die die Plattform spaeter bekommt, ist damit nicht automatisch
# vergebbar -- der Fehler, den eine Verbotsliste machen wuerde.
DANGEROUS = set(place.NODE_WIDE_ROLES) | {"tenant_admin"}
ok("knotenweite Macht steht nicht auf der Erlaubnis-Liste",
   not (set(idp.GRANTABLE_AT_FIRST_LOGIN) & DANGEROUS),
   idp.GRANTABLE_AT_FIRST_LOGIN)
ok("die Verbotsliste nennt beide knotenweiten Rollen und tenant_admin",
   set(idp.NEVER_AT_FIRST_LOGIN) == DANGEROUS, idp.NEVER_AT_FIRST_LOGIN)

# ---------------------------------------------------------------------------
print("\nEin Name, den ein Anbieter vorschlaegt, ist ein Vorschlag")

ok("der Wunschname wird genommen, wenn er frei ist",
   idp.local_username({"preferred_username": "mueller"}, []) == "mueller")
ok("ein belegter Wunschname bekommt eine Nummer -- nie den fremden Satz",
   idp.local_username({"preferred_username": "mueller"}, ["mueller"])
   == "mueller-2")
ok("ohne Vorschlag wird aus dem sub ein Name",
   idp.local_username({"sub": "abc-123"}, []).startswith("user-"))
ok("ein unbrauchbarer Vorschlag faellt auf die E-Mail zurueck",
   idp.local_username({"preferred_username": "@@@", "email": "Anna@x.de"},
                      []) == "anna")

# ---------------------------------------------------------------------------
print("\nKein Rueckfall auf ein lokales Passwort")

ok("ein Satz ohne Passwort kann sich lokal nicht anmelden",
   not idp.has_local_password({"password_hash": ""}))
ok("... auch nicht mit einem Rest, der kein Hash ist",
   not idp.has_local_password({"password_hash": "geheim"}))
ok("ein echter Hash geht weiterhin",
   idp.has_local_password({"password_hash": "scrypt:32768:8:1$abc$def"}))
ok("die Anmeldemaske FRAGT diese Funktion",
   "idp.has_local_password(u)" in IDENTITY_SRC)

# ---------------------------------------------------------------------------
print("\nDas Geheimnis bleibt, wo es hingehoert")

sys.path.insert(0, PLATFORM)
import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.APPS_DIR, exist_ok=True)
default_id = m.ensure_default_tenant()
hbvp, _t = m.tenant_create("hbvp", name="Handball Verein Probe")
cls_id, _t = m.tenant_create("cls", name="Clausen GmbH")

ok("ein Anbieter mit unsicherem Kanal wird an der Maschine abgelehnt",
   m.tenant_set_provider(hbvp, "oidc", "http://auth.example.org/realms/hbvp",
                         "oaap-node", "x" * 32)[0] is False)

SECRET = "S3hr-geheim-und-lang-genug-fuer-alles"
good, msg = m.tenant_set_provider(
    hbvp, "oidc", f"http://127.0.0.1:1/realms/hbvp", "oaap-node", SECRET,
    label="Mit dem Vereinskonto anmelden")
ok("ein brauchbarer Anbieter wird angenommen", good, msg)
ok("die ERSTE Anbindung warnt nicht vor verlorenen Bindungen",
   "ISSUER CHANGED" not in msg and "no longer match" not in msg, msg)
moved, msg2 = m.tenant_set_provider(
    hbvp, "oidc", "http://127.0.0.1:2/realms/hbvp", "oaap-node", SECRET)
ok("ein WECHSEL des Ausstellers warnt sehr wohl",
   moved and "no longer match" in msg2, msg2)
m.tenant_set_provider(hbvp, "oidc", "http://127.0.0.1:1/realms/hbvp",
                      "oaap-node", SECRET)

stored = json.dumps(m.load_tenants()[hbvp])
ok("das Geheimnis steht NICHT im Mandantensatz", SECRET not in stored, stored)
ok("das Anbieter-Objekt steht im Mandantensatz",
   "127.0.0.1" in stored and "oaap-node" in stored)
ok("provider_of() gibt nie ein Geheimnis heraus",
   SECRET not in json.dumps(idp.provider_of(m.load_tenants()[hbvp])))
ok("das Geheimnis liegt in seiner eigenen Datei",
   m.load_idp_secrets().get(hbvp, {}).get("client_secret") == SECRET)
if os.name == "posix":
    mode = os.stat(m.IDP_SECRETS_FILE).st_mode & 0o777
    ok("und zwar 0600", mode == 0o600, oct(mode))
else:
    ok("und zwar 0600 (nur auf POSIX pruefbar)", True)

# Die Mandantendatei ist 0644 und reist im Mandantenarchiv. Deshalb
# darf das Geheimnis dort nie landen -- ein Geheimnis in einer Sicherung
# ist ein Geheimnis in jeder Kopie dieser Sicherung.
ok("tenants.json bleibt lesbar fuer die Dienste, die es brauchen",
   os.path.isfile(m.TENANTS_FILE))

compose = read(PLATFORM, "docker-compose.yml")
mounts = {}
current = None
for line in compose.splitlines():
    svc = re.match(r"^  ([a-z][a-z0-9-]*):\s*$", line)
    if svc:
        current = svc.group(1)
    hit = re.search(r'^\s*-\s*"?([^":]+):([^":]+?)(:ro)?"?\s*$', line)
    if hit and current:
        mounts.setdefault(current, []).append(
            (hit.group(1), hit.group(2), bool(hit.group(3))))

idp_mounts = [(svc, src, dst, ro) for svc, lst in mounts.items()
              for src, dst, ro in lst if "/data/idp" in src]
ok("genau EIN Dienst haengt das Geheimnis-Verzeichnis ein",
   len(idp_mounts) == 1, idp_mounts)
ok("und das ist der Anmeldedienst, lesend",
   idp_mounts and idp_mounts[0][0] == "identity" and idp_mounts[0][3],
   idp_mounts)
ok("das Portal haengt es NICHT ein",
   not any(svc == "portal" for svc, *_ in idp_mounts), idp_mounts)
ok("die Sicht des Portals traegt kein Geheimnis",
   "client_secret" not in PORTAL_SRC, "client_secret kommt in portal/app.py vor")

# Und der Umkehrschluss zu 0.1.118: das Verzeichnis, das eingehaengt
# wird, muss vor dem Start existieren -- also wird es angelegt.
ok("eine Migration legt das Verzeichnis an",
   "migrate-idp-dir" in read(PLATFORM, "migrate.sh")
   and "def cmd_migrate_idp_dir" in APPCTL_SRC)

# ---------------------------------------------------------------------------
print("\nDie Richtlinie an der Maschine")

good, msg = m.tenant_set_policy(hbvp, first_login="role", default_role="user")
ok("eine gueltige Richtlinie wird geschrieben", good, msg)
ok("der Mandantensatz kennt sie danach",
   idp.policy_of(m.load_tenants()[hbvp])["default_role"] == "user")
bad, msg = m.tenant_set_policy(hbvp, self_registration=True)
ok("Selbstregistrierung ueber eine bestehende 'role' wird abgelehnt -- die "
   "Kombination zaehlt, nicht der einzelne Aufruf", bad is False, msg)
good, msg = m.tenant_set_policy(hbvp, self_registration=True,
                                reason="Der Verein prueft die Aufnahme selbst")
ok("... mit Begruendung angenommen", good, msg)
log = read(m.TENANT_LOG) if os.path.isfile(m.TENANT_LOG) else ""
ok("die Begruendung steht im Protokoll DIESES Mandanten",
   "Der Verein prueft die Aufnahme selbst" in log and '"tenant.idp-policy"' in log,
   log[-400:])
ok("das Anhaengen eines Anbieters steht ebenfalls dort",
   '"tenant.idp"' in log)

m.tenant_set_policy(hbvp, first_login="eingang", default_role="",
                    self_registration=False)

# ---------------------------------------------------------------------------
print("\nDer ganze Weg: ein erfundener Anbieter, ein echter Durchlauf")

CLAIMS = {}


class Provider(BaseHTTPRequestHandler):
    """Gerade genug OIDC, um den Durchlauf echt zu machen."""

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/.well-known/openid-configuration"):
            return self._json({
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/auth",
                "token_endpoint": ISSUER + "/token",
                "token_endpoint_auth_methods_supported":
                    ["client_secret_basic", "client_secret_post"],
            })
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("ascii")
        SEEN["token_request"] = body
        SEEN["authorization"] = self.headers.get("Authorization", "")
        self._json({"id_token": jwt(CLAIMS), "token_type": "Bearer"})


SEEN = {}
srv = HTTPServer(("127.0.0.1", 0), Provider)
ISSUER = f"http://127.0.0.1:{srv.server_port}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
ID_DATA = tempfile.mkdtemp(prefix="oaap-idp-id-")
os.environ["OAAP_IDENTITY_DATA_DIR"] = ID_DATA

import importlib.util                                          # noqa: E402

spec = importlib.util.spec_from_file_location(
    "identity_app_idp", os.path.join(SERVICES, "identity", "app.py"))
ident = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ident)

TENANTS = {
    default_id: {"label": "default", "former_labels": []},
    hbvp: {"label": "hbvp", "name": "Handball Verein Probe",
           "former_labels": [],
           "idp": {"kind": "oidc", "issuer": ISSUER, "client_id": "oaap-node"},
           "idp_policy": {"first_login": "role", "default_role": "user"}},
    cls_id: {"label": "cls", "name": "Clausen GmbH", "former_labels": []},
}
ident.known_tenants = lambda: TENANTS
ident._external_host = lambda: HOST
ident.default_tenant_id = lambda: default_id
ident.AUDIT_LOG = os.path.join(ID_DATA, "tenant-log.jsonl")
SECRETS = os.path.join(ID_DATA, "secrets.json")
with _io.open(SECRETS, "w", encoding="utf-8") as f:
    json.dump({"providers": {hbvp: {"client_secret": SECRET}}}, f)
ident.IDP_SECRETS_FILE = SECRETS
ident.app.config["TESTING"] = True
c = ident.app.test_client()

# Der Koeder. Ein Satz, der dem gleich Ankommenden in allem gleicht,
# was ein Anbieter behaupten kann -- E-Mail, Anzeigename, sogar ein
# passender Benutzername -- und dem genau das eine fehlt, was zaehlt:
# eine Bindung. Wird er uebernommen, hat jemand ein fremdes Konto
# bekommen, indem er eine E-Mail-Adresse behauptet hat.
ident.save_users([{
    "username": "mueller", "display_name": "Maria Mueller",
    "email": "m@example.org", "email_verified": True,
    "password_hash": "scrypt:32768:8:1$abc$def", "roles": ["tenant_admin"],
    "groups": [], "active": True, "kind": "human", "tenant": hbvp,
    "id": "11111111-1111-1111-1111-111111111111", "session_epoch": 0}])

body = c.get("/auth/login", headers={"Host": f"hbvp.{HOST}"}).get_data(
    as_text=True)
ok("am Ort des Mandanten bietet die Anmeldeseite seinen Dienst an",
   "Mit dem Vereinskonto anmelden" in body and "/auth/oidc/start" in body,
   body[:400])
body = c.get("/auth/login", headers={"Host": f"cls.{HOST}"}).get_data(
    as_text=True)
ok("am Ort eines Mandanten OHNE Dienst steht kein Knopf",
   "/auth/oidc/start" not in body, body[:400])
body = c.get("/auth/login", headers={"Host": HOST}).get_data(as_text=True)
ok("an der Wurzel (Standard-Mandant, kein Dienst) auch nicht",
   "/auth/oidc/start" not in body, body[:400])

r = c.get("/auth/oidc/start", headers={"Host": f"hbvp.{HOST}"})
ok("der Start leitet zum Anbieter weiter", r.status_code == 303, r.status_code)
target = r.headers.get("Location", "")
ok("und zwar an dessen Autorisierungs-Endpunkt",
   target.startswith(ISSUER + "/auth?"), target[:200])
params = dict(p.split("=", 1) for p in target.split("?", 1)[1].split("&"))
ok("mit PKCE", params.get("code_challenge_method") == "S256"
   and len(params.get("code_challenge", "")) > 20, params)
ok("mit einer Rueckkehradresse an DIESEM Ort",
   "hbvp." in params.get("redirect_uri", "").replace("%2F", "/")
   .replace("%3A", ":"), params.get("redirect_uri"))

from urllib.parse import unquote                               # noqa: E402

state, nonce = unquote(params["state"]), unquote(params["nonce"])

# Erst der Fall, der nichts werden darf: ein Token mit fremdem nonce.
CLAIMS.clear()
CLAIMS.update({"iss": ISSUER, "aud": "oaap-node", "sub": "S-PROBE",
               "nonce": "ein-anderes", "exp": int(time.time()) + 300,
               "preferred_username": "mueller"})
r = c.get(f"/auth/oidc/callback?code=X&state={params['state']}",
          headers={"Host": f"hbvp.{HOST}"})
ok("ein Token, das nicht zu diesem Versuch gehoert, wird abgewiesen",
   r.status_code == 403, r.status_code)
ok("und es entsteht kein neuer Benutzersatz",
   len(ident.load_users()) == 1, ident.load_users())

# Jetzt der echte Durchlauf.
r = c.get("/auth/oidc/start", headers={"Host": f"hbvp.{HOST}"})
params = dict(p.split("=", 1)
              for p in r.headers["Location"].split("?", 1)[1].split("&"))
CLAIMS.clear()
CLAIMS.update({"iss": ISSUER, "aud": "oaap-node", "sub": "S-PROBE",
               "nonce": unquote(params["nonce"]),
               "exp": int(time.time()) + 300,
               "preferred_username": "mueller", "email": "m@example.org",
               "email_verified": True, "name": "Maria Mueller",
               "amr": ["pwd", "otp"],
               "groups": ["server_admin", "vorstand"]})
r = c.get(f"/auth/oidc/callback?code=DERCODE&state={params['state']}",
          headers={"Host": f"hbvp.{HOST}"})
ok("die Rueckmeldung wird angenommen", r.status_code == 303,
   (r.status_code, r.get_data(as_text=True)[:300]))
ok("der Code wurde gegen ein Token getauscht, mit PKCE-Nachweis",
   "code_verifier=" in SEEN.get("token_request", ""), SEEN.get("token_request"))
ok("und das Client-Geheimnis reiste als Basic-Anmeldung, nicht im Klartext "
   "in der Adresse", SEEN.get("authorization", "").startswith("Basic "),
   SEEN.get("authorization"))

users = ident.load_users()
ok("ein NEUER Satz ist entstanden, der Koeder wurde nicht uebernommen",
   len(users) == 2, users)
bait = next((x for x in users if x.get("id", "").startswith("1111")), {})
ok("der Koeder blieb ungebunden -- eine gleiche E-Mail uebernimmt kein "
   "Konto", not bait.get("idp"), bait)
ok("und behielt seine Rolle",
   bait.get("roles") == ["tenant_admin"], bait.get("roles"))
u = next((x for x in users if (x.get("idp") or {}).get("subject")
          == "S-PROBE"), {})
ok("der neue Satz bekam einen eigenen Namen, nicht den des Koeders",
   u.get("username") and u.get("username") != "mueller", u.get("username"))
ok("er gehoert dem richtigen Mandanten", u.get("tenant") == hbvp, u.get("tenant"))
ok("er ist an (Anbieter, sub) gebunden",
   (u.get("idp") or {}).get("subject") == "S-PROBE"
   and (u.get("idp") or {}).get("provider") == f"oidc|{ISSUER}", u.get("idp"))
ok("er traegt die Rolle aus der Richtlinie -- und nur die",
   u.get("roles") == ["user"], u.get("roles"))
ok("die Gruppe 'server_admin' aus der Behauptung wirkte NICHT",
   not (u.get("groups") or []), u.get("groups"))
ok("er hat kein lokales Passwort", not idp.has_local_password(u))
ok("er hat eine eigene Kennung (RFC-0040)", len(u.get("id", "")) == 36, u.get("id"))
ok("Anzeigename und Adresse kamen mit", u.get("display_name") == "Maria Mueller"
   and u.get("email") == "m@example.org", u)
idlog = read(ident.AUDIT_LOG) if os.path.isfile(ident.AUDIT_LOG) else ""
ok("der erste Login steht im Protokoll des Mandanten",
   "user.idp-first-login" in idlog, idlog[-300:])
ok("und der behauptete zweite Faktor ist darin notiert",
   "otp" in idlog, idlog[-300:])

# Zweiter Durchlauf: derselbe Mensch bekommt denselben Satz, keinen neuen.
r = c.get("/auth/oidc/start", headers={"Host": f"hbvp.{HOST}"})
params = dict(p.split("=", 1)
              for p in r.headers["Location"].split("?", 1)[1].split("&"))
CLAIMS["nonce"] = unquote(params["nonce"])
CLAIMS["exp"] = int(time.time()) + 300
c.get(f"/auth/oidc/callback?code=DERCODE&state={params['state']}",
      headers={"Host": f"hbvp.{HOST}"})
ok("eine zweite Anmeldung legt keinen dritten Satz an",
   len(ident.load_users()) == 2, ident.load_users())

# Was eine geloeste Bindung WIRKLICH bedeutet. Gemessen auf oaap-test:
# eine erneute Anmeldung findet nicht zurueck, sie legt einen NEUEN Satz
# an. Das ist kein Fehler, sondern die einzige Folge, die zu K4 passt --
# die Alternative waere, jemanden an seinem NAMEN wiederzuerkennen, und
# genau das ist die Kontouebernahme, die eine Funktion weiter oben
# abgelehnt wird. Der Befehl an der Maschine sagt das inzwischen.
users = ident.load_users()
for x in users:
    if (x.get("idp") or {}).get("subject") == "S-PROBE":
        x.pop("idp")
        x["active"] = False
ident.save_users(users)
r = c.get("/auth/oidc/start", headers={"Host": f"hbvp.{HOST}"})
params = dict(p.split("=", 1)
              for p in r.headers["Location"].split("?", 1)[1].split("&"))
CLAIMS["nonce"] = unquote(params["nonce"])
CLAIMS["exp"] = int(time.time()) + 300
c.get(f"/auth/oidc/callback?code=DERCODE&state={params['state']}",
      headers={"Host": f"hbvp.{HOST}"})
after = ident.load_users()
ok("nach dem Loesen der Bindung entsteht ein NEUER Satz, kein Rueckweg "
   "in den alten", len(after) == 3, [x["username"] for x in after])
ok("der geloeste Satz bleibt deaktiviert und ungebunden",
   any(not x.get("active") and not x.get("idp")
       and x.get("username") == "mueller-2" for x in after),
   [(x["username"], x.get("active"), bool(x.get("idp"))) for x in after])
ok("der Befehl an der Maschine sagt genau das",
   "a NEW account" in APPCTL_SRC and "matching by name" in APPCTL_SRC)
ok("und er deaktiviert einen Satz ohne lokales Passwort",
   "--keep-active" in APPCTL_SRC and "was DEACTIVATED" in APPCTL_SRC)

# Und das lokale Formular bleibt fuer den Anbieter-Satz verschlossen.
r = c.post("/auth/login",
           data={"username": "mueller-2", "password": ""},
           headers={"Host": f"hbvp.{HOST}"})
ok("das lokale Anmeldeformular laesst diesen Satz nicht herein",
   r.status_code == 401, r.status_code)

srv.shutdown()

# ---------------------------------------------------------------------------
print("\nDer Quelltext bleibt lesbar")
for f in ("idp.py", "portal/app.py", "identity/app.py"):
    src = read(SERVICES, *f.split("/"))
    try:
        ast.parse(src)
        good = True
    except SyntaxError as e:
        good, src = False, str(e)
    ok(f"{f} ist gueltiges Python", good, src[:200])

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
