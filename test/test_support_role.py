#!/usr/bin/env python3
"""RFC-0039: the node-wide half of `partner` becomes its own role.

`partner` meant two things. RFC-0002 published it as "external partner
organization" -- an app-facing classification. Everything built since
used it for the service provider who looks after the node, and gave it
a node-wide read: the portal health page, which names every instance on
the machine, across tenants. The unsafe meaning was the one nobody
wrote down, and an outside project (oaap-hbsha) walked into it.

The privilege moved out to `support`. This file defends the three
things that can go wrong with that:

    THE ROLE LIST MOVED EVERYWHERE AT ONCE. It is written out in NINE
    places -- identity, portal, appctl, the app schema, the store
    schema, FleetView's manifest, FleetView's own guard, Studio's
    packager and the store editor's checker. A role that exists in
    eight of them is a role that installs and then refuses, or packages
    and then fails validation, or passes the node and fails the tool a
    developer actually uses. (RFC-0039 §4 listed five at acceptance;
    four more turned up while building it, which is why this check
    counts them rather than trusting a list.)

    THE MIGRATION IS EXACTLY RFC-0008's. Every `partner` holder also
    gets `support`, so nobody who looks after a node loses the health
    page on update. It runs once per installation, never strips
    `partner`, and is marked done on a fresh install so it never walks
    over a new node's users. The RFC-0008 migration it copies has no
    test; this one does, because it rewrites users.json on every node
    in the fleet at startup.

    THE BOUNDARY STILL HOLDS, AND THE RIGHT WAY ROUND. A tenant_admin
    may not grant `support` (it reaches past their tenant) but MAY now
    grant `partner` (it no longer reaches anywhere) -- that second half
    is the whole point of the RFC, so it is checked as a positive, not
    only as a refusal.

Run: python3 test/test_support_role.py
The identity half needs flask + werkzeug (as the service does). If they
are not installed, that section reports SKIP rather than a false PASS.
"""
import importlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
PLATFORM_DIR = os.path.join(HERE, "..", "platform")
IDENTITY_DIR = os.path.join(PLATFORM_DIR, "services", "identity")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


print("Die Rollenliste steht an neun Stellen -- alle neun zusammen")

identity_src = read(IDENTITY_DIR, "app.py")
portal_src = read(PLATFORM_DIR, "services", "portal", "app.py")
appctl_src = read(PLATFORM_DIR, "appctl.py")

ok("identity: support ist vergebbar",
   '"support"' in identity_src.split("ASSIGNABLE_ROLES = ")[1][:200])
ok("identity: support reicht ueber den Mandanten hinaus",
   '"support"' in identity_src.split("NODE_WIDE_ROLES = ")[1][:200])
ok("identity: partner reicht es NICHT mehr",
   '"partner"' not in identity_src.split("NODE_WIDE_ROLES = ")[1][:200],
   "sonst darf ein tenant_admin die Rolle weiterhin nicht vergeben, "
   "und genau das war der Zweck von RFC-0039")
ok("portal: support steht in ALL_ROLES",
   '"support"' in portal_src.split("ALL_ROLES = ")[1][:200])
ok("portal: und in NODE_WIDE_ROLES, partner nicht",
   '"support"' in portal_src.split("NODE_WIDE_ROLES = ")[1][:200]
   and '"partner"' not in portal_src.split("NODE_WIDE_ROLES = ")[1][:200])
ok("appctl: eine App darf eine Route auf support sperren",
   '"support"' in appctl_src.split("ROLES = {")[1][:120])

app_schema = json.loads(read(ROOT, "oaap-spec", "schema",
                             "oaap-app.schema.json"))


def find_role_enums(node, out):
    """Every 'roles' array enum in a schema, wherever it sits."""
    if isinstance(node, dict):
        if (node.get("type") == "array"
                and isinstance(node.get("items"), dict)
                and "enum" in node["items"]):
            out.append(node["items"]["enum"])
        for v in node.values():
            find_role_enums(v, out)
    elif isinstance(node, list):
        for v in node:
            find_role_enums(v, out)
    return out


app_enums = [e for e in find_role_enums(app_schema, []) if "guest" in e]
ok("App-Schema: die Routen-Enum kennt support",
   app_enums and all("support" in e for e in app_enums), app_enums)

store_schema = json.loads(read(ROOT, "oaap-spec", "schema",
                               "oaap-store.schema.json"))
store_enums = [e for e in find_role_enums(store_schema, []) if "guest" in e]
ok("Store-Schema: die erzeugte Rollenliste kennt support ebenfalls",
   store_enums and all("support" in e for e in store_enums),
   f"{store_enums} -- sonst installiert eine App mit support sauber "
   "und faellt erst beim Veroeffentlichen durch")

fleetview_yaml = read(ROOT, "oaap-apps", "apps", "fleetview", "oaap-app.yaml")
fleetview_src = read(ROOT, "oaap-apps", "apps", "fleetview", "app.py")
ok("FleetView-Manifest: die Route steht auf [admin, support]",
   "roles: [admin, support]" in fleetview_yaml)
ok("FleetView prueft im eigenen Code dieselbe Rolle",
   '{"admin", "support"}' in fleetview_src,
   "die App prueft zusaetzlich zum Gateway -- stuende hier noch "
   "partner, liesse das Tor jemanden durch, den die App dann abweist")

# Studio und der Store-Editor pruefen Manifeste mit EIGENEN Rollenlisten,
# bevor ein Knoten sie je sieht. Studio ist produktiv; eine App mit
# `support` waere dort durchgefallen, obwohl der Knoten sie annimmt --
# und der Entwickler haette der Plattform geglaubt, nicht dem Werkzeug.
studio_pkg = read(ROOT, "oaap-apps", "apps", "studio", "pkg.py")
ok("Studio laesst support im Manifest zu",
   '"support"' in studio_pkg.split("ROLES = (")[1][:160],
   "Studio prueft das Paket, bevor ein Knoten es sieht")
checker_src = read(ROOT, "oaap-apps", "apps", "store-editor", "checker.py")
ok("der Store-Editor ebenfalls",
   '"support"' in checker_src.split("ROLES = {")[1][:160])
studio_app = read(ROOT, "oaap-apps", "apps", "studio", "app.py")
ok("und Studio nennt die Rolle dem Entwickler, den es beraet",
   "`support`" in studio_app,
   "die Liste im Briefing ist das, was ein Entwickler fuer vollstaendig "
   "haelt")


print("")
print("Der Bestand: appctl.py laesst eine Route auf support zu")

sys.path.insert(0, PLATFORM_DIR)
import appctl  # noqa: E402

ok("support ist eine gueltige Manifest-Rolle", "support" in appctl.ROLES)
ok("server_admin bleibt es nicht (Plattformmacht geht Apps nichts an)",
   "server_admin" not in appctl.ROLES)
ok("partner bleibt gueltig -- RFC-0039 nimmt keine Rolle weg",
   "partner" in appctl.ROLES)


try:
    import flask  # noqa: F401
except ImportError:
    print("")
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(1 if fails else 0)


def load_identity(data_dir):
    os.environ["SESSION_SECRET"] = "test-session-secret"
    os.environ["SETUP_TOKEN"] = "test-setup-token"
    os.environ["INTERNAL_API_KEY"] = "test-internal-key"
    os.environ["OAAP_IDENTITY_DATA_DIR"] = data_dir
    sys.path.insert(0, IDENTITY_DIR)
    sys.modules.pop("app", None)
    m = importlib.reload(importlib.import_module("app"))
    m.USERS_FILE = os.path.join(data_dir, "users.json")
    m.STATE_FILE = os.path.join(data_dir, "state.json")
    return m


def user(name, roles, tenant="t-op", active=True):
    return {"username": name, "display_name": "", "password_hash": "",
            "kind": "human", "roles": roles, "groups": [],
            "tenant": tenant, "active": active, "session_epoch": 0}


def seed(users, state=None):
    """A data directory as it looks BEFORE the update, then load
    identity against it -- the migration runs at import, exactly as it
    does when the container starts."""
    d = tempfile.mkdtemp(prefix="oaap-support-test-")
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
        # Bewusst "is None" und nicht "or": ein leeres state.json ist
        # der frische Knoten, und der ist ein eigener Fall.
        json.dump({"setup_done": True, "server_admin_migrated": True}
                  if state is None else state, f)
    return d, load_identity(d)


def users_of(d):
    with open(os.path.join(d, "users.json"), encoding="utf-8") as f:
        return {u["username"]: u for u in json.load(f)}


def state_of(d):
    with open(os.path.join(d, "state.json"), encoding="utf-8") as f:
        return json.load(f)


print("")
print("Die Umstellung: wer den Knoten betreut, verliert nichts")

d, m = seed([
    user("dienstleister", ["partner", "user"]),
    user("betreiber", ["server_admin", "admin", "keyuser"]),
    user("sponsor", ["partner"], tenant="t-kunde"),
    user("ausgeschieden", ["partner"], active=False),
])
after = users_of(d)
ok("ein partner-Traeger bekommt support dazu",
   "support" in after["dienstleister"]["roles"], after["dienstleister"])
ok("und behaelt partner -- es wird keine Rolle weggenommen",
   "partner" in after["dienstleister"]["roles"])
ok("auch ein inaktives Konto wird mitgezogen",
   "support" in after["ausgeschieden"]["roles"],
   "sonst faellt der Zugang lautlos weg, wenn es wieder aktiviert wird")
ok("wer nie partner hatte, bekommt nichts",
   "support" not in after["betreiber"]["roles"], after["betreiber"])
ok("die uebrigen Rollen bleiben unangetastet",
   set(after["betreiber"]["roles"]) == {"server_admin", "admin", "keyuser"})
ok("die Umstellung ist als erledigt vermerkt",
   state_of(d).get("support_migrated") is True)

print("")
print("Sie laeuft genau einmal")

# Rolle von Hand wieder abgenommen -- ein Betreiber, der aufgeraeumt
# hat. Ein zweiter Start darf sie nicht zurueckgeben.
u = json.load(open(os.path.join(d, "users.json"), encoding="utf-8"))
for entry in u:
    entry["roles"] = [r for r in entry["roles"] if r != "support"]
with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
    json.dump(u, f)
load_identity(d)
ok("ein zweiter Start gibt support NICHT erneut",
   all("support" not in x["roles"] for x in users_of(d).values()),
   "sonst kann der Betreiber die Aufraeumaufgabe aus der "
   "Freigabemitteilung nie abschliessen")

print("")
print("Ein frischer Knoten wird nicht angefasst")

d2, m2 = seed([], state={})
c = m2.app.test_client()
r = c.post("/internal/setup", json={"token": "test-setup-token",
                                    "username": "chef",
                                    "password": "geheim12345"},
           headers={"X-OAAP-Internal-Key": "test-internal-key"})
ok("die Ersteinrichtung geht durch", r.status_code == 201, r.get_json())
first = users_of(d2)["chef"]
ok("der erste Benutzer bekommt weder partner noch support",
   "partner" not in first["roles"] and "support" not in first["roles"],
   f"{first['roles']} -- ein server_admin sieht ohnehin alles")
ok("und die Umstellung gilt als erledigt, nicht als ausstehend",
   state_of(d2).get("support_migrated") is True,
   "sonst laeuft sie beim naechsten Start ueber einen neuen Knoten")

print("")
print("Die Mandantengrenze: support bleibt draussen, partner darf rein")

d3, m3 = seed([
    user("chefin", ["server_admin", "admin"]),
    user("madmin", ["tenant_admin", "admin"], tenant="t-kunde"),
])
# Ein Knoten mit zwei Mandanten: dem des Betreibers und einem Kunden.
# Ohne diese Datei gilt jeder benannte Mandant als unbekannt, und die
# Pruefung unten wuerde aus dem falschen Grund gruen oder rot.
TENANTS = os.path.join(d3, "tenants.json")
with open(TENANTS, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-op": {"label": "default"},
                           "t-kunde": {"label": "k7f3"}}}, f)
m3.TENANTS_FILE = TENANTS
c3 = m3.app.test_client()
H = {"X-OAAP-Internal-Key": "test-internal-key"}


def create(actor, name, roles, tenant=""):
    return c3.post("/internal/users", json={
        "actor": actor, "username": name, "password": "geheim12345",
        "roles": roles, "groups": [], "tenant": tenant}, headers=H)


r = create("madmin", "neu-support", ["support", "user"])
ok("ein tenant_admin darf support nicht vergeben", r.status_code == 403,
   r.get_json())
ok("und die Absage nennt support, nicht mehr partner",
   "support" in (r.get_json() or {}).get("error", ""),
   (r.get_json() or {}).get("error"))

r = create("madmin", "sponsor-neu", ["partner", "user"])
ok("ABER partner darf er jetzt vergeben -- der Zweck des RFC",
   r.status_code == 201, r.get_json())
ok("und das Konto liegt in seinem eigenen Mandanten",
   users_of(d3)["sponsor-neu"]["tenant"] == "t-kunde")

r = create("chefin", "technikerin", ["support", "admin"], tenant="t-op")
ok("ein server_admin darf support vergeben", r.status_code == 201,
   r.get_json())
ok("die Rolle liegt so im Satz, wie sie vergeben wurde",
   "support" in users_of(d3)["technikerin"]["roles"])

print("")
print("Und sie wird an Apps durchgereicht wie jede andere")

# Ende zu Ende: anmelden und die Kopfzeilen lesen, die das Gateway an
# die App weiterreicht. Nur so ist bewiesen, dass support wirklich bei
# FleetView ankommt -- eine Quelltextprobe wuerde das nur behaupten.
r = c3.post("/auth/login", data={"username": "technikerin",
                                 "password": "geheim12345"})
ok("die Technikerin kann sich anmelden", r.status_code in (302, 303),
   r.status_code)
r = c3.get("/verify")
ok("verify laesst sie durch", r.status_code == 204, r.status_code)
roles_header = r.headers.get("X-OAAP-Roles", "")
ok("und support steht in der Kopfzeile fuer die App",
   "support" in roles_header.split(","), roles_header)

r = c3.get("/verify?roles=support")
ok("eine Route, die support verlangt, laesst sie durch",
   r.status_code == 204, r.status_code)

# Der Sponsor aus dem Kundenmandanten darf genau das NICHT.
c4 = m3.app.test_client()
c4.post("/auth/login", data={"username": "sponsor-neu",
                             "password": "geheim12345"})
r = c4.get("/verify?roles=support")
ok("ein partner kommt an einer support-Route nicht vorbei",
   r.status_code == 403, r.status_code)
r = c4.get("/verify?roles=partner")
ok("an seiner eigenen Rolle aber schon", r.status_code == 204,
   r.status_code)

print("")
print("Der Aufraeum-Hinweis: meldet sich, bis er nicht mehr noetig ist")

# Die Umstellung oben kann genau eine Sache NICHT: unterscheiden, ob
# ein bisheriger `partner` der Dienstleister war oder eine echte
# Fremdfirma. Nur ein Mensch weiss das, also fragt die Plattform bei
# jedem `oaap update` -- und hoert auf zu fragen, sobald niemand mehr
# beide Rollen haelt. Ein Hinweis, der nie verstummt, wird ignoriert;
# einer, der zu frueh verstummt, hinterlaesst falsche Konten.
sys.path.insert(0, PLATFORM_DIR)
try:
    import appctl
except Exception as e:                                   # pragma: no cover
    print(f"SKIP  appctl laesst sich nicht laden ({e})")
else:
    class _Args:
        pass

    def note_for(usernames):
        """Der Hinweis, wie er auf einem Knoten mit diesem Bestand faellt.

        `_identity_exec` laeuft sonst im Container; hier steht an seiner
        Stelle die Antwort, die er zurueckgaebe.
        """
        real = appctl._identity_exec
        appctl._identity_exec = lambda *a, **k: json.dumps(usernames)
        buf = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = buf
        try:
            appctl.cmd_support_cleanup_note(_Args())
        finally:
            sys.stdout = real_stdout
            appctl._identity_exec = real
        return buf.getvalue()

    ok("still, solange niemand beide Rollen haelt", note_for([]) == "",
       repr(note_for([])))

    text = note_for(["technikerin", "lieferant-mueller"])
    ok("meldet sich, sobald jemand beide haelt", text != "")
    ok("und nennt beide Konten beim Namen",
       "technikerin" in text and "lieferant-mueller" in text, text)
    # Ohne diesen Satz weiss der Betreiber zwar, DASS etwas offen ist,
    # aber nicht, welche der beiden Rollen er wem lassen soll.
    ok("sagt auch, woran man die beiden unterscheidet",
       "support" in text and "partner" in text
       and ("extern" in text.lower() or "external" in text.lower()), text)

print("")
print(f"{'FAILED' if fails else 'OK'} ({fails} Fehler)")
sys.exit(1 if fails else 0)
