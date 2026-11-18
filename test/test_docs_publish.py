#!/usr/bin/env python3
"""Die Doku, die eine App mitbringt (RFC-0094 Stufe 1, oaap.apps.runtime
2.36, Manifest 0.17 `docs:` / `docs_sources:`).

    python3 test/test_docs_publish.py

A. Die Erklärung: Felder, Voreinstellungen, Ablehnungen (Köder je Regel)
B. Das Auspacken: nur der Ordner, nur .md und erklärte Medien, kein Link,
   kein Ausbruch, Grenzen lassen das Produkt fallen, nie die Installation
C. Der Knoten: Ablage je Instanz-ID und Version, docs.json, index.json mit
   dem AKTUELLEN Namen, ein Update ersetzt, Entfernen räumt ab, eine
   Generalprobe legt nichts ab; der Einhängepunkt nur für docs_sources,
   nie für eine Generalprobe, lesend
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-docs-test-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import docs_publish as dp  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def m(docs=None, **extra):
    d = {"oaap_manifest": "0.17", "app": {"id": "bdt", "name": "BDT", "version": "1.0.0"}}
    if docs is not None:
        d["docs"] = docs
    d.update(extra)
    return d


print("=== A. Die Erklärung ===")
GOOD = [{"path": "app/help/", "title": "BDT", "lang": "de", "audience": ["user", "admin"],
         "readers": ["end-user"], "kinds": {"faq.md": "faq", "kacheln/": "function"},
         "entry": "index.md", "color": "#0a6ed1"}]
ok("gute Erklärung", dp.declaration_error(m(GOOD)) == "")
p = dp.products_of(m(GOOD))[0]
ok("Produkt-id = app.id, Pfad mit /", p["product"] == "bdt" and p["path"] == "app/help/", p)
q = dp.products_of(m({"path": "docs"}))[0]
ok("eine einzelne Abbildung zählt als Liste; Voreinstellungen engen ein",
   q["audience"] == ["admin"] and q["title"] == "BDT" and "html" not in q["media"], q)
for bad, why in (
        ([], "one to"),
        ([{"path": "../etc"}], "folder inside the package"),
        ([{"path": "/abs"}], "folder inside the package"),
        ([{"path": "a"}, {"path": "b"}], "declared twice"),
        ([{"path": "a", "product": "Böse"}], "lowercase"),
        ([{"path": "a", "kinds": {"x/": "roman"}}], "not a document type"),
        ([{"path": "a", "kinds": {"../x": "faq"}}], "not a path inside"),
        ([{"path": "a", "readers": ["chef"]}], "readers"),
        ([{"path": "a", "media": ["exe"]}], "media"),
        ([{"path": "a", "audience": []}], "audience"),
        ([{"path": "a", "color": "rot"}], "#rrggbb"),
        ([{"path": "a", "geheim": 1}], "unknown field"),
        ("ja", "list of help products")):
    err = dp.declaration_error(m(bad))
    ok(f"KÖDER {json.dumps(bad)[:50]}: abgelehnt ({why})", why in err and not dp.products_of(m(bad)), err)
ok("docs_sources: nur read, nur true/false", dp.declaration_error(m(docs_sources={"read": "ja"}))
   and dp.declaration_error(m(docs_sources={"write": True}))
   and dp.docs_sources_wanted(m(docs_sources={"read": True}))
   and not dp.docs_sources_wanted(m(docs_sources={"read": "true"})))

print("\n=== B. Das Auspacken ===")
PKG = tempfile.mkdtemp(prefix="pkg-")


def put(rel, data=b"# x\n"):
    path = os.path.join(PKG, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


put("app/help/index.md", "# BDT\n".encode())
put("app/help/faq.md")
put("app/help/kacheln/chat.md")
put("app/help/bild.png", b"\x89PNG")
put("app/help/film.html", b"<script>alert(1)</script>")
put("app/help/tool.exe", b"MZ")
put("app/src/geheim.md", b"# nicht Doku")
put("secret.md", b"# draussen")
link_ok = True
try:
    os.symlink(os.path.join(PKG, "secret.md"), os.path.join(PKG, "app/help/link.md"))
except (OSError, NotImplementedError):
    link_ok = False
DEST = tempfile.mkdtemp(prefix="dest-")
pub, drop = dp.extract(PKG, dp.products_of(m(GOOD)), DEST)
got = sorted(os.path.relpath(os.path.join(d, f), DEST).replace(os.sep, "/")
             for d, _s, fs in os.walk(DEST) for f in fs)
ok("nur der Ordner, nur .md und Standard-Medien", got == ["bdt/bild.png", "bdt/faq.md",
                                                         "bdt/index.md", "bdt/kacheln/chat.md"], got)
ok("KÖDER: kein .html ohne media: [html], kein .exe, nichts von außerhalb",
   not any(x.endswith((".html", ".exe")) or "geheim" in x or "secret" in x for x in got))
if link_ok:
    ok("KÖDER: ein Link im Ordner wird nicht verfolgt", "bdt/link.md" not in got)
ok("veröffentlicht mit Zahl der Dateien, Einstieg bleibt", pub and pub[0]["files"] == 4
   and pub[0]["entry"] == "index.md" and not drop, (pub, drop))
pub2, drop2 = dp.extract(PKG, dp.products_of(m([{"path": "gibtsnicht/"}])), tempfile.mkdtemp())
ok("fehlender Ordner: fallen gelassen mit Grund, kein Fehler", not pub2
   and "not in the package" in drop2[0]["reason"])
old = dp.MAX_FILES
dp.MAX_FILES = 2
pub3, drop3 = dp.extract(PKG, dp.products_of(m(GOOD)), tempfile.mkdtemp())
dp.MAX_FILES = old
ok("zu viele Dateien: das Produkt fällt, nicht die Installation", not pub3
   and "more than 2 files" in drop3[0]["reason"])
pub4, _ = dp.extract(PKG, dp.products_of(m([{"path": "app/help", "media": ["html", "png"]}])),
                     d4 := tempfile.mkdtemp())
ok("media: [html] nimmt die HTML-Datei mit", os.path.isfile(os.path.join(d4, "bdt", "film.html")))

print("\n=== C. Der Knoten ===")
import appctl as a  # noqa: E402

a.ensure_default_tenant()
tid, _t = a.tenant_create("bdt", name="BDT")
iid = "abc123def456"
inst = {"id": iid, "tenant": tid, "app_id": "bdt", "app_name": "BDT", "version": "0.753.0",
        "channel": "production", "name": "app", "docs": dp.products_of(m(GOOD))}
reg = a.load_registry()
reg.setdefault("instances", {})["bdt-app"] = inst
a.save_registry(reg)
doc = a.docs_publish_instance("bdt-app", inst, PKG)
root = a.docs_root(tid)
ok("abgelegt unter <tenant>/docs/<instanz-id>/<version>/<produkt>/",
   os.path.isfile(os.path.join(root, iid, "0.753.0", "bdt", "faq.md")), os.listdir(root))
meta = json.load(open(os.path.join(root, iid, "0.753.0", "docs.json"), encoding="utf-8"))
ok("docs.json nennt Instanz, App, Version, Kanal, Produkte", meta["instance"] == "bdt-app"
   and meta["version"] == "0.753.0" and meta["channel"] == "production"
   and meta["products"][0]["product"] == "bdt")
idx = json.load(open(os.path.join(root, "index.json"), encoding="utf-8"))
ok("index.json: ein Satz mit Ordner und Namen", len(idx["sets"]) == 1
   and idx["sets"][0]["dir"] == f"{iid}/0.753.0" and idx["sets"][0]["name"] == "app"
   and idx["tenant"] == "bdt", idx)
ok("lesbar für den Container (0644/0755)", oct(os.stat(os.path.join(root, iid, "0.753.0", "bdt",
                                                                      "faq.md")).st_mode)[-3:] == "644"
   or os.name == "nt")
inst["version"] = "0.754.0"
a.docs_publish_instance("bdt-app", inst, PKG)
ok("ein Update ersetzt: nur noch die neue Version", os.listdir(os.path.join(root, iid)) == ["0.754.0"],
   os.listdir(os.path.join(root, iid)))
reg = a.load_registry()
reg["instances"]["bdt-app"]["name"] = "werkzeuge"
a.save_registry(reg)
a.docs_index_refresh(tid)
idx = json.load(open(os.path.join(root, "index.json"), encoding="utf-8"))
ok("umbenannt: der Index nennt den aktuellen Namen, die Dateien bleiben liegen",
   idx["sets"][0]["name"] == "werkzeuge" and os.path.isdir(os.path.join(root, iid, "0.754.0")))
reh = dict(inst, id="reh000000000", rehearsal={"of": "bdt-app", "expires": "2099-01-01T00:00:00Z"})
a.docs_publish_instance("bdt-app-probe", reh, PKG)
ok("eine Generalprobe legt nichts ab", not os.path.isdir(os.path.join(root, "reh000000000")))
help_inst = {"id": "hlp000000000", "tenant": tid, "docs_sources": True}
ok("Einhängepunkt für eine Hilfe mit docs_sources: der Ordner des Mandanten",
   a.docs_mount_of("bdt-help", help_inst) == root)
ok("KÖDER: keiner ohne docs_sources, keiner für eine Generalprobe",
   a.docs_mount_of("bdt-x", {"id": "x", "tenant": tid}) == ""
   and a.docs_mount_of("bdt-help-probe", dict(help_inst, rehearsal={"of": "bdt-help"})) == "")
src = open(a.__file__, encoding="utf-8").read()
ok("lesend eingehängt", 'mounts += ["-v", f"{dmount}:{DOCS_MOUNT}:ro"]' in src)
ok("die Installation legt ab, das Entfernen räumt ab",
   "docs_publish_instance(name, reg[\"instances\"][name], pkg)" in src
   and "docs_retire_instance(name, inst, reg)" in src)
ok("die Plattform kennt Manifest 0.17", a.MANIFEST_MINOR >= 17)
ok("das Register trägt docs und docs_sources", '"docs": docs_publish.products_of(m),' in src
   and '"docs_sources": docs_publish.docs_sources_wanted(m),' in src)
a.docs_retire_instance("bdt-app", inst)
idx = json.load(open(os.path.join(root, "index.json"), encoding="utf-8"))
ok("entfernt: Ordner weg, Index leer", not os.path.isdir(os.path.join(root, iid)) and idx["sets"] == [])

print("\n" + ("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER"))
sys.exit(1 if fails else 0)
