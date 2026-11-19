#!/usr/bin/env python3
"""Destinations im Aufbau: der Schritt `destination.ensure` (RFC-0104 D1).

Der Befund (Mandantenlandschaft bec, 18.11.): Ein Aufbau installierte die
KI-Apps, aber die Hilfe bekam ihre Destination `docs-origin` und der Chat
seine `ki` erst von Hand. Jetzt legt ein Profil eine Destination an und
ordnet sie einer Instanz zu.

Die Saetze, die diese Datei verteidigt:

    Angelegt und zugeordnet wird ueber dieselben Funktionen wie in der CLI.
    Ein zweiter Lauf tut nichts (Weiter wiederholt nichts).
    Eine fremde Destination gleichen Namens, die anders aussieht, ist ein
    Konflikt und wird nicht angefasst; eine fremde Zuordnung auch nicht.
    Ein Profil traegt nie ein Geheimnis: eine Destination, die eins braucht,
    entsteht ohne, und `manual` mit done_when destination.secret wartet,
    bis ein Mensch es eintraegt -- ohne den Wert je zu nennen.
    Der Rueckbau loest die Zuordnung und entfernt nur, was der Aufbau anlegte.

Aufruf: python3 test/test_tenant_build_destination.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-build-dest-")
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


REG = {"instances": {
    "kit1-hilfe": {"tenant": "t-kit1", "version": "0.6.1", "app": "help"},
    "kit1-chat": {"tenant": "t-kit1", "version": "0.15.0", "app": "chat"},
}}
AUDIT = []
a.load_registry = lambda: REG
a.save_registry = lambda reg: None
a.resolve_tenant = lambda t: t
a.tenant_by_label = lambda label, include_former=False: (
    ("t-kit1", {"label": "kit1"}) if label == "kit1" else (None, None))
a.audit_tenant = lambda event, tid, **kw: AUDIT.append((event, tid, kw))
a.refresh_destinations = lambda reg=None: None
a.sync_destination_env = lambda reg, only=None: None
a.egress_destination_refusal = lambda inst, name, d: ""
a.dest_target_refusal = lambda host: ""
a.is_rehearsal = lambda inst: False
a.declared_dest_needs = lambda inst: {}

d = a._BuildDrivers("kit1", who="anna", role="server_admin")
names = {"hilfe": "kit1-hilfe", "chat": "kit1-chat"}
d._instance_key = lambda name: names.get(name, "")
CTX = {"started": "2000-01-01T00:00:00+00:00"}

print("Eine Destination ohne Geheimnis, angelegt und zugeordnet")
docs = {"name": "docs-origin", "instance": "hilfe",
        "target": "https://codeload.github.com/MDJoerg/oaap-docs", "auth": "none"}
v, note = d.check("destination.ensure", docs, CTX)
ok("vorher: fehlt", v == tb.ABSENT, (v, note))
okd, note, made = d.do("destination.ensure", docs, CTX)
ok("legt an und ordnet zu", okd and made == ["destination:docs-origin",
                                             "binding:kit1-hilfe:docs-origin"], (okd, note, made))
rec = a.load_destinations()["t-kit1"]["docs-origin"]
ok("dieselbe Datei wie die CLI: Ziel, Anmeldung keine, wer",
   rec["target"] == {"direct": docs["target"]} and rec["auth"]["type"] == "none"
   and rec["created_by"] == "anna", rec)
ok("die Instanz hat docs-origin als Bedarf",
   REG["instances"]["kit1-hilfe"]["destinations"] == {"docs-origin": "docs-origin"},
   REG["instances"]["kit1-hilfe"])
ok("das Mandantenprotokoll nennt Anlegen und Zuordnen mit der Person des Aufbaus",
   [e[0] for e in AUDIT[-2:]] == ["destination.add", "destination.bind"]
   and all(e[2]["who"] == "anna" for e in AUDIT[-2:]), AUDIT[-2:])
v, note = d.check("destination.ensure", docs, CTX)
ok("danach: vorhanden", v == tb.PRESENT, (v, note))
okd, note, made = d.do("destination.ensure", docs, CTX)
ok("ein zweiter Lauf legt nichts neu an", okd and made == [], (okd, note, made))

print("")
print("Ein Geheimnis kommt nie aus dem Profil")
ki = {"name": "ki", "instance": "chat", "target": "https://modelle.example.org/v1",
      "auth": "bearer"}
okd, note, made = d.do("destination.ensure", ki, CTX)
rec = a.load_destinations()["t-kit1"]["ki"]
ok("bearer ohne Geheimnis wird angelegt, als 'Geheimnis fehlt'",
   okd and rec["secret"] is True and not (a.load_dest_secrets().get("t-kit1") or {}).get("ki"),
   (okd, note, rec))
ok("der Hinweis sagt, dass ein Mensch es eintraegt", "Mensch" in note, note)
line = a._dest_line("t-kit1", "ki", rec, a.load_dest_secrets(), REG)
ok("die Liste der Destinations sagt SECRET MISSING", "SECRET MISSING" in line, line)
wait = {"text": "Geheimnis eintragen", "done_when": "destination.secret",
        "destination": "ki", "instance": "chat"}
v, note = d.check("manual", wait, CTX)
ok("manual destination.secret wartet", v == tb.ABSENT and "fehlt" in note, (v, note))
a.destination_set_secret = a.destination_set_secret  # the CLI's own function
a.egress_reread_destination = lambda tid, name, **kw: {}
a.destination_set_secret("t-kit1", "ki", "sk-geheim-4711", who="joerg", role="tenant_admin")
v, note = d.check("manual", wait, CTX)
ok("nach dem Eintragen: erledigt", v == tb.PRESENT, (v, note))
ok("ohne den Wert zu nennen", "sk-geheim-4711" not in note, note)
v, note = d.check("manual", dict(wait, destination="docs-origin"), CTX)
ok("eine Destination ohne Geheimnis braucht keins", v == tb.PRESENT, (v, note))

print("")
print("Was nicht angefasst wird")
a.destination_add("t-kit1", "fremd", "http", "https://alt.example.org", who="bert")
v, note = d.check("destination.ensure", {"name": "fremd", "instance": "chat",
                                         "target": "https://neu.example.org"}, CTX)
ok("eine fremde Destination mit anderem Ziel: Konflikt", v == tb.CONFLICT and "Ziel" in note,
   (v, note))
v, note = d.check("destination.ensure", {"name": "fremd", "instance": "chat",
                                         "target": "https://alt.example.org", "auth": "bearer"}, CTX)
ok("... mit anderer Anmeldung: Konflikt", v == tb.CONFLICT and "Anmeldung" in note, (v, note))
REG["instances"]["kit1-chat"]["destinations"]["andere"] = "fremd"
v, note = d.check("destination.ensure", {"name": "ki", "instance": "chat", "need": "andere",
                                         "target": ki["target"], "auth": "bearer"}, CTX)
ok("ein schon anders belegter Bedarf: Konflikt, nicht ersetzt", v == tb.CONFLICT, (v, note))
okd, note, made = d.do("destination.ensure", dict(docs, instance="gibtsnicht"), CTX)
ok("ohne Instanz: gescheitert, mit Hinweis auf app.install",
   okd is False and "app.install" in note, (okd, note))

print("")
print("Nach einer Unterbrechung")
got = d.claim("destination.ensure", docs, CTX)
ok("was nach dem Start entstand, gehoert dem Aufbau",
   got == ["destination:docs-origin", "binding:kit1-hilfe:docs-origin"], got)
got = d.claim("destination.ensure", docs, {"started": "2999-01-01T00:00:00+00:00"})
ok("was vorher schon da war, nicht", got == [], got)

print("")
print("Rueckbau")
okd, note = d.undo("destination.ensure", docs, ["binding:kit1-hilfe:docs-origin"], CTX)
ok("loest die Zuordnung", okd and "docs-origin" not in
   (REG["instances"]["kit1-hilfe"].get("destinations") or {}), (okd, note))
okd, note = d.undo("destination.ensure", docs, ["destination:docs-origin"], CTX)
ok("entfernt die Destination, die der Aufbau anlegte",
   okd and "docs-origin" not in a.load_destinations()["t-kit1"], (okd, note))
okd, note = d.undo("destination.ensure", docs, ["destination:docs-origin"], CTX)
ok("ein zweiter Rueckbau: schon entfernt", okd and "schon" in note, (okd, note))

print("")
print("Im Profil")
prof = {"profile": tb.PROFILE_FORMAT, "id": "probe", "title": "t",
        "params": {"label": {"kind": "label", "required": True}},
        "steps": [{"id": "t", "type": "tenant.create", "label": "{label}"},
                  {"id": "h", "type": "app.install", "app": "help", "name": "{label}-hilfe"},
                  {"id": "d", "type": "destination.ensure", "name": "docs-origin",
                   "instance": "{label}-hilfe", "target": "https://codeload.github.com/x"},
                  {"id": "k", "type": "destination.ensure", "name": "ki",
                   "instance": "{label}-chat", "target": "https://x/v1", "auth": "bearer"},
                  {"id": "w", "type": "manual", "text": "Geheimnis von ki eintragen",
                   "done_when": "destination.secret", "destination": "ki",
                   "instance": "{label}-chat"}]}
ok("ein Profil mit destination.ensure und destination.secret ist gueltig",
   tb.profile_problems(prof) == [], tb.profile_problems(prof))


def refused(step, words):
    p = tb.profile_problems(dict(prof, steps=prof["steps"][:2] + [step]))
    return any(words in x for x in p), p


r = refused({"id": "x", "type": "destination.ensure", "name": "ki", "instance": "c",
             "target": "https://x", "auth": "bearer", "secret": "sk-1"}, "never holds a secret")
ok("ein Geheimnis im Profil wird abgelehnt", *r)
r = refused({"id": "x", "type": "destination.ensure", "name": "sap", "instance": "c",
             "target": "https://x", "auth": "principal_propagation"}, "auth is one of")
ok("Durchreichen der Person ist keine Anmeldung fuer ein Profil", *r)
r = refused({"id": "x", "type": "destination.ensure", "name": "b", "instance": "c",
             "target": "https://x", "auth": "basic"}, "needs 'user'")
ok("basic ohne Benutzer wird abgelehnt", *r)
r = refused({"id": "x", "type": "manual", "text": "t", "done_when": "destination.secret",
             "destination": "ki"}, "needs 'instance'")
ok("destination.secret ohne Instanz wird abgelehnt", *r)
r = refused({"id": "x", "type": "manual", "text": "t", "done_when": "confirmed",
             "destination": "ki"}, "belongs only to")
ok("'destination' an einem anderen done_when wird abgelehnt", *r)
r = tb.render_step(prof["steps"][2], {"label": "kit1"})
ok("der Parameter fuellt die Instanz", r["instance"] == "kit1-hilfe", r)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
