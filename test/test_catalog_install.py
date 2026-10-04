#!/usr/bin/env python3
"""Ein Paketkatalog als Store-Quelle, Weg Ende zu Ende (RFC-0050 Stufe 2).

Der Worker ist echt (Spool, Benutzerspeicher, Mandantenspeicher, Quellen,
Registry im Wegwerfverzeichnis); nur der Bau des Containers ist ersetzt
(`_install_from_dir` merkt sich, was ihm uebergeben wurde). Geprueft wird,
was die Spec verspricht und was am ehesten still schiefginge:

    - die Aufloesung nimmt Pakete AUS DEM VERZEICHNIS des Katalogs, kopiert
      sie auf den Knoten und prueft die Kopie; ein Paket, das nicht zur
      Pruefsumme der Liste passt, wird nicht installiert -- mit Grund in der
      Antwort, nicht als "nicht gelistet"
    - was die Liste ueber ein Paket sagt, wird gegen das Manifest im Paket
      gehalten (App-Id und Version): eine Liste kann nichts unter falschem
      Namen ausliefern
    - die Kanalwahl: "mit Test-Instanz" legt `<app>-test` auf dem Test-Kanal
      an, "direkt" die Produktiv-Instanz; eine bestehende Instanz behaelt ihren
    - ein Paket aus dem Katalog traegt seine Quelle (damit "Aktualisieren"
      wieder dort sucht) und ist KEIN "sideload"
    - eine ungeprueft eingetragene Quelle verlangt die Bestaetigung wie jede
    - das Portal bekommt einen Schnappschuss ohne Dateipfade
    - eine App, die der Katalog nicht kennt, wird nicht aus Git "erraten"

Aufruf: python3 test/test_catalog_install.py
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
DATA = tempfile.mkdtemp(prefix="oaap-catalog-install-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                             # noqa: E402
import catalog_source as cs                                    # noqa: E402

a.reload_gateway = lambda: None
a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


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


def make_zip(app_id, version, manifest_id=None, manifest_version=None):
    p = os.path.join(tempfile.mkdtemp(prefix="oaap-pkg-"), f"{app_id}.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("oaap-app.yaml", MANIFEST.format(
            id=manifest_id or app_id, version=manifest_version or version))
        z.writestr("Dockerfile", "FROM scratch\n")
    return open(p, "rb").read()


# --- a node with a catalog instance -------------------------------------
default_id = a.ensure_default_tenant()
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "betreiber", "roles": ["server_admin"], "active": True},
               {"username": "mitglied", "roles": ["user"], "active": True}], f)
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(a.SPOOL_DIR, d), exist_ok=True)

reg = a.load_registry()
reg["instances"]["katalog"] = {"app_id": "package-catalog", "name": "katalog",
                               "channel": "production", "tenant": default_id,
                               "version": "0.1.0"}
a.save_registry(reg)
BASE = os.path.join(a.instance_dir("katalog", reg["instances"]["katalog"]),
                    "storage", "data")
os.makedirs(os.path.join(BASE, "packages"), exist_ok=True)


def release(app_id, version, data=None, listed_version=None, listed_id=None,
            sha=None, size=None):
    data = data if data is not None else make_zip(app_id, version)
    rel = f"packages/{app_id}-{version}.zip"
    with open(os.path.join(BASE, *rel.split("/")), "wb") as f:
        f.write(data)
    return {"id": listed_id or app_id, "version": listed_version or version,
            "name": app_id.title(), "summary": f"{app_id} {version}",
            "package": {"zip": rel, "sha256": sha or hashlib.sha256(data).hexdigest(),
                        "size": size if size is not None else len(data)}}


def write_list(apps):
    with open(os.path.join(BASE, "store.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "0.2", "name": "Betreiber", "apps": apps}, f)


def add_source(trust="verified"):
    argv = a.argparse.Namespace(action="add-catalog", target="katalog", value=None,
                                name="Betreiber-Katalog", id=None, origin="",
                                trust=trust)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        a.cmd_store(argv)
    return buf.getvalue()


def sources():
    return a.load_sources()[0]


def reset_sources():
    a.save_sources([], [])


# --- what the builder was handed ----------------------------------------
BUILT = []


def fake_install_from_dir(pkg, args, source):
    BUILT.append({"name": getattr(args, "name", ""), "channel": args.channel,
                  "store_source": getattr(args, "store_source", ""),
                  "source": dict(source),
                  "manifest": open(os.path.join(pkg, "oaap-app.yaml"),
                                   encoding="utf-8").read()})
    reg = a.load_registry()
    key = a.instance_key(a.resolve_tenant_arg(""), args.name)
    reg["instances"][key] = {"app_id": "x", "name": args.name,
                             "channel": args.channel, "tenant": default_id,
                             "version": source.get("version", ""),
                             "source": dict(source)}
    a.save_registry(reg)


a._install_from_dir = fake_install_from_dir


def work():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.cmd_process_deploys(None)
    return buf.getvalue()


def install(app_id, channel=None, source_id="catalog-katalog", by="betreiber",
            confirm=""):
    rid = os.urandom(6).hex()
    req = {"id": rid, "instance": app_id, "action": "install", "by": by,
           "source_id": source_id, "confirm_source": confirm,
           "requested": "now"}
    if channel:
        req["channel"] = channel
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


# ------------------------------------------------------------------------
print("=== die Quelle eintragen ===")
write_list([release("website", "0.1.2")])
out = add_source()
s = sources()
ok("`store add-catalog` traegt catalog:<Instanz> als Quelle ein, geprueft",
   len(s) == 1 and s[0]["url"] == "catalog:katalog" and s[0]["trust"] == "verified"
   and s[0]["id"] == "catalog-katalog", (s, out))
reset_sources()
add_source("unverified")
ok("... auf Wunsch ungeprueft (Bestaetigung je Installation)",
   sources()[0]["trust"] == "unverified")
reset_sources()
argv = a.argparse.Namespace(action="add-catalog", target="gibt-es-nicht", value=None,
                            name=None, id=None, origin="", trust=None)
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        a.cmd_store(argv)
    refused = False
except SystemExit:
    refused = True
ok("eine Instanz, die es nicht gibt, wird nicht eingetragen", refused and not sources())
add_source()

print("\n=== das Portal sieht einen Schnappschuss ohne Dateipfade ===")
a.catalog_view_write()
snap_path = os.path.join(a.APPS_DIR, "catalog-catalog-katalog.json")
snap = json.load(open(snap_path, encoding="utf-8"))
ok("der Schnappschuss liegt da, mit dem Marker statt des Pfads",
   snap["apps"][0]["id"] == "website" and snap["apps"][0]["package"].get("catalog")
   and "zip" not in json.dumps(snap) and "packages/" not in json.dumps(snap), snap)
write_list([])
with open(os.path.join(BASE, "store.json"), "w") as f:
    f.write("{kaputt")
a.catalog_view_write()
ok("ein kaputter Katalog wird als Fehler an das Portal gegeben, nicht als leer",
   json.load(open(snap_path, encoding="utf-8")).get("error"))
write_list([release("website", "0.1.2")])

print("\n=== die Aufloesung ===")
src, ver, store = a._store_lookup("website")
ok("die App wird im Katalog gefunden: Version 0.1.2, Quelle der Katalog",
   src and ver == "0.1.2" and store["id"] == "catalog-katalog", (src, ver))
ok("das Paket ist eine KOPIE des Knotens, nicht die Datei des Katalogs",
   src["url"].startswith(a.CATALOG_STAGE) and
   os.path.dirname(src["url"]) != os.path.dirname(os.path.join(BASE, "packages", "x")),
   src)
ok("die Kopie hat die Bytes der Datei",
   open(src["url"], "rb").read() == open(os.path.join(
       BASE, "packages", "website-0.1.2.zip"), "rb").read())
ok("was die Liste ueber das Paket sagt, liegt daneben",
   a._staged_expect(src["url"]) == {"app_id": "website", "version": "0.1.2",
                                    "source": "catalog-katalog"})
ok("eine fremde ZIP hat keine solche Erwartung",
   a._staged_expect(os.path.join(DATA, "irgendwas.zip")) is None)
src2, _v, _s = a._store_lookup("gibt-es-nicht")
ok("eine unbekannte App ist nicht gelistet", src2 is None)

write_list([release("website", "0.1.2", sha="0" * 64)])
src3, _v, _s = a._store_lookup("website")
ok("falsche Pruefsumme: nichts wird geliefert, und der GRUND steht da",
   src3 is None and "checksum" in a._store_lookup.note, a._store_lookup.note)
write_list([release("website", "0.1.2", size=7)])
src3, _v, _s = a._store_lookup("website")
ok("falsche Groesse ebenso", src3 is None and "size" in a._store_lookup.note,
   a._store_lookup.note)
write_list([release("website", "0.1.2")])

print("\n=== die Installation durch den Worker ===")
r = install("website")
ok("direkt in Produktion: angelegt", r.get("ok"), r)
b = BUILT[-1]
ok("... als `website` auf dem Produktiv-Kanal", b["name"] == "website"
   and b["channel"] == "production", b)
ok("... mit der Quelle des Katalogs im Eintrag (fuer das spaetere Aktualisieren)",
   b["source"].get("store_source") == "catalog-katalog"
   and b["source"]["kind"] == "artifact", b["source"])
ok("... und NICHT als sideload markiert", "sideloaded_by" not in b["source"], b["source"])
ok("... mit der Pruefsumme der Datei",
   b["source"]["sha256"] == hashlib.sha256(open(os.path.join(
       BASE, "packages", "website-0.1.2.zip"), "rb").read()).hexdigest())

r = install("website", channel="test")
ok("dieselbe App noch einmal 'mit Test-Instanz': eine zweite Instanz "
   "`website-test` auf dem Test-Kanal, die Produktiv-Instanz bleibt unberuehrt",
   r.get("ok") and BUILT[-1]["name"] == "website-test"
   and BUILT[-1]["channel"] == "test"
   and a.load_registry()["instances"]["website"]["channel"] == "production",
   (r, BUILT[-1]))

write_list([release("vereinsportal", "0.0.9")])
r = install("vereinsportal", channel="test")
b = BUILT[-1]
ok("'mit Test-Instanz' einer NEUEN App: `vereinsportal-test` auf dem Test-Kanal",
   r.get("ok") and b["name"] == "vereinsportal-test" and b["channel"] == "test", (r, b))
ok("... und die App-Id der Anfrage blieb `vereinsportal` (der Katalog kennt "
   "keinen `-test`)", "vereinsportal" in b["manifest"])

print("\n=== eine Liste, die luegt ===")
write_list([release("tarnname", "1.0.0", data=make_zip("tarnname", "1.0.0",
                                                        manifest_id="ganz-anderes"))])
n = len(BUILT)
r = install("tarnname")
ok("Liste sagt `tarnname`, das Paket ist `ganz-anderes`: nicht gebaut, mit Grund",
   not r.get("ok") and len(BUILT) == n and "catalog lists" in r.get("message", ""), r)
write_list([release("alt", "1.0.0", data=make_zip("alt", "1.0.0",
                                                  manifest_version="9.9.9"))])
r = install("alt")
ok("Liste sagt 1.0.0, das Paket ist 9.9.9: ebenso",
   not r.get("ok") and len(BUILT) == n and "catalog lists" in r.get("message", ""), r)

write_list([release("kaputtpaket", "1.0.0", sha="0" * 64)])
n = len(BUILT)
r = install("kaputtpaket")
ok("Pruefsumme stimmt nicht: nicht gebaut, und die Antwort sagt WARUM -- nicht "
   "'nicht gelistet'", not r.get("ok") and len(BUILT) == n
   and "refused the package" in r.get("message", "")
   and "checksum" in r.get("message", "")
   and "not listed" not in r.get("message", ""), r)

print("\n=== was nicht dasselbe ist ===")
write_list([release("website", "0.1.2")])
reset_sources()
add_source("unverified")
r = install("neu-app")
ok("eine unbekannte App ist `nicht gelistet`", not r.get("ok")
   and "not listed" in r.get("message", ""), r)
write_list([release("webneu", "0.2.0")])
r = install("webneu")
ok("ungeprueft eingetragen: ohne Bestaetigung nicht installiert",
   not r.get("ok") and "unverified" in r.get("message", ""), r)
r = install("webneu", confirm="catalog-katalog")
ok("... mit der Bestaetigung der Quelle schon", r.get("ok"), r)
r = install("webneu", by="mitglied")
ok("ein normaler Benutzer installiert nichts", not r.get("ok"), r)
reset_sources()
r = install("webneu2")
ok("ohne Quelle gibt es nichts zu installieren", not r.get("ok"), r)

print("\n=== die Quelle verschwindet ===")
add_source()
reg = a.load_registry()
del reg["instances"]["katalog"]
a.save_registry(reg)
src4, _v, _s = a._store_lookup("website")
ok("die Katalog-Instanz gibt es nicht mehr: nichts geliefert, Grund genannt",
   src4 is None and "does not exist" in a._store_lookup.note, a._store_lookup.note)
a.catalog_view_write()
ok("... und das Portal bekommt einen Fehler statt einer alten Liste",
   json.load(open(snap_path, encoding="utf-8")).get("error"))
reset_sources()
a.catalog_view_write()
ok("ohne Quelle wird der Schnappschuss entfernt", not os.path.exists(snap_path))

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
