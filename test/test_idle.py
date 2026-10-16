#!/usr/bin/env python3
"""RFC-0059 Stufe 3: eine unbenutzte Instanz legt sich selbst schlafen.

Geprueft wird, dass
1. die Zahlen des Gateways richtig gelesen werden (offene Anfragen, fertige);
2. eine offene Anfrage (WebSocket) oder eine neu fertige "Benutzung" ist,
   und jede Unsicherheit (fehlende Zahl, rueckwaerts laufender Zaehler,
   Instanz im Gateway nie gesehen) als Benutzung zaehlt, nie als Ruhe;
3. der Lauf nur Instanzen mit `standby_after` schlafen legt, nicht solche in
   Wartung, mit laufendem Ausrollen oder ohne Zahlen, und mit "auto-standby"
   als Urheber;
4. ein Gateway, das nicht antwortet, NICHTS aendert.

Aufruf: python3 test/test_idle.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-idle-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402
import idle                                                   # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


METRICS = """\
# HELP caddy_http_requests_in_flight Number of requests currently handled by this server.
# TYPE caddy_http_requests_in_flight gauge
caddy_http_requests_in_flight{handler="reverse_proxy",host="_other",server="srv1"} 1
caddy_http_requests_in_flight{handler="reverse_proxy",host="desk.node.example",server="srv0"} 2
caddy_http_request_duration_seconds_count{code="200",handler="reverse_proxy",host="_other",method="GET",server="srv1"} 40
caddy_http_request_duration_seconds_count{code="404",handler="reverse_proxy",host="_other",method="GET",server="srv1"} 2
caddy_http_request_duration_seconds_count{code="200",handler="reverse_proxy",host="desk.node.example",method="GET",server="srv0"} 7
promhttp_metric_handler_requests_in_flight 1
"""
CONFIG = json.dumps({"apps": {"http": {"servers": {
    "srv0": {"listen": [":443"]}, "srv1": {"listen": [":8101"]},
    "srv2": {"listen": [":8102"]}}}}})

print("Zahlen lesen")
mt = idle.parse_metrics(METRICS)
ok("offene Anfragen je (Listener, Host)", mt[("srv1", "_other")]["inflight"] == 1
   and mt[("srv0", "desk.node.example")]["inflight"] == 2, mt)
ok("fertige Anfragen werden ueber Codes zusammengezaehlt",
   mt[("srv1", "_other")]["done"] == 42, mt)
ok("eine Zeile ohne Zahl oder fremde Familie bleibt draussen",
   len(mt) == 2, mt)
ports = idle.server_ports(CONFIG)
ok("Listener -> Ports", ports == {"srv0": [443], "srv1": [8101], "srv2": [8102]}, ports)
ok("kaputte Konfiguration -> leer, kein Absturz", idle.server_ports("{{") == {})

print("")
print("Zuordnung zur Instanz")
specs = {"crm": {"port": 8101, "hosts": []},
         "desk": {"port": 8102, "hosts": ["desk.node.example"]},
         "ghost": {"port": 8199, "hosts": []}}
rd = idle.readings(mt, ports, specs)
ok("die eigene Portseite zaehlt", rd["crm"]["inflight"] == 1 and rd["crm"]["seen"], rd)
ok("der Host-Name zaehlt, auch auf einem fremden Listener",
   rd["desk"]["inflight"] == 2 and rd["desk"]["done"] == 7, rd)
ok("eine nie gesehene Instanz ist 'unbekannt', nicht 'ruhig'",
   not rd["ghost"]["seen"], rd)

print("")
print("Ruhe und Benutzung")
quiet = {"crm": {"inflight": 0, "done": 42, "seen": True}}
st = idle.update({}, 1000, quiet)
ok("der erste Blick startet die Uhr, er schlaeft nicht sofort",
   idle.idle_minutes(st, "crm", 1000) == 0)
st = idle.update(st, 1000 + 600, quiet)
ok("unveraendert -> die Uhr laeuft: 10 Minuten", idle.idle_minutes(st, "crm", 1600) == 10)
st2 = idle.update(st, 1660, {"crm": {"inflight": 0, "done": 43, "seen": True}})
ok("eine neue fertige Anfrage setzt die Uhr zurueck", idle.idle_minutes(st2, "crm", 1660) == 0)
st3 = idle.update(st, 1660, {"crm": {"inflight": 1, "done": 42, "seen": True}})
ok("eine OFFENE Anfrage (WebSocket) setzt die Uhr zurueck",
   idle.idle_minutes(st3, "crm", 1660) == 0)
st4 = idle.update(st, 1660, {"crm": {"inflight": 0, "done": 5, "seen": True}})
ok("ein rueckwaerts laufender Zaehler zaehlt als Benutzung",
   idle.idle_minutes(st4, "crm", 1660) == 0)
ok("unbekannt -> keine Zahl", idle.idle_minutes(
    idle.update({}, 1, {"x": {"inflight": 0, "done": 0, "seen": False}}), "x", 9999) is None)

print("")
print("Der Lauf")
CALLS = []
tid = m.ensure_default_tenant()


def record(name, port, **over):
    base = {"app_id": name, "app_name": name, "version": "1", "channel": "production",
            "port": port, "container": "oaap-app-" + name, "image": "i", "svc_port": 8000,
            "services": [], "routes": [], "storage": [], "config": [], "roles": [],
            "visibility": {}, "tenant": tid, "id": "x" * 12, "name": name}
    base.update(over)
    return base


reg = m.load_registry()
reg["instances"] = {
    "crm": record("crm", 8101, standby_after=10),          # still, will sleep
    "desk": record("desk", 8102, standby_after=10),        # in use
    "wartung": record("wartung", 8103, standby_after=10,
                      maintenance={"since": "x", "by": "y", "message": ""}),
    "ohne": record("ohne", 8104),                           # no opt-in
}
m.save_registry(reg)
m.deployment_in_flight = lambda name, own_rid="": False
m.load_external = lambda: "node.example"
m.instance_auto_hosts = lambda name, inst, ext_host=None: (
    ["desk.node.example"] if name == "desk" else [])
m.standby_instance = lambda name, inst, who="root", role="root": CALLS.append((name, who, role))
GW = {"/metrics": METRICS, "/config/": json.dumps({"apps": {"http": {"servers": {
    "srv0": {"listen": [":443"]}, "srv1": {"listen": [":8101"]},
    "srv2": {"listen": [":8102"]}, "srv3": {"listen": [":8103"]},
    "srv4": {"listen": [":8104"]}}}}})}
m._gateway_get = lambda path: GW[path]
quiet_metrics = METRICS.replace(
    'host="_other",server="srv1"} 1', 'host="_other",server="srv1"} 0').replace(
    'host="desk.node.example",server="srv0"} 2', 'host="desk.node.example",server="srv0"} 1')
with contextlib.redirect_stderr(io.StringIO()):
    r0 = m.idle_sweep(now=10_000)
ok("der erste Lauf legt niemanden schlafen", r0 == [] and CALLS == [], (r0, CALLS))

GW["/metrics"] = quiet_metrics
with contextlib.redirect_stderr(io.StringIO()):
    r1 = m.idle_sweep(now=10_000 + 11 * 60)
ok("nach 11 ruhigen Minuten schlaeft nur die ruhige Instanz mit Auftrag", r1 == ["crm"], r1)
ok("Urheber ist auto-standby, Rolle system", CALLS == [("crm", "auto-standby", "system")], CALLS)
ok("Instanz in Wartung wird nie geweckt/geschlafen -- schlaeft nicht",
   "wartung" not in r1)
ok("Instanz ohne standby_after bleibt", "ohne" not in r1)
ok("die in Benutzung stehende bleibt (offene Anfrage auf ihrem Host)", "desk" not in r1)

CALLS.clear()
m.deployment_in_flight = lambda name, own_rid="": name == "crm"
with contextlib.redirect_stderr(io.StringIO()):
    r2 = m.idle_sweep(now=10_000 + 30 * 60)
ok("beim Ausrollen schlaeft sie nicht", r2 == [] and not CALLS, (r2, CALLS))
m.deployment_in_flight = lambda name, own_rid="": False

print("")
print("Gateway antwortet nicht -> nichts aendert sich")
before = open(m.IDLE_FILE, encoding="utf-8").read()


def broken(path):
    raise RuntimeError("no answer")


m._gateway_get = broken
CALLS.clear()
with contextlib.redirect_stderr(io.StringIO()):
    r3 = m.idle_sweep(now=10_000 + 90 * 60)
ok("kein Schlafenlegen, keine neue Uhr",
   r3 == [] and not CALLS and open(m.IDLE_FILE, encoding="utf-8").read() == before)

print("")
print("Unter der Mindestzeit wird abgelehnt")
args = type("A", (), {"name": "crm", "minutes": "2"})
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        m.cmd_standby_after(args)
    refused = False
except SystemExit:
    refused = True
ok("2 Minuten sind zu wenig", refused)
m.audit_tenant = lambda *a, **k: None
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_standby_after(type("A", (), {"name": "crm", "minutes": "30"}))
ok("30 Minuten werden im Eintrag gehalten",
   m.load_registry()["instances"]["crm"].get("standby_after") == 30)
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_standby_after(type("A", (), {"name": "crm", "minutes": "off"}))
ok("off nimmt es wieder weg", "standby_after" not in m.load_registry()["instances"]["crm"])

print("")
print("OK" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
