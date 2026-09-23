#!/usr/bin/env python3
"""Der Umzug: ein Verein zieht auf seinen eigenen Knoten (RFC-0041 K6,
Schritt 7).

`oaap backup create --tenant` verspricht ausdruecklich NICHT, einen
Mandanten in einen laufenden Knoten zurueckzuspielen -- weil das ein
Verschmelzen waere, und jede Frage dabei still falsch beantwortet
werden kann. Schritt 7 baut die andere Richtung, und die ist genau
deshalb loesbar, weil das Ziel LEER ist.

Womit „leer" das tragende Wort ist. Also wird es nicht behauptet,
sondern gesucht -- Stueck fuer Stueck, und jeder Zusammenstoss wird
BEIM NAMEN genannt.

    Der Vertrag      'export' ist gebaut, 'later' ist leer, 'users'
                     bleibt abgeschworen.
    Die Tuer         Das erste Verb, das KEIN API-Aufruf ist. Gemessen:
                     der Export der Verwaltungsschnittstelle traegt
                     keinen einzigen Menschen.
    Die Zaehlprobe   Zwei von drei Tueren schreiben eine Datei ohne
                     Menschen und melden keinen Fehler. Also wird die
                     DATEI gezaehlt, nie das Werkzeug geglaubt.
    Das Geheimnis    Die Datei traegt jedes Passwort des Vereins. Wohin
                     sie darf und wohin nicht, entscheidet eine Regel.
    Leer             Mandant, Name, frueherer Name, Instanz, Port,
                     Mensch -- jeder Zusammenstoss wird genannt.
    Die Reihenfolge  Erst suchen, dann schreiben. Nichts schreibt vor
                     der Suche.
    Der Anbieter     Was mitkommt und NICHT gelten darf, muss auch
                     nicht so aussehen.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_move.py
"""
import inspect
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-move-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, SERVICES)
sys.path.insert(0, PLATFORM)

import idp_admin as a                                          # noqa: E402
import move                                                    # noqa: E402

PIN = a.connector_of("keycloak")["pinned"]
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
MOVE_SRC = read(SERVICES, "move.py")
APPCTL_SRC = read(PLATFORM, "appctl.py")

# ---------------------------------------------------------------------------
print("Der Vertrag kennt jetzt ein sechstes Verb")

ok("'export' ist gebaut", a.declares("keycloak", "export"))
ok("und 'later' ist damit leer",
   a.connector_of("keycloak")["later"] == (),
   a.connector_of("keycloak")["later"])
ok("'users' bleibt abgeschworen",
   "users" in a.connector_of("keycloak")["never"])
ok("die Zeile widerspricht sich nicht",
   a.incoherent_verbs("keycloak") == (), a.incoherent_verbs("keycloak"))
ok("kein Pflichtverb fehlt", a.missing_verbs("keycloak") == ())
ok("'export' wird nicht mehr als 'noch nicht' abgelehnt",
   a.verb_refusal("keycloak", "export") == "")
ok("'users' wird weiter abgelehnt, und NICHT als Luecke",
   "not a gap" in a.verb_refusal("keycloak", "users"))
ok("der Konnektor sagt, wie der Export erreicht wird",
   a.export_door("keycloak") == "container", a.export_door("keycloak"))
ok("und welche OAAP-App das Produkt traegt",
   a.export_app_id("keycloak") == "keycloak")
ok("ein Konnektor, der 'export' verspricht, sagt auch wie",
   all(a.export_of(k) for k in a.connector_kinds()
       if a.declares(k, "export")))

# ---------------------------------------------------------------------------
print("\nDie Tuer -- das erste Verb, das kein API-Aufruf ist")

PLAN = a.export_plan("keycloak", "hbvp")
ok("der Export hat einen Plan", bool(PLAN))
ok("und derselbe Richter wie das Anlegen laesst ihn durch",
   a.plan_refusal(PLAN) == "", a.plan_refusal(PLAN))
ok("die Fassung kommt zuerst", PLAN[0]["verb"] == "version")
ok("genau ein Schritt ist kein HTTP-Aufruf",
   sum(1 for s in PLAN if s.get("door") == "container") == 1)
ok("und er hat einen Befehl statt einer Adresse",
   all(s.get("run") and not s.get("path")
       for s in PLAN if s.get("door") == "container"))
ok("kein Schritt des Exports schreibt beim Anbieter",
   not any(s.get("writes") for s in PLAN))
ok("der Plan nennt den Wegwerf-Container",
   any("THROWAWAY" in l for l in a.plan_lines(PLAN)), a.plan_lines(PLAN))

# Und die Regel, die es ohne die neue Tuer nicht gaebe: K3.3 bekommt
# keine Ausnahme, nur weil ein Aufruf durch eine andere Tuer kommt.
BAD = [dict(PLAN[0])] + [dict(s, writes=True) for s in PLAN
                         if s.get("door") == "container"]
ok("ein Container-Schritt, der SCHREIBT, wird abgelehnt",
   "K3.3" in a.plan_refusal(BAD), a.plan_refusal(BAD))
NOCMD = [dict(PLAN[0])] + [dict(s, run="") for s in PLAN
                           if s.get("door") == "container"]
ok("ein Container-Schritt ohne Befehl wird abgelehnt",
   "no command" in a.plan_refusal(NOCMD), a.plan_refusal(NOCMD))
REORDER = [s for s in PLAN if s["verb"] != "version"] + [PLAN[0]]
ok("ein Plan, der die Fassung ans Ende stellt, wird abgelehnt",
   "K3.2" in a.plan_refusal(REORDER), a.plan_refusal(REORDER))

# Die Zaehlung steht VOR der Datei und danach noch einmal -- sonst
# wuerde die Datei gegen sich selbst geprueft.
COUNTS = [i for i, s in enumerate(PLAN)
          if s.get("path") == a.export_count_path("keycloak", "hbvp")]
RUN = next(i for i, s in enumerate(PLAN) if s.get("door") == "container")
ok("gezaehlt wird zweimal", len(COUNTS) == 2, COUNTS)
ok("einmal BEVOR es die Datei gibt", COUNTS[0] < RUN)
ok("und einmal danach", COUNTS[-1] > RUN)

# Joergs Gestalt: Keycloaks eigene Woerter duerfen in der Tabelle und
# in den Keycloak-Funktionen stehen, in keinem gemeinsamen Weg.
WORDS = ("kc.sh", "KC_DB", "realm_file", "POSTGRES_PASSWORD")


def code_of(fn):
    src = inspect.getsource(fn)
    return re.sub(r'""".*?"""', "", src, count=1, flags=re.S)


for fn in (a.export_plan, a.export_shell, a.export_env, a.export_people,
           a.export_refusal, a.export_door, a.export_file):
    body = code_of(fn)
    ok(f"'{fn.__name__}' kennt keine Keycloak-Woerter",
       not [w for w in WORDS if w in body],
       [w for w in WORDS if w in body])
ok("die Keycloak-Woerter stehen in den Keycloak-Funktionen",
   all(any(w in code_of(f) for w in WORDS)
       for f in (a._keycloak_export_shell, a._keycloak_export_env)))
ok("appctl kennt sie nicht",
   not [w for w in WORDS if w in APPCTL_SRC],
   [w for w in WORDS if w in APPCTL_SRC])

# ---------------------------------------------------------------------------
print("\nDie Zaehlprobe -- die Datei wird gezaehlt, nie das Werkzeug geglaubt")

ok("gleich viele: in Ordnung",
   move.export_count_refusal(7, 7, "realm", "Keycloak") == "")
ok("null in der Datei, sieben im Realm: abgelehnt",
   bool(move.export_count_refusal(7, 0, "realm", "Keycloak")))
ok("... und der Satz sagt, warum genau das die gefaehrliche Lage ist",
   "looks exactly like a good one"
   in move.export_count_refusal(7, 0, "realm", "Keycloak"))
ok("eine Differenz wird abgelehnt",
   bool(move.export_count_refusal(7, 6, "realm", "Keycloak")))
ok("... und nennt beide Zahlen",
   all(x in move.export_count_refusal(7, 6, "realm", "Keycloak")
       for x in ("6", "7")))
ok("keine Antwort ist nicht null",
   bool(move.export_count_refusal(None, 0, "realm", "Keycloak")))
ok("ein leerer Realm bleibt erlaubt",
   move.export_count_refusal(0, 0, "realm", "Keycloak") == "")
ok("die Zaehlregel steht in move.py, nicht im Konnektor",
   "def export_count_refusal" in MOVE_SRC
   and "def export_count_refusal" not in ADMIN_SRC)
ok("und die Messung, aus der sie kommt, steht dabei",
   "not one person" in MOVE_SRC or "NOT ONE PERSON" in MOVE_SRC)

# ---------------------------------------------------------------------------
print("\nDas Geheimnis -- wohin so eine Datei darf")

ok("ohne Ziel: abgelehnt", bool(move.export_target_refusal("", DATA)))
ok("relativ: abgelehnt",
   "absolute" in move.export_target_refusal("hbvp.json", DATA))
ok("im Datenverzeichnis: abgelehnt",
   "not inside" in move.export_target_refusal(
       os.path.join(DATA, "x.json"), DATA))
ok("... auch tief darin",
   "not inside" in move.export_target_refusal(
       os.path.join(DATA, "tenants", "hbvp", "x.json"), DATA))
GOOD = os.path.join(tempfile.mkdtemp(prefix="oaap-move-out-"), "hbvp.json")
ok("daneben: erlaubt", move.export_target_refusal(GOOD, DATA) == "",
   move.export_target_refusal(GOOD, DATA))
with open(GOOD, "w", encoding="utf-8") as f:
    f.write("{}")
ok("ueber eine bestehende Datei: abgelehnt",
   "already exists" in move.export_target_refusal(GOOD, DATA))
# Absolut in der Schreibweise DIESES Systems -- der Test prueft die
# Regel, nicht die Wegtrennzeichen von Windows.
NIRGENDS = os.path.join(os.path.abspath(os.sep), "nicht", "da", "hbvp.json")
ok("in ein Verzeichnis, das es nicht gibt: abgelehnt",
   "no directory" in move.export_target_refusal(NIRGENDS, DATA),
   move.export_target_refusal(NIRGENDS, DATA))
WORDS_OUT = move.export_words("/root/hbvp.json", "hbvp", "realm", 12)
ok("der Betreiber wird jedes Mal gewarnt",
   any("SECRET" in l for l in WORDS_OUT))
ok("... und aufgefordert, sie wieder zu entfernen",
   any("REMOVE IT" in l for l in WORDS_OUT))
ok("... und die Zahl steht dabei",
   any("12 person" in l for l in WORDS_OUT))

# ---------------------------------------------------------------------------
print("\nDas Archiv -- und was 0.1 nicht trug")

ok("0.2 wird angenommen",
   move.format_refusal({"backup_format": move.ARCHIVE_FORMAT}) == "")
ok("0.1 wird abgelehnt",
   bool(move.format_refusal({"backup_format": "tenant-0.1"})))
ok("... und der Satz sagt, WAS gefehlt haette",
   "first-login policy"
   in move.format_refusal({"backup_format": "tenant-0.1"}))
ok("ein Knotenarchiv bekommt eine Antwort darueber, was es IST",
   "install.sh restore"
   in move.format_refusal({"backup_format": "2.1", "scope": "node"}))
ok("etwas, das gar kein Archiv ist", bool(move.format_refusal({})))
ok("ein Archiv ohne Formatangabe", bool(move.format_refusal({"scope": "x"})))
ok("der Datensatz steht in der Teileliste",
   "tenant-record.json" in move.ARCHIVE_PARTS)
ok("und das Archiv schreibt ihn auch",
   'json.dump({"tenant": tid, "record": record}' in APPCTL_SRC)
ok("das Archiv nennt sich selbst 0.2",
   "move.ARCHIVE_FORMAT" in APPCTL_SRC
   and '"backup_format": "tenant-0.1"' not in APPCTL_SRC)

ok("ein Archiv aus einem NEUEREN Bau wird abgelehnt",
   bool(move.version_refusal("0.1.130", "0.1.123")))
ok("... und sagt, was zu tun ist",
   "oaap update" in move.version_refusal("0.1.130", "0.1.123"))
ok("ein aelteres ist in Ordnung",
   move.version_refusal("0.1.120", "0.1.123") == "")
ok("dasselbe ist in Ordnung",
   move.version_refusal("0.1.123", "0.1.123") == "")
ok("unbekannt ist kein Grund zu verweigern",
   move.version_refusal("", "0.1.123") == "")

# ---------------------------------------------------------------------------
print("\n'Leer' wird gesucht, nicht behauptet")

MAN = {"backup_format": move.ARCHIVE_FORMAT, "tenant": "tid-hbvp",
       "tenant_label": "hbvp", "platform_version": "0.1.123",
       "hostname": "oaap-alt", "created": "2026-09-23T06:00:00Z"}
REC = {"label": "hbvp", "name": "Handball Verein Probe"}
INST = {"hbvp-web": {"app_id": "demo", "port": 8101, "routes": [{}],
                     "svc_port": 8080}}
USERS = [{"username": "anna", "tenant": "tid-hbvp"}]

ok("auf einem leeren Knoten stoesst nichts zusammen",
   move.collisions(MAN, REC, INST, USERS, {}, {}, [], set()) == [])

C = move.collisions(MAN, REC, INST, USERS,
                    {"tid-hbvp": {"label": "irgendwas"}}, {}, [], set())
ok("derselbe Mandant ist schon da", any(c["what"] == "tenant" for c in C))
C = move.collisions(MAN, REC, INST, USERS,
                    {"other": {"label": "hbvp"}}, {}, [], set())
ok("der Name ist vergeben", any(c["what"] == "label" for c in C))
C = move.collisions(MAN, REC, INST, USERS,
                    {"other": {"label": "x", "former_labels": ["hbvp"]}},
                    {}, [], set())
ok("auch ein FRUEHERER Name zaehlt", any(c["what"] == "label" for c in C))
ok("... und der Satz sagt, warum",
   any("renamed away" in c["why"] for c in C if c["what"] == "label"))
C = move.collisions(MAN, REC, INST, USERS, {}, {"hbvp-web": {}}, [], set())
ok("eine Instanz dieses Namens ist schon da",
   any(c["what"] == "instance" for c in C))
C = move.collisions(MAN, REC, INST, USERS, {}, {}, [], {8101})
ok("der Port ist vergeben", any(c["what"] == "port" for c in C))
C = move.collisions(MAN, REC, INST, USERS, {}, {},
                    [{"username": "Anna"}], set())
ok("jemand dieses Namens meldet sich hier schon an",
   any(c["what"] == "user" for c in C))
ok("... und OAAP entscheidet NICHT, ob das dieselbe Person ist",
   any("same person" in c["why"] for c in C if c["what"] == "user"))
ok("jeder Zusammenstoss nennt das Ding beim Namen",
   all(c.get("name") and c.get("why") for c in C))
ok("die Ablehnung zaehlt sie und nennt die Arten",
   "1 collision" in move.collision_refusal(C), move.collision_refusal(C))
ok("... und sie sagt, dass Verschmelzen genau das Nichtversprochene ist",
   "RFC-0029 D5" in move.collision_refusal(C))
ok("ohne Zusammenstoss keine Ablehnung", move.collision_refusal([]) == "")

# ---------------------------------------------------------------------------
print("\nDie Reihenfolge -- erst suchen, dann schreiben")

P = move.adopt_plan(MAN, REC, INST, USERS)
ok("die Uebernahme hat einen Plan", bool(P))
ok("und er wird durchgelassen", move.plan_refusal(P) == "", move.plan_refusal(P))
ok("der erste Schritt sucht", P[0]["step"] == "check")
ok("und er schreibt nichts", not P[0]["writes"])
ok("alles andere schreibt", all(s["writes"] for s in P[1:]))
ok("ein Plan, der zuerst schreibt, wird abgelehnt",
   "K6" in move.plan_refusal(P[1:] + [P[0]]))
ok("ein Plan, in dem das Suchen schreibt, wird abgelehnt",
   bool(move.plan_refusal([dict(P[0], writes=True)] + P[1:])))
ok("ein Schritt ohne Beschreibung wird abgelehnt",
   bool(move.plan_refusal([P[0], dict(P[1], what="")])))
ok("leerer Plan: abgelehnt", bool(move.plan_refusal([])))
ok("die Zeilen sagen, was schreibt und was liest",
   any(l.strip().startswith("reads") for l in move.plan_lines(P))
   and any(l.strip().startswith("writes") for l in move.plan_lines(P)))

# ---------------------------------------------------------------------------
print("\nDer Anbieter, der mitkommt und nicht gelten darf")

WITH = dict(REC, idp={"kind": "oidc", "issuer": "https://alt.example/realms/hbvp",
                      "client_id": "oaap-alt"})
carried, says = move.provider_parked(WITH)
ok("er kommt mit", carried.get("issuer") == "https://alt.example/realms/hbvp")
ok("und der Satz nennt die Adresse des ALTEN Knotens",
   "alt.example" in says)
ok("... sagt, dass er NICHT gilt", "NOT in force" in says)
ok("... sagt, warum das Geheimnis nicht im Archiv liegt", "K3" in says)
ok("... und sagt, dass niemand sich neu anmelden muss",
   "register again" in says)
ok("ohne Anbieter gibt es nichts zu parken",
   move.provider_parked(REC) == ({}, ""))
LINES = move.parked_lines(carried)
ok("das Parken ist sichtbar",
   any("NOT IN FORCE" in l for l in LINES), LINES)
ok("... und sagt, dass hier niemand durch diese Tuer kommt",
   any("Nobody can sign in through it here" in l for l in LINES))
ok("ohne Anbieter keine Zeilen", move.parked_lines({}) == [])
ok("der Plan nennt das Parken als eigenen Schritt",
   any(s["step"] == "provider"
       for s in move.adopt_plan(MAN, WITH, INST, USERS, carried=True)))
ok("und ohne Anbieter steht es nicht im Plan",
   not any(s["step"] == "provider" for s in P))

# ---------------------------------------------------------------------------
print("\nWo die Regel steht")

ok("die Uebernahme setzt den mitgebrachten Anbieter nicht in Kraft",
   'record.pop("idp", None)' in APPCTL_SRC
   and 'record["idp_carried"] = dict(carried)' in APPCTL_SRC)
ok("der Anmeldedienst liest 'idp_carried' nicht",
   "idp_carried" not in read(SERVICES, "identity", "app.py"))
ok("... und idp.py auch nicht",
   "idp_carried" not in read(SERVICES, "idp.py"))
ok("ein echter Anbieter loest den mitgebrachten ab",
   't.pop("idp_carried", None)' in APPCTL_SRC)
ok("... und das steht im Protokoll des Mandanten",
   "tenant.idp-move-done" in APPCTL_SRC)
ok("die Uebernahme wird protokolliert", "tenant.adopted" in APPCTL_SRC)
ok("der Export wird protokolliert", "tenant.idp-export" in APPCTL_SRC)

ok("der Export laeuft in einem Container, der wieder verschwindet",
   '"docker", "run", "--rm"' in APPCTL_SRC)
ok("... und NICHT im bedienenden Container",
   "docker\", \"exec" not in APPCTL_SRC.split("def _run_export")[1][:2500])
ok("die Datei entsteht mit 0600",
   "os.umask(0o077)" in APPCTL_SRC
   and "os.chmod(tmp, 0o600)" in APPCTL_SRC)
ok("eine Datei, die die Zaehlprobe nicht besteht, wird entfernt",
   "os.remove(out)" in APPCTL_SRC)
ok("appctl schreibt den Benutzerspeicher an genau EINER Stelle",
   APPCTL_SRC.count("def _rewrite_identity_users") == 1
   and APPCTL_SRC.count('"docker", "stop", IDENTITY_CONTAINER') == 1)
ok("... und nur bei stehendem Anmeldedienst",
   '"docker", "stop", IDENTITY_CONTAINER'
   in APPCTL_SRC.split("def _rewrite_identity_users")[1][:2000])
ok("... und startet ihn wieder, auch wenn etwas schiefgeht",
   "finally:" in APPCTL_SRC.split("def _rewrite_identity_users")[1][:3000])
ok("beide Schreiber gehen durch dieselbe Tuer",
   "_rewrite_identity_users(" in APPCTL_SRC.split("def _adopt_users")[1][:400]
   and "_rewrite_identity_users("
   in APPCTL_SRC.split("def tenant_repoint_bindings")[1][:2500])
ok("die Uebernahme sagt, dass der alte Knoten alles noch hat",
   "this club exists twice" in APPCTL_SRC)

# ---------------------------------------------------------------------------
print("\nDie zweite Haelfte des Schluessels (gemessen 23.09., Schritt 7)")

# RFC-0041 §5.0 hat gemessen, dass ein Realm-Export den `sub` erhaelt,
# und daraus geschlossen, dass der Umzug kein Neu-Binden braucht. K4
# bindet aber an ein PAAR, und die andere Haelfte ist der Aussteller --
# also die Adresse des Knotens, und genau die wechselt beim Umzug.
ALT = "oidc|http://alt.example/realms/hbvp"
NEU = "oidc|http://neu.example/realms/hbvp"
CARRIED = {"kind": "oidc", "issuer": "http://alt.example/realms/hbvp"}

ok("nach einem Umzug duerfen die Bindungen umgehaengt werden",
   move.rebind_refusal(CARRIED, ALT, NEU) == "",
   move.rebind_refusal(CARRIED, ALT, NEU))
ok("OHNE Umzug nicht -- und das ist die gefaehrliche Haelfte",
   bool(move.rebind_refusal({}, ALT, NEU)))
ok("... und der Satz sagt, warum: niemand hat versprochen, dass es "
   "dieselben Menschen sind",
   "same people" in move.rebind_refusal({}, ALT, NEU))
ok("derselbe Aussteller: nichts zu tun",
   bool(move.rebind_refusal(CARRIED, ALT, ALT)))
ok("eine fehlende Haelfte: abgelehnt",
   bool(move.rebind_refusal(CARRIED, "", NEU))
   and bool(move.rebind_refusal(CARRIED, ALT, "")))
W = move.rebind_words(ALT, NEU, 12)
ok("der Betreiber erfaehrt, was umgehaengt wurde",
   any("12 binding" in l for l in W))
ok("... dass der SUBJECT unangetastet bleibt",
   any("SUBJECT of each binding is untouched" in l for l in W))
ok("... und was ohne das passiert waere",
   any("stranger" in l for l in W))
ok("die Regel nennt die Messung, die sie widerlegt hat",
   "5.0" in MOVE_SRC.split("def rebind_refusal")[1][:2500])

ok("umgehaengt wird nur beim Abloesen eines MITGEBRACHTEN Anbieters",
   "move.rebind_refusal" in APPCTL_SRC
   and APPCTL_SRC.count("tenant_repoint_bindings(") == 2)
ok("... und nur fuer die Menschen DIESES Mandanten",
   'resolve_tenant(u.get("tenant")) != tid'
   in APPCTL_SRC.split("def tenant_repoint_bindings")[1][:2500])
ok("... und nur fuer Bindungen mit GENAU dem alten Schluessel",
   'b.get("provider") != old_key'
   in APPCTL_SRC.split("def tenant_repoint_bindings")[1][:2500])
ok("... der subject wird nicht angefasst",
   '"subject"' not in APPCTL_SRC.split("def tenant_repoint_bindings")[1][:2500])
ok("... und es steht im Protokoll des Mandanten",
   "tenant.idp-repointed" in APPCTL_SRC)
ok("die widerlegte Zusage steht nicht mehr als Zusage im Code",
   "so the issuer keeps its" not in read(SERVICES, "idp.py"))
ok("... sondern als das, was gemessen wurde",
   "That was wrong" in read(SERVICES, "idp.py"))

# ---------------------------------------------------------------------------
print("\nAn einer erfundenen Maschine: die Zahl kommt vom Anbieter")

STATE = {"realms": {"hbvp": {"realm": "hbvp"}, "fremd": {"realm": "fremd"}},
         "count": 12, "count_forbidden": {"fremd"}, "methods": []}


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

    def do_GET(self):
        STATE["methods"].append("GET " + self.path)
        path = self.path.split("?")[0]
        if self.headers.get("Authorization") != "Bearer fake-admin-token":
            return self._json({"error": "no"}, 401)
        if path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": PIN}})
        m = re.match(r"^/admin/realms/([^/]+)/users/count$", path)
        if m:
            # Die gemessene Gestalt: der Realm selbst antwortet 200,
            # abgelehnt wird erst am ZWEITEN Dokument.
            if m.group(1) in STATE["count_forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            if m.group(1) not in STATE["realms"]:
                return self._json({"error": "not found"}, 404)
            return self._json(STATE["count"])
        m = re.match(r"^/admin/realms/([^/]+)$", path)
        if m:
            if m.group(1) in STATE["realms"]:
                return self._json(STATE["realms"][m.group(1)])
            return self._json({"error": "not found"}, 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        # Den Rumpf IMMER lesen, auch wenn er nicht gebraucht wird.
        # Ohne das bleibt er in der Leitung stehen, und der naechste
        # Aufruf bekommt gelegentlich ein 401 statt einer Antwort --
        # ein flatternder Test, der wie ein Befund aussieht.
        try:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
        except (ValueError, OSError):
            pass
        if self.path.endswith("/protocol/openid-connect/token"):
            return self._json({"access_token": "fake-admin-token"})
        return self._json({"error": "no"}, 404)


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_port}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def admin():
    ad = a.Admin("keycloak", BASE, "client", "oaap-admin",
                 "geheim-geheim-geheim-1234")
    got, why = ad.login()
    # Laut scheitern statt still weiterzulaufen: eine misslungene
    # Anmeldung sieht sonst wie ein 401 an einer ganz anderen Tuer aus.
    ok("die Anmeldung am erfundenen Server gelingt", got, why)
    return ad


n, err = admin().count_people("hbvp")
ok("der Anbieter sagt, wie viele Menschen im Realm sind", n == 12 and not err,
   (n, err))
# Genau die Reihenfolge, die `oaap idp export` laeuft -- und genau die
# Lehre aus Schritt 6: ein Test, der die Tuer nicht trifft, an der die
# Regel wirklich zuschlaegt, prueft die Regel nicht.
ad = admin()
doc, err = ad.find_space("fremd")
ok("der fremde Realm antwortet auf die erste Frage mit 200",
   doc is not None and not err, (doc, err))
n, err = ad.count_people("fremd")
ok("in einem FREMDEN Realm wird gezaehlt und abgelehnt", n is None and err)
ok("... mit dem sorgfaeltigen Satz, nicht mit der nackten Zahl",
   "K3.3" in (err or ""), err)
ok("... und der Realm selbst hat vorher 200 geantwortet",
   "GET /admin/realms/fremd" in STATE["methods"])
n, err = admin().count_people("gibtsnicht")
ok("ein Realm, den es nicht gibt, gibt KEINE Null zurueck",
   n is None and err, (n, err))

STATE["count"] = "viele"
n, err = admin().count_people("hbvp")
ok("etwas, das keine Zahl ist, ist auch keine Null", n is None and err)
ok("... und der Satz sagt es", "other than a number" in (err or ""))

srv.shutdown()

print("")
print(f"{'FAILED' if fails else 'OK'}: {fails} Fehler")
sys.exit(1 if fails else 0)
