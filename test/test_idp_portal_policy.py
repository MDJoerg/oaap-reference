#!/usr/bin/env python3
"""Erster Login aus dem Portal (oaap.core.tenant 2.8, 1.27; Jörg 2026-11-23).

Was eine erste Anmeldung ueber den Anmeldedienst eines Mandanten bekommt,
stellt der Betreiber ein -- bisher nur mit `oaap tenant policy`. Jetzt
auch im Portal, als zweite und schmalere Tuer:

    Wer          nur server_admin; der Knoten urteilt selbst (Spool ist
                 Daten, kein Vertrauen)
    Was          'eingang' oder 'role' mit einer Rolle der Erlaubt-Liste;
                 nichts, was eine Abbildung oder eine Begruendung braucht
    Vorher       vor dem Vergeben einer Rolle wird der Realm SOFORT gelesen;
                 Lesefehler, unbekannte Broker oder irgendeine offene Tuer
                 lehnen ab
    Schliessen   'eingang' geht immer
    Pruefen      'check' sagt, was passieren wuerde, und schreibt nichts
    Protokoll    Erfolg und Ablehnung stehen im Log des Mandanten

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_idp_portal_policy.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-portalpolicy-test-")
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


OPEN_GOOGLE = a.settings_of("keycloak", {"brokers": [kc("google", "google")]})[
    "brokers"][0]

# ---------------------------------------------------------------------------
print("Die Regel der Portaltuer (idp.portal_policy_refusal)")

READ_CLEAN = {"realm": {"read": "2026-11-23T05:10", "self_registration": False,
                        "brokers": []}}
ok("'eingang' geht immer, auch ohne jede Lesung",
   idp.portal_policy_refusal("eingang", "", {}) == "")
ok("'groups' bleibt der Kommandozeile",
   "command line" in idp.portal_policy_refusal("groups", "user", READ_CLEAN))
ok("eine Rolle ausserhalb der Erlaubt-Liste nicht",
   "cannot be granted" in idp.portal_policy_refusal("role", "tenant_admin",
                                                    READ_CLEAN))
ok("ohne jede Lesung des Realms: keine Rolle",
   "never been read" in idp.portal_policy_refusal("role", "user", {}))
ok("Lesung ohne Brokerliste: keine Rolle",
   "brokers" in idp.portal_policy_refusal(
       "role", "user", {"realm": {"read": "x", "self_registration": False}}))
ok("Selbstregistrierung im Realm offen: keine Rolle",
   "register" in idp.portal_policy_refusal(
       "role", "user", {"realm": {"read": "x", "self_registration": True,
                                  "brokers": []}}))
ok("Selbstregistrierung nur in der Notiz an: auch keine Rolle",
   idp.portal_policy_refusal("role", "user", dict(READ_CLEAN,
                                                  self_registration=True)) != "")
ok("offener Broker: keine Rolle",
   "google" in idp.portal_policy_refusal(
       "role", "user", {"realm": {"read": "x", "self_registration": False,
                                  "brokers": [OPEN_GOOGLE]}}))
ok("Realm gelesen, keine Tuer offen: 'user' geht",
   idp.portal_policy_refusal("role", "user", READ_CLEAN) == "")

# ---------------------------------------------------------------------------
print("\nDer Knoten (appctl.tenant_policy_from_portal)")

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()
tid, _t = m.tenant_create("kunde", name="Kunde Probe")
noidp, _t2 = m.tenant_create("leer", name="Ohne Anmeldedienst")
t = m.load_tenants()
t[tid]["idp"] = {"issuer": "http://kc.invalid/realms/kunde",
                 "connector": "kc", "space": "kunde"}
m.save_tenants(t)
m.load_idp_connectors = lambda: {"kc": {
    "kind": "keycloak", "base_url": "http://kc.invalid", "auth": "client",
    "admin_id": "id", "admin_secret": "s"}}

REALM = {"self_registration": False, "brokers": []}
CALLS = []
LOGIN_OK = [True]


class FakeAdmin:
    def __init__(self, *a_, **kw):
        pass

    def login(self):
        CALLS.append("login")
        return (True, "ok") if LOGIN_OK[0] else (False, "401")

    def read_settings(self, space):
        CALLS.append(("read", space))
        return dict(REALM), ""


m.idp_admin.Admin = FakeAdmin


def pol():
    return idp.policy_of(m.load_tenants()[tid])


def log():
    try:
        with open(m.TENANT_LOG, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except OSError:
        return []


good, msg = m.tenant_policy_from_portal(tid, "role", "user", role="tenant_admin",
                                        act_tenant=tid, who="kunde.admin")
ok("ein tenant_admin darf es nicht, auch nicht im eigenen Mandanten",
   not good and "server_admin" in msg and pol()["first_login"] == "eingang", msg)
ok("... und der Realm wurde dafuer nicht einmal gefragt", not CALLS, CALLS)

good, msg = m.tenant_policy_from_portal(noidp, "role", "user",
                                        role="server_admin", who="op")
ok("ohne eigenen Anmeldedienst gibt es nichts einzustellen",
   not good and "no provider" in msg, msg)

good, msg = m.tenant_policy_from_portal(tid, "role", "user", check=True,
                                        role="server_admin", who="op")
ok("Pruefen liest den Realm sofort", ("read", "kunde") in CALLS, CALLS)
ok("... sagt, was passieren wuerde", good and "would mean 'role'" in msg, msg)
ok("... und aendert nichts", pol()["first_login"] == "eingang", pol())

REALM["self_registration"] = True
CALLS.clear()
good, msg = m.tenant_policy_from_portal(tid, "role", "user",
                                        role="server_admin", who="op")
ok("Selbstregistrierung frisch gelesen offen: abgelehnt",
   not good and "register" in msg and CALLS, (msg, CALLS))
ok("... die Richtlinie bleibt 'eingang'", pol()["first_login"] == "eingang")

REALM["self_registration"] = False
REALM["brokers"] = [kc("google", "google")]
good, msg = m.tenant_policy_from_portal(tid, "role", "user",
                                        role="server_admin", who="op")
ok("offener Broker frisch gelesen: abgelehnt",
   not good and "google" in msg, msg)

REALM["brokers"] = []
LOGIN_OK[0] = False
good, msg = m.tenant_policy_from_portal(tid, "role", "user",
                                        role="server_admin", who="op")
ok("Realm nicht lesbar: abgelehnt, keine Rolle auf die Lesung von gestern",
   not good and "could not be read" in msg and pol()["first_login"] == "eingang",
   msg)

LOGIN_OK[0] = True
good, msg = m.tenant_policy_from_portal(tid, "role", "user",
                                        role="server_admin", who="op")
ok("Realm gelesen, keine Tuer offen: umgestellt", good
   and pol()["first_login"] == "role" and pol()["default_role"] == "user",
   (msg, pol()))
ok("... im Log des Mandanten, mit dem Betreiber als Urheber", any(
    e.get("action") == "tenant.idp-policy" and e.get("tenant") == tid
    and e.get("who") == "op" and e.get("role") == "server_admin"
    for e in log()), log()[-3:])

good, msg = m.tenant_policy_from_portal(tid, "role", "user", check=True,
                                        role="server_admin", who="op")
ok("noch einmal dasselbe: nichts zu tun", good and "nothing to change" in msg,
   msg)

REALM["self_registration"] = True
good, msg = m.tenant_policy_from_portal(tid, "eingang", "user",
                                        role="server_admin", who="op")
ok("zurueck auf 'eingang' geht auch mit offener Tuer", good
   and pol()["first_login"] == "eingang" and pol()["default_role"] == "",
   (msg, pol()))

# ---------------------------------------------------------------------------
print("\nSpool-Zweig und Portal")

SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("der Arbeiter kennt 'tenant-policy'", 'elif action == "tenant-policy":' in SRC)
branch = SRC.split('elif action == "tenant-policy":', 1)[1][:2500]
ok("... ruft tenant_policy_from_portal mit der Rolle aus dem Benutzersatz",
   "tenant_policy_from_portal(" in branch and "role=act_role" in branch)
ok("... und schreibt die Ablehnung ins Log", '"denied"' in branch)
ok("die taegliche Lesung nutzt dieselbe Lesefunktion",
   "tenant_read_realm_now(tid, t, held)" in SRC)
PORTAL = open(os.path.join(SERVICES, "portal", "app.py"),
              encoding="utf-8").read()
ok("das Portal hat die Seite /tenant/first-login (GET und POST)",
   '@app.get("/tenant/first-login")' in PORTAL
   and '@app.post("/tenant/first-login")' in PORTAL)
ok("... nur fuer server_admin (dasselbe Ziel wie die Domain-Seite)",
   PORTAL.split('def tenant_policy_post', 1)[1][:600].count(
       "_tenant_domain_target()") == 1)
ok("... prueft auf fremde Herkunft",
   "_same_origin()" in PORTAL.split('def tenant_policy_post', 1)[1][:600])
ok("... Pruefen, dann Bestaetigen", '"check": check' in PORTAL
   and "Jetzt umstellen" in PORTAL)
ok("die Mandantentabelle verlinkt 'Erster Login'",
   'href="/tenant/first-login?tenant={{ t.label }}">Erster Login' in PORTAL)

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
