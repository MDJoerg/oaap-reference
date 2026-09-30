#!/usr/bin/env python3
"""Ein Auftrag im Spool muss einen Menschen nennen (Doku-Pruefung 2026-09-30, C2).

Der Worker auf dem Wirt (oaap-deployd, root) liest Rolle und Mandant des
Handelnden aus identitys eigenem Benutzerspeicher -- nie aus dem Auftrag.
Den NAMEN nimmt er aber aus dem Feld `by`, und viele Zweige pruefen
nach dem Muster `if actor and act_role != ...`. Ein Auftrag ganz ohne
`by` lief deshalb an all diesen Pruefungen vorbei, der
Sicherungs-Zeitplan eingeschlossen: Wer den Spool schreiben kann (ein
uebernommenes Portal), brauchte das Feld nur wegzulassen.

Jetzt gilt an EINER Stelle: Ohne Namen kommen nur die Auftraege durch,
die einen eigenen Beweis tragen (Deploy-Hook, Artefakt-Paar,
Erstlauf-Profil). Alles andere braucht einen Benutzer, den es gibt und
der aktiv ist.

Geprueft ueber den echten Worker; kein Docker, kein Knoten.
Der Test beisst: Er setzt die Ausnahme zum Schluss testweise auf
`backup-schedule` und erwartet, dass eine Pruefung faellt.

Aufruf: python3 test/test_spool_actor.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-spool-actor-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


# --- Attrappen -----------------------------------------------------------
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
SCHEDULE_CALLS = []


def fake_schedule(at="", keep=None, enabled=None):
    SCHEDULE_CALLS.append((at, keep, enabled))
    return "schedule set"


m.backup_schedule_set = fake_schedule
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


write_users([
    {"username": "root", "roles": ["server_admin"], "tenant": "",
     "groups": [], "active": True},
    {"username": "alt", "roles": ["server_admin"], "tenant": "",
     "groups": [], "active": False},
])

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(**req):
    """Einen Auftrag durch den echten Worker schicken, Ergebnis lesen."""
    _rid[0] += 1
    rid = f"r{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": "", **req}, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"),
              encoding="utf-8") as f:
        return json.load(f)


def refused_for_actor(res):
    return (not res.get("ok")) and "must be requested by" in res.get("message", "")


def run_cases():
    SCHEDULE_CALLS.clear()
    print("=== ohne Namen, mit falschem Namen ===")
    r = queue(action="backup-schedule", op="off")
    ok("Zeitplan ohne `by` wird abgelehnt", refused_for_actor(r), r)
    r = queue(action="backup-schedule", op="off", by="")
    ok("Zeitplan mit leerem `by` wird abgelehnt", refused_for_actor(r), r)
    r = queue(action="backup-schedule", op="off", by="?")
    ok("Zeitplan mit `?` (öffentliche Route) wird abgelehnt",
       not r.get("ok"), r)
    r = queue(action="backup-schedule", op="off", by="gibtsnicht")
    ok("Zeitplan mit unbekanntem Namen wird abgelehnt", refused_for_actor(r), r)
    r = queue(action="backup-schedule", op="off", by="alt")
    ok("Zeitplan eines deaktivierten server_admin wird abgelehnt",
       refused_for_actor(r), r)
    ok("… und keiner davon hat den Zeitplan angefasst",
       SCHEDULE_CALLS == [], SCHEDULE_CALLS)
    r = queue(action="tile", instance="irgendwas", mode="off")
    ok("auch jede andere Sitzungs-Aktion (tile) ohne `by` wird abgelehnt",
       refused_for_actor(r), r)

    print("=== der berechtigte Weg bleibt offen ===")
    r = queue(action="backup-schedule", op="off", by="root")
    ok("Zeitplan durch aktiven server_admin geht durch",
       r.get("ok") and SCHEDULE_CALLS == [("", None, False)], (r, SCHEDULE_CALLS))

    print("=== Auftraege mit eigenem Beweis brauchen keinen Namen ===")
    r = queue(instance="gibtsnicht")          # Deploy-Hook: keine action
    ok("Deploy-Hook ohne `by` wird nicht am Namen abgelehnt",
       not refused_for_actor(r) and "no deploy token" in r.get("message", ""), r)
    r = queue(action="node", profiles=["dev"], setup_token="falsch")
    ok("Erstlauf-Profil ohne `by` wird nicht am Namen abgelehnt",
       not refused_for_actor(r), r)
    r = queue(action="artifact", instance="gibtsnicht", digest="x")
    ok("Artefakt-Upload ohne `by` wird nicht am Namen abgelehnt",
       not refused_for_actor(r), r)


run_cases()

print("=== Mutation: beisst der Test? ===")
before = fails
saved = m.SPOOL_ACTIONS_WITHOUT_ACTOR
m.SPOOL_ACTIONS_WITHOUT_ACTOR = saved | {"backup-schedule"}
with contextlib.redirect_stdout(io.StringIO()):
    run_cases()
bit = fails > before
fails = before
m.SPOOL_ACTIONS_WITHOUT_ACTOR = saved
ok("mit `backup-schedule` als Ausnahme fallen Pruefungen", bit)

print()
print("OK" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
