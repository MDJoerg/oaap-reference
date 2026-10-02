#!/usr/bin/env python3
"""Rechte am Broker: Schluessel mit Rechteliste, Knoten- und Betreiberschluessel
(RFC-0054, Stufe 2).

Zwei Teile. Erst die reinen Regeln (`services/mqtt_acl.py`, ohne Flask), dann
dieselben Regeln durch die echten Wege: `/mqtt-auth/getuser` und
`/mqtt-auth/aclcheck` mit Flasks Testclient, die Ausstellung durch
`oaap key issue` (appctl, mit dem echten Identity-Code statt eines
Containers), und die Sichtbarkeit in der Schluesselliste.

Was geprueft wird, weil es am ehesten still schiefginge:

    ein Schluessel von vor diesem Stand verhaelt sich genau wie vorher;
    ein Knoten- oder Betreiberschluessel oeffnet NIE eine HTTP-Tuer;
    unter der Metrik-Wurzel schreibt nur der passende Knoten -- auch
    wenn ein anderer Schluessel ein ueberlappendes Recht traegt;
    ein Abonnement, das breiter ist als das Recht, wird abgelehnt;
    kein Mandant sieht oder sperrt einen Knoten- oder Betreiberschluessel.

Was diese Datei NICHT pruefen kann: dass Mosquitto die Verweigerung als
Code 0x87 quittiert -- das ist am 02.10. auf oaap-test gemessen (RFC-0054
1.7), nicht hier.

Aufruf: python3 test/test_mqtt_acl.py
"""
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
sys.path.insert(0, SERVICES)
sys.path.insert(0, IDENTITY_DIR)
sys.path.insert(0, PLATFORM)

import mqtt_acl as acl                                         # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


print("=== die reinen Regeln ===")
for f, good in (("home/#", True), ("a/+/b", True), ("a", True), ("#", True),
                ("a/#/b", False), ("a/b#", False), ("a/+b", False), ("", False),
                ("a//b", False), ("a/", False), ("/a", False), ("a\x00b", False),
                ("x" * 201, False)):
    ok(f"Filter {f[:20]!r} ist {'gueltig' if good else 'ungueltig'}",
       acl.valid_filter(f) is good)
cov = (("home/#", "home/kitchen/light", True), ("home/#", "home", True),
       ("home/#", "home/+/x", True), ("home/#", "home/#", True),
       ("home/+/set", "home/kitchen/set", True), ("home/+/set", "home/+/set", True),
       ("home/+/set", "home/#", False), ("home/+/set", "home/kitchen/get", False),
       ("home/kitchen", "home/+", False), ("home/kitchen", "home/kitchen/x", False),
       ("home/#", "garage/x", False), ("a/b", "a", False), ("a/#", "#", False),
       ("#", "a/b", True))
for g, r, want in cov:
    ok(f"Recht {g!r} deckt {r!r}: {want}", acl.covers(g, r) is want)
for a, b, want in (("a/#", "a/b", True), ("a/b", "a/c", False), ("+/x", "a/x", True),
                   ("+/x", "a/y", False), ("a/#", "a", True), ("a", "a/b", False),
                   ("#", "z", True), ("oaap-node/#", "oaap-node/n1/metrics/cpu", True),
                   ("oaap-node/#", "home/x", False), ("oaap-node/#", "+/n1/metrics", True)):
    ok(f"Ueberlappung {a!r} / {b!r}: {want}", acl.overlaps(a, b) is want)
ok("Zugriffsart: 2 schreibt, 1/4/8 lesen, anderes ist keine Frage",
   [acl.need(x) for x in (2, 1, 4, 8, 3, 0, "x", None)]
   == ["write", "read", "read", "read", None, None, None, None])
for raw, why in (([], "leer"), ("x", "keine Liste"),
                 ([{"filter": "oaap/#", "access": "read"}], "Mandantenbaum"),
                 ([{"filter": "#", "access": "read"}], "alles"),
                 ([{"filter": "+/x", "access": "read"}], "Plus vorn"),
                 ([{"filter": "$SYS/#", "access": "read"}], "$-Themen"),
                 ([{"filter": "home/#", "access": "alles"}], "Zugriffsart"),
                 ([{"filter": "home/#/x", "access": "read"}], "ungueltiger Filter"),
                 ([{"access": "read"}], "ohne Filter"),
                 ([{"filter": f"h{i}/#", "access": "read"} for i in range(51)], "zu viele")):
    try:
        acl.parse_grants(raw)
        ok(f"Rechte {why} werden abgelehnt", False)
    except ValueError as e:
        ok(f"Rechte {why} werden abgelehnt", bool(str(e)))
ok("gueltige Rechte kommen sauber zurueck",
   acl.parse_grants([{"filter": "home/#", "access": "readwrite", "x": 1}])
   == [{"filter": "home/#", "access": "readwrite"}])

R = "oaap-node"
tenant_rule = lambda t, ten: bool(ten) and (t == f"oaap/{ten}" or t.startswith(f"oaap/{ten}/"))  # noqa: E731
node = {"kind": "node", "node": "oaapx02"}
op = {"kind": "operator", "grants": [{"filter": "home/#", "access": "readwrite"},
                                     {"filter": "events/+/in", "access": "read"},
                                     {"filter": "oaap-node/#", "access": "readwrite"}]}
ten = {"tenant": "t1"}


def d(rec, topic, acc):
    return acl.decide(rec, topic, acc, R, tenant_rule)[0]


print("\n=== Mandantenschluessel: wie vorher ===")
ok("ohne `kind` ist es ein Mandantenschluessel (alte Eintraege)",
   d(ten, "oaap/t1/x", 2) and d(ten, "oaap/t1/x", 1) and d(ten, "oaap/t1/+", 4))
ok("... und bleibt in seinem Baum", not d(ten, "oaap/t2/x", 2) and not d(ten, "home/x", 2)
   and not d(ten, "#", 4))
ok("... ein Schluessel ohne Mandant darf nichts", not d({}, "oaap/t1/x", 2))
ok("... und schreibt nie unter der Metrik-Wurzel", not d(dict(ten, tenant="oaap-node"), "oaap-node/x", 2))

print("\n=== Knotenschluessel ===")
ok("schreibt unter seinem Namen", d(node, "oaap-node/oaapx02/metrics/cpu", 2))
ok("... auch tiefer", d(node, "oaap-node/oaapx02/metrics/disk", 2))
ok("nicht unter einem anderen Knoten", not d(node, "oaap-node/oaapx01/metrics/cpu", 2))
ok("nicht auf der Wurzel selbst", not d(node, "oaap-node", 2))
ok("nicht anderswo", not d(node, "home/x", 2) and not d(node, "oaap/t1/x", 2))
ok("liest und abonniert NIE -- auch seinen eigenen Zweig nicht",
   not any(d(node, t, a) for t in ("oaap-node/oaapx02/#", "oaap-node/oaapx02/metrics/cpu", "#")
           for a in (1, 4, 8)))
ok("ein Knotenschluessel ohne gueltigen Namen darf nichts",
   not d({"kind": "node", "node": ""}, "oaap-node//x", 2)
   and not d({"kind": "node", "node": "../x"}, "oaap-node/../x/y", 2))
ok("mit anderer Wurzel gilt die andere",
   acl.decide(node, "home/nodes/oaapx02/metrics/cpu", 2, "home/nodes", tenant_rule)[0]
   and not acl.decide(node, "oaap-node/oaapx02/metrics/cpu", 2, "home/nodes", tenant_rule)[0])

print("\n=== Betreiberschluessel ===")
ok("schreibt und liest, wo readwrite steht", d(op, "home/kitchen/light", 2) and d(op, "home/kitchen/light", 1)
   and d(op, "home/#", 4))
ok("nur lesen, wo read steht", d(op, "events/a/in", 1) and d(op, "events/a/in", 4)
   and not d(op, "events/a/in", 2))
ok("ein Abonnement breiter als das Recht wird abgelehnt",
   not d(op, "events/+/#", 4) and not d(op, "#", 4) and not d(op, "events/#", 4))
ok("nichts ausserhalb der Rechte", not d(op, "garage/x", 2) and not d(op, "oaap/t1/x", 1))
ok("`$`-Themen nie", not d({"kind": "operator", "grants": [{"filter": "#", "access": "readwrite"}]},
                          "$SYS/broker/uptime", 1))
ok("DIE METRIK-WURZEL: ein ueberlappendes Schreibrecht nuetzt nichts",
   not d(op, "oaap-node/oaapx02/metrics/cpu", 2) and not d(op, "oaap-node/x", 2))
ok("... Lesen derselben Wurzel ist ein gewoehnliches Recht", d(op, "oaap-node/oaapx02/metrics/cpu", 1)
   and d(op, "oaap-node/#", 4))
ok("... auch ein Recht mit Plus ueber der Wurzel schreibt dort nicht",
   not d({"kind": "operator", "grants": [{"filter": "+/oaapx02/#", "access": "write"}]},
         "oaap-node/oaapx02/metrics/cpu", 2))
ok("ohne Rechte darf er nichts", not d({"kind": "operator", "grants": []}, "home/x", 1))
ok("unbekannte Art: wie ein Mandantenschluessel ohne Mandanten (nichts)",
   not d({"kind": "gast"}, "home/x", 2))

print("\n=== durch die echten Wege (Identity) ===")
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich hier nicht laden.")
    print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
    sys.exit(1 if fails else 0)


def load_identity(data_dir, root=None):
    os.environ["SESSION_SECRET"] = "test-session-secret"
    os.environ["SETUP_TOKEN"] = "test-setup-token"
    os.environ["INTERNAL_API_KEY"] = "test-internal-key"
    os.environ["OAAP_IDENTITY_DATA_DIR"] = data_dir
    if root is None:
        os.environ.pop("OAAP_METRICS_ROOT", None)
    else:
        os.environ["OAAP_METRICS_ROOT"] = root
    sys.modules.pop("app", None)
    sys.modules.pop("mqtt_acl", None)
    mm = importlib.reload(importlib.import_module("app"))
    mm.USERS_FILE = os.path.join(data_dir, "users.json")
    mm.KEYS_FILE = os.path.join(data_dir, "api-keys.json")
    mm.THROTTLE_FILE = os.path.join(data_dir, "login-throttle.json")
    mm.AUDIT_LOG = os.path.join(data_dir, "audit.jsonl")
    mm.TENANTS_FILE = os.path.join(data_dir, "tenants.json")
    return mm


def user(name, roles, kind="human", tenant=""):
    return {"username": name, "display_name": "", "password_hash": "", "kind": kind,
            "roles": roles, "groups": [], "tenant": tenant, "active": True,
            "session_epoch": 0}


DATA = tempfile.mkdtemp(prefix="oaap-mqtt-acl-")
m = load_identity(DATA)
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"}, "t-cls": {"label": "cls"}}}, f)
with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump([user("joerg", ["server_admin", "admin", "user"], tenant="t-default"),
               user("cls_admin", ["tenant_admin", "admin", "keyuser", "user"], tenant="t-cls"),
               user("sensor-1", ["user"], kind="machine", tenant="t-cls")], f)
client = m.app.test_client()


def post(route, body):
    return client.post(f"{route}?k=test-internal-key", json=body).get_json()


def login(token, user=None):
    kid = token.split("_")[1]
    return post("/mqtt-auth/getuser", {"username": user or kid, "password": token})["Ok"]


def may(token_or_id, topic, acc):
    kid = token_or_id.split("_")[1] if token_or_id.startswith("oaapk_") else token_or_id
    return post("/mqtt-auth/aclcheck", {"username": kid, "topic": topic, "acc": acc})["Ok"]


old_rec, old_tok = m.issue_key(m.load_users(), "sensor-1", ["user"], "", "Sensor", 30, "joerg")
nrec, ntok = m.issue_broker_key("node", "oaapx02", "oaapx02", None, "RFC-0052 sender", 365, "root")
orec, otok = m.issue_broker_key("operator", "haus", None,
                                [{"filter": "home/#", "access": "readwrite"},
                                 {"filter": "oaap-node/#", "access": "readwrite"}],
                                "Smarthome", 365, "root")

print("-- Ausstellen")
ok("ein Knotenschluessel traegt Art und Knotenname, keinen Mandanten und keine Rollen",
   nrec["kind"] == "node" and nrec["node"] == "oaapx02" and nrec["tenant"] == ""
   and nrec["roles"] == [] and nrec["principal"] == "node:oaapx02", nrec)
ok("der Hauptname enthaelt einen Doppelpunkt (kein Benutzername kann so heissen)",
   ":" in nrec["principal"] and ":" in orec["principal"])
ok("gespeichert wird nur ein Hash", ntok.split("_", 2)[2] not in json.dumps(m.load_keys()))
ok("die oeffentliche Fassung zeigt Art, Knoten und Rechte -- ohne Hash",
   m.public_key(orec)["kind"] == "operator" and len(m.public_key(orec)["grants"]) == 2
   and m.public_key(nrec)["node"] == "oaapx02" and "hash" not in m.public_key(nrec))
ok("ein alter Schluessel ist ein Mandantenschluessel", m.public_key(old_rec)["kind"] == "tenant")
for kw, why in ((dict(kind="gast", name="x", node=None, grants=None), "falsche Art"),
                (dict(kind="node", name="X!", node="n1", grants=None), "schlechter Name"),
                (dict(kind="node", name="n1", node="", grants=None), "Knoten fehlt"),
                (dict(kind="node", name="n1", node="Bad_Name", grants=None), "Knotenname ungueltig"),
                (dict(kind="node", name="n1", node="n1", grants=[{"filter": "a", "access": "read"}]),
                 "Knoten mit Rechten"),
                (dict(kind="operator", name="op", node="n1", grants=[{"filter": "a/#", "access": "read"}]),
                 "Betreiber mit Knoten"),
                (dict(kind="operator", name="op", node=None, grants=[]), "Betreiber ohne Rechte"),
                (dict(kind="operator", name="op", node=None,
                      grants=[{"filter": "oaap/#", "access": "read"}]), "Betreiber auf Mandantenbaum")):
    try:
        m.issue_broker_key(kw["kind"], kw["name"], kw["node"], kw["grants"], "", 30, "root")
        ok(f"abgelehnt: {why}", False)
    except ValueError as e:
        ok(f"abgelehnt: {why}", bool(str(e)))
for days in (0, 366, "abc"):
    try:
        m.issue_broker_key("node", "n1", "n1", None, "", days, "root")
        ok(f"Gueltigkeit {days!r} wird abgelehnt", False)
    except ValueError:
        ok(f"Gueltigkeit {days!r} wird abgelehnt", True)

print("-- die Anmeldung am Broker (getuser)")
ok("ein Knotenschluessel meldet sich an", login(ntok))
ok("ein Betreiberschluessel meldet sich an", login(otok))
ok("ein alter Mandantenschluessel meldet sich weiter an", login(old_tok))
ok("falsches Geheimnis, falscher Benutzername, Muell: abgelehnt",
   not login(ntok[:-3] + "xxx") and not login(ntok, user="nobody")
   and not login(ntok, user=orec["id"]) and not post("/mqtt-auth/getuser", {"username": "x", "password": "y"})["Ok"])
m.revoke_key(nrec["id"])
ok("ein gesperrter Knotenschluessel meldet sich nicht mehr an (sofort)", not login(ntok))
nrec, ntok = m.issue_broker_key("node", "oaapx02", "oaapx02", None, "neu", 365, "root")

print("-- die HTTP-Tueren bleiben zu")


def by_key(tok, inst):
    with m.app.test_request_context("/"):
        return m._by_key(tok, inst)


for tok, label in ((ntok, "Knoten"), (otok, "Betreiber")):
    user_, method, err = by_key(tok, "")
    ok(f"{label}schluessel oeffnet keinen HTTP-Weg (403, nur fuer den Broker)",
       user_ is None and err is not None and err[1] == 403 and "broker" in err[0].lower(), err)
    user2, _m2, err2 = by_key(tok, "any-instance")
    ok(f"... auch nicht mit Instanz", user2 is None and err2 is not None)
ok("der alte Mandantenschluessel geht auf HTTP weiter", by_key(old_tok, "")[0] is not None)

print("-- die Rechte (aclcheck), nach dem Wirken")
nt = lambda t, a=2: may(ntok, t, a)                      # noqa: E731
ok("Knoten: schreibt unter seinem Namen", nt("oaap-node/oaapx02/metrics/cpu"))
ok("Knoten: nicht unter einem anderen Namen, nicht lesend, nicht abonnierend",
   not nt("oaap-node/oaapx01/metrics/cpu") and not nt("oaap-node/oaapx02/#", 4)
   and not nt("oaap-node/oaapx02/metrics/cpu", 1))
ok("Betreiber: schreibt im Smarthome-Baum, liest dort, abonniert ihn",
   may(otok, "home/kitchen/light", 2) and may(otok, "home/kitchen/light", 1) and may(otok, "home/#", 4))
ok("Betreiber: DIE METRIK-WURZEL -- sein ueberlappendes Schreibrecht greift nicht, Lesen schon",
   not may(otok, "oaap-node/oaapx02/metrics/cpu", 2) and may(otok, "oaap-node/#", 4))
ok("Mandantenschluessel: wie vorher",
   may(old_tok, "oaap/t-cls/x", 2) and may(old_tok, "oaap/t-cls/x", 1) and not may(old_tok, "oaap/t-default/x", 2)
   and not may(old_tok, "oaap-node/x", 2))
ok("ein unbekannter Schluessel darf nichts", not may("deadbeef", "home/x", 2))
ok("ein gesperrter darf nichts mehr (sofort)", (m.revoke_key(orec["id"]), not may(otok, "home/x", 2))[1])
exp = m.load_keys()
for k in exp:
    if k["id"] == old_rec["id"]:
        k["expires"] = "2000-01-01T00:00:00Z"
with open(m.KEYS_FILE, "w", encoding="utf-8") as f:
    json.dump(exp, f)
ok("ein abgelaufener Schluessel darf bei einer laufenden Verbindung nichts mehr",
   not may(old_tok, "oaap/t-cls/x", 2))
ok("ohne den internen Schluessel antwortet die Route nicht",
   client.post("/mqtt-auth/aclcheck", json={"username": nrec["id"], "topic": "x", "acc": 2}).get_json()["Ok"] is False)

print("-- Sichtbarkeit: Mandanten sehen sie nicht")
ok("ein server_admin sieht beide Arten", m._key_visible("server_admin", "t-default", "joerg", nrec)
   and m._key_visible("server_admin", "t-default", "joerg", orec))
ok("kein tenant_admin sieht oder sperrt sie -- auch nicht der des Standardmandanten",
   not m._key_visible("tenant_admin", "t-cls", "cls_admin", nrec)
   and not m._key_visible("tenant_admin", "t-default", "x", nrec)
   and not m._key_visible("tenant_admin", "t-default", "x", orec))
ok("... und ein gewoehnlicher Benutzer erst recht nicht",
   not m._key_visible("user", "t-cls", "node:oaapx02", nrec))
HDR = {"X-OAAP-Internal-Key": "test-internal-key"}
r = client.get("/internal/keys?actor=cls_admin", headers=HDR)
keys = (r.get_json() or {}).get("keys", [])
ok("die Liste des Mandantenverwalters enthaelt sie nicht -- aber seine eigenen Schluessel",
   r.status_code == 200 and any(k["id"] == old_rec["id"] for k in keys)
   and not [k for k in keys if k.get("kind") in ("node", "operator")], (r.status_code, keys))
r = client.get("/internal/keys?actor=joerg", headers=HDR)
ok("die Liste des server_admin enthaelt sie",
   {k["id"] for k in (r.get_json() or {}).get("keys", [])} >= {nrec["id"], orec["id"]})
r = client.post(f"/internal/keys/{nrec['id']}/revoke", json={"actor": "cls_admin"}, headers=HDR)
ok("... und er kann sie nicht sperren", r.status_code == 403, (r.status_code, r.get_data(as_text=True)[:200]))

print("-- eine andere Wurzel (Einstellung des Brokerknotens)")
m2 = load_identity(tempfile.mkdtemp(prefix="oaap-mqtt-acl2-"), root="home/nodes")
ok("die Einstellung wird gelesen", m2.METRICS_ROOT == "home/nodes")
m3 = load_identity(tempfile.mkdtemp(prefix="oaap-mqtt-acl3-"), root="/boese/#")
ok("eine ungueltige Wurzel faellt auf die Vorgabe zurueck, nicht auf 'ungeschuetzt'",
   m3.METRICS_ROOT == "oaap-node")

print("\n=== `oaap key issue --kind` (der echte Weg an der Maschine) ===")
import argparse                                                 # noqa: E402

os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-mqtt-acl-data-")
DATA2 = tempfile.mkdtemp(prefix="oaap-mqtt-acl4-")
mi = load_identity(DATA2)
with open(mi.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump([user("joerg", ["server_admin", "admin", "user"], tenant="t-default")], f)
with open(mi.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"}}}, f)
import appctl as a                                              # noqa: E402

audits = []
a.audit_tenant = lambda *x, **k: audits.append((x, k))


def fake_identity_exec(code, env=None):
    """Der Identity-Code, im selben Prozess statt in einem Container."""
    buf = io.StringIO()
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<identity>", "exec"), {"__name__": "x"})
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return buf.getvalue()


a.IDENTITY_APP = mi
sys.modules["app"] = mi
a._identity_exec = fake_identity_exec


def key_cmd(**kw):
    ns = argparse.Namespace(action="issue", name=None, roles="user", instance="", label="",
                            days=90, kind="tenant", node=None, grant=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    buf, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            a.cmd_key(ns)
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue() + err.getvalue(), code


out, code = key_cmd(name="oaapx02", kind="node", node="oaapx02")
ok("--kind node stellt aus und sagt, was er darf und dass er nur den Broker oeffnet",
   code == 0 and "may publish under oaap-node/oaapx02/" in out and "cannot subscribe" in out
   and "broker and nothing else" in out and "oaapk_" in out, out)
ok("... gueltig 365 Tage (Vorgabe fuer Broker-Schluessel)",
   mi.load_keys()[-1]["expires"][:4] == str(int(mi._now_iso()[:4]) + 1)
   or (mi.load_keys()[-1]["expires"] > mi._in_days_iso(360)), mi.load_keys()[-1]["expires"])
ok("... und nennt den MQTT-Benutzernamen (die Schluessel-Id) und den Weg zum Sender",
   "MQTT user name:" in out and "oaap metrics sender set" in out)
out, code = key_cmd(name="haus", kind="operator", grant=["home/#:readwrite", "events/+/in:read"])
ok("--kind operator zeigt die Rechte", code == 0 and "readwrite  home/#" in out and "read       events/+/in" in out, out)
for kw, why in ((dict(name="x", kind="operator", grant=["oaap/#:read"]), "Mandantenbaum"),
                (dict(name="x", kind="operator", grant=["kein-doppelpunkt"]), "Recht ohne Zugriffsart"),
                (dict(name="x", kind="node", node="Falsch!"), "Knotenname"),
                (dict(name=None, kind="node", node="n1"), "ohne Namen"),
                (dict(name="x", kind="operator"), "ohne Rechte")):
    out, code = key_cmd(**kw)
    ok(f"abgelehnt: {why}", code != 0, out)
rows = [a._key_row(mi.public_key(k)) for k in mi.load_keys()]
ok("die Liste nennt sie als Broker-Schluessel, mit Knoten bzw. Zahl der Rechte",
   any("(MQTT broker)" in r and "node oaapx02" in r for r in rows)
   and any("(MQTT broker)" in r and "2 grant(s)" in r for r in rows), rows)
ok("die Ausstellung steht im Protokoll, ohne Geheimnis",
   len(audits) >= 2 and all("oaapk_" not in json.dumps(x) for x in audits), audits[:1])
ok("der Aufruf mit dem Mandantenweg (Standard) ist unveraendert",
   key_cmd(name="nobody")[1] != 0)

print("\n=== Bild, Compose, Dateien ===")
df = open(os.path.join(IDENTITY_DIR, "Dockerfile"), encoding="utf-8").read()
ok("das Image enthaelt mqtt_acl.py (sonst Neustartschleife, CURRENT_STATE 132)",
   "COPY mqtt_acl.py ." in df)
dc = open(os.path.join(PLATFORM, "docker-compose.yml"), encoding="utf-8").read()
ok("Compose reicht die Wurzel an identity durch",
   'OAAP_METRICS_ROOT: "${OAAP_METRICS_ROOT:-}"' in dc)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
