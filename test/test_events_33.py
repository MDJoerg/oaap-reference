#!/usr/bin/env python3
"""RFC-0127 stage 3.3 (oaap.events.bus 0.10): Jörg's D13-D15.

  1. D13: a grant delivers from its moment on -- for instances (feed and
     push) as for clients, one rule (bus_model.grant_condition)
  2. D14: a client for events only (`add-client --events`, the portal's
     box): no right in the twin; a webhook in oaap-v1 would carry nothing
  3. D15: /twin/v1 at the node's external name: the key as at the node's
     address, and there only an integration client

psycopg2 is a stand-in where the twin's app is loaded (as in
test_twin_clients.py).

    py -3 test/test_events_33.py
"""
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "platform", "services")
TWIN = os.path.join(SERVICES, "twin")
PLATFORM = os.path.join(HERE, "..", "platform")
DATA = tempfile.mkdtemp(prefix="oaap-events-33-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, SERVICES)
sys.path.insert(0, TWIN)

fails = 0


def ok(what, cond, info=""):
    global fails
    print(("PASS  " if cond else "FAIL  ") + what + ("" if cond else f"   {str(info)[:600]}"))
    if not cond:
        fails += 1


import bus_model as bm                                         # noqa: E402

print("=== D13: eine Freigabe liefert ab ihrem Zeitpunkt, fuer Instanzen wie fuer Clients ===")
G = {"type": "app.shop.order.placed.v1", "subject_prefix": None, "granted_at": "T0"}
w = bm.pending_conditions([G], ["x.y.z.v1"], "me")
ok("Feed/Push einer Instanz: time >= granted_at, adressierte bleiben wie sie waren",
   "oaapto IS NULL AND type = %s AND time >= %s" in w[0] and "oaapto = %s AND type = ANY(%s)" in w[0]
   and w[1] == ["me", G["type"], "T0", "me", ["x.y.z.v1"]], w)
w2 = bm.pending_conditions([dict(G, subject_prefix="S-")], [], "me")
ok("... mit Praefix", w2[1] == ["me", G["type"], "T0", "S-"] and "starts_with(subject, %s)" in w2[0], w2)
ok("Instanz und Client lesen dieselbe Bedingung je Freigabe (eine Regel, eine Funktion)",
   bm.pending_conditions([G], [], "x") == bm.client_conditions([G], "x"))
try:
    bm.pending_conditions([{"type": G["type"], "subject_prefix": None}], [], "me")
    ok("KOEDER eine Freigabe ohne granted_at oeffnet nicht still die Vergangenheit", False)
except KeyError:
    ok("KOEDER eine Freigabe ohne granted_at oeffnet nicht still die Vergangenheit", True)
for f in ("bus_door.py", "bus_push.py", "hooks.py"):
    src = open(os.path.join(TWIN, f), encoding="utf-8").read()
    ok(f"{f}: liest granted_at aus bus_grants", "granted_at FROM bus_grants" in src)
ok("kein Leser mehr ohne granted_at",
   all("SELECT type, subject_prefix FROM bus_grants" not in open(os.path.join(TWIN, f), encoding="utf-8").read()
       for f in os.listdir(TWIN) if f.endswith(".py")))

print("\n=== D14: Clients nur fuer Ereignisse ===")
sys.path.insert(0, PLATFORM)
try:
    import appctl as ac
except ImportError as e:
    ac = None
    print(f"SKIP  appctl: {e}")
if ac is not None:
    ac.TWIN_CLIENTS_FILE = os.path.join(DATA, "twin-clients.json")
    ok("--events allein: erlaubt", ac.twin_client_problem("ev-erp", "T1", "", set(), events=True) == "")
    ok("ohne alles: abgelehnt, nennt --events",
       "--events" in ac.twin_client_problem("ev-erp", "T1", "", set()))
    ok("--events mit Lesetypen: abgelehnt (nur Ereignisse heisst nur Ereignisse)",
       "for events only" in ac.twin_client_problem("ev-erp", "T1", "FleetNode", {"FleetNode"}, events=True))
    ok("--events mit --models: abgelehnt",
       "for events only" in ac.twin_client_problem("ev-erp", "T1", "", set(), models=True, events=True))
    src = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
    ok("CLI: --events am Parser, an twin_client_add, im Eintrag 'events': true",
       'pdt.add_argument("--events"' in src and "events=args.events" in src
       and 'clients[label]["events"] = True' in src)
    ok("Portal-Auftrag: events reicht bis twin_client_add", 'events = a.get("events") is True' in src
       and "models=models,\n                            events=events)" in src.replace("\r\n", "\n"))
portal = open(os.path.join(SERVICES, "portal", "app.py"), encoding="utf-8").read()
ok("Portal: Kaestchen 'Nur Ereignisse', geht in den Auftrag, Liste zeigt es",
   'name="events" value="ja"' in portal and '"events": request.form.get("events") == "ja"' in portal
   and "nur Ereignisse" in portal and '"events": c.get("events") is True' in portal)

print("\n=== Der Zwilling: Webhook eines Clients, der nichts liest ===")
fake_pg = types.ModuleType("psycopg2")
fake_pg.extras = types.ModuleType("psycopg2.extras")
fake_pg.extras.RealDictCursor = object
fake_pg.Error = Exception
fake_pg.connect = lambda **kw: None
sys.modules.setdefault("psycopg2", fake_pg)
sys.modules.setdefault("psycopg2.extras", fake_pg.extras)
try:
    import flask  # noqa: F401
except ImportError:
    flask = None
    print("SKIP  flask fehlt -- der Dienst kann hier nicht laufen")
if flask is not None:
    import twin_sync as tsy
    import twin_write as tw
    for fmt, want in ((None, True), ("oaap-v1", True), ("cloudevents", False)):
        body = {"url": "https://erp.example.com/hook"}
        if fmt:
            body["format"] = fmt
        try:
            tsy.add_hook(None, "client:ev-erp", set(), body)
            refused = False
        except tw.Refused as e:
            refused = "reads nothing" in str(e)
        except Exception:                                    # noqa: BLE001 -- got past the check
            refused = False
        ok(f"Format {fmt or '(Vorgabe oaap-v1)'}: " + ("abgelehnt, nennt cloudevents" if want else
                                                     "kommt an der Pruefung vorbei"), refused == want)

    print("\n=== D15: /twin/v1 an der externen Adresse ===")
    import app as twin
    APPS = os.path.join(DATA, "platform-apps")
    os.makedirs(APPS, exist_ok=True)
    twin.REGISTRY_FILE = os.path.join(APPS, "registry.json")
    twin.READERS_FILE = os.path.join(APPS, "twin-readers.json")
    twin.CLIENTS_FILE = os.path.join(APPS, "twin-clients.json")
    with open(twin.REGISTRY_FILE, "w") as f:
        json.dump({"instances": {"orders": {"tenant": "t1", "app_id": "orders"}}}, f)
    with open(twin.READERS_FILE, "w") as f:
        json.dump({"readers": {"far": {"tenant": "t1", "reads": ["FleetNode"]}}}, f)
    with open(twin.CLIENTS_FILE, "w") as f:
        json.dump({"clients": {"graph": {"tenant": "t1", "reads": ["FleetNode"]}}}, f)

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            pass

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur()

        def close(self):
            pass

    twin.get_conn = lambda tid: Conn()
    c = twin.app.test_client()
    OUT = {twin.TWIN_DOOR_HEADER: "external"}
    ok("Name der Kopfzeile: Zwilling und Gateway sagen dasselbe",
       ac is None or twin.TWIN_DOOR_HEADER == ac.TWIN_DOOR_HEADER)
    r = c.get("/twin/v1/types", headers=dict(OUT, **{"X-OAAP-User": "client:graph"}))
    ok("aussen: ein Integrations-Client kommt durch", r.status_code == 200, r.get_data(as_text=True))
    for who, what in (("instance:orders", "der Schluessel einer Instanz"),
                      ("remote:far", "ein entfernter Leser"), ("joerg", "eine Person")):
        r = c.get("/twin/v1/types", headers=dict(OUT, **{"X-OAAP-User": who}))
        ok(f"KOEDER aussen: {what} -> 403, nur Clients",
           r.status_code == 403 and "integration client's door only" in r.get_data(as_text=True),
           (r.status_code, r.get_data(as_text=True)))
    r = c.get("/twin/v1/types", headers={"X-OAAP-User": "instance:orders"})
    ok("innen (Knotenadresse, ohne Kopfzeile): die Instanz liest weiter wie bisher",
       r.status_code == 200, r.get_data(as_text=True))

if ac is not None:
    body = ac._portal_site_body()
    i_v1 = body.index("\thandle /twin/v1/* {")
    i_in = body.index("\thandle /twin/v1/inbound/* {")
    i_all = body.index("\thandle {")
    blk = body[i_v1:body.index("\t}", i_v1) + 1]
    ok("externe Seite: /twin/v1/* vor dem Anmelde-Fang, hinter dem Eingang, forward_auth wie /twin/*",
       i_in < i_v1 < i_all and "\t\t\t\turi /verify?roles=user&instance=oaap.twin" in blk
       and any(x.startswith("\t\t\t\tcopy_headers X-OAAP-User ") for x in blk)
       and "\t\t\treverse_proxy twin:8000" in blk, blk)
    ok("... setzt die Tuer-Kopfzeile (ueberschreibt, was der Absender schrieb)",
       f"\t\t\trequest_header {ac.TWIN_DOOR_HEADER} external" in blk, blk)
    i_nk = blk.index("\t\thandle @twin_nokey {")
    ok("... ohne Schluessel 401 vor identity, nicht die Anmeldeseite (gemessen: 303)",
       "\t\t@twin_nokey not header_regexp Authorization (?i)^bearer\\s" in blk
       and any(x.strip().startswith("respond ") and x.rstrip().endswith(" 401") for x in blk)
       and i_nk < blk.index("\t\t\tforward_auth identity:8000 {"), blk)
    ok("... die Apps-Wege /twin/objects bleiben aussen zu", not any("handle /twin/* {" in x for x in body))
    caddy = open(os.path.join(PLATFORM, "Caddyfile"), encoding="utf-8").read()
    ok("Knotenadresse: /twin/* fragt dieselbe Pruefung wie die externe Seite",
       "uri /verify?roles=user&instance=oaap.twin" in caddy)
    import inspect as _ins
    mig = _ins.getsource(ac.cmd_migrate_tenant_places)
    ok("Update: schon geschriebene externe Seiten ohne /twin/v1/* werden einmal neu geschrieben",
       '"@twin_nokey not header_regexp Authorization" not in have' in mig and "refresh_generated_sites()" in mig)

print()
print("all stage-3.3 checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
