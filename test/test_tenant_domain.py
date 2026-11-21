#!/usr/bin/env python3
"""Die eigene Domain eines Mandanten (RFC-0115 Stufe 1).

Stufe 0 hat auf oaap-test gemessen, was heute bricht: Auf einem eigenen
Namen fehlt die Anmeldung des Mandanten (kein Knopf fuer seinen Anbieter,
`/auth/oidc/start` 404), das Cookie gilt nur fuer genau diesen Namen, und
Namen ohne eigene Seite landen bei der Anmeldung des Knotens.

Geprueft wird hier, dass eine Domain genau einen Mandanten nennt -- in
`place.py`, wo Portal, Anmeldedienst und `appctl` dasselbe Urteil holen --,
dass `appctl` sie nur nach der DNS-Pruefung ausliefert, und dass der
Anmeldedienst auf ihr die Anmeldung des Mandanten zeigt, das Cookie auf
sie weitet und den Anbieter dort zurueckkommen laesst.

Koeder:
- eine Domain unter oder ueber dem Knotennamen, oder eine, die die eines
  anderen Mandanten ueberlappt, wird abgelehnt;
- eine wartende (`pending`) oder abgeschaltete Domain nennt nichts und
  bekommt keine Seite;
- das Cookie der Domain gilt nie fuer den Knoten, und umgekehrt;
- ein Ruecksprung in eine fremde Domain nach der Anmeldung wird
  verweigert (offene Weiterleitung);
- unter der Domain antworten hinter einem Edge nur die Instanzen, alles
  andere mit 404;
- die Sandbox-Namen bleiben unter dem Knoten (D6).

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_tenant_domain.py
"""
import argparse
import contextlib
import io as _io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-domain-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, os.path.join(SERVICES, "portal"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402
import instance_view as iv                                     # noqa: E402
import idp_admin                                               # noqa: E402
import place                                                   # noqa: E402

m.reload_gateway = lambda: None
m.sync_instance_names = lambda *a, **k: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

HOST = "oaap.example.org"
DOM = "bec.kunde.example"
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def quiet(fn, *a, **k):
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn(*a, **k)
            return 0, buf.getvalue()
        except SystemExit as e:
            return (e.code or 0), buf.getvalue()


def tenant(action, name=None, target=None, title=""):
    return quiet(m.cmd_tenant, argparse.Namespace(
        action=action, name=name, target=target, title=title, account="",
        account_name="", grace_days=30, yes=True, count=50, dry_run=False))


# ---------------------------------------------------------------------------
print("place.py: eine Domain nennt genau einen Mandanten")

T = {"D": {"label": "default"},
     "B": {"label": "bec", "domain": {"name": DOM, "state": "active", "canonical": "domain"}},
     "C": {"label": "cls", "domain": {"name": "cls.kunde.example", "state": "pending"}},
     "E": {"label": "efg"}}
NOW = "2026-11-20T00:00:00+00:00"
ok("die Domain selbst nennt den Mandanten", place.host_place(DOM, HOST, T, "D", NOW) == ("B", True))
ok("ein Name darunter nennt keinen Ort (dort steht eine Instanz)",
   place.host_place(f"chat.{DOM}", HOST, T, "D", NOW) == (None, True))
ok("KÖDER: eine wartende Domain nennt nichts",
   place.domain_of_host("cls.kunde.example", T) == (None, False)
   and place.host_place("cls.kunde.example", HOST, T, "D", NOW) == (None, True))
ok("der Ort unter dem Knoten bleibt, wie er war",
   place.host_place(f"bec.{HOST}", HOST, T, "D", NOW) == ("B", True))
ok("Cookie: die Domain bekommt ihr eigenes, der Knoten seins",
   place.cookie_scope(f"chat.{DOM}", HOST, T) == DOM and place.cookie_scope(DOM, HOST, T) == DOM
   and place.cookie_scope(f"chat.bec.{HOST}", HOST, T) == HOST)
ok("KÖDER: das Cookie der Domain gilt nie für den Knoten (und ein fremder Name bekommt keins)",
   place.cookie_scope(f"x.{HOST}", HOST, T) != DOM and place.cookie_scope("evil.example", HOST, T) == "")
ok("der Ort einer Anmeldung: von der Domain aus die Domain, sonst <label>.<knoten>",
   place.place_host_of("B", T, HOST, "D", f"chat.{DOM}") == DOM
   and place.place_host_of("B", T, HOST, "D", f"chat.bec.{HOST}") == f"bec.{HOST}"
   and place.place_host_of("D", T, HOST, "D", HOST) == HOST)
for bad, why in ((f"x.{HOST}", "unter dem Knotennamen"), ("example.org", "über dem Knotennamen"),
                 (HOST, "der Knotenname selbst"), ("kunde.example", "überlappt cls und bec"),
                 (f"a.{DOM}", "unter der Domain eines anderen"), ("nicht eine domain", "kein Name")):
    ok(f"KÖDER: abgelehnt -- {why}", place.domain_refusal(bad, HOST, T, "E") != "", bad)
ok("eine freie Domain wird angenommen", place.domain_refusal("efg.anders.example", HOST, T, "E") == "")
ok("der eigene Name darf neu gesetzt werden", place.domain_refusal(DOM, HOST, T, "B") == "")
ok("Portal und appctl sind sich über den gedruckten Namen einig",
   iv.auto_host("chat", {"name": "chat", "tenant": "B"}, T, HOST) == f"chat.{DOM}"
   and iv.auto_host("chat", {"name": "chat", "tenant": "C"}, T, HOST) == f"chat.cls.{HOST}")

# ---------------------------------------------------------------------------
print("\nappctl: setzen, prüfen, ausliefern")

os.makedirs(os.path.dirname(m.EXTERNAL_FILE), exist_ok=True)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": HOST}, f)
default_id = m.ensure_default_tenant()
tenant("create", "bec", title="Brandeis")
tenant("create", "cls", title="Clausen")
bec_id = m.tenant_by_label("bec")[0]
cls_id = m.tenant_by_label("cls")[0]
reg = m.load_registry()
for key, local, tid in (("bec-chat", "chat", bec_id), ("cls-wiki", "wiki", cls_id)):
    reg["instances"][key] = {"name": local, "tenant": tid, "container": f"oaap-app-{key}",
                             "svc_port": 8000, "port": 8100, "routes": [{"path": "/", "roles": ["user"]}]}
m.save_registry(reg)

DNS = {}
m.domain_dns_check.__defaults__ = (lambda h: DNS.get("x." + h.split(".", 1)[1] if h.startswith("oaap-check-") else h, set()),)
DNS[HOST] = {"203.0.113.7"}


def caddy():
    with open(os.path.join(m.CADDY_APPS_DIR, "external.caddy"), encoding="utf-8") as f:
        return f.read()


rc, out = tenant("domain", "bec", f"x.{HOST}")
ok("KÖDER: eine Domain unter dem Knotennamen wird beim Setzen abgelehnt", rc != 0, out)
rc, out = tenant("domain", "bec", DOM)
t = m.load_tenants()[bec_id]
ok("DNS zeigt noch nicht her: eingetragen, aber wartend, mit dem Satz, was fehlt",
   rc == 0 and t["domain"]["state"] == "pending" and "does not fit" in out and f"{DOM}  A/AAAA" in out, out)
m.write_external_caddy()
ok("KÖDER: eine wartende Domain bekommt keine Seite", DOM not in caddy())
DNS[DOM] = {"203.0.113.7"}
rc, out = tenant("domain", "bec", "check")
ok("nur die Domain, ohne Wildcard: weiter wartend, der Satz nennt *.domain",
   m.load_tenants()[bec_id]["domain"]["state"] == "pending" and f"*.{DOM}" in out, out)
DNS[f"x.{DOM}"] = {"203.0.113.7"}
rc, out = tenant("domain", "bec", "check")
t = m.load_tenants()[bec_id]
ok("Domain und Wildcard zeigen her: aktiv, mit der Rücksprungadresse für den Anbieter",
   t["domain"]["state"] == "active" and f"https://{DOM}/auth/oidc/callback" in out, out)
c = caddy()
ok("das Gateway schreibt den Ort auf der Domain", f"https://{DOM} {{" in c)
ok("und jede Instanz des Mandanten darunter", f"https://chat.{DOM} {{" in c)
ok("KÖDER: keine Instanz eines anderen Mandanten unter der Domain", f"wiki.{DOM}" not in c)
ok("die Namen unter dem Knoten antworten weiter", f"https://bec.{HOST} {{" in c and f"https://chat.bec.{HOST} {{" in c)
ok("http leitet auch für die Domain auf https", f"http://*.{DOM}" in c)
names = m.instance_auto_hosts("bec-chat", m.load_registry()["instances"]["bec-chat"], ext_host=HOST)
ok("die Domain steht zuerst (gedruckter Name)", names[:2] == [f"chat.{DOM}", f"chat.bec.{HOST}"], names)
inst = dict(m.load_registry()["instances"]["bec-chat"], ui_sandbox="/sandbox/")
m.ui_sandbox_path = lambda i: "/sandbox/" if i.get("ui_sandbox") else ""
sb = m.instance_sandbox_hosts("bec-chat", inst, ext_host=HOST)
ok("D6: Sandbox-Namen bleiben unter dem Knoten", sb and all(DOM not in h for h in sb), sb)
ok("der Connector trägt die Rücksprungadresse der Domain mit ein",
   idp_admin.redirect_uris_for("bec", HOST, domain=place.tenant_domain(t))
   == [f"https://bec.{HOST}/auth/oidc/callback", f"https://{DOM}/auth/oidc/callback"])
# oaapx01, 2026-11-20: chat.bec.brandeis.de war schon ein eigener Name der Instanz,
# bevor bec die Domain bekam -- zwei Seiten für einen Namen lehnt Caddy ab
reg = m.load_registry()
reg["instances"]["bec-chat"]["address"] = f"chat.{DOM}"
m.save_registry(reg)
m.write_external_caddy()
m.write_instance_address_caddy()
c = caddy()
with open(os.path.join(m.CADDY_APPS_DIR, "instance-addresses.caddy"), encoding="utf-8") as f:
    own = f.read()
sites = (c + own).count(f"https://chat.{DOM} {{")
ok("KÖDER: ein Name, den eine Instanz schon als eigenen trägt, steht genau einmal im Gateway",
   sites == 1 and f"https://chat.{DOM} {{" in own and f"https://{DOM} {{" in c, sites)
reg["instances"]["bec-chat"].pop("address")
m.save_registry(reg)
m.write_instance_address_caddy()
calls = []


def refused_reload():
    calls.append(1)
    if len(calls) == 1:
        m.die("caddy: ambiguous site definition")


m.reload_gateway = refused_reload
tenant("domain", "bec", "off")
calls.clear()
rc, out = tenant("domain", "bec", DOM)
ok("lehnt das Gateway die Seiten ab, wartet die Domain wieder, und die Dateien stehen ohne sie",
   rc != 0 and m.load_tenants()[bec_id]["domain"]["state"] == "pending" and DOM not in caddy()
   and len(calls) == 2, (rc, out[-300:]))
m.reload_gateway = lambda: None
tenant("domain", "bec", DOM)
rc, out = tenant("domain", "cls", DOM)
ok("KÖDER: dieselbe Domain für einen zweiten Mandanten wird abgelehnt", rc != 0, out)
rc, out = tenant("domain", "default", "x.anders.example")
ok("KÖDER: der Standard-Mandant hat keine eigene Domain", rc != 0, out)

with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": HOST, "edge": "10.10.10.75"}, f)
m.write_external_caddy()
c = caddy()
wild = c[c.find(f"http://*.{DOM} {{"):]
wild = wild[:wild.find("\n}\n") + 3]
ok("hinter einem Edge: unbekannte Namen unter der Domain bekommen 404, nur vom Edge",
   "respond 404" in wild and "not remote_ip 10.10.10.75" in wild, wild)
ok("hinter einem Edge: Ort und Instanz als http-Seiten mit Edge-Schutz",
   f"http://{DOM} {{" in c and f"http://chat.{DOM} {{" in c)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": HOST}, f)

rc, out = tenant("domain", "bec", "off")
m.write_external_caddy()
ok("abgeschaltet: keine Seite mehr, der Satz bleibt", DOM not in caddy()
   and m.load_tenants()[bec_id]["domain"]["state"] == "off")
rc, out = tenant("domain", "bec", "check")
ok("KÖDER: eine abgeschaltete Domain schaltet sich durch check nicht wieder ein",
   m.load_tenants()[bec_id]["domain"]["state"] == "off", out)
tenant("domain", "bec", DOM)
ok("neu gesetzt und passend: wieder aktiv", m.load_tenants()[bec_id]["domain"]["state"] == "active")


# ---------------------------------------------------------------------------
print("\nStufe 2: wo die Plattform den Ort druckt")

TT = m.load_tenants()
ok("der gedruckte Ort ist die Domain, solange sie gilt",
   place.canonical_place_host(bec_id, TT, HOST, default_id) == DOM
   and place.canonical_place_host(cls_id, TT, HOST, default_id) == f"cls.{HOST}")
ok("das Gesicht (Anmeldeseite, Portal) nennt die Domain als Adresse",
   place.theme_of(TT[bec_id], "bec", HOST)["address"] == DOM
   and place.theme_of(TT[cls_id], "cls", HOST)["address"] == f"cls.{HOST}")
ok("eine Mail an den Mandanten verlinkt die Domain", m.notify_link(bec_id, "/x") == f"https://{DOM}/x"
   and m.notify_link(cls_id, "/x") == f"https://cls.{HOST}/x", m.notify_link(bec_id, "/x"))
rc, out = tenant("domain", "bec", "check")
ok("beim Aktivieren nennt der Befehl die Apps, die die Namen erst nach einem Neustart kennen",
   "sudo oaap app restart bec-chat" in out, out[-400:])

# die tägliche Nachprüfung
tenant("domain", "bec", "off")
TT = m.load_tenants()
TT[bec_id]["domain"]["state"] = "pending"
m.save_tenants(TT)
saved = dict(DNS)
DNS.pop(f"x.{DOM}")
rc, out = tenant("domains")
ok("täglich: passt DNS noch nicht, bleibt die Domain wartend und still",
   m.load_tenants()[bec_id]["domain"]["state"] == "pending" and DOM not in caddy(), out)
DNS.update(saved)
rc, out = tenant("domains")
ok("täglich: sobald DNS passt, wird sie aktiv und bekommt ihre Seiten",
   m.load_tenants()[bec_id]["domain"]["state"] == "active" and f"https://{DOM} {{" in caddy(), out)
DNS.pop(DOM)
rc, out = tenant("domains")
ok("KÖDER: täglich schaltet ein DNS-Aussetzer eine aktive Domain nie ab",
   m.load_tenants()[bec_id]["domain"]["state"] == "active" and "stays active" in out, out)
DNS.update(saved)
for f in ("platform/migrate.sh", "install.sh"):
    src = open(os.path.join(HERE, "..", f), encoding="utf-8").read()
    ok(f"{f} richtet den täglichen Zeitgeber ein",
       "oaap-domain-check.timer" in src and "appctl.py tenant domains" in src)

# das Portal
import app as p                                                # noqa: E402

TT = m.load_tenants()
p.load_tenants = lambda: TT
p.load_instances = lambda: m.load_registry()["instances"]
p.external_host = lambda: HOST
p.default_tenant_id = lambda: default_id
p.resolve_tenant = lambda ref: (ref or default_id) if (ref or default_id) in TT else None
p.multi_tenant = lambda: True
p.caller_record = lambda: {"tenant": bec_id}
p.setup_done = lambda: True
p.identity_users = lambda: []
p.caller_groups = lambda: set()
p.app.config["TESTING"] = True
pc = p.app.test_client()
ok("Portal: der Link „Einrichten“ zeigt auf die Domain", p._place_url("bec", "/einrichten") == f"https://{DOM}/einrichten"
   and p._place_url("cls", "/einrichten") == f"https://cls.{HOST}/einrichten")
facts = p._tenant_domain_facts(bec_id)
ok("Portal: die Gesundheitsseite kennt Domain und Zustand", facts and facts["name"] == DOM and facts["word"] == "aktiv", facts)
ok("Portal: ohne Domain steht dort nichts", p._tenant_domain_facts(cls_id) is None)
r = pc.get("/", headers={"Host": DOM, "X-OAAP-Roles": "user", "X-OAAP-User": "anna"})
body = r.get_data(as_text=True)
ok("Portal am Ort der Domain: die Kachel verlinkt <app>.<domain>", r.status_code == 200 and f"https://chat.{DOM}/" in body,
   (r.status_code, body[-300:]))


# ---------------------------------------------------------------------------
print("\n§15: eine App unter der Domain verbergen, Domain aus dem Portal")

ok_h, said = m.instance_set_domain_hidden("bec-chat", True, who="root")
c = caddy()
names = m.instance_auto_hosts("bec-chat", m.load_registry()["instances"]["bec-chat"], ext_host=HOST)
ok("verborgen: kein Name unter der Domain, der Name unter dem Knoten bleibt",
   ok_h and f"chat.{DOM}" not in names and f"chat.bec.{HOST}" in names
   and f"https://chat.{DOM} {{" not in c and f"https://chat.bec.{HOST} {{" in c, (said, names))
ok("verborgen: das Portal verlinkt die App unter dem Knoten",
   iv.auto_host("chat", m.load_registry()["instances"]["bec-chat"], m.load_tenants(), HOST) == f"chat.bec.{HOST}")
ok("verborgen: der Ort der Domain bleibt", f"https://{DOM} {{" in c)
log = [e for e in m.read_tenant_log(bec_id) if e["action"] == "instance.domain"]
ok("das Verbergen steht im Protokoll des Mandanten", log and "hidden under" in log[-1].get("detail", ""), log[-1:])
ok_h2, said2 = m.instance_set_domain_hidden("bec-chat", True)
ok("noch einmal verbergen: nichts geändert, kein Fehler", ok_h2 and "nothing changed" in said2, said2)
ok_n, said_n = m.instance_set_domain_hidden("cls-wiki", True)
ok("KÖDER: ohne eigene Domain gibt es nichts zu verbergen", not ok_n and "no own domain" in said_n, said_n)
rc, out = quiet(m.cmd_address, argparse.Namespace(action="domain-show", name="bec-chat", hostname=None))
ok("CLI: oaap app address bec-chat domain-show zeigt sie wieder", rc == 0 and f"https://chat.{DOM} {{" in caddy(), out)
rc, out = quiet(m.cmd_address, argparse.Namespace(action="domain-hide", name="bec-chat", hostname=None))
ok("CLI: domain-hide verbirgt", rc == 0 and f"https://chat.{DOM} {{" not in caddy(), out)

# die Warteschlange (der Weg des Portals) -- durch den echten Worker
users_dir = os.path.join(DATA, "data", "identity")
os.makedirs(users_dir, exist_ok=True)
with open(os.path.join(users_dir, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": "root", "roles": ["server_admin"], "tenant": "", "groups": [], "active": True},
               {"username": "karin", "roles": ["tenant_admin"], "tenant": bec_id, "groups": [], "active": True}], f)
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def spool(by, instance, **req):
    _rid[0] += 1
    rid = f"d{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": instance, "by": by, **req}, f)
    with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


r = spool("karin", "", action="tenant-domain", tenant=bec_id, what="off")
ok("KÖDER: eine tenant_admin ordnet keine Domain zu (auch nicht die eigene)",
   not r.get("ok") and m.load_tenants()[bec_id]["domain"]["state"] == "active", r)
r = spool("karin", "bec-chat", action="instance-domain", hidden=False)
ok("KÖDER: eine tenant_admin verbirgt und zeigt keine App",
   not r.get("ok") and m.load_registry()["instances"]["bec-chat"].get("domain_hidden"), r)
r = spool("root", "bec-chat", action="instance-domain", hidden=False)
ok("server_admin über die Warteschlange: die App wieder unter der Domain",
   r.get("ok") and not m.load_registry()["instances"]["bec-chat"].get("domain_hidden")
   and f"https://chat.{DOM} {{" in caddy(), r)
r = spool("root", "", action="tenant-domain", tenant=bec_id, what="off")
ok("server_admin über die Warteschlange: Domain abschalten, die Antwort ist der Satz der CLI",
   r.get("ok") and m.load_tenants()[bec_id]["domain"]["state"] == "off" and "switched off" in r.get("message", ""), r)
r = spool("root", "", action="tenant-domain", tenant=bec_id, what=f"x.{HOST}")
ok("eine Ablehnung kommt mit dem Satz der CLI zurück", not r.get("ok") and "node's own name" in r.get("message", ""), r)
r = spool("root", "", action="tenant-domain", tenant=bec_id, what=DOM)
ok("server_admin über die Warteschlange: Domain zuordnen und prüfen", r.get("ok")
   and m.load_tenants()[bec_id]["domain"]["state"] == "active" and "Active." in r.get("message", ""), r)

# die Seite im Portal
TT = m.load_tenants()
p.load_tenants = lambda: TT
p.load_instances = lambda: m.load_registry()["instances"]
p.caller_scope = lambda: ("server_admin", default_id)
p.require_user_admin = lambda: None
body = pc.get("/tenant/domain?tenant=bec", headers={"Host": HOST, "X-OAAP-Roles": "server_admin",
                                                     "X-OAAP-User": "root"}).get_data(as_text=True)
ok("Portal: die Seite Domain zeigt Domain, Zustand und die Apps mit Verbergen",
   DOM in body and "aktiv" in body and "Unter der Domain verbergen" in body and f"chat.bec.{HOST}" in body, body[-400:])
p.caller_scope = lambda: ("tenant_admin", bec_id)
r = pc.get("/tenant/domain?tenant=bec", headers={"Host": HOST, "X-OAAP-Roles": "tenant_admin", "X-OAAP-User": "karin"})
ok("KÖDER: eine tenant_admin kommt nicht auf die Seite", r.status_code == 303, r.status_code)
p.caller_scope = lambda: ("server_admin", default_id)
r = pc.get("/tenant/domain?tenant=default", headers={"Host": HOST, "X-OAAP-Roles": "server_admin", "X-OAAP-User": "root"})
ok("KÖDER: der Standard-Mandant hat keine Domain-Seite", r.status_code == 303, r.status_code)

# ---------------------------------------------------------------------------
print("\nDer Anmeldedienst auf der Domain")


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        raw = json.dumps({"issuer": ISSUER, "authorization_endpoint": ISSUER + "/auth",
                          "token_endpoint": ISSUER + "/token",
                          "token_endpoint_auth_methods_supported": ["client_secret_basic"]}).encode()
        self.send_response(200 if "openid-configuration" in self.path else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


srv = HTTPServer(("127.0.0.1", 0), Provider)
ISSUER = f"http://127.0.0.1:{srv.server_port}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
ID_DATA = tempfile.mkdtemp(prefix="oaap-domain-id-")
os.environ["OAAP_IDENTITY_DATA_DIR"] = ID_DATA
import importlib.util                                          # noqa: E402

spec = importlib.util.spec_from_file_location("identity_app_domain", os.path.join(SERVICES, "identity", "app.py"))
ident = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ident)
TEN = {"D": {"label": "default", "former_labels": []},
       "B": {"label": "bec", "name": "Brandeis", "former_labels": [],
             "domain": {"name": DOM, "state": "active", "canonical": "domain"},
             "idp": {"kind": "oidc", "issuer": ISSUER, "client_id": "oaap-node", "label": "Mit dem Vereinskonto anmelden"}},
       "C": {"label": "cls", "name": "Clausen", "former_labels": [],
             "domain": {"name": "cls.kunde.example", "state": "pending"}}}
ident.known_tenants = lambda: TEN
ident._external_host = lambda: HOST
ident.default_tenant_id = lambda: "D"
ident.AUDIT_LOG = os.path.join(ID_DATA, "tenant-log.jsonl")
with open(os.path.join(ID_DATA, "secrets.json"), "w", encoding="utf-8") as f:
    json.dump({"providers": {"B": {"client_secret": "geheim"}}}, f)
ident.IDP_SECRETS_FILE = os.path.join(ID_DATA, "secrets.json")
ident.app.config["TESTING"] = True
cl = ident.app.test_client()


def get(path, host, **kw):
    return cl.get(path, headers={"Host": host, "X-Forwarded-Host": host, "X-Forwarded-Proto": "https"}, **kw)


body = get("/auth/login", DOM).get_data(as_text=True)
ok("auf der Domain: die Anmeldung des Mandanten mit dem Knopf seines Anbieters (Stufe 0: fehlte)",
   "Mit dem Vereinskonto anmelden" in body and "/auth/oidc/start" in body, body[body.find("<form"):][:300])
r = get("/auth/oidc/start", DOM)
loc = r.headers.get("Location", "")
ruri = unquote(dict(p.split("=", 1) for p in loc.split("?", 1)[1].split("&")).get("redirect_uri", "")) if "?" in loc else ""
ok("der Anbieter startet auf der Domain (Stufe 0: 404) und kommt dort zurück",
   r.status_code == 303 and ruri == f"https://{DOM}/auth/oidc/callback", (r.status_code, loc[:200]))
setc = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
ok("das Cookie gilt für die ganze Domain", any(f"Domain={DOM}" in v or f"Domain=.{DOM}" in v for v in setc), setc)
r = get("/auth/login?tenant=B&next=%2F", f"chat.{DOM}")
ok("von einer Instanz unter der Domain geht die Anmeldung an den Ort der Domain",
   r.status_code in (302, 303) and r.headers.get("Location", "").startswith(f"https://{DOM}/auth/oidc/start"),
   (r.status_code, r.headers.get("Location")))
ok("... und kommt danach zu genau dieser Instanz zurück",
   f"back=chat.{DOM}" in r.headers.get("Location", ""), r.headers.get("Location"))
r = get(f"/auth/oidc/start?back=chat.bec.{HOST}", DOM)
loc = r.headers.get("Location", "")
with ident.app.test_request_context("/", headers={"Host": DOM, "X-Forwarded-Host": DOM}):
    ok("KÖDER: ein Rücksprung von der Domain zum Knoten (anderes Cookie) wird verweigert",
       ident._return_host(f"chat.bec.{HOST}") == "" and ident._return_host(f"chat.{DOM}") == f"chat.{DOM}")
    ok("KÖDER: ein Rücksprung zu einem fremden Namen wird verweigert", ident._return_host("evil.example") == "")
with ident.app.test_request_context("/", headers={"Host": f"bec.{HOST}", "X-Forwarded-Host": f"bec.{HOST}"}):
    ok("am Ort unter dem Knoten wie bisher: Rücksprung nur unter dem Knoten",
       ident._return_host(f"chat.bec.{HOST}") == f"chat.bec.{HOST}" and ident._return_host(f"chat.{DOM}") == "")
body = get("/auth/login", "cls.kunde.example").get_data(as_text=True)
ok("KÖDER: eine wartende Domain zeigt nicht die Anmeldung des Mandanten", "Clausen" not in body, body[:300])
body = get("/auth/login", f"bec.{HOST}").get_data(as_text=True)
ok("der Ort unter dem Knoten zeigt die Anmeldung weiter", "Mit dem Vereinskonto anmelden" in body)

print("ALLE PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
