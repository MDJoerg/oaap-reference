#!/usr/bin/env python3
"""Der zweite Mandant (oaap.core.tenant 0.2, RFC-0022 Stufe 3).

Fassung 0.1 hat eine Dimension gebaut, die niemand sehen durfte.
Fassung 0.2 schaltet sie ein — und genau das macht die Pruefungen hier
anders als dort: Vorher wurde verteidigt, dass **nichts** erscheint;
jetzt wird verteidigt, dass das Erscheinende **an der Grenze haelt**.

Die drei Saetze, an denen alles haengt:

    Die Grenze wird am Gateway durchgesetzt, nicht in der App.
    Der Mandant, in dem jemand handelt, kommt aus seinem eigenen
    Datensatz — nie aus der Anfrage.
    Was getan wurde, steht im Log des Mandanten — auch wenn der
    Betreiber es getan hat.

Der zweite Satz ist der Grund fuer die Haelfte dieser Datei. Ein
Aufrufer, der seinen Mandanten selbst waehlen darf, hat keine Grenze.

Braucht kein Docker und keinen Knoten.

Run: python3 test/test_tenant_boundary.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-tenant2-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

# Das Gateway laeuft hier nicht. Alles andere ist echt.
m.reload_gateway = lambda: None

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {detail}")


def capture(fn, *args):
    import contextlib
    import io
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            fn(*args)
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue(), code


class Args:
    """Was argparse fuer `oaap tenant ...` liefert."""

    def __init__(self, action, name=None, target=None, title="", account="",
                 account_name="", grace_days=30, yes=False, count=50):
        self.action, self.name, self.target = action, name, target
        self.title, self.account, self.account_name = title, account, account_name
        self.grace_days, self.yes, self.count = grace_days, yes, count


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant,
            "groups": [], "active": True}


def instance(name, tenant, port=8100):
    return {name: {"app_id": "demo", "app_name": "Demo", "version": "1.0",
                   "channel": "production", "container": f"oaap-{name}",
                   "port": port, "svc_port": 8080, "tenant": tenant,
                   "routes": [{"path": "/", "roles": ["user"]},
                              {"path": "/status", "roles": ["public"]}]}}


DEFAULT = m.ensure_default_tenant()
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)


print("\n=== ein zweiter Mandant entsteht, und sagt was er kostet ===")

out, code = capture(m.cmd_tenant, Args("create", "kunde-a", title="Kunde A"))
KUNDE = m.tenant_by_label("kunde-a")[0]
check("'tenant create' legt einen an", bool(KUNDE) and code == 0)
check("mit eigener UUID, nicht mit dem Kuerzel als Kennung",
      KUNDE != "kunde-a" and KUNDE != DEFAULT)
check("es warnt, dass das Kuerzel oeffentlich wird",
      "Certificate" in out and "PUBLIC" in out,
      "Kuerzel landen im CT-Log — gesagt wird das beim Waehlen, nicht danach")
check("es sagt, wie man es wieder aendert", "tenant rename kunde-a" in out)
check("es prueft die Zone, bevor Apps unerreichbar sind",
      "external hostname" in out or "resolve" in out)
check("und es sagt, dass Mandanten ab jetzt sichtbar sind",
      "more than one tenant" in out)

out, code = capture(m.cmd_tenant, Args("create", "default"))
check("der Standard-Mandant ist nicht neu anlegbar", code != 0)
out, code = capture(m.cmd_tenant, Args("create", "kunde-a"))
check("ein vergebenes Kuerzel wird abgelehnt", code != 0)
out, code = capture(m.cmd_tenant, Args("create", "Kunde A"))
check("ein ungueltiges Kuerzel wird abgelehnt", code != 0)

check("das Anlegen steht im Log des neuen Mandanten",
      any(e["action"] == "tenant.create" and e["tenant"] == KUNDE
          for e in m.read_tenant_log()))


print("\n=== Namen: das Kuerzel steht zwischen Instanz und Knoten ===")

check("der Standard-Mandant hat gar kein Kuerzel im Namen",
      m.tenant_host_prefixes(DEFAULT) == [""],
      "sein Kuerzel IST die Abwesenheit eines Kuerzels (RFC-0018)")
check("jeder andere bringt seines mit",
      m.tenant_host_prefixes(KUNDE) == ["kunde-a"])
check("ein unbekannter Mandant bringt keines — und damit keinen Namen",
      m.tenant_host_prefixes("nicht-da") == [],
      "lieber unerreichbar als unter dem Namen des Betreibers erreichbar")

reg = m.load_registry()
reg["instances"] = {}
reg["instances"].update(instance("alt", DEFAULT, 8100))
reg["instances"].update(instance("viewer", KUNDE, 8101))
reg["instances"].update(instance("verwaist", "00000000-0000-4000-8000-000000000000", 8102))
m.save_registry(reg)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "oaap.example.org", "edge": ""}, f)

m.write_external_caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), encoding="utf-8") as f:
    ext = f.read()
check("eine Instanz des Standard-Mandanten behaelt ihre Adresse",
      "https://alt.oaap.example.org {" in ext)
check("eine Instanz eines Mandanten bekommt dessen Kuerzel dazwischen",
      "https://viewer.kunde-a.oaap.example.org {" in ext)
check("und NICHT zusaetzlich die kuerzellose Adresse",
      "https://viewer.oaap.example.org {" not in ext,
      "sonst waere die Grenze im Namen nur Dekoration")
check("eine Instanz mit unbekanntem Mandanten bekommt gar keinen Namen",
      "verwaist" not in ext)
check("die HTTP-Weiterleitung deckt die zweite Stufe mit ab",
      "http://*.kunde-a.oaap.example.org" in ext,
      "ein Platzhalter deckt genau EINE Stufe ab — auch bei Caddy")


print("\n=== die Grenze steht am Gateway, nicht in der App ===")

body = "\n".join(m.site_body(reg["instances"]["viewer"]["routes"],
                             "oaap-viewer", 8080, tenant=KUNDE))
check("jede angemeldete Route traegt den Mandanten der Instanz",
      f"&tenant={KUNDE}" in body)
check("die oeffentliche Route traegt ihn nicht",
      body.count(f"&tenant={KUNDE}") == 1,
      "auf einer oeffentlichen Route gibt es keine Sitzung einzuordnen")

site = m.caddy_site(8101, reg["instances"]["viewer"]["routes"], "oaap-viewer",
                    8080, tenant=KUNDE)
check("auch die LAN-Adresse der Instanz ist eingegrenzt",
      f"&tenant={KUNDE}" in site,
      "sonst waere der Umweg ueber den Port die Luecke")

check("was gespeichert ist, wird durchgereicht — nicht was aufloest",
      m.instance_tenant_ref(reg["instances"]["verwaist"])
      == "00000000-0000-4000-8000-000000000000",
      "identity lehnt einen unbekannten Mandanten ab; genau das ist gewollt")
check("und ein Datensatz ganz ohne Bezug liest sich als Standard",
      m.instance_tenant_ref({}) == DEFAULT)


print("\n=== alte Gateway-Dateien bekommen die Grenze nachgereicht ===")

stale = os.path.join(m.CADDY_APPS_DIR, "viewer.caddy")
with open(stale, "w", encoding="utf-8") as f:
    f.write(m.caddy_site(8101, reg["instances"]["viewer"]["routes"],
                         "oaap-viewer", 8080))   # wie vor 0.2 geschrieben
out, _ = capture(m.cmd_migrate_tenant_routes, None)
with open(stale, encoding="utf-8") as f:
    rewritten = f.read()
check("eine Datei ohne Mandanten wird einmal neu geschrieben",
      f"&tenant={KUNDE}" in rewritten and "rewritten" in out)
out2, _ = capture(m.cmd_migrate_tenant_routes, None)
check("und beim zweiten Lauf passiert nichts und wird nichts gesagt",
      out2 == "")


print("\n=== in welchem Mandanten jemand handelt, sagt sein Datensatz ===")

write_users([
    user("joerg", ["server_admin"], DEFAULT),
    user("kunde-chef", ["tenant_admin"], KUNDE),
    user("kunde-mit", ["user"], KUNDE),
    user("verirrt", ["tenant_admin"], "00000000-0000-4000-8000-000000000000"),
])

tid, role, err = m.acting_tenant("joerg")
check("ein server_admin handelt vorgabegemaess im eigenen Mandanten",
      tid == DEFAULT and role == "server_admin" and not err)
tid, role, err = m.acting_tenant("joerg", KUNDE)
check("und darf einen anderen benennen (RFC-0022 D5)",
      tid == KUNDE and role == "server_admin")
tid, role, err = m.acting_tenant("joerg", "gibt-es-nicht")
check("aber keinen erfundenen", tid is None and "does not exist" in err)

tid, role, err = m.acting_tenant("kunde-chef")
check("ein tenant_admin handelt in seinem eigenen",
      tid == KUNDE and role == "tenant_admin" and not err)
tid, role, err = m.acting_tenant("kunde-chef", DEFAULT)
check("und in keinem anderen, auch wenn die Anfrage es verlangt",
      tid is None and "own tenant" in err,
      "ein vom Aufrufer gewaehlter Mandant ist keine Grenze")

tid, role, err = m.acting_tenant("kunde-mit")
check("wer keine Verwaltungsrolle hat, handelt nirgends", tid is None)
tid, role, err = m.acting_tenant("verirrt")
check("und wessen Konto einen unbekannten Mandanten nennt, auch nicht",
      tid is None and "does not have" in err)
tid, role, err = m.acting_tenant("gibt-es-nicht")
check("ein unbekannter Handelnder erst recht nicht", tid is None)


print("\n=== eine Anlege-Erlaubnis waehlt den Mandanten bewusst ===")

check("eine neue Instanz folgt der Erlaubnis",
      m.tenant_for_new_instance(None, {"tenant": KUNDE}) == KUNDE)
check("eine bestehende Instanz laesst sich davon nicht verschieben",
      m.tenant_for_new_instance({"tenant": DEFAULT}, {"tenant": KUNDE}) == DEFAULT,
      "ein Redeploy darf keine Instanz von einem Kunden zum naechsten tragen")
check("ohne beides: der Mandant des Knotens",
      m.tenant_for_new_instance(None, None) == DEFAULT)
check("ein Deploy-Token speichert weiterhin keinen Mandanten",
      "tenant" not in json.dumps(m.load_tokens()),
      "es haengt an einer Instanz, und die weiss es schon")


print("\n=== das Log haelt auch fest, was der Betreiber getan hat ===")

m.audit_tenant("instance.install", KUNDE, "viewer", who="joerg",
               role="server_admin")
m.audit_tenant("user.create", DEFAULT, "intern", who="joerg",
               role="server_admin")
mine = m.read_tenant_log(KUNDE)
check("ein Eingriff des Betreibers steht im Log DES KUNDEN",
      any(e["who"] == "joerg" and e["role"] == "server_admin"
          and e["subject"] == "viewer" for e in mine),
      "es gibt keine technische Schranke vor server_admin — nur diese Zeile")
check("und nichts aus einem fremden Mandanten steht darin",
      all(e["tenant"] == KUNDE for e in mine))
check("jeder Eintrag sagt wer, wann, was und mit welchem Ergebnis",
      all({"when", "who", "role", "action", "subject", "result"} <= set(e)
          for e in m.read_tenant_log()))
check("das Kuerzel steht mit drin, damit das Log ohne Nachschlagen lesbar ist",
      all(e.get("tenant_label") for e in mine))

before = len(m.read_tenant_log())
m.read_tenant_log(KUNDE)
capture(m.cmd_tenant, Args("log"))
check("Lesen ist kein Ereignis", len(m.read_tenant_log()) == before,
      "ein Log, das seine Leser mitschreibt, begraebt was zaehlt")

with open(m.TENANT_LOG, "a", encoding="utf-8") as f:
    f.write("{kaputt\n")
check("eine kaputte Zeile macht das Log nicht unlesbar",
      len(m.read_tenant_log()) >= before)


print("\n=== umbenennen ist eine Adressaenderung und sagt das vorher ===")

out, code = capture(m.cmd_tenant, Args("rename", "kunde-a", "k7f3"))
check("ohne --yes wird nichts geaendert",
      code != 0 and m.tenant_label(KUNDE) == "kunde-a")
check("und vorher steht da, welche Adresse sich wie aendert",
      "viewer.kunde-a.oaap.example.org" in out
      and "viewer.k7f3.oaap.example.org" in out)
check("samt Schonfrist fuer das alte Kuerzel", "30 more day(s)" in out)

out, code = capture(m.cmd_tenant, Args("rename", "kunde-a", "k7f3", yes=True))
check("mit --yes wird umbenannt", m.tenant_label(KUNDE) == "k7f3")
check("die Kennung bleibt dieselbe — es ist eine Umbenennung, keine Migration",
      m.tenant_by_label("k7f3")[0] == KUNDE)
check("das alte Kuerzel antwortet weiter",
      m.tenant_host_prefixes(KUNDE) == ["k7f3", "kunde-a"])
check("und loest weiter auf", m.tenant_by_label("kunde-a")[0] == KUNDE)
check("ein Kuerzel in der Schonfrist ist nicht neu vergebbar",
      not m.label_is_free("kunde-a"),
      "sonst bekaeme ein Mandant den Verkehr eines anderen")

m.write_external_caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), encoding="utf-8") as f:
    ext = f.read()
check("beide Namen werden bedient",
      "https://viewer.k7f3.oaap.example.org {" in ext
      and "https://viewer.kunde-a.oaap.example.org {" in ext)

tenants = m.load_tenants()
tenants[KUNDE]["former_labels"] = [{"label": "kunde-a", "until": "2000-01-01T00:00:00+00:00"}]
m.save_tenants(tenants)
check("nach Ablauf der Schonfrist ist es weg",
      m.tenant_host_prefixes(KUNDE) == ["k7f3"]
      and m.tenant_by_label("kunde-a")[0] is None)

out, code = capture(m.cmd_tenant, Args("rename", "default", "betreiber", yes=True))
check("der Standard-Mandant laesst sich nicht umbenennen",
      code != 0 and m.tenant_label(DEFAULT) == "default",
      "sein Kuerzel ist die Abwesenheit eines Kuerzels — es zu setzen "
      "verschoebe jede Adresse des Knotens auf einmal")

check("die Umbenennung steht im Log",
      any(e["action"] == "tenant.rename" for e in m.read_tenant_log(KUNDE)))


print("\n=== 'tenant show' bleibt eine Inventur, kein Datenexport ===")

out, _ = capture(m.cmd_tenant, Args("show", "k7f3"))
check("es zaehlt Benutzer und Instanzen",
      "Users:" in out and "Instances:" in out)
check("und nennt dabei keinen Namen",
      "viewer" not in out and "kunde-chef" not in out)
check("es nennt die alten Kuerzel, die noch routen", "Also as:" not in out)

out, _ = capture(m.cmd_tenant, Args("list"))
check("'tenant list' sagt jetzt nicht mehr, dass Mandanten unbenutzt sind",
      "not in use here" not in out and "k7f3" in out)


print("\n=== eine Pruefung, die nicht lesen kann, besteht nicht ===")
# Befund aus der Inbetriebnahme auf oaap-test (29.08.): `oaap tenant
# check` laeuft ohne root und meldete "alles loest auf" -- ohne den
# Benutzerspeicher (0600, root) auch nur geoeffnet zu haben. Und
# `tenant show` schrieb "Users: 0" auf einen Knoten mit acht.

import stat as _stat

users_path = m._identity_users_path()
os.makedirs(os.path.dirname(users_path), exist_ok=True)
write_users([user("joerg", ["server_admin"], DEFAULT)])

check("ein lesbarer Speicher wird gelesen",
      m._read_identity_users() == [user("joerg", ["server_admin"], DEFAULT)])

os.remove(users_path)
check("ein Knoten vor dem ersten Benutzer zaehlt ehrlich null",
      m._read_identity_users() == [],
      "nicht vorhanden ist etwas anderes als nicht lesbar")

write_users([user("joerg", ["server_admin"], DEFAULT)])
broken = users_path + ".orig"
os.rename(users_path, broken)
with open(users_path, "w", encoding="utf-8") as f:
    f.write("{kaputt")
check("ein unlesbarer Speicher ist NICHT die leere Liste",
      m._read_identity_users() is None,
      "sonst besteht eine Pruefung, die nichts angesehen hat")

out, code = capture(m.cmd_tenant, Args("check"))
check("'tenant check' besteht dann nicht, sondern sagt warum",
      code == 1 and "could not be read" in out and "sudo oaap tenant check" in out)

out, _ = capture(m.cmd_tenant, Args("show"))
check("'tenant show' schreibt keine erfundene Null",
      "Users:     0" not in out and "not readable" in out)

os.remove(users_path)
os.rename(broken, users_path)
out, code = capture(m.cmd_tenant, Args("check"))
check("mit lesbarem Speicher prueft sie die Benutzer wieder mit",
      "could not be read" not in out,
      "sie meldet hier weiter die verwaiste Instanz von oben — das ist "
      "der Befund, um den es geht, und nicht das Leseproblem")


print("\n=== was die Quelltexte versprechen muessen ===")


def read(path):
    with open(os.path.join(HERE, "..", path), encoding="utf-8") as f:
        return f.read()


ident = read("platform/services/identity/app.py")
portal = read("platform/services/portal/app.py")
compose = read("platform/docker-compose.yml")

check("identity kennt die Rolle tenant_admin",
      '"tenant_admin"' in ident and "ASSIGNABLE_ROLES" in ident)
check("/verify weist eine Sitzung aus einem anderen Mandanten ab",
      'request.args.get("tenant"' in ident
      and "another tenant" in ident)
check("ein server_admin kommt trotzdem durch (RFC-0022 D5)",
      'required_tenant and "server_admin" not in user["roles"]' in ident)
check("jeder schreibende Aufruf der internen Schnittstelle nennt den Handelnden",
      ident.count("_actor(") >= 4,
      "ohne Handelnden muesste identity die maechtigere Rolle annehmen")
check("ein tenant_admin darf keine knotenweite Rolle vergeben",
      ident.count("NODE_WIDE_ROLES &") == 2
      and '"partner"' in ident.split("NODE_WIDE_ROLES = ")[1][:120],
      "server_admin verwaltet den Knoten, partner sieht auf der "
      "Gesundheitsseite jede Instanz der Maschine")
check("und keinen Benutzer eines anderen Mandanten anfassen",
      ident.count("may_see(role, actor_tenant, u)") >= 2)
check("ein fremder Benutzer wird beantwortet, als gaebe es ihn nicht",
      ident.count('"Benutzer nicht gefunden."}, 404') >= 2,
      "'verboten' verriete, dass es den Namen auf diesem Knoten gibt")
check("der Mandant eines Kontos ist nachtraeglich nicht aenderbar",
      "NOT settable here" in ident)

check("das Portal filtert die Instanzen nach Mandant",
      "def visible_instances(" in portal and "def require_instance_admin(" in portal)
check("das Launchpad zeigt keine App eines fremden Mandanten",
      "user_tenant is not None and not is_server_admin" in portal)
check("das Portal reicht den Handelnden an identity durch",
      portal.count('"actor": caller_name()') >= 3)
check("es hat eine Mandantenseite mit Protokoll",
      '@app.get("/tenant")' in portal and "def read_audit(" in portal)
check("die Seite gibt es nur, wo es mehr als einen Mandanten gibt",
      "if not multi_tenant():" in portal)

check("das Portal schreibt das Protokoll nicht, es zeigt es nur",
      "/data/audit:/audit:ro" in compose)
check("identity darf es schreiben — Benutzerverwaltung laeuft nie ueber den Host",
      "/data/audit:/audit\"" in compose)
check("der Mandantenspeicher bleibt fuer beide nur lesbar",
      compose.count("/apps:/platform-apps:ro") == 1
      and compose.count("/apps:/apps-registry:ro") == 1)

migrate = read("platform/migrate.sh")
check("das Update reicht die Grenze in alte Gateway-Dateien nach",
      "migrate-tenant-routes" in migrate)
check("und legt das Verzeichnis fuer das Protokoll an",
      "data/audit" in migrate)


print("\n=== die Oberflaechen: sichtbar erst ab dem zweiten Mandanten ===")

try:
    import ast as _ast
    from jinja2 import Environment
except ImportError:
    print("  SKIP  jinja2 fehlt — die Vorlagen werden nicht gerendert")
else:
    APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")

    def template(name):
        """Die Vorlage aus app.py holen, ohne app.py zu importieren.

        Der Import zoege Flask herein, und diese Pruefung soll ueberall
        laufen — dieselbe Technik wie in test_instance_page.py.
        """
        import io as _io
        tree = _ast.parse(_io.open(APP_PY, encoding="utf-8").read())
        for node in tree.body:
            if (isinstance(node, _ast.Assign)
                    and any(getattr(t, "id", "") == name for t in node.targets)):
                return _ast.literal_eval(node.value)
        raise AssertionError(f"{name} nicht in app.py gefunden")

    ENV = Environment(autoescape=True)

    def render(name, **ctx):
        return ENV.from_string(template(name)).render(**ctx)

    USERS = [{"username": "joerg", "display_name": "Joerg",
              "roles": ["server_admin"], "groups": [], "active": True,
              "tenant": DEFAULT},
             {"username": "kunde-chef", "display_name": "", "groups": [],
              "roles": ["tenant_admin"], "active": True, "tenant": KUNDE}]
    INST = [{"name": "viewer", "app_name": "Viewer", "version": "1.0",
             "channel": "test", "channel_label": "Test",
             "visibility_label": "Alle", "tile_visible": True,
             "tile_mode": "auto", "tenant": "k7f3"}]
    NEW_FORM = {"username": "", "display_name": "", "roles": [], "groups": [],
                "tenant": ""}

    # --- ein Knoten mit einem Mandanten: kein Wort davon ---
    solo = render("USERS_LIST_BODY", users=USERS, show_tenant=False, labels={},
                  default_tenant=DEFAULT, scope_note="", msg=None, error=None)
    check("die Benutzerliste erwaehnt keinen Mandanten, solange es einen gibt",
          "Mandant" not in solo)
    solo_i = render("INSTANCES_LIST_BODY", instances=INST, can_create=False,
                    show_tenant=False, scope_note="", grants=[],
                    grant_minutes=30, msg=None, error=None)
    check("die Instanzenliste auch nicht", "Mandant" not in solo_i)
    solo_n = render("USER_NEW_BODY", all_roles=("user",), tenants=[],
                    error=None, form=NEW_FORM)
    check("und das Anlegeformular fragt nicht danach", "Mandant" not in solo_n)
    solo_e = render("USER_EDIT_BODY", u=USERS[0], all_roles=("user",),
                    tenant_of="", msg=None, error=None)
    check("die Benutzer-Objektseite nennt ihn auch nicht",
          "Mandant" not in solo_e)

    # --- und einer mit mehreren: genau dort, wo es etwas zu unterscheiden gibt ---
    multi = render("USERS_LIST_BODY", users=USERS, show_tenant=True,
                   labels={DEFAULT: "default", KUNDE: "k7f3"},
                   default_tenant=DEFAULT, scope_note="", msg=None, error=None)
    check("mit mehreren steht der Mandant als Spalte da",
          "Mandant" in multi and "k7f3" in multi)
    scoped = render("USERS_LIST_BODY", users=USERS[1:], show_tenant=False,
                    labels={}, default_tenant=DEFAULT, scope_note="Kunde A",
                    msg=None, error=None)
    check("ein tenant_admin liest, in wessen Liste er steht",
          "Kunde A" in scoped)
    check("aber keine Spalte, in der ohnehin nur sein eigener stuende",
          "<th>Mandant</th>" not in scoped)
    form = render("USER_NEW_BODY", all_roles=("user", "tenant_admin"),
                  tenants=[{"id": KUNDE, "label": "k7f3", "name": "Kunde A"}],
                  error=None, form=NEW_FORM)
    check("und beim Anlegen wird der Mandant gewaehlt",
          "Mandant" in form and "k7f3" in form)
    check("das Formular bietet einem tenant_admin keine knotenweite Rolle an",
          "server_admin" not in form and "partner" not in form,
          "identity lehnt sie ohnehin ab — hier wird niemand dazu eingeladen")

    log = [{"when": "2026-08-29T10:00:00+00:00", "who": "joerg",
            "role": "server_admin", "action": "instance.install",
            "tenant": KUNDE, "tenant_label": "k7f3", "subject": "viewer",
            "result": "ok", "detail": ""}]
    mine_page = render("TENANT_BODY", is_server_admin=False, tenants=[],
                       me={"label": "k7f3", "name": "Kunde A",
                           "created": "heute", "users": 2, "instances": 1},
                       host="oaap.example.org", entries=log)
    check("die Mandantenseite zeigt dem Kunden den Eingriff des Betreibers",
          "joerg" in mine_page and "server_admin" in mine_page
          and "instance.install" in mine_page,
          "das ist das ganze Gegengewicht zu 'server_admin darf alles'")
    check("und sagt, unter welchem Namen seine Apps erreichbar sind",
          "k7f3.oaap.example.org" in mine_page)
    all_page = render("TENANT_BODY", is_server_admin=True,
                      tenants=[{"label": "k7f3", "name": "Kunde A",
                                "created": "heute", "users": 2,
                                "instances": 1}],
                      me=None, host="oaap.example.org", entries=log)
    check("der Betreiber sieht alle Mandanten und alle Eintraege",
          "k7f3" in all_page and "instance.install" in all_page)
    check("und wird daran erinnert, dass das Kuerzel oeffentlich ist",
          "Certificate-Transparency" in all_page)


print(f"\n{ok} bestanden, {fail} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail else "PRUEFUNGEN FEHLGESCHLAGEN")
sys.exit(1 if fail else 0)
