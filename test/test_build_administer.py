#!/usr/bin/env python3
"""Rollen & Rechte im Aufbau-Assistenten (Jörg, 06.11.).

oaap.core.authorization 2.10: eine App, die die Rechte ihres Mandanten
verwaltet, bekommt ihren Schlüssel nur mit der Zusage des Betreibers. Im
Aufbau ist diese Zusage ein Haken: derselbe bool-Parameter, der den Schritt
einschaltet (`when`), steht in `confirm_administer`. Geprüft wird:

    Profil
    - `confirm_administer: "{p}"` mit p == when: angenommen
    - ein festes "true", ein anderer Parameter, ohne `when`: abgelehnt
      (ein Profil sagt nie im Voraus zu)
    Knoten (der Treiber des Aufbaus)
    - Haken gesetzt, server_admin: die Installation bekommt die Zusage
    - Haken nicht gesetzt (oder ein anderer als server_admin/root): keine
      Zusage; die Meldung ohne Marke, mit dem Hinweis auf den Haken
    - der Antrag eines Interessenten: der Haken ist immer "nein"
    Portal
    - das Formular erklärt den Haken; die Schritt-Liste sagt „verwaltet Rechte“
    - das Antragsformular zeigt ihn nicht und schickt ihn nie als "ja"
    - die Vorlage zum Herunterladen hat den Schritt und ist gültig

Aufruf (im Portal-Abbild): python3 test/test_build_administer.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-build-administer-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import appctl as a  # noqa: E402
import tenant_build as tb  # noqa: E402
import tenant_request as tr  # noqa: E402
import build_view as bv  # noqa: E402

a.reload_gateway = lambda: None
a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def profile(step_extra=None, params_extra=None):
    step = {"id": "rights", "type": "app.install", "app": "rollen-rechte",
            "name": "rechte", "when": "with_rights",
            "confirm_administer": "{with_rights}"}
    step.update(step_extra or {})
    step = {k: v for k, v in step.items() if v is not None}
    params = {"label": {"kind": "label", "required": True},
              "with_rights": {"kind": "bool", "default": False,
                              "label": "Mit Rollen & Rechte"},
              "other": {"kind": "bool", "default": True}}
    params.update(params_extra or {})
    return {"profile": tb.PROFILE_FORMAT, "id": "club", "title": "Club",
            "params": params,
            "steps": [{"id": "tenant", "type": "tenant.create", "label": "{label}"},
                      step]}


print("=== das Profil ===")
ok("confirm_administer = derselbe Haken wie when: angenommen",
   tb.profile_problems(profile()) == [], tb.profile_problems(profile()))
for label, extra in (("ein festes 'true'", {"confirm_administer": "true"}),
                     ("ein anderer Parameter", {"confirm_administer": "{other}"}),
                     ("ohne when", {"when": None})):
    pr = tb.profile_problems(profile(extra))
    ok(f"abgelehnt: {label}", any("confirm_administer" in p for p in pr), pr)
ok("administer_params nennt den Haken", tb.administer_params(profile()) == {"with_rights"})
vals, _p = tb.param_values(profile(), {"label": "kunde", "with_rights": "true"})
args = tb.plan(profile(), vals)[1][2]
ok("gerendert: der Haken wird zu 'true'", args.get("confirm_administer") == "true", args)
vals, _p = tb.param_values(profile(), {"label": "kunde"})
ok("ohne Haken läuft der Schritt gar nicht",
   [s[0] for s in tb.plan(profile(), vals)] == ["tenant"])

print("\n=== der Knoten ===")
SEEN = []
a._store_lookup = lambda app_id, source_id="", prefer="": (
    {"kind": "git", "url": "https://example.invalid/oaap-apps",
     "path": f"apps/{app_id}"}, "0.2.1",
    {"id": "oaap.platform", "name": "Plattform", "trust": "platform"})


def fake_install(ns):
    SEEN.append(bool(getattr(ns, "confirm_administer", False)))
    why = a.authz_admin_refusal("rollen-rechte", True, False,
                                getattr(ns, "confirm_administer", False))
    if why:
        print(why)
        a.die(why)


a.cmd_install = fake_install
step = {"app": "rollen-rechte", "name": "kunde-rechte",
        "confirm_administer": "true"}
d = a._BuildDrivers("kunde", who="betreiber", role="server_admin")
d._instance_key = lambda name: name
okd, note, made = d._install_from_catalogue(step)
ok("server_admin + Haken: installiert, mit Zusage", okd and SEEN[-1] is True, (okd, note))
okd, note, made = d._install_from_catalogue(dict(step, confirm_administer="false"))
ok("ohne Haken: keine Zusage, abgelehnt", not okd and SEEN[-1] is False, note)
ok("... die Meldung ohne Marke, mit Hinweis auf den Haken",
   a.ADMINISTER_MARK not in note and "tick" in note, note)
d2 = a._BuildDrivers("kunde", who="x", role="tenant_admin")
d2._instance_key = lambda name: name
okd, note, made = d2._install_from_catalogue(step)
ok("ein anderer als server_admin/root: der Haken zählt nicht",
   not okd and SEEN[-1] is False, note)

a.ensure_default_tenant()
os.makedirs(a.PROFILE_DIR, exist_ok=True)
with open(os.path.join(a.PROFILE_DIR, "club.json"), "w", encoding="utf-8") as f:
    json.dump(profile(), f)
now = datetime.now(timezone.utc)
token = tr.new_token()
tr.create_invite(a.REQUEST_DIR, token, "club", "", 14, "betreiber", now)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    msg, rid = a.tenant_request_submit(
        {"token": token, "params": {"label": "antrag", "with_rights": "true"},
         "contact": "vorstand@verein.example"}, now)
got = tr.get(a.REQUEST_DIR, rid) or {}
ok("der Antrag eines Interessenten: der Haken ist immer 'nein'",
   (got.get("params") or {}).get("with_rights") == "false", got.get("params"))

print("\n=== das Portal ===")
row = {"id": "club", "title": "Club", "problem": "",
       "params": profile()["params"],
       "steps": [{"id": s["id"], "type": s["type"], "when": s.get("when", "")}
                 for s in profile()["steps"]],
       "doc": profile()}
f = {x["name"]: x for x in bv.fields(row)}
ok("das Feld kennt seine Rechte-App", f["with_rights"]["administer"] == "rollen-rechte"
   and f["other"]["administer"] == "", f)
ok("das Antragsformular zeigt den Haken nicht",
   "with_rights" not in [x["name"] for x in bv.public_fields(row)])
params, contact, errors = bv.public_params(
    row, {"label": "antrag", "with_rights": "1", "contact": "a@b.example"})
ok("... und schickt ihn nie als 'ja'", params.get("with_rights") == "false", params)
ok("die Schritt-Liste markiert den Rechte-Schritt",
   [s["administer"] for s in bv.plan(row)] == [False, True])
tmpl = json.loads(bv.template_text())
ok("die Vorlage hat den Schritt und ist gültig",
   tb.profile_problems(tmpl) == [] and tb.administer_params(tmpl) == {"with_rights"},
   tb.profile_problems(tmpl))

import app as portal  # noqa: E402
VIEW = {"profiles": [row], "builds": []}
portal._build_view = lambda: VIEW
portal._request_view = lambda: {}
portal.caller_roles = lambda: {"server_admin"}
portal.caller_scope = lambda: ("server_admin", "")
portal.host_tenant_scope = lambda host: (None, True)
c = portal.app.test_client()
t = c.get("/aufbau/neu?profil=club",
          headers={"X-OAAP-User": "b", "X-OAAP-Roles": "server_admin"}).get_data(as_text=True)
ok("das Formular erklärt den Haken als Zusage",
   'name="with_rights"' in t and "deine Zusage als Server-Admin" in t
   and "kann nie eine Plattform-Rolle" in t, t[-2500:])
ok("die Schritt-Liste sagt „verwaltet Rechte“", "verwaltet Rechte" in t)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
