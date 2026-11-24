"""RFC-0127 stage 3.2 (oaap.events.bus 0.9): receivers in, CloudEvents.

  1. bus_model: the declaration, reading a request (structured, batched,
     binary), mapping a sender's type to our ext. type, the envelope
  2. the catalogue with the receivers: grants, the overview, AsyncAPI
  3. twin_inbound.store_events (stand-in cursor): once per (source, id)
  4. the route, the schema, the CLI

    py -3 test/test_events_in.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "platform", "services")
TWIN = os.path.join(SERVICES, "twin")
sys.path.insert(0, SERVICES)
sys.path.insert(0, TWIN)
import bus_model as bm                                         # noqa: E402

fails = 0


def ok(what, cond, info=""):
    global fails
    print(("PASS  " if cond else "FAIL  ") + what + ("" if cond else f"   {info}"))
    if not cond:
        fails += 1


SAP_TYPE = "sap.s4.beh.businesspartner.v1.BusinessPartner.Changed.v1"
OUR = "ext.s4erp.businesspartner.changed.v1"
DECL = {"types": [{"type": OUR, "title": "Geschaeftspartner geaendert (S/4HANA)", "from": SAP_TYPE,
                   "schema": {"type": "object", "properties": {"BusinessPartner": {"type": "string"}},
                              "required": ["BusinessPartner"]}}]}

print("=== Die Erklaerung eines Empfaengers ===")
ok("gueltig: eigener ext.-Typ, Absendertyp in from, Schema", bm.receiver_events_error(DECL, "s4-erp") == "")
ok("Typ ausserhalb ext.<client>. -> nein", "declares types under 'ext.s4erp." in
   bm.receiver_events_error({"types": [dict(DECL["types"][0], type="ext.anders.x.y.v1")]}, "s4-erp"))
ok("app.-Typ -> nein", bm.receiver_events_error({"types": [dict(DECL["types"][0], type="app.x.y.z.v1")]}, "s4-erp"))
ok("ohne Titel -> nein", "title" in bm.receiver_events_error(
    {"types": [{"type": OUR, "from": SAP_TYPE}]}, "s4-erp"))
two = {"types": [DECL["types"][0], dict(DECL["types"][0], type="ext.s4erp.businesspartner.created.v1")]}
ok("zwei Typen aus demselben Absendertyp -> nein", "maps to two" in bm.receiver_events_error(two, "s4-erp"))
ok("Schema ausserhalb des Teilumfangs -> nein", "schema" in bm.receiver_events_error(
    {"types": [dict(DECL["types"][0], schema={"type": "object", "oneOf": []})]}, "s4-erp"))
ok("ohne from: Absender schickt unseren Typ", bm.receiver_events_error(
    {"types": [{"type": OUR, "title": "x"}]}, "s4-erp") == "")

print("\n=== Eine Anfrage lesen ===")


class H(dict):
    def get(self, k, d=None):
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return d


SAP = {"specversion": "1.0", "type": SAP_TYPE, "source": "/default/sap.s4.beh/244572008",
       "id": "a823e884-5edc-4194-a81a-f3a3632417ee", "time": "2026-11-24T10:00:00Z",
       "datacontenttype": "application/json", "data": {"BusinessPartner": "1000667"}}
evs, why = bm.parse_cloudevents(json.dumps(SAP).encode(), H({"Content-Type": "application/cloudevents+json"}))
ok("strukturiert (application/cloudevents+json)", evs == [SAP], why)
evs, why = bm.parse_cloudevents(json.dumps(SAP).encode(), H({"Content-Type": "application/json"}))
ok("strukturiert auch als application/json mit specversion", evs == [SAP])
evs, why = bm.parse_cloudevents(json.dumps([SAP, dict(SAP, id="2")]).encode(),
                                H({"Content-Type": "application/cloudevents-batch+json"}))
ok("Batch (application/cloudevents-batch+json)", len(evs) == 2)
evs, why = bm.parse_cloudevents(json.dumps({"BusinessPartner": "1000667"}).encode(), H({
    "Content-Type": "application/json", "ce-specversion": "1.0", "ce-type": SAP_TYPE,
    "ce-source": "/default/sap.s4.beh/244572008", "ce-id": "b-1"}))
ok("binaer (ce-* Kopfzeilen, Body = data)", evs and evs[0]["type"] == SAP_TYPE
   and evs[0]["data"] == {"BusinessPartner": "1000667"} and evs[0]["id"] == "b-1", evs)
ok("kein CloudEvent -> Satz", bm.parse_cloudevents(b'{"a":1}', H({}))[0] is None)
ok("kein JSON -> Satz", bm.parse_cloudevents(b"x", H({}))[0] is None)
ok("leerer Batch -> Satz", bm.parse_cloudevents(b"[]", H({"Content-Type": "application/cloudevents-batch+json"}))[0] is None)

print("\n=== Abbilden auf den eigenen Typ ===")
a, why = bm.map_cloudevent(SAP, DECL["types"])
ok("SAP-Ereignis -> ext.s4erp.businesspartner.changed.v1, Original in ext, Betreff = id",
   a and a["type"] == OUR and a["ext"] == {"source": SAP["source"], "type": SAP_TYPE, "id": SAP["id"]}
   and a["subject"] == SAP["id"] and a["data"] == {"BusinessPartner": "1000667"}, (a, why))
ok("mit subject: der des Absenders", bm.map_cloudevent(dict(SAP, subject="BP-1000667"), DECL["types"])[0]["subject"]
   == "BP-1000667")
ok("unerklaerter Typ -> Satz", "not a type this receiver declares" in bm.map_cloudevent(
    dict(SAP, type="sap.other.v1"), DECL["types"])[1])
ok("Schema verletzt -> Satz mit Pfad", "data.BusinessPartner" in bm.map_cloudevent(
    dict(SAP, data={}), DECL["types"])[1])
ok("specversion 0.3 -> Satz", "specversion" in bm.map_cloudevent(dict(SAP, specversion="0.3"), DECL["types"])[1])
ok("ohne id -> Satz", "id" in bm.map_cloudevent({k: v for k, v in SAP.items() if k != "id"}, DECL["types"])[1])
ok("data als Text -> {value: ...}", bm.map_cloudevent(dict(SAP, data="x"), [{"type": OUR, "from": SAP_TYPE}])[0]["data"]
   == {"value": "x"})
row = {"id": "01R", "type": OUR, "source_instance": "receiver:s4-erp/bp", "subject": SAP["id"],
       "time": "2026-11-24T10:00:01.000Z", "data": a["data"], "ext": a["ext"]}
env = bm.envelope(row, "T1", "client:s4-erp")
ok("Umschlag: source unser Empfaenger, origin der Client, oaapext* vom Absender",
   env["source"] == "urn:oaap:tenant:T1:receiver:s4-erp/bp" and env["oaaporigin"] == "client:s4-erp"
   and env["oaapextsource"] == SAP["source"] and env["oaapexttype"] == SAP_TYPE
   and env["oaapextid"] == SAP["id"] and env["type"] == OUR, env)

print("\n=== Der Katalog mit den Empfaengern ===")
CAT = {"instances": {"t1-bestand": {"tenant": "T1", "app_id": "bestand", "publishes": [],
                                    "subscribes": [{"type": OUR, "why": "Partner aktuell halten"}]}}}
RECV = [{"principal": "client:s4-erp", "name": "bp", "events": json.dumps(DECL)}]
full = bm.with_receivers(CAT, "T1", RECV)
ok("Empfaenger als Herausgeber receiver:<client>/<name>, Publikum tenant",
   full["instances"]["receiver:s4-erp/bp"]["publishes"][0]["audience"] == "tenant")
ok("Instanz, die den Typ erklaert: freigebbar", bm.grant_error(full, "T1", "t1-bestand", OUR) == "")
ok("ohne Empfaenger: niemand veroeffentlicht ihn", "no instance" in bm.grant_error(CAT, "T1", "t1-bestand", OUR))
ok("an einen Client weiterreichbar (freigebbar)", bm.client_grant_error(
    full, "T1", {"weiter": {"tenant": "T1"}}, "client:weiter", OUR) == "")
ov = bm.overview(full, {}, "T1", [], {}, {})
ok("Uebersicht: Empfaenger nicht als App-Karte, sondern in receiver_types mit from",
   [i["instance"] for i in ov["instances"]] == ["t1-bestand"]
   and ov["receiver_types"] == [{"receiver": "s4-erp/bp", "type": OUR, "title": DECL["types"][0]["title"],
                                 "from": SAP_TYPE, "personal": False}], ov["receiver_types"])
doc = bm.asyncapi(full, {}, "T1", None, "inv1")
ok("AsyncAPI des Mandanten: Nachricht mit Herausgeber receiver:…, Datenschema", doc["components"]["messages"][OUR]
   ["x-oaap-publisher"] == "receiver:s4-erp/bp"
   and doc["components"]["messages"][OUR]["payload"]["properties"]["data"] == DECL["types"][0]["schema"])

print("\n=== Ablage: einmal je (Quelle, id) ===")
try:
    import psycopg2  # noqa: F401
    import twin_inbound as tsi
except ImportError:
    tsi = None
    print("SKIP  psycopg2 fehlt")
if tsi is not None:
    class Cur:
        seen, ins = set(), []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            self.last = (sql, args)
            if sql.startswith("INSERT"):
                Cur.ins.append(args)
                Cur.seen.add((args[2], json.loads(args[6])["source"], json.loads(args[6])["id"]))

        def fetchone(self):
            sql, args = self.last
            return {"x": 1} if tuple(args) in Cur.seen else None

    class Conn:
        def cursor(self):
            return Cur()

    acc = [a, dict(a, ext=dict(a["ext"], id="2"))]
    st = tsi.store_events(Conn(), {"name": "bp"}, "s4-erp", acc)
    ok("zwei neue -> zwei gespeichert", st == (2, 0) and Cur.ins[0][2] == "receiver:s4-erp/bp"
       and Cur.ins[0][3] == "client:s4-erp", (st, Cur.ins[:1]))
    st = tsi.store_events(Conn(), {"name": "bp"}, "s4-erp", acc)
    ok("dieselben noch einmal -> angenommen, nicht wieder gespeichert", st == (0, 2))
    try:
        tsi._events("client:s4-erp", {"name": "x"})
        ok("ohne mapping und ohne events -> abgelehnt", False)
    except Exception as e:                                       # noqa: BLE001
        ok("ohne mapping und ohne events -> abgelehnt", "mapping" in str(e))

print("\n=== Route, Schema, CLI ===")
app_src = open(os.path.join(TWIN, "app.py"), encoding="utf-8").read()
ok("Eingang: CloudEvents-Zweig vor der Twin-Abbildung, alle oder keins, 202 mit stored/replayed",
   "bm.parse_cloudevents(raw, request.headers)" in app_src and "tsi.store_events(conn, rec, label, accepted)" in app_src
   and app_src.find("bm.parse_cloudevents") < app_src.find("root = json.loads(raw)", app_src.find("def v1_inbound")))
ok("ein Empfaenger nur mit events braucht kein 'writes'",
   'body.get("mapping") is None and body.get("events") is not None' in app_src)
ac = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("Schema: ALTER receivers steht HINTER CREATE receivers (frischer Mandant; der Container-Test fand es)",
   ac.find('CREATE TABLE IF NOT EXISTS "{schema}".receivers') < ac.find('ALTER TABLE "{schema}".receivers')
   and ac.find('CREATE TABLE IF NOT EXISTS "{schema}".bus_events') < ac.find('ALTER TABLE "{schema}".bus_events')
   and ac.find('CREATE TABLE IF NOT EXISTS "{schema}".webhooks') < ac.find('ALTER TABLE "{schema}".webhooks'))
ok("Schema: receivers.events, bus_events.ext mit Index",
   'ALTER TABLE "{schema}".receivers ADD COLUMN IF NOT EXISTS events jsonb' in ac
   and 'ALTER TABLE "{schema}".bus_events ADD COLUMN IF NOT EXISTS ext jsonb' in ac)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
try:
    import appctl as _a
    body = _a._portal_site_body()
    i_in = body.index("\thandle /twin/v1/inbound/* {")
    i_all = body.index("\thandle {")
    blk = body[i_in:body.index("\t}", i_in) + 1]
    ok("externe Seite: /twin/v1/inbound/* vor dem Anmelde-Fang, ohne forward_auth, Kopfzeilen weg, 1 MB "
       "(gemessen: die externe Adresse antwortete dem Absender mit der Anmeldeseite)",
       i_in < i_all and not any("forward_auth" in x for x in blk)
       and "\t\trequest_header -X-OAAP-User" in blk and "\t\t\tmax_size 1MB" in blk
       and "\t\treverse_proxy twin:8000" in blk, blk)
    import inspect as _ins
    mig = _ins.getsource(_a.cmd_migrate_tenant_places)
    ok("Update: schon geschriebene externe Seiten ohne den Eingang werden einmal neu geschrieben",
       '"handle /twin/v1/inbound/* {" not in have' in mig and "refresh_generated_sites()" in mig)
except ImportError as e:
    print(f"SKIP  appctl: {e}")
ok("CLI-Freigaben sehen die Empfaenger-Typen", ac.count("bus_model.with_receivers(cat, tid, _events_receivers(tid))") == 2)
for f in ("bus_door.py", "bus_push.py", "hooks.py"):
    ok(f"{f}: ext wird mitgelesen (oaapext* im Umschlag)", "oaapto, ext FROM bus_events" in
       open(os.path.join(TWIN, f), encoding="utf-8").read())

print()
print("all way-in checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
