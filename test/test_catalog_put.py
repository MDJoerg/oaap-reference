#!/usr/bin/env python3
"""Das getestete Paket in den Katalog, und wer es dort sieht (RFC-0050 §9).

Der Worker ist echt (Spool, Benutzer, Mandanten, Quellen, Registry im
Wegwerfverzeichnis), und auf der anderen Seite des Eingangs arbeitet der
ECHTE Katalog aus oaap-apps (`catalog.Catalog.ingest_inbox` in einem
Faden) -- nur der Bau eines Containers ist ersetzt. Geprueft wird:

    - "In den Katalog uebernehmen" legt genau die Bytes des Teststands in
      den Katalog, als NICHT freigegeben, mit Herkunft (Mandant, Instanz,
      Kanal) und dem Menschen, der es ausloeste
    - dieselbe Version ein zweites Mal: der Katalog lehnt ab, und die
      Ablehnung kommt beim Menschen an (nicht "unklar", nicht "ok")
    - nur ein hochgeladenes Paket beweist die Bytes: eine Instanz aus Git
      wird abgelehnt; ohne Katalog als Quelle gibt es nichts zu uebergeben
    - nur server_admin: ein tenant_admin wird vom Worker abgelehnt
    - der Eingang ist Speicher der App: ist er eine Verknuepfung, schreibt
      der Knoten (root) NICHT dorthin, wohin sie zeigt
    - visible_to: ein Eintrag fuer pxx loest fuer pxx auf, fuer bec nicht --
      mit einem Grund, der den Filter nennt; ohne Mandant (kein Mandant im
      Spiel) wird nicht gefiltert; der Worker lehnt die Installation in einen
      nicht genannten Mandanten ab, auch wenn die Anfrage sie verlangt
    - ein beschaedigtes visible_to ist NICHT "alle", sondern unbrauchbar

Laeuft nur auf Linux (Verzeichnis-Handles, O_NOFOLLOW).
Aufruf: python3 test/test_catalog_put.py
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if not hasattr(os, "O_NOFOLLOW") or os.name != "posix":
    print("SKIP  braucht Linux (O_NOFOLLOW, Verzeichnis-Handles)")
    sys.exit(0)
CATALOG_APP = os.path.join(HERE, "..", "..", "oaap-apps", "apps", "package-catalog")
if not os.path.isdir(CATALOG_APP):
    CATALOG_APP = os.environ.get("OAAP_CATALOG_APP", "")
if not CATALOG_APP or not os.path.isdir(CATALOG_APP):
    print("SKIP  der Paketkatalog (oaap-apps) liegt nicht daneben "
          "(OAAP_CATALOG_APP=<Pfad zu apps/package-catalog>)")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-catalog-put-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, CATALOG_APP)

import appctl as a                                             # noqa: E402
import catalog as appcat                                       # noqa: E402

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
"""


class TenantArgs:
    def __init__(self, action, name=None):
        self.action, self.name, self.target = action, name, None
        self.title, self.account, self.account_name = "", "", ""
        self.grace_days, self.yes, self.count = 30, False, 50


def zip_bytes(app_id, version):
    p = os.path.join(tempfile.mkdtemp(prefix="oaap-pkg-"), "x.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("oaap-app.yaml", MANIFEST.format(id=app_id, version=version))
        z.writestr("Dockerfile", "FROM scratch\n")
    return open(p, "rb").read()


default_id = a.ensure_default_tenant()
os.makedirs(a.CADDY_APPS_DIR, exist_ok=True)
with contextlib.redirect_stdout(io.StringIO()):
    for t in ("bdt", "pxx", "bec"):
        a.cmd_tenant(TenantArgs("create", t))
T = {t: a.tenant_by_label(t)[0] for t in ("bdt", "pxx", "bec")}

os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([
        {"username": "betreiber", "roles": ["server_admin"], "active": True,
         "tenant": default_id, "groups": []},
        {"username": "chef-bdt", "roles": ["tenant_admin"], "active": True,
         "tenant": T["bdt"], "groups": []},
        {"username": "chef-pxx", "roles": ["tenant_admin"], "active": True,
         "tenant": T["pxx"], "groups": []},
        {"username": "chef-bec", "roles": ["tenant_admin"], "active": True,
         "tenant": T["bec"], "groups": []},
    ], f)
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(a.SPOOL_DIR, d), exist_ok=True)

# the catalog instance and its storage, written by the REAL catalog code
reg = a.load_registry()
reg["instances"]["katalog"] = {"app_id": "package-catalog", "name": "katalog",
                               "channel": "production", "tenant": default_id,
                               "version": "0.4.0"}
a.save_registry(reg)
BASE = os.path.join(a.instance_dir("katalog", reg["instances"]["katalog"]),
                    "storage", "data")
os.makedirs(BASE, exist_ok=True)
CAT = appcat.Catalog(BASE)
STOP = threading.Event()


def catalog_loop():
    while not STOP.is_set():
        try:
            CAT.ingest_inbox(10 ** 8)
        except Exception as e:                                # noqa: BLE001
            print("catalog loop:", e)
        STOP.wait(0.2)


threading.Thread(target=catalog_loop, daemon=True).start()


def test_instance(key, tenant, version, app_id="bdt-app", channel="test",
                  kind="artifact"):
    """A test instance that runs from a retained package (as RFC-0019 leaves it)."""
    reg = a.load_registry()
    rec = {"app_id": app_id, "name": key.split("-", 1)[1] if tenant != default_id else key,
           "channel": channel, "tenant": tenant, "version": version}
    if kind == "artifact":
        data = zip_bytes(app_id, version)
        stored = f"{version}-{hashlib.sha256(data).hexdigest()[:12]}.zip"
        rec["source"] = {"kind": "artifact", "stored": stored, "version": version}
        reg["instances"][key] = rec
        a.save_registry(reg)
        d = a.artifact_dir(key, rec)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, stored), "wb") as f:
            f.write(data)
        return data
    rec["source"] = {"kind": "git", "url": "https://example.org/x.git",
                     "commit": "a" * 40}
    reg["instances"][key] = rec
    a.save_registry(reg)
    return b""


def put_cli(name, catalog="", wait=10):
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            a.cmd_catalog_put(a.argparse.Namespace(name=name, catalog=catalog,
                                                   wait=wait))
        except SystemExit as e:
            code = e.code or 0
    return code, buf.getvalue()


def work():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.cmd_process_deploys(None)
    return buf.getvalue()


def spool(req):
    rid = os.urandom(6).hex()
    req = dict(req, id=rid, requested="now")
    with open(os.path.join(a.SPOOL_DIR, "queue", rid + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(req, f)
    work()
    try:
        with open(os.path.join(a.SPOOL_DIR, "results", rid + ".json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


BUILT = []


def fake_install_from_dir(pkg, args, source):
    BUILT.append({"name": getattr(args, "name", ""), "key": args.key,
                  "tenant": getattr(args, "tenant", ""), "source": dict(source)})
    reg = a.load_registry()
    reg["instances"][args.key] = {
        "app_id": "bdt-app", "name": args.name, "channel": args.channel,
        "tenant": args.tenant, "version": source.get("version", ""),
        "source": dict(source)}
    a.save_registry(reg)


a._install_from_dir = fake_install_from_dir

# ------------------------------------------------------------------------
print("=== ohne Katalog als Quelle ===")
tested = test_instance("bdt-bdt-app-test", T["bdt"], "1.0.0")
code, out = put_cli("bdt-bdt-app-test")
ok("keine Quelle eingetragen: abgelehnt, mit dem Weg dorthin",
   code != 0 and "add-catalog" in out, out)

with contextlib.redirect_stdout(io.StringIO()):
    a.cmd_store(a.argparse.Namespace(action="add-catalog", target="katalog",
                                     value=None, name="Katalog", id=None,
                                     origin="", trust="verified"))

print("\n=== die Uebergabe ===")
code, out = put_cli("bdt-bdt-app-test")
r = CAT.get("bdt-app", "1.0.0")
ok("uebergeben: der Katalog hat 1.0.0", code == 0 and r is not None, out)
ok("... NICHT freigegeben (freigeben bleibt ein Mensch)",
   r is not None and r["released"] is False, r)
ok("... genau die Bytes des Teststands",
   r is not None and r["sha256"] == hashlib.sha256(tested).hexdigest()
   and open(CAT.path_of("bdt-app", "1.0.0"), "rb").read() == tested)
ok("... mit Herkunft: Mandant, Instanz, Kanal",
   r is not None and r.get("origin") == {"tenant": "bdt", "instance": "bdt-app-test",
                                         "channel": "test"}, r)
ok("... und die Antwort sagt es dem Menschen",
   "nicht freigegeben" in out and "Release it in the catalog" in out, out)
box = os.path.join(BASE, "inbox")
ok("der Eingang ist danach leer (Antwort gelesen und entfernt)",
   os.listdir(box) == [], os.listdir(box))
ok("der Eingang gehoert dem Besitzer des Speichers",
   os.stat(box).st_uid == os.stat(BASE).st_uid)
ok("nicht freigegeben heisst: nicht im Store",
   a._store_lookup("bdt-app", tenant=T["pxx"])[0] is None)

code, out = put_cli("bdt-bdt-app-test")
ok("dieselbe Version noch einmal: die Ablehnung des Katalogs kommt an",
   code != 0 and "refused" in out and "gibt es schon" in out, out)

test_instance("bdt-hub-test", T["bdt"], "0.3.0", app_id="bdt-hub", kind="git")
code, out = put_cli("bdt-hub-test")
ok("eine Instanz aus Git: abgelehnt (nur ein Paket beweist die Bytes)",
   code != 0 and "uploaded" in out, out)

print("\n=== nur der Betreiber ===")
test_instance("bdt-bdt-app-test", T["bdt"], "1.1.0")
res = spool({"instance": "bdt-bdt-app-test", "action": "catalog-put",
             "by": "chef-bdt", "catalog": ""})
ok("ein tenant_admin (auch des eigenen Mandanten): abgelehnt",
   res.get("ok") is False and "server_admin" in res.get("message", "")
   and CAT.get("bdt-app", "1.1.0") is None, res)
res = spool({"instance": "bdt-bdt-app-test", "action": "catalog-put",
             "by": "betreiber", "catalog": ""})
ok("server_admin ueber den Worker (der Knopf im Portal): 1.1.0 ist im Katalog",
   res.get("ok") is True and CAT.get("bdt-app", "1.1.0") is not None, res)

print("\n=== der Eingang ist Speicher der App ===")
STOP.set()
time.sleep(0.5)
elsewhere = tempfile.mkdtemp(prefix="oaap-elsewhere-")
shutil.rmtree(box)
os.symlink(elsewhere, box)
test_instance("bdt-bdt-app-test", T["bdt"], "1.2.0")
code, out = put_cli("bdt-bdt-app-test", wait=1)
ok("inbox ist eine Verknuepfung: abgelehnt, und dorthin wurde nichts geschrieben",
   code != 0 and "symbolic link" in out and os.listdir(elsewhere) == [],
   out + str(os.listdir(elsewhere)))
os.remove(box)
code, out = put_cli("bdt-bdt-app-test", wait=1)
ok("der Katalog antwortet nicht: 'noch nicht geantwortet', nicht 'abgelehnt'",
   code == 0 and "not answered" in out, out)
STOP.clear()
threading.Thread(target=catalog_loop, daemon=True).start()
deadline = time.time() + 5
while time.time() < deadline and CAT.get("bdt-app", "1.2.0") is None:
    time.sleep(0.2)
ok("... und nimmt das Paket auf, sobald er wieder laeuft",
   CAT.get("bdt-app", "1.2.0") is not None)

print("\n=== Sichtbar fuer (visible_to) ===")
CAT.set_released("bdt-app", "1.2.0", True, "betreiber")
CAT.set_visible("bdt-app", ["pxx"], "betreiber")
src, ver, _s = a._store_lookup("bdt-app", tenant=T["pxx"])
ok("fuer pxx freigegeben: loest fuer pxx auf (1.2.0)", src is not None and ver == "1.2.0")
src, ver, _s = a._store_lookup("bdt-app", tenant=T["bec"])
ok("fuer bec nicht: nicht gelistet, mit Grund, der den Filter nennt",
   src is None and "Sichtbar" in a._store_lookup.note and "'bec'" in a._store_lookup.note,
   a._store_lookup.note)
src, _v, _s = a._store_lookup("bdt-app")
ok("kein Mandant im Spiel: nicht gefiltert", src is not None)
src, _v, _s = a._store_lookup("bdt-app", label="pxx")
ok("per Kennung (Profil-Lauf) ebenso", src is not None)
res = spool({"instance": "bdt-app", "action": "install", "by": "chef-bec",
             "source_id": "catalog-katalog", "app_id": "bdt-app"})
ok("der Worker lehnt die Installation in bec ab (die Anfrage ist Daten)",
   res.get("ok") is False and "Sichtbar" in res.get("message", "")
   and not any(b["tenant"] == T["bec"] for b in BUILT), res)
res = spool({"instance": "bdt-app", "action": "install", "by": "chef-pxx",
             "source_id": "catalog-katalog", "app_id": "bdt-app"})
ok("... und fuehrt sie fuer pxx aus",
   res.get("ok") is True and any(b["tenant"] == T["pxx"] for b in BUILT), res)
res = spool({"instance": "bdt-app", "action": "install", "by": "betreiber",
             "tenant": T["bec"], "source_id": "catalog-katalog", "app_id": "bdt-app"})
ok("auch der Betreiber installiert nicht in einen Mandanten, den der Eintrag "
   "nicht nennt", res.get("ok") is False and "Sichtbar" in res.get("message", "")
   and not any(b["tenant"] == T["bec"] for b in BUILT), res)

print("\n=== was der Knoten aus der Liste liest ===")
import catalog_source as cs                                    # noqa: E402
good = {"id": "x", "version": "1.0.0",
        "package": {"zip": "packages/x.zip", "sha256": "a" * 64, "size": 1}}
ok("ohne visible_to: fuer alle", cs.visible_for(cs._entry(good), "bec"))
for bad in ([], "pxx", ["PXX"], [1], ["pxx", "../x"]):
    ok(f"visible_to={bad!r}: der Eintrag ist unbrauchbar, nicht 'fuer alle'",
       cs._entry(dict(good, visible_to=bad)) is None)
e = cs._entry(dict(good, visible_to=["pxx", "bec", "pxx"]))
ok("doppelte Kennungen fallen weg, sortiert", e["visible_to"] == ["bec", "pxx"], e)
snap = cs.snapshot({"name": "", "apps": [e]})
ok("das Portal bekommt visible_to mit (sonst kann es nicht filtern)",
   snap["apps"][0].get("visible_to") == ["bec", "pxx"], snap)

STOP.set()
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
