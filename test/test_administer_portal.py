#!/usr/bin/env python3
"""Eine App, die Rechte verwaltet, aus dem Store installieren (Jörg, 06.11.).

oaap.core.authorization 2.10: der Schlüssel `oaap.authz.admin` wird nur
mit der Zusage des Betreibers geprägt. Bisher ging das nur auf der
Maschine (`--confirm-administer`); jetzt auch im Store des Portals --
aber nur durch einen server_admin. Geprüft wird:

    Worker (der Knoten urteilt, nicht die Anfrage)
    - ohne Zusage: abgelehnt, das Ergebnis sagt `needs: confirm_administer`,
      die Meldung ohne Marke
    - Zusage eines server_admin für genau diese App: geht durch
    - Zusage eines tenant_admin: zählt nicht
    - Zusage für eine andere App: zählt nicht
    Portal
    - die Ablehnung wird zur Frage: Weiterleitung auf die App-Seite mit
      need=administer und dem Formularstand
    - die Seite erklärt den Schlüssel; nur der server_admin bekommt das
      Häkchen, der tenant_admin einen Satz
    - nur ein server_admin schickt die Zusage überhaupt in den Spool

Aufruf (im Portal-Abbild): python3 test/test_administer_portal.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-administer-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import appctl as a  # noqa: E402

a.reload_gateway = lambda: None
a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


default_id = a.ensure_default_tenant()
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([
        {"username": "betreiber", "roles": ["server_admin"], "active": True,
         "tenant": default_id, "groups": []},
        {"username": "chef", "roles": ["tenant_admin"], "active": True,
         "tenant": default_id, "groups": []},
    ], f)
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(a.SPOOL_DIR, d), exist_ok=True)

print("=== die Ablehnung trägt die Marke ===")
why = a.authz_admin_refusal("rollen-rechte", True, False, False)
ok("ohne Zusage abgelehnt, mit Marke am Ende", why.endswith(a.ADMINISTER_MARK), why[-120:])
ok("nennt auch den Weg über das Portal", "portal" in why)
ok("eine offene Tür fragt nicht noch einmal (Update)",
   a.authz_admin_refusal("rollen-rechte", True, True, False) == "")

SEEN = []
a._store_lookup = lambda app_id, source_id="", prefer="": (
    {"kind": "git", "url": "https://example.invalid/oaap-apps",
     "path": f"apps/{app_id}"}, "0.2.1",
    {"id": "oaap.platform", "name": "Plattform", "trust": "platform"})
a._resolve_revision = lambda src: "abc"


def fake_install(ns):
    SEEN.append(bool(getattr(ns, "confirm_administer", False)))
    why = a.authz_admin_refusal("rollen-rechte", True, False,
                                getattr(ns, "confirm_administer", False))
    if why:
        a.die(why)
    reg = a.load_registry()
    reg["instances"][ns.name] = {"app_id": "rollen-rechte", "name": ns.name,
                                 "channel": "production", "tenant": default_id,
                                 "version": "0.2.1"}
    a.save_registry(reg)


a.cmd_install = fake_install


def ask(extra, by="betreiber", app="rollen-rechte"):
    rid = os.urandom(6).hex()
    req = {"id": rid, "instance": app, "app_id": app, "action": "install", "by": by,
           "source_id": "oaap.platform", "confirm_source": "", "new": True,
           "requested": "now", **extra}
    with open(os.path.join(a.SPOOL_DIR, "queue", rid + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(req, f)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.cmd_process_deploys(None)
    try:
        with open(os.path.join(a.SPOOL_DIR, "results", rid + ".json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {"ok": False, "message": "no result: " + buf.getvalue()[-400:]}


print("\n=== der Worker ===")
r = ask({})
ok("ohne Zusage: abgelehnt, needs confirm_administer",
   not r.get("ok") and r.get("needs") == "confirm_administer", r)
ok("... und die Meldung ohne Marke", a.ADMINISTER_MARK not in r.get("message", ""), r)
r = ask({"confirm_administer": "rollen-rechte"}, by="chef")
ok("Zusage eines tenant_admin zählt nicht", not r.get("ok") and SEEN[-1] is False, (r, SEEN))
r = ask({"confirm_administer": "andere-app"})
ok("Zusage für eine andere App zählt nicht", not r.get("ok") and SEEN[-1] is False, (r, SEEN))
r = ask({"confirm_administer": "rollen-rechte"})
ok("Zusage des server_admin für genau diese App: installiert",
   r.get("ok") and SEEN[-1] is True and "rollen-rechte" in a.load_registry()["instances"], r)

print("\n=== das Portal ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal  # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-administer-portal-")
portal.APPS_REGISTRY_DIR = TMP
portal.STORE_SOURCES_FILE = os.path.join(TMP, "store-sources.json")
portal.SPOOL_DIR = os.path.join(TMP, "spool")
portal.SPOOL_QUEUE = os.path.join(portal.SPOOL_DIR, "queue")
portal.SPOOL_RESULTS = os.path.join(portal.SPOOL_DIR, "results")
os.makedirs(portal.SPOOL_RESULTS, exist_ok=True)
json.dump({"sources": [{"id": "katalog", "name": "Katalog", "url": "catalog:katalog",
                        "trust": "platform", "enabled": True}]},
          open(portal.STORE_SOURCES_FILE, "w"))
json.dump({"schema": "0.2", "name": "Katalog", "apps": [{
    "id": "rollen-rechte", "name": "Rollen & Rechte", "version": "0.2.1",
    "summary": "s", "package": {"catalog": True}}]},
    open(os.path.join(TMP, "catalog-katalog.json"), "w"))
ROLE = {"r": "server_admin"}
portal.visible_instances = lambda: {}
portal.load_instances = lambda: {}
portal.pending_installs = lambda: set()
portal.node_profiles = lambda: []
portal.caller_roles = lambda: {ROLE["r"]}
portal.caller_scope = lambda: (ROLE["r"], None)
portal.multi_tenant = lambda: False
portal.resolve_tenant = lambda ref: ref or ""
portal.INSTALL_WAIT_SECONDS = 3
c = portal.app.test_client()

# The worker's answer, written as soon as the request appears.
import threading  # noqa: E402

ANSWER = {"ok": False, "message": "asks to ADMINISTER", "needs": "confirm_administer"}
SENT = []


def answer_once():
    import time
    for _ in range(60):
        for fn in os.listdir(portal.SPOOL_QUEUE) if os.path.isdir(portal.SPOOL_QUEUE) else []:
            req = json.load(open(os.path.join(portal.SPOOL_QUEUE, fn)))
            os.remove(os.path.join(portal.SPOOL_QUEUE, fn))
            SENT.append(req)
            json.dump(dict(ANSWER, id=req["id"]),
                      open(os.path.join(portal.SPOOL_RESULTS, req["id"] + ".json"), "w"))
            return
        time.sleep(0.1)


def post(form, role):
    ROLE["r"] = role
    t = threading.Thread(target=answer_once)
    t.start()
    r = c.post("/store/install", data=form,
               headers={"X-OAAP-User": "x", "X-OAAP-Roles": role})
    t.join()
    return r


FORM = {"app_id": "rollen-rechte", "source_id": "katalog", "new": "1", "name": "rechte"}
r = post(FORM, "server_admin")
loc = r.headers.get("Location", "")
ok("die Ablehnung wird zur Frage: Weiterleitung mit need=administer und Formularstand",
   r.status_code == 303 and "need=administer" in loc and "nn=rechte" in loc, (r.status_code, loc))
ok("ohne Häkchen keine Zusage im Spool", "confirm_administer" not in SENT[-1], SENT[-1])

ROLE["r"] = "server_admin"
body = c.get(loc, headers={"X-OAAP-User": "x"}).get_data(as_text=True)
ok("die Seite erklärt den Schlüssel", "Er kann nie" in body and "Plattform-Rolle" in body)
ok("server_admin: Häkchen (Pflicht) und derselbe Auftrag noch einmal",
   'name="confirm_administer"' in body and 'value="rollen-rechte" required' in body
   and 'name="name" value="rechte"' in body, body[-3000:])

ROLE["r"] = "tenant_admin"
body = c.get(loc, headers={"X-OAAP-User": "x"}).get_data(as_text=True)
ok("tenant_admin: kein Häkchen, ein Satz", 'name="confirm_administer"' not in body
   and "bestätigt der <strong>Server-Admin</strong>" in body)

ANSWER = {"ok": True, "message": "deployed", "version": "0.2.1"}
r = post(dict(FORM, confirm_administer="rollen-rechte"), "tenant_admin")
ok("tenant_admin schickt die Zusage nicht in den Spool", "confirm_administer" not in SENT[-1], SENT[-1])
r = post(dict(FORM, confirm_administer="rollen-rechte"), "server_admin")
ok("server_admin schickt sie, benannt nach der App",
   SENT[-1].get("confirm_administer") == "rollen-rechte", SENT[-1])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
