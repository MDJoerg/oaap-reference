#!/usr/bin/env python3
"""`oaap authz setup` durch main() (oaap.core.authorization 0.4, 2.11).

Der Plan ist in test_authz_setup_plan.py geprueft. Hier geht es um die
REIHENFOLGE und um das, was NICHT geschieht -- durch denselben Weg wie an
der Maschine, mit dem echten Kern der Berechtigungen im Speicher statt des
Identity-Dienstes und einem Konnektor, der aufschreibt, was man ihn fragt:

    - --dry-run ruehrt weder den Realm noch die Berechtigungen an;
    - der Realm kommt ZUERST; scheitert er, ist auf der OAAP-Seite nichts
      geschrieben (KOEDER: sonst steht eine Abbildung auf eine Gruppe, die
      es nicht gibt, und niemand erfaehrt es);
    - ein Mandant ohne Anbieter, ohne Konnektor oder mit einem Konnektor an
      einer ANDEREN Adresse bekommt die Abbildungen trotzdem, mit dem Satz;
      der Konnektor wird nie auf den Namen im Anbieter-Objekt hin benutzt,
      sondern nur, wenn der Aussteller passt;
    - --no-groups macht keine Gruppen und sagt es;
    - ein zweiter Lauf: nichts zu tun, kein Aufruf an den Realm;
    - eine Zeile, die scheitert, haelt die anderen nicht auf, der Lauf endet
      mit Fehlercode, und ein Wiederholen macht nur den Rest;
    - jeder Lauf mit Wirkung schreibt EINEN Eintrag `authz.setup`.

Aufruf: python3 test/test_authz_setup_cli.py
"""
import contextlib
import copy
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-authz-setup-cli-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m                                             # noqa: E402
import authorization as az                                     # noqa: E402
import idp_admin                                               # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


TID, LABEL, APP = "t-verein", "verein", "hib"
DECL = {
    "objects": [
        {"key": "team", "title": "Mannschaft", "activities": ["coach"]},
        {"key": "venue", "title": "Halle", "activities": ["announce"]},
    ],
    "role_templates": [
        {"key": "trainer", "title": "Trainer/in",
         "suggested_group": "Verein/Trainer",
         "grants": [{"object": "team", "activities": ["coach"]}]},
        {"key": "announcer", "title": "Hallensprecher/in",
         "suggested_group": "Verein/Hallensprecher",
         "grants": [{"object": "venue", "activities": ["announce"]}]},
        {"key": "official", "title": "Betreuer/in",
         "grants": [{"object": "team", "activities": ["coach"]}]},
    ],
}

STATE = {}
LOG = {"audit": [], "realm": [], "routes": []}
FAIL_ROLE = set()


def reset():
    STATE.clear()
    STATE.update(az.empty_state())
    az.register(STATE, APP, "1.0.0", copy.deepcopy(DECL))
    LOG["audit"].clear()
    LOG["realm"].clear()
    LOG["routes"].clear()
    FAIL_ROLE.clear()


def fake_route(method, path, body=None):
    """Die Verwaltungsrouten des Identity-Dienstes, auf dem echten Kern."""
    LOG["routes"].append((method, path.split("?")[0]))
    route = path.split("?")[0]
    mine = lambda d: [dict(x) for x in d.values() if x["tenant"] == TID]  # noqa: E731
    if method == "GET":
        if route.endswith("/declarations"):
            return 200, {"declarations": [
                {"app": a, "version": d["version"],
                 "declaration": d["declaration"], "roles": 0}
                for a, d in STATE["declarations"].items()]}
        if route.endswith("/roles"):
            return 200, {"roles": mine(STATE["roles"])}
        if route.endswith("/collections"):
            return 200, {"collections": mine(STATE["collections"])}
        if route.endswith("/mappings"):
            names = {k: c["name"] for k, c in STATE["collections"].items()}
            return 200, {"mappings": [dict(x, collection_name=names.get(
                x["collection"], "")) for x in mine(STATE["mappings"])]}
    try:
        if route.endswith("/roles"):
            if body["name"] in FAIL_ROLE:
                return 400, {"error": "simulated failure for " + body["name"]}
            rid = az.create_role(STATE, TID, body["app"], body["template"],
                                 body["name"], body.get("values"), who="root")
            return 201, {"role": STATE["roles"][rid]}
        if route.endswith("/collections"):
            cid = az.create_collection(STATE, TID, body["name"], body["roles"],
                                       who="root")
            return 201, {"collection": STATE["collections"][cid]}
        if route.endswith("/mappings"):
            mid = az.add_mapping(STATE, TID, body["group"], body["collection"],
                                 who="root")
            return 201, {"mapping": STATE["mappings"][mid]}
    except az.AuthzError as e:
        return 400, {"error": str(e)}
    return 404, {"error": "nf"}


ISSUER = "http://kc.example:8113/realms/verein"
TENANT = {"label": LABEL, "name": "Verein",
          "idp": {"kind": "oidc", "issuer": ISSUER, "client_id": "oaap-verein",
                  "version": "26.7.4", "label": "", "connector": "auth",
                  "space": "verein", "version_how": "asserted",
                  "added": "2026-10-18"}}
CONNECTORS = {"auth": {"kind": "keycloak", "base_url": "http://kc.example:8113",
                       "auth": "client", "admin_id": "oaap-admin",
                       "admin_secret": "x" * 20}}


class FakeAdmin:
    outcome = (True, {"created": ["Verein"], "present": []}, "made")

    def __init__(self, kind, base_url, auth, admin_id, admin_secret, **kw):
        self.trace = []

    def ensure_groups(self, space, paths, accept_version=""):
        LOG["realm"].append((space, list(paths)))
        LOG["order"] = len(LOG["routes"])        # wie viele Routen VORHER liefen
        return type(self).outcome


tenant_now = [TENANT]
m.tenant_by_label = lambda label, include_former=True: (
    (TID, tenant_now[0]) if label == LABEL else (None, None))
m._authz_call = fake_route
m.audit_tenant = lambda action, tenant, subject="", **kw: LOG["audit"].append(
    (action, subject, kw.get("detail", "")))
m.load_idp_connectors = lambda: CONNECTORS
m.idp_admin.Admin = FakeAdmin
m.os.geteuid = lambda: 0


def cli(*argv):
    old = sys.argv
    sys.argv = ["oaap-app", "authz", "setup", "--tenant", LABEL] + list(argv)
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old
    return buf.getvalue(), code


def writes():
    return [r for r in LOG["routes"] if r[0] != "GET"]


def counts():
    return (len(STATE["roles"]), len(STATE["collections"]),
            len(STATE["mappings"]))


print("Probelauf")
reset()
out, code = cli("--app", APP, "--dry-run")
ok("--dry-run: Erfolg, nichts geschrieben, kein Aufruf an den Realm",
   code == 0 and writes() == [] and LOG["realm"] == [] and counts() == (0, 0, 0),
   (out, code, writes()))
ok("der Plan steht in der Ausgabe, mit Gruppe und Herkunft",
   "Verein/Trainer" in out and "manifest" in out and "Nothing was changed" in out,
   out)

print("Der Lauf")
reset()
out, code = cli("--app", APP)
ok("Erfolg", code == 0, (out, code))
ok("drei Rollen, drei Sammlungen, zwei Abbildungen (die dritte Vorlage hat keinen Pfad)",
   counts() == (3, 3, 2), counts())
ok("der Realm wurde EINMAL gefragt, mit beiden Pfaden, im richtigen Realm",
   LOG["realm"] == [("verein", ["Verein/Hallensprecher", "Verein/Trainer"])]
   or LOG["realm"] == [("verein", ["Verein/Trainer", "Verein/Hallensprecher"])],
   LOG["realm"])
first_write = next(i for i, r in enumerate(LOG["routes"]) if r[0] != "GET")
ok("KOEDER Reihenfolge: der Realm wurde gefragt, BEVOR die erste Route schrieb",
   LOG["order"] <= first_write, (LOG["order"], first_write))
ok("EIN Eintrag authz.setup, mit den Zahlen",
   [a for a, _s, _d in LOG["audit"]] == ["authz.setup"]
   and "3 Rolle(n)" in LOG["audit"][0][2] and "2 Abbildung(en)" in LOG["audit"][0][2],
   LOG["audit"])
ok("die Ausgabe nennt die Vorlage ohne Gruppe und wie man ihr eine gibt",
   "--group-for official" in out, out)

print("Zweiter Lauf")
n_realm, n_writes = len(LOG["realm"]), len(writes())
out, code = cli("--app", APP)
ok("nichts zu tun, kein Schreiben, KEIN Aufruf an den Realm",
   code == 0 and "Nothing to do" in out and len(writes()) == n_writes
   and len(LOG["realm"]) == n_realm, (out, len(writes()), n_writes))
ok("und kein weiterer Eintrag", len(LOG["audit"]) == 1, LOG["audit"])

print("Der Realm scheitert")
reset()
FakeAdmin.outcome = (False, {}, "the realm said no")
out, code = cli("--app", APP)
ok("KOEDER: Fehlercode, und auf der OAAP-Seite ist NICHTS geschrieben",
   code != 0 and counts() == (0, 0, 0) and writes() == [], (out, code, counts()))
ok("der Satz sagt, dass nichts geschrieben wurde",
   "NOTHING was written" in out, out)
ok("kein Eintrag authz.setup fuer einen Lauf ohne Wirkung", LOG["audit"] == [],
   LOG["audit"])
FakeAdmin.outcome = (True, {"created": ["Verein"], "present": []}, "made")

print("Kein Weg zum Realm")
reset()
tenant_now[0] = {"label": LABEL, "name": "Verein"}
out, code = cli("--app", APP)
ok("ohne Anbieter: Abbildungen trotzdem, der Realm wird nicht gefragt, der Satz sagt es",
   code == 0 and counts() == (3, 3, 2) and LOG["realm"] == []
   and "NOT made here" in out and "no provider" in out, (out, counts()))
reset()
t2 = copy.deepcopy(TENANT)
t2["idp"]["connector"] = ""
tenant_now[0] = t2
out, code = cli("--app", APP)
ok("Anbieter von Hand eingetragen (kein Konnektor): dasselbe",
   code == 0 and counts() == (3, 3, 2) and LOG["realm"] == []
   and "NOT made here" in out, out)
reset()
t3 = copy.deepcopy(TENANT)
t3["idp"]["issuer"] = "http://fremd.example/realms/verein"
tenant_now[0] = t3
out, code = cli("--app", APP)
ok("KOEDER: der Name im Anbieter-Objekt genuegt nicht -- der Konnektor liegt an "
   "einer anderen Adresse als der Aussteller: er wird NICHT benutzt",
   code == 0 and LOG["realm"] == [] and "somewhere else" in out, (out, LOG["realm"]))
tenant_now[0] = TENANT

print("--no-groups")
reset()
out, code = cli("--app", APP, "--no-groups")
ok("kein Aufruf an den Realm, Abbildungen ja, der Satz sagt es",
   code == 0 and LOG["realm"] == [] and counts() == (3, 3, 2)
   and "--no-groups" in out, out)

print("Auswahl und Pfad")
reset()
out, code = cli("--app", APP, "--only", "trainer")
ok("--only: eine Rolle, eine Sammlung, eine Abbildung",
   code == 0 and counts() == (1, 1, 1), counts())
out, code = cli("--app", APP, "--group-for", "official=Verein/Betreuer")
ok("--group-for: die zweite Runde macht den Rest, auch die Gruppe fuer 'official'",
   code == 0 and counts() == (3, 3, 3)
   and "Verein/Betreuer" in LOG["realm"][-1][1],
   (counts(), LOG["realm"]))
reset()
out, code = cli("--app", APP, "--only", "trainr")
ok("ein unbekannter Schluessel: Fehlercode, nichts geschrieben",
   code != 0 and writes() == [] and "trainr" in out, (out, code))
out, code = cli("--app", APP, "--group-for", "trainer")
ok("--group-for ohne Pfad: abgelehnt", code != 0 and writes() == [], out)
out, code = cli()
ok("ohne --app: abgelehnt", code != 0 and writes() == [], out)
out, code = cli("--app", "gibtsnicht")
ok("eine App ohne Erklaerung: abgelehnt, nichts geschrieben",
   code != 0 and writes() == [], out)

print("Eine Zeile scheitert")
reset()
FAIL_ROLE.add("Hallensprecher/in")
out, code = cli("--app", APP)
ok("Fehlercode, aber die anderen Zeilen sind gemacht",
   code != 0 and "FAILED  announcer" in out
   and {r["name"] for r in STATE["roles"].values()} == {"Trainer/in", "Betreuer/in"},
   (out, code))
ok("der Lauf hat trotzdem protokolliert, was er gemacht hat",
   [a for a, _s, _d in LOG["audit"]] == ["authz.setup"], LOG["audit"])
FAIL_ROLE.clear()
out, code = cli("--app", APP)
ok("ein Wiederholen macht nur den Rest",
   code == 0 and counts() == (3, 3, 2), (out, counts()))

print("Die Mandantengrenze")
reset()
cli("--app", APP)
ok("alles, was entstand, gehoert dem Mandanten",
   all(x["tenant"] == TID for d in (STATE["roles"], STATE["collections"],
                                    STATE["mappings"]) for x in d.values()))

print("\nALLE PRUEFUNGEN BESTANDEN" if not fails
      else f"\n{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
