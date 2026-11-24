"""Apps invite people, RFC-0126 stage 1 (reference 0.1.366).

Without containers:
  1. the pure half at Keycloak (services/people_kc.py): addresses, the body
     of a new login, the marker check, the status;
  2. identity's door /people/v1 with Flask's test client and a stand-in realm:
     who may speak, the record made BEFORE the first login (partner, bound to
     the realm's subject), duplicates, limits, only one's own people, lock;
     and that a realm group mapping never raises an invited person;
  3. the node (appctl): the manifest block, the key, the gateway door, the
     switch's command.

What this file cannot check: Keycloak itself -- measured on oaap-test (RFC-0126
§1.5 and stage 1's measurement).

Run: python3 test/test_people_invite.py
"""
import importlib
import inspect
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
IDENTITY_DIR = os.path.join(SERVICES, "identity")
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-people-")
sys.path.insert(0, SERVICES)

import people_kc as pk                                         # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


print("\n=== people_kc: die reinen Regeln ===")
ok("Admin-Adresse und Token-Adresse aus dem Issuer",
   pk.admin_base("https://auth.x/realms/inv1") == "https://auth.x/admin/realms/inv1"
   and pk.token_url("https://auth.x/realms/inv1") == "https://auth.x/realms/inv1/protocol/openid-connect/token"
   and pk.admin_base("https://auth.x/kc/realms/a-b") == "https://auth.x/kc/admin/realms/a-b"
   and pk.admin_base("nonsense") == "")
b = pk.user_body("Ina@Example.org", "s0-bestand", "Ina Probe", "en")
ok("neuer Login: aktiv, Adresse unbestaetigt, beide Aktionen, Markierung der Instanz, Sprache",
   b["enabled"] is True and b["emailVerified"] is False
   and b["requiredActions"] == ["VERIFY_EMAIL", "UPDATE_PASSWORD"]
   and b["attributes"]["oaap_invited_by"] == ["instance:s0-bestand"]
   and b["attributes"]["locale"] == ["en"] and b["username"] == "ina@example.org"
   and b["firstName"] == "Ina" and b["lastName"] == "Probe")
ok("unbekannte Sprache -> de", pk.user_body("a@b.de", "x", language="fr")["attributes"]["locale"] == ["de"])
ok("kein Feld fuer Passwort, Rollen oder Gruppen im Koerper",
   not {"credentials", "realmRoles", "clientRoles", "groups"} & set(b))
ok("Besitz nur ueber die Markierung genau dieser Instanz",
   pk.owned_by({"attributes": {"oaap_invited_by": ["instance:a"]}}, "a")
   and not pk.owned_by({"attributes": {"oaap_invited_by": ["instance:b"]}}, "a")
   and not pk.owned_by({"attributes": {}}, "a") and not pk.owned_by({}, "a"))
ok("Status aus der Antwort des Realms",
   pk.status_of({"enabled": True, "requiredActions": ["X"]}, 1000, 72, now=2000) == "invited"
   and pk.status_of({"enabled": True, "requiredActions": ["X"]}, 1000, 72, now=1000 + 73 * 3600) == "expired"
   and pk.status_of({"enabled": True, "requiredActions": []}, 1000, 72) == "active"
   and pk.status_of({"enabled": False, "requiredActions": []}, 1000, 72) == "locked"
   and pk.status_of(None, 1000, 72) == "gone")
ok("Adressen: eine einzelne, sonst abgelehnt",
   pk.email_error("ina@example.org") == "" and pk.email_error("a@b") and pk.email_error("a b@c.de")
   and pk.email_error("a@b.de, c@d.de") and pk.email_error(""))

print("\n=== identity: die Tuer /people/v1 ===")
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt")
    sys.exit(1 if fails else 0)

DATA = tempfile.mkdtemp(prefix="oaap-people-id-")
os.environ.update(SESSION_SECRET="s", SETUP_TOKEN="t", INTERNAL_API_KEY="k",
                  OAAP_IDENTITY_DATA_DIR=DATA)
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.import_module("app")
T, T2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
m.USERS_FILE = os.path.join(DATA, "users.json")
m.KEYS_FILE = os.path.join(DATA, "api-keys.json")
m.TENANTS_FILE = os.path.join(DATA, "tenants.json")
m.IDP_SECRETS_FILE = os.path.join(DATA, "idp-secrets.json")
m.PEOPLE_LIMITS_FILE = os.path.join(DATA, "people-limits.json")
ISS = "https://auth.x/realms/inv1"
tenants = {T: {"label": "inv1", "people_invite": True,
               "idp": {"kind": "oidc", "issuer": ISS, "client_id": "oaap-node"}},
           T2: {"label": "other", "people_invite": False}}
json.dump({"tenants": tenants}, open(m.TENANTS_FILE, "w"))
json.dump({"providers": {T: {"client_secret": "c", "people": {"client_id": "oaap-people",
                                                              "secret": "s3", "issuer": ISS}}}},
          open(m.IDP_SECRETS_FILE, "w"))
mach = lambda name, ten: {"username": name, "display_name": name, "password_hash": "",  # noqa: E731
                          "kind": "machine", "roles": ["user"], "groups": [], "tenant": ten,
                          "active": True, "id": name}
m._save(m.USERS_FILE, [mach("instance:inv1-bestand", T), mach("instance:inv1-other", T),
                       mach("instance:other-app", T2)])


class FakeRealm:
    users, mails = {}, []

    def __init__(self, issuer, cid, secret):
        self.issuer = issuer

    def create(self, email, instance, name="", language="de"):
        if any(u["email"].lower() == email.lower() for u in self.users.values()):
            raise pk.RealmError(409, "exists: this address already has a login in the tenant")
        sub = f"sub-{len(self.users) + 1}"
        self.users[sub] = dict(pk.user_body(email, instance, name, language), id=sub)
        return sub

    def read(self, sub, instance):
        d = self.users.get(sub)
        if d is None:
            return None
        if not pk.owned_by(d, instance):
            raise pk.RealmError(403, "not a person this app invited")
        return d

    def mail(self, sub, instance, hours=72):
        self.read(sub, instance)
        self.mails.append((sub, hours))

    def set_enabled(self, sub, instance, enabled):
        self.read(sub, instance)
        self.users[sub]["enabled"] = enabled


m.people_kc.Realm = FakeRealm
m._people_realms.clear()
c = m.app.test_client()


def call(method, path, inst, body=None, src=None):
    h = {"X-OAAP-User": f"instance:{inst}", "X-OAAP-Source-Instance": src or inst}
    return getattr(c, method)(path, json=body, headers=h)


r = call("post", "/people/v1/invite", "inv1-bestand", {"email": "ina@example.org"}, src="inv1-other")
ok("Schluessel einer Instanz auf der Seite einer anderen -> 403", r.status_code == 403)
r = c.post("/people/v1/invite", json={"email": "ina@example.org"},
           headers={"X-OAAP-User": "joerg", "X-OAAP-Source-Instance": "inv1-bestand"})
ok("eine Person statt einer Instanz -> 403", r.status_code == 403)
r = call("post", "/people/v1/invite", "other-app", {"email": "x@example.org"})
ok("Mandant mit ausgeschaltetem Schalter -> 403 mit Grund",
   r.status_code == 403 and "server_admin" in r.get_json()["error"])
r = call("post", "/people/v1/invite", "inv1-bestand", {"email": "nicht-gueltig"})
ok("ungueltige Adresse -> 400", r.status_code == 400)
r = call("post", "/people/v1/invite", "inv1-bestand",
         {"email": "Ina@Example.org", "name": "Ina Probe", "ref": "node-17", "language": "de"})
d = r.get_json()
ok("Einladung: 201 mit UUID und Status invited", r.status_code == 201 and d["status"] == "invited"
   and len(d["user_id"]) == 36 and d["ref"] == "node-17", d)
rec = next(u for u in m.load_users() if u.get("id") == d["user_id"])
ok("der Datensatz steht VOR der ersten Anmeldung: partner, gebunden an (Provider, sub), eingeladen von",
   rec["roles"] == ["partner"] and rec["idp"]["subject"] == "sub-1"
   and rec["idp"]["provider"] == m.idp.provider_key(tenants[T]["idp"])
   and rec["invited_by"] == "instance:inv1-bestand" and rec["email_verified"] is False
   and rec["tenant"] == T and rec["password_hash"] == "", rec)
ok("die Mail ging einmal raus, 72 h", FakeRealm.mails == [("sub-1", 72)])
ok("die erste Anmeldung findet die Bindung (kein zweiter Datensatz)",
   m.idp.find_binding(m.load_users(), rec["idp"]["provider"], "sub-1", T)["id"] == rec["id"])
r = call("post", "/people/v1/invite", "inv1-bestand", {"email": "ina@EXAMPLE.org"})
ok("dieselbe Adresse noch einmal (andere Schreibweise) -> 409, nie verknuepft",
   r.status_code == 409 and "exists" in r.get_json()["error"])
r = call("get", "/people/v1?ref=node-17", "inv1-bestand")
ok("Lesen nach ref: genau diese Person", r.status_code == 200
   and [p["user_id"] for p in r.get_json()["people"]] == [d["user_id"]])
r = call("get", "/people/v1", "inv1-other")
ok("eine andere App sieht sie nicht", r.status_code == 200 and r.get_json()["people"] == [])
r = call("post", f"/people/v1/{d['user_id']}/lock", "inv1-other")
ok("eine andere App kann sie nicht sperren -> 404", r.status_code == 404)
r = call("post", f"/people/v1/{d['user_id']}/lock", "inv1-bestand")
rec = next(u for u in m.load_users() if u.get("id") == d["user_id"])
ok("sperren: im Realm aus, hier inaktiv, Sitzungen verfallen",
   r.status_code == 200 and r.get_json()["status"] == "locked" and FakeRealm.users["sub-1"]["enabled"] is False
   and rec["active"] is False and rec["session_epoch"] == 1)
r = call("post", f"/people/v1/{d['user_id']}/unlock", "inv1-bestand")
ok("entsperren", r.status_code == 200 and r.get_json()["status"] == "invited"
   and FakeRealm.users["sub-1"]["enabled"] is True)
r = call("post", f"/people/v1/{d['user_id']}/resend", "inv1-bestand")
ok("erneut senden", r.status_code == 200 and len(FakeRealm.mails) == 2)
r = call("post", f"/people/v1/{d['user_id']}/delete", "inv1-bestand")
ok("loeschen gibt es nicht -> 404", r.status_code == 404)
call("post", f"/people/v1/{d['user_id']}/resend", "inv1-bestand")
r = call("post", f"/people/v1/{d['user_id']}/resend", "inv1-bestand")
ok("Grenze je Adresse und Tag -> 429", r.status_code == 429 and "address" in r.get_json()["error"])
FakeRealm.users["sub-1"]["requiredActions"] = []
r = call("get", f"/people/v1/{d['user_id']}", "inv1-bestand")
ok("nach Bestaetigung: active", r.get_json()["status"] == "active")
m._save(m.PEOPLE_LIMITS_FILE, [])
r = call("post", f"/people/v1/{d['user_id']}/resend", "inv1-bestand")
ok("einer aktiven Person geht keine Einladung mehr -> 409 (dafuer: Passwort vergessen)",
   r.status_code == 409 and "forgot password" in r.get_json()["error"])
src = inspect.getsource(m._idp_principal)
ok("die Rollen-Synchronisation hebt eine eingeladene Person nie ueber partner",
   'u.get("invited_by")' in src and '("partner", "guest")' in src)

print("\n=== Der Knoten (appctl) ===")
sys.path.insert(0, PLATFORM)
import appctl as a                                             # noqa: E402
ok("people_wanted nur bei true", a.people_wanted({"people": {"invite": True}})
   and not a.people_wanted({"people": {"invite": "yes"}}) and not a.people_wanted({}))
vsrc = inspect.getsource(a.validate_manifest)
ok("validate_manifest prueft den Block", '"people" in m' in vsrc and "people.invite: true or false" in vsrc)
ok("Schluessel, Adresse, reserviert, Instanzschluessel",
   a.people_scope("x") == "oaap.people.invite:x" and a.people_url(8000).endswith(":8000/platform/people")
   and "OAAP_PEOPLE_KEY" in a.RESERVED_ENV and "OAAP_PEOPLE_URL" in a.RESERVED_ENV
   and "OAAP_PEOPLE_KEY" in a.INSTANCE_KEY_VARS and a.MANIFEST_MINOR >= 22)
src_all = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
door = src_all[src_all.index('if me.get("people_invite") and not login_only'):][:1500]
ok("die Tuer: identity fragt genau diesen Scope, Weg nach /people/v1, Quelle gesetzt, in `route`",
   "people_scope(scope" in door and "rewrite * /people/v1{uri}" in door
   and "reverse_proxy identity:8000" in door and "SOURCE_HEADERS" in door and '"\\t\\troute {"' in door)
ok("Format der Gateway-Zeilen >= 12", a.CASES_BLOCK_FORMAT >= 12)
tsrc = inspect.getsource(a.tenant_people_apply) + inspect.getsource(a._tenant_people_cli)
ok("der Schalter: nur als root, schreibt das Geheimnis in die 0600-Datei, setzt people_invite, Audit",
   "geteuid() != 0" in tsrc and "save_idp_secrets" in tsrc and "people_invite=bool(on)" in tsrc
   and 'audit_tenant("tenant.people-invite"' in tsrc)
import idp_admin                                               # noqa: E402
ok("Konnektor: Verb invited_users erklaert, users bleibt in never",
   "invited_users" in idp_admin.CONNECTOR_KINDS["keycloak"]["verbs"]
   and idp_admin.CONNECTOR_KINDS["keycloak"]["never"] == ("users",)
   and idp_admin.PEOPLE_ROLES == ("manage-users", "view-users", "query-groups"))
ok("ein vorhandener Client, der Personen anmeldet, wird nicht umgebogen",
   idp_admin.people_client_fix({"clientId": "oaap-people", "standardFlowEnabled": True})[0] != ""
   and idp_admin.people_client_fix({"enabled": True, "serviceAccountsEnabled": True}) == ("", None)
   and idp_admin.people_client_fix({"enabled": False, "serviceAccountsEnabled": True})[1]["enabled"] is True)

print()
print("all people checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
