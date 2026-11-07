#!/usr/bin/env python3
"""Freie App-Auswahl beim Start eines Aufbaus (RFC-0055 §15).

Drei Schichten, alle ohne Docker und ohne Knoten:

1. `services/tenant_build.py` mit einem erfundenen Knoten: die Parameterart
   `apps` (jede Regel mit Koeder), das Aufweiten eines `app.install` ueber
   die Auswahl in einen Schritt je App, Test und Produktiv, Fortsetzen und
   Rueckbau finden dieselben Schritte wieder.
2. Der echte `appctl.py`: `tenant_build_start` lehnt eine App, die kein
   Katalog kennt, VOR dem ersten Schritt ab (kein Mandant entsteht), und
   ohne lesbaren Katalog ebenso; das Antragsformular eines Interessenten
   kann den ganzen Katalog nicht waehlen, nur `choices`.
3. `portal/build_view.py`: Felder, Haken je App, Formularwerte, Plan.

Aufruf: python3 test/test_tenant_build_apps.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-build-apps-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import tenant_build as tb                                      # noqa: E402
import build_view as bv                                        # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def prof(params=None, steps=None, **over):
    doc = {
        "profile": tb.PROFILE_FORMAT, "id": "kunde", "title": "Kunde",
        "params": {"label": {"kind": "label", "required": True},
                   "apps": {"kind": "apps", "default": ["n8n"],
                            "label": "Apps aus dem Store"},
                   "with_test": {"kind": "bool", "default": True}},
        "steps": [
            {"id": "tenant", "type": "tenant.create", "label": "{label}"},
            {"id": "test", "type": "app.install", "app": "{apps}",
             "name": "{app}-test", "channel": "test", "when": "with_test"},
            {"id": "prod", "type": "app.install", "app": "{apps}",
             "name": "{app}", "channel": "production"},
            {"id": "backup", "type": "backup.check"},
        ],
    }
    if params is not None:
        doc["params"].update(params)
    if steps is not None:
        doc["steps"] = steps
    doc.update(over)
    return doc


def has(probs, word):
    return any(word in p for p in probs)


# ===================== 1. the pure half ==================================

print("=== die Parameterart apps wird ganz geprueft ===")
ok("ein gutes Profil mit Auswahl hat keine Probleme", tb.profile_problems(prof()) == [],
   tb.profile_problems(prof()))
d = prof()
d["steps"][2]["name"] = "prod"
ok("ein Schritt ueber die Auswahl ohne {app} im Namen wird abgelehnt (alle hiessen gleich)",
   has(tb.profile_problems(d), "needs '{app}'"))
d = prof()
d["steps"][2]["confirm_administer"] = "{with_test}"
d["steps"][2]["when"] = "with_test"
ok("eine Rechte-App kommt nie aus einer Liste (confirm_administer verboten)",
   has(tb.profile_problems(d), "not chosen from a list"))
d = prof(params={"app": {"kind": "text"}})
ok("ein Parameter darf nicht 'app' heissen", has(tb.profile_problems(d), "'app' is not allowed"))
d = prof()
d["steps"][0]["title"] = "{apps}"
ok("die Auswahl steht nur als ganzes 'app' eines app.install",
   has(tb.profile_problems(d), "is a choice of apps"))
d = prof()
d["steps"][1]["app"] = "x-{apps}"
ok("...auch nicht als Teil eines App-Namens", has(tb.profile_problems(d), "is a choice of apps"))
d = prof()
d["steps"][0]["title"] = "{app}"
ok("{app} ausserhalb eines Auswahlschritts ist kein Parameter",
   has(tb.profile_problems(d), "'{app}' is not a declared parameter"))
for spec, word, label in (
        ({"kind": "apps", "max": 0}, "max is 1", "max 0"),
        ({"kind": "apps", "max": 99}, "max is 1", "max zu gross"),
        ({"kind": "apps", "choices": []}, "choices is a list", "leere choices"),
        ({"kind": "apps", "choices": ["Gross Buchstaben"]}, "choices is a list", "choices mit Unsinn"),
        ({"kind": "apps", "default": ["a;b"]}, "default", "Standard mit Unsinn"),
        ({"kind": "apps", "choices": ["a"], "default": ["b"]}, "outside", "Standard ausserhalb der choices")):
    ok(f"Koeder: {label} wird abgelehnt", has(tb.profile_problems(prof(params={"apps": spec})), word),
       tb.profile_problems(prof(params={"apps": spec})))

print("=== Werte ===")
p = prof()
v, pr = tb.param_values(p, {"label": "kd", "apps": "forgejo, n8n,forgejo"})
ok("Komma-Text, Leerzeichen und Doppeltes: Reihenfolge bleibt, doppelt faellt weg",
   pr == [] and v["apps"] == "forgejo,n8n", (v, pr))
v, pr = tb.param_values(p, {"label": "kd"})
ok("ohne Angabe gilt der Standard des Profils", v.get("apps") == "n8n", v)
v, pr = tb.param_values(p, {"label": "kd", "apps": ""})
ok("leer gewaehlt ist keine App (nicht der Standard)", pr == [] and v["apps"] == "", (v, pr))
v, pr = tb.param_values(p, {"label": "kd", "apps": "n8n;rm -rf"})
ok("Koeder: keine App-ID wird abgelehnt", has(pr, "not an app id"), pr)
pc = prof(params={"apps": {"kind": "apps", "choices": ["n8n", "forgejo"]}})
v, pr = tb.param_values(pc, {"label": "kd", "apps": "n8n,vaultwarden"})
ok("Koeder: eine App ausserhalb der choices wird abgelehnt", has(pr, "does not offer"), pr)
pm = prof(params={"apps": {"kind": "apps", "max": 2}})
v, pr = tb.param_values(pm, {"label": "kd", "apps": "a,b,c"})
ok("Koeder: mehr als max wird abgelehnt", has(pr, "at most 2"), pr)
pr_ = prof(params={"apps": {"kind": "apps", "required": True}})
v, pr = tb.param_values(pr_, {"label": "kd", "apps": ""})
ok("Koeder: Pflichtauswahl leer wird abgelehnt", has(pr, "at least one"), pr)

print("=== das Aufweiten ===")
v, _ = tb.param_values(p, {"label": "kd", "apps": "forgejo,n8n", "with_test": "yes"})
plan = tb.plan(p, v)
ids = [s[0] for s in plan]
ok("je App ein Test- und ein Produktivschritt, in Profil- und Auswahlreihenfolge",
   ids == ["tenant", "test-forgejo", "test-n8n", "prod-forgejo", "prod-n8n", "backup"], ids)
args = dict((s[0], s[2]) for s in plan)
ok("app und Name sind eingesetzt, der Kanal bleibt",
   args["test-n8n"] == {"app": "n8n", "name": "n8n-test", "channel": "test"}
   and args["prod-forgejo"] == {"app": "forgejo", "name": "forgejo", "channel": "production"},
   args)
v2, _ = tb.param_values(p, {"label": "kd", "apps": "forgejo", "with_test": "no"})
ok("ohne Haken nur Produktiv", [s[0] for s in tb.plan(p, v2)] == ["tenant", "prod-forgejo", "backup"])
v3, _ = tb.param_values(p, {"label": "kd", "apps": ""})
ok("keine App gewaehlt: keine App-Schritte", [s[0] for s in tb.plan(p, v3)] == ["tenant", "backup"])
d = prof()
d["steps"].append({"id": "prod-n8n", "type": "backup.check"})
try:
    tb.plan(d, v)
    ok("eine Kollision der aufgeweiteten IDs wird vor dem Lauf abgelehnt", False)
except tb.Refusal as exc:
    ok("eine Kollision der aufgeweiteten IDs wird vor dem Lauf abgelehnt", "occurs twice" in str(exc))
ok("das Profil selbst bleibt unveraendert", p["steps"][1]["id"] == "test" and p["steps"][1]["app"] == "{apps}")


class FakeNode:
    def __init__(self):
        self.things, self.calls = set(), []
        self.fail = {}

    def check(self, typ, a, ctx):
        key = f"{typ}:{a.get('label', a.get('name', ''))}"
        return (tb.PRESENT, "there") if key in self.things else (tb.ABSENT, "no")

    def do(self, typ, a, ctx):
        sid = ctx["step"]["id"]
        self.calls.append(sid)
        if sid in self.fail:
            return False, self.fail.pop(sid), []
        key = f"{typ}:{a.get('label', a.get('name', ''))}"
        self.things.add(key)
        return True, "did", [key]

    def undo(self, typ, a, made, ctx):
        self.calls.append("undo " + made[0])
        self.things.discard(made[0])
        return True, "undone"


print("=== Lauf, Fortsetzen, Rueckbau ===")
root = tempfile.mkdtemp(prefix="oaap-build-apps-state-")
node = FakeNode()
node.fail["prod-n8n"] = "the image could not be pulled"
st = tb.new_state(p, "d", v, "probe")
ok("der Zustand traegt die aufgeweiteten Schritte",
   [r["id"] for r in st["steps"]] == ids, [r["id"] for r in st["steps"]])
st = tb.run(st, p, node, root)
ok("ein Fehler an der zweiten App haelt dort an", st["state"] == "failed"
   and [r["state"] for r in st["steps"]][4] == "failed", [r["state"] for r in st["steps"]])
st = tb.load_state(root, st["id"])
before = list(node.calls)
st = tb.run(st, p, node, root)
ok("Fortsetzen findet dieselben Schritte und macht nur den fehlenden",
   st["state"] == "done" and node.calls[len(before):] == ["prod-n8n", "backup"],
   node.calls[len(before):])
ok("auf dem Knoten: vier Instanzen + Mandant + Sicherung",
   {"app.install:n8n", "app.install:n8n-test", "app.install:forgejo",
    "app.install:forgejo-test", "tenant.create:kd"} <= node.things, node.things)
st = tb.rollback(st, p, node, root)
ok("Rueckbau baut jede Instanz einzeln zurueck, rueckwaerts",
   st["state"] == "rolled-back"
   and [c for c in node.calls if c.startswith("undo app.install")] == [
       "undo app.install:n8n", "undo app.install:forgejo", "undo app.install:n8n-test",
       "undo app.install:forgejo-test"], node.calls)

# ===================== 2. the host door ==================================

print("=== die Tuer auf dem Host ===")
import appctl as a                                             # noqa: E402

a.reload_gateway = lambda: None
a.zone_probe = lambda label: ""
os.makedirs(a.PROFILE_DIR, exist_ok=True)
with open(os.path.join(a.PROFILE_DIR, "kunde.json"), "w", encoding="utf-8") as f:
    json.dump(prof(), f)
KNOWN = {"ids": {"n8n", "forgejo"}}
a._catalogue_app_ids = lambda: KNOWN["ids"]
a.ensure_default_tenant()
try:
    a.tenant_build_start("kunde", {"label": "kd1", "apps": "n8n,gibtsnicht"}, "probe",
                         "server_admin", drivers=FakeNode())
    ok("eine App, die kein Katalog kennt, wird vor dem ersten Schritt abgelehnt", False)
except tb.Refusal as exc:
    ok("eine App, die kein Katalog kennt, wird vor dem ersten Schritt abgelehnt",
       "gibtsnicht" in str(exc), exc)
ok("...und es ist kein Mandant und kein Aufbau entstanden",
   not any(t.get("label") == "kd1" for t in a.load_tenants().values()))
ok("...kein Zustand liegt herum", tb.list_states(a.BUILD_DIR) == [])
KNOWN["ids"] = None
try:
    a.tenant_build_start("kunde", {"label": "kd1", "apps": "n8n"}, "probe",
                         "server_admin", drivers=FakeNode())
    ok("ohne lesbaren Katalog wird eine Auswahl nicht ungeprueft gestartet", False)
except tb.Refusal as exc:
    ok("ohne lesbaren Katalog wird eine Auswahl nicht ungeprueft gestartet",
       "cannot be checked" in str(exc), exc)
ok("eine leere Auswahl braucht keinen Katalog",
   a.tenant_build_apps_refusal(prof(), {"apps": ""}, catalogue=None) == "")
KNOWN["ids"] = {"n8n", "forgejo"}
fake = FakeNode()
st = a.tenant_build_start("kunde", {"label": "kd2", "apps": "forgejo", "with_test": "false"},
                          "probe", "server_admin", drivers=fake)
ok("eine bekannte App laeuft durch den echten Start",
   st["state"] == "done" and "prod-forgejo" in fake.calls and "test-forgejo" not in fake.calls,
   (st["state"], fake.calls))
try:
    a.tenant_build_start("kunde", {"label": "kd3", "apps": "n8n"}, "probe", "tenant_admin",
                         drivers=FakeNode())
    ok("Koeder: ein tenant_admin startet keinen Aufbau", False)
except tb.Refusal:
    ok("Koeder: ein tenant_admin startet keinen Aufbau", True)

print("=== der Interessent ===")
import tenant_request as tr                                    # noqa: E402

SUBMITTED = []
tr.find_by_token = lambda root, token, now: {"profile": "kunde"} if token == "t" else None
tr.submit = lambda root, token, values, contact, now: (
    SUBMITTED.append(values) or {"id": "req-1", "label": values["label"]})
a._request_label_check = lambda lb: ""
a.tenant_request_submit({"token": "t", "params": {"label": "ia", "apps": "vaultwarden,forgejo"},
                         "contact": "x@example.org"}, None)
ok("ohne choices waehlt der Interessent nicht: es gilt der Standard des Profils",
   SUBMITTED and SUBMITTED[-1]["apps"] == "n8n", SUBMITTED)
with open(os.path.join(a.PROFILE_DIR, "kunde.json"), "w", encoding="utf-8") as f:
    json.dump(prof(params={"apps": {"kind": "apps", "choices": ["n8n", "forgejo"],
                                    "default": []}}), f)
a.tenant_request_submit({"token": "t", "params": {"label": "ib", "apps": "forgejo"},
                         "contact": "x@example.org"}, None)
ok("mit choices waehlt er daraus", SUBMITTED[-1]["apps"] == "forgejo", SUBMITTED[-1])
try:
    a.tenant_request_submit({"token": "t", "params": {"label": "ic", "apps": "vaultwarden"},
                             "contact": "x@example.org"}, None)
    ok("Koeder: ausserhalb der choices wird der Antrag abgelehnt", False)
except tr.Refusal as exc:
    ok("Koeder: ausserhalb der choices wird der Antrag abgelehnt", "does not offer" in str(exc), exc)

# ===================== 3. the portal's form ==============================

print("=== das Formular im Portal ===")
view = {"id": "kunde", "params": prof()["params"], "doc": prof()}
fs = bv.fields(view)
fa = [f for f in fs if f["kind"] == "apps"][0]
ok("das Feld kennt Standard (als Liste) und choices (None = Katalog)",
   fa["default"] == ["n8n"] and fa["choices"] is None, fa)
CATALOGUE = [{"id": "forgejo", "name": "Forgejo", "summary": "Git", "status": "stable",
              "profile_fit": True},
             {"id": "n8n", "name": "n8n", "summary": "Abläufe", "status": "stable",
              "profile_fit": True},
             {"id": "alt", "name": "Alt", "summary": "", "status": "archived", "profile_fit": True},
             {"id": "dev", "name": "Dev", "summary": "", "status": "stable", "profile_fit": False}]
off = bv.app_offer(fa, CATALOGUE)
ok("ohne choices: der Katalog ohne Archiviertes und ohne fremdes Profil, Standard angehakt",
   [(o["id"], o["checked"]) for o in off] == [("forgejo", False), ("n8n", True)], off)
off = bv.app_offer(fa, CATALOGUE, chosen=["forgejo"])
ok("nach einem Fehler gilt das Angehakte, nicht der Standard",
   [(o["id"], o["checked"]) for o in off] == [("forgejo", True), ("n8n", False)], off)
fc = dict(fa, choices=["n8n", "weg"], default=[])
off = bv.app_offer(fc, CATALOGUE)
ok("mit choices genau diese, eine unbekannte als nicht im Katalog markiert",
   [(o["id"], o["known"]) for o in off] == [("n8n", True), ("weg", False)], off)


class Form(dict):
    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([v] if v else [])


params, missing = bv.start_params(view, Form(label="kd", apps=["n8n", "forgejo"]))
ok("zwei Haken werden ein Komma-Text", params["apps"] == "n8n,forgejo", params)
params, missing = bv.start_params(view, Form(label="kd"))
ok("kein Haken wird ausdruecklich leer gesendet (nicht der Standard)", params["apps"] == "", params)
pv = {"id": "kunde", "params": prof()["params"], "doc": prof()}
ok("das Formular des Interessenten zeigt eine Auswahl ohne choices nicht",
   not [f for f in bv.public_fields(pv) if f["kind"] == "apps"])
pp, contact, errs = bv.public_params(pv, Form(label="kd", apps=["vaultwarden"], contact="a@b.de"))
ok("...und schickt sie auch nicht mit", "apps" not in pp and not errs, (pp, errs))
plan_rows = bv.plan(view)
ok("der Plan nennt die Auswahlschritte in Klartext",
   any("Je gewählte App" in r["text"] and "(Test)" in r["text"] for r in plan_rows)
   and any("(Produktiv)" in r["text"] for r in plan_rows), plan_rows)

print(f"\n{'OK' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
