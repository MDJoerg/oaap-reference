#!/usr/bin/env python3
"""Ein Eimer je Route (Nachtrag zu RFC-0010, 29.09.2026).

Der Satz, den diese Datei verteidigt:

    Was auf der einen oeffentlichen Route los ist, nimmt der anderen
    nichts weg.

Der Anlass (Brief des Handball-Infoboards vom 28.09.): In einer Halle
fragen Anzeigegeraete `/display` ab, und am Spieltag stimmen sechzig
Kinder-Handys auf `/vote` ab -- alle im Hallen-WLAN, das das Internet
als EINE Adresse sieht. Mit einem Eimer je Instanz leert die Abstimmung
das Budget der Anzeige, und die Tafel wird mitten im Spiel leer.

Wir haben uns dabei schon einmal geirrt: Am 26.09. haben wir die
geschuetzte Route `/manage` gemessen und daraus auf die Geraete-Kanaele
geschlossen, die oeffentlich sind. Deshalb traegt der Test hier die
Routen des Infoboards selbst, nicht eine Ersatzroute.

Geprueft wird:

    1. Jede oeffentliche Route fragt die Bremse mit IHRER Route.
    2. Der Wert der Instanz gilt je Route; ein Wert je Route ueberschreibt
       ihn, auch "aus" -- und auch dann, wenn die Instanz aus ist.
    3. Alle Erzeuger einer Site reichen die Werte je Route durch.
    4. Der Befehl: setzen, abschalten, zuruecksetzen, und eine Route,
       die nicht oeffentlich ist, wird abgelehnt.
    5. Ein Redeploy behaelt die Werte je Route.
    6. Das Update schreibt eine alte Site ohne Route neu -- einmal.
    7. identity zaehlt je Route getrennt, ueber alle Zugaenge gemeinsam,
       und eine alte Site ohne Route bleibt beim Eimer der Instanz.

Aufruf: python3 test/test_throttle_routes.py
Teil 7 braucht flask (wie der Identity-Dienst selbst).
"""
import argparse
import contextlib
import importlib
import io
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-throttle-routes-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def capture(fn, *a):
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fn(*a)
    except SystemExit as e:
        code = e.code or 1
    return buf.getvalue(), code


URI = re.compile(r"uri /throttle\?scope=(\S+?)&limit=(\d+)&window=(\d+)"
                 r"(?:&route=(\S+))?")


def brakes(text):
    """route -> (limit, window) for every /throttle call in a site."""
    out = {}
    for scope, limit, window, route in URI.findall(text):
        out.setdefault(route or "", []).append((int(limit), int(window)))
    return out


# --- Docker-Attrappen: jeder Aufruf gelingt, nichts laeuft
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="",
                                                    returncode=0)
m.reload_gateway = lambda: None
m.image_uid = lambda img: None
m.recreate_instance_containers = lambda name, *a, **kw: None
m.container_env = lambda container: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()

# Die Routen des Infoboards, abgeschrieben aus SEINEM Manifest
# (oaap-hbsha, hosts/oaap/oaap-app.yaml, Stand 29.09.): die Schaltzentrale
# `/` hinter der Anmeldung, die Geraete-Kanaele, die Bundles, die
# Versionsanzeige und die Kinder-App `/tasks` oeffentlich. Dazu `/vote`,
# die Abstimmungsseite aus dem Brief vom 26.09. -- angekuendigt, noch
# nicht im Manifest, und der Anlass fuer diesen Nachtrag.
ROUTES = [
    ("/", "admin, keyuser, user"),
    ("/display", "public"), ("/control", "public"), ("/coach", "public"),
    ("/assets", "public"), ("/api/version", "public"), ("/tasks", "public"),
    ("/vote", "public"),
]
PUBLIC = sorted(p for p, r in ROUTES if r == "public")


def manifest(routes):
    lines = ['oaap_manifest: "0.1"', "app:", "  id: board", "  name: Board",
             "  version: 0.1.0", "  type: native", "services:", "  web:",
             "    build: .", "    port: 80", "routes:"]
    for path, role in routes:
        lines += [f"  - path: {path}", f"    roles: [{role}]"]
    lines += ["health:", "  path: /healthz", ""]
    return "\n".join(lines)


pkg = tempfile.mkdtemp(prefix="oaap-throttle-pkg-")


def install(routes):
    with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
        f.write(manifest(routes))
    args = argparse.Namespace(package=pkg, path="", ref="", name="board",
                              channel="test", store_source="", tenant="",
                              key="", ident=None, rehearsal=None, bind=[])
    return capture(m._install_from_dir, pkg, args,
                   {"kind": "local", "url": pkg, "path": ""})


def site():
    with open(os.path.join(m.CADDY_APPS_DIR, "board.caddy"),
              encoding="utf-8") as f:
        return f.read()


def throttle(action, rate=None, route=None):
    return capture(m.cmd_throttle, argparse.Namespace(
        action=action, name="board", rate=rate, route=route))


print("")
print("1. Jede oeffentliche Route fragt mit ihrer eigenen Route")

said, code = install(ROUTES)
ok("die Instanz ist angelegt (Docker durch Attrappen ersetzt)",
   code == 0 and "board" in m.load_registry()["instances"], said)
b = brakes(site())
ok("jede der sieben oeffentlichen Routen hat genau eine Bremse, mit ihrem Pfad",
   sorted(b) == PUBLIC and all(len(v) == 1 for v in b.values()), b)
ok("die Schaltzentrale `/` fragt die Bremse nicht (sie hat /verify)",
   "/" not in b and "/verify?" in site())
ok("und alle mit dem Wert der Instanz, hier dem Standard 300/60",
   all(v == [(300, 60)] for v in b.values()), b)

print("")
print("2. Werte je Route")

said, code = throttle("set", "600/60", "/vote")
b = brakes(site())
ok("`set --route /vote 600/60` gilt fuer /vote ...",
   code == 0 and b.get("/vote") == [(600, 60)], (said, b))
ok("... und nur dort: /display bleibt beim Wert der Instanz",
   b.get("/display") == [(300, 60)] and b.get("/tasks") == [(300, 60)], b)

said, code = throttle("off", route="/coach")
b = brakes(site())
ok("`off --route /coach` nimmt nur /coach die Bremse, mit Warnung",
   code == 0 and "/coach" not in b and "WARNING" in said
   and b.get("/control") == [(300, 60)], (said, b))

said, code = throttle("set", "120/60")
b = brakes(site())
ok("ein neuer Wert der Instanz trifft die Routen ohne eigenen Wert ...",
   b.get("/display") == [(120, 60)] and b.get("/assets") == [(120, 60)], b)
ok("... und laesst die Routen mit eigenem Wert in Ruhe",
   b.get("/vote") == [(600, 60)] and "/coach" not in b, b)

said, code = throttle("off")
b = brakes(site())
ok("Instanz aus: nur die Route mit eigenem Wert bleibt gebremst",
   sorted(b) == ["/vote"] and "keep it" in said, (said, b))

said, code = throttle("reset")
b = brakes(site())
ok("`reset` ohne Route: zurueck zum Standard der Plattform",
   b.get("/display") == [(300, 60)]
   and "throttle" not in m.load_registry()["instances"]["board"], b)

said, code = throttle("set", "600/60", "/")
ok("eine Route, die nicht oeffentlich ist, wird abgelehnt -- dort bremst nichts",
   code != 0 and "not a public route" in said, said)
said, code = throttle("set", "600/60", "/gibtsnicht")
ok("eine Route, die es nicht gibt, ebenso", code != 0, said)
said, code = throttle("set", "600/60", "/vote/")
ok("ein Schraegstrich am Ende wird verziehen: /vote/ ist /vote",
   code == 0 and brakes(site()).get("/vote") == [(600, 60)], said)

said, code = throttle("show")
ok("`show` nennt jede oeffentliche Route mit ihrem Wert und woher er kommt",
   code == 0 and re.search(r"/vote\s+600/60 s\s+\(set for this route\)", said)
   and re.search(r"/coach\s+off\s+\(set for this route\)", said)
   and re.search(r"/display\s+300/60 s\s+\(instance value\)", said)
   and not re.search(r"^\s+/\s", said, re.M), said)

print("")
print("3. Alle Erzeuger einer Site reichen die Werte je Route durch")

# Fuenf Stellen schreiben eine Site: LAN-Site, Install, Migration und
# die beiden externen Sites (automatischer Name, eigener Name). Hat eine
# den blanken Wert der Instanz statt des Plans, bremst sie jede Route
# mit dem Wert der Instanz -- still, und nur an diesem einen Zugang.
src = open(m.__file__, encoding="utf-8").read()


def call_args(text, name):
    """The argument text of every CALL of `name(` (not its def)."""
    out = []
    for mt in re.finditer(r"(?<![\w.])" + name + r"\(", text):
        if text[max(0, mt.start() - 4):mt.start()] == "def ":
            continue
        depth, i = 1, mt.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        out.append(text[mt.end():i - 1])
    return out


generators = call_args(src, "caddy_site") + call_args(src, "site_body")
# Wer einen Wert aus dem Register holt (throttle_of/throttle_plan), ist
# ein Erzeuger; caddy_site reicht nur weiter, was es bekommen hat.
braking = [a for a in generators if re.search(r"throttle_\w+\(", a)]
ok("jeder Erzeuger, der eine Bremse weitergibt, gibt den Plan weiter",
   braking and all("throttle_plan(" in a for a in braking),
   [a for a in braking if "throttle_plan(" not in a])
ok("und es sind die fuenf bekannten (LAN, Install, Migration, zwei externe)",
   len(braking) == 5, len(braking))

reg = m.load_registry()
inst = reg["instances"]["board"]
inst["address"] = "board.example.org"
m.save_registry(reg)
ext = "\n".join(m.site_body(inst["routes"], inst["container"], inst["svc_port"],
                            None, "board", m.throttle_plan(inst), "edge"))
ok("auch eine externe Site (hinter einem Edge) bremst /vote mit 600/60",
   brakes(ext).get("/vote") == [(600, 60)]
   and "{http.request.header.X-Forwarded-For}" in ext, brakes(ext))

print("")
print("4. Ein Redeploy behaelt die Werte je Route")

said, code = install(ROUTES)
inst = m.load_registry()["instances"]["board"]
b = brakes(site())
ok("nach dem Redeploy stehen die Werte je Route noch im Register",
   inst.get("throttle_routes") == {"/vote": {"limit": 600, "window": 60},
                                   "/coach": {}}, inst.get("throttle_routes"))
ok("und in der neu geschriebenen Site",
   b.get("/vote") == [(600, 60)] and "/coach" not in b, b)

said, code = install([r for r in ROUTES if r[0] != "/coach"])
said, code = throttle("show")
ok("verschwindet eine Route, bleibt ihr Wert -- `show` sagt, dass er nichts tut",
   "Override for '/coach'" in said and "does nothing" in said
   and "/coach" in (m.load_registry()["instances"]["board"]
                    .get("throttle_routes") or {}), said)
said, code = throttle("reset", route="/coach")
ok("... und `reset --route` wird ihn wieder los, obwohl die Route keine "
   "oeffentliche mehr ist (genau das raet `show`)",
   code == 0 and "/coach" not in (m.load_registry()["instances"]["board"]
                                  .get("throttle_routes") or {}), said)
said, code = throttle("off", route="/coach")
ok("setzen laesst sich eine verschwundene Route dagegen nicht",
   code != 0 and "not a public route" in said, said)

print("")
print("5. Das Update schreibt eine alte Site ohne Route neu -- einmal")

legacy = site().replace("&route=", "&xroute=")
legacy = re.sub(r"&xroute=\S+", "", legacy)
with open(os.path.join(m.CADDY_APPS_DIR, "board.caddy"), "w",
          encoding="utf-8") as f:
    f.write(legacy)
ok("(Vorbereitung) die alte Site fragt die Bremse ohne Route",
   "/throttle?" in site() and "&route=" not in site())
out, code = capture(m.cmd_migrate_tenant_routes, None)
ok("die Migration schreibt sie neu, und danach traegt jede Bremse ihre Route",
   "rate brake per route" in out and "" not in brakes(site())
   and brakes(site()).get("/vote") == [(600, 60)], (out, brakes(site())))
before = site()
out, code = capture(m.cmd_migrate_tenant_routes, None)
ok("ein zweiter Lauf findet nichts mehr (keine Endlosschleife wie bei forgejo)",
   out.strip() == "" and site() == before, out)

print("")
print("6. identity zaehlt je Route")

try:
    import flask  # noqa: F401
except ImportError:
    flask = None
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden")

if flask:
    ID_DATA = tempfile.mkdtemp(prefix="oaap-throttle-id-")
    os.environ["SESSION_SECRET"] = "test-session-secret"
    os.environ["SETUP_TOKEN"] = "test-setup-token"
    os.environ["INTERNAL_API_KEY"] = "test-internal-key"
    os.environ["OAAP_IDENTITY_DATA_DIR"] = ID_DATA
    sys.path.insert(0, os.path.join(HERE, "..", "platform", "services",
                                    "identity"))
    sys.modules.pop("app", None)
    ident = importlib.reload(importlib.import_module("app"))
    # Die Zaehlung gebremster Anfragen braucht fcntl (POSIX) und ist
    # nicht, was hier geprueft wird.
    ident._braked_note = lambda scope: None
    ident._RATE.clear()
    c = ident.app.test_client()

    def hit(route, client="203.0.113.7", scope="board"):
        qs = {"scope": scope, "limit": "3", "window": "60"}
        if route is not None:
            qs["route"] = route
        return c.get("/throttle", query_string=qs,
                     headers={"X-OAAP-Client": client}).status_code

    votes = [hit("/vote") for _ in range(5)]
    ok("sechzig Handys in klein: /vote ist nach drei Anfragen gebremst",
       votes == [204, 204, 204, 429, 429], votes)
    ok("die Anzeige an derselben Adresse bekommt trotzdem ihre Antwort",
       hit("/display") == 204)
    ok("ein anderes Hallen-WLAN hat einen eigenen Eimer auf /vote",
       hit("/vote", client="198.51.100.9") == 204)
    ok("dieselbe Route an derselben Adresse bleibt EIN Eimer, egal ueber "
       "welchen Zugang (der Zugang ist nicht Teil des Schluessels)",
       hit("/vote") == 429)
    ident._RATE.clear()
    old = [hit(None) for _ in range(3)] + [hit(None)]
    ok("eine alte Site ohne Route zaehlt weiter in einem Eimer je Instanz",
       old == [204, 204, 204, 429], old)
    ok("und der ist ein anderer als der jeder Route -- ein halb migrierter "
       "Knoten bremst niemanden doppelt",
       hit("/vote") == 204)

print("")
print(f"{'FEHLER: ' + str(fails) + ' Pruefungen' if fails else 'ALL PASS'}")
sys.exit(1 if fails else 0)
