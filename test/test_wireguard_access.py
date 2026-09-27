#!/usr/bin/env python3
"""Die WireGuard-Mechanik (RFC-0044 §5, oaap.net.remote-access 0.3) --
gebaut, hier getestet, NICHT an einem echten Knoten gemessen.

Das ist keine Floskel: D2's Folge verlangt ausdrücklich, dass die
Firewall-Fessel (§5.1) an einem echten Knoten gemessen wird, BEVOR
irgendwo eine WireGuard-Datei an jemand anderen als den Betreiber an
der Kommandozeile geht. Was hier geprüft wird, ist deshalb die
TEXTFORM der Befehle -- die genaue Reihenfolge der `iptables`-Aufrufe,
gegen ein nachgebautes Programm, kein echter Kernel, kein echtes
Docker-Netz, kein echter Peer.

Festgehalten wird:

- ein 'wireguard'-Zugang braucht das Knotenprofil `remote-access`
  (D4) -- ohne, wird nichts geschrieben, kein Schlüssel erzeugt;
- die Gateway-Adresse dieses Netzes wird ZUERST ausgeschlossen, dann
  der Rest des Instanznetzes erlaubt, dann alles andere von diesem
  Peer verworfen -- und zwar EINGEFÜGT, nicht angehängt (Dockers
  eigene Vorgabe in DOCKER-USER ist ein abschließendes RETURN);
- schlägt die Firewall-Fessel fehl, wird der schon zugefügte Peer
  wieder entfernt -- nichts bleibt halb angewendet;
- zwei gleichzeitige Zugänge bekommen zwei verschiedene Tunnel-
  Adressen, und das Schließen des einen laesst den anderen unberuehrt;
- die `.conf`-Datei wird genau einmal zurückgegeben, nie in
  remote-access.json geschrieben (D10);
- das Entfernen des Profils wird abgelehnt, solange ein
  'wireguard'-Zugang offen ist (wie beim Profil `store`);
- die Portal-Worker-Aktion `access` mit `op=open, shape=wireguard`
  lehnt IMMER ab, ohne access_open() je zu erreichen (Spec §9).

Braucht kein echtes `wg`/`iptables`/`ip` -- alles gemockt.
Aufruf: python3 test/test_wireguard_access.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-wireguard-test-")
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


# --- ein nachgebautes wg/ip/iptables --------------------------------------
STATE = {"peers": {}, "keys": 0, "iptables": [], "fail_fence": False,
        "fail_link_add": False}
GW_IP = "172.30.0.1"
INSTANCE_SUBNET = m.ipaddress.ip_network("172.30.0.0/24")


def fake_run(cmd, **kw):
    """Steht fuer appctl.run() -- hebt bei Fehlschlag selbst eine
    subprocess.CalledProcessError, weil appctl.run() das ebenfalls tut."""
    if cmd[:2] == ["wg", "genkey"]:
        STATE["keys"] += 1
        return types.SimpleNamespace(stdout=f"priv{STATE['keys']}\n", stderr="", returncode=0)
    if cmd[:2] == ["wg", "pubkey"]:
        # Deliberately NOT "pub-<priv>": a real public key never lets
        # you recover the private one, and a test string that embeds
        # "priv2" as a substring would wrongly trip the D10 check below
        # ("no private key is ever persisted") on the PUBLIC key alone.
        priv = (kw.get("input") or "").strip()
        idx = priv.replace("priv", "")
        return types.SimpleNamespace(stdout=f"pubkey{idx}\n", stderr="", returncode=0)
    if cmd[:2] == ["wg", "set"] and "private-key" in cmd:
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)
    if cmd[:2] == ["wg", "set"] and "peer" in cmd and "allowed-ips" in cmd:
        pub = cmd[cmd.index("peer") + 1]
        ip_ = cmd[cmd.index("allowed-ips") + 1]
        STATE["peers"][pub] = ip_
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)
    if cmd[:3] == ["ip", "link", "set"]:
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)
    if cmd[0] == "iptables" and cmd[1] == "-I":
        if STATE["fail_fence"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="no such chain")
        STATE["iptables"].append(("-I", tuple(cmd[4:])))  # skip iptables -I DOCKER-USER 1
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)
    raise AssertionError(f"fake_run: unerwarteter Aufruf {cmd}")


def fake_subprocess_run(cmd, **kw):
    """Steht fuer subprocess.run() direkt -- appctl ruft es dort, wo
    ein Fehlschlag KEIN Fehler ist (wg show, ip link add, Entfernen)."""
    if cmd[:2] == ["wg", "show"]:
        up = m.WG_INTERFACE in STATE.get("up", set())
        return types.SimpleNamespace(returncode=0 if up else 1, stdout="", stderr="")
    if cmd[:3] == ["ip", "link", "add"]:
        if STATE["fail_link_add"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        STATE.setdefault("up", set()).add(m.WG_INTERFACE)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:3] == ["ip", "link", "delete"]:
        STATE.setdefault("up", set()).discard(m.WG_INTERFACE)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:2] == ["ip", "addr"]:
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:2] == ["wg", "set"] and "remove" in cmd:
        pub = cmd[cmd.index("peer") + 1]
        STATE["peers"].pop(pub, None)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[0] == "iptables" and cmd[1] == "-D":
        # "iptables -D DOCKER-USER -s ... -j DROP" -- NO extra "1"
        # positional the way -I has one, so the rule starts at cmd[3].
        rule = tuple(cmd[3:])
        STATE["iptables"] = [c for c in STATE["iptables"]
                             if not (c[0] == "-D" and c[1] == rule)]
        STATE["iptables"].append(("-D", rule))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:2] == ["docker", "inspect"]:
        # Housekeeping calls unrelated to WireGuard (state_view_write()
        # asking about the instance's own container) -- "not found" is
        # a harmless, honest answer for a container this test never ran.
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    raise AssertionError(f"fake_subprocess_run: unerwarteter Aufruf {cmd}")


m.run = fake_run
m.subprocess.run = fake_subprocess_run
m.shutil.which = lambda name: f"/usr/bin/{name}" if name in ("wg", "wg-quick") else None
m.docker_subnets = lambda net=None: [INSTANCE_SUBNET]
m.container_ip = lambda container, net: GW_IP
m.recreate_instance_containers = lambda name, *a, **kw: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
KUNDE, _ = m.tenant_create("kunde")


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant,
            "groups": [], "active": True}


write_users([user("root", ["server_admin"])])

reg = m.load_registry()
K_DB = "kunde-db"
reg["instances"][K_DB] = {
    "app_id": "db", "app_name": "DB", "channel": "test", "tenant": KUNDE,
    "id": "dddddddddddd", "version": "0.1.0", "name": "db",
    "container": "oaap-app-kunde-db", "image": "",
}
m.save_registry(reg)


def log(tid, action):
    return [e for e in m.read_tenant_log(tid) if e["action"] == action]


print("=== ohne Knotenprofil 'remote-access' geht nichts ===")
ok("das Profil ist noch nicht gesetzt", not m.has_profile("remote-access"))
try:
    m.access_open(K_DB, "wireguard", "karin", target={"endpoint": "node.example"},
                 who="root", role="server_admin")
    ok("Oeffnen ohne Profil wird abgelehnt", False)
except m.AccessRefused as e:
    ok("Oeffnen ohne Profil wird abgelehnt, mit Nennung von D4",
       "remote-access" in str(e), e)
ok("... nichts wurde erzeugt (kein Schluessel, kein Peer)",
   STATE["keys"] == 0 and STATE["peers"] == {})
ok("... und remote-access.json bleibt leer", m.load_access() == {})

print("")
print("=== Knotenprofil setzen (cmd_node) bringt 'wg0' hoch ===")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_node(types.SimpleNamespace(action="add-profile", profile="remote-access"))
out = buf.getvalue()
ok("das Profil ist jetzt gesetzt", m.has_profile("remote-access"))
ok("'wg0' ist hochgefahren", m.WG_INTERFACE in STATE.get("up", set()))
ok("... und der Ausdruck erinnert an die fehlende Messung (CLI ist Englisch, ADR-0003)",
   "not been measured" in out or "not measured" in out, out)
ok("der Knoten hat jetzt seinen eigenen Schluessel", os.path.exists(m.WG_SERVER_KEY_FILE))
if sys.platform != "win32":
    ok("... zu Recht 0600 -- Windows kennt diese Bits nicht, siehe test-Konvention",
       oct(os.stat(m.WG_SERVER_KEY_FILE).st_mode)[-3:] == "600")
SERVER_PUB = open(m.WG_SERVER_PUB_FILE, encoding="utf-8").read().strip()

print("")
print("=== oeffnen ohne --endpoint ===")
try:
    m.access_open(K_DB, "wireguard", "karin", who="root", role="server_admin")
    ok("ohne Ziel-Adresse wird abgelehnt", False)
except m.AccessRefused as e:
    ok("ohne Ziel-Adresse wird abgelehnt", "endpoint" in str(e), e)

print("")
print("=== oeffnen ===")
rec = m.access_open(K_DB, "wireguard", "karin", target={"endpoint": "node.example"},
                    who="root", role="server_admin")
ok("ein zweites Schluesselpaar wurde erzeugt (das erste ging an den Knoten selbst,"
   " beim Hochfahren von 'wg0')", STATE["keys"] == 2)
ok("... und der Peer steht in 'wg0', mit /32", rec["target"]["peer_pubkey"] in STATE["peers"]
   and STATE["peers"][rec["target"]["peer_pubkey"]].endswith("/32"))
ok("... die Tunnel-Adresse ist die niedrigste freie (nicht die Server-Adresse selbst)",
   rec["target"]["tunnel_ip"] == "10.200.0.2")
ok("... genau drei iptables-Regeln, in der Lese-Reihenfolge: Gateway-DROP, "
   "Subnetz-ACCEPT, Alles-DROP -- eingefuegt in umgekehrter Ausfuehrungsreihenfolge",
   [c[1] for c in STATE["iptables"]] == [
       ("-s", "10.200.0.2/32", "-j", "DROP"),
       ("-s", "10.200.0.2/32", "-d", str(INSTANCE_SUBNET), "-j", "ACCEPT"),
       ("-s", "10.200.0.2/32", "-d", f"{GW_IP}/32", "-j", "DROP"),
   ], STATE["iptables"])
ok(".conf wird genau EINMAL zurueckgegeben, mit dem PEERS eigenem privaten "
   "Schluessel (priv2 -- priv1 ging an den KNOTEN selbst) und dem Knoten "
   "als [Peer]",
   "conf" in rec and "PrivateKey = priv2" in rec["conf"]
   and f"PublicKey = {SERVER_PUB}" in rec["conf"], rec.get("conf"))
ok("... die Server-Endpunkt-Adresse und das Instanznetz stehen drin",
   "Endpoint = node.example:" in rec["conf"] and str(INSTANCE_SUBNET) in rec["conf"])
saved = m.load_access()[rec["id"]]
ok("... aber .conf UND der private Schluessel des PEERS stehen NIRGENDS in "
   "remote-access.json (D10)",
   "conf" not in saved and "priv2" not in json.dumps(saved), saved)
ok("... im Mandantenprotokoll steht access.opened, mit karin als Inhaberin",
   any(e["detail"].startswith("wireguard for karin") for e in log(KUNDE, "access.opened")))

print("")
print("=== zwei gleichzeitige Zugaenge, unabhaengig ===")
rec2 = m.access_open(K_DB, "wireguard", "uwe", target={"endpoint": "node.example"},
                     who="root", role="server_admin")
ok("der zweite Zugang bekommt eine ANDERE Tunnel-Adresse",
   rec2["target"]["tunnel_ip"] == "10.200.0.3"
   and rec2["target"]["tunnel_ip"] != rec["target"]["tunnel_ip"], rec2["target"])
ok("... und einen ANDEREN Peer-Schluessel", rec2["target"]["peer_pubkey"] != rec["target"]["peer_pubkey"])
before_close = dict(STATE["peers"])
m.access_close(rec["id"], who="root", role="server_admin")
ok("das Schliessen des einen entfernt genau seinen Peer",
   rec["target"]["peer_pubkey"] not in STATE["peers"]
   and rec2["target"]["peer_pubkey"] in STATE["peers"], STATE["peers"])
ok("... die -D-Aufrufe fuer den geschlossenen Peer sind da",
   any(c == ("-D", ("-s", f"{rec['target']['tunnel_ip']}/32", "-j", "DROP"))
       for c in STATE["iptables"]))
ok("... kein -D-Aufruf nennt die Adresse des ANDEREN, noch offenen Zugangs",
   not any(c[0] == "-D" and rec2["target"]["tunnel_ip"] in c[1][1] for c in STATE["iptables"]))
m.access_close(rec2["id"], who="root", role="server_admin")

print("")
print("=== schlaegt die Firewall-Fessel fehl, bleibt nichts halb angewendet ===")
STATE["fail_fence"] = True
before_peers = dict(STATE["peers"])
try:
    m.access_open(K_DB, "wireguard", "karin", target={"endpoint": "node.example"},
                 who="root", role="server_admin")
    ok("ein Fehlschlag der Fessel wird als Ablehnung gemeldet", False)
except m.AccessRefused as e:
    ok("ein Fehlschlag der Fessel wird als Ablehnung gemeldet", "firewall" in str(e), e)
ok("... der eben erst zugefuegte Peer wurde wieder entfernt",
   STATE["peers"] == before_peers, STATE["peers"])
ok("... und remote-access.json hat KEINEN neuen Eintrag",
   len(m.load_access()) == 0, m.load_access())
STATE["fail_fence"] = False

print("")
print("=== Portal-Worker: wireguard geht NIE durch, komplett andere Tuer ===")
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, instance, **req):
    _rid[0] += 1
    rid = f"r{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": instance, "action": "access", "by": by, **req}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


before_keys = STATE["keys"]
r = queue("root", K_DB, op="open", shape="wireguard", endpoint="node.example", holder="karin")
ok("das Portal-Worker lehnt 'wireguard' IMMER ab, mit einem Verweis auf die Kommandozeile",
   not r["ok"] and "portal" in r["message"] and "machine" in r["message"], r)
ok("... access_open() wurde dafuer gar nicht erst erreicht (kein neuer Schluessel)",
   STATE["keys"] == before_keys)
ok("... auch server_admin kommt hier nicht durch -- es ist keine Rollenfrage",
   "server_admin" not in r["message"])

print("")
print("=== Profil entfernen, waehrend ein Zugang offen ist ===")
rec3 = m.access_open(K_DB, "wireguard", "karin", target={"endpoint": "node.example"},
                     who="root", role="server_admin")
try:
    m.cmd_node(types.SimpleNamespace(action="remove-profile", profile="remote-access"))
    ok("Entfernen bei offenem Zugang wird abgelehnt", False)
except SystemExit:
    ok("Entfernen bei offenem Zugang wird abgelehnt (wie beim Profil 'store')", True)
ok("... 'wg0' blieb oben", m.WG_INTERFACE in STATE.get("up", set()))
m.access_close(rec3["id"], who="root", role="server_admin")
m.cmd_node(types.SimpleNamespace(action="remove-profile", profile="remote-access"))
ok("jetzt (kein Zugang mehr offen) laesst sich das Profil entfernen",
   not m.has_profile("remote-access"))
ok("... 'wg0' ist unten, aber der Schluessel des Knotens bleibt (fuer ein Wieder-Hinzufuegen)",
   m.WG_INTERFACE not in STATE.get("up", set()) and os.path.exists(m.WG_SERVER_KEY_FILE))

print("")
print("=== Werkzeug fehlt ===")
old_which = m.shutil.which
m.shutil.which = lambda name: None
try:
    m.wg_ensure_interface()
    ok("ohne 'wg'/'wg-quick' wird die Fessel abgelehnt", False)
except m.WireguardRefused as e:
    ok("ohne 'wg'/'wg-quick' wird die Fessel abgelehnt", "wireguard-tools" in str(e), e)
m.shutil.which = old_which

print("")
print("=== die zwei Profiltabellen kennen 'remote-access' ===")
PORTAL_APP = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
src = io.open(PORTAL_APP, encoding="utf-8").read()
ok("das Portal kennt 'remote-access' (sonst zeigt die Gesundheitsseite es nicht an)",
   '"remote-access"' in src)

print("")
print("PASS" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
