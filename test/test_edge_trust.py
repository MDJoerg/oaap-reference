#!/usr/bin/env python3
"""Hinter einem Edge glaubt das Gateway dem Edge X-Forwarded-* (0.1.240).

Caddy uebernimmt X-Forwarded-Proto/-For nur von einem Vorgaenger in
`trusted_proxies`. Hinter einem Edge sah deshalb jede App „http“, obwohl
der Besucher https benutzte -- gemessen am 07.11. auf oaap-test: das
MCP-Gateway nannte `http://mcpgw.vuebung.<knoten>/mcp` als Ressource.

1. hinter einem Edge: eine Datei mit genau der Adresse des Edge,
2. direkt oder ohne Namen: keine Datei (KOEDER: keine Liste, kein Bereich),
3. der Caddyfile bindet sie im globalen `servers`-Block ein,
4. ein Knoten, der schon vorher hinter einem Edge stand, bekommt sie beim
   Update einmal (ein Neuladen), danach nie wieder.

Aufruf: python3 test/test_edge_trust.py
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-edge-trust-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

reloads = []
m.reload_gateway = lambda: reloads.append(1)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
TRUST = os.path.join(m.CADDY_APPS_DIR, m.EDGE_TRUST_FILE)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def external(host, edge=""):
    conf = {"host": host}
    if edge:
        conf["edge"] = edge
    with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(conf, f)


def trust():
    return m._read_file(TRUST)


print("=== hinter einem Edge ===")
m.ensure_default_tenant()
external("knoten.example.org", "10.10.10.75")
m.write_external_caddy()
body = trust() or ""
ok("genau die Adresse des Edge wird geglaubt",
   re.search(r"^trusted_proxies static 10\.10\.10\.75$", body, re.M) is not None, body)
ok("KOEDER: kein Bereich, keine zweite Adresse",
   "private_ranges" not in body and "/" not in body.split("static", 1)[-1]
   and len(body.split("static", 1)[-1].split()) == 1, body)
ok("die Datei endet nicht auf .caddy (sonst laese Caddy sie als Site)",
   not m.EDGE_TRUST_FILE.endswith(".caddy"), m.EDGE_TRUST_FILE)
ok("ein zweites Schreiben aendert nichts", m.write_edge_trust() is False)

print("\n=== direkt und ohne Namen ===")
external("knoten.example.org")
m.write_external_caddy()
ok("direkt: keine Datei, niemand wird geglaubt", trust() is None)
external("knoten.example.org", "10.10.10.75")
m.write_external_caddy()
os.remove(m.EXTERNAL_FILE)
m.write_external_caddy()
ok("ohne Namen: keine Datei", trust() is None)

print("\n=== der Caddyfile bindet sie ein ===")
cf = open(os.path.join(HERE, "..", "platform", "Caddyfile"), encoding="utf-8").read()
glob_block = cf[cf.index("{"):cf.index("\n}\n")]
servers = glob_block[glob_block.index("servers {"):]
ok("im globalen servers-Block: import /etc/caddy/apps/*.servers",
   "import /etc/caddy/apps/*.servers" in servers, servers)

print("\n=== ein Knoten von vor 0.1.240 ===")
external("knoten.example.org", "10.10.10.75")
m.refresh_generated_sites()
os.remove(TRUST)
reloads.clear()
m.cmd_migrate_stream_close(None)
ok("das Update schreibt sie und laedt einmal neu",
   trust() is not None and len(reloads) == 1, reloads)
m.cmd_migrate_stream_close(None)
ok("zweiter Lauf tut nichts", len(reloads) == 1, reloads)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
