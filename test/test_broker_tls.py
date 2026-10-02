#!/usr/bin/env python3
"""Der TLS-Anschluss des Brokers und der Klartext-Port (RFC-0054, Stufe 3).

Was ohne Docker und ohne Knoten beweisbar ist:

    broker_certs: woher das Zertifikat kommt (Caddy, sonst die eigene CA des
    Knotens), dass es KOPIERT wird (Besitzer, Modus), dass der CA-Schluessel
    nie im eingehaengten Verzeichnis liegt, Erneuerung, Wechsel der Quelle.
    appctl: das Profil `broker-plain` -- abgelehnt ohne `broker`, abgelehnt
    ohne private Adresse, und in BEIDEN Faellen wird nichts gespeichert;
    `lan_address()` aus der Routingtabelle; `broker sync` mit Neustart beim
    ersten Zertifikat und SIGHUP bei einer Erneuerung (so gemessen am
    02.10.); `oaap broker show|ca`.
    Compose/Skripte: der TLS-Listener nur, wenn ein Zertifikat da ist, das
    Overlay fuer 1883 scheitert laut ohne Adresse statt auf allen
    Schnittstellen zu veroeffentlichen, und alle drei Stellen, die den
    Broker neu erzeugen, nennen dieselben Dateien.

Was diese Datei NICHT pruefen kann: dass Mosquitto das kopierte Zertifikat
wirklich bedient und eine Erneuerung per SIGHUP uebernimmt (am 02.10. mit
einem Wegwerf-Container gemessen, RFC-0054 1.7) und TLS mit einem echten
Caddy-Zertifikat.

Aufruf: python3 test/test_broker_tls.py
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, os.path.join(PLATFORM, "services"))
sys.path.insert(0, PLATFORM)

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


import broker_certs as bc                                      # noqa: E402

HAVE_SSL = shutil.which("openssl") is not None
if not HAVE_SSL:
    print("SKIP  openssl fehlt -- der Zertifikatsteil laesst sich hier nicht pruefen.")
else:
    print("=== das Zertifikat: eigene CA des Knotens ===")
    base = tempfile.mkdtemp(prefix="oaap-bt-")
    cd, ca, caddy = (os.path.join(base, x) for x in ("certs", "ca", "caddy"))
    r = bc.sync(cd, ca, caddy, "oaap-test", "", "10.10.10.96")
    ok("das erste Mal: eingebaut, aus der eigenen CA, und `first` sagt: Neustart noetig",
       r["action"] == "installed" and r["source"] == "local" and r["first"], r)
    ok("die Namen: `broker`, der Rechnername, die LAN-Adresse",
       r["names"] == sorted(["broker", "oaap-test", "10.10.10.96"]), r["names"])
    ok("Gueltigkeit rund ein Jahr", 380 <= r["days"] <= 398, r["days"])
    ok("das Verzeichnis, das der Broker liest, enthaelt NUR Zertifikat, Schluessel, Notiz",
       sorted(os.listdir(cd)) == ["server.crt", "server.key", "source.json"], os.listdir(cd))
    ok("... und nie den Schluessel der CA (der liegt in einem Verzeichnis, das niemand einhaengt)",
       "ca.key" not in os.listdir(cd) and os.path.isfile(os.path.join(ca, "ca.key")))
    if os.name != "nt":
        ok("der Schluessel hat Modus 0600, das Zertifikat 0644",
           oct(os.stat(os.path.join(cd, "server.key")).st_mode & 0o777) == "0o600"
           and oct(os.stat(os.path.join(cd, "server.crt")).st_mode & 0o777) == "0o644")
        ok("die CA liegt in einem Verzeichnis 0700, ihr Schluessel 0600",
           oct(os.stat(ca).st_mode & 0o777) == "0o700"
           and oct(os.stat(os.path.join(ca, "ca.key")).st_mode & 0o777) == "0o600")
    v = subprocess.run(["openssl", "verify", "-CAfile", os.path.join(ca, "ca.crt"),
                        "-verify_hostname", "oaap-test", os.path.join(cd, "server.crt")],
                       capture_output=True, text=True)
    ok("ein Client, der der CA vertraut, prueft das Zertifikat durch (Kette und Name)",
       v.returncode == 0 and "OK" in v.stdout, v.stdout + v.stderr)
    v = subprocess.run(["openssl", "verify", "-CAfile", os.path.join(ca, "ca.crt"),
                        "-verify_hostname", "anderer-name", os.path.join(cd, "server.crt")],
                       capture_output=True, text=True)
    ok("... und ein falscher Name faellt durch", v.returncode != 0)
    ca_pem = open(os.path.join(ca, "ca.crt")).read()
    r2 = bc.sync(cd, ca, caddy, "oaap-test", "", "10.10.10.96")
    ok("noch einmal: nichts zu tun", r2["action"] == "unchanged" and not r2["first"], r2)
    r3 = bc.sync(cd, ca, caddy, "oaap-test", "", "10.10.10.99")
    ok("die LAN-Adresse hat sich geaendert: neu ausgestellt, KEIN Neustart noetig (SIGHUP)",
       r3["action"] == "renewed" and not r3["first"] and "10.10.10.99" in r3["names"], r3)
    ok("... von derselben CA (die Clients muessen nichts neu bekommen)",
       open(os.path.join(ca, "ca.crt")).read() == ca_pem)
    old = bc.RENEW_DAYS
    bc.RENEW_DAYS = 10_000
    r4 = bc.sync(cd, ca, caddy, "oaap-test", "", "10.10.10.99")
    bc.RENEW_DAYS = old
    ok("wenige Tage vor dem Ende: erneuert", r4["action"] == "renewed", r4)

    print("\n=== das Zertifikat: das des Gateways, wenn es eines hat ===")
    ok("ohne Namen, ohne Datei, ohne Schluessel: keines",
       bc.caddy_cert_for(caddy, "") is None and bc.caddy_cert_for(caddy, "x.example.org") is None)
    host = "oaap.example.org"
    d = os.path.join(caddy, "caddy", "certificates", "acme-v02.api.letsencrypt.org-directory", host)
    os.makedirs(d)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout",
                    os.path.join(d, host + ".key"), "-out", os.path.join(d, host + ".crt"),
                    "-days", "60", "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}"],
                   capture_output=True, check=True)
    ok("gefunden, wo Caddy es ablegt", bc.caddy_cert_for(caddy, host) is not None)
    os.rename(os.path.join(d, host + ".key"), os.path.join(d, "weg.key"))
    ok("ohne seinen Schluessel gilt es nicht", bc.caddy_cert_for(caddy, host) is None)
    os.rename(os.path.join(d, "weg.key"), os.path.join(d, host + ".key"))
    r5 = bc.sync(cd, ca, caddy, "oaap-test", host, "10.10.10.99")
    ok("gibt es eines fuer den Aussennamen, wird DAS genommen (Quelle wechselt, SIGHUP genuegt)",
       r5["action"] == "switched" and r5["source"] == "caddy" and not r5["first"]
       and r5["names"] == [host], r5)
    ok("... und es ist eine KOPIE: dieselben Bytes, im Verzeichnis des Brokers",
       open(os.path.join(cd, "server.crt"), "rb").read() == open(os.path.join(d, host + ".crt"), "rb").read()
       and open(os.path.join(cd, "server.key"), "rb").read() == open(os.path.join(d, host + ".key"), "rb").read())
    r6 = bc.sync(cd, ca, caddy, "oaap-test", host, "10.10.10.99")
    ok("unveraendert: nichts zu tun (kein Kopieren jede Minute)", r6["action"] == "unchanged", r6)
    later = time.time() + 3600
    os.utime(os.path.join(d, host + ".crt"), (later, later))
    r7 = bc.sync(cd, ca, caddy, "oaap-test", host, "10.10.10.99")
    ok("Caddy hat erneuert (neuer Zeitstempel): kopiert, SIGHUP genuegt",
       r7["action"] == "renewed" and not r7["first"], r7)
    cd2 = os.path.join(base, "certs2")
    r8 = bc.sync(cd2, ca, caddy, "oaap-test", host, "10.10.10.99")
    ok("auf einem Knoten ohne Zertifikat und mit dem von Caddy: erstes Mal heisst Neustart",
       r8["action"] == "installed" and r8["first"] and r8["source"] == "caddy", r8)

    print("\n=== Fehler ===")
    old_path = os.environ["PATH"]
    os.environ["PATH"] = ""
    try:
        bc.sync(os.path.join(base, "c3"), os.path.join(base, "a3"), caddy, "h", "", "10.0.0.1")
        ok("ohne openssl: ein Fehler mit Satz, kein Absturz mit Stapel", False)
    except bc.CertError as e:
        ok("ohne openssl: ein Fehler mit Satz, kein Absturz mit Stapel", "openssl" in str(e))
    finally:
        os.environ["PATH"] = old_path

print("\n=== appctl: das Profil `broker-plain` ===")
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-bt-data-")
import appctl as a                                              # noqa: E402

APP = tempfile.mkdtemp(prefix="oaap-bt-app-")
a.APP_DIR = APP
a.NODE_FILE = os.path.join(tempfile.mkdtemp(prefix="oaap-bt-node-"), "node.json")
calls = []
a._compose = lambda *x: calls.append(x)
ok("das Profil gibt es und sagt in seiner Beschreibung, dass NICHT verschluesselt wird",
   "broker-plain" in a.PROFILES and "NO TLS" in a.PROFILES["broker-plain"]
   and "private LAN address only" in a.PROFILES["broker-plain"])
ok("'broker' beschreibt 8883 (nicht mehr 1883) als das, was 'exposed' veroeffentlicht",
   "8883" in a.PROFILES["broker"] and "'broker-plain'" in a.PROFILES["broker"])


def node_cmd(action, profile, lan="192.168.1.20"):
    a.lan_address = lambda: lan
    ns = argparse.Namespace(action=action, profile=profile)
    buf, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            a.cmd_node(ns)
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue() + err.getvalue(), code


out, code = node_cmd("add-profile", "broker-plain")
ok("ohne 'broker' abgelehnt, und NICHTS gespeichert",
   code != 0 and "needs the 'broker' profile" in out and a.load_profiles() == [], out)
node_cmd("add-profile", "broker")
calls.clear()
out, code = node_cmd("add-profile", "broker-plain", lan=None)
ok("ohne private Adresse abgelehnt (ein Server nur mit oeffentlicher), nichts gespeichert, nichts neu erzeugt",
   code != 0 and "no private (LAN) address" in out and "broker-plain" not in a.load_profiles()
   and not calls, out)
out, code = node_cmd("add-profile", "broker-plain")
env = open(os.path.join(APP, ".env")).read() if os.path.exists(os.path.join(APP, ".env")) else ""
ok("mit 'broker' und privater Adresse: gespeichert, die Adresse in .env, der Broker neu erzeugt",
   code == 0 and "broker-plain" in a.load_profiles() and "BROKER_PLAIN_BIND=192.168.1.20" in env
   and any("up" in c and "broker" in c for c in calls), (out, env, calls))
ok("... und die Meldung sagt: nur diese Adresse, und im Klartext",
   "192.168.1.20 ONLY" in out and "in the clear" in out, out)
files = a._broker_compose_files()
ok("der Broker wird mit dem Klartext-Overlay erzeugt, ohne 'exposed' ohne das andere",
   any(f.endswith("docker-compose.broker-plain.yml") for f in files)
   and not any(f.endswith("broker-exposed.yml") for f in files), files)
out, code = node_cmd("show", None)
ok("`oaap node show` sagt in einer Zeile fuer sich, dass dieser Knoten Schluessel im Klartext annimmt",
   "NOTE: this node accepts MQTT keys IN THE CLEAR" in out and "192.168.1.20" in out, out)
out, code = node_cmd("remove-profile", "broker")
ok("'broker' laesst sich nicht entfernen, solange 'broker-plain' gilt", code != 0
   and "broker-plain" in out and "broker" in a.load_profiles(), out)
calls.clear()
out, code = node_cmd("remove-profile", "broker-plain")
env = open(os.path.join(APP, ".env")).read()
ok("entfernt: die Adresse verschwindet aus .env, der Broker wird ohne den Port neu erzeugt",
   code == 0 and "BROKER_PLAIN_BIND" not in env and "no longer published" in out
   and not any(f.endswith("broker-plain.yml") for f in a._broker_compose_files()), (out, env))
node_cmd("add-profile", "exposed")
ok("'exposed' bringt 8883 (TLS), nicht 1883",
   any(f.endswith("broker-exposed.yml") for f in a._broker_compose_files())
   and "TLS port (8883)" in node_cmd("remove-profile", "exposed")[0])

print("\n=== lan_address(): die Routingtabelle antwortet ===")
import socket                                                   # noqa: E402

real_socket = socket.socket


class FakeSock:
    def __init__(self, addr):
        self.addr = addr

    def connect(self, _a):
        if self.addr is None:
            raise OSError("kein Netz")

    def getsockname(self):
        return (self.addr, 0)

    def close(self):
        pass


src = read(PLATFORM, "appctl.py")
fn_src = "def lan_address():" + src.split("def lan_address():", 1)[1].split("\n\n\ndef ", 1)[0]
ns_ = {}
exec(compile(fn_src, "lan_address", "exec"), ns_)
for addr, want in (("192.168.1.20", "192.168.1.20"), ("10.10.10.96", "10.10.10.96"),
                   ("172.16.4.2", "172.16.4.2"), ("212.132.64.58", None),
                   ("127.0.0.1", None), (None, None)):
    socket.socket = lambda *x, _a=addr, **k: FakeSock(_a)
    try:
        ok(f"{addr}: {want}", ns_["lan_address"]() == want)
    finally:
        socket.socket = real_socket

print("\n=== `broker sync`: Neustart beim ersten Zertifikat, SIGHUP bei einer Erneuerung ===")
a.__dict__["lan_address"] = ns_["lan_address"]
a.lan_address = lambda: "192.168.1.20"
sh = []
real_run = a.run


class Res:
    def __init__(self, out=""):
        self.stdout = out


def fake_run(cmd, **kw):
    sh.append(cmd)
    return Res("abc123\n" if cmd[:2] == ["docker", "ps"] else "")


a.run = fake_run
a.load_external_conf = lambda: ("", "")
a.has_profile = lambda p: p in ("broker",)
verdicts = {}
for action, first, want in (("installed", True, "restart"), ("renewed", False, "HUP"),
                            ("switched", False, "HUP"), ("unchanged", False, None)):
    sh.clear()
    a.broker_certs.sync = lambda *x, _a=action, _f=first, **k: {
        "action": _a, "source": "local", "first": _f, "days": 390, "names": []}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        a.broker_sync()
    acts = [c for c in sh if c[:2] != ["docker", "ps"]]
    got = ("restart" if any(c[:2] == ["docker", "restart"] for c in acts) else
           "HUP" if any(c[:3] == ["docker", "kill", "-s"] and "HUP" in c for c in acts) else None)
    ok(f"{action} (first={first}): {want or 'nichts am Broker'}", got == want, (acts, buf.getvalue()))
sh.clear()
a.has_profile = lambda p: False
ok("ohne Profil 'broker': nichts, nicht einmal ein Aufruf", a.broker_sync() is None and not sh)
a.has_profile = lambda p: p == "broker"


def boom(*x, **k):
    raise bc.CertError("openssl is not available (test)")


a.broker_certs.sync = boom
err = io.StringIO()
with contextlib.redirect_stderr(err):
    a.broker_sync()
ok("ein Fehler beim Zertifikat bricht den Lauf nicht (er teilt sich die Einheit)",
   "sync skipped" in err.getvalue(), err.getvalue())
a.broker_certs.sync = lambda *x, **k: {"action": "unchanged", "source": "local", "first": False,
                                      "days": 300, "names": []}
a.has_profile = lambda p: p in ("broker", "broker-plain")
a.lan_address = lambda: "192.168.1.77"
a._read_platform_env = lambda k: "192.168.1.20"
envset, comp = [], []
a._set_platform_env = lambda k, v: envset.append((k, v))
a._apply_broker_ports = lambda: comp.append(1) or True
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    a.broker_sync()
ok("die LAN-Adresse hat sich geaendert: .env nachgezogen und der Broker mit dem neuen Bind neu erzeugt",
   envset == [("BROKER_PLAIN_BIND", "192.168.1.77")] and comp and "LAN address changed" in buf.getvalue(),
   (envset, buf.getvalue()))
a.lan_address = lambda: None
envset.clear()
a.broker_sync()
ok("... faellt die Adresse weg, wird NICHT geraten (kein Bind auf irgendetwas)", not envset)

print("\n=== `oaap broker ca` / `show` ===")
a.BROKER_CA_DIR = tempfile.mkdtemp(prefix="oaap-bt-ca-")
a.BROKER_CERT_DIR = tempfile.mkdtemp(prefix="oaap-bt-cd-")
buf, err = io.StringIO(), io.StringIO()
code = 0
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        a.cmd_broker(argparse.Namespace(action="ca"))
except SystemExit as e:
    code = e.code
ok("ohne eigene CA sagt `ca`, dass der Client keine Datei braucht, und endet mit Fehlercode",
   code == 1 and "needs no CA file" in err.getvalue(), err.getvalue())
with open(os.path.join(a.BROKER_CA_DIR, "ca.crt"), "w") as f:
    f.write("-----BEGIN CERTIFICATE-----\nXYZ\n-----END CERTIFICATE-----\n")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    a.cmd_broker(argparse.Namespace(action="ca"))
ok("mit CA gibt `ca` das Zertifikat aus (`oaap broker ca > ca.crt`) und nur das",
   buf.getvalue().startswith("-----BEGIN CERTIFICATE-----") and "KEY" not in buf.getvalue())
a.has_profile = lambda p: p == "broker"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    a.cmd_broker(argparse.Namespace(action="show"))
ok("show ohne Zertifikat sagt es und nennt, wie man eines macht",
   "no certificate yet" in buf.getvalue() and "plain port 1883: not published" in buf.getvalue()
   and "not published (needs the profile 'exposed')" in buf.getvalue(), buf.getvalue())

print("\n=== Compose, Overlays, Skripte ===")
dc = read(PLATFORM, "docker-compose.yml")
broker = dc.split("\n  broker:\n", 1)[1].split("\n  relay:", 1)[0]
ok("der Broker haengt das Zertifikatsverzeichnis NUR lesend ein",
   '"${OAAP_DATA_DIR}/data/broker-certs:/mosquitto/certs:ro"' in broker)
ok("der TLS-Listener entsteht nur, wenn Zertifikat UND Schluessel lesbar da sind",
   "if [ -r /mosquitto/certs/server.crt ] && [ -r /mosquitto/certs/server.key ]; then" in broker
   and "listener 8883" in broker)
ok("... steht am ENDE der Konfiguration (certfile/keyfile gehoeren zum Listener davor)",
   broker.index("sed ") < broker.index("listener 8883") < broker.index("exec /usr/sbin/mosquitto"))
ok("... mit TLS 1.2 oder neuer", "tls_version tlsv1.2" in broker)
ok("der Broker selbst veroeffentlicht weiterhin keinen Port", "ports:" not in broker)
ok("der Ordner der CA wird nirgends eingehaengt", "broker-ca" not in dc)
plain = read(PLATFORM, "docker-compose.broker-plain.yml")
ok("das Klartext-Overlay scheitert LAUT ohne Adresse (`:?`), statt auf allen Schnittstellen zu veroeffentlichen",
   "${BROKER_PLAIN_BIND:?" in plain and '"1883:1883"' not in plain)
ok("... und bindet an die Adresse", ":1883:1883" in plain)
mig, upd, ins = (read(PLATFORM, "migrate.sh"), read(PLATFORM, "update.sh"),
                 read(HERE, "..", "install.sh"))
ok("migrate.sh, update.sh und appctl nennen dasselbe Klartext-Overlay",
   "docker-compose.broker-plain.yml" in mig and "docker-compose.broker-plain.yml" in upd
   and "docker-compose.broker-plain.yml" in read(PLATFORM, "appctl.py"))
ok("die minuetliche Einheit macht `broker sync` -- mit '-', ein Fehler stoppt die anderen nicht",
   "ExecStart=-$PYTHON3 $APP_DIR/appctl.py broker sync" in mig
   and "ExecStart=-$PYTHON3 $OAAP_DATA_DIR/app/appctl.py broker sync" in ins)
ok("das Verb `oaap broker` kennt der Wrapper",
   'broker)      exec python3 "$APP_DIR/appctl.py" broker "$@" ;;' in read(HERE, "..", "bin", "oaap"))
ok("der Beschreibung des Knotens ist die Warnung beigegeben",
   "anyone on the network" in a.PROFILES["broker-plain"])

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
