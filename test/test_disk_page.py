#!/usr/bin/env python3
"""Die Seite „Datenträger“ (oaap.data.disks 0.2, RFC-0063 Stufe 2).

Gemessen wird, was die Spezifikation verspricht:

  * die Seite gibt es nur fuer den server_admin am Knoten selbst; ein
    Verwalter bekommt 403, am Ort eines Mandanten gibt es sie nicht (404),
    und der Menuepunkt erscheint genau dort;
  * sie schreibt nichts selbst: jede Schaltflaeche stellt EINEN Auftrag
    (Aktion `disk`); eine falsche Eingabe (UUID nicht zurueckgetippt, eine
    Nummer, die keine ist, eine Instanz, die es nicht gibt) und eine fremde
    Herkunft kommen nie in den Spool;
  * ein Archiv wird ueber seine NUMMER angelegt, nie ueber einen Pfad;
  * eine abgezogene Platte, deren Einhaengung haengt, ist zu sehen und
    auszuwerfen; formatiert wird nur eine Arbeitsplatte, die zu ist;
  * die Instanzseite zeigt die Freigaben lesend, mit „angekommen“.

Der reine Teil braucht nichts; die Seiten brauchen Flask (im Portal-Abbild).
Aufruf: python3 test/test_disk_page.py
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import disk_view as dv                                        # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


VIEW = {
    "schema": "0.1", "written": "2026-10-22T12:00:00+00:00",
    "tools": {"ratarmount": True, "cryptsetup": True},
    "disks": [
        {"name": "bak01", "role": "raw", "encryption": "none", "uuid": "24A5-9455",
         "label": "OAAP_BAK01", "model": "HDD", "state": "open", "stale": False,
         "size": 1_000_000_000_000, "usb_speed": 5000, "free": 2e11, "in_use": True},
        {"name": "bak02", "role": "work", "encryption": "luks",
         "uuid": "0b56b075-081a-4940-a066-54ce07041a48", "state": "open",
         "stale": False, "size": 5e11, "usb_speed": 480, "free": 4e11, "in_use": True},
        {"name": "leer", "role": "work", "encryption": "none", "uuid": "AAAA-1111",
         "state": "present", "stale": False, "size": 6.4e10, "usb_speed": 5000,
         "free": None, "in_use": False},
        {"name": "gezogen", "role": "raw", "encryption": "none", "uuid": "BBBB-2222",
         "state": "absent", "stale": True, "size": 1e11, "usb_speed": 0,
         "free": None, "in_use": False}],
    "devices": [{"uuid": "CCCC-3333", "fstype": "vfat", "size": 6.4e10,
                 "label": "STICK", "model": "Flash", "usb_speed": 5000, "luks": False}],
    "sources": [
        {"name": "bak01-05", "kind": "archive", "disk": "bak01", "format": "tar",
         "index_on": "bak02", "available": True, "index_size": 4_200_000,
         "verify": {"latest": "20261022-110000", "files": 560, "lists": 2,
                    "against": "20261022-100000", "equal": 559, "different": 1,
                    "new": 0, "missing": 0},
         "verify_running": False,
         "grants": [{"instance": "joerg-desk", "grant": "bak01-05", "mode": "ro",
                     "optional": False, "arrived": "ok"}]},
        {"name": "bak01-06", "kind": "archive", "disk": "bak01", "format": "tar",
         "index_on": "bak02", "available": True, "index_size": None, "verify": {},
         "verify_running": True, "grants": []}],
    "scans": {"bak01": {"written": "2026-10-22T10:00:00+00:00", "count": 3, "files": [
        {"nr": 1, "size": 5e10, "format": "tar", "source": "bak01-05"},
        {"nr": 2, "size": 4e10, "format": "tar", "source": ""},
        {"nr": 3, "size": 1e5, "format": "other", "source": ""}]},
        "leer": {"written": "", "count": 0, "files": []}},
    "instances": ["joerg-desk", "webtop"]}

print("=== die reinen Regeln ===")
d = {x["name"]: x for x in dv.disks(VIEW)}
ok("Zustand in Klartext, Groessen lesbar",
   d["bak01"]["state_label"] == "Geöffnet" and d["bak01"]["size"] == "1,0 TB"
   and d["leer"]["state_label"] == "Angesteckt, zu", d["bak01"])
ok("USB 2 warnt nur bei einer Platte in Benutzung",
   d["bak02"]["slow"] and not d["bak01"]["slow"] and not d["leer"]["slow"])
ok("abgezogen, Einhaengung haengt: sichtbar und auszuwerfen, nicht zu oeffnen oder zu durchsuchen",
   d["gezogen"]["stale"] and d["gezogen"]["can_eject"] and not d["gezogen"]["can_open"]
   and not d["gezogen"]["can_scan"])
ok("formatierbar nur eine Arbeitsplatte, die zu ist und nichts traegt",
   [x for x in d if d[x]["formattable"]] == ["leer"])
s = {x["name"]: x for x in dv.sources(VIEW)}
ok("Pruefsummen in Zahlen, eine Abweichung ist ein Alarm",
   "1 anders" in s["bak01-05"]["verify"] and s["bak01-05"]["verify_alarm"]
   and s["bak01-06"]["verify"] == "Prüfung läuft", s["bak01-05"]["verify"])
ok("Freigabe: angekommen", s["bak01-05"]["grants"][0]["arrived_label"] == "angekommen")
sc = dv.scans(VIEW)["bak01"]["files"]
ok("Scan: nur unbenutzte Archive sind waehlbar",
   [f["nr"] for f in sc if f["addable"]] == [2], sc)
gi = dv.grants_of(VIEW, "joerg-desk", [{"grant": "bak01-05", "source": "bak01-05"},
                                        {"grant": "neu", "source": "x", "rw": True}])
ok("Instanzseite: Freigabe mit Modus und Ankunft; eine, die die Ansicht nicht kennt, 'unbekannt'",
   gi[0]["mode"] == "ro" and gi[0]["arrived"] == "angekommen"
   and gi[1]["mode"] == "rw" and gi[1]["arrived"] == "unbekannt", gi)


def refused(verb, form):
    try:
        dv.request_args(verb, form, VIEW)
        return False
    except ValueError:
        return True


ok("Formatieren: falsche UUID, eine Rohplatte, keine Art -> abgelehnt",
   refused("format", {"disk": "leer", "mode": "luks", "confirm_uuid": "falsch"})
   and refused("format", {"disk": "bak01", "mode": "luks", "confirm_uuid": "24A5-9455"})
   and refused("format", {"disk": "leer", "mode": "", "confirm_uuid": "AAAA-1111"}))
ok("Formatieren: richtig zurueckgetippt (Gross/klein egal) -> Auftrag",
   dv.request_args("format", {"disk": "leer", "mode": "luks", "confirm_uuid": "aaaa-1111"},
                   VIEW) == {"disk": "leer", "mode": "luks", "confirm_uuid": "aaaa-1111"})
ok("Archiv: eine Nummer, nie ein Pfad",
   refused("archive-add", {"disk": "bak01", "nr": "Vertraulich/x.tar", "name": "q"})
   and dv.request_args("archive-add", {"disk": "bak01", "nr": "2", "name": "bak01-09",
                                       "index_on": "bak02", "recursive": "1"}, VIEW)
   == {"disk": "bak01", "nr": 2, "name": "bak01-09", "recursive": True, "index_on": "bak02"})
ok("Freigeben: unbekannte Instanz, schlechter Name, Eigentuemer keine Zahl -> abgelehnt",
   refused("grant", {"source": "bak01-05", "instance": "fremd", "as": "g"})
   and refused("grant", {"source": "bak01-05", "instance": "webtop", "as": "../g"})
   and refused("grant", {"source": "bak01-05", "instance": "webtop", "as": "g", "owner": "abc"}))
ok("Freigeben: Auftrag mit Zahl als Eigentuemer",
   dv.request_args("grant", {"source": "bak01-06", "instance": "webtop", "as": "roh",
                             "rw": "1", "owner": "1000"}, VIEW)
   == {"source": "bak01-06", "instance": "webtop", "as": "roh", "rw": True,
       "optional": False, "owner": 1000})
ok("Uebernehmen: nur ein angestecktes Geraet, nur raw|work",
   refused("adopt", {"uuid": "DDDD", "name": "x", "role": "raw"})
   and refused("adopt", {"uuid": "CCCC-3333", "name": "x", "role": "admin"})
   and dv.request_args("adopt", {"uuid": "CCCC-3333", "name": "stick", "role": "work"},
                       VIEW) == {"uuid": "CCCC-3333", "name": "stick", "role": "work"})
ok("Entziehen: nur eine Freigabe, die es gibt",
   refused("revoke", {"instance": "webtop", "grant": "bak01-05"})
   and dv.request_args("revoke", {"instance": "joerg-desk", "grant": "bak01-05"}, VIEW)
   == {"instance": "joerg-desk", "grant": "bak01-05"})
ok("leere Ansicht bricht nicht", dv.disks(None) == [] and dv.sources({}) == []
   and dv.scans(None) == {} and dv.grants_of(None, "x", None) == [])

# ----------------------------------------------------------- the pages
try:
    import app as portal                                      # noqa: E402
except ImportError as e:
    print(f"NOT RUN  die Seiten: {e} (Flask steht im Portal-Abbild)")
    print(f"{fails} FAIL" if fails else "PARTIAL PASS (Seiten nicht geprueft)")
    sys.exit(1 if fails else 2)

TMP = tempfile.mkdtemp(prefix="oaap-disk-page-")
VF = os.path.join(TMP, "disk-view.json")
portal.DISK_VIEW_FILE = VF
json.dump(VIEW, open(VF, "w"))
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []
mg.CTX["queue"] = lambda rid, name, payload, wait: QUEUED.append(dict(payload, _rid=rid))

WHO = {"role": "server_admin", "roles": {"server_admin"}, "tenant": "", "host": None}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}

print("=== wer, und wo ===")
r = c.get("/datentraeger", headers=H)
t = r.get_data(as_text=True)
ok("der Betreiber am Knoten sieht Platten, Quellen, Freigaben, das Fremdgeraet",
   r.status_code == 200 and "bak01" in t and "bak01-05" in t and "joerg-desk" in t
   and "CCCC-3333" in t, t[-800:])
ok("der Menuepunkt Datenträger steht da", 'href="/datentraeger"' in t)
ok("abgezogene Platte: der Satz und ein Auswerfen-Knopf",
   "Das Gerät ist weg" in t and t.count('action="/datentraeger/eject"') == 3)
ok("Archiv anlegen nur fuer die geoeffnete Platte, waehlbar nur Nr. 2",
   "Archiv als Quelle anlegen: bak01" in t and "Archiv als Quelle anlegen: leer" not in t
   and t.count('name="nr"') == 1 and 'value="2"' in t)
ok("Formatieren: nur 'leer' steht zur Wahl", 'action="/datentraeger/format"' in t
   and t.count("(UUID AAAA-1111)") == 1 and "(UUID 24A5-9455)" not in t)
for who, role, roles in (("Verwalter", "tenant_admin", {"tenant_admin"}),
                         ("Mitglied", "", {"user"})):
    WHO.update(role=role, roles=roles, tenant="t-a")
    codes = [c.get("/datentraeger", headers=H).status_code,
             c.post("/datentraeger/open", headers=H, data={"disk": "leer"}).status_code]
    mt = c.get("/kohorten", headers=H).get_data(as_text=True) if role else ""
    ok(f"{who}: 403 an jeder Tuer, nichts im Spool, kein Menuepunkt",
       codes == [403, 403] and not QUEUED and 'href="/datentraeger"' not in mt, codes)
WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host="t-kunde")
codes = [c.get("/datentraeger", headers=H).status_code,
         c.post("/datentraeger/open", headers=H, data={"disk": "leer"}).status_code]
ok("am Ort eines Mandanten gibt es die Seite nicht (404), auch nicht fuer den Betreiber",
   codes == [404, 404] and not QUEUED, codes)
WHO.update(host=None)

print("=== Auftraege ===")
r = c.post("/datentraeger/open", headers=H, data={"disk": "leer"})
ok("Oeffnen: genau ein Auftrag `disk`/open, zurueck zur Seite mit Job",
   r.status_code == 302 and len(QUEUED) == 1 and QUEUED[0]["action"] == "disk"
   and QUEUED[0]["op"] == "open" and QUEUED[0]["args"] == {"disk": "leer"}
   and "/datentraeger?job=" in r.headers["Location"], QUEUED)
QUEUED.clear()
r = c.post("/datentraeger/format", headers=H,
           data={"disk": "leer", "mode": "luks", "confirm_uuid": "falsch"})
ok("Formatieren mit falscher UUID: nichts im Spool, der Satz auf der Seite",
   not QUEUED and "err=" in r.headers["Location"], r.headers.get("Location"))
r = c.post("/datentraeger/archive-add", headers=H,
           data={"disk": "bak01", "nr": "../../etc", "name": "x"})
ok("Archiv mit Pfad statt Nummer: nichts im Spool", not QUEUED)
r = c.post("/datentraeger/forget", headers=H, data={"disk": "leer"})
ok("Vergessen gibt es auf der Seite nicht", not QUEUED and r.status_code == 302)
r = c.post("/datentraeger/open", data={"disk": "leer"},
           headers=dict(H, Origin="https://evil.example"))
ok("fremde Herkunft: 403, nichts im Spool", r.status_code == 403 and not QUEUED)

jid = "a" * 32
os.makedirs(os.path.join(TMP, "jobs", jid))
json.dump({"id": jid, "op": "eject", "tenant": "", "by": "betreiber"},
          open(os.path.join(TMP, "jobs", jid, "meta.json"), "w"))
json.dump({"id": jid, "ok": False, "message": "'bak01' is busy: root 4242 m bash"},
          open(os.path.join(TMP, "jobs", jid, "result.json"), "w"))
t = c.get(f"/datentraeger?job={jid}", headers=H).get_data(as_text=True)
ok("die Antwort des Knotens steht oben (wer die Platte festhaelt)",
   "is busy: root 4242" in t, t[:600])

portal.DISK_VIEW_FILE = os.path.join(TMP, "gibt-es-nicht.json")
t = c.get("/datentraeger", headers=H).get_data(as_text=True)
ok("Knoten ohne Ansicht: sagt, wie man einrichtet, statt eine leere Seite",
   "noch nicht\neingerichtet" in t and "install-disk-tools.sh" in t, t[-900:])
portal.DISK_VIEW_FILE = VF

print("=== die Instanzseite ===")
src = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
           encoding="utf-8").read()
m = re.search(r"\{% if i\.disk_grants %\}.*?\{% endif %\}", src, re.S)
html = (portal.app.jinja_env.from_string(m.group(0)).render(i={"disk_grants": gi})
        if m else "")
ok("Verwaltung: 'Datenträger: bak01-05 (ro) angekommen, neu (rw)', ohne Knopf",
   "bak01-05" in html and "(ro)" in html and "angekommen" in html and "(rw)" in html
   and "<form" not in html, html)
ok("die Zeile steht im Reiter Verwaltung", bool(m) and src.rfind(
   '<section class="panel', 0, m.start()) == src.rfind(
   "<section class=\"panel {{ 'active' if tab == 'verwaltung' }}\">", 0, m.start()))

print("ALL PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
