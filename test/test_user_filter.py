#!/usr/bin/env python3
"""Filter in der Benutzerliste (I-55): Suche, Mandant, Status, Rolle, Art.

    Eine Regel     user_filter.matches fuer Portal und `oaap user list`
    Verengen       der Filter nimmt nur weg, er fuegt nie hinzu
    Tippfehler     ein unbekannter Wert im Link zeigt alles, nicht nichts
    Blaettern      ab 100 Eintraegen Seiten
    Befund         `oaap user list --tenant` uebergab --tenant still und
                   listete den ganzen Knoten (oaapx01, 17.11.)

Aufruf: python3 test/test_user_filter.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-userfilter-test-")
os.environ["OAAP_DATA_DIR"] = DATA
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
sys.path.insert(0, SERVICES)
sys.path.insert(0, PLATFORM)

import user_filter as uf                                       # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def user(name, **kw):
    return dict({"username": name, "display_name": name.title(),
                 "email": "", "roles": ["user"], "groups": [], "active": True,
                 "tenant": "t-a", "kind": "human", "deactivate_at": "",
                 "delete_at": "", "must_change_password": False}, **kw)


U = [user("anna", email="anna@beispiel.de", roles=["admin"]),
     user("bert", tenant="t-b", active=False),
     user("carla", roles=[], tenant="t-b"),
     user("kurs-tn-01", deactivate_at="2026-12-01T00:00:00Z",
          must_change_password=True),
     user("terminal-halle", kind="machine", roles=["guest"])]
LABEL = {"t-a": "verein", "t-b": "kunde"}


def run(**args):
    f = uf.normalise(args, tenants=set(LABEL.values()))
    return [u["username"] for u in U
            if uf.matches(u, f, tenant_of=lambda u: LABEL[u["tenant"]])]


print("Die Regel")
ok("ohne Filter alle", run() == [u["username"] for u in U])
ok("Suche im Benutzernamen", run(q="kurs") == ["kurs-tn-01"])
ok("Suche im Anzeigenamen, ohne Gross/Klein", run(q="CARLA") == ["carla"])
ok("Suche in der E-Mail", run(q="beispiel.de") == ["anna"])
ok("Mandant", run(tenant="kunde") == ["bert", "carla"])
ok("aktiv", "bert" not in run(state="active"))
ok("deaktiviert", run(state="inactive") == ["bert"])
ok("mit Ablaufdatum", run(state="scheduled") == ["kurs-tn-01"])
ok("ohne Rolle (Eingang)", run(state="inbox") == ["carla"])
ok("muss Passwort aendern", run(state="password") == ["kurs-tn-01"])
ok("Rolle", run(role="admin") == ["anna"])
ok("Art Maschine", run(kind="machine") == ["terminal-halle"])
ok("Filter kombinieren", run(tenant="kunde", state="inbox") == ["carla"])
ok("ein unbekannter Status zeigt alles statt nichts",
   len(run(state="gibtsnicht")) == len(U))
ok("ein unbekannter Mandant zeigt alles statt nichts",
   len(run(tenant="gibtsnicht")) == len(U))
ok("eine deaktivierte Person mit Datum zaehlt nicht als 'mit Ablaufdatum'",
   "scheduled" not in uf.state_of(user("x", active=False,
                                       deactivate_at="2026-01-01")))

print("\nBlaettern")
rows = list(range(250))
shown, p, pages = uf.page_of(rows, 3)
ok("drei Seiten, die dritte hat 50", pages == 3 and p == 3 and len(shown) == 50)
ok("eine Seite zu weit zeigt die letzte", uf.page_of(rows, 9)[1] == 3)
ok("Unsinn zeigt die erste", uf.page_of(rows, "x")[1] == 1)
ok("eine leere Liste hat eine Seite", uf.page_of([], 1) == ([], 1, 1))

print("\n`oaap user list` (Befund oaapx01: --tenant wurde uebergangen)")
import appctl as m                                             # noqa: E402

m.ensure_default_tenant()
tb, _t = m.tenant_create("kunde", name="Kunde")
REC = [user("anna", tenant=""), user("carla", tenant=tb, roles=[]),
       user("dora", tenant=tb)]
m._identity_exec = lambda code: json.dumps(REC)


def cli(**kw):
    args = types.SimpleNamespace(action="list", tenant="", search="",
                                 state="", role="", kind="")
    for k, v in kw.items():
        setattr(args, k, v)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.cmd_user(args)
    return buf.getvalue()


out = cli(tenant="kunde")
ok("--tenant kunde listet nur dessen Benutzer",
   "carla" in out and "dora" in out and "anna" not in out, out)
ok("... und sagt, wie viele es insgesamt sind", "2 of 3" in out, out)
ok("ohne --tenant der ganze Knoten", all(n in cli() for n in
                                         ("anna", "carla", "dora")))
ok("--state inbox", "carla" in cli(state="inbox")
   and "dora" not in cli(state="inbox"))
ok("--search", "dora" in cli(search="dor") and "anna" not in cli(search="dor"))
try:
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        cli(tenant="gibtsnicht")
    ok("ein unbekannter Mandant wird abgelehnt", False)
except SystemExit as e:
    ok("ein unbekannter Mandant wird abgelehnt", e.code)

print("\nDas Portal")
PORTAL = open(os.path.join(SERVICES, "portal", "app.py"),
              encoding="utf-8").read()
ok("das Portal benutzt dieselbe Regel", "user_filter.matches(" in PORTAL)
ok("der Mandantenfilter nur fuer server_admin auf Mehrmandanten-Knoten",
   'if not show_tenant:\n        f["tenant"] = ""' in PORTAL)
ok("Blaettern mit erhaltenen Filtern",
   "dict(query, page=pg+1)|urlencode" in PORTAL)
DOCKER = open(os.path.join(SERVICES, "portal", "Dockerfile"),
              encoding="utf-8").read()
ok("das Portal-Abbild bekommt das Modul", "COPY user_filter.py ." in DOCKER)

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
