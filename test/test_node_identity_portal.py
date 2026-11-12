#!/usr/bin/env python3
"""Der Name im Verbund auf der Gesundheitsseite (RFC-0082 Stufe 1).

    server_admin   sieht Alias, Ort, Tags und das Formular; Speichern ist
                   eine Anfrage `node-identity` im Spool.
    support        sieht den Namen, nicht das Formular; ein POST wird
                   abgewiesen.

Läuft im Portal-Abbild (Flask). Aufruf: python3 test/test_node_identity_portal.py
"""
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


TMP = tempfile.mkdtemp()
portal.NODE_IDENTITY_FILE = os.path.join(TMP, "node-identity.json")
portal.SPOOL_DIR = os.path.join(TMP, "spool")
portal.SPOOL_QUEUE = os.path.join(portal.SPOOL_DIR, "queue")
portal.SPOOL_RESULTS = os.path.join(portal.SPOOL_DIR, "results")
os.makedirs(portal.SPOOL_RESULTS, exist_ok=True)
portal.NODE_IDENTITY_WAIT = 3
ROLES = {"v": {"server_admin"}}
portal.caller_roles = lambda: ROLES["v"]
portal._same_origin = lambda: True
SENT = []


def answer():
    import time
    for _ in range(40):
        for fn in os.listdir(portal.SPOOL_QUEUE) if os.path.isdir(portal.SPOOL_QUEUE) else []:
            path = os.path.join(portal.SPOOL_QUEUE, fn)
            SENT.append(json.load(open(path)))
            os.remove(path)
            json.dump({"ok": True, "message": "This node: alias oaap-test."},
                      open(os.path.join(portal.SPOOL_RESULTS, fn), "w"))
            return
        time.sleep(0.1)


json.dump({"default": "oaap-test-vm"}, open(portal.NODE_IDENTITY_FILE, "w"))
v = portal.node_identity_view()
ok("ohne Alias: der Rechnername, den der Host hinterlegt hat",
   v["alias"] == "oaap-test-vm" and not v["alias_set"], v)
json.dump({"default": "oaap-test-vm", "alias": "oaap-test", "place": ["de", "home", "lab"],
           "tags": ["test", "BAD TAG"]}, open(portal.NODE_IDENTITY_FILE, "w"))
v = portal.node_identity_view()
ok("mit Alias, Ort; ein ungültiger Tag fällt weg",
   v["alias"] == "oaap-test" and v["place"] == "de/home/lab" and v["tags"] == ["test"], v)
rows = portal.node_values()
row = next((r for r in rows if r["name"] == "Name im Verbund"), None)
ok("die Knoten-Tabelle hat die Zeile „Name im Verbund“",
   row and row["label"] == "oaap-test" and "de/home/lab" in row["detail"], rows[:2])

cl = portal.app.test_client()
H = {"X-OAAP-User": "boss", "X-OAAP-Roles": "server_admin"}
th = threading.Thread(target=answer)
th.start()
r = cl.post("/node/identity", data={"alias": "oaap-test", "place": "de/home/lab", "tags": "test"},
            headers=H)
th.join()
ok("server_admin: eine Anfrage node-identity mit den drei Feldern",
   SENT and SENT[-1]["action"] == "node-identity" and SENT[-1]["alias"] == "oaap-test"
   and SENT[-1]["place"] == "de/home/lab" and SENT[-1]["tags"] == "test", SENT)
ok("… zurück auf /health mit der Antwort", "/health?msg=" in r.headers.get("Location", ""),
   r.headers.get("Location"))
with portal.app.test_request_context():
    html = portal.render_template_string(
        portal.HEALTH_BODY.split("{# oaap.core.updates 2.6.2")[0],
        node=rows, ident=v, is_server_admin=True, mx="", mx_style="")
ok("das Formular trägt den gesetzten Alias und den Rechnernamen als Platzhalter",
   'action="/node/identity"' in html and 'value="oaap-test"' in html
   and 'placeholder="oaap-test-vm"' in html, html[-1500:])
with portal.app.test_request_context():
    html = portal.render_template_string(
        portal.HEALTH_BODY.split("{# oaap.core.updates 2.6.2")[0],
        node=rows, ident=v, is_server_admin=False, mx="", mx_style="")
ok("support: kein Formular", 'action="/node/identity"' not in html)
ROLES["v"] = {"support"}
n = len(SENT)
r = cl.post("/node/identity", data={"alias": "x1"}, headers={"X-OAAP-User": "s",
                                                             "X-OAAP-Roles": "support"})
ok("support: ein POST wird abgewiesen, nichts im Spool", r.status_code == 403 and len(SENT) == n)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
