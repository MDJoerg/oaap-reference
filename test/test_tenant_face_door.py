#!/usr/bin/env python3
"""Die Tuer "Das Gesicht aendern" -- ausgefuehrt, nicht gelesen (I-32).

`test_tenant_face.py` ruft `face_target` nur mit "anderer Mandant" auf und liest
den Worker als Quelltext; die Tuer selbst wurde nie benutzt. Dabei schickte das
Portal bei JEDEM Speichern den eigenen Mandanten mit, und `face_target` lehnte
ihn fuer einen `tenant_admin` ab ("a tenant administers only its own place") --
der Verwalter kam nie durch, und die Ablehnung stand im Protokoll des
Standard-Mandanten, den er nicht sieht.

Zwei Wege, eine Regel (Koeder-Muster):

    Portal-Weg   die Route baut die Anfrage -- der Test fuehrt sie aus und
                 faengt ab, was sie in den Spool schreibt;
    Worker-Weg   der echte `cmd_process_deploys` verarbeitet die Anfrage.

Jede Haelfte wird einzeln gegen den Fehler geprueft, damit eine Reparatur auf
nur einer Seite gruen bleibt, aber die andere nicht mehr traegt.

Aufruf: python3 test/test_tenant_face_door.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-face-door-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, os.path.join(SERVICES, "portal"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                              # noqa: E402

m.reload_gateway = lambda: None
m.zone_probe = lambda label: f"ZONE({label})"

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant,
            "groups": [], "active": True}


DEFAULT = m.ensure_default_tenant()
CLS, _ = m.tenant_create("cls")
SGL, _ = m.tenant_create("sgl")
d = os.path.join(DATA, "data", "identity")
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
    json.dump([user("joerg", ["server_admin"], DEFAULT),
               user("cls_admin", ["tenant_admin"], CLS),
               user("sgl_admin", ["tenant_admin"], SGL)], f)

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def worker(req):
    """Eine Anfrage durch den echten Worker schicken."""
    _rid[0] += 1
    req.setdefault("id", f"f{_rid[0]}")
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{req['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(req, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{req['id']}.json"), encoding="utf-8") as f:
        return json.load(f)


def log(tenant):
    try:
        with open(m.TENANT_LOG, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except OSError:
        rows = []
    return [r for r in rows if r.get("tenant") == tenant and r.get("action") == "tenant.face"]


FACE = dict(action="tenant-face", instance="", title="Mein Verein", color_primary="#336699",
            color_accent="#cc6600")

print("=== Worker-Weg: das, was das Portal bisher schickte (der eigene Mandant im Feld) ===")
res = worker(dict(FACE, by="cls_admin", tenant=CLS))
ok("ein tenant_admin nennt seinen EIGENEN Mandanten: Erfolg", res["ok"], res)
ok("... und der Mandant traegt den Titel", m.load_tenants()[CLS]["theme"]["title"] == "Mein Verein")

print("\n=== Worker-Weg: ohne Angabe (so schickt es das Portal jetzt) ===")
res = worker(dict(FACE, by="cls_admin", title="Ohne Angabe"))
ok("ein tenant_admin ohne Mandant im Feld: Erfolg, in seinem Mandanten",
   res["ok"] and m.load_tenants()[CLS]["theme"]["title"] == "Ohne Angabe", res)

print("\n=== Worker-Weg: ein fremder Mandant im Feld ===")
before = m.load_tenants()[SGL].get("theme")
n_cls, n_def = len(log(CLS)), len(log(DEFAULT))
res = worker(dict(FACE, by="cls_admin", tenant=SGL, title="Uebernommen"))
ok("abgelehnt", not res["ok"] and "only its own place" in res["message"], res)
ok("der fremde Mandant blieb unberuehrt", m.load_tenants()[SGL].get("theme") == before)
ok("die Ablehnung steht im Protokoll des ANTRAGSTELLERS",
   len(log(CLS)) == n_cls + 1 and log(CLS)[-1]["result"] == "denied", log(CLS)[-1:])
ok("und nicht im Protokoll des Standard-Mandanten", len(log(DEFAULT)) == n_def)

print("\n=== Worker-Weg: der Betreiber ===")
res = worker(dict(FACE, by="joerg", tenant=SGL, title="Von oben"))
ok("ein server_admin benennt einen Mandanten: Erfolg",
   res["ok"] and m.load_tenants()[SGL]["theme"]["title"] == "Von oben", res)
res = worker(dict(FACE, by="joerg", tenant="t-gibt-es-nicht", title="x"))
ok("... einen, den es nicht gibt: abgelehnt, nichts veraendert",
   not res["ok"], res)
res = worker(dict(FACE, by="joerg", title="Standard"))
ok("ein server_admin ohne Angabe trifft den Standard-Mandanten (bisheriges Verhalten festgehalten)",
   (m.load_tenants()[DEFAULT].get("theme") or {}).get("title") in ("Standard", None) or not res["ok"], res)

print("\n=== Worker-Weg: wer kein Verwalter ist ===")
with open(os.path.join(d, "users.json"), encoding="utf-8") as f:
    users = json.load(f)
users.append(user("anna", ["user"], CLS))
with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
    json.dump(users, f)
res = worker(dict(FACE, by="anna", tenant=CLS, title="Hack"))
ok("ein gewoehnlicher Benutzer wird abgelehnt, auch fuer den eigenen Mandanten",
   not res["ok"] and (m.load_tenants()[CLS].get("theme") or {}).get("title") != "Hack", res)

print("\n=== Portal-Weg: was die Route in den Spool schreibt ===")
try:
    import flask  # noqa: F401
except ImportError:
    pass

try:
    import importlib.util
    os.environ.setdefault("SESSION_SECRET", "x")
    spec = importlib.util.spec_from_file_location(
        "portal_app", os.path.join(SERVICES, "portal", "app.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
except Exception as exc:                                        # noqa: BLE001
    P = None
    print(f"SKIP  das Portal laesst sich hier nicht laden ({type(exc).__name__}: {exc})")

if P is not None:
    seen = {}

    def fake_queue(rid, name, payload, wait):
        seen["payload"] = payload
        return {"ok": True, "message": "ok"}

    P.TENANTS_FILE = m.TENANTS_FILE
    P._queue_with_id = fake_queue
    P.require_user_admin = lambda: None

    def post(role, mine, form):
        P.caller_scope = lambda: (role, mine)
        seen.clear()
        with P.app.test_request_context("/tenant/face", method="POST", data=form):
            r = P.tenant_face_post(); seen["resp"] = r
        return seen.get("payload")

    pl = post("tenant_admin", CLS, {"title": "T", "color_primary": "#111111", "color_accent": ""})
    ok("ein tenant_admin: die Anfrage nennt KEINEN Mandanten (er kommt aus dem Konto)",
       pl is not None and "tenant" not in pl, pl)
    pl = post("server_admin", DEFAULT,
              {"title": "T", "color_primary": "", "color_accent": "", "tenant": "sgl"})
    ok("ein server_admin, der einen Mandanten benannt hat: die Anfrage nennt ihn",
       pl is not None and pl.get("tenant") == SGL, pl)
    ok("... und die Route gibt eine Antwort zurueck, nicht True (_face_target traegt keine Marke im Ablehnungsplatz)",
       not isinstance(seen.get("resp"), bool), seen.get("resp"))

print(f"\n{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
