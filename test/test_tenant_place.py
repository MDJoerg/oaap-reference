#!/usr/bin/env python3
"""Der Mandant ist ein Ort (RFC-0042 T1/T2).

`<kuerzel>.<knoten>` war die Stelle im Namensschema, die beschrieben und
nie gefuellt war: Instanzen heissen laengst `<instanz>.<kuerzel>.<knoten>`.
Jetzt antwortet dort das **Portal** -- dieselbe Sitzung, dieselben
Rollen- und Gruppenfilter, dieselbe Mandantengrenze. Eine zweite App
haette vier Dinge neu erwerben muessen, die das Portal hat und die
getestet sind; genau so entstehen die teuersten Fehler dieses Codes.

Neu ist nur ein Satz: **Der Host bestimmt, was das Portal zeigt.** Und
der Zusatz, der ihn erst sicher macht: Nennt der Host einen Mandanten,
den dieser Knoten nicht hat, wird **nichts** ausgeliefert -- nicht die
Sicht des Betreibers.

Geprueft werden beide Seiten:

    appctl  schreibt die Site je Mandant (auch fuer fruehere Kuerzel),
            und fuer den Standard-Mandanten keine -- sein Ort IST die
            Wurzel.
    portal  verengt das Launchpad am Host, fuer JEDEN, auch fuer einen
            server_admin, und lehnt einen unbekannten Ort ab.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_tenant_place.py
"""
import argparse
import contextlib
import io as _io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-place-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

HOST = "oaap.joomp.de"
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def make_tenant(label, title):
    with contextlib.redirect_stdout(_io.StringIO()):
        m.cmd_tenant(argparse.Namespace(
            action="create", name=label, target=None, title=title,
            account="", account_name="", grace_days=30, yes=True, count=50))
    return m.tenant_by_label(label)[0]


default_id = m.ensure_default_tenant()
cls_id = make_tenant("cls", "Clausen Systeme")
hbvp_id = make_tenant("hbvp", "HB Vereinsportal")

print("")
print("appctl: die Adresse entsteht an einer Stelle")

places = dict(m.tenant_place_hosts(HOST))
ok("jeder Mandant bekommt seinen Ort",
   places.get(f"cls.{HOST}") == cls_id
   and places.get(f"hbvp.{HOST}") == hbvp_id)
ok("der Standard-Mandant bekommt keinen",
   not any(t == default_id for t in places.values()),
   "sein Kuerzel ist die Abwesenheit eines Kuerzels -- sein Ort ist "
   "die Wurzel, und dort antwortet das Portal seit jeher")
ok("ohne externen Namen gibt es gar keine Orte",
   m.tenant_place_hosts("") == [])

with contextlib.redirect_stdout(_io.StringIO()):
    m.cmd_tenant(argparse.Namespace(
        action="rename", name="cls", target="clausen", title="", account="",
        account_name="", grace_days=30, yes=True, count=50))
after = dict(m.tenant_place_hosts(HOST))
ok("nach dem Umbenennen antwortet der neue Ort",
   after.get(f"clausen.{HOST}") == cls_id)
ok("und der alte noch dazu",
   after.get(f"cls.{HOST}") == cls_id,
   "ein Verein, der gerade umbenannt wurde, findet seine Seite weiter "
   "(RFC-0026 3.3) -- sonst waere ein Umbenennen ein Adressbruch")

print("")
print("appctl: das Gateway schreibt sie auch")

import json as _json
os.makedirs(os.path.dirname(m.EXTERNAL_FILE), exist_ok=True)
_io.open(m.EXTERNAL_FILE, "w", encoding="utf-8").write(
    _json.dumps({"host": HOST}))
with contextlib.redirect_stdout(_io.StringIO()):
    m.write_external_caddy()
caddy = _io.open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"),
                 encoding="utf-8").read()
ok("es gibt eine Site fuer den Ort", f"https://clausen.{HOST} {{" in caddy)
ok("und eine fuer den frueheren Namen", f"https://cls.{HOST} {{" in caddy)
ok("sie zeigt auf dasselbe Portal wie die Wurzel",
   caddy.count("reverse_proxy portal:8000")
   >= 4 * (1 + len(m.tenant_place_hosts(HOST))),
   "derselbe Rumpf, kein zweiter Weg -- sonst traegt eine der beiden "
   "Stellen irgendwann eine Regel nicht")

print("")
print("Der Umstieg schreibt sie in einen bestehenden Knoten")

# Eine Caddy-Datei wie vor RFC-0042: die Wurzel, aber kein Ort.
plain = "\n".join(l for l in caddy.splitlines()
                  if not any(f"://{f} {{" in l or f"# {f} ->" in l
                             for f, _t in m.tenant_place_hosts(HOST)))
_io.open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), "w",
         encoding="utf-8").write(plain + "\n")
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_migrate_tenant_places(None)
first = buf.getvalue()
ok("der Umstieg holt die fehlenden Orte nach",
   f"clausen.{HOST}" in first, first)
after_file = _io.open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"),
                      encoding="utf-8").read()
ok("und sie stehen danach in der Datei",
   f"https://clausen.{HOST} {{" in after_file)
buf2 = _io.StringIO()
with contextlib.redirect_stdout(buf2):
    m.cmd_migrate_tenant_places(None)
ok("beim zweiten Lauf ist er still", buf2.getvalue().strip() == "",
   buf2.getvalue())

print("")
print("Portal: der Host verengt, und er weicht nicht aus")

import app as p                                                # noqa: E402

TENANTS = m.load_tenants()
INSTANCES = {
    "cls-viewer": {"tenant": cls_id, "name": "viewer", "app_name": "Viewer",
                   "version": "1.0", "channel": "production", "routes": [],
                   "svc_port": 8000, "port": 8101},
    "hbvp-board": {"tenant": hbvp_id, "name": "board", "app_name": "Board",
                   "version": "1.0", "channel": "production", "routes": [],
                   "svc_port": 8000, "port": 8102},
    "studio": {"tenant": default_id, "name": "studio", "app_name": "Studio",
               "version": "1.0", "channel": "production", "routes": [],
               "svc_port": 8000, "port": 8103},
}
p.load_tenants = lambda: TENANTS
p.load_instances = lambda: INSTANCES
p.external_host = lambda: HOST
p.default_tenant_id = lambda: default_id
p.resolve_tenant = lambda ref: (ref or default_id) if (ref or default_id) in TENANTS else None
p.multi_tenant = lambda: True
p.caller_record = lambda: {"tenant": default_id}
p.setup_done = lambda: True
# identity laeuft hier nicht; die Gruppen sind nicht der Gegenstand
# dieses Tests -- der Gruppenfilter hat seinen eigenen.
p.identity_users = lambda: []
p.caller_groups = lambda: set()
p.app.config["TESTING"] = True
client = p.app.test_client()


def names_on(host, roles="server_admin", user="chef"):
    r = client.get("/", headers={"Host": host, "X-OAAP-Roles": roles,
                                 "X-OAAP-User": user})
    return r.status_code, r.get_data(as_text=True)


code, body = names_on(HOST)
ok("an der Wurzel sieht der Betreiber weiter alles",
   code == 200 and "Viewer" in body and "Board" in body and "Studio" in body,
   f"{code}: {body[-300:]}")

code, body = names_on(f"clausen.{HOST}")
ok("am Ort eines Mandanten nur dessen Apps",
   code == 200 and "Viewer" in body and "Board" not in body
   and "Studio" not in body, f"{code}: {body[-300:]}")
ok("auch fuer einen server_admin", "Board" not in body,
   "wer den Ort eines Mandanten aufruft, hat dessen Seite verlangt, "
   "nicht die des Knotens -- die ist einen Hostnamen entfernt")
ok("und der Name des Mandanten steht darauf",
   "Clausen Systeme" in body,
   "nicht als Zierde, sondern als Anker (T3): eine Seite, die jemandem "
   "gehoert, sagt wem")

code, body = names_on(f"cls.{HOST}")
ok("der fruehere Name fuehrt an denselben Ort",
   code == 200 and "Viewer" in body and "Board" not in body, code)

code, body = names_on(f"gibtsnicht.{HOST}")
ok("ein unbekannter Ort liefert nichts", code == 404, code)
ok("und weicht NICHT auf die Betreibersicht aus",
   "Studio" not in body and "Board" not in body,
   "das waere genau die Verwechslung, gegen die es die Aufloesungs"
   "regeln gibt")

other = client.get("/instances", headers={
    "Host": f"gibtsnicht.{HOST}", "X-OAAP-Roles": "server_admin",
    "X-OAAP-User": "chef"})
ok("und zwar auf dem GANZEN Portal, nicht nur auf dem Launchpad",
   other.status_code == 404,
   "sonst antwortet ein Ort, den es nicht gibt, ueberall ausser dort, "
   "wo jemand hingeschaut hat")

code, body = names_on(f"default.{HOST}")
ok("der Standard-Mandant hat keinen ausgeschriebenen Ort", code == 404,
   "sein Ort ist die Wurzel; ein Host, der sein Kuerzel ausspricht, "
   "ist nicht seine Adresse")

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
