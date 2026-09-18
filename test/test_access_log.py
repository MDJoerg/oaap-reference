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

print()
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLSCHLAG(E)")
sys.exit(1 if fails else 0)
