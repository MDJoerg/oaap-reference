#!/usr/bin/env python3
"""Der Tunnel auf der Plattformseite (oaap.net.connector 0.1, RFC-0033
Stufe 2): Schluessel, Connector, Angebote, `via`-Destinationen, die
Route im Gateway. Der Dienst selbst hat seinen eigenen Test
(test_connector_tunnel.py) mit echten Prozessen.

Was hier festgehalten wird:

- ein Schluessel wird einmal gezeigt, und auf dem Knoten steht nur sein
  Hash (2.2); widerrufen geht IMMER und nennt, wen es abschneidet;
- ein Schluessel gehoert einem Mandanten -- eine Destination eines
  anderen Mandanten bekommt "gibt es nicht" (2.2, 2.5);
- ein Connector-Schluessel steht nicht in connect.json, das im Backup
  liegt und das Portal liest (2.3);
- ein Angebot darf nicht auf die Plattform selbst zeigen, und appctl und
  der Dienst urteilen gleich -- zwei Kopien einer Regel, die gegeneinander
  geprueft werden (2.3);
- das Gateway setzt seine drei Kopfzeilen selbst, mit dem Schluessel,
  den der Dienst liest (2.5);
- die Route /connect/tunnel steht auf der :80-Site UND auf den
  erzeugten externen Sites -- genau diese Route, nicht /connect/*
  (die /platform/*-Lehre von 0.1.117);
- der Flottenstatus meldet connector_down und tunnel_down (2.7).

Aufruf: python3 test/test_connector.py
"""
import argparse
import hashlib
import ipaddress
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-connector-unit-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


NETS = {"oaap_default": ["172.18.0.0/16"], "oaap-inst-orders": ["172.29.0.0/16"],
        "oaap-inst-other": ["172.30.0.0/16"]}
RELOADS = []
m.docker_subnets = lambda net=None: [ipaddress.ip_network(s) for k, v in NETS.items()
                                     for s in v if net is None or k == net]
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: RELOADS.append(1)
m.recreate_instance_containers = lambda name, *a, **kw: None
m.container_env = lambda container: None
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


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def audit():
    return [json.loads(x) for x in read(m.TENANT_LOG).splitlines() if x.strip()]


# --- the key (outer side) ------------------------------------------------
key = m.connect_key_issue("x01", DEFAULT)
ok("the key has its prefix and length", key.startswith("oaapc_") and len(key) > 40, key)
conf_text = read(m.CONNECT_FILE)
ok("connect.json holds the hash, never the key",
   key not in conf_text and hashlib.sha256(key.encode()).hexdigest() in conf_text)
ok("the key belongs to the tenant", m.load_connect()["keys"]["x01"]["tenant"] == DEFAULT)
ok("the same label twice is refused", "exists" in refused(m.connect_key_issue, "x01", DEFAULT))
ok("a bad label is refused", refused(m.connect_key_issue, "X_01", DEFAULT) != "")
ok("issuing is in the tenant's audit log",
   any(e["action"] == "connect.key.issue" and e["tenant"] == DEFAULT for e in audit()))

# --- via destinations ----------------------------------------------------
ok("via: a tunnel that does not exist is refused",
   "no tunnel" in refused(m.destination_add, DEFAULT, "erp", "http", "via:nope/erp"))
ok("via: another tenant's tunnel is answered as not there",
   "no tunnel 'x01'" in refused(m.destination_add, OTHER, "erp", "http", "via:x01/erp"))
ok("via: tcp through the tunnel is refused in 0.1",
   "http only" in refused(m.destination_add, DEFAULT, "db", "tcp", "via:x01/db"))
ok("via: malformed target refused",
   refused(m.destination_add, DEFAULT, "erp", "http", "via:x01") != "")
d = m.destination_add(DEFAULT, "erp", "http", "via:x01/erp", "bearer",
                      secret="inner-issued-key")
ok("via: the destination records the tunnel and the offer", d["target"] == {"via": "x01/erp"}, d)
ok("via: the target text says so", m.dest_target_text(d) == "via x01/erp")

# bind an instance and look at the listener
reg = m.load_registry()
reg["instances"]["orders"] = {"app_name": "orders", "tenant": DEFAULT,
                              "container": "oaap-orders", "channel": "test"}
m.save_registry(reg)
m.destination_bind(reg, "orders", "erp")
site = read(os.path.join(m.CADDY_APPS_DIR, m.DEST_SITE))
gw_key = read(m.CONNECT_GATEWAY_KEY_FILE).strip()
ok("the gateway key exists after the first via binding", len(gw_key) > 30)
ok("the listener proxies a via call to the connector service",
   "reverse_proxy connect:8000 {" in site, site)
ok("... rewritten to /via/<tunnel>/<offer>{uri}", "rewrite * /via/x01/erp{uri}" in site, site)
ok("... AFTER the need's prefix is stripped (the route order of stage 1)",
   site.find("uri strip_prefix /destinations/erp") < site.find("rewrite * /via/x01/erp"))
ok("... carrying the gateway's key", f'header_up X-OAAP-Connect-Gateway "{gw_key}"' in site)
ok("... naming the caller by the listener, not by the app",
   'header_up X-OAAP-Connect-Caller "orders"' in site)
ok("... and the destination with its tenant",
   f'header_up X-OAAP-Connect-Destination "{DEFAULT}/erp"' in site)
ok("... with the destination's credential", 'Authorization "Bearer inner-issued-key"' in site)
ok("... and the identity headers stripped", all(f"request_header -{h}" in site
                                                 for h in m.IDENTITY_HEADERS))
if sys.platform != "win32":
    ok("the gateway key file is 0600",
       oct(os.stat(m.CONNECT_GATEWAY_KEY_FILE).st_mode & 0o777) == "0o600")

# the header names and the offer rule are written twice -- once here, once
# in the service. The service needs aiohttp; without it this half is
# skipped, loudly.
try:
    sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "connect"))
    import app as svc                                          # noqa: E402
except ImportError as e:
    svc = None
    print(f"SKIP  service comparison (import failed: {e})")
if svc:
    ok("header names agree with the service",
       (m.CONNECT_GW_KEY, m.CONNECT_GW_CALLER, m.CONNECT_GW_DEST)
       == (svc.GW_KEY, svc.GW_CALLER, svc.GW_DEST))
    ok("the tunnel route agrees with the service's own path",
       any(r.resource.canonical == m.CONNECT_ROUTE
           for r in svc.make_app(svc.Node()).router.routes()))
    cases = ["http://portal:8000/", "http://identity:8000/", "http://localhost/",
             "http://127.0.0.1:9000/", "http://oaap-gateway-1:8098/",
             "http://oaap-portal-1:8000/", "http://gateway:80/twin/",
             "https://oaap-gateway-1/twin/", "https://erp.lan:8443/api",
             "http://10.10.10.5:8080/", "http://u:p@erp.lan/", "ftp://erp.lan/",
             "http://twin:8000/", "http://connect:8000/", "http://[::1]/"]
    differ = [c for c in cases
              if bool(m.offer_target_refusal(c)) != bool(svc.offer_refusal(c))]
    ok("appctl and the service judge every offer alike", not differ, differ)

# --- revoke ---------------------------------------------------------------
cut = m.connect_key_revoke("x01")
ok("revoke succeeds while a destination uses the tunnel", "x01" not in m.load_connect()["keys"])
ok("... and names what it cut", cut == [(DEFAULT, "erp")], cut)
ok("revoking is in the tenant's audit log",
   any(e["action"] == "connect.key.revoke" and "erp" in e.get("detail", "") for e in audit()))
ok("revoking a key that is not there is refused", refused(m.connect_key_revoke, "x01") != "")

# --- the connector (inner side) ------------------------------------------
K = "oaapc_" + "a" * 43
ok("endpoint: http refused without --plain",
   "https" in refused(m.tunnel_connector_add, "up", "http://oaapx01.example", K))
ok("endpoint: a path refused",
   refused(m.tunnel_connector_add, "up", "https://oaapx01.example/x", K) != "")
ok("key: something else than a connect key refused",
   "connect key" in refused(m.tunnel_connector_add, "up", "https://oaapx01.example", "hunter2"))
m.tunnel_connector_add("up", "https://oaapx01.example/", K)
c = m.load_connect()["connectors"]["up"]
ok("the connector is recorded without a trailing slash", c["endpoint"] == "https://oaapx01.example")
ok("connect.json holds no connector key", K not in read(m.CONNECT_FILE))
ok("the key is in the secrets directory", m.load_connector_keys().get("up") == K)
if sys.platform != "win32":
    ok("the secrets directory is 0700",
       oct(os.stat(m.CONNECT_SECRETS_DIR).st_mode & 0o777) == "0o700")
m.tunnel_connector_add("lan", "http://oaap-test.lan", K, plain=True)
ok("--plain is recorded and visible", m.load_connect()["connectors"]["lan"]["plain"] is True)

for bad, why in (("http://portal:8000/", "platform"), ("http://localhost:9/", "platform"),
                 ("http://oaap-gateway-1:8098/", "public ports"),
                 ("http://u:p@erp.lan/", "user")):
    ok(f"offer {bad} refused", why in refused(m.tunnel_offer_add, "up", "erp", bad), bad)
ok("offer: --path with '..' refused",
   refused(m.tunnel_offer_add, "up", "erp", "http://erp.lan", path="/api/../x") != "")
ok("offer: an unknown method refused",
   refused(m.tunnel_offer_add, "up", "erp", "http://erp.lan", methods="GET,FETCH") != "")
m.tunnel_offer_add("up", "erp", "http://10.10.10.5:8080/base", path="/api/", methods="get,post")
o = m.load_connect()["connectors"]["up"]["offers"]["erp"]
ok("an offer is recorded with prefix and methods",
   o == {"to": "http://10.10.10.5:8080/base", "path": "/api", "methods": ["GET", "POST"]}, o)
m.tunnel_offer_add("up", "twin", "http://gateway:80/twin/")
ok("the front door may be offered", "twin" in m.load_connect()["connectors"]["up"]["offers"])
ok("pause", m.tunnel_connector_set_paused("up", True) and m.load_connect()["connectors"]["up"]["paused"])
ok("pause twice changes nothing", m.tunnel_connector_set_paused("up", True) is False)
ok("resume", m.tunnel_connector_set_paused("up", False) and not m.load_connect()["connectors"]["up"]["paused"])
m.tunnel_offer_remove("up", "twin")
ok("an offer can be removed", "twin" not in m.load_connect()["connectors"]["up"]["offers"])
acts = [e["action"] for e in audit()]
ok("connector actions are audited",
   all(a in acts for a in ("connector.add", "connector.offer.add", "connector.pause",
                           "connector.resume", "connector.offer.remove")), acts)
m.tunnel_connector_remove("lan")
ok("remove takes the key with it", "lan" not in m.load_connector_keys())

# --- CLI -------------------------------------------------------------------
parser = None
orig_argv = sys.argv
try:
    import argparse as _ap
    captured = {}
    real_parse = _ap.ArgumentParser.parse_args

    def grab(self, *a, **kw):
        captured["p"] = self
        raise SystemExit(0)
    _ap.ArgumentParser.parse_args = grab
    try:
        m.main()
    except SystemExit:
        pass
    finally:
        _ap.ArgumentParser.parse_args = real_parse
    parser = captured.get("p")
except Exception as e:                                           # noqa: BLE001
    print(f"SKIP  CLI parse ({e})")
if parser:
    a = parser.parse_args(["connect", "key", "issue", "x02", "--tenant", "meier"])
    ok("CLI: connect key issue parses", (a.action, a.name, a.second, a.tenant)
       == ("key", "issue", "x02", "meier"))
    a = parser.parse_args(["connector", "offer", "add", "up", "erp", "--to", "http://e.lan",
                           "--path", "/api", "--methods", "GET"])
    ok("CLI: connector offer add parses", (a.action, a.name, a.second, a.third, a.to)
       == ("offer", "add", "up", "erp", "http://e.lan"))
bin_oaap = read(os.path.join(HERE, "..", "bin", "oaap"))
ok("bin/oaap routes connect and connector",
   'connect)     exec python3 "$APP_DIR/appctl.py" connect' in bin_oaap
   and 'connector)   exec python3 "$APP_DIR/appctl.py" connector' in bin_oaap)

# --- the route ---------------------------------------------------------------
caddyfile = read(os.path.join(HERE, "..", "platform", "Caddyfile"))
ok("Caddyfile: exactly /connect/tunnel is public", "handle /connect/tunnel {" in caddyfile
   and "handle /connect/*" not in caddyfile)
blk = caddyfile[caddyfile.find("handle /connect/tunnel {"):]
blk = blk[:blk.find("\n\t}\n")]
ok("Caddyfile: the route strips identity headers and survives reloads",
   all(f"request_header -{h}" in blk for h in m.IDENTITY_HEADERS)
   and "stream_close_delay" in blk and "forward_auth" not in blk, blk)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "oaap.example.org", "edge": ""}, f)
m.refresh_generated_sites()
ext = read(os.path.join(m.CADDY_APPS_DIR, "external.caddy"))
ok("external site: the tunnel route is there", f"handle {m.CONNECT_ROUTE} {{" in ext, ext[:400])
# migrate-connect on a node whose external site predates the route
with open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), "w", encoding="utf-8") as f:
    f.write(ext.replace(f"handle {m.CONNECT_ROUTE} {{", "handle /old-site-without-it {"))
RELOADS.clear()
m.cmd_migrate_connect(argparse.Namespace())
ext2 = read(os.path.join(m.CADDY_APPS_DIR, "external.caddy"))
ok("migrate-connect carries the route into an old external site",
   f"handle {m.CONNECT_ROUTE} {{" in ext2 and RELOADS == [1], RELOADS)
RELOADS.clear()
m.cmd_migrate_connect(argparse.Namespace())
ok("migrate-connect is quiet the second time", RELOADS == [])

# --- compose -----------------------------------------------------------------
import yaml                                                    # noqa: E402
comp = yaml.safe_load(read(os.path.join(HERE, "..", "platform", "docker-compose.yml")))
cs = comp["services"].get("connect") or {}
ok("compose: connect has no ports and no profile",
   "ports" not in cs and "profiles" not in cs, cs)
ok("compose: only connect mounts the connector secrets",
   [n for n, sv in comp["services"].items()
    if any("data/connect/secrets" in v for v in sv.get("volumes") or [])] == ["connect"])
ok("compose: the portal reads the state read-only",
   any(v.endswith("data/connect/state:/connect-state:ro")
       for v in comp["services"]["portal"]["volumes"]))
gw = comp["services"]["gateway"]
ok("compose: the gateway entry is unchanged by this (no recreate, no cut streams)",
   "connect" not in json.dumps(gw))

# --- fleet status ------------------------------------------------------------
import fleet_view                                               # noqa: E402
doc = fleet_view.build_document("n", "v", [], "t", [], [], [], [],
                                extra_attention=[{"kind": "tunnel_down", "detail": "x01"}])
ok("fleet: extra attention items are carried",
   {"kind": "tunnel_down", "detail": "x01"} in doc["attention"])

print("")
print("OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
