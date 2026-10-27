#!/usr/bin/env python3
"""Die angemeldete Person bis ins Ziel (RFC-0066 Stufe 1,
oaap.net.destinations 2.9, oaap.core.identity 2.11).

Was hier festgehalten wird -- jede Regel mit ihrer Gegenprobe:

- die Zusicherung: identity unterschreibt, connect prueft, mit ZWEI
  Kopien des Formats, die hier gegeneinander laufen; falscher Adressat,
  abgelaufen, veraendert, ohne Schluessel -- alles abgelehnt;
- nur sap_user / sap_user_<x> werden uebernommen, und bei jeder
  Anmeldung ERSETZT (ein entzogener SAP-Benutzer verschwindet);
- kein Name -> Ablehnung mit Grund, nie ein technischer Benutzer;
- das Zertifikat: O=OAAP, OU=<Mandant> von der Plattform, CN=<Benutzer>,
  nur Client-Authentisierung, die UUID als SAN;
- appctl: das Serverzertifikat wird erst mit --accept-sha256 gepinnt;
  ein Attribut, das die Person selbst aendern darf, wird abgelehnt;
  ein ungelesenes nur mit ausdruecklicher Begruendung;
- die Freigabe ist eigen: ohne --propagate antwortet das Gateway 403,
  mit ihr laesst es NUR die Zusicherung durch und nennt den Aufrufer
  selbst; an jede andere Destination wird die Zusicherung gestrichen.

Die Messung gegen ein echtes SAP steht in RFC-0066 §8.1 (Phase A) und
folgt fuer den ganzen Weg auf oaapx01 (Phase C).

Aufruf: python3 test/test_propagation.py
Braucht `cryptography` (wie der connect-Dienst) und flask (wie identity);
fehlt eines, meldet der betroffene Teil SKIP statt PASS.
"""
import argparse
import contextlib
import datetime
import importlib
import io
import ipaddress
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
CONNECT = os.path.join(SERVICES, "connect")
DATA = tempfile.mkdtemp(prefix="oaap-propagation-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)
sys.path.insert(0, SERVICES)

fails = 0
skips = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def skip(label, why):
    global skips
    skips += 1
    print(f"SKIP  {label} ({why})")


import assertion                                              # noqa: E402
import idp                                                    # noqa: E402

try:
    sys.path.insert(0, CONNECT)
    import propagate                                          # noqa: E402
except ImportError as e:                                      # noqa: F841
    propagate = None

KEY = bytes.fromhex("11" * 32)

# ---------------------------------------------------------------------------
print("")
print("Die Zusicherung: identity unterschreibt, connect prueft")
if propagate is None:
    skip("Zusicherung gegen connect", "cryptography fehlt")
else:
    tok = assertion.mint(KEY, "uuid-1", "demo-dev", "T1", "inst-a",
                         {"sap_user": "ACCX_T_DEV"}, now=1000)
    claims, why = propagate.check_assertion(KEY, tok, "inst-a", now=1100)
    ok("dieselbe Instanz, in der Zeit: angenommen, mit allen Angaben",
       not why and claims["sub"] == "uuid-1" and claims["ten"] == "T1"
       and claims["prop"] == {"sap_user": "ACCX_T_DEV"}, why)
    ok("eine andere Instanz: abgelehnt (der Adressat ist das Netz, nicht die App)",
       "another instance" in propagate.check_assertion(KEY, tok, "inst-b", now=1100)[1])
    ok("nach fuenf Minuten: abgelehnt",
       "expired" in propagate.check_assertion(KEY, tok, "inst-a", now=1000 + 300)[1])
    head, body, sig = tok.split(".")
    forged = assertion._b64(json.dumps(dict(claims, prop={"sap_user": "DDIC"}),
                                       separators=(",", ":"), sort_keys=True).encode())
    ok("ein umgeschriebener Benutzer: Unterschrift haelt nicht",
       "signature" in propagate.check_assertion(KEY, f"{head}.{forged}.{sig}",
                                                "inst-a", now=1100)[1])
    ok("ein anderer Schluessel: abgelehnt",
       "signature" in propagate.check_assertion(bytes(32), tok, "inst-a", now=1100)[1])
    ok("ohne Schluessel auf dem Knoten: abgelehnt, nicht durchgewunken",
       "no assertion key" in propagate.check_assertion(b"", tok, "inst-a", now=1100)[1])
    ok("keine Zusicherung: abgelehnt mit Grund",
       "no assertion" in propagate.check_assertion(KEY, "", "inst-a")[1])
    # beide Kopien des Formats urteilen gleich
    ok("assertion.check und propagate.check_assertion urteilen gleich",
       assertion.check(KEY, tok, "inst-a", now=1100)[0]
       == propagate.check_assertion(KEY, tok, "inst-a", now=1100)[0]
       and bool(assertion.check(KEY, tok, "inst-b", now=1100)[1])
       == bool(propagate.check_assertion(KEY, tok, "inst-b", now=1100)[1]))
ok("ohne Schluessel, ohne Instanz, ohne Person: keine Zusicherung",
   assertion.mint(b"", "u", "n", "T", "i") == ""
   and assertion.mint(KEY, "u", "n", "T", "") == ""
   and assertion.mint(KEY, "", "n", "T", "i") == "")

# ---------------------------------------------------------------------------
print("")
print("Was vom Anmeldedienst uebernommen wird")
got = idp.propagation_from({"sap_user": "ACCX_T_DEV", "sap_user_a4h": "X1",
                            "sap_user_A4H": "nein", "email": "a@b.c",
                            "sap_user_x": "mit leerzeichen", "groups": ["/a"],
                            "sap_user_y": ["liste"]})
ok("sap_user und sap_user_<klein> ja, alles andere nein",
   got == {"sap_user": "ACCX_T_DEV", "sap_user_a4h": "X1"}, got)
if propagate is not None:
    ok("idp und connect kennen dasselbe Muster",
       idp.PROPAGATE_CLAIM_RE == propagate.PROPAGATE_CLAIM_RE)

# ---------------------------------------------------------------------------
print("")
print("Der Backend-Benutzer: erste Regel mit Namen, sonst Ablehnung")
if propagate is None:
    skip("Benutzerregeln", "cryptography fehlt")
else:
    rules = [{"rule": "claim", "name": "sap_user_a4h"},
             {"rule": "claim", "name": "sap_user"}]
    ok("die erste Regel mit Wert gewinnt",
       propagate.backend_user(rules, {"sap_user": "a", "sap_user_a4h": "b"}, "sap")[0] == "B")
    ok("faellt durch, wenn die erste leer ist; SAP-Namen gross",
       propagate.backend_user(rules, {"sap_user": "accx_t_dev"}, "sap")[0] == "ACCX_T_DEV")
    name, why = propagate.backend_user(rules, {}, "sap")
    ok("kein Wert: kein Name und ein Grund -- kein technischer Benutzer",
       name == "" and "no backend user" in why, why)
    ok("ein zu langer SAP-Name: abgelehnt",
       "valid SAP" in propagate.backend_user(rules, {"sap_user": "A" * 13}, "sap")[1])
    ok("eine Regel auf einen fremden Claim zaehlt nicht",
       propagate.backend_user([{"rule": "claim", "name": "email"}],
                              {"email": "x"}, "")[0] == "")

# ---------------------------------------------------------------------------
print("")
print("Die Zertifizierungsstelle und das Zertifikat")
if propagate is None:
    skip("CA und Zertifikat", "cryptography fehlt")
else:
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    ca_dir = os.path.join(DATA, "ca")
    ca, ca_key, made = propagate.ensure_ca(ca_dir, "T1", "pxx")
    ca2, _k2, made2 = propagate.ensure_ca(ca_dir, "T1", "pxx")
    ok("einmal gemacht, danach wiederverwendet",
       made and not made2 and ca.serial_number == ca2.serial_number)
    ok("die CA darf nur Zertifikate ausstellen, eine Ebene",
       ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
       and ca.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0)
    if sys.platform != "win32":
        ok("der CA-Schluessel ist 0600",
           oct(os.stat(os.path.join(ca_dir, "T1.key")).st_mode & 0o777) == "0o600")
    now = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)
    cert_pem, key_pem, serial, not_after = propagate.issue(
        ca, ca_key, "pxx", "ACCX_T_DEV", "uuid-1", 10, now=now)
    c = x509.load_pem_x509_certificate(cert_pem)
    subj = {a.oid: a.value for a in c.subject}
    ok("Inhaber O=OAAP, OU=<Mandant>, CN=<Benutzer>",
       subj == {NameOID.ORGANIZATION_NAME: "OAAP",
                NameOID.ORGANIZATIONAL_UNIT_NAME: "pxx",
                NameOID.COMMON_NAME: "ACCX_T_DEV"}, subj)
    ok("ausgestellt von der Mandanten-CA", c.issuer == ca.subject)
    ok("nur Client-Authentisierung",
       list(c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
       == [ExtendedKeyUsageOID.CLIENT_AUTH])
    ok("die Person als SAN urn:oaap:user:<uuid>",
       c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
       .get_values_for_type(x509.UniformResourceIdentifier) == ["urn:oaap:user:uuid-1"])
    ok("zehn Minuten gueltig, eine Minute zurueck",
       c.not_valid_after_utc == now + datetime.timedelta(minutes=10)
       and c.not_valid_before_utc == now - datetime.timedelta(minutes=1))
    ok("Laufzeit nach oben begrenzt",
       propagate.issue(ca, ca_key, "pxx", "U", "", 9999, now=now)[3]
       == now + datetime.timedelta(minutes=60))
    cache = propagate.CertCache()
    cache.put(("T1", "a4h", "uuid-1", "ACCX_T_DEV"), "ctx", serial, not_after)
    ok("Zwischenspeicher: gilt bis kurz vor Ablauf",
       cache.get(("T1", "a4h", "uuid-1", "ACCX_T_DEV"), now=now) is not None
       and cache.get(("T1", "a4h", "uuid-1", "ACCX_T_DEV"),
                     now=not_after - datetime.timedelta(seconds=30)) is None)
    ok("... und ein geaenderter SAP-Benutzer bekommt ein neues Zertifikat",
       cache.get(("T1", "a4h", "uuid-1", "ANDERER"), now=now) is None)

# ---------------------------------------------------------------------------
print("")
print("appctl: anlegen, pinnen, das Attribut pruefen, freigeben")
import appctl as m                                            # noqa: E402

NETS = {"oaap_default": ["172.18.0.0/16"]}


def fake_subnets(net=None):
    if net is None:
        return [ipaddress.ip_network(s) for v in NETS.values() for s in v]
    return [ipaddress.ip_network(s) for s in NETS.get(net, [])]


m.docker_subnets = fake_subnets
m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.image_uid = lambda img: None
m.recreate_instance_containers = lambda name, *a, **kw: None
m.container_env = lambda container: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
FP = "ab" * 32
m.fetch_server_cert = lambda host, port: ("-----BEGIN CERTIFICATE-----\nAAA\n"
                                          "-----END CERTIFICATE-----\n", FP)
EDIT = {"sap_user": "admin"}
m.propagation_claim_check = lambda tid, claims: (
    {c: EDIT.get(c, "") for c in claims}, "read from test realm")


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except m.DestinationRefused as e:
        return str(e)
    return ""


T = "https://a4h.example.com:50001"
why = refused(m.destination_add, DEFAULT, "a4h", "http", T, "principal_propagation")
ok("ohne --accept-sha256: abgelehnt, der Fingerabdruck wird gezeigt, nichts gespeichert",
   "AB:AB" in why and "a4h" not in (m.load_destinations().get(DEFAULT) or {}), why)
ok("ein falscher Fingerabdruck: abgelehnt",
   bool(refused(m.destination_add, DEFAULT, "a4h", "http", T,
                "principal_propagation", accept_sha256="cd" * 32)))
ok("http statt https: abgelehnt",
   "https" in refused(m.destination_add, DEFAULT, "a4h", "http",
                      "http://a4h.example.com:50000", "principal_propagation",
                      accept_sha256=FP))
EDIT["sap_user"] = "self"
ok("die Person darf sap_user selbst aendern: abgelehnt",
   "themselves" in refused(m.destination_add, DEFAULT, "a4h", "http", T,
                           "principal_propagation", accept_sha256=FP))
EDIT["sap_user"] = ""
ok("nicht lesbar und keine Begruendung: abgelehnt",
   "--unchecked-claim" in refused(m.destination_add, DEFAULT, "a4h", "http", T,
                                  "principal_propagation", accept_sha256=FP))
ok("ein Claim ausserhalb des Musters: abgelehnt",
   "not a claim" in refused(m.destination_add, DEFAULT, "a4h", "http", T,
                            "principal_propagation", accept_sha256=FP,
                            claims=["email"]))
EDIT["sap_user"] = "admin"
d = m.destination_add(DEFAULT, "a4h", "http", T, "principal_propagation",
                      accept_sha256=FP.upper())
ok("nur Admins aendern: angelegt, gepinnt, gelesen statt behauptet",
   d["auth"]["server_sha256"] == FP and d["auth"]["user"] == [
       {"rule": "claim", "name": "sap_user"}]
   and d["auth"]["claim_check"].startswith("read from") and d["secret"] is False, d)
ok("der Zusicherungsschluessel des Knotens existiert jetzt",
   len((m._read_file(m.ASSERTION_KEY_FILE) or "").strip()) == 64)
if sys.platform != "win32":
    ok("... und ist 0600",
       oct(os.stat(m.ASSERTION_KEY_FILE).st_mode & 0o777) == "0o600")
m.destination_add(DEFAULT, "erp", "http", "https://erp.example.com/api", "bearer",
                  secret="tok123")

pkg = tempfile.mkdtemp(prefix="oaap-prop-pkg-")
with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
    f.write("\n".join(['oaap_manifest: "0.5"', "app:", "  id: probe",
                       "  name: Probe", "  version: 0.1.0", "  type: native",
                       "services:", "  web:", "    build: .", "    port: 80",
                       "routes:", "  - path: /", "    roles: [user]",
                       "health:", "  path: /healthz", ""]))
NETS[m.app_network("probe")] = ["172.30.0.0/16"]
args = argparse.Namespace(package=pkg, path="", ref="", name="probe",
                          channel="test", store_source="", tenant="", key="",
                          ident=None, rehearsal=None, bind=[])
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    m._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
reg = m.load_registry()
key = next(k for k in reg["instances"] if k.endswith("probe") or k == "probe")
ok("--propagate an einer gewoehnlichen Destination: abgelehnt",
   "does not carry" in refused(m.destination_bind, reg, key, "erp", propagate=True))
m.destination_bind(reg, key, "a4h")
m.destination_bind(m.load_registry(), key, "erp")
site = m._read_file(os.path.join(m.CADDY_APPS_DIR, m.DEST_SITE)) or ""
start = site.find("path /destinations/a4h")
end = site.find("path /destinations/erp")
blk = site[start:end if end > start else len(site)]
ok("gebunden OHNE Freigabe: das Gateway antwortet 403 mit dem Weg zur Freigabe",
   "does not carry the signed-in person" in blk and "--propagate" in blk
   and "/propagate/" not in blk, blk)
m.destination_bind(m.load_registry(), key, "a4h", propagate=True)
reg = m.load_registry()
ok("die Freigabe steht an der Instanz, getrennt von der Bindung",
   reg["instances"][key].get("propagate") == ["a4h"]
   and reg["instances"][key]["destinations"]["a4h"] == "a4h")
site = m._read_file(os.path.join(m.CADDY_APPS_DIR, m.DEST_SITE)) or ""
start = site.find("path /destinations/a4h")
blk = site[start:site.find("path /destinations/erp")] if "path /destinations/erp" in site[start:] else site[start:]
ok("mit Freigabe: weiter an connect /propagate/a4h",
   "rewrite * /propagate/a4h{uri}" in blk and f"reverse_proxy {m.CONNECT_SERVICE}" in blk, blk)
ok("... den Aufrufer nennt das Gateway, nicht die App",
   f'header_up X-OAAP-Connect-Caller "{key}"' in blk
   and f'header_up X-OAAP-Connect-Destination "{DEFAULT}/a4h"' in blk)
ok("... die Zusicherung bleibt, jede andere Identitaetszeile wird gestrichen",
   "request_header -X-OAAP-User-Assertion" not in blk
   and all(f"request_header -{h}" in blk for h in m.IDENTITY_HEADERS
           if h != m.ASSERTION_HEADER), blk)
erp = site[site.find("path /destinations/erp"):]
ok("an eine gewoehnliche Destination wird die Zusicherung gestrichen",
   "request_header -X-OAAP-User-Assertion" in erp[:erp.find("reverse_proxy")])
m.destination_bind(m.load_registry(), key, "a4h")
ok("neu binden ohne --propagate nimmt die Freigabe zurueck",
   "propagate" not in m.load_registry()["instances"][key])
m.destination_bind(m.load_registry(), key, "a4h", propagate=True)
m.destination_unbind(m.load_registry(), key, "a4h")
ok("loesen nimmt die Freigabe mit",
   "propagate" not in m.load_registry()["instances"][key])

caddyfile = m._read_file(os.path.join(PLATFORM, "Caddyfile")) or ""
ok("die feste Caddyfile streicht die Zusicherung so oft wie die E-Mail",
   caddyfile.count("request_header -X-OAAP-User-Assertion")
   == caddyfile.count("request_header -X-OAAP-Email"))

# ---------------------------------------------------------------------------
print("")
print("identity: die sechste Kopfzeile")
try:
    import flask                                              # noqa: F401
except ImportError:
    flask = None
if flask is None or propagate is None:
    skip("identity", "flask oder cryptography fehlt")
else:
    idata = tempfile.mkdtemp(prefix="oaap-prop-identity-")
    os.environ.update(SESSION_SECRET="s", SETUP_TOKEN="t", INTERNAL_API_KEY="k",
                      OAAP_IDENTITY_DATA_DIR=idata,
                      ASSERTION_KEY_FILE=os.path.join(idata, "assertion.key"))
    sys.path.insert(0, os.path.join(SERVICES, "identity"))
    sys.modules.pop("app", None)
    ident = importlib.import_module("app")
    # the test has no tenants file; a node resolves the person's tenant
    # (an unresolvable one gets NO assertion -- checked below)
    ident.resolve_tenant = lambda ref: {"default": "T-default"}.get(ref)
    person = {"username": "demo-dev", "roles": ["user"], "id": "uuid-1",
              "kind": "human", "tenant": "default",
              "idp": {"propagate": {"sap_user": "ACCX_T_DEV"}}}
    h = ident.identity_headers(person, "probe")
    ok("ohne Schluessel: die Kopfzeile ist da und leer",
       h.get("X-OAAP-User-Assertion") == "")
    with open(os.environ["ASSERTION_KEY_FILE"], "w") as f:
        f.write(KEY.hex())
    h = ident.identity_headers(person, "probe")
    claims, why = propagate.check_assertion(KEY, h["X-OAAP-User-Assertion"], "probe")
    ok("mit Schluessel: fuer diese Instanz, mit sap_user",
       not why and claims["prop"] == {"sap_user": "ACCX_T_DEV"}
       and claims["sub"] == "uuid-1", why)
    ok("ein Mandant, den der Knoten nicht kennt: leer",
       ident.identity_headers(dict(person, tenant="fremd"), "probe")
       ["X-OAAP-User-Assertion"] == "")
    ok("ohne Instanz: leer",
       ident.identity_headers(person, "")["X-OAAP-User-Assertion"] == "")
    ok("eine Maschine: leer",
       ident.identity_headers(dict(person, kind="machine"), "probe")
       ["X-OAAP-User-Assertion"] == "")
    ok("dieselbe Kopfzeilenliste wie appctl",
       set(h) == set(m.IDENTITY_HEADERS), set(h) ^ set(m.IDENTITY_HEADERS))

# ---------------------------------------------------------------------------
print("")
print("connect: jede Person kommt als sie selbst an -- nichts bleibt haengen")
# Gemessen an a4h am 27.10.: mit dem Cookie-Speicher des HTTP-Clients kam
# die ZWEITE und die DRITTE Person als die erste an (SAP setzt nach der
# Anmeldung ein Ticket als Cookie). Dieses Ziel tut dasselbe und sagt,
# welches Zertifikat und welches Cookie es gesehen hat.
try:
    import asyncio
    import ssl as _ssl
    from aiohttp import web as _web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:
    _web = None
if propagate is None or _web is None:
    skip("connect gegen ein Ziel mit Anmelde-Cookie", "aiohttp oder cryptography fehlt")
else:
    from cryptography import x509 as _x
    from cryptography.hazmat.primitives import hashes as _h, serialization as _s
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    from cryptography.x509.oid import NameOID as _N
    work = tempfile.mkdtemp(prefix="oaap-prop-e2e-")
    # das Serverzertifikat des Ziels (gepinnt)
    skey = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sname = _x.Name([_x.NameAttribute(_N.COMMON_NAME, "*.dummy.nodomain")])
    snow = datetime.datetime.now(datetime.timezone.utc)
    scert = (_x.CertificateBuilder().subject_name(sname).issuer_name(sname)
             .public_key(skey.public_key()).serial_number(_x.random_serial_number())
             .not_valid_before(snow - datetime.timedelta(minutes=5))
             .not_valid_after(snow + datetime.timedelta(days=1))
             .sign(skey, _h.SHA256()))
    spem = scert.public_bytes(_s.Encoding.PEM).decode()
    with open(os.path.join(work, "s.pem"), "wb") as f:
        f.write(scert.public_bytes(_s.Encoding.PEM) + skey.private_bytes(
            _s.Encoding.PEM, _s.PrivateFormat.PKCS8, _s.NoEncryption()))
    ca_dir2 = os.path.join(work, "ca")
    tca, _tk, _m = propagate.ensure_ca(ca_dir2, "T1", "pxx")
    with open(os.path.join(ca_dir2, "T1.crt"), encoding="ascii") as f:
        tca_pem = f.read()

    async def target(request):
        der = request.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        cn = _x.load_der_x509_certificate(der).subject.get_attributes_for_oid(
            _N.COMMON_NAME)[0].value
        r = _web.json_response({"cn": cn, "cookie": request.headers.get("Cookie", ""),
                                "authorization": request.headers.get("Authorization", ""),
                                "assertion": request.headers.get(assertion.HEADER, "")})
        r.set_cookie("MYSAPSSO2", f"ticket-for-{cn}")
        return r

    async def run_e2e():
        sctx = _ssl.create_default_context(_ssl.Purpose.CLIENT_AUTH)
        sctx.load_cert_chain(os.path.join(work, "s.pem"))
        sctx.verify_mode = _ssl.CERT_REQUIRED
        sctx.load_verify_locations(cadata=tca_pem)
        tapp = _web.Application()
        tapp.router.add_get("/{tail:.*}", target)
        runner = _web.AppRunner(tapp)
        await runner.setup()
        site_ = _web.TCPSite(runner, "127.0.0.1", 0, ssl_context=sctx)
        await site_.start()
        port = site_._server.sockets[0].getsockname()[1]
        apps = os.path.join(work, "apps")
        os.makedirs(apps)
        with open(os.path.join(apps, "tenants.json"), "w") as f:
            json.dump({"tenants": {"T1": {"label": "pxx"}}}, f)
        with open(os.path.join(apps, "destinations.json"), "w") as f:
            json.dump({"destinations": {"T1": {"a4h": {
                # a NAME, not 127.0.0.1: aiohttp's jar keeps no cookie of a
                # bare IP address, and a test on one would pass with the jar
                # switched back on (the bait said so)
                "kind": "http", "target": {"direct": f"https://localhost:{port}"},
                "auth": {"type": "principal_propagation", "credential": "x509",
                         "user": [{"rule": "claim", "name": "sap_user"}],
                         "name_check": "sap", "lifetime_minutes": 5,
                         "server_cert": spem}}}}}, f)
        with open(os.path.join(work, "akey"), "w") as f:
            f.write(KEY.hex())
        sec = os.path.join(work, "secrets")
        os.makedirs(sec)
        with open(os.path.join(sec, "gateway.key"), "w") as f:
            f.write("gw")
        svc = None
        os.environ.update(CONNECT_APPS=apps, CONNECT_CA=ca_dir2,
                          CONNECT_STATE=os.path.join(work, "state"),
                          CONNECT_SECRETS=sec,
                          CONNECT_ASSERTION_KEY=os.path.join(work, "akey"),
                          CONNECT_AUDIT=os.path.join(work, "audit.jsonl"))
        sys.modules.pop("app", None)
        sys.path.insert(0, CONNECT)
        svc = importlib.import_module("app")
        node = svc.Node()
        node.load()
        client = TestClient(TestServer(svc.make_app(node)))
        await client.start_server()
        seen = []
        for who, sap in (("dev", "ACCX_T_DEV"), ("mon", "ACCX_T_MON1"),
                         ("dev", "ACCX_T_DEV"), ("lock", "ACCX_T_LOCKE")):
            tok = assertion.mint(KEY, f"uuid-{who}", who, "T1", "probe", {"sap_user": sap})
            r = await client.get("/propagate/a4h/sap/bc/ui2/start_up", headers={
                "X-OAAP-Connect-Gateway": "gw", "X-OAAP-Connect-Caller": "probe",
                "X-OAAP-Connect-Destination": "T1/a4h", assertion.HEADER: tok,
                "Cookie": "MYSAPSSO2=von-der-app", "Authorization": "Basic eA=="})
            body = await r.json() if r.status == 200 else {}
            seen.append((sap, r.status, body, r.headers.get("Set-Cookie", "")))
        if node.prop_http:
            await node.prop_http.close()
        await client.close()
        await runner.cleanup()
        return seen

    seen = asyncio.run(run_e2e())
    ok("jede Person kommt mit ihrem eigenen Zertifikat an, auch im Wechsel",
       [b.get("cn") for _s2, _st, b, _c in seen] == [s2 for s2, *_ in seen], seen)
    ok("das Ziel sieht nie ein Cookie -- weder das der App noch ein Ticket eines Vorgaengers",
       all(b.get("cookie") == "" for _s2, _st, b, _c in seen), seen)
    ok("... und keine Authorization der App",
       all(b.get("authorization") == "" for _s2, _st, b, _c in seen))
    ok("... und keine Zusicherung (sie gehoert dem Knoten)",
       all(b.get("assertion") == "" for _s2, _st, b, _c in seen))
    ok("das Ticket des Ziels geht nicht an die App zurueck",
       all(c == "" for *_r, c in seen), [c for *_r, c in seen])

print("")
print(f"{'OK' if not fails else 'FEHLER'}: {fails} fehlgeschlagen, {skips} uebersprungen")
sys.exit(1 if fails else 0)
