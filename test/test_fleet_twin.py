#!/usr/bin/env python3
"""Die Flotte im Zwilling (RFC-0085 Nachtrag A2, Stufe 4 Datenteil;
RFC-0086 Stufe 2; oaap.data.model 0.2 semantische IDs).

Teil 1 laeuft ueberall (rein): das Typmodell der Flotte ist ein gueltiges
data_model, jede semantische ID ist eine IRI, eine IRDI oder unsere
Kurzform, die Kurzform nennt nie eine Domain, die Abbildung Statusdokument
-> Gruppen, das Urteil je Knoten mit `since`.

Teil 2 braucht eine echte PostgreSQL (TS_DSN, wie test_timeseries_api.py):
die Tabellen des Zwillings kommen aus appctl._twin_ensure_tables selbst,
nicht aus einer Abschrift. Festgehalten wird:

- ein Statusdokument legt Knoten, Broker und Instanzen an, mit Gruppen,
  Beziehungen und Zeitreihen;
- dasselbe Dokument noch einmal schreibt KEINE Zeile -- auch keine
  Beziehung (der Fehler, den twin_write 0.5 behebt);
- eine verschwundene Instanz bleibt, mit Zustand `removed`;
- ein Knoten, der schweigt, sendet nichts -- sein Zustand kommt trotzdem
  aus dem Urteil des Hosts;
- eine Gruppe der Menschen (`owner.*`) schreibt die Flotte nie;
- faellt die Datenbank aus, bleibt alles ausstehend und kommt beim
  naechsten Mal an.

Aufruf: py -3 test/test_fleet_twin.py            (Teil 1)
        TS_DSN=... python3 test/test_fleet_twin.py   (Teil 1 + 2, oaap-test)
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TWIN = os.path.join(HERE, "..", "platform", "services", "twin")
sys.path.insert(0, TWIN)
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-fleet-twin-"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or detail == "" else f"  -- {str(detail)[:700]}"))


import fleet_model as fm          # noqa: E402
import fleet_twin as ft           # noqa: E402
import fleet_watch                # noqa: E402
import semantic                   # noqa: E402
import appctl as a                # noqa: E402

print("=== Teil 1: das Typmodell ===")
dm = fm.data_model()
ok("das data_model der Flotte besteht die Manifestpruefung (auch die semantischen IDs)",
   a.validate_data_model_sections({"data_model": dm}) == [],
   a.validate_data_model_sections({"data_model": dm}))
ids = [e["semantic_id"] for k in dm for e in dm[k] if e.get("semantic_id")]
ok("jede semantische ID ist gueltig", all(semantic.valid(i) for i in ids),
   [i for i in ids if not semantic.valid(i)])
own = [i for i in ids if i.startswith(semantic.PREFIX)]
ok("unsere IDs sind gespeichert ohne Domain (Kurzform oaap-aas:)",
   own and not any("objid" in i or "http" in i for i in own), own[:3])
ok("IDTA-IDs stehen woertlich drin (Software Nameplate, IsPartOf, Contact)",
   fm.IDTA_SOFTWARE in ids and fm.IDTA_IS_PART_OF in ids and fm.IDTA_CONTACT in ids)
ok("Kurzform -> objid.info, Standard-IDs unveraendert",
   semantic.expand("oaap-aas:HostCare/0/1") == "https://objid.info/oaap/aas/HostCare/0/1"
   and semantic.expand(fm.IDTA_SOFTWARE) == fm.IDTA_SOFTWARE)
os.environ["OAAP_SEMANTIC_BASE"] = "https://example.org/sem/"
ok("die Domain ist eine Einstellung (OAAP_SEMANTIC_BASE), keine Migration",
   semantic.expand("oaap-aas:HostCare/0/1") == "https://example.org/sem/HostCare/0/1")
del os.environ["OAAP_SEMANTIC_BASE"]
ok("ungueltige IDs werden abgewiesen (Manifestpruefung)",
   a.validate_data_model_sections({"data_model": {"attribute_types": [
       {"key": "X", "value_type": "text", "semantic_id": "irgendwas"}]}}) != [])
keys = [e["key"] for e in dm["attribute_types"]] + [e["key"] for e in dm["relation_types"]]
ok("jeder Attribut- und Beziehungsschluessel beginnt mit 'fleet' (knotenweit eindeutig)",
   all(k.startswith("fleet") for k in keys))
gk = [g["key"] for g in dm["group_types"]]
ok("Gruppenschluessel eindeutig ueber alle drei Objekttypen", len(gk) == len(set(gk)), gk)
ok("eine Aenderung der semantischen ID ist zerstoerend, ihr Hinzufuegen additiv",
   a.type_change_kind({"key": "K", "semantic_id": "oaap-aas:A/0/1"},
                      {"key": "K", "semantic_id": "oaap-aas:A/0/2"}) == "destructive"
   and a.type_change_kind({"key": "K"}, {"key": "K", "semantic_id": "oaap-aas:A/0/1"}) == "additive")

print("=== Teil 1: Statusdokument -> Gruppen ===")
DOC = {
    "schema": "oaap.fleet.status/0.9", "node": "oaap.joomp.de", "alias": "oaapx01",
    "place": "de/strato", "tags": ["kunde-cls"], "platform_version": "0.1.277",
    "profiles": ["store", "broker"],
    "host": {"measured": "2026-11-14T10:00:00Z", "os": "Debian GNU/Linux 13 (trixie)",
             "kernel_running": "6.12.116", "kernel_installed": "6.12.116",
             "reboot_required": False, "auto_updates": "ok", "reboot_window": "04:15",
             "advisories": {"open": 3, "oldest": "2026-11-01", "stale": False}},
    "core": [{"name": "identity", "state": "ok"}, {"name": "deploy-worker", "state": "warn"}],
    "instances": [{"instance": "market", "app": "market", "version": "0.3.0",
                   "channel": "production", "state": "ok", "origin": "git",
                   "address": "market.oaap.joomp.de", "auto_address": "market.oaap.joomp.de",
                   "auto_state": "ok", "secret": "NIE"}],
    "names": [{"name": "oaap.joomp.de", "state": "ok"}, {"name": "x.joomp.de", "state": "warn"}],
    "public_ip": "203.0.113.7",
    "attention": [{"kind": "dns_drift", "detail": "x.joomp.de: points elsewhere"}],
}
g = ft.node_groups(DOC)
ok("Software: Version, OS, Profile", g["oaap.software"] == {
    "platformVersion": "0.1.277", "os": "Debian GNU/Linux 13 (trixie)",
    "profiles": "broker, store"}, g["oaap.software"])
ok("Platzierung: Alias, Ort, Tags, Adresse",
   g["oaap.placement"]["alias"] == "oaapx01" and g["oaap.placement"]["place"] == "de/strato"
   and g["oaap.placement"]["tags"] == "kunde-cls", g["oaap.placement"])
ok("Kerndienste: Liste und schlechtester Zustand",
   g["oaap.services"] == {"coreServices": "deploy-worker warn, identity ok", "coreWorst": "warn"},
   g["oaap.services"])
ok("Pflege aus dem host-Block", g["oaap.hostcare"]["advisoriesOpen"] == 3
   and g["oaap.hostcare"]["rebootRequired"] is False, g["oaap.hostcare"])
ok("Namen: zwei, einer nicht in Ordnung", g["oaap.dns"]["namesNotOk"] == 1
   and "x.joomp.de warn" in g["oaap.dns"]["namesProblems"], g["oaap.dns"])
ok("ohne host-Block keine Pflege-Gruppe (nichts erfunden)",
   "oaap.hostcare" not in ft.node_groups({"node": "n"}))
rows = ft.instance_rows(DOC)
ok("Instanzen: Feld fuer Feld, nie ein fremdes Feld", rows[0][0] == "market"
   and "NIE" not in json.dumps(rows) and rows[0][1]["oaap.runtime"]["channel"] == "production",
   rows)
ok("Kerndienste als Textreihen", ft.core_states(DOC) == {"core.identity": "ok",
                                                        "core.deploy_worker": "warn"})
ok("ein Messpunkt", ft.metric_point({"v": 1, "node": "n", "t": "2026-11-14T10:00:00Z",
                                     "m": "cpu", "x": 12.5}) == ("cpu", "2026-11-14T10:00:00Z", 12.5))
ok("unbekannte Reihe, Text oder bool: kein Punkt",
   ft.metric_point({"m": "evil", "t": "x", "x": 1}) is None
   and ft.metric_point({"m": "cpu", "t": "2026-11-14T10:00:00Z", "x": True}) is None)
ok("Lebenszeichen: nur langsame Werte (kein seq, keine Zeit)",
   set(ft.liveness({"seq": 9, "t": "x", "interval": 60, "lost": 0}, {"state": "up"}))
   == {"state", "since", "interval", "lostTotal", "eventsDropped"})

print("=== Teil 1: das Urteil je Knoten ===")
now = 1_800_000_000
st = {"started": now - 3600, "nodes": {"n1": {"name": "n1", "alive": {"t": fleet_watch.iso(now - 30)}},
                                       "n2": {"name": "n2", "alive": {"t": fleet_watch.iso(now - 900)}}}}
v1 = fleet_watch.verdict(st, now, "hub", {})
ok("dieselbe Regel wie die Mails: n1 da, n2 schweigt",
   v1["nodes"]["n1"]["state"] == "up" and v1["nodes"]["n2"]["state"] == "silent", v1)
v2 = fleet_watch.verdict(st, now + 60, "hub", v1)
ok("`since` bleibt, solange sich der Zustand nicht aendert",
   v2["nodes"]["n2"]["since"] == v1["nodes"]["n2"]["since"])
st["nodes"]["n2"]["alive"]["t"] = fleet_watch.iso(now + 110)
v3 = fleet_watch.verdict(st, now + 120, "hub", v2)
ok("… und springt bei einem Wechsel", v3["nodes"]["n2"]["state"] == "up"
   and v3["nodes"]["n2"]["since"] == fleet_watch.iso(now + 120))
try:
    import fleet                                             # noqa: E402
    fleet.DIR = tempfile.mkdtemp(prefix="fleet-dir-")
    with open(os.path.join(fleet.DIR, "verdict.json"), "w") as f:
        json.dump({"written": fleet_watch.iso(now), "nodes": {"n1": {"state": "up"}}}, f)
    ok("frisches Urteil wird gelesen", fleet.read_verdict(now + 60) == {"n1": {"state": "up"}})
    ok("ein altes Urteil ist keins (dann: unbekannt)", fleet.read_verdict(now + 600) == {})
except ImportError as exc:
    print(f"SKIP  fleet.py braucht paho-mqtt ({exc}) -- laeuft im Abbild des Zwillings")

DSN = os.environ.get("TS_DSN", "")
if not DSN:
    print("SKIP  Teil 2: TS_DSN fehlt -- braucht eine echte PostgreSQL (oaap-test)")
    print(f"\n{'ALLE GRUEN' if not fails else str(fails) + ' FEHLER'}")
    sys.exit(1 if fails else 0)

print("=== Teil 2: die Senke gegen eine echte PostgreSQL ===")
import psycopg2                   # noqa: E402
import psycopg2.extras            # noqa: E402

import fleet_sink                 # noqa: E402
import timeseries as ts           # noqa: E402
import twin_write as tw           # noqa: E402

TID = "ops"
admin = psycopg2.connect(DSN)
admin.autocommit = True
DDL = []
real_psql = a._store_psql
a._store_psql = lambda sql, *x, **k: DDL.append(sql) or type("R", (), {"stdout": "", "returncode": 0})()
a._twin_ensure_tables(f"twin_{TID}")
a._store_psql = real_psql
with admin.cursor() as cur:
    for sch in (f"twin_{TID}", f"ts_{TID}"):
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
        cur.execute(f'CREATE SCHEMA "{sch}"')
    # the tenant's role, as _twin_ensure_schema makes it -- the DDL grants to it
    cur.execute(f"""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twin_{TID}')
                    THEN CREATE ROLE "twin_{TID}" LOGIN; END IF; END $$""")
    for sql in DDL:
        cur.execute(sql)
ok("die Tabellen des Zwillings kommen aus appctl._twin_ensure_tables", len(DDL) >= 1)


def conns(_tid):
    c = psycopg2.connect(DSN, options=f"-c search_path=twin_{TID}",
                         cursor_factory=psycopg2.extras.RealDictCursor)
    d = ts.PostgresDriver(psycopg2.connect(DSN), f"ts_{TID}")
    return c, d


d0 = conns(TID)[1]
d0.ensure_schema()
d0.ensure_partitions(time.time())
d0.conn.close()
FDIR = tempfile.mkdtemp(prefix="fleet-sink-")
with open(os.path.join(FDIR, "hub.json"), "w") as f:
    json.dump({"tenant": TID, "hub": "hubnode"}, f)
sink = fleet_sink.Sink(FDIR)
sink._conns = conns


def q(sql, *args):
    with admin.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql.replace("T.", f'"twin_{TID}".').replace("S.", f'"ts_{TID}".'), args)
        return cur.fetchall()


NOW = time.time() // 60 * 60
t0 = fleet_watch.iso(NOW - 120)
snap = {"status": {"n1": ("n1", "", DOC)},
        "nodes": {"n1": ("n1", "", {"interval": 60, "lost": 0, "seq": 5})},
        "points": [("n1", "cpu", fleet_watch.iso(NOW - 120 + 60 * i), 10.0 + i) for i in range(3)],
        "brokers": {}, "bridges": {}, "verdict": {"n1": {"state": "up", "since": t0}}}
rows, left = sink.flush(dict(snap), NOW)
ok("erster Lauf ohne Fehler", sink.last_error == "", sink.last_error)
objs = {r["type_key"]: r for r in q("SELECT type_key, title FROM T.objects")}
ok("Knoten, Broker der Zentrale und Instanz angelegt",
   set(objs) == {fm.NODE, fm.BROKER, fm.INSTANCE}, objs)
attrs = {(r["group_key"], r["attr_key"]): r["value"]
         for r in q("SELECT group_key, attr_key, value FROM T.current_attributes")}
ok("Gruppen mit registrierten Schluesseln",
   attrs.get(("oaap.software", "fleetPlatformVersion")) == "0.1.277"
   and attrs.get(("oaap.liveness", "fleetState")) == "up"
   and attrs.get(("oaap.runtime", "fleetChannel")) == "production", list(attrs)[:8])
rels = q("SELECT group_key, rel_key FROM T.current_relations")
ok("Beziehungen: Knoten meldet ueber Zentrale, Instanz laeuft auf Knoten",
   {(r["group_key"], r["rel_key"]) for r in rels}
   == {("oaap.placement", "fleetReportsThrough"), ("oaap.runtime", "fleetIsPartOf")}, rels)
series = {r["measure"]: r for r in q("SELECT id, measure, kind FROM S.series")}
ok("Reihen: cpu (Zahl), state und Kerndienste (Text)",
   series.get("cpu", {}).get("kind") == "number" and series.get("state", {}).get("kind") == "text"
   and "core.deploy_worker" in series, list(series))
before = q("SELECT (SELECT count(*) FROM T.attributes) a, (SELECT count(*) FROM T.relations) r, "
           "(SELECT count(*) FROM T.events) e")[0]
rows2, _ = sink.flush({**snap, "points": []}, NOW + 30)
after = q("SELECT (SELECT count(*) FROM T.attributes) a, (SELECT count(*) FROM T.relations) r, "
          "(SELECT count(*) FROM T.events) e")[0]
ok("dasselbe Dokument noch einmal: keine Zeile, keine Beziehung, kein Ereignis",
   rows2 == 0 and before == after, (rows2, before, after))
sink2 = fleet_sink.Sink(FDIR)
sink2._conns = conns
sink2.flush({**snap, "points": []}, NOW + 40)
ok("… auch nach einem Neustart des Dienstes (Objekte ueber den Quellschluessel gefunden)",
   len(q("SELECT id FROM T.objects")) == 3 and sink2.last_error == "", sink2.last_error)

doc2 = json.loads(json.dumps(DOC))
doc2["instances"] = []
doc2["host"]["kernel_installed"] = "6.12.117"
doc2["host"]["reboot_required"] = True
sink.flush({**snap, "status": {"n1": ("n1", "", doc2)}, "points": []}, NOW + 60)
attrs = {(r["group_key"], r["attr_key"]): r["value"]
         for r in q("SELECT group_key, attr_key, value FROM T.current_attributes")}
ok("verschwundene Instanz: bleibt, Zustand `removed`",
   attrs.get(("oaap.runtime", "fleetRunState")) == "removed")
hist = q("SELECT value FROM T.attributes WHERE attr_key = 'fleetKernelInstalled' ORDER BY id")
ok("ein geaenderter Wert: neue Zeile, die alte bleibt als Geschichte",
   [h["value"] for h in hist] == ["6.12.116", "6.12.117"], hist)

sink.flush({**snap, "status": {}, "points": [],
            "verdict": {"n1": {"state": "silent", "since": fleet_watch.iso(NOW + 90)}}}, NOW + 90)
attrs = {(r["group_key"], r["attr_key"]): r["value"]
         for r in q("SELECT group_key, attr_key, value FROM T.current_attributes")}
ok("schweigender Knoten: kein neues Lebenszeichen, Zustand trotzdem `silent`",
   attrs.get(("oaap.liveness", "fleetState")) == "silent")

nid = q("SELECT id FROM T.objects WHERE type_key = %s", fm.NODE)[0]["id"]
c, d = conns(TID)
with c:
    tw.write_group(c, str(nid), "owner.contact", "tenant:ops", "anna",
                   {"attributes": {"fleetContactName": "Anna"}})
refused = None
try:
    with c:
        tw.write_group(c, str(nid), "owner.contact", fm.ORIGIN, "fleet",
                       {"attributes": {"fleetContactName": "Bot"}})
except tw.Refused as exc:
    refused = str(exc)
c.close()
d.conn.close()
ok("die Gruppe eines Menschen schreibt die Flotte nie", refused and "belongs to" in refused, refused)

broken = fleet_sink.Sink(FDIR)
broken._conns = lambda _t: (_ for _ in ()).throw(psycopg2.OperationalError("store down"))
rows3, left3 = broken.flush({**snap, "points": [("n1", "cpu", fleet_watch.iso(NOW), 1.0)]}, NOW + 120)
ok("Datenbank weg: Fehler gemerkt, Dokument und Punkte bleiben ausstehend",
   "store down" in broken.last_error and left3["status"] and len(left3["points"]) == 1,
   (broken.last_error, left3))

snapv = {"status": {"entry1/n2": ("n2", "entry1", {**DOC, "alias": "n2", "instances": []})},
         "nodes": {}, "points": [], "brokers": {"entry1": {"queued": 4}},
         "bridges": {"entry1@hubnode": {"entry": "entry1", "hub": "hubnode", "up": True},
                     "hubnode@far": {"entry": "hubnode", "hub": "far", "up": True}},
         "verdict": {}}
sink.flush(snapv, NOW + 150)
rels = {(r["src"], r["rel_key"], r["dst"]) for r in q(
    "SELECT o.title src, r.rel_key, t.title dst FROM T.current_relations r "
    "JOIN T.objects o ON o.id = r.object_id JOIN T.objects t ON t.id = r.target_id")}
ok("hinter einem Eingang: n2 meldet ueber entry1, entry1 ist zur Zentrale gebrueckt",
   ("n2", "fleetReportsThrough", "entry1") in rels and ("entry1", "fleetBridgedTo", "hubnode") in rels,
   rels)
roles = {r["title"]: r["value"] for r in q(
    "SELECT o.title, a.value FROM T.current_attributes a JOIN T.objects o ON o.id = a.object_id "
    "WHERE a.attr_key = 'fleetRole'")}
ok("Rollen: diese Zentrale, ihr Eingang, eine fremde Zentrale, die hereinbrueckt",
   roles.get("hubnode") == "hub" and roles.get("entry1") == "entry"
   and roles.get("far") == "remote-hub", roles)
ok("die Bruecke einer fremden Zentrale wird hier nicht als eigene Bruecke verbunden",
   ("hubnode", "fleetBridgedTo", "far") not in rels, rels)
ok("Warteschlange und Bruecke als Reihen des Brokers",
   {"queued", "bridge"} <= {r["measure"] for r in q("SELECT measure FROM S.series")})

with admin.cursor() as cur:
    for sch in (f"twin_{TID}", f"ts_{TID}"):
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
print(f"\n{'ALLE GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
