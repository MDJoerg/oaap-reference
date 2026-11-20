"""Rueckbau mit Konten und waehlbare Instanznamen (RFC-0055 §11, RFC-0104, 2026-11-20).

    python3 test/test_build_rollback_accounts.py

Anlass: kit1 auf oaapx01 (2026-11-19). Der Rueckbau blieb am Mandanten
stehen, weil die erste Verwaltung ein Konto im Mandanten war; loeschen liess
es sich nicht, weil es der letzte tenant_admin war.

A. identity `_delete_tenant_accounts`: nur server_admin; alle Konten des
   Mandanten, auch der letzte tenant_admin; nie ein server_admin, nie ein
   Konto mit gueltigem API-Schluessel (dann niemand -- alles oder nichts);
   andere Mandanten bleiben; `dry` aendert nichts; je Konto eine
   Protokollzeile.
B. appctl: der Rueckbau lehnt VOR dem ersten Schritt ab, wenn der Mandant
   Konten hat und der Haken fehlt; mit Haken loescht der Mandanten-Schritt
   erst die Konten, dann den Mandanten; ein Nein von identity haelt ihn an.
C. Portal und API reichen `purge_accounts` weiter; der Haken steht nur bei
   einem Aufbau, der den Mandanten angelegt hat.
D. Profil: `distinct` -- zwei Parameter derselben Gruppe mit gleichem Wert
   sind ein Problem; das Profil ki-arbeitsplatz 2.2.0 nimmt eigene Namen an
   und fuehrt sie in jeden Bezug.
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


SERVICES = os.path.join(ROOT, "platform", "services")
sys.path.insert(0, SERVICES)
import tenant_build as tb                                      # noqa: E402

# ------------------------------------------------------------------ D
print("D. Profil: distinct und eigene Namen")
PROFILE = os.path.join(ROOT, "..", "oaap-docs", "szenarien", "profile",
                       "ki-arbeitsplatz.json")
if os.path.exists(PROFILE):
    prof = json.load(open(PROFILE, encoding="utf-8"))
    ok("Profil ki-arbeitsplatz ohne Probleme", tb.profile_problems(prof) == [],
       tb.profile_problems(prof))
    v, pr = tb.param_values(prof, {"label": "kit2", "title": "K",
                                   "name_help": "help", "name_tools": "mcp"})
    ok("eigene Namen werden angenommen", pr == [] and v["name_help"] == "help", pr)
    steps = {s["id"]: tb.render_step(s, v) for s in prof["steps"]}
    ok("app.install nennt den eigenen Namen",
       steps["hilfe"]["name"] == "help" and steps["werkzeuge"]["name"] == "mcp")
    ok("destination.ensure und app.setup folgen",
       steps["docs-origin"]["instance"] == "help"
       and steps["hilfe-holen"]["name"] == "help"
       and steps["werkzeuge-einrichten"]["name"] == "mcp")
    items = steps["werkzeuge-einrichten"]["items"]
    ok("Einrichtungseintraege nennen den Knotenschluessel <kuerzel>-<name>",
       items[0]["id"] == "kit2-help"
       and items[2]["tools"]["server"] == "kit2-help"
       and items[4]["gateway"] == "kit2-mcp", items)
    ok("ungeaenderte Namen bleiben der Vorschlag",
       steps["modelle"]["name"] == "modelle"
       and steps["modellname"]["items"][1]["id"] == "kit2-chat")
    _v, pr = tb.param_values(prof, {"label": "kit2", "title": "K",
                                    "name_help": "chat"})
    ok("zwei gleiche Instanznamen werden vor dem Start abgelehnt",
       any("twice" in p for p in pr), pr)
    _v, pr = tb.param_values(prof, {"label": "kit2", "title": "K",
                                    "name_help": "Hilfe Neu"})
    ok("ein Name mit Grossbuchstaben/Leerzeichen wird abgelehnt", bool(pr), pr)
else:
    print("SKIP  oaap-docs nicht ausgecheckt")

bad = {"profile": "oaap.tenant-profile/1", "id": "x", "title": "x",
       "params": {"label": {"kind": "label", "required": True},
                  "n": {"kind": "bool", "distinct": "g"}},
       "steps": [{"id": "t", "type": "tenant.create", "label": "{label}",
                  "title": "x"}]}
ok("distinct bei kind bool ist ein Profilproblem",
   any("distinct" in p for p in tb.profile_problems(bad)), tb.profile_problems(bad))

# ------------------------------------------------------------------ A
print("A. identity: _delete_tenant_accounts")
try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False
    print("SKIP  flask fehlt -- Teil A laeuft im Container")

if HAVE_FLASK:
    DATA = tempfile.mkdtemp(prefix="oaap-rollback-accounts-")
    os.environ.update({"SESSION_SECRET": "s", "SETUP_TOKEN": "t",
                       "INTERNAL_API_KEY": "k", "OAAP_IDENTITY_DATA_DIR": DATA})
    sys.path.insert(0, os.path.join(SERVICES, "identity"))
    sys.modules.pop("app", None)
    m = importlib.reload(importlib.import_module("app"))
    for attr, fn in (("USERS_FILE", "users.json"), ("KEYS_FILE", "api-keys.json"),
                     ("AUDIT_LOG", "audit.jsonl"), ("TENANTS_FILE", "tenants.json"),
                     ("AUTHZ_FILE", "authorization.json"),
                     ("AUTHZ_LOCK_FILE", "authorization.lock")):
        setattr(m, attr, os.path.join(DATA, fn))
    with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"tenants": {"t-kit": {"label": "kit1"},
                               "t-bec": {"label": "bec"}}}, f)

    def user(uid, name, roles, tenant):
        return {"id": uid, "username": name, "display_name": name,
                "password_hash": "x", "kind": "human", "roles": roles,
                "groups": [], "tenant": tenant, "active": True,
                "session_epoch": 0}

    def seed(users, keys=()):
        with open(m.USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f)
        with open(m.KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(keys), f)

    def names():
        return sorted(u["username"] for u in m.load_users())

    base = [user("u1", "kit1-admin", ["tenant_admin", "user"], "t-kit"),
            user("u2", "kit1-anna", ["user"], "t-kit"),
            user("u3", "bec-chef", ["tenant_admin", "user"], "t-bec")]

    seed(base)
    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("kit1", us, "root", "tenant_admin")
    ok("nur ein server_admin", code == 403 and len(names()) == 3, (code, body))

    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("kit1", us, "root", "server_admin",
                                               dry=True)
    ok("dry nennt die Konten und aendert nichts",
       code == 200 and body["would_delete"] == ["kit1-admin", "kit1-anna"]
       and len(names()) == 3, (code, body))

    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("kit1", us, "root", "server_admin")
    ok("alle Konten des Mandanten weg, auch der letzte tenant_admin",
       code == 200 and names() == ["bec-chef"], (code, body, names()))
    audit = open(m.AUDIT_LOG, encoding="utf-8").read()
    ok("je Konto eine Protokollzeile user.delete",
       audit.count('"user.delete"') == 2 and "kit1-admin" in audit
       and "Rückbau des Mandanten" in audit)

    seed(base + [user("u9", "kit1-root", ["server_admin"], "t-kit")])
    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("kit1", us, "root", "server_admin")
    ok("ein server_admin im Mandanten: niemand wird geloescht",
       code == 409 and "kit1-root" in body["error"] and len(names()) == 4,
       (code, body))

    seed(base, [{"id": "k1", "principal": "kit1-anna", "revoked": False,
                 "expires": ""}])
    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("kit1", us, "root", "server_admin")
    ok("gueltiger API-Schluessel: niemand wird geloescht, der Name steht da",
       code == 409 and body.get("keys") == ["kit1-anna"] and len(names()) == 3,
       (code, body))

    seed(base, [{"id": "k1", "principal": "kit1-anna", "revoked": True,
                 "expires": ""}])
    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("kit1", us, "root", "server_admin")
    ok("ein entzogener Schluessel haelt nicht", code == 200, (code, body))

    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("gibtsnicht", us, "root", "server_admin")
    ok("unbekannter Mandant: 404", code == 404)
    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("", us, "root", "server_admin")
    ok("leerer Verweis trifft NIE den Standard-Mandanten", code == 404
       and "bec-chef" in names(), (code, body))
    real_default = m.default_tenant_id
    m.default_tenant_id = lambda: "t-bec"
    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("bec", us, "root", "server_admin")
    m.default_tenant_id = real_default
    ok("der Standard-Mandant ist tabu, auch beim Namen genannt",
       code == 404 and "bec-chef" in names(), (code, body))
    with m.users_rw() as us:
        body, code = m._delete_tenant_accounts("t-kit", us, "root", "server_admin",
                                               dry=True)
    ok("die interne Kennung geht wie das Kuerzel", code == 200, (code, body))
    sys.modules.pop("app", None)

# ------------------------------------------------------------------ B
print("B. appctl: Rueckbau des Mandanten mit Konten")
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-rollback-appctl-")
sys.path.insert(0, os.path.join(ROOT, "platform"))
import appctl as a                                             # noqa: E402

calls = []
answer = {"dry": (200, {"ok": True, "would_delete": ["kit1-admin"]}),
          "real": (200, {"ok": True, "deleted": ["kit1-admin"]})}


def fake_delete(tenant, actor, dry=False):
    calls.append(("dry" if dry else "real", tenant))
    return answer["dry" if dry else "real"]


verbs = []
a.tenant_accounts_delete = fake_delete


class D(a._BuildDrivers):
    def _tenant(self):
        return "t-kit", {"label": "kit1"}

    def _verb(self, argv, ok_text):
        verbs.append(argv)
        return True, ok_text


state = {"steps": [{"id": "tenant", "made": ["tenant:t-kit"]}]}
why = D("kit1").rollback_refusal(state)
ok("ohne Haken: Ablehnung VOR dem Rueckbau, mit Kontonamen und Weg",
   "kit1-admin" in why and "--purge-accounts" in why, why)
ok("die Frage war ein dry-Lauf", calls and calls[-1][0] == "dry", calls)

calls.clear()
ok("mit Haken: keine Ablehnung", D("kit1", accounts=True).rollback_refusal(state) == "")
okk, note = D("kit1", accounts=True)._undo_tenant_create({}, "tenant:t-kit", {})
ok("mit Haken: erst Konten loeschen, dann den Mandanten entfernen",
   okk and calls == [("real", "t-kit")]
   and verbs[-1] == ["tenant", "remove", "kit1", "--yes"]
   and "kit1-admin" in note, (calls, verbs, note))

answer["real"] = (409, {"error": "Gültige API-Schlüssel — zuerst entziehen: kit1-admin"})
verbs.clear()
okk, note = D("kit1", accounts=True)._undo_tenant_create({}, "tenant:t-kit", {})
ok("ein Nein von identity haelt den Schritt an, der Mandant bleibt",
   not okk and not verbs and "API-Schlüssel" in note, (okk, verbs, note))

answer["dry"] = (200, {"ok": True, "would_delete": []})
ok("ein Mandant ohne Konten: keine Ablehnung",
   D("kit1").rollback_refusal(state) == "")
ok("ein Aufbau, der den Mandanten nicht angelegt hat: keine Frage an identity",
   D("kit1").rollback_refusal({"steps": [{"id": "x", "made": ["instance:k"]}]}) == "")

# ------------------------------------------------------------------ C
print("C. Portal und API")
PORTAL = open(os.path.join(SERVICES, "portal", "app.py"), encoding="utf-8").read()
API = open(os.path.join(SERVICES, "portal", "management_api.py"), encoding="utf-8").read()
VIEW = open(os.path.join(SERVICES, "portal", "build_view.py"), encoding="utf-8").read()
APPCTL = open(os.path.join(ROOT, "platform", "appctl.py"), encoding="utf-8").read()
ok("Haken purge_accounts nur bei b.made_tenant",
   '{% if b.made_tenant %}' in PORTAL and 'name="purge_accounts"' in PORTAL)
ok("Portal reicht ihn beim Rueckbau weiter, beim Fortsetzen nie",
   'args["purge_accounts"] = request.form.get("purge_accounts") == "1"' in PORTAL
   and 'args["purge_accounts"] = False' in PORTAL)
ok("API nimmt nur ein woertliches true",
   'args["purge_accounts"] = body.get("purge_accounts") is True' in API)
ok("die Ansicht kennt made_tenant", '"made_tenant": made_tenant' in VIEW)
ok("der Auftrag liest purge_accounts", 'a.get("purge_accounts")' in APPCTL)
ok("CLI --purge-accounts", '"--purge-accounts"' in APPCTL)

print("")
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
