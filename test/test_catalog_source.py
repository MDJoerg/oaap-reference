#!/usr/bin/env python3
"""Paketkatalog als Store-Quelle: was die Datei des Katalogs sagen darf
(RFC-0050 Stufe 2, `catalog_source.py`).

Alles, was die Katalog-App in ihr Verzeichnis schreibt, ist DATEN einer
App: ein Pfad kann hinaus zeigen, eine Datei kann ein Link sein, Groesse
und Pruefsumme koennen luegen, die Datei kann sich zwischen Pruefen und
Benutzen aendern. Geprueft wird, was am ehesten still schiefginge:

    - ein Link (auf eine Datei ausserhalb) wird nicht gefolgt
    - ein Pfad mit `..`, absolut, mit Backslash oder zu tief wird abgelehnt
    - Groesse und SHA-256 kommen aus der DATEI, nicht aus der Liste
    - die Pruefung gilt der KOPIE des Knotens: aendert die App die Datei
      danach, aendert sich nichts an dem, was installiert wird
    - eine kaputte Liste ist ein Fehler, keine leere Liste
    - ein Eintrag, dem man nicht trauen kann, faellt weg, ohne die anderen
      mitzunehmen

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_catalog_source.py
"""
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import catalog_source as cs                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def refuses(label, fn, needle=""):
    try:
        fn()
    except cs.CatalogError as e:
        ok(label, needle in str(e), str(e))
        return
    except Exception as e:                                   # noqa: BLE001
        ok(label, False, f"wrong error {type(e).__name__}: {e}")
        return
    ok(label, False, "no error")


BASE = tempfile.mkdtemp(prefix="oaap-catalog-")
STAGE = tempfile.mkdtemp(prefix="oaap-catalog-stage-")


def put(rel, data):
    p = os.path.join(BASE, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def entry(app_id, version, rel, data, **kw):
    return {"id": app_id, "version": version, "name": app_id.title(),
            "package": {"zip": rel, "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data)}, **kw}


def write_list(apps, name="Betreiber"):
    with open(os.path.join(BASE, "store.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "0.2", "name": name, "apps": apps}, f)


Z1 = b"PK\x03\x04" + b"a" * 200
Z2 = b"PK\x03\x04" + b"b" * 300
put("packages/website/0.1.2-aaaa.zip", Z1)
put("packages/vereinsportal/0.0.9-bbbb.zip", Z2)

print("=== die Liste ===")
ok("ohne Liste: ein leerer Katalog, kein Fehler",
   cs.read_list(BASE) == {"name": "", "apps": []})
write_list([entry("website", "0.1.2", "packages/website/0.1.2-aaaa.zip", Z1),
            entry("vereinsportal", "0.0.9", "packages/vereinsportal/0.0.9-bbbb.zip",
                  Z2, summary="Portal", categories=["web"], evil="x")])
d = cs.read_list(BASE)
ok("zwei Eintraege gelesen, Name uebernommen",
   [a["id"] for a in d["apps"]] == ["website", "vereinsportal"]
   and d["name"] == "Betreiber", d)
ok("unbekannte Felder werden nicht durchgereicht",
   "evil" not in d["apps"][1] and d["apps"][1]["summary"] == "Portal")
refuses("eine Liste, die kein JSON ist, ist ein Fehler (keine leere Liste)",
        lambda: (open(os.path.join(BASE, "store.json"), "w").write("{kaputt"),
                 cs.read_list(BASE)), "JSON")
write_list([entry("website", "0.1.2", "packages/website/0.1.2-aaaa.zip", Z1)])
with open(os.path.join(BASE, "store.json"), "w") as f:
    json.dump({"apps": "nein"}, f)
refuses("'apps' ist keine Liste", lambda: cs.read_list(BASE), "'apps'")

print("\n=== ein Eintrag, dem man nicht traut, faellt allein weg ===")
good = entry("gut", "1.0.0", "packages/website/0.1.2-aaaa.zip", Z1)
bad = [
    dict(good, id="../x"), dict(good, id="Gross"), dict(good, version=""),
    dict(good, version=5), dict(good, package="x"),
    dict(good, package={"zip": "../../etc/passwd", "sha256": "0" * 64, "size": 5}),
    dict(good, package={"zip": "/etc/passwd", "sha256": "0" * 64, "size": 5}),
    dict(good, package={"zip": "a\\b.zip", "sha256": "0" * 64, "size": 5}),
    dict(good, package={"zip": "a/b/c/d/e.zip", "sha256": "0" * 64, "size": 5}),
    dict(good, package={"zip": "p/x.zip", "sha256": "xyz", "size": 5}),
    dict(good, package={"zip": "p/x.zip", "sha256": "0" * 64, "size": 0}),
    dict(good, package={"zip": "p/x.zip", "sha256": "0" * 64, "size": True}),
    "kein objekt", None, 7,
]
write_list(bad + [good, dict(good, version="9.9.9")])
d = cs.read_list(BASE)
ok("alle 15 unbrauchbaren Eintraege weg, der gute bleibt, der Doppelgaenger "
   "(gleiche App-Id) nicht", [a["id"] for a in d["apps"]] == ["gut"]
   and d["apps"][0]["version"] == "1.0.0", d)

print("\n=== was die Datei ist ===")
e = entry("website", "0.1.2", "packages/website/0.1.2-aaaa.zip", Z1)
p = cs.stage(BASE, e, STAGE, 10_000_000)
ok("die Kopie hat die Bytes der Datei", open(p, "rb").read() == Z1)
ok("... liegt im Verzeichnis des Knotens, nicht beim Katalog",
   os.path.dirname(p) == STAGE)
put("packages/website/0.1.2-aaaa.zip", b"PK\x03\x04" + b"EVIL" * 50)
ok("die App tauscht die Datei NACH dem Pruefen: die Kopie bleibt unberuehrt",
   open(p, "rb").read() == Z1)
put("packages/website/0.1.2-aaaa.zip", Z1)

refuses("falsche Pruefsumme in der Liste",
        lambda: cs.stage(BASE, dict(e, package=dict(e["package"], sha256="0" * 64)),
                         STAGE, 10_000_000), "checksum")
refuses("falsche Groesse in der Liste (kleiner)",
        lambda: cs.stage(BASE, dict(e, package=dict(e["package"], size=5)),
                         STAGE, 10_000_000), "size")
refuses("falsche Groesse in der Liste (groesser)",
        lambda: cs.stage(BASE, dict(e, package=dict(e["package"], size=9999)),
                         STAGE, 10_000_000), "size")
refuses("ueber der Grenze, bevor etwas kopiert wird",
        lambda: cs.stage(BASE, e, STAGE, 100), "limit")
left = [f for f in os.listdir(STAGE)]
ok("ein Fehlschlag laesst keine Kopie liegen (nur die eine gute von oben)",
   len(left) == 1, left)
refuses("die Datei gibt es nicht",
        lambda: cs.stage(BASE, dict(e, package=dict(e["package"],
                                                    zip="packages/website/weg.zip")),
                         STAGE, 10_000_000), "does not exist")

print("\n=== Links ===")
link_ok = True
outside = tempfile.mkdtemp(prefix="oaap-catalog-out-")
secret = os.path.join(outside, "geheim.zip")
open(secret, "wb").write(Z1)
try:
    os.symlink(secret, os.path.join(BASE, "packages", "website", "link.zip"))
    os.symlink(outside, os.path.join(BASE, "packages", "linkdir"))
except (OSError, NotImplementedError):
    link_ok = False
if link_ok:
    refuses("eine Datei, die ein Link nach draussen ist, wird nicht gefolgt",
            lambda: cs.stage(BASE, dict(e, package=dict(
                e["package"], zip="packages/website/link.zip")), STAGE, 10_000_000))
    refuses("ein Verzeichnis, das ein Link ist, ebenso",
            lambda: cs.stage(BASE, dict(e, package=dict(
                e["package"], zip="packages/linkdir/geheim.zip")), STAGE, 10_000_000))
    os.symlink(outside, os.path.join(BASE, "store-link"))
    os.replace(os.path.join(BASE, "store.json"), os.path.join(BASE, "alt.json"))
    os.symlink(os.path.join(outside, "x.json"), os.path.join(BASE, "store.json"))
    refuses("auch die Liste selbst darf kein Link sein", lambda: cs.read_list(BASE))
    os.remove(os.path.join(BASE, "store.json"))
    os.replace(os.path.join(BASE, "alt.json"), os.path.join(BASE, "store.json"))
else:
    print("SKIP  Links: hier koennen keine Symlinks angelegt werden "
          "(nur auf dem Knoten pruefbar)")
refuses("ein Verzeichnis ist keine Paketdatei",
        lambda: cs.stage(BASE, dict(e, package=dict(
            e["package"], zip="packages/website")), STAGE, 10_000_000), "regular")

print("\n=== aufraeumen ===")
import time                                                    # noqa: E402
old = os.path.join(STAGE, "pkg-alt.zip")
open(old, "wb").write(b"x")
os.utime(old, (time.time() - 7200, time.time() - 7200))
other = os.path.join(STAGE, "fremd.txt")
open(other, "wb").write(b"x")
os.utime(other, (time.time() - 7200, time.time() - 7200))
n = cs.prune(STAGE)
ok("eine alte Kopie wird entfernt, fremde Dateien nicht, junge nicht",
   n >= 1 and not os.path.exists(old) and os.path.exists(other)
   and os.path.exists(p), (n, os.listdir(STAGE)))

print("\n=== Schnappschuss fuer das Portal ===")
write_list([entry("website", "0.1.2", "packages/website/0.1.2-aaaa.zip", Z1)])
snap = cs.snapshot(cs.read_list(BASE))
ok("kein Dateipfad im Schnappschuss, ein Marker, Pruefsumme und Groesse",
   snap["apps"][0]["package"] == {"catalog": True, "sha256": hashlib.sha256(Z1).hexdigest(),
                                  "size": len(Z1)}
   and "zip" not in json.dumps(snap), snap)

print("\n=== Adresse ===")
ok("catalog:<Schluessel> -> Schluessel", cs.instance_of("catalog:katalog") == "katalog"
   and cs.instance_of("catalog:acme.katalog") == "acme.katalog")
ok("alles andere ist keiner",
   cs.instance_of("https://x") == "" and cs.instance_of("catalog:../x") == ""
   and cs.instance_of("catalog:") == "" and cs.instance_of(None) == "")

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
