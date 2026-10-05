#!/usr/bin/env python3
"""`oaap authz assignments --user <name>` zeigt die Zuordnungen EINER Person
(oaap.core.authorization §2.6: GET /internal/authz/assignments?user=…).

Gemessen am 2026-10-05 auf oaap-test (0.1.193): mit `--user cap-editor`
erschien auch die Zuordnung von `cap-viewer`. Die Route filterte laengst
richtig -- die CLI haengte die Benutzer-ID nur nie an die Abfrage. Ein Test
der Route allein (test_authorization_identity.py) konnte das nicht sehen;
deshalb geht dieser durch die Tuer, an der der Fehler sass:

    echte Kommandozeile (main() mit argv, also auch der echte Parser)
      -> cmd_authz -> die echte Route im Testclient des Identity-Dienstes.

Ersetzt ist nur der Transport (`docker exec` in den Identity-Container):
`_authz_call` ruft hier denselben Testclient mit demselben internen
Schluessel direkt. Beide Seiten lesen dieselben Dateien.

Geprueft wird (ohne Docker):
    - ohne `--user`: alle Zuordnungen des Mandanten (Gegenprobe);
    - mit `--user`: nur die dieser Person, und die Abfrage traegt `user=<ID>`;
    - KOEDER: derselbe Benutzername in einem anderen Mandanten;
    - KOEDER: eine Benutzer-ID mit `&` darin (URL-Kodierung);
    - ein unbekannter Name, ein Name eines anderen Mandanten und ein leerer
      Name werden abgelehnt -- NICHT mit der ganzen Liste beantwortet;
    - eine Person ohne Zuordnung: leer, und es wird gesagt, fuer wen;
    - `assign` loest den Namen weiter auf wie bisher (gleiche Funktion).

Aufruf: python3 test/test_authz_cli_assignments.py
"""
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
SERVICES = os.path.join(HERE, "..", "platform", "services")
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-authz-cli-test-")
os.environ["OAAP_DATA_DIR"] = DATA
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = os.path.join(DATA, "data", "identity")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, IDENTITY_DIR)

import appctl  # noqa: E402

IDDATA = os.path.dirname(appctl._identity_users_path())
os.makedirs(IDDATA, exist_ok=True)
os.makedirs(appctl.APPS_DIR, exist_ok=True)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
# EIN Benutzerspeicher und EINE Mandantenliste fuer beide Seiten -- wie auf
# einem Knoten, wo appctl die Dateien liest, die Identity schreibt.
m.USERS_FILE = appctl._identity_users_path()
m.TENANTS_FILE = appctl.TENANTS_FILE
m.KEYS_FILE = os.path.join(IDDATA, "api-keys.json")
m.THROTTLE_FILE = os.path.join(IDDATA, "login-throttle.json")
m.AUDIT_LOG = os.path.join(IDDATA, "audit.jsonl")
m.AUTHZ_FILE = os.path.join(IDDATA, "authorization.json")
m.AUTHZ_LOCK_FILE = os.path.join(IDDATA, "authorization.lock")

with open(appctl.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"},
                           "t-vp": {"label": "vp"},
                           "t-sgl": {"label": "sgl"}}}, f)


def user(uid, name, roles, tenant):
    return {"id": uid, "username": name, "display_name": "",
            "password_hash": "", "kind": "human", "roles": roles,
            "groups": [], "tenant": tenant, "active": True,
            "session_epoch": 0}


USERS = [
    user("u-op", "joerg", ["server_admin", "admin", "user"], "t-default"),
    user("u-vp-ben", "ben", ["user"], "t-vp"),
    user("u-vp-carla", "carla", ["user"], "t-vp"),
    user("u-vp-dora", "dora", ["user"], "t-vp"),
    # KOEDER: wuerde die ID roh an die Abfrage gehaengt, laese die Route
    # `user=u-vp-ben` -- und eve bekaeme Bens Zuordnung zu sehen.
    user("u-vp-ben&x=1", "eve", ["user"], "t-vp"),
    # KOEDER: derselbe NAME, anderer Mandant, eigene Zuordnung.
    user("u-sgl-ben", "ben", ["user"], "t-sgl"),
    user("u-sgl-max", "max", ["user"], "t-sgl"),
]


def write_users():
    with open(m.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(USERS, f)


write_users()

c = m.app.test_client()
calls = []


def authz_call(method, path, body=None):
    """Was `_authz_call` IM Container tut -- ohne den Container."""
    calls.append((method, path))
    r = getattr(c, method.lower())(
        path, headers={m.INTERNAL_HEADER: m.INTERNAL_KEY}, json=body or {})
    return r.status_code, r.get_json()


appctl._authz_call = authz_call
# main() verlangt root; der Test ist es nicht (und Windows kennt es nicht).
os.geteuid = lambda: 0


def oaap(*argv):
    """Die echte Kommandozeile: (stdout, stderr, exit code)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    real = sys.argv
    sys.argv = ["oaap"] + list(argv)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            appctl.main()
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.argv = real
    return out.getvalue(), err.getvalue(), code


def rows(out):
    """Die Namensspalte der Liste, sortiert."""
    return sorted(line.split()[1] for line in out.splitlines()
                  if line.strip() and not line.startswith("No "))


print("Aufbau (durch dieselbe Kommandozeile)")
r = c.post("/internal/authz/register", headers={"X-OAAP-Internal-Key":
                                                "test-internal-key"},
           json={"app": "vereinsportal", "version": "0.1.0", "declaration": {
               "objects": [{"key": "news", "title": "News",
                            "activities": ["read", "publish"],
                            "fields": [{"key": "area",
                                        "values": ["news", "sponsoring"]}]}],
               "role_templates": [{"key": "bereich", "title": "Bereich",
                                   "grants": [{"object": "news",
                                               "activities": ["read"],
                                               "area": "$value"}]}]}})
ok("die Erklaerung der App ist registriert", r.status_code == 200,
   r.get_json())
for t in ("vp", "sgl"):
    oaap("authz", "role-add", "--tenant", t, "--app", "vereinsportal",
         "--template", "bereich", "--name", "News", "--value", "area=news")
    oaap("authz", "collection-add", "--tenant", t, "--name", "Redaktion",
         "--role", "News")
for t, who in (("vp", "ben"), ("vp", "carla"), ("sgl", "ben")):
    out, err, code = oaap("authz", "assign", "--tenant", t,
                          "--collection", "Redaktion", "--user", who)
    ok(f"assign loest den Namen auf wie bisher ({t}/{who})",
       code == 0 and "now holds" in out, out + err)
held = {a["subject"] for a in m.load_authz()["assignments"].values()}
ok("gespeichert ist die ID der Person IHRES Mandanten",
   held == {"u-vp-ben", "u-vp-carla", "u-sgl-ben"}, held)
out, err, code = oaap("authz", "assign", "--tenant", "vp",
                      "--collection", "Redaktion", "--user", "max")
ok("assign an den Namen eines anderen Mandanten: abgelehnt, mit Grund",
   code == 1 and "'max' is not a user of tenant 'vp'" in err, out + err)

print("Die Liste (Abschnitt 2.6)")
out, err, code = oaap("authz", "assignments", "--tenant", "vp")
ok("GEGENPROBE ohne --user: beide Zuordnungen des Mandanten",
   code == 0 and rows(out) == ["ben", "carla"], out + err)

calls.clear()
out, err, code = oaap("authz", "assignments", "--tenant", "vp",
                      "--user", "ben")
ok("mit --user ben: nur Bens Zuordnung (carla fehlt)",
   code == 0 and rows(out) == ["ben"], out + err)
ok("die Abfrage traegt die Benutzer-ID, nicht den Namen",
   [p for _m, p in calls if "/assignments?" in p
    and p.endswith("&user=u-vp-ben")] != [], calls)
out, err, code = oaap("authz", "assignments", "--tenant", "vp",
                      "--user", "carla")
ok("mit --user carla: nur Carlas (ben fehlt)",
   code == 0 and rows(out) == ["carla"], out + err)

out, err, code = oaap("authz", "assignments", "--tenant", "sgl",
                      "--user", "ben")
sgl_ids = [line.split()[0] for line in out.splitlines() if line.strip()]
vp_ben = next(a["id"] for a in m.load_authz()["assignments"].values()
              if a["subject"] == "u-vp-ben")
ok("KOEDER: derselbe Name im anderen Mandanten ist eine andere Person",
   code == 0 and rows(out) == ["ben"] and len(sgl_ids) == 1
   and not vp_ben.startswith(sgl_ids[0]), out + err)

out, err, code = oaap("authz", "assignments", "--tenant", "vp",
                      "--user", "dora")
ok("eine Person ohne Zuordnung: leer, und es wird gesagt, fuer wen",
   code == 0 and rows(out) == []
   and "No assignments for 'dora' yet." in out, out + err)

calls.clear()
out, err, code = oaap("authz", "assignments", "--tenant", "vp",
                      "--user", "eve")
ok("KOEDER: eine ID mit '&' wird kodiert -- eve sieht nicht Bens Zuordnung",
   code == 0 and rows(out) == [], out + err)
ok("... und steht kodiert in der Abfrage",
   any(p.endswith("&user=u-vp-ben%26x%3D1") for _m, p in calls), calls)

print("Wer nicht gemeint sein kann, bekommt nicht die ganze Liste")
for name, what in (("niemand", "ein unbekannter Name"),
                   ("max", "ein Name eines ANDEREN Mandanten"),
                   ("u-vp-ben", "eine ID statt eines Namens"),
                   ("", "KOEDER: ein leerer Name")):
    calls.clear()
    out, err, code = oaap("authz", "assignments", "--tenant", "vp",
                          "--user", name)
    ok(f"{what}: abgelehnt, mit Grund",
       code == 1 and f"'{name}' is not a user of tenant 'vp'" in err,
       out + err)
    ok("... nichts gelistet, die Route gar nicht erst gefragt",
       out == "" and calls == [], (out, calls))

with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    f.write("{kaputt")
out, err, code = oaap("authz", "assignments", "--tenant", "vp",
                      "--user", "ben")
ok("ein unlesbarer Benutzerspeicher: gesagt, nicht 'kein solcher Benutzer'",
   code == 1 and "cannot read the user store" in err, out + err)
write_users()

print("Die Hilfe")
out, err, code = oaap("authz", "--help")
ok("--user nennt beide Verwendungen (assign und assignments)",
   code == 0 and "for assignments:" in " ".join(out.split())
   and "for assign:" in " ".join(out.split()), out)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
