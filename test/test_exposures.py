#!/usr/bin/env python3
"""Freigaben, Plattformseite (oaap.net.connector 0.2, RFC-0033 Stufe 3).

Der Dienst selbst hat seinen Test mit echten Prozessen
(test_exposures_tunnel.py). Hier steht, was auf dem Knoten geschrieben
wird -- und die Regeln, die dort in zwei Kopien stehen:

- EINE Site fuer die ganze Zone `*.t.<Name des Knotens>` (2.8.1): ein
  neuer Name fasst das Gateway nie an. Direkt mit Zertifikat auf Zuruf,
  hinter einer Edge als http mit Edge-Wache;
- die REIHENFOLGE ist die Regel: erst abstreifen, was ein Client als
  Identitaet mitschickte, dann fragen (forward_auth an den DIENST, nicht
  an identity -- der Mandant haengt an der Freigabe), dann umschreiben,
  dann weiterreichen. Ein `handle` sortiert Direktiven um (0.1.128), also
  steht alles im `route` (2.8.4);
- die Site traegt den Schluessel des Dienstes: 0600, eigene Datei;
- die Zone steht in connect.json, und was der Betreiber schliesst, auch;
- `expose` am inneren Knoten: das Ende steht ABSOLUT in der Datei
  (ein Neustart gibt keine neue Frist), die Grenzen der Spec, jede
  Entscheidung im Pruefprotokoll;
- das Portal gibt ein Zertifikat fuer genau den Namen einer LEBENDEN
  Freigabe frei -- nicht fuer die Zone, nicht fuer einen anderen Namen
  darunter, nicht fuer einen gerade abgelaufenen (2.8.7);
- /connect/client ist oeffentlich, EXAKT dieser Pfad.

Aufruf: python3 test/test_exposures.py
"""
import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-exposures-unit-")
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


RELOADS = []
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: RELOADS.append(1)
m.docker_subnets = lambda net=None: []
m.container_env = lambda container: None
m.recreate_instance_containers = lambda name, *a, **kw: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except m.DestinationRefused as e:
        return str(e)
    except SystemExit:
        return "die"
    return ""


def _raises(fn, *a):
    try:
        fn(*a)
    except Exception:                                          # noqa: BLE001
        return True
    return False


def audit():
    return [json.loads(x) for x in read(m.TENANT_LOG).splitlines() if x.strip()]


def out(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn(*a, **kw)
        except SystemExit as e:
            return buf.getvalue() + f"<exit {e.code}>"
    return buf.getvalue()


SITE = os.path.join(m.CADDY_APPS_DIR, m.EXPOSE_SITE)

# --- no external hostname: no zone -------------------------------------------
ok("no external hostname: no zone", m.exposure_zone() == ("", ""))
m.write_exposures()
ok("... and no site", not os.path.exists(SITE))
ok("... and connect.json says so", m.load_connect()["exposure"] == {})

# --- the zone, direct ---------------------------------------------------------
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "oaap.example.org", "edge": ""}, f)
ok("the zone is t.<external host>", m.exposure_zone() == ("t.oaap.example.org", "https"))
changed = m.write_exposures()
ok("the site is written and reported as changed", changed is True and os.path.exists(SITE))
ok("connect.json carries the zone for the service",
   m.load_connect()["exposure"] == {"zone": "t.oaap.example.org", "scheme": "https"},
   m.load_connect()["exposure"])
site = read(SITE)
gw_key = read(m.CONNECT_GATEWAY_KEY_FILE).strip()
ok("one site for the whole zone", "https://*.t.oaap.example.org {" in site
   and site.count("*.t.oaap.example.org") == 2, site[:300])
ok("certificates on demand (approved per name by the portal)",
   "tls {" in site and "on_demand" in site)
ok("plain http is redirected", "http://*.t.oaap.example.org {" in site
   and "redir https://{host}{uri} permanent" in site)
ok("login and logout are served on the zone too",
   "handle /auth/* {" in site and "reverse_proxy identity:8000" in site)

# the ORDER is the rule
i_route = site.find("\t\troute {")
i_strip = site.find("request_header -X-OAAP-User", i_route)
i_auth = site.find("forward_auth connect:8000 {")
i_rewrite = site.find("rewrite * /exposed{uri}")
i_proxy = site.find("reverse_proxy connect:8000 {")
ok("everything inside ONE route block", i_route > 0)
ok("order: strip identity, ask the service, rewrite, proxy",
   0 < i_route < i_strip < i_auth < i_rewrite < i_proxy, (i_route, i_strip, i_auth, i_rewrite, i_proxy))
ok("all five identity headers are stripped before the question",
   all(site.find(f"request_header -{h}", i_route) in range(i_route, i_auth)
       for h in m.IDENTITY_HEADERS))
ok("the question goes to the SERVICE, not to identity (the tenant is per exposure)",
   "forward_auth identity" not in site[i_route:] and "uri /exposure/verify" in site)
ok("the answer's identity headers are copied onto the call",
   "copy_headers " + " ".join(m.IDENTITY_HEADERS) in site)
ok("the call carries the service's key, the asked name and the client, set not passed",
   site.count(f'header_up X-OAAP-Connect-Gateway "{gw_key}"') == 2
   and site.count("header_up X-OAAP-Connect-Host {http.request.host}") == 2
   and site.count("header_up X-OAAP-Connect-Client {http.request.remote.host}") == 2, site)
ok("the question does not carry an Upgrade",
   "header_up -Connection" in site and "header_up -Upgrade" in site)
ok("the stream survives a reload like the tunnel does",
   f"stream_close_delay {m.STREAM_CLOSE_DELAY}" in site)
ok("the access log filter is in", "request>headers>Authorization replace" in site)
ok("the site holds a key and says so", "Holds a key: 0600" in site)
if sys.platform != "win32":
    ok("the site file is 0600", oct(os.stat(SITE).st_mode & 0o777) == "0o600")
ok("writing it again changes nothing", m.write_exposures() is False)

# the external site and the update path
m.refresh_generated_sites()
ext = read(os.path.join(m.CADDY_APPS_DIR, "external.caddy"))
ok("refresh_generated_sites writes the zone as well", os.path.exists(SITE))
ok("the external site serves /connect/client, exactly",
   f"handle {m.CONNECT_CLIENT_ROUTE} {{" in ext and "handle /connect/*" not in ext, ext[:200])
caddyfile = read(os.path.join(HERE, "..", "platform", "Caddyfile"))
ok("Caddyfile: exactly /connect/client is public",
   "handle /connect/client {" in caddyfile and "handle /connect/*" not in caddyfile)
blk = caddyfile[caddyfile.find("handle /connect/client {"):]
blk = blk[:blk.find("\n\t}\n")]
ok("Caddyfile: the client route strips identity and asks nobody",
   all(f"request_header -{h}" in blk for h in m.IDENTITY_HEADERS)
   and "forward_auth" not in blk, blk)

# migrate-connect: a node from before 0.1.131
os.remove(SITE)
with open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), "w", encoding="utf-8") as f:
    f.write(ext.replace(f"handle {m.CONNECT_CLIENT_ROUTE} {{", "handle /old {"))
RELOADS.clear()
text = out(m.cmd_migrate_connect, argparse.Namespace())
ok("migrate-connect carries the client route into an old external site",
   f"handle {m.CONNECT_CLIENT_ROUTE} {{" in read(os.path.join(m.CADDY_APPS_DIR, "external.caddy"))
   and RELOADS == [1], (RELOADS, text))
ok("... and the zone site comes with it", os.path.exists(SITE))
os.remove(SITE)
RELOADS.clear()
text = out(m.cmd_migrate_connect, argparse.Namespace())
ok("migrate-connect opens the zone on a node that only lacks the zone",
   os.path.exists(SITE) and RELOADS == [1] and "exposure zone" in text, (RELOADS, text))
RELOADS.clear()
out(m.cmd_migrate_connect, argparse.Namespace())
ok("migrate-connect is quiet the second time", RELOADS == [])

# --- behind an edge -----------------------------------------------------------
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "oaap.example.org", "edge": "10.10.10.97"}, f)
m.write_exposures()
site = read(SITE)
ok("behind an edge: plain http, only the edge is let in",
   "http://*.t.oaap.example.org {" in site and "not remote_ip 10.10.10.97" in site
   and "on_demand" not in site and "redir https://" not in site, site[:400])
ok("behind an edge the client is the one the edge names",
   "header_up X-OAAP-Connect-Client {http.request.header.X-Forwarded-For}" in site)
ok("... and the service is told the zone is http",
   m.load_connect()["exposure"] == {"zone": "t.oaap.example.org", "scheme": "http"})

# and the external name goes away again
os.remove(m.EXTERNAL_FILE)
ok("without the external hostname the site is removed", m.write_exposures() is True
   and not os.path.exists(SITE))
ok("... and the service is told there is no zone", m.load_connect()["exposure"] == {})
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "oaap.example.org", "edge": ""}, f)
m.write_exposures()

# --- times --------------------------------------------------------------------
ok("ttl: default is 8 hours", m.parse_ttl("") == 8 * 3600)
ok("ttl: 30m, 8h, 2d, 90s and a bare number",
   [m.parse_ttl(x) for x in ("30m", "8h", "2d", "90s", "3600")] == [1800, 28800, 172800, 90, 3600])
ok("ttl: below a minute refused", "at least" in refused(m.parse_ttl, "30s"))
ok("ttl: above 7 days refused, and says why", "at most 7 days" in refused(m.parse_ttl, "8d"))
ok("ttl: nonsense refused", refused(m.parse_ttl, "soon") != "")

# --- expose on the inner node -------------------------------------------------
K = "oaapc_" + "a" * 43
m.tunnel_connector_add("up", "https://oaapx01.example", K)
ok("expose: an unknown connector is refused",
   "no connector" in refused(m.tunnel_exposure_add, "nope", "http://192.168.178.20:3000", 3600))
for bad, why in (("http://portal:8000/", "platform"), ("http://localhost:3000/", "platform"),
                 ("http://oaap-gateway-1:8098/", "public ports"), ("http://u:p@lan/", "user"),
                 ("ftp://lan/", "http")):
    ok(f"expose: {bad} refused (what an offer may not be, a target may not be)",
       why in refused(m.tunnel_exposure_add, "up", bad, 3600), bad)
t0 = time.time()
ref = m.tunnel_exposure_add("up", "http://192.168.178.20:3000", 3600, public=False, who="jörg")
w = m.load_connect()["connectors"]["up"]["exposures"][ref]
ok("expose: the end is ABSOLUTE in the file", abs(w["expires_at"] - (t0 + 3600)) < 5, w)
ok("expose: the ref looks like x-<hex>", ref.startswith("x-") and len(ref) == 8, ref)
ok("expose: it is login-protected unless said otherwise", w["public"] is False)
ok("expose: the decision is in the audit log",
   any(e["action"] == "connector.expose" and e["subject"] == f"up/{ref}" and e["who"] == "jörg"
       and "login" in e.get("detail", "") for e in audit()))
ref2 = m.tunnel_exposure_add("up", "http://192.168.178.21:8080", 1800, public=True)
ok("expose: --public is recorded and named in the audit line",
   m.load_connect()["connectors"]["up"]["exposures"][ref2]["public"] is True
   and any("PUBLIC" in e.get("detail", "") for e in audit() if e["action"] == "connector.expose"))
for n in range(8):
    m.tunnel_exposure_add("up", f"http://192.168.178.{30 + n}:80", 600)
ok("expose: at most 10 per connector", "at most 10" in refused(
    m.tunnel_exposure_add, "up", "http://192.168.178.99:80", 600))

# extend / unexpose
before = m.load_connect()["connectors"]["up"]["exposures"][ref]["expires_at"]
m.tunnel_exposure_extend("up", ref, 7200)
after = m.load_connect()["connectors"]["up"]["exposures"][ref]["expires_at"]
ok("extend: counts from NOW", abs(after - (time.time() + 7200)) < 5 and after > before)
ok("extend: is in the audit log", any(e["action"] == "connector.extend" for e in audit()))
ok("extend: an unknown exposure is refused",
   "holds no exposure" in refused(m.tunnel_exposure_extend, "up", "x-nope", 600))
c = m.load_connect()
c["connectors"]["up"]["exposures"][ref2]["expires_at"] = time.time() - 10
m.save_connect(c)
ok("extend: one that ran out is refused (a new one is a new act)",
   "run out" in refused(m.tunnel_exposure_extend, "up", ref2, 600))
m.tunnel_exposure_remove("up", ref)
ok("unexpose: removed", ref not in m.load_connect()["connectors"]["up"]["exposures"])
ok("unexpose: is in the audit log", any(e["action"] == "connector.unexpose" for e in audit()))
ok("unexpose: twice is refused", "holds no exposure" in refused(m.tunnel_exposure_remove, "up", ref))
# by the public name
ref3 = m.tunnel_exposure_add("up", "http://192.168.178.50:80", 600)
open_state = os.path.join(m.CONNECT_STATE_DIR, "state.json")
os.makedirs(m.CONNECT_STATE_DIR, exist_ok=True)
with open(open_state, "w", encoding="utf-8") as f:
    json.dump({"connectors": {"up": {"exposures": {ref3: {"name": "k3f9x2mh4a"}}}}}, f)
m.tunnel_exposure_remove("up", "k3f9x2mh4a")
ok("unexpose: the name on the internet works as well",
   ref3 not in m.load_connect()["connectors"]["up"]["exposures"])
ok("the connect key is still in no file that is backed up", K not in read(m.CONNECT_FILE))

# --- the CLI ---------------------------------------------------------------
with open(open_state, "w", encoding="utf-8") as f:
    json.dump({"schema": "0.2", "zone": "t.oaap.example.org", "scheme": "https",
               "certs_week": 41, "certs_limit": 50,
               "exposures": {"k3f9x2mh4a": {
                   "host": "k3f9x2mh4a.t.oaap.example.org", "tenant": DEFAULT, "owner": "p:jm:x",
                   "public": False, "opened": "2026-09-26T08:00:00Z",
                   "expires": "2026-09-26T16:00:00Z", "opened_by": "jm", "calls": 7,
                   "connected": True}},
               "people": [{"user": "jm", "tenant": DEFAULT, "since": "2026-09-26T08:00:00Z",
                           "remote": "1.2.3.4"}]}, f)
text = out(m.cmd_connect, argparse.Namespace(action="exposures", name=None, second=None,
                                              tenant="", lines=10))
ok("connect exposures: lists the name, the tenant, who opened it and how many calls",
   "k3f9x2mh4a" in text and "https://k3f9x2mh4a.t.oaap.example.org/" in text
   and "by jm" in text and "7 calls" in text, text)
ok("connect exposures: says the certificate count and warns near the limit",
   "41 names" in text and "NEAR THE LIMIT" in text and "50" in text, text)
ok("connect exposures: names the laptop that is connected", "laptop: jm" in text, text)
# found live on oaap-test 2026-09-26: behind an edge the zone is http, and the list said https
with open(open_state, encoding="utf-8") as f:
    st_http = json.load(f)
st_http["scheme"] = "http"
with open(open_state, "w", encoding="utf-8") as f:
    json.dump(st_http, f)
text = out(m.cmd_connect, argparse.Namespace(action="exposures", name=None, second=None,
                                              tenant="", lines=10))
ok("connect exposures: prints the scheme the zone really has (http behind an edge)",
   "http://k3f9x2mh4a.t.oaap.example.org/" in text and "https://" not in text, text)
text = out(m.cmd_connect, argparse.Namespace(action="exposure", name="close", second="zzzzzzzzzz",
                                              tenant="", lines=10))
ok("connect exposure close: an unknown name is refused", "exit" in text, text)
text = out(m.cmd_connect, argparse.Namespace(action="exposure", name="close", second="k3f9x2mh4a",
                                              tenant="", lines=10))
ok("connect exposure close: written where the service reads it",
   "k3f9x2mh4a" in m.load_connect()["exposure_closed"], text)
ok("connect exposure close: is in the tenant's log, with the operator's name",
   any(e["action"] == "exposure.close.requested" and e["tenant"] == DEFAULT for e in audit()))
c = m.load_connect()
c["exposure_closed"]["oldname000"] = "2020-01-01T00:00:00Z"
m.save_connect(c)
m.write_exposure_conf()
ok("a closure older than a day is forgotten, a fresh one is kept",
   "oldname000" not in m.load_connect()["exposure_closed"]
   and "k3f9x2mh4a" in m.load_connect()["exposure_closed"])
c = m.load_connect()
ok("connect.json keeps the zone, the connectors and the closures side by side",
   c["exposure"]["zone"] == "t.oaap.example.org" and "up" in c["connectors"])

text = out(m.cmd_connector, argparse.Namespace(action="list", name=None, second=None, third=None,
                                                lines=10))
ok("connector list: shows what was asked for and that nothing answered yet -- and not "
   "what has run out",
   "expose x-" in text and "192.168.178.35:80" in text and "no answer yet" in text
   and "192.168.178.21:8080" not in text, text)

# the parser knows the verbs
ap_src = read(os.path.join(HERE, "..", "platform", "appctl.py"))
ok("argparse: connector expose / unexpose / extend and connect exposures / exposure",
   '"expose", "unexpose", "extend"' in ap_src and '"exposures", "exposure"' in ap_src)

# --- the portal approves a certificate for a live name and nothing else -------
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
spec = importlib.util.spec_from_file_location(
    "portal_app_for_exposures", os.path.join(HERE, "..", "platform", "services", "portal", "app.py"))
portal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portal)
portal.EXTERNAL_FILE = m.EXTERNAL_FILE
portal.EDGE_FILE = os.path.join(DATA, "no-edge.json")
portal.CONNECT_STATE = open_state


def ask(domain):
    with portal.app.test_request_context("/edge/tls-ask?domain=" + domain):
        r = portal.edge_tls_ask()
    status = r[1] if isinstance(r, tuple) else 200
    return status


future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
with open(open_state, "w", encoding="utf-8") as f:
    json.dump({"exposures": {
        "livelivelv": {"host": "livelivelv.t.oaap.example.org", "expires": future},
        "deaddeaddd": {"host": "deaddeaddd.t.oaap.example.org", "expires": past}}}, f)
ok("tls-ask: a live exposure's name is approved", ask("livelivelv.t.oaap.example.org") == 200)
ok("tls-ask: a name whose time just ran out is not", ask("deaddeaddd.t.oaap.example.org") == 404)
ok("tls-ask: another name under the zone is not", ask("anythingelse.t.oaap.example.org") == 404)
ok("tls-ask: the zone itself is not", ask("t.oaap.example.org") == 404)
ok("tls-ask: a name one level too deep is not", ask("a.livelivelv.t.oaap.example.org") == 404)
ok("tls-ask: the same name under another domain is not", ask("livelivelv.t.evil.example") == 404)
ok("tls-ask: a name beside the zone is not (nothing under oaap.example.org is approved)",
   ask("livelivelv.oaap.example.org") == 404)
os.remove(open_state)
ok("tls-ask: with no state the answer is no, not an error", ask("livelivelv.t.oaap.example.org") == 404)

# --- the service and this file agree ------------------------------------------
try:
    sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "connect"))
    import app as svc                                          # noqa: E402
except ImportError as e:
    svc = None
    print(f"SKIP  service comparison (import failed: {e})")
if svc:
    ok("header names agree with the service", (m.CONNECT_GW_HOST, m.CONNECT_GW_CLIENT)
       == (svc.GW_HOST, svc.GW_CLIENT))
    ok("the limits agree with the service",
       (m.EXPOSE_DEFAULT_TTL, m.EXPOSE_MAX_TTL, m.CERT_WEEK_LIMIT, m.CERT_WEEK_WARN)
       == (svc.EXPOSE_DEFAULT_TTL, svc.EXPOSE_MAX_TTL, svc.CERT_WEEK_LIMIT, svc.CERT_WEEK_WARN))
    cli = importlib.util.spec_from_file_location(
        "expose_client", os.path.join(HERE, "..", "platform", "services", "connect", "client",
                                      "oaap-expose.py"))
    client = importlib.util.module_from_spec(cli)
    cli.loader.exec_module(client)
    ok("the requesters share ONE minimum and ONE maximum (appctl and the client)",
       [m.parse_ttl(x) for x in ("60s", "1m", "7d")] == [client.parse_ttl(x) for x in ("60s", "1m", "7d")]
       == [60, 60, 7 * 86400]
       and all(bool(refused(m.parse_ttl, x)) for x in ("59s", "8d"))
       and all(_raises(client.parse_ttl, x) for x in ("59s", "8d")))
    routes = {r.resource.canonical for r in svc.make_app(svc.Node()).router.routes()}
    ok("the service answers exactly the paths the gateway site uses",
       {"/connect/client", "/exposure/verify"} <= routes
       and any(r.startswith("/exposed") for r in routes), routes)
    ok("the identity headers agree with the service", tuple(svc.IDENTITY_HEADERS) == m.IDENTITY_HEADERS)
    ok("the session cookie the service strips is the one identity sets",
       svc.SESSION_COOKIE == "oaap_session"
       and 'SESSION_COOKIE_NAME="oaap_session"' in read(
           os.path.join(HERE, "..", "platform", "services", "identity", "app.py")))

# --- compose and image ---------------------------------------------------------
import yaml                                                    # noqa: E402
comp = yaml.safe_load(read(os.path.join(HERE, "..", "platform", "docker-compose.yml")))
cs = comp["services"]["connect"]
ok("compose: connect appends to the audit log (and nothing else of the platform's)",
   any(v.endswith("data/audit:/audit") for v in cs["volumes"]), cs["volumes"])
ok("compose: still no ports, still no profile", "ports" not in cs and "profiles" not in cs)
dockerfile = read(os.path.join(HERE, "..", "platform", "services", "connect", "Dockerfile"))
ok("the image carries the client (listed by name, like every file here)",
   "COPY client/oaap-expose.py" in dockerfile, dockerfile)
ok("the client file exists and is what the service serves",
   os.path.exists(os.path.join(HERE, "..", "platform", "services", "connect", "client",
                               "oaap-expose.py")))

print("")
print("OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
