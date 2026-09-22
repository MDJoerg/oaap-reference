#!/usr/bin/env python3
"""Offene WebSockets ueberstehen ein Neuladen des Gateways (0.1.102).

Jedes Ausrollen irgendeiner App laedt das Gateway neu, und Caddy
schliesst dabei alle Streams, die unter der alten Konfiguration
geoeffnet wurden -- bei ALLEN Apps des Knotens. Auf oaap-test gemessen
(18.09.2026): ein gehaltener /broker/-Socket starb in derselben Sekunde
wie `caddy reload`. `stream_close_delay` haelt ihn stattdessen an der
alten Konfiguration fest.

Geprueft wird, dass JEDER Weg vom Gateway zu einer App die Verzoegerung
traegt: oeffentliche Route, geschuetzte Route, zweiter Dienst einer
Mehr-Container-App, Edge-Weiterleitung und der Broker im festen
Caddyfile. Ein vergessener Weg wuerde lautlos weiter abreissen.

Aufruf: python3 test/test_stream_close.py
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-stream-close-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def proxy_blocks(text):
    """Every reverse_proxy to an app container, with its block body."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        mm = re.match(r"\s*reverse_proxy (\S+)(.*)$", line)
        if not mm:
            continue
        body = []
        if mm.group(2).strip() == "{":
            for nxt in lines[i + 1:]:
                if nxt.strip() == "}":
                    break
                body.append(nxt.strip())
        out.append((mm.group(1), body))
    return out


DELAY = f"stream_close_delay {m.STREAM_CLOSE_DELAY}"

print("=== App-Routen (LAN-Eingang und externe Namen teilen site_body) ===")
routes = [
    {"path": "/", "roles": ["admin", "user"]},
    {"path": "/display", "roles": ["public"]},
    {"path": "/media", "roles": ["public"], "service": "media"},
]
site = m.caddy_site(8150, routes, "oaap-app-x", 8080, scope="x",
                    services={"media": ("oaap-app-x-media", 7880)})
blocks = [(t, b) for t, b in proxy_blocks(site)
          if t.startswith("oaap-app-x")]
# preflight handlers carry no long-lived stream; the route handlers do
route_blocks = [(t, b) for t, b in blocks if b]
ok("jede Route hat einen Proxy-Block", len(route_blocks) == 3, site)
ok("geschuetzte, oeffentliche und zweiter Dienst tragen die Verzoegerung",
   all(DELAY in b for _, b in route_blocks), route_blocks)
ok("zweiter Dienst geht an seinen eigenen Container",
   any(t == "oaap-app-x-media:7880" for t, _ in route_blocks), route_blocks)
ok("ohne Verzoegerung bleibt nur der Preflight der geschuetzten Route",
   len([t for t, b in blocks if not b]) == 1, blocks)

print("\n=== Generalprobe (nur Anmeldung) ===")
site = m.caddy_site(8151, routes, "oaap-app-y", 8080, scope="y",
                    login_only=True)
rb = [b for t, b in proxy_blocks(site) if t.startswith("oaap-app-y")]
ok("auch eine Generalprobe verliert Streams nicht beim Neuladen",
   rb and all(DELAY in b for b in rb), site)

print("\n=== Edge-Weiterleitung ===")
m.save_edge([{"host": "kunde.example", "target": "10.0.0.5", "port": 80}])
m.write_edge_caddy()
edge = open(os.path.join(m.CADDY_APPS_DIR, "edge.caddy"),
            encoding="utf-8").read()
eb = [b for t, b in proxy_blocks(edge) if t == "10.0.0.5:80"]
ok("Edge traegt die Verzoegerung", eb and DELAY in eb[0], edge)
ok("Edge ueberschreibt X-Forwarded-For weiterhin",
   eb and "header_up X-Forwarded-For {http.request.remote.host}" in eb[0],
   edge)

print("\n=== Broker im festen Caddyfile ===")
cf = open(os.path.join(HERE, "..", "platform", "Caddyfile"),
          encoding="utf-8").read()
bb = [b for t, b in proxy_blocks(cf) if t == "broker:9001"]
ok("Broker-WebSocket traegt dieselbe Verzoegerung",
   bb and DELAY in bb[0], bb)

print("\n=== Umstellung vorhandener Instanzen (migrate.sh) ===")
DEFAULT = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"] = {
    "anzeige": {"app_id": "hb", "app_name": "HB", "version": "1.0",
                "channel": "test", "container": "oaap-anzeige",
                "port": 8160, "svc_port": 8080, "tenant": DEFAULT,
                # nur oeffentlich: genau die Sorte, die die
                # Mandanten-Umstellung bewusst auslaesst
                "routes": [{"path": "/", "roles": ["public"]}]}}
m.save_registry(reg)
path = os.path.join(m.CADDY_APPS_DIR, "anzeige.caddy")
with open(path, "w", encoding="utf-8") as f:
    f.write(":8160 {\n\thandle {\n\t\treverse_proxy oaap-anzeige:8080\n\t}\n}\n")
reloads = []
m.reload_gateway = lambda: reloads.append(1)
m.cmd_migrate_stream_close(None)
body = open(path, encoding="utf-8").read()
ok("eine alte, rein oeffentliche Seite wird umgeschrieben", DELAY in body, body)
ok("und das Gateway danach neu geladen", len(reloads) == 1)
m.cmd_migrate_stream_close(None)
ok("zweiter Lauf tut nichts (kein Neuladen, das selbst Streams kostet)",
   len(reloads) == 1)

print()
print("Ein Plattform-Update laedt das Gateway neu, statt es neu zu starten (0.1.109)")

# Die Verzoegerung oben haelt einen Stream ueber ein NEULADEN. Ein
# `docker restart` des Gateway-Containers geht daran vorbei und trennt
# alles -- die Zusage aus oaap.core.gateway 0.2.8 war damit auf jedem
# Knoten gebrochen, dessen Caddyfile sich aenderte, also bei fast jedem
# Update. Gefunden auf oaapx01 beim Ausrollen von 0.1.108, wo
# LiveKit-Signalisierung und die Hallendisplays daran haengen.
SH = os.path.join(HERE, "..", "platform")


def sh_lines(name):
    """Die Zeilen eines Skripts ohne Kommentare -- Fortsetzungen
    zusammengefuegt, weil ein Befehl ueber zwei Zeilen fuer die Shell
    EIN Befehl ist. Zeilenweise zu pruefen, ohne das zu tun, sucht nach
    einer Schreibweise, die es so nie gibt."""
    with open(os.path.join(SH, name), encoding="utf-8") as f:
        raw = [ln.strip() for ln in f if not ln.lstrip().startswith("#")]
    joined, buf = [], ""
    for ln in raw:
        if ln.endswith("\\"):
            buf += ln[:-1].rstrip() + " "
            continue
        joined.append(buf + ln)
        buf = ""
    if buf:
        joined.append(buf)
    return joined


up, mig = sh_lines("update.sh"), sh_lines("migrate.sh")
RESTART = "docker restart oaap-gateway-1 >/dev/null"

ok("update.sh laedt das Gateway neu",
   any("caddy reload" in ln and "/etc/caddy/Caddyfile" in ln for ln in up),
   "sonst startet das Update weiter hart neu")
ok("und nennt denselben Konfigurationspfad wie appctl.reload_gateway",
   "/etc/caddy/Caddyfile" in " ".join(up),
   "zwei Pfade waeren zwei Wahrheiten")
ok("migrate.sh startet das Gateway gar nicht mehr hart neu",
   not any(RESTART in ln for ln in mig), mig)
ok("in update.sh bleibt genau EIN harter Neustart: der Notnagel",
   sum(RESTART in ln for ln in up) == 1,
   "mehr als einer hiesse, ein Pfad wurde vergessen")

# Der Notnagel ist kein Schoenheitsfehler, sondern die Bedingung dafuer,
# dass das Neuladen ueberhaupt sicher ist: ein abgelehntes Neuladen
# laesst Caddy auf der ALTEN Konfiguration weiterlaufen, waehrend die
# Dateien etwas anderes sagen. Lautlos ist das schlimmer als getrennte
# Streams.
up_src = open(os.path.join(SH, "update.sh"), encoding="utf-8").read()
tail = up_src.split("caddy reload", 1)[1]
ok("und er meldet sich, statt lautlos zu greifen",
   "WARNING" in tail.split(RESTART)[0],
   "ein stiller Rueckfall ist ein Knoten, der nach gestrigen Regeln routet")
ok("und sagt auch, was er gekostet hat",
   "cut" in tail.split(RESTART)[0].lower(),
   "der Betreiber muss wissen, dass Verbindungen abgerissen sind")

print()
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLSCHLAG(E)")
sys.exit(1 if fails else 0)
