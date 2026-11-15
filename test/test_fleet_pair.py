#!/usr/bin/env python3
"""Kopplung von Knoten, Eingang und Zentrale (RFC-0085 Stufe 3, Nachtrag A3).

A  die Regeln (services/fleet_pair.py): Code, Angebot, Widerruf, Brücke;
B  der Sender schickt nur, was der Knoten anbietet (Lebenszeichen immer);
C  die Flottenwache: ein entfernter Knoten ist "entfernt", nie "verstummt";
D  Ende zu Ende in einem Prozess: Zentrale, Eingang und Knoten als drei
   Rollen von appctl, die HTTPS-Anrufe gehen an die Rolle des Gegenübers,
   identity ist ein Schein mit eigener Schlüsselliste. Braucht Linux
   (Dateisperre), kein Docker.

Aufruf: python3 test/test_fleet_pair.py
"""
import contextlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import fleet_pair as fp                                        # noqa: E402
import fleet_watch as fw                                       # noqa: E402
import metrics as m                                            # noqa: E402
import metrics_sender as s                                     # noqa: E402

fails = 0
NOW = 1_800_000_000


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def refused(fn, *a, **k):
    try:
        fn(*a, **k)
    except fp.Refusal as exc:
        return str(exc)
    return ""


PLACE = {"https": "entry.example", "broker": "mqtts://entry.example:8883", "fp": "ab" * 32}

print("=== A: Textform ===")
text, rec = fp.make_node_code("hub1", "entry1", PLACE, "geheim", "node1", NOW)
ok("beginnt mit OAAP-PAIR1.", text.startswith(fp.PREFIX))
wrapped = "\n".join(text[i:i + 60] for i in range(0, len(text), 60)) + "\n"
ok("Zeilenumbrüche (Mail) stören nicht", fp.decode(wrapped)["node"] == "node1")
ok("Müll abgelehnt", "keine Kopplungsdatei" in refused(fp.decode, "hallo"))
ok("beschädigt abgelehnt", "beschädigt" in refused(fp.decode, fp.PREFIX + "%%%"))
ok("der Hub hält nur die Prüfsumme des Einmalwerts",
   rec["nonce"] == fp.digest(fp.decode(text)["nonce"]) and rec["node"] == "node1")
ok("Vorschau ohne Geheimnis", "mac" not in fp.preview(fp.decode(text))
   and "nonce" not in fp.preview(fp.decode(text)))
ok("ungültiger Knotenname abgelehnt",
   "Name des Knotens" in refused(fp.make_node_code, "hub1", "e", PLACE, "g", "Node_1", NOW))
ok("ohne TLS-Broker kein Code", "TLS" in refused(
    fp.make_node_code, "hub1", "e", dict(PLACE, broker="mqtt://x"), "g", "node1", NOW))

print("\n=== A: der Eingang prüft einen Code ===")


class Keys:
    def __init__(self):
        self.rows = []

    def active(self, node, label=""):
        return [r["id"] for r in self.rows if r["node"] == node and not r["revoked"]
                and (not label or r["label"] == label)]

    def issue(self, node, hub):
        kid = f"{len(self.rows) + 1:08x}"
        self.rows.append({"id": kid, "node": node, "label": fp.KEY_LABEL + hub,
                          "revoked": False})
        return kid, f"oaapk_{kid}_{'x' * 30}"

    def revoke(self, kid):
        next(r for r in self.rows if r["id"] == kid)["revoked"] = True


def entry_state():
    return {"hubs": {"hub1": {"secret": "geheim"}}}


st, keys = entry_state(), Keys()
ans = fp.claim_node(st, {"code": text, "node": "node1", "classes": ["metrics"]}, NOW + 10,
                    "entry1", keys.active, keys.issue, keys.revoke)
ok("gültiger Code: Schlüssel für genau diesen Knoten",
   ans["user"] == "00000001" and ans["secret"].startswith("oaapk_00000001_")
   and ans["node"] == "node1" and ans["broker"] == PLACE["broker"], ans)
ok("Lebenszeichen immer dabei, Unbekanntes nie",
   st["nodes"]["node1"]["classes"] == ["alive", "metrics"], st["nodes"])
ok("zweimal benutzt: abgelehnt", "schon benutzt" in refused(
    fp.claim_node, st, {"code": text}, NOW + 20, "entry1", keys.active, keys.issue, keys.revoke))
ok("abgelaufen: abgelehnt", "abgelaufen" in refused(
    fp.claim_node, entry_state(), {"code": text}, NOW + fp.TTL + 1, "entry1",
    keys.active, keys.issue, keys.revoke))
obj = fp.decode(text)
forged = fp.encode(dict(obj, node="node9"))
ok("gefälscht (Name geändert): abgelehnt", "ungültig" in refused(
    fp.claim_node, entry_state(), {"code": forged}, NOW, "entry1", keys.active,
    keys.issue, keys.revoke))
ok("unbekannte Zentrale: abgelehnt", "ungültig" in refused(
    fp.claim_node, {"hubs": {"hub2": {"secret": "geheim"}}}, {"code": text}, NOW, "entry1",
    keys.active, keys.issue, keys.revoke))
ok("anderer Eingang: abgelehnt", "anderen Eingang" in refused(
    fp.claim_node, entry_state(), {"code": text}, NOW, "entry2", keys.active,
    keys.issue, keys.revoke))
ok("Knoten nennt anderen Namen: abgelehnt", "anderen Knoten" in refused(
    fp.claim_node, entry_state(), {"code": text, "node": "node2"}, NOW, "entry1",
    keys.active, keys.issue, keys.revoke))
st2 = entry_state()
t2, _ = fp.make_node_code("hub1", "entry1", PLACE, "geheim", "node1", NOW)
why = refused(fp.claim_node, st2, {"code": t2}, NOW, "entry1", keys.active, keys.issue,
              keys.revoke)
ok("schon gekoppelt ohne Ersatz: abgelehnt", "schon gekoppelt" in why, why)
ok("... und der Code ist trotzdem verbraucht (ein Versuch)",
   len(st2["used"]) == 1 and "schon benutzt" in refused(
       fp.claim_node, st2, {"code": t2}, NOW, "entry1", keys.active, keys.issue, keys.revoke))
t3, _ = fp.make_node_code("hub1", "entry1", PLACE, "geheim", "node1", NOW, replace=True)
ans3 = fp.claim_node(entry_state(), {"code": t3}, NOW, "entry1", keys.active, keys.issue,
                     keys.revoke)
ok("als Ersatz: alter Schlüssel widerrufen, neuer aktiv",
   keys.rows[0]["revoked"] and keys.active("node1") == [ans3["user"]], keys.rows)

print("\n=== A: Angebot eines Eingangs ===")
otext, orec = fp.make_entry_offer("entry1", "entry.example", PLACE["broker"], "ab" * 32, NOW)
est = {"offers": [orec]}
token = fp.decode(otext)["token"]
ops = []
ea = fp.claim_entry(est, {"token": token, "hub": "hub1"}, NOW + 5, "entry1",
                    lambda hub: (ops.append(hub), ("op000001", "oaapk_op000001_" + "y" * 30))[1],
                    PLACE["broker"])
ok("gültiges Angebot: Betreiberschlüssel und Kopplungsgeheimnis",
   ea["operator_user"] == "op000001" and len(ea["pair_secret"]) > 30
   and est["hubs"]["hub1"]["secret"] == ea["pair_secret"] and ops == ["hub1"], ea)
ok("der Eingang hält nur die Prüfsumme des Tokens", token not in json.dumps(orec))
ok("zweimal benutzt: abgelehnt", "ungültig, abgelaufen oder schon benutzt" in refused(
    fp.claim_entry, est, {"token": token, "hub": "hub1"}, NOW + 6, "entry1",
    lambda h: ("x", "y"), PLACE["broker"]))
ok("abgelaufen: abgelehnt", "abgelaufen" in refused(
    fp.claim_entry, {"offers": [orec]}, {"token": token, "hub": "hub1"}, NOW + fp.TTL + 1,
    "entry1", lambda h: ("x", "y"), PLACE["broker"]))
ok("falsches Token: abgelehnt", refused(
    fp.claim_entry, {"offers": [orec]}, {"token": "falsch", "hub": "hub1"}, NOW, "entry1",
    lambda h: ("x", "y"), PLACE["broker"]) != "")

print("\n=== A: Widerruf durch die Zentrale ===")
rst = {"hubs": {"hub1": {"secret": "geheim"}}, "nodes": {"node1": {}}}
rk = Keys()
rk.issue("node1", "hub1")
rk.rows.append({"id": "hand0001", "node": "node1", "label": "", "revoked": False})
body = {"hub": "hub1", "node": "node1", "ts": NOW, "nonce": "n1",
        "mac": fp.revoke_mac("geheim", "hub1", "node1", NOW, "n1")}
ra = fp.revoke_node(rst, body, NOW + 3, rk.active, rk.revoke)
ok("gültig: der von der Kopplung ausgestellte Schlüssel geht, der von Hand bleibt",
   ra["revoked"] == 1 and rk.rows[0]["revoked"] and not rk.rows[1]["revoked"], rk.rows)
ok("Wiederholung: abgelehnt", "schon bearbeitet" in refused(
    fp.revoke_node, rst, body, NOW + 4, rk.active, rk.revoke))
ok("zu alt: abgelehnt", "zu alt" in refused(
    fp.revoke_node, {"hubs": {"hub1": {"secret": "geheim"}}},
    dict(body, nonce="n2", mac=fp.revoke_mac("geheim", "hub1", "node1", NOW, "n2")),
    NOW + fp.REVOKE_SKEW + 1, rk.active, rk.revoke))
ok("falsches Geheimnis: abgelehnt", "Nicht berechtigt" in refused(
    fp.revoke_node, {"hubs": {"hub1": {"secret": "geheim"}}},
    dict(body, nonce="n3", mac=fp.revoke_mac("anders", "hub1", "node1", NOW, "n3")),
    NOW, rk.active, rk.revoke))

print("\n=== A: die Brücke der Zentrale ===")
conf = fp.bridge_conf("entry1", "hub1", "mqtts://entry.example", "op000001", "oaapk_x")
ok("wie die von Hand geschriebene (Stufe 1)", all(line in conf.splitlines() for line in (
    "connection entry-entry1", "address entry.example:8883",
    "topic # in 1 oaap-via/entry1/ oaap-node/",
    "topic # out 1 oaap-fleet/control/entry1/ oaap-fleet/control/entry1/",
    "remote_username op000001", "cleansession false",
    "notification_topic oaap-fleet/bridge/entry1/hub1")), conf)
ok("Leerzeichen in einem Wert: abgelehnt",
   refused(fp.bridge_conf, "entry1", "hub1", "mqtts://e", "a b", "x") != "")

print("\n=== B: der Sender schickt nur, was angeboten ist ===")


class Fake:
    sent = []

    def __init__(self, cfg, deadline):
        pass

    def connect(self, secret):
        pass

    def publish(self, topic, payload, retain=True):
        Fake.sent.append((topic, json.loads(payload)))

    def close(self):
        pass


def sender_with(classes):
    mdir, cdir = tempfile.mkdtemp(), tempfile.mkdtemp()
    s.configure(cdir, "mqtts://hub.example", "u1", "geheim", node="node1", classes=classes)
    with open(os.path.join(mdir, s.STATUS_OUT), "w") as f:
        json.dump({"digest": "d1", "document": {"v": 1}}, f)
    s.spool_event(mdir, {"id": "e1", "type": "node.test", "text": "x"})
    return mdir, cdir


mdir, cdir = sender_with(["alive"])
ok("alive wird erzwungen", s.load_config(cdir)["classes"] == ["alive"])
Fake.sent = []
r = s.run(mdir, cdir, now=NOW, client_factory=Fake)
topics = [t for t, _ in Fake.sent]
ok("nur das Lebenszeichen geht hinaus", r["status"] == "ok"
   and topics == ["oaap-node/node1/alive"], topics)
ok("es nennt die angebotenen Klassen", Fake.sent[0][1].get("classes") == ["alive"], Fake.sent)
ok("das nicht angebotene Ereignis bleibt nicht liegen", s.events_pending(mdir) == [])
mdir, cdir = sender_with(["status", "events"])
Fake.sent = []
s.run(mdir, cdir, now=NOW, client_factory=Fake)
topics = sorted(t for t, _ in Fake.sent)
ok("Status und Ereignisse, wenn angeboten", topics == [
    "oaap-node/node1/alive", "oaap-node/node1/events/node.test",
    "oaap-node/node1/status"], topics)
ok("set_classes ändert, alive bleibt", s.set_classes(cdir, ["metrics"]) == ["alive", "metrics"])
mdir, cdir = sender_with(None)
Fake.sent = []
s.run(mdir, cdir, now=NOW, client_factory=Fake)
ok("eine Konfiguration ohne Klassen (von Hand) sendet alles wie bisher",
   "classes" not in s.load_config(cdir) and len(Fake.sent) == 3
   and "classes" not in Fake.sent[0][1], Fake.sent)

print("\n=== C: entfernt ist nicht verstummt ===")
alive_t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))
state = {"started": NOW - 86400, "connected_since": NOW - 86400,
         "nodes": {"node1": {"name": "node1", "via": "", "alive": {"t": alive_t}},
                   "node2": {"name": "node2", "via": "", "alive": {"t": alive_t}}}}
rows = {r["name"]: r for r in fw.view(state, NOW, "hub1", {"node1": NOW - 600})}
ok("entfernter Knoten: removed", rows["node1"]["state"] == "removed", rows)
ok("der andere: silent", rows["node2"]["state"] == "silent", rows)
conds = fw.hub_conditions(state, NOW, "hub1", {}, {"node1": NOW - 600})
ok("keine Lage für den entfernten", "node.silent/node1" not in conds
   and "node.silent/node2" in conds, list(conds))
ok("wieder gekoppelt (neues alive nach dem Entfernen): wieder normal",
   {r["name"]: r for r in fw.view(state, NOW, "hub1", {"node1": NOW - 7200})}
   ["node1"]["state"] == "silent")
ok("das Urteil für den Zwilling sagt es auch",
   fw.verdict(state, NOW, "hub1", {}, {"node1": NOW - 600})["nodes"]["node1"]["state"]
   == "removed")

# ------------------------------------------------------------------ D
try:
    import fcntl  # noqa: F401
except ImportError:
    print("\n(D übersprungen: braucht Linux)")
    print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
    sys.exit(1 if fails else 0)

print("\n=== D: Ende zu Ende, drei Rollen ===")
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-pair-")
import appctl as a                                             # noqa: E402


class Ident:
    def __init__(self):
        self.keys = []

    def __call__(self, op, env=None, stdin=None):
        env = env or {}
        if op == "active":
            return [k["id"] for k in self.keys if k["kind"] == "node"
                    and k["node"] == env["OAAP_P_NODE"] and not k["revoked"]
                    and (not env.get("OAAP_P_LABEL") or k["label"] == env["OAAP_P_LABEL"])]
        if op == "issue":
            kid = f"{len(self.keys) + 1:08x}"
            sec = f"oaapk_{kid}_{'z' * 30}"
            self.keys.append({"id": kid, "kind": env["OAAP_P_KIND"], "node": env.get("OAAP_P_NODE", ""),
                              "label": env["OAAP_P_LABEL"], "revoked": False, "secret": sec,
                              "grants": json.loads(env.get("OAAP_P_GRANTS") or "null")})
            return {"id": kid, "secret": sec, "principal": f"{env['OAAP_P_KIND']}:x",
                    "expires": "2027-11-15T00:00:00Z"}
        if op == "revoke":
            k = next((k for k in self.keys if k["id"] == env["OAAP_P_KID"]), None)
            if k:
                k["revoked"] = True
            return bool(k)
        if op == "login":
            tok = (stdin or "").strip()
            k = next((k for k in self.keys if k["secret"] == tok), None)
            return {"ok": bool(k) and not k["revoked"], "id": (k or {}).get("id", ""),
                    "node": (k or {}).get("node", ""), "label": (k or {}).get("label", "")}
        raise AssertionError(op)


class Role:
    def __init__(self, name, host, profiles):
        self.name, self.host, self.profiles = name, host, set(profiles)
        self.dir = tempfile.mkdtemp(prefix=f"role-{name}-")
        self.ident = Ident()
        self.restarts = 0


ROLES = {}
SAVED = {k: getattr(a, k) for k in ("FLEET_PAIR_DIR", "FLEET_PAIR_FILE", "FLEET_PAIR_VIEW",
                                    "METRICS_SENDER_DIR", "METRICS_DIR", "BROKER_CONF_DIR",
                                    "BROKER_CERT_DIR", "BROKER_CA_DIR", "APPS_DIR", "FLEET_LOG",
                                    "FLEET_STATE")}
CURRENT = []


@contextlib.contextmanager
def as_role(role):
    prev = CURRENT[-1] if CURRENT else None
    CURRENT.append(role)
    _apply(role)
    try:
        yield role
    finally:
        CURRENT.pop()
        if prev:
            _apply(prev)


def _apply(r):
    d = r.dir
    a.FLEET_PAIR_DIR = os.path.join(d, "fleet-pair")
    a.FLEET_PAIR_FILE = os.path.join(d, "fleet-pair", "pair.json")
    a.APPS_DIR = os.path.join(d, "apps")
    a.FLEET_PAIR_VIEW = os.path.join(d, "apps", "fleet-pair-view.json")
    a.FLEET_LOG = os.path.join(d, "apps", "fleet-log.jsonl")
    a.METRICS_SENDER_DIR = os.path.join(d, "metrics-sender")
    a.METRICS_DIR = os.path.join(d, "metrics")
    a.BROKER_CONF_DIR = os.path.join(d, "broker-conf.d")
    a.BROKER_CERT_DIR = os.path.join(d, "broker-certs")
    a.BROKER_CA_DIR = os.path.join(d, "broker-ca")
    a.FLEET_STATE = os.path.join(d, "fleet-state.json")
    a.fleet_node_name = lambda: r.name
    a.has_profile = lambda p: p in r.profiles
    a.load_external = lambda: r.host
    a.lan_address = lambda: "10.0.0.5"
    a._pair_identity = r.ident


def fake_post(host, path, body, fpin, address="", timeout=40):
    target = next(r for r in ROLES.values() if r.host == host)
    CALLS.append((host, path, address))
    with as_role(target):
        good, msg, ans = a.fleet_pair_public({"op": path.rsplit("/", 1)[-1], "args": body})
    return (200, ans) if good else (403, {"ok": False, "error": msg})


def fake_run(cmd, **kw):
    if cmd[:2] == ["docker", "restart"]:
        CURRENT[-1].restarts += 1

    class R:
        stdout = "abc\n"
    return R()


CALLS = []
fp.peer_fingerprint = lambda host, port=443, address="", timeout=5: "ab" * 32
fp.post = fake_post
a.run = fake_run
a.audit_tenant = lambda *x, **k: None
a._pair_broker_ca = lambda: None

H = ROLES["hub"] = Role("hub1", "hub.example", ["fleet-hub", "broker", "exposed", "store"])
E = ROLES["entry"] = Role("entry1", "entry.example", ["broker", "exposed"])
N = ROLES["node"] = Role("node1", "node.example", [])
N2 = ROLES["node2"] = Role("node2", "node2.example", [])

with as_role(E):
    offer, _exp = a.fleet_pair_offer()
with as_role(H):
    msg = a.fleet_pair_entry_add(offer)
    conf = open(os.path.join(a.BROKER_CONF_DIR, "bridge-entry1.conf")).read()
    hub_pair = a._pair_load()
ok("D1 Eingang gekoppelt: Brücke geschrieben, Broker neu gestartet",
   "Eingang entry1 gekoppelt" in msg and H.restarts == 1
   and "connection entry-entry1" in conf and "address entry.example:8883" in conf, msg)
op_key = E.ident.keys[0]
ok("D1 Betreiberschlüssel am Eingang mit genau den drei Rechten (auch die Meldung der Brücke)",
   op_key["kind"] == "operator" and op_key["grants"] == [
       {"filter": "oaap-node/#", "access": "read"},
       {"filter": "oaap-fleet/control/entry1/#", "access": "write"},
       {"filter": "oaap-fleet/bridge/entry1/hub1", "access": "write"}], op_key)
import mqtt_acl  # noqa: E402
ok("D1 die drei Rechte bestehen die Prüfung der Rechte",
   mqtt_acl.parse_grants(op_key["grants"], root="oaap-node") == op_key["grants"])
ok("D1 Zentrale und Eingang teilen das Kopplungsgeheimnis",
   hub_pair["hub"]["entries"]["entry1"]["secret"]
   == json.load(open(os.path.join(E.dir, "fleet-pair", "pair.json")))["entry"]["hubs"]["hub1"]["secret"])
with as_role(H):
    ok("D1 dasselbe Angebot ein zweites Mal: abgelehnt",
       "schon benutzt" in refused(a.fleet_pair_entry_add, offer))

with as_role(H):
    code, _rec = a.fleet_pair_code("node1", via="entry1")
with as_role(N):
    msg = a.fleet_pair_apply(code, ["metrics", "status"])
    cfg = s.load_config(a.METRICS_SENDER_DIR)
    secret = s.load_secret(a.METRICS_SENDER_DIR)
nk = [k for k in E.ident.keys if k["kind"] == "node"]
ok("D2 Knoten gekoppelt: Sender zeigt auf den Eingang, als node1",
   "Gekoppelt" in msg and cfg["url"] == "mqtts://entry.example:8883" and cfg["node"] == "node1"
   and cfg["user"] == nk[0]["id"] and secret == nk[0]["secret"], (msg, cfg))
ok("D2 Klassen wie gewählt, alive immer", cfg["classes"] == ["alive", "metrics", "status"], cfg)
ok("D2 der Schlüssel trägt die Zentrale im Etikett", nk[0]["label"] == "pair:hub1", nk)
ok("D2 der Anruf ging an den Eingang", CALLS[-1][:2] == ("entry.example", "/fleet/pair/claim"))
with as_role(N):
    why = refused(a.fleet_pair_apply, code, None)
ok("D3 derselbe Code noch einmal: abgelehnt", "schon benutzt" in why, why)
with as_role(H):
    code2, _ = a.fleet_pair_code("node1", via="entry1")
with as_role(N):
    why = refused(a.fleet_pair_apply, code2, None)
ok("D4 neuer Code ohne Ersatz: abgelehnt (schon gekoppelt)", "schon gekoppelt" in why, why)
with as_role(H):
    code3, _ = a.fleet_pair_code("node1", via="entry1", replace=True)
with as_role(N):
    a.fleet_pair_apply(code3, None)
    cfg = s.load_config(a.METRICS_SENDER_DIR)
nk = [k for k in E.ident.keys if k["kind"] == "node"]
ok("D4 als Ersatz: alter Schlüssel widerrufen, Sender mit dem neuen",
   nk[0]["revoked"] and not nk[-1]["revoked"] and cfg["user"] == nk[-1]["id"], nk)
with as_role(N):
    msg = a.fleet_pair_classes(["events"])
    ok("D5 Klassen geändert", s.load_config(a.METRICS_SENDER_DIR)["classes"] == ["alive", "events"]
       and a._pair_load()["node"]["classes"] == ["alive", "events"], msg)
with as_role(N):
    view = a._pair_view_write()
    raw = open(a.FLEET_PAIR_VIEW).read()
ok("D6 die Seite des Knotens: gekoppelt mit hub1 über entry1, kein Geheimnis darin",
   view["node"]["hub"] == "hub1" and view["node"]["entry"] == "entry1"
   and nk[-1]["secret"] not in raw and "secret" not in raw, raw[:300])

with as_role(H):
    alive_t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
    with open(a.FLEET_STATE, "w") as f:
        json.dump({"started": 0, "connected_since": 0, "nodes": {
            "entry1/node1": {"name": "node1", "via": "entry1", "alive": {"t": alive_t}}}}, f)
    msg = a.fleet_pair_remove("node1")
    hv = a._pair_view_write()
ok("D7 an der Zentrale entfernt: Eingang widerruft",
   "1 Schlüssel widerrufen" in msg and all(k["revoked"] for k in E.ident.keys
                                            if k["kind"] == "node"), msg)
ok("D7 ... und die Zentrale zeigt ihn als entfernt",
   [n["state"] for n in hv["nodes"] if n["name"] == "node1"] == ["removed"], hv["nodes"])
ok("D7 der Widerruf ging mit HMAC an den Eingang",
   CALLS[-1][:2] == ("entry.example", "/fleet/pair/revoke"))

with as_role(H):
    code4, _ = a.fleet_pair_code("node1", via="entry1", replace=True)
    ok("D8 neuer Code nimmt ihn aus 'entfernt'", "node1" not in a._pair_removed())
with as_role(N):
    a.fleet_pair_apply(code4, None)
    msg = a.fleet_pair_unpair()
    gone = s.load_config(a.METRICS_SENDER_DIR)
ok("D8 Knoten löst selbst: Eingang widerruft, Sender weg",
   "hat den Schlüssel widerrufen" in msg and gone is None
   and E.ident.keys[-1]["revoked"], msg)

with as_role(H):
    dcode, _ = a.fleet_pair_code("node2", lan=True)
with as_role(N2):
    a.fleet_pair_apply(dcode, None)
    cfg2 = s.load_config(a.METRICS_SENDER_DIR)
ok("D9 direkt mit LAN-Adresse: Schlüssel an der Zentrale, Sender verbindet über das LAN",
   cfg2["url"] == "mqtts://hub.example:8883" and cfg2.get("address") == "10.0.0.5"
   and H.ident.keys[-1]["node"] == "node2" and CALLS[-1] == ("hub.example", "/fleet/pair/claim",
                                                             "10.0.0.5"), (cfg2, CALLS[-1]))
with as_role(H):
    with open(a.FLEET_STATE, "w") as f:
        json.dump({"nodes": {"node2": {"name": "node2", "via": "", "alive": {"t": alive_t}}}}, f)
    msg = a.fleet_pair_remove("node2")
ok("D9 direkt entfernt: Schlüssel auf der Zentrale widerrufen",
   "1 Schlüssel auf diesem Knoten widerrufen" in msg and H.ident.keys[-1]["revoked"], msg)

with as_role(H):
    bad = fp.decode(a.fleet_pair_code("node3", via="entry1")[0])
    bad["node"] = "node4"
with as_role(N):
    why = refused(a.fleet_pair_apply, fp.encode(bad), None)
ok("D10 gefälschter Code (Name geändert): abgelehnt", "ungültig" in why, why)
with as_role(E):
    why = refused(a.fleet_pair_code, "node5")
ok("D10 ein Knoten ohne Profil fleet-hub erzeugt keine Codes", "keine Zentrale" in why, why)
with as_role(N):
    good, msg, _ = a.fleet_pair_public({"op": "claim", "args": {"code": code}})
ok("D10 ein Knoten ohne Broker nimmt keine Kopplung an", not good and "Broker" in msg, msg)
with as_role(H):
    good, msg, f = a.fleet_pair_job({"op": "code", "args": {"node": "node6"}},
                                    "tenant_admin", "x")
ok("D11 nur server_admin", not good and "server_admin" in msg)
with as_role(H):
    good, msg, f = a.fleet_pair_job({"op": "code", "args": {"node": "node6", "via": "entry1"}},
                                    "server_admin", "x")
ok("D11 der Auftrag des Portals liefert die Datei", good and f.startswith(fp.PREFIX), msg)
with as_role(N):
    good, msg, _ = a.fleet_pair_public({"op": "unpair", "args": {"node": "node1",
                                                                  "key": "oaapk_falsch"}})
with as_role(E):
    good, msg, _ = a.fleet_pair_public({"op": "unpair", "args": {"node": "node1",
                                                                  "key": "oaapk_falsch"}})
ok("D12 Lösen mit fremdem Schlüssel: abgelehnt", not good and "Nicht berechtigt" in msg, msg)
with as_role(E):
    good, msg, _ = a.fleet_pair_public({"op": "revoke", "args": {
        "hub": "hub1", "node": "node1", "ts": int(time.time()), "nonce": "q",
        "mac": "0" * 64}})
ok("D12 Widerruf ohne gültige Unterschrift: abgelehnt", not good and "Nicht berechtigt" in msg)
for k, v in SAVED.items():
    setattr(a, k, v)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
