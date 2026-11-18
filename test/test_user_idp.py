#!/usr/bin/env python3
"""0.1.307 (RFC-0095 D2, identity 2.12, runtime 2.37): X-OAAP-User-Idp.

The person's binding at the tenant's identity provider as "<issuer> <sub>",
so the MCP gateway's /me can match calls signed in at the realm to the
person. A header an app believes must be one the platform writes AND strips
everywhere else -- so it lives on the one list, and every instance is told
which headers that list holds (OAAP_IDENTITY_HEADERS).

Run: python3 test/test_user_idp.py
"""
import importlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.join(HERE, "..", "platform")
IDENTITY_DIR = os.path.join(PLATFORM_DIR, "services", "identity")
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


sys.path.insert(0, PLATFORM_DIR)
import appctl  # noqa: E402

ok("die Kopfzeile steht auf der einen Liste", "X-OAAP-User-Idp" in appctl.IDENTITY_HEADERS)
ok("… also wird sie auf jeder nicht angemeldeten Route entfernt",
   "\t\trequest_header -X-OAAP-User-Idp" in appctl.strip_identity())
ok("… und bei der Anmeldung kopiert", "X-OAAP-User-Idp" in appctl._COPY_IDENTITY.split())
ok("eine Gateway-Datei mit der alten Liste gilt als veraltet und wird beim Update neu geschrieben",
   appctl._site_identity_stale("\n".join(
       f"request_header -{h}" for h in appctl.IDENTITY_HEADERS if h != "X-OAAP-User-Idp")))
src = open(os.path.join(PLATFORM_DIR, "appctl.py"), encoding="utf-8").read()
ok("jede Instanz erfährt die Liste (bei der Installation und in der Migration)",
   src.count('env["OAAP_IDENTITY_HEADERS"]') >= 2)

# 0.1.308 (runtime 2.38, L5): die Seite der Instanz im Portal.
ok("jede Instanz erfährt ihre Portalseite (Installation und Migration)",
   src.count('env["OAAP_PORTAL_URL"]') >= 2)
_tenants = {"t-bec": {"label": "bec"}, "t-def": {"label": appctl.DEFAULT_TENANT_LABEL}}
_old = appctl.load_tenants, appctl.former_labels
appctl.load_tenants = lambda: _tenants
appctl.former_labels = lambda t: []
try:
    ok("Mandant: https://<label>.<knoten>/instances/<name>",
       appctl.portal_instance_url("bec-tools", "t-bec", ext_host="oaap.example") ==
       "https://bec.oaap.example/instances/bec-tools")
    ok("Standardmandant: https://<knoten>/instances/<name>",
       appctl.portal_instance_url("wiki", "t-def", ext_host="oaap.example") ==
       "https://oaap.example/instances/wiki")
    ok("ohne externen Namen: leer", appctl.portal_instance_url("wiki", "t-def", ext_host="") == "")
    ok("unbekannter Mandant: leer (fail closed)",
       appctl.portal_instance_url("x", "t-gibtsnicht", ext_host="oaap.example") == "")
finally:
    appctl.load_tenants, appctl.former_labels = _old

d = tempfile.mkdtemp(prefix="oaap-idp-")
os.environ.update({"SESSION_SECRET": "s", "SETUP_TOKEN": "t", "INTERNAL_API_KEY": "k",
                   "OAAP_IDENTITY_DATA_DIR": d})
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
bound = {"username": "bec.jmueller", "roles": ["user"], "id": "317c922b", "kind": "human",
         "idp": {"provider": "oidc|https://auth.example/realms/bec", "subject": "5f09d2c6-ef39"}}
h = m.identity_headers(bound)
ok("mit Bindung: „<Issuer> <sub>“", h.get("X-OAAP-User-Idp") == "https://auth.example/realms/bec 5f09d2c6-ef39", h)
ok("ohne Bindung: vorhanden, aber leer (die Überschreib-Regel)",
   m.identity_headers({"username": "x", "roles": ["user"], "id": "1"}).get("X-OAAP-User-Idp", None) == "")
ok("ein anderer Anbieter (nicht OIDC): leer",
   m.user_idp({"idp": {"provider": "saml|x", "subject": "y"}}) == "")
ok("ein Leerzeichen im sub ergäbe zwei Lesarten: leer",
   m.user_idp({"idp": {"provider": "oidc|https://a", "subject": "a b"}}) == "")
ok("nie ein Token in der Kopfzeile", "token" not in str(m.user_idp(bound)).lower())

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
