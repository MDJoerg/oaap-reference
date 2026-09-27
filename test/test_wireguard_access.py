#!/usr/bin/env python3
"""Die WireGuard-Mechanik (RFC-0044 §5, oaap.net.remote-access 0.4) --
gebaut, hier getestet, NICHT als komplette Kette an einem echten Knoten
gemessen.

Das ist keine Floskel: die MECHANIK dieses Baus -- ein Peer wird ein
echtes Bridge-Mitglied im Netz seiner Instanz, statt nur geroutet zu
werden -- wurde an einem echten Knoten von Hand nachgebaut und
gemessen (oaap-test, 2026-09-27, spec §5.1a): ein einziges knotenweites
'wg0', nur per Routing erreicht, kommt an Dockers eigener
Anti-Spoofing-Regel (`table ip raw`, PREROUTING) nie vorbei -- diese
Regel gilt für JEDEN Container auf JEDEM Netz und greift, bevor
irgendeine DOCKER-USER-Regel überhaupt erreicht wird. Ein Peer, der als
echtes Bridge-Mitglied ankommt, besteht dieselbe Regel dagegen, weil
Dockers eigene Zwischen-Container-Trennung auf einer Bridge ohnehin
voraussetzt, dass FORWARD/DOCKER-USER gebrücktem Verkehr begegnet
(br_netfilter). Was hier geprüft wird, ist die TEXTFORM der Befehle,
die dieser Bau daraus macht -- die genaue Reihenfolge, gegen ein
nachgebautes Programm, kein echter Kernel, kein echtes Docker-Netz.

Festgehalten wird:

- ein 'wireguard'-Zugang braucht das Knotenprofil `remote-access`
  (D4) -- ohne, wird nichts geschrieben, kein Schlüssel erzeugt;
- das Setzen des Profils selbst startet NICHTS mehr (anders als noch
  in 0.3) -- die Instanz bekommt ihre EIGENE Apparatur erst beim
  ersten Peer, mit ihrem EIGENEN Schlüsselpaar, ihrem EIGENEN
  Namensraum, ihrem EIGENEN extern erreichbaren Port;
- ein zweiter Peer DERSELBEN Instanz teilt sich diese Apparatur
  (derselbe externe Port, kein zweiter Namensraum) -- eine ANDERE
  Instanz bekommt eine eigene, mit einem ANDEREN externen Port;
- die Gateway-Adresse dieses Netzes wird ZUERST ausgeschlossen, dann
  der Rest des Instanznetzes erlaubt, dann alles andere von diesem
  Peer verworfen -- und zwar EINGEFÜGT, nicht angehängt (Dockers
  eigene Vorgabe in DOCKER-USER ist ein abschließendes RETURN);
- schlägt die Firewall-Fessel fehl, wird der schon zugefügte Peer
  wieder entfernt -- und, war das der ERSTE Peer dieser Instanz, auch
  die gerade erst angelegte Apparatur wieder abgebaut;
- schließt der LETZTE Peer einer Instanz, geht die ganze Apparatur mit
  ihm -- schließt nicht der letzte, bleibt sie stehen;
- die `.conf`-Datei wird genau einmal zurückgegeben, nie in
  remote-access.json geschrieben (D10), mit dem Port DIESER Instanz;
- das Entfernen des Profils wird abgelehnt, solange ein
  'wireguard'-Zugang offen ist (wie beim Profil `store`);
- die Portal-Worker-Aktion `access` mit `op=open, shape=wireguard`
  lehnt IMMER ab, ohne access_open() je zu erreichen (Spec §9).

Braucht kein echtes `wg`/`ip`/`iptables` -- alles gemockt.
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


def r0(stdout=""):
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


# --- ein nachgebautes wg/ip/iptables --------------------------------------
STATE = {"keys": 0, "netns": set(), "netns_add_calls": 0, "wg0": set(),
        "peers": {}, "dnat": [], "forward": [], "routes": [],
        "iptables": [], "fail_fence": False, "fail_apparatus": False}
GW_IP = "172.30.0.1"
INSTANCE_SUBNET = m.ipaddress.ip_network("172.30.0.0/24")
GW_IP_2 = "172.31.0.1"
INSTANCE_SUBNET_2 = m.ipaddress.ip_network("172.31.0.0/24")


def _inner(cmd):
    """The command after an 'ip netns exec <ns> ...' prefix, and the ns
    it names -- both wg_instance_up()/wg_add_peer() go through this."""
    return cmd[3], cmd[4:]


def fake_run(cmd, **kw):
    """Steht fuer appctl.run() -- hebt bei Fehlschlag selbst eine
    subprocess.CalledProcessError, weil appctl.run() das ebenfalls tut."""
    if cmd[:2] == ["wg", "genkey"]:
        STATE["keys"] += 1
        return r0(f"priv{STATE['keys']}\n")
    if cmd[:2] == ["wg", "pubkey"]:
        # Deliberately NOT "pub-<priv>": a real public key never lets
        # you recover the private one, and a test string that embeds
        # a private key's own name as a substring would wrongly trip
        # the D10 check below on the PUBLIC key alone.
        priv = (kw.get("input") or "").strip()
        idx = priv.replace("priv", "")
        return r0(f"pubkey{idx}\n")
    if cmd[:3] == ["ip", "netns", "add"]:
        if STATE["fail_apparatus"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="no such thing")
        STATE["netns"].add(cmd[3])
        STATE["netns_add_calls"] += 1
        return r0()
    if cmd[:3] == ["ip", "link", "add"] and "veth" in cmd:
        return r0()
    if cmd[:3] == ["ip", "link", "set"]:
        return r0()  # netns-move, master-join, up -- none tracked, all succeed
    if cmd[:2] == ["ip", "addr"]:
        return r0()
    if cmd[:3] == ["ip", "route", "add"]:
        STATE["routes"].append(tuple(cmd[3:]))
        return r0()
    if cmd[:3] == ["ip", "netns", "exec"]:
        ns, inner = _inner(cmd)
        if inner[:4] == ["ip", "link", "add", "wg0"]:
            STATE["wg0"].add(ns)
            STATE["peers"].setdefault(ns, {})
            return r0()
        if inner[:2] == ["wg", "set"] and "private-key" in inner:
            return r0()
        if inner[:2] == ["wg", "set"] and "peer" in inner and "allowed-ips" in inner:
            pub = inner[inner.index("peer") + 1]
            ip_ = inner[inner.index("allowed-ips") + 1]
            STATE["peers"].setdefault(ns, {})[pub] = ip_
            return r0()
        if inner[:2] == ["ip", "addr"] or inner[:3] == ["ip", "link", "set"] \
                or inner[:3] == ["ip", "route", "add"]:
            return r0()
        raise AssertionError(f"fake_run (netns exec): unerwarteter Aufruf {inner}")
    if cmd[0] == "iptables" and cmd[1] == "-t" and cmd[3] == "-A":
        STATE["dnat"].append(tuple(cmd[4:]))
        return r0()
    if cmd[0] == "iptables" and cmd[1] == "-I" and cmd[2] == "FORWARD":
        STATE["forward"].append(tuple(cmd[4:]))
        return r0()
    if cmd[0] == "iptables" and cmd[1] == "-I" and cmd[2] == "DOCKER-USER":
        if STATE["fail_fence"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="no such chain")
        STATE["iptables"].append(("-I", tuple(cmd[4:])))  # skip -I DOCKER-USER 1
        return r0()
    raise AssertionError(f"fake_run: unerwarteter Aufruf {cmd}")


def fake_subprocess_run(cmd, **kw):
    """Steht fuer subprocess.run() direkt -- appctl ruft es dort, wo
    ein Fehlschlag KEIN Fehler ist (Existenzproben, Entfernen)."""
    if cmd[:3] == ["ip", "netns", "list"]:
        return r0("".join(f"{ns}\n" for ns in STATE["netns"]))
    if cmd[:3] == ["ip", "netns", "delete"]:
        STATE["netns"].discard(cmd[3])
        STATE["wg0"].discard(cmd[3])
        STATE["peers"].pop(cmd[3], None)
        return r0()
    if cmd[:3] == ["ip", "link", "delete"]:
        return r0()
    if cmd[:3] == ["ip", "route", "del"]:
        rule = tuple(cmd[3:])
        STATE["routes"] = [c for c in STATE["routes"] if c != rule]
        return r0()
    if cmd[:3] == ["ip", "netns", "exec"]:
        ns, inner = _inner(cmd)
        if inner[:2] == ["wg", "set"] and "remove" in inner:
            pub = inner[inner.index("peer") + 1]
            STATE["peers"].get(ns, {}).pop(pub, None)
            return r0()
        raise AssertionError(f"fake_subprocess_run (netns exec): {inner}")
    if cmd[0] == "iptables" and cmd[1] == "-t" and cmd[3] == "-D":
        rule = tuple(cmd[4:])
        STATE["dnat"] = [c for c in STATE["dnat"] if c != rule]
        return r0()
    if cmd[0] == "iptables" and cmd[1] == "-D" and cmd[2] == "FORWARD":
        rule = tuple(cmd[3:])
        STATE["forward"] = [c for c in STATE["forward"] if c != rule]
        return r0()
    if cmd[0] == "iptables" and cmd[1] == "-D" and cmd[2] == "DOCKER-USER":
        # "iptables -D DOCKER-USER -s ... -j DROP" -- NO extra "1"
        # positional the way -I has one, so the rule starts at cmd[3].
        rule = tuple(cmd[3:])
        STATE["iptables"] = [c for c in STATE["iptables"]
                             if not (c[0] == "-D" and c[1] == rule)]
        STATE["iptables"].append(("-D", rule))
        return r0()
    if cmd[:2] == ["docker", "inspect"]:
        # Housekeeping calls unrelated to WireGuard (state_view_write()
        # asking about the instance's own container) -- "not found" is
        # a harmless, honest answer for a container this test never ran.
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    if cmd[:2] == ["modprobe", "br_netfilter"]:
        STATE["br_netfilter_calls"] = STATE.get("br_netfilter_calls", 0) + 1
        return r0()
    if cmd[:2] == ["sysctl", "-w"]:
        if STATE.get("fail_bridge_netfilter"):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no such file")
        return r0()
    raise AssertionError(f"fake_subprocess_run: unerwarteter Aufruf {cmd}")


m.run = fake_run
m.subprocess.run = fake_subprocess_run
m.shutil.which = lambda name: f"/usr/bin/{name}" if name in ("wg", "wg-quick", "ip") else None
m.docker_subnets = lambda net=None: [INSTANCE_SUBNET_2 if "2" in net else INSTANCE_SUBNET]
m.container_ip = lambda container, net: GW_IP_2 if "2" in net else GW_IP
m.docker_network_used_ips = lambda net: set()
m._docker_bridge_name = lambda net: f"br-{net}"
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
K_APP2 = "kunde-app2"
for name in (K_DB, K_APP2):
    reg["instances"][name] = {
        "app_id": "db", "app_name": "DB", "channel": "test", "tenant": KUNDE,
        "id": "dddddddddddd", "version": "0.1.0", "name": name,
        "container": f"oaap-app-{name}", "image": "",
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
ok("... nichts wurde erzeugt (kein Schluessel, kein Namensraum)",
   STATE["keys"] == 0 and not STATE["netns"])
ok("... und remote-access.json bleibt leer", m.load_access() == {})

print("")
print("=== Knotenprofil setzen (cmd_node) startet NICHTS mehr ===")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_node(types.SimpleNamespace(action="add-profile", profile="remote-access"))
out = buf.getvalue()
ok("das Profil ist jetzt gesetzt", m.has_profile("remote-access"))
ok("... aber kein Namensraum und kein Schluessel entstanden dabei",
   not STATE["netns"] and STATE["keys"] == 0, out)
ok("... der Ausdruck nennt keine Messungs-Warnung mehr, sondern dass nichts "
   "startet (CLI ist Englisch, ADR-0003)",
   "Nothing starts" in out, out)

print("")
print("=== oeffnen ohne --endpoint ===")
try:
    m.access_open(K_DB, "wireguard", "karin", who="root", role="server_admin")
    ok("ohne Ziel-Adresse wird abgelehnt", False)
except m.AccessRefused as e:
    ok("ohne Ziel-Adresse wird abgelehnt", "endpoint" in str(e), e)

print("")
print("=== oeffnen (erster Peer dieser Instanz -- baut die Apparatur) ===")
rec = m.access_open(K_DB, "wireguard", "karin", target={"endpoint": "node.example"},
                    who="root", role="server_admin")
NS = m.wg_netns(K_DB)
ok("genau EIN Namensraum fuer diese Instanz entstand", STATE["netns"] == {NS})
ok("... mit ihrem EIGENEN wg0 darin", NS in STATE["wg0"])
ok("... und der Peer steht drin, mit /32", rec["target"]["peer_pubkey"] in STATE["peers"][NS]
   and STATE["peers"][NS][rec["target"]["peer_pubkey"]].endswith("/32"))
ok("... die Tunnel-Adresse ist die niedrigste freie (nicht die Namensraum-Adresse selbst)",
   rec["target"]["tunnel_ip"] == "10.200.0.2")
st = m.wg_instance_state(K_DB)
ok("die Instanz hat jetzt ihre eigene Zustandsdatei, mit externem Port ab 51820",
   st is not None and st["external_port"] == 51820, st)
ok("... und einen eigenen Schluessel, zu Recht nicht in remote-access.json",
   os.path.exists(os.path.join(m.WG_DATA_DIR, f"{K_DB}.key")))
if sys.platform != "win32":
    ok("... zu Recht 0600 -- Windows kennt diese Bits nicht, siehe test-Konvention",
       oct(os.stat(os.path.join(m.WG_DATA_DIR, f"{K_DB}.key")).st_mode)[-3:] == "600")
ok("... genau EIN DNAT-Eintrag fuer diesen externen Port, auf den Namensraum",
   len(STATE["dnat"]) == 1)
ok("... genau ZWEI FORWARD-Regeln -- hinein UND die Antwort zurueck (auf "
   "oaap-test 2026-09-27 gemessen: ohne die Rueckrichtung sieht der Server "
   "'empfangen', der Peer aber nie 'empfangen' zurueck)",
   len(STATE["forward"]) == 2, STATE["forward"])
ok("... genau drei DOCKER-USER-Regeln, in der Lese-Reihenfolge: Gateway-DROP, "
   "Subnetz-ACCEPT, Alles-DROP -- eingefuegt in umgekehrter Ausfuehrungsreihenfolge",
   [c[1] for c in STATE["iptables"]] == [
       ("-s", "10.200.0.2/32", "-j", "DROP"),
       ("-s", "10.200.0.2/32", "-d", str(INSTANCE_SUBNET), "-j", "ACCEPT"),
       ("-s", "10.200.0.2/32", "-d", f"{GW_IP}/32", "-j", "DROP"),
   ], STATE["iptables"])
ok(".conf wird genau EINMAL zurueckgegeben, mit dem PEERS eigenem privaten "
   "Schluessel und dem Namensraum-Schluessel als [Peer], auf dem EXTERNEN Port",
   "conf" in rec and "PrivateKey = " in rec["conf"]
   and f"PublicKey = {st['pubkey']}" in rec["conf"]
   and f"Endpoint = node.example:{st['external_port']}" in rec["conf"], rec.get("conf"))
ok("... das Instanznetz steht drin", str(INSTANCE_SUBNET) in rec["conf"])
saved = m.load_access()[rec["id"]]
ok("... aber .conf UND der private Schluessel des PEERS stehen NIRGENDS in "
   "remote-access.json (D10)",
   "conf" not in saved and "PrivateKey" not in json.dumps(saved), saved)
ok("... im Mandantenprotokoll steht access.opened, mit karin als Inhaberin",
   any(e["detail"].startswith("wireguard for karin") for e in log(KUNDE, "access.opened")))
ok("... und 'br_netfilter' wurde dabei sichergestellt (auf oaap-test 2026-09-27 "
   "gemessen: ohne das sieht DOCKER-USER gebrücktem Verkehr gar nicht zu)",
   STATE.get("br_netfilter_calls", 0) >= 1)

print("")
print("=== zweiter Peer DERSELBEN Instanz teilt sich die Apparatur ===")
before_ns_calls = STATE["netns_add_calls"]
before_dnat = len(STATE["dnat"])
rec2 = m.access_open(K_DB, "wireguard", "uwe", target={"endpoint": "node.example"},
                     who="root", role="server_admin")
ok("kein zweiter Namensraum, kein zweiter DNAT-Eintrag",
   STATE["netns_add_calls"] == before_ns_calls and len(STATE["dnat"]) == before_dnat)
ok("der zweite Zugang bekommt eine ANDERE Tunnel-Adresse",
   rec2["target"]["tunnel_ip"] == "10.200.0.3"
   and rec2["target"]["tunnel_ip"] != rec["target"]["tunnel_ip"], rec2["target"])
ok("... und einen ANDEREN Peer-Schluessel", rec2["target"]["peer_pubkey"] != rec["target"]["peer_pubkey"])
ok("... denselben externen Port wie der erste Peer (dasselbe .conf-Ziel)",
   f":{st['external_port']}" in rec2["conf"])

print("")
print("=== eine ANDERE Instanz bekommt ihre EIGENE Apparatur ===")
rec_b = m.access_open(K_APP2, "wireguard", "nora", target={"endpoint": "node.example"},
                      who="root", role="server_admin")
NS2 = m.wg_netns(K_APP2)
st_b = m.wg_instance_state(K_APP2)
ok("ein ZWEITER Namensraum entstand, verschieden vom ersten",
   NS2 in STATE["netns"] and NS2 != NS)
ok("... mit einem ANDEREN externen Port als die erste Instanz",
   st_b["external_port"] != st["external_port"], (st, st_b))
m.access_close(rec_b["id"], who="root", role="server_admin")
ok("... und beim Schliessen ihres einzigen Peers geht ihre Apparatur wieder "
   "vollstaendig ab -- die der ANDEREN Instanz bleibt unberuehrt",
   NS2 not in STATE["netns"] and NS in STATE["netns"]
   and m.wg_instance_state(K_APP2) is None and m.wg_instance_state(K_DB) is not None)

print("")
print("=== Schliessen: nur der letzte Peer nimmt die Apparatur mit ===")
before_close = {n: dict(p) for n, p in STATE["peers"].items()}
m.access_close(rec["id"], who="root", role="server_admin")
ok("das Schliessen des einen entfernt genau seinen Peer",
   rec["target"]["peer_pubkey"] not in STATE["peers"].get(NS, {})
   and rec2["target"]["peer_pubkey"] in STATE["peers"].get(NS, {}), STATE["peers"])
ok("... die Apparatur bleibt stehen -- uwe ist noch drin", NS in STATE["netns"])
ok("... die -D-Aufrufe fuer den geschlossenen Peer sind da",
   any(c == ("-D", ("-s", f"{rec['target']['tunnel_ip']}/32", "-j", "DROP"))
       for c in STATE["iptables"]))
ok("... kein -D-Aufruf nennt die Adresse des ANDEREN, noch offenen Zugangs",
   not any(c[0] == "-D" and rec2["target"]["tunnel_ip"] in c[1][1] for c in STATE["iptables"]))
m.access_close(rec2["id"], who="root", role="server_admin")
ok("jetzt (kein Peer dieser Instanz mehr offen) geht auch IHRE Apparatur ab",
   NS not in STATE["netns"] and m.wg_instance_state(K_DB) is None)
ok("... beide FORWARD-Regeln sind mit ihr weg", STATE["forward"] == [], STATE["forward"])

print("")
print("=== kann 'bridge-nf-call-iptables' nicht gesetzt werden, wird "
      "abgelehnt -- BEVOR irgendein Namensraum entsteht ===")
STATE["fail_bridge_netfilter"] = True
try:
    m.access_open(K_DB, "wireguard", "karin", target={"endpoint": "node.example"},
                 who="root", role="server_admin")
    ok("Ablehnung ohne diese Voraussetzung", False)
except m.WireguardRefused as e:
    ok("Ablehnung ohne diese Voraussetzung, mit Nennung der Fessel",
       "fence" in str(e) or "5.1" in str(e), e)
ok("... nichts wurde angelegt", m.wg_netns(K_DB) not in STATE["netns"]
   and m.wg_instance_state(K_DB) is None)
STATE["fail_bridge_netfilter"] = False

print("")
print("=== schlaegt die Firewall-Fessel beim ERSTEN Peer fehl, geht die "
      "gerade erst angelegte Apparatur wieder ab ===")
STATE["fail_fence"] = True
try:
    m.access_open(K_DB, "wireguard", "karin", target={"endpoint": "node.example"},
                 who="root", role="server_admin")
    ok("ein Fehlschlag der Fessel wird als Ablehnung gemeldet", False)
except m.AccessRefused as e:
    ok("ein Fehlschlag der Fessel wird als Ablehnung gemeldet", "firewall" in str(e), e)
ok("... die eben erst angelegte Apparatur ist wieder weg (kein Peer war schon offen)",
   m.wg_netns(K_DB) not in STATE["netns"] and m.wg_instance_state(K_DB) is None)
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
ok("... die Apparatur blieb oben", m.wg_netns(K_DB) in STATE["netns"])
m.access_close(rec3["id"], who="root", role="server_admin")
m.cmd_node(types.SimpleNamespace(action="remove-profile", profile="remote-access"))
ok("jetzt (kein Zugang mehr offen) laesst sich das Profil entfernen",
   not m.has_profile("remote-access"))

print("")
print("=== Werkzeug fehlt ===")
old_which = m.shutil.which
m.shutil.which = lambda name: None
try:
    m.wg_instance_up(K_DB, m.app_network(K_DB), GW_IP)
    ok("ohne 'wg'/'wg-quick'/'ip' wird die Apparatur abgelehnt", False)
except m.WireguardRefused as e:
    ok("ohne 'wg'/'wg-quick'/'ip' wird die Apparatur abgelehnt", "wireguard-tools" in str(e), e)
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
