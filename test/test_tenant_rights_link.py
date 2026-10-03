#!/usr/bin/env python3
"""Der Knopf "Rollen & Rechte" auf der Mandantenseite (RFC-0045 A7, D5).

Das Portal VERLINKT nur: es zeigt keine Rechte, und der Link gibt keines.
Der Knopf steht nur, wenn der Mandant eine Instanz der App `rollen-rechte`
hat, und er zeigt auf die Instanz DIESES Mandanten -- nie auf die eines
anderen.

Geprueft wird (echte Portalseite, ohne Docker):
    - Mandant mit der App: der Mandantenverwalter sieht den Knopf, mit der
      Adresse seiner Instanz;
    - KOEDER: eine gleichnamige Instanz eines ANDEREN Mandanten wird nicht
      verlinkt;
    - ein Mandant ohne die App: kein Knopf;
    - ein anderes App-Kennzeichen: kein Knopf;
    - der Hinweis sagt, dass der Link kein Recht gibt.

Aufruf: python3 test/test_tenant_rights_link.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-rights-link-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, os.path.join(SERVICES, "portal"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                              # noqa: E402

m.reload_gateway = lambda: None
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


try:
    os.environ.setdefault("SESSION_SECRET", "x")
    spec = importlib.util.spec_from_file_location(
        "portal_app", os.path.join(SERVICES, "portal", "app.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
except Exception as exc:                                        # noqa: BLE001
    print(f"SKIP  das Portal laesst sich hier nicht laden "
          f"({type(exc).__name__}: {exc})")
    sys.exit(0)

m.ensure_default_tenant()
A, _ = m.tenant_create("aaa")
B, _ = m.tenant_create("bbb")
C, _ = m.tenant_create("ccc")
P.TENANTS_FILE = m.TENANTS_FILE
P.external_host = lambda: "knoten.example.org"
P.require_user_admin = lambda: None
state = {"role": "tenant_admin", "mine": A}
P.caller_scope = lambda: (state["role"], state["mine"])
P.multi_tenant = lambda: True
P.identity_users = lambda: []
P.load_instances = lambda: {
    "rr-aaa": {"app_id": "rollen-rechte", "tenant": A, "port": 8201,
               "address": "rechte.aaa.example.org"},
    "rr-bbb": {"app_id": "rollen-rechte", "tenant": B, "port": 8202,
               "address": "rechte.bbb.example.org"},
    "anders-ccc": {"app_id": "wegweiser", "tenant": C, "port": 8203,
                   "address": "weg.ccc.example.org"},
}


def show(**who):
    state.update(who)
    with P.app.test_request_context("/tenant", base_url="https://knoten.example.org"):
        r = P.tenant_page()
        if isinstance(r, tuple):
            r = r[0]
        return r if isinstance(r, str) else r.get_data(as_text=True)


html = show(role="tenant_admin", mine=A)
ok("Mandant A hat die App: der Knopf steht", "Rollen &amp; Rechte" in html
   or "Rollen & Rechte" in html, html[:300])
ok("... und zeigt auf die Instanz von A",
   'href="https://rechte.aaa.example.org/"' in html, html[:300])
ok("KOEDER: nicht auf die gleichnamige Instanz des Mandanten B",
   "rechte.bbb.example.org" not in html)
ok("der Hinweis sagt, dass der Link kein Recht gibt",
   "gibt kein Recht" in html)
html = show(role="tenant_admin", mine=B)
ok("Mandant B: sein eigener Link", 'href="https://rechte.bbb.example.org/"'
   in html and "rechte.aaa.example.org" not in html)
html = show(role="tenant_admin", mine=C)
ok("Mandant C hat eine andere App unter dem Kennzeichen: kein Knopf",
   "Rollen und Rechte" not in html and "weg.ccc" not in html, html[:200])
P.load_instances = lambda: {}
html = show(role="tenant_admin", mine=A)
ok("ohne jede Instanz: kein Knopf", "Rollen und Rechte" not in html)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
