#!/usr/bin/env python3
"""Das dauerhafte Zugriffsprotokoll schreibt keine Schluessel (0.1.103).

Das Gateway protokolliert jeden Zugriff auf einen oeffentlichen Namen in
`external-access.log`. Bis 0.1.102 mit der vollstaendigen URI -- ein
Schluessel im Query-String stand dort im Klartext (bdt-hub am 08.08.
darauf hingewiesen, die Pruefung zugesagt und nie eingeloest; erneut
gefragt vom Handball-Infoboard am 18.09.).

Jetzt gilt fuer dieses Protokoll derselbe Filter wie fuer das
Diagnose-Protokoll aus RFC-0038 D3, aus EINER Quelle:

1. jede Seite, die das Dauerprotokoll schreibt (Portal-Name,
   Instanz-Namen, eigene Adressen, Edge), traegt den Filter,
2. das Diagnose-Protokoll traegt wortgleich denselben,
3. die Felder, die das Portal liest (Zeit, Host, IP, Status), bleiben,
4. alte Dateien werden beim Update einmal umgeschrieben, danach nie
   wieder.

Aufruf: python3 test/test_access_log.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-access-log-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

reloads = []
m.reload_gateway = lambda: reloads.append(1)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def read(name):
    with open(os.path.join(m.CADDY_APPS_DIR, name), encoding="utf-8") as f:
        return f.read()


FILTER = "\n".join(m._log_filter("\t\t"))
MUST = ['request>uri regexp "\\?.*$" ""',
        "request>headers>Authorization replace REDACTED",
        "request>headers>Cookie replace REDACTED",
        "request>headers>Proxy-Authorization delete",
        "resp_headers>Set-Cookie delete",
        'resp_headers>Location regexp "\\?.*$" ""']

print("=== der Filter selbst ===")
for rule in MUST:
    ok(f"enthaelt: {rule}", rule in FILTER)
ok("das Protokoll bleibt JSON (das Portal liest ts/host/remote_ip/status)",
   "wrap json" in FILTER)
ok("kein Feld, das das Portal liest, wird geloescht oder ersetzt",
   not any(f in FILTER for f in ("request>host", "request>remote_ip",
                                  " status ", "ts ")))

print("\n=== eine Quelle fuer beide Protokolle ===")
ok("das Diagnose-Protokoll traegt wortgleich denselben Filter",
   FILTER in "\n".join(m._diagnose_log_block("x")))

print("\n=== jede Seite mit Dauerprotokoll traegt ihn ===")
DEFAULT = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"] = {
    "anzeige": {"app_id": "hb", "app_name": "HB", "version": "1.0",
                "channel": "test", "container": "oaap-anzeige",
                "port": 8160, "svc_port": 8080, "tenant": DEFAULT,
                "address": "hb.example.org",
                "routes": [{"path": "/", "roles": ["public"]}]}}
m.save_registry(reg)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "oaap.example.org", "edge": ""}, f)
m.save_edge([{"host": "kunde.example", "target": "10.0.0.5", "port": 80}])
m.refresh_generated_sites()
m.write_edge_caddy()
for name in ("external.caddy", "edge.caddy"):
    body = read(name)
    n_logs = body.count("output file /logs/external-access.log")
    ok(f"{name}: jedes Dauerprotokoll gefiltert ({n_logs} Stueck)",
       n_logs > 0 and body.count("request>uri regexp") == n_logs, body)
body = read("instance-addresses.caddy")
n_logs = body.count("output file /logs/external-access.log")
ok(f"instance-addresses.caddy: gefiltert ({n_logs} Stueck)",
   n_logs > 0 and body.count("request>uri regexp") == n_logs, body)

print("\n=== Umstellung vorhandener Knoten ===")
# a node from before 0.1.103: the same sites, logging full URIs
for name in ("external.caddy", "instance-addresses.caddy", "edge.caddy"):
    body = read(name)
    old = body.replace(FILTER, "\t\tformat json")
    with open(os.path.join(m.CADDY_APPS_DIR, name), "w", encoding="utf-8") as f:
        f.write(old)
    ok(f"Ausgangslage: {name} ungefiltert", "request>uri regexp" not in old)
# the instance's own site is current, so only the log step is due
m.write_app_caddy("anzeige", reg["instances"]["anzeige"])
reloads.clear()
m.cmd_migrate_stream_close(None)
for name in ("external.caddy", "instance-addresses.caddy", "edge.caddy"):
    ok(f"{name} danach gefiltert", "request>uri regexp" in read(name))
ok("genau ein Neuladen", len(reloads) == 1, reloads)
m.cmd_migrate_stream_close(None)
ok("zweiter Lauf tut nichts", len(reloads) == 1, reloads)

print("\n=== alte Zeilen von vor 0.1.103 (0.1.104, einmal je Knoten) ===")
import gzip  # noqa: E402

os.makedirs(m.GATEWAY_LOG_DIR, exist_ok=True)


def line(uri, auth=None, cookie=None, loc=None, extra=None):
    e = {"level": "info", "ts": 1789700000.5, "logger": "http.log.access",
         "request": {"remote_ip": "91.1.40.210", "host": "bdt-hub.joomp.de",
                     "method": "GET", "uri": uri, "headers": {}},
         "status": 200, "resp_headers": {}}
    if auth:
        e["request"]["headers"]["Authorization"] = [auth]
    if cookie:
        e["request"]["headers"]["Cookie"] = [cookie]
    if loc:
        e["resp_headers"]["Location"] = [loc]
    if extra:
        e["request"]["headers"].update(extra)
    return json.dumps(e)


old = "\n".join([
    line("/xr?key=GEHEIM-QUERY-1"),
    line("/api/x", auth="Bearer GEHEIM-BEARER-2", cookie="s=GEHEIM-COOKIE-3"),
    line("/ok"),                                     # nothing to change
    line("/login", loc="/auth/login?next=/x&t=GEHEIM-LOC-4",
         extra={"X-Oaap-User": ["gefaelscht"]}),
    "kein json, bleibt stehen",
]) + "\n" + '{"unfertig": "zeile'                     # Caddy mid-write
active = os.path.join(m.GATEWAY_LOG_DIR, "external-access.log")
rotated = os.path.join(m.GATEWAY_LOG_DIR,
                       "external-access-2026-09-05T17-30-07.744-size.log.gz")
with open(active, "w", encoding="utf-8") as f:
    f.write(old)
with gzip.open(rotated, "wb") as z:
    z.write(line("/alt?key=GEHEIM-GZ-5").encode() + b"\n")
unrelated = os.path.join(m.GATEWAY_LOG_DIR, "diagnose-x.log")
with open(unrelated, "w", encoding="utf-8") as f:
    f.write(line("/d?q=bleibt-unberuehrt") + "\n")

m.cmd_scrub_access_log(None)
now = open(active, encoding="utf-8").read()
gz = gzip.open(rotated, "rb").read().decode()
ok("kein Geheimnis mehr in der aktiven Datei",
   "GEHEIM" not in now, now)
ok("keines in der rotierten .gz-Datei", "GEHEIM" not in gz, gz)
ok("Pfade bleiben", "/xr" in now and "/api/x" in now and "/alt" in gz)
ok("Nachweise als REDACTED, nicht geloescht",
   now.count('"REDACTED"') == 2, now)
ok("Location behaelt den Pfad", '"/auth/login"' in now, now)
ok("gefaelschte X-Oaap-User-Kopfzeile entfernt", "gefaelscht" not in now)
rows = [json.loads(x) for x in now.splitlines()[:4]]
ok("Zeit, Host, IP und Status bleiben (was das Portal liest)",
   all(r["ts"] == 1789700000.5 and r["request"]["host"] == "bdt-hub.joomp.de"
       and r["request"]["remote_ip"] == "91.1.40.210" and r["status"] == 200
       for r in rows), rows)
ok("eine Zeile ohne Aenderungsbedarf bleibt byte-gleich",
   now.splitlines()[2] == line("/ok"))
ok("Nicht-JSON und eine unfertige letzte Zeile bleiben byte-gleich",
   now.splitlines()[4] == "kein json, bleibt stehen"
   and now.endswith('{"unfertig": "zeile'))
ok("andere Protokolldateien werden nicht angefasst",
   "bleibt-unberuehrt" in open(unrelated, encoding="utf-8").read())
ok("die Merkdatei zaehlt ehrlich (4 Zeilen, 2 Dateien)",
   json.load(open(os.path.join(m.GATEWAY_LOG_DIR, m.SCRUB_MARKER)))
   ["lines"] == 4)
with open(active, "a", encoding="utf-8") as f:
    f.write("\n" + line("/neu?key=nach-dem-lauf"))
m.cmd_scrub_access_log(None)
ok("zweiter Lauf tut nichts (Merkdatei)",
   "nach-dem-lauf" in open(active, encoding="utf-8").read())

print()
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLSCHLAG(E)")
sys.exit(1 if fails else 0)
