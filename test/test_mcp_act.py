"""Verfahren I an der Tuer eines MCP-Gateways (RFC-0073 Stufe 2, 0.1.238).

    python3 test/test_mcp_act.py

Was hier gehalten wird (jede Zeile hat einen Koeder):

A. act.py prueft ein Token vollstaendig: Unterschrift (RS256, >= 2048 Bit),
   Issuer, Audience, Ablauf, sub. `none`, HS256, verfaelscht, fremder
   Issuer, an das Gateway adressiert, abgelaufen: abgelehnt. Eine unbekannte
   kid holt die Schluessel hoechstens einmal je Minute neu.
B. /verify?act=oaap.mcp.sources:<gw>&instance=<quelle> (identity):
   - beide Nachweise noetig: der Quellen-Schluessel GENAU dieses Gateways
     (kein unbeschraenkter Schluessel, keiner eines anderen Gateways) UND
     das Act-Token;
   - die Person kommt nur aus der Bindung {Anbieter, sub} im Mandanten;
     ohne Bindung: der Satz aus §5, nichts angelegt;
   - die Route der Quelle gilt fuer die Person (Rolle fehlt: 403);
   - Gateway und Quelle im selben Mandanten;
   - die Antwort: die sechs Kopfzeilen der PERSON, die Zusicherung an die
     Quelle adressiert;
   - jeder Akt steht im Mandantenprotokoll, ohne das Token;
   - ein Act-Token allein an einer gewoehnlichen Route oeffnet nichts.
"""
import importlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "platform", "services")
IDENTITY_DIR = os.path.join(SERVICES, "identity")
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import act                                                    # noqa: E402
import rsa_test_keys as rk                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


ISS = "http://kc.example:8113/realms/vp"
PLATFORM_CLIENT = "oaap-vp"
GW_CLIENT = "mcp-gw-vp"
print("Schluessel erzeugen (reines Python, ein paar Sekunden) ...")
KEY = rk.keypair()
KEY2 = rk.keypair()                       # ein fremder Schluessel derselben kid
SHORT = rk.keypair(1024)
JWKS = {"keys": [rk.jwk(KEY[0], KEY[1], "k1"), rk.jwk(SHORT[0], SHORT[1], "kurz")]}


def tok(sub="sub-anna", aud=PLATFORM_CLIENT, iss=ISS, key=KEY, kid="k1", alg="RS256", **extra):
    return rk.token(key, rk.claims(iss, aud, sub=sub, **extra), kid=kid, alg=alg)


# ---------------------------------------------------------------- A. act.py
print("")
print("A. act.py")
FETCHES = []


def fetch():
    FETCHES.append(1)
    return JWKS


now = [1000.0]
keys = act.Keys(fetch, clock=lambda: now[0])


def refused(token, audience=PLATFORM_CLIENT, issuer=ISS):
    try:
        act.check(token, keys, issuer, audience)
        return ""
    except act.Invalid as e:
        return str(e) or "?"


ok("ein gutes Token geht durch und nennt sub",
   act.check(tok(), keys, ISS, PLATFORM_CLIENT)["sub"] == "sub-anna")
ok("aud als Liste mit dem Plattform-Client geht durch",
   not refused(tok(aud=[GW_CLIENT, PLATFORM_CLIENT])))
for label, t in (("an das Gateway adressiert", tok(aud=GW_CLIENT)),
                 ("fremder Issuer", tok(iss="http://kc.example:8113/realms/anders")),
                 ("abgelaufen", tok(lifetime=-120)),
                 ("anderer Schluessel mit derselben kid", tok(key=KEY2)),
                 ("ohne sub", tok(sub=""))):
    r = refused(t)
    ok(f"abgelehnt: {label}", bool(r), r)
    ok(f"… und der Grund enthaelt das Token nicht ({label})", t not in r and t.split(".")[1] not in r)
h, b, s = tok().split(".")
claims = json.loads(rk.base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
claims["sub"] = "sub-chef"
forged = h + "." + rk.b64(json.dumps(claims).encode()) + "." + s
ok("abgelehnt: verfaelschter Koerper (sub ausgetauscht)", "signature" in refused(forged), refused(forged))
none_tok = rk.b64(b'{"alg":"none","kid":"k1"}') + "." + b + "."
ok("abgelehnt: alg none", "algorithm" in refused(none_tok), refused(none_tok))
ok("abgelehnt: HS256", "algorithm" in refused(tok(alg="HS256")), refused(tok(alg="HS256")))
ok("abgelehnt: Schluessel kuerzer als 2048 Bit", "2048" in refused(tok(key=SHORT, kid="kurz")))
ok("abgelehnt: kein JWT", refused("abc") == "not a JWT")
now[0] += 61
n0 = len(FETCHES)
r1, r2 = refused(tok(kid="neu")), refused(tok(kid="neu2"))
ok("eine unbekannte kid holt neu, die naechste in derselben Minute nicht",
   len(FETCHES) == n0 + 1 and r1 and r2, (len(FETCHES) - n0, r1, r2))
now[0] += 61
refused(tok(kid="neu3"))
ok("… nach einer Minute wieder", len(FETCHES) == n0 + 2)
ok("gateway_of: nur der Scope der Quellen, mit gueltigem Namen",
   [act.gateway_of(x) for x in ("oaap.mcp.sources:gw", "oaap.cases.submit:gw",
                                "oaap.mcp.sources:", "oaap.mcp.sources:a b")]
   == ["gw", "", "", ""])

# ------------------------------------------------------------- B. identity
print("")
print("B. /verify?act=... (identity)")
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    sys.exit(1 if fails else 0)

DATA = tempfile.mkdtemp(prefix="oaap-mcp-act-test-")
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"
os.environ["OAAP_IDENTITY_DATA_DIR"] = DATA
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
m.USERS_FILE = os.path.join(DATA, "users.json")
m.KEYS_FILE = os.path.join(DATA, "api-keys.json")
m.THROTTLE_FILE = os.path.join(DATA, "login-throttle.json")
m.AUDIT_LOG = os.path.join(DATA, "audit.jsonl")
m.TENANTS_FILE = os.path.join(DATA, "tenants.json")
m.ASSERTION_KEY_FILE = os.path.join(DATA, "assertion.key")
with open(m.ASSERTION_KEY_FILE, "w", encoding="ascii") as f:
    f.write("ab" * 32)
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {
        "t-vp": {"label": "vp", "idp": {"kind": "oidc", "issuer": ISS, "client_id": PLATFORM_CLIENT}},
        "t-sgl": {"label": "sgl", "idp": {"kind": "oidc", "issuer": ISS, "client_id": PLATFORM_CLIENT}},
        "t-ohne": {"label": "ohne"}}}, f)
PKEY = "oidc|" + ISS


def user(uid, name, roles, tenant, kind="human", active=True, sub=""):
    u = {"id": uid, "username": name, "display_name": name.title(), "password_hash": "",
         "kind": kind, "roles": roles, "groups": [], "tenant": tenant, "active": active,
         "session_epoch": 0, "email": f"{name}@vp.invalid", "email_verified": True}
    if sub:
        u["idp"] = {"provider": PKEY, "subject": sub}
    return u


USERS = [
    user("u-anna", "vp-anna", ["user"], "t-vp", sub="sub-anna"),
    user("u-bert", "vp-bert", ["user"], "t-vp", sub="sub-bert"),
    user("u-gone", "vp-gone", ["user"], "t-vp", sub="sub-gone", active=False),
    user("u-max", "sgl-max", ["user"], "t-sgl", sub="sub-max"),
    user("u-chef", "vp-chef", ["tenant_admin", "user", "admin"], "t-vp"),
    user("u-gw", "instance:gw", ["user"], "t-vp", "machine"),
    user("u-gw2", "instance:gw2", ["user"], "t-vp", "machine"),
    user("u-gws", "instance:gw-sgl", ["user"], "t-sgl", "machine"),
]
with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(USERS, f)


def key_for(principal, scope, roles=("user",)):
    _rec, secret = m.issue_key(m.load_users(), principal, list(roles), scope,
                               "test", 30, "root")
    return secret


GW_KEY = key_for("instance:gw", "oaap.mcp.sources:gw")
GW2_KEY = key_for("instance:gw2", "oaap.mcp.sources:gw2")
GWS_KEY = key_for("instance:gw-sgl", "oaap.mcp.sources:gw-sgl")
CASES_KEY = key_for("instance:gw", "oaap.cases.submit:gw")
CHEF_FREE = key_for("vp-chef", "", roles=("user", "admin"))
GW_FREE = key_for("instance:gw", "")
m.ACT_KEYS[ISS] = act.Keys(lambda: JWKS)
c = m.app.test_client()


def door(token=None, key=GW_KEY, gw="gw", target="kp", tenant="t-vp", roles="user", extra=None):
    h = {}
    if key:
        h["Authorization"] = "Bearer " + key
    if token:
        h[act.HEADER] = token
    h.update(extra or {})
    q = f"/verify?roles={roles}&tenant={tenant}&instance={target}&act=oaap.mcp.sources:{gw}"
    return c.get(q, headers=h)


def text(r):
    return r.get_data(as_text=True)


n_users = len(m.load_users())
r = door(tok())
ok("anna mit gutem Token und dem Schluessel des Gateways: 204", r.status_code == 204, (r.status_code, text(r)))
ok("… die Kopfzeilen sind die von anna (Name, Kennung, Rollen, E-Mail)",
   r.headers.get("X-OAAP-User") == "vp-anna" and r.headers.get("X-OAAP-User-Id") == "u-anna"
   and r.headers.get("X-OAAP-Roles") == "user" and r.headers.get("X-OAAP-Email") == "vp-anna@vp.invalid",
   dict(r.headers))
asrt = r.headers.get("X-OAAP-User-Assertion", "")
ok("… mit Zusicherung, adressiert an die Quelle kp",
   bool(asrt) and m.assertion.check(bytes.fromhex("ab" * 32), asrt, "kp")[0] is not None,
   asrt[:40])
r = door(tok(sub="sub-bert"))
ok("bert: 204 mit seinen eigenen Kopfzeilen", r.status_code == 204 and r.headers.get("X-OAAP-User") == "vp-bert")
r = door(tok(sub="sub-bert"), roles="tenant_admin")
ok("bert an einer Quelle, deren Route tenant_admin verlangt: 403 von der Route",
   r.status_code == 403 and "role" in text(r), (r.status_code, text(r)))
r = door(tok(sub="sub-niemand"))
ok("gueltiges Token ohne Bindung: 403 mit dem Satz aus §5",
   r.status_code == 403 and "noch nie angemeldet" in text(r), (r.status_code, text(r)))
ok("… und es wurde niemand angelegt", len(m.load_users()) == n_users)
r = door(tok(sub="sub-gone"))
ok("eine gesperrte Person: 403", r.status_code == 403, (r.status_code, text(r)))
r = door(tok(sub="sub-max"))
ok("die Bindung zaehlt nur im Mandanten der Quelle (max ist in sgl): §5-Satz",
   r.status_code == 403 and "noch nie angemeldet" in text(r), (r.status_code, text(r)))

for label, t in (("an das Gateway adressiert", tok(aud=GW_CLIENT)),
                 ("abgelaufen", tok(lifetime=-120)), ("verfaelscht", forged),
                 ("fremder Issuer", tok(iss="http://kc.example:8113/realms/anders")),
                 ("HS256", tok(alg="HS256"))):
    r = door(t)
    ok(f"Act-Token {label}: 401", r.status_code == 401, (r.status_code, text(r)))
r = door(None)
ok("ohne Act-Token: 401", r.status_code == 401 and "act token" in text(r), (r.status_code, text(r)))
r = door(tok(), key=None)
ok("ohne Schluessel des Gateways: 401", r.status_code == 401, (r.status_code, text(r)))
r = door(tok(), key=CHEF_FREE)
ok("ein UNBESCHRAENKTER Schluessel (tenant_admin) oeffnet die Tuer nicht: 403",
   r.status_code == 403, (r.status_code, text(r)))
r = door(tok(), key=GW_FREE)
ok("ein unbeschraenkter Schluessel DESSELBEN Prinzipals instance:gw: 403",
   r.status_code == 403, (r.status_code, text(r)))
r = door(tok(), key=GW2_KEY)
ok("der Schluessel eines ANDEREN Gateways an dieser Tuer: 403", r.status_code == 403, (r.status_code, text(r)))
r = door(tok(), key=CASES_KEY)
ok("der Vorgangs-Schluessel desselben Gateways: 403", r.status_code == 403, (r.status_code, text(r)))
r = door(tok(), key=GWS_KEY, gw="gw-sgl")
ok("ein Gateway aus sgl an einer Quelle in vp: 403 (Mandanten verschieden)",
   r.status_code == 403 and "tenant" in text(r), (r.status_code, text(r)))
r = door(tok(), tenant="t-ohne")
ok("Quelle in einem Mandanten ohne Anbieter: 403", r.status_code == 403, (r.status_code, text(r)))
r = c.get("/verify?roles=user&tenant=t-vp&instance=oaap.mcp.sources:gw&act=oaap.cases.submit:gw",
          headers={"Authorization": "Bearer " + GW_KEY, act.HEADER: tok()})
ok("act mit einem Scope, der keine MCP-Tuer ist: 403", r.status_code == 403, (r.status_code, text(r)))
r = c.get("/verify?roles=user&tenant=t-vp&instance=kp", headers={act.HEADER: tok()})
ok("ein Act-Token allein an einer gewoehnlichen Route oeffnet nichts",
   r.status_code in (303, 401) and not r.headers.get("X-OAAP-User"), (r.status_code, dict(r.headers)))

with open(m.AUDIT_LOG, encoding="utf-8") as f:
    log = [json.loads(ln) for ln in f]
acts = [e for e in log if e["action"] == "mcp.act"]
ok("jeder Akt steht im Mandantenprotokoll: Quelle, Gateway, Person",
   any(e["result"] == "ok" and e["subject"] == "kp" and e["who"] == "instance:gw"
       and "vp-anna" in e.get("detail", "") for e in acts), acts[:2])
ok("… auch die Ablehnungen (Token, Bindung)",
   any(e["result"] == "denied" and "act token" in e.get("detail", "") for e in acts)
   and any(e["result"] == "denied" and "binding" in e.get("detail", "") for e in acts))
ok("… und die an der Route der Quelle (bert ohne tenant_admin)",
   any(e["result"] == "denied" and "vp-bert" in e.get("detail", "") and "missing role" in e.get("detail", "")
       for e in acts), [e for e in acts if "vp-bert" in e.get("detail", "")])
raw = open(m.AUDIT_LOG, encoding="utf-8").read()
ok("… und kein Token steht darin", tok().split(".")[0] not in raw and GW_KEY not in raw)

print("")
print("FEHLER: %d" % fails if fails else "Alles gruen.")
sys.exit(1 if fails else 0)
