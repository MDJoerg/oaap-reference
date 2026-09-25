#!/usr/bin/env python3
"""Destinationen, Stufe 1 (oaap.net.destinations 0.1, RFC-0033).

Was hier festgehalten wird, sind die Regeln der Spec, nicht das heutige
Verhalten des Codes:

- ein Ziel darf nicht auf die Plattform selbst zeigen (2.1) -- das
  Gateway, das den Aufruf ausfuehrt, haengt in JEDEM Instanznetz;
- binden ist ein Recht, erklaeren ein Bedarf (2.2), nur im eigenen
  Mandanten, und ein fremder Mandant bekommt "gibt es nicht" zu hoeren;
- fuer HTTP sieht die App eine Adresse und nie das Geheimnis (2.3), fuer
  TCP wird uebergeben und das auch so genannt (2.4);
- der Aufrufer wird am Netz erkannt, und die Zuordnung Netz -> Instanz
  darf einen freigewordenen Adressbereich NICHT vererben (2.3);
- eine Generalprobe hat keine Bindungen (2.6);
- und der Gesundheits-Port 8099 antwortet nur dem Plattformnetz --
  gemessen am 25.09. auf oaap-test: ein App-Container bekam 200.

Docker ist durch Attrappen ersetzt; die Adressbereiche der Netze stehen
in NETS und werden im Test veraendert wie Docker es tun wuerde.

Aufruf: python3 test/test_destinations.py
"""
import argparse
import base64
import contextlib
import io
import ipaddress
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-destinations-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


# --- Docker-Attrappen ------------------------------------------------------
NETS = {"oaap_default": ["172.18.0.0/16"], "oaap-inst-orders": ["172.29.0.0/16"]}
RECREATED = []
RELOADS = []
CONTAINER_ENV = {}


def fake_subnets(net=None):
    if net is None:
        return [ipaddress.ip_network(s) for v in NETS.values() for s in v]
    return [ipaddress.ip_network(s) for s in NETS.get(net, [])]


m.docker_subnets = fake_subnets
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: RELOADS.append(1)
m.image_uid = lambda img: None
m.recreate_instance_containers = lambda name, *a, **kw: RECREATED.append(name)
m.container_env = lambda container: CONTAINER_ENV.get(container)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except m.DestinationRefused as e:
        return str(e)
    except SystemExit:
        return "die"
    return ""


def site():
    try:
        with open(os.path.join(m.CADDY_APPS_DIR, m.DEST_SITE), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


MANIFEST = "\n".join([
    'oaap_manifest: "0.5"',
    "app:",
    "  id: orders",
    "  name: Orders",
    "  version: 0.1.0",
    "  type: native",
    "services:",
    "  web:",
    "    build: .",
    "    port: 80",
    "routes:",
    "  - path: /",
    "    roles: [user]",
    "health:",
    "  path: /healthz",
    "config:",
    "  - key: GREETING",
    "    label: Greeting",
    "destinations:",
    "  - name: erp",
    "    kind: http",
    "    purpose: order lookup",
    "  - name: mail",
    "    kind: tcp",
    "    env: { host: SMTP_HOST, port: SMTP_PORT, user: SMTP_USER, password: SMTP_PASSWORD }",
    "",
])
pkg = tempfile.mkdtemp(prefix="oaap-dest-pkg-")
with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
    f.write(MANIFEST)


def install(name, tenant=""):
    NETS[m.app_network(name)] = NETS.get(m.app_network(name)) or ["172.29.0.0/16"]
    args = argparse.Namespace(package=pkg, path="", ref="", name=name,
                              channel="test", store_source="", tenant=tenant,
                              key="", ident=None, rehearsal=None, bind=[])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
    except SystemExit:
        return "ABGELEHNT: " + buf.getvalue()
    return buf.getvalue()


# ---------------------------------------------------------------------------
print("")
print("2.1 Ein Ziel zeigt nicht auf die Plattform")
for target, why in [("http://portal:8000/", "Name ohne Punkt = Container/Dienst"),
                    ("http://localhost:8080/", "diese Maschine"),
                    ("http://127.0.0.1/", "Loopback"),
                    ("http://169.254.169.254/", "link-local (Cloud-Metadaten)"),
                    ("http://172.29.0.5/", "Adresse in einem Docker-Netz"),
                    ("http://user:pw@erp.example.com/", "Zugangsdaten in der URL"),
                    ("https://erp.example.com/api?x=1", "Query"),
                    ("ftp://erp.example.com/", "falsches Schema")]:
    ok(f"abgelehnt: {target} ({why})",
       bool(refused(m.destination_add, DEFAULT, "bad", "http", target)))
ok("via-Ziele sind Stufe 2 und werden abgelehnt",
   "stage 2" in refused(m.destination_add, DEFAULT, "bad", "http", "via:x01/erp"))
ok("ein bearer-Geheimnis mit geschweifter Klammer wird abgelehnt, nicht maskiert",
   "refused" in refused(m.destination_add, DEFAULT, "tok", "http",
                        "https://api.example.com/", "bearer", secret="a{env.X}b"))
ok("ein Kopfzeilen-Name, der eine Identitaetskopfzeile ist, wird abgelehnt",
   bool(refused(m.destination_add, DEFAULT, "hdr", "http", "https://api.example.com/",
                "header", header="X-OAAP-User", secret="abc")))

m.destination_add(DEFAULT, "erp", "http", "https://erp.example.com/api/", "basic",
                  user="oaap", secret="s3cr:et p@ss")
m.destination_add(DEFAULT, "smtp", "tcp", "tcp://mail.example.com:587", "basic",
                  user="bot", secret="mailpw")
dests = m.load_destinations()
ok("die Destination steht OHNE Geheimnis im Objekt (das Portal liest es)",
   "s3cr" not in json.dumps(dests) and dests[DEFAULT]["erp"]["secret"] is True)
ok("das Geheimnis steht in der eigenen Ablage",
   m.load_dest_secrets()[DEFAULT]["erp"] == "s3cr:et p@ss")
if os.name != "nt":
    ok("die Ablage ist 0600", oct(os.stat(m.DEST_SECRETS_FILE).st_mode & 0o777) == "0o600")
ok("doppelter Name im selben Mandanten wird abgelehnt",
   bool(refused(m.destination_add, DEFAULT, "erp", "http", "https://x.example.com/")))

# ---------------------------------------------------------------------------
print("")
print("Manifest 0.5: Bedarf erklaeren")
said = install("orders")
reg = m.load_registry()
ok("die App mit zwei Beduerfnissen ist installiert", "orders" in reg["instances"], said)
ok("die Beduerfnisse stehen im Register, gebunden ist nichts",
   [d["name"] for d in reg["instances"]["orders"]["declared_destinations"]] == ["erp", "mail"]
   and not reg["instances"]["orders"].get("destinations"))
env = m.load_env("orders")
ok("ohne Bindung: keine Variable in der Umgebung",
   not any(k.startswith("OAAP_DESTINATION_") for k in env) and "SMTP_HOST" not in env)
bad = MANIFEST.replace("password: SMTP_PASSWORD", "password: GREETING")
errs = m.validate_destination_needs(__import__("yaml").safe_load(bad))
ok("ein uebergebenes Feld, das auch config-Schluessel ist, macht das Manifest ungueltig",
   any("GREETING" in e for e in errs), errs)

# ---------------------------------------------------------------------------
print("")
print("2.2/2.3 Binden, und was die App davon sieht")
RELOADS.clear()
m.destination_bind(m.load_registry(), "orders", "erp")
env = m.load_env("orders")
ok("nach dem Binden: OAAP_DESTINATION_ERP_URL zeigt aufs Gateway",
   env.get("OAAP_DESTINATION_ERP_URL") == "http://oaap-gateway-1:8098/destinations/erp/", env)
ok("... und das Geheimnis steht NICHT in der Umgebung",
   not any("s3cr" in v for v in env.values()))
text = site()
token = base64.b64encode(b"oaap:s3cr:et p@ss").decode()
ok("das Gateway erkennt den Aufrufer am Netz der Instanz UND am Pfad",
   "remote_ip 172.29.0.0/16" in text and "path /destinations/erp /destinations/erp/*" in text,
   text)
ok("es setzt die Anmeldung selbst (Basic, von der Plattform kodiert)",
   f'header_up Authorization "Basic {token}"' in text)
ok("es schickt den Host des Ziels, nicht den des Gateways",
   "header_up Host erp.example.com" in text and "reverse_proxy https://erp.example.com:443" in text)
ok("es haengt den Pfad an den Basispfad des Ziels", "rewrite * /api{uri}" in text)
blk = text.split("handle @d1 {")[1].split("\n\t}\n")[0]
ok("... und zwar in einem route-Block, erst abschneiden, dann umschreiben -- "
   "gemessen: ohne route sortiert Caddy rewrite VOR uri",
   "route {" in blk and blk.index("uri strip_prefix") < blk.index("rewrite *"), blk)
ok("es entfernt die Identitaets- und Weiterleitungs-Kopfzeilen",
   "request_header -X-OAAP-User" in text and "header_up -X-Forwarded-For" in text)
ok("alles andere bekommt 403 mit einem Satz", 'calling instance' in text and "403" in text)
ok("das Gateway wurde neu geladen", bool(RELOADS))
ok("der Container wurde neu erzeugt, damit die App die Variable sieht",
   RECREATED[-1] == "orders")
if os.name != "nt":
    ok("die Gateway-Datei ist 0600 (sie traegt das Geheimnis)",
       oct(os.stat(os.path.join(m.CADDY_APPS_DIR, m.DEST_SITE)).st_mode & 0o777) == "0o600")
log = m.read_tenant_log(DEFAULT)
ok("das Binden steht im Pruefprotokoll des Mandanten",
   any(e["action"] == "destination.bind" and e["subject"] == "orders" for e in log))

print("")
print("2.4 TCP: Uebergabe, nur an einen erklaerten Bedarf")
ok("tcp an einen nicht erklaerten Bedarf wird abgelehnt",
   "declares no tcp need" in refused(m.destination_bind, m.load_registry(), "orders", "smtp"))
m.destination_bind(m.load_registry(), "orders", "smtp", need="mail")
env = m.load_env("orders")
ok("die erklaerten Felder sind gefuellt -- auch das Passwort (Uebergabe)",
   (env.get("SMTP_HOST"), env.get("SMTP_PORT"), env.get("SMTP_USER"), env.get("SMTP_PASSWORD"))
   == ("mail.example.com", "587", "bot", "mailpw"), env)
ok("config zeigt die uebergebenen Felder nicht als eigene Schluessel",
   {e["key"] for e in m.config_entries("orders", m.load_registry()["instances"]["orders"])}
   == {"GREETING"})

print("")
print("Mandantengrenze")
other, _ = m.tenant_create("meier")
m.destination_add(other, "meier-erp", "http", "https://meier.example.com/", "none")
msg = refused(m.destination_bind, m.load_registry(), "orders", "meier-erp")
ok("die Destination eines anderen Mandanten 'gibt es nicht'",
   "no destination 'meier-erp' in this instance's tenant" in msg, msg)

# ---------------------------------------------------------------------------
print("")
print("Redeploy behaelt die Bindungen")
RECREATED.clear()
install("orders")
inst = m.load_registry()["instances"]["orders"]
env = m.load_env("orders")
ok("die Bindungen stehen nach dem Redeploy noch im Register",
   inst.get("destinations") == {"erp": "erp", "mail": "smtp"}, inst.get("destinations"))
ok("... und die Umgebung traegt sie noch",
   env.get("OAAP_DESTINATION_ERP_URL") and env.get("SMTP_HOST") == "mail.example.com")

print("")
print("Entfernen, solange gebunden")
msg = refused(m.destination_remove, DEFAULT, "erp")
ok("wird abgelehnt und nennt die Instanz", "'orders' (as erp)" in msg, msg)

print("")
print("2.5 Geheimnis fehlt (nach einer Wiederherstellung)")
s = m.load_dest_secrets()
del s[DEFAULT]["erp"]
m.save_dest_secrets(s)
m.write_destinations_caddy()
text = site()
ok("ohne Geheimnis ruft das Gateway das Ziel NICHT ohne Anmeldung auf -- 503",
   "no credential on this node" in text and "reverse_proxy https://erp.example.com" not in text,
   text)
m.destination_set_secret(DEFAULT, "erp", "neu-und-gut")
ok("set-secret stellt die Weiterleitung wieder her",
   "reverse_proxy https://erp.example.com" in site())

# ---------------------------------------------------------------------------
print("")
print("2.3 Ein freigewordener Adressbereich wird nicht vererbt")
reg = m.load_registry()
m.destination_bind(reg, "orders", "erp", need="erp2")
NETS.pop(m.app_network("orders"))        # das Netz ist weg (entfernt/umbenannt)
m.write_destinations_caddy()
ok("ohne Netz steht der Bereich nicht mehr in der Zuordnung",
   "172.29.0.0/16" not in site(), site())
NETS[m.app_network("fresh")] = ["172.29.0.0/16"]   # Docker vergibt ihn neu
reg = m.load_registry()
reg["instances"]["fresh"] = {"tenant": DEFAULT, "container": "oaap-app-fresh"}
m.save_registry(reg)
m.write_destinations_caddy()
ok("die neue Instanz im selben Bereich erbt die Bindungen NICHT",
   "172.29.0.0/16" not in site(), site())
NETS[m.app_network("orders")] = ["172.30.0.0/16"]
m.write_destinations_caddy()
ok("orders mit neuem Bereich bekommt seine Bindungen unter dem neuen Bereich",
   "remote_ip 172.30.0.0/16" in site())
reg = m.load_registry()
del reg["instances"]["fresh"]
m.save_registry(reg)
NETS.pop(m.app_network("fresh"))

print("")
print("Den Netz-Weg entlang: remove_app_network schreibt die Zuordnung neu")
calls = []
real_refresh = m.refresh_destinations
m.refresh_destinations = lambda reg=None: calls.append("refresh")
m._network_exists = lambda net: True
m.subprocess = types.SimpleNamespace(run=lambda *a, **kw: types.SimpleNamespace(
    returncode=0, stdout="", stderr=""), TimeoutExpired=Exception)
m.remove_app_network("irgendwas")
m.refresh_destinations = real_refresh
m.subprocess = __import__("subprocess")
ok("nach dem Entfernen eines Netzes wird die Zuordnung neu geschrieben", calls == ["refresh"])

# ---------------------------------------------------------------------------
print("")
print("2.6 Die Generalprobe hat keine Bindungen")
key = "orders-probe"
ident = {"tenant": DEFAULT, "id": "abc123abc123"}
os.makedirs(m.instance_dir(key, ident), exist_ok=True)
m.save_env(key, dict(m.load_env("orders"), GREETING="hallo"), ident)
dropped = m._scrub_rehearsal_env(
    key, ident, set(), m.dest_handover_fields(m.load_registry()["instances"]["orders"]))
env = m.load_env(key, ident)
ok("die kopierte Umgebung verliert jede Destinations-Variable",
   not any(k.startswith("OAAP_DESTINATION_") for k in env), env)
ok("... und jedes uebergebene Feld samt Passwort",
   not any(k in env for k in ("SMTP_HOST", "SMTP_PASSWORD")), env)
ok("... und die gewoehnliche Konfiguration bleibt", env.get("GREETING") == "hallo")
ok("gemeldet wird davon nichts zum 'Nachtragen' -- die Antwort ist eine Bindung",
   not any(k.startswith("OAAP_DESTINATION_") or k.startswith("SMTP_") for k in dropped),
   dropped)
reg = m.load_registry()
reg["instances"][key] = {"tenant": DEFAULT, "id": ident["id"], "container": "oaap-app-probe",
                         "rehearsal": {"of": "orders", "expires": "2099-01-01T00:00:00Z"},
                         "declared_destinations": reg["instances"]["orders"]["declared_destinations"]}
m.save_registry(reg)
NETS[m.app_network(key)] = ["192.168.16.0/20"]
msg = refused(m.destination_bind, m.load_registry(), key, "erp")
ok("binden an eine Generalprobe wird ohne ausdrueckliche Ausnahme abgelehnt",
   "rehearsal" in msg and "--rehearsal-exception" in msg, msg)
m.write_destinations_caddy()
ok("ihr Netz bekommt am Gateway eine eigene 403 mit dem Grund",
   "remote_ip 192.168.16.0/20" in site() and "is a rehearsal" in site(), site())
m.destination_bind(m.load_registry(), key, "erp", rehearsal_exception=True)
ok("mit Ausnahme gebunden -- und das Protokoll sagt es",
   any(e["action"] == "destination.bind" and "REHEARSAL EXCEPTION" in e.get("detail", "")
       for e in m.read_tenant_log(DEFAULT)))

# ---------------------------------------------------------------------------
print("")
print("Loesen")
RECREATED.clear()
m.destination_unbind(m.load_registry(), "orders", "mail")
env = m.load_env("orders")
ok("die uebergebenen Felder verschwinden mit der Bindung",
   not any(k in env for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")), env)
m.destination_unbind(m.load_registry(), "orders", "erp")
m.destination_unbind(m.load_registry(), "orders", "erp2")
m.destination_unbind(m.load_registry(), key, "erp")
env = m.load_env("orders")
ok("die URL verschwindet mit der Bindung", "OAAP_DESTINATION_ERP_URL" not in env, env)
ok("jetzt darf die Destination gehen", not refused(m.destination_remove, DEFAULT, "erp"))
ok("... mit ihrem Geheimnis", "erp" not in (m.load_dest_secrets().get(DEFAULT) or {}))

# ---------------------------------------------------------------------------
print("")
print("Der Gesundheits-Port antwortet nur dem Plattformnetz")
m.write_internal_health_caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "_internal-health.caddy"), encoding="utf-8") as f:
    health = f.read()
ok("die Proben stehen hinter remote_ip des Plattformnetzes",
   "@platform remote_ip 172.18.0.0/16" in health and "handle @platform" in health, health)
ok("alles andere bekommt 403", health.rstrip().endswith("respond 403\n\t}\n}")
   or "respond 403" in health.split("handle @platform")[1].split("\n\t}\n")[-1], health)
NETS.pop("oaap_default")
m.write_internal_health_caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "_internal-health.caddy"), encoding="utf-8") as f:
    health = f.read()
ok("kennt Docker den Bereich nicht, passt NICHTS (laut statt still offen)",
   "remote_ip 255.255.255.255/32" in health, health)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
