"""RFC-0127 stage 2.1 (oaap.events.bus 0.4): the platform publishes.

  1. bus_model: the platform types, their check, the envelope of a platform row
  2. the twin's internal door /internal/bus/platform (Flask test client, a
     stand-in connection)
  3. identity: the item for a person, the outbox and its order, the hooks

    py -3 test/test_events_platform.py
"""
import importlib
import inspect
import json
import os
import sys
import tempfile

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


print("=== bus_model: Plattformtypen ===")
ACT, LOCK, UNL = ("oaap.identity.person.activated.v1", "oaap.identity.person.locked.v1",
                  "oaap.identity.person.unlocked.v1")
ok("drei Typen von identity, alle adressiert, Namen gueltig",
   all(bm.PLATFORM_TYPES[t]["publisher"] == "identity" and bm.PLATFORM_TYPES[t]["audience"] == "addressed"
       and bm.type_error(t) == "" and bm.schema_error(bm.PLATFORM_TYPES[t]["schema"]) == ""
       for t in (ACT, LOCK, UNL)))
good = {"publisher": "identity", "type": ACT, "subject": "u-1", "data": {"user_id": "u-1", "ref": "n17"},
        "to": "inv1-bestand"}
st, why, acc = bm.check_platform_publish(good)
ok("gueltig -> 202, Adressat uebernommen", st == 202 and acc["to"] == "inv1-bestand", why)
ok("fremder Herausgeber -> 403", bm.check_platform_publish(dict(good, publisher="twin"))[0] == 403)
ok("App-Typ ueber die Plattformtuer -> 403",
   bm.check_platform_publish(dict(good, type="app.vorgaenge.case.statuschanged.v1"))[0] == 403)
ok("ohne Adressat -> 400", bm.check_platform_publish(dict(good, to=""))[0] == 400)
ok("Adressat mit fremden Zeichen -> 400", bm.check_platform_publish(dict(good, to="x y"))[0] == 400)
ok("Zusatzfeld in data (z. B. die Adresse) -> 422",
   bm.check_platform_publish(dict(good, data={"user_id": "u-1", "email": "a@b"}))[0] == 422)
ok("locked ohne by -> 422", bm.check_platform_publish(dict(good, type=LOCK))[0] == 422)
ok("locked mit by=app -> 202",
   bm.check_platform_publish(dict(good, type=LOCK, data={"user_id": "u-1", "by": "app"}))[0] == 202)
ok("by ausserhalb app/admin -> 422",
   bm.check_platform_publish(dict(good, type=UNL, data={"user_id": "u-1", "by": "x"}))[0] == 422)
ev = bm.envelope({"id": "01X", "type": ACT, "source_instance": "platform:identity", "subject": "u-1",
                  "time": "2026-11-24T10:00:00.000Z", "data": {"user_id": "u-1"},
                  "semantic": "urn:oaap:concept:identity:person:activated", "oaapto": "inv1-bestand"},
                 "T1", "platform")
ok("Umschlag: source identity, origin platform:identity, Schema der Plattform, oaapto",
   ev["source"] == "urn:oaap:tenant:T1:identity" and ev["oaaporigin"] == "platform:identity"
   and ev["dataschema"] == "urn:oaap:events:schema:platform:" + ACT
   and ev["oaapto"] == "instance:inv1-bestand" and ev["oaapsemantic"].endswith("activated"), ev)
ev2 = bm.envelope({"id": "01Y", "type": "app.x.y.z.v1", "source_instance": "a", "subject": "s",
                   "time": "2026-11-24T10:00:00.000Z", "data": {}}, "T1", "x")
ok("App-Umschlag unveraendert", ev2["source"] == "urn:oaap:tenant:T1:instance:a"
   and ev2["oaaporigin"] == "app:x")


def decl(subs):
    return bm.declaration({"id": "probe-sub", "events": {"subscribes": subs}}, lambda p: None)


ok("eine App abonniert einen Plattformtyp", decl([{"type": ACT, "why": "freischalten"}])[0] == "")
err = decl([{"type": "oaap.identity.person.vanished.v1", "why": "x"}])[0]
ok("unbekannter oaap.-Typ beim Installieren abgelehnt", "not a platform type" in err, err)
cat = {"instances": {"s": {"tenant": "T1", "app_id": "probe-sub", "subscribes": [{"type": ACT}]}}}
ok("Freigabe fuer einen adressierten Plattformtyp: nicht noetig",
   "no grant needed" in bm.grant_error(cat, "T1", "s", ACT))
w = bm.pending_conditions([], [ACT], "s")
ok("Feed/Push: adressierte Plattformereignisse erreichen die erklaerende Instanz",
   "oaapto = %s AND type = ANY(%s)" in w[0] and w[1][0] == "s")

print("\n=== Zwilling im Umschlag (Stufe 2.2, D10) ===")
TC, TX, TR, TS = ("oaap.twin.object.created.v1", "oaap.twin.object.changed.v1",
                  "oaap.twin.object.retired.v1", "oaap.twin.object.statuschanged.v1")
ok("vier Zwilling-Typen, Herausgeber twin, Publikum tenant, Schemas im Teilumfang",
   all(bm.PLATFORM_TYPES[t]["publisher"] == "twin" and bm.PLATFORM_TYPES[t]["audience"] == "tenant"
       and bm.schema_error(bm.PLATFORM_TYPES[t]["schema"]) == "" for t in (TC, TX, TR, TS)))
O = "6f1c2d3e-0000-4000-8000-000000000001"
row = {"kind": "object.created", "object_id": O, "group_key": "", "origin": "app:bestand"}
t, subj, data = bm.twin_event(row, "bundle")
ok("object.created -> created, subject die Objekt-URN, data type+origin",
   t == TC and subj == "urn:oaap:obj:" + O and data == {"type": "bundle", "origin": "app:bestand"}, data)
t, _s, data = bm.twin_event(dict(row, kind="group.written", group_key="stock"), "bundle")
ok("group.written -> changed mit group und change", t == TX
   and data == {"type": "bundle", "group": "stock", "origin": "app:bestand", "change": "group.written"}, data)
for k in ("period.written", "object.renamed", "object.merged", "object.unmerged"):
    ok(f"{k} -> changed", bm.twin_event(dict(row, kind=k), "bundle")[0] == TX)
ok("object.retired -> retired", bm.twin_event(dict(row, kind="object.retired"), "bundle")[0] == TR)
t, _s, data = bm.twin_event(dict(row, kind="status.changed"), "bundle", {"from": "open", "to": "done"})
ok("status.changed -> statuschanged mit from/to (ohne Begruendung)", t == TS
   and data["from"] == "open" and data["to"] == "done" and "reason" not in data, data)
ok("type.created, ohne Objekt, ohne Typ, unbekannte Art -> kein Ereignis",
   bm.twin_event(dict(row, kind="type.created"), "bundle") is None
   and bm.twin_event(dict(row, object_id=None), "bundle") is None
   and bm.twin_event(row, None) is None and bm.twin_event(dict(row, kind="x.y"), "bundle") is None)
for k in ("object.created", "group.written", "object.retired", "status.changed", "object.merged"):
    ev = bm.twin_event(dict(row, kind=k, group_key="stock"), "bundle", {"from": None, "to": "x"})
    bad = bm.validate(ev[2], bm.PLATFORM_TYPES[ev[0]]["schema"])
    ok(f"{k}: data besteht das eigene Schema", bad is None, bad)
env = bm.envelope({"id": "01Z", "type": TC, "source_instance": "platform:twin", "subject": "urn:oaap:obj:" + O,
                   "time": "2026-11-24T10:00:00.000Z", "data": {"type": "bundle", "origin": "app:bestand"},
                   "semantic": "oaap-aas:Bundle/1/0"}, "T1", "platform")
ok("Umschlag: source twin, oaaporigin = Herkunft wie der Zwilling sie nennt, Semantik",
   env["source"] == "urn:oaap:tenant:T1:twin" and env["oaaporigin"] == "app:bestand"
   and env["oaapsemantic"] == "oaap-aas:Bundle/1/0" and "oaapto" not in env, env)
cat2 = {"instances": {"s": {"tenant": "T1", "app_id": "probe-sub", "subscribes": [{"type": TC}]}}}
ok("Freigabe fuer einen Zwilling-Typ moeglich (kein Herausgeber unter den Instanzen noetig)",
   bm.grant_error(cat2, "T1", "s", TC) == "")
ok("... aber nur fuer eine Instanz, die ihn erklaert", "does not declare" in bm.grant_error(cat2, "T1", "s", TX))
ok("eine App darf nicht unter oaap.twin. veroeffentlichen",
   "platform's namespace" in bm.own_type_error(TC, "probe-sub"))
import relay                                                    # noqa: E402
done = [(dict(row, id=1, type_key="bundle"), "t", None, None),
        (dict(row, id=2, kind="status.changed", type_key="bundle"), "t/status",
         None, json.dumps({"status": {"from": "a", "to": "b", "reason": "geheim"}}).encode()),
        (dict(row, id=3, kind="type.created", object_id=None, type_key=None), None, None, None),
        (dict(row, id=4, kind="object.retired", type_key="bundle"), "t", None, None)]
br = relay.bus_rows(done, {TC, TS})
ok("Relais: nur freigegebene Typen kopiert, Status mit from/to aus der Nachricht, ohne reason",
   [(b[1], b[4]["id"]) for b in br] == [(TC, 1), (TS, 2)] and br[1][3]["to"] == "b"
   and "reason" not in br[1][3], [(b[1], b[4]["id"]) for b in br])
ok("Relais: ohne Freigabe nichts", relay.bus_rows(done, set()) == [])
rs = inspect.getsource(relay.relay_tenant)
i_ins, i_wm = rs.find("INSERT INTO bus_events"), rs.find("UPDATE relay_watermark SET last_event_id")
ok("Relais: Kopie und Wasserstand in EINER Transaktion, Kopie vor dem Wasserstand (genau einmal)",
   0 < i_ins < i_wm and "'platform:twin'" in rs)
ok("Relais: Freigaben werden nur gelesen, wenn etwas veroeffentlicht wurde",
   rs.find("granted_twin_types(conn)") > rs.find("done = plan[:stop]"))

print("\n=== Die interne Tuer im Zwilling ===")
try:
    from flask import Flask
except ImportError:
    print("SKIP  flask fehlt")
    sys.exit(1 if fails else 0)
import bus_door                                                # noqa: E402

apps = tempfile.mkdtemp(prefix="oaap-bus-plat-")
json.dump({"instances": {"inv1-bestand": {"tenant": "T1", "app_id": "probe-sub"},
                         "inv1-stumm": {"tenant": "T1", "app_id": "probe-x"},
                         "fremd": {"tenant": "T2", "app_id": "probe-sub"}}},
          open(os.path.join(apps, "registry.json"), "w"))
json.dump({"instances": {"inv1-bestand": {"tenant": "T1", "app_id": "probe-sub",
                                          "subscribes": [{"type": ACT}, {"type": LOCK}]},
                         "inv1-stumm": {"tenant": "T1", "app_id": "probe-x", "subscribes": []},
                         "fremd": {"tenant": "T2", "app_id": "probe-sub",
                                   "subscribes": [{"type": ACT}]}}},
          open(os.path.join(apps, "events-catalog.json"), "w"))


class Cur:
    rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        Cur.rows.append((sql, args))


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return Cur()

    def close(self):
        pass


fapp = Flask("t")
ASKED = []
bus_door.register(fapp, lambda t: ASKED.append(t) or (Conn() if t == "T1" else None), apps)
c = fapp.test_client()
P = "/internal/bus/platform"
r = c.post(P, json=dict(good, tenant="T1"))
ok("gueltig -> 202, gespeichert", r.status_code == 202 and r.get_json()["stored"] is True, r.get_json())
sql, args = Cur.rows[-1]
ok("... in bus_events als platform:identity, source_app platform, adressiert",
   "INSERT INTO bus_events" in sql and args[2] == "platform:identity" and args[3] == "platform"
   and args[7] == "inv1-bestand" and json.loads(args[5]) == good["data"], args)
n = len(Cur.rows)
r = c.post(P, json=dict(good, tenant="T1", to="inv1-stumm"))
ok("Adressat erklaert den Typ nicht -> 200 stored false, nichts geschrieben",
   r.status_code == 200 and r.get_json()["stored"] is False and len(Cur.rows) == n)
r = c.post(P, json=dict(good, tenant="T1", to="fremd"))
ok("Adressat eines anderen Mandanten -> stored false", r.get_json()["stored"] is False
   and "tenant" in r.get_json()["why"])
r = c.post(P, json=dict(good, tenant="T1", publisher="twin"))
ok("falscher Herausgeber -> 403", r.status_code == 403)
r = c.post(P, json=dict(good, tenant="T1", type=LOCK, data={"user_id": "u-1", "by": "admin"}))
ok("locked -> 202", r.status_code == 202)
src = open(os.path.join(TWIN, "app.py"), encoding="utf-8").read()
ok("die Tuer liegt unter /internal/ -- der Plattformschluessel-Waechter des Zwillings gilt",
   'request.path.startswith("/internal/")' in src and P.startswith("/internal/"))

print("\n=== Die Portalseite Ereignisse (Stufe 2.3) ===")
OV_CAT = {"instances": {
    "inv1-pp-a": {"tenant": "T1", "app_id": "peopleprobe", "version": "0.2.2", "endpoint": "/oaap/events",
                  "publishes": [], "subscribes": [
                      {"type": ACT, "why": "da"}, {"type": TC, "why": "Zwilling"},
                      {"type": TX, "why": "Zwilling"},
                      {"type": "app.vorgaenge.case.statuschanged.v1", "why": "x", "addressed_only": True}]},
    "fremd": {"tenant": "T2", "app_id": "x", "subscribes": [{"type": TC, "why": "y"}]}}}
ov = bm.overview(OV_CAT, {"instances": {}}, "T1",
                 [{"instance": "inv1-pp-a", "type": TX, "subject_prefix": "urn:oaap:obj:1",
                   "granted_by": "anna", "granted_at": "2026-11-24T15:00:00"}],
                 {"inv1-pp-a": {"delivered": 12, "failures": 0, "stopped_at": None,
                                "last_error": None, "last_ok_at": "x"}})
st = {s["type"]: s for s in ov["instances"][0]["subscribes"]}
ok("Uebersicht: nur Instanzen des eigenen Mandanten", [i["instance"] for i in ov["instances"]] == ["inv1-pp-a"])
ok("... identity-Typ: adressiert von identity, keine Freigabe", st[ACT]["state"] == "addressed_platform"
   and st[ACT]["publisher"] == "identity" and not st[ACT]["grantable"])
ok("... Zwilling-Typ ohne Freigabe: wartet, freigebbar", st[TC]["state"] == "waiting" and st[TC]["grantable"])
ok("... freigegeben mit wem und Betreff-Praefix", st[TX]["state"] == "granted"
   and st[TX]["granted_by"] == "anna" and st[TX]["subject_prefix"] == "urn:oaap:obj:1")
ok("... App-Typ nur adressiert", st["app.vorgaenge.case.statuschanged.v1"]["state"] == "addressed")
ok("... Zustellung: 12 zugestellt", ov["instances"][0]["push"]["delivered"] == 12
   and ov["instances"][0]["push"]["started"])
ok("... die Plattformtypen alle neun (mit den zwei der Tore)", len(ov["platform_types"]) == 9)

json.dump(OV_CAT, open(os.path.join(apps, "events-catalog.json"), "w"))
json.dump({"instances": {"inv1-pp-a": {"tenant": "T1"}, "fremd": {"tenant": "T2"}}},
          open(os.path.join(apps, "registry.json"), "w"))


class Cur2(Cur):
    answer = {"id": 1}

    def fetchone(self):
        return Cur2.answer

    def fetchall(self):
        return []


class Conn2(Conn):
    def cursor(self):
        return Cur2()


fapp2 = Flask("t2")
bus_door.register(fapp2, lambda t: Conn2() if t == "T1" else None, apps)
bus_door.AUDIT_LOG = os.path.join(apps, "audit.jsonl")
c2 = fapp2.test_client()
ADM = {"X-OAAP-Internal-Key": "x", "X-OAAP-Person-User": "anna", "X-OAAP-Person-Tenant": "T1",
       "X-OAAP-Person-Roles": "tenant_admin"}
r = c2.get("/internal/bus/overview", headers=dict(ADM, **{"X-OAAP-Person-Roles": "user"}))
ok("Seite: eine Person ohne Admin-Rolle -> 403", r.status_code == 403)
r = c2.get("/internal/bus/overview", headers=ADM)
ok("Seite: Admin liest die Uebersicht seines Mandanten", r.status_code == 200
   and r.get_json()["store"] is True and r.get_json()["instances"][0]["instance"] == "inv1-pp-a")
r = c2.post("/internal/bus/grant", json={"instance": "inv1-pp-a", "type": TC}, headers=ADM)
sql, args = Cur.rows[-1]
ok("Freigeben: 200, Zeile mit granted_by = die Person", r.status_code == 200
   and "INSERT INTO bus_grants" in sql and args == ("inv1-pp-a", TC, None, "anna"), (r.get_json(), args))
au = [json.loads(x) for x in open(bus_door.AUDIT_LOG, encoding="utf-8")]
ok("... im Mandanten-Protokoll: events.grant, wer, welcher Mandant",
   au[-1]["action"] == "events.grant" and au[-1]["who"] == "anna" and au[-1]["tenant"] == "T1"
   and au[-1]["subject"] == "inv1-pp-a " + TC, au[-1])
r = c2.post("/internal/bus/grant", json={"instance": "inv1-pp-a", "type": ACT}, headers=ADM)
ok("Freigeben eines adressierten Typs -> 409 mit Grund", r.status_code == 409
   and "no grant needed" in r.get_json()["error"])
r = c2.post("/internal/bus/grant", json={"instance": "fremd", "type": TC}, headers=ADM)
ok("Instanz eines anderen Mandanten -> 404", r.status_code == 404)
r = c2.post("/internal/bus/grant", json={"instance": "inv1-pp-a", "type": TC, "subject_prefix": "x\ty"},
            headers=ADM)
ok("Praefix mit Steuerzeichen -> 400", r.status_code == 400)
Cur2.answer = None
r = c2.post("/internal/bus/revoke", json={"instance": "inv1-pp-a", "type": TC}, headers=ADM)
ok("Widerrufen ohne Freigabe -> 409, kein Protokolleintrag", r.status_code == 409
   and len(open(bus_door.AUDIT_LOG, encoding="utf-8").readlines()) == len(au))
Cur2.answer = {"instance": "inv1-pp-a"}
r = c2.post("/internal/bus/resume", json={"instance": "inv1-pp-a"}, headers=ADM)
ok("Fortsetzen: 200, Protokoll events.resume", r.status_code == 200
   and json.loads(open(bus_door.AUDIT_LOG, encoding="utf-8").readlines()[-1])["action"] == "events.resume")
ok("die Plattform-Tuer bleibt eine eigene Route (statisch vor <action>)",
   c2.post("/internal/bus/platform", json={}).status_code == 403)
psrc = open(os.path.join(SERVICES, "portal", "app.py"), encoding="utf-8").read()
nsrc = open(os.path.join(SERVICES, "portal", "nav_view.py"), encoding="utf-8").read()
ok("Portal: Seite /ereignisse, Aktionen ueber _twin_call an /internal/bus/, nur Admins",
   '@app.get("/ereignisse")' in psrc and '_twin_call("POST", f"/internal/bus/{action}"' in psrc
   and 'caller_roles() & {"tenant_admin", "server_admin"}' in psrc)
ok("Portal: Menue Katalog & Daten -> Ereignisse", '("events", "/ereignisse", "Ereignisse", "can_events")' in nsrc)
csrc = open(os.path.join(HERE, "..", "platform", "docker-compose.yml"), encoding="utf-8").read()
ok("der Zwilling haengt an das Mandanten-Protokoll an (Mount /audit)",
   '${OAAP_DATA_DIR}/data/audit:/audit"' in csrc.split("  twin:")[1].split("\n  hooks:")[0].split("# Webhook")[0])

print("\n=== AsyncAPI-Export (Stufe 2.4) ===")
import re as _re                                                # noqa: E402
VG = {"tenant": "T1", "app_id": "vorgaenge", "version": "0.20.0", "subscribes": [],
      "publishes": [{"type": "app.vorgaenge.case.statuschanged.v1", "title": "Status gewechselt",
                     "audience": "addressed", "semantic_id": "oaap-aas:Cases/1/0/StatusChanged",
                     "personal": False, "schema": {"type": "object", "properties": {"case": {"type": "string"}},
                                                   "required": ["case"]}}]}
AC = {"instances": dict(OV_CAT["instances"], vg=VG)}


def refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                out.append(v)
            else:
                refs(v, out)
    elif isinstance(node, list):
        for v in node:
            refs(v, out)
    return out


def resolve(doc, ref):
    cur = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


da = bm.asyncapi(AC, {}, "T1", "inv1-pp-a")
dt = bm.asyncapi(AC, {}, "T1", None, "inv1")
ok("App-Dokument: asyncapi 3.1.0, info mit Titel und Version der App",
   da["asyncapi"] == "3.1.0" and da["info"]["version"] == "0.2.2" and "inv1-pp-a" in da["info"]["title"])
ok("jeder $ref loest sich auf (App und Mandant)",
   all(resolve(d, r) is not None for d in (da, dt) for r in refs(d, [])),
   [r for d in (da, dt) for r in refs(d, []) if resolve(d, r) is None][:3])
ok("Schluessel der Komponenten nach ^[a-zA-Z0-9.\\-_]+$",
   all(_re.fullmatch(r"[a-zA-Z0-9.\-_]+", k) for d in (da, dt) for k in d["components"]["messages"]))
ok("Server: host, protocol, Variable fuer den Host",
   da["servers"]["platform"]["protocol"] == "http" and "site" in da["servers"]["platform"]["variables"])
ok("App empfaengt: vier receive-Operationen, kein send (sie veroeffentlicht nichts)",
   sorted(o["action"] for o in da["operations"].values()) == ["receive"] * 4)
m = da["components"]["messages"][ACT]
ok("Nachricht: CloudEvents-Umschlag, data = Schema des Typs, adressiert -> oaapto Pflicht",
   m["contentType"] == "application/cloudevents+json" and m["payload"]["properties"]["type"] == {"const": ACT}
   and m["payload"]["properties"]["data"] == bm.PLATFORM_TYPES[ACT]["schema"]
   and "oaapto" in m["payload"]["required"] and m["x-oaap-publisher"] == "platform:identity")
ok("Mandant: send von vg, receive von pp-a, Operationen nach Instanz benannt",
   dt["operations"]["vg__send__app.vorgaenge.case.statuschanged.v1"]["action"] == "send"
   and "inv1-pp-a__receive__" + TC in dt["operations"])
mv = dt["components"]["messages"]["app.vorgaenge.case.statuschanged.v1"]
ok("App-Typ: Semantik-ID und Herausgeber als x-oaap-*",
   mv["x-oaap-semantic-id"] == "oaap-aas:Cases/1/0/StatusChanged" and mv["x-oaap-publisher"] == "instance:vg")
ok("Mandant: keine Instanz eines anderen Mandanten", not any("fremd" in k for k in dt["operations"]))
ok("unbekannte oder fremde Instanz -> None",
   bm.asyncapi(AC, {}, "T1", "fremd") is None and bm.asyncapi(AC, {}, "T1", "weg") is None)
ok("nur Plattformfelder und x-oaap-* auf oberster Ebene",
   set(dt) <= {"asyncapi", "info", "defaultContentType", "servers", "channels", "operations",
               "components", "x-oaap-tenant", "x-oaap-instance", "x-oaap-app"})
r = c2.get("/internal/bus/asyncapi?instance=inv1-pp-a", headers=ADM)
ok("Zwilling: /internal/bus/asyncapi fuer den Admin", r.status_code == 200
   and r.get_json()["asyncapi"] == "3.1.0")
r = c2.get("/internal/bus/asyncapi?instance=fremd", headers=ADM)
ok("... fremde Instanz -> 404", r.status_code == 404)
r = c2.get("/internal/bus/asyncapi", headers=dict(ADM, **{"X-OAAP-Person-Roles": "user"}))
ok("... ohne Admin-Rolle -> 403", r.status_code == 403)
psrc = open(os.path.join(SERVICES, "portal", "app.py"), encoding="utf-8").read()
ok("Portal: Downloads je Mandant und je App", '@app.get("/ereignisse/asyncapi.json")' in psrc
   and '@app.get("/ereignisse/<instance>/asyncapi.json")' in psrc and "attachment; filename=" in psrc)
asrc = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("CLI: oaap events asyncapi", '"resume", "asyncapi"]' in asrc and "bus_model.asyncapi(cat, reg, tid, key" in asrc)

print("\n=== identity: Ausgang zum Bus ===")
DATA = tempfile.mkdtemp(prefix="oaap-bus-id-")
os.environ.update(SESSION_SECRET="s", SETUP_TOKEN="t", INTERNAL_API_KEY="k",
                  OAAP_IDENTITY_DATA_DIR=DATA, OAAP_IDENTITY_BUS_LOOP="0")
sys.path.insert(0, os.path.join(SERVICES, "identity"))
sys.modules.pop("app", None)
m = importlib.import_module("app")
u = {"id": "u-1", "tenant": "T1", "invited_by": "instance:inv1-bestand", "people": {"ref": "n17"}}
it = m.person_event_item(u, "activated", now=5)
ok("Eintrag: adressiert an die einladende Instanz, ref dabei, keine Adresse",
   it == {"tenant": "T1", "publisher": "identity", "type": ACT, "subject": "u-1",
          "data": {"user_id": "u-1", "ref": "n17"}, "to": "inv1-bestand", "at": 5}, it)
ok("... besteht die Pruefung des Zwillings", bm.check_platform_publish(it)[0] == 202)
ok("nicht von einer App eingeladen -> kein Ereignis",
   m.person_event_item({"id": "u-2", "tenant": "T1"}, "activated") is None)
lk = m.person_event_item(u, "locked", by="admin")
ok("locked traegt by", lk["data"]["by"] == "admin" and bm.check_platform_publish(lk)[0] == 202)

SENT, ANSWERS = [], []
m._bus_send = lambda item: SENT.append(item["type"]) or (ANSWERS.pop(0) if ANSWERS else "sent")
m.threading.Thread = lambda target, daemon: type("T", (), {"start": lambda self: None})()
m.person_event(u, "activated")
m.person_event(u, "locked", by="app")
ok("zwei Ereignisse im Ausgang, in Reihenfolge", [i["type"] for i in m._bus_queue()] == [ACT, LOCK])
ANSWERS[:] = ["later"]
m.bus_flush()
ok("Zwilling nicht da: nichts verloren, Reihenfolge bleibt (das zweite nicht vor dem ersten)",
   SENT == [ACT] and [i["type"] for i in m._bus_queue()] == [ACT, LOCK])
SENT.clear()
ANSWERS[:] = ["refused", "sent"]
m.bus_flush()
ok("Ablehnung ist endgueltig, das naechste geht raus, Ausgang leer",
   SENT == [ACT, LOCK] and m._bus_queue() == [])
m._save(m.BUS_OUTBOX_FILE, [dict(it, at=1)])
SENT.clear()
m.bus_flush()
ok("aelter als 7 Tage: verworfen, nicht gesendet", SENT == [] and m._bus_queue() == [])

print("\n=== identity: die Stellen, die melden ===")
cb = inspect.getsource(m._idp_principal)
ok("erste Anmeldung: last_login vorher leer -> activated",
   'first_login = not u["idp"].get("last_login")' in cb and 'person_event(u, "activated")' in cb)
act = inspect.getsource(m.people_act)
ok("die App sperrt/entsperrt -> locked/unlocked by app, nur bei echter Aenderung",
   'person_event(u, action + "ed", by="app")' in act and "changed" in act)
upd = open(os.path.join(SERVICES, "identity", "app.py"), encoding="utf-8").read()
ok("der Mandantenadmin sperrt/entsperrt im Portal -> by admin",
   'person_event(u, "unlocked" if active else "locked", by="admin")' in upd)
ok("Senden mit dem Plattformschluessel an die interne Tuer",
   '"X-OAAP-Internal-Key": INTERNAL_KEY' in upd and "/internal/bus/platform" in upd)

ac = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
i = ac.find("if events_entry and not is_artefact and not rehearsal:")
ok("ein events-Block legt den Ereignisspeicher (Zwilling-Schema) des Mandanten an",
   i > 0 and "_twin_ensure_schema(twin_tenant)" in ac[i:i + 900])

print()
print("all platform-event checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
