#!/usr/bin/env python3
"""Die Schalter IM Realm, und die Wahrheit darueber (RFC-0041 K7, Schritt 6).

Schritt 4 hat die Tuer gebaut. Schritt 6 legt die beiden Schalter um,
die dahinter stehen: wer sich selbst anmelden darf, und ob ein zweiter
Faktor verlangt wird. Beide gehoeren dem Anmeldedienst, nicht OAAP --
und genau daraus entsteht die Schwierigkeit, um die es hier geht.

Jeder dieser Schalter steht an ZWEI Orten: im Realm, wo er wirkt, und
im Satz des Mandanten, wo ein Mandantenverwalter ihn liest. Gehen die
beiden auseinander, liest jemand eine Zahl, die nichts deckt -- und
hoert auf zu suchen. Deshalb:

    Der Vertrag      'settings' ist jetzt gebaut und steht im Vertrag.
                     'export' ist genannt und nicht gebaut (Schritt 7),
                     'users' ist abgeschworen und bleibt es.
    Die Reihenfolge  Derselbe Plan-Richter wie beim Anlegen: erst die
                     Fassung, dann irgendetwas. Und das Nachlesen steht
                     IM Plan, nicht dahinter.
    Die Wahrheit     Was in unsere Konfiguration kommt, hat der Realm
                     gesagt -- nicht wir. Ein Realm, der eine Aenderung
                     annimmt und beim Nachlesen etwas anderes sagt,
                     muss den Betreiber anschreien und den Satz des
                     Mandanten auf die WIRKLICHKEIT setzen.
    Der Unterschied  Dreht jemand am Anmeldedienst von Hand, sagt OAAP
                     es, statt seine eigene Notiz zu glauben.
    Das Paar         'role' plus Selbstregistrierung wird abgelehnt --
                     auch dann, wenn nur der REALM offen ist und unsere
                     Notiz das Gegenteil behauptet.
    Die Reichweite   Ein Schalter, der weniger erreicht als sein Name
                     verspricht, sagt das einmal laut.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_idp_settings.py
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
DATA = tempfile.mkdtemp(prefix="oaap-idpset-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, SERVICES)
sys.path.insert(0, PLATFORM)

import idp                                                     # noqa: E402
import idp_admin as a                                          # noqa: E402

HOST = "oaap.joomp.de"
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
APPCTL_SRC = read(PLATFORM, "appctl.py")

# ---------------------------------------------------------------------------
print("Der Vertrag kennt jetzt ein fuenftes Verb")

ok("'settings' ist gebaut", a.declares("keycloak", "settings"))
ok("und der Konnektor kann es auch wirklich",
   "keycloak" in a.settings_kinds(), a.settings_kinds())
ok("jede Art, die 'settings' verspricht, kann es auch",
   all(k in a.settings_kinds() for k in a.connector_kinds()
       if a.declares(k, "settings")))
ok("beide Schalter haben eine Adresse",
   all(a.switch_path("keycloak", "hbvp", sw) for sw in a.SWITCHES))
ok("und sie liegen in ZWEI verschiedenen Dokumenten",
   len({a.switch_where("keycloak", sw) for sw in a.SWITCHES}) == 2,
   [a.switch_where("keycloak", sw) for sw in a.SWITCHES])
ok("ein erfundener Schalter wird abgelehnt",
   a.switch_refusal("keycloak", "haarfarbe", True))
ok("ein erfundener Wert wird abgelehnt",
   a.switch_refusal("keycloak", "second_factor", "vielleicht"))
ok("... und der Satz nennt die erlaubten",
   "required" in a.switch_refusal("keycloak", "second_factor", "vielleicht"))
ok("beide guten Werte gehen",
   not a.switch_refusal("keycloak", "second_factor", "required")
   and not a.switch_refusal("keycloak", "self_registration", True))
ok("OAAP spricht von Schaltern in EIGENEN Worten, nicht in Keycloaks",
   a.SWITCHES == ("self_registration", "second_factor"), a.SWITCHES)
# Und die Probe auf Joergs Gestalt: Keycloaks eigene Feldnamen duerfen
# in der Tabelle und in den Keycloak-Funktionen stehen -- aber in
# keinem Stueck Weg, das ein zweites Produkt genauso durchlaufen
# wuerde. Sonst ist ein zweiter Konnektor kein Zeilenpaar mehr.
import inspect                                                 # noqa: E402

WORDS = ("registrationAllowed", "CONFIGURE_TOTP", "defaultAction", "realms/")


def code_of(fn):
    """Der Quelltext ohne seinen Docstring.

    Die Prosa DARF Keycloak erklaeren -- sie erklaert ja, warum die
    Tabelle so aussieht. Der ausgefuehrte Teil darf es nicht.
    """
    src = inspect.getsource(fn)
    return re.sub(r'""".*?"""', "", src, count=1, flags=re.S)


for fn in (a.settings_of, a.settings_call, a.settings_plan, a.switch_refusal,
           a.settings_words, a.settings_disagreement, a.Admin.settings,
           a.Admin.read_settings, a.Admin.write_switch, a.switch_path,
           a.switch_where):
    hit = [w for w in WORDS if w in code_of(fn)]
    ok(f"'{fn.__name__}' spricht kein Keycloak", not hit, hit)
ok("... und die Keycloak-Funktionen tun es sehr wohl",
   all(w in code_of(a._keycloak_settings_read)
       + code_of(a._keycloak_settings_write)
       for w in ("registrationAllowed", "defaultAction")))
ok("'CONFIGURE_TOTP' steht in genau EINER ausgefuehrten Zeile",
   len([ln for ln in ADMIN_SRC.splitlines()
        if "CONFIGURE_TOTP" in ln and not ln.strip().startswith("#")
        and "alias" in ln]) == 1)

# ---------------------------------------------------------------------------
print("\nDie Reihenfolge: erst fragen, dann schalten -- und dann NACHLESEN")

PLAN = a.settings_plan("keycloak", "hbvp",
                       {"self_registration": True,
                        "second_factor": "required"})
ok("der Plan wird vom selben Richter angenommen wie beim Anlegen",
   not a.plan_refusal(PLAN), a.plan_refusal(PLAN))
ok("der erste Schritt fragt nach der Fassung", PLAN[0]["verb"] == "version")
ok("... und begruendet das mit dem LESEN, nicht mit dem Schreiben",
   "read wrongly" in PLAN[0]["why"], PLAN[0]["why"])
ok("kein Schritt loescht", all(not a.method_refusal(s["method"])
                               for s in PLAN))
ok("gelesen wird vor dem Schreiben",
   min(i for i, s in enumerate(PLAN) if not s["writes"])
   < min(i for i, s in enumerate(PLAN) if s["writes"]))
ok("und das LETZTE Wort hat der Anbieter",
   not PLAN[-1]["writes"] and "read it back" in PLAN[-1]["why"],
   PLAN[-1])
ok("ein Plan ohne Wuensche liest nur", not any(
    s["writes"] for s in a.settings_plan("keycloak", "hbvp", {})))
ok("ein erfundener Schalter kommt gar nicht in den Plan",
   not any("haarfarbe" in s["why"] for s in a.settings_plan(
       "keycloak", "hbvp", {"haarfarbe": "rot"})))

# ---------------------------------------------------------------------------
print("\nZwei Orte, ein Schalter")

REALM_DOC = {"realm": "hbvp", "registrationAllowed": True}
TOTP_DOC = {"alias": "CONFIGURE_TOTP", "enabled": True,
            "defaultAction": True, "priority": 10}
SAID = a.settings_of("keycloak", {"space": REALM_DOC,
                                  "required_action": TOTP_DOC})
ok("Keycloaks Felder werden in OAAPs Worte gelesen",
   SAID == {"self_registration": True, "second_factor": "required"}, SAID)
ok("'enabled' allein ist noch kein verlangter Faktor",
   a.settings_of("keycloak", {"required_action": dict(
       TOTP_DOC, defaultAction=False)})["second_factor"] == "off")
ok("ein Dokument, das gar nicht kam, ergibt keinen Schalter",
   "self_registration" not in a.settings_of("keycloak",
                                            {"required_action": TOTP_DOC}))

where, method, body = a.settings_call("keycloak", "self_registration", True,
                                      REALM_DOC, "hbvp")
ok("der Realm wird mit PUT geschaltet", method == "PUT" and where == "space")
ok("... und es wird NUR unser Feld geschickt",
   set(body) == {"realm", "registrationAllowed"}, body)
ok("... nicht das ganze zurueckgelesene Dokument",
   "displayName" not in body and "sslRequired" not in body, body)
where, method, body = a.settings_call("keycloak", "second_factor", "off",
                                      TOTP_DOC, "hbvp")
ok("'aus' loescht die Vorgabe", body["defaultAction"] is False, body)
ok("... laesst die Aktion aber eingeschaltet -- wer schon einen Faktor "
   "hat, behaelt ihn", body["enabled"] is True, body)

# Der Satz, um den es geht: was der Realm ANTWORTET, nicht was wir wollten.
bad = a.settings_disagreement("keycloak", {"self_registration": True},
                              {"self_registration": False})
ok("ein Realm, der etwas anderes sagt, wird gemeldet", bool(bad))
ok("... und der Satz sagt, dass die ANTWORT aufgeschrieben wird",
   "records" in bad and "not the instruction" in bad, bad)
ok("ein Realm, der gar nicht antwortet, wird auch gemeldet",
   bool(a.settings_disagreement("keycloak", {"second_factor": "off"}, {})))
ok("stimmen beide ueberein, gibt es nichts zu melden",
   not a.settings_disagreement("keycloak", SAID, SAID))

# ---------------------------------------------------------------------------
print("\nDer Unterschied zwischen unserer Notiz und dem Ort")

POL = idp.policy_of({"idp_policy": {
    "first_login": "eingang", "self_registration": False,
    "second_factor": "off",
    "realm": {"self_registration": True, "second_factor": "required",
              "read": "2026-09-23T23:00:00Z", "space": "hbvp",
              "connector": "auth"}}})
lines = idp.drift_lines(POL)
ok("beide Unterschiede werden benannt", len(lines) == 2, lines)
ok("... und jeder nennt BEIDE Seiten",
   all("this record says" in x and "said" in x for x in lines), lines)
ok("... und sagt, welche entscheidet",
   any("space is what decides" in x for x in lines), lines)
ok("eine Notiz ohne Lesung erzeugt keinen Unterschied",
   idp.drift_lines(idp.policy_of({"idp_policy":
                                  {"self_registration": True}})) == [])
ok("eine Lesung ohne Zeitpunkt ist keine Lesung",
   idp.realm_reading({"self_registration": True}) == {},
   idp.realm_reading({"self_registration": True}))
ok("nach dem Schreiben sind beide gleich und es gibt nichts zu melden",
   idp.drift_lines(idp.policy_of({"idp_policy": {
       "self_registration": True, "second_factor": "required",
       "realm": {"self_registration": True, "second_factor": "required",
                 "read": "2026-09-23T23:00:00Z"}}})) == [])

# Und die Regel, die daraus folgt: offen ist offen, egal wer es sagt.
ok("die Notiz allein macht offen",
   idp.self_registration_open({"self_registration": True}))
ok("der Ort allein macht AUCH offen",
   idp.self_registration_open({"realm": {"self_registration": True}}))
ok("beide zu, heisst zu",
   not idp.self_registration_open({"self_registration": False,
                                   "realm": {"self_registration": False}}))

# ---------------------------------------------------------------------------
print("\nDas gefaehrliche Paar -- auch wenn nur der Ort es aufmacht (K7)")

bad = idp.policy_refusal("role", "user", True)
ok("Rolle plus Selbstregistrierung wird abgelehnt", bool(bad))
ok("mit einer Begruendung geht es",
   not idp.policy_refusal("role", "user", True,
                          reason="Der Verein laesst nur Mitglieder herein"))
OPEN_REALM = {"self_registration": True, "read": "2026-09-23T23:00:00Z",
              "space": "hbvp"}
bad = idp.policy_refusal("role", "user", False, realm=OPEN_REALM)
ok("und es wird AUCH abgelehnt, wenn nur der Ort offen ist", bool(bad), bad)
ok("... und der Satz sagt, dass es der Ort ist",
   "SPACE that has it switched on" in bad, bad)
ok("... und was man tun kann", "Close it there" in bad, bad)
ok("ein geschlossener Ort laesst 'role' durch",
   not idp.policy_refusal("role", "user", False,
                          realm={"self_registration": False,
                                 "read": "2026-09-23T23:00:00Z"}))
ok("'eingang' plus offener Ort ist in Ordnung -- genau dafuer ist er da",
   not idp.policy_refusal("eingang", "", False, realm=OPEN_REALM))
ok("der zweite Faktor hat eigene Werte",
   bool(idp.policy_refusal("eingang", "", False, second_factor="an")))

# ---------------------------------------------------------------------------
print("\nDie Reichweite wird genannt, bevor jemand sie annimmt")

reach = a.settings_reach("keycloak", "second_factor", "required")
ok("der zweite Faktor sagt, wen er NICHT erreicht",
   "not asked retroactively" in reach, reach)
ok("... und warum nicht", "K3.3" in reach, reach)
ok("... und wo es doch ginge", "own console" in reach, reach)
reach = a.settings_reach("keycloak", "self_registration", True)
ok("die Selbstregistrierung sagt, was sie hergibt",
   "identity" in reach and "K4" in reach, reach)
ok("'aus' hat nichts zu erklaeren",
   a.settings_reach("keycloak", "self_registration", False) == "")

# ---------------------------------------------------------------------------
print("\nEin erfundenes Keycloak, und beide Schalter wirklich umgelegt")

STATE = {"realms": {}, "clients": {}, "actions": {}, "writes": 0,
         "methods": [], "stubborn": False, "forbidden": set(),
         "realm_puts": [], "actions_forbidden": set()}


def _totp(space):
    return STATE["actions"].setdefault(
        space, {"alias": "CONFIGURE_TOTP", "name": "Configure OTP",
                "providerId": "CONFIGURE_TOTP", "enabled": True,
                "defaultAction": False, "priority": 10})


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

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        STATE["methods"].append("GET " + self.path)
        path = self.path.split("?")[0]
        if not self._auth():
            return self._json({"error": "forbidden"}, 401)
        if path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": PIN}})
        m = re.match(r"^/admin/realms/([^/]+)$", path)
        if m:
            if m.group(1) in STATE["forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            if m.group(1) in STATE["realms"]:
                return self._json(STATE["realms"][m.group(1)])
            return self._json({"error": "not found"}, 404)
        m = re.match(r"^/admin/realms/([^/]+)/authentication/"
                     r"required-actions/CONFIGURE_TOTP$", path)
        if m:
            if m.group(1) in STATE["forbidden"] \
                    or m.group(1) in STATE["actions_forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            return self._json(_totp(m.group(1)))
        m = re.match(r"^/admin/realms/([^/]+)/clients$", path)
        if m:
            hit = re.search(r"clientId=([^&]+)", self.path)
            want = hit.group(1) if hit else ""
            return self._json([c for c in STATE["clients"].values()
                               if c["realm"] == m.group(1)
                               and c["clientId"] == want])
        m = re.match(r"^/admin/realms/([^/]+)/clients/([^/]+)/client-secret$",
                     path)
        if m:
            return self._json({"type": "secret",
                               "value": "geheim-vom-anbieter-" + m.group(2)})
        return self._json({"error": "not found"}, 404)

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
        m = re.match(r"^/admin/realms/([^/]+)$", self.path)
        if m and m.group(1) in STATE["realms"]:
            if m.group(1) in STATE["forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            STATE["realm_puts"].append(dict(body))
            # Keycloak wendet die genannten Felder an und laesst den
            # Rest stehen -- ein Teil-PUT ist hier kein Trick.
            if not STATE["stubborn"]:
                STATE["realms"][m.group(1)].update(body)
            return self._json({}, 204)
        m = re.match(r"^/admin/realms/([^/]+)/authentication/"
                     r"required-actions/CONFIGURE_TOTP$", self.path)
        if m:
            if m.group(1) in STATE["forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            if not STATE["stubborn"]:
                _totp(m.group(1)).update(body)
            return self._json({}, 204)
        m = re.match(r"^/admin/realms/([^/]+)/clients/([^/]+)$", self.path)
        if m and m.group(2) in STATE["clients"]:
            STATE["clients"][m.group(2)].update(body)
            return self._json({}, 204)
        return self._json({"error": "not found"}, 404)


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
ok("der Konnektor steht", good, msg)


def run(**kw):
    args = types.SimpleNamespace(
        action="settings", name="kc", tenant="hbvp", space=None,
        client_id=None, idp_label=None, accept_version=None, dry_run=False,
        self_registration=None, second_factor=None, idp_reason=None)
    for k, v in kw.items():
        setattr(args, k, v)
    try:
        m.cmd_idp(args)
        return True
    except SystemExit as e:
        return not e.code


def policy():
    return idp.policy_of(m.load_tenants()[hbvp])


# Schalter gibt es nur IN einem Ort. Gibt es ihn nicht, wird nichts
# angelegt -- Anlegen ist ein eigener Akt und bleibt einer.
STATE["writes"] = 0
ok("ohne Realm wird abgelehnt", not run(self_registration="on"))
ok("... und nichts angelegt", STATE["writes"] == 0, STATE["methods"][-3:])
ok("... und der Satz nennt den Befehl, der das taete",
   "oaap idp provision" in ADMIN_SRC)

m.cmd_idp(types.SimpleNamespace(
    action="provision", name="kc", tenant="hbvp", space=None, client_id=None,
    idp_label="Vereinskonto", accept_version=None, dry_run=False))
ok("der Realm steht", "hbvp" in STATE["realms"])
ok("und er ist zu geboren",
   STATE["realms"]["hbvp"]["registrationAllowed"] is False)
ok("der Mandant weiss noch nichts vom Ort", not policy()["realm"])

# --dry-run ruft nichts.
STATE["methods"] = []
ok("--dry-run geht durch", run(self_registration="on", dry_run=True))
ok("... und ruft gar nichts", STATE["methods"] == [], STATE["methods"])
ok("... und aendert den Realm nicht",
   STATE["realms"]["hbvp"]["registrationAllowed"] is False)

# Der Durchlauf, um den es geht.
STATE["realm_puts"] = []
ok("die Selbstregistrierung wird eingeschaltet", run(self_registration="on"))
ok("... im Realm", STATE["realms"]["hbvp"]["registrationAllowed"] is True)
ok("... mit genau einem Feld und dem Namen",
   [set(b) for b in STATE["realm_puts"]] == [{"realm",
                                              "registrationAllowed"}],
   STATE["realm_puts"])
pol = policy()
ok("... und der Mandantensatz haelt die ANTWORT fest",
   (pol["realm"] or {}).get("self_registration") is True, pol["realm"])
ok("... mit Zeitpunkt, Ort und Konnektor",
   pol["realm"]["read"] and pol["realm"]["space"] == "hbvp"
   and pol["realm"]["connector"] == "kc", pol["realm"])
ok("... und die Absicht steht daneben und stimmt ueberein",
   pol["self_registration"] is True and idp.drift_lines(pol) == [],
   idp.drift_lines(pol))
ok("der erste Login bleibt, was er war -- der Schalter vergibt nichts",
   pol["first_login"] == "eingang", pol)
ok("und es steht im Protokoll des Mandanten",
   any(e.get("action") == "tenant.idp-realm"
       for e in m.read_tenant_log(hbvp, 20)),
   [e.get("action") for e in m.read_tenant_log(hbvp, 20)])

# Zweimal dasselbe schalten schreibt nicht zweimal.
STATE["writes"] = 0
ok("noch einmal dasselbe geht durch", run(self_registration="on"))
ok("... ohne einen einzigen Schreibzugriff", STATE["writes"] == 0,
   STATE["methods"][-4:])

# Der zweite Faktor.
ok("der zweite Faktor wird verlangt", run(second_factor="required"))
ok("... als Vorgabe-Aktion im Realm",
   _totp("hbvp")["defaultAction"] is True and _totp("hbvp")["enabled"] is True,
   _totp("hbvp"))
ok("... und der Satz des Mandanten sagt es",
   policy()["second_factor"] == "required"
   and policy()["realm"]["second_factor"] == "required", policy())
ok("... und eine Anmeldung ohne genannten Faktor faellt jetzt auf",
   idp.factor_expected(policy()))
ok("wieder aus", run(second_factor="off"))
ok("... loescht nur die Vorgabe",
   _totp("hbvp")["defaultAction"] is False
   and _totp("hbvp")["enabled"] is True, _totp("hbvp"))
ok("... und der Satz auch", policy()["second_factor"] == "off")

# Beide auf einmal, und der erste Login bleibt unberuehrt.
ok("beide Schalter in einem Zug",
   run(self_registration="off", second_factor="required"))
ok("... und der Realm stimmt",
   STATE["realms"]["hbvp"]["registrationAllowed"] is False
   and _totp("hbvp")["defaultAction"] is True)
ok("... und es gibt keinen Unterschied zu melden",
   idp.drift_lines(policy()) == [], idp.drift_lines(policy()))

# ---------------------------------------------------------------------------
print("\nEin Realm, der etwas anderes sagt, als er getan hat")

# Damit die Probe etwas beweist, muss sich am Satz auch etwas AENDERN
# koennen: der Realm steht in Wahrheit auf 'an', unser Satz sagt 'aus',
# und der Befehl will 'aus' setzen -- und der Realm nimmt es an, ohne
# es zu tun.
STATE["realms"]["hbvp"]["registrationAllowed"] = True
ok("vorher sagt unser Satz noch 'aus'",
   policy()["realm"]["self_registration"] is False)
STATE["stubborn"] = True
ok("OAAP meldet das als Fehlschlag", not run(self_registration="off"))
pol = policy()
ok("... und schreibt die WIRKLICHKEIT auf, nicht die Anweisung",
   pol["realm"]["self_registration"] is True, pol["realm"])
ok("... eine Anweisung, die nicht ankam, wird NICHT zur Absicht",
   pol["self_registration"] is False, pol)
ok("... also bleibt das Unerledigte als Unterschied stehen",
   any("self-registration" in x for x in idp.drift_lines(pol)),
   idp.drift_lines(pol))
ok("... und der Mandant gilt weiter als offen",
   idp.self_registration_open(pol))
STATE["stubborn"] = False
# Aufgeraeumt: jetzt wirklich zu.
ok("und dann geht es doch", run(self_registration="off"))
ok("... im Realm", STATE["realms"]["hbvp"]["registrationAllowed"] is False)
ok("... und ohne Unterschied", idp.drift_lines(policy()) == [])

# ---------------------------------------------------------------------------
print("\nEin Ort, der uns nicht gehoert -- derselbe Satz wie an jeder Tuer")

STATE["realms"]["fremd"] = {"realm": "fremd", "registrationAllowed": False}
STATE["forbidden"].add("fremd")
STATE["writes"] = 0
CAP = []
_print = print
import builtins                                                # noqa: E402


def _catch(*args, **kw):
    CAP.append(" ".join(str(x) for x in args))


builtins.print = _catch
good = run(space="fremd", self_registration="on")
builtins.print = _print
said = " ".join(" ".join(CAP).split())
ok("ein fremder Ort bricht ab", not good)
ok("... mit dem Satz, der an jeder Tuer steht",
   "somebody else's club" in said and "K3.3" in said, said[-300:])
ok("... und ohne einen Schreibzugriff", STATE["writes"] == 0,
   STATE["methods"][-4:])
ok("... und ohne den Satz des Mandanten anzufassen",
   policy()["realm"]["space"] == "hbvp", policy()["realm"])
STATE["forbidden"].clear()
STATE["realms"].pop("fremd", None)

# Und die Tuer, die an der Maschine WIRKLICH zuschlug (23.09.2026): Der
# Realm selbst antwortet mit 200 -- eine `create-realm`-Vollmacht darf
# sehen, dass er da ist -- und erst das ZWEITE Dokument wird abgelehnt.
# Genau dort stand beim Anlegen einmal nur eine nackte Zahl.
STATE["actions_forbidden"].add("hbvp")
STATE["writes"] = 0
CAP.clear()
builtins.print = _catch
good = run(second_factor="required")
builtins.print = _print
said = " ".join(" ".join(CAP).split())
ok("ein zweites Dokument, das uns nicht gehoert, bricht ab", not good)
ok("... mit DEMSELBEN Satz wie an der ersten Tuer",
   "somebody else's club" in said and "K3.3" in said, said[-300:])
ok("... und ohne einen Schreibzugriff", STATE["writes"] == 0)
STATE["actions_forbidden"].clear()

# ---------------------------------------------------------------------------
print("\nDreht jemand von Hand daran, sagt OAAP es")

STATE["realms"]["hbvp"]["registrationAllowed"] = True
pol = policy()
ok("unsere Notiz sagt noch 'aus'", pol["self_registration"] is False)
ok("... und der Unterschied ist noch nicht sichtbar, weil niemand nachsah",
   idp.drift_lines(pol) == [])
ok("nachsehen kostet keinen Schalter", run())
pol = policy()
ok("... und danach steht die Wahrheit im Satz",
   pol["realm"]["self_registration"] is True, pol["realm"])
# Und das Entscheidende: ein reines Nachlesen ueberschreibt die ABSICHT
# nicht. Taete es das, loeschte es den Unterschied in dem Augenblick,
# in dem es ihn findet -- und niemand erfuehre, dass von Hand gedreht
# wurde.
ok("... und die Absicht bleibt, was jemand gewaehlt hat",
   pol["self_registration"] is False, pol)
ok("... also wird der Unterschied GEMELDET",
   any("self-registration" in x for x in idp.drift_lines(pol)),
   idp.drift_lines(pol))
ok("... und der Ort gilt: der Mandant ist offen",
   idp.self_registration_open(pol))

# Und jetzt das Paar, um das es K7 geht -- der Realm ist offen, unsere
# Notiz sagt nichts davon, und jemand will 'role' setzen.
good, msg = m.tenant_set_policy(hbvp, first_login="role", default_role="user")
ok("'role' wird abgelehnt, weil der ORT offen ist", not good, msg)
ok("... und der Satz sagt genau das", "SPACE that has it switched on" in msg,
   msg)
good, msg = m.tenant_set_policy(
    hbvp, first_login="role", default_role="user",
    reason="Der Verein prueft jede Registrierung im Eingang nach")
ok("mit Begruendung geht es", good, msg)
ok("... und die Begruendung steht im Protokoll",
   any("Begruendung" in (e.get("detail") or "")
       for e in m.read_tenant_log(hbvp, 5)),
   [e.get("detail") for e in m.read_tenant_log(hbvp, 5)])

# Und eine Absicht, die niemand dem Ort gesagt hat, wird als solche
# markiert statt als Tatsache.
STATE["writes"] = 0
good, _msg = m.tenant_set_policy(hbvp, second_factor="off")
ok("eine Absicht laesst sich notieren", good)
ok("... ohne dass der Ort davon erfaehrt", STATE["writes"] == 0)
ok("... und der Unterschied wird gemeldet",
   any("second factor" in x for x in idp.drift_lines(policy())),
   idp.drift_lines(policy()))
ok("... und der Befehl sagt dem Betreiber, dass er nur notiert hat",
   "This wrote down an INTENTION" in APPCTL_SRC)

# ---------------------------------------------------------------------------
print("\nWo die Regel steht")

ok("die Lesung kommt nur aus EINER Funktion in den Satz",
   APPCTL_SRC.count("def tenant_record_realm_settings") == 1
   and APPCTL_SRC.count('pol["realm"] = reading') == 1)
ok("und 'tenant policy' ueberschreibt sie nicht",
   '"realm": now["realm"],' in APPCTL_SRC)
ok("die beiden Schalter bewegt nur der Konnektor",
   "def write_switch" in ADMIN_SRC
   and "def write_switch" not in read(SERVICES, "identity", "app.py"))
ok("der Anmeldedienst erzwingt keinen zweiten Faktor",
   "factor_expected" in read(SERVICES, "identity", "app.py")
   and "abort(" not in read(SERVICES, "identity",
                            "app.py").split("factor_expected")[1][:400])
ok("und sagt dazu, dass er es nicht tut",
   "OAAP does not enforce" in read(SERVICES, "idp.py"))

print("")
print(f"{'FAILED' if fails else 'OK'}: {fails} Fehler")
sys.exit(1 if fails else 0)
