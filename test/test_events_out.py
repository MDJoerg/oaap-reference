"""RFC-0127 stage 3.1 (oaap.events.bus 0.8): the way out.

  1. bus_model: grants to integration clients, personal types, the twin's
     change as a CloudEvent, the overview's clients
  2. hooks.py: the OPTIONS handshake, delivery in the CloudEvents format
     (stand-in database cursor, stand-in sender)
  3. the places that grant, revoke and register

    py -3 test/test_events_out.py
"""
import inspect
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


print("=== Freigaben an Integrations-Clients ===")
P1 = {"type": "app.probe.thing.changed.v1", "title": "x", "audience": "tenant", "personal": False,
      "schema": {"type": "object"}}
P2 = dict(P1, type="app.probe.person.changed.v1", personal=True)
P3 = dict(P1, type="app.probe.case.done.v1", audience="addressed")
P4 = dict(P1, type="app.probe.only.listed.v1", audience="listed", listed=["other"])
CAT = {"instances": {"t1-pub": {"tenant": "T1", "app_id": "probe", "publishes": [P1, P2, P3, P4],
                                "subscribes": []},
                     "t2-pub": {"tenant": "T2", "app_id": "probe", "publishes": [dict(P1, type="app.probe.far.away.v1")],
                                "subscribes": []}}}
CL = {"erp": {"tenant": "T1"}, "fremd": {"tenant": "T2"}}
ce = bm.client_grant_error
ok("Typ fuer alle -> freigebbar", ce(CAT, "T1", CL, "client:erp", P1["type"]) == "")
ok("personenbezogener Typ -> freigebbar (die Freigabe ist die Erlaubnis)",
   ce(CAT, "T1", CL, "client:erp", P2["type"]) == "" and bm.is_personal(CAT, "T1", P2["type"]))
ok("adressierter App-Typ -> nein", "addressed" in ce(CAT, "T1", CL, "client:erp", P3["type"]))
ok("nur fuer genannte Apps -> nein", "listed" in ce(CAT, "T1", CL, "client:erp", P4["type"]))
ok("Zwilling-Typ -> nein, kommt ueber die Leserechte", "read rights" in
   ce(CAT, "T1", CL, "client:erp", "oaap.twin.object.created.v1"))
ok("identity-Typ -> nein, verlaesst den Mandanten nicht", "does not leave" in
   ce(CAT, "T1", CL, "client:erp", "oaap.identity.person.activated.v1"))
ok("Client eines anderen Mandanten -> nein", "not an integration client of this tenant" in
   ce(CAT, "T1", CL, "client:fremd", P1["type"]))
ok("unbekannter Client / kein client:-Name -> nein",
   ce(CAT, "T1", CL, "client:weg", P1["type"]) and ce(CAT, "T1", CL, "t1-pub", P1["type"]))
ok("Typ eines anderen Mandanten -> nein", "no instance" in
   ce(CAT, "T1", CL, "client:erp", "app.probe.far.away.v1"))
ok("Angebot fuer Clients: nur tenant-Typen des Mandanten",
   bm.client_types(CAT, "T1") == sorted([P1["type"], P2["type"]]))
ov = bm.overview(CAT, {}, "T1", [{"instance": "client:erp", "type": P1["type"], "subject_prefix": None,
                                  "granted_by": "anna", "granted_at": "x"}], {}, CL)
ok("Uebersicht: Client mit Freigabe und Angebot, personenbezogen markiert",
   [c["client"] for c in ov["clients"]] == ["client:erp"]
   and [g["type"] for g in ov["clients"][0]["grants"]] == [P1["type"]]
   and ov["clients"][0]["grantable"] == [{"type": P2["type"], "personal": True}], ov["clients"])

w = bm.client_conditions([{"type": P1["type"], "subject_prefix": "S-", "granted_at": "T0"}], "client:erp")
ok("Client bekommt nur Ereignisse ab dem Freigabezeitpunkt (time >= granted_at), mit Praefix",
   "time >= %s" in w[0] and "starts_with(subject, %s)" in w[0] and w[1] == ["client:erp", P1["type"], "T0", "S-"], w)
ok("... ohne Freigabe nichts", bm.client_conditions([], "client:erp") is None)
hsrc = open(os.path.join(TWIN, "hooks.py"), encoding="utf-8").read()
ok("hooks.py liest granted_at und nimmt client_conditions (nicht pending_conditions)",
   "granted_at FROM bus_grants" in hsrc and "bm.client_conditions(" in hsrc
   and "bm.pending_conditions(" not in hsrc)

print("\n=== Eine Zwilling-Aenderung als CloudEvent ===")
O = "6f1c2d3e-0000-4000-8000-000000000001"
ch = {"id": 42, "at": "2026-11-24T10:00:00Z", "kind": "status.changed", "object": "urn:oaap:obj:" + O,
      "type": "bundle", "group": None, "origin": "app:bestand", "status": {"from": "open", "to": "done"}}
ev = bm.twin_change_event(ch, "T1")
ok("statuschanged, id twin-42, source twin, Betreff die URN, Herkunft, from/to",
   ev["type"] == "oaap.twin.object.statuschanged.v1" and ev["id"] == "twin-42"
   and ev["source"] == "urn:oaap:tenant:T1:twin" and ev["subject"] == "urn:oaap:obj:" + O
   and ev["oaaporigin"] == "app:bestand" and ev["data"]["to"] == "done" and ev["specversion"] == "1.0", ev)
ok("... data besteht das Schema des Typs",
   bm.validate(ev["data"], bm.PLATFORM_TYPES[ev["type"]]["schema"]) is None)
ok("type.created -> kein Ereignis", bm.twin_change_event(dict(ch, kind="type.created"), "T1") is None)
ok("zusammengefuehrt: oaapmergedfrom", bm.twin_change_event(
    dict(ch, kind="group.written", merged_from="urn:oaap:obj:x"), "T1")["oaapmergedfrom"] == "urn:oaap:obj:x")

print("\n=== hooks.py: Handshake und CloudEvents-Zustellung ===")
try:
    import psycopg2  # noqa: F401
except ImportError:
    print("SKIP  psycopg2 fehlt")
    print(f"\n{fails} check(s) FAILED" if fails else "\nall way-out checks passed")
    sys.exit(1 if fails else 0)
import hooks                                                    # noqa: E402

SENT = []


def sender(answers):
    def send(url, body, headers, method="POST"):
        SENT.append((method, url, headers, body))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    return send


ok("Handshake: 200 mit unserem Ursprung -> ok",
   hooks.handshake_problem("https://x", "inv1.oaap", sender([(200, "", {"webhook-allowed-origin": "inv1.oaap"})])) is None)
ok("... mit * -> ok", hooks.handshake_problem("https://x", "o", sender([(200, "", {"webhook-allowed-origin": "*"})])) is None)
ok("... ohne Allowed-Origin -> Satz", "missing" in hooks.handshake_problem("https://x", "o", sender([(200, "", {})])))
ok("... fremder Ursprung -> Satz", "not 'o'" in hooks.handshake_problem(
    "https://x", "o", sender([(200, "", {"webhook-allowed-origin": "other"})])))
ok("... 405 -> Satz", "405" in hooks.handshake_problem("https://x", "o", sender([(405, "", {})])))
ok("... gesendet: OPTIONS mit WebHook-Request-Origin", SENT[0][0] == "OPTIONS"
   and SENT[0][2]["WebHook-Request-Origin"] == "inv1.oaap")


class Cur:
    log = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        Cur.log.append((sql, args))

    def fetchone(self):
        return {"ok": True}

    def fetchall(self):
        return []


class Conn:
    def cursor(self):
        return Cur()


hook = {"id": 7, "principal": "client:erp", "url": "https://erp.example/hook", "secret": "whsec_x",
        "cursor": 10, "bus_cursor": 5, "kinds": [], "own": False, "failures": 0,
        "format": "cloudevents", "handshake_at": None}
hooks.tr.changes = lambda conn, types, since, limit: ([ch, dict(ch, id=43, kind="type.created")], 43, False)
ROW = {"seq": 9, "id": "01ABC", "type": P1["type"], "source_instance": "t1-pub", "source_app": "probe",
       "subject": "S-1", "time": "2026-11-24T10:00:01.000Z", "data": {"n": 1}, "semantic": None, "oaapto": None}
hooks.bus_batch = lambda conn, principal, cursor, limit: ([ROW], 9)
SENT.clear()
Cur.log.clear()
r = hooks.deliver_ce(Conn(), hook, "T1", {"bundle"}, "inv1.oaap", now=1000,
                     send=sender([(200, "", {"webhook-allowed-origin": "*"}), (202, "")]))
m, url, hdr, body = SENT[-1]
evs = json.loads(body)
ok("erste Zustellung: erst OPTIONS, dann ein POST", [s[0] for s in SENT] == ["OPTIONS", "POST"], SENT)
ok("... Batch-Format, Signatur ueber den Body", hdr["Content-Type"] == "application/cloudevents-batch+json"
   and hooks.verify("whsec_x", hdr["X-OAAP-Signature"], body, now=1000))
ok("... Zwilling-Aenderung und freigegebenes Ereignis im selben Batch, type.created nicht",
   [e["type"] for e in evs] == ["oaap.twin.object.statuschanged.v1", P1["type"]]
   and evs[1]["source"] == "urn:oaap:tenant:T1:instance:t1-pub", [e["type"] for e in evs])
upd = [s for s, a in Cur.log if s.startswith("UPDATE webhooks")]
ok("... Handshake vermerkt, beide Cursor bewegt, zugestellt gezaehlt", r == "sent 2"
   and any("handshake_at = now()" in s for s in upd)
   and any(a == (43, 9, 2, 7) for s, a in Cur.log if "bus_cursor = %s" in s), Cur.log)
SENT.clear()
Cur.log.clear()
r = hooks.deliver_ce(Conn(), dict(hook), "T1", {"bundle"}, "o",
                     send=sender([(200, "", {"webhook-allowed-origin": "jemand"})]))
ok("Handshake abgelehnt: kein POST, Fehlschlag mit Grund, Cursor unveraendert",
   r == "failed" and [s[0] for s in SENT] == ["OPTIONS"]
   and any("failures = %s" in s and "WebHook-Allowed-Origin" in str(a) for s, a in Cur.log)
   and not any("bus_cursor = %s" in s for s, a in Cur.log))
SENT.clear()
r = hooks.deliver_ce(Conn(), dict(hook, handshake_at="gestern"), "T1", {"bundle"}, "o",
                     send=sender([(500, "kaputt")]))
ok("schon gehandshaked: direkt POST; 500 -> Fehlschlag", r == "failed" and [s[0] for s in SENT] == ["POST"])
ok("altes Format bleibt fuer bestehende Hooks (deliver_one verzweigt nur bei cloudevents)",
   'if hook.get("format") == "cloudevents":' in inspect.getsource(hooks.deliver_one))

print("\n=== Wo freigegeben, widerrufen, angemeldet wird ===")
import twin_sync as tsy                                         # noqa: E402
src = inspect.getsource(tsy.add_hook)
ok("Webhook anmelden: format oaap-v1 (Standard) oder cloudevents, Bus-Cursor am Ende des Speichers",
   '"oaap-v1", "cloudevents"' in src and "max(seq)" in src and "bus_cursor" in src)
ac = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("Schema: format, handshake_at, bus_cursor an webhooks (ADD COLUMN IF NOT EXISTS)",
   "ADD COLUMN IF NOT EXISTS format text NOT NULL DEFAULT 'oaap-v1'" in ac
   and "ADD COLUMN IF NOT EXISTS bus_cursor" in ac)
ok("CLI: oaap events grant client:<label> mit Hinweis bei personenbezogen",
   "client_grant_error(cat, tid, load_twin_clients(), key, args.type)" in ac
   and "leaves the tenant" in ac)
j = ac.find("def cmd_events")
body = ac[j:ac.find("\ndef ", j + 10)]
g = body.find("        # a client was checked above (client_grant_error); an instance here")
ok("CLI: der Freigabe-Zweig gilt fuer Instanz UND Client (gemessen: Client fiel in den Widerruf)",
   g > 0 and body.find("INSERT INTO {s}.bus_grants", g) > g
   and body.find("INSERT INTO {s}.bus_grants", g) < body.find("SET revoked_at = now()", g))
i = ac.find("def twin_client_remove")
ok("Client entfernen widerruft seine Freigaben", "UPDATE {s}.bus_grants SET revoked_at" in ac[i:i + 2500])
door = open(os.path.join(TWIN, "bus_door.py"), encoding="utf-8").read()
ok("Portal-Tuer: Clients des Mandanten freigebbar, Protokoll sagt personenbezogen",
   "bm.client_grant_error(cat, tenant, _clients(), name, t)" in door and "personal, leaves the tenant" in door)
po = open(os.path.join(SERVICES, "portal", "app.py"), encoding="utf-8").read()
ok("Portal: Abschnitt Integrations-Clients mit Warnung beim Freigeben",
   "Integrations-Clients" in po and "Freigeben — verlässt den Mandanten" in po)

print()
print("all way-out checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
