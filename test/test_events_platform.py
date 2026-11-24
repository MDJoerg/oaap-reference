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
