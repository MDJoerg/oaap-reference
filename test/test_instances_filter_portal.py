#!/usr/bin/env python3
"""Instanzliste: der Filter überlebt den Weg über die Instanzseite.

Jörg, 23.11.: „Filter wählen wie Mandant, in die Bearbeitung einer
Instanz gehen, zurückkommen -- der Filter ist weg und ich sehe alle
Mandanten.“ Wie der Store (11.11.) merkt sich die Liste ihren Filter in
einem Cookie dieses Browsers; jeder Rückweg geht über /instances?back=1.

    - die Liste setzt das Cookie mit genau den Filterfeldern
    - „← Zurück zur Liste“ auf der Instanzseite führt über back=1
    - back=1 stellt den Filter wieder her, msg/err kommen mit
    - ein schlichtes /instances (Menü, „Zurücksetzen“) leert ihn
    - eine Umleitung nach einer Aktion (Instanz fehlt) behält ihn
    - fremde Felder im Cookie werden nicht übernommen

Aufruf (im Portal-Abbild): python3 test/test_instances_filter_portal.py
"""
import os
import sys
import tempfile
from urllib.parse import parse_qsl, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-inst-filter-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


INST = {
    "pxx-chat": {"app_id": "chat", "app_name": "Chat", "version": "0.21.0",
                 "channel": "production", "tenant": "pxx"},
    "csa-chat": {"app_id": "chat", "app_name": "Chat", "version": "0.21.0",
                 "channel": "production", "tenant": "csa"},
}
portal.visible_instances = lambda: dict(INST)
portal.load_instances = lambda: dict(INST)
portal.care_facts = lambda: {}
portal.catalogue_versions = lambda: {}
portal.pending_installs = lambda: set()
portal.node_profiles = lambda: []
portal.caller_roles = lambda: {"server_admin"}
portal.caller_scope = lambda: ("server_admin", None)
portal.multi_tenant = lambda: True
portal.resolve_tenant = lambda ref: ref or ""
portal.tenant_label = lambda t: t or ""
portal.fleet_view.probe_all = lambda pairs, probe: {n: ("ok", "Läuft", "") for n, _ in pairs}

c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}
COOKIE = portal.INSTANCES_FILTER_COOKIE


def cookie():
    ck = c.get_cookie(COOKIE, path="/instances")
    return dict(parse_qsl(ck.value)) if ck else None


def target(r):
    u = urlparse(r.headers.get("Location", ""))
    return u.path, dict(parse_qsl(u.query))


print("=== Filter setzen ===")
r = c.get("/instances?q=&state=&channel=&tenant=pxx&care=", headers=H)
body = r.get_data(as_text=True)
ok("Liste mit Filter antwortet", r.status_code == 200, body[:300])
ok("nur pxx in der Liste", "pxx-chat" in body and "csa-chat" not in body)
ok("Cookie hält genau tenant=pxx", cookie() == {"tenant": "pxx"}, cookie())

print("\n=== Zurück von der Instanzseite ===")
ok("Rückweg in den Vorlagen führt über back=1",
   '<a class="back" href="/instances?back=1">' in portal.INSTANCE_EDIT_BODY
   and 'href="/instances">← Zurück' not in portal.INSTANCE_EDIT_BODY)
r = c.get("/instances?back=1", headers=H)
ok("back=1 leitet um", r.status_code == 303, r.status_code)
ok("… auf den gemerkten Filter", target(r) == ("/instances", {"tenant": "pxx"}), target(r))
r = c.get("/instances?back=1&msg=Gespeichert.", headers=H)
ok("msg kommt mit", target(r) == ("/instances", {"tenant": "pxx", "msg": "Gespeichert."}),
   target(r))

print("\n=== Umleitung nach einer Aktion ===")
r = c.get("/instances/gibtsnicht", headers=H)
p, q = target(r)
ok("Instanz fehlt → back=1 mit err", p == "/instances" and q.get("back") == "1"
   and "err" in q, (r.status_code, r.headers.get("Location")))
r = c.get(r.headers["Location"], headers=H)
ok("… und landet im gefilterten Stand",
   target(r)[1].get("tenant") == "pxx" and "err" in target(r)[1], target(r))

print("\n=== Zurücksetzen ===")
r = c.get("/instances", headers=H)
body = r.get_data(as_text=True)
ok("schlichtes /instances zeigt alle", "pxx-chat" in body and "csa-chat" in body)
ok("… und leert das Cookie", cookie() == {}, cookie())
r = c.get("/instances?back=1", headers=H)
ok("back=1 danach ohne Filter", target(r) == ("/instances", {}), target(r))

print("\n=== fremde Felder ===")
c.set_cookie(COOKIE, "tenant=csa&evil=1&back=1", path="/instances")
r = c.get("/instances?back=1", headers=H)
ok("nur bekannte Filterfelder", target(r) == ("/instances", {"tenant": "csa"}), target(r))

print(f"\n{'OK' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
