#!/usr/bin/env python3
"""Der Store aktualisiert eine BESTIMMTE Instanz (Worker + Portalseite).

Vorher schickte der Knopf "Aktualisieren auf x" nur die App-Id als
Instanzname. Hiess die Instanz anders oder gab es mehrere, fand der Host
keine und legte eine NEUE an (gemeldet von der Vereinsloesung, oaapx02).

Geprueft wird:

    - ein Update nennt den Instanzschluessel; der Host findet genau diese
      Instanz (auch in einem fremden Mandanten, wenn der Betreiber fragt)
    - es entsteht keine zweite Instanz; der Kanal der Instanz bleibt
    - die Instanz muss zur App gehoeren, und ein Mandanten-Admin erreicht
      keine Instanz eines anderen Mandanten
    - "nur ueber Test, wenn Test existiert": eine Produktiv-Instanz mit
      Test-Geschwister wird nicht direkt aktualisiert, die Test-Instanz
      selbst schon; ohne Test-Instanz geht die Produktiv-Instanz direkt
    - eine NEUE Instanz mit eigenem Namen ersetzt keine bestehende
    - das Portal bietet je Instanz einen Knopf an, und die Anfrage nennt
      nie ein Paket

Aufruf: python3 test/test_store_update_instance.py
"""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-store-update-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                             # noqa: E402

a.reload_gateway = lambda: None
a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


MANIFEST = """oaap_manifest: "0.1"
app:
  id: {id}
  name: {id}
  version: {version}
  type: native
services:
  web:
    build: .
    port: 80
routes:
  - path: /
    roles: [user]
health:
  path: /healthz
"""


class TenantArgs:
    def __init__(self, action, name=None):
        self.action, self.name, self.target = action, name, None
        self.title, self.account, self.account_name = "", "", ""
        self.grace_days, self.yes, self.count = 30, False, 50


default_id = a.ensure_default_tenant()
os.makedirs(a.CADDY_APPS_DIR, exist_ok=True)
with contextlib.redirect_stdout(io.StringIO()):
    a.cmd_tenant(TenantArgs("create", "kunde-a"))
    a.cmd_tenant(TenantArgs("create", "kunde-b"))
KA = a.tenant_by_label("kunde-a")[0]
KB = a.tenant_by_label("kunde-b")[0]

os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([
        {"username": "betreiber", "roles": ["server_admin"], "active": True,
         "tenant": default_id, "groups": []},
        {"username": "chef-a", "roles": ["tenant_admin"], "active": True,
         "tenant": KA, "groups": []},
        {"username": "chef-b", "roles": ["tenant_admin"], "active": True,
         "tenant": KB, "groups": []},
    ], f)
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(a.SPOOL_DIR, d), exist_ok=True)

# the catalog instance (default tenant) and its package
reg = a.load_registry()
reg["instances"]["katalog"] = {"app_id": "package-catalog", "name": "katalog",
                               "channel": "production", "tenant": default_id,
                               "version": "0.1.0"}
a.save_registry(reg)
BASE = os.path.join(a.instance_dir("katalog", reg["instances"]["katalog"]),
                    "storage", "data")
os.makedirs(os.path.join(BASE, "packages"), exist_ok=True)


def zip_bytes(app_id, version):
    p = os.path.join(tempfile.mkdtemp(prefix="oaap-pkg-"), "x.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("oaap-app.yaml", MANIFEST.format(id=app_id, version=version))
        z.writestr("Dockerfile", "FROM scratch\n")
    return open(p, "rb").read()


def publish(app_id, version):
    data = zip_bytes(app_id, version)
    rel = f"packages/{app_id}-{version}.zip"
    with open(os.path.join(BASE, *rel.split("/")), "wb") as f:
        f.write(data)
    with open(os.path.join(BASE, "store.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "0.2", "name": "Betreiber", "apps": [{
            "id": app_id, "version": version, "name": app_id, "summary": "x",
            "package": {"zip": rel, "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data)}}]}, f)


publish("vereinsportal", "0.2.0")
with contextlib.redirect_stdout(io.StringIO()):
    a.cmd_store(a.argparse.Namespace(action="add-catalog", target="katalog",
                                     value=None, name="Katalog", id=None,
                                     origin="", trust="verified"))

BUILT = []


def fake_install_from_dir(pkg, args, source):
    BUILT.append({"name": getattr(args, "name", ""), "key": args.key,
                  "channel": args.channel, "tenant": getattr(args, "tenant", ""),
                  "source": dict(source)})
    reg = a.load_registry()
    if args.key not in reg["instances"]:
        # what the real builder does for a NEW instance: it comes into being
        reg["instances"][args.key] = {
            "app_id": "vereinsportal", "name": args.name, "channel": args.channel,
            "tenant": args.tenant, "version": source.get("version", ""),
            "source": dict(source)}
        a.save_registry(reg)


a._install_from_dir = fake_install_from_dir


def put(key, tenant, channel="production", version="0.1.0", app_id="vereinsportal",
        name=None, rehearsal=False):
    reg = a.load_registry()
    rec = {"app_id": app_id, "name": name or key, "channel": channel,
           "tenant": tenant, "version": version,
           "source": {"kind": "artifact", "version": version}}
    if rehearsal:
        rec["rehearsal"] = {"of": "x"}
    reg["instances"][key] = rec
    a.save_registry(reg)


def drop(*keys):
    reg = a.load_registry()
    for k in keys:
        reg["instances"].pop(k, None)
    a.save_registry(reg)


def ask(extra, by="betreiber", app="vereinsportal"):
    rid = os.urandom(6).hex()
    req = {"id": rid, "instance": app, "action": "install", "by": by,
           "source_id": "catalog-katalog", "confirm_source": "",
           "requested": "now", **extra}
    with open(os.path.join(a.SPOOL_DIR, "queue", rid + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(req, f)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.cmd_process_deploys(None)
    try:
        with open(os.path.join(a.SPOOL_DIR, "results", rid + ".json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {"ok": False, "message": "no result: " + buf.getvalue()[-300:]}


def keys():
    return sorted(a.load_registry()["instances"])


print("=== eine Instanz mit anderem Namen als die App ===")
put("kunde-a-portal-verein", KA, name="portal-verein")
n_before, built_before = len(keys()), len(BUILT)
r = ask({"app_id": "vereinsportal", "update": "kunde-a-portal-verein"}, by="chef-a")
ok("der Mandanten-Admin aktualisiert die Instanz ueber ihren Schluessel", r.get("ok"), r)
ok("es wurde genau einmal gebaut, auf den Schluessel der bestehenden Instanz",
   len(BUILT) == built_before + 1 and BUILT[-1]["key"] == "kunde-a-portal-verein",
   BUILT[-1:])
ok("... im Mandanten der Instanz, auf ihrem Kanal",
   BUILT[-1]["channel"] == "production", BUILT[-1])
ok("es entstand keine zweite Instanz", len(keys()) == n_before, keys())

print("\n=== der Betreiber aktualisiert die Instanz eines Mandanten ===")
built_before = len(BUILT)
r = ask({"app_id": "vereinsportal", "update": "kunde-a-portal-verein"})
ok("auch der Betreiber (ohne eigenen Mandanten dort) trifft sie", r.get("ok"), r)
ok("... auf den Schluessel der Instanz, die im Mandanten bleibt",
   len(BUILT) == built_before + 1 and BUILT[-1]["key"] == "kunde-a-portal-verein"
   and a.load_registry()["instances"]["kunde-a-portal-verein"]["tenant"] == KA,
   BUILT[-1:])
ok("... ohne neue Instanz", len(keys()) == n_before, keys())

print("\n=== was nicht geht ===")
built_before = len(BUILT)
r = ask({"app_id": "vereinsportal", "update": "kunde-a-portal-verein"}, by="chef-b")
ok("ein Mandanten-Admin eines anderen Mandanten erreicht sie nicht",
   not r.get("ok") and len(BUILT) == built_before, r)
r = ask({"app_id": "vereinsportal", "update": "gibt-es-nicht"})
ok("ein Schluessel, den es nicht gibt, wird nicht zu einer neuen Instanz",
   not r.get("ok") and len(BUILT) == built_before and len(keys()) == n_before, r)
put("kunde-a-andere", KA, app_id="andere-app")
r = ask({"app_id": "vereinsportal", "update": "kunde-a-andere"}, by="chef-a")
ok("eine Instanz einer ANDEREN App wird nicht mit diesem Paket ueberschrieben",
   not r.get("ok") and len(BUILT) == built_before
   and "not of" in r.get("message", ""), r)
drop("kunde-a-andere")

print("\n=== nur ueber Test, wenn Test existiert ===")
put("kunde-a-portal-verein-test", KA, channel="test", name="portal-verein-test")
built_before = len(BUILT)
r = ask({"app_id": "vereinsportal", "update": "kunde-a-portal-verein"}, by="chef-a")
ok("die Produktiv-Instanz mit Test-Geschwister wird nicht direkt aktualisiert, "
   "und die Antwort nennt die Test-Instanz",
   not r.get("ok") and len(BUILT) == built_before
   and "portal-verein-test" in r.get("message", ""), r)
r = ask({"app_id": "vereinsportal", "update": "kunde-a-portal-verein-test"}, by="chef-a")
ok("die Test-Instanz selbst schon", r.get("ok") and len(BUILT) == built_before + 1
   and BUILT[-1]["channel"] == "test", r)
ok("... auf ihrem Kanal, ohne neue Instanz", len(keys()) == n_before + 1, keys())
drop("kunde-a-portal-verein-test")
put("kunde-b-test", KB, channel="test", name="portal-verein-test")
built_before = len(BUILT)
r = ask({"app_id": "vereinsportal", "update": "kunde-a-portal-verein"}, by="chef-a")
ok("eine Test-Instanz eines ANDEREN Mandanten zaehlt nicht als Geschwister",
   r.get("ok") and len(BUILT) == built_before + 1, r)
drop("kunde-b-test")
put("kunde-a-probe", KA, channel="test", name="probe", rehearsal=True)
built_before = len(BUILT)
r = ask({"app_id": "vereinsportal", "update": "kunde-a-portal-verein"}, by="chef-a")
ok("eine Generalprobe (Rehearsal) zaehlt nicht als Test-Instanz",
   r.get("ok") and len(BUILT) == built_before + 1, r)
drop("kunde-a-probe")

print("\n=== eine neue Instanz unter eigenem Namen ===")
n_before = len(keys())
built_before = len(BUILT)
r = ask({"instance": "verein-zwei", "app_id": "vereinsportal", "new": True,
         "channel": "production"}, by="chef-a")
ok("ein neuer Name legt eine Instanz an, der Katalog sucht die APP",
   r.get("ok") and len(BUILT) == built_before + 1
   and BUILT[-1]["key"] == a.instance_key(KA, "verein-zwei")
   and a.load_registry()["instances"][BUILT[-1]["key"]]["tenant"] == KA,
   (r, BUILT[-1:]))
r = ask({"instance": "portal-verein", "app_id": "vereinsportal", "new": True},
        by="chef-a")
ok("ein neuer Name, der schon vergeben ist, ueberschreibt nichts",
   not r.get("ok") and len(BUILT) == built_before + 1, r)
built_before = len(BUILT)
r = ask({"instance": "verein-drei", "app_id": "vereinsportal", "new": True,
         "channel": "test"}, by="chef-a")
ok("... mit Test-Instanz: <Name>-test auf dem Test-Kanal",
   r.get("ok") and BUILT[-1]["key"] == a.instance_key(KA, "verein-drei-test")
   and BUILT[-1]["channel"] == "test", (r, BUILT[-1:]))

print("\n=== das Portal ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal                                           # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-store-update-portal-")
portal.APPS_REGISTRY_DIR = TMP
portal.STORE_SOURCES_FILE = os.path.join(TMP, "store-sources.json")
portal.SPOOL_DIR = os.path.join(TMP, "spool")
portal.SPOOL_QUEUE = os.path.join(portal.SPOOL_DIR, "queue")
portal.SPOOL_RESULTS = os.path.join(portal.SPOOL_DIR, "results")
portal.INSTALL_WAIT_SECONDS = 0
INST = {}
portal.visible_instances = lambda: dict(INST)
portal.pending_installs = lambda: set()
portal.node_profiles = lambda: []
portal.caller_roles = lambda: {"tenant_admin"}
portal.caller_scope = lambda: ("tenant_admin", KA)
portal.host_tenant_scope = lambda host: (None, True)
portal.resolve_tenant = lambda ref: ref or ""
portal.multi_tenant = lambda: False
c = portal.app.test_client()
H = {"X-OAAP-User": "chef-a", "X-OAAP-Roles": "tenant_admin"}
json.dump({"sources": [{"id": "catalog-katalog", "name": "Katalog",
                        "url": "catalog:katalog", "trust": "verified",
                        "enabled": True}]}, open(portal.STORE_SOURCES_FILE, "w"))
json.dump({"schema": "0.2", "name": "Katalog", "apps": [{
    "id": "vereinsportal", "name": "Vereinsportal", "version": "0.2.0",
    "summary": "s", "package": {"catalog": True}}]},
    open(os.path.join(TMP, "catalog-catalog-katalog.json"), "w"))


def rec(name, channel="production", version="0.1.0", app_id="vereinsportal",
        tenant=KA):
    return {"app_id": app_id, "name": name, "channel": channel,
            "version": version, "tenant": tenant}


def queued():
    out = []
    d = portal.SPOOL_QUEUE
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            out.append(json.load(open(os.path.join(d, fn))))
            os.remove(os.path.join(d, fn))
    return out


def page():
    return c.get("/store/catalog-katalog/vereinsportal",
                 headers=H).get_data(as_text=True)


INST.update({"kunde-a-portal-verein": rec("portal-verein"),
             "kunde-a-verein-zwei": rec("verein-zwei"),
             "kunde-a-fremd": rec("fremd", app_id="andere")})
t = page()
ok("beide Instanzen der App stehen da, die einer anderen App nicht",
   "portal-verein" in t and "verein-zwei" in t and "fremd" not in t, t[-900:])
ok("je Instanz ein eigener Knopf mit ihrem Schluessel",
   t.count('name="update" value="kunde-a-portal-verein"') == 1
   and t.count('name="update" value="kunde-a-verein-zwei"') == 1)
INST["kunde-a-portal-verein-test"] = rec("portal-verein-test", channel="test")
t = page()
ok("mit Test-Geschwister: kein Knopf an der Produktiv-Instanz, der Hinweis "
   "nennt die Test-Instanz",
   'name="update" value="kunde-a-portal-verein"' not in t
   and 'name="update" value="kunde-a-portal-verein-test"' in t
   and "portal-verein-test" in t and "Nach Produktiv" in t, t[-1500:])
ok("die andere Produktiv-Instanz (derselbe Mandant, gleiche App) geht ebenfalls "
   "ueber Test", 'name="update" value="kunde-a-verein-zwei"' not in t)
INST["kunde-a-portal-verein-test"]["tenant"] = KB
t = page()
ok("eine Test-Instanz eines anderen Mandanten ist kein Geschwister",
   'name="update" value="kunde-a-portal-verein"' in t)
INST.pop("kunde-a-portal-verein-test")
INST["kunde-a-portal-verein"]["version"] = "0.2.0"
t = page()
ok("auf dem aktuellen Stand: 'aktuell', kein Knopf",
   'name="update" value="kunde-a-portal-verein"' not in t and "aktuell" in t)
INST["kunde-a-portal-verein"]["version"] = "0.1.0"

r = c.post("/store/install", headers=H, data={
    "app_id": "vereinsportal", "source_id": "catalog-katalog",
    "update": "kunde-a-portal-verein"})
q = queued()
ok("'Aktualisieren': die Anfrage nennt Instanzschluessel und App, nie ein Paket",
   r.status_code == 303 and len(q) == 1 and q[0]["update"] == "kunde-a-portal-verein"
   and q[0]["app_id"] == "vereinsportal" and q[0]["action"] == "install"
   and not any(k in q[0] for k in ("url", "package", "path", "zip", "version")), q)
r = c.post("/store/install", headers=H, data={
    "app_id": "vereinsportal", "source_id": "catalog-katalog", "update": "fremde-instanz"})
ok("ein Schluessel, den der Aufrufer nicht sieht: nicht in den Spool",
   queued() == [] and "err=1" in r.headers.get("Location", ""))
r = c.post("/store/install", headers=H, data={
    "app_id": "vereinsportal", "source_id": "catalog-katalog",
    "update": "kunde-a-fremd"})
ok("eine Instanz einer anderen App: nicht in den Spool", queued() == [])
r = c.post("/store/install", headers=H, data={
    "app_id": "vereinsportal", "source_id": "catalog-katalog",
    "new": "1", "name": "verein-zwei"})
ok("neue Instanz mit vergebenem Namen: nicht in den Spool", queued() == []
   and "err=1" in r.headers.get("Location", ""))
r = c.post("/store/install", headers=H, data={
    "app_id": "vereinsportal", "source_id": "catalog-katalog",
    "new": "1", "name": "../x"})
ok("neue Instanz mit unmoeglichem Namen: nicht in den Spool", queued() == [])
r = c.post("/store/install", headers=H, data={
    "app_id": "vereinsportal", "source_id": "catalog-katalog",
    "new": "1", "name": "verein-vier", "channel": "test"})
q = queued()
ok("neue Instanz: Name als Instanz, App-Id getrennt, kein 'update'",
   len(q) == 1 and q[0]["instance"] == "verein-vier"
   and q[0]["app_id"] == "vereinsportal" and q[0].get("new") is True
   and "update" not in q[0] and q[0]["channel"] == "test", q)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
