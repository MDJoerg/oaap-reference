#!/usr/bin/env python3
"""Apps bringen ihre Vorlagen für Assistenten mit (RFC-0119 Stufen 1 und 2).

Gemessen wird, was das RFC verspricht:

  * Manifest 0.20 `assistant_templates: {path}` -- nur mit MCP-Tür, nur ein
    Ordner im Paket, kein anderes Feld;
  * die Vorlage `vorgaenge-ueberblick` der App Vorgänge ist ein gültiges
    Rezept und hält §4.2-4.4;
  * KÖDER §4.2/4.3: fremde Kennung, Installation, Destination, eine andere
    App, ein fremder Server, schreibende Werkzeuge, eine Ampel mit Vorgabe,
    ein Profil oder Assistent auf einer fremden Sicht -- nicht übernommen;
  * §4.1: der Knoten kopiert je Mandant, was gilt, und sagt, was er
    fallen lässt; ein Link wird nicht verfolgt; bei Entfernen weg;
  * D2: nur im Mandanten der App -- Datei, Riegel, Sicht fürs Portal;
  * §4.2: eine Datei des Betreibers mit derselben Kennung gewinnt;
  * D6: frei für den tenant_admin des Mandanten, eine veränderte nicht;
  * §4.4: steht die Quelle auf Rot, fragt der Assistent die Ampel -- der
    Vorschlag der App ist zu sehen, nicht gewählt; hat ein Mensch sie
    gesetzt, gilt seine.

Stufe 2:

  * jede Vorlage der Apps in oaap-apps hält die Regeln, ihr Manifest ist gültig;
  * §4.5: der Betreiber sperrt die Vorlagen einer App je Mandant -- die Karte
    sagt „vom Betreiber gesperrt“, der Riegel hält auch gegen eine Freigabe,
    nur der server_admin sperrt, und er gibt sie zurück;
  * ein Lauf merkt sich die App seiner Vorlage; die Seite sagt „Vorlage
    neuer“ nach einem Update und „Vorlage nicht mehr da“ nach dem Entfernen,
    nichts bei einem zurückgebauten Lauf (§5, §6, D9).

Aufruf: python3 test/test_app_templates.py
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-app-tpl-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as a                                            # noqa: E402
import app_templates as at                                    # noqa: E402
import tenant_build as tb                                     # noqa: E402
import tenant_recipe as tr                                    # noqa: E402
import recipe_view as rv                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


APP = os.path.join(HERE, "..", "..", "oaap-apps", "apps", "vorgaenge")
with open(os.path.join(APP, "assistants", "vorgaenge-ueberblick.json"), encoding="utf-8") as f:
    TPL = json.load(f)
with open(os.path.join(APP, "oaap-app.yaml"), encoding="utf-8") as f:
    MANIFEST = yaml.safe_load(f)

print("=== Manifest 0.20 ===")
ok("das Manifest der App Vorgänge nennt den Ordner und ist gültig",
   at.declaration_error(MANIFEST) == "" and at.folder_of(MANIFEST) == "assistants",
   at.declaration_error(MANIFEST))
ok("der Knoten liest Manifest 0.20", a.MANIFEST_MINOR >= 20)
m = dict(MANIFEST)
del m["mcp"]
ok("ohne MCP-Tür: abgelehnt", "MCP door" in at.declaration_error(m), at.declaration_error(m))
ok("ein Pfad, der das Paket verlässt: abgelehnt",
   "folder inside" in at.declaration_error(dict(MANIFEST, assistant_templates={"path": "../x"})))
ok("ein unbekanntes Feld: abgelehnt",
   "only 'path'" in at.declaration_error(
       dict(MANIFEST, assistant_templates={"path": "assistants/", "url": "http://x"})))
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.validate_manifest(dict(MANIFEST, assistant_templates={"path": "/etc"}))
    stopped = False
except SystemExit:
    stopped = True
ok("…und so sagt es die Prüfung des Manifests (die Installation hält an)",
   stopped and "assistant_templates.path" in buf.getvalue(), buf.getvalue())

print("=== die Vorlage der App Vorgänge ===")
ok("ein gültiges Rezept, eine Vorlage für einen Assistenten",
   tr.recipe_problems(TPL) == [] and TPL["offers"] == "assistant", tr.recipe_problems(TPL))
ok("hält die Regeln einer App-Vorlage (§4.2-4.4)",
   at.template_problems(TPL, "vorgaenge") == [], at.template_problems(TPL, "vorgaenge"))
ok("…aber nicht als Vorlage einer anderen App",
   any("begin with 'projekt-'" in p for p in at.template_problems(TPL, "projekt")))
ok("die Ampel der eigenen Quelle ist ein Wert ohne Vorgabe",
   TPL["params"]["quelle_ampel"]["kind"] == "light"
   and "default" not in TPL["params"]["quelle_ampel"])


def bait(change, word, label):
    d = copy.deepcopy(TPL)
    change(d)
    p = at.template_problems(d, "vorgaenge")
    ok(f"KÖDER {label}: abgelehnt", any(word in x for x in p), p)


def items(d):
    return d["steps"][0]["items"]


bait(lambda d: d.update(id="ueberblick"), "begin with", "Kennung ohne den Namen der App")
bait(lambda d: d["needs"]["source"].update(install=True), "installs nothing", "Installation")
bait(lambda d: d["needs"].update(sap={"app": "sap-sso", "label": "SAP"}), "not 'sap-sso'",
     "eine andere App")
bait(lambda d: d["steps"].insert(0, {"id": "weg", "type": "destination.ensure", "name": "x",
                                     "instance": "{source}", "target": "https://x.example",
                                     "auth": "none"}),
     "app.setup", "eine Destination")
bait(lambda d: items(d)[2]["tools"].update(server="{chat_key}"), "own server",
     "Sicht auf einen fremden Server")
bait(lambda d: items(d)[2].update(tools=[{"server": "{source_key}", "tool": "create_case",
                                          "effect": "write"}]), "reading tools",
     "schreibende Werkzeuge")
bait(lambda d: items(d)[3].update(max_effect="write"), "reads only", "Profil schreibt")
bait(lambda d: d["params"]["quelle_ampel"].update(default="green"), "never chooses",
     "die App wählt die Ampel ihrer Quelle")
bait(lambda d: items(d)[1].update(light="green"), "never chooses", "Ampel als feste Angabe")
bait(lambda d: items(d)[0].update(id="{chat_key}"), "own server", "refresh eines fremden Servers")
bait(lambda d: items(d)[3].update(views=["hilfe"]), "own view", "Profil auf einer fremden Sicht")
bait(lambda d: items(d)[4].update(view="hilfe"), "own view", "Assistent auf einer fremden Sicht")
bait(lambda d: d["steps"].append({"id": "frei", "type": "app.setup", "name": "{chat}",
                                  "items": [{"kind": "x", "id": "y"}]}),
     "only the MCP gateway", "eine andere App einrichten")

print("=== §4.1: der Knoten kopiert je Mandant ===")
tenants = a.load_tenants()
tenants.update({"t-kunde": {"label": "kunde", "name": "Kunde"},
                "t-pxx": {"label": "pxx", "name": "PXX"}})
a.save_tenants(tenants)
pkg = tempfile.mkdtemp(prefix="oaap-pkg-")
os.makedirs(os.path.join(pkg, "assistants"))
json.dump(TPL, open(os.path.join(pkg, "assistants", "vorgaenge-ueberblick.json"), "w",
                    encoding="utf-8"), ensure_ascii=False)
bad = dict(copy.deepcopy(TPL), id="ueberblick")
json.dump(bad, open(os.path.join(pkg, "assistants", "ueberblick.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
open(os.path.join(pkg, "assistants", "liesmich.txt"), "w").write("nicht lesen")
secret = os.path.join(pkg, "geheim.json")
json.dump(dict(copy.deepcopy(TPL), id="vorgaenge-link"), open(secret, "w", encoding="utf-8"))
linked = True
try:
    os.symlink(secret, os.path.join(pkg, "assistants", "vorgaenge-link.json"))
except (OSError, NotImplementedError):
    linked = False
INST = {"tenant": "t-kunde", "id": "i0001", "app_id": "vorgaenge", "app_name": "Vorgänge",
        "name": "vorgaenge", "version": "0.18.0", "assistant_templates": "assistants"}
meta = a.app_templates_publish_instance("kunde-vorgaenge", INST, pkg)
root_k = a.app_templates_root("t-kunde")
ok("die gültige Vorlage liegt im Ordner des Mandanten",
   [t["id"] for t in meta["templates"]] == ["vorgaenge-ueberblick"]
   and os.path.isfile(os.path.join(root_k, "i0001", "vorgaenge-ueberblick.json")), meta)
ok("die ungültige ist gefallen, mit Grund",
   any(x["file"] == "ueberblick.json" and "begin with" in x["reason"] for x in meta["dropped"]),
   meta["dropped"])
ok("eine Datei, die kein JSON ist, wird gar nicht gelesen",
   not any(x["file"] == "liesmich.txt" for x in meta["dropped"]))
if linked:
    ok("ein Link wird nicht verfolgt",
       any(x["file"] == "vorgaenge-link.json" and "link" in x["reason"] for x in meta["dropped"])
       and not os.path.exists(os.path.join(root_k, "i0001", "vorgaenge-link.json")),
       meta["dropped"])
ok("im anderen Mandanten: nichts", at.listing(a.app_templates_root("t-pxx")) == [])

print("=== D2/§4.2: welche Datei ===")
own = os.path.join(a.RECIPE_DIR, "vorgaenge-ueberblick.json")
ok("mit dem Mandanten: die Datei der App",
   a.tenant_recipe_path("vorgaenge-ueberblick", "t-kunde")
   == os.path.join(root_k, "i0001", "vorgaenge-ueberblick.json"))
ok("ohne Mandanten oder im anderen: nicht die der App",
   a.tenant_recipe_path("vorgaenge-ueberblick") == own
   and a.tenant_recipe_path("vorgaenge-ueberblick", "t-pxx") == own)
os.makedirs(a.RECIPE_DIR, exist_ok=True)
json.dump(TPL, open(own, "w", encoding="utf-8"), ensure_ascii=False)
ok("eine Datei des Betreibers mit derselben Kennung gewinnt",
   a.tenant_recipe_path("vorgaenge-ueberblick", "t-kunde") == own
   and a.app_template_meta(TPL, "t-kunde") is None)
os.remove(own)

print("=== D6: der Riegel ===")


def gate(tid, label, doc):
    try:
        a._recipe_tenant_gate("tenant_admin", tid, tid, {"label": label,
                                                         "tenant_admin_may": []}, doc)
        return ""
    except tb.Refusal as exc:
        return str(exc)


ok("der tenant_admin von kunde: ohne Freigabe", gate("t-kunde", "kunde", TPL) == "",
   gate("t-kunde", "kunde", TPL))
changed = copy.deepcopy(TPL)
changed["params"]["system"]["default"] = "anders"
ok("eine veränderte Vorlage: nicht frei", "not released" in gate("t-kunde", "kunde", changed))
ok("der tenant_admin von pxx: nicht frei (die App gibt es dort nicht)",
   "not released" in gate("t-pxx", "pxx", TPL))

print("=== die Sicht fürs Portal ===")
a.tenant_recipe_view_write()
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
row = next((r for r in view["recipes"] if r["id"] == "vorgaenge-ueberblick"), None)
ok("eine Zeile mit Mandant, App und frei",
   row and row["tenant"] == "kunde" and row["app"]["name"] == "Vorgänge"
   and row["free"] is True and row["offers"] == "assistant" and not row["problem"], row)
cards_k = rv.templates_for(view, "kunde", "tenant_admin")
card = next((c for c in cards_k if c["id"] == "vorgaenge-ueberblick"), None)
ok("die Karte in kunde: startbar, „von der App Vorgänge“",
   card and card["usable"] and card["app"] == "Vorgänge", cards_k)
ok("in pxx keine Karte, auch nicht für den Betreiber",
   not any(c["id"] == "vorgaenge-ueberblick" for c in rv.templates_for(view, "pxx", "tenant_admin"))
   and not any(c["id"] == "vorgaenge-ueberblick"
               for c in rv.templates_for(view, "pxx", "server_admin")))
ok("find_template: nur in kunde",
   rv.find_template(view, "vorgaenge-ueberblick", "kunde", "tenant_admin")
   and not rv.find_template(view, "vorgaenge-ueberblick", "pxx", "tenant_admin"))
_n, asked, shaped = rv.wizard_parts(row)
q = next((f for f in asked if f["name"] == "quelle_ampel"), None)
ok("der Assistent fragt die Ampel der Quelle -- nichts gewählt",
   q and q["kind"] == "light" and not any(o["selected"] for o in q["options"]), asked)
ok("die Ampel des Chats (aus dem Modell) bleibt Sache der Vorlage",
   not any(f["name"] == "ceiling" for f in asked + shaped))

print("=== §4.4: die Ampel der eigenen Quelle ===")
a._recipe_instances = lambda tid: [("kunde-vorgaenge", "vorgaenge", "vorgaenge"),
                                   ("kunde-werkzeuge", "werkzeuge", "mcp-gateway"),
                                   ("kunde-modelle", "modelle", "ai-gateway"),
                                   ("kunde-chat", "chat", "chat")] if tid == "t-kunde" else []


def reader(light):
    def read(key, kind):
        if kind == "alias":
            return [{"id": "standard", "title": "Standard", "light": "yellow"}]
        if kind == "server_light":
            return [{"id": "kunde-vorgaenge", "title": "Vorgänge", "light": light},
                    {"id": "kunde-hilfe", "title": "Hilfe", "light": "green"}]
        return []
    return read


_d, _g, prof, vals, _c, choices, probs = a._recipe_prepare(
    "vorgaenge-ueberblick", "kunde", {}, "tenant_admin", actor_tid="t-kunde",
    read_list=reader("red"), soft=True)
ch = choices.get("quelle_ampel") or {}
ok("Quelle rot: die Frage bleibt offen, der Vorschlag der App steht daneben",
   any("Ampel der Quelle" in p for p in probs) and ch.get("suggested") == "yellow"
   and ch.get("proposal") == "app" and not ch.get("value"), (probs, ch))
_d, _g, prof, vals, _c, choices, probs = a._recipe_prepare(
    "vorgaenge-ueberblick", "kunde", {"quelle_ampel": "yellow"}, "tenant_admin",
    actor_tid="t-kunde", read_list=reader("red"), soft=True)
ok("die Person wählt gelb: die Vorlage setzt gelb", probs == [] and vals["quelle_ampel"] == "yellow",
   probs)
_d, _g, prof, vals, _c, choices, probs = a._recipe_prepare(
    "vorgaenge-ueberblick", "kunde", {}, "tenant_admin", actor_tid="t-kunde",
    read_list=reader("green"), soft=True)
ok("ein Mensch hat grün gesetzt: das gilt, nichts zu fragen",
   probs == [] and vals["quelle_ampel"] == "green"
   and choices["quelle_ampel"].get("current") == "green", (probs, choices.get("quelle_ampel")))
plan = tr.filled_plan(prof, vals)
ok("der gefüllte Plan richtet nur die Quelle der App ein",
   all(t == "app.setup" for _i, t, _a in plan)
   and any(i.get("kind") == "server_light" and i.get("id") == "kunde-vorgaenge"
           for _i, _t, ar in plan for i in ar.get("items") or []), plan)

print("=== Stufe 2: die Vorlagen aller Apps ===")
import glob                                                   # noqa: E402
APPS = os.path.join(HERE, "..", "..", "oaap-apps", "apps")
found = 0
for path in sorted(glob.glob(os.path.join(APPS, "*", "assistants", "*.json"))):
    app_id = os.path.basename(os.path.dirname(os.path.dirname(path)))
    with open(path, "rb") as f:
        doc, why = at._check(f.read(), app_id)
    with open(os.path.join(APPS, app_id, "oaap-app.yaml"), encoding="utf-8") as f:
        man = yaml.safe_load(f)
    ok(f"{app_id}: {os.path.basename(path)} hält die Regeln, Manifest gültig",
       doc is not None and at.declaration_error(man) == "" and at.folder_of(man) == "assistants",
       why or at.declaration_error(man))
    found += 1
ok("mindestens elf Apps bringen eine Vorlage mit", found >= 11, found)

print("=== Stufe 2, §4.5: die Sperre des Betreibers ===")
okb, msg = a.tenant_templates_block("t-kunde", "vorgaenge", True, "probe", "tenant_admin")
ok("KÖDER: ein tenant_admin sperrt nicht", not okb and "server_admin" in msg, msg)
okb, msg = a.tenant_templates_block("t-kunde", "vorgaenge", True, "root", "root")
ok("der Betreiber sperrt die Vorlagen von vorgaenge in kunde", okb and "blocked" in msg, msg)
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
row = next((r for r in view["recipes"] if r["id"] == "vorgaenge-ueberblick"), {})
ok("die Zeile fürs Portal: gesperrt, nicht frei", row.get("blocked") is True and row.get("free") is False,
   row)
card = next((c for c in rv.templates_for(view, "kunde", "tenant_admin")
             if c["id"] == "vorgaenge-ueberblick"), {})
ok("die Karte: „vom Betreiber gesperrt“ (blocked), nicht startbar",
   card.get("blocked") is True and card.get("usable") is False, card)
ok("der Betreiber selbst kann sie im Mandanten noch starten",
   rv.find_template(view, "vorgaenge-ueberblick", "kunde", "server_admin") is not None)
ok("der Riegel: gesperrt", "blocked" in gate("t-kunde", "kunde", TPL), gate("t-kunde", "kunde", TPL))
try:
    a._recipe_tenant_gate("tenant_admin", "t-kunde", "t-kunde",
                          dict(a.load_tenants()["t-kunde"],
                               tenant_admin_may=[tr.release_word("vorgaenge-ueberblick", "1.0.0")]),
                          TPL)
    why = ""
except tb.Refusal as exc:
    why = str(exc)
ok("KÖDER: auch eine Freigabe schlägt die Sperre nicht", "blocked" in why, why)
view2 = dict(view, recipes=[dict(r, released_for=["kunde"]) if r["id"] == "vorgaenge-ueberblick" else r
                            for r in view["recipes"]])
ok("…und im Portal nicht", rv.find_template(view2, "vorgaenge-ueberblick", "kunde", "tenant_admin") is None)
ok("der Mandant pxx bleibt unberührt", a.app_templates_blocked("t-pxx") == set())
okb, msg = a.tenant_templates_block("t-kunde", "vorgaenge", False, "root", "root")
ok("zurückgegeben: wieder frei", okb and gate("t-kunde", "kunde", TPL) == "", (msg, gate("t-kunde", "kunde", TPL)))

print("=== Stufe 2: der Lauf merkt sich die App; Vorlage neuer / nicht mehr da ===")
import types                                                  # noqa: E402
saved = (a.tenant_recipe.preview, a._recipe_drive)
a.tenant_recipe.preview = lambda *x, **k: []
a._recipe_drive = lambda state, profile, drivers: state
try:
    st = a.tenant_recipe_run("vorgaenge-ueberblick", "kunde", {"quelle_ampel": "yellow"}, "probe",
                             "tenant_admin", drivers=types.SimpleNamespace(), actor_tid="t-kunde",
                             read_list=reader("red"))
finally:
    a.tenant_recipe.preview, a._recipe_drive = saved
ok("der Lauf nennt die App seiner Vorlage",
   (st.get("app") or {}).get("id") == "vorgaenge" and st["app"].get("name") == "Vorgänge", st.get("app"))
st["state"] = "done"
tb.save_state(a.RECIPE_RUN_DIR, st)
a.tenant_recipe_view_write()
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
runs = rv.template_runs(view, "kunde")
r0 = next((r for r in runs if r["id"] == st["id"]), {})
ok("die Seite listet ihn, „von der App Vorgänge“, ohne Hinweis", r0.get("app") == "Vorgänge"
   and r0.get("mark") == "", r0)
ok("in pxx nicht", not any(r["id"] == st["id"] for r in rv.template_runs(view, "pxx")))
pkg2 = tempfile.mkdtemp(prefix="oaap-pkg2-")
os.makedirs(os.path.join(pkg2, "assistants"))
json.dump(dict(copy.deepcopy(TPL), version="1.1.0"),
          open(os.path.join(pkg2, "assistants", "vorgaenge-ueberblick.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
a.app_templates_publish_instance("kunde-vorgaenge", dict(INST, version="0.19.0"), pkg2)
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
r0 = next((r for r in rv.template_runs(view, "kunde") if r["id"] == st["id"]), {})
ok("nach dem Update der App: „Vorlage neuer (1.1.0)“, der Assistent bleibt",
   "Vorlage neuer (1.1.0)" in r0.get("mark", ""), r0)
ok("die Karte zeigt 1.1.0",
   any(c["id"] == "vorgaenge-ueberblick" and c["version"] == "1.1.0"
       for c in rv.templates_for(view, "kunde", "tenant_admin")))
a.app_templates_retire_instance("kunde-vorgaenge", INST)
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
r0 = next((r for r in rv.template_runs(view, "kunde") if r["id"] == st["id"]), {})
ok("nach dem Entfernen: der Lauf steht noch da, „Vorlage nicht mehr da“",
   "Vorlage nicht mehr da" in r0.get("mark", "") and r0.get("mark_tone") == "err", r0)
st["state"] = "rolled-back"
tb.save_state(a.RECIPE_RUN_DIR, st)
a.tenant_recipe_view_write()
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
r0 = next((r for r in rv.template_runs(view, "kunde") if r["id"] == st["id"]), {})
ok("ein zurückgebauter Lauf: kein Hinweis", r0 and r0.get("mark") == "", r0)
os.remove(tb.state_path(a.RECIPE_RUN_DIR, st["id"]))
shutil.rmtree(pkg2, ignore_errors=True)
a.app_templates_publish_instance("kunde-vorgaenge", INST, pkg)

print("=== Entfernen ===")
a.app_templates_retire_instance("kunde-vorgaenge", INST)
ok("die Vorlage ist weg", at.listing(root_k) == [])
a.tenant_recipe_view_write()
view = json.load(open(a.RECIPE_VIEW, encoding="utf-8"))
ok("…und die Karte auch", not any(r["id"] == "vorgaenge-ueberblick" for r in view["recipes"]))
meta = a.app_templates_publish_instance("kunde-vorgaenge", dict(INST, rehearsal="probe"), pkg)
ok("eine Generalprobe bringt keine Vorlagen", meta is None and at.listing(root_k) == [])

shutil.rmtree(pkg, ignore_errors=True)
print(f"\nFAIL: {fails}" if fails else "\nalle grün")
sys.exit(1 if fails else 0)
