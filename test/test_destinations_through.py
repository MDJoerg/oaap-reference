#!/usr/bin/env python3
"""Zielart `through` -- was hinter einer Tunnel-App liegt, ist eine
Destination (oaap.net.destinations 0.4, RFC-0060 Stufe 1, D4).

Festgehalten werden die Regeln, nicht das heutige Verhalten:

  * `through:<instanz>/<name>` zeigt nur auf eine Instanz DES EIGENEN
    Mandanten, deren Dienst `tun: true` traegt; eine fremde Instanz ist
    eine, die es nicht gibt; nur http;
  * das Gateway behandelt den Aufruf wie `direct` (Identitaet entfernt,
    Schluessel der Destination ergaenzt) und reicht ihn an den
    Tunnel-Container weiter, unter /t/<name>/;
  * eine fehlende, schlafende oder tunnellose Instanz bekommt 502 MIT
    Satz -- und das wird bei jedem Schreiben neu gefragt;
  * eine Tunnel-Instanz mit `through`-Destinationen wird nicht entfernt,
    an der Kommandozeile nicht und ueber das Portal nicht;
  * die Verbraucher-App sieht dieselbe Adresse wie bei `direct`.

Docker ist durch Attrappen ersetzt.
Aufruf: python3 test/test_destinations_through.py
"""
import argparse
import contextlib
import io
import ipaddress
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-through-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


NETS = {}


def fake_subnets(net=None):
    if net is None:
        return [ipaddress.ip_network(s) for v in NETS.values() for s in v]
    return [ipaddress.ip_network(s) for s in NETS.get(net, [])]


m.docker_subnets = fake_subnets
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.recreate_instance_containers = lambda *a, **kw: None
m.container_env = lambda c: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
OTHER, _ = m.tenant_create("meier")


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except m.DestinationRefused as e:
        return str(e)
    except SystemExit:
        return "die"
    return ""


def site():
    m.write_destinations_caddy()
    try:
        with open(os.path.join(m.CADDY_APPS_DIR, m.DEST_SITE), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def block(text, need="llm"):
    """The handle block of one need, so a check cannot pass on another's lines."""
    start = text.find(f"path /destinations/{need} ")
    if start < 0:
        return ""
    h = text.find("\thandle @", start)
    end = text.find("\n\t}\n", h)
    return text[h:end]


def put(key, rec):
    reg = m.load_registry()
    reg["instances"][key] = rec
    m.save_registry(reg)


# Die Tunnel-App (ein Dienst mit tun) und eine ohne
put("vpn", {"tenant": DEFAULT, "name": "vpn", "app_id": "vpn-client",
            "container": "oaap-app-vpn", "image": "x/vpn:1", "svc_port": 8080,
            "services": [{"service": "", "container": "oaap-app-vpn",
                          "image": "x/vpn:1", "port": 8080, "tun": True}]})
put("plain", {"tenant": DEFAULT, "name": "plain", "app_id": "other",
              "container": "oaap-app-plain", "image": "x/p:1", "svc_port": 80,
              "services": [{"service": "", "container": "oaap-app-plain",
                            "image": "x/p:1", "port": 80}]})
put("meier-vpn", {"tenant": OTHER, "name": "vpn", "app_id": "vpn-client",
                  "container": "oaap-app-meier-vpn", "image": "x/vpn:1",
                  "svc_port": 8080,
                  "services": [{"service": "", "container": "oaap-app-meier-vpn",
                                "image": "x/vpn:1", "port": 8080, "tun": True}]})
# Der Verbraucher
put("gw", {"tenant": DEFAULT, "name": "gw", "app_id": "ai-gateway",
           "container": "oaap-app-gw", "image": "x/gw:1", "svc_port": 80})
NETS[m.app_network("gw")] = ["172.31.0.0/16"]

# ------------------------------------------------------------------ anlegen
print("")
print("Anlegen -- nur eine Tunnel-App des eigenen Mandanten, nur http")
ok("through auf eine Instanz ohne tun wird abgelehnt",
   "no tunnel instance 'plain'" in refused(
       m.destination_add, DEFAULT, "x1", "http", "through:plain/gaming"))
ok("through auf eine Instanz, die es nicht gibt, wird abgelehnt",
   "no tunnel instance 'nope'" in refused(
       m.destination_add, DEFAULT, "x2", "http", "through:nope/gaming"))
msg_other = refused(m.destination_add, OTHER, "x3", "http", "through:plain/gaming")
ok("eine Instanz eines ANDEREN Mandanten ist eine, die es nicht gibt",
   "no tunnel instance 'plain'" in msg_other, msg_other)
ok("tcp durch eine Tunnel-App wird abgelehnt",
   "http only" in refused(m.destination_add, DEFAULT, "x4", "tcp", "through:vpn/gaming"))
ok("ein kaputtes Ziel wird abgelehnt",
   "through:<instance>/<name>" in refused(
       m.destination_add, DEFAULT, "x5", "http", "through:vpn"))
d = m.destination_add(DEFAULT, "gaming", "http", "through:vpn/gaming",
                      auth_type="bearer", secret="k3-geheim")
ok("through auf die eigene Tunnel-App wird angelegt",
   d["target"] == {"through": "vpn/gaming"}, d)
ok("die Anzeige sagt 'through'", m.dest_target_text(d) == "through vpn/gaming")
ok("Mandant meier legt seins auf SEINE Instanz 'vpn' an (gleicher Name, anderer Mandant)",
   m.destination_add(OTHER, "gaming", "http", "through:vpn/gaming")["target"]
   == {"through": "vpn/gaming"})

# ------------------------------------------------------------------ Gateway
print("")
print("Das Gateway -- wie direct, nur zum Tunnel-Container")
reg = m.load_registry()
m.destination_bind(reg, "gw", "gaming", need="llm")
text = site()
b = block(text)
ok("der Aufruf geht an den Tunnel-Container dieses Mandanten",
   "reverse_proxy http://oaap-app-vpn:8080" in b
   and "oaap-app-meier-vpn" not in b, b)
ok("... unter /t/<name>/, nach dem Abschneiden des eigenen Praefix",
   "uri strip_prefix /destinations/llm" in b and "rewrite * /t/gaming{uri}" in b
   and b.index("strip_prefix") < b.index("rewrite * /t/gaming"), b)
ok("... mit dem Schluessel der Destination, den die App nie sieht",
   "header_up Authorization \"Bearer k3-geheim\"" in b, b)
ok("... und ohne die Identitaets-Kopfzeilen des Aufrufers",
   all(f"header_up -{h}" in b or f"request_header -{h}" in b
       for h in ("X-Forwarded-For",)) and m.strip_identity("")[0].strip() in b, b)
ok("... und nur fuer das Netz des gebundenen Aufrufers",
   "remote_ip 172.31.0.0/16" in text)
env = m.load_env("gw")
ok("die Verbraucher-App sieht dieselbe Art Adresse wie bei direct",
   env.get("OAAP_DESTINATION_LLM_URL")
   == f"http://{m.GATEWAY_CONTAINER}:{m.DEST_PORT}/destinations/llm/",
   env)
ok("... und keinen Hinweis auf den Tunnel",
   "vpn" not in (env.get("OAAP_DESTINATION_LLM_URL") or ""), env)

# ------------------------------------------------------------------ Zustand
print("")
print("Der Zustand der Tunnel-App wird bei jedem Schreiben gefragt")
reg = m.load_registry()
reg["instances"]["vpn"]["state"] = "standby"
m.save_registry(reg)
b = block(site())
ok("schlafend: 502 mit Satz, keine Weiterleitung",
   "asleep" in b and " 502" in b and "reverse_proxy" not in b, b)
reg = m.load_registry()
reg["instances"]["vpn"].pop("state")
reg["instances"]["vpn"]["services"][0]["tun"] = "yes"
m.save_registry(reg)
b = block(site())
ok("handbearbeiteter Eintrag (tun: 'yes'): kein Tunnel, 502 mit Satz",
   "holds no tunnel" in b and " 502" in b and "reverse_proxy" not in b, b)
reg = m.load_registry()
reg["instances"]["vpn"]["services"][0]["tun"] = True
reg["instances"]["vpn"]["rehearsal"] = {"of": "x"}
m.save_registry(reg)
b = block(site())
ok("Generalprobe: kein Tunnel, 502 mit Satz",
   "rehearsal" in b and " 502" in b and "reverse_proxy" not in b, b)
reg = m.load_registry()
reg["instances"]["vpn"].pop("rehearsal")
m.save_registry(reg)
ok("wieder wach: die Weiterleitung ist zurueck",
   "reverse_proxy http://oaap-app-vpn:8080" in block(site()))

# ------------------------------------------------------------------ entfernen
print("")
print("Entfernen der Tunnel-App, solange eine Destination durch sie geht")
reg = m.load_registry()
why = m.through_removal_refusal(reg, "vpn")
ok("wird abgelehnt und nennt die Destination", "'gaming'" in why, why)
ok("eine Instanz, durch die nichts geht, darf gehen",
   m.through_removal_refusal(reg, "plain") == "")
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        m.cmd_remove(argparse.Namespace(name="vpn", purge=False))
    gone = True
except SystemExit:
    gone = False
except Exception as e:                                  # noqa: BLE001
    # the removal STARTED -- with Docker faked it may stop half-way, but
    # the verdict is the same: the refusal did not hold
    gone = True
    buf.write(f"\nremoval started and broke: {e!r}")
ok("die Kommandozeile entfernt sie nicht",
   not gone and "vpn" in m.load_registry()["instances"], buf.getvalue())
SRC = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("das Portal fragt dieselbe Funktion, bevor es entfernt",
   "elif through_removal_refusal(reg, name):" in SRC)

# Fehlt die Instanz trotzdem (Mandant entfernt, von Hand geloescht): Satz.
reg = m.load_registry()
reg["instances"].pop("vpn", None)
m.save_registry(reg)
b = block(site())
ok("fehlende Tunnel-Instanz: 502 mit Satz, nie ein fremder Container",
   "does not exist" in b and " 502" in b and "reverse_proxy" not in b
   and "meier-vpn" not in b, b)

print("")
print("FAIL" if fails else "OK", f"-- {fails} Fehler" if fails else "")
sys.exit(1 if fails else 0)
