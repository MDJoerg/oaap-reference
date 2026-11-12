#!/usr/bin/env python3
"""Geheimnisdateien: das Gateway weist sie ab, der Knoten sucht sie (RFC-0084).

    Muster         .env, .git/, Schlüssel, Abzüge … werden erkannt, auch unter
                   einem Präfix; /, /envelope, /config.json, /.well-known nicht.
    Gateway        jede Instanz bekommt rewrite + handle vor allen App-Routen;
                   eine erlaubte Kategorie fällt aus dem Muster, alle erlaubt
                   = kein Block.
    Selbstprüfung  ein echter Webserver mit liegengelassener .env und einer
                   Startseite für alles andere: genau .env ist ein Fund, die
                   Startseite nie.
    Lage           ein Fund öffnet exposure.found beim Mandanten UND beim
                   Knoten; erlaubt = keine Lage; nie „nur im Bericht“.
    Bericht        abgewiesene Anfragen und Funde stehen im Wochenbericht.

Braucht kein Docker. Aufruf: python3 test/test_guard.py
"""
import http.server
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-guard-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import guard as g  # noqa: E402
import notify as n  # noqa: E402
import report as r  # noqa: E402
import appctl as a  # noqa: E402

a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


print("=== Muster ===")
hits = ["/.env", "/.ENV", "/api/.env.local", "/.git/config", "/.git/HEAD", "/x/.svn/entries",
        "/wp-config.php.bak", "/id_rsa", "/server.key", "/backup.sql.gz", "/db.sqlite3",
        "/.aws/credentials", "/.npmrc", "/.htpasswd", "/var/task/vercel.json",
        "/serverless.yml", "/docker-compose.prod.yml", "/dump.bak"]
miss = ["/", "/envelope", "/environment", "/config.json", "/.well-known/oauth-protected-resource",
        "/assets/app.js", "/api/users", "/git", "/keys", "/docs/env.md", "/sql-tutorial"]
ok("alle Geheimnis-Pfade erkannt", all(g.blocked(p) for p in hits),
   [p for p in hits if not g.blocked(p)])
ok("gewöhnliche Pfade nicht", not any(g.blocked(p) for p in miss), [p for p in miss if g.blocked(p)])
ok("eine erlaubte Kategorie fällt heraus", not g.blocked("/backup.sql", ["dumps"])
   and g.blocked("/.env", ["dumps"]))
ok("alles erlaubt: kein Muster", g.path_regex(list(g.CATEGORIES)) == "")

print("=== Gateway ===")
dflt = a.ensure_default_tenant()
tv, _ = a.tenant_create("verein", "Verein")
reg = a.load_registry()
reg["instances"]["kasse"] = {"app_id": "kasse", "app_name": "Kasse", "tenant": tv,
                             "routes": [{"path": "/", "roles": ["public"]},
                                        {"path": "/api", "roles": ["user"]}],
                             "container": "oaap-app-kasse", "svc_port": 0, "port": 8950,
                             "channel": "production", "version": "1"}
a.save_registry(reg)
lines = a.guard_site_lines("kasse")
ok("rewrite auf den reservierten Pfad, ein handle, identity zählt",
   any("rewrite @oaap_guard /.oaap-guard" in x for x in lines)
   and any("rewrite * /guard?scope=kasse" in x for x in lines)
   and any("reverse_proxy identity:8000" in x for x in lines), lines)
body = "\n".join(a.site_body(reg["instances"]["kasse"]["routes"], "oaap-app-kasse", 80,
                             scope="kasse", tenant=tv))
ok("im gemeinsamen Baustein jedes Zugangs, vor /auth und vor den App-Routen",
   body.index("@oaap_guard") < body.index("handle /auth/*") < body.index("reverse_proxy oaap-app-kasse"),
   body[:800])
ok("ohne Instanz (z. B. Portal-Hilfsseiten): kein Block", a.guard_site_lines("") == [])
APPLIED = []
a._guard_apply = lambda nm, inst: APPLIED.append(nm)
try:
    a.guard_allow_set("kasse", "dumps", True, "")
    said = ""
except ValueError as e:
    said = str(e)
ok("eine Ausnahme braucht einen Grund", "say why" in said, said)
a.guard_allow_set("kasse", "dumps", True, "die Kasse bietet ihren Export als .sql an", who="joerg")
ok("Ausnahme gesetzt: Sites neu geschrieben, Muster ohne Abzüge, im Protokoll",
   APPLIED == ["kasse"] and "sql" not in "".join(a.guard_site_lines("kasse"))
   and any(e.get("action") == "guard.allow" for e in a.read_tenant_log(tv, limit=5)))

print("=== Selbstprüfung gegen einen echten Webserver ===")
ROOT = tempfile.mkdtemp()
open(os.path.join(ROOT, ".env"), "w").write("DATABASE_URL=postgres://u:geheim@db/x\nSECRET_KEY=abc\n")
os.makedirs(os.path.join(ROOT, ".git"))
open(os.path.join(ROOT, ".git", "HEAD"), "w").write("<!doctype html><p>kein git</p>")
open(os.path.join(ROOT, "backup.sql"), "w").write("CREATE TABLE x (id int);\n")
INDEX = b"<!doctype html><html><body>Startseite</body></html>"


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *x, **k):
        super().__init__(*x, directory=ROOT, **k)

    def send_error(self, code, message=None, explain=None):
        # a single-page app: every unknown path answers the start page, 200
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(INDEX)

    def log_message(self, *x):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
reg = a.load_registry()
reg["instances"]["kasse"]["svc_port"] = srv.server_address[1]
a.save_registry(reg)
a.container_ip = lambda c, net: "127.0.0.1"
out = a.guard_scan()
rec = out["instances"]["kasse"]
paths = {f["path"]: f for f in rec["findings"]}
ok("Fund: /.env (Variablen); /api/.env antwortet die Startseite -- kein Fund",
   "/.env" in paths and paths["/.env"]["why"].startswith("Variablen") and "/api/.env" not in paths,
   rec)
ok("die Startseite für alles andere ist nie ein Fund, auch nicht .git/HEAD mit HTML",
   "/.git/HEAD" not in paths and "/.env.local" not in paths, rec)
ok("backup.sql ist ein Fund, aber erlaubt (Ausnahme der Kasse)",
   paths.get("/backup.sql", {}).get("allowed") is True, rec)
ok("die Ergebnisdatei nennt Pfade, nie Inhalte, 0644",
   "geheim" not in open(a.GUARD_SCAN_FILE).read()
   and oct(os.stat(a.GUARD_SCAN_FILE).st_mode & 0o777) in ("0o644", "0o666"))
srv.shutdown()

print("=== die Lage ===")
cur = a.guard_conditions()
ok("exposure.found beim Mandanten und beim Knoten, die erlaubte Datei nicht im Text",
   "exposure.found/kasse" in cur.get(tv, {}) and "exposure.found/kasse" in cur.get(dflt, {})
   and "/.env" in cur[tv]["exposure.found/kasse"]["text"]
   and "backup.sql" not in cur[tv]["exposure.found/kasse"]["text"], cur)
ok("… in notify_conditions", "exposure.found/kasse" in a.notify_conditions({}).get(tv, {}))
ok("kritisch, nie „nur im Bericht“", n.CATALOGUE["exposure.found"]["category"] == "critical"
   and "exposure.found" in n.NEVER_REPORT_ONLY)
data = json.load(open(a.GUARD_SCAN_FILE))
data["instances"]["kasse"]["findings"] = [f for f in data["instances"]["kasse"]["findings"]
                                          if f.get("allowed")]
json.dump(data, open(a.GUARD_SCAN_FILE, "w"))
ok("nach dem Entfernen: keine Lage mehr (Behoben folgt über die Lagen-Logik)",
   a.guard_conditions() == {})
ok("die Minutenaufgabe prüft höchstens einmal am Tag", a.guard_minute() is None)

print("=== Tagebuch und Bericht ===")
import time  # noqa: E402
hour = int(time.time() // 3600) - 1
os.makedirs(os.path.dirname(a.GUARD_DENIED_FILE), exist_ok=True)
json.dump({"kasse": {str(hour): {"n": 187, "c": ["aa11bb22"], "m": {"3": 187}}}},
          open(a.GUARD_DENIED_FILE, "w"))
data["instances"]["kasse"]["findings"].append({"path": "/.env", "category": "env",
                                               "why": "Variablen", "allowed": False})
json.dump(data, open(a.GUARD_SCAN_FILE, "w"))
day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3600))
lines = a.ledger_lines_for_day(day, measured_now=True)
ok("Tagebuch: abgewiesene Anfragen und der Fund, beim Mandanten und beim Knoten",
   lines[tv]["guard"]["kasse"]["count"] == 187 and lines[tv]["exposed"] == {"kasse": ["/.env"]}
   and lines[dflt]["guard"]["kasse"]["count"] == 187, lines[tv])
md = r.build([lines[tv]], [day], "Wochenbericht Verein", scope_is_node=False)
ok("Bericht: Fund fett in der Zusammenfassung, Abschnitt Geheimnisdateien",
   "**1 Instanz gibt auf direkte Anfrage eine Geheimnisdatei heraus**" in md
   and "187 Anfragen nach Geheimnisdateien abgewiesen" in md and "| kasse | 187 | 1 Adresse | 1 |" in md, md)

print("=== die Freigabe-Zone (Connector) ===")
a.load_external_conf = lambda: ("oaap.example", "")
a.exposure_zone = lambda ext: ("t.oaap.example", "https")
a.connect_gateway_key = lambda: "k"
os.makedirs(a.CADDY_APPS_DIR, exist_ok=True)
a.write_exposures_caddy()
expo = open(os.path.join(a.CADDY_APPS_DIR, a.EXPOSE_SITE)).read()
ok("auch die Site der Freigaben weist Geheimnisdateien ab, gezählt unter „exposures“",
   "@oaap_guard" in expo and "/guard?scope=exposures" in expo
   and expo.index("@oaap_guard") < expo.index("/exposure/verify"), expo[:600])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
