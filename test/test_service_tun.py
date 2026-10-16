#!/usr/bin/env python3
"""Ein Tunnel in einem Container (Manifest 0.8, oaap.apps.runtime 0.2.42,
2.23, RFC-0060 Stufe 0).

Gemessen wird die Eigenschaft, nicht die Absicht:

  * `tun: true` besteht die Manifest-Pruefung, `"yes"` oder 1 nicht;
  * eine Installation OHNE `--confirm-tun` haelt an, BEVOR etwas gebaut
    wird -- kein `docker build`, kein `docker pull`, kein Register-Eintrag;
  * auch eine Tuer ausser der Kommandozeile (hochgeladenes Paket) wird
    beim ersten Mal abgewiesen, und nur die Kommandozeile setzt das Flag;
  * mit der Bestaetigung steht `tun: true` im Dienst-Eintrag und der
    echte `docker run` traegt `--device /dev/net/tun --cap-add NET_ADMIN`
    VOR dem Image -- und durch jede Tuer, die Container neu baut;
  * ein Redeploy, der das Recht behaelt, fragt nicht erneut;
  * ein zweiter Dienst derselben App, ein Dienst ohne `tun`, eine
    Generalprobe und ein handbearbeiteter Eintrag bekommen nichts;
  * nie `--privileged`, nie ein Host-Netz.

Braucht nur Python 3; Docker wird durch einen Rekorder ersetzt.
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import types
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
DATA = tempfile.mkdtemp(prefix="oaap-tun-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)

import appctl as a                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


# ------------------------------------------------------------ Testboden
RUNS = []


def fake_run(cmd, *x, **kw):
    RUNS.append(list(cmd))
    return types.SimpleNamespace(stdout="", stderr="", returncode=0)


a.run = fake_run
a.subprocess.run = lambda *x, **k: types.SimpleNamespace(
    stdout="", stderr="", returncode=0)
a.ensure_app_network = lambda name: "net-" + name
a.connect_gateway = lambda net: None
a.image_uid = lambda image: None
a.restore_links = lambda name: None
a.state_view_write = lambda *x, **k: None
a.reload_gateway = lambda: None
a.container_env = lambda container: None
os.makedirs(a.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(a.APPS_DIR, exist_ok=True)
a.ensure_default_tenant()


def docker_runs(container=None):
    return [r for r in RUNS if r[:2] == ["docker", "run"]
            and (container is None or container in r)]


def built():
    return [r for r in RUNS if r[:2] in (["docker", "build"], ["docker", "pull"])]


def granted(cmd):
    """Is the exact grant in this command, before the image?"""
    if "--device" not in cmd or "--cap-add" not in cmd:
        return False
    return (cmd[cmd.index("--device") + 1] == "/dev/net/tun"
            and cmd[cmd.index("--cap-add") + 1] == "NET_ADMIN"
            and cmd.index("--cap-add") < len(cmd) - 1)


def nothing_more(cmd):
    return ("--privileged" not in cmd and "host" not in
            [cmd[i + 1] for i, x in enumerate(cmd[:-1]) if x == "--network"]
            and cmd.count("--cap-add") <= 1 and cmd.count("--device") <= 1)


def validate(doc):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            a.validate_manifest(doc)
        return True, buf.getvalue()
    except SystemExit:
        return False, buf.getvalue()


def manifest_doc(**svc):
    return {"oaap_manifest": "0.8",
            "app": {"id": "vpn-client", "name": "VPN", "version": "0.1.0",
                    "type": "image"},
            "services": {"vpn": {"image": "x/vpn:1", "port": 8080, **svc}},
            "routes": [{"path": "/", "roles": ["user"]}],
            "health": {"path": "/"}}


def write_pkg(version="0.1.0", tun="true", second=False):
    lines = ['oaap_manifest: "0.8"', "app:", "  id: vpn-client",
             "  name: VPN", f"  version: {version}", "  type: image",
             "services:", "  vpn:", "    image: x/vpn:1", "    port: 8080"]
    if tun is not None:
        lines.append(f"    tun: {tun}")
    if second:
        lines += ["  ui:", "    image: x/ui:1", "    port: 80"]
    lines += ["routes:", "  - path: /", "    roles: [user]"]
    if second:
        lines.append("    service: ui")
    lines += ["health:", "  path: /", ""]
    pkg = tempfile.mkdtemp(prefix="oaap-tun-pkg-")
    with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return pkg


def install(pkg, name, confirm=False, rehearsal=None):
    args = argparse.Namespace(package=pkg, path="", ref="", name=name,
                              channel="test", store_source="", tenant="",
                              key="", ident=None, rehearsal=rehearsal,
                              bind=[], confirm_tun=confirm)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            a._install_from_dir(pkg, args, {"kind": "local", "url": pkg,
                                            "path": ""})
    except SystemExit:
        return False, buf.getvalue()
    return True, buf.getvalue()


def record(name):
    return (a.load_registry()["instances"].get(name) or {})


# ------------------------------------------------------------ Werte
print("")
print("Werte -- was das Manifest annimmt und was es ablehnt")
ok("Manifest 0.8 ist dem Knoten bekannt", a.MANIFEST_MINOR >= 8, a.MANIFEST_MINOR)
ok("tun: true wird angenommen", validate(manifest_doc(tun=True))[0])
ok("tun: false wird angenommen", validate(manifest_doc(tun=False))[0])
ok("ohne tun ist das Manifest wie bisher gueltig", validate(manifest_doc())[0])
for bad in ("yes", "true", 1, None):
    acc, out = validate(manifest_doc(tun=bad))
    ok(f"tun: {bad!r} wird bei der Manifest-Pruefung abgelehnt",
       not acc and "services.vpn.tun" in out, out)

# ------------------------------------------------------------ die Frage
print("")
print("Die Frage -- ohne Bestaetigung wird nichts gebaut")
pkg = write_pkg()
del RUNS[:]
acc, out = install(pkg, "vpn")
ok("ohne --confirm-tun: abgelehnt", not acc and "--confirm-tun" in out, out)
ok("... und die Ablehnung sagt, was die App koennte",
   "/dev/net/tun" in out and "NET_ADMIN" in out and "OWN" in out, out)
ok("... und es wurde NICHTS gebaut oder gezogen", not built(), RUNS)
ok("... und kein Container, kein Register-Eintrag",
   not docker_runs() and not record("vpn"), (RUNS, record("vpn")))

# Eine Tuer ausser der Kommandozeile: das hochgeladene Paket (RFC-0019).
zpath = os.path.join(tempfile.mkdtemp(prefix="oaap-tun-zip-"), "vpn.zip")
with zipfile.ZipFile(zpath, "w") as z:
    z.write(os.path.join(pkg, "oaap-app.yaml"), "oaap-app.yaml")
del RUNS[:]
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.install_artifact("vpn-upload", zpath, None, channel="test")
    refused = False
except SystemExit:
    refused = True
ok("ein hochgeladenes Paket (keine Kommandozeile) wird beim ersten Mal "
   "abgewiesen", refused and "--confirm-tun" in buf.getvalue()
   and not record("vpn-upload") and not built(), buf.getvalue())

SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("nur die Kommandozeile setzt confirm_tun (argparse + die eine Abfrage)",
   SRC.count("confirm_tun") == 2
   and 'getattr(args, "confirm_tun", False)' in SRC
   and 'dest="confirm_tun"' in SRC, SRC.count("confirm_tun"))

# ------------------------------------------------------------ mit Bestaetigung
print("")
print("Mit Bestaetigung -- Register und Befehl")
del RUNS[:]
acc, out = install(pkg, "vpn", confirm=True)
ok("mit --confirm-tun: installiert", acc, out)
svcs = a.instance_services(record("vpn"))
ok("der Dienst-Eintrag traegt tun: true", svcs and svcs[0].get("tun") is True, svcs)
run = docker_runs("oaap-app-vpn")[-1] if docker_runs("oaap-app-vpn") else []
ok("der docker run traegt --device /dev/net/tun --cap-add NET_ADMIN",
   granted(run), run)
ok("... VOR dem Image (danach waere es ein Argument der App)",
   granted(run) and "x/vpn:1" in run
   and run.index("--cap-add") < run.index("x/vpn:1")
   and run.index("--device") < run.index("x/vpn:1"), run)
ok("... und nichts darueber hinaus (kein --privileged, kein Host-Netz)",
   nothing_more(run), run)

# Jede Tuer, die neu baut, liest den Eintrag.
del RUNS[:]
a.recreate_instance_containers("vpn", a.instance_services(record("vpn")), [])
ok("Neuaufbau aus dem Register (Konfiguration speichern, Neustart) traegt es",
   granted(docker_runs()[-1]), RUNS)

# Redeploy fragt nicht erneut.
pkg2 = write_pkg(version="0.1.1")
del RUNS[:]
acc, out = install(pkg2, "vpn")
ok("ein Redeploy, der das Recht behaelt, fragt NICHT erneut", acc, out)
ok("... und der neue Container hat es wieder", granted(docker_runs()[-1]), RUNS)

# Ein Redeploy, der das Recht abgibt.
pkg3 = write_pkg(version="0.1.2", tun=None)
del RUNS[:]
acc, out = install(pkg3, "vpn")
ok("ein Redeploy ohne tun: keine Frage, kein Recht mehr",
   acc and not granted(docker_runs()[-1])
   and "tun" not in a.instance_services(record("vpn"))[0], (out, RUNS))
del RUNS[:]
acc, out = install(write_pkg(version="0.1.3"), "vpn")
ok("... und wer es danach wieder will, wird wieder gefragt",
   not acc and "--confirm-tun" in out and not built(), out)

# ------------------------------------------------------------ was nichts bekommt
print("")
print("Was nichts bekommt")
del RUNS[:]
acc, out = install(write_pkg(tun=None), "plain")
ok("ein Dienst ohne tun: keine Frage, kein Flag",
   acc and docker_runs() and "--device" not in docker_runs()[-1]
   and "--cap-add" not in docker_runs()[-1], (out, RUNS))
del RUNS[:]
acc, out = install(write_pkg(tun="false"), "off")
ok("tun: false: keine Frage, kein Flag",
   acc and "--cap-add" not in docker_runs()[-1], (out, RUNS))

del RUNS[:]
acc, out = install(write_pkg(second=True), "two")
ok("Mehr-Container-App: die Frage nennt den einen Dienst",
   not acc and "services.vpn" in out and "services.ui" not in out, out)
del RUNS[:]
acc, out = install(write_pkg(second=True), "two", confirm=True)
vpn = docker_runs("oaap-app-two-vpn")
ui = docker_runs("oaap-app-two-ui")
ok("Mehr-Container-App: nur der Dienst mit tun bekommt das Recht",
   acc and vpn and ui and granted(vpn[-1]) and "--cap-add" not in ui[-1],
   (out, vpn, ui))

del RUNS[:]
acc, out = install(write_pkg(), "probe",
                   rehearsal={"of": "vpn", "expires": "2026-10-23"})
run = docker_runs("oaap-app-probe")
ok("Generalprobe: nicht gefragt, angelegt", acc, out)
ok("... aber ohne Recht und ohne Eintrag (D6), und sie sagt es",
   run and not granted(run[-1]) and "--cap-add" not in run[-1]
   and "tun" not in a.instance_services(record("probe"))[0]
   and "rehearsal gets no tunnel" in out, (out, run))
reg = a.load_registry()
reg["instances"]["probe"]["services"][0]["tun"] = True
a.save_registry(reg)
del RUNS[:]
a.recreate_instance_containers("probe", a.instance_services(record("probe")), [])
ok("... auch wenn jemand tun in den Eintrag der Generalprobe schreibt",
   not granted(docker_runs()[-1]), RUNS)

print("")
print("Handbearbeitete Eintraege")
for val in ("yes", 1, "true", {"x": 1}):
    ok(f"tun: {val!r} im Eintrag: kein Flag, kein kaputter Befehl",
       a.tun_args({"tun": val}) == [])
ok("tun: True im Eintrag: genau das Recht",
   a.tun_args({"tun": True}) == ["--device", "/dev/net/tun",
                                 "--cap-add", "NET_ADMIN"])
ok("kein Eintrag, kaputter Eintrag: nichts",
   a.tun_args(None) == [] and a.tun_args({}) == [])
ok("die eine Funktion, die Container baut, fragt den Dienst-Eintrag",
   "tun = tun_args(s, rehearsal=" in SRC and "*limits, *shm, *tun," in SRC)

print("")
print("FAIL" if fails else "OK", f"-- {fails} Fehler" if fails else "")
sys.exit(1 if fails else 0)
