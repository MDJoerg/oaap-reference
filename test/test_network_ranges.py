"""oaap.apps.runtime 2.11 (0.2.73): jedes Netz der Plattform bekommt ein eigenes /26.

    python3 test/test_network_ranges.py

Anlass: oaapx01 am 2026-11-19 -- bei 33 Netzen scheiterte die naechste
Installation, weil Dockers Standardpools (~31 Netze) aufgebraucht waren.

Was hier gehalten wird:
- das erste freie /26 aus 172.16.0.0/16, an keinem Docker-Netz, keiner
  Adresse und keiner Route des Rechners;
- ein von Docker als ueberlappend abgelehnter Bereich wird uebersprungen;
- ist der Pool voll, entsteht das Netz ohne Bereich wie vor 0.2.73;
- ein anderer Fehler von Docker bleibt ein Fehler;
- alle drei Stellen, an denen Netze entstehen, gehen durch create_network;
- keine daemon.json (die Regel aus RFC-0038 D5 bleibt).
"""
import ipaddress
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-net-test-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import appctl as m                                            # noqa: E402

N = ipaddress.ip_network


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def fake(docker_nets, addr="", route="", refuse=(), other_error=None):
    """A subprocess.run that knows Docker networks, `ip` output and which
    ranges Docker refuses; records every `network create`."""
    made = []

    def run(argv, **kw):
        if argv[:3] == ["docker", "network", "ls"]:
            return R(0, "\n".join(f"id{i}" for i in range(len(docker_nets))))
        if argv[:3] == ["docker", "network", "inspect"]:
            return R(0, "\n".join(docker_nets))
        if argv[:2] == ["ip", "-4"] and "addr" in argv:
            return R(0, addr)
        if argv[:2] == ["ip", "-4"] and "route" in argv:
            return R(0, route)
        if argv[:3] == ["docker", "network", "create"]:
            made.append(list(argv))
            if other_error:
                return R(1, "", other_error)
            if "--subnet" in argv and argv[argv.index("--subnet") + 1] in refuse:
                return R(1, "", "Error response from daemon: Pool overlaps with "
                                "other one on this address space")
            return R(0, "netid")
        return R(1, "", "unexpected " + " ".join(argv))
    return run, made


real_run = subprocess.run


def with_fake(f, fn):
    subprocess.run = f
    try:
        return fn()
    finally:
        subprocess.run = real_run


# ------------------------------------------------------------- free_subnet
print("free_subnet")
ok("leerer Rechner: das erste /26", m.free_subnet([]) == "172.16.0.0/26")
ok("belegtes erstes /26 wird uebersprungen",
   m.free_subnet([N("172.16.0.0/26")]) == "172.16.0.64/26")
ok("ein Docker-/16 auf 172.16 sperrt den ganzen Pool",
   m.free_subnet([N("172.16.0.0/16")]) == "")
ok("eine Host-Adresse /32 sperrt nur ihr /26",
   m.free_subnet([N("172.16.0.5/32")]) == "172.16.0.64/26")
ok("IPv6 stoert nicht", m.free_subnet([N("fd00::/8")]) == "172.16.0.0/26")
ok("skip wird beachtet",
   m.free_subnet([], skip={"172.16.0.0/26"}) == "172.16.0.64/26")
ok("1024 Netze passen in den Pool",
   len(list(N(m.INSTANCE_NET_POOL).subnets(new_prefix=m.INSTANCE_NET_PREFIX))) == 1024)

# ------------------------------------------------------------- host_ranges
print("host_ranges")
ADDR = ("2: eth0    inet 10.10.10.95/24 brd 10.10.10.255 scope global eth0\n"
        "5: wg0    inet 172.16.0.10/32 scope global wg0\n")
ROUTE = ("default via 10.10.10.1 dev eth0\n"
         "10.10.10.0/24 dev eth0 proto kernel scope link src 10.10.10.95\n"
         "172.16.1.0/24 via 172.16.0.1 dev wg0\n"
         "local 10.10.10.95 dev eth0 table local proto kernel scope host\n"
         "broadcast 10.10.10.255 dev eth0 table local proto kernel scope link\n")
f, _ = fake([], ADDR, ROUTE)
hr = with_fake(f, m.host_ranges)
ok("Adresse des Rechners gelesen", N("10.10.10.0/24") in hr, hr)
ok("WireGuard-Adresse gelesen", N("172.16.0.10/32") in hr, hr)
ok("Route ueber VPN gelesen", N("172.16.1.0/24") in hr, hr)
ok("local/broadcast-Zeilen gelesen", N("10.10.10.95/32") in hr, hr)
ok("default wird nicht zum ganzen Adressraum",
   N("0.0.0.0/0") not in hr, hr)

# ------------------------------------------------------------- create_network
print("create_network")
f, made = fake(["172.17.0.0/16 ", "172.18.0.0/16 ", "172.16.0.0/26 "], ADDR, ROUTE)
sub = with_fake(f, lambda: m.create_network("oaap-inst-x"))
ok("das erste freie /26 neben Docker, WireGuard und VPN-Route",
   sub == "172.16.0.64/26", (sub, made))
ok("mit --subnet angelegt",
   made and made[-1] == ["docker", "network", "create", "--subnet",
                         "172.16.0.64/26", "oaap-inst-x"], made)

f, made = fake([], "", "", refuse={"172.16.0.0/26"})
sub = with_fake(f, lambda: m.create_network("oaap-inst-y"))
ok("ueberlappend abgelehnt -> naechster Bereich", sub == "172.16.0.64/26", made)
ok("zwei Versuche", len(made) == 2, made)

f, made = fake(["172.16.0.0/16 "])
sub = with_fake(f, lambda: m.create_network("oaap-inst-z"))
ok("Pool voll -> ohne Bereich wie frueher",
   sub == "" and made == [["docker", "network", "create", "oaap-inst-z"]], made)

f, made = fake([], other_error="permission denied")
try:
    with_fake(f, lambda: m.create_network("oaap-inst-e"))
    raised = False
except subprocess.CalledProcessError:
    raised = True
ok("ein anderer Fehler bleibt ein Fehler (kein stiller Rueckfall)",
   raised and len(made) == 1, made)

f, made = fake([])
with_fake(f, lambda: m.create_network("oaap-inst-c", ["--internal", "-o", "k=v"]))
ok("geschlossenes Netz behaelt --internal und die Option",
   made[-1][:6] == ["docker", "network", "create", "--internal", "-o", "k=v"]
   and "--subnet" in made[-1], made)

# ------------------------------------------------------------- source rules
print("Quelltext")
SRC = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("kein nacktes docker network create mehr ausser in create_network",
   SRC.count('"docker", "network", "create"') == 2,
   SRC.count('"docker", "network", "create"'))
ok("ensure_app_network, Verknuepfung und geschlossenes Netz gehen durch create_network",
   SRC.count("create_network(net") >= 3)
INSTALL = open(os.path.join(HERE, "..", "platform", "install.sh"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(HERE, "..", "platform", "install.sh")) else ""
ok("keine daemon.json (RFC-0038 D5 bleibt)",
   "daemon.json" not in INSTALL and "default-address-pools" not in SRC)

print("")
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
