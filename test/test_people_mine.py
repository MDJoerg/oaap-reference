#!/usr/bin/env python3
"""Zuständige hören ihre Instanz, „Meine Instanzen“ (RFC-0143 Stufe 2).

Eine Person, die als Betreuer oder Verantwortlich an eine Instanz gebunden
ist, bekommt deren Meldungen (`instance.down`, `site.google`,
`site.notfound`, `site.check`) per E-Mail -- ohne auf einer Liste des
Mandanten zu stehen (D4). Beobachter hören nichts, solange sie es nicht
einschalten; jede Person schaltet für sich selbst ab oder an, über den
Spool (`people:self`), und nur für sich selbst. Die Regel steht in einer
Datei (services/bindings.py), die Knoten und Portal lesen.

Im Portal: die Seite „Meine Instanzen“ mit den Knöpfen der Rolle; die
Knöpfe gehen durch dieselben Türen wie die der Verwaltung, die jetzt auch
eine Bindung gelten lassen.

Der Test beißt: ohne den Empfänger in der Zustellung, ohne den Vorgabewert
für Betreuer und ohne den Weg über die Produktivinstanz fallen Prüfungen.

Aufruf: python3 test/test_people_mine.py
"""
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-people-mine-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402
import bindings                                               # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
CALLS = []
m.maintenance_set = lambda name, inst, on, message="", who="", role="": (
    CALLS.append(("maintenance", name, on, who, role)) or "is in maintenance")
os.makedirs(m.APPS_DIR, exist_ok=True)
tid = m.ensure_default_tenant()
tenants = m.load_tenants()
tenants["t-club"] = {"label": "verein", "slug": "verein", "name": "Verein"}
m.save_tenants(tenants)
CLUB = "t-club"


def inst(name, channel="production", **kw):
    return {"app_id": "website", "name": name, "tenant": CLUB, "channel": channel,
            "port": 8400, "id": name[:1] * 12,
            "routes": [{"path": "/", "roles": ["public"]}], **kw}


reg = m.load_registry()
reg["instances"].update({
    "web": inst("web", source={"kind": "artifact", "promoted_from": "web-test"},
                site_check={"on": True}),
    "web-test": inst("web-test", channel="test"),
    "shop": inst("shop"),
})
m.save_registry(reg)
d = os.path.join(DATA, "data", "identity")
os.makedirs(d, exist_ok=True)
USERS = [
    {"username": "root", "roles": ["server_admin"], "tenant": "", "active": True,
     "email": "root@example.org"},
    {"username": "tina", "roles": ["tenant_admin"], "tenant": CLUB, "active": True,
     "email": "tina@example.org"},
    {"username": "uli", "roles": ["user"], "tenant": CLUB, "active": True,
     "email": "uli@example.org"},
    {"username": "olga", "roles": ["partner"], "tenant": CLUB, "active": True,
     "email": "olga@example.org"},
    {"username": "bea", "roles": ["user"], "tenant": CLUB, "active": True,
     "email": "bea@example.org"},
    {"username": "nomail", "roles": ["user"], "tenant": CLUB, "active": True},
]


def write_users():
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(USERS, f)


write_users()
m.people_set("web", "add", "uli", "caretaker", who="tina")
m.people_set("web", "add", "olga", "owner", who="tina")
m.people_set("web", "add", "bea", "observer", who="tina")
m.people_set("web", "add", "nomail", "caretaker", who="tina")

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(**req):
    _rid[0] += 1
    rid = f"r{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, **req}, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


# --- the mail goes nowhere: what would have been sent is kept ---------------
SENT = []
m.notify_mailbox = lambda t: ({"host": "smtp.example.org", "port": 587, "tls": "starttls",
                               "from": "noreply@example.org"}, "OAAP")
SMTP_OK = [True]


def fake_smtp(mail, sender, rcpts, subject, body, html=None, headers=None):
    SENT.append({"to": list(rcpts["to"]), "cc": list(rcpts["cc"]), "bcc": list(rcpts["bcc"]),
                 "subject": subject, "body": body})
    return (True, "accepted", False) if SMTP_OK[0] else (False, "421 busy", False)


m.notify_smtp = fake_smtp


def deliver(ntype, instanz, op="open", kurz="2 rot", text="Zwei rote Befunde."):
    for n in os.listdir(m.NOTIFY_OUTBOX_DIR) if os.path.isdir(m.NOTIFY_OUTBOX_DIR) else []:
        os.remove(os.path.join(m.NOTIFY_OUTBOX_DIR, n))
    SENT.clear()
    m.notify_event(ntype, CLUB, instanz=instanz, kurz=kurz, text=text, op=op)
    with contextlib.redirect_stdout(io.StringIO()):
        m.notify_deliver(datetime.datetime.now(datetime.timezone.utc))
    return SENT


def to_whom(sent):
    return sorted(a for s in sent for a in s["to"] + s["cc"] + s["bcc"])


print("=== die Regel: wer hört was (bindings.hears / listeners) ===")
insts = m.load_registry()["instances"]
who = bindings.listeners(insts, "web", "site.check", USERS, m.resolve_tenant)
ok("Betreuer und Verantwortliche hören, Beobachter nicht, ohne Adresse niemand",
   sorted(p for p, _a, _r in who) == ["olga", "uli"], who)
ok("eine Art, die keine Instanz-Sache ist, hört niemand",
   bindings.listeners(insts, "web", "backup.failed", USERS, m.resolve_tenant) == [])
who_t = bindings.listeners(insts, "web-test", "site.check", USERS, m.resolve_tenant)
ok("Test-Instanz: die Zuständigen der Produktivinstanz hören mit (D2)",
   sorted(p for p, _a, _r in who_t) == ["olga", "uli"], who_t)
ok("nicht gebundene Instanz: niemand",
   bindings.listeners(insts, "shop", "site.check", USERS, m.resolve_tenant) == [])

print("=== Zustellung: site.check erreicht den Betreuer ohne Liste ===")
sent = deliver("site.check", "web")
ok("Betreuer und Verantwortliche bekommen je eine eigene Mail",
   any(s["to"] == ["uli@example.org"] for s in sent)
   and any(s["to"] == ["olga@example.org"] for s in sent), sent)
ok("die Liste des Mandanten bekommt ihre Mail wie bisher (tina, Mandantenverwaltung)",
   any("tina@example.org" in s["to"] for s in sent), sent)
ok("keine Mail nennt einen Zuständigen neben anderen Empfängern",
   all(len(s["to"] + s["cc"] + s["bcc"]) == 1 for s in sent
       if "uli@example.org" in s["to"] + s["cc"] + s["bcc"]), sent)
ok("Beobachterin bekommt nichts", "bea@example.org" not in to_whom(sent), sent)
mine = [s for s in sent if s["to"] == ["uli@example.org"]]
ok("die Mail nennt den Grund und wo man sie abschaltet",
   mine and "zuständig" in mine[0]["body"] and "Meine Instanzen" in mine[0]["body"]
   and mine[0]["body"].index("zuständig") < mine[0]["body"].index("\n--\n"), mine)
ok("Betreff: die Instanz und der Mandant", mine and "web" in mine[0]["subject"], mine)
log = open(m.NOTIFY_LOG_FILE, encoding="utf-8").read()
ok("Protokoll nennt den Empfänger „Zuständige der Instanz“", bindings.RECIPIENT in log)
sent = deliver("site.check", "web-test")
ok("Meldung über die Test-Instanz erreicht die Zuständigen der Produktivinstanz",
   "uli@example.org" in to_whom(sent), sent)
sent = deliver("site.check", "shop")
ok("nicht gebundene Instanz: nur die Liste", to_whom(sent) == ["tina@example.org"], sent)
sent = deliver("instance.down", "web", kurz="", text="Seit 6 Minuten nicht erreichbar.")
ok("instance.down: auch an die Zuständigen", "uli@example.org" in to_whom(sent), sent)
sent = deliver("backup.failed", "web")
ok("keine Instanz-Sache (backup.failed): nichts an Zuständige",
   "uli@example.org" not in to_whom(sent), sent)

print("=== Wiederholen: wer schon hat, bekommt es kein zweites Mal ===")
SMTP_OK[0] = False
sent = deliver("site.check", "web")
files = os.listdir(m.NOTIFY_OUTBOX_DIR)
ok("Mailserver nimmt nicht: die Meldung bleibt im Ausgang", len(files) == 1, files)
SMTP_OK[0] = True
SENT.clear()
with contextlib.redirect_stdout(io.StringIO()):
    m.notify_deliver(datetime.datetime.now(datetime.timezone.utc))
ok("zweiter Versuch: jetzt an beide Zuständige, die Liste nicht doppelt",
   sorted(to_whom(SENT)) == ["olga@example.org", "tina@example.org", "uli@example.org"],
   SENT)

print("=== der eigene Schalter (D4) über den Spool ===")
r = queue(action="instance-people", instance="web", op="notify", person="uli",
          type="site.check", on=False, by="uli")
ok("Betreuer schaltet site.check für sich ab", r.get("ok"), r)
b = next(p for p in m.load_registry()["instances"]["web"]["people"] if p["person"] == "uli")
ok("am Eintrag gespeichert", b.get("notify") == {"site.check": False}, b)
sent = deliver("site.check", "web")
ok("… und bekommt site.check nicht mehr", "uli@example.org" not in to_whom(sent), sent)
sent = deliver("site.google", "web")
ok("… andere Arten weiter", "uli@example.org" in to_whom(sent), sent)
r = queue(action="instance-people", instance="web", op="notify", person="bea",
          type="site.check", on=True, by="bea")
ok("Beobachterin schaltet site.check für sich ein", r.get("ok"), r)
sent = deliver("site.check", "web")
ok("… und bekommt es dann", "bea@example.org" in to_whom(sent), sent)
r = queue(action="instance-people", instance="web", op="notify", person="olga",
          type="site.check", on=False, by="uli")
ok("für jemand anderen schalten: abgewiesen", not r.get("ok")
   and "requires server_admin" in r.get("message", ""), r)
r = queue(action="instance-people", instance="web", op="add", person="uli", role="owner",
          by="uli")
ok("sich selbst hochstufen: abgewiesen (D3)", not r.get("ok"), r)
r = queue(action="instance-people", instance="web", op="notify", person="uli",
          type="backup.failed", on=True, by="uli")
ok("unbekannte Art: abgewiesen", not r.get("ok") and "type must be one of" in r["message"], r)
r = queue(action="instance-people", instance="web-test", op="notify", person="uli",
          type="site.google", on=False, by="uli")
ok("über die Produktivinstanz gebunden: der Schalter landet an deren Bindung",
   r.get("ok") and next(p for p in m.load_registry()["instances"]["web"]["people"]
                        if p["person"] == "uli")["notify"].get("site.google") is False, r)
m.people_set("web", "set", "uli", "owner", who="tina")
b = next(p for p in m.load_registry()["instances"]["web"]["people"] if p["person"] == "uli")
ok("Rolle ändern behält die eigenen Schalter", b.get("notify", {}).get("site.check") is False, b)
m.people_set("web", "set", "uli", "caretaker", who="tina")
logt = "\n".join(json.dumps(e) for e in m.read_tenant_log(limit=0))
ok("Protokoll: instance.people mit Art an/aus", "uli: site.check on -> off" in logt)

print("=== CLI ===")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_people(types.SimpleNamespace(name="web", op=None, person=None, role=None,
                                       switch=None))
ok("Liste zeigt, was jede Person hört", "hears:" in out.getvalue()
   and "site.google" in out.getvalue(), out.getvalue())
out = io.StringIO()
os.environ.pop("SUDO_USER", None)
with contextlib.redirect_stdout(out):
    m.cmd_people(types.SimpleNamespace(name="web", op="notify", person="olga",
                                       role="instance.down", switch="off"))
ok("oaap app people web notify olga instance.down off", "no longer hears" in out.getvalue(),
   out.getvalue())

print("=== Mutation: beißt der Test? ===")


def bites(patch, undo, inst_name="web"):
    patch()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            got = deliver("site.google", inst_name)
    finally:
        undo()
    return "olga@example.org" not in to_whom(got)


saved_bound = m._notify_bound
ok("ohne den Empfänger in der Zustellung fällt die Prüfung", bites(
    lambda: setattr(m, "_notify_bound", lambda item, state, now: ""),
    lambda: setattr(m, "_notify_bound", saved_bound)))
saved_hears = bindings.hears
ok("ohne den Vorgabewert für Betreuer/Verantwortliche fällt die Prüfung", bites(
    lambda: setattr(bindings, "hears", lambda b, t: bool((b.get("notify") or {}).get(t))),
    lambda: setattr(bindings, "hears", saved_hears)))
saved_prod = bindings.production_of
ok("ohne den Weg über die Produktivinstanz fällt die Prüfung (Test-Instanz)", bites(
    lambda: setattr(bindings, "production_of", lambda insts, key, resolve: (None, None)),
    lambda: setattr(bindings, "production_of", saved_prod), inst_name="web-test"))

print("=== Portal: Meine Instanzen ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
try:
    import app as portal  # noqa: E402
except ImportError as e:
    portal = None
    print("SKIP  Portal (Flask fehlt):", e)
if portal is not None:
    regnow = m.load_registry()["instances"]
    WHO = {"user": "uli", "roles": "user"}
    portal.identity_users = lambda: [u for u in USERS if u["username"] == WHO["user"]]
    portal.load_instances = lambda: regnow
    portal.resolve_tenant = m.resolve_tenant
    portal.host_tenant_scope = lambda host: (None, True)
    portal.fleet_view.probe_all = lambda items, fn: {k: ("ok", "Läuft", "") for k, _i in items}
    portal.METRICS_DIR = os.path.join(DATA, "metrics")
    QUEUED = []
    portal._deploy_now = lambda name: False
    portal._queue_and_wait = lambda name, payload, wait: (
        QUEUED.append((name, payload)) or {"ok": True, "message": "ok"})
    c = portal.app.test_client()

    def H():
        return {"X-OAAP-User": WHO["user"], "X-OAAP-Roles": WHO["roles"]}

    r = c.get("/meine-instanzen", headers=H())
    html = r.get_data(as_text=True)
    ok("Seite: 200, beide Instanzen (web direkt, web-test über web)",
       r.status_code == 200 and 'id="i-web"' in html and 'id="i-web-test"' in html,
       (r.status_code, html[:600]))
    ok("Betreuer: Knöpfe Wartung, Neu starten, Jetzt prüfen, nicht Seiten-Check schalten",
       "Wartung an" in html and "Neu starten" in html and "Jetzt prüfen" in html
       and "Seiten-Check aus" not in html, html[html.find('id="i-web"'):][:1500])
    ok("Schalter je Meldungsart, mit dem eigenen Stand",
       "Seiten-Check" in html and "Abschalten" in html and "Einschalten" in html)
    ok("Hinweis über die Produktivinstanz", "über die Produktivinstanz" in html)
    ok("Menüeintrag „Meine Instanzen“", 'href="/meine-instanzen"' in html)
    r = c.post("/instances/web/maintenance", data={"op": "on", "back": "mine"}, headers=H())
    ok("Betreuer: Wartung über die Tür der Verwaltung, zurück zu Meine Instanzen",
       r.status_code == 303 and r.headers["Location"].startswith("/meine-instanzen")
       and QUEUED[-1] == ("web", {"action": "maintenance", "on": True, "message": ""}),
       (r.status_code, r.headers.get("Location"), QUEUED[-1:]))
    n = len(QUEUED)
    r = c.post("/instances/web/site-check", data={"op": "off", "back": "mine"}, headers=H())
    ok("Betreuer: Seiten-Check schalten wird schon im Portal abgewiesen",
       r.status_code in (403, 404) and len(QUEUED) == n, r.status_code)
    r = c.post("/instances/shop/maintenance", data={"op": "on"}, headers=H())
    ok("nicht gebundene Instanz: wie bisher abgewiesen", r.status_code in (403, 404)
       and len(QUEUED) == n, r.status_code)
    r = c.get("/instances/web", headers=H())
    ok("die Instanzseite der Verwaltung bleibt zu", r.status_code in (403, 404), r.status_code)
    r = c.post("/meine-instanzen/web/notify", data={"type": "site.google", "op": "on",
                                                    "back": "mine"}, headers=H())
    ok("eigener Schalter: Spool-Auftrag mit der eigenen Person",
       r.status_code == 303 and QUEUED[-1][1] == {
           "action": "instance-people", "op": "notify", "person": "uli",
           "type": "site.google", "on": True}, (r.status_code, QUEUED[-1:]))
    WHO.update(user="olga", roles="partner")
    html = c.get("/meine-instanzen", headers=H()).get_data(as_text=True)
    ok("Verantwortliche: zusätzlich Seiten-Check schalten", "Seiten-Check aus" in html)
    WHO.update(user="nomail", roles="user")
    html = c.get("/meine-instanzen", headers=H()).get_data(as_text=True)
    ok("ohne E-Mail-Adresse: der Hinweis", "keine E-Mail-Adresse" in html)
    WHO.update(user="tina", roles="tenant_admin")
    html = c.get("/meine-instanzen", headers=H()).get_data(as_text=True)
    ok("nicht gebunden: leere Seite mit Satz, kein Menüeintrag",
       "an keine Instanz gebunden" in html and 'href="/meine-instanzen"' not in html)

print()
print("OK" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
