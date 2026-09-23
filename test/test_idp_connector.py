#!/usr/bin/env python3
"""OAAP legt den Realm selbst an (RFC-0041 K3, Schritt 4).

K3 wurde GEGEN die Empfehlung entschieden: OAAP verwaltet Realms ueber
Keycloaks Verwaltungsschnittstelle, statt nur einen zu benutzen, den
jemand von Hand gebaut hat. Der Einwand, der dabei ueberstimmt wurde,
verschwindet nicht -- er wird zur Auflage. Genau die wird hier geprueft.

Und eine Gestaltentscheidung von Joerg (23.09.2026) steht mit auf dem
Pruefstand: Keycloak ist der ERSTE Konnektor mit einer Schnittstelle,
nicht *die* Keycloak-Anbindung. Ein zweites SSO-Produkt soll eine Datei
und zwei Zeilen in einer Tabelle sein.

    Der Vertrag      Jede Art im Verzeichnis kann alle vier Verben und
                     hat ihre Adressen, ihre Version-Stelle und ihre
                     beiden Anfragekoerper. Eine Art ohne das faellt
                     hier auf und nicht bei einem Betreiber.
    Nichts loescht   K3.3: verwalten ist nicht besitzen. Die Regel
                     steht als Funktion AUF dem Weg jedes Aufrufs, und
                     der Test fuehrt sie aus statt sie zu lesen.
    Die Reihenfolge  Die Version wird geprueft, BEVOR etwas entsteht.
                     Ein Plan, der frueher schreibt, wird abgelehnt.
    Der Kanal        Dieselbe Regel wie beim Aussteller, aus einem
                     schaerferen Grund: hier reist eine Vollmacht, die
                     Realms anlegen kann.
    Die Vollmacht    Sie liegt 0600 in einem Verzeichnis, das KEIN
                     Container einhaengt -- auch der Anmeldedienst
                     nicht, der die Client-Geheimnisse hat.
    Der Abbruch      Die schaerfste Probe: ein Server mit falscher
                     Fassung. OAAP muss ablehnen UND nichts angelegt
                     haben -- gezaehlt wird beim erfundenen Server.
    Der ganze Weg    Ein erfundenes Keycloak, ein echter Durchlauf:
                     Realm anlegen, Client anlegen, Geheimnis abholen,
                     Anbieter-Objekt schreiben. Und beim zweiten Mal
                     nichts noch einmal anlegen.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_idp_connector.py
"""
import json
import os
import re
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-idpadm-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, SERVICES)
sys.path.insert(0, PLATFORM)

import idp                                                     # noqa: E402
import idp_admin as a                                          # noqa: E402

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


ADMIN_SRC = read(SERVICES, "idp_admin.py")
APPCTL_SRC = read(PLATFORM, "appctl.py")

# ---------------------------------------------------------------------------
print("Der Vertrag: ein Konnektor ist vier Verben")

ok("es gibt mindestens eine Art", len(a.connector_kinds()) >= 1,
   a.connector_kinds())
for kind in a.connector_kinds():
    decl = a.connector_of(kind)
    ok(f"'{kind}' kann alle vier Verben", not a.missing_verbs(kind),
       a.missing_verbs(kind))
    ok(f"'{kind}' hat seine Anfragekoerper", kind in a.body_kinds(),
       a.body_kinds())
    for name in ("spaces", "space", "clients", "client", "client_secret"):
        ok(f"'{kind}' nennt die Adresse '{name}'",
           bool(a.path_of(kind, name, space="x", uuid="y")))
    ok(f"'{kind}' sagt, wo der Server seine Fassung nennt",
       bool(decl.get("version_path")) and bool(decl.get("version_field")))
    ok(f"'{kind}' ist auf eine Fassung festgenagelt",
       re.match(r"^\d+\.\d+", decl.get("pinned", "")), decl.get("pinned"))
    ok(f"'{kind}' sagt, was es NOCH nicht kann",
       "settings" in (decl.get("later") or ()), decl.get("later"))

# Ein genanntes und nicht gebautes Verb ist die kleinere Luege -- aber
# nur, solange es auch wirklich nicht gebaut ist. Waere es da, muesste
# es im Vertrag stehen und nicht in `later`.
ok("'settings' ist genannt und NICHT gebaut (das ist Schritt 6)",
   "def set_settings" not in ADMIN_SRC and "settings" not in a.REQUIRED_VERBS)
ok("eine unbekannte Art wird abgelehnt", a.kind_refusal("okta"))
ok("die Plattform-Version steht an EINER Stelle",
   a.connector_of("keycloak")["pinned"] == idp.KEYCLOAK_PINNED)
ok("der Konnektor nennt keine eigene Zweitregel fuer den Kanal",
   "def channel_refusal" not in ADMIN_SRC
   and "idp.channel_refusal" in ADMIN_SRC)

# ---------------------------------------------------------------------------
print("\nNichts loescht: verwalten ist nicht besitzen (K3.3)")

ok("DELETE wird abgelehnt", a.method_refusal("DELETE"))
ok("PATCH auch", a.method_refusal("PATCH"))
for good in ("GET", "POST", "PUT", "get"):
    ok(f"{good} ist erlaubt", not a.method_refusal(good))
ok("die Regel steht als Funktion, nicht als fehlende Zeile",
   "def method_refusal" in ADMIN_SRC)
ok("und sie liegt AUF dem Weg jedes Aufrufs",
   re.search(r"def _call\(.*?method_refusal\(method\)", ADMIN_SRC, re.S))

# ---------------------------------------------------------------------------
print("\nDie Reihenfolge: erst fragen, dann anlegen (K3.2)")

PLAN = a.provision_plan("keycloak", "hbvp", "oaap-node")
ok("der Plan ist nicht leer", len(PLAN) >= 5, len(PLAN))
ok("er wird angenommen", not a.plan_refusal(PLAN), a.plan_refusal(PLAN))
ok("der erste Schritt fragt nach der Fassung", PLAN[0]["verb"] == "version")
ok("und er schreibt nichts", not PLAN[0]["writes"])
ok("jeder Schritt sagt, WOZU", all(s.get("why") for s in PLAN))
ok("kein Schritt loescht", all(not a.method_refusal(s["method"])
                               for s in PLAN))

# Die Regel ausgefuehrt, nicht gelesen: ein Plan, dem die Frage nach
# der Fassung fehlt, wird abgelehnt -- und einer, der vorher schreibt.
ok("ein Plan ohne Fassungsfrage wird abgelehnt",
   a.plan_refusal(PLAN[1:]))
ok("ein Plan, der vor der Fassungsfrage schreibt, wird abgelehnt",
   a.plan_refusal([dict(PLAN[2]), dict(PLAN[0])]))
ok("ein Plan mit DELETE wird abgelehnt",
   a.plan_refusal([dict(PLAN[0]), dict(PLAN[1], method="DELETE")]))
ok("ein Schritt ohne Adresse wird abgelehnt",
   a.plan_refusal([dict(PLAN[0]), dict(PLAN[1], path="")]))
ok("der Plan laesst sich einem Menschen vorlesen",
   len(a.plan_lines(PLAN)) == 2 * len(PLAN))
ok("und --dry-run gibt es wirklich", "--dry-run" in APPCTL_SRC
   and "args.dry_run" in APPCTL_SRC)

# ---------------------------------------------------------------------------
print("\nDie Fassung wird laut geprueft, nicht nur aufgeschrieben (K3.1)")

PIN = a.connector_of("keycloak")["pinned"]
ok("die festgenagelte Fassung geht durch", not a.version_refusal("keycloak", PIN))
bad = a.version_refusal("keycloak", "26.8.0")
ok("eine andere wird abgelehnt", bool(bad))
ok("... und die Ablehnung nennt BEIDE Zahlen",
   PIN in bad and "26.8.0" in bad, bad)
ok("... und den Aufruf, der gefragt hat", "/admin/serverinfo" in bad, bad)
ok("... und den Weg nach vorn", "--accept-version" in bad, bad)
ok("wer die Zahl TIPPT, kommt durch",
   not a.version_refusal("keycloak", "26.8.0", "26.8.0"))
ok("eine andere getippte Zahl hilft nicht",
   a.version_refusal("keycloak", "26.9.0", "26.8.0"))
ok("ein Server, der seine Fassung verschweigt, wird abgelehnt",
   a.version_refusal("keycloak", ""))

# Der dritte Ausgang, an der Maschine gefunden und nicht im Entwurf:
# /admin/serverinfo antwortet einer engen Vollmacht OHNE Fassung. K3.1
# will sie pruefbar, K3.4 will die Vollmacht eng -- beides zusammen gibt
# es bei Keycloak 26.7.4 nicht. Also: ein Mensch nennt die Zahl, und
# OAAP sagt ueberall dazu, dass sie genannt und nicht gelesen wurde.
how, bad = a.version_check("keycloak", PIN)
ok("gelesen heisst gemessen", how == a.MEASURED and not bad)
how, bad = a.version_check("keycloak", "", PIN)
ok("verschwiegen plus getippt heisst BEHAUPTET",
   how == a.ASSERTED and not bad, (how, bad))
how, bad = a.version_check("keycloak", "", "")
ok("verschwiegen und nichts getippt wird abgelehnt", not how and bool(bad))
ok("... und die Ablehnung nennt die gemessene Ursache",
   "trimmed" in bad and "2026-09-23" in bad, bad)
# Die Regel, die eine Behauptung ungefaehrlich macht: sie schlaegt eine
# Messung nie. Sagt der Server etwas, gilt das -- auch gegen den Zettel.
how, bad = a.version_check("keycloak", "26.9.9", PIN)
ok("eine Behauptung schlaegt eine Messung NICHT", not how and bool(bad),
   (how, bad))
ok("das Wort fuer eine gelesene Zahl sagt, woher sie kommt",
   "/admin/serverinfo" in a.version_words(a.MEASURED, PIN, "keycloak"))
ok("das Wort fuer eine genannte Zahl sagt, dass sie genannt ist",
   "STATED" in a.version_words(a.ASSERTED, PIN, "keycloak"))
ok("die Fassung wird an der Stelle gelesen, die der Vertrag nennt",
   a.version_said("keycloak", {"systemInfo": {"version": "26.7.4"}})
   == "26.7.4")
ok("ein Dokument ohne diese Stelle ergibt nichts",
   a.version_said("keycloak", {"version": "26.7.4"}) == "")

# ---------------------------------------------------------------------------
print("\nDer Kanal, jetzt mit einer Vollmacht darauf")

CRED = "V0llmacht-lang-genug"
ok("https geht", not a.connector_refusal("keycloak", "kc",
                                         "https://auth.example.org",
                                         "client", "oaap-admin", CRED))
bad = a.connector_refusal("keycloak", "kc", "http://auth.example.org",
                          "client", "oaap-admin", CRED)
ok("http aus dem Internet wird abgelehnt", bool(bad))
ok("... und die Ablehnung sagt, WAS hier reist",
   "create realm" in bad, bad)
ok("http auf einen Container geht", not a.connector_refusal(
    "keycloak", "kc", "http://oaap-app-auth-keycloak:8080", "client",
    "oaap-admin", CRED))
ok("http auf 127.0.0.1 geht", not a.connector_refusal(
    "keycloak", "kc", "http://127.0.0.1:8113", "client", "oaap-admin", CRED))
ok("eine Adresse mit Schraegstrich am Ende wird abgelehnt",
   a.connector_refusal("keycloak", "kc", "https://auth.example.org/",
                       "client", "oaap-admin", CRED))
ok("ein zu kurzes Geheimnis wird abgelehnt",
   a.connector_refusal("keycloak", "kc", "https://auth.example.org",
                       "client", "oaap-admin", "kurz"))
ok("eine unbekannte Vollmachtsform wird abgelehnt",
   a.connector_refusal("keycloak", "kc", "https://auth.example.org",
                       "kerberos", "oaap-admin", CRED))
ok("ein unbrauchbarer Konnektorname wird abgelehnt",
   a.connector_refusal("keycloak", "Kc Eins", "https://auth.example.org",
                       "client", "oaap-admin", CRED))

# K3.4 verlangte eine Vollmacht, die NIE die des Master-Realms ist. Beim
# Bauen zeigte sich, dass ein Realm ANLEGEN ein Akt im Master-Realm ist.
# Was bleibt, ist: benennen statt stillschweigen.
note_client = a.credential_note("keycloak", "client")
note_pass = a.credential_note("keycloak", "password")
ok("ein Dienstkonto wird als solches benannt",
   "create-realm" in note_client and "nothing else" in note_client,
   note_client)
ok("ein Benutzerkonto sagt, was es KOSTET",
   "every club" in note_pass and "K3.4" in note_pass, note_pass)
ok("und beide Saetze sind verschieden", note_client != note_pass)

# ---------------------------------------------------------------------------
print("\nNamen: der Realm heisst wie der Mandant, weil er umzieht")

ok("der Realm eines Mandanten ist sein Kuerzel",
   a.space_for("HBVP") == "hbvp")
ok("der Client heisst nach dem KNOTEN",
   a.client_for("oaap.joomp.de") == "oaap-oaap.joomp.de")
ok("'master' wird abgelehnt", a.space_refusal("keycloak", "master"))
ok("... und der Satz sagt warum",
   "administers the server" in a.space_refusal("keycloak", "master"))
ok("ein leerer Name wird abgelehnt", a.space_refusal("keycloak", ""))
ok("Grossbuchstaben werden abgelehnt", a.space_refusal("keycloak", "HBVP"))
ok("ein brauchbarer Name geht", not a.space_refusal("keycloak", "hbvp"))

URIS = a.redirect_uris_for("hbvp", HOST)
ok("die Rueckkehradresse liegt am ORT des Mandanten",
   all(u.split("://")[1].startswith("hbvp." + HOST) for u in URIS), URIS)
ok("und es sind BEIDE Schemata -- das Schema ist das des Browsers",
   {u.split(":")[0] for u in URIS} == {"http", "https"}, URIS)
ok("ohne externen Namen gibt es keine", a.redirect_uris_for("hbvp", "") == [])
ok("der Aussteller wird aus Adresse und Realm gebildet",
   a.issuer_for("keycloak", "https://auth.x/", "hbvp")
   == "https://auth.x/realms/hbvp")
ok("und er ist einer, den der Anmeldedienst annimmt",
   not idp.issuer_refusal(a.issuer_for("keycloak", "https://auth.x", "hbvp")))

BODY = a.space_body("keycloak", "hbvp", "Handball Verein Probe")
ok("der neue Realm ist an", BODY.get("enabled") is True)
ok("Selbstregistrierung ist AUS -- das ist Schritt 6, nicht dieser",
   BODY.get("registrationAllowed") is False, BODY)
CBODY = a.client_body("keycloak", "oaap-node", URIS)
ok("der Client ist vertraulich", CBODY.get("publicClient") is False)
ok("... und das ist die Voraussetzung des ganzen Weges",
   "publicClient" in ADMIN_SRC and "signature" in ADMIN_SRC)
ok("nur der Standard-Fluss ist an",
   CBODY.get("standardFlowEnabled") is True
   and CBODY.get("implicitFlowEnabled") is False
   and CBODY.get("directAccessGrantsEnabled") is False, CBODY)
ok("und genau unsere Rueckkehradressen stehen drin",
   CBODY.get("redirectUris") == URIS, CBODY)
ok("eine unbekannte Art baut gar keinen Koerper",
   a.space_body("okta", "x") == {} and a.client_body("okta", "x", []) == {})

# ---------------------------------------------------------------------------
print("\nEin erfundenes Keycloak, und ein echter Durchlauf")

STATE = {"realms": {}, "clients": {}, "writes": 0, "version": PIN,
         "methods": [], "forbidden": set(), "trim": False,
         "clients_forbidden": set()}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _json(self, doc, status=200):
        raw = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        return self.headers.get("Authorization") == "Bearer fake-admin-token"

    def do_GET(self):
        STATE["methods"].append("GET " + self.path)
        path = self.path.split("?")[0]
        if not self._auth():
            return self._json({"error": "forbidden"}, 401)
        if path == "/admin/serverinfo":
            if STATE["trim"]:
                # Genau das, was eine enge Vollmacht bei Keycloak
                # 26.7.4 zurueckbekommt: ein Dokument ohne systemInfo.
                return self._json({"profileInfo": {"name": "default"}})
            return self._json({"systemInfo": {"version": STATE["version"]}})
        m = re.match(r"^/admin/realms/([^/]+)$", path)
        if m:
            if m.group(1) in STATE["forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            if m.group(1) in STATE["realms"]:
                return self._json({"realm": m.group(1)})
            return self._json({"error": "not found"}, 404)
        m = re.match(r"^/admin/realms/([^/]+)/clients$", path)
        if m:
            if m.group(1) in STATE["clients_forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            hit = re.search(r"clientId=([^&]+)", self.path)
            want = hit.group(1) if hit else ""
            found = [c for c in STATE["clients"].values()
                     if c["realm"] == m.group(1) and c["clientId"] == want]
            return self._json(found)
        m = re.match(r"^/admin/realms/([^/]+)/clients/([^/]+)/client-secret$",
                     path)
        if m:
            return self._json({"type": "secret",
                               "value": "geheim-vom-anbieter-" + m.group(2)})
        return self._json({"error": "not found"}, 404)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("ascii")

    def do_POST(self):
        STATE["methods"].append("POST " + self.path)
        body = self._body()
        if self.path.endswith("/protocol/openid-connect/token"):
            return self._json({"access_token": "fake-admin-token"})
        if not self._auth():
            return self._json({"error": "forbidden"}, 401)
        STATE["writes"] += 1
        if self.path == "/admin/realms":
            STATE["realms"][body["realm"]] = body
            return self._json({}, 201)
        m = re.match(r"^/admin/realms/([^/]+)/clients$", self.path)
        if m:
            uid = "cid-%d" % (len(STATE["clients"]) + 1)
            STATE["clients"][uid] = dict(body, id=uid, realm=m.group(1))
            return self._json({}, 201)
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        STATE["methods"].append("PUT " + self.path)
        body = self._body()
        if not self._auth():
            return self._json({"error": "forbidden"}, 401)
        STATE["writes"] += 1
        m = re.match(r"^/admin/realms/([^/]+)/clients/([^/]+)$", self.path)
        if m and m.group(2) in STATE["clients"]:
            STATE["clients"][m.group(2)].update(body)
            return self._json({}, 204)
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        STATE["methods"].append("DELETE " + self.path)
        return self._json({}, 204)


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_port}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()
hbvp, _t = m.tenant_create("hbvp", name="Handball Verein Probe")
os.makedirs(os.path.dirname(m.EXTERNAL_FILE), exist_ok=True)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": HOST, "edge": ""}, f)

good, msg = m.connector_add("kc", "keycloak", BASE, "client", "oaap-admin",
                            "V0llmacht-lang-genug")
ok("der Konnektor wird angenommen", good, msg)
ok("und liegt in einer Datei fuer sich",
   os.path.isfile(m.IDP_CONNECTORS_FILE))
if os.name == "posix":
    mode = os.stat(m.IDP_CONNECTORS_FILE).st_mode & 0o777
    ok("und zwar 0600", mode == 0o600, oct(mode))
else:
    ok("und zwar 0600 (nur auf POSIX pruefbar)",
       "os.O_CREAT | os.O_TRUNC, 0o600" in APPCTL_SRC)


def provision(**kw):
    args = types.SimpleNamespace(
        action="provision", name="kc", tenant="hbvp", space=None,
        client_id=None, idp_label="Mit dem Vereinskonto anmelden",
        accept_version=None, dry_run=False)
    for k, v in kw.items():
        setattr(args, k, v)
    return m.cmd_idp(args)


# Die schaerfste Probe zuerst, weil sie eine ZAHL hinterlaesst: Ein
# Server mit einer Fassung, gegen die nie gemessen wurde. K3.2 sagt,
# OAAP legt dann nichts halb an -- also wird beim Anbieter gezaehlt.
STATE["version"] = "26.8.0"
before = STATE["writes"]
try:
    provision()
    died = False
except SystemExit as e:
    died = e.code != 0
ok("eine fremde Fassung bricht ab", died)
ok("... und es wurde NICHTS angelegt", STATE["writes"] == before,
   STATE["methods"])
ok("... wirklich nichts: kein Realm, kein Client",
   not STATE["realms"] and not STATE["clients"])
ok("und der Mandant hat weiterhin keinen Anbieter",
   not idp.provider_of(m.load_tenants()[hbvp]))

# Dieselbe fremde Fassung, diesmal getippt.
provision(accept_version="26.8.0")
ok("wer die Zahl tippt, kommt durch", bool(STATE["realms"]), STATE["realms"])
ok("und der Mandantensatz haelt die GEMESSENE Fassung fest",
   idp.provider_of(m.load_tenants()[hbvp])["version"] == "26.8.0")

# Zurueck auf die festgenagelte Fassung, und der Durchlauf, der zaehlt.
STATE["version"] = PIN
STATE["realms"].clear()
STATE["clients"].clear()
STATE["writes"] = 0
STATE["methods"] = []
provision()

ok("der Realm heisst wie der Mandant", "hbvp" in STATE["realms"],
   list(STATE["realms"]))
ok("er ist an", STATE["realms"]["hbvp"].get("enabled") is True)
ok("und Selbstregistrierung ist darin AUS",
   STATE["realms"]["hbvp"].get("registrationAllowed") is False)
client = list(STATE["clients"].values())[0]
ok("der Client ist vertraulich", client.get("publicClient") is False)
ok("er kennt beide Rueckkehradressen",
   set(client.get("redirectUris") or []) == set(URIS),
   client.get("redirectUris"))

prov = idp.provider_of(m.load_tenants()[hbvp])
ok("der Mandant hat jetzt einen Anbieter", bool(prov), prov)
ok("der Aussteller zeigt auf seinen Realm",
   prov["issuer"] == BASE + "/realms/hbvp", prov)
ok("die gemessene Fassung steht dabei", prov["version"] == PIN, prov)
ok("und es steht dabei, WER das gemacht hat", prov["connector"] == "kc", prov)
ok("der Knopf traegt den gewaehlten Text",
   prov["label"] == "Mit dem Vereinskonto anmelden", prov)

stored = json.dumps(m.load_tenants()[hbvp])
held = m.load_idp_secrets().get(hbvp, {}).get("client_secret", "")
ok("das Geheimnis kam vom Anbieter", held.startswith("geheim-vom-anbieter-"),
   held)
ok("und steht NICHT im Mandantensatz", held not in stored, stored)
ok("und nicht im Anbieter-Objekt, das das Portal sieht",
   held not in json.dumps(prov))

# Die Vollmacht des Knotens hat in einem Mandantensatz nichts verloren.
# Das Client-Geheimnis eines Mandanten wandert schon nicht dorthin; das
# hier kann Realms ANLEGEN, also erst recht nicht.
ok("die Vollmacht des Konnektors steht in keinem Mandantensatz",
   "V0llmacht-lang-genug" not in json.dumps(m.load_tenants()))
ok("... und in keinem Anbieter-Objekt, das das Portal sieht",
   "V0llmacht-lang-genug" not in json.dumps(prov))

# Ein 403 ist NICHT "nicht da". Auf einer geteilten Maschine ist es der
# Realm eines anderen Vereins, und darueber anzulegen ist das Letzte,
# was jemand will.
STATE["forbidden"].add("fremd")
STATE["writes"] = 0
try:
    provision(space="fremd")
    died = False
except SystemExit as e:
    died = e.code != 0
ok("ein Realm, den diese Vollmacht nicht ansehen darf, bricht ab", died)
ok("... und es wurde nichts darueber angelegt", STATE["writes"] == 0,
   STATE["methods"][-4:])
STATE["forbidden"].clear()

# Und der Fall, der an der Maschine WIRKLICH auftrat (23.09.2026): Eine
# `create-realm`-Vollmacht darf SEHEN, dass ein fremder Realm existiert
# (200), und erst der Blick auf seine Clients wird abgelehnt. Der Satz
# war fuer die erste Tuer geschrieben und kam an der zweiten nie an.
STATE["realms"]["fremd"] = {"realm": "fremd"}
STATE["clients_forbidden"].add("fremd")
STATE["writes"] = 0
CAP = []
_print = print


def _catch(*args, **kw):
    CAP.append(" ".join(str(x) for x in args))
    _print(*args, **kw)


import builtins                                                # noqa: E402

builtins.print = _catch
try:
    provision(space="fremd")
    died = False
except SystemExit as e:
    died = e.code != 0
builtins.print = _print
said = " ".join(" ".join(CAP).split())
ok("ein Realm, dessen Clients uns nicht gehoeren, bricht ab", died)
ok("... und sagt DENSELBEN Satz wie an der ersten Tuer",
   "somebody else's club" in said and "K3.3" in said, said[-300:])
ok("... und legt nichts an", STATE["writes"] == 0)
STATE["clients_forbidden"].clear()
STATE["realms"].pop("fremd", None)

# Und derselbe Abbruch noch einmal, mit der Ursache, die an der
# Maschine gefunden wurde: der Server ANTWORTET, nennt aber keine
# Fassung. Auch das legt nichts an.
STATE["trim"] = True
STATE["writes"] = 0
try:
    provision(space="trimm")
    died = False
except SystemExit as e:
    died = e.code != 0
ok("ein Server, der die Fassung verschweigt, bricht ab", died)
ok("... und es wurde nichts angelegt", STATE["writes"] == 0)
ok("... und 'trimm' gibt es beim Anbieter nicht",
   "trimm" not in STATE["realms"], list(STATE["realms"]))
# Mit der genannten Zahl geht es -- und der Mandantensatz sagt, dass
# sie genannt und nicht gelesen wurde.
provision(space="trimm", accept_version=PIN)
ok("mit der genannten Zahl geht es", "trimm" in STATE["realms"])
prov_t = idp.provider_of(m.load_tenants()[hbvp])
ok("und der Satz haelt fest, dass sie BEHAUPTET war",
   prov_t["version_how"] == a.ASSERTED, prov_t)
STATE["trim"] = False
provision()
ok("wird sie danach wieder gelesen, steht das auch da",
   idp.provider_of(m.load_tenants()[hbvp])["version_how"] == a.MEASURED)

# Ein zweiter Durchlauf. K3.3: was OAAP vorfindet, nimmt es, wie es ist.
writes_before = STATE["writes"]
realm_before = dict(STATE["realms"]["hbvp"])
provision()
ok("ein zweiter Durchlauf legt nichts noch einmal an",
   STATE["writes"] == writes_before, STATE["methods"][-6:])
ok("und ruehrt den vorgefundenen Realm nicht an",
   STATE["realms"]["hbvp"] == realm_before)
ok("er hat den Realm aber SEHR WOHL angesehen",
   any(x.startswith("GET ") and "/admin/realms/hbvp" in x
       for x in STATE["methods"][-6:]))

# Ein Client, der schon da ist und unsere Adresse nicht kennt: seine
# eigenen Adressen bleiben, unsere kommen dazu. Verwalten, nicht
# besitzen -- eine Rueckkehradresse wegzunehmen bricht eine Anmeldung,
# an die gerade niemand denkt.
for c in STATE["clients"].values():
    c["redirectUris"] = ["https://etwas-anderes.example.org/cb"]
STATE["writes"] = 0
provision()
have = list(STATE["clients"].values())[0]["redirectUris"]
ok("unsere Adresse kommt dazu", set(URIS) <= set(have), have)
ok("und die fremde bleibt stehen",
   "https://etwas-anderes.example.org/cb" in have, have)
ok("dafuer war genau EIN Schreibzugriff noetig", STATE["writes"] == 1,
   STATE["methods"][-4:])

# --dry-run: der Plan wird gedruckt und NICHTS gerufen.
STATE["writes"] = 0
STATE["methods"] = []
provision(dry_run=True)
ok("--dry-run ruft gar nichts", STATE["methods"] == [], STATE["methods"])

# Und die Regel, die auf dem Weg liegt, ausgefuehrt statt gelesen.
admin = m.idp_admin.Admin("keycloak", BASE, "client", "oaap-admin",
                          "V0llmacht-lang-genug")
status, doc, err = admin._call("DELETE", "/admin/realms/hbvp")
ok("ein DELETE kommt gar nicht erst los", status == 0 and bool(err), err)
ok("... und der erfundene Server hat es nie gesehen",
   not any(x.startswith("DELETE") for x in STATE["methods"]), STATE["methods"])

# ---------------------------------------------------------------------------
print("\nDie Vollmacht liegt, wo ihr Leser ist -- und keinen Schritt weiter")

compose = read(PLATFORM, "docker-compose.yml")
ok("das Verzeichnis der Client-Geheimnisse wird genau einmal eingehaengt",
   compose.count("/platform-idp") == 1, compose.count("/platform-idp"))
ok("das Verzeichnis der Vollmachten wird NIRGENDS eingehaengt",
   "idp-admin" not in compose)
# Und es liegt auch wirklich woanders. Ohne diese Zeile waere die
# Zeile darueber gruen, sobald jemand die Datei in das Verzeichnis
# legt, das der Anmeldedienst einhaengt: "idp-admin" stuende dann
# nirgends mehr, und die Pruefung haette nichts mehr zu finden.
ok("die Vollmachten liegen nicht im eingehaengten Verzeichnis",
   not os.path.abspath(m.IDP_CONNECTORS_FILE).startswith(
       os.path.abspath(m.IDP_DIR) + os.sep),
   m.IDP_CONNECTORS_FILE)
for svc in ("identity", "portal"):
    dockerfile = read(SERVICES, svc, "Dockerfile")
    ok(f"{svc} traegt idp_admin.py nicht im Image",
       "idp_admin.py" not in dockerfile, dockerfile)
ok("nur appctl fragt danach",
   "import idp_admin" in APPCTL_SRC
   and "import idp_admin" not in read(SERVICES, "identity", "app.py")
   and "import idp_admin" not in read(SERVICES, "portal", "app.py"))
ok("und die Migration legt beide Verzeichnisse an",
   "ensure_idp_admin_dir()" in APPCTL_SRC
   and re.search(r"ensure_idp_dir\(\)\n    ensure_idp_admin_dir\(\)",
                 APPCTL_SRC))
ok("ein Konnektor vergessen heisst: beim Anbieter passiert nichts",
   "nothing at the provider was touched" in APPCTL_SRC)
good, _msg = m.connector_remove("kc")
ok("... und der Mandant meldet sich weiter an",
   good and bool(idp.provider_of(m.load_tenants()[hbvp])))

# ---------------------------------------------------------------------------
print("")
print(f"{'FAILED' if fails else 'OK'}: {fails} Fehler")
sys.exit(1 if fails else 0)
