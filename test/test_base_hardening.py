#!/usr/bin/env python3
"""Grundhärtung am Gateway, Stufe 1 (RFC-0140, gateway 0.2.28) und die
gemeinsame Bremse (RFC-0140 F1/D9); Grund-Header (Stufe 2) und der
Alias, der auf den Hauptnamen umleitet (Stufe 3).

    Kompression   jede Instanzseite trägt `encode zstd gzip`, je Instanz
                  abschaltbar (`oaap app hardening <i> --compress off`);
                  gespeichert nur, wenn aus
    Methoden      TRACE, TRACK, CONNECT -> 405 am Gateway, immer, vor allen
                  anderen Umschreibungen (KI-Abrufer, Wächter)
    Umzug         migrate-hardening schreibt alte Seiten einmal um, danach still
    Bremse        zwei Worker (zwei Verbindungen auf dieselbe Datei) teilen
                  einen Eimer: die erste 429 kommt beim Limit, nicht beim
                  Doppelten; ohne Datei zählt der Worker selbst weiter

Aufruf: python3 test/test_base_hardening.py
"""
import importlib
import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-hardening-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
m.refresh_destinations = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def record(name, port, **over):
    base = {
        "app_id": "website", "app_name": "Webseite", "version": "0.1.0",
        "channel": "production", "port": port, "container": f"oaap-app-{name}",
        "image": "oaap-app/website:0.1.0", "svc_port": 8080,
        "services": [{"service": "", "container": f"oaap-app-{name}",
                      "image": "oaap-app/website:0.1.0", "build": "", "port": 8080}],
        "routes": [{"path": "/", "roles": ["public"]}, {"path": "/admin", "roles": ["admin"]}],
        "storage": [], "config": [], "roles": [], "visibility": {},
        "id": (name * 12)[:12], "name": name,
    }
    base.update(over)
    return base


tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["web"] = record("web", 8301, tenant=tid)
reg["instances"]["shop"] = record("shop", 8302, tenant=tid)
m.save_registry(reg)


def site(name):
    m.write_app_caddy(name, m.load_registry()["instances"][name])
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


print("Was das Gateway schreibt")
w = site("web")
lines = w.splitlines()
n_sites = sum(1 for ln in lines if ln.startswith("\t@oaap_method "))
ok("encode zstd gzip genau einmal je Site-Block",
   n_sites >= 1 and lines.count(m.ENCODE_LINE) == n_sites, w[:600])
ok("Methodensperre: TRACE TRACK CONNECT -> 405",
   "\t@oaap_method method TRACE TRACK CONNECT" in lines
   and f"\trewrite @oaap_method {m.METHOD_RESERVED}" in lines
   and "\t\trespond 405" in lines, w[:900])
ok("keine Liste erlaubter Methoden (WebDAV bleibt möglich)",
   "PROPFIND" not in w and "not method" not in w)
i_meth = w.index("rewrite @oaap_method")
i_guard = w.index("rewrite @oaap_guard") if "rewrite @oaap_guard" in w else len(w)
ok("Methodensperre steht vor dem Wächter (erste Umschreibung gewinnt)", i_meth < i_guard)

m.ai_fetchers_set("web", m.load_registry()["instances"]["web"], "all", who="t")
w = site("web")
ok("… und vor der KI-Abrufer-Sperre", w.index("rewrite @oaap_method") < w.index("rewrite @oaap_ai"))
m.ai_fetchers_set("web", m.load_registry()["instances"]["web"], "allow", who="t")

body = m.site_body([{"path": "/", "roles": ["public"]}], "oaap-app-x", 8080, scope="web",
                   tenant=tid)
ok("site_body selbst trägt beides (jeder Eingang: LAN, Namen, Aliase)",
   m.ENCODE_LINE in body and "\t@oaap_method method TRACE TRACK CONNECT" in body)

print("\nGrund-Header (Stufe 2)")
w = site("web")
lines = w.splitlines()
ok("nosniff und Referrer-Policy genau einmal je Site-Block, nur mit ?",
   lines.count(m.HEADER_MARK) == n_sites
   and lines.count("\theader ?Referrer-Policy strict-origin-when-cross-origin") == n_sites, w[:900])
ok("HSTS ein Jahr, ohne includeSubDomains/preload, nur mit ?",
   f'\theader @oaap_tls ?Strict-Transport-Security "{m.HSTS_VALUE}"' in lines
   and m.HSTS_VALUE == "max-age=31536000" and "includeSubDomains" not in w
   and "preload" not in w)
ok("HSTS nur bei https: TLS-Site oder Edge mit X-Forwarded-Proto https",
   "\t@oaap_tls protocol https" in lines
   and "\t@oaap_edge_tls header X-Forwarded-Proto https" in lines
   and not any("Strict-Transport-Security" in ln and "@oaap_" not in ln for ln in lines))
ok("jede Header-Zeile setzt nur, wo die App nichts gesetzt hat",
   all("?" in ln for ln in lines if ln.startswith("\theader ") and "X-Robots-Tag" not in ln
       and ln.strip() != "header {"), [ln for ln in lines if ln.startswith("\theader ")])
ok("keine CSP, kein frame-ancestors, keine Permissions-Policy (§3, D4)",
   "Content-Security-Policy" not in w and "X-Frame-Options" not in w
   and "Permissions-Policy" not in w)
ok("site_body trägt die Header für jeden Eingang", m.HEADER_MARK in body)

print("\nSchalter")
msg = m.hardening_set("shop", m.load_registry()["instances"]["shop"], False, who="joerg",
                      role="server_admin")
rec = m.load_registry()["instances"]["shop"]
ok("aus: im Satz mit wer und seit", rec.get("hardening", {}).get("compress") is False
   and rec["hardening"].get("by") == "joerg" and rec["hardening"].get("since"), rec.get("hardening"))
ok("Antwort sagt, dass die eigene Kodierung der App bleibt", "untouched" in msg, msg)
s_off = site("shop")
ok("aus: kein encode, Methodensperre bleibt",
   m.ENCODE_LINE not in s_off and "@oaap_method" in s_off, s_off[:500])
ok("die andere Instanz bleibt komprimiert", m.ENCODE_LINE in site("web"))
ok("zweimal aus: nichts ändert sich",
   m.hardening_set("shop", m.load_registry()["instances"]["shop"], False).startswith("already"))
m.hardening_set("shop", m.load_registry()["instances"]["shop"], True, who="joerg")
rec = m.load_registry()["instances"]["shop"]
ok("an: der Satz verschwindet ganz (Standard steht nicht im Satz)", "hardening" not in rec, rec)
ok("an: encode wieder da", m.ENCODE_LINE in site("shop"))
ok("hardening_of: fremde Werte heißen Standard",
   m.hardening_of({"hardening": "quatsch"})["compress"] is True
   and m.hardening_of({"hardening": {"compress": False}})["compress"] is False
   and m.hardening_of({"hardening": {"compress": False}})["headers"] is True)

msg = m.hardening_set("shop", m.load_registry()["instances"]["shop"], headers=False, who="joerg")
rec = m.load_registry()["instances"]["shop"]
ok("Header aus: im Satz, Kompression bleibt an",
   rec.get("hardening", {}).get("headers") is False and "compress" not in rec["hardening"],
   rec.get("hardening"))
ok("Antwort sagt, dass die eigenen Header der App bleiben", "untouched" in msg, msg)
s_off = site("shop")
ok("Header aus: keine Header-Zeilen, encode und Methodensperre bleiben",
   "Strict-Transport-Security" not in s_off and m.HEADER_MARK not in s_off
   and m.ENCODE_LINE in s_off and "@oaap_method" in s_off, s_off[:600])
m.hardening_set("shop", m.load_registry()["instances"]["shop"], False, who="joerg")
m.hardening_set("shop", m.load_registry()["instances"]["shop"], headers=True, who="joerg")
rec = m.load_registry()["instances"]["shop"]
ok("Header wieder an, Kompression bleibt aus (Schalter unabhängig)",
   rec.get("hardening", {}).get("compress") is False and "headers" not in rec["hardening"]
   and m.HEADER_MARK in site("shop") and m.ENCODE_LINE not in site("shop"), rec.get("hardening"))
m.hardening_set("shop", m.load_registry()["instances"]["shop"], True, who="joerg")
ok("beide an: Satz leer", "hardening" not in m.load_registry()["instances"]["shop"])

print("\nCLI")
ns = type("A", (), {"name": "web", "compress": None, "headers": None})()
buf = io.StringIO()
with redirect_stdout(buf):
    m.cmd_hardening(ns)
ok("show nennt Kompression, Header und Methoden", "compression  on" in buf.getvalue()
   and "headers      on" in buf.getvalue()
   and "TRACE, TRACK, CONNECT" in buf.getvalue(), buf.getvalue())
ns2 = type("A", (), {"name": "web", "compress": "off", "headers": "off"})()
try:
    with redirect_stdout(io.StringIO()):
        m.cmd_hardening(ns2)
    refused = False
except SystemExit:
    refused = True
ok("beide Schalter auf einmal: abgelehnt", refused)

print("\nUmzug alter Seiten")
p = os.path.join(m.CADDY_APPS_DIR, "web.caddy")
old = "\n".join(ln for ln in site("web").splitlines()
                if ln != m.ENCODE_LINE and "oaap_method" not in ln
                and m.METHOD_RESERVED not in ln) + "\n"
with open(p, "w", encoding="utf-8") as f:
    f.write(old)
buf = io.StringIO()
with redirect_stdout(buf):
    m.cmd_migrate_hardening(None)
with open(p, encoding="utf-8") as f:
    now_ = f.read()
ok("alte Seite wird umgeschrieben und genannt", m.ENCODE_LINE in now_ and "@oaap_method" in now_
   and "1 instance site" in buf.getvalue(), buf.getvalue())
buf = io.StringIO()
with redirect_stdout(buf):
    m.cmd_migrate_hardening(None)
ok("zweiter Lauf still", buf.getvalue() == "", buf.getvalue())
# eine Seite aus Stufe 1 (encode + Methoden, keine Header)
s1 = "\n".join(ln for ln in site("web").splitlines()
               if "Strict-Transport-Security" not in ln and "oaap_tls" not in ln
               and "oaap_edge_tls" not in ln and ln != m.HEADER_MARK
               and "Referrer-Policy" not in ln) + "\n"
with open(p, "w", encoding="utf-8") as f:
    f.write(s1)
buf = io.StringIO()
with redirect_stdout(buf):
    m.cmd_migrate_hardening(None)
ok("Seite aus Stufe 1 lernt die Header", m.HEADER_MARK in open(p, encoding="utf-8").read()
   and "1 instance site" in buf.getvalue(), buf.getvalue())
m.hardening_set("shop", m.load_registry()["instances"]["shop"], False)
buf = io.StringIO()
with redirect_stdout(buf):
    m.cmd_migrate_hardening(None)
ok("ausgeschaltete Kompression wird vom Umzug nicht zurückgeholt",
   buf.getvalue() == "" and m.ENCODE_LINE not in open(os.path.join(m.CADDY_APPS_DIR, "shop.caddy"),
                                                      encoding="utf-8").read(), buf.getvalue())

print("\nAlias leitet um (Stufe 3)")
m.sync_instance_names = lambda reg, only=None: ([], [])
AP = os.path.join(m.CADDY_APPS_DIR, "instance-addresses.caddy")


def addr(action, name, hostname=None):
    ns = type("A", (), {"action": action, "name": name, "hostname": hostname})()
    buf = io.StringIO()
    with redirect_stdout(buf):
        m.cmd_address(ns)
    return buf.getvalue()


def blocks(text):
    """{site address: block text} of instance-addresses.caddy."""
    out, cur, name = {}, [], None
    for ln in text.splitlines():
        if ln.endswith(" {") and not ln.startswith("\t"):
            name, cur = ln[:-2], [ln]
        elif ln == "}" and name:
            cur.append(ln)
            out[name] = "\n".join(cur)
            name = None
        elif name:
            cur.append(ln)
    return out


def aps():
    return blocks(open(AP, encoding="utf-8").read())


# Bestand: eine Instanz mit Alias aus der Zeit vor Stufe 3 (kein alias_mode)
reg = m.load_registry()
reg["instances"]["web"]["address"] = "sglok-sbk.example"
reg["instances"]["web"]["aliases"] = ["www.sglok-sbk.example"]
m.save_registry(reg)
m.write_instance_address_caddy()
b = aps()
ok("Bestand (D3): ein alter Alias liefert weiter selbst aus",
   m.alias_mode(m.load_registry()["instances"]["web"]) == "serve"
   and m.ENCODE_LINE in b.get("https://www.sglok-sbk.example", ""), list(b))
canon_before = b.get("https://sglok-sbk.example")
out = addr("show", "web")
ok("show nennt, was die Aliasse tun", "Aliases: serve the instance" in out, out)

out = addr("aliases", "web", "redirect")
b = aps()
al = b.get("https://www.sglok-sbk.example", "")
ok("umgeschaltet: der Alias trägt nur die 301 auf den Hauptnamen",
   "redir https://sglok-sbk.example{uri} permanent" in al and "reverse_proxy" not in al
   and m.ENCODE_LINE not in al, al)
ok("auch http:// des Alias geht in einem Sprung zum Hauptnamen",
   "redir https://sglok-sbk.example{uri} permanent" in b.get("http://www.sglok-sbk.example", ""),
   b.get("http://www.sglok-sbk.example"))
ok("der Hauptname bleibt Byte für Byte gleich",
   canon_before and b.get("https://sglok-sbk.example") == canon_before)
ok("der Alias wird weiter protokolliert (Statistik zählt ihn als Umleitung)",
   m._LOG_BLOCK[0] in al, al)
ok("die App kennt weiter alle Namen (OAAP_INSTANCE_NAMES)",
   "www.sglok-sbk.example" in m.instance_names_env("web", m.load_registry()["instances"]["web"]))
out = addr("show", "web")
ok("show sagt: leitet um", "Aliases: redirect (301)" in out, out)
out = addr("aliases", "web", "redirect")
ok("zweimal umleiten: sagt 'already'", "already" in out, out)
addr("aliases", "web", "serve")
b = aps()
ok("zurück auf serve: Alias liefert wieder aus",
   m.ENCODE_LINE in b.get("https://www.sglok-sbk.example", ""), b.get("https://www.sglok-sbk.example"))
try:
    addr("aliases", "web", "sometimes")
    refused = False
except SystemExit:
    refused = True
ok("unbekannter Wert: abgelehnt", refused)

# Neu: erster Alias einer Instanz ab Stufe 3 -> redirect (D3)
reg = m.load_registry()
reg["instances"]["shop"]["address"] = "shop.example"
m.save_registry(reg)
out = addr("alias-add", "shop", "www.shop.example")
rec = m.load_registry()["instances"]["shop"]
b = aps()
ok("erster Alias ab jetzt (D3): leitet um, und die Ausgabe sagt es",
   rec.get("alias_mode") == "redirect" and "redirects 301" in out
   and "redir https://shop.example{uri}" in b.get("https://www.shop.example", ""), out)
addr("aliases", "shop", "serve")
addr("alias-remove", "shop", "www.shop.example")
addr("alias-add", "shop", "www2.shop.example")
ok("gewähltes serve bleibt beim nächsten ersten Alias",
   m.alias_mode(m.load_registry()["instances"]["shop"]) == "serve")

lines_ = m.alias_redirect_site("http", "www.x.example", "x.example", "10.0.0.1")
ok("hinter dem Edge: Wächter + Umleitung auf https, keine zweite http-Site",
   lines_[0] == "http://www.x.example {" and any("10.0.0.1" in ln for ln in lines_)
   and "\t\tredir https://x.example{uri} permanent" in lines_
   and sum(1 for ln in lines_ if ln.endswith(" {") and not ln.startswith("\t")) == 1, lines_)
# Caddy ordnet ein nacktes redir VOR handle -- der Wächter käme zu spät
# (gemessen auf oaap-test 28.11.: Aufruf nicht vom Edge bekam 301 statt 403)
ok("die Umleitung steht in einem handle, damit der Edge-Wächter zuerst greift",
   "\tredir https://x.example{uri} permanent" not in lines_
   and lines_.index("\thandle {") > max(i for i, ln in enumerate(lines_) if "10.0.0.1" in ln), lines_)

src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("ein Update behält alias_mode", 'reg["instances"][name]["alias_mode"] = inst["alias_mode"]' in src)
ok("das Portal fragt über den Spool (op alias-mode)", 'elif op == "alias-mode":' in src)
ok("die Umstellung steht im Mandantenprotokoll", '"instance.aliases"' in src)
reg = m.load_registry()
for k in ("web", "shop"):
    for f_ in ("address", "aliases", "alias_mode"):
        reg["instances"][k].pop(f_, None)
m.save_registry(reg)

print("\nGemeinsame Bremse (F1)")
try:
    import flask  # noqa: F401
    have_flask = True
except ImportError:
    have_flask = False
if not have_flask:
    print("SKIP  Bremse braucht flask (Identity-Abbild oder Runner)")
else:
    ID_DATA = tempfile.mkdtemp(prefix="oaap-hardening-id-")
    os.environ.update(SESSION_SECRET="s", SETUP_TOKEN="t", INTERNAL_API_KEY="k",
                      OAAP_IDENTITY_DATA_DIR=ID_DATA)
    sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "identity"))
    sys.modules.pop("app", None)
    ident = importlib.import_module("app")
    path = ident._RATE.path
    ok("Datei je Identity (Datenordner im Namen)", "oaap-throttle-" in os.path.basename(path), path)
    ident._RATE.clear()
    w1, w2 = ident._SharedRate(path), ident._SharedRate(path)   # zwei Worker
    now = time.time()
    res = [(w1 if i % 2 == 0 else w2).hit("s|/|1.2.3.4", 6, 60, now + i * 0.01)[0]
           for i in range(10)]
    ok("zwei Worker, Limit 6: genau 6 durch, dann 429", res == [True] * 6 + [False] * 4, res)
    ok("andere Adresse hat ihren eigenen Eimer", w2.hit("s|/|5.6.7.8", 6, 60, now)[0])
    allowed, _ = w1.hit("s|/|1.2.3.4", 6, 60, now + 61)
    ok("nach dem Fenster wieder offen", allowed)

    c = ident.app.test_client()
    ident._RATE.clear()
    codes = [c.get("/throttle", query_string={"scope": "web", "limit": "3", "window": "60",
                                               "route": "/"},
                   headers={"X-OAAP-Client": "9.9.9.9"}).status_code for _ in range(5)]
    ok("Endpunkt /throttle: 204 204 204 429 429", codes == [204, 204, 204, 429, 429], codes)
    r = c.get("/throttle", query_string={"scope": "web", "limit": "3", "window": "60", "route": "/"},
              headers={"X-OAAP-Client": "9.9.9.9"})
    ok("429 mit Retry-After", r.status_code == 429 and int(r.headers.get("Retry-After", "0")) >= 1)

    broken = ident._SharedRate(os.path.join(ID_DATA, "gibt-es-nicht", "x.sqlite"))
    err = io.StringIO()
    old_err, sys.stderr = sys.stderr, err
    try:
        res = [broken.hit("k", 2, 60, now)[0] for _ in range(3)]
    finally:
        sys.stderr = old_err
    ok("ohne Datei: zählt im Worker weiter statt zu scheitern", res == [True, True, False], res)
    ok("… und sagt es", "shared brake unavailable" in err.getvalue(), err.getvalue())

print(f"\n{'ALLE GRÜN' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
