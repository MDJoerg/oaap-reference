#!/usr/bin/env python3
"""Profilversionen und erzeugte Werte (RFC-0079, I-42 Teil 2/3).

Die Saetze, die diese Datei verteidigt:

    Eine hoehere Version ersetzt, die bisherige bleibt im Verlauf.
    Dieselbe Version mit denselben Bytes aendert nichts; mit anderen wird sie
    abgelehnt, ebenso eine aeltere, ebenso eine ohne Version ueber einer mit.
    Loeschen nimmt den Verlauf mit, aber nicht, solange ein Aufbau offen ist.
    Ein Aufbau merkt sich Version und Pruefsumme.
    `generate: true` erzeugt nur in einen LEEREN Schluessel und nie, wenn das
    Manifest es nicht erlaubt; der Wert steht in keinem Hinweis.
    Zwei Instanzen bekommen zwei verschiedene Schluessel.
    Ein erzeugter VAPID-Schluessel ist ein gueltiger P-256-Skalar.

Aufruf: python3 test/test_profile_versions.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-profile-versions-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
import gen_values                                             # noqa: E402
import tenant_build as tb                                     # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def prof(version=None, title="Probe"):
    d = {"profile": tb.PROFILE_FORMAT, "id": "probe", "title": title,
         "params": {"label": {"kind": "label", "required": True}},
         "steps": [{"id": "t", "type": "tenant.create", "label": "{label}"}]}
    if version is not None:
        d["version"] = version
    return d


def put(doc):
    return a.tenant_profile_job({"op": "put", "args": {
        "content": json.dumps(doc, ensure_ascii=False)}}, "server_admin", "betreiber")


def current():
    with open(a.tenant_build_profile_path("probe"), encoding="utf-8") as f:
        return json.load(f)


print("Die Reihenfolge der Versionen")
k = tb.version_key
ok("1.2.10 nach 1.2.9", k("1.2.10") > k("1.2.9"))
ok("1.0.0 nach 1.0.0-rc1", k("1.0.0") > k("1.0.0-rc1"))
ok("1.0.0-rc2 nach 1.0.0-rc1", k("1.0.0-rc2") > k("1.0.0-rc1"))
ok("ein falsches Versionsfeld wird abgelehnt",
   any("'version'" in p for p in tb.profile_problems(prof("1.0"))))
ok("ein Profil ohne Version bleibt gueltig", tb.profile_problems(prof()) == [])

print("")
print("Hochladen nach Regeln (V1-V3)")
r = put(prof("1.0.0"))
ok("1.0.0 wird angelegt", r[0] and "added" in r[1], r)
r = put(prof("1.1.0", "Probe neu"))
ok("1.1.0 ersetzt 1.0.0", r[0] and "replaces 1.0.0" in r[1]
   and current()["version"] == "1.1.0", r)
hist = tb.history_list(a.PROFILE_DIR, "probe")
ok("beide stehen im Verlauf, die neueste zuerst",
   [h["version"] for h in hist] == ["1.1.0", "1.0.0"], hist)
ok("der Verlauf merkt sich, wer hochgeladen hat",
   hist[0]["by"] == "betreiber", hist[0])
r = put(prof("1.1.0", "Probe neu"))
ok("dieselbe Version mit denselben Bytes: keine Aenderung",
   r[0] and "no change" in r[1], r)
r = put(prof("1.1.0", "Probe anders"))
ok("dieselbe Version mit anderem Inhalt: abgelehnt, nichts abgelegt",
   not r[0] and "raise the version" in r[1]
   and current()["title"] == "Probe neu"
   and len(tb.history_list(a.PROFILE_DIR, "probe")) == 2, r)
r = put(prof("1.0.1"))
ok("eine aeltere Version: abgelehnt", not r[0] and "older" in r[1], r)
r = put(prof(None, "ohne"))
ok("ohne Version ueber einer mit: abgelehnt",
   not r[0] and "without a version" in r[1], r)

print("")
print("Eine Datei, die auf der Maschine geschrieben wurde, geht nicht verloren")
with open(a.tenant_build_profile_path("probe"), "w", encoding="utf-8") as f:
    json.dump(prof("1.2.0", "von Hand"), f)
r = put(prof("1.3.0"))
ok("die ersetzte Datei von Hand steht danach im Verlauf",
   r[0] and "1.2.0" in [h["version"] for h in tb.history_list(a.PROFILE_DIR, "probe")], r)

print("")
print("Die Ansicht fuer das Portal (D3/D4)")
a.tenant_build_view_write()
with open(a.BUILD_VIEW, encoding="utf-8") as f:
    view = json.load(f)
row = next(p for p in view["profiles"] if p["id"] == "probe")
ok("die Ansicht nennt Version und Verlauf mit Datei",
   row["version"] == "1.3.0" and len(row["history"]) == 4
   and row["history"][0]["current"] and isinstance(row["history"][0]["doc"], dict),
   {k_: row.get(k_) for k_ in ("version",)})
st = tb.new_state(prof("1.3.0"), "ab" * 32, {"label": "vx"}, "betreiber")
ok("ein Aufbau merkt sich Profil, Version und Pruefsumme",
   st["profile"] == "probe" and st["version"] == "1.3.0" and st["digest"] == "ab" * 32)

print("")
print("Loeschen (V5)")
st["state"] = "waiting"
tb.save_state(a.BUILD_DIR, st)
r = a.tenant_profile_job({"op": "delete", "args": {"id": "probe"}}, "server_admin", "b")
ok("solange ein Aufbau offen ist: abgelehnt, Verlauf bleibt",
   not r[0] and tb.history_list(a.PROFILE_DIR, "probe"), r)
st["state"] = "done"
tb.save_state(a.BUILD_DIR, st)
r = a.tenant_profile_job({"op": "delete", "args": {"id": "probe"}}, "server_admin", "b")
ok("danach: Profil und Verlauf sind weg",
   r[0] and not os.path.exists(a.tenant_build_profile_path("probe"))
   and tb.history_list(a.PROFILE_DIR, "probe") == [], r)

print("")
print("Der Schritt app.config mit generate")
good = {"profile": tb.PROFILE_FORMAT, "id": "g", "title": "g",
        "params": {"label": {"kind": "label", "required": True}},
        "steps": [{"id": "t", "type": "tenant.create", "label": "{label}"},
                  {"id": "push", "type": "app.config", "name": "web",
                   "key": "PUSH_KEY", "generate": True}]}
ok("ein Profil mit generate: true ist gueltig", tb.profile_problems(good) == [],
   tb.profile_problems(good))
both = json.loads(json.dumps(good))
both["steps"][1]["value"] = "x"
ok("value UND generate: abgelehnt",
   any("exactly one" in p for p in tb.profile_problems(both)))
txt = json.loads(json.dumps(good))
txt["steps"][1]["generate"] = "vapid"
ok("generate nennt nie die Art (nur true)",
   any("generate is true" in p for p in tb.profile_problems(txt)))

STORE = {"web": {"PUSH_KEY": "", "PLAIN": ""}, "web2": {"PUSH_KEY": ""}}
KINDS = {"PUSH_KEY": "vapid", "PLAIN": ""}
APPLIED, AUDITED = [], []
a.load_registry = lambda: {"instances": {"k-web": {"version": "1"},
                                         "k-web2": {"version": "1"}}}
a.config_entries = lambda key, inst: [
    {"key": k_, "value": v, "secret": True, "label": k_, "default": "",
     "generate": KINDS.get(k_, "")} for k_, v in STORE[key[2:]].items()]


def _apply(key, inst, values):
    APPLIED.append((key, dict(values)))
    STORE[key[2:]].update(values)
    return "PUSH_KEY"


a.apply_config = _apply
a.audit_deploy = lambda entry: AUDITED.append(entry)
d = a._BuildDrivers("k")
d._instance_key = lambda name: {"web": "k-web", "web2": "k-web2"}.get(name, "")

step = {"name": "web", "key": "PUSH_KEY", "generate": True}
v, note = d.check("app.config", step, {})
ok("leer: noch zu tun", v == tb.ABSENT, (v, note))
res = d.do("app.config", step, {})
made = STORE["web"]["PUSH_KEY"]
ok("G1: erzeugt einen gueltigen P-256-Schluessel (VAPID)",
   res[0] and gen_values.valid("vapid", made), (res, made))
ok("G1: der Wert steht in keinem Hinweis und keinem Protokoll",
   made not in res[1] and made not in json.dumps(AUDITED) and "vapid" in res[1],
   (res[1], AUDITED))
v, note = d.check("app.config", step, {})
ok("G2: danach gilt er als erledigt", v == tb.PRESENT and made not in note, note)
res = d.do("app.config", step, {})
ok("G2: ein zweiter Lauf ersetzt ihn nie",
   res[0] and STORE["web"]["PUSH_KEY"] == made and len(APPLIED) == 1, res)
STORE["web2"]["PUSH_KEY"] = "von-hand"
v, _n = d.check("app.config", {"name": "web2", "key": "PUSH_KEY", "generate": True}, {})
ok("G3: ein von Hand gesetzter Wert bleibt", v == tb.PRESENT
   and STORE["web2"]["PUSH_KEY"] == "von-hand")
v, note = d.check("app.config", {"name": "web", "key": "PLAIN", "generate": True}, {})
res = d.do("app.config", {"name": "web", "key": "PLAIN", "generate": True}, {})
ok("G4: ohne generate im Manifest: Konflikt, nichts gesetzt",
   v == tb.CONFLICT and not res[0] and STORE["web"]["PLAIN"] == "", (v, note, res))
STORE["web2"]["PUSH_KEY"] = ""
d.do("app.config", {"name": "web2", "key": "PUSH_KEY", "generate": True}, {})
ok("G6: zwei Instanzen, zwei verschiedene Schluessel",
   STORE["web2"]["PUSH_KEY"] and STORE["web2"]["PUSH_KEY"] != made)

print("")
print("Der Erzeuger selbst")
keys = {gen_values.generate("vapid") for _ in range(200)}
ok("200 VAPID-Schluessel, alle verschieden und gueltig, 43 Zeichen ohne '='",
   len(keys) == 200 and all(gen_values.valid("vapid", x) and len(x) == 43
                            and "=" not in x for x in keys))
ok("eine unbekannte Art ist ein Fehler, kein stiller Token",
   gen_values.kind_of("rsa") == "")
try:
    gen_values.generate("rsa")
    ok("generate('rsa') wirft", False)
except ValueError:
    ok("generate('rsa') wirft", True)

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
