#!/usr/bin/env python3
"""Der Absprung zur Benutzerverwaltung des eigenen Realms (RFC-0056, I-28).

Ein Mandantenverwalter bekommt auf seiner Seite einen Knopf zur Konsole des
Realms, in dem seine Leute leben. Die Adresse kommt aus dem Anbieter-Objekt
des Mandanten -- nie aus einer Eingabe. Und sie gibt es nur dort, wo ein
Konnektor den Realm gemacht hat: nur dort gibt es die Gruppe, fuer die der
Knopf gedacht ist.

Geprueft wird (echte Portalseite, ohne Docker):
    - ein vom Konnektor gemachter Realm: der Knopf steht auf der Seite
      des Mandantenverwalters UND in der Tabelle des Betreibers;
    - ein von Hand eingetragener Anbieter: kein Knopf;
    - kein Anbieter: kein Knopf;
    - ein Anbieter mit Pfad-Praefix (`/auth/realms/x`): die Adresse behaelt es;
    - Koeder: eine Aussteller-Adresse, die keine Realm-Adresse ist (auch
      `javascript:`), ergibt keinen Knopf;
    - ein Mandantenverwalter sieht den Knopf nur seines eigenen Mandanten;
    - der Hinweis nennt die Gruppe und dass OAAP keine Person anlegt.

Aufruf: python3 test/test_tenant_console_link.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-console-link-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, os.path.join(SERVICES, "portal"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import idp                                                      # noqa: E402
import appctl as m                                              # noqa: E402

m.reload_gateway = lambda: None
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


print("=== die Funktion ===")
made = {"connector": "kc", "issuer": "https://auth.example.org/realms/cls"}
ok("Konnektor-Realm -> Konsole",
   idp.realm_console_url(made) == "https://auth.example.org/admin/cls/console/")
ok("Pfad-Praefix bleibt",
   idp.realm_console_url({"connector": "kc",
                          "issuer": "https://h.example/auth/realms/x"})
   == "https://h.example/auth/admin/x/console/")
ok("Port bleibt",
   idp.realm_console_url({"connector": "kc",
                          "issuer": "http://10.0.0.1:8113/realms/x/"})
   == "http://10.0.0.1:8113/admin/x/console/")
ok("von Hand eingetragen (kein Konnektor) -> nichts",
   idp.realm_console_url({"issuer": made["issuer"]}) == "")
for bad in ("", "javascript:alert(1)//realms/x", "https://a.example/other/x",
            "https://a.example/realms/", "https://a.example/realms/x/y",
            "ftp://a.example/realms/x", "https://a.example/realms/x?y=1"):
    ok(f"Koeder '{bad}' -> nichts",
       idp.realm_console_url({"connector": "kc", "issuer": bad}) == "", bad)
ok("kein Anbieter -> nichts", idp.realm_console_url({}) == ""
   and idp.realm_console_url(None) == "")

print("\n=== die Seite (echtes Portal) ===")
try:
    os.environ.setdefault("SESSION_SECRET", "x")
    spec = importlib.util.spec_from_file_location(
        "portal_app", os.path.join(SERVICES, "portal", "app.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
except Exception as exc:                                        # noqa: BLE001
    P = None
    print(f"SKIP  das Portal laesst sich hier nicht laden "
          f"({type(exc).__name__}: {exc})")

if P is not None:
    m.ensure_default_tenant()
    A, _ = m.tenant_create("aaa")
    B, _ = m.tenant_create("bbb")
    C, _ = m.tenant_create("ccc")
    D, _ = m.tenant_create("ddd")
    tenants = m.load_tenants()
    tenants[A]["idp"] = {"kind": "oidc", "connector": "kc",
                         "issuer": "https://auth.example.org/realms/aaa",
                         "client_id": "x", "space": "aaa"}
    tenants[B]["idp"] = {"kind": "oidc", "connector": "kc",
                         "issuer": "https://auth.example.org/realms/bbb",
                         "client_id": "x", "space": "bbb"}
    tenants[C]["idp"] = {"kind": "oidc",
                         "issuer": "https://hand.example.org/realms/ccc",
                         "client_id": "x"}
    m.save_tenants(tenants)
    P.TENANTS_FILE = m.TENANTS_FILE
    P.external_host = lambda: "knoten.example.org"
    P.require_user_admin = lambda: None
    state = {"role": "tenant_admin", "mine": A}
    P.caller_scope = lambda: (state["role"], state["mine"])
    P.multi_tenant = lambda: True

    def show(**who):
        state.update(who)
        with P.app.test_request_context("/tenant"):
            r = P.tenant_page()
            if isinstance(r, tuple):
                r = r[0]
            return r if isinstance(r, str) else r.get_data(as_text=True)

    html = show(role="tenant_admin", mine=A)
    ok("Mandantenverwalter A: Knopf zur Konsole von aaa",
       'href="https://auth.example.org/admin/aaa/console/"' in html)
    ok("... und NICHT der von bbb",
       "/admin/bbb/console/" not in html)
    ok("... der Hinweis nennt die Gruppe und dass OAAP keine Person anlegt",
       "oaap-verwalter" in html and "keine Person" in html)
    html = show(role="tenant_admin", mine=C)
    ok("von Hand eingetragener Anbieter: kein Knopf",
       "/console/" not in html and "Benutzer im" not in html, html[:200])
    html = show(role="tenant_admin", mine=D)
    ok("kein Anbieter: kein Knopf", "/console/" not in html)
    html = show(role="server_admin", mine="")
    ok("Betreiber: in der Tabelle steht der Link fuer aaa und bbb",
       'href="https://auth.example.org/admin/aaa/console/"' in html
       and 'href="https://auth.example.org/admin/bbb/console/"' in html)
    ok("... aber keiner fuer den von Hand eingetragenen ccc",
       "hand.example.org/admin" not in html)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
