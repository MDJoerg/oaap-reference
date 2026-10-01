#!/usr/bin/env python3
"""Die Verwaltungs-API (oaap.core.management 0.1): Tore, Auftraege, Handout.

Gemessen wird, was die Spezifikation verspricht -- nicht, dass der Code
laeuft:

  * ein Archiv mit `..`, absolutem Pfad, Link, zu vielen Eintraegen oder
    ohne cohort.yaml wird abgelehnt, BEVOR etwas entpackt wird;
  * der Mandant ist der des Aufrufers; ein fremder Name ist 404, ein
    genannter Mandant im Body aendert fuer einen tenant_admin nichts;
  * mit Sitzung verlangt jeder aendernde Aufruf `X-OAAP-API: 1`, mit
    Schluessel nicht;
  * ein Auftrag ist queued -> running -> done, und nur sein Mandant sieht ihn;
  * das Handout kommt einmal, als ZIP, mit Passwort verschluesselt, ohne
    Passwort als `unencrypted` gekennzeichnet; ein zweites Mal ist 410, ein
    Kollege bekommt es nicht.

Braucht Flask und pyzipper; der Knoten (Spool, Worker) ist ersetzt.
"""
import io
import json
import os
import stat
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


import management_api as m  # noqa: E402
from flask import Flask, request  # noqa: E402

SPOOL = tempfile.mkdtemp(prefix="oaap-mgmt-spool-")
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(SPOOL, d))
VIEWF = os.path.join(SPOOL, "cohort-view.json")
m.SPOOL_DIR = SPOOL
m.VIEW = VIEWF


def build_zip(entries, links=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            i = zipfile.ZipInfo("x")
            i.filename = name            # ZipInfo() would turn "\\" into "/" on Windows
            i.external_attr = 0o100644 << 16
            z.writestr(i, data)
        for name, target in links:
            i = zipfile.ZipInfo(name)
            i.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(i, target)
    return buf.getvalue()


GOOD = build_zip([("cohort.yaml", "oaap_cohort: '0.1'\n"), ("seeds/a.json", "{}")])

print("Das Archiv -- vor dem Entpacken geprueft")
ok("ein gutes Archiv geht durch", m.archive_problem(GOOD) == ("", ""))
for label, data in (
        ("`..` im Pfad", build_zip([("cohort.yaml", "x"), ("../evil", "x")])),
        ("absoluter Pfad", build_zip([("cohort.yaml", "x"), ("/etc/x", "x")])),
        ("Laufwerksbuchstabe", build_zip([("cohort.yaml", "x"), ("C:/x", "x")])),
        ("Symlink", build_zip([("cohort.yaml", "x")], links=[("l", "/etc/passwd")])),
        ("ohne cohort.yaml", build_zip([("seeds/a", "x")])),
        ("kein ZIP", b"das ist kein zip")):
    ok(f"abgelehnt: {label}", m.archive_problem(data)[0] == "bad", m.archive_problem(data))
if os.sep == "/":   # zipfile turns a backslash into "/" on Windows, on read and on write
    ok("abgelehnt: Backslash", m.archive_problem(
        build_zip([("cohort.yaml", "x"), ("a\\b", "x")]))[0] == "bad")
m.MAX_ENTRIES = 3
many = build_zip([("cohort.yaml", "x")] + [(f"f{i}", "x") for i in range(5)])
ok("abgelehnt: zu viele Eintraege", m.archive_problem(many)[0] == "bad")
m.MAX_ENTRIES = 5000
m.MAX_UNPACKED = 10
ok("abgelehnt: entpackt zu gross", m.archive_problem(GOOD)[0] == "too_large")
m.MAX_UNPACKED = 512 * 1024 * 1024
dest = os.path.join(SPOOL, "x", "template")
m.extract_archive(GOOD, dest)
ok("entpackt: Dateien liegen im Zielverzeichnis",
   os.path.isfile(os.path.join(dest, "cohort.yaml"))
   and os.path.isfile(os.path.join(dest, "seeds", "a.json")))

print("")
print("Das Handout als ZIP")
csvb = b"seat,username,password,address\n01,kurs-tn-01,Geheim123,https://x/\n"
blob, enc = m.handout_zip(csvb, "meinPasswort1")
ok("mit Passwort: verschluesselt, nur mit ihm lesbar", enc, "")
import pyzipper  # noqa: E402
with pyzipper.AESZipFile(io.BytesIO(blob)) as z:
    try:
        z.read("handout.csv")
        no_pw = True
    except RuntimeError:
        no_pw = False
    z.setpassword(b"meinPasswort1")
    good = z.read("handout.csv") == csvb
    z.setpassword(b"falsch-falsch")
    try:
        z.read("handout.csv")
        wrong = True
    except RuntimeError:
        wrong = False
ok("ohne Passwort nicht, mit falschem nicht, mit richtigem ja",
   not no_pw and good and not wrong, (no_pw, good, wrong))
blob2, enc2 = m.handout_zip(csvb, "")
ok("ohne Passwort: ein einfacher Behaelter, als solcher gekennzeichnet",
   not enc2 and zipfile.ZipFile(io.BytesIO(blob2)).read("handout.csv") == csvb)

print("")
print("Die Tore")
WHO = {"name": "ausbilder", "roles": {"tenant_admin"}, "tenant": "t-a", "host": None}
app = Flask(__name__)
QUEUED = []


def fake_queue(rid, name, payload, wait):
    QUEUED.append((rid, dict(payload), request.headers.get("X-OAAP-User", "?")))
    with open(os.path.join(SPOOL, "queue", rid + ".json"), "w") as f:
        json.dump(payload, f)


m.init(app, lambda: WHO["name"], lambda: set(WHO["roles"]),
       lambda: ("server_admin" if "server_admin" in WHO["roles"] else
                "tenant_admin" if "tenant_admin" in WHO["roles"] else "", WHO["tenant"]),
       lambda host: WHO["host"], fake_queue)
c = app.test_client()
API = "/api/v1/tenant"
H = {"X-OAAP-API": "1"}


def view(data):
    with open(VIEWF, "w") as f:
        json.dump({"schema": "0.1", "cohorts": data}, f)


def cohort(name, seats=("01", "02")):
    return {"name": name, "state": "running", "ended": "",
            "lifetime": {"ends": "2999-10-24"},
            "seats": [{"id": s, "user": f"{name}-tn-{s}", "instances": []} for s in seats]}


view({"t-a": {"kurs-a": cohort("kurs-a")}, "t-b": {"kurs-b": cohort("kurs-b")}})

r = c.get(f"{API}/cohorts")
ok("tenant_admin sieht NUR die Kohorten seines Mandanten",
   r.status_code == 200 and [x["name"] for x in r.json["cohorts"]] == ["kurs-a"], r.json)
r = c.get(f"{API}/cohorts/kurs-b")
ok("die Kohorte eines anderen Mandanten ist 404, wie eine unbekannte",
   r.status_code == 404 and c.get(f"{API}/cohorts/gibt-es-nicht").status_code == 404
   and r.json == c.get(f"{API}/cohorts/gibt-es-nicht").json.__class__(
       {"error": "no cohort 'kurs-b' in this tenant"}), r.json)
WHO["roles"] = {"user"}
ok("ohne Rolle: 403", c.get(f"{API}/cohorts").status_code == 403)
WHO["roles"] = {"tenant_admin"}
WHO["host"] = "t-b"
ok("am Ort eines fremden Mandanten: 404", c.get(f"{API}/cohorts").status_code == 404)
WHO["host"] = None

r = c.post(f"{API}/cohorts/kurs-a/stop")
ok("Sitzung ohne X-OAAP-API: abgelehnt, nichts in der Warteschlange",
   r.status_code == 403 and not QUEUED, (r.status_code, r.json))
r = c.post(f"{API}/cohorts/kurs-a/stop", headers={"Authorization": "Bearer oaapk_x"})
ok("Schluessel ohne den Kopf: geht", r.status_code == 202, r.json)
QUEUED.clear()
r = c.post(f"{API}/cohorts/kurs-a/stop", headers=H, json={"tenant": "b"})
ok("ein genannter Mandant im Body aendert fuer tenant_admin nichts",
   r.status_code == 202 and "tenant" not in QUEUED[-1][1]["args"], QUEUED[-1])
ok("der Auftrag nennt Handelnden und Operation, der Mandant kommt vom Aufrufer",
   QUEUED[-1][1]["op"] == "stop" and QUEUED[-1][1]["args"]["cohort"] == "kurs-a")
meta = json.load(open(os.path.join(SPOOL, "jobs", r.json["job"], "meta.json")))
ok("der Auftragssatz ist 0600 und tragt den Mandanten", meta["tenant"] == "t-a"
   and meta["by"] == "ausbilder")
r = c.post(f"{API}/cohorts/kurs-b/start", headers=H)
ok("fremde Kohorte starten: 404", r.status_code == 404)
r = c.post(f"{API}/cohorts/kurs-a/extend", headers=H, json={"ends": "morgen"})
ok("extend mit falschem Datum: 400", r.status_code == 400)
r = c.post(f"{API}/cohorts/kurs-a/extend", headers=H, json={"ends": "2999-11-07"})
ok("extend mit Datum: Auftrag", r.status_code == 202
   and QUEUED[-1][1]["args"]["ends"] == "2999-11-07")
r = c.delete(f"{API}/cohorts/kurs-a", headers=H, json={"purge": True})
ok("Entfernen ohne confirm: 400", r.status_code == 400)
r = c.delete(f"{API}/cohorts/kurs-a", headers=H, json={"confirm": "anderer"})
ok("Entfernen mit falschem confirm: 409", r.status_code == 409)
n = len(QUEUED)
r = c.delete(f"{API}/cohorts/kurs-a", headers=H, json={"confirm": "kurs-a", "purge": True, "users": True})
ok("Entfernen mit confirm: Auftrag mit allen Angaben",
   r.status_code == 202 and QUEUED[-1][1]["op"] == "remove"
   and QUEUED[-1][1]["args"]["purge"] and QUEUED[-1][1]["args"]["users"], len(QUEUED) - n)
r = c.delete(f"{API}/cohorts/kurs-a/seats/02", headers=H, json={"confirm": "kurs-a"})
ok("einen Platz entfernen", r.status_code == 202 and QUEUED[-1][1]["op"] == "remove-seat"
   and QUEUED[-1][1]["args"]["seat"] == "02")
ok("einen Platz, den es nicht gibt: 404",
   c.delete(f"{API}/cohorts/kurs-a/seats/99", headers=H, json={"confirm": "kurs-a"}).status_code == 404)
r = c.post(f"{API}/cohorts/kurs-a/seats/01/reset", headers=H, json={"keep_home": True})
ok("reset eines Platzes", r.status_code == 202 and QUEUED[-1][1]["args"]["keep_home"] is True)
r = c.post(f"{API}/cohorts/kurs-a/seats", headers=H, json={"name": "Anna Beispiel"})
ok("Platz hinzufuegen mit Namen", r.status_code == 202
   and QUEUED[-1][1]["args"]["name"] == "Anna Beispiel")

print("")
print("Anlegen aus einem Archiv")
n = len(QUEUED)
r = c.post(f"{API}/cohorts", headers=H, data=GOOD, content_type="application/zip")
ok("ein gutes Archiv: Auftrag, Vorlage liegt im Auftragsverzeichnis",
   r.status_code == 202 and os.path.isfile(os.path.join(
       SPOOL, "jobs", r.json["job"], "template", "cohort.yaml")), r.json)
rid_create = r.json["job"]
n = len(QUEUED)
bad = build_zip([("cohort.yaml", "x"), ("../evil", "x")])
r = c.post(f"{API}/cohorts", headers=H, data=bad, content_type="application/zip")
ok("ein boeses Archiv: 400 und NICHTS in der Warteschlange, nichts entpackt",
   r.status_code == 400 and len(QUEUED) == n
   and not os.path.exists(os.path.join(SPOOL, "evil")), (r.status_code, r.json))
r = c.post(f"{API}/cohorts", headers=H, data=b"{}", content_type="application/json")
ok("ein anderer Inhaltstyp: 400", r.status_code == 400)

print("")
print("Die Auftraege")
jid = rid_create
r = c.get(f"{API}/jobs/{jid}")
ok("wartet: queued", r.status_code == 200 and r.json["status"] == "queued", r.json)
os.replace(os.path.join(SPOOL, "queue", jid + ".json"), os.path.join(SPOOL, "claims", jid + ".json"))
ok("angenommen: running", c.get(f"{API}/jobs/{jid}").json["status"] == "running")
HF = os.path.join(SPOOL, "jobs", jid, "handout.csv")
with open(HF, "wb") as f:
    f.write(csvb)
os.remove(os.path.join(SPOOL, "claims", jid + ".json"))
with open(os.path.join(SPOOL, "jobs", jid, "result.json"), "w") as f:
    json.dump({"id": jid, "op": "create", "ok": True, "message": "ok", "finished": "x", "handout": True}, f)
r = c.get(f"{API}/jobs/{jid}")
ok("fertig: done, ok, mit Handout", r.json["status"] == "done" and r.json["ok"] and r.json["handout"], r.json)
WHO["tenant"] = "t-b"
ok("ein Tenant-Admin eines ANDEREN Mandanten sieht den Auftrag nicht: 404",
   c.get(f"{API}/jobs/{jid}").status_code == 404)
WHO["tenant"] = "t-a"
ok("ein Auftrag, den es nicht gibt: 404", c.get(f"{API}/jobs/{'0' * 32}").status_code == 404)
ok("eine Auftragsnummer, die kein Auftrag ist: 404", c.get(f"{API}/jobs/..%2F..%2Fx").status_code == 404)

print("")
print("Das Handout")
WHO["name"] = "kollege"
r = c.post(f"{API}/jobs/{jid}/handout", headers=H)
ok("ein Kollege im selben Mandanten bekommt es nicht", r.status_code == 403, r.status_code)
WHO["name"] = "ausbilder"
r = c.post(f"{API}/jobs/{jid}/handout", headers=H, json={"password": "kurz"})
ok("ein zu kurzes Passwort: 400, die Datei bleibt", r.status_code == 400 and os.path.exists(HF))
r = c.post(f"{API}/jobs/{jid}/handout", json={})
ok("Sitzung ohne X-OAAP-API: 403, die Datei bleibt", r.status_code == 403 and os.path.exists(HF))
QUEUED.clear()
r = c.post(f"{API}/jobs/{jid}/handout", headers=H, json={"password": "meinPasswort1"})
ok("mit Passwort: ZIP, verschluesselt, ohne Kennzeichen 'unencrypted'",
   r.status_code == 200 and r.mimetype == "application/zip"
   and "X-OAAP-Handout" not in r.headers, (r.status_code, dict(r.headers)))
with pyzipper.AESZipFile(io.BytesIO(r.data)) as z:
    z.setpassword(b"meinPasswort1")
    ok("der Inhalt ist das Handout", z.read("handout.csv") == csvb)
ok("danach ist die Datei WEG (nicht nur umbenannt)",
   not os.path.exists(HF) and not [f for f in os.listdir(os.path.join(SPOOL, "jobs", jid))
                                   if f.startswith("handout")])
ok("das Protokoll bekommt eine Notiz -- ohne Passwort",
   QUEUED and QUEUED[-1][1]["op"] == "handout-note"
   and "meinPasswort1" not in json.dumps(QUEUED[-1][1]), QUEUED)
r = c.post(f"{API}/jobs/{jid}/handout", headers=H, json={})
ok("ein zweites Mal: 410", r.status_code == 410)
ok("der Auftrag zeigt jetzt: kein Handout mehr", c.get(f"{API}/jobs/{jid}").json["handout"] is False)

jid2 = "a" * 32
os.makedirs(os.path.join(SPOOL, "jobs", jid2))
json.dump({"id": jid2, "op": "add", "by": "ausbilder", "tenant": "t-a", "cohort": "kurs-a"},
          open(os.path.join(SPOOL, "jobs", jid2, "meta.json"), "w"))
json.dump({"id": jid2, "ok": True, "message": "ok"},
          open(os.path.join(SPOOL, "jobs", jid2, "result.json"), "w"))
open(os.path.join(SPOOL, "jobs", jid2, "handout.csv"), "wb").write(csvb)
r = c.post(f"{API}/jobs/{jid2}/handout", headers=H)
ok("ohne Passwort: ZIP als 'unencrypted' gekennzeichnet",
   r.status_code == 200 and r.headers.get("X-OAAP-Handout") == "unencrypted"
   and zipfile.ZipFile(io.BytesIO(r.data)).read("handout.csv") == csvb)
jid3 = "b" * 32
os.makedirs(os.path.join(SPOOL, "jobs", jid3))
json.dump({"id": jid3, "op": "add", "by": "ausbilder", "tenant": "t-a", "cohort": "kurs-a"},
          open(os.path.join(SPOOL, "jobs", jid3, "meta.json"), "w"))
open(os.path.join(SPOOL, "claims", jid3 + ".json"), "w").write("{}")
r = c.post(f"{API}/jobs/{jid3}/handout", headers=H)
ok("ein Auftrag, der noch laeuft: 409", r.status_code == 409)

print("")
print("ALLE BESTANDEN" if not fails else f"FAILED ({fails} Fehler)")
sys.exit(1 if fails else 0)
