#!/usr/bin/env python3
"""Geschlossener Abfluss (RFC-0068 Stufe 1).

Gemessen wird die Eigenschaft gegen ein nachgebautes Docker, das Netze
wirklich fuehrt: ein Flag, das nur in unserem Datensatz stuende, faellt
hier durch.

  * ein offener Mandant bleibt, wie er ist: normales Netz, keine Regel,
    und die Sweep fragt Docker gar nichts;
  * geschlossen heisst intern UND mit Bruecken-Praefix, von der ersten
    Instanz an, auch fuer Link-Netze;
  * die Instanz startet nicht auf dem falschen Netz und nicht ohne
    Knotenregel, und die Absage kommt VOR dem ersten `docker rm`;
  * Endpunkt und Tunnel werden abgewiesen, Ausnahme mit Begruendung,
    Generalprobe immer geschlossen, kein Weg ueber das Manifest;
  * oeffnen und ausnehmen nur der Server-Admin, schliessen auch der
    Mandanten-Admin, alles im Mandanten-Log;
  * Stufe 2: hinaus nur ueber eine erlaubte http-Destination, gefragt beim
    Binden, am Listener und bei der Uebergabe, und schliessen nur, wenn
    jede Bindung geklaert ist.

Braucht nur Python 3; Docker und iptables werden nachgebaut.
"""
import argparse
import contextlib
import io
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
DATA = tempfile.mkdtemp(prefix="oaap-egress-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)

import appctl as a                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


# ------------------------------------------------------- nachgebautes Docker
NETS = {}        # name -> {"internal": bool, "bridge": str, "members": set}
CONTAINERS = {}  # name -> {"running": bool, "nets": set}
RULES = set()    # (tool, "INPUT", interface)
LOG = []
RULE_FAILS = {"on": False}


def result(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def fake(cmd, *x, **kw):
    cmd = list(cmd)
    LOG.append(cmd)
    if cmd[0] in ("iptables", "ip6tables"):
        iface = cmd[cmd.index("-i") + 1]
        key = (cmd[0], iface)
        if "-C" in cmd:
            return result(0 if key in RULES else 1)
        if "-I" in cmd:
            if RULE_FAILS["on"]:
                return result(1)
            RULES.add(key)
        return result()
    if cmd[0] != "docker":
        return result()
    sub = cmd[1:]
    if sub[:2] == ["network", "create"]:
        name = sub[-1]
        bridge = ""
        if "-o" in sub:
            bridge = sub[sub.index("-o") + 1].split("=", 1)[1]
        NETS[name] = {"internal": "--internal" in sub, "bridge": bridge, "members": set()}
        return result()
    if sub[:2] == ["network", "inspect"]:
        name = sub[-1]
        if name not in NETS:
            return result(1)
        if "-f" not in sub:
            return result(0, "{}")
        fmt = sub[sub.index("-f") + 1]
        if ".Options" in fmt and ".Internal" in fmt:
            return result(0, f"{str(NETS[name]['internal']).lower()} {NETS[name]['bridge']}\n")
        if ".Internal" in fmt:
            return result(0, str(NETS[name]["internal"]).lower() + "\n")
        if ".Containers" in fmt:
            return result(0, " ".join(sorted(NETS[name]["members"])))
        return result(0, "")
    if sub[:2] == ["network", "connect"]:
        if "--help" in sub:
            return result(0, "")
        net, c = sub[-2], sub[-1]
        if net not in NETS:
            return result(1)
        NETS[net]["members"].add(c)
        CONTAINERS.setdefault(c, {"running": True, "nets": set()})["nets"].add(net)
        return result()
    if sub[:2] == ["network", "disconnect"]:
        net, c = sub[-2], sub[-1]
        if net in NETS:
            NETS[net]["members"].discard(c)
        if c in CONTAINERS:
            CONTAINERS[c]["nets"].discard(net)
        return result()
    if sub[:2] == ["network", "rm"]:
        NETS.pop(sub[-1], None)
        return result()
    if sub[0] == "rm":
        c = sub[-1]
        for n in CONTAINERS.get(c, {}).get("nets", ()):
            NETS[n]["members"].discard(c) if n in NETS else None
        CONTAINERS.pop(c, None)
        return result()
    if sub[0] in ("run", "create"):
        name = sub[sub.index("--name") + 1]
        net = sub[sub.index("--network") + 1]
        if net not in NETS:
            return result(1, "", "no such network")
        CONTAINERS[name] = {"running": sub[0] == "run", "nets": {net}, "cmd": cmd}
        NETS[net]["members"].add(name)
        return result()
    if sub[0] == "stop":
        for c in sub[3:]:
            if c in CONTAINERS:
                CONTAINERS[c]["running"] = False
        return result()
    if sub[0] == "inspect":
        c = sub[-1]
        fmt = sub[sub.index("-f") + 1] if "-f" in sub else ""
        if c not in CONTAINERS:
            return result(1)
        if "State.Running" in fmt:
            return result(0, str(CONTAINERS[c]["running"]).lower() + "\n")
        if "NetworkSettings.Networks" in fmt:
            return result(0, " ".join(sorted(CONTAINERS[c]["nets"])) + " ")
        return result(0, "")
    return result()


def fake_run(cmd, *x, **kw):
    r = fake(cmd)
    if r.returncode:
        raise a.subprocess.CalledProcessError(r.returncode, cmd, stderr=r.stderr)
    return r


a.run = fake_run
a.subprocess.run = fake
a.image_uid = lambda image: None
a.state_view_write = lambda *x, **k: None
a.reload_gateway = lambda: None
a.refresh_destinations = lambda *x, **k: None
REAL_WRITE = a.write_destinations_caddy
a.write_destinations_caddy = lambda *x, **k: False
a.container_env = lambda container: None
a._egress_rule_tools = lambda: ["iptables", "ip6tables"]
os.makedirs(a.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(a.APPS_DIR, exist_ok=True)
a.ensure_default_tenant()


def tenant(label):
    tid, _t = a.tenant_create(label)
    return tid


T = tenant("zu")
T2 = tenant("auf")


def instance(key, tid, **extra):
    """Eine Instanz im Register, ihr Container laeuft auf ihrem Netz."""
    reg = a.load_registry()
    rec = {"tenant": tid, "id": key + "-id", "container": f"oaap-app-{key}",
           "image": "img:1", "port": 9000, "routes": [],
           "services": [{"service": "", "container": f"oaap-app-{key}",
                         "image": "img:1", "port": 80}]}
    rec.update(extra)
    reg["instances"][key] = rec
    a.save_registry(reg)
    a.recreate_instance_containers(key, a.instance_services(rec), [],
                                   rec.get("endpoints") or [], inst=rec)
    return rec


def rec(key):
    return a.load_registry()["instances"][key]


def net_of(key):
    return NETS.get(a.app_network(key))


def closed_shape(n):
    return bool(n and n["internal"] and n["bridge"].startswith("oaapc"))


def log_actions(tid):
    return [e.get("action") for e in a.read_tenant_log(tid, limit=100)]


def refused(fn, *x, **k):
    try:
        fn(*x, **k)
    except a.EgressRefused as e:
        return str(e)
    return ""


def cli(*argv):
    """`oaap tenant egress ...` durch den echten Parser-Pfad der Funktion."""
    ns = types.SimpleNamespace(
        name=argv[0], egress_close="--close" in argv, egress_open="--open" in argv,
        except_instance=_val(argv, "--except-instance"),
        allow_destination=_val(argv, "--allow-destination"),
        disallow_destination=_val(argv, "--disallow-destination"),
        end_exception=_val(argv, "--end-exception"),
        idp_reason=_val(argv, "--reason"), yes="--yes" in argv)
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            a._tenant_egress_cli(ns)
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue()


def _val(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


# --------------------------------------------------------------- offen
print("=== ein offener Mandant bleibt, wie er war ===")
instance("offen1", T2)
ok("normales Netz, ohne --internal", net_of("offen1") == {
    "internal": False, "bridge": "", "members": net_of("offen1")["members"]})
ok("keine Knotenregel angefasst", not any(c[0] in ("iptables", "ip6tables") for c in LOG))
LOG.clear()
a.egress_sweep()
ok("die Sweep fragt ohne geschlossenen Mandanten Docker nichts", LOG == [], LOG)
ok("`oaap status` sagt auf einem offenen Knoten nichts", a.egress_faults_line() == "")

# ------------------------------------------------------------- schliessen
print("=== schliessen: fragt, dann intern plus Regel ===")
instance("kp", T)
before = dict(net_of("kp"))
code, out = cli("zu", "--close")
ok("ohne --yes nennt es die Instanz und aendert nichts",
   code == 0 and "kp" in out and "Nothing changed" in out
   and not a.tenant_egress_closed(T) and net_of("kp")["internal"] is False, out)
code, out = cli("zu", "--close", "--yes")
ok("mit --yes: Netz ist intern und traegt das Praefix", closed_shape(net_of("kp")), net_of("kp"))
ok("die Bruecke passt in 15 Zeichen", len(net_of("kp")["bridge"]) <= 15, net_of("kp"))
ok("der Container laeuft wieder, auf diesem Netz",
   CONTAINERS["oaap-app-kp"]["running"]
   and CONTAINERS["oaap-app-kp"]["nets"] == {a.app_network("kp")}, CONTAINERS["oaap-app-kp"])
ok("die eine Regel steht, v4 und v6",
   ("iptables", "oaapc+") in RULES and ("ip6tables", "oaapc+") in RULES, RULES)
ok("im Mandanten-Log", "egress.close" in log_actions(T), log_actions(T))
ok("das Gateway ist wieder im Netz", a.GATEWAY_CONTAINER in net_of("kp")["members"], net_of("kp"))
ok("der andere Mandant bleibt offen", not net_of("offen1")["internal"])
LOG.clear()
code, out = cli("zu", "--close", "--yes")
ok("ein zweites Schliessen bewegt nichts",
   "nothing had to move" in out and not any(c[:2] == ["docker", "rm"] for c in LOG), out)

print("=== eine neue Instanz ist vom ersten Start an geschlossen ===")
instance("neu", T)
ok("eigenes Netz intern", closed_shape(net_of("neu")), net_of("neu"))

# --------------------------------------------------------- falsches Netz
print("=== falsches Netz: startet nicht, Absage vor dem ersten rm ===")
NETS[a.app_network("neu")]["internal"] = False     # Docker sagt es, nicht wir
LOG.clear()
why = refused(a.recreate_instance_containers, "neu", a.instance_services(rec("neu")), [])
ok("abgewiesen, nennt das Netz", "not internal" in why, why)
ok("kein docker rm", not any(c[:2] == ["docker", "rm"] for c in LOG), LOG)
ok("der Datensatz sagt weiter geschlossen, Docker entscheidet",
   a.instance_egress_closed(rec("neu")))
ok("die Status-Zeile meldet den Fehler", "FAULT" in a.egress_faults_line(), a.egress_faults_line())
stopped = a.egress_sweep()
ok("die Sweep stoppt genau sie", stopped == ["neu"] and not CONTAINERS["oaap-app-neu"]["running"]
   and CONTAINERS["oaap-app-kp"]["running"], stopped)
ok("und schreibt es ins Log", "egress.fault-stopped" in log_actions(T), log_actions(T))
LOG.clear()
ok("ein zweiter Lauf stoppt nichts mehr", a.egress_sweep() == [])
code, out = cli("zu", "--close", "--yes")
ok("erneutes --close heilt: intern, laeuft",
   closed_shape(net_of("neu")) and CONTAINERS["oaap-app-neu"]["running"] and "neu" in out, out)

print("=== ein normales zweites Netz wird gesehen ===")
NETS["koeder"] = {"internal": False, "bridge": "", "members": set()}
fake(["docker", "network", "connect", "koeder", "oaap-app-kp"])
ok("als Fehler der Instanz", any("koeder" in f for f in a.egress_instance_faults("kp", rec("kp"))))
ok("die Sweep stoppt sie", "kp" in a.egress_sweep() and not CONTAINERS["oaap-app-kp"]["running"])
fake(["docker", "network", "disconnect", "koeder", "oaap-app-kp"])
NETS.pop("koeder")
cli("zu", "--close", "--yes")
ok("ein gestoppter Container kommt mit --close zurueck", CONTAINERS["oaap-app-kp"]["running"])

# --------------------------------------------------------- Knotenregel
print("=== Status und Seite setzen die Regel nicht ===")
RULES.clear()
a.os.geteuid = lambda: 0
line = a.egress_faults_line()
ok("die Status-Zeile meldet sie und setzt sie nicht", "node rule" in line and not RULES, line)
ok("die Seite auch", "the node rule is missing" in cli("zu")[1] and not RULES)
a.os.geteuid = lambda: 1000
line = a.egress_faults_line()
ok("ohne Root kein falscher Alarm, und sie sagt es",
   "FAULT" not in line and "sudo" in line and not RULES, line)
del a.os.geteuid  # zurueck auf das echte
print("=== eine verschwundene Regel: die Sweep setzt sie und sagt es ===")
ok("sie stoppt niemanden, wenn es gelingt", a.egress_sweep() == [] and ("iptables", "oaapc+") in RULES)
ok("und schreibt es ins Log", "egress.rule-missing" in log_actions(T), log_actions(T))
n = log_actions(T).count("egress.rule-missing")
a.egress_sweep()
ok("ein zweiter Lauf schreibt nichts", log_actions(T).count("egress.rule-missing") == n)

print("=== ohne Knotenregel kein Start ===")
RULES.clear()
RULE_FAILS["on"] = True
LOG.clear()
why = refused(a.recreate_instance_containers, "kp", a.instance_services(rec("kp")), [])
ok("abgewiesen vor dem ersten rm", "node rule" in why
   and not any(c[:2] == ["docker", "rm"] for c in LOG), why)
ok("kein Schliessen ohne Regel", "node rule" in refused(a.egress_close, T), "")
stopped = a.egress_sweep()
ok("eine verschwundene Regel stoppt laufende Instanzen", "kp" in stopped, stopped)
RULE_FAILS["on"] = False
ok("die Regel kommt zurueck, sobald sie gesetzt werden kann",
   a.egress_node_rule_ensure() and ("iptables", "oaapc+") in RULES)
cli("zu", "--close", "--yes")
ok("und --close startet wieder", CONTAINERS["oaap-app-kp"]["running"])

# --------------------------------------------------------------- Links
print("=== Link-Netze ===")
reg = a.load_registry()
a.setup_link_network(reg, "kp", "neu")
ln = NETS[a.link_network("kp", "neu")]
ok("zwischen Geschlossenen intern mit Praefix", closed_shape(ln), ln)
reg = a.load_registry()
a.setup_link_network(reg, "kp", "offen1")
ln = NETS[a.link_network("kp", "offen1")]
ok("eine geschlossene Seite reicht", closed_shape(ln), ln)
instance("offen2", T2)
reg = a.load_registry()
a.setup_link_network(reg, "offen1", "offen2")
ok("zwischen Offenen bleibt es normal", not NETS[a.link_network("offen1", "offen2")]["internal"])
wrong = a.link_network("kp", "offen2")
NETS[wrong] = {"internal": False, "bridge": "", "members": {"oaap-app-offen2"}}
CONTAINERS["oaap-app-offen2"]["nets"].add(wrong)
reg = a.load_registry()
a.setup_link_network(reg, "kp", "offen2")
ok("ein altes normales Link-Netz wird ersetzt, beide Enden wieder dran",
   closed_shape(NETS[wrong]) and {"oaap-app-kp", "oaap-app-offen2"} <= NETS[wrong]["members"], NETS[wrong])

# ---------------------------------------------------- Endpunkt, Tunnel
print("=== Endpunkt und Tunnel ===")
EP = [{"name": "rtp", "protocol": "udp", "container_port": 5, "host_port": 8201}]
r = a.load_registry()
r["instances"]["kp"]["declared_endpoints"] = [{"name": "rtp", "protocol": "udp", "container_port": 5}]
a.save_registry(r)
out = io.StringIO()
code = 0
with contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
    try:
        a.cmd_endpoint(types.SimpleNamespace(name="kp", action="allow", endpoint="rtp"))
    except SystemExit as e:
        code = e.code
ok("Endpunkt-Freigabe abgewiesen", code and "closed" in out.getvalue(), out.getvalue())
LOG.clear()
why = refused(a.recreate_instance_containers, "kp", a.instance_services(rec("kp")), [], EP)
ok("ein Endpunkt, wie auch immer er kommt: kein Start, kein rm",
   "endpoint" in why and not any(c[:2] == ["docker", "rm"] for c in LOG), why)
tun_svc = [dict(a.instance_services(rec("kp"))[0], tun=True)]
why = refused(a.recreate_instance_containers, "kp", tun_svc, [])
ok("ein Tunnel-Dienst: kein Start", "tunnel" in why, why)
m = {"app": {"id": "vpn"}, "services": {"vpn": {"tun": True}, "web": {}}}
ok("die Installation sagt es, bevor sie baut",
   "tunnel" in a.egress_install_refusal(m, T, None, None), "")
ok("in einem offenen Mandanten nicht", a.egress_install_refusal(m, T2, None, None) == "")
instance("tunnel", T2, services=[{"service": "", "container": "oaap-app-tunnel", "image": "i",
                                  "port": 80, "tun": True}])
ok("Tunnel-Instanz in offenem Mandant hat normales Netz", not net_of("tunnel")["internal"])
instance("portvoll", T2, endpoints=EP)
blockers = refused(a.egress_close, T2)
ok("Schliessen scheitert an beiden, nennt beide, schliesst nichts",
   "tunnel" in blockers and "portvoll" in blockers and not a.tenant_egress_closed(T2), blockers)

# ------------------------------------------------------------ Ausnahme
print("=== Ausnahme mit Begruendung ===")
code, out = cli("zu", "--except-instance", "neu", "--yes")
ok("ohne Begruendung abgewiesen", code and "why" in out and not a.egress_exception(rec("neu")), out)
code, out = cli("auf", "--except-instance", "neu", "--reason", "x", "--yes")
ok("eine Instanz eines anderen Mandanten nicht", code and "no instance" in out, out)
code, out = cli("zu", "--except-instance", "neu", "--reason", "Kundenauftrag Cloud-Assistent", "--yes")
ok("mit Begruendung: normales Netz", code == 0 and net_of("neu")["internal"] is False, out)
ok("im Log, mit der Begruendung", any(e.get("action") == "egress.exception"
                                       and "Cloud-Assistent" in (e.get("detail") or "")
                                       for e in a.read_tenant_log(T, limit=100)))
ok("nicht mehr geschlossen, der Rest schon",
   not a.instance_egress_closed(rec("neu")) and a.instance_egress_closed(rec("kp")))
hand = dict(rec("kp"), egress_exception=True)
ok("ein handbearbeitetes `true` ist keine Ausnahme", a.instance_egress_closed(hand))
hand = dict(rec("kp"), egress_exception={"reason": ""})
ok("eine leere Begruendung auch nicht", a.instance_egress_closed(hand))
ok("eine Generalprobe erbt die Ausnahme nicht",
   a.instance_egress_closed(dict(rec("neu"), rehearsal={"of": "neu"})))
instance("probe", T, rehearsal={"of": "neu"})
ok("sie sitzt von Anfang an auf einem internen Netz", closed_shape(net_of("probe")), net_of("probe"))
why = refused(a.egress_set_exception, "probe", "weil")
ok("und bekommt auch keine eigene", "rehearsal" in why, why)
a.egress_set_exception("neu", "")
ok("Ausnahme beendet: wieder intern", closed_shape(net_of("neu")) and not a.egress_exception(rec("neu")))
ok("Ende im Log", "egress.exception-ended" in log_actions(T))

# ----------------------------------------------- die echte Installation
print("=== die echte Installation, durch ihre Tuer ===")


def write_pkg(version="0.1.0", extra=()):
    lines = ['oaap_manifest: "0.8"', "app:", "  id: egress-app", "  name: E",
             f"  version: {version}", "  type: image", "services:", "  web:",
             "    image: x/e:1", "    port: 8080", *extra,
             "routes:", "  - path: /", "    roles: [user]", "health:", "  path: /", ""]
    pkg = tempfile.mkdtemp(prefix="oaap-egress-pkg-")
    with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
        f.write(chr(10).join(lines))
    return pkg


def install(pkg, name, tenant_label="zu"):
    args = argparse.Namespace(package=pkg, path="", ref="", name=name, channel="test",
                              store_source="", tenant=tenant_label, key="", ident=None,
                              rehearsal=None, bind=[], confirm_tun=True)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            a._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
    except SystemExit:
        return False, buf.getvalue()
    return True, buf.getvalue()


LOG.clear()
acc, out = install(write_pkg(extra=["    tun: true"]), "tun1")
ok("ein Tunnel wird in einen geschlossenen Mandanten nicht installiert, auch bestaetigt",
   not acc and "closed" in out, out)
ok("... und nichts wurde gebaut oder gezogen",
   not any(c[:2] in (["docker", "build"], ["docker", "pull"]) for c in LOG), LOG)
acc, out = install(write_pkg(), "pk")
key = next((k for k, i in a.load_registry()["instances"].items() if i.get("app_id") == "egress-app"), "")
ok("eine Installation landet auf dem internen Netz", acc and closed_shape(net_of(key)), out)
cont = rec(key)["services"][0]["container"]
ok("sie laeuft", CONTAINERS[cont]["running"], CONTAINERS.get(cont))
a.egress_set_exception(key, "Probe Redeploy")
acc, out = install(write_pkg("0.1.1"), key.split("-", 1)[-1])
ok("ein Redeploy behaelt die Ausnahme und das normale Netz",
   acc and a.egress_exception(rec(key)) and rec(key)["version"] == "0.1.1"
   and net_of(key)["internal"] is False, out)
a.egress_set_exception(key, "")
acc, out = install(write_pkg("0.1.2", extra=["    egress_exception:", "      reason: die App selbst"]),
                   key.split("-", 1)[-1])
ok("ein Manifest kann sich nicht ausnehmen",
   not a.egress_exception(rec(key)) and closed_shape(net_of(key)), out)

print("=== das Update repariert nichts ins Offene ===")
NETS[a.app_network(key)]["internal"] = False
LOG.clear()
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
    a.cmd_migrate_networks(None)
ok("es warnt und laesst die Instanz, wie sie ist", key in out.getvalue() and "not internal" in out.getvalue()
   and not any(c[:2] == ["docker", "rm"] and cont in c for c in LOG), out.getvalue())
cli("zu", "--close", "--yes")
ok("--close holt sie zurueck", closed_shape(net_of(key)))

# ------------------------------------------------------------- Rollen
print("=== Rollen ===")
ok("Mandanten-Admin darf nicht oeffnen", "server admin" in refused(a.egress_open, T, "weil", role="tenant_admin"))
ok("und nicht ausnehmen", "server admin" in refused(a.egress_set_exception, "kp", "weil", role="tenant_admin"))
ok("ein Benutzer darf nicht schliessen", refused(a.egress_close, T, role="user") != "")
ok("Mandanten-Admin darf schliessen", refused(a.egress_close, T, role="tenant_admin") == "")
ok("Oeffnen ohne Begruendung nicht", "why" in refused(a.egress_open, T, " "))

print("=== Ein offener Zugang blockiert den Umzug ===")
real_access = a.access_list
a.access_list = lambda instance=None, tenant=None: [{"instance": instance}] if instance == "kp" else []
why = refused(a.egress_open, T, "Versuch")
ok("Oeffnen wird abgewiesen, nichts geaendert", "remote access" in why and a.tenant_egress_closed(T), why)
a.access_list = real_access

# ---------------------------------------------------------------- wake
print("=== Aufwachen ===")
r = a.load_registry()
r["instances"]["kp"]["state"] = "standby"
a.save_registry(r)
NETS[a.app_network("kp")]["internal"] = False
why = refused(a.wake_instance, "kp", rec("kp"), wait=0)
ok("wecken auf falschem Netz abgewiesen, schlaeft weiter",
   "not internal" in why and rec("kp").get("state") == "standby", why)
NETS[a.app_network("kp")]["internal"] = True
r = a.load_registry()
r["instances"]["kp"].pop("state")
a.save_registry(r)

# --------------------------------------------------------------- Seite
print("=== die Seite ===")
r = a.load_registry()
r["instances"]["kp"]["routes"] = [{"path": "/pub", "roles": ["public"]}]
r["instances"]["kp"]["destinations"] = {"ki": "ollama-lan"}
r["instances"]["kp"]["links"] = ["offen1"]
a.save_registry(r)
code, page = cli("zu")
ok("nennt den Zustand", "CLOSED" in page, page)
for needle in ("1. Instances with an exception", "2. Allowed destination",
               "3. Links", "offen1", "4. Public routes", "/pub", "5. Remote access",
               "6. AI", "7. Backups", "8. Faults", "does not do"):
    ok(f"... enthaelt '{needle}'", needle in page, page)
ok("der offene Mandant sagt, was er kann",
   "open" in cli("auf")[1] and "whatever this node can" in cli("auf")[1])

print("=== oeffnen ===")
code, out = cli("zu", "--open", "--yes")
ok("ohne Begruendung abgewiesen", code and a.tenant_egress_closed(T), out)
code, out = cli("zu", "--open", "--reason", "Probe beendet", "--yes")
ok("mit Begruendung: normale Netze", code == 0 and net_of("kp")["internal"] is False, out)
ok("Generalprobe bleibt im offenen Mandanten normal", net_of("probe")["internal"] is False)
ok("im Log", "egress.open" in log_actions(T))
ok("und die Seite nennt den Grund", "Probe beendet" in cli("zu")[1])

# ---------------------------------------------- Stufe 2: die Destination
print("=== Destinations: der eine Weg hinaus (Stufe 2) ===")
REAL_READ = a.egress_read_target
PLAIN = {"at": "t", "gateway": False, "answer": "no OAAP AI gateway (HTTP 404)"}
a.egress_read_target = lambda tid, name, d: dict(PLAIN)
T3 = tenant("dz")
instance("d1", T3)
cli("dz", "--close", "--yes")
ok("Ausgangslage: geschlossen", a.instance_egress_closed(rec("d1")))


def bind(key, name, need=""):
    reg = a.load_registry()
    try:
        return a.destination_bind(reg, key, name, need=need), ""
    except a.DestinationRefused as e:
        return None, str(e)


def listener():
    a.write_destinations_caddy = REAL_WRITE
    a.docker_subnets = lambda net=None: ([a.ipaddress_for_test] if net else [])
    a.write_destinations_caddy()
    a.write_destinations_caddy = lambda *x, **k: False
    with open(os.path.join(a.CADDY_APPS_DIR, a.DEST_SITE), encoding="utf-8") as f:
        return f.read()


a.destination_add(T3, "lan", "http", "http://10.10.10.95/")
a.destination_add(T3, "mail", "tcp", "tcp://smtp.example.org:25", "basic", "u", secret="geheim")
a.destination_add(T3, "web", "http", "https://example.org/", "bearer", secret="geheim")

_b, why = bind("d1", "lan")
ok("nicht erlaubt: Bindung abgewiesen, nennt den Weg", "--allow-destination lan" in why, why)
ok("nichts im Datensatz", not rec("d1").get("destinations"))
code, out = cli("dz", "--allow-destination", "lan")
ok("erlauben ohne Begruendung abgewiesen", code and "why" in out, out)
why = refused(a.egress_allow_destination, T3, "lan", "weil", role="tenant_admin")
ok("erlauben darf nur der Server-Admin", "server admin" in why, why)
why = refused(a.egress_allow_destination, T3, "mail", "weil")
ok("eine tcp-Destination kann nicht erlaubt werden", "cannot be allowed" in why, why)
why = refused(a.egress_allow_destination, T3, "fehlt", "weil")
ok("eine unbekannte auch nicht", "no destination" in why, why)
code, out = cli("dz", "--allow-destination", "lan", "--reason", "KI-Knoten im LAN")
ok("mit Begruendung erlaubt", code == 0 and "allowed" in out, out)
ok("im Log, mit Ziel und Begruendung", any(
    e.get("action") == "egress.destination-allowed" and "10.10.10.95" in (e.get("detail") or "")
    and "KI-Knoten" in (e.get("detail") or "") for e in a.read_tenant_log(T3, limit=100)))
done, why = bind("d1", "lan")
ok("danach geht die Bindung", done is True, why)
_b, why = bind("d1", "mail")
ok("tcp: abgewiesen, und es sagt warum", "no route" in why and "mail" not in (rec("d1").get("destinations") or {}), why)

print("=== die Regel gilt am Listener, nicht nur beim Binden ===")
import ipaddress                                              # noqa: E402
a.ipaddress_for_test = ipaddress.ip_network("192.168.224.0/20")
text = listener()
ok("erlaubt: der Listener leitet weiter", "reverse_proxy http://10.10.10.95" in text, text)
r = a.load_registry()
r["instances"]["d1"]["destinations"]["alt"] = "web"
a.save_registry(r)
text = listener()
ok("eine alte Bindung ohne Erlaubnis: 403 mit Satz, nicht weitergeleitet",
   "not allowed for this closed tenant" in text and "example.org" not in text and "geheim" not in text, text)
fault = cli("dz")[1]
ok("die Seite nennt sie als Fehler", "bound as alt" in fault, fault)
r = a.load_registry()
r["instances"]["d1"]["destinations"].pop("alt")
a.save_registry(r)
ds = a.load_destinations()
ds[T3]["lan"]["egress_allowed"] = True
a.save_destinations(ds)
ok("ein handbearbeitetes `true` ist keine Erlaubnis", "reverse_proxy http://10.10.10.95" not in listener())
ds[T3]["lan"]["egress_allowed"] = {"reason": "KI-Knoten im LAN", "by": "root"}
a.save_destinations(ds)
ok("eine Erlaubnis ohne Lesen (vor Stufe 3) leitet nicht weiter",
   "reverse_proxy http://10.10.10.95" not in listener()
   and "without its target being read" in listener())
ds[T3]["lan"]["egress_allowed"]["read"] = dict(PLAIN)
a.save_destinations(ds)

print("=== die Uebergabe: kein Geheimnis in einen geschlossenen Container ===")
needs = [{"name": "mail", "kind": "tcp", "env": {"host": "SMTP_HOST", "password": "SMTP_PASSWORD"}}]
probe = dict(rec("d1"), destinations={"mail": "mail"}, declared_destinations=needs)
wanted, _m = a.destination_env(probe)
ok("geschlossen: nichts uebergeben", "SMTP_PASSWORD" not in wanted, wanted)
wanted, _m = a.destination_env(dict(probe, egress_exception={"reason": "x"}))
ok("ausgenommen: wie bisher", wanted.get("SMTP_PASSWORD") == "geheim", wanted)

print("=== schliessen mit bestehenden Bindungen: nichts still ===")
a.egress_set_exception("d1", "Vorbereitung")
done, why = bind("d1", "web")
ok("ausgenommen darf sie binden, was sie will", done is True, why)
code, out = cli("dz", "--end-exception", "d1", "--yes")
ok("das Ende der Ausnahme scheitert an der Bindung, nennt sie", code and "web" in out and a.egress_exception(rec("d1")), out)
instance("d2", T3, destinations={"post": "web"})
code, out = cli("dz", "--close")
ok("die Vorschau nennt jede offene Bindung", "'d2' is bound to 'web'" in out and "Nothing changed" in out, out)
why = refused(a.egress_close, T3)
ok("das Schliessen nennt sie und aendert nichts", "'d2' is bound to 'web'" in why, why)
a.egress_allow_destination(T3, "web", "Kundenportal")
ok("erlaubt: das Schliessen geht", refused(a.egress_close, T3) == "")
a.egress_set_exception("d1", "")
ok("und das Ende der Ausnahme", closed_shape(net_of("d1")) and not a.egress_exception(rec("d1")))
page = cli("dz")[1]
ok("die Seite nennt Ziel, Grund und wer bindet",
   "lan: http://10.10.10.95/. KI-Knoten im LAN" in page and "Bound: d1" in page and "Bound: d1, d2" in page, page)
why = refused(a.egress_disallow_destination, T3, "lan")
ok("zuruecknehmen scheitert, solange gebunden", "still bound" in why and "d1" in why, why)
why = refused(a.egress_disallow_destination, T3, "lan", role="tenant_admin")
ok("und ist nur des Server-Admins", "server admin" in why, why)
reg = a.load_registry()
a.destination_unbind(reg, "d1", "lan")
code, out = cli("dz", "--disallow-destination", "lan")
ok("ungebunden geht es, im Log", code == 0 and "egress.destination-disallowed" in log_actions(T3), out)
_b, why = bind("d1", "lan")
ok("danach wird wieder abgewiesen", "not allowed" in why, why)

print("=== ein offener Mandant merkt nichts ===")
instance("o3", T2)
a.destination_add(T2, "frei", "http", "http://10.10.10.95/")
done, why = bind("o3", "frei")
ok("er bindet ohne Erlaubnis", done is True, why)
ok("und der Listener leitet weiter", "reverse_proxy http://10.10.10.95" in listener())
a.destination_add(T3, "tun1", "http", "http://10.10.10.96/")
ds = a.load_destinations()
ds[T3]["tun1"]["target"] = {"through": "x/y"}
a.save_destinations(ds)
_b, why = bind("d1", "tun1")
ok("through: abgewiesen", "tunnel app" in why, why)
why = refused(a.egress_allow_destination, T3, "tun1", "weil")
ok("und nicht erlaubbar", "cannot be allowed" in why, why)

# ------------------------------------------------- Stufe 3: das KI-Gateway
print("=== das Lesen des Ziels, gegen einen echten HTTP-Dienst (Stufe 3) ===")
import http.server                                            # noqa: E402
import json                                                   # noqa: E402
import threading                                              # noqa: E402

KEYS = {"Bearer sk-gruen": "green", "Bearer sk-gelb": "yellow"}
SEEN = []
MODE = {"is": "gateway"}


class FakeGateway(http.server.BaseHTTPRequestHandler):
    """Antwortet wie oaap.ai.gateway auf /v1/usage, und wie anderes sonst."""
    def log_message(self, *x):
        pass

    def do_GET(self):
        SEEN.append((self.path, self.headers.get("Authorization")))
        if MODE["is"] == "kaputt":
            self.send_response(502)
            self.end_headers()
            return
        if MODE["is"] == "weiter":
            self.send_response(303)
            self.send_header("Location", "/v1/usage")
            self.end_headers()
            return
        if (MODE["is"] == "anders" or not self.path.endswith("/v1/usage")
                or (MODE["is"] == "pfad" and not self.path.startswith("/ki/"))):
            self.send_response(404)
            self.end_headers()
            return
        light = KEYS.get(self.headers.get("Authorization") or "")
        body = json.dumps({"ceiling": light} if light else
                          {"error": {"code": "invalid_api_key"}}).encode()
        self.send_response(200 if light else 403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
a.egress_read_target = REAL_READ


def read_at(path, secret, auth="bearer", mode="gateway"):
    """Das echte Lesen; nur der Ort zeigt auf den Dienst im Test."""
    MODE["is"] = mode
    s = a.load_dest_secrets()
    s.setdefault(T3, {})["probe"] = secret
    a.save_dest_secrets(s)
    d = {"kind": "http", "target": {"direct": f"http://127.0.0.1:{PORT}{path}"},
         "auth": {"type": auth}}
    return a.egress_read_target(T3, "probe", d)


r = read_at("/v1", "sk-gruen")
ok("ein Gateway mit gruenem Schluessel: erkannt, Licht und Schluessel-Fingerabdruck",
   r.get("gateway") is True and r.get("ceiling") == "green"
   and r.get("key") == a._egress_key_digest("sk-gruen") and "sk-gruen" not in json.dumps(r), r)
ok("... und es fragte mit dem Schluessel, den der Listener schickt",
   ("/v1/usage", "Bearer sk-gruen") in SEEN, SEEN)
r = read_at("/v1", "sk-gelb")
ok("gelber Schluessel: erkannt als gelb", r.get("gateway") and r.get("ceiling") == "yellow", r)
r = read_at("/v1", "sk-falsch")
ok("ein Schluessel, den das Ziel abweist: nicht lesbar, abgewiesen",
   "HTTP 403" in (r.get("refused") or ""), r)
r = read_at("/ki", "sk-gruen", mode="anders")
ok("ein anderes Ziel (404): kein Gateway, an beiden Orten gefragt",
   r.get("gateway") is False and r.get("answer", "").count("404") == 2, r)
r = read_at("/v1", "sk-gruen", mode="kaputt")
ok("ein Ziel, das 502 sagt: nicht lesbar, abgewiesen", "502" in (r.get("refused") or ""), r)
SEEN.clear()
r = read_at("/v1", "sk-gruen", mode="weiter")
ok("eine Umleitung wird nicht verfolgt (der Schluessel ginge mit)",
   r.get("gateway") is False and "303" in r.get("answer", "") and len(SEEN) == 1, (r, SEEN))
r = read_at("/ki/v1", "sk-gruen", mode="pfad")
ok("ein Gateway unter einem Pfad wird dort gefunden",
   r.get("gateway") is True and r.get("url", "").endswith("/ki/v1/usage"), r)
MODE["is"] = "gateway"
dead = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
dead_port = dead.server_address[1]
dead.server_close()
s = a.load_dest_secrets()
d = {"kind": "http", "target": {"direct": f"http://127.0.0.1:{dead_port}/v1"}, "auth": {"type": "none"}}
r = a.egress_read_target(T3, "probe", d)
ok("ein Gateway, das gerade nicht antwortet, geht nicht als gewoehnliches Ziel durch",
   "did not answer" in (r.get("refused") or ""), r)
r = a.egress_read_target(T3, "probe", {"kind": "http", "target": {"via": "t/o"}})
ok("hinter einem Connector-Tunnel: nicht lesbar, abgewiesen", "connector tunnel" in (r.get("refused") or ""), r)
ok("unter einem Pfad wird auch dort gefragt",
   a._egress_probe_urls({"target": {"direct": "https://h.example.org/ki/v1"}})
   == ["https://h.example.org/v1/usage", "https://h.example.org/ki/v1/usage"])

print("=== erlauben, binden, weiterleiten: nur gruen ===")
a._egress_probe_urls = lambda d: [f"http://127.0.0.1:{PORT}/v1/usage"]
a.destination_add(T3, "ki", "http", "https://ki.example.org/v1", "bearer", secret="sk-gelb")
code, out = cli("dz", "--allow-destination", "ki", "--reason", "lokales Modell")
ok("gelber Schluessel: Erlaubnis abgewiesen, sagt warum",
   code and "'yellow'" in out and not a.egress_destination_allowed(a.load_destinations()[T3]["ki"]), out)
ok("die Absage steht im Log", any(e.get("action") == "egress.destination-allowed"
                                  and e.get("result") == "refused"
                                  for e in a.read_tenant_log(T3, limit=100)))
read = a.destination_set_secret(T3, "ki", "sk-gruen")
ok("ein neuer Schluessel ohne Erlaubnis liest nichts", read == "", read)
code, out = cli("dz", "--allow-destination", "ki", "--reason", "lokales Modell")
ok("gruener Schluessel: erlaubt, und es sagt, was gelesen wurde",
   code == 0 and "AI gateway" in out and "'green'" in out, out)
done, why = bind("d1", "ki")
ok("die geschlossene Instanz bindet ihn", done is True, why)
text = listener()
ok("der Listener leitet weiter, mit dem Schluessel", "reverse_proxy https://ki.example.org:443" in text
   and "Bearer sk-gruen" in text, text)
page = cli("dz")[1]
ok("die Seite nennt Instanz, Ziel und Licht",
   "d1: ki -> ki, an OAAP AI gateway, key light 'green'" in page, page)
read = a.destination_set_secret(T3, "ki", "sk-gelb")
ok("ein neuer gelber Schluessel wird sofort gelesen", "'yellow'" in read, read)
text = listener()
ok("... und der Listener schickt ihn nicht: 403 mit Satz",
   "sk-gelb" not in text and "may only use" in text, text)
ok("die Seite nennt den Fehler", "bound as ki" in cli("dz")[1])
ok("im Log", any(e.get("action") == "egress.destination-read" and e.get("result") == "refused"
                 for e in a.read_tenant_log(T3, limit=100)))
a.destination_set_secret(T3, "ki", "sk-gruen")
ok("wieder gruen: der Listener leitet weiter", "Bearer sk-gruen" in listener())
ds = a.load_destinations()
ds[T3]["ki"]["egress_allowed"]["read"]["ceiling"] = "yellow"
a.save_destinations(ds)
text = listener()
ok("ein gelbes Licht im Datensatz (Hand, Wiederherstellung): 403, kein Schluessel",
   "sk-gruen" not in text and "may only use" in text, text)
ds[T3]["ki"]["egress_allowed"]["read"]["ceiling"] = "green"
a.save_destinations(ds)
s = a.load_dest_secrets()
s[T3]["ki"] = "sk-unbekannt"
a.save_dest_secrets(s)
text = listener()
ok("ein Schluessel, von Hand getauscht, ohne Lesen: 403, kein Schluessel im Listener",
   "credential changed" in text and "sk-unbekannt" not in text, text)
_b, why = bind("d2", "ki")
ok("und beim Binden abgewiesen", "credential changed" in why, why)
s[T3]["ki"] = "sk-gruen"
a.save_dest_secrets(s)
srv.shutdown()
srv.server_close()
code, out = cli("dz", "--allow-destination", "ki", "--reason", "nochmal")
ok("ein Gateway, das beim Erlauben nicht antwortet: abgewiesen", code and "did not answer" in out, out)
ok("die alte, gelesene Erlaubnis bleibt", "Bearer sk-gruen" in listener())
calls = []
a.refresh_destinations = lambda *x, **k: calls.append(1)
a.egress_sweep()
ok("die Sweep schreibt den Listener neu (eine aeltere Datei haelt sonst offen)", calls, calls)
a.refresh_destinations = lambda *x, **k: None

print("=== ein Link zum KI-Gateway: der Schluessel laege in der App ===")


def link(action, src, dst):
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            a.cmd_link(types.SimpleNamespace(action=action, source=src, target=dst))
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue()


instance("gw", T2, app_id="ai-gateway")
code, out = link("add", "d1", "gw")
ok("geschlossen -> offenes Gateway: abgewiesen, nennt den Weg",
   code and "destination" in out and "gw" not in (rec("d1").get("links") or []), out)
code, out = link("add", "gw", "d1")
ok("auch in die andere Richtung", code and "gw" not in (rec("gw").get("links") or []), out)
ok("kein Netz gebaut", a.link_network("d1", "gw") not in NETS)
code, out = link("add", "o3", "gw")
ok("eine offene Instanz darf", code == 0 and "gw" in rec("o3")["links"], out)
r = a.load_registry()
r["instances"]["d1"]["links"] = ["gw"]
a.save_registry(r)
a.restore_links("d1")
ok("ein Link, der zurueckkommt (Wiederherstellung), bekommt kein Netz",
   a.link_network("d1", "gw") not in NETS, NETS.get(a.link_network("d1", "gw")))
ok("die Seite nennt ihn als Fehler", "an AI gateway" in cli("dz")[1].split("8. Faults")[1])
r["instances"]["d1"]["links"] = []
a.save_registry(r)
T4 = tenant("kz")
instance("k1", T4, links=["gw"])
why = refused(a.egress_close, T4)
ok("schliessen mit einem Link zum offenen Gateway: abgewiesen, nennt ihn",
   "'k1' is linked to the AI gateway 'gw'" in why and not a.tenant_egress_closed(T4), why)
instance("gw2", T4, app_id="ai-gateway")
r = a.load_registry()
r["instances"]["k1"]["links"] = ["gw2"]
a.save_registry(r)
ok("ein Gateway im selben Mandanten schliesst mit: geht", refused(a.egress_close, T4) == "")
ok("der Link zum geschlossenen Gateway steht, intern",
   a.setup_link_network(a.load_registry(), "k1", "gw2") == ""
   and closed_shape(NETS[a.link_network("k1", "gw2")]))
why = refused(a.egress_set_exception, "gw2", "Anbieter im Netz")
ok("das Gateway ausnehmen, solange eine geschlossene Instanz verlinkt ist: abgewiesen",
   "'k1' is linked to the AI gateway 'gw2'" in why and not a.egress_exception(rec("gw2")), why)
r = a.load_registry()
r["instances"]["k1"]["links"] = []
a.save_registry(r)
instance("x1", T3, links=["gw2"])
r = a.load_registry()
why = refused(a.egress_open, T4, "Versuch")
ok("den Mandanten des Gateways oeffnen, solange ein fremder Geschlossener verlinkt ist: abgewiesen",
   "'x1' is linked to the AI gateway 'gw2'" in why and a.tenant_egress_closed(T4), why)
r["instances"]["x1"]["links"] = []
a.save_registry(r)

print("=== der echte Parser ===")
real_argv = sys.argv
sys.argv = ["appctl", "egress-faults"]
out = io.StringIO()
with contextlib.redirect_stdout(out):
    a.main()                     # baut den ganzen Parser: ein Konflikt stuerzt hier ab
sys.argv = real_argv
ok("die Befehlszeile baut sich und antwortet", True)
got = {}
real_cli = a._tenant_egress_cli
a._tenant_egress_cli = lambda args: got.update(vars(args))
a.os.geteuid = lambda: 0
sys.argv = ["appctl", "tenant", "egress", "zu", "--except-instance", "kp", "--reason", "weil", "--yes"]
a.main()
sys.argv = real_argv
a._tenant_egress_cli = real_cli
ok("die Argumente kommen so an, wie die Funktion sie liest",
   got.get("except_instance") == "kp" and got.get("idp_reason") == "weil" and got.get("yes") is True
   and got.get("egress_close") is False and got.get("end_exception") is None, got)

# ---------------------------------------------------- Weg in den Knoten
print("=== Einheit, Sweep, Status: auf jedem Weg ===")
ROOT = os.path.join(HERE, "..")


def text(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def unit(s):
    m = re.search(r"oaap-egress\.service <<'EOF'\n(.*?)\nEOF", s, re.S)
    return m.group(1) if m else ""


inst_sh, mig_sh, cli_sh = text("install.sh"), text("platform/migrate.sh"), text("bin/oaap")
ok("Installation und Update schreiben dieselbe Einheit", unit(inst_sh) and unit(inst_sh) == unit(mig_sh))
ok("die Einheit laeuft vor Docker", "Before=docker.service" in unit(inst_sh))
ok("sie setzt genau das Praefix des Codes", f"-i {a.EGRESS_BRIDGE_PREFIX}+ -j DROP" in unit(inst_sh))
ok("beim Update vor dem Blick auf die Netze",
   mig_sh.index("oaap-egress.service") < mig_sh.index("appctl.py\" migrate-networks"))
ok("die Sweep steht in beiden Wachen",
   "appctl.py egress-sweep" in inst_sh and "appctl.py egress-sweep" in mig_sh)
ok("`oaap status` fragt und wird bei FAULT DEGRADED",
   "egress-faults" in cli_sh and "grep -q FAULT" in cli_sh)
ok("das Entfernen der Plattform nimmt Einheit und Regel mit",
   "rm -f /etc/systemd/system/oaap-egress.service" in cli_sh and "-D INPUT -i oaapc+" in cli_sh)
src = text("platform/appctl.py")
ok("das Manifest kennt das Wort nicht", not re.search(
    r"def validate_manifest.*?egress", src[:src.index("def validate_manifest") + 40000].split("def validate_manifest", 1)[1][:30000], re.S))

print(f"\n{'ALLES GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
