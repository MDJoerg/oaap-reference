#!/usr/bin/env python3
"""Die Kohorte (RFC-0046 §2-§4): Vorlage, Plaetze, Saat, Handout, Befehle.

Was hier gemessen wird, ist die Eigenschaft, nicht die Absicht:

  * eine Vorlage mit einem Geheimnis im Klartext, einem Saat-Pfad nach
    aussen oder einer Rolle server_admin wird abgelehnt -- alle Fehler auf
    einmal;
  * ein Platz besteht aus Benutzer (Platz- UND Kohortengruppe, Passwort-
    zwang, Termine), Instanz (nur fuer die Platzgruppe sichtbar, mit
    Grenzen) und Saat, die VOR dem ersten Start liegt;
  * die Saat ueberschreibt nichts, was der Teilnehmer geaendert hat --
    `reset` schon;
  * das Handout entsteht einmal, mit 0600, nicht im Vorlagenverzeichnis,
    und ein Platz, dessen Installation scheitert, verliert sein Passwort
    trotzdem nicht;
  * `create` auf eine halbe Kohorte macht sie fertig; auf eine fertige
    lehnt es ab;
  * Bestaetigung nennt, was geloescht wird; Benutzer nur mit --users.

Braucht Python 3 und PyYAML; Docker und Identity sind ersetzt.
"""
import argparse
import contextlib
import csv
import importlib
import io
import json
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
sys.path.insert(0, PLATFORM)
sys.path.insert(0, SERVICES)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


DATA = tempfile.mkdtemp(prefix="oaap-cohort-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.modules.pop("appctl", None)
import cohort  # noqa: E402
a = importlib.import_module("appctl")
WORK = tempfile.mkdtemp(prefix="oaap-cohort-work-")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def template(root, extra_app="", mutate=None):
    """A complete template directory below `root`."""
    write(os.path.join(root, "seeds", "destinations.json"),
          '{"user": "{user}", "seat": "{nn}", "keep": {"a": 1}}\n')
    write(os.path.join(root, "seeds", "kurs.code-workspace"),
          '{"folders": [{"path": "/home/coder/{name}"}]}\n')
    write(os.path.join(root, "material", "uebung1.md"), "# Uebung 1\n")
    write(os.path.join(root, "material", "sub", "b.txt"), "b\n")
    text = f"""oaap_cohort: "0.1"
name: kurs-2026-10
seats: 3
lifetime:
  ends: 2999-10-24
  deactivate_users_after: 30d
  delete_users_after: 90d
resources:
  memory: 3g
  cpus: 1.5
apps:
  - id: code-server
    name: ide
    config:
      IDE_EXTENSIONS: |
        SAPSE.ext-one
        https://example.org/two.vsix
      ANTHROPIC_API_KEY: {{secret: schule-anthropic}}
      TZ: Europe/Berlin
    seed:
      .adtls/destinations.json: seeds/destinations.json
      projects/kurs.code-workspace: seeds/kurs.code-workspace
    material: material/
    start: "?workspace=/home/coder/projects/kurs.code-workspace"
{extra_app}users:
  prefix: tn
  display_name: "Teilnehmer {{nn}}"
handout: handout.csv
"""
    if mutate:
        text = mutate(text)
    write(os.path.join(root, "cohort.yaml"), text)
    return root


# ============================================================ die Vorlage
print("")
print("Die Vorlage -- was sie sagen darf und was nicht")

T = template(os.path.join(WORK, "t-good"))
tpl = cohort.load(T)
ok("eine gute Vorlage wird gelesen: 3 Plaetze 01..03",
   tpl["seat_ids"] == ["01", "02", "03"] and tpl["name"] == "kurs-2026-10")


def problems(text_fn, extra=""):
    d = os.path.join(WORK, "t-bad-%d" % len(os.listdir(WORK)))
    template(d, extra_app=extra, mutate=text_fn)
    try:
        cohort.load(d)
    except cohort.TemplateError as e:
        return e.problems
    return []


bad = problems(lambda t: t.replace("{secret: schule-anthropic}", "sk-ant-abcdef"))
ok("ein Schluessel im Klartext wird abgelehnt (nach dem FELDNAMEN)",
   any("Geheimnis" in p and "ANTHROPIC_API_KEY" in p for p in bad), bad)
bad = problems(lambda t: t.replace("projects/kurs.code-workspace:",
                                   "../etc/passwd:"))
ok("ein Saat-Pfad mit '..' wird abgelehnt", any(".." in p for p in bad), bad)
bad = problems(lambda t: t.replace("projects/kurs.code-workspace:",
                                   "/etc/cron.d/x:"))
ok("ein absoluter Saat-Pfad wird abgelehnt", any("absolut" in p for p in bad), bad)
bad = problems(lambda t: t.replace("seeds/destinations.json",
                                   "../secret.json"))
ok("eine Saat-QUELLE ausserhalb der Vorlage wird abgelehnt",
   any("außerhalb" in p for p in bad), bad)
bad = problems(lambda t: t.replace("prefix: tn", "prefix: tn\n  roles: [user, server_admin]"))
ok("die Rolle server_admin wird schon in der Vorlage abgelehnt",
   any("server_admin" in p for p in bad), bad)
bad = problems(lambda t: t.replace("seats: 3", "seats: 500"))
ok("500 Plaetze werden abgelehnt", any("seats" in p for p in bad), bad)
bad = problems(lambda t: t.replace("name: kurs-2026-10",
                                   "name: ein-sehr-langer-kursname-ueber-grenzen"))
ok("ein zu langer Kursname wird abgelehnt", any("name" in p for p in bad), bad)
bad = problems(lambda t: t.replace("delete_users_after: 90d",
                                   "delete_users_after: 10d"))
ok("Loeschen vor Deaktivieren wird abgelehnt",
   any("nach deactivate" in p for p in bad), bad)
bad = problems(lambda t: t.replace("    id: code-server\n", "    id: code-server\n    color: red\n")
               .replace("  - id: code-server", "  - id: code-server\n    git: https://x/y"))
ok("id UND git zugleich sowie ein unbekannter Schluessel werden gemeldet",
   len(bad) >= 1, bad)
bad = problems(lambda t: t.replace("seats: 3", "seats: 3\nfoo: 1")
               .replace("name: ide", "name: ide\n    shared: 1x")
               .replace("oaap_cohort: \"0.1\"", "oaap_cohort: \"9\""))
ok("ALLE Fehler auf einmal, nicht einer je Lauf", len(bad) >= 2, bad)

ok("Namen: Benutzer kurs-2026-10-tn-02, Gruppe kurs-2026-10-02, Instanz "
   "kurs-2026-10-ide-02",
   cohort.user_name(tpl, "02") == "kurs-2026-10-tn-02"
   and cohort.seat_group(tpl, "02") == "kurs-2026-10-02"
   and cohort.instance_name(tpl, tpl["apps"][0], "02") == "kurs-2026-10-ide-02")
shared = dict(tpl["apps"][0], name="forgejo", shared=True)
ok("eine gemeinsame Instanz hat keinen Platz im Namen",
   cohort.instance_name(tpl, shared, "02") == "kurs-2026-10-forgejo")
ctx = cohort.context(tpl, "02", tpl["apps"][0])
ok("Platzhalter werden ersetzt, die Klammern einer JSON-Datei bleiben",
   cohort.expand('{"u": "{user}", "n": {"x": {nn}}, "{other}": 1}', ctx)
   == '{"u": "kurs-2026-10-tn-02", "n": {"x": 02}, "{other}": 1}',
   cohort.expand('{"u": "{user}", "n": {"x": {nn}}, "{other}": 1}', ctx))
ok("Termine: Ende 2999-10-24 + 30d / + 90d",
   cohort.user_dates(tpl) == ("2999-11-23", "3000-01-22"), cohort.user_dates(tpl))
noend = dict(tpl, lifetime={"ends": "", "deactivate_users_after": "",
                            "delete_users_after": ""})
ok("ohne Kursende keine Termine", cohort.user_dates(noend) == ("", ""))

# ---------------------------------------------- was auf die Platte darf
print("")
print("Saat und Material -- nur innerhalb der Ablage, nie ueber Vorhandenes")
ROOT = os.path.join(WORK, "home")
os.makedirs(ROOT)
ok("eine Saat wird geschrieben (samt Verzeichnissen)",
   cohort.write_seed(ROOT, ".adtls/destinations.json", b"one")
   and open(os.path.join(ROOT, ".adtls", "destinations.json"), "rb").read() == b"one")
ok("und beim zweiten Mal NICHT ueberschrieben (der Teilnehmer hat sie geaendert)",
   cohort.write_seed(ROOT, ".adtls/destinations.json", b"two") is False
   and open(os.path.join(ROOT, ".adtls", "destinations.json"), "rb").read() == b"one")
ok("reset ueberschreibt (overwrite)",
   cohort.write_seed(ROOT, ".adtls/destinations.json", b"three", overwrite=True)
   and open(os.path.join(ROOT, ".adtls", "destinations.json"), "rb").read() == b"three")


def refused(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except ValueError:
        return True
    return False


ok("'..' im Zielpfad wird abgelehnt", refused(cohort.write_seed, ROOT, "../x", b"x"))
ok("ein absoluter Zielpfad wird abgelehnt", refused(cohort.write_seed, ROOT, "/tmp/x", b"x"))
if hasattr(os, "symlink"):
    outside = os.path.join(WORK, "outside")
    os.makedirs(outside)
    try:
        os.symlink(outside, os.path.join(ROOT, "escape"), target_is_directory=True)
        linked = True
    except (OSError, NotImplementedError):
        linked = False
    if linked:
        ok("ein Link, den der Teilnehmer im Zuhause anlegte, fuehrt eine Saat nicht hinaus",
           refused(cohort.write_seed, ROOT, "escape/x.txt", b"x")
           and not os.path.exists(os.path.join(outside, "x.txt")))
MAT = os.path.join(WORK, "seatmat")
n = cohort.copy_material(os.path.join(T, "material"), MAT)
ok("Material wird kopiert (2 Dateien, mit Unterverzeichnis)",
   n == 2 and os.path.isfile(os.path.join(MAT, "sub", "b.txt")))
write(os.path.join(MAT, "uebung1.md"), "vom Teilnehmer\n")
cohort.copy_material(os.path.join(T, "material"), MAT)
ok("ohne replace bleibt die Datei des Teilnehmers",
   open(os.path.join(MAT, "uebung1.md"), encoding="utf-8").read() == "vom Teilnehmer\n")
write(os.path.join(WORK, "newmat", "neu.md"), "neu\n")
cohort.copy_material(os.path.join(WORK, "newmat"), MAT, replace=True)
ok("replace tauscht das Material aus (alt weg, neu da)",
   sorted(os.listdir(MAT)) == ["neu.md"], os.listdir(MAT))

# ----------------------------------------------------------- das Handout
print("")
print("Das Handout -- einmal, 0600, nicht in der Vorlage")
HP = os.path.join(WORK, "out", "handout.csv")
os.makedirs(os.path.dirname(HP))
ok("ein Handout im Vorlagenverzeichnis wird abgelehnt (das ist ein Repository)",
   bool(cohort.handout_path_refusal(os.path.join(T, "handout.csv"), T)))
ok("ausserhalb: erlaubt", cohort.handout_path_refusal(HP, T) == "")
h = cohort.Handout(HP)
h.add({"seat": "01", "username": "u1", "password": "pw1", "address": "https://x/"})
raw = open(HP, encoding="utf-8").read()
ok("die Zeile steht SOFORT auf der Platte (nicht erst beim Schliessen)",
   "u1,pw1" in raw, raw)
h.close()
ok("Dateirechte 0600 (wo das Dateisystem sie kennt)",
   os.name == "nt" or stat.S_IMODE(os.stat(HP).st_mode) == 0o600)
ok("ein zweites Handout an derselben Stelle wird abgelehnt",
   bool(cohort.handout_path_refusal(HP, T)))
try:
    cohort.Handout(HP)
    second = False
except FileExistsError:
    second = True
ok("und ueberschreibt nie (O_EXCL)", second)

# ================================================ die Befehle (mit Attrappen)
print("")
print("Die Befehle -- Attrappen fuer Docker, Identity und Store")

OUT = io.StringIO()
INSTALLS, USERS, REMOVED, DELETED, DOCKER, AUDIT = [], [], [], [], [], []
FAIL_ON = {"name": None}

a.load_external = lambda: "node.test"
a._node_ram_bytes = lambda: 8 * 1024 ** 3
a.image_uid = lambda image: None
a.state_view_write = lambda *x, **k: None
a.container_states = lambda names: ({n: {"state": "running"} for n in names}, True)
a.audit_tenant = lambda action, tenant, **kw: AUDIT.append((action, kw))
a._operator_name = lambda: "tester"
EXISTING = set()
a._cohort_existing_users = lambda: EXISTING


class Trust:
    pass


a._store_lookup = lambda app_id, source_id="", prefer="": (
    {"kind": "git", "url": "https://forge.example/oaap-apps", "path": "apps/" + app_id,
     "ref": ""}, "0.1.2", {"id": "core", "name": "Core", "trust": "official"})
REAL_LOAD_SECRETS = a.load_cohort_secrets
a.load_cohort_secrets = lambda: {a.ensure_default_tenant(): {"schule-anthropic": "sk-live-123"}}


def fake_create_user(body):
    USERS.append(dict(body))
    EXISTING.add(body["username"])
    return 201, {"ok": True}


a.identity_user_create = fake_create_user
a.identity_user_delete = lambda username: (DELETED.append(username), (200, {}))[1]

MANIFEST = {"app": {"id": "code-server"},
            "config": [{"key": "IDE_EXTENSIONS", "multiline": True},
                       {"key": "ANTHROPIC_API_KEY", "secret": True},
                       {"key": "TZ"}],
            "storage": [{"name": "home", "mount": "/home/coder"},
                        {"name": "material", "mount": "/home/coder/material"}]}
SEEN_ENV = {}


def fake_install(ns):
    """What cmd_install does that matters here: identity, hook, record."""
    if FAIL_ON["name"] and ns.name == FAIL_ON["name"]:
        FAIL_ON["name"] = None
        raise SystemExit("install refused (Attrappe)")
    reg = a.load_registry()
    tid = ns.tenant or a.ensure_default_tenant()
    key = a.instance_key(tid, ns.name)
    inst = reg["instances"].get(key)
    ident = ({"id": inst["id"], "tenant": tid} if inst
             else {"id": a.new_instance_id(), "tenant": tid})
    os.makedirs(a.instance_dir(key, ident), exist_ok=True)
    services = [{"service": "", "container": "oaap-app-" + key,
                 "image": "oaap-app/code-server:0.1.2", "port": 8080}]
    hook = getattr(ns, "before_start", None)
    if hook:
        hook(key, ident, services, MANIFEST)
    preset = getattr(ns, "preset", None) or {}
    SEEN_ENV[key] = dict(a.load_env(key, ident))
    rec = dict(inst or {})
    rec.update({"app_id": "code-server", "app_name": "code-server",
                "channel": ns.channel, "tenant": tid, "id": ident["id"],
                "version": "0.1.2", "name": ns.name, "port": 8130,
                "svc_port": 8080, "container": "oaap-app-" + key,
                "image": services[0]["image"], "services": services,
                "routes": [{"path": "/", "roles": ["user"]}],
                "storage": MANIFEST["storage"]})
    if not inst:
        rec["visibility"] = dict(preset.get("visibility") or {})
    if preset.get("resources") and not rec.get("resources"):
        rec["resources"] = dict(preset["resources"])
    if (rec.get("cohort") or preset.get("cohort")):
        rec["cohort"] = dict(rec.get("cohort") or preset["cohort"])
    reg["instances"][key] = rec
    a.save_registry(reg)
    INSTALLS.append((ns.name, bool(inst)))


a.cmd_install = fake_install


def fake_remove(reg, name, purge):
    REMOVED.append((name, purge))
    reg["instances"].pop(name, None)
    a.save_registry(reg)
    return f"removed '{name}'" + (" including data" if purge else "")


a.remove_instance = fake_remove


class R:
    stdout = ""
    stderr = ""
    returncode = 0


def fake_sub_run(cmd, **kw):
    DOCKER.append(list(cmd))
    return R()


a.subprocess.run = fake_sub_run


def ns(**kw):
    base = dict(target=None, second=None, third=None, tenant="", seat="",
                who="", keep_home=False, purge=False, users=False, yes=True,
                handout_file="", confirm_source="", stdin=False, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def cli(action, **kw):
    """Run one command -> (exit code or None, its output)."""
    buf, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            a.cmd_cohort(ns(action=action, **kw))
        except SystemExit as e:
            code = e.code if e.code is not None else 0
    return code, buf.getvalue() + err.getvalue()


TID = a.ensure_default_tenant()
cwd = os.getcwd()
os.chdir(os.path.join(WORK, "out"))

# ------------------------------------------------ create
tdir = template(os.path.join(WORK, "t-create"), extra_app="""  - id: forgejo
    name: git
    shared: true
""")
code, out = cli("create", target=tdir, handout_file=os.path.join(WORK, "out", "h1.csv"))
rec = a.load_cohort(TID, "kurs-2026-10")
ok("create endet ohne Fehler, die Kohorte ist fertig", code is None and rec and rec["complete"], out)
ok("drei Benutzer, mit Platz- UND Kohortengruppe",
   [u["username"] for u in USERS] == ["kurs-2026-10-tn-01", "kurs-2026-10-tn-02",
                                      "kurs-2026-10-tn-03"]
   and USERS[1]["groups"] == ["kurs-2026-10", "kurs-2026-10-02"], USERS[1:2])
ok("Passwortzwang gesetzt, Rolle user, Anzeigename mit Platznummer",
   USERS[0]["must_change_password"] is True and USERS[0]["roles"] == ["user"]
   and USERS[0]["display_name"] == "Teilnehmer 01")
ok("Termine aus dem Kursende, Grund nennt die Kohorte",
   USERS[0]["deactivate_at"] == "2999-11-23" and USERS[0]["delete_at"] == "3000-01-22"
   and USERS[0]["schedule_reason"] == "cohort kurs-2026-10", USERS[0])
reg = a.load_registry()["instances"]
ok("je Platz eine Instanz kurs-2026-10-ide-0N, dazu die gemeinsame",
   {"kurs-2026-10-ide-01", "kurs-2026-10-ide-02", "kurs-2026-10-ide-03",
    "kurs-2026-10-git"} <= set(reg), sorted(reg))
ok("die Instanz ist NUR fuer die Platzgruppe sichtbar",
   reg["kurs-2026-10-ide-02"]["visibility"] == {"groups": ["kurs-2026-10-02"]})
ok("die gemeinsame Instanz fuer die Kohortengruppe",
   reg["kurs-2026-10-git"]["visibility"] == {"groups": ["kurs-2026-10"]})
ok("Grenzen aus der Vorlage schon im ersten Eintrag",
   reg["kurs-2026-10-ide-01"]["resources"] == {"memory": "3g", "cpus": 1.5})
ok("die Instanz weiss, zu welchem Platz sie gehoert",
   reg["kurs-2026-10-ide-02"]["cohort"] == {"name": "kurs-2026-10", "seat": "02"})
env = SEEN_ENV["kurs-2026-10-ide-02"]
ok("Konfiguration lag VOR dem Start in der Umgebung; mehrzeilig als ';'",
   env.get("IDE_EXTENSIONS") == "SAPSE.ext-one;https://example.org/two.vsix"
   and env.get("TZ") == "Europe/Berlin", env)
ok("das benannte Geheimnis wurde aufgeloest, im Register steht es nicht",
   env.get("ANTHROPIC_API_KEY") == "sk-live-123"
   and "sk-live-123" not in json.dumps(reg))
inst2 = reg["kurs-2026-10-ide-02"]
home = os.path.join(a.instance_dir("kurs-2026-10-ide-02", inst2), "storage", "home")
seed = open(os.path.join(home, ".adtls", "destinations.json"), encoding="utf-8").read()
ok("die Saat liegt im Zuhause, Platzhalter ersetzt, JSON-Klammern heil",
   json.loads(seed) == {"user": "kurs-2026-10-tn-02", "seat": "02", "keep": {"a": 1}}, seed)
ok("der Arbeitsbereich ist mit dem Kursnamen gesaeet",
   "/home/coder/kurs-2026-10" in open(os.path.join(home, "projects", "kurs.code-workspace"),
                                      encoding="utf-8").read())
mat = os.path.join(a.instance_dir("kurs-2026-10-ide-02", inst2), "storage", "material")
ok("das Material liegt im zweiten Mount", os.path.isfile(os.path.join(mat, "sub", "b.txt")))
rows = list(csv.DictReader(open(os.path.join(WORK, "out", "h1.csv"), encoding="utf-8")))
ok("Handout: drei Zeilen mit Passwort und Adresse",
   len(rows) == 3 and all(len(r["password"]) >= 12 for r in rows)
   and rows[0]["address"].startswith("https://kurs-2026-10-ide-01.node.test/?workspace="), rows[:1])
ok("das Passwort im Handout ist das, mit dem der Benutzer angelegt wurde",
   [r["password"] for r in rows] == [u["password"] for u in USERS])
ok("die gespeicherte Vorlage liegt beim Knoten (fuer list/reset/export)",
   os.path.isfile(os.path.join(DATA, "data", "cohorts", TID, "kurs-2026-10", "template", "cohort.yaml")))
ok("kein Passwort im Kohorten-Eintrag",
   not any(u["password"] in json.dumps(rec) for u in USERS))
ok("Audit: cohort.create",
   any(x[0] == "cohort.create" for x in AUDIT))

# ------------------------------------------------ create zweimal
code, out = cli("create", target=tdir, handout_file=os.path.join(WORK, "out", "h2.csv"))
ok("create auf eine FERTIGE Kohorte wird abgelehnt", code == 1 and "exists" in out, out)

# ------------------------------------------------ list
code, out = cli("list", target="kurs-2026-10")
ok("list zeigt Platz, Benutzer, Instanz, Zustand, Adresse",
   code is None and "kurs-2026-10-tn-03" in out and "running" in out
   and "https://kurs-2026-10-ide-03.node.test/" in out
   and "2999-11-23" in out, out)
code, out = cli("list")
ok("list ohne Namen nennt die Kohorte", "kurs-2026-10" in out and "3 seat" in out, out)

# ------------------------------------------------ handout
code, out = cli("handout", target="kurs-2026-10")
ok("handout ist einmalig: verweigert und nennt `oaap user password`",
   code == 1 and "oaap user password" in out, out)

# ------------------------------------------------ eine halbe Kohorte
print("")
print("Eine halbe Kohorte wird fertig gemacht -- und kein Passwort geht verloren")
EXISTING.clear()
half = template(os.path.join(WORK, "t-half"), mutate=lambda t: t.replace("kurs-2026-10", "kurs-half"))
USERS.clear(); INSTALLS.clear()
FAIL_ON["name"] = "kurs-half-ide-02"
code, out = cli("create", target=half, handout_file=os.path.join(WORK, "out", "half1.csv"))
rec = a.load_cohort(TID, "kurs-half")
h1 = list(csv.DictReader(open(os.path.join(WORK, "out", "half1.csv"), encoding="utf-8")))
ok("Platz 02 scheitert: der Lauf bricht ab, die Kohorte ist NICHT fertig",
   code not in (None, 0) and rec and not rec["complete"], (code, out))
ok("das Passwort des gescheiterten Platzes steht trotzdem im Handout",
   [r["username"] for r in h1] == ["kurs-half-tn-01", "kurs-half-tn-02"], h1)
code, out = cli("create", target=half, handout_file=os.path.join(WORK, "out", "half2.csv"))
h2 = list(csv.DictReader(open(os.path.join(WORK, "out", "half2.csv"), encoding="utf-8")))
rec = a.load_cohort(TID, "kurs-half")
ok("erneut: fertig, nur Platz 03 ist neu im Handout, 01/02 waren da",
   code is None and rec["complete"] and [r["username"] for r in h2] == ["kurs-half-tn-03"], (code, out, h2))
ok("Platz 01 wurde nicht noch einmal installiert",
   INSTALLS.count(("kurs-half-ide-01", False)) == 1, INSTALLS)

# ------------------------------------------------ Geheimnis fehlt
print("")
print("Vorpruefungen -- bevor ein Benutzer entsteht")
USERS.clear()
a.load_cohort_secrets = lambda: {}
nosec = template(os.path.join(WORK, "t-nosec"), mutate=lambda t: t.replace("kurs-2026-10", "kurs-nosec"))
code, out = cli("create", target=nosec, handout_file=os.path.join(WORK, "out", "n.csv"))
ok("ein nicht hinterlegtes Geheimnis stoppt VOR dem ersten Benutzer",
   code == 1 and "schule-anthropic" in out and not USERS and a.load_cohort(TID, "kurs-nosec") is None, out)
a.load_cohort_secrets = lambda: {TID: {"schule-anthropic": "sk-live-123"}}
past = template(os.path.join(WORK, "t-past"), mutate=lambda t: t.replace("kurs-2026-10", "kurs-past")
                .replace("2999-10-24", "2020-01-01"))
code, out = cli("create", target=past, handout_file=os.path.join(WORK, "out", "p.csv"))
ok("ein Termin in der Vergangenheit stoppt VOR dem ersten Benutzer",
   code == 1 and "future" in out and not USERS, out)
inrepo = template(os.path.join(WORK, "t-inrepo"), mutate=lambda t: t.replace("kurs-2026-10", "kurs-inrepo"))
code, out = cli("create", target=inrepo, handout_file=os.path.join(inrepo, "handout.csv"))
ok("ein Handout im Vorlagenverzeichnis stoppt den Lauf (kein Benutzer entsteht)",
   code == 1 and "Vorlage" in out and not USERS and not os.path.exists(os.path.join(inrepo, "handout.csv")), out)
ok("die Kohorte wird nicht halb angelegt, wenn der Vorlauf scheitert",
   a.load_cohort(TID, "kurs-past") is None)

# ------------------------------------------------ add
print("")
print("add, reset, stop/start, material, export, remove")
USERS.clear()
code, out = cli("add", target="kurs-2026-10", who="Anna Beispiel",
                handout_file=os.path.join(WORK, "out", "add.csv"))
rec = a.load_cohort(TID, "kurs-2026-10")
ok("add: benannter Nachzuegler bekommt Platz anna-beispiel",
   code is None and "anna-beispiel" in rec["seats"]
   and USERS[0]["username"] == "kurs-2026-10-tn-anna-beispiel", (code, out))
ok("add: eigene Handout-Datei nur fuer diesen Platz",
   len(list(csv.DictReader(open(os.path.join(WORK, "out", "add.csv"), encoding="utf-8")))) == 1)
USERS.clear()
code, out = cli("add", target="kurs-2026-10", handout_file=os.path.join(WORK, "out", "add2.csv"))
rec = a.load_cohort(TID, "kurs-2026-10")
ok("add ohne Angabe: naechste Nummer 04", "04" in rec["seats"], sorted(rec["seats"]))

# reset (Zuhause weg)
inst2 = a.load_registry()["instances"]["kurs-2026-10-ide-02"]
home2 = os.path.join(a.instance_dir("kurs-2026-10-ide-02", inst2), "storage", "home")
write(os.path.join(home2, "arbeit.txt"), "Arbeit des Teilnehmers\n")
REMOVED.clear(); INSTALLS.clear(); USERS.clear()
code, out = cli("reset", target="kurs-2026-10", seat="02")
ok("reset: Instanz mit Daten entfernt, neu gebaut, kein neuer Benutzer",
   code is None and REMOVED == [("kurs-2026-10-ide-02", True)]
   and ("kurs-2026-10-ide-02", False) in INSTALLS and not USERS, (code, out, REMOVED, INSTALLS))
ok("reset: die Saat ist wieder da",
   os.path.isfile(os.path.join(a.instance_dir("kurs-2026-10-ide-02",
                  a.load_registry()["instances"]["kurs-2026-10-ide-02"]), "storage", "home", ".adtls",
                  "destinations.json")))
code, out = cli("reset", target="kurs-2026-10", seat="99")
ok("reset eines unbekannten Platzes wird abgelehnt", code == 1, out)
code, out = cli("reset", target="kurs-2026-10", seat="03", yes=False)
ok("reset ohne --yes fragt und nennt, was geloescht wird "
   "(hier: keine Eingabe -> nichts passiert)", code not in (None, 0) or "DELETES" in out, out)

# reset --keep-home
inst3 = a.load_registry()["instances"]["kurs-2026-10-ide-03"]
home3 = os.path.join(a.instance_dir("kurs-2026-10-ide-03", inst3), "storage", "home")
write(os.path.join(home3, ".adtls", "destinations.json"), '{"vom": "Teilnehmer"}\n')
REMOVED.clear(); INSTALLS.clear()
code, out = cli("reset", target="kurs-2026-10", seat="03", keep_home=True)
ok("reset --keep-home: nichts entfernt, ueber die vorhandene Instanz gebaut",
   code is None and not REMOVED and ("kurs-2026-10-ide-03", True) in INSTALLS, (code, out, REMOVED, INSTALLS))
ok("reset --keep-home: die geaenderte Datei des Teilnehmers bleibt",
   json.load(open(os.path.join(home3, ".adtls", "destinations.json"), encoding="utf-8")) == {"vom": "Teilnehmer"})

# stop / start
DOCKER.clear()
code, out = cli("stop", target="kurs-2026-10")
stopped = [c[2] for c in DOCKER if c[:2] == ["docker", "stop"]]
ok("stop: jeder Container der Kohorte (Plaetze + gemeinsame), nichts geloescht",
   "oaap-app-kurs-2026-10-ide-01" in stopped and "oaap-app-kurs-2026-10-git" in stopped
   and not REMOVED and a.load_cohort(TID, "kurs-2026-10")["stopped"] is True, (out, stopped))
DOCKER.clear()
code, out = cli("start", target="kurs-2026-10")
ok("start: dieselben wieder an, Kennzeichen zurueck",
   len([c for c in DOCKER if c[:2] == ["docker", "start"]]) == len(stopped)
   and a.load_cohort(TID, "kurs-2026-10")["stopped"] is False)

# material update
newmat = os.path.join(WORK, "material-v2")
write(os.path.join(newmat, "uebung2.md"), "# Uebung 2\n")
code, out = cli("material", target="update", second="kurs-2026-10", third=newmat)
mat1 = os.path.join(a.instance_dir("kurs-2026-10-ide-01",
                    a.load_registry()["instances"]["kurs-2026-10-ide-01"]), "storage", "material")
ok("material update: bei jedem Platz ausgetauscht, das Zuhause unberuehrt",
   code is None and sorted(os.listdir(mat1)) == ["uebung2.md"]
   and os.path.isfile(os.path.join(home3, ".adtls", "destinations.json")), (code, out))
code, out = cli("material", target="update", second="kurs-2026-10", third=os.path.join(WORK, "nope"))
ok("material update mit fehlendem Verzeichnis wird abgelehnt", code == 1, out)
ok("die gespeicherte Vorlage traegt das neue Material (naechster Lauf)",
   os.path.isfile(os.path.join(DATA, "data", "cohorts", TID, "kurs-2026-10", "template", "material", "uebung2.md")))

# export
exp = os.path.join(WORK, "export")
code, out = cli("export", target="kurs-2026-10", second=exp)
created = json.load(open(os.path.join(exp, "created.json"), encoding="utf-8"))
ok("export: Vorlage + created.json, ohne Handout-Hinweis und ohne Passwort",
   code is None and os.path.isfile(os.path.join(exp, "cohort.yaml"))
   and "handout" not in created and "seats" in created
   and not any(u["password"] in json.dumps(created) for u in USERS), (code, out))
ok("der Export ist selbst eine gueltige Vorlage (der naechste Lauf)",
   cohort.load(exp)["name"] == "kurs-2026-10")
code, out = cli("export", target="kurs-2026-10", second=exp)
ok("export in ein nicht leeres Verzeichnis wird abgelehnt", code == 1, out)

# remove
REMOVED.clear(); DELETED.clear()
code, out = cli("remove", target="kurs-2026-10", seat="03")
rec = a.load_cohort(TID, "kurs-2026-10")
ok("remove --seat: nur dieser Platz, Speicher BLEIBT, Benutzer BLEIBT",
   code is None and REMOVED == [("kurs-2026-10-ide-03", False)] and not DELETED
   and "03" not in rec["seats"] and "01" in rec["seats"], (code, out, REMOVED))
REMOVED.clear()
code, out = cli("remove", target="kurs-2026-10", seat="02", purge=True, users=True)
ok("remove --purge --users: Speicher und Benutzer des Platzes weg",
   REMOVED == [("kurs-2026-10-ide-02", True)] and DELETED == ["kurs-2026-10-tn-02"], (REMOVED, DELETED))
REMOVED.clear(); DELETED.clear()
code, out = cli("remove", target="kurs-2026-10", yes=False)
ok("remove ohne --yes und ohne Eingabe: nichts wird entfernt (die Frage nennt den Namen)",
   code not in (None, 0) and not REMOVED and "kurs-2026-10" in out, (code, out))
code, out = cli("remove", target="kurs-2026-10", purge=True)
ok("remove der ganzen Kohorte: Plaetze, gemeinsame Instanz, Eintrag weg",
   ("kurs-2026-10-git", True) in REMOVED and a.load_cohort(TID, "kurs-2026-10") is None, (REMOVED, out))
ok("Audit fuer jeden Schritt",
   {"cohort.add", "cohort.reset", "cohort.stop", "cohort.start", "cohort.material",
    "cohort.export", "cohort.remove"} <= {x[0] for x in AUDIT}, sorted({x[0] for x in AUDIT}))

os.chdir(cwd)

# ------------------------------------------------ der taegliche Lauf (Stufe 4)
print("")
print("Der taegliche Lauf -- Termine als Daten, jetzt mit Handelnden")
import datetime  # noqa: E402

UTC = datetime.timezone.utc
USERDB = {}
REFUSE = set()


def iso(d):
    return d + "T00:00:00Z" if d and "T" not in d else d


def sweep_create_user(body):
    USERS.append(dict(body))
    EXISTING.add(body["username"])
    USERDB[body["username"]] = {
        "tenant": body.get("tenant", ""), "active": True,
        "deactivate_at": iso(body.get("deactivate_at", "")),
        "delete_at": iso(body.get("delete_at", "")),
        "reason": body.get("schedule_reason", "")}
    return 201, {"ok": True}


def due(now_iso):
    return [dict(username=n, tenant=u["tenant"], active=u["active"],
                 deactivate_at=u["deactivate_at"], delete_at=u["delete_at"],
                 reason=u["reason"])
            for n, u in USERDB.items()
            if (u["delete_at"] and u["delete_at"] <= now_iso)
            or (u["deactivate_at"] and u["deactivate_at"] <= now_iso and u["active"])]


def sweep_deactivate(username):
    USERDB[username]["active"] = False
    USERDB[username]["deactivate_at"] = ""
    return 200, {"ok": True}


def sweep_delete(username):
    if username in REFUSE:
        return 409, {"error": "der letzte tenant_admin"}
    DELETED.append(username)
    USERDB.pop(username, None)
    return 200, {}


a.identity_user_create = sweep_create_user
a.identity_users_due = due
a.identity_user_deactivate = sweep_deactivate
a.identity_user_delete = sweep_delete

t_sw = template(os.path.join(WORK, "t-sweep"),
                mutate=lambda t: t.replace("kurs-2026-10", "kurs-sweep"))
code, out = cli("create", target=t_sw, handout_file=os.path.join(WORK, "out", "hs.csv"))
rec = a.load_cohort(TID, "kurs-sweep")
ok("die Kohorte fuer den Lauf steht, Termine liegen am Benutzer",
   code is None and rec and rec["complete"]
   and USERDB["kurs-sweep-tn-01"]["deactivate_at"] == "2999-11-23T00:00:00Z"
   and USERDB["kurs-sweep-tn-01"]["delete_at"] == "3000-01-22T00:00:00Z", out)
keys = [k for _s, _a, k in a._cohort_instances(rec)]
REMOVED_BEFORE = len(REMOVED)


def sweep(y, m, d, **kw):
    AUDIT.clear()
    DOCKER.clear()
    return a.cohort_sweep(now=datetime.datetime(y, m, d, 4, 40, tzinfo=UTC), **kw)


def stops():
    return [c for c in DOCKER if c[:2] == ["docker", "stop"] and "-sweep-" in c[2]]


sw = sweep(2999, 10, 24)
ok("am Tag `ends` selbst passiert nichts (der letzte Kurstag laeuft noch)",
   sw == [] and not stops() and not a.load_cohort(TID, "kurs-sweep").get("ended"), sw)
sw = sweep(2999, 10, 25)
rec = a.load_cohort(TID, "kurs-sweep")
ok("am Tag danach werden die Instanzen GESTOPPT -- alle drei, nichts geloescht",
   len(stops()) == 3 and rec["stopped"] and rec["ended"] == "2999-10-24"
   and all(k in a.load_registry()["instances"] for k in keys), (sw, DOCKER))
ok("und das steht im Protokoll des Mandanten",
   [x[0] for x in AUDIT if x[1]["subject"] == "kurs-sweep"] == ["cohort.end"], AUDIT)
sw = sweep(2999, 10, 26)
ok("ein zweiter Lauf stoppt nicht noch einmal", sw == [] and not stops(), sw)
cli("start", target="kurs-sweep")
sw = sweep(2999, 10, 27)
ok("hat der Ausbilder wieder gestartet, ueberstimmt der Lauf ihn nicht",
   sw == [] and not stops()
   and a.load_cohort(TID, "kurs-sweep")["stopped"] is False, (sw, DOCKER))
sw = sweep(2999, 11, 23, dry=True)
ok("--dry-run sagt, was geschaehe, und aendert nichts",
   len(sw) == 3 and all("would be deactivated" in t for _s, t in sw)
   and all(u["active"] for n, u in USERDB.items()) and not AUDIT, sw)
sw = sweep(2999, 11, 23)
ok("am Termin werden die drei Benutzer deaktiviert",
   len(sw) == 3 and not any(USERDB[n]["active"] for n in USERDB
                           if n.startswith("kurs-sweep-")), sw)
ok("das Datum ist gefeuert und geloescht, die Loeschung bleibt",
   all(USERDB[n]["deactivate_at"] == "" and USERDB[n]["delete_at"]
       for n in USERDB if n.startswith("kurs-sweep-")))
ok("jeder Schritt steht im Protokoll",
   [x[0] for x in AUDIT] == ["cohort.sweep"] * 3, AUDIT)
USERDB["kurs-sweep-tn-02"]["active"] = True
sw = sweep(2999, 11, 24)
ok("wer von Hand wieder aktiviert wurde, wird morgen nicht erneut abgeschaltet",
   sw == [] and USERDB["kurs-sweep-tn-02"]["active"], sw)

sw = sweep(3000, 1, 22)
rec = a.load_cohort(TID, "kurs-sweep")
ok("die Loeschung wartet, solange der Platz Instanzen hat -- nichts geloescht",
   len(sw) == 3 and not DELETED and all("waiting" in t for _s, t in sw)
   and all(rec["seats"][x].get("waiting") for x in ("01", "02", "03")), sw)
ok("das Warten wird EINMAL gesagt, nicht jeden Tag ins Protokoll",
   len([x for x in AUDIT if x[0] == "cohort.sweep"]) == 3, AUDIT)
sw = sweep(3000, 1, 23)
ok("am naechsten Tag: noch immer keine Loeschung, kein neuer Protokolleintrag",
   len(sw) == 3 and not DELETED and not AUDIT, (sw, AUDIT))

cli("remove", target="kurs-sweep", seat="01")
REFUSE.add("kurs-sweep-tn-02")
cli("remove", target="kurs-sweep", seat="02")
sw = sweep(3000, 1, 24)
rec = a.load_cohort(TID, "kurs-sweep")
ok("ist der Platz leer, wird der Benutzer geloescht",
   DELETED == ["kurs-sweep-tn-01"] or "kurs-sweep-tn-01" in DELETED, (sw, DELETED))
ok("der Platz verschwindet dabei aus der Kohorte, die anderen bleiben",
   "01" not in rec["seats"] and "03" in rec["seats"])
ok("verweigert die Identitaet die Loeschung, steht der Grund da -- der Benutzer bleibt",
   "kurs-sweep-tn-02" in USERDB
   and any("NOT deleted -- der letzte tenant_admin" in t for _s, t in sw), sw)
ok("die Verweigerung kommt EINMAL ins Protokoll",
   len([x for x in AUDIT if "NOT deleted" in x[1].get("detail", "")]) == 1, AUDIT)
sw = sweep(3000, 1, 25)
ok("am naechsten Tag wieder nur die Anzeige, kein zweiter Eintrag",
   any("NOT deleted" in t for _s, t in sw) and not AUDIT, (sw, AUDIT))
ok("kein Termin hat je eine Instanz oder ihren Speicher entfernt -- nur meine beiden `remove`",
   len(REMOVED) - REMOVED_BEFORE == 2
   and all(k in a.load_registry()["instances"] for k in keys if k.endswith("-03")))

USERDB["solo"] = {"tenant": TID, "active": True, "deactivate_at": "2999-01-01T00:00:00Z",
                  "delete_at": "", "reason": ""}
USERDB["solo2"] = {"tenant": TID, "active": True, "deactivate_at": "",
                   "delete_at": "2999-01-01T00:00:00Z", "reason": ""}
sw = sweep(3000, 2, 1)
ok("Termine auch ausserhalb einer Kohorte: der Lauf handelt (deaktivieren, loeschen)",
   USERDB["solo"]["active"] is False and "solo2" not in USERDB, sw)

# ------------------------------------------------ benannte Geheimnisse
print("")
print("Benannte Geheimnisse -- gespeichert, aufgelistet, nie zurueckgegeben")
a.load_cohort_secrets = REAL_LOAD_SECRETS
sys.stdin = io.StringIO("sk-echt-999" + chr(10))
code, out = cli("secret", target="set", second="schule-key", stdin=True)
ok("secret set --stdin speichert, ohne den Wert zu zeigen",
   code is None and "sk-echt-999" not in out and "schule-key" in out, out)
code, out = cli("secret", target="list")
ok("secret list nennt Namen, nie Werte",
   "schule-key" in out and "sk-echt-999" not in out, out)
SF = a.COHORT_SECRETS_FILE
ok("die Datei liegt im Verzeichnis des Wirts (0600, wo das Dateisystem es kennt)",
   os.path.isfile(SF) and (os.name == "nt" or stat.S_IMODE(os.stat(SF).st_mode) == 0o600))
code, out = cli("secret", target="set", second="Grosse Buchstaben", stdin=True)
ok("ein Name mit Leerzeichen und Grossbuchstaben wird abgelehnt", code == 1, out)
code, out = cli("secret", target="remove", second="schule-key")
code2, out2 = cli("secret", target="list")
ok("secret remove entfernt", "schule-key" not in out2, out2)

# ------------------------------------------------ Quelltext
print("")
print("Quelltext -- die Stellen, die Tests nicht ausfuehren koennen")
APPCTL = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
BIN = open(os.path.join(HERE, "..", "bin", "oaap"), encoding="utf-8").read()
ok("_install_from_dir ruft den Haken NACH save_env und VOR dem ersten Container",
   APPCTL.index('hook(name, ident, services, m)')
   > APPCTL.index("save_env(name, env, ident)")
   and APPCTL.index('hook(name, ident, services, m)')
   < APPCTL.index("recreate_instance_containers(name, services, m.get(\"storage\") or [], granted,"))
ok("ein Redeploy behaelt den Kohorten-Vermerk",
   '(inst or {}).get("cohort") or preset.get("cohort")' in APPCTL)
ok("bin/oaap kennt `oaap cohort`", 'cohort)      exec python3 "$APP_DIR/appctl.py" cohort' in BIN)
INSTALL_SH = open(os.path.join(HERE, "..", "install.sh"), encoding="utf-8").read()
MIGRATE_SH = open(os.path.join(PLATFORM, "migrate.sh"), encoding="utf-8").read()
ok("der Lauf hat einen Zeitgeber -- bei Neuinstallation UND beim Update",
   "oaap-cohort-sweep.timer" in INSTALL_SH and "oaap-cohort-sweep.timer" in MIGRATE_SH
   and "cohort sweep" in INSTALL_SH and "cohort sweep" in MIGRATE_SH)
ok("der Zeitgeber holt verpasste Laeufe nach (Persistent=true)",
   "OnCalendar=*-*-* 04:40:00" in INSTALL_SH
   and INSTALL_SH.split("oaap-cohort-sweep.timer <<'EOF'")[1].split("EOF")[0].count("Persistent=true") == 1)
ok("`oaap uninstall` nimmt den Zeitgeber mit",
   "oaap-cohort-sweep" in BIN)
ok("die Kohorten-Geheimnisse liegen im Verzeichnis, das nur der Wirt liest",
   'COHORT_SECRETS_FILE = os.path.join(DEST_SECRETS_DIR' in APPCTL)

print("")
print("FAILED" if fails else "OK", f"({fails} Fehler)" if fails else "")
sys.exit(1 if fails else 0)
