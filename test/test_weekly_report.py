#!/usr/bin/env python3
"""Der Wochenbericht (RFC-0083 Stufen 2-4).

    Das Tagebuch    eine Zeile je Bereich und Tag: Ereignisse, Mails,
                    Messwerte, Backup, Bremse, Bytes; der erste Lauf holt
                    sechs Tage nach, ein zweiter schreibt nichts doppelt.
    Der Bericht     Zusammenfassung zuerst; Neustart mit Kernel, jede
                    verschickte Mail, Backup, Bremse mit Adressenzahl,
                    min/Ø/max, Wachstum "seit N Tagen gemessen".
    Die Mail        Text + HTML, Betreff [alias] Wochenbericht 2026-Wnn;
                    der HTML-Teil maskiert Text aus dem Protokoll.
    Zustellung      "nur im Bericht" verschickt nichts und steht im Bericht;
                    Kritisches kann nicht "nur im Bericht" sein.
    Mandanten       ein Bericht nur mit eigener Zuordnung, nur eigene Daten.

Braucht kein Docker, keinen Knoten, keinen Mailserver.
Aufruf: python3 test/test_weekly_report.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-report-test-")
os.environ["OAAP_DATA_DIR"] = DATA
os.environ["TZ"] = "Europe/Berlin"
if hasattr(time, "tzset"):
    time.tzset()
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import report as r  # noqa: E402
import notify as n  # noqa: E402
import appctl as a  # noqa: E402

a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


def at(day, hh=12, mm=0):
    return time.mktime(time.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M"))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))


dflt = a.ensure_default_tenant()
tv, _ = a.tenant_create("verein", "Handballverein X")
to, _ = a.tenant_create("andere", "Andere")
a.load_external = lambda: "oaap.example"
a.socket.gethostname = lambda: "oaapx01"
a.node_identity_set(alias="oaapx01")
a.mail_set("smtps://mail.example.org:465", "noreply@example.org", "", "", "")
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "roles": ["server_admin"], "active": True,
                "tenant": dflt, "email": "joerg@node.example"},
               {"username": "kasse", "roles": ["tenant_admin"], "active": True,
                "tenant": tv, "email": "kasse@verein.example"}], f)
SENT = []
a.NOTIFY_SEND = lambda mail, sender, rcpts, subject, body, html=None: (
    SENT.append({"subject": subject, "body": body, "html": html, "to": rcpts["to"]})
    or (True, "accepted for 1"))
a.tenant_dir = lambda t: os.path.join(DATA, "tenants", t)
for t in (dflt, tv, to):
    os.makedirs(a.tenant_dir(t), exist_ok=True)
    open(os.path.join(a.tenant_dir(t), "x"), "w").write("y" * (1000 if t == tv else 10))

# a week: Mon 2026-11-09 .. Sun 2026-11-15; "today" Mon 2026-11-16 07:05
WEEK = [f"2026-11-{d:02d}" for d in range(9, 16)]
NOW = at("2026-11-16", 7, 5)

# the records the sources keep
log = []
for t in (dflt, tv, to):
    log.append({"when": iso(at("2026-11-10", 6, 42)), "tenant": t, "action": "reboot.planned",
                "subject": "node", "result": "ok", "detail": "2026-11-12 04:15 UTC (care): kernel"})
    log.append({"when": iso(at("2026-11-12", 4, 15)), "tenant": t, "action": "reboot.started",
                "subject": "node", "result": "ok", "detail": "kernel waits"})
    log.append({"when": iso(at("2026-11-12", 4, 16)), "tenant": t, "action": "reboot.done",
                "subject": "node", "result": "ok", "detail": "rebooted; running 6.12.116+deb13-amd64"})
log.append({"when": iso(at("2026-11-11", 9)), "tenant": tv, "action": "key.issue", "result": "ok",
            "detail": "not a report event"})
log.append({"when": iso(at("2026-11-13", 9)), "tenant": tv, "action": "instance.rebuild",
            "subject": "kasse", "result": "failed", "detail": "exit 1 <script>x</script> | pipe"})
os.makedirs(os.path.dirname(a.TENANT_LOG), exist_ok=True)
with open(a.TENANT_LOG, "a", encoding="utf-8") as f:
    for e in log:
        f.write(json.dumps(e) + "\n")
with open(a.NOTIFY_LOG_FILE, "a", encoding="utf-8") as f:
    f.write(json.dumps({"when": iso(at("2026-11-12", 4, 15)), "tenant": dflt,
                        "type": "reboot.started", "op": "event", "result": "sent"}) + "\n")
    f.write(json.dumps({"when": iso(at("2026-11-13", 9, 1)), "tenant": tv,
                        "type": "rebuild.failed", "op": "event", "result": "sent"}) + "\n")
os.makedirs(a.METRICS_DIR, exist_ok=True)
with open(os.path.join(a.METRICS_DIR, "30m.jsonl"), "w") as f:
    for d in WEEK:
        for h in range(0, 24):
            f.write(json.dumps({"t": int(at(d, h)), "cpu": [30, 9.0, 2.0, 71.0 if d == WEEK[2] else 40.0],
                                "disk": [30, 61.0, 60.0, 62.0]}) + "\n")
hour = int(at("2026-11-15", 10, 7) // 3600)
os.makedirs(os.path.dirname(a.IDENTITY_BRAKED_FILE), exist_ok=True)
json.dump({"market": {str(hour): {"n": 2312, "c": ["abcd1234"], "m": {"7": 2312}}}},
          open(a.IDENTITY_BRAKED_FILE, "w"))
reg = a.load_registry()
reg["instances"]["market"] = {"app_id": "m", "app_name": "Markt", "tenant": tv, "routes": [],
                              "channel": "production", "version": "1"}
a.save_registry(reg)
a._record_backup_run("ok", archive="/x.tar.gz", bytes=4_100_000_000, downtime_seconds=47,
                     total_seconds=52)
with open(a.BACKUP_RUN_FILE) as f:
    rec = json.load(f)
rec["finished"] = iso(at("2026-11-15", 2, 31))
json.dump(rec, open(a.BACKUP_RUN_FILE, "w"))

print("=== das Tagebuch ===")
days = a.ledger_minute(now=NOW)
ok("der erste Lauf schreibt sieben Tage (gestern + sechs davor)",
   days == WEEK[0:7] or days == WEEK[-7:], days)
ok("ein zweiter Lauf schreibt nichts doppelt", a.ledger_minute(now=NOW + 60) == [])
node = r.read_ledger(a._ledger_path(dflt))
byday = {e["day"]: e for e in node}
ok("Neustart am Donnerstag im Tagebuch des Knotens",
   [e["a"] for e in byday["2026-11-12"]["events"] if e["a"].startswith("reboot.")]
   == ["reboot.started", "reboot.done"], byday["2026-11-12"])
ok("nur Ereignisse aus der Liste (key.issue nicht)",
   not any(e["a"] == "key.issue" for x in r.read_ledger(a._ledger_path(tv)) for e in x.get("events", [])))
ok("Messwerte je Tag: min/Ø/max", byday["2026-11-11"]["metrics"]["cpu"][3] == 71.0, byday["2026-11-11"].get("metrics"))
ok("gestern (So): Bytes, Backup, Bremse, Pflege",
   byday["2026-11-15"].get("bytes", {}).get("disk_used") and byday["2026-11-15"]["backup"]["state"] == "ok"
   and byday["2026-11-15"]["brake"]["market"]["count"] == 2312, byday["2026-11-15"])
ok("ältere Tage: Bytes als „nicht gemessen“, nichts erfunden",
   "bytes" in byday["2026-11-10"].get("unmeasured", []) and "brake" not in byday["2026-11-10"])
tl = {e["day"]: e for e in r.read_ledger(a._ledger_path(tv))}
ok("der Mandant: seine Mail, seine Bremse, keine Messwerte des Knotens",
   tl["2026-11-13"]["mails"][0]["type"] == "rebuild.failed" and "market" in tl["2026-11-15"]["brake"]
   and "metrics" not in tl["2026-11-15"], tl["2026-11-15"])
ok("der andere Mandant sieht die Bremse des Vereins nicht",
   "brake" not in {e["day"]: e for e in r.read_ledger(a._ledger_path(to))}["2026-11-15"])
ok("das Tagebuch ist 0600", oct(os.stat(a._ledger_path(dflt)).st_mode & 0o777) in ("0o600", "0o666"))

print("=== der Bericht ===")
ok("vor Montag 07:00 kein Bericht", a.report_minute(now=at("2026-11-16", 6, 59)) == [])
done = a.report_minute(now=NOW)
ok("Montag 07:05: der Knoten bekommt KW 46, der Mandant ohne Zuordnung nicht",
   done == [(dflt, "2026-W46")], done)
md = a.report_read(dflt, "2026-W46")
ok("Titel mit Alias und Woche", md.startswith("# Wochenbericht oaapx01 — KW 46 (09.11.–15.11.2026)"), md[:120])
summary = md.split("## Ereignisse")[0]
ok("Zusammenfassung: Neustart mit Kernel, Backup, Platte, Bremse mit Adresse",
   "1 Neustart (Kernel 6.12.116+deb13-amd64)" in summary and "Backup: 1 von 1 gelungen" in summary
   and "Platte: 61 % belegt" in summary and "2312 gebremste Anfragen, größter Teil von 1 Adresse" in summary,
   summary)
ok("Ereignisse: Neustart geplant/Neustart/Zurück", "Neustart geplant" in md and "| Zurück nach Neustart" in md)
ok("jede verschickte Mail des Knotens steht drin", "reboot.started | Ereignis | verschickt" in md, md)
ok("Messwerte min/Ø/max, deutsch geschrieben", "| CPU | 2 % | 9 % | 71 % |" in md
   and "| Platte | 60 % | 61 % | 62 % |" in md, md.split("## Messwerte")[-1][:300])
ok("… Kommazahlen mit Komma", r._num(3.24) == "3,2" and r._num(0.1) == "0,1" and r._num(46.4) == "46")
ok("Wachstum: „seit 1 Tagen gemessen“ statt einer erfundenen Zahl", "seit 1 Tagen gemessen" in md,
   md.split("## Wachstum")[-1][:400])
ok("ein zweiter Lauf schreibt die Woche nicht noch einmal", a.report_minute(now=NOW + 300) == [])
res = a.notify_deliver()
m = SENT[-1]
ok("verschickt: Betreff [oaapx01] Wochenbericht 2026-W46, an den Serverbesitzer",
   m["subject"] == "[oaapx01] Wochenbericht 2026-W46" and m["to"] == ["joerg@node.example"], (res, m["subject"]))
ok("Text = der Bericht + Link auf den Reiter", m["body"].startswith("# Wochenbericht")
   and "tab=berichte&woche=2026-W46" in m["body"])
ok("HTML-Teil mit Tabellen", m["html"] and "<table>" in m["html"] and "<h2>Auf einen Blick</h2>" in m["html"])

print("=== Mandant mit eigener Zuordnung ===")
a.notify_list_set(tv, "Vorstand", a.notify_parse_entries("vorstand@verein.example"))
a.notify_route_set(tv, "report.weekly", ["Vorstand"])
done = a.report_minute(now=NOW + 600)
ok("jetzt bekommt der Verein seinen Bericht", done == [(tv, "2026-W46")], done)
tmd = a.report_read(tv, "2026-W46")
ok("… mit seiner Mail und der Bremse seiner Instanz, ohne Messwerte und ohne den anderen",
   "rebuild.failed" in tmd and "| market | 2312" in tmd and "## Messwerte" not in tmd
   and "andere" not in tmd, tmd)
ok("Text aus dem Protokoll wird im HTML maskiert",
   "<script>" not in r.to_html(tmd) and "&lt;script&gt;" in r.to_html(tmd))
a.notify_deliver()
ok("die Mail geht an den Verteiler des Vereins",
   SENT[-1]["to"] == ["vorstand@verein.example"] and "Handballverein X" in SENT[-1]["body"].splitlines()[0])

print("=== Zustellung: nur im Bericht ===")
try:
    a.notify_route_set(dflt, "backup.failed", [], delivery="report")
    said = ""
except a.NotifyRefused as e:
    said = str(e)
ok("Kritisches kann nicht „nur im Bericht“ sein", "always mailed at once" in said, said)
try:
    a.notify_route_set(dflt, "critical", [], delivery="report")
    said = ""
except a.NotifyRefused as e:
    said = str(e)
ok("… auch nicht die ganze Kategorie", "always mailed at once" in said, said)
a.notify_route_set(dflt, "operations", [], delivery="report")
ok("Betrieb nur im Bericht: gespeichert", n.delivery_for(a.notify_scope(dflt)[0], "reboot.started") == "report")
ok("… der Wochenbericht selbst bleibt sofort", n.delivery_for(a.notify_scope(dflt)[0], "report.weekly") == "now")
k = len(SENT)
a.notify_event("reboot.started", dflt, text="kernel")
res = a.notify_deliver()
ok("ein Neustart geht nicht raus, steht als „nur im Bericht“ im Protokoll",
   len(SENT) == k and res.get("report") == 1
   and a.notify_log_read(dflt, 1)[-1]["result"] == "report", (res, a.notify_log_read(dflt, 1)))
a.notify_route_set(dflt, "operations", [])
ok("ohne Angabe bleibt die Zustellung, wie sie war",
   n.delivery_for(a.notify_scope(dflt)[0], "reboot.started") == "report")
a.notify_route_set(dflt, "operations", [], delivery="now")
ok("zurück auf sofort", n.delivery_for(a.notify_scope(dflt)[0], "reboot.started") == "now")

print("=== Einstellungen ===")
ok("Standard Montag 07:00", a.report_settings() == {"day": "mon", "at": "07:00", "enabled": True})
ok("Freitag 18:30 gesetzt", "freitags 18:30" in a.report_settings_set("fri", "18:30"))
try:
    a.report_settings_set(at="25:00")
    said = ""
except ValueError as e:
    said = str(e)
ok("25:00 abgelehnt", "HH:MM" in said, said)

print("=== Markdown ===")
h = r.to_html("# T\n\n- a **b**\n- c\n\n| x | y |\n|---|---|\n| 1 | <b> |\n\n--\nfuß")
ok("Überschrift, Liste, Tabelle, Trennlinie; Text maskiert",
   "<h1>T</h1>" in h and "<li>a <strong>b</strong></li>" in h and "<td>&lt;b&gt;</td>" in h
   and "<hr>" in h, h)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
