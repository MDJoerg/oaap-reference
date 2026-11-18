#!/usr/bin/env python3
"""Broker sind Tueren (RFC-0091 Stufe 1, D4/D5).

K7 lehnt 'role'/'groups' zusammen mit Selbstregistrierung ab, wenn
niemand begruendet. Gelesen wurde dafuer bis 0.1.296 nur der
Registrierungs-Schalter. Ein Google-Broker ohne Domaene vor dem Realm
laesst aber jedes Google-Konto herein -- und rutschte durch.

    Lesen        Der Konnektor liest die Broker des Realms (nur lesen,
                 D5) und uebersetzt Keycloaks Felder in OAAPs Worte.
    Unbekannt    Eine Liste, die nicht gelesen werden konnte, ist
                 UNBEKANNT, nicht leer -- und der Schalter-Teil der
                 Lesung geht trotzdem durch.
    Urteil       offen/geschlossen in EINER Funktion, fuer CLI, Portal
                 und Ablehnung.
    Ablehnung    Ein offener Broker zaehlt wie Selbstregistrierung (D4).
    Quittung     Der Betreiber kann einen Broker 'geschlossen' nennen --
                 nur mit Begruendung.
    Nachher      Ein Broker, der NACH der Richtlinie aufging, steht als
                 Unterschied da, im Mandantenlog und als Meldung.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_idp_brokers.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-brokers-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, SERVICES)
sys.path.insert(0, PLATFORM)

import idp                                                     # noqa: E402
import idp_admin as a                                          # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def kc(alias, provider, **kw):
    cfg = kw.pop("config", {})
    return dict({"alias": alias, "providerId": provider, "enabled": True,
                 "trustEmail": False,
                 "firstBrokerLoginFlowAlias": "first broker login",
                 "config": cfg}, **kw)


# ---------------------------------------------------------------------------
print("Keycloaks Felder in OAAPs Worte")

READ = a.settings_of("keycloak", {"brokers": [
    kc("google", "google"),
    kc("google-firma", "google", config={"hostedDomain": "firma.example"}),
    kc("google-stern", "google", config={"hostedDomain": "*"}),
    kc("entra-alle", "oidc", config={
        "issuer": "https://login.microsoftonline.com/common/v2.0"}),
    kc("entra-partner", "oidc", config={
        "authorizationUrl": "https://login.microsoftonline.com/"
                            "0b1c2d3e-aaaa-bbbb-cccc-0123456789ab/oauth2/v2.0/"
                            "authorize"}),
    kc("ms", "microsoft", config={"tenant": "organizations"}),
    kc("ms-eins", "microsoft", config={"tenant": "contoso.example"}),
    kc("gh", "github"),
    kc("partner-kc", "keycloak-oidc", config={
        "issuer": "https://sso.partner.example/realms/p"}),
    kc("saml-x", "saml"),
    kc("aus", "github", enabled=False),
    {"providerId": "google"},                     # ohne alias: kein Broker
]})
B = {b["alias"]: b for b in READ["brokers"]}
ok("ein Eintrag ohne Alias wird nicht mitgezaehlt", len(B) == 11, list(B))
ok("Google ohne Domaene: jeder", B["google"]["population"] == "any")
ok("Google mit Domaene: nur die", B["google-firma"]["population"]
   == "domain:firma.example")
ok("Google mit '*': jeder Workspace-Kunde ist kein Zaun",
   B["google-stern"]["population"] == "any")
ok("Entra ueber oidc mit 'common': jeder",
   B["entra-alle"]["population"] == "any")
ok("Entra ueber oidc mit einer Verzeichnis-ID: nur das Verzeichnis",
   B["entra-partner"]["population"].startswith("directory:0b1c2d3e"),
   B["entra-partner"])
ok("Microsoft-Broker mit 'organizations': jeder",
   B["ms"]["population"] == "any")
ok("Microsoft-Broker mit einem Verzeichnis: nur das",
   B["ms-eins"]["population"] == "directory:contoso.example")
ok("GitHub: jeder", B["gh"]["population"] == "any")
ok("ein fremdes Keycloak: unbekannt", B["partner-kc"]["population"]
   == "unknown")
ok("SAML: unbekannt", B["saml-x"]["population"] == "unknown")
ok("ein abgeschalteter Broker wird als abgeschaltet gelesen",
   B["aus"]["enabled"] is False)
ok("ohne Broker-Dokument keine Broker-Antwort (unbekannt, nicht leer)",
   "brokers" not in a.settings_of("keycloak", {"space": {
       "registrationAllowed": False}}))
ok("ein leeres Dokument heisst: gelesen, keiner da",
   a.settings_of("keycloak", {"brokers": []}).get("brokers") == [])

# ---------------------------------------------------------------------------
print("\nDas Urteil, an EINER Stelle")

for alias, want in (("google", True), ("google-firma", False),
                    ("google-stern", True), ("entra-alle", True),
                    ("entra-partner", False), ("ms", True),
                    ("ms-eins", False), ("gh", True), ("partner-kc", True),
                    ("saml-x", True), ("aus", False)):
    is_open, why = idp.broker_door(B[alias])
    ok(f"{alias}: {'offen' if want else 'geschlossen'}", is_open is want, why)
    ok(f"{alias}: der Satz nennt den Broker", f"'{alias}'" in why, why)
ok("eine Quittung des Betreibers schliesst einen unbekannten Broker",
   idp.broker_door(B["partner-kc"],
                   {"partner-kc": "nur Partner X, eigenes Verzeichnis"})[0]
   is False)
ok("eine Quittung ohne Begruendung ist keine",
   idp.broker_closed_of({"partner-kc": "ja"}) == {})

ok("trustEmail wird gewarnt", any(
    "trusts the e-mail" in w for w in idp.broker_warnings(dict(
        idp.broker_of(B["gh"]), trust_email=True))))
ok("ein eigener Erstanmelde-Ablauf wird gewarnt", any(
    "first-login flow" in w for w in idp.broker_warnings(
        a._keycloak_broker(kc("x", "github",
                              firstBrokerLoginFlowAlias="auto-link")))))
RF = a.settings_of("keycloak", {
    "space": {"registrationAllowed": False,
              "firstBrokerLoginFlow": "auto-link-by-mail"},
    "brokers": [{"alias": "p", "providerId": "keycloak-oidc", "config": {}}]})
ok("ohne eigenen Ablauf am Broker gilt der des Realms (gemessen 26.7.4)",
   RF["brokers"][0]["first_login_flow"] == "auto-link-by-mail"
   and not RF["brokers"][0]["flow_default"], RF)
ok("... und ein Realm mit Standard-Ablauf warnt nicht",
   a.settings_of("keycloak", {
       "space": {"firstBrokerLoginFlow": "first broker login"},
       "brokers": [{"alias": "p", "providerId": "github"}]}
   )["brokers"][0]["flow_default"])
ok("der Standard-Ablauf wird nicht gewarnt",
   not idp.broker_warnings(B["gh"]))
ok("ein abgeschalteter Broker warnt nicht",
   not idp.broker_warnings(dict(B["aus"], trust_email=True)))

# ---------------------------------------------------------------------------
print("\nDie Ablehnung (D4): ein offener Broker ist eine Tuer")

REALM = {"read": "2026-11-18T10:00:00Z", "space": "kunde",
         "self_registration": False,
         "brokers": [B["google"], B["entra-partner"]]}
bad = idp.policy_refusal("role", "user", False, realm=REALM)
ok("'role' mit offenem Google-Broker und ohne Grund wird abgelehnt", bad)
ok("... und der Satz nennt die Tuer", "'google'" in bad
   and "any google account" in bad, bad)
ok("... und nicht den geschlossenen Broker", "entra-partner" not in bad, bad)
ok("mit Begruendung geht es",
   not idp.policy_refusal("role", "user", False,
                          reason="Kunden melden sich mit Google an",
                          realm=REALM))
ok("'eingang' gibt nichts und braucht keinen Grund",
   not idp.policy_refusal("eingang", "", False, realm=REALM))
ok("die Quittung des Betreibers schliesst die Tuer fuer die Ablehnung",
   not idp.policy_refusal("role", "user", False, realm=dict(
       REALM, brokers=[B["partner-kc"]]),
       broker_closed={"partner-kc": "nur Partner X, ein Verzeichnis"}))
ok("nur geschlossene Broker: keine Ablehnung",
   not idp.policy_refusal("role", "user", False, realm=dict(
       REALM, brokers=[B["entra-partner"], B["google-firma"]])))
old = idp.policy_refusal("role", "user", True)
ok("die alte Selbstregistrierungs-Ablehnung bleibt wortgleich",
   old.startswith("self-registration together with 'role'"), old)
ok("unbekannte Broker-Liste: keine Erfindung einer Tuer",
   not idp.policy_refusal("role", "user", False, realm={
       "read": "2026-11-18T10:00:00Z", "self_registration": False}))

doors = idp.open_doors(idp.policy_of({"idp_policy": {
    "first_login": "role", "default_role": "user",
    "self_registration": True, "realm": REALM}}))
ok("open_doors listet Seite UND Broker",
   [d for d, _w in doors] == ["registration", "broker:google"], doors)

# ---------------------------------------------------------------------------
print("\nDer Konnektor liest -- und eine 403 macht UNBEKANNT, nicht leer")

admin = a.Admin("keycloak", "http://kc.invalid", "client", "id", "secret")
ANSWERS = {}


def fake_call(method, path, body=None, **kw):
    for key, ans in ANSWERS.items():
        if path.endswith(key):
            return ans
    return 200, {}, ""


admin._call = fake_call
ANSWERS.clear()
ANSWERS.update({
    "/realms/kunde": (200, {"realm": "kunde", "registrationAllowed": False},
                      ""),
    "/CONFIGURE_TOTP": (200, {"enabled": True, "defaultAction": False}, ""),
    "/identity-provider/instances": (200, [kc("google", "google")], ""),
})
said, err = admin.read_settings("kunde")
ok("die Lesung bringt die Broker mit", not err and said.get("brokers")
   and said["brokers"][0]["alias"] == "google", (said, err))
ANSWERS["/identity-provider/instances"] = (403, {}, "")
admin.trace = []
said, err = admin.read_settings("kunde")
ok("Broker 403: die Schalter kommen trotzdem", not err
   and said.get("self_registration") is False, (said, err))
ok("... und Broker fehlen (unbekannt), statt leer zu sein",
   "brokers" not in said, said)
ok("... und die Spur sagt es", any("unknown, not none" in ln
                                   for ln in admin.trace), admin.trace)
ok("der Pfad steht in der Tabelle und wird nur gelesen",
   a.path_of("keycloak", "identity_providers", space="kunde")
   == "/admin/realms/kunde/identity-provider/instances")
ok("die Verwaltergruppe bekommt KEIN manage-identity-providers (D5)",
   "manage-identity-providers"
   not in a.connector_of("keycloak")["admin_group"]["roles"])

# ---------------------------------------------------------------------------
print("\nAufschreiben, quittieren, melden")

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()
tid, _t = m.tenant_create("kunde", name="Kunde Probe")
good, msg = m.tenant_set_policy(tid, first_login="role", default_role="user")
ok("die Richtlinie 'role' steht (noch keine Tuer bekannt)", good, msg)


def pol():
    return idp.policy_of(m.load_tenants()[tid])


def outbox():
    try:
        names = sorted(os.listdir(m.NOTIFY_OUTBOX_DIR))
    except OSError:
        return []
    out = []
    for n in names:
        with open(os.path.join(m.NOTIFY_OUTBOX_DIR, n), encoding="utf-8") as f:
            out.append(json.load(f))
    return out


good, msg = m.tenant_record_realm_settings(
    tid, "kc", "kunde", {"self_registration": False, "brokers": []})
ok("eine Lesung ohne Broker wird aufgeschrieben", good, msg)
ok("... als 'gelesen, keiner'", pol()["realm"].get("brokers") == [])
ok("... und meldet nichts", not [x for x in outbox()
                                 if x["type"] == "idp.door"])

good, msg = m.tenant_record_realm_settings(
    tid, "kc", "kunde", {"self_registration": False,
                         "brokers": [B["google"], B["entra-partner"]]})
ok("die naechste Lesung mit einem offenen Google-Broker", good, msg)
doors = [x for x in outbox() if x["type"] == "idp.door"]
ok("... meldet genau EINE neue offene Tuer", len(doors) == 1
   and doors[0]["kurz"] == "google", doors)
with open(m.TENANT_LOG, encoding="utf-8") as f:
    LOG = [json.loads(ln) for ln in f if ln.strip()]
ok("... im Mandantenlog steht sie", any(
    e.get("action") == "tenant.idp-door" and e.get("tenant") == tid
    and e.get("subject") == "google" for e in LOG), LOG[-3:])
drift = idp.drift_lines(pol())
ok("... und als Unterschied: offener Broker, 'role', kein Grund",
   any("'google'" in ln and "'user'" in ln for ln in drift), drift)
ok("die Richtlinie selbst hat die Lesung NICHT veraendert",
   pol()["first_login"] == "role")

m.tenant_record_realm_settings(
    tid, "kc", "kunde", {"self_registration": False,
                         "brokers": [B["google"], B["entra-partner"]]})
ok("dieselbe Lesung noch einmal meldet nichts Neues",
   len([x for x in outbox() if x["type"] == "idp.door"]) == 1)

good, msg = m.tenant_set_policy(tid, default_role="partner")
ok("eine neue Richtlinie ohne Grund wird jetzt abgelehnt", not good
   and "'google'" in msg, msg)
good, msg = m.tenant_set_policy(
    tid, broker_closed={"google": "ja"})
ok("'geschlossen' ohne Begruendung wird abgelehnt", not good
   and "reason" in msg, msg)
good, msg = m.tenant_set_policy(
    tid, broker_closed={"google": "nur zum Test, Domaene folgt morgen"})
ok("mit Begruendung wird quittiert", good, msg)
ok("... die Quittung steht in der Richtlinie",
   "google" in pol()["broker_closed"], pol())
ok("... und der Unterschied ist weg", not any(
    "'google'" in ln for ln in idp.drift_lines(pol())), idp.drift_lines(pol()))
good, msg = m.tenant_set_policy(tid, broker_closed={"google": None})
ok("zuruecknehmen ohne Grund wird abgelehnt: dann waere die Tuer offen",
   not good and "'google'" in msg and "google" in pol()["broker_closed"],
   (good, msg))
good, msg = m.tenant_set_policy(tid, broker_closed={"google": None},
                                reason="Kunden melden sich mit Google an")
ok("mit Grund laesst sich die Quittung zuruecknehmen", good
   and "google" not in pol()["broker_closed"], (good, msg, pol()))
ok("... und die Lesung ist dabei unberuehrt geblieben",
   len(pol()["realm"].get("brokers") or []) == 2, pol()["realm"])

m.tenant_record_realm_settings(tid, "kc", "kunde",
                               {"self_registration": False})
ok("eine Lesung, die die Broker nicht lesen konnte, macht sie unbekannt",
   "brokers" not in pol()["realm"], pol()["realm"])
ok("... und brokers_judged sagt None, nicht []",
   idp.brokers_judged(pol()) is None)

# ---------------------------------------------------------------------------
print("\nDie taegliche Lesung")

t = m.load_tenants()
t[tid]["idp"] = {"issuer": "http://kc.invalid/realms/kunde",
                 "connector": "kc", "space": "kunde"}
m.save_tenants(t)
m.load_idp_connectors = lambda: {"kc": {
    "kind": "keycloak", "base_url": "http://kc.invalid", "auth": "client",
    "admin_id": "id", "admin_secret": "s"}}
CALLS = []


class FakeAdmin:
    def __init__(self, *a, **kw):
        pass

    def login(self):
        CALLS.append("login")
        return True, "ok"

    def read_settings(self, space):
        CALLS.append(("read", space))
        return {"self_registration": False,
                "brokers": [B["google"], B["gh"]]}, ""


m.idp_admin.Admin = FakeAdmin
import time                                                    # noqa: E402

early = time.mktime(time.strptime("2026-11-18 04:00", "%Y-%m-%d %H:%M"))
late = time.mktime(time.strptime("2026-11-18 06:00", "%Y-%m-%d %H:%M"))
ok("vor 05:10 wird nicht gelesen", m.idp_watch_minute(now=early) == []
   and not CALLS)
done = m.idp_watch_minute(now=late)
ok("danach wird jeder Mandant mit Konnektor gelesen",
   ("read", "kunde") in CALLS and done, (CALLS, done))
ok("... aufgeschrieben wie eine Lesung von Hand",
   len(pol()["realm"].get("brokers") or []) == 2, pol()["realm"])
n_door = len([x for x in outbox() if x["type"] == "idp.door"])
ok("... und neu offene Tueren gemeldet (nach einer Lesung ohne Brokerliste: beide)",
   n_door == 3, [x["kurz"] for x in outbox() if x["type"] == "idp.door"])
CALLS.clear()
ok("am selben Tag nicht noch einmal", m.idp_watch_minute(now=late + 600)
   == [] and not CALLS)
with open(m.TENANT_LOG, encoding="utf-8") as f:
    n_before = sum(1 for ln in f if '"tenant.idp-realm"' in ln)
m.idp_watch_minute(now=late + 86400)
with open(m.TENANT_LOG, encoding="utf-8") as f:
    n_after = sum(1 for ln in f if '"tenant.idp-realm"' in ln)
ok("eine unveraenderte Lesung am naechsten Tag schreibt keine Logzeile",
   n_after == n_before, (n_before, n_after))
ok("... aber die Lesung traegt den neuen Zeitpunkt",
   pol()["realm"]["read"] and CALLS, pol()["realm"])

# ---------------------------------------------------------------------------
print("\nMeldung, Portal, CLI")

import notify                                                  # noqa: E402

ok("'idp.door' steht im Katalog, Kategorie Sicherheit",
   notify.CATALOGUE.get("idp.door", {}).get("category") == "security")
PORTAL = open(os.path.join(SERVICES, "portal", "app.py"),
              encoding="utf-8").read()
ok("das Portal reicht die beurteilten Broker durch",
   '"brokers": idp.brokers_judged(policy)' in PORTAL)
ok("... und sagt 'nicht gelesen' statt nichts",
   "wurde nicht gelesen" in PORTAL)
SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("die CLI kennt --broker-closed und --broker-open",
   '"--broker-closed"' in SRC and '"--broker-open"' in SRC)
ok("die Einstellungs-Lesung reicht die Quittungen an die Ablehnung",
   'realm=pol["realm"], broker_closed=pol["broker_closed"]' in SRC)

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
