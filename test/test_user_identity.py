#!/usr/bin/env python3
"""RFC-0040: the person behind the name.

RFC-0026 settled it for this platform: identity is a UUID, and every
name a human reads is an alias that may change. Instances and tenants
follow that. The user record was the one place it was never applied --
`X-OAAP-User` carries the login name, and the login name IS the key.

This file defends the five things that can go wrong with fixing that:

    THE IDENTITY MUST BE THERE, ALWAYS, AND MUST NOT MOVE. A record
    without an `id` is not an error anybody sees; it is an app
    anchoring its permissions on an empty string. So: existing records
    get one at startup, every creation path gives one, no request can
    change one, and a restart does not renumber anybody.

    THE HEADER LIST MOVED, AND IT MOVED IN THREE KINDS OF PLACE.
    copy_headers (a header missing there never reaches the app), the
    strip blocks (a header missing there is the anti-spoofing hole --
    the app believes a name the visitor typed), and the access log's
    filter (a header missing there writes a person's name and e-mail
    into a file that is not in the backup). RFC-0039 taught this the
    hard way: its own inventory said five places and there were nine.
    So this counts them instead of trusting a list.

    AN UNPROVEN ADDRESS MUST NOT LOOK PROVEN (D2). An address in a
    platform header is read as verified whatever flag stands beside it,
    so an unverified one is not handed over at all -- and a CHANGED
    address drops its old assertion, because nobody proved the new one.

    A NAME LIKE "Jörg Müller" MUST SURVIVE THE WIRE (D4). Arbitrary
    Unicode in an HTTP header does not fail cleanly: it is mojibake in
    one app and a dropped header in another, found in production.

    THE RETURN TARGET MUST NOT BECOME AN OPEN REDIRECT (D5). A login
    page that sends the visitor to somebody else's site AFTER they
    signed in is where a convincing phishing page belongs.

Run: python3 test/test_user_identity.py
The identity half needs flask + werkzeug (as the service does). If they
are not installed, that section reports SKIP rather than a false PASS.
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.join(HERE, "..", "platform")
IDENTITY_DIR = os.path.join(PLATFORM_DIR, "services", "identity")
PORTAL_DIR = os.path.join(PLATFORM_DIR, "services", "portal")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


print("Die fuenf Kopfzeilen stehen an EINER Stelle -- und alle Stellen "
      "leiten sich davon ab")

sys.path.insert(0, PLATFORM_DIR)
import appctl  # noqa: E402

NEW = ("X-OAAP-User-Id", "X-OAAP-Display-Name", "X-OAAP-Email")
ok("die Liste nennt die zwei alten und die drei neuen",
   appctl.IDENTITY_HEADERS == ("X-OAAP-User", "X-OAAP-Roles") + NEW,
   appctl.IDENTITY_HEADERS)

appctl_src = read(PLATFORM_DIR, "appctl.py")
ok("kein Generator schreibt die Kopfzeilen noch von Hand",
   'copy_headers X-OAAP-User X-OAAP-Roles"' not in appctl_src
   and 'request_header -X-OAAP-User")' not in appctl_src,
   "sonst gibt es wieder eine zweite Liste, die man vergessen kann")

# --- 1. copy_headers: was die App ueberhaupt erreicht ---------------------
route = [{"path": "/", "roles": ["user"]}]
body = "\n".join(appctl.site_body(route, "c", 8000, scope="i", tenant="t"))
for h in appctl.IDENTITY_HEADERS:
    ok(f"copy_headers auf der App-Route nennt {h}",
       h in body.split("copy_headers")[1].split("\n")[0], body)

portal_body = "\n".join(appctl._portal_site_body())
for h in appctl.IDENTITY_HEADERS:
    ok(f"copy_headers am Portal-Apex nennt {h}",
       h in portal_body.split("copy_headers")[1].split("\n")[0])

caddyfile = read(PLATFORM_DIR, "Caddyfile")
copy_lines = [l for l in caddyfile.splitlines() if "copy_headers" in l]
ok("der feste Caddyfile hat drei copy_headers-Zeilen", len(copy_lines) == 3,
   copy_lines)
ok("und jede nennt alle fuenf",
   all(all(h in l for h in appctl.IDENTITY_HEADERS) for l in copy_lines),
   copy_lines)

# --- 2. die Stripp-Bloecke: das Anti-Spoofing-Versprechen -----------------
public = "\n".join(appctl.site_body(
    [{"path": "/", "roles": ["public"]}], "c", 8000, scope="i",
    throttle={"limit": 300, "window": 60}))
for h in appctl.IDENTITY_HEADERS:
    ok(f"eine public-Route entfernt ein mitgeschicktes {h}",
       f"request_header -{h}" in public,
       "sonst glaubt die App einen Namen, den der Besucher getippt hat")

ok("der /auth/*-Block jeder Instanz entfernt alle fuenf",
   all(f"request_header -{h}" in body for h in appctl.IDENTITY_HEADERS), body)

# Zeilenweise, nicht per Teilstring: "-X-OAAP-User" steckt auch in
# "-X-OAAP-User-Id", und ein Zaehler, der sich selbst mitzaehlt, ist
# ein Test, der immer gruen ist.
stripped = [l.strip().removeprefix("request_header -")
            for l in caddyfile.splitlines()
            if l.strip().startswith("request_header -X-OAAP-")]
# Sechs seit 0.1.129: /connect/tunnel (oaap.net.connector) ist die
# sechste oeffentliche Route. Sieben seit 0.1.131: /connect/client, der
# Laptop-Client der Freigaben (oaap.net.connector 2.8.5). Die Zahl steht
# hier, damit eine weitere Route diesen Test anfassen muss -- und dabei
# jede Stelle mit allen fuenf Kopfzeilen.
PUBLIC_ROUTES = 7
ok(f"der feste Caddyfile strippt an {PUBLIC_ROUTES} Stellen",
   len(stripped) == PUBLIC_ROUTES * len(appctl.IDENTITY_HEADERS),
   f"{len(stripped)} Zeilen: {sorted(set(stripped))}")
for h in appctl.IDENTITY_HEADERS:
    ok(f"und jede dieser Stellen nennt {h}",
       stripped.count(h) == PUBLIC_ROUTES, stripped.count(h))

# --- 3. das Zugriffsprotokoll: Name und Adresse gehoeren nicht hinein -----
logf = "\n".join(appctl._log_filter("\t"))
for h in appctl.IDENTITY_HEADERS:
    canon = h.replace("X-OAAP-", "X-Oaap-")
    ok(f"der Protokollfilter loescht {canon}",
       f"request>headers>{canon} delete" in logf, logf)

rec = {"request": {"uri": "/x?t=1",
                   "headers": {"X-Oaap-User": ["joerg"],
                               "X-Oaap-Roles": ["user"],
                               "X-Oaap-User-Id": ["abc"],
                               "X-Oaap-Display-Name": ["J%C3%B6rg"],
                               "X-Oaap-Email": ["j@example.org"],
                               "Authorization": ["Bearer oaapk_x"]}}}
appctl._scrub_record(rec)
left = sorted(rec["request"]["headers"])
ok("der Nachreiniger alter Protokolle raeumt dieselben fuenf",
   left == ["Authorization"], left)

# --- 4. die Test-Spiegelung von copy_headers ------------------------------
twin_test = read(HERE, "test_data_twin.py")
ok("test_data_twin prueft copy_headers nicht mehr gegen eine feste Liste",
   'copy_headers X-OAAP-User X-OAAP-Roles"' not in twin_test
   or "IDENTITY_HEADERS" in twin_test,
   "sonst faellt dieser Test beim naechsten Zusatz, nicht die Luecke")


try:
    import flask  # noqa: F401
except ImportError:
    print("")
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(1 if fails else 0)

from werkzeug.security import generate_password_hash  # noqa: E402


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


PW = "geheim123"


def user(name, roles, **over):
    rec = {"username": name, "display_name": "", "kind": "human",
           "password_hash": generate_password_hash(PW), "roles": roles,
           "groups": [], "tenant": "", "active": True, "session_epoch": 0}
    rec.update(over)
    return rec


def seed(users):
    """A data directory as it looks BEFORE the update -- no ids at all --
    then load identity against it: the backfill runs at import, exactly
    as it does when the container starts."""
    d = tempfile.mkdtemp(prefix="oaap-rfc40-")
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
        json.dump({"setup_done": True, "server_admin_migrated": True,
                   "support_migrated": True}, f)
    return d, load_identity(d)


def stored(d):
    with open(os.path.join(d, "users.json"), encoding="utf-8") as f:
        return {u["username"]: u for u in json.load(f)}


print("")
print("Jeder Bestandsbenutzer bekommt eine Kennung -- beim ersten Start")

d, m = seed([user("joerg", ["server_admin", "admin"]),
             user("bernd", ["user"], display_name="Bernd"),
             user("alt", ["user"], active=False)])
after = stored(d)
ok("alle drei haben eine Kennung",
   all(len(u["id"]) == 36 for u in after.values()), after)
ok("auch das inaktive Konto",
   len(after["alt"]["id"]) == 36,
   "sonst bekommt es beim Reaktivieren eine neue und alle Freigaben "
   "darauf zeigen ins Leere")
ok("die Kennungen sind verschieden",
   len({u["id"] for u in after.values()}) == 3)
ok("das E-Mail-Feld existiert und ist leer und ungeprueft",
   all(u["email"] == "" and u["email_verified"] is False
       for u in after.values()))

print("")
print("Und sie bleibt -- ueber Neustarts, Aenderungen und Abmeldungen")

before = {n: u["id"] for n, u in after.items()}
load_identity(d)
ok("ein zweiter Start vergibt nichts neu",
   {n: u["id"] for n, u in stored(d).items()} == before,
   "sonst wandert die Kennung, auf die eine App ihre Rechte gehaengt hat")

m = load_identity(d)
c = m.app.test_client()
c.post("/auth/login", data={"username": "bernd", "password": PW})
c.post("/auth/profile", data={"display_name": "Bernd B."})
c.post("/auth/password", data={"current": PW, "new": "neupasswort"})
ok("Anzeigename und Passwort aendern lassen sie unberuehrt",
   stored(d)["bernd"]["id"] == before["bernd"], stored(d)["bernd"])

print("")
print("Die Kennung ist kein Feld, das jemand setzen kann")

H = {m.INTERNAL_HEADER: "test-internal-key"}
r = c.put("/internal/users/bernd", headers=H,
          json={"actor": "joerg", "roles": ["user"], "active": True,
                "id": "00000000-0000-0000-0000-000000000000"})
ok("ein 'id' im Aenderungsauftrag wird nicht uebernommen",
   r.status_code == 200 and stored(d)["bernd"]["id"] == before["bernd"],
   stored(d)["bernd"]["id"])

r = c.post("/internal/users", headers=H,
           json={"actor": "joerg", "username": "neu", "password": PW,
                 "roles": ["user"],
                 "id": "00000000-0000-0000-0000-000000000000"})
ok("und auch nicht beim Anlegen",
   r.status_code == 201
   and stored(d)["neu"]["id"] != "00000000-0000-0000-0000-000000000000",
   stored(d)["neu"]["id"])
ok("ein neu angelegter Benutzer hat trotzdem eine",
   len(stored(d)["neu"]["id"]) == 36)

print("")
print("Der Maschinen-Prinzipal ist ein Benutzer und bekommt eine wie alle")

r = c.post("/internal/users", headers=H,
           json={"actor": "joerg", "username": "leser-1", "kind": "machine",
                 "roles": ["user"]})
ok("eine Maschine bekommt eine Kennung",
   r.status_code == 201 and len(stored(d)["leser-1"]["id"]) == 36)
ok("save_users() vergibt sie auch dort, wo ein Pfad sie vergessen hat",
   True)
with m.users_rw() as us:
    us.append({"username": "handgemacht", "display_name": "", "roles": ["user"],
               "groups": [], "tenant": "", "active": True, "kind": "machine",
               "password_hash": ""})
    m.save_users(us)
ok("ein Satz ohne 'id' ueberlebt keinen Schreibvorgang",
   len(stored(d)["handgemacht"]["id"]) == 36,
   "das ist die Absicherung gegen den fuenften Anlegepfad, den niemand "
   "kennt -- viermal hat genau das hier schon zugeschlagen")

print("")
print("Die fuenf Kopfzeilen kommen aus /verify -- immer alle fuenf")

cb = m.app.test_client()
cb.post("/auth/login", data={"username": "joerg", "password": PW})
r = cb.get("/verify")
got = {k: v for k, v in r.headers if k.lower().startswith("x-oaap")}
ok("alle fuenf sind da", sorted(got) == sorted(appctl.IDENTITY_HEADERS), got)
ok("die Kennung ist die gespeicherte",
   got["X-OAAP-User-Id"] == stored(d)["joerg"]["id"])
ok("X-OAAP-User ist unveraendert der Anmeldename",
   got["X-OAAP-User"] == "joerg",
   "RFC-0040 D3: alles andere bricht laufende Apps fuer Kosmetik")
ok("eine leere Adresse ist leer, nicht abwesend",
   got["X-OAAP-Email"] == "",
   "eine fehlende Kopfzeile hat nichts, was einen mitgeschickten Wert "
   "ueberschreibt -- genau das Loch, das copy_headers schliessen soll")

print("")
print("D2: eine ungepruefte Adresse bekommt keine App zu sehen")

c.put("/internal/users/joerg", headers=H,
      json={"actor": "joerg", "roles": ["server_admin", "admin"],
            "active": True, "email": "joerg@beispiel.de"})
ok("eingetragen, aber ungeprueft",
   stored(d)["joerg"]["email"] == "joerg@beispiel.de"
   and stored(d)["joerg"]["email_verified"] is False,
   "eine Adresse, die ein Admin tippt, ist damit nicht bewiesen")
ok("und sie steht NICHT in der Kopfzeile",
   cb.get("/verify").headers["X-OAAP-Email"] == "")
ok("auch whoami gibt sie nicht heraus",
   cb.get("/auth/whoami").get_json()["email"] == "")

c.put("/internal/users/joerg", headers=H,
      json={"actor": "joerg", "roles": ["server_admin", "admin"],
            "active": True, "email": "joerg@beispiel.de",
            "email_verified": True})
ok("als geprueft behauptet, steht sie drin",
   cb.get("/verify").headers["X-OAAP-Email"] == "joerg@beispiel.de")

c.put("/internal/users/joerg", headers=H,
      json={"actor": "joerg", "roles": ["server_admin", "admin"],
            "active": True, "email": "andere@beispiel.de",
            "email_verified": True})
ok("eine GEAENDERTE Adresse verliert die Behauptung, auch mit Haekchen",
   stored(d)["joerg"]["email_verified"] is False
   and cb.get("/verify").headers["X-OAAP-Email"] == "",
   "sonst tragt ein stehengebliebenes Haekchen die alte Zusicherung "
   "auf eine Adresse, die niemand geprueft hat")

r = c.put("/internal/users/joerg", headers=H,
          json={"actor": "joerg", "roles": ["server_admin", "admin"],
                "active": True, "email": "kein-at-zeichen"})
ok("was keine Adresse ist, wird abgelehnt statt gespeichert",
   r.status_code == 400 and stored(d)["joerg"]["email"] == "andere@beispiel.de",
   r.get_json())

print("")
print("D4: ein Name wie „Jörg Müller\" uebersteht die Leitung")

ok("reines ASCII bleibt lesbar", m._header_value("Bernd") == "Bernd")
ok("Umlaute werden UTF-8-prozentkodiert",
   m._header_value("Jörg Müller") == "J%C3%B6rg%20M%C3%BCller")
ok("ein Prozentzeichen wird ebenfalls kodiert",
   m._header_value("100% sicher") == "100%25%20sicher",
   "sonst ist 'immer prozentdekodieren' keine Regel, die eine App "
   "blind anwenden kann")
ok("und die Kopfzeile traegt nur noch ASCII",
   all(ord(ch) < 128
       for ch in m.identity_headers(
           dict(stored(d)["joerg"], display_name="Jörg Müller",
                roles=["user"]))["X-OAAP-Display-Name"]))

print("")
print("D5: der tiefe Link uebersteht die Anmeldung -- und nichts weiter")

fresh = m.app.test_client()
r = fresh.get("/verify", headers={"X-Forwarded-Uri": "/einladung?tok=abc"})
ok("die Abweisung nennt das Ziel",
   r.status_code == 303
   and r.headers["Location"] == "/auth/login?next=%2Feinladung%3Ftok%3Dabc",
   r.headers.get("Location"))
ok("das Anmeldeformular traegt es als verstecktes Feld",
   'name="next" value="/einladung?tok=abc"'
   in fresh.get("/auth/login?next=%2Feinladung%3Ftok%3Dabc").get_data(as_text=True))
page = fresh.get("/auth/login").get_data(as_text=True)
ok("ohne Ziel gibt es das Feld gar nicht", 'name="next"' not in page)
# Das Ziel kommt von aussen und landet in HTML -- die zweite Gefahr
# neben dem offenen Umleiten.
page = fresh.get('/auth/login?next=%2Fx%3Fa%3D%22%3E%3Cscript%3E').get_data(as_text=True)
ok("und es wird maskiert, nicht eingebaut",
   "<script>" not in page and "&lt;script&gt;" in page, page[-600:])
r = fresh.post("/auth/login",
               data={"username": "joerg", "password": PW,
                     "next": "/einladung?tok=abc"})
ok("und nach der Anmeldung landet man dort, mit Query",
   r.headers["Location"] == "/einladung?tok=abc", r.headers.get("Location"))

BAD = ("//evil.example", "/\\evil.example", "https://evil.example/",
       "http:/evil", "evil.example", "/x\r\nLocation: http://evil",
       "/" + "x" * 600)
for bad in BAD:
    t = m.app.test_client()
    r = t.post("/auth/login",
               data={"username": "joerg", "password": PW, "next": bad})
    ok(f"ein fremdes Ziel wird verworfen: {bad[:30]!r}",
       r.headers["Location"] == "/", r.headers.get("Location"))
    if "\r" in bad or "\n" in bad:
        # Der Weg ueber die Kopfzeile laesst sich hier nicht bauen:
        # werkzeug weigert sich, eine Kopfzeile mit Zeilenumbruch
        # ueberhaupt zu erzeugen -- was den Punkt schon macht. Die Regel
        # selbst wird deshalb direkt geprueft.
        ok(f"   und schon die Regel weist es ab: {bad[:20]!r}",
           m._return_target(bad) == "")
        continue
    r = t.get("/verify", headers={"X-Forwarded-Uri": bad})
    ok(f"   auch schon auf dem Weg zur Anmeldung: {bad[:30]!r}",
       "evil" not in (r.headers.get("Location") or "")
       and len(r.headers.get("Location") or "") < 200,
       r.headers.get("Location"))

t = m.app.test_client()
for loop in ("/auth/login", "/auth/logout", "/verify", "/throttle"):
    r = t.get("/verify", headers={"X-Forwarded-Uri": loop})
    ok(f"kein Rueckkehrziel in die eigene Mechanik: {loop}",
       r.headers["Location"] == "/auth/login",
       "sonst zeigt der Sprung auf sich selbst -- und /verify ist der "
       "Notweg, falls das Gateway die Adresse einmal nicht mitschickt")
r = t.get("/verify", headers={"X-Forwarded-Uri": "/a,b?x=1,2"})
ok("ein Komma im Pfad wird nicht abgeschnitten",
   r.headers["Location"] == "/auth/login?next=%2Fa%2Cb%3Fx%3D1%2C2",
   r.headers.get("Location"))

print("")
print("Abmelden ist kein abgewiesener Aufruf und merkt sich nichts")

out = m.app.test_client()
out.post("/auth/login", data={"username": "joerg", "password": PW})
r = out.post("/auth/logout")
ok("nach dem Abmelden fuehrt der Weg ohne Ziel zur Anmeldung",
   r.headers["Location"] == "/auth/login", r.headers.get("Location"))

print("")
print("D6: die Benutzerdatei wird nicht mehr ohne Sperre geschrieben")

identity_src = read(IDENTITY_DIR, "app.py")
ok("es gibt eine Sperre und eine Schreibklammer darum",
   "_users_lock" in identity_src and "def users_rw" in identity_src)
ok("save_users() ist der einzige Schreiber der Benutzerdatei",
   identity_src.count("_save(USERS_FILE") == 2,
   "einer in save_users(), einer in der Ersteinrichtung -- jede weitere "
   "Stelle schreibt an der Sperre vorbei")
for fn in ("users_create", "users_update", "users_set_password",
           "profile_change", "password_change", "_revoke_sessions",
           "internal_setup"):
    seg = identity_src.split(f"def {fn}(")[1].split("\ndef ")[0]
    ok(f"{fn} schreibt unter der Sperre",
       "users_rw()" in seg or "_users_lock()" in seg, seg[:200])

ok("appctl schreibt Benutzer nur noch ueber save_users()",
   "m._save(m.USERS_FILE" not in appctl_src
   and appctl_src.count("m.users_rw()") >= 3,
   "die beiden Anlegepfade auf der Maschine (Twin-Prinzipal, "
   "'oaap machine add') und das Passwort-Zurucksetzen")

print("")
print("Das Portal zeigt Kennung und Adresse und luegt nicht ueber das Haekchen")

portal_src = read(PORTAL_DIR, "app.py")
ok("die Benutzerseite zeigt die Kennung", "u.id" in portal_src)
ok("sie hat ein Adressfeld", 'name="email"' in portal_src)
ok("und ein Haekchen fuer geprueft", 'name="email_verified"' in portal_src)
ok("beide werden an identity weitergegeben",
   '"email": email' in portal_src and '"email_verified": asserted' in portal_src)
ok("eine nicht uebernommene Behauptung wird gesagt, nicht verschluckt",
   "NICHT" in portal_src.split("asserted and done is not None")[1][:300],
   "sonst verlaesst der Admin die Seite im Glauben, die Adresse sei "
   "geprueft")

print("")
print("Die Dateien, die vor der Liste geschrieben wurden (0.1.108)")

# Jede Stelle aus der Konstante abzuleiten reicht nicht: eine Site-Datei
# wird EINMAL geschrieben, beim Ausrollen, und dann behalten. Auf
# oaap-test standen nach dem Update auf 0.1.107 13 von 13 App-Dateien
# weiter auf zwei Kopfzeilen. Das ist nicht nur eine fehlende Funktion --
# eine Kopfzeile, die die Datei weder strippt noch kopiert, geht
# ungeprueft an die App durch.
current = "\n".join(appctl.site_body(route, "c", 8000, scope="i", tenant="t"))
ok("eine frisch erzeugte Datei gilt als aktuell",
   not appctl._site_identity_stale(current),
   "sonst schreibt der Schritt bei jedem Update alles neu")

old = current
for h in NEW:
    old = "\n".join(l for l in old.splitlines()
                    if l.strip() != f"request_header -{h}")
old = old.replace(appctl._COPY_IDENTITY, "copy_headers X-OAAP-User X-OAAP-Roles")
ok("eine Datei im Stand vor 0.1.107 gilt als alt",
   appctl._site_identity_stale(old), old)

ok("auch wenn nur die copy_headers-Zeile alt ist",
   appctl._site_identity_stale(
       current.replace(appctl._COPY_IDENTITY,
                       "copy_headers X-OAAP-User X-OAAP-Roles")))

# Der Prefix-Fall: "request_header -X-OAAP-User" ist Anfang von
# "request_header -X-OAAP-User-Id". Ein Teilstring-Test haette eine
# Datei, die NUR die laengere nennt, fuer vollstaendig gehalten.
ok("eine Datei, die nur die laengere Kopfzeile nennt, gilt als alt",
   appctl._site_identity_stale("\trequest_header -X-OAAP-User-Id"),
   "sonst prueft der Test einen Namen, der sich selbst mitzaehlt")

ok("eine Datei ganz ohne Identitaets-Zeilen ist nicht alt",
   not appctl._site_identity_stale(
       "https://x {\n\ttls {\n\t\ton_demand\n\t}\n\treverse_proxy y:80\n}"),
   "edge.caddy leitet fremde Namen weiter und fuehrt keine")

mig_fn = appctl_src.split("def cmd_migrate_identity_headers")[1].split("\ndef ")[0]
ok("der Schritt schreibt die App-Dateien neu",
   "write_app_caddy(name, inst)" in mig_fn)
ok("und die aus der Registry erzeugten mit",
   "refresh_generated_sites()" in mig_fn
   and "external.caddy" in mig_fn and "instance-addresses.caddy" in mig_fn)
ok("und laedt das Gateway danach neu", "reload_gateway()" in mig_fn)
ok("er schweigt, wenn es nichts zu tragen gibt",
   "if not stale and not generated:" in mig_fn and "return" in mig_fn,
   "ein Schritt, der jedes Mal etwas sagt, wird nicht mehr gelesen")

ok("migrate.sh ruft ihn auf",
   "migrate-identity-headers" in read(PLATFORM_DIR, "migrate.sh"),
   "sonst laeuft er auf keinem Knoten")
ok("und appctl kennt ihn als Unterbefehl",
   'sub.add_parser("migrate-identity-headers"' in appctl_src)

print("")
print("Der Vertrag und die Spezifikation sagen es den Apps")

SPEC = os.path.join(HERE, "..", "..", "oaap-spec")
if os.path.isdir(SPEC):
    contract = read(SPEC, "docs", "app-deployment-contract.md")
    for h in NEW:
        ok(f"der Deployment Contract nennt {h}", h in contract)
    ok("und sagt, worauf eine App verankert",
       "X-OAAP-User-Id" in contract and "anchor" in contract.lower())
    ispec = read(SPEC, "spec", "oaap.core.identity.md")
    for h in NEW:
        ok(f"oaap.core.identity nennt {h}", h in ispec)
    ok("die Rueckkehr nach der Anmeldung steht in der Spezifikation",
       "return target" in ispec.lower())
else:
    print("SKIP  oaap-spec liegt hier nicht daneben")

print("")
print("ALLE BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
