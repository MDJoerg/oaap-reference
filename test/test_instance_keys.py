"""Instanzschluessel folgen der Instanz (RFC-0062, 0.1.202).

    python3 test/test_instance_keys.py      # braucht flask/werkzeug

Gegen den ECHTEN Identity-Code (Schluessel, Benutzer, Fachrechte-Zustand
in einem Wegwerf-Verzeichnis): `_identity_exec` fuehrt die Skripte hier im
Prozess aus statt im Container, `docker` wird abgefangen. Ob ein Schluessel
"noch spricht", fragt der Test so, wie Identity jede Anfrage prueft:
`_by_key(token, instance)`.

Gehalten wird:
  D1  nach dem Entfernen (mit UND ohne --purge) spricht kein Schluessel der
      Instanz mehr, Prinzipal und Fachrechte-Eintrag sind weg, die
      aufbewahrte instance.env traegt keine Schluessel; eine Neuinstallation
      unter demselben Namen ist von den alten nicht erreichbar.
  D2  nach dem Umbenennen gelten nur neue Schluessel unter dem neuen Namen,
      der alte Prinzipal ist weg, das Verwaltungsrecht bleibt, die
      Vorgangs-Tuer passt zum neuen Namen.
  D3  was in 30 Tagen ablaeuft, wird erneuert (neuer Schluessel gilt, alter
      nicht mehr, Container neu erzeugt); eine schlafende Instanz ohne
      Neuerzeugung; was lange gilt, bleibt; --dry-run aendert nichts.
  D4  Waisen werden einmal abgeraeumt, der zweite Lauf schweigt.
"""
import contextlib
import datetime
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
DATA = tempfile.mkdtemp(prefix="oaap-instkeys-")
os.environ["OAAP_DATA_DIR"] = DATA

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
    print("SKIP  flask/werkzeug fehlen.")
    sys.exit(1)

# --- Identity, echt, im Wegwerf-Verzeichnis
d = tempfile.mkdtemp(prefix="oaap-instkeys-id-")
for k, v in {"SESSION_SECRET": "s", "SETUP_TOKEN": "t", "INTERNAL_API_KEY": "i",
             "OAAP_IDENTITY_DATA_DIR": d}.items():
    os.environ[k] = v
json.dump([], open(os.path.join(d, "users.json"), "w"))
json.dump({"setup_done": True, "server_admin_migrated": True, "support_migrated": True,
           "tenant_migrated": True}, open(os.path.join(d, "state.json"), "w"))
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
ident = importlib.reload(importlib.import_module("app"))
ident.USERS_FILE = os.path.join(d, "users.json")
ident.STATE_FILE = os.path.join(d, "state.json")
ident.KEYS_FILE = os.path.join(d, "api-keys.json")
ident.AUDIT_LOG = os.path.join(d, "audit.jsonl")
ident.AUTHZ_FILE = os.path.join(d, "authorization.json")
ident.AUTHZ_LOCK_FILE = os.path.join(d, "authorization.lock")
TENANT = "t-op"
json.dump({"tenants": {TENANT: {"label": "default"}}}, open(os.path.join(d, "tenants.json"), "w"))
ident.TENANTS_FILE = os.path.join(d, "tenants.json")
ident.THROTTLE_FILE = os.path.join(d, "throttle.json")
# Der Test fragt absichtlich viele ALTE Schluessel von derselben Adresse --
# genau das, wogegen die Bremse fuer Schluessel (RFC-0010) schuetzt. Sie
# wuerde danach auch jeden gueltigen abweisen und ist hier nicht Gegenstand.
ident._login_blocked = lambda b: False
ident._login_failed = lambda b: None
sys.path.remove(IDENTITY_DIR)
sys.modules.pop("app", None)

# --- appctl
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)


def fake_identity_exec(script, env=None):
    old_env = dict(os.environ)
    os.environ.update(env or {})
    sys.modules["app"] = ident
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(script, "<identity>", "exec"), {"__name__": "__identity__"})
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.modules.pop("app", None)
    return out.getvalue()


m._identity_exec = fake_identity_exec
m.resolve_tenant = lambda ref: ref or TENANT
RECREATED = []
m.recreate_instance_containers = lambda name, *a, **k: RECREATED.append(name)
_real_run = subprocess.run


def fake_run(cmd, *a, **k):
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "docker":
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return _real_run(cmd, *a, **k)


subprocess.run = fake_run
m.remove_app_network = lambda *a, **k: None
m.ensure_app_network = lambda *a, **k: "net"
m.connect_gateway = lambda *a, **k: None


def speaks(token, instance):
    """Wuerde Identity diese Anfrage durchlassen?"""
    with ident.app.test_request_context("/verify"):
        user, _method, refusal = ident._by_key(token, instance)
    return refusal is None and user is not None


def principal_exists(name):
    return any(u["username"] == f"instance:{name}" for u in ident.load_users())


def record(name, port, **extra):
    return dict({"app_id": name + "-app", "app_name": name, "version": "1.0.0",
                 "channel": "production", "port": port, "container": f"oaap-app-{name}",
                 "svc_port": 8000, "image": "x", "tenant": TENANT,
                 "id": (name.replace("-", "") + "x" * 12)[:12], "name": name,
                 "services": [{"service": "", "container": f"oaap-app-{name}",
                               "image": "x", "build": "", "port": 8000}],
                 "routes": [{"path": "/", "roles": ["user"]}], "visibility": {}}, **extra)


def install(name, port, app_id=None, admin=False, **extra):
    reg = m.load_registry()
    reg["instances"][name] = record(name, port, **extra)
    if app_id:
        reg["instances"][name]["app_id"] = app_id
    m.save_registry(reg)
    inst = reg["instances"][name]
    env = {"OAAP_APP_SECRET": "geheim-" + name, "EIGENE_VARIABLE": "bleibt"}
    env.update(m.instance_keys_issue(name, TENANT, inst["app_id"], m.INSTANCE_KEY_VARS,
                                     administer=admin, port=port))
    m.save_env(name, env, inst)
    return env


SCOPE = {"OAAP_PLATFORM_KEY": lambda n: m.TWIN_KEY_SCOPE,
         "OAAP_AUTHZ_KEY": lambda n: m.AUTHZ_KEY_SCOPE,
         "OAAP_AUTHZ_ADMIN_KEY": lambda n: m.AUTHZ_ADMIN_KEY_SCOPE,
         "OAAP_CASES_KEY": lambda n: m.cases_submit_scope(n)}


def all_speak(env, name):
    return {v: speaks(env[v], SCOPE[v](name)) for v in m.INSTANCE_KEY_VARS}


def quiet(fn, *a, **k):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        r = fn(*a, **k)
    return r, out.getvalue()


print("")
print("0. Ausgangslage")
env = install("probe", 8150, app_id="probe-a", admin=True)
ok("alle vier Schluessel sprechen", all(all_speak(env, "probe").values()), all_speak(env, "probe"))
ok("Fachrechte-Eintrag und Prinzipal da",
   ident.load_authz()["instances"].get("probe", {}).get("app") == "probe-a"
   and principal_exists("probe"))

print("")
print("1. D1 -- Entfernen ohne --purge")
reg = m.load_registry()
inst = reg["instances"]["probe"]
res, out = quiet(m.remove_instance, reg, "probe", False)
ok("kein alter Schluessel spricht mehr", not any(all_speak(env, "probe").values()),
   all_speak(env, "probe"))
ok("Prinzipal geloescht, Fachrechte-Eintrag weg",
   not principal_exists("probe") and "probe" not in ident.load_authz()["instances"])
kept = m.load_env("probe", inst)
ok("die aufbewahrte instance.env traegt keine Schluessel, behaelt den Rest",
   not any(v in kept for v in m.INSTANCE_KEY_VARS) and "OAAP_CASES_URL" not in kept
   and kept.get("OAAP_APP_SECRET") == "geheim-probe" and kept.get("EIGENE_VARIABLE") == "bleibt",
   kept)
ok("die Ausgabe sagt es", "Retired the platform keys" in out, out)
env_b = install("probe", 8150, app_id="probe-b")
ok("eine ANDERE App unter demselben Namen: die alten Schluessel sprechen nicht fuer sie",
   not any(all_speak(env, "probe").values()), all_speak(env, "probe"))
ok("… ihre eigenen schon", all(all_speak(env_b, "probe").values()))

print("")
print("2. D1 -- Entfernen mit --purge")
reg = m.load_registry()
res, out = quiet(m.remove_instance, reg, "probe", True)
ok("kein Schluessel der zweiten App spricht mehr", not any(all_speak(env_b, "probe").values()))
ok("Prinzipal und Eintrag weg", not principal_exists("probe")
   and "probe" not in ident.load_authz()["instances"])

print("")
print("3. D2 -- Umbenennen")
env_r = install("ren-a", 8151, admin=True)
m.write_app_caddy("ren-a", m.load_registry()["instances"]["ren-a"])
reg = m.load_registry()
_r, out = quiet(m.rekey_instance, reg, "ren-a", "ren-b", 0)
reg = m.load_registry()
new_env = m.load_env("ren-b", reg["instances"]["ren-b"])
ok("die alten Schluessel sprechen nicht mehr", not any(all_speak(env_r, "ren-a").values()),
   all_speak(env_r, "ren-a"))
ok("die neuen sprechen, unter dem neuen Namen", all(all_speak(new_env, "ren-b").values()),
   all_speak(new_env, "ren-b"))
ok("alter Prinzipal weg, neuer da", not principal_exists("ren-a") and principal_exists("ren-b"))
rec = ident.load_authz()["instances"]
ok("Fachrechte-Eintrag umgezogen, Verwaltungsrecht behalten",
   "ren-a" not in rec and rec.get("ren-b", {}).get("administer") is True, rec)
ok("die Vorgangs-Tuer passt zum neuen Namen, der Port bleibt",
   speaks(new_env["OAAP_CASES_KEY"], "oaap.cases.submit:ren-b")
   and not speaks(new_env["OAAP_CASES_KEY"], "oaap.cases.submit:ren-a")
   and new_env.get("OAAP_CASES_URL", "").endswith(":8151/platform/cases/submit"), new_env.get("OAAP_CASES_URL"))
ok("die Container wurden danach neu erzeugt", "ren-b" in RECREATED, RECREATED)
ok("die LAN-Site steht unter dem neuen Namen (seit 0.1.61 fehlte sie)",
   os.path.isfile(os.path.join(m.CADDY_APPS_DIR, "ren-b.caddy"))
   and not os.path.isfile(os.path.join(m.CADDY_APPS_DIR, "ren-a.caddy")))
# eine Instanz, deren LAN-Site schon fehlt (eine Umbenennung vor 0.1.202)
os.remove(os.path.join(m.CADDY_APPS_DIR, "ren-b.caddy"))
reg = m.load_registry()
quiet(m.rekey_instance, reg, "ren-b", "ren-c", 0)
ok("… auch wenn die alte Site schon fehlte",
   os.path.isfile(os.path.join(m.CADDY_APPS_DIR, "ren-c.caddy")))
ok("die Ausgabe sagt es", "Reissued 4 platform key(s)" in out, out)

print("")
print("4. D3 -- Erneuerung")
env_soon = install("soon", 8152)
env_late = install("late", 8153)
env_sleep = install("sleepy", 8154)
keys = json.load(open(ident.KEYS_FILE))
soon_ids = {m._key_id(env_soon[v]) for v in m.INSTANCE_KEY_VARS}
sleep_id = m._key_id(env_sleep["OAAP_AUTHZ_KEY"])
in5 = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
for k in keys:
    if k["id"] in soon_ids or k["id"] == sleep_id:
        k["expires"] = in5
json.dump(keys, open(ident.KEYS_FILE, "w"))
done = m.renew_instance_keys(dry_run=True)
ok("--dry-run nennt genau die faelligen", sorted((n, v) for n, v, _ in done)
   == sorted([("soon", v) for v in m.INSTANCE_KEY_VARS] + [("sleepy", "OAAP_AUTHZ_KEY")]), done)
ok("--dry-run aendert nichts", m.load_env("soon")["OAAP_AUTHZ_KEY"] == env_soon["OAAP_AUTHZ_KEY"])
m.mode_keys = lambda: (frozenset({"sleepy"}), frozenset())
del RECREATED[:]
done = m.renew_instance_keys()
new_soon = m.load_env("soon")
ok("die neuen Schluessel sprechen", all(all_speak(new_soon, "soon").values()), all_speak(new_soon, "soon"))
ok("die alten nicht mehr", not any(all_speak(env_soon, "soon").values()))
ok("die laufende Instanz wurde neu erzeugt, die schlafende nicht",
   RECREATED == ["soon"], RECREATED)
new_sleep = m.load_env("sleepy")
ok("die schlafende hat den neuen Schluessel in der instance.env, der alte gilt nicht",
   new_sleep["OAAP_AUTHZ_KEY"] != env_sleep["OAAP_AUTHZ_KEY"]
   and speaks(new_sleep["OAAP_AUTHZ_KEY"], m.AUTHZ_KEY_SCOPE)
   and not speaks(env_sleep["OAAP_AUTHZ_KEY"], m.AUTHZ_KEY_SCOPE))
ok("… ihre anderen Schluessel blieben", new_sleep["OAAP_CASES_KEY"] == env_sleep["OAAP_CASES_KEY"])
ok("was lange gilt, bleibt unberuehrt", m.load_env("late") == {**env_late})
ok("ein zweiter Lauf findet nichts", m.renew_instance_keys() == [])
# ein Schluessel in der instance.env, den Identity nicht (mehr) kennt
e = m.load_env("late")
ident.revoke_key(m._key_id(e["OAAP_PLATFORM_KEY"]))
done = m.renew_instance_keys()
ok("ein entzogener Schluessel in der instance.env wird ersetzt",
   done == [("late", "OAAP_PLATFORM_KEY", "revoked")]
   and speaks(m.load_env("late")["OAAP_PLATFORM_KEY"], m.TWIN_KEY_SCOPE), done)
# Generalprobe: keine Schluessel, keine Erneuerung
reg = m.load_registry()
reg["instances"]["late"]["rehearsal"] = {"of": "x", "expires": "2099-01-01T00:00:00Z"}
m.save_registry(reg)
keys = json.load(open(ident.KEYS_FILE))
for k in keys:
    if k["principal"] == "instance:late":
        k["expires"] = in5
json.dump(keys, open(ident.KEYS_FILE, "w"))
ok("eine Generalprobe wird nicht erneuert", all(n != "late" for n, _v, _w in m.renew_instance_keys(dry_run=True)))

print("")
print("5. D4 -- Waisen")
ghost = m.instance_keys_issue("ghost", TENANT, "ghost-app", ["OAAP_AUTHZ_KEY", "OAAP_CASES_KEY"])
with ident.authz_rw() as st:
    st["instances"]["phantom"] = {"app": "p", "tenant": TENANT}
    ident.save_authz(st)
ok("Ausgangslage: Waise spricht", speaks(ghost["OAAP_AUTHZ_KEY"], m.AUTHZ_KEY_SCOPE))
_r, out = quiet(m.cmd_migrate_instance_keys, None)
ok("die Waise spricht nicht mehr, Prinzipal und Eintraege weg",
   not speaks(ghost["OAAP_AUTHZ_KEY"], m.AUTHZ_KEY_SCOPE) and not principal_exists("ghost")
   and "ghost" not in ident.load_authz()["instances"]
   and "phantom" not in ident.load_authz()["instances"], out)
ok("… und die Ausgabe nennt sie", "ghost" in out and "phantom" in out, out)
ok("lebende Instanzen bleiben unberuehrt", all(all_speak(new_soon, "soon").values()))
_r, out = quiet(m.cmd_migrate_instance_keys, None)
ok("ein zweiter Lauf schweigt", out == "", out)

print("")
print("ALLE PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
