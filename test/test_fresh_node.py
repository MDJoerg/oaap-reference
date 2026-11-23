#!/usr/bin/env python3
"""I-68: a fresh node that is used before anyone ran `oaap update`.

The demo spike of 2026-11-23 (a whole node in one container on a
Windows laptop) was the first node in months that was installed and
then used straight away. Three things broke that every node of the
fleet had long healed over, and this file holds them shut:

    (1) THE GATEWAY ON A KERNEL WITHOUT MPTCP. The gateway asks for
        net.mptcp.enabled=0 (CVE-2026-98432). WSL2's kernel has no such
        switch, and Docker refuses to start the container at all. The
        compose line now reads a variable whose DEFAULT is the old
        value -- on every real node the resolved file is byte for byte
        what it was, so compose recreates nothing -- and install.sh /
        update.sh set a no-op there only when the kernel lacks MPTCP.

    (2) MIGRATE.SH ON A FRESH NODE. Only `oaap update` ran it, so a new
        node had no default tenant ("no tenant store yet") and none of
        the other node-level steps until its first update.

    (3) THE FIRST ADMIN AFTER AN APP. An app installed before the first
        administrator brings its machine principal (RFC-0027), and
        /internal/setup refused with 409 because users.json was not
        empty. Worse, had it gone through, it would have written the
        file over and taken the app's principal with it.

Run: python3 test/test_fresh_node.py
The identity half needs flask + werkzeug; without them it reports SKIP.
"""
import importlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, "..")
PLATFORM_DIR = os.path.join(REF, "platform")
IDENTITY_DIR = os.path.join(PLATFORM_DIR, "services", "identity")

fails = 0
skipped = False


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


print("(1) Das Gateway auf einem Kernel ohne MPTCP")

compose = read(PLATFORM_DIR, "docker-compose.yml")
install = read(REF, "install.sh")
update = read(PLATFORM_DIR, "update.sh")

m = re.search(r"sysctls:\s*\n\s*-\s*(\S+)", compose)
ok("die Zeile liest OAAP_GATEWAY_SYSCTL", m and m.group(1).startswith("${OAAP_GATEWAY_SYSCTL"),
   m and m.group(1))
ok("ihre Vorgabe ist genau der alte Wert (kein Neuanlegen auf der Flotte)",
   m and m.group(1) == "${OAAP_GATEWAY_SYSCTL:-net.mptcp.enabled=0}", m and m.group(1))
NOOP = "net.ipv4.ip_unprivileged_port_start=0"
ok("install.sh setzt den Ersatz nur ohne /proc/sys/net/mptcp/enabled",
   "if [ ! -e /proc/sys/net/mptcp/enabled ]; then" in install
   and f"OAAP_GATEWAY_SYSCTL={NOOP}" in install)
ok("update.sh setzt ihn ebenso und nimmt ihn mit MPTCP wieder heraus",
   "if [ -e /proc/sys/net/mptcp/enabled ]; then" in update
   and "sed -i '/^OAAP_GATEWAY_SYSCTL=/d'" in update
   and f'set_env OAAP_GATEWAY_SYSCTL "{NOOP}"' in update)
ok("update.sh entscheidet VOR dem compose up der Kerndienste",
   update.index("OAAP_GATEWAY_SYSCTL") < update.rindex(" up -d"))

print("")
print("(2) migrate.sh läuft auch bei der Installation")

i_mig = install.find('bash "$APP_DIR/migrate.sh"')
ok("install.sh ruft migrate.sh auf", i_mig > 0)
ok("nach dem Start der Kerndienste und nach restore-instances",
   install.index("up -d") < i_mig and install.index("restore-instances") < i_mig)
ok("vor der Übergabe an den Betreiber", i_mig < install.index("# ---------------------------------------------------------------- handover"))
ok("ein Fehler darin bricht die Installation nicht ab",
   re.search(r'bash "\$APP_DIR/migrate\.sh" \\\s*\n\s*\|\| say', install) is not None)
ok("migrate.sh legt den Standardmandanten an (migrate-tenants)",
   "appctl.py\" migrate-tenants" in read(PLATFORM_DIR, "migrate.sh"))

print("")
print("(3) Der erste Admin nach einer App")

try:
    import flask  # noqa: F401
    import werkzeug  # noqa: F401
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Teil läuft nicht")
    skipped = True
else:
    def load_identity(data_dir):
        os.environ["SESSION_SECRET"] = "test-session-secret"
        os.environ["SETUP_TOKEN"] = "test-setup-token"
        os.environ["INTERNAL_API_KEY"] = "test-internal-key"
        os.environ["OAAP_IDENTITY_DATA_DIR"] = data_dir
        sys.path.insert(0, IDENTITY_DIR)
        sys.modules.pop("app", None)
        mod = importlib.reload(importlib.import_module("app"))
        mod.USERS_FILE = os.path.join(data_dir, "users.json")
        mod.STATE_FILE = os.path.join(data_dir, "state.json")
        return mod

    def principal(name, kind):
        return {"username": name, "display_name": "", "password_hash": "",
                "kind": kind, "roles": ["user"], "groups": [],
                "tenant": "", "active": True, "session_epoch": 0}

    def fresh(users):
        d = tempfile.mkdtemp(prefix="oaap-fresh-test-")
        with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
            json.dump(users, f)
        with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        return d, load_identity(d)

    def users_of(d):
        with open(os.path.join(d, "users.json"), encoding="utf-8") as f:
            return {u["username"]: u for u in json.load(f)}

    H = {"X-OAAP-Internal-Key": "test-internal-key"}

    def setup(mod, name="chef"):
        return mod.app.test_client().post("/internal/setup", json={
            "token": "test-setup-token", "username": name,
            "password": "geheim12345"}, headers=H)

    d, mod = fresh([principal("svc-chat-1", "machine")])
    r = setup(mod)
    ok("mit nur einem Maschinenkonto geht die Ersteinrichtung durch",
       r.status_code == 201, (r.status_code, r.get_json()))
    after = users_of(d)
    ok("der erste Admin steht in der Datei, mit server_admin",
       "server_admin" in (after.get("chef") or {}).get("roles", []), after)
    ok("und das Maschinenkonto der App ist noch da",
       "svc-chat-1" in after and after["svc-chat-1"].get("kind") == "machine", after)

    d, mod = fresh([principal("svc-chat-1", "machine")])
    r = setup(mod, "svc-chat-1")
    ok("der Name eines Maschinenkontos wird nicht der erste Admin",
       r.status_code == 409, (r.status_code, r.get_json()))
    ok("und das Konto bleibt unverändert",
       users_of(d)["svc-chat-1"].get("kind") == "machine")

    d, mod = fresh([principal("anna", "human")])
    r = setup(mod)
    ok("ist schon eine Person da, bleibt es bei 409",
       r.status_code == 409, (r.status_code, r.get_json()))

    d, mod = fresh([principal("altdatensatz", "human")])
    rec = json.load(open(os.path.join(d, "users.json"), encoding="utf-8"))
    rec[0].pop("kind")
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    mod = load_identity(d)
    ok("ein Satz ohne 'kind' (vor RFC-0027) zählt als Person",
       setup(mod).status_code == 409)

    d, mod = fresh([principal("svc-chat-1", "machine")])
    page = mod.app.test_client().get("/auth/login").get_data(as_text=True)
    ok("die Anmeldeseite zeigt mit nur Maschinen den Hinweis auf die Einrichtung",
       "Es gibt noch keine Benutzer" in page, page[:300])

print("")
print("Die Begrüßung beim Login auf der Konsole")
ok("die motd fragt setup_done, nicht 'users.json ist nicht leer'",
   '"setup_done": true\' "$OAAP_DATA_DIR/data/identity/state.json"' in install
   and '-s "$OAAP_DATA_DIR/data/identity/users.json"' not in install)

print("")
print(f"{'OK' if not fails else 'FEHLER'}: {fails} fehlgeschlagen")
sys.exit(1 if fails else (2 if skipped else 0))
