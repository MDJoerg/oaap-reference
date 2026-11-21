"""/platform/context.json auf jeder Instanz-Site (RFC-0118 P3) und das Kit
der Plattform (P1/P2).

    python3 test/test_app_context.py

Was hier gehalten wird:

1. Jede Instanz-Site trägt /platform/context.json -- mit Anmeldung (wer die
   Instanz benutzen darf), als feste Antwort, vor /platform/*.
2. Der Inhalt: App, Version, Kanal, Plattformversion; Titel und Logo des
   Mandanten nur, wenn es sie gibt; Portal; Hilfe und Chat des EIGENEN
   Mandanten, nie eines fremden, eine Produktions-App nie zu einer Test-
   Instanz; keine Mandantenkennung, keine Gruppen (Guidelines §5.1 Regel 3).
3. Ein Backtick im Titel bricht Caddys Anführung nicht.
4. Ändert sich das Gesicht des Mandanten oder kommt eine Hilfe hinzu, ist die
   Site veraltet (Marker) und wird neu geschrieben.
5. Kit und Rahmenskript liegen unter static/ und halten, was RFC-0118 nennt.
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-ctx-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
m._platform_env = lambda: {"OAAP_VERSION": "0.1.340"}
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(m.EXTERNAL_FILE), exist_ok=True)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "node.example.org"}, f)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def record(name, tenant, app_id, channel="production", port=8101, address=None):
    c = f"oaap-app-{name}"
    r = {"app_id": app_id, "app_name": app_id, "version": "1.0.0",
         "channel": channel, "port": port, "container": c, "svc_port": 8000,
         "image": f"oaap-app/{app_id}:1.0.0", "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 8000}],
         "routes": [{"path": "/", "roles": ["user", "admin"]}], "visibility": {}}
    if address:
        r["address"] = address
    return r


def site(name):
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


def block(body, opener):
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == opener:
            out = []
            for x in lines[i + 1:]:
                if x == "\t}":
                    return out
                out.append(x.strip())
    return None


def ctx_of(body):
    b = block(body, "handle /platform/context.json {") or []
    for x in b:
        if x.startswith("respond `"):
            return json.loads(x[len("respond `"):x.rindex("`")])
    return None


tid = m.ensure_default_tenant()
m.tenant_create("bec", "Brandeis")
T = m.load_tenants()
BEC = [k for k, v in T.items() if v.get("label") == "bec"][0]
OTHER = "t-other00000"
reg = m.load_registry()
reg["instances"]["vorgaenge"] = record("vorgaenge", BEC, "vorgaenge", port=8101, address="vorgaenge.example.org")
reg["instances"]["chat-test"] = record("chat-test", BEC, "chat", "test", 8102, address="chat-test.example.org")
reg["instances"]["fremd-help"] = record("fremd-help", OTHER, "help", port=8103, address="hilfe.fremd.example")
m.save_registry(reg)
for n in reg["instances"]:
    m.write_app_caddy(n, m.load_registry()["instances"][n])

print("1. Die Route")
body = site("vorgaenge")
b = block(body, "handle /platform/context.json {")
ok("die Site trägt /platform/context.json", b is not None, body[:400])
ok("… mit Anmeldung für diese Instanz",
   b and any(x.startswith("uri /verify?roles=") and "&instance=vorgaenge" in x for x in b), b)
ok("… als JSON, nicht zwischengespeichert",
   b and "header Content-Type application/json" in b and "header Cache-Control no-store" in b, b)
ok("… vor dem statischen /platform/*",
   body.index("handle /platform/context.json {") < body.index("handle /platform/* {"))

print("2. Der Inhalt")
c = ctx_of(body)
ok("App, Version, Kanal", c and c["app"] == {"id": "vorgaenge", "version": "1.0.0", "channel": "production"}, c)
ok("die Plattformversion", c and c.get("platform") == "0.1.340", c)
ok("ohne Gesicht kein Mandantentitel, kein Logo", c and "tenant" not in c, c)
ok("das Portal des Mandanten", c and c.get("portal", "").startswith("https://") and c["portal"].endswith("/"), c)
ok("keine Hilfe aus einem FREMDEN Mandanten", c and "help" not in c, c)
ok("eine Produktions-App nie zum Test-Chat", c and "chat" not in c, c)
ok("keine Mandantenkennung, keine Gruppen",
   c and BEC not in json.dumps(c) and "groups" not in c and "tenant_id" not in c, c)
ct = ctx_of(site("chat-test"))
ok("die Test-Instanz selbst nennt ihren Chat", ct and ct.get("chat") == "https://chat-test.example.org", ct)

print("3. Gesicht und Hilfe kommen dazu")
m.tenant_set_theme(BEC, title="Brandeis `GmbH` & Söhne")
body2 = site("vorgaenge")
c2 = ctx_of(body2)
ok("der neue Titel steht drin (Site neu geschrieben)", c2 and c2.get("tenant", {}).get("title") == "Brandeis `GmbH` & Söhne", c2)
ok("ein Backtick im Titel bleibt aus Caddys Anführung",
   "`GmbH`" not in body2 and "\\u0060GmbH" in body2, block(body2, "handle /platform/context.json {"))
reg = m.load_registry()
reg["instances"]["hilfe"] = record("hilfe", BEC, "help", port=8104, address="hilfe.example.org")
m.save_registry(reg)
m.refresh_generated_sites()
c3 = ctx_of(site("vorgaenge"))
ok("die Hilfe des eigenen Mandanten erscheint", c3 and c3.get("help") == "https://hilfe.example.org", c3)

print("4. Kit und Rahmen")
static = os.path.join(HERE, "..", "platform", "static")
css = open(os.path.join(static, "theme.css"), encoding="utf-8").read()
js = open(os.path.join(static, "app.js"), encoding="utf-8").read()
ok("D9: das Blau der Guidelines", "--oaap-color-primary: #2563eb" in css and "#2f6fed" not in css)
ok("Dunkelmodus aus dem System und auf Wunsch", "prefers-color-scheme: dark" in css and '[data-theme="dark"]' in css)
for cls in (".oaap-header", ".oaap-tabs", ".oaap-footer", ".oaap-card", ".oaap-filter",
            ".oaap-table", ".oaap-paging", ".oaap-badge", ".oaap-empty", ".oaap-objhead"):
    ok(f"das Kit hat {cls}", cls in css)
ok("das Skript liest Kontext und Person", "/platform/context.json" in js and "/auth/whoami" in js)
ok("Abmelden ist ein POST", 'method: "post"' in js and "/auth/logout" in js)
ok("Rollen werden nicht ausgegeben", not re.search(r"me\.roles", js))
ok("Hilfe über /p/<produkt>, Fragen über ?frage=&kontext=",
   '"/p/"' in js and "?frage=" in js and "&kontext=" in js)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
