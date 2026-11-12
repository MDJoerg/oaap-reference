#!/usr/bin/env python3
"""Verteiler und Benachrichtigungen (RFC-0080, oaap.core.notifications 0.1).

    Die Regeln    ein Verteiler braucht einen An-Eintrag, Adressen oder
                  Rollen, höchstens 25; Betreffvorlagen kennen nur ihre
                  Platzhalter; der Typ schlägt die Kategorie, Ungeroutetes
                  landet im Pflicht-Verteiler.
    Die Lagen     beginnen -> eine Mail, bestehen -> nichts (kritisch:
                  täglich), enden -> „Behoben“.
    Der Weg       Audit-Zeile -> Ausgangskorb -> Minutenaufgabe -> Mail;
                  Bcc steht in keiner Kopfzeile; ein Mandant ohne Adresse
                  fällt auf die Serverbesitzer zurück; die Stundengrenze;
                  ein Mailserver, der nicht annimmt -> erneut, nie eine
                  Mail über die gescheiterte Mail.
    Stufe 2       Produktiv-Instanz > 5 min aus -> Lage; „unbekannt“ ist
                  kein „Behoben“; Identity sperrt einen Namen -> eine Mail
                  je Mandant, einmal je Stunde.
    Die Tür       Mandanten-Admin nur im eigenen Mandanten, nie am Knoten.

Braucht kein Docker, keinen Knoten, keinen Mailserver.
Aufruf: python3 test/test_notify.py
"""
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-notify-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import notify as n  # noqa: E402
import appctl as a  # noqa: E402

a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


print("=== die Regeln ===")
E = lambda f, x: {"channel": "mail", "to": f, **({"role": x[6:]} if x.startswith("rolle:")
                                                   else {"address": x})}
ok("ohne An-Eintrag abgelehnt",
   "To" in n.list_refusal("X", {"entries": [E("cc", "a@b.de")]}, True))
ok("keine Einträge abgelehnt", n.list_refusal("X", {"entries": []}, True))
ok("Unsinn als Adresse abgelehnt",
   "not an e-mail" in n.list_refusal("X", {"entries": [E("to", "kein-at")]}, True))
ok("ein Mandantenverteiler nennt nur die Rolle tenant_admin",
   "tenant_admin" in n.list_refusal("X", {"entries": [E("to", "rolle:server_admin")]}, False))
ok("26 Adressen abgelehnt",
   "25" in n.list_refusal("X", {"entries": [E("to", f"p{i}@b.de") for i in range(26)]}, True))
ok("doppelte Adresse abgelehnt",
   "twice" in n.list_refusal("X", {"entries": [E("to", "a@b.de"), E("cc", "A@b.de")]}, True))
ok("ein Telegram-Eintrag ist noch nicht gebaut",
   "not built" in n.list_refusal("X", {"entries": [{"channel": "telegram", "to": "to",
                                                     "address": "a@b.de"}]}, True))
ok("ein gültiger Verteiler", n.list_refusal("Servicepartner", {"entries": [
    E("to", "service@partner.de"), E("bcc", "rolle:server_admin")]}, True) == "")
ok("unbekannter Platzhalter abgelehnt", "{wer}" in n.template_refusal("[{wer}] x"))
ok("bekannte Platzhalter gehen", n.template_refusal("[{mandant}] Backup ({knoten})") == "")
ok("lose Klammer abgelehnt", n.template_refusal("a { b") != "")

sc = n.scope_of({"lists": {"Security": {"entries": [E("to", "sec@x.de")]},
                           "Service": {"entries": [E("to", "svc@x.de")]}},
                 "routes": {"security": {"lists": ["Security"], "subject": "SEC {typ}"},
                            "advisory.host": {"lists": ["Service"]}}}, True)
ok("der Pflicht-Verteiler entsteht von selbst", n.NODE_MANDATORY in sc["lists"])
ok("der Typ schlägt die Kategorie (Liste), der Betreff der Kategorie bleibt",
   n.route_for(sc, "advisory.host", True) == (["Service"], "SEC {typ}"))
ok("Ungeroutetes landet beim Serverbesitzer",
   n.route_for(sc, "disk.full", True)[0] == [n.NODE_MANDATORY])
ok("ein Mandantenereignis hört der Knoten nur mit eigener Typzeile",
   n.route_for(sc, "rebuild.failed", True, from_tenant=True)[0] == [])

sc2 = n.scope_of({"lists": {"A": {"entries": [E("bcc", "x@y.de"), E("to", "rolle:server_admin")]},
                            "B": {"entries": [E("to", "X@y.de"), E("cc", "z@y.de")]}}}, True)
r = n.resolve(["A", "B"], sc2, lambda role: ["chef@y.de"] if role == "server_admin" else [])
ok("jede Adresse einmal, im stärksten Feld (An vor Bcc)",
   r == {"to": ["x@y.de", "chef@y.de"], "cc": ["z@y.de"], "bcc": []}, r)

t0 = datetime.datetime(2026, 11, 11, 20, 0, tzinfo=datetime.timezone.utc)
cur = {"backup.failed": {"type": "backup.failed", "text": "x"}}
ops, st = n.matter_ops(cur, {}, t0)
ok("eine Lage beginnt -> open", [o[0] for o in ops] == ["open"])
ops, st = n.matter_ops(cur, st, t0 + datetime.timedelta(hours=3))
ok("sie besteht -> nichts", ops == [])
ops, st = n.matter_ops(cur, st, t0 + datetime.timedelta(hours=24))
ok("kritisch nach 24 h -> remind", [o[0] for o in ops] == ["remind"])
dcur = {"disk.full": {"type": "disk.full", "text": "x"}}
_o, dst = n.matter_ops(dcur, {}, t0)
ops, _ = n.matter_ops(dcur, dst, t0 + datetime.timedelta(hours=30))
ok("Gesundheit erinnert nicht täglich", ops == [])
ops, st = n.matter_ops({}, st, t0 + datetime.timedelta(hours=25))
ok("sie endet -> close", [o[0] for o in ops] == ["close"] and st == {})
allowed, kept = n.within_limit([(t0 - datetime.timedelta(minutes=m)).isoformat()
                                for m in range(20)], t0, 20)
ok("die 21. Mail in einer Stunde nicht", not allowed and len(kept) == 20)
allowed, kept = n.within_limit([(t0 - datetime.timedelta(minutes=61)).isoformat()], t0, 20)
ok("eine Stunde alte Stempel zählen nicht mehr", allowed and kept == [])

print("=== der Weg ===")
dflt = a.ensure_default_tenant()
tid, _ = a.tenant_create("verein", "Handballverein X")
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([
        {"username": "joerg", "roles": ["server_admin"], "active": True,
         "tenant": dflt, "email": "joerg@node.example"},
        {"username": "kassierer", "roles": ["tenant_admin"], "active": True,
         "tenant": tid, "email": "kasse@verein.example"},
        {"username": "weg", "roles": ["tenant_admin"], "active": False,
         "tenant": tid, "email": "weg@verein.example"},
    ], f)
a.mail_set("smtps://mail.example.org:465", "noreply@example.org", "", "", "")
SENT = []
FAIL = {"on": False}


def fake(mail, sender, rcpts, subject, body):
    if FAIL["on"]:
        return False, "SMTPServerDisconnected: gone"
    SENT.append({"sender": sender, "rcpts": rcpts, "subject": subject, "body": body})
    return True, f"accepted for {sum(len(v) for v in rcpts.values())}"


a.NOTIFY_SEND = fake
a.load_external = lambda: "oaap.example"

ok("Rolle Server-Admin löst auf die aktive Person auf",
   a.notify_people_of_role(dflt)("server_admin") == ["joerg@node.example"])
ok("Rolle Mandanten-Admin: nur aktive Personen dieses Mandanten",
   a.notify_people_of_role(tid)("tenant_admin") == ["kasse@verein.example"])

a.audit_tenant("instance.rebuild", tid, "kasse", "failed", detail="exit 1")
ok("eine Audit-Zeile legt eine Benachrichtigung in den Ausgangskorb",
   len(os.listdir(a.NOTIFY_OUTBOX_DIR)) == 1)
a.audit_tenant("instance.rebuild", tid, "kasse", "ok")
ok("ein gelungener Neubau legt nichts ab", len(os.listdir(a.NOTIFY_OUTBOX_DIR)) == 1)
res = a.notify_deliver()
ok("zugestellt: an den Mandanten, der Knoten hört nicht mit (keine Typzeile)",
   res == {"sent": 1, "skipped": 1} and len(SENT) == 1, (res, SENT))
m = SENT[-1]
ok("an die Mandanten-Admins, Absendername = Mandantentitel, Betreff aus dem Katalog",
   m["rcpts"]["to"] == ["kasse@verein.example"] and m["sender"] == "Handballverein X"
   and m["subject"] == "[Handballverein X] Neubau fehlgeschlagen: kasse", m)
ok("der Text nennt Instanz, Grund und den Weg ins Portal",
   "Instanz: kasse" in m["body"] and "exit 1" in m["body"]
   and "https://verein.oaap.example/store" in m["body"], m["body"])
ok("der Korb ist leer", os.listdir(a.NOTIFY_OUTBOX_DIR) == [])

a.notify_list_set(tid, "Backup-Verantwortliche", a.notify_parse_entries(
    "vorstand@verein.example", "Mandanten-Admins", "archiv@verein.example"), "[Backup]")
a.notify_route_set(tid, "backup.failed", ["Backup-Verantwortliche"],
                   "{mandant}: Backup fehlgeschlagen ({knoten})")
a.notify_list_set(dflt, "Servicepartner", a.notify_parse_entries("service@partner.example"))
a.notify_route_set(dflt, "health", ["Servicepartner"])
with open(a.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "failed", "finished": "2026-11-11T03:12:00Z",
               "message": "tar exit 2: No space left on device"}, f)
a.metrics.read_disk_percent = lambda p: 96.4
SENT.clear()
a.notify_minute({"advisories": {"open": 0}})
by = {s["subject"]: s for s in SENT}
v = by.get("[Backup] Handballverein X: Backup fehlgeschlagen (oaap.example)")
ok("Backup fehlgeschlagen beim Mandanten: An/Cc/Bcc und eigener Betreff mit Präfix",
   v and v["rcpts"] == {"to": ["vorstand@verein.example"], "cc": ["kasse@verein.example"],
                         "bcc": ["archiv@verein.example"]}, list(by))
ok("… und beim Knoten an die Serverbesitzer (Katalogbetreff)",
   by.get("[oaap.example] Backup fehlgeschlagen", {}).get("rcpts", {}).get("to")
   == ["joerg@node.example"], list(by))
dk = [s for s in SENT if "Platte" in s["subject"]]
ok("Platte zu 96 % an den Servicepartner",
   dk and dk[0]["rcpts"]["to"] == ["service@partner.example"]
   and "zu 96 % voll" in dk[0]["subject"], [s["subject"] for s in SENT])
ok("der Grund des Backups steht im Text", v and "No space left" in v["body"])
ok("der Mandant bekommt seine Startseite, der Knoten die Gesundheitsseite",
   v and "https://verein.oaap.example/\n" in v["body"]
   and "https://oaap.example/health" in by["[oaap.example] Backup fehlgeschlagen"]["body"],
   v and v["body"])
SENT.clear()
a.notify_minute({"advisories": {"open": 0}})
ok("die nächste Minute: nichts Neues", SENT == [], [s["subject"] for s in SENT])

with open(a.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "ok", "finished": "2026-11-11T21:00:00Z"}, f)
a.metrics.read_disk_percent = lambda p: 89.0
a.notify_minute({"advisories": {"open": 0}})
ok("89 % hält die Lage noch offen (zwei Punkte Spiel)",
   not any("Platte" in s["subject"] for s in SENT), [s["subject"] for s in SENT])
ok("Backup wieder gut -> „Behoben“ an beide",
   sorted(s["subject"] for s in SENT if s["subject"].startswith(("Behoben", "[Backup] Behoben")))
   == ["Behoben: [oaap.example] Backup fehlgeschlagen",
       "[Backup] Behoben: Handballverein X: Backup fehlgeschlagen (oaap.example)"],
   [s["subject"] for s in SENT])
a.metrics.read_disk_percent = lambda p: 80.0
SENT.clear()
a.notify_minute({"advisories": {"open": 0}})
ok("80 % -> Platte behoben", [s["subject"] for s in SENT] == ["Behoben: [oaap.example] Platte zu 96 % voll"],
   [s["subject"] for s in SENT])

print("=== Rückfall, Grenze, Ausfall ===")
a.notify_list_set(tid, "Mandantenverwaltung", a.notify_parse_entries("Mandanten-Admins"))
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "roles": ["server_admin"], "active": True,
                "tenant": dflt, "email": "joerg@node.example"}], f)
SENT.clear()
a.notify_event("bulk.halted", tid, kurz="Alle neu bauen", text="kasse: exit 1")
a.notify_deliver()
ok("niemand mit Adresse beim Mandanten -> an die Serverbesitzer, mit Hinweis",
   len(SENT) == 1 and SENT[0]["rcpts"]["to"] == ["joerg@node.example"]
   and "niemand mit E-Mail-Adresse" in SENT[0]["body"], SENT)
SENT.clear()
a.notify_event("reboot.planned", tid, kurz="03:30")
a.notify_deliver()
ok("… aber nicht für eine Meldung, die der Knoten selbst hört (Neustart, Backup)",
   SENT == [] and a.notify_log_read(tid, 1)[-1]["result"] == "nobody", SENT)

SENT.clear()
for i in range(22):
    a.notify_event("rebuild.failed", dflt, instanz=f"i{i}")
res = a.notify_deliver()
ok("der Knoten darf 60 je Stunde: 22 gehen", res.get("sent") == 22, res)
st = a._notify_state_rw()
st["stamps"]["sent:" + tid] = [n.now_utc().isoformat()] * 20
a._notify_state_rw(lambda s: st)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "kassierer", "roles": ["tenant_admin"], "active": True,
                "tenant": tid, "email": "kasse@verein.example"}], f)
SENT.clear()
a.notify_event("rebuild.failed", tid, instanz="x")
res = a.notify_deliver()
ok("der Mandant nach 20 in der Stunde: nicht verschickt, im Protokoll „limited“",
   res.get("limited") == 1 and SENT == []
   and a.notify_log_read(tid, 1)[-1]["result"] == "limited", res)
a._notify_state_rw(lambda s: dict(s, stamps={}))

FAIL["on"] = True
a.notify_event("rebuild.failed", tid, instanz="y")
res = a.notify_deliver()
ok("Mailserver nimmt nicht an -> bleibt im Korb", res.get("retry") == 1
   and len(os.listdir(a.NOTIFY_OUTBOX_DIR)) == 1, res)
later = n.now_utc() + datetime.timedelta(hours=2)
res = a.notify_deliver(now=later)
ok("nach einer Stunde: gescheitert vermerkt, Korb leer, keine Mail darüber",
   os.listdir(a.NOTIFY_OUTBOX_DIR) == [] and a._notify_state_rw().get("last_failure")
   and a.notify_log_read(tid, 1)[-1]["result"] == "failed", res)
FAIL["on"] = False

print("=== Befunde der Messung auf oaap-test (11.11.) ===")
LINES = len(a.notify_log_read(None, 100000))
a.NOTIFY_SEND = lambda *x: (False, "every recipient refused: 521 Domain does not exist", True)
a.notify_event("rebuild.failed", tid, instanz="z")
res = a.notify_deliver()
ok("5xx ist endgültig: nicht wieder versucht, Korb leer",
   res == {"failed": 1, "skipped": 1} and os.listdir(a.NOTIFY_OUTBOX_DIR) == [], res)
a.NOTIFY_SEND = lambda *x: (False, "SMTPServerDisconnected", False)
a.notify_event("rebuild.failed", tid, instanz="z2")
for i in range(5):
    a.notify_deliver(now=n.now_utc() + datetime.timedelta(minutes=i))
retries = [e for e in a.notify_log_read(None, 100000)[LINES:] if e["result"] == "retry"]
ok("ein vorübergehender Fehler steht einmal im Protokoll, nicht jede Minute",
   len(retries) == 1, retries)
for fn in os.listdir(a.NOTIFY_OUTBOX_DIR):
    os.remove(os.path.join(a.NOTIFY_OUTBOX_DIR, fn))
a.NOTIFY_SEND = fake
a._notify_state_rw(lambda s: {})
a.load_registry = lambda: {"instances": {"kasse": {"tenant": tid}, "web": {"tenant": tid},
                                         "uk": {"tenant": dflt}}}
facts = {"advisories": {"open": 0}, "images": {"instances": {
    "kasse": {"state": "open", "open": 2}, "web": {"state": "open", "open": 5},
    "uk": {"state": "fresh"}}}}
cur = a.notify_conditions(facts)
ok("Abbild-Korrekturen: eine Lage je Mandant, der Text nennt die Instanzen",
   list(k for k in cur[tid] if k.startswith("advisory.image")) == ["advisory.image"]
   and "kasse: 2" in cur[tid]["advisory.image"]["text"]
   and "web: 5" in cur[tid]["advisory.image"]["text"]
   and not any(k.startswith("advisory.image") for k in cur[dflt]), cur[tid])
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "roles": ["server_admin"], "active": True,
                "tenant": dflt, "email": "joerg@node.example"},
               {"username": "kassierer", "roles": ["tenant_admin"], "active": True,
                "tenant": tid, "email": "kasse@verein.example"}], f)
view = a.notify_view_write()
ok("die Ansicht zählt, wen ein Verteiler gerade erreicht",
   view[dflt][n.NODE_MANDATORY] == 1 and view[tid]["Backup-Verantwortliche"] == 3, view)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "roles": ["server_admin"], "active": True,
                "tenant": dflt}], f)
ok("ein Server-Admin ohne Adresse: der Pflicht-Verteiler erreicht niemanden",
   a.notify_view_write()[dflt][n.NODE_MANDATORY] == 0)

print("=== Backup und apps/ (Messung 11.11.) ===")
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "roles": ["server_admin"], "active": True,
                "tenant": dflt, "email": "joerg@node.example"},
               {"username": "kassierer", "roles": ["tenant_admin"], "active": True,
                "tenant": tid, "email": "kasse@verein.example"}], f)
ok("Zustand und Korb liegen außerhalb von apps/ (die Liste des Backups)",
   not a.NOTIFY_STATE_FILE.startswith(a.APPS_DIR) and not a.NOTIFY_OUTBOX_DIR.startswith(a.APPS_DIR))
a.notify_view_write()
m0 = os.stat(a.NOTIFY_VIEW_FILE).st_mtime_ns
before = sorted(os.listdir(a.APPS_DIR))
a.notify_view_write()
ok("die Ansicht wird nur bei Änderung geschrieben",
   os.stat(a.NOTIFY_VIEW_FILE).st_mtime_ns == m0 and sorted(os.listdir(a.APPS_DIR)) == before)
with open(a.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "running", "started": n.now_utc().isoformat()}, f)
a._notify_state_rw(lambda s: {"matters": {dflt: {"backup.failed": {
    "type": "backup.failed", "opened": "x", "last_sent": n.now_utc().isoformat()}}}})
SENT.clear()
a.notify_event("rebuild.failed", tid, instanz="w")
res = a.notify_minute({})
ok("während ein Backup läuft: nichts verschickt, keine Lage geschlossen",
   res == {} and SENT == [] and "backup.failed" in a._notify_state_rw()["matters"][dflt]
   and len(os.listdir(a.NOTIFY_OUTBOX_DIR)) == 1, (res, SENT))
with open(a.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "running", "started": "2026-01-01T00:00:00+00:00"}, f)
ok("ein seit Stunden „laufendes“ Backup hält nichts mehr auf", not a.notify_paused_by_backup())
with open(a.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "ok", "finished": n.now_utc().isoformat()}, f)
a.notify_minute({})
ok("danach: der wartende Neubau geht raus, das Backup meldet „Behoben“",
   any("Neubau" in m["subject"] for m in SENT)
   and any(m["subject"].startswith("Behoben") and "Backup" in m["subject"] for m in SENT),
   [m["subject"] for m in SENT])

print("=== Stufe 2: Instanz ausgefallen ===")
R = lambda st="running", **k: dict({"service": "", "container": "c", "state": st}, **k)
ok("gesund: nichts", n.instance_trouble({"services": [R(health="healthy")]}) == "")
ok("Bereitschaft und Wartung sind Entscheidungen, keine Ausfälle",
   n.instance_trouble({"mode": "standby", "services": [R("exited")]}) == ""
   and n.instance_trouble({"maintenance": True, "services": [R("exited")]}) == "")
ok("beendet mit Speichermangel, Gesundheitsprüfung, Container fehlt",
   "Speicher" in n.instance_trouble({"services": [R("exited", oom=True, exit_code=137)]})
   and "Gesundheitsprüfung" in n.instance_trouble({"services": [R(health="unhealthy")]})
   and n.instance_trouble({"services": [R("absent")]}) == "Container fehlt")
ok("mehrere Dienste: der kaputte wird genannt",
   n.instance_trouble({"services": [R(service="web"), R("restarting", service="db")]})
   == "db: Container restarting")
V = {"kasse": {"services": [R("exited", exit_code=1)]}}
t0 = n.now_utc()
seen_by, st_ = [], {}
for mnt in range(6):          # the minute job, minute by minute
    cm, st_ = n.down_conditions(V, {"kasse": tid}, st_, t0 + datetime.timedelta(minutes=mnt))
    seen_by.append(list(cm))
    if mnt == 4:
        s4 = st_
ok("erst nach fünf Minuten eine Lage", seen_by == [[]] * 5 + [["kasse"]]
   and "Exit-Code 1" in cm["kasse"][1]["text"], seen_by)
cg, _ = n.down_conditions(V, {"kasse": tid}, s4, t0 + datetime.timedelta(minutes=9))
ok("eine Lücke in der Beobachtung (Backup-Pause) startet die Uhr neu", cg == {}, cg)

a._notify_state_rw(lambda st: {})
for fn in os.listdir(a.NOTIFY_OUTBOX_DIR):
    os.remove(os.path.join(a.NOTIFY_OUTBOX_DIR, fn))
REG = {"instances": {"kasse": {"tenant": tid}, "kasse-test": {"tenant": tid, "channel": "test"},
                     "uk": {"tenant": dflt}}}
a.load_registry = lambda: REG


def state_view(runtime_ok=True, **down):
    inst = {nm: {"services": [R()]} for nm in REG["instances"]}
    inst.update({nm: {"services": [R(st)]} for nm, st in down.items()})
    with open(a.STATE_VIEW, "w", encoding="utf-8") as f:
        json.dump({"written": n.now_utc().isoformat(), "runtime_ok": runtime_ok,
                   "instances": inst}, f)


state_view(**{"kasse": "exited", "kasse-test": "exited"})
past = (n.now_utc() - datetime.timedelta(minutes=6)).isoformat()
seen = (n.now_utc() - datetime.timedelta(minutes=1)).isoformat()
a._notify_state_rw(lambda st: {"down_since": {nm: {"since": past, "seen": seen}
                                              for nm in ("kasse", "kasse-test")}})
SENT.clear()
a.notify_minute({})
down = [m for m in SENT if "ausgefallen" in m["subject"]]
ok("Produktiv-Instanz seit sechs Minuten aus: eine Mail an den Mandanten, die Test-Instanz nicht",
   [m["subject"] for m in down] == ["[Handballverein X] Instanz ausgefallen: kasse"]
   and down[0]["rcpts"]["to"] == ["kasse@verein.example"], [m["subject"] for m in SENT])
ok("der Link führt auf die Instanzseite",
   down and "https://verein.oaap.example/instances/kasse" in down[0]["body"], down and down[0]["body"])
state_view(runtime_ok=False)
SENT.clear()
a.notify_minute({})
ok("Laufzeit nicht erreichbar: „unbekannt“ ist kein „Behoben“", SENT == [],
   [m["subject"] for m in SENT])
state_view()
SENT.clear()
a.notify_minute({})
ok("wieder gesund -> „Behoben“",
   [m["subject"] for m in SENT] == ["Behoben: [Handballverein X] Instanz ausgefallen: kasse"],
   [m["subject"] for m in SENT])
state_view(kasse="exited")
a._notify_state_rw(lambda st: dict(st, down_since={"kasse": {"since": past, "seen": seen}}))
a.notify_minute({})
del REG["instances"]["kasse"]
state_view()
SENT.clear()
a.notify_minute({})
ok("im Ausfall entfernt: keine „Behoben“-Mail, die Lage ist zu",
   SENT == [] and not any(k.startswith("instance.down")
                          for k in (a._notify_state_rw().get("matters") or {}).get(tid, {})),
   [m["subject"] for m in SENT])

print("=== Stufe 2: Anmeldung gesperrt ===")
ident = open(os.path.join(HERE, "..", "platform", "services", "identity", "app.py"),
             encoding="utf-8").read()
ok("dieselben Zahlen wie Identity (5 Fehlversuche in 300 s)",
   f"_LOCK_THRESHOLD, _LOCK_WINDOW, _LOCK_SECONDS = {n.LOCK_THRESHOLD}, {n.LOCK_WINDOW}, 60" in ident)
import time as _time  # noqa: E402
now = _time.time()
hits = lambda k, ago=10: [now - ago - i for i in range(k)]
with open(a.IDENTITY_THROTTLE_FILE, "w", encoding="utf-8") as f:
    json.dump({"203.0.113.7|kassierer": hits(5), "198.51.100.9|admin\nBcc: x@evil": hits(6),
               "192.0.2.1|joerg": hits(4), "192.0.2.2|kassierer": hits(5, ago=400),
               "keyclient|192.0.2.3": hits(5)}, f)
SENT.clear()
q = a.notify_login_locks(now)
a.notify_deliver()
by = {m["subject"]: m for m in SENT}
kas = by.get("[Handballverein X] Anmeldung gesperrt: kassierer")
ok("gesperrter Name des Mandanten -> an den Mandanten, mit Adresse und Zahl",
   kas and kas["rcpts"]["to"] == ["kasse@verein.example"] and "203.0.113.7" in kas["body"]
   and "5 Fehlversuche" in kas["body"], list(by))
node = [m for k, m in by.items() if k.startswith("[oaap.example] Anmeldung gesperrt")]
ok("unbekannter Name und Schlüssel-Brute-Force -> an den Knoten, in einer Mail",
   q == 2 and len(node) == 1 and "diesen Benutzer gibt es nicht" in node[0]["body"]
   and "192.0.2.3" in node[0]["body"] and node[0]["rcpts"]["to"] == ["joerg@node.example"],
   (q, list(by)))
ok("vier Versuche und alte Versuche sperren nicht", "192.0.2.1" not in str(SENT)
   and "192.0.2.2" not in str(SENT))
ok("ein Name mit Zeilenumbruch kommt nicht als Kopfzeile an",
   all("\n" not in m["subject"] for m in SENT) and "adminBcc" in str(node and node[0]["body"]))
SENT.clear()
ok("dieselbe Sperre in der nächsten Minute: keine zweite Mail",
   a.notify_login_locks(now + 60) == 0)
ok("nach einer Stunde ohne frische Fehlversuche: nichts",
   a.notify_login_locks(now + 3700) == 0)   # the hits are older than five minutes by then
with open(a.IDENTITY_THROTTLE_FILE, "w", encoding="utf-8") as f:
    json.dump({"203.0.113.7|kassierer": [now + 3690 - i for i in range(5)]}, f)
ok("… mit frischen Fehlversuchen nach einer Stunde: wieder eine", a.notify_login_locks(now + 3700) == 1)
for fn in os.listdir(a.NOTIFY_OUTBOX_DIR):
    os.remove(os.path.join(a.NOTIFY_OUTBOX_DIR, fn))

print("=== die Tür ===")
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([
        {"username": "joerg", "roles": ["server_admin"], "active": True,
         "tenant": dflt, "email": "joerg@node.example"},
        {"username": "kassierer", "roles": ["tenant_admin"], "active": True,
         "tenant": tid, "email": "kasse@verein.example"}], f)
okj, said = a.notify_job({"op": "list-set", "args": {"name": "Böse", "to": "x@y.de"}},
                         "kassierer")
ok("ein Mandanten-Admin schreibt in seinem Mandanten", okj, said)
okj, said = a.notify_job({"op": "list-set", "args": {"name": "Böse", "to": "x@y.de",
                                                     "tenant": dflt}}, "kassierer")
ok("… aber nie am Knoten", not okj, said)
okj, said = a.notify_job({"op": "list-remove", "args": {"name": "Mandantenverwaltung"}},
                         "kassierer")
ok("der Pflicht-Verteiler lässt sich nicht entfernen", not okj and "mandatory" in said, said)
okj, said = a.notify_job({"op": "route-set", "args": {"routes": {
    "critical": {"lists": ["Gibtsnicht"]}}}}, "kassierer")
ok("eine Zuordnung auf einen unbekannten Verteiler abgelehnt", not okj, said)
okj, said = a.notify_job({"op": "route-set", "args": {"routes": {
    "disk.full": {"lists": ["Böse"]}}}}, "kassierer")
ok("ein Mandant kann keine Knotenart zuordnen", not okj, said)
SENT.clear()
okj, said = a.notify_job({"op": "test", "args": {"name": "Böse"}}, "kassierer")
ok("Probe senden: eine Mail an genau diesen Verteiler, als Probe erkennbar",
   okj and len(SENT) == 1 and SENT[0]["rcpts"]["to"] == ["x@y.de"]
   and SENT[0]["subject"] == "[oaap.example] Probe des Verteilers Böse"
   and "Es ist nichts passiert" in SENT[0]["body"], (said, SENT))
for _ in range(5):
    a.notify_job({"op": "test", "args": {"name": "Böse"}}, "kassierer")
okj, said = a.notify_job({"op": "test", "args": {"name": "Böse"}}, "kassierer")
ok("höchstens 5 Proben je Stunde", not okj and "5" in said, said)
log = open(a.NOTIFY_LOG_FILE, encoding="utf-8").read()
ok("das Protokoll nennt keine Adresse", "@" not in log)
ok("die Verteiler-Datei ist für das Portal lesbar, der Zustand nicht",
   (os.stat(a.NOTIFY_FILE).st_mode & 0o777) == 0o644 or os.name == "nt")

print()
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
