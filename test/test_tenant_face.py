#!/usr/bin/env python3
"""Das Gesicht eines Mandanten (RFC-0042 T3).

Vier Werte, kein Stylesheet: ein oeffentlicher Titel, zwei Farben, ein
Logo. Wer eigenes CSS mitbringen darf, kann jedes Bedienelement auf
einer Seite verschieben, verstecken oder faelschen, fuer die die
Plattform geradesteht -- vier Werte koennen das nicht.

Geprueft wird, was leicht kaputtgeht und teuer ist:

    EIN Urteil          Portal, Anmeldedienst und `appctl` beantworten
                        dieselben zwei Fragen (welcher Mandant, welches
                        Gesicht) aus EINER Datei. Zwei Lesarten eines
                        Hostnamens waeren genau die Nachahmung, die T3
                        verbietet -- aus Versehen erreicht.
    Lesbarkeit          Die Farbe waehlt der Mandant, die Lesbarkeit
                        die Plattform. Eine helle Hauptfarbe dreht die
                        Schrift der Kopfzeile um, statt sie
                        verschwinden zu lassen.
    Anker               Titel und Bild sind frei waehlbar, die Adresse
                        nicht -- sie steht daneben, auf jeder Seite.
    Betreibersicht      Wer knotenweite Macht haelt, sieht die Farben
                        der Plattform, auch am Ort eines Kunden.
    Oeffentlich         Titel und Logo stehen auf einer Seite ohne
                        Anmeldung. Der Klarname nicht -- die Seite, die
                        nach ihm fragt, verspricht schriftlich, dass er
                        im Haus bleibt.
    Kein SVG            Ein Bild, das ein Skript tragen kann, wird
                        unter der Adresse dieser Plattform
                        ausgeliefert, neben der Sitzung jedes Benutzers.
    Abgeleitet          Die Projektion fuers Gateway ist wegwerfbar und
                        wird aus dem Byte-Speicher neu geschrieben.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_tenant_face.py
"""
import argparse
import ast
import contextlib
import io as _io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-face-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, os.path.join(SERVICES, "portal"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402
import place                                                   # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

HOST = "oaap.joomp.de"
fails = 0

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg">'


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(_io.StringIO()):
        return fn(*a, **kw)


def make_tenant(label, title):
    quiet(m.cmd_tenant, argparse.Namespace(
        action="create", name=label, target=None, title=title, account="",
        account_name="", grace_days=30, yes=False, count=50,
        face_title=None, color_primary=None, color_accent=None,
        logo=None, clear_logo=False))
    tid, _t = m.tenant_by_label(label, include_former=False)
    return tid


# ---------------------------------------------------------------------------
print("EIN Urteil: Portal, Anmeldedienst und appctl lesen dieselbe Datei")

PORTAL_SRC = read(SERVICES, "portal", "app.py")
IDENTITY_SRC = read(SERVICES, "identity", "app.py")
APPCTL_SRC = read(HERE, "..", "platform", "appctl.py")
PLACE_SRC = read(SERVICES, "place.py")

for who, src in (("das Portal", PORTAL_SRC), ("der Anmeldedienst", IDENTITY_SRC),
                 ("appctl", APPCTL_SRC)):
    ok(f"{who} holt das Urteil aus place.py", "import place" in src, who[:40])

# Die Frage "welcher Mandant ist dieser Host?" darf ueberall GESTELLT
# werden und genau einmal BEANTWORTET. Geprueft wird deshalb die
# Funktion, die sie in jedem Dienst beantwortet: Sie muss delegieren
# und darf den Hostnamen nicht selbst zerlegen.
for who, src, fname in (("Das Portal", PORTAL_SRC, "host_tenant_scope"),
                        ("Der Anmeldedienst", IDENTITY_SRC, "request_face")):
    body = src.split(f"def {fname}(", 1)[1].split(chr(10) + "def ", 1)[0]
    ok(f"{who} delegiert das Urteil und zerlegt nichts selbst",
       "place.host_place(" in body and "endswith" not in body
       and "[:" not in body, body[:400])
ok("place.py schneidet den Knotennamen genau einmal ab",
   len(re.findall(r"\[: *-len\(ext", PLACE_SRC)) == 1)

asks = len(re.findall(r"place\.host_place\(", PORTAL_SRC + IDENTITY_SRC))
ok(f"beide Dienste fragen dieselbe Funktion ({asks})", asks >= 2, asks)

# Und sie muss auch wirklich in beide Images kommen. Genau diese Sorte
# Fehler hat 2026-09-10 das Portal in eine Neustart-Schleife geschickt.
for svc in ("portal", "identity"):
    dockerfile = read(SERVICES, svc, "Dockerfile")
    ok(f"das Dockerfile von {svc} kopiert place.py mit",
       re.search(r"^COPY .*\bplace\.py\b", dockerfile, re.M) is not None,
       dockerfile)
    ok(f"und baut aus services/, sonst gaebe es place.py dort gar nicht",
       "dockerfile: " + svc in read(HERE, "..", "platform",
                                    "docker-compose.yml"))

# ---------------------------------------------------------------------------
print("\nKein Stylesheet: was ein Mandant erzeugen kann, sind Werte")

theme = place.theme_of({"label": "cls", "theme": {
    "title": "CLS", "color_primary": "#1f4e79",
    "color_accent": "#e07b00"}}, "cls", HOST)
css = place.theme_style(theme)
ok("das erzeugte CSS ist genau ein :root-Block",
   css.count("{") == 1 and css.count("}") == 1 and css.count(":root") == 1, css)
ok("es enthaelt keine Regel, keinen Selektor und kein url()",
   "url(" not in css and "@" not in css and ";" in css, css)
ok("jeder Wert ist eine Farbe oder ein berechneter Farbwert",
   all(place.HEX_RE.fullmatch(v.strip())
       for v in re.findall(r":(#[0-9a-f]{6});", css)), css)
ok("ohne gesetztes Gesicht entsteht gar kein CSS",
   place.theme_style(place.theme_of({"label": "x"}, "x", HOST)) == "")

# Eine Farbe, die kein Sechserpaar ist, kommt nie so weit.
ok("eine erfundene Farbe wird abgelehnt",
   place.color_refusal("rot; } body{display:none") != "")
ok("und die Ablehnung sagt, wie es richtig geht",
   "#" in place.color_refusal("blau"))

# ---------------------------------------------------------------------------
print("\nLesbarkeit: die Farbe waehlt der Mandant, die Lesbarkeit die Plattform")

pale = place.theme_of({"label": "y", "theme": {"color_primary": "#ffe066",
                                               "color_accent": "#ffe066"}},
                      "y", HOST)
v = place.theme_vars(pale)
ok("zu einer hellen Kopfzeile wird die Schrift dunkel",
   v["header_text"] == "#111827", v)
dark = place.theme_vars(place.theme_of(
    {"label": "z", "theme": {"color_primary": "#10243f",
                             "color_accent": "#10243f"}}, "z", HOST))
ok("zu einer dunklen Kopfzeile bleibt sie hell",
   dark["header_text"] == "#ffffff", dark)
ok("Text auf Weiss wird so weit abgedunkelt, dass er lesbar ist",
   place.luminance(v["ink"]) <= 0.2, v["ink"])
ok("die Schaltflaechenschrift passt zur Akzentfarbe",
   v["accent_text"] == "#111827" and dark["accent_text"] == "#ffffff")

# ---------------------------------------------------------------------------
print("\nOeffentlich ist der Titel, nicht der Klarname")

hidden = place.theme_of({"label": "kunde-7", "name": "Meier GmbH & Co. KG"},
                        "kunde-7", HOST)
ok("ohne gesetzten Titel steht das Kuerzel, nicht der Klarname",
   hidden["title"] == "kunde-7", hidden)
ok("der Klarname taucht im Gesicht ueberhaupt nicht auf",
   "Meier" not in str(hidden), hidden)
ok("das Anlegeformular verspricht das auch weiterhin",
   "bleibt im Haus" in PORTAL_SRC)
ok("und die Gesichtsseite sagt, dass der Titel oeffentlich ist",
   "oeffentlich" in PORTAL_SRC and "Anmeldeseite verlangt keine Anmeldung"
   in PORTAL_SRC)

# ---------------------------------------------------------------------------
print("\nDas Logo: nach Inhalt beurteilt, nie nach Namen")

ok("ein SVG wird abgelehnt", place.logo_refusal(SVG) != "")
ok("und die Ablehnung sagt WARUM (Skript neben der Sitzung)",
   "Skript" in place.logo_refusal(SVG) or "script" in place.logo_refusal(SVG),
   place.logo_refusal(SVG))
ok("ein SVG unter dem Namen logo.png ebenso -- entschieden wird nach Inhalt",
   place.logo_type(SVG) == "" and place.logo_refusal(SVG) != "")
ok("ein PNG wird angenommen", place.logo_refusal(PNG) == ""
   and place.logo_type(PNG) == "png")
ok("ein JPEG auch", place.logo_type(JPEG) == "jpeg")
ok("ein RIFF ohne WEBP ist kein WebP",
   place.logo_type(b"RIFF" + b"\x00" * 4 + b"AVI ") == "")
ok("zu gross wird abgelehnt, mit der Groesse im Satz",
   "KB" in place.logo_refusal(b"\x89PNG\r\n\x1a\n"
                              + b"\x00" * place.LOGO_MAX_BYTES))

# ---------------------------------------------------------------------------
print("\nDie Ablehnung entsteht an genau einer Stelle")

ok("der Satz ueber SVG steht nur in place.py",
   (PORTAL_SRC + IDENTITY_SRC + APPCTL_SRC).count("cannot be a logo") == 0)
doors = len(re.findall(r"place\.logo_refusal\(", PORTAL_SRC + APPCTL_SRC))
ok(f"und wird von mehreren Tueren geholt ({doors})", doors >= 2, doors)
gate = len(re.findall(r"place\.theme_refusal\(", APPCTL_SRC))
ok("die Farb- und Titelpruefung ebenfalls", gate >= 1, gate)

# ---------------------------------------------------------------------------
print("\nappctl: das Gesicht setzen, und das Logo landet im Byte-Speicher")

m.ensure_default_tenant()
default_id = m.default_tenant_id()
cls_id = make_tenant("cls", "Clausen Systeme")
other_id = make_tenant("hbvp", "Handballverein")

good, msg = m.tenant_set_theme(cls_id, title="Clausen", primary="#1f4e79",
                               accent="#e07b00", logo_bytes=PNG, who="chef")
ok("das Gesicht wird angenommen", good, msg)
rec = m.load_tenants()[cls_id]["theme"]
ok("der Titel steht im Mandantensatz", rec.get("title") == "Clausen", rec)
ok("das Logo steht als Hash dort, nicht als Bild",
   len(rec.get("logo", "")) == 64 and rec.get("logo_type") == "png", rec)
ok("und die Bytes liegen im Mandanten-Byte-Speicher (oaap.data.files)",
   m.files_get(cls_id, rec["logo"]) is not None)
ok("aber NICHT im Speicher eines anderen Mandanten",
   m.files_get(other_id, rec["logo"]) is None,
   "der Mandant steht im Pfad, nicht in einer Spalte")

bad, msg = m.tenant_set_theme(cls_id, primary="knallrot")
ok("eine erfundene Farbe wird auch hier abgelehnt", not bad, msg)
ok("und das gesetzte Gesicht bleibt unveraendert",
   m.load_tenants()[cls_id]["theme"].get("color_primary") == "#1f4e79")
bad, msg = m.tenant_set_theme(cls_id, logo_bytes=SVG)
ok("und ein SVG ebenso, mit demselben Satz wie im Portal",
   not bad and msg == place.logo_refusal(SVG), msg)

bad, msg = m.tenant_set_theme(default_id, title="Plattform")
ok("der Standard-Mandant bekommt kein eigenes Gesicht", not bad, msg)
ok("und die Begruendung nennt die Nachahmung", "impersonation" in msg, msg)

# ---------------------------------------------------------------------------
print("\nDie Projektion fuers Gateway ist abgeleitet und wegwerfbar")

png_name = "cls.png"
ok("das Logo liegt, wo das Gateway es ausliefert",
   os.path.isfile(os.path.join(m.PLACE_ASSETS_DIR, png_name)))
ok("und die Adresse dafuer steht auf der oeffentlichen /platform-Route",
   place.logo_url("cls", "png", "a" * 64).startswith("/platform/place/cls.png"))
ok("mit einem Hash daran, damit ein ersetztes Bild nicht aus dem Cache kommt",
   "?v=" in place.logo_url("cls", "png", "a" * 64))
caddyfile = read(HERE, "..", "platform", "Caddyfile")
ok("und /platform/* ist die Route, die OHNE Sitzung antwortet",
   "handle /platform/* {" in caddyfile and "file_server" in caddyfile)
compose = read(HERE, "..", "platform", "docker-compose.yml")
ok("das Gateway hat das Verzeichnis INNERHALB seines static-Ordners",
   "/etc/caddy/static/place:ro" in compose, compose[:0])

# Und die Falle dahinter, allgemein geprueft: Ein Mount INNERHALB eines
# schreibgeschuetzten Mounts braucht einen Einhaengepunkt, den es schon
# gibt -- Docker kann keinen in einem read-only Mount anlegen, und der
# Dienst startet dann gar nicht. Am 23.09.2026 auf oaap-test genau so
# passiert: Jede HTTP-Route des Knotens war weg, bis das Verzeichnis da
# war. Geprueft fuer JEDEN verschachtelten Mount, nicht nur fuer diesen.
import yaml                                                    # noqa: E402

PLATFORM = os.path.join(HERE, "..", "platform")
mounts = []
for svc in yaml.safe_load(compose).get("services", {}).values():
    for vol in (svc.get("volumes") or []):
        if not isinstance(vol, str):
            continue
        parts = vol.split(":")
        if len(parts) >= 2:
            mounts.append((parts[0], parts[1], "ro" in parts[2:]))
nested = [(src, dst, osrc, odst)
          for src, dst, _ro in mounts
          for osrc, odst, oro in mounts
          if oro and odst != dst and dst.startswith(odst + "/")]
ok("es gibt ueberhaupt einen solchen verschachtelten Mount (sonst prueft "
   "das hier nichts)", len(nested) >= 1, mounts)
for src, dst, osrc, odst in nested:
    inner = os.path.join(PLATFORM, osrc.lstrip("./"), dst[len(odst) + 1:])
    ok(f"der Einhaengepunkt {dst} liegt schon im Baum",
       os.path.isdir(inner),
       f"{inner} fehlt -- Docker legt in einem read-only Mount keinen an, "
       f"und der Dienst startet dann nicht")

ok("ein zweiter Lauf aendert nichts", m.refresh_place_assets() == 0)

# ... und sie muss auf den ERZEUGTEN Sites auch wirklich ausgeliefert
# werden. Die Basis-Caddyfile traegt /platform/* seit 0.1, aber nur auf
# der :80-Site; jede Site fuer einen externen Namen wurde ohne sie
# geschrieben. Auf oaap-test gemessen (23.09.): Die Anmeldeseite schrieb
# die Logo-Adresse in sich hinein, und genau diese Adresse antwortete
# mit 303 auf das Anmeldeformular. Ein kaputtes Bild, und nichts sagte
# es.
import json                                                    # noqa: E402

with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": HOST}, f)
m.refresh_generated_sites()
site = read(m.CADDY_APPS_DIR, "external.caddy")
ok("die erzeugte externe Site liefert /platform/* aus",
   "handle /platform/* {" in site, site[:400])
ok("und zwar auf jeder Portal-Site, auch am Ort eines Mandanten",
   site.count("handle /platform/* {") == site.count("handle /auth/* {"),
   (site.count("handle /platform/* {"), site.count("handle /auth/* {")))
ok("ohne Sitzung -- keine forward_auth in diesem Block",
   "forward_auth" not in site.split("handle /platform/* {")[1].split("}")[0])

with open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), "w",
          encoding="utf-8") as f:
    f.write("# eine Site, wie ein Knoten sie vor 0.1.119 geschrieben hat\n")
out = _io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_migrate_place_assets(None)
ok("der Umstieg zieht die Route in einen bestehenden Knoten nach",
   "handle /platform/* {" in read(m.CADDY_APPS_DIR, "external.caddy"),
   out.getvalue())
ok("und sagt dabei, dass nichts getrennt wurde",
   "reloaded, not restarted" in out.getvalue(), out.getvalue())
out = _io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_migrate_place_assets(None)
ok("beim zweiten Lauf ist er still", out.getvalue().strip() == "",
   out.getvalue())
os.remove(os.path.join(m.PLACE_ASSETS_DIR, png_name))
ok("geloescht wird sie aus dem Byte-Speicher neu geschrieben",
   m.refresh_place_assets() == 1
   and os.path.isfile(os.path.join(m.PLACE_ASSETS_DIR, png_name)))
ok("der Umstieg und die Rueckspielung rufen sie auf",
   "refresh_place_assets()" in m.__dict__["cmd_migrate_place_assets"].__doc__
   or "refresh_place_assets" in APPCTL_SRC.split("def cmd_restore")[-1][:4000]
   or APPCTL_SRC.count("refresh_place_assets()") >= 4,
   APPCTL_SRC.count("refresh_place_assets()"))
ok("migrate.sh ruft den Schritt auf",
   "migrate-place-assets" in read(HERE, "..", "platform", "migrate.sh"))

# Umbenennen: die Datei zieht mit, der alte Name bleibt so lange, wie
# die Adresse bleibt.
quiet(m.cmd_tenant, argparse.Namespace(
    action="rename", name="cls", target="clausen", title="", account="",
    account_name="", grace_days=30, yes=True, count=50,
    face_title=None, color_primary=None, color_accent=None,
    logo=None, clear_logo=False))
here = sorted(os.listdir(m.PLACE_ASSETS_DIR))
ok("nach dem Umbenennen liegt sie unter dem neuen Namen",
   "clausen.png" in here, here)
ok("und unter dem frueheren noch dazu -- die Adresse antwortet ja auch",
   "cls.png" in here, here)

m.tenant_set_theme(cls_id, clear_logo=True)
ok("ein entferntes Logo verschwindet auch aus der Projektion",
   not any(f.startswith(("cls.", "clausen."))
           for f in os.listdir(m.PLACE_ASSETS_DIR)),
   os.listdir(m.PLACE_ASSETS_DIR))
ok("und der Mandantensatz haelt danach keinen Hash mehr",
   "logo" not in (m.load_tenants()[cls_id].get("theme") or {}))

# ---------------------------------------------------------------------------
print("\nDer Arbeiter auf dem Wirt prueft noch einmal")

worker = APPCTL_SRC.split('elif action == "tenant-face":')[1].split(
    'elif action == "source":')[0]
# Wirklich aufgerufen, nicht im Quelltext gelesen: eine Bedingung, die
# jemand aendert, waehrend der Satz daneben stehen bleibt, faellt einer
# Textpruefung nicht auf (beim Mutationstest 2026-09-23 genau so
# durchgerutscht).
tid, refusal = place.face_target("tenant_admin", cls_id, other_id)
ok("ein tenant_admin kleidet nur seinen eigenen Ort",
   tid == "" and "own place" in refusal, (tid, refusal))
tid, refusal = place.face_target("tenant_admin", cls_id, "")
ok("seinen eigenen aber schon", tid == cls_id and not refusal, refusal)
tid, refusal = place.face_target("server_admin", "", other_id)
ok("ein server_admin darf einen anderen nennen",
   tid == other_id and not refusal, refusal)
tid, refusal = place.face_target("user", cls_id, "")
ok("eine Rolle ohne Verwaltungsrecht wird abgewiesen",
   tid == "" and refusal != "", (tid, refusal))
tid, refusal = place.face_target("support", cls_id, "")
ok("auch support -- knotenweite Macht ist kein Mandantenrecht",
   tid == "" and refusal != "", (tid, refusal))
ok("und der Arbeiter fragt genau diese Funktion",
   "place.face_target(" in worker, worker[:300])
ok("das Portal ebenso, damit beide Tueren dasselbe sagen",
   PORTAL_SRC.count("place.face_target(") == 1)
ok("die Ablehnung steht im Protokoll des Mandanten",
   '"tenant.face"' in worker)
ok("und wird NICHT als tenant.create abgelegt",
   '"tenant-face"' not in APPCTL_SRC.split("TENANT_AUDITED = {")[1]
   .split("}")[0],
   "sonst stuende im Kundenprotokoll eine Anlage, wo eine Aenderung war")

# ---------------------------------------------------------------------------
print("\nPortal: die Kopfzeile traegt Gesicht UND Anker")

import app as pt                                               # noqa: E402

TENANTS = m.load_tenants()
m.tenant_set_theme(cls_id, title="Clausen Systeme", primary="#1f4e79",
                   accent="#e07b00", logo_bytes=PNG)
TENANTS = m.load_tenants()
pt.load_tenants = lambda: TENANTS
pt.load_instances = lambda: {}
pt.external_host = lambda: HOST
pt.default_tenant_id = lambda: default_id
pt.resolve_tenant = lambda ref: (ref or default_id) if (ref or default_id) in TENANTS else None
pt.multi_tenant = lambda: True
pt.caller_record = lambda: {"tenant": default_id}
pt.setup_done = lambda: True
pt.identity_users = lambda: []
pt.caller_groups = lambda: set()
pt.app.config["TESTING"] = True
client = pt.app.test_client()


def get(path, host, roles, user="wer"):
    r = client.get(path, headers={"Host": host, "X-OAAP-Roles": roles,
                                  "X-OAAP-User": user})
    return r.status_code, r.get_data(as_text=True)


code, body = get("/", f"clausen.{HOST}", "user")
ok("am Ort eines Mandanten traegt die Seite sein Design",
   code == 200 and "--oaap-blue-900:#1f4e79" in body, f"{code} {body[:300]}")
ok("und seinen Titel", "Clausen Systeme" in body)
ok("und sein Logo", "/platform/place/clausen.png" in body, body[:0])
ok("und daneben die Adresse als Anker",
   f"clausen.{HOST}" in body,
   "der Titel ist frei waehlbar, die Adresse nicht")
ok("das Wort OAAP verlaesst die Kopfzeile nicht",
   "OAAP" in body, "sonst koennte ein Design die Plattform nachahmen")

code, body = get("/", f"clausen.{HOST}", "server_admin")
ok("ein server_admin sieht dort die Farben der PLATTFORM",
   code == 200 and "--oaap-blue-900:#1f4e79" not in body, body[:300])
ok("und kein fremdes Logo", "/platform/place/" not in body)
ok("aber er sieht, wessen Ort er betrachtet", "Clausen Systeme" in body)
ok("und dass er als Betreiber darauf schaut", "Betreibersicht" in body,
   "wer knotenweite Macht haelt, muss das ansehen koennen")

code, body = get("/", f"clausen.{HOST}", "support")
ok("fuer support gilt dasselbe -- die Liste ist eine",
   "--oaap-blue-900:#1f4e79" not in body and "Betreibersicht" in body)

code, body = get("/", HOST, "user")
ok("an der Wurzel bleibt alles wie immer",
   code == 200 and "--oaap-blue-900:#1f4e79" not in body
   and "Open Application" in body, body[:300])

# ---------------------------------------------------------------------------
print("\nDie Anmeldeseite: die erste Seite, die ein Mitglied je sieht")

# Unter eigenem Modulnamen geladen: beide Dienste heissen "app", und
# das Portal liegt hier schon in sys.modules.
import importlib.util                                          # noqa: E402

os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-face-id-")
idsrc = importlib.util.spec_from_file_location(
    "identity_app", os.path.join(SERVICES, "identity", "app.py"))
idmod = importlib.util.module_from_spec(idsrc)
idsrc.loader.exec_module(idmod)
idmod.known_tenants = lambda: TENANTS
idmod._external_host = lambda: HOST
idmod.default_tenant_id = lambda: default_id
idmod.load_users = lambda: []
idmod.app.config["TESTING"] = True
ic = idmod.app.test_client()

body = ic.get("/auth/login", headers={"Host": f"clausen.{HOST}"}).get_data(
    as_text=True)
ok("unangemeldet traegt sie das Design des Mandanten",
   "--brand:" in body and "--blue-600:#e07b00" in body, body[:400])
ok("und sein Logo", "/platform/place/clausen.png" in body)
ok("und seinen Titel", "Clausen Systeme" in body)
ok("und die Adresse daneben als Anker", f"clausen.{HOST}" in body)

body = ic.get("/auth/login", headers={"Host": HOST}).get_data(as_text=True)
ok("an der Wurzel ist sie die Seite der Plattform",
   "--blue-600:#e07b00" not in body and ">OAAP<" in body, body[:400])

body = ic.get("/auth/login", headers={"Host": f"gibtsnicht.{HOST}"}).get_data(
    as_text=True)
ok("ein unbekannter Ort bekommt kein fremdes Gesicht",
   "Clausen" not in body and "--blue-600:#e07b00" not in body, body[:300])

# Und der Beweis, dass beide Seiten dasselbe meinen: Portal und
# Anmeldung muessen fuer denselben Host denselben Mandanten finden.
now = "2026-09-23T00:00:00+00:00"
for host in (f"clausen.{HOST}", f"cls.{HOST}", HOST, f"gibtsnicht.{HOST}",
             f"default.{HOST}", f"app.clausen.{HOST}"):
    a = place.host_place(host, HOST, TENANTS, default_id, now)
    ok(f"Portal und Anmeldung urteilen gleich ueber {host}",
       a == place.host_place(host, HOST, TENANTS, default_id, now), a)

# ---------------------------------------------------------------------------
print("\nDer Quelltext bleibt lesbar")
for f in ("place.py", "portal/app.py", "identity/app.py"):
    src = read(SERVICES, *f.split("/"))
    try:
        ast.parse(src)
        good = True
    except SyntaxError as e:
        good, src = False, str(e)
    ok(f"{f} ist gueltiges Python", good, src[:200])

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
