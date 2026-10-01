#!/usr/bin/env python3
"""Das Profil 'gateway-only': die Instanz-Ports 8100-8199 gehen zu.

Befund C1 (program/doku-pruefung-2026-09-30.md): das Gateway
veröffentlichte 8100-8199 auf allen Schnittstellen, ohne Schalter. Auf
einem Knoten mit öffentlicher Adresse sieht ein Portscan 100 Ports mit
Klartext-HTTP und Login. Das Profil setzt OAAP_APP_PORT_BIND=127.0.0.1
in der .env; docker-compose.yml liest die Variable. Eine Bind-Adresse
statt einer Overlay-Datei, damit update.sh/migrate.sh/CLI ohne eigene
Dateiliste übereinstimmen (die Broker-Overlay-Lehre).

GRENZE, offen gesagt: Dieser Test beweist die Verdrahtung, nicht den
Zaun. Dass der Port von AUSSEN zu ist, misst nur ein Scan von einer
anderen Maschine auf einem echten Knoten.

Run: python3 test/test_gateway_only_profile.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, PLATFORM)
import appctl  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


compose = read(PLATFORM, "docker-compose.yml")
portal = read(PLATFORM, "services", "portal", "app.py")

print("=== Profil registriert, in beiden Tabellen ===")
ok("'gateway-only' ist in appctl.PROFILES", "gateway-only" in appctl.PROFILES)
ok("'gateway-only' steht in PROFILE_LABELS des Portals",
   re.search(r'PROFILE_LABELS\s*=\s*\{.*?"gateway-only":', portal, re.S))

print("=== Compose: Bind-Adresse aus der .env, Standard offen wie bisher ===")
ok("8100-8199 wird über ${OAAP_APP_PORT_BIND:-0.0.0.0} gebunden",
   '"${OAAP_APP_PORT_BIND:-0.0.0.0}:8100-8199:8100-8199"' in compose)
ok("80 und 443 bleiben unbedingt veröffentlicht",
   '"${OAAP_HTTP_PORT}:80"' in compose and '"443:443"' in compose)
ok("kein Overlay nötig: es gibt keine docker-compose.gateway-only.yml",
   not os.path.exists(os.path.join(PLATFORM, "docker-compose.gateway-only.yml")))

print("=== .env schreiben und wieder entfernen ===")
tmp = tempfile.mkdtemp()
appctl.APP_DIR = tmp
with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
    f.write("OAAP_HTTP_PORT=80\nOAAP_VERSION=0.1.0\n")
appctl._set_platform_env("OAAP_APP_PORT_BIND", "127.0.0.1")
env = appctl._platform_env()
ok("Variable gesetzt, andere Zeilen unberührt",
   env.get("OAAP_APP_PORT_BIND") == "127.0.0.1" and env.get("OAAP_HTTP_PORT") == "80")
appctl._set_platform_env("OAAP_APP_PORT_BIND", "127.0.0.1")
with open(os.path.join(tmp, ".env"), encoding="utf-8") as f:
    ok("zweimal setzen erzeugt keine Dublette",
       f.read().count("OAAP_APP_PORT_BIND=") == 1)
appctl._set_platform_env("OAAP_APP_PORT_BIND", None)
ok("None entfernt die Zeile wieder",
   "OAAP_APP_PORT_BIND" not in appctl._platform_env())

print("=== apply_gateway_only: .env UND nur das Gateway neu ===")
calls = []
appctl._compose = lambda *a: calls.append(a)
ok("an: Gateway wird ohne Abhängigkeiten neu erzeugt",
   appctl.apply_gateway_only(True) and calls[-1] == ("up", "-d", "--no-deps", "gateway"))
ok("an: .env trägt 127.0.0.1", appctl._platform_env().get("OAAP_APP_PORT_BIND") == "127.0.0.1")
appctl.apply_gateway_only(False)
ok("aus: .env ohne die Variable", "OAAP_APP_PORT_BIND" not in appctl._platform_env())


def boom(*a):
    raise subprocess.CalledProcessError(1, "docker")


appctl._compose = boom
ok("schlägt das Neuerzeugen fehl, meldet die Funktion False (nicht still True)",
   appctl.apply_gateway_only(True) is False)

print("=== cmd_node ruft es in beide Richtungen ===")
src = read(PLATFORM, "appctl.py")
ok("add-profile ruft apply_gateway_only(True)", "apply_gateway_only(True)" in src)
ok("remove-profile ruft apply_gateway_only(False)", "apply_gateway_only(False)" in src)

print("=== Portal: Namen vor Port, wenn das Profil gilt ===")
ok("_tile_url fragt das Profil, bevor es einen Port-Link baut",
   re.search(r'def _tile_url.*?"gateway-only" in node_profiles\(\).*?http://\{host\}:',
             portal, re.S))

print("=== Köder: mit Docker die aufgelöste Compose-Datei ansehen ===")
if shutil.which("docker"):
    def ports(bind):
        e = dict(os.environ, OAAP_HTTP_PORT="80", **({"OAAP_APP_PORT_BIND": bind} if bind else {}))
        r = subprocess.run(["docker", "compose", "-f", os.path.join(PLATFORM, "docker-compose.yml"),
                            "config"], capture_output=True, text=True, env=e)
        return r.stdout
    ok("ohne Variable: Bereich auf 0.0.0.0 oder ohne Host-IP", "host_ip: 127.0.0.1" not in ports(None))
    ok("mit Variable: host_ip 127.0.0.1", "127.0.0.1" in ports("127.0.0.1"))
else:
    print("SKIP  kein docker auf dieser Maschine")

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
