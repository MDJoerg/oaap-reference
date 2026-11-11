#!/usr/bin/env python3
"""Konfiguration im Aufbau: der Schritt `app.config` (I-42, 0.1.250).

Der Befund (Aufbau von svl auf oaapx02, 09.11.): Ein Aufbau installierte
eine App nur mit den Vorgaben ihres Manifests; danach musste jemand in die
Instanzkonfiguration (Betriebsmodus `club`, Kontaktadresse,
Betreibername). Jetzt setzt ein Profil das selbst -- ein Schluessel je
Schritt, mit festem Wert oder `{param}`.

Die Saetze, die diese Datei verteidigt:

    Der Wert landet in der Instanz (derselbe Weg wie `oaap app config set`).
    Steht er schon so da, wird nichts neu gesetzt (Weiter wiederholt nichts).
    Ein Schluessel, den die App nicht erklaert, ist ein Konflikt, kein Raten.
    Der WERT steht nie in einem Hinweis -- er kann ein Geheimnis sein.
    Ein leerer optionaler Parameter laesst den Schritt aus.

Aufruf: python3 test/test_tenant_build_config.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-build-config-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
import tenant_build as tb                                     # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


STORE = {"MODE": "", "CONTACT": "", "SECRET_TOKEN": ""}
APPLIED, AUDITED = [], []
a.load_registry = lambda: {"instances": {"kunde-portal": {"version": "1.0"}}}
a.config_entries = lambda key, inst: [
    {"key": k, "value": v, "secret": k == "SECRET_TOKEN", "label": k, "default": ""}
    for k, v in STORE.items()]


def _apply(key, inst, values):
    APPLIED.append((key, dict(values)))
    changed = any(STORE.get(k) != v for k, v in values.items())
    STORE.update(values)
    return "MODE" if changed else "no change"


a.apply_config = _apply
a.audit_deploy = lambda entry: AUDITED.append(entry)

d = a._BuildDrivers("kunde")
d._instance_key = lambda name: "kunde-portal" if name == "portal" else ""

print("Der Schritt setzt einen Wert")
step = {"name": "portal", "key": "MODE", "value": "club"}
v, note = d.check("app.config", step, {})
ok("vorher: nicht wie gewuenscht", v == tb.ABSENT, (v, note))
res = d.do("app.config", step, {})
ok("setzt ueber apply_config, denselben Weg wie die CLI",
   res[0] is True and APPLIED[-1] == ("kunde-portal", {"MODE": "club"}), (res, APPLIED))
ok("und schreibt es ins Deploy-Protokoll, ohne den Wert",
   AUDITED and "club" not in str(AUDITED[-1]) and AUDITED[-1]["via"] == "build", AUDITED)
v, note = d.check("app.config", step, {})
ok("danach: wie gewuenscht", v == tb.PRESENT, (v, note))

print("")
print("Kein Wert in einem Hinweis")
secret = {"name": "portal", "key": "SECRET_TOKEN", "value": "geheim-123"}
_, note = d.check("app.config", secret, {})
res = d.do("app.config", secret, {})
_, note2 = d.check("app.config", secret, {})
ok("weder Pruefung noch Ausfuehrung nennen den Wert",
   "geheim-123" not in note + res[1] + note2, (note, res[1], note2))

print("")
print("Was abgelehnt wird")
v, note = d.check("app.config", {"name": "portal", "key": "GIBTSNICHT", "value": "x"}, {})
ok("ein Schluessel, den die App nicht erklaert: Konflikt", v == tb.CONFLICT, (v, note))
res = d.do("app.config", {"name": "fehlt", "key": "MODE", "value": "x"}, {})
ok("ohne Instanz: gescheitert, mit Hinweis auf app.install",
   res[0] is False and "app.install" in res[1], res)
v, note = d.check("app.config", {"name": "portal", "key": "CONTACT"}, {})
ok("ohne Wert (leerer optionaler Parameter): nichts zu tun", v == tb.PRESENT, (v, note))

print("")
print("Im Profil")
prof = {"profile": tb.PROFILE_FORMAT, "id": "probe", "title": "t",
        "params": {"label": {"kind": "label", "required": True},
                   "kontakt": {"kind": "text", "label": "Kontakt"}},
        "steps": [{"id": "t", "type": "tenant.create", "label": "{label}"},
                  {"id": "web", "type": "app.install", "app": "portal", "name": "portal"},
                  {"id": "modus", "type": "app.config", "name": "portal",
                   "key": "MODE", "value": "club"},
                  {"id": "kontakt", "type": "app.config", "name": "portal",
                   "key": "CONTACT", "value": "{kontakt}"}]}
ok("ein Profil mit app.config ist gueltig", tb.profile_problems(prof) == [],
   tb.profile_problems(prof))
bad = dict(prof, steps=prof["steps"][:2] + [{"id": "x", "type": "app.config",
                                              "name": "portal", "key": "MODE"}])
ok("ohne 'value' wird es abgelehnt", any("needs 'value'" in p for p in tb.profile_problems(bad)),
   tb.profile_problems(bad))
r = tb.render_step(prof["steps"][3], {"label": "k", "kontakt": None})
ok("ein leerer Parameter laesst 'value' weg", "value" not in r, r)
r = tb.render_step(prof["steps"][3], {"label": "k", "kontakt": "info@verein.de"})
ok("ein gesetzter fuellt ihn", r.get("value") == "info@verein.de", r)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
