#!/usr/bin/env python3
"""Einladungen und Antraege vor dem Mandanten-Aufbau (RFC-0055 Stufe 4).

Gemessen wird, was RFC-0055 §13 verspricht:

  * der Einladungslink wird NIE gespeichert, nur sein SHA-256; wer die Dateien
    des Knotens liest, kann keinen Antrag stellen (auch nicht aus der Sichtdatei);
  * eine Einladung ist einmal benutzbar, laeuft ab, ist widerrufbar, und an ein
    Profil gebunden -- das Formular kann kein anderes waehlen;
  * der Interessent baut nichts: sein Absenden erzeugt einen Antrag, sonst nichts;
    erst die Freigabe eines server_admin startet den Aufbau, mit DESSEN Rolle;
  * Koeder an der Tuer des Workers: kein Benutzer, falsche Rolle, falscher Link,
    fremder Parameter, vergebenes Kuerzel, zweites Absenden, doppelte Freigabe;
  * die Kontakt-Adresse verschwindet bei Ablehnung sofort, sonst nach 30 Tagen.

Der Worker ist echt (echter Mandantenspeicher, Spool, Benutzerspeicher im
Wegwerfverzeichnis); Docker und Gateway sind ersetzt.
Aufruf: python3 test/test_tenant_request.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-request-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as a                                             # noqa: E402
import tenant_build as tb                                      # noqa: E402
import tenant_request as tr                                    # noqa: E402
import management_api as mg                                    # noqa: E402
from flask import Flask                                        # noqa: E402

a.reload_gateway = lambda: None
a.zone_probe = lambda label: ""
a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

# ------------------------------------------------------------ the pure core
print("=== der reine Kern ===")
R = tempfile.mkdtemp(prefix="oaap-request-core-")
T1 = tr.new_token()
inv = tr.create_invite(R, T1, "club", "Handballverein", 14, "betreiber", NOW)
raw_files = "".join(open(os.path.join(R, n), encoding="utf-8").read()
                    for n in os.listdir(R))
ok("gespeichert wird der Hash, nie der Link", T1 not in raw_files
   and tr.hash_token(T1) in raw_files, raw_files[:300])
ok("die Datei ist nur fuer den Besitzer lesbar",
   (os.stat(os.path.join(R, inv["id"] + ".json")).st_mode & 0o077) == 0
   or os.name == "nt")
ok("ein Link wird zur offenen Einladung gefunden, ein erfundener nicht",
   tr.find_by_token(R, T1, NOW)["id"] == inv["id"]
   and tr.find_by_token(R, tr.new_token(), NOW) is None
   and tr.find_by_token(R, "kurz", NOW) is None
   and tr.find_by_token(R, "../" * 12, NOW) is None)
ok("nach Ablauf gilt der Link nicht mehr",
   tr.find_by_token(R, T1, NOW + timedelta(days=15)) is None
   and tr.invite_state(inv, NOW + timedelta(days=15)) == "expired")
for bad in ("kurz", "a b" * 20, "x" * 80, ""):
    try:
        tr.create_invite(R, bad, "club", "", 14, "b", NOW)
        refused = False
    except tr.Refusal:
        refused = True
    if not refused:
        break
ok("zu kurze oder unzulaessige Links werden nicht gespeichert", refused)
for days in (0, 61, "x"):
    try:
        tr.create_invite(R, tr.new_token(), "club", "", days, "b", NOW)
        refused = False
    except tr.Refusal:
        refused = True
    if not refused:
        break
ok("Gueltigkeit nur 1 bis 60 Tage", refused)

v = {"label": "vneu", "title": "Verein Neu"}
req = tr.submit(R, T1, v, "vorstand@verein.example", NOW)
ok("Absenden macht aus der Einladung einen Antrag und verbraucht sie",
   req["state"] == "pending" and req["profile"] == "club"
   and tr.get(R, inv["id"])["state"] == "used"
   and tr.get(R, inv["id"])["request"] == req["id"])
try:
    tr.submit(R, T1, {"label": "vzwei", "title": "Z"}, "x@y.example", NOW)
    again = True
except tr.Refusal:
    again = False
ok("derselbe Link ein zweites Mal: abgelehnt, es bleibt bei einem Antrag",
   not again and len(tr.requests(R)) == 1)
for mail in ("keine-adresse", "a@b", "a b@c.de", "<x>@y.de", "a@b.de,c@d.de", "",
             "x" * 200 + "@y.de"):
    T = tr.new_token()
    tr.create_invite(R, T, "club", "", 14, "b", NOW)
    try:
        tr.submit(R, T, {"label": "v" + str(abs(hash(mail)))[:5], "title": "T"},
                  mail, NOW)
        accepted = True
    except tr.Refusal:
        accepted = False
    if accepted:
        break
ok("eine Kontakt-Adresse, die keine ist, ergibt keinen Antrag und verbraucht den Link nicht",
   not accepted and tr.find_by_token(R, T, NOW) is not None)
T2 = tr.new_token()
inv2 = tr.create_invite(R, T2, "club", "", 14, "b", NOW)
try:
    tr.submit(R, T2, {"label": "vneu", "title": "Z"}, "z@y.example", NOW)
    dup = True
except tr.Refusal:
    dup = False
ok("zwei offene Antraege fuer dasselbe Kuerzel gibt es nicht", not dup)
rv = tr.revoke_invite(R, inv2["id"], NOW)
ok("widerrufen: der Link gilt danach nicht mehr",
   rv["state"] == "revoked" and tr.find_by_token(R, T2, NOW) is None)
try:
    tr.revoke_invite(R, inv["id"], NOW)
    rr = True
except tr.Refusal:
    rr = False
ok("eine benutzte Einladung ist nicht zu widerrufen", not rr)

vw = tr.view(R, NOW)
vw_text = json.dumps(vw)
ok("die Sichtdatei kennt weder Link noch Hash der Einladungen -- ausser dem Nachschlag-Hash der offenen",
   T1 not in vw_text and T2 not in vw_text
   and all(len(h) == 64 for h in vw["live"]) and "hash" not in
   json.dumps(vw["invites"]))
rj = tr.reject(R, req["id"], "betreiber", "passt nicht", NOW)
ok("Ablehnen loescht die Kontakt-Adresse sofort und nennt den Grund",
   rj["contact"] == "" and rj["reason"] == "passt nicht"
   and "vorstand@verein.example" not in open(os.path.join(R, req["id"] + ".json"),
                                              encoding="utf-8").read())
try:
    tr.reject(R, req["id"], "b", "", NOW)
    twice = True
except tr.Refusal:
    twice = False
ok("ein entschiedener Antrag ist nicht noch einmal zu entscheiden", not twice)
gone = tr.prune(R, NOW + timedelta(days=31))
ok("nach 30 Tagen verschwindet das Entschiedene, nach 7 die abgelaufene Einladung",
   req["id"] in gone and not [r for r in tr.requests(R)], gone)

# ------------------------------------------------------- the door: the worker
print("=== die Tuer: der Worker ===")
default_id = a.ensure_default_tenant()
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "betreiber", "roles": ["server_admin"], "active": True},
               {"username": "kunde-verwalter", "roles": ["tenant_admin"],
                "active": True, "tenant": default_id},
               {"username": "mitglied", "roles": ["user"], "active": True}], f)
mg.SPOOL_DIR = a.SPOOL_DIR
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(a.SPOOL_DIR, d), exist_ok=True)
os.makedirs(a.PROFILE_DIR, exist_ok=True)


def club(pid="club"):
    return {"profile": tb.PROFILE_FORMAT, "id": pid, "title": "Club",
            "params": {"label": {"kind": "label", "required": True},
                       "title": {"kind": "text", "required": True, "max": 60},
                       "color": {"kind": "color", "default": None}},
            "steps": [
                {"id": "tenant", "type": "tenant.create", "label": "{label}",
                 "title": "{title}"},
                {"id": "admin", "type": "manual", "text": "Make the admin.",
                 "done_when": "confirmed"}]}


def write_profile(pid, doc):
    with open(os.path.join(a.PROFILE_DIR, pid + ".json"), "w", encoding="utf-8") as f:
        json.dump(doc, f)


write_profile("club", club())
bad = club("evil")
bad["steps"].append({"id": "x", "type": "shell.run"})
write_profile("evil", bad)

APP = Flask(__name__)
WHO = {"name": ""}


def queue(rid, name, payload, wait):
    with open(os.path.join(a.SPOOL_DIR, "queue", rid + ".json"), "w",
              encoding="utf-8") as f:
        json.dump({"id": rid, "instance": name, "by": WHO["name"],
                   "requested": "now", **payload}, f)


mg.init(APP, lambda: WHO["name"], lambda: set(), lambda: ("", None),
        lambda host: None, queue)


def work():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.cmd_process_deploys(None)
    return buf.getvalue()


def result(rid):
    try:
        with open(os.path.join(a.SPOOL_DIR, "jobs", rid, "result.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def ask(who, op, args, action="tenant-request"):
    WHO["name"] = who
    rid = mg.enqueue("", "", op, args, action=action)
    work()
    return result(rid)


def reqs():
    return tr.requests(a.REQUEST_DIR)


def invs():
    return tr.invites(a.REQUEST_DIR)


def invite(who="betreiber", profile="club", token=None):
    token = token or tr.new_token()
    r = ask(who, "invite", {"profile": profile, "token": token,
                            "note": "Handballverein", "days": 14})
    return token, r


def submit(token, params, contact="vorstand@verein.example"):
    return ask("", "submit", {"token": token, "params": params, "contact": contact},
               action="tenant-request-submit")


tok, r = invite()
ok("der Betreiber stellt eine Einladung aus", r.get("ok") and len(invs()) == 1, r)
ok("der Link steht nirgends auf dem Knoten -- weder Speicher noch Spool noch Sichtdatei",
   not any(tok in open(os.path.join(root, n), encoding="utf-8", errors="ignore").read()
           for root, _d, files in os.walk(DATA) for n in files), "")
for who, label in (("kunde-verwalter", "ein Mandanten-Verwalter"),
                   ("mitglied", "ein Mitglied"), ("", "niemand"),
                   ("erfunden", "ein Benutzer, den es nicht gibt")):
    before = len(invs())
    t, r = invite(who)
    ok(f"{label} kann nicht einladen", not r.get("ok") and len(invs()) == before, r)
t, r = invite(profile="evil")
ok("ein Profil, das der Knoten ablehnt, bekommt keine Einladung",
   not r.get("ok") and len(invs()) == 1, r)
t, r = invite(profile="../club")
ok("ein Profilname mit Pfad bekommt keine Einladung", not r.get("ok") and len(invs()) == 1, r)
t, r = invite(token="kurz")
ok("ein zu kurzer Link wird nicht angenommen", not r.get("ok") and len(invs()) == 1, r)

# --- the prospect
r = submit(tr.new_token(), {"label": "vneu", "title": "Verein Neu"})
ok("ein erfundener Link ergibt keinen Antrag", not r.get("ok") and not reqs(), r)
r = submit(tok, {"label": "vneu", "title": "Verein Neu", "shell": "rm -rf /"})
ok("ein Parameter, den das Profil nicht kennt, ergibt keinen Antrag und laesst den Link offen",
   not r.get("ok") and not reqs() and tr.find_by_token(a.REQUEST_DIR, tok,
   datetime.now(timezone.utc)) is not None, r)
r = submit(tok, {"label": "Ver ein!", "title": "x"})
ok("ein Kuerzel in falscher Form ergibt keinen Antrag", not r.get("ok") and not reqs(), r)
r = submit(tok, {"label": a.DEFAULT_TENANT_LABEL, "title": "x"})
ok("das Kuerzel des Knotens selbst ist nicht zu beantragen", not r.get("ok") and not reqs(), r)
r = submit(tok, {"label": "vneu", "title": "x" * 200})
ok("ein zu langer Titel ergibt keinen Antrag", not r.get("ok") and not reqs(), r)
r = submit(tok, {"label": "vneu", "title": "Verein Neu"}, contact="keine-adresse")
ok("eine Kontakt-Adresse, die keine ist, ergibt keinen Antrag", not r.get("ok") and not reqs(), r)
r = submit(tok, "nur ein Text")
ok("Parameter, die kein Objekt sind, ergeben keinen Antrag", not r.get("ok") and not reqs(), r)
r = submit(tok, {"label": "vneu", "title": "Verein Neu"})
ok("das richtige Formular ergibt EINEN Antrag, wartend", r.get("ok") and len(reqs()) == 1
   and reqs()[0]["state"] == "pending" and reqs()[0]["profile"] == "club", r)
ok("...und es wurde nichts gebaut: kein Mandant, kein Aufbau",
   not a.tenant_by_label("vneu", include_former=False)[1]
   and not tb.list_states(a.BUILD_DIR))
r = submit(tok, {"label": "vzwei", "title": "Zwei"})
ok("derselbe Link ein zweites Mal ergibt keinen zweiten Antrag", not r.get("ok") and len(reqs()) == 1, r)
rid_req = reqs()[0]["id"]

# --- the operator decides
before = (len(reqs()), len(tb.list_states(a.BUILD_DIR)))
for who, label in (("kunde-verwalter", "ein Mandanten-Verwalter"),
                   ("mitglied", "ein Mitglied"), ("", "niemand")):
    r = ask(who, "approve", {"id": rid_req})
    ok(f"{label} kann nicht freigeben: nichts gebaut, der Antrag wartet",
       not r.get("ok") and not tb.list_states(a.BUILD_DIR)
       and reqs()[0]["state"] == "pending", r)
    r = ask(who, "reject", {"id": rid_req})
    ok(f"{label} kann nicht ablehnen", not r.get("ok") and reqs()[0]["state"] == "pending", r)
r = ask("betreiber", "approve", {"id": "req-000000000000"})
ok("einen Antrag, den es nicht gibt, kann man nicht freigeben", not r.get("ok"), r)
r = ask("betreiber", "approve", {"id": "../../etc/passwd"})
ok("eine Kennung mit Pfad wird nicht gelesen", not r.get("ok"), r)
r = ask("betreiber", "approve", {"id": rid_req})
states = tb.list_states(a.BUILD_DIR)
ok("der Betreiber gibt frei: der Aufbau startet und haelt am Menschenschritt",
   r.get("ok") and r.get("build", "").startswith("b-") and len(states) == 1
   and states[0]["state"] == "waiting" and states[0]["label"] == "vneu", (r, states))
ok("der Aufbau steht auf den Namen des Betreibers, nicht des Interessenten",
   states[0].get("by") == "betreiber", states[0].get("by"))
ok("...und der Mandant ist angelegt, mit dem Klarnamen des Antrags",
   bool(a.tenant_by_label("vneu", include_former=False)[1]))
ok("der Antrag merkt sich den Aufbau; die Adresse bleibt fuer den, der den Verwalter einrichtet",
   reqs()[0]["state"] == "approved" and reqs()[0]["build"] == states[0]["id"]
   and reqs()[0]["contact"] == "vorstand@verein.example")
ok("die Kontakt-Adresse ist KEIN Parameter des Aufbaus und steht nicht im Zustand",
   "verein.example" not in json.dumps(states[0]))
r = ask("betreiber", "approve", {"id": rid_req})
ok("doppelt freigeben: abgelehnt, kein zweiter Aufbau",
   not r.get("ok") and len(tb.list_states(a.BUILD_DIR)) == 1, r)
r = ask("betreiber", "reject", {"id": rid_req})
ok("einen freigegebenen Antrag kann man nicht mehr ablehnen", not r.get("ok"), r)

# a label that became taken between request and approval
tok2, _ = invite()
submit(tok2, {"label": "vdrei", "title": "Drei"})
mid = [x for x in reqs() if x["label"] == "vdrei"][0]["id"]
# another build for the same label is started by hand in the meantime
st_taken = tb.new_state(club(), "x" * 64, {"label": "vdrei", "title": "Drei"}, "betreiber")
tb.save_state(a.BUILD_DIR, st_taken)
r = ask("betreiber", "approve", {"id": mid})
ok("ist das Kuerzel inzwischen belegt (offener Aufbau), bleibt der Antrag wartend, mit Grund",
   not r.get("ok") and [x for x in reqs() if x["id"] == mid][0]["state"] == "pending", r)
r = ask("betreiber", "reject", {"id": mid, "reason": "schon vergeben"})
ok("Ablehnen: Adresse sofort weg, Grund da",
   r.get("ok") and [x for x in reqs() if x["id"] == mid][0]["contact"] == ""
   and [x for x in reqs() if x["id"] == mid][0]["reason"] == "schon vergeben", r)

# revoke
tok3, _ = invite()
iid = [i for i in invs() if i["state"] == "open"][0]["id"]
for who in ("kunde-verwalter", ""):
    r = ask(who, "revoke", {"id": iid})
    ok(f"widerrufen: {who or 'niemand'} darf nicht", not r.get("ok")
       and [i for i in invs() if i["id"] == iid][0]["state"] == "open", r)
r = ask("betreiber", "revoke", {"id": iid})
r2 = submit(tok3, {"label": "vvier", "title": "Vier"})
ok("der Betreiber widerruft; mit dem Link geht danach nichts mehr",
   r.get("ok") and not r2.get("ok") and not [x for x in reqs() if x["label"] == "vvier"], (r, r2))

# the view
vw = json.load(open(a.REQUEST_VIEW, encoding="utf-8"))
vtext = open(a.REQUEST_VIEW, encoding="utf-8").read()
ok("die Sichtdatei ist da, lesbar fuer das Portal, ohne einen einzigen Link",
   all(t_ not in vtext for t_ in (tok, tok2, tok3)) and os.stat(a.REQUEST_VIEW).st_mode & 0o004
   and vw["requests"] and vw["invites"])
ok("...und die abgelehnten Antraege tragen keine Adresse darin",
   all(not x["contact"] for x in vw["requests"] if x["state"] == "rejected"))
ok("die Sichtdatei der Aufbauten kennt den neuen Aufbau",
   any(b["label"] == "vneu" for b in json.load(open(a.BUILD_VIEW))["builds"]))

print()
print("FAILURES" if fails else "ALL PASS", fails)
sys.exit(1 if fails else 0)
