#!/usr/bin/env python3
"""Der Kettentest einer Destination „Person durchreichen“ (RFC-0096 Stufe 1,
oaap.net.destinations 0.14 §2.9.10).

Festgehalten wird, was der Test verspricht, gegen einen ECHTEN TLS-Server auf
127.0.0.1, der wie ein SAP-ICM nach einem Client-Zertifikat fragt:

- er meldet sich als der reservierte Prüfbenutzer OAAP_CHECK an, nie als eine
  Person; das Zertifikat trägt O=OAAP, OU=<Mandant>, CN=OAAP_CHECK, die SAN
  urn:oaap:check und lebt eine Minute;
- er sagt je Glied, ob es hält: erreichbar, Serverzertifikat = Pin, CA vertraut,
  Zuordnung (kein 401), Benutzer; nach dem ersten Bruch wird nichts mehr
  versucht; ohne Prüfpfad endet er nach der Zuordnung;
- die Antwort des Ziels steht nirgends: nicht im Ergebnis, nicht im Protokoll;
- er läuft in connect per docker exec (hier: derselbe Code als Prozess), connect
  bekommt keine neue Route;
- starten dürfen server_admin und der tenant_admin des Mandanten; den Prüfpfad
  ändert, wer die Destination ändern darf.

Braucht kein Docker und keinen Knoten, aber `cryptography`.
Aufruf: python3 test/test_chain_check.py
"""
import contextlib
import datetime
import http.server
import io
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
CONNECT = os.path.join(PLATFORM, "services", "connect")
DATA = tempfile.mkdtemp(prefix="oaap-chain-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)
sys.path.insert(0, CONNECT)

import appctl as m                                            # noqa: E402
import propagate                                              # noqa: E402
from cryptography import x509                                 # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa    # noqa: E402
from cryptography.x509.oid import NameOID                     # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.docker_subnets = lambda net=None: [ipaddress.ip_network("172.18.0.0/16")]
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
KUNDE, _ = m.tenant_create("kunde")
ANDERE, _ = m.tenant_create("andere")
CA_DIR = os.path.join(DATA, "ca")
WORK = tempfile.mkdtemp(prefix="oaap-chain-srv-")

# --- ein Ziel wie ein ICM ------------------------------------------------------
skey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
sname = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.dummy.nodomain")])
now = datetime.datetime.now(datetime.timezone.utc)
scert = (x509.CertificateBuilder().subject_name(sname).issuer_name(sname)
         .public_key(skey.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=30))
         .sign(skey, hashes.SHA256()))
SERVER_PEM = scert.public_bytes(serialization.Encoding.PEM).decode()
import hashlib                                                # noqa: E402
FP = hashlib.sha256(scert.public_bytes(serialization.Encoding.DER)).hexdigest()
chain_file = os.path.join(WORK, "server.pem")
with open(chain_file, "w", encoding="utf-8") as f:
    f.write(SERVER_PEM + skey.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode())
propagate.ensure_ca(CA_DIR, KUNDE, "kunde")
propagate.ensure_ca(CA_DIR, ANDERE, "andere")
SECRET_BODY = "GEHEIME-ANTWORT-DES-ZIELS"
MODE = {"status": 200, "id": "cn"}
SEEN = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        der = self.connection.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der)
        SEEN["cert"], SEEN["path"] = cert, self.path
        SEEN["cookie"] = self.headers.get("Cookie")
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        uid = cn if MODE["id"] == "cn" else MODE["id"]
        body = json.dumps({"id": uid, "secret": SECRET_BODY} if uid else
                          {"secret": SECRET_BODY}).encode()
        self.send_response(MODE["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "MYSAPSSO2=ticket")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(trust_tid):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain_file)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=propagate.ca_paths(CA_DIR, trust_tid)[1])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def states(res):
    return [s for _l, s, _w in res["links"]]


def check(port, pin=FP, path="/sap/bc/ui2/start_up", tid=KUNDE, label="kunde"):
    return propagate.chain_check(CA_DIR, tid, label, f"https://127.0.0.1:{port}",
                                 SERVER_PEM, pin, path, timeout=5)


srv, PORT = serve(KUNDE)
srv_other, PORT_OTHER = serve(ANDERE)

print("=== die Glieder, gegen ein echtes TLS-Ziel ===")
res = check(PORT)
ok("alles in Ordnung: fünfmal „hält“, Status 200",
   states(res) == ["held"] * 5 and res["status"] == 200, res)
c = SEEN["cert"]
ok("... angemeldet als OAAP_CHECK im Mandanten: O=OAAP, OU=kunde, CN=OAAP_CHECK",
   c.subject.rfc4514_string() == "CN=OAAP_CHECK,OU=kunde,O=OAAP", c.subject.rfc4514_string())
san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
ok("... SAN urn:oaap:check, keine Personen-UUID",
   san.get_values_for_type(x509.UniformResourceIdentifier) == ["urn:oaap:check"], san)
life = c.not_valid_after_utc - c.not_valid_before_utc
ok("... höchstens eine Minute gültig (plus die Minute zurück für Uhren)",
   life <= datetime.timedelta(minutes=2), life)
ok("... ausgestellt von der CA dieses Mandanten",
   c.issuer.rfc4514_string() == "CN=OAAP Propagation kunde,OU=kunde,O=OAAP", c.issuer.rfc4514_string())
ok("... ein GET auf den Prüfpfad, ohne Cookie", SEEN["path"] == "/sap/bc/ui2/start_up"
   and SEEN["cookie"] is None, SEEN)
ok("... die Seriennummer steht im Ergebnis, die Antwort des Ziels nicht",
   res["serial"] == format(c.serial_number, "x") and SECRET_BODY not in json.dumps(res), res)

res = check(free_port())
ok("Ziel aus: „erreichbar“ bricht, der Rest wird nicht versucht",
   states(res) == ["broke"] + ["not reached"] * 4 and not res["serial"], res)
SEEN.clear()
res = check(PORT, pin="00" * 32)
ok("anderes Serverzertifikat als der Pin: bricht bei „Pin“, mit dem neuen Fingerabdruck",
   states(res) == ["held", "broke"] + ["not reached"] * 3 and res["links"][1][2] == FP, res)
ok("... und es wurde kein Zertifikat ausgestellt", not res["serial"] and "cert" not in SEEN, res)
res = check(PORT_OTHER)
ok("das Ziel vertraut einer anderen CA: bricht bei „CA“",
   states(res) == ["held", "held", "broke", "not reached", "not reached"], res)
MODE.update(status=401)
res = check(PORT)
ok("401: bricht bei der Zuordnung", states(res)[:4] == ["held", "held", "held", "broke"]
   and res["links"][3][2] == "401" and states(res)[4] == "not reached", res)
MODE.update(status=200, id="JMUELLER")
res = check(PORT)
ok("200, aber ein anderer Benutzer: bricht bei „Benutzer“, ohne dessen Namen",
   states(res) == ["held"] * 4 + ["broke"] and "JMUELLER" not in json.dumps(res), res)
MODE.update(status=403, id="cn")
res = check(PORT)
ok("403: Zuordnung hält, Benutzer bricht mit dem Status",
   states(res) == ["held"] * 4 + ["broke"] and res["links"][4][2] == "403", res)
MODE.update(status=200, id="")
res = check(PORT)
ok("200 ohne Benutzer in der Antwort: hält, mit dem Satz dazu",
   states(res) == ["held"] * 5 and "names no user" in res["links"][4][2], res)
MODE.update(status=200, id="cn")
res = check(PORT, path="")
ok("ohne Prüfpfad (D8): bis zur Zuordnung, der Benutzer „nicht geprüft“",
   states(res) == ["held"] * 4 + ["not checked"], res)

print("")
print("=== der Befehl und der Auftrag des Portals ===")
_real_run = subprocess.run


def fake_run(cmd, *a, **kw):
    """docker exec <connect> python -c <code> -- hier derselbe Code als Prozess,
    mit dem CA-Verzeichnis des Tests statt /ca."""
    if cmd[:2] == ["docker", "exec"] and cmd[3:5] == ["python", "-c"]:
        code = cmd[5].replace(json.dumps("/ca"), json.dumps(CA_DIR))
        return _real_run([sys.executable, "-c", code], cwd=CONNECT, **kw)
    return _real_run(cmd, *a, **kw)


m.subprocess.run = fake_run
DEST = {"kind": "http", "target": {"direct": f"https://127.0.0.1:{PORT}"},
        "auth": {"type": "principal_propagation", "credential": "x509",
                 "user": [{"rule": "claim", "name": "sap_user"}], "name_check": "sap",
                 "lifetime_minutes": 10, "server_cert": SERVER_PEM, "server_sha256": FP,
                 "claim_check": "test"},
        "secret": False, "created": "", "created_by": "root"}
m.save_destinations({KUNDE: {"sap": json.loads(json.dumps(DEST)),
                             "ksap": dict(json.loads(json.dumps(DEST)), author="tenant_admin"),
                             "ki": {"kind": "http", "target": {"direct": "https://ki.example.com"},
                                    "auth": {"type": "none"}, "secret": False}}})
ok("ohne gespeicherten Prüfpfad nimmt eine SAP-Destination /sap/bc/ui2/start_up",
   m.effective_check_path(DEST["auth"]) == "/sap/bc/ui2/start_up")
ok("... ein leer gespeicherter heißt „ohne“", m.effective_check_path({"check_path": ""}) == "")
for bad in ("sap/bc", "//evil.example.com/x", "/a/../b", "/a/..", "/x#y", "/a b", "/" + "a" * 300):
    ok(f"Prüfpfad abgewiesen: {bad[:30]!r}", bool(m.check_path_refusal(bad)))
ok("... erlaubt: /sap/bc/ui2/start_up?sap-client=001",
   m.check_path_refusal("/sap/bc/ui2/start_up?sap-client=001") == "")

res = m.propagation_check(KUNDE, "sap", who="root")
ok("propagation_check läuft wie in connect und meldet „hält“",
   res["ok"] and states(res) == ["held"] * 5 and res["path"] == "/sap/bc/ui2/start_up", res)
logs = [e for e in m.read_tenant_log(KUNDE) if e["action"] == "destination.propagation.check"]
ok("... eine Zeile im Protokoll: Benutzer, Seriennummer, Glieder",
   logs and "OAAP_CHECK" in logs[-1]["detail"] and res["serial"] in logs[-1]["detail"]
   and "user:held" in logs[-1]["detail"], logs)
ok("... die Antwort des Ziels steht nicht im Protokoll", SECRET_BODY not in json.dumps(logs))
lines = m.chain_lines(res)
ok("... fünf Zeilen, eine je Glied", len(lines) == 5 and all("holds" in l_ for l_ in lines), lines)
MODE.update(status=401)
res = m.propagation_check(KUNDE, "sap", who="root")
de = m.chain_lines(res, de=True)
ok("401: „BRICHT“ bei der Zuordnung, darunter RZ10 und CERTRULE",
   not res["ok"] and any("BRICHT" in l_ and "Zuordnung" in l_ for l_ in de)
   and any("RZ10" in l_ and "CERTRULE" in l_ for l_ in de), de)
ok("... und im Protokoll „failed“",
   [e for e in m.read_tenant_log(KUNDE) if e["action"] == "destination.propagation.check"][-1]["result"]
   == "failed")
MODE.update(status=200)
try:
    m.propagation_check(KUNDE, "ki")
    ok("eine Destination ohne Person durchreichen wird nicht geprüft", False)
except m.DestinationRefused as e:
    ok("eine Destination ohne Person durchreichen wird nicht geprüft", "principal_propagation" in str(e), e)

# der Auftrag des Portals, durch den echten Worker
d_ = os.path.join(DATA, "data", "identity")
os.makedirs(d_, exist_ok=True)
with open(os.path.join(d_, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": n, "roles": r, "tenant": t, "groups": [], "active": True}
               for n, r, t in (("root", ["server_admin"], ""), ("karin", ["tenant_admin"], KUNDE),
                               ("uwe", ["user"], KUNDE), ("anna", ["tenant_admin"], ANDERE))], f)
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, op, **args):
    _rid[0] += 1
    rid = f"c{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": "", "action": "destination-admin", "by": by,
                   "op": op, "args": args}, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


r = queue("karin", "check", tenant=KUNDE, name="sap")
ok("der tenant_admin des Mandanten startet den Test (D5), auch ohne Erlaubnis aus 2.7.1",
   r["ok"] and r["detail"]["ok"] and "hält" in r["message"] and len(r["detail"]["lines"]) == 5, r)
ok("... die Antwort des Ziels steht nicht im Ergebnis", SECRET_BODY not in json.dumps(r))
r = queue("uwe", "check", tenant=KUNDE, name="sap")
ok("ein Benutzer nicht", not r["ok"] and not r.get("detail"), r)
r = queue("anna", "check", tenant=KUNDE, name="sap")
ok("ein fremder tenant_admin prüft nicht die Destination dieses Mandanten",
   not (r.get("detail") or {}).get("lines"), r)
r = queue("karin", "check", tenant=KUNDE, name="sap", check_path="/anders")
ok("den Prüfpfad einer Destination des Betreibers ändert der tenant_admin nicht",
   not r["ok"] and "Prüfpfad" in r["message"]
   and "check_path" not in m.load_destinations()[KUNDE]["sap"]["auth"], r)
r = queue("karin", "check", tenant=KUNDE, name="sap", check_path="/sap/bc/ui2/start_up")
ok("... derselbe Pfad wie bisher ist keine Änderung: geprüft", r["ok"], r)
m.tenant_set_admin_may(KUNDE, "destinations", True)
r = queue("karin", "check", tenant=KUNDE, name="ksap", check_path="")
ok("mit Erlaubnis an der eigenen Destination: Pfad leer gesetzt, dann geprüft (D8)",
   r["ok"] and m.load_destinations()[KUNDE]["ksap"]["auth"]["check_path"] == ""
   and states(r["detail"])[-1] == "not checked", r)
r = queue("root", "check", tenant=KUNDE, name="sap", check_path="../x")
ok("ein schlechter Pfad wird abgewiesen, nichts geprüft", not r["ok"] and "check path" in r["message"], r)
MODE.update(status=401)
r = queue("root", "check", tenant=KUNDE, name="sap")
ok("ein Bruch ist eine Antwort, kein Fehler des Auftrags: „bricht bei: Zuordnung“",
   r["ok"] and not r["detail"]["ok"] and "bricht bei: Zuordnung" in r["message"], r)
MODE.update(status=200)

print("")
print("=== Kommandozeile und connect ===")
parser_src = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("oaap destination kennt „check“ und --check-path",
   '"bindings", "ca", "check"]' in parser_src and '"--check-path"' in parser_src)
args = types.SimpleNamespace(action="check", name="sap", second=None, tenant="kunde",
                             check_path=None)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_destination(args)
ok("oaap destination check sap --tenant kunde: Kopfzeile und fünf Glieder",
   "as OAAP_CHECK" in out.getvalue() and out.getvalue().count("holds") == 5, out.getvalue())
MODE.update(status=401)
out = io.StringIO()
try:
    with contextlib.redirect_stdout(out):
        m.cmd_destination(args)
    code = 0
except SystemExit as e:
    code = e.code
ok("... ein Bruch endet mit Rückgabewert 1 (für Skripte)", code == 1 and "BREAKS" in out.getvalue(),
   out.getvalue())
MODE.update(status=200)
conn_src = open(os.path.join(CONNECT, "app.py"), encoding="utf-8").read()
ok("connect hat keine Route für den Test (D3): kein chain_check im Dienst",
   "chain_check" not in conn_src and "CHECK_USER" not in conn_src)

srv.shutdown()
srv_other.shutdown()
print("")
print("PASS" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
