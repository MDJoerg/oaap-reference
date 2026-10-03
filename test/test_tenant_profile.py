#!/usr/bin/env python3
"""Profile steuern Apps und kommen ueber das Portal auf den Knoten (RFC-0055 §14).

  * ein Parameter der Art `bool` und ein Schritt mit `when`: der Schritt laeuft
    nur bei gesetztem Haken, und ein Parameter, den nur dieser Schritt braucht,
    laesst den Aufbau ohne Haken nicht scheitern;
  * `app.install` nennt eine App des Katalogs (`app`) statt eines Pfads: die
    Auflösung geschieht auf dem Host, eine unbestaetigte Quelle wird nie aus
    einem Profil bedient, eine unbekannte App ist ein Fehlschlag mit Grund;
  * hochgeladene Profile: Worker-Aktion `tenant-profile` -- nur server_admin,
    das ganze Profil wird VOR dem Schreiben geprueft (JSON, Groesse, Format,
    Schrittarten, keine Pfade/Quellen, Apps im Katalog), Ersetzen und Loeschen,
    Loeschen nicht unter einem offenen Aufbau.

Der Worker ist echt (Mandantenspeicher, Spool, Benutzerspeicher im
Wegwerfverzeichnis); Katalog und Installation sind ersetzt.
Aufruf: python3 test/test_tenant_profile.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-profile-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as a                                             # noqa: E402
import tenant_build as tb                                      # noqa: E402
import build_view as bv                                        # noqa: E402
import management_api as mg                                    # noqa: E402
from flask import Flask                                        # noqa: E402

a.reload_gateway = lambda: None
a.zone_probe = lambda label: ""
a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def prof(**over):
    d = json.loads(json.dumps(bv.TEMPLATE))
    d["id"] = over.pop("id", "mitweb")
    d.update(over)
    return d


print("=== bool und when ===")
p = prof()
v, pr = tb.param_values(p, {"label": "abc", "title": "T"})
ok("ein bool ohne Angabe nimmt den Standard des Profils", v["with_web"] == "true" and not pr, (v, pr))
for given, want in (("false", "false"), ("0", "false"), ("", "false"), ("yes", "true"),
                    ("ON", "true"), ("1", "true")):
    v, pr = tb.param_values(p, {"label": "abc", "title": "T", "with_web": given})
    ok(f"bool '{given}' -> {want}", v["with_web"] == want and not pr, (v, pr))
v, pr = tb.param_values(p, {"label": "abc", "title": "T", "with_web": "vielleicht"})
ok("bool 'vielleicht' ist ein Fehler, kein stilles 'nein'", bool(pr), pr)
ok("das Profil der Vorlage besteht die Pruefung", tb.profile_problems(p) == [])
bad = prof()
bad["steps"][2]["when"] = "nope"
ok("'when' auf einen Parameter, den es nicht gibt, wird abgelehnt",
   any("'when'" in x for x in tb.profile_problems(bad)))
bad = prof()
bad["steps"][2]["when"] = "title"
ok("'when' auf einen Parameter ohne Art bool wird abgelehnt",
   any("'when'" in x for x in tb.profile_problems(bad)))
bad = prof()
bad["steps"][2]["source"] = "/x"
ok("app.install mit 'app' UND 'source' wird abgelehnt",
   any("exactly one" in x for x in tb.profile_problems(bad)))
bad = prof()
del bad["steps"][2]["app"]
ok("app.install mit keinem von beiden wird abgelehnt",
   any("exactly one" in x for x in tb.profile_problems(bad)))
on = [x[0] for x in tb.plan(p, {**v, "with_web": "true"})]
off = [x[0] for x in tb.plan(p, {**v, "with_web": "false"})]
ok("der Plan zeigt den Schritt nur mit Haken", "web" in on and "web" not in off, (on, off))

print("=== app.install aus dem Katalog ===")
default_id = a.ensure_default_tenant()
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "betreiber", "roles": ["server_admin"], "active": True},
               {"username": "kunde-verwalter", "roles": ["tenant_admin"],
                "active": True, "tenant": default_id},
               {"username": "mitglied", "roles": ["user"], "active": True}], f)
mg.SPOOL_DIR = a.SPOOL_DIR
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(a.SPOOL_DIR, d), exist_ok=True)
os.makedirs(a.PROFILE_DIR, exist_ok=True)

CAT = {"webseite": ("verified", "plattform"), "fremd": ("unverified", "fremdliste")}
LOOKUPS, INSTALLS = [], []


def fake_lookup(app_id, source_id="", prefer=""):
    LOOKUPS.append((app_id, source_id))
    if app_id not in CAT:
        return None, "", None
    trust, sid = CAT[app_id]
    return ({"kind": "git", "url": "https://git.example/" + app_id, "path": "pkg",
             "ref": "v1"}, "1.0", {"id": sid, "name": sid, "trust": trust})


def fake_install(ns):
    INSTALLS.append(ns)


a._store_lookup = fake_lookup
a.cmd_install = fake_install
a.fetch_store_list = lambda url: {"apps": [{"id": k, "package": {"git": "x"}} for k in CAT]}
a.load_sources = lambda: ([{"id": "plattform", "url": "u", "enabled": True,
                            "trust": "verified"}], None)
drv = a._BuildDrivers("vx")
good = {"name": "vx-web", "app": "webseite", "channel": "production"}
a._BuildDrivers._instance_key = lambda self, name: ""
r = drv._install_from_catalogue(good)
ns = INSTALLS[-1] if INSTALLS else None
ok("die App wird auf dem Host aufgeloest und mit Quelle, Pfad, Stand, Mandant und Katalogquelle installiert",
   ns and ns.package == "https://git.example/webseite" and ns.path == "pkg"
   and ns.ref == "v1" and ns.name == "vx-web" and ns.tenant == "vx"
   and ns.store_source == "plattform" and ns.channel == "production", (r, ns))
INSTALLS.clear()
r = drv._install_from_catalogue({**good, "app": "fremd"})
ok("eine Quelle, die eine Bestaetigung braucht, wird aus einem Profil nie bedient",
   r[0] is False and "unverified" in r[1] and not INSTALLS, r)
r = drv._install_from_catalogue({**good, "app": "gibtsnicht"})
ok("eine App, die kein Katalog kennt, scheitert mit Grund und installiert nichts",
   r[0] is False and "not listed" in r[1] and not INSTALLS, r)
r = drv._install_from_catalogue({**good, "source_id": "plattform"})
ok("eine bestimmte Katalogquelle laesst sich nennen", ("webseite", "plattform") in LOOKUPS)

print("=== die Tuer: Hochladen und Loeschen ===")
APP = Flask(__name__)
WHO = {"name": ""}


def queue(rid, name, payload, wait):
    with open(os.path.join(a.SPOOL_DIR, "queue", rid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": name, "by": WHO["name"], "requested": "now",
                   **payload}, f)


mg.init(APP, lambda: WHO["name"], lambda: set(), lambda: ("", None), lambda host: None, queue)


def work():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.cmd_process_deploys(None)


def result(rid):
    try:
        with open(os.path.join(a.SPOOL_DIR, "jobs", rid, "result.json"), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def ask(who, op, args):
    WHO["name"] = who
    rid = mg.enqueue("", "", op, args, action="tenant-profile")
    work()
    return result(rid)


def on_disk():
    return sorted(n for n in os.listdir(a.PROFILE_DIR) if n.endswith(".json"))


def put(text, who="betreiber"):
    return ask(who, "put", {"content": text})


text = json.dumps(prof(id="verein-neu"))
r = put(text)
ok("der Betreiber legt ein gueltiges Profil ab", r.get("ok") and on_disk() == ["verein-neu.json"], r)
ok("...byteweise so, wie es kam", open(os.path.join(a.PROFILE_DIR, "verein-neu.json"),
                                       encoding="utf-8").read() == text)
view = json.load(open(a.BUILD_VIEW, encoding="utf-8"))
row = [x for x in view["profiles"] if x["id"] == "verein-neu"][0]
ok("die Sichtdatei traegt das ganze Profil fuer den Download, mit 'when' in den Schritten",
   row["doc"]["id"] == "verein-neu" and any(s_["when"] == "with_web" for s_ in row["steps"]), row.keys())
ok("das mitgelieferte Beispiel der Vorlage laesst sich hochladen (mit bekannter App)",
   put(bv.template_text().replace('"verein-vorlage"', '"vorlage-test"')).get("ok"))
for who, label in (("kunde-verwalter", "ein Mandanten-Verwalter"), ("mitglied", "ein Mitglied"),
                   ("", "niemand"), ("erfunden", "ein Benutzer, den es nicht gibt")):
    before = on_disk()
    r = put(json.dumps(prof(id="fremd-" + str(len(before)))), who)
    ok(f"{label} kann kein Profil ablegen", not r.get("ok") and on_disk() == before, r)
    r = ask(who, "delete", {"id": "verein-neu"})
    ok(f"{label} kann kein Profil loeschen", not r.get("ok") and "verein-neu.json" in on_disk(), r)
before = on_disk()
for label, content in (
        ("kein JSON", "das ist {kein json"),
        ("kein Objekt", "[1, 2]"),
        ("falsches Format", json.dumps({**prof(id="x1"), "profile": "oaap.tenant-profile/9"})),
        ("unbekannte Schrittart", json.dumps({**prof(id="x2"), "steps": prof()["steps"] +
                                              [{"id": "z", "type": "shell.run"}]})),
        ("Pfad statt Katalog", json.dumps({**prof(id="x3"), "steps": [
            {"id": "t", "type": "tenant.create", "label": "{label}"},
            {"id": "w", "type": "app.install", "source": "/etc", "name": "w"}]})),
        ("Pfad neben dem Katalog", json.dumps({**prof(id="x4"), "steps": [
            {"id": "t", "type": "tenant.create", "label": "{label}"},
            {"id": "w", "type": "app.install", "app": "webseite", "path": "../..", "name": "w"}]})),
        ("App ausserhalb des Katalogs", json.dumps({**prof(id="x5"), "steps": [
            {"id": "t", "type": "tenant.create", "label": "{label}"},
            {"id": "w", "type": "app.install", "app": "boese", "name": "w"}]})),
        ("Name mit Pfad", json.dumps(prof(id="../x6"))),
        ("Name mit Grossbuchstaben", json.dumps(prof(id="Gross"))),
        ("zu gross", json.dumps({**prof(id="x7"), "description": "x" * 70000})),
        ("ohne Text", None)):
    r = ask("betreiber", "put", {"content": content} if content is not None else {})
    ok(f"abgelehnt, nichts geschrieben: {label}", not r.get("ok") and on_disk() == before, r)
ok("die Ablehnung nennt den Grund in einem Satz",
   "unknown step type" in put(json.dumps({**prof(id="x2"), "steps": prof()["steps"] +
                                          [{"id": "z", "type": "shell.run"}]})).get("message", ""))
ok("ausserhalb des Profilordners liegt nichts", not os.path.exists(os.path.join(DATA, "x6.json"))
   and not [n for n in os.listdir(DATA) if n.startswith("x")])
a.fetch_store_list = lambda url: None
r = put(json.dumps(prof(id="offline")))
ok("ist kein Katalog lesbar, wird ein Profil mit App abgelehnt statt ungeprueft abgelegt",
   not r.get("ok") and "catalogue" in r.get("message", "") and "offline.json" not in on_disk(), r)
r = put(json.dumps({**prof(id="ohneapp"), "steps": [prof()["steps"][0]]}))
ok("...ein Profil ganz ohne App braucht den Katalog nicht", r.get("ok"), r)
a.fetch_store_list = lambda url: {"apps": [{"id": k, "package": {"git": "x"}} for k in CAT]}

r = put(json.dumps({**prof(id="verein-neu"), "title": "Neuer Titel"}))
ok("gleiche id ersetzt das Profil und sagt es", r.get("ok") and "replaced" in r["message"]
   and json.load(open(os.path.join(a.PROFILE_DIR, "verein-neu.json")))["title"] == "Neuer Titel", r)

# delete under an open build
st = tb.new_state(prof(id="verein-neu"), "d" * 64, {"label": "vo", "title": "T"}, "betreiber")
tb.save_state(a.BUILD_DIR, st)
r = ask("betreiber", "delete", {"id": "verein-neu"})
ok("unter einem offenen Aufbau wird das Profil nicht geloescht",
   not r.get("ok") and "still uses" in r["message"] and "verein-neu.json" in on_disk(), r)
st["state"] = "done"
tb.save_state(a.BUILD_DIR, st)
r = ask("betreiber", "delete", {"id": "verein-neu"})
ok("nach dem Aufbau geht es", r.get("ok") and "verein-neu.json" not in on_disk(), r)
for bad_id in ("verein-neu", "../etc/passwd", "", "Gross"):
    r = ask("betreiber", "delete", {"id": bad_id})
    ok(f"loeschen von '{bad_id}': abgelehnt", not r.get("ok"), r)

print("=== ein echter Aufbau mit und ohne Haken ===")
put(json.dumps(prof(id="lauf")))
st = a.tenant_build_start("lauf", {"label": "vskip", "title": "Ohne Web", "with_web": "false"},
                          "betreiber", "server_admin")
by_id = {r["id"]: r for r in st["steps"]}
ok("ohne Haken wird der App-Schritt uebersprungen und der Aufbau laeuft bis zum Menschenschritt",
   by_id["web"]["state"] == "skipped" and st["state"] == "waiting"
   and by_id["tenant"]["state"] == "done", [(r["id"], r["state"]) for r in st["steps"]])
ok("...und eine App wurde nicht angefasst", not INSTALLS or INSTALLS[-1].tenant != "vskip")
INSTALLS.clear()
st = a.tenant_build_start("lauf", {"label": "vweb", "title": "Mit Web", "with_web": "true"},
                          "betreiber", "server_admin")
by_id = {r["id"]: r for r in st["steps"]}
ok("mit Haken erreicht der Aufbau den App-Schritt und ruft die Installation mit der Katalog-App auf",
   INSTALLS and INSTALLS[-1].package == "https://git.example/webseite"
   and INSTALLS[-1].tenant == "vweb" and INSTALLS[-1].name == "vweb-webseite",
   [(r["id"], r["state"], r["note"]) for r in st["steps"]])
ok("(die Attrappe legt keine Instanz an, darum meldet der Schritt, dass sein Ergebnis fehlt)",
   by_id["web"]["state"] == "failed" and "not on the node" in by_id["web"]["note"],
   by_id["web"])

print()
print("FAILURES" if fails else "ALL PASS", fails)
sys.exit(1 if fails else 0)
