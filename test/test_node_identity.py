#!/usr/bin/env python3
"""Was ein Knoten über sich sagt: Alias, Ort, Tags (RFC-0082 Stufe 1).

    Die Regeln     Alias wie beim Metrik-Sender, Ort bis sechs Stufen, bis
                   zehn Tags; ohne Eintrag gilt der Rechnername — und dann
                   ändert sich nichts (Betreff, Archivname).
    Die Leser      Betreff [alias], der Text nennt beide Namen und den Ort;
                   Archivnamen und Begleitblatt; der Flottenstatus 0.9.
    Die Tür        nur server_admin, auch über den Spool; ein Restore sagt,
                   welchen Namen er mitgebracht hat.

Braucht kein Docker, keinen Knoten, keinen Mailserver.
Aufruf: python3 test/test_node_identity.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-node-identity-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import notify as n  # noqa: E402
import fleet_view  # noqa: E402
import appctl as a  # noqa: E402

a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def refused(fn, *x, **k):
    try:
        fn(*x, **k)
    except ValueError as e:
        return str(e)
    return ""


dflt = a.ensure_default_tenant()
a.socket.gethostname = lambda: "OAAPX01.example"
a.load_external = lambda: "oaap.joomp.de"

print("=== die Regeln ===")
ok("Standard-Alias: der Rechnername, normalisiert", a.alias_from_hostname() == "oaapx01")
ok("… aus Unsinn wird etwas Gültiges", a.alias_from_hostname("Mein_PC!") == "mein-pc"
   and a.alias_from_hostname("_") == "oaap-node")
ident = a.node_identity()
ok("ohne Eintrag: Alias = Rechnername, nicht gesetzt, kein Ort",
   ident["alias"] == "oaapx01" and not ident["alias_set"] and ident["place"] == "", ident)
ok("… und ein Betreff sagt, was er immer sagte (externer Name)",
   a.node_mail_name() == "oaap.joomp.de")
ok("… und ein Archiv heißt wie immer (roher Rechnername)",
   a.node_file_name() == "OAAPX01.example")
ok("ein Alias mit Unterstrich abgelehnt",
   refused(a.node_identity_set, alias="Oaap_x") != "")
ok("ein Zeichen ist kein Alias", "not an alias" in refused(a.node_identity_set, alias="x"))
ok("sieben Stufen sind kein Ort", "at most 6" in refused(a.node_identity_set,
                                                         place_text="a/b/c/d/e/f/g"))
ok("ein Ort ohne Leerzeichen", "not a place level" in refused(a.node_identity_set,
                                                              place_text="de/data center"))
ok("elf Tags sind zu viele", "at most 10" in refused(
    a.node_identity_set, tags=",".join(f"t{i}" for i in range(11))))
ok("nichts geschrieben, solange nur abgelehnt wurde", not os.path.exists(a.NODE_IDENTITY_FILE))

print("=== setzen ===")
said = a.node_identity_set(alias="oaap-x1", place_text="/de/DC1/group3/",
                           tags="kunde-cls, test test", who="joerg")
ident = a.node_identity()
ok("Alias, Ort (normalisiert), Tags (einmalig, sortiert)",
   ident["alias"] == "oaap-x1" and ident["alias_set"] and ident["place"] == "de/dc1/group3"
   and ident["tags"] == ["kunde-cls", "test"], (said, ident))
ok("die Datei ist lesbar für das Portal (0644)",
   oct(os.stat(a.NODE_IDENTITY_FILE).st_mode & 0o777) in ("0o644", "0o666"))
ok("im Protokoll des Knotens", any(e.get("action") == "node.identity" and "oaap-x1" in e.get("detail", "")
                                   for e in a.read_tenant_log(dflt, limit=10)))
ok("noch einmal dasselbe: unverändert", a.node_identity_set(alias="oaap-x1") == "Unchanged.")
ok("nur den Ort ändern lässt Alias und Tags", (a.node_identity_set(place_text="de/dc2"),
   a.node_identity())[1]["tags"] == ["kunde-cls", "test"] and a.node_alias() == "oaap-x1")
raw = json.load(open(a.NODE_IDENTITY_FILE))
raw["place"] = ["de", "a/b"]
raw["alias"] = "BAD alias"
json.dump(raw, open(a.NODE_IDENTITY_FILE, "w"))
ident = a.node_identity()
ok("eine von Hand verdorbene Datei: ungültiges wird ignoriert, nicht geglaubt",
   ident["alias"] == "oaapx01" and ident["place"] == "" and not ident["alias_set"], ident)
a.node_identity_set(alias="oaap-x1", place_text="de/dc1/group3")

print("=== die Leser: Mails ===")
item = {"tenant": dflt, "type": "backup.failed", "kurz": ""}
v = a._notify_values(item, dflt)
ok("Betreff [oaap-x1]", n.expand(n.CATALOGUE["backup.failed"]["subject"], v)
   == "[oaap-x1] Backup fehlgeschlagen", v)
body = n.body("backup.failed", "open", v, "tar exit 2")
ok("der Text nennt beide Namen und den Ort",
   "Server: oaap-x1 (oaap.joomp.de)" in body and "Ort: de/dc1/group3" in body, body)
ok("{ort} ist ein Platzhalter der Vorlagen", n.expand("[{knoten}] {ort}", v)
   == "[oaap-x1] de/dc1/group3")
ok("der Absendername des Knotens", a.node_mail_name() == "oaap-x1")

print("=== die Leser: Archive, Flotte ===")
ok("das Archiv heißt nach dem Alias", a.node_file_name() == "oaap-x1")
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("Knoten- und Mandantenarchiv tragen ihn im Namen, das Begleitblatt Alias und Ort",
   src.count("{node_file_name()}-") >= 3 and '"alias": node_alias(),' in src)
doc = fleet_view.build_document("oaap.joomp.de", "0.1.260", [], "2026-11-12T00:00:00Z",
                                [], [], [], [], identity={"alias": "oaap-x1",
                                                          "place": "de/dc1/group3",
                                                          "tags": ["test"]})
ok("Flottenstatus 0.9: node bleibt, alias/place/tags kommen dazu",
   doc["schema"] == "oaap.fleet.status/0.9" and doc["node"] == "oaap.joomp.de"
   and doc["alias"] == "oaap-x1" and doc["place"] == "de/dc1/group3" and doc["tags"] == ["test"], doc)
ok("… ohne Angabe fehlen die Felder (wie bisher)",
   "alias" not in fleet_view.build_document("x", "v", [], "t", [], [], [], []))

print("=== die Minutenaufgabe hält den Rechnernamen fürs Portal aktuell ===")
ok("default steht schon (beim Setzen geschrieben)",
   json.load(open(a.NODE_IDENTITY_FILE)).get("default") == "oaapx01")
a.socket.gethostname = lambda: "neuer-name"
ok("nach einer Umbenennung: einmal neu geschrieben, dann nicht mehr",
   a.node_identity_refresh() is True and a.node_identity_refresh() is False
   and json.load(open(a.NODE_IDENTITY_FILE))["default"] == "neuer-name"
   and a.node_alias() == "oaap-x1")

print("=== die Tür ===")
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "boss", "roles": ["server_admin"], "active": True, "tenant": dflt},
               {"username": "vorstand", "roles": ["tenant_admin"], "active": True,
                "tenant": dflt}], f)
QUEUE = os.path.join(a.SPOOL_DIR, "queue")
_rid = [0]


def queue(**req):
    _rid[0] += 1
    rid = f"n{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": "", **req}, f)
    with contextlib.redirect_stdout(io.StringIO()):
        a.cmd_process_deploys(None)
    with open(os.path.join(a.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


r = queue(action="node-identity", alias="fremd", place="", tags="", by="vorstand")
ok("ein tenant_admin setzt den Namen nicht", not r.get("ok") and a.node_alias() == "oaap-x1", r)
r = queue(action="node-identity", alias="fremd", place="", tags="")
ok("ohne Auftraggeber: abgelehnt", not r.get("ok") and a.node_alias() == "oaap-x1", r)
r = queue(action="node-identity", alias="oaap-test", place="de/home/lab", tags="test",
          by="boss")
ok("der server_admin über den Spool", r.get("ok") and a.node_alias() == "oaap-test"
   and a.node_identity()["place"] == "de/home/lab", r)
r = queue(action="node-identity", alias="", place="", tags="", by="boss")
ok("leere Felder: zurück auf den Rechnernamen", r.get("ok") and not a.node_identity()["alias_set"]
   and a.node_identity()["place"] == "", r)

print("=== der Restore sagt, was er mitbrachte ===")
with open(os.path.join(DATA, "last-restore-manifest.json"), "w", encoding="utf-8") as f:
    json.dump({"alias": "oaapx01", "alias_set": True, "place": "de/strato"}, f)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    a._report_restored_identity()
ok("„dieser Knoten nennt sich oaapx01 … wenn das eine Kopie ist“",
   "calls itself 'oaapx01'" in buf.getvalue() and "COPY" in buf.getvalue(), buf.getvalue())
with open(os.path.join(DATA, "last-restore-manifest.json"), "w", encoding="utf-8") as f:
    json.dump({"alias": "oaapx01", "alias_set": False}, f)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    a._report_restored_identity()
ok("… ohne gesetzten Alias kein Wort", buf.getvalue() == "")

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
