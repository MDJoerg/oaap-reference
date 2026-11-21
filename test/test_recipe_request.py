#!/usr/bin/env python3
"""Anfragen eines Mandanten an den Betreiber (RFC-0109 §13, Stufe 3).

Die Sätze, die diese Datei verteidigt:

    Die PLATTFORM schreibt die Anfrage als Vorgang in den Posteingang des
    Knotens (eine Vorgänge-Instanz im eigenen Mandanten des Knotens), durch
    dessen Knotentür; der Mandant braucht dort nichts.
    Ohne Posteingang, oder wenn er nicht antwortet, geht sie als Nachricht
    an die Verteiler des Knotens (recipe.request).
    Ein tenant_admin fragt nur für SEINEN Mandanten (S2, nach Kennung).
    Die Anfrage trägt nur, was der Mensch eingab und was die Vorschau IHM
    zeigte (oder den Satz, mit dem sie ablehnte); kein Geheimnis (S3).
    Der Stand und die Antworten des Betreibers werden zurückgelesen.
    Eine Freigabe beantwortet die offenen Anfragen dieses Mandanten nach
    diesem Rezept und schreibt es in ihren Vorgang.
    Der Posteingang ist nur eine Produktiv-Vorgänge-Instanz des Knotens.
    Die Seite Einrichten stellt die Anfrage als Auftrag mit dem eigenen
    Kürzel; Aufbau zeigt dem Betreiber die offenen Anfragen und den
    Posteingang.

Aufruf: python3 test/test_recipe_request.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-recipe-req-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
# RFC-0109 semantics: without the templates the platform ships (RFC-0117 D4
# makes a shipped template free; test_assistant_templates.py measures that)
a.SHIPPED_RECIPE_DIR = tempfile.mkdtemp(prefix="oaap-no-shipped-")
import recipe_request as rq                                   # noqa: E402
import notify                                                 # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def refusal(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except (a.tenant_build.Refusal, rq.Refusal) as exc:
        return str(exc)
    return ""


print("=== der reine Kern ===")
r = rq.new_request("kunde", "kv", "Karla V.", recipe="sap-sso", version="1.0.0",
                   title="SAP SSO einrichten", values={"sap_url": "https://a4h:44300"},
                   text="Wir brauchen SAP SSO.", seen=rq.preview_summary(refused="not released"))
ok("eine Anfrage: Kennung q-…-kunde, noch nicht zugestellt",
   rq.ID_RE.match(r["id"]) and r["id"].endswith("-kunde") and r["delivery"] == "", r)
ok("ohne Rezept und ohne Text: abgelehnt",
   "say what you need" in refusal(rq.new_request, "kunde", "kv", "K"))
ok("ein Wert mit Geheimwort im Namen: abgelehnt (S3)",
   "secret" in refusal(rq.new_request, "kunde", "kv", "K", text="x",
                       values={"api_token": "abc"}))
ok("zu langer Text: abgelehnt", "2000" in refusal(rq.new_request, "kunde", "kv", "K",
                                                   text="x" * 2001))
body = rq.case_body(r, "oaapx01")
ok("der Text des Vorgangs: Mandant, Person, Rezept, Werte, Text, was die Seite sagte, wie man freigibt",
   "Mandant kunde" in body and "Karla V." in body and "sap-sso 1.0.0" in body
   and "sap_url=https://a4h:44300" in body and "Wir brauchen SAP SSO." in body
   and "not released" in body and "oaap tenant allow kunde" in body, body)
ok("der Titel", rq.case_title(r) == "Anfrage aus kunde: SAP SSO einrichten", rq.case_title(r))
changed = rq.apply_reading(r, {"ok": True, "status": "answered", "status_label": "beantwortet",
                               "replies": [{"at": "t", "by": "Max", "text": "Freigegeben."}]})
ok("eine Lesung ändert Stand und Antworten", changed and r["case_status"] == "answered"
   and r["replies"][0]["text"] == "Freigegeben.", r)
ok("dieselbe Lesung noch einmal: nichts Neues", not rq.apply_reading(
    r, {"ok": True, "status": "answered", "status_label": "beantwortet",
        "replies": [{"at": "t", "by": "Max", "text": "Freigegeben."}]}))
r["delivery"] = "sent"
ok("offen, solange der Vorgang nicht geschlossen ist", rq.is_open(r))
r["case_status"] = "closed"
ok("geschlossen: nicht mehr offen", not rq.is_open(r))
ok("im Katalog der Nachrichten: recipe.request, nur der Knoten hört es",
   notify.CATALOGUE["recipe.request"]["scope"] == "node")

print("")
print("=== auf dem Knoten ===")
REG = {"instances": {
    "posteingang": {"tenant": "t-default", "app_id": "vorgaenge", "name": "posteingang",
                    "port": 8301, "channel": "production"},
    "kunde-vorgaenge": {"tenant": "t-kunde", "app_id": "vorgaenge", "name": "vorgaenge",
                        "port": 8302, "channel": "production"},
    "test-vorgaenge": {"tenant": "t-default", "app_id": "vorgaenge", "name": "test-vorgaenge",
                       "port": 8303, "channel": "test"},
    "kunde-chat": {"tenant": "t-kunde", "app_id": "chat", "name": "chat"},
}}
a.load_registry = lambda: REG
a.resolve_tenant = lambda t: t
a._instance_tenant_id = lambda inst: inst.get("tenant", "")
a.is_rehearsal = lambda inst: False
TENANTS = {"t-kunde": {"label": "kunde"}, "t-pxx": {"label": "pxx"},
           "t-default": {"label": "node"}}
AUDIT = []
a.load_tenants = lambda: TENANTS
a.save_tenants = lambda t: TENANTS.update(t)
a.default_tenant_id = lambda: "t-default"
a.audit_tenant = lambda action, tenant, **kw: AUDIT.append((action, tenant, kw.get("subject"),
                                                            kw.get("detail", "")))
a._read_identity_users = lambda: [{"username": "kv", "display_name": "Karla V."}]


def _by_label(label, include_former=False):
    for tid, t in TENANTS.items():
        if t.get("label") == label:
            return tid, t
    return None, None


a.tenant_by_label = _by_label
a.instance_key = lambda tid, loc: tid[2:] + "-" + loc
CASES = {}
POSTS = []
INBOX_UP = {"up": True}


def fake_post(name, inst, doc):
    POSTS.append((name, dict(doc)))
    if not INBOX_UP["up"]:
        raise OSError("connection refused")
    if doc["op"] == "open":
        have = next((n for n, c in CASES.items() if c["ref"] == doc["ref"]), None)
        if have:
            return 200, {"ok": True, "number": have, "existing": True}
        num = f"V-2026-{len(CASES) + 1:04d}"
        CASES[num] = {"ref": doc["ref"], "status": "new", "replies": [], "notes": [],
                      "doc": doc}
        return 201, {"ok": True, "number": num}
    c = CASES.get(doc.get("number"))
    if not c:
        return 404, {"error": "nicht angelegt"}
    if doc["op"] == "note":
        c["notes"].append(doc["text"])
        return 200, {"ok": True}
    if doc["op"] == "read":
        return 200, {"ok": True, "number": doc["number"], "status": c["status"],
                     "status_label": {"new": "neu", "answered": "beantwortet",
                                      "closed": "geschlossen"}[c["status"]],
                     "replies": c["replies"]}
    return 400, {"error": "op"}


a._node_case_post = fake_post
NOTES = []
a.notify_event = lambda ntype, tid, **kw: (NOTES.append((ntype, tid, kw)) or "queued.json")
DOCS = os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "rezepte",
                    "hilfe-assistent.json")
os.makedirs(a.RECIPE_DIR, exist_ok=True)
shutil.copy(DOCS, a.tenant_recipe_path("hilfe-assistent"))
os.makedirs(a.APPS_DIR, exist_ok=True)

ok("kein Posteingang benannt", a.recipe_inbox() == ("", None))
ok("Posteingang: eine Instanz eines Kunden wird abgelehnt",
   "own tenant" in refusal(a.recipe_inbox_set, "kunde-vorgaenge"))
ok("…eine Test-Instanz auch", "production" in refusal(a.recipe_inbox_set, "test-vorgaenge"))
ok("…eine andere App auch", "not an instance of the app" in refusal(a.recipe_inbox_set,
                                                                     "kunde-chat"))
ok("…und ein tenant_admin benennt keinen", "server_admin" in refusal(
    a.recipe_inbox_set, "posteingang", "kv", "tenant_admin"))

# without an inbox: a message
req = a.tenant_recipe_request("kunde", "kv", "tenant_admin", actor_tid="t-kunde",
                              recipe="hilfe-assistent", values={"model": "chat-default"},
                              text="Bitte freigeben.")
ok("ohne Posteingang: als Nachricht an den Knoten (recipe.request), Stand gespeichert",
   req["delivery"] == "message" and NOTES and NOTES[-1][0] == "recipe.request"
   and NOTES[-1][1] == "t-default" and "Bitte freigeben." in NOTES[-1][2]["text"]
   and rq.load(a.RECIPE_REQUEST_DIR, req["id"])["delivery"] == "message", (req, NOTES))
ok("die Vorschau als der tenant_admin: die Ablehnung, weil nicht freigegeben, geht mit",
   "not released" in (req["seen"].get("refused") or ""), req["seen"])
ok("im Protokoll des Mandanten: tenant.recipe.request", any(
    x[0] == "tenant.recipe.request" and x[1] == "t-kunde" for x in AUDIT), AUDIT[-2:])

a.recipe_inbox_set("posteingang", "root", "root")
ok("Posteingang benannt", a.recipe_inbox()[0] == "posteingang")
req2 = a.tenant_recipe_request("kunde", "kv", "tenant_admin", actor_tid="t-kunde",
                               text="Wir brauchen SAP SSO für A4H.")
op = POSTS[-1]
ok("mit Posteingang: ein Vorgang durch die Knotentür, object request, für die Person",
   req2["delivery"] == "sent" and req2["case"] == "V-2026-0001" and op[0] == "posteingang"
   and op[1]["object"] == "request" and op[1]["ref"] == req2["id"]
   and op[1]["from"] == "kunde · Karla V." and "SAP SSO" in op[1]["body"], (req2, op))
ok("tenant_admin von pxx fragt nicht für kunde (S2)", "own tenant" in refusal(
    a.tenant_recipe_request, "kunde", "px", "tenant_admin", actor_tid="t-pxx", text="x"))
ok("der eigene Mandant des Knotens fragt nicht", "operator's" in refusal(
    a.tenant_recipe_request, "node", "root", "server_admin", text="x"))
ok("ein Mitglied fragt nicht", "server_admin" in refusal(
    a.tenant_recipe_request, "kunde", "kv", "keyuser", actor_tid="t-kunde", text="x"))
ok("ein Geheimnis in den Werten: abgelehnt, nichts gesendet", "secret" in refusal(
    a.tenant_recipe_request, "kunde", "kv", "tenant_admin", actor_tid="t-kunde",
    text="x", values={"password": "x"}) and len(CASES) == 1)
ok("ein unbekanntes Rezept: abgelehnt", "no recipe" in refusal(
    a.tenant_recipe_request, "kunde", "kv", "tenant_admin", actor_tid="t-kunde",
    recipe="gibts-nicht"))

INBOX_UP["up"] = False
req3 = a.tenant_recipe_request("kunde", "kv", "tenant_admin", actor_tid="t-kunde",
                               text="Noch eine Frage.")
ok("Posteingang antwortet nicht: als Nachricht, mit dem Grund",
   req3["delivery"] == "message" and "nicht erreichbar" in req3["note"], req3)
INBOX_UP["up"] = True

# reading back
CASES["V-2026-0001"].update(status="answered", replies=[
    {"at": "2026-11-20T12:00:00", "by": "Max (Betreiber)", "text": "Welches System?"}])
n = a.recipe_requests_refresh("kunde", force=True)
got = rq.load(a.RECIPE_REQUEST_DIR, req2["id"])
ok("zurückgelesen: Stand beantwortet, die Rückfrage des Betreibers",
   n == 1 and got["case_status"] == "answered" and got["replies"][0]["text"] == "Welches System?",
   got)
CASES["V-2026-0001"]["replies"].append({"at": "t2", "by": "Max", "text": "Zweite Rückfrage."})
first = a.recipe_requests_refresh()
CASES["V-2026-0001"]["replies"].append({"at": "t3", "by": "Max", "text": "Dritte Rückfrage."})
second = a.recipe_requests_refresh()
ok("die Minute liest nicht öfter als alle zehn Minuten", first == 1 and second == 0,
   (first, second))

# a release answers the request for that recipe
req4 = a.tenant_recipe_request("kunde", "kv", "tenant_admin", actor_tid="t-kunde",
                               recipe="hilfe-assistent", text="Bitte.")
okr, msg = a.tenant_recipe_release("t-kunde", "recipe:hilfe-assistent", True, who="max",
                                   role="server_admin")
got = rq.load(a.RECIPE_REQUEST_DIR, req4["id"])
c4 = CASES[got["case"]]
ok("die Freigabe beantwortet die Anfrage nach diesem Rezept und schreibt in ihren Vorgang",
   okr and got["released"] and c4["notes"] and "recipe:hilfe-assistent@1" in c4["notes"][-1],
   (got, c4))
ok("eine Anfrage ohne dieses Rezept bleibt offen",
   not rq.load(a.RECIPE_REQUEST_DIR, req2["id"]).get("released"))
ok("Stand „freigegeben“ in der Liste", rq.state_text(got) == "freigegeben")

a.tenant_recipe_view_write()
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
vr = {x["id"]: x for x in view.get("requests") or []}
ok("die Sicht für das Portal: Anfragen mit Stand, Posteingang, Auswahl nur der Produktiv-Instanz",
   req2["id"] in vr and vr[req2["id"]]["state"] == "beantwortet" and view["inbox"] == "posteingang"
   and view["inbox_choices"] == ["posteingang"], view.get("inbox_choices"))
ok("…ohne die volle Vorschau (bleibt auf dem Host)", "seen" not in vr[req["id"]])

# the job
okj, msg, extra = a.tenant_recipe_job({"op": "request", "args": {
    "tenant": "kunde", "text": "Über den Auftrag."}}, "t-kunde", "tenant_admin", "kv")
ok("Auftrag request: zugestellt, mit der Kennung", okj and extra.get("request")
   and "als Vorgang" in msg, (okj, msg))
okj, msg, extra = a.tenant_recipe_job({"op": "request", "args": {
    "tenant": "kunde", "text": "x"}}, "t-pxx", "tenant_admin", "px")
ok("Köder: tenant_admin von pxx nennt kunde im Auftrag: abgelehnt", not okj, msg)
okj, msg, _e = a.tenant_recipe_job({"op": "requests", "args": {"tenant": "kunde"}},
                                   "t-pxx", "tenant_admin", "px")
ok("Stand holen für einen fremden Mandanten: abgelehnt", not okj, msg)
okj, msg, _e = a.tenant_recipe_job({"op": "inbox", "args": {"inbox": "off"}},
                                   "t-kunde", "tenant_admin", "kv")
ok("Posteingang ändern als tenant_admin: abgelehnt", not okj and a.recipe_inbox()[0] == "posteingang")
okj, msg, _e = a.tenant_recipe_job({"op": "inbox", "args": {"inbox": "off"}},
                                   "t-default", "server_admin", "max")
ok("…als Betreiber: aus", okj and a.recipe_inbox() == ("", None), msg)

print("")
print("=== der Betreiber antwortet (auch ohne Posteingang) ===")
msg_req = rq.load(a.RECIPE_REQUEST_DIR, req3["id"])          # went as a message
ok("eine Anfrage als Nachricht ist offen", msg_req["delivery"] == "message" and rq.is_open(msg_req))
why = refusal(a.tenant_recipe_request_close, req3["id"], "rejected", "", "max", "server_admin")
ok("Ablehnen ohne Satz: abgelehnt, nichts geändert", "says why" in why
   and rq.is_open(rq.load(a.RECIPE_REQUEST_DIR, req3["id"])), why)
why = refusal(a.tenant_recipe_request_close, req3["id"], "done", "x", "kv", "tenant_admin")
ok("ein tenant_admin antwortet nicht", "server_admin" in why, why)
r = a.tenant_recipe_request_close(req3["id"], "done", "Ist eingerichtet.", "max", "server_admin")
ok("Erledigt mit Antwort: nicht mehr offen, Stand „erledigt“, die Antwort unter den Antworten",
   not rq.is_open(r) and rq.state_text(r) == "erledigt"
   and r["replies"][-1]["text"] == "Ist eingerichtet."
   and r["replies"][-1]["status_label"] == "erledigt", r)
ok("…im Protokoll des Mandanten", any(x[0] == "tenant.recipe.request.close" and x[1] == "t-kunde"
                                      for x in AUDIT))
why = refusal(a.tenant_recipe_request_close, req3["id"], "rejected", "doch nicht", "max",
              "server_admin")
ok("ein zweites Mal: abgelehnt", "already answered" in why, why)
n_posts = len(POSTS)
r2 = a.tenant_recipe_request_close(req2["id"], "rejected", "Dafür gibt es kein Rezept.",
                                   "max", "server_admin")
op = POSTS[-1]
ok("eine Anfrage, die ein Vorgang wurde: der Vorgang wird mit dem Satz geschlossen",
   len(POSTS) == n_posts + 1 and op[1]["op"] == "close" and op[1]["number"] == req2["case"]
   and "Dafür gibt es kein Rezept." in op[1]["text"] and rq.state_text(r2) == "abgelehnt", op)
okj, msg, _e = a.tenant_recipe_job({"op": "close", "args": {
    "request": req4["id"], "verdict": "done", "text": ""}}, "t-kunde", "tenant_admin", "kv")
ok("Auftrag close als tenant_admin: abgelehnt", not okj, msg)
a.tenant_recipe_view_write()
view2 = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
v3 = next(x for x in view2["requests"] if x["id"] == req3["id"])
ok("die Sicht: erledigt, nicht offen", v3["state"] == "erledigt" and not v3["open"], v3)

print("")
print("=== die Seiten ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import recipe_view as rv                                      # noqa: E402
import app as portal                                          # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-recipe-req-page-")
VF = os.path.join(TMP, "recipe-view.json")
portal.RECIPE_VIEW_FILE = VF
view["inbox"] = "posteingang"
view["requests"].append({"id": "q-20261120t130000-pxx", "label": "pxx", "by": "px",
                         "by_name": "P X", "text": "Fremde Anfrage von pxx", "open": True,
                         "state": "beim Betreiber", "delivery": "sent", "case": "V-2026-0099",
                         "replies": []})
json.dump(view, open(VF, "w", encoding="utf-8"))
portal.BUILD_VIEW_FILE = os.path.join(TMP, "build-view.json")
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []
mg.CTX["queue"] = lambda rid, name, payload, wait: QUEUED.append(dict(payload, _rid=rid))
portal.load_tenants = lambda: TENANTS
WHO = {"role": "tenant_admin", "roles": {"tenant_admin"}, "tenant": "t-kunde", "host": "t-kunde"}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "kv", "X-OAAP-Roles": "tenant_admin"}


def as_(role, tenant, host):
    WHO.update(role=role, roles={role} if role else {"user"}, tenant=tenant, host=host)


t = c.get("/einrichten", headers=H).get_data(as_text=True)
ok("Einrichten: die eigenen Anfragen mit Antwort, keine fremde, das Formular mit allen Rezepten",
   "Anfragen an den Betreiber" in t and "Welches System?" in t
   and "Fremde Anfrage von pxx" not in t and 'action="/einrichten/anfrage"' in t
   and 'value="hilfe-assistent"' in t and "als Vorgang in seinen Posteingang" in t, t[-2500:])
r = c.post("/einrichten/anfrage", headers=H, data={"recipe": "", "text": ""})
ok("leer: nichts im Spool, mit Hinweis", not QUEUED and "err=" in r.headers["Location"])
r = c.post("/einrichten/anfrage", headers=H, data={"recipe": "gibts-nicht", "text": "x"})
ok("ein unbekanntes Rezept: nichts im Spool", not QUEUED)
r = c.post("/einrichten/anfrage", headers=dict(H, Origin="http://boese.example"),
           data={"text": "x"})
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and not QUEUED)
r = c.post("/einrichten/anfrage", headers=H,
           data={"recipe": "hilfe-assistent", "text": "Bitte freigeben.", "tenant": "pxx",
                 "v_model": "chat-default", "boese": "x"})
q = QUEUED[-1] if QUEUED else {}
ok("Anfrage: ein Auftrag request mit dem eigenen Kürzel, nur v_-Werte",
   q.get("action") == "tenant-recipe" and q.get("op") == "request"
   and q["args"] == {"recipe": "hilfe-assistent", "tenant": "kunde",
                     "params": {"model": "chat-default"}, "text": "Bitte freigeben."}, q)
r = c.post("/einrichten/anfragen/stand", headers=H)
ok("Stand holen: ein Auftrag requests für den eigenen Mandanten",
   QUEUED[-1]["op"] == "requests" and QUEUED[-1]["args"] == {"tenant": "kunde"}, QUEUED[-1])
as_("tenant_admin", "t-pxx", "t-kunde")
n = len(QUEUED)
r = c.post("/einrichten/anfrage", headers=H, data={"text": "x"})
ok("tenant_admin von pxx am Ort von kunde: 403", r.status_code == 403 and len(QUEUED) == n)

as_("server_admin", "t-default", None)
t = c.get("/aufbau", headers=H).get_data(as_text=True)
ok("Aufbau: der Posteingang zur Wahl, die offenen Anfragen aller Mandanten",
   "Anfragen der Mandanten" in t and 'value="posteingang" selected' in t
   and "Fremde Anfrage von pxx" in t, t[-2500:])
ok("je offene Anfrage: Antwort, „Erledigt“, „Ablehnen“",
   'action="/aufbau/rezepte/anfrage"' in t and 'value="q-20261120t130000-pxx"' in t
   and "Ablehnen" in t)
n = len(QUEUED)
r = c.post("/aufbau/rezepte/anfrage", headers=H,
           data={"request": "q-20261120t130000-pxx", "verdict": "rejected", "text": ""})
ok("Ablehnen ohne Satz: nichts im Spool", len(QUEUED) == n and "err=" in r.headers["Location"])
r = c.post("/aufbau/rezepte/anfrage", headers=H,
           data={"request": "q-gibts-nicht", "verdict": "done", "text": "x"})
ok("eine Anfrage, die nicht offen ist: nichts im Spool", len(QUEUED) == n)
r = c.post("/aufbau/rezepte/anfrage", headers=H,
           data={"request": "q-20261120t130000-pxx", "verdict": "done", "text": "Erledigt."})
ok("Erledigt: ein Auftrag close", QUEUED[-1]["op"] == "close" and QUEUED[-1]["args"] == {
    "request": "q-20261120t130000-pxx", "verdict": "done", "text": "Erledigt."}, QUEUED[-1])
n = len(QUEUED)
r = c.post("/aufbau/rezepte/posteingang", headers=H, data={"inbox": "kunde-vorgaenge"})
ok("ein Posteingang, der nicht zur Wahl steht: nichts im Spool", len(QUEUED) == n
   and "err=" in r.headers["Location"])
r = c.post("/aufbau/rezepte/posteingang", headers=H, data={"inbox": "off"})
ok("Posteingang aus: ein Auftrag inbox", QUEUED[-1]["op"] == "inbox"
   and QUEUED[-1]["args"] == {"inbox": "off"}, QUEUED[-1])
as_("tenant_admin", "t-kunde", None)
n = len(QUEUED)
r = c.post("/aufbau/rezepte/posteingang", headers=H, data={"inbox": "off"})
ok("ein tenant_admin ändert den Posteingang nicht: 403", r.status_code == 403
   and len(QUEUED) == n)
r = c.post("/aufbau/rezepte/anfrage", headers=H,
           data={"request": "q-20261120t130000-pxx", "verdict": "done", "text": "x"})
ok("…und beantwortet keine Anfrage: 403", r.status_code == 403 and len(QUEUED) == n)

print("")
print("PASS: alles" if not fails else f"FAIL: {fails}")
sys.exit(1 if fails else 0)
