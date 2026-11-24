"""Der Ereignisbus, Stufe 1.2 (oaap.events.bus 0.1, RFC-0127).

Drei Teile ohne Container:
  1. die reinen Regeln (services/twin/bus_model.py): Typnamen, Manifestblock,
     Schema-Teilmenge, Pruefung einer Veroeffentlichung, Umschlag, Freigaben;
  2. die Tuer (services/twin/bus_door.py) mit Flasks Testclient bis zur
     Datenbank: wer sprechen darf, was abgelehnt wird, und dass ein vom
     Absender gesetztes `source`/`id`/`time` nicht durchkommt;
  3. der Knoten (appctl.py): Katalogdatei aus der Registry, Schluessel- und
     Tuerzeilen im Gateway.

Was hier NICHT geprueft wird: Postgres (Ablage, Feed, Rate) und der echte
Weg Gateway -> identity -> Zwilling -- das misst Stufe 1.2 auf oaap-test.

Aufruf: python3 test/test_events_bus.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-events-bus-")
TWIN = os.path.join(HERE, "..", "platform", "services", "twin")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, TWIN)

import bus_model as bm                                         # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


SCHEMA = {"type": "object", "required": ["case", "to"], "additionalProperties": False,
          "properties": {"case": {"type": "string", "maxLength": 40},
                         "from": {"type": ["string", "null"]},
                         "to": {"type": "string", "enum": ["open", "in_progress", "done"]},
                         "n": {"type": "integer", "minimum": 0}}}
FILES = {"events/s.json": json.dumps(SCHEMA).encode()}


def manifest(ev, app_id="probe-pub"):
    return {"app": {"id": app_id}, "events": ev}


def decl(ev, app_id="probe-pub", files=FILES):
    return bm.declaration(manifest(ev, app_id), files.get)


PUB = {"type": "app.probepub.case.statuschanged.v1", "title": "Status", "schema": "events/s.json"}

print("\n=== Typnamen ===")
ok("ein guter Name", bm.type_error("app.vorgaenge.case.statuschanged.v1") == "")
ok("zu wenige Ebenen, Grossbuchstaben, ohne Hauptversion -> abgelehnt",
   bm.type_error("app.x.v1") and bm.type_error("app.X.case.changed.v1")
   and bm.type_error("app.x.case.changed"))
ok("eine App veroeffentlicht nur unter app.<id ohne Bindestrich>.",
   bm.own_type_error("app.probepub.a.b.v1", "probe-pub") == ""
   and bm.own_type_error("app.other.a.b.v1", "probe-pub")
   and bm.own_type_error("oaap.twin.object.changed.v1", "probe-pub")
   and bm.own_type_error("ext.sap.bp.changed.v1", "probe-pub"))

print("\n=== Manifestblock ===")
err, entry = decl({"publishes": [PUB], "subscribes": [
    {"type": "app.vorgaenge.case.statuschanged.v1", "why": "Rueckmeldung"}]})
ok("gueltiger Block -> Eintrag mit Schema, audience tenant als Vorgabe",
   err == "" and entry["publishes"][0]["schema"] == SCHEMA
   and entry["publishes"][0]["audience"] == "tenant"
   and entry["subscribes"][0]["addressed_only"] is False, err)
ok("kein Block -> ('', None)", bm.declaration({"app": {"id": "x"}}, FILES.get) == ("", None))
for label, ev, frag in [
    ("unbekannter Schluessel im Block", {"publish": []}, "only 'publishes'"),
    ("fremder Namensraum", {"publishes": [dict(PUB, type="app.other.a.b.v1")]}, "own namespace"),
    ("Typ doppelt", {"publishes": [PUB, PUB]}, "twice"),
    ("Titel fehlt", {"publishes": [dict(PUB, title="")]}, "title"),
    ("Schema fehlt im Paket", {"publishes": [dict(PUB, schema="events/x.json")]}, "not in the package"),
    ("Schema-Pfad nach oben", {"publishes": [dict(PUB, schema="../x.json")]}, "relative path"),
    ("listed ohne Liste", {"publishes": [dict(PUB, audience="listed")]}, "listed"),
    ("listed bei tenant", {"publishes": [dict(PUB, listed=["a-b"])]}, "only with audience"),
    ("Wunsch ohne Begruendung", {"subscribes": [{"type": PUB["type"]}]}, "why"),
    ("unbekannte audience", {"publishes": [dict(PUB, audience="all")]}, "audience"),
]:
    e, _ = decl(ev)
    ok(f"abgelehnt: {label}", frag in e, e)
bad = {"events/s.json": json.dumps({"type": "object", "oneOf": []}).encode()}
e, _ = decl({"publishes": [PUB]}, files=bad)
ok("Schema mit Schluesselwort ausserhalb der Teilmenge -> abgelehnt, nicht still ungeprueft",
   "oneOf" in e and "subset" in e, e)
e, _ = decl({"publishes": [PUB]}, files={"events/s.json": b'{"type": "array"}'})
ok("data muss oben ein Objekt sein", "object at the top" in e, e)
e, _ = decl({"publishes": [PUB]}, files={"events/s.json": b"{nicht json"})
ok("kein JSON -> abgelehnt mit Pfad", "events/s.json" in e and "not JSON" in e, e)

print("\n=== Schema-Pruefung ===")
ok("passt", bm.validate({"case": "V-1", "to": "open", "from": None}, SCHEMA) is None)
ok("Pflichtfeld fehlt -> Pfad", bm.validate({"case": "V-1"}, SCHEMA) == ("data.to", "required"))
ok("enum", bm.validate({"case": "V-1", "to": "weg"}, SCHEMA)[0] == "data.to")
ok("zusaetzliches Feld bei additionalProperties false",
   bm.validate({"case": "V-1", "to": "open", "x": 1}, SCHEMA) == ("data.x", "not allowed here"))
ok("bool ist keine Zahl", bm.validate({"case": "V-1", "to": "open", "n": True}, SCHEMA)[0] == "data.n")
ok("minimum", bm.validate({"case": "V-1", "to": "open", "n": -1}, SCHEMA)[0] == "data.n")

print("\n=== Veroeffentlichen pruefen ===")
st, why, acc = bm.check_publish({"type": PUB["type"], "subject": "V-1",
                                 "data": {"case": "V-1", "to": "open"},
                                 "source": "urn:fake", "id": "X", "time": "1999",
                                 "oaaptenant": "fremd"}, entry)
ok("202, und angenommen wird NUR type/subject/data -- source, id, time, oaaptenant fallen weg",
   st == 202 and set(acc) == {"type", "subject", "data", "to"}, (st, why, acc))
for label, body, code in [
    ("kein Objekt", [], 400),
    ("oaapto bei einem nicht adressierten Typ", {"type": PUB["type"], "subject": "a",
                                                  "oaapto": "instance:x"}, 400),
    ("nicht erklaerter Typ", {"type": "app.probepub.other.thing.v1", "subject": "a"}, 403),
    ("subject fehlt", {"type": PUB["type"], "data": {"case": "a", "to": "open"}}, 400),
    ("subject mit Steuerzeichen", {"type": PUB["type"], "subject": "a\nb"}, 400),
    ("data kein Objekt", {"type": PUB["type"], "subject": "a", "data": [1]}, 400),
    ("data gegen das Schema", {"type": PUB["type"], "subject": "a", "data": {"case": "a"}}, 422),
    ("data zu gross", {"type": PUB["type"], "subject": "a",
                       "data": {"case": "a", "to": "open", "x": "y" * 70000}}, 413),
]:
    ok(f"{code}: {label}", bm.check_publish(body, entry)[0] == code,
       bm.check_publish(body, entry)[:2])
err_a, entry_a = decl({"publishes": [dict(PUB, audience="addressed")]})
GOOD_A = {"type": PUB["type"], "subject": "a", "data": {"case": "a", "to": "open"}}
ok("adressierter Typ ohne oaapto -> 400", bm.check_publish(GOOD_A, entry_a)[0] == 400)
ok("adressierter Typ, oaapto nicht 'instance:<name>' -> 400",
   bm.check_publish(dict(GOOD_A, oaapto="t1-sub"), entry_a)[0] == 400
   and bm.check_publish(dict(GOOD_A, oaapto="instance:../x"), entry_a)[0] == 400)
st, _w, acc_a = bm.check_publish(dict(GOOD_A, oaapto="instance:t1-sub"), entry_a)
ok("adressierter Typ mit oaapto -> 202, der Empfaenger als `to`", st == 202 and acc_a["to"] == "t1-sub")
ok("ein nicht adressierter Typ hat to None", acc["to"] is None)

print("\n=== Umschlag und Kennung ===")
u1, u2 = bm.ulid(1700000000000, b"\0" * 10), bm.ulid(1700000000001, b"\0" * 10)
ok("ULID: 26 Zeichen, nach Zeit sortierbar", len(u1) == 26 and u1 < u2 and
   len(bm.ulid()) == 26)
ev = bm.envelope({"id": u1, "type": PUB["type"], "source_instance": "t1-pub",
                  "subject": "V-1", "time": "2026-11-24T12:00:00.000Z",
                  "data": {"case": "V-1", "to": "open"}}, "T-ID", "probe-pub", "oaap-aas:X")
ok("CloudEvents 1.0: Pflichtattribute, Quelle von der Plattform, Erweiterungen",
   ev["specversion"] == "1.0" and ev["source"] == "urn:oaap:tenant:T-ID:instance:t1-pub"
   and ev["oaaptenant"] == "T-ID" and ev["oaaporigin"] == "app:probe-pub"
   and ev["oaapsemantic"] == "oaap-aas:X" and ev["datacontenttype"] == "application/json"
   and ev["dataschema"] == f"urn:oaap:events:schema:probe-pub:{PUB['type']}", ev)
eva = bm.envelope({"id": u1, "type": PUB["type"], "source_instance": "t1-pub", "subject": "V-1",
                   "time": "2026-11-24T12:00:00.000Z", "data": {}, "oaapto": "t1-sub"}, "T", "p")
ok("adressiertes Ereignis traegt oaapto als instance:<name>", eva.get("oaapto") == "instance:t1-sub"
   and "oaapto" not in ev)
ok("Attributnamen nach CloudEvents: a-z0-9, hoechstens 20 Zeichen",
   all(k.isalnum() and k.islower() and len(k) <= 20 for k in ev))

print("\n=== Freigaben ===")
_, sub_entry = decl({"subscribes": [{"type": PUB["type"], "why": "w"}]}, app_id="probe-sub")
cat = {"instances": {
    "t1-pub": dict(entry, tenant="T1", app_id="probe-pub"),
    "t1-sub": dict(sub_entry, tenant="T1", app_id="probe-sub"),
    "t2-sub": dict(sub_entry, tenant="T2", app_id="probe-sub"),
    "t1-nix": {"tenant": "T1", "app_id": "nix", "publishes": [], "subscribes": []}}}
ok("Freigabe im selben Mandanten: erlaubt", bm.grant_error(cat, "T1", "t1-sub", PUB["type"]) == "")
ok("Abonnent in einem anderen Mandanten (kein Veroeffentlicher dort): abgelehnt",
   "no instance of this tenant publishes" in bm.grant_error(cat, "T2", "t2-sub", PUB["type"]))
ok("Instanz eines fremden Mandanten unter diesem Mandanten: abgelehnt",
   "not an instance of this tenant" in bm.grant_error(cat, "T1", "t2-sub", PUB["type"]))
ok("ohne erklaerten Wunsch: abgelehnt",
   "does not declare" in bm.grant_error(cat, "T1", "t1-nix", PUB["type"]))
_, listed = decl({"publishes": [dict(PUB, audience="listed", listed=["andere-app"])]})
cat2 = {"instances": dict(cat["instances"], **{"t1-pub": dict(listed, tenant="T1", app_id="probe-pub")})}
ok("listed fuer eine andere App: abgelehnt",
   "listed for other apps" in bm.grant_error(cat2, "T1", "t1-sub", PUB["type"]))

ok("Empfaenger muss im Mandanten sein und den Typ erklaeren",
   bm.addressee_error(cat["instances"]["t1-sub"], "T1", {"tenant": "T1"}, PUB["type"]) == ""
   and "no instance of this tenant" in bm.addressee_error(cat["instances"]["t2-sub"], "T1",
                                                           {"tenant": "T2"}, PUB["type"])
   and "does not declare" in bm.addressee_error(cat["instances"]["t1-nix"], "T1",
                                                {"tenant": "T1"}, PUB["type"])
   and "no instance" in bm.addressee_error(None, "T1", None, PUB["type"]))

print("\n=== Die Tuer (bus_door, ohne Datenbank) ===")
apps = tempfile.mkdtemp(prefix="oaap-events-apps-")
json.dump({"instances": {"t1-pub": {"tenant": "T1", "app_id": "probe-pub"},
                         "t1-sub": {"tenant": "T1", "app_id": "probe-sub"}}},
          open(os.path.join(apps, "registry.json"), "w"))
json.dump(cat, open(os.path.join(apps, "events-catalog.json"), "w"))
from flask import Flask                                        # noqa: E402
import bus_door                                                # noqa: E402
fapp = Flask("t")
CONNS = []
bus_door.register(fapp, lambda t: CONNS.append(t) or None, apps)
c = fapp.test_client()


def post(user, src, body, **kw):
    h = {"X-OAAP-User": user, "X-OAAP-Source-Instance": src}
    h.update(kw.pop("headers", {}))
    return c.post("/bus/v1/", data=json.dumps(body), headers=h, content_type="application/json")


good = {"type": PUB["type"], "subject": "V-1", "data": {"case": "V-1", "to": "open"}}
ok("Person statt Instanz -> 403", post("joerg", "t1-pub", good).status_code == 403)
ok("Schluessel einer anderen Instanz auf dieser Seite -> 403",
   post("instance:t1-sub", "t1-pub", good).status_code == 403)
ok("ohne Quellkopfzeile -> 403", post("instance:t1-pub", "", good).status_code == 403)
ok("unbekannte Instanz -> 403", post("instance:weg", "weg", good).status_code == 403)
r = post("instance:t1-pub", "t1-pub", dict(good, type="app.probepub.x.y.v1"))
ok("nicht erklaerter Typ -> 403 mit Satz", r.status_code == 403 and "declares" in r.get_json()["error"])
r = post("instance:t1-pub", "t1-pub", dict(good, data={"case": "V-1"}))
ok("Schema verletzt -> 422 mit Pfad", r.status_code == 422 and "data.to" in r.get_json()["error"])
ok("bis hierhin keine Datenbank gefragt", CONNS == [])
r = post("instance:t1-pub", "t1-pub", good)
ok("gueltig, aber Mandant ohne Zwilling -> 409, erst jetzt die Datenbank gefragt",
   r.status_code == 409 and CONNS == ["T1"], (r.status_code, CONNS))
r = c.post("/bus/v1/", data=json.dumps({"case": "V-1", "to": "open"}),
           headers={"X-OAAP-User": "instance:t1-pub", "X-OAAP-Source-Instance": "t1-pub",
                    "ce-type": PUB["type"], "ce-subject": "V-1"})
ok("Binaermodus (ce-* Kopfzeilen) wird gelesen", r.status_code == 409, r.get_json())
r = c.get("/bus/v1/feed", headers={"X-OAAP-User": "instance:t1-sub",
                                   "X-OAAP-Source-Instance": "t1-sub"})
ok("Feed ohne Zwilling: leer, kein Fehler", r.status_code == 200 and r.get_json()["events"] == [])
print("  -- der Eintrag, wer wen adressieren darf (0.2)")
r = c.get("/bus/v1/relate?from=t1-sub&to=t1-pub", headers={"X-OAAP-User": "instance:t1-pub"})
ok("relate: nur fuer die einreichende Instanz selbst (fremder Schluessel -> 403)", r.status_code == 403)
r = c.get("/bus/v1/relate?from=t1-sub&to=weg", headers={"X-OAAP-User": "instance:t1-sub"})
ok("relate: unbekanntes Ziel -> 403", r.status_code == 403)
r = c.get("/bus/v1/relate?from=t1-sub&to=t1-pub", headers={"X-OAAP-User": "instance:t1-sub"})
ok("relate: richtig, Mandant ohne Zwilling -> 200, die Einreichung geht weiter",
   r.status_code == 200, r.status_code)
r = c.get("/bus/v1/feed?limit=x", headers={"X-OAAP-User": "instance:t1-sub",
                                           "X-OAAP-Source-Instance": "t1-sub"})
ok("Feed: limit keine Zahl -> 400", r.status_code == 400)

print("\n=== Der Knoten (appctl) ===")
import appctl as a                                             # noqa: E402
ok("appctl liest dieselbe Datei wie der Zwilling", a.bus_model.declaration is not None
   and a.bus_model.__file__.endswith(os.path.join("twin", "bus_model.py")))
import inspect                                                 # noqa: E402
ok("validate_manifest prueft den Block schon ohne Paket (Schemas als leeres Objekt)",
   "bus_model.declaration(m" in inspect.getsource(a.validate_manifest))
pkg = tempfile.mkdtemp(prefix="oaap-events-pkg-")
os.makedirs(os.path.join(pkg, "events"))
open(os.path.join(pkg, "events", "s.json"), "wb").write(FILES["events/s.json"])
got = a.events_declared(manifest({"publishes": [PUB]}), pkg)
ok("events_declared liest das Schema aus dem Paket", got["publishes"][0]["schema"] == SCHEMA)
try:
    a.events_declared(manifest({"publishes": [dict(PUB, schema="events/x.json")]}), pkg)
    ok("events_declared: fehlendes Schema bricht ab", False)
except SystemExit:
    ok("events_declared: fehlendes Schema bricht die Installation ab", True)
a.is_rehearsal = lambda inst: bool((inst or {}).get("rehearsal"))
reg = {"instances": {
    "t1-pub": {"tenant": "T1", "app_id": "probe-pub", "version": "0.1.0", "events": got},
    "t1-old": {"tenant": "T1", "app_id": "old", "version": "1.0.0"},
    "t1-reh": {"tenant": "T1", "app_id": "probe-pub", "version": "0.1.0", "events": got,
               "rehearsal": {"of": "t1-pub"}}}}
os.makedirs(a.APPS_DIR, exist_ok=True)
a.events_catalog_write(reg)
catw = json.load(open(a.EVENTS_CATALOG))
ok("Katalog: nur Instanzen mit Block, nie eine Generalprobe",
   set(catw["instances"]) == {"t1-pub"} and catw["instances"]["t1-pub"]["tenant"] == "T1")
ok("Katalog ist lesbar wie die Registry (0644, keine Geheimnisse)",
   os.name == "nt" or (os.stat(a.EVENTS_CATALOG).st_mode & 0o777) == 0o644)
ok("OAAP_EVENTS_KEY ist ein Instanzschluessel (folgt der Instanz, RFC-0062)",
   "OAAP_EVENTS_KEY" in a.INSTANCE_KEY_VARS and "OAAP_EVENTS_KEY" in a.RESERVED_ENV
   and "OAAP_EVENTS_URL" in a.RESERVED_ENV)
ok("Scope und Adresse", a.events_scope("t1-pub") == "oaap.events:t1-pub"
   and a.events_url(8123) == "http://gateway:8123/platform/events"
   or a.events_url(8123).endswith(":8123/platform/events"))
ok("Gateway-Zeilen bekommen eine neue Formatnummer (Seiten werden neu geschrieben)",
   a.CASES_BLOCK_FORMAT >= 11)  # 12 since RFC-0126
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
door = src[src.index("if events_wanted(me) and not login_only"):src.index("# RFC-0069 stage 2: on a `vorgaenge` site only")]
ok("die Tuer fragt identity nach genau diesem Scope, ohne Rollen, und reicht an twin:8000",
   "events_scope(scope" in door and "/verify?roles=" in door and "reverse_proxy twin:8000" in door
   and "rewrite * /bus/v1{uri}" in door and "SOURCE_HEADERS" in door)
ok("... in `route`, damit forward_auth vor dem Umschreiben fragt", '"\\t\\troute {"' in door)
ok("... nur `/` und `/feed`: der Eintrag /bus/v1/relate ist von dort nie erreichbar",
   '("/platform/events", "/platform/events/", "/platform/events/feed")' in door
   and "/platform/events/*" not in door)
sub = src[src.index('lines.append("\\thandle /platform/cases/submit {")'):src.index("# oaap.events.bus 0.1 (RFC-0127 D3): the instance's events door")]
ok("die Vorgangstuer fragt nach identity den Zwilling und nennt Einreicher und Posteingang",
   sub.index("forward_auth identity:8000") < sub.index("forward_auth twin:8000")
   and "/bus/v1/relate?from={scope}&to={inbox}" in sub and "copy_headers" not in sub.split("forward_auth twin:8000")[1].split("source =")[0])
insts = {"t1-app": {"tenant": "T1", "app_id": "probe", "channel": "production"},
         "t1-vg": {"tenant": "T1", "app_id": "vorgaenge", "channel": "production",
                   "container": "oaap-app-t1-vg", "svc_port": 8080}}
a.instance_tenant_ref = lambda i: i.get("tenant")
ok("cases_target_key nennt den Schluessel des Posteingangs",
   a.cases_target_key(insts, "t1-app") == "t1-vg" and a.cases_target_key({}, "x") == "")

print("\n=== Der Push (0.3) ===")
e, ent = decl({"subscribes": [{"type": PUB["type"], "why": "w"}], "endpoint": "/oaap/events"})
ok("endpoint wird uebernommen", e == "" and ent["endpoint"] == "/oaap/events", e)
for label, ev, frag in [
    ("endpoint ohne Abo", {"publishes": [PUB], "endpoint": "/x"}, "only an app that subscribes"),
    ("endpoint mit Query", {"subscribes": [{"type": PUB["type"], "why": "w"}], "endpoint": "/x?a=1"}, "events.endpoint"),
    ("endpoint relativ", {"subscribes": [{"type": PUB["type"], "why": "w"}], "endpoint": "x"}, "events.endpoint"),
    ("endpoint mit ..", {"subscribes": [{"type": PUB["type"], "why": "w"}], "endpoint": "/a/../b"}, "events.endpoint"),
]:
    e2, _ = decl(ev)
    ok(f"abgelehnt: {label}", frag in e2, e2)
body = b'{"id":"x"}'
sig = bm.sign("geheim", 1700000000, body)
ok("Signatur: die App prueft sie mit ihrem Geheimnis",
   bm.verify("geheim", sig, body, now=1700000010) and not bm.verify("anders", sig, body, now=1700000010)
   and not bm.verify("geheim", sig, body + b" ", now=1700000010)
   and not bm.verify("geheim", sig, body, now=1700000000 + 301) and not bm.verify("geheim", "", body))
ok("Backoff: 2^n, hoechstens eine Stunde", bm.backoff(1) == 2 and bm.backoff(3) == 8 and bm.backoff(30) == 3600)
w = bm.pending_conditions([{"type": "a.b.c.v1", "subject_prefix": None, "granted_at": "T0"}], ["x.y.z.v1"], "me")
ok("Feed und Push lesen dieselbe Regel: Freigabe nicht adressiert, adressiert an mich, nie meine eigenen",
   w[0].startswith("source_instance <> %s") and "oaapto IS NULL AND type = %s" in w[0]
   and "oaapto = %s AND type = ANY(%s)" in w[0] and w[1] == ["me", "a.b.c.v1", "T0", "me", ["x.y.z.v1"]])
ok("nichts erreichbar -> None", bm.pending_conditions([], [], "me") is None)

import datetime as _dt                                         # noqa: E402
try:
    import psycopg2                                            # noqa: F401
except ImportError:                                            # lokal ohne Treiber: Platzhalter
    import types as _types
    _pg = _types.ModuleType("psycopg2")
    _pg.extras = _types.ModuleType("psycopg2.extras")
    _pg.Error = Exception
    sys.modules["psycopg2"], sys.modules["psycopg2.extras"] = _pg, _pg.extras
import bus_push as bp                                          # noqa: E402


class FakeCur:
    """Genug Postgres fuer push_instance: bus_deliveries, bus_grants, bus_events."""
    def __init__(self, events, grants=()):
        self.events, self.grants, self.deliv, self._r = list(events), list(grants), {}, None

    def execute(self, sql, args=()):
        q = " ".join(sql.split())
        if q.startswith("SELECT * FROM bus_deliveries"):
            self._r = [dict(self.deliv[args[0]])] if args[0] in self.deliv else []
        elif q.startswith("INSERT INTO bus_deliveries"):
            row = {"instance": args[0], "last_seq": max([e["seq"] for e in self.events] or [0]),
                   "failures": 0, "next_at": None, "stopped_at": None}
            self.deliv[args[0]] = row
            self._r = [dict(row)]
        elif q.startswith("SELECT type, subject_prefix, granted_at FROM bus_grants"):
            self._r = [{"type": g, "subject_prefix": None, "granted_at": "2026-01-01T00:00:00Z"}
                       for g in self.grants]
        elif q.startswith("SELECT seq, id"):
            after, me = args[0], args[1]
            types = [a for a in args[2:-1] if isinstance(a, str) and a != me]
            self._r = [e for e in self.events if e["seq"] > after and e["source_instance"] != me
                       and ((e.get("oaapto") is None and e["type"] in types)
                            or (e.get("oaapto") == me))][:args[-1]]
        elif q.startswith("UPDATE bus_deliveries SET last_seq"):
            d = self.deliv[args[1]]
            d.update(last_seq=args[0], failures=0, next_at=None)
        elif q.startswith("UPDATE bus_deliveries SET failures = %s, stopped_at"):
            d = self.deliv[args[2]]
            d.update(failures=args[0], stopped_at=_dt.datetime.now(_dt.timezone.utc), last_error=args[1])
        elif q.startswith("UPDATE bus_deliveries SET failures"):
            d = self.deliv[args[3]]
            d.update(failures=args[0], last_error=args[1],
                     next_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=args[2]))
        else:
            raise AssertionError(q)

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return self._r


def ev_row(seq, typ=PUB["type"], src="t1-pub", to=None):
    return {"seq": seq, "id": f"ID{seq}", "type": typ, "source_instance": src, "source_app": "probe-pub",
            "subject": "s", "time": "2026-11-24T12:00:00.000Z", "data": {"n": seq}, "semantic": None,
            "oaapto": to}


SENT = []


def send_ok(inst, body, headers):
    SENT.append((inst, json.loads(body), headers))
    return 200, ""


cur = FakeCur([ev_row(1), ev_row(2)], grants=[PUB["type"]])
ok("erstes Mal: der Push beginnt beim neuesten Ereignis (die Vergangenheit steht im Zwilling, D13)",
   bp.push_instance(cur, "T1", "t1-sub", ent, "geheim", send=send_ok) == "nothing" and SENT == [])
cur.events += [ev_row(3), ev_row(4), ev_row(5, src="t1-sub"), ev_row(6, typ="app.x.y.z.v1"),
               ev_row(7, to="t1-sub"), ev_row(8, to="t1-other")]
what = bp.push_instance(cur, "T1", "t1-sub", ent, "geheim", send=send_ok)
ok("zugestellt in Reihenfolge: freigegebene, adressierte an mich; nie eigene, fremde Typen, an andere adressierte",
   what == "delivered 3" and [x[1]["id"] for x in SENT] == ["ID3", "ID4", "ID7"], (what, [x[1]["id"] for x in SENT]))
inst, ce, h = SENT[0]
ok("ein Ereignis je POST, CloudEvents strukturiert, signiert, an die Instanz",
   inst == "t1-sub" and ce["specversion"] == "1.0" and h["Content-Type"] == "application/cloudevents+json"
   and bm.verify("geheim", h["X-OAAP-Signature"], json.dumps(ce, separators=(",", ":")).encode())
   and h["X-OAAP-Event-Id"] == "ID3")
ok("Cursor steht hinter dem letzten zugestellten", cur.deliv["t1-sub"]["last_seq"] == 7)
SENT.clear()
cur.events += [ev_row(9), ev_row(10)]
flaky = iter([(500, "boom"), (200, "")])


def send_flaky(inst, body, headers):
    SENT.append(json.loads(body)["id"])
    return next(flaky)


what = bp.push_instance(cur, "T1", "t1-sub", ent, "geheim", send=send_flaky)
d = cur.deliv["t1-sub"]
ok("Fehler: Cursor bleibt, Zaehler 1, Wartezeit gesetzt, das naechste wartet",
   what.startswith("failed (1)") and d["last_seq"] == 7 and d["failures"] == 1 and d["next_at"] is not None
   and SENT == ["ID9"], (what, d))
ok("waehrend der Wartezeit: nichts gesendet", bp.push_instance(cur, "T1", "t1-sub", ent, "g", send=send_flaky) == "waiting")
d["next_at"] = None
what = bp.push_instance(cur, "T1", "t1-sub", ent, "geheim",
                        send=lambda i, b, h: (SENT.append(json.loads(b)["id"]) or (200, "")))
ok("danach: dasselbe Ereignis noch einmal (mindestens einmal), dann weiter",
   what == "delivered 2" and SENT[1:] == ["ID9", "ID10"] and d["failures"] == 0, (what, SENT))
cur.events.append(ev_row(11))
for i in range(bp.bm.PUSH_FAILURES_MAX):
    d["next_at"] = None
    what = bp.push_instance(cur, "T1", "t1-sub", ent, "geheim", send=lambda i, b, h: (0, "ConnectionRefused"))
ok(f"nach {bp.bm.PUSH_FAILURES_MAX} Fehlern in Folge: gestoppt, mit Grund",
   what.startswith("stopped after 10") and d["stopped_at"] is not None and "ConnectionRefused" in d["last_error"], what)
ok("gestoppt: nichts mehr, bis resume", bp.push_instance(cur, "T1", "t1-sub", ent, "g", send=send_ok) == "stopped")

print("\n=== Der Push am Knoten ===")
csrc = open(os.path.join(HERE, "..", "platform", "docker-compose.yml"), encoding="utf-8").read()
ok("Dienst `bus`: Zwilling-Image, bus_push.py, die Plattformdateien nur lesend",
   "\n  bus:\n" in csrc and '"bus_push.py"' in csrc)
dsrc = open(os.path.join(TWIN, "Dockerfile"), encoding="utf-8").read()
ok("Dockerfile kopiert bus_push.py", "bus_push.py" in dsrc)
hsrc = inspect.getsource(a.write_internal_health_caddy)
ok("interner Listener: /e/<name> auf den erklaerten Pfad, ohne Identitaets-Kopfzeilen, nie Generalprobe",
   'handle /e/{name}' in hsrc and "request_header -{h}" in hsrc and "IDENTITY_HEADERS" in hsrc
   and "is_rehearsal(inst)" in hsrc)
ok("OAAP_EVENTS_SECRET ist reserviert", "OAAP_EVENTS_SECRET" in a.RESERVED_ENV)
a.load_env = lambda name, inst=None: {"OAAP_EVENTS_SECRET": "s3cr3t"} if name == "t1-push" else {}
got_ep = dict(got, endpoint="/oaap/events", subscribes=[{"type": PUB["type"], "why": "w", "addressed_only": False}])
reg2 = {"instances": {"t1-push": {"tenant": "T1", "app_id": "probe-pub", "version": "0.3.0", "events": got_ep},
                      "t1-pub": reg["instances"]["t1-pub"]}}
a.events_catalog_write(reg2)
sec = json.load(open(a.EVENTS_SECRETS))
ok("events-secrets.json: das Geheimnis der Instanz mit endpoint, nur dieser",
   sec == {"t1-push": "s3cr3t"}, sec)
ok("... nur fuer root lesbar (0600)", os.name == "nt" or (os.stat(a.EVENTS_SECRETS).st_mode & 0o777) == 0o600)
ok("Katalog traegt den endpoint", json.load(open(a.EVENTS_CATALOG))["instances"]["t1-push"]["endpoint"] == "/oaap/events")

print("\n=== Was der Bus unter einem Namen haelt, folgt der Instanz ===")
SQL = []
a._store_running = lambda: True
a._twin_schema_exists = lambda schema: True
a._events_psql = lambda tid, sql: SQL.append((tid, sql)) or ""
T1 = "9edc585b-2161-4f8b-8a03-8dada10becbf"
a.events_follow_instance({"tenant": T1}, "evp-b")
ok("Entfernen: Freigaben widerrufen, Cursor und Eintraege geloescht",
   SQL and SQL[-1][0] == T1 and "UPDATE {s}.bus_grants SET revoked_at = now()" in SQL[-1][1]
   and "DELETE FROM {s}.bus_deliveries WHERE instance = 'evp-b'" in SQL[-1][1]
   and "DELETE FROM {s}.bus_relations WHERE publisher = 'evp-b' OR addressee = 'evp-b'" in SQL[-1][1], SQL[-1:])
a.events_follow_instance({"tenant": T1}, "evp-b", "evp-neu")
ok("Umbenennen: alle drei ziehen auf den neuen Namen um",
   "SET instance = 'evp-neu' WHERE instance = 'evp-b'" in SQL[-1][1]
   and "SET publisher = 'evp-neu'" in SQL[-1][1] and "SET addressee = 'evp-neu'" in SQL[-1][1])
a.events_follow_instance({"tenant": T1}, "x'; DROP TABLE y; --")
ok("Namen werden als SQL-Literal gesetzt", "'x''; DROP TABLE y; --'" in SQL[-1][1])
n0 = len(SQL)
a.events_follow_instance({"tenant": "kein-mandant"}, "evp-b")
ok("ohne gueltigen Mandanten: nichts", len(SQL) == n0)
rsrc = inspect.getsource(a.remove_instance) + inspect.getsource(a.rename_instance)
ok("remove_instance und rename_instance rufen es", rsrc.count("events_follow_instance(") == 2)

print()
print("all events-bus checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
