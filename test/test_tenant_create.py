#!/usr/bin/env python3
"""Mandant anlegen — an der Maschine und im Portal (oaap.core.tenant 2.2).

Bis hierher war Anlegen CLI-only. Diese Datei verteidigt den zweiten
Weg und vor allem, dass er **derselbe** ist:

    Ein Mandant kommt gleich heraus, egal durch welche Tür er kam.
    Anlegen darf nur ein server_admin — ein tenant_admin, der einen
    Mandanten anlegen könnte, könnte sich darin selbst einsetzen.
    Was angelegt wurde, steht im Log DES NEUEN Mandanten.

Der dritte Satz ist der unbequemste: Die erste Zeile im Protokoll eines
Mandanten ist der Vermerk seiner eigenen Entstehung, und sein Verwalter
muss sie lesen können (RFC-0022 §6).

Braucht kein Docker und keinen Knoten.

Run: python3 test/test_tenant_create.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-tenant-create-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

m.reload_gateway = lambda: None
# Kein DNS im Test. Die Zonenprüfung ist eine echte Netzabfrage; was
# hier zählt, ist dass ihr Satz beim Anlegen mitkommt, nicht was er sagt.
m.zone_probe = lambda label: f"ZONE({label})"

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant,
            "groups": [], "active": True}


QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(req):
    """Eine Anfrage durch den echten Worker schicken."""
    _rid[0] += 1
    req.setdefault("id", f"r{_rid[0]}")
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{req['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(req, f)
    m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{req['id']}.json"),
              encoding="utf-8") as f:
        return json.load(f)


def audit():
    try:
        with open(m.TENANT_LOG, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def labels():
    return sorted(t.get("label") for t in m.load_tenants().values())


print("=== die Regel, die beide Türen teilen ===")
default_id = m.ensure_default_tenant()
ok("der Knoten hat einen Standard-Mandanten", bool(default_id))

ok("ein Kürzel mit Großbuchstaben wird abgelehnt",
   "lowercase" in m.tenant_label_error("Kunde", "create"))
ok("ein Kürzel, das mit einem Bindestrich beginnt, auch",
   bool(m.tenant_label_error("-kunde", "create")))
ok("ein zu langes Kürzel auch",
   bool(m.tenant_label_error("k" * 32, "create")))
ok("'default' gehört dem Knoten selbst",
   "not available" in m.tenant_label_error("default", "create"))
ok("ein freies Kürzel ist frei", m.tenant_label_error("kunde-meier", "create") == "")

print("\n=== Tür 1: die Maschine ===")


class Args:
    def __init__(self, **kw):
        self.action = kw.get("action", "create")
        self.name = kw.get("name")
        self.target = kw.get("target")
        self.title = kw.get("title", "")
        self.account = kw.get("account", "")
        self.account_name = kw.get("account_name", "")
        self.grace_days = kw.get("grace_days", 30)
        self.yes = kw.get("yes", False)
        self.count = kw.get("count", 50)


import contextlib  # noqa: E402
import io  # noqa: E402

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_tenant(Args(action="create", name="kunde-meier",
                      title="Kunde Meier GmbH"))
cli_out = buf.getvalue()
ok("die CLI legt den Mandanten an", "kunde-meier" in labels())
ok("und sagt vorher, dass das Kürzel öffentlich ist",
   "PUBLIC" in cli_out and "Certificate" in cli_out)
cli_rec = next(t for t in m.load_tenants().values() if t["label"] == "kunde-meier")
ok("mit Klarnamen", cli_rec["name"] == "Kunde Meier GmbH")
ok("und einem Vermerk im Protokoll",
   audit()[-1]["action"] == "tenant.create"
   and audit()[-1]["tenant_label"] == "kunde-meier", str(audit()[-1]))

print("\n=== Tür 2: das Portal (über den Spool-Worker) ===")
write_users([
    user("joerg", ["server_admin", "admin"], default_id),
    user("meier-chef", ["tenant_admin"],
         next(tid for tid, t in m.load_tenants().items()
              if t["label"] == "kunde-meier")),
])

before = labels()
res = queue({"instance": "", "action": "tenant", "op": "create",
             "label": "kunde-schulz", "by": "meier-chef"})
ok("ein tenant_admin darf keinen Mandanten anlegen",
   not res["ok"] and "server_admin" in res["message"], str(res))
ok("und es ist auch keiner entstanden", labels() == before)
ok("die Ablehnung steht im Protokoll",
   audit()[-1]["action"] == "tenant.create"
   and audit()[-1]["result"] == "denied", str(audit()[-1]))
ok("und nennt das Kürzel, das abgelehnt wurde",
   audit()[-1]["subject"] == "kunde-schulz", str(audit()[-1]))

res = queue({"instance": "", "action": "tenant", "op": "create",
             "label": "kunde-meier", "by": "joerg"})
ok("ein schon vergebenes Kürzel wird abgelehnt",
   not res["ok"] and "already taken" in res["message"], str(res))

res = queue({"instance": "", "action": "tenant", "op": "create",
             "label": "Kunde Schulz", "by": "joerg"})
ok("ein ungültiges Kürzel wird abgelehnt",
   not res["ok"] and "lowercase" in res["message"], str(res))
ok("nach drei Ablehnungen gibt es weiter nur die bekannten Mandanten",
   labels() == before, str(labels()))

res = queue({"instance": "", "action": "tenant", "op": "create",
             "label": "kunde-schulz", "title": "Schulz & Söhne",
             "by": "joerg"})
ok("ein server_admin legt ihn an", res["ok"], str(res))
ok("und die Antwort trägt die Zonenprüfung mit",
   "ZONE(kunde-schulz)" in res["message"], str(res))
ok("sowie den Hinweis, dass Mandanten jetzt sichtbar sind",
   "become visible" not in res["message"], "erst ab dem zweiten Mandanten")

new = next((t for t in m.load_tenants().values()
            if t["label"] == "kunde-schulz"), None)
ok("der Datensatz ist da", new is not None)
ok("mit Klarnamen", new and new["name"] == "Schulz & Söhne")

print("\n=== beide Türen erzeugen denselben Datensatz ===")
ok("dieselben Felder",
   set(new) == set(cli_rec), f"{sorted(new)} != {sorted(cli_rec)}")
ok("beide haben eine eigene Account-Referenz",
   new["account"] != cli_rec["account"] and len(new["account"]) == 36)
ok("beide starten ohne frühere Kürzel",
   new["former_labels"] == [] == cli_rec["former_labels"])

print("\n=== der Vermerk steht im Log des NEUEN Mandanten ===")
new_id = next(tid for tid, t in m.load_tenants().items()
              if t["label"] == "kunde-schulz")
last = audit()[-1]
ok("Aktion tenant.create", last["action"] == "tenant.create", str(last))
ok("abgelegt im neuen Mandanten, nicht im Standard-Mandanten",
   last["tenant"] == new_id, str(last))
ok("mit dem Namen dessen, der es getan hat", last["who"] == "joerg", str(last))
ok("und seiner Rolle", last["role"] == "server_admin", str(last))
mine = [e for e in audit() if e["tenant"] == new_id]
ok("der Verwalter des neuen Mandanten sieht die Entstehung in seinem Log",
   len(mine) == 1 and mine[0]["subject"] == "kunde-schulz", str(mine))

print("\n=== der zweite Mandant schaltet die Sichtbarkeit ein ===")
# Der Knoten hat jetzt drei. Der Satz gehört an den Übergang von einem
# auf zwei, und dort wird er auch geprüft: frischer Knoten, ein Mandant.
DATA2 = tempfile.mkdtemp(prefix="oaap-tenant-create-2-")
os.environ["OAAP_DATA_DIR"] = DATA2
import importlib  # noqa: E402
m2 = importlib.reload(m)
m2.reload_gateway = lambda: None
m2.zone_probe = lambda label: f"ZONE({label})"
d2 = m2.ensure_default_tenant()
os.makedirs(os.path.join(DATA2, "data", "identity"), exist_ok=True)
with open(os.path.join(DATA2, "data", "identity", "users.json"), "w",
          encoding="utf-8") as f:
    json.dump([user("joerg", ["server_admin"], d2)], f)
q2 = os.path.join(m2.SPOOL_DIR, "queue")
os.makedirs(q2, exist_ok=True)
with open(os.path.join(q2, "x1.json"), "w", encoding="utf-8") as f:
    json.dump({"id": "x1", "instance": "", "action": "tenant",
               "op": "create", "label": "erster-kunde", "by": "joerg"}, f)
m2.cmd_process_deploys(None)
with open(os.path.join(m2.SPOOL_DIR, "results", "x1.json"), encoding="utf-8") as f:
    res2 = json.load(f)
ok("der Sprung von einem auf zwei wird angesagt",
   res2["ok"] and "become visible" in res2["message"], str(res2))
ok("und sagt zugleich, dass sich für den bestehenden nichts ändert",
   "Nothing about the existing tenant changes" in res2["message"], str(res2))

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
