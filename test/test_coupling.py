#!/usr/bin/env python3
"""Kopplung -- eine Übergabe für jeden Schlüssel (RFC-0136 Stufe 1: Tor-Zugänge).

Festgehalten wird, was die Kopplung verspricht -- jede Regel mit Gegenprobe:

- das Ticket: eine Zeile `OAAP-COUPLE1.<base64url>` = dieselbe JSON-Datei;
  ein Zeilenumbruch schadet nicht; pin (curl-Form, über den öffentlichen
  Schlüssel) und ca nur für eine nicht öffentlich vertraute Adresse;
- Ausstellen (Portal über den Spool, und die Kommandozeile): zum Knoten und zu
  identity gehen nur die SHA-256 von Code und Link; der Prinzipal entsteht
  inaktiv, ein Schlüssel noch nicht (D4); im Protokoll `coupling.issue`;
- Einlösen an der Tür: 200 mit Schlüssel und SM59-Angaben genau einmal, dann
  410; ein unbekannter Code 404 (und zählt für die Bremse); ein Code eines
  anderen Mandanten 404; nach Ablauf und nach Zurückziehen 410; der Schlüssel
  gilt danach am Tor (Basic wie SM59), sonst nirgends;
- der Browser-Link: GET und HEAD zeigen nur die Seite (eine Linkvorschau
  verbraucht nichts), erst der POST löst ein, ein zweiter Klick 410;
- die Liste: identity zeigt jedem Admin nur seinen Mandanten, nie einen Hash;
- das Gateway: /platform/couple/* geht an identity mit dem Mandanten in einer
  Kopfzeile, die ein Client nicht setzen kann;
- das Portal: zwei Knöpfe, die Ticketseite, die Seite „Kopplungen“.

Braucht flask/werkzeug (identity echt), kein Docker. Aufruf:
python3 test/test_coupling.py
"""
import argparse
import ast
import base64
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import time
import types

from template_src import resolve  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
PORTAL = os.path.join(SERVICES, "portal")
DATA = tempfile.mkdtemp(prefix="oaap-coupling-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)
sys.path.insert(0, SERVICES)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


import coupling as cp  # noqa: E402

print("=== das Ticket ===")
FX_PEM = """-----BEGIN CERTIFICATE-----
MIIBhjCCAS2gAwIBAgIUCsmUdkQastHx90mSSBbIFV4H+lMwCgYIKoZIzj0EAwIw
GTEXMBUGA1UEAwwOY291cGxlLWZpeHR1cmUwHhcNMjYxMTI2MjAzMTA1WhcNMzYx
MTIzMjAzMTA1WjAZMRcwFQYDVQQDDA5jb3VwbGUtZml4dHVyZTBZMBMGByqGSM49
AgEGCCqGSM49AwEHA0IABPeH8s7o54i8UrF24brQriAj47yQARFlMIkwa14un8Wc
Mxxr6QI7Z9YMN+vm+1Ubk6ZOPn2DtjSwAVN2aHb4xt2jUzBRMB0GA1UdDgQWBBQs
U//ADrWQ9uThlrDD95dyJn5AMTAfBgNVHSMEGDAWgBQsU//ADrWQ9uThlrDD95dy
Jn5AMTAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49BAMCA0cAMEQCIG3EHQupchNf
hSgAQkps88xq1PHYOSSUo4jMNXWL+7glAiB46YW4R+pr2q6N5T6qxjA83I8DymFy
6UO+3OzUvUSxOQ==
-----END CERTIFICATE-----
"""
import ssl  # noqa: E402

FX_DER = ssl.PEM_cert_to_DER_cert(FX_PEM)
# openssl x509 -pubkey | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64
ok("pin über den öffentlichen Schlüssel, in der Form von curl --pinnedpubkey (wie openssl)",
   cp.pin_of(FX_DER) == "sha256//upyM6AOnTFJ4ERbQflNKIwJdwYZDysyMVHhzoyrjl4w=", cp.pin_of(FX_DER))
tr = cp.trust_of(FX_DER, False)
ok("nicht öffentlich vertraut: pin und ca (PEM)", tr["pin"].startswith("sha256//")
   and ssl.PEM_cert_to_DER_cert(tr["ca"]) == FX_DER, tr)
ok("öffentlich vertraut: nichts davon", cp.trust_of(FX_DER, True) == {})
t0 = {"v": 1, "kind": "gate-access", "code": cp.new_code(), "label": "SAP A4H 001 – Ü"}
line = cp.encode(t0)
wrapped = "\n".join(line[i:i + 60] for i in range(0, len(line), 60)) + "\n"
ok("Zeile und zurück; ein Zeilenumbruch (Mail) schadet nicht; die Datei ist dasselbe",
   line.startswith("OAAP-COUPLE1.") and cp.decode(line) == t0 and cp.decode(wrapped) == t0
   and cp.decode(json.dumps(t0)) == t0)
for bad in ("OAAP-PAIR1.abc", "OAAP-COUPLE1.!!!", cp.encode({"v": 2})):
    try:
        cp.decode(bad)
        ok(f"abgelehnt: {bad[:20]}", False)
    except ValueError:
        ok(f"abgelehnt: {bad[:20]}", True)
ok("Code: 32 Zufallsbytes mit Kennung; Link: eine UUID",
   cp.CODE_RE.match(cp.new_code()) and cp.LINK_RE.match(cp.new_link())
   and cp.new_code() != cp.new_code())
ok("Fenster: 15 Minuten vorgegeben, höchstens 7 Tage (D3)", cp.window_minutes(None) == 15
   and cp.window_minutes("10080") == 10080)
for bad in ("0", "10081", "x"):
    try:
        cp.window_minutes(bad)
        ok(f"Fenster {bad} abgelehnt", False)
    except ValueError:
        ok(f"Fenster {bad} abgelehnt", True)
ok("Zustand: offen, abgelaufen, eingelöst",
   cp.state({"until": "2026-11-26T10:00:00Z"}, "2026-11-26T09:00:00Z") == "open"
   and cp.state({"until": "2026-11-26T10:00:00Z"}, "2026-11-26T10:00:00Z") == "expired"
   and cp.state({"until": "2099-01-01T00:00:00Z", "state": "redeemed"}, "x") == "redeemed")
old = {"until": "2026-01-01T00:00:00Z", "state": "open"}
ok("nach 30 Tagen fällt ein beendetes Ticket aus der Liste, ein offenes nicht",
   cp.prune([old, {"until": "2099-01-01T00:00:00Z"}], 1795000000) == [{"until": "2099-01-01T00:00:00Z"}])

try:
    import flask  # noqa: F401
    from werkzeug.security import generate_password_hash  # noqa: F401
except ImportError:
    print("SKIP  flask/werkzeug fehlen.")
    sys.exit(1)

# --- appctl + identity, echt (wie test_gates_portal)
import appctl as m  # noqa: E402

m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
m.write_external_caddy = lambda *a, **kw: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
KUNDE, _ = m.tenant_create("kunde")
ANDERE, _ = m.tenant_create("andere")
ID_DIR = os.path.join(DATA, "data", "identity")
os.makedirs(ID_DIR, exist_ok=True)
for k, v in {"SESSION_SECRET": "s", "SETUP_TOKEN": "t", "INTERNAL_API_KEY": "i",
             "OAAP_IDENTITY_DATA_DIR": ID_DIR}.items():
    os.environ[k] = v


def user(name, roles, tenant=""):
    return {"username": name, "roles": roles, "tenant": tenant, "groups": [], "active": True,
            "password_hash": ""}


json.dump([user("root", ["server_admin"]), user("karin", ["tenant_admin"], KUNDE),
           user("uwe", ["user"], KUNDE), user("anna", ["tenant_admin"], ANDERE)],
          open(os.path.join(ID_DIR, "users.json"), "w"))
json.dump({"setup_done": True, "server_admin_migrated": True, "support_migrated": True,
           "tenant_migrated": True}, open(os.path.join(ID_DIR, "state.json"), "w"))
IDENTITY_DIR = os.path.join(SERVICES, "identity")
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
ident = importlib.reload(importlib.import_module("app"))
sys.path.remove(IDENTITY_DIR)
sys.modules.pop("app", None)
ident.TENANTS_FILE = m.TENANTS_FILE
ident.GATES_FILE = m.GATES_FILE
ident.AUDIT_LOG = m.TENANT_LOG
ident.THROTTLE_FILE = os.path.join(ID_DIR, "throttle.json")
FAILED = []
ident._login_blocked = lambda b: b == "couple|9.9.9.9"
ident._login_failed = lambda b: FAILED.append(b)


def fake_identity_exec(script, env=None):
    old_env = dict(os.environ)
    os.environ.update(env or {})
    sys.modules["app"] = ident
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(script, "<identity>", "exec"), {"__name__": "__identity__"})
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.modules.pop("app", None)
    return out.getvalue()


m._identity_exec = fake_identity_exec
m.gate_url = lambda tid, n: f"https://kunde.example/gate/{n}" if tid == KUNDE else ""
TRUST = {}
m.couple_trust = lambda host: dict(TRUST)
reg = m.load_registry()
reg["instances"]["kunde-vorgaenge"] = {"app_id": "vorgaenge", "tenant": KUNDE, "channel": "production",
                                       "container": "oaap-app-kunde-vorgaenge", "svc_port": 8000}
m.save_registry(reg)
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(by, op, **args):
    _rid[0] += 1
    rid = f"c{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    body = {"id": rid, "instance": "", "action": "gate-admin", "by": by, "op": op, "args": args}
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def log(tid, action):
    return [e for e in m.read_tenant_log(tid, limit=500) if e["action"] == action]


cl = ident.app.test_client()


def redeem(code, tenant=KUNDE, client="10.0.0.7", form=False):
    h = {"X-OAAP-Client": client}
    if tenant:
        h["X-OAAP-Couple-Tenant"] = tenant
    if form:
        return cl.post("/couple-door/redeem", data={"code": code}, headers=h)
    return cl.post("/couple-door/redeem", json={"code": code}, headers=h)


def basic_ok(key, user_="sap-support"):
    hdr = "Basic " + base64.b64encode(f"{user_}:{key}".encode()).decode()
    with ident.app.test_request_context(headers={"Authorization": hdr}):
        return ident.gate_access(KUNDE, "sap-support") is not None


def couple(by="karin", window=None, label="SAP A4H 001", **kw):
    code, link = cp.new_code(), cp.new_link()
    a = dict(tenant=KUNDE, name="sap-support", label=label, days="30",
             code_hash=cp.digest(code), link_hash=cp.digest(link), **kw)
    if window:
        a["window"] = window
    return queue(by, "couple", **a), code, link


queue("karin", "process", tenant=KUNDE, name="sap-anfrage", queue="sap-support")
queue("karin", "add", tenant=KUNDE, name="sap-support", process="sap-anfrage")

print("")
print("=== ausstellen (Portal über den Spool) ===")
r, code, link = couple()
d = r.get("detail") or {}
ok("eine Kopplung wird ausgestellt: Kennung, Prinzipal, Fenster, Tür und Link",
   r["ok"] and len(d.get("id", "")) == 8 and d.get("principal") == "gate-sap-support-1"
   and d.get("redeem") == "https://kunde.example/platform/couple/redeem"
   and d.get("browser") == "https://kunde.example/platform/couple/"
   and d.get("key_days") == 30 and d.get("user") == "sap-support", r)
ok("... die Antwort ist ein deutscher Satz", r["message"] == "Kopplung für „SAP A4H 001“ erzeugt.", r)
ok("Code und Link stehen nicht im Ergebnis, nicht im Protokoll, nicht in der Ticketdatei",
   code not in json.dumps(r) and link not in json.dumps(r)
   and code not in json.dumps(m.read_tenant_log(KUNDE, limit=500))
   and code not in open(ident.COUPLINGS_FILE).read() and link not in open(ident.COUPLINGS_FILE).read())
u1 = ident.find_user(ident.load_users(), "gate-sap-support-1")
ok("der Prinzipal entsteht inaktiv, ein Schlüssel noch nicht (D4)",
   u1 and u1["active"] is False and u1["kind"] == "machine"
   and not [k for k in ident.load_keys() if k["principal"] == "gate-sap-support-1"], u1)
ok("im Protokoll: coupling.issue mit der Person", any(e["who"] == "karin" for e in log(KUNDE, "coupling.issue")),
   log(KUNDE, "coupling.issue"))
t = cp.gate_ticket(d, code, link)
ok("das Ticket: Adresse, Tür, Code, Ping, Vorlage, Browser-Link; ohne pin für eine öffentliche Adresse",
   t["address"] == "https://kunde.example/gate/sap-support" and t["code"] == code
   and t["links"]["template"].endswith("/gate/sap-support/template?form=placeholders")
   and t["links"]["browser"] == "https://kunde.example/platform/couple/" + link
   and "pin" not in t and "ca" not in t, t)

r2 = queue("anna", "couple", tenant=KUNDE, name="sap-support", label="fremd",
           code_hash=cp.digest(cp.new_code()), link_hash=cp.digest(cp.new_link()))
ok("der Admin eines anderen Mandanten koppelt nicht in „kunde“", not r2["ok"]
   and not any(c.get("label") == "fremd" for c in ident.load_couplings()), r2)
r2 = queue("uwe", "couple", tenant=KUNDE, name="sap-support", label="x",
           code_hash=cp.digest("a"), link_hash=cp.digest("b"))
ok("ein Benutzer auch nicht", not r2["ok"], r2)
r2 = queue("karin", "couple", tenant=KUNDE, name="sap-support", label="x", code_hash="klartext",
           link_hash=cp.digest("b"))
ok("ohne echte Hashes: abgewiesen (das Portal macht Code und Link)", not r2["ok"]
   and r2["message"] == "Das Portal schickt die Hashes von Code und Link.", r2)
r2 = queue("karin", "couple", tenant=KUNDE, name="gibtsnicht", label="x",
           code_hash=cp.digest("a"), link_hash=cp.digest("b"))
ok("ein fehlendes Tor: deutscher Satz", not r2["ok"] and r2["message"].startswith("Kein Tor"), r2)
r2, _c, _l = couple(window="20000")
ok("ein Fenster über 7 Tage: deutscher Satz", not r2["ok"] and r2["message"] == "Fenster: 1 Minute bis 7 Tage.", r2)

print("")
print("=== einlösen (Programm) ===")
FAILED.clear()
res = redeem(code)
j = res.get_json() or {}
ok("200: der Schlüssel, einmal, mit Adresse, Benutzer, Ablauf und SM59",
   res.status_code == 200 and j.get("key", "").startswith("oaapk_") and j.get("user") == "sap-support"
   and j.get("address") == "https://kunde.example/gate/sap-support" and j.get("expires")
   and j.get("sm59") == {"host": "kunde.example", "port": "443", "path": "/gate/sap-support",
                         "user": "sap-support", "logon": "basic"}
   and res.headers.get("Cache-Control") == "no-store", (res.status_code, j))
ok("... der Schlüssel gilt am Tor (Basic wie SM59)", basic_ok(j.get("key", "")))
krec = next((k for k in ident.load_keys() if k["principal"] == "gate-sap-support-1"), {})
ok("... auf genau dieses Tor begrenzt, von der Kopplung erzeugt, 30 Tage",
   krec.get("instance") == f"oaap.gate:{KUNDE}/sap-support" and krec.get("created_by") == f"coupling:{d['id']}",
   krec)
ok("... der Prinzipal ist jetzt aktiv", ident.find_user(ident.load_users(), "gate-sap-support-1")["active"])
rec = next(c for c in ident.load_couplings() if c["id"] == d["id"])
ok("... das Ticket ist eingelöst: wann, von welcher Adresse, welcher Schlüssel, welcher Weg",
   rec["state"] == "redeemed" and rec["redeemed_from"] == "10.0.0.7" and rec["key_id"] == krec.get("id")
   and rec["via"] == "code", rec)
ok("... im Protokoll coupling.redeem, ohne den Schlüssel",
   log(KUNDE, "coupling.redeem") and j["key"] not in json.dumps(m.read_tenant_log(KUNDE, limit=500)))
res = redeem(code)
ok("derselbe Code noch einmal: 410 mit dem einen Satz", res.status_code == 410
   and res.get_json()["error"] == cp.GONE, res.get_json())
ok("... und das steht als coupling.refused im Protokoll (ohne den Code)",
   log(KUNDE, "coupling.refused") and code not in json.dumps(log(KUNDE, "coupling.refused")))
ok("eingelöst zählt nicht für die Bremse", FAILED == [], FAILED)
res = redeem(cp.new_code())
ok("unbekannter Code: 404, ein kurzer Satz -- und er zählt für die Bremse",
   res.status_code == 404 and res.get_json()["error"] == cp.UNKNOWN and FAILED == ["couple|10.0.0.7"], FAILED)
res = redeem("kein-code")
ok("ein Code in falscher Form: 404 wie unbekannt", res.status_code == 404
   and res.get_json()["error"] == cp.UNKNOWN)
res = redeem(cp.new_code(), client="9.9.9.9")
ok("gebremste Adresse: 429", res.status_code == 429)
res = redeem(code, tenant="")
ok("ohne Mandant vom Gateway: 404", res.status_code == 404)

r, code2, link2 = couple(label="Formular")
res = redeem(code2, tenant=ANDERE)
ok("der Code eines anderen Mandanten: 404 -- er existiert nur dort, wo er ausgestellt wurde",
   res.status_code == 404)
res = redeem(code2, form=True)
ok("... beim richtigen Mandanten als Formularfeld: 200", res.status_code == 200
   and (res.get_json() or {}).get("key", "").startswith("oaapk_"))
ok("ein zweiter Zugang bekommt den nächsten Prinzipal",
   (r.get("detail") or {}).get("principal") == "gate-sap-support-2", r)

print("")
print("=== der Browser-Link (Mensch, SAP) ===")
r, code3, link3 = couple(label="SAP Q4H 100")
H = {"X-OAAP-Couple-Tenant": KUNDE, "X-OAAP-Client": "10.0.0.8"}
g1 = cl.get(f"/couple-door/{link3}", headers=H)
page1 = g1.get_data(as_text=True)
ok("GET zeigt die Seite mit dem Knopf -- und keinen Schlüssel", g1.status_code == 200
   and "Schlüssel jetzt erzeugen" in page1 and "SAP Q4H 100" in page1 and "oaapk_" not in page1
   and g1.headers.get("Cache-Control") == "no-store", page1[:300])
hd = cl.head(f"/couple-door/{link3}", headers=H)
ok("HEAD auch", hd.status_code == 200)
st = next(c for c in ident.load_couplings() if c["id"] == r["detail"]["id"])
ok("eine Linkvorschau (GET, HEAD) verbraucht nichts", cp.state(st, ident._now_iso()) == "open", st)
p1 = cl.post(f"/couple-door/{link3}", headers=H)
pt = p1.get_data(as_text=True)
ok("der Klick (POST): der Schlüssel einmal, mit SM59", p1.status_code == 200 and "oaapk_" in pt
   and "kunde.example" in pt and "/gate/sap-support" in pt and "Basis-Authentifizierung" in pt, pt[:400])
ok("... der Weg heißt Link", next(c for c in ident.load_couplings()
                                  if c["id"] == r["detail"]["id"])["via"] == "link")
ok("ein zweiter Klick: 410", cl.post(f"/couple-door/{link3}", headers=H).status_code == 410)
ok("... und die Seite danach auch", cl.get(f"/couple-door/{link3}", headers=H).status_code == 410)
ok("... auch der Code dieses Tickets gilt nicht mehr", redeem(code3).status_code == 410)
FAILED.clear()
g2 = cl.get(f"/couple-door/{cp.new_link()}", headers=H)
ok("ein unbekannter Link: 404, und er zählt", g2.status_code == 404 and FAILED == ["couple|10.0.0.8"])
ok("kein Link in UUID-Form: 404", cl.get("/couple-door/redeem", headers=H).status_code == 404)

print("")
print("=== Ablauf und Zurückziehen ===")
r, code4, _l4 = couple(window="15")
recs = ident.load_couplings()
for c in recs:
    if c["id"] == r["detail"]["id"]:
        c["until"] = cp.iso(time.time() - 60)
ident._save(ident.COUPLINGS_FILE, recs)
ok("abgelaufen: 410", redeem(code4).status_code == 410)
r, code5, _l5 = couple()
cid = r["detail"]["id"]
w = queue("karin", "withdraw", tenant=KUNDE, name="sap-support", coupling=cid)
ok("zurückziehen: ein deutscher Satz", w["ok"] and w["message"].startswith("Kopplung zurückgezogen"), w)
ok("... danach 410", redeem(code5).status_code == 410)
ok("... der Prinzipal bleibt inaktiv und ohne Schlüssel",
   not ident.find_user(ident.load_users(), r["detail"]["principal"])["active"]
   and not [k for k in ident.load_keys() if k["principal"] == r["detail"]["principal"]])
ok("... im Protokoll coupling.withdraw", any(e["who"] == "karin" for e in log(KUNDE, "coupling.withdraw")))
w = queue("karin", "withdraw", tenant=KUNDE, name="sap-support", coupling=cid)
ok("noch einmal: nicht mehr offen, auf Deutsch", not w["ok"]
   and w["message"] == f"Die Kopplung „{cid}“ ist nicht mehr offen (zurückgezogen).", w)
w = queue("anna", "withdraw", tenant=KUNDE, name="sap-support", coupling=cid)
ok("der Admin eines anderen Mandanten zieht nichts zurück", not w["ok"], w)
old_p = r["detail"]["principal"]
recs = ident.load_couplings()
for c in recs:
    if c["id"] == cid:
        c["withdrawn"] = c["until"] = cp.iso(time.time() - 40 * 86400)
ident._save(ident.COUPLINGS_FILE, recs)
r6, _c6, _l6 = couple(label="nach 40 Tagen")
# the freed name may be taken again: the old principal never held a key, and
# the log tells the two apart by ticket and key id
nu = ident.find_user(ident.load_users(), old_p)
ok("nach 30 Tagen fällt das zurückgezogene Ticket weg -- und sein Prinzipal, der nie einen Schlüssel hatte",
   r6["ok"] and not any(c["id"] == cid for c in ident.load_couplings())
   and (nu is None or (nu["display_name"] == "nach 40 Tagen"
                       and r6["detail"]["principal"] == old_p))
   and sum(1 for u_ in ident.load_users() if u_["username"] == old_p) <= 1
   and ident.find_user(ident.load_users(), "gate-sap-support-1"), (old_p, nu, r6.get("detail")))
queue("karin", "withdraw", tenant=KUNDE, name="sap-support", coupling=r6["detail"]["id"])

print("")
print("=== die Liste ===")


def listed(actor):
    rr = cl.get("/internal/couplings", query_string={"actor": actor},
                headers={ident.INTERNAL_HEADER: "i"})
    return rr.get_json() or {}


lk = listed("karin")
ok("der tenant_admin sieht die Kopplungen seines Mandanten mit Zustand, ohne Hash",
   len(lk["couplings"]) >= 5 and all(c["tenant"] == KUNDE for c in lk["couplings"])
   and {"redeemed", "expired", "withdrawn"} <= {c["state"] for c in lk["couplings"]}
   and "code_hash" not in json.dumps(lk) and "link_hash" not in json.dumps(lk), lk)
ok("der Admin eines anderen Mandanten sieht keine", listed("anna")["couplings"] == [])
ok("der server_admin sieht alle", len(listed("root")["couplings"]) == len(lk["couplings"]))
ok("ein Benutzer sieht keine", listed("uwe")["couplings"] == [])

print("")
print("=== die Kommandozeile ===")
TRUST.update(cp.trust_of(FX_DER, False))
ns = argparse.Namespace(action="couple", tenant="kunde", name="sap-support", who="root", queue=None,
                        kind=None, instance=None, label="Node-RED Halle 2", process=None, user=None,
                        fields=None, days=0, access="", yes=False, window=60, coupling="")
okc, said = m._run_captured(m.cmd_gate, ns)
tline = next((ln for ln in said.splitlines() if ln.startswith("OAAP-COUPLE1.")), "")
tk = cp.decode(tline) if tline else {}
ok("oaap gate couple druckt das Ticket, die Tür und den Link", okc and tk.get("kind") == "gate-access"
   and "POST https://kunde.example/platform/couple/redeem" in said
   and tk.get("links", {}).get("browser", "") in said, said)
ok("... nicht öffentlich vertraut: mit pin und ca, und der Satz dazu",
   tk.get("pin") == cp.pin_of(FX_DER) and tk.get("ca") == cp.pem_of(FX_DER) and "--pinnedpubkey" in said, tk)
ok("... und der Code aus der Zeile löst ein", redeem(tk.get("code", "")).status_code == 200)
TRUST.clear()
ns.label, ns.window = "", 0
okc, said = m._run_captured(m.cmd_gate, ns)
ok("ohne Absender: abgewiesen", not okc and "--label is required" in said, said)

print("")
print("=== das Gateway ===")
site = "\n".join(m.gate_site_lines(KUNDE))
ok("auf dem Ort des Mandanten: /platform/couple/* an identity", "\thandle /platform/couple/* {" in site
   and "rewrite * /couple-door{uri}" in site and "reverse_proxy identity:8000" in site, site[:700])
ok("... die Kopfzeilen erst gestrichen, dann vom Gateway gesetzt (Mandant, Client-Adresse)",
   "request_header -X-OAAP-Couple-Tenant" in site and "request_header -X-OAAP-Client" in site
   and f"header_up X-OAAP-Couple-Tenant {KUNDE}" in site
   and "header_up X-OAAP-Client {http.request.remote.host}" in site)
ok("... mit kleiner Größengrenze", "max_size 4KB" in site)
blk = site.split("handle /platform/couple/* {")[1].split("handle /gate/*")[0]
ok("... Präfix weg, dann umschreiben -- in einem route-Block, sonst ordnet Caddy rewrite vor uri"
   " (gemessen auf oaap-test: identity bekam /couple-door/platform/couple/...)",
   "\t\troute {" in blk and blk.index("route {") < blk.index("uri strip_prefix /platform/couple")
   < blk.index("rewrite * /couple-door{uri}"), blk)
ok("ein Mandant ohne Tor: keine Tür (Stufe 1)", m.gate_site_lines(ANDERE) == [])
ok("die Tür sitzt vor dem Block der Tore", site.index("/platform/couple/*") < site.index("handle /gate/*"))

print("")
print("=== deutsche Sätze ===")
for said_, want in (("--coupling ID is required", "Welche Kopplung?"),
                    ("this tenant has no external address -- x", "Dieser Mandant hat keine Adresse"),
                    (f"no coupling 'abcd1234' in this tenant", "Keine Kopplung „abcd1234“"),
                    ("coupling 'abcd1234' is not open (expired)", "(abgelaufen)")):
    ok(f"„{said_[:30]}“ → deutsch", want in m.gate_refusal_de(said_), m.gate_refusal_de(said_))
idsrc = open(os.path.join(SERVICES, "identity", "app.py"), encoding="utf-8").read()
import re as _re  # noqa: E402

body = idsrc.split("\ndef coupling_create(")[1].split("\ndef coupling_redeem(")[0] \
    + idsrc.split("\ndef coupling_withdraw(")[1].split("\ndef _couple_json(")[0]
said = [_re.sub(r"\{[^}]*\}", "7", s_) for s_ in _re.findall(r'(?:ValueError\(|return )f?"([^"]+)"', body)]
said = [s_ if not s_.startswith("coupling '7' is not open") else "coupling '7' is not open (open)"
        for s_ in said]
un = [s_ for s_ in said if m.gate_refusal_de(s_).startswith("Der Knoten lehnt ab")]
ok(f"jede Absage von identity beim Ausstellen und Zurückziehen ({len(said)}) hat einen deutschen Satz",
   said and not un, un)

print("")
print("=== das Portal ===")
sys.path.insert(0, PORTAL)
import gates_view as gv  # noqa: E402
from jinja2 import Environment  # noqa: E402

tree = ast.parse(open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read())


def tpl(name):
    b = next(resolve(tree, n.value) for n in tree.body if isinstance(n, ast.Assign)
             and any(getattr(t_, "id", "") == name for t_ in n.targets))
    return Environment(autoescape=True).from_string(b)


cs = lk["couplings"] + [{"id": "0pen0001", "tenant": KUNDE, "gate": "sap-support", "kind": "gate-access",
                         "label": "wartet", "issuer": "karin", "issued": "2026-11-26T10:00:00Z",
                         "until": "2099-01-01T00:00:00Z", "state": "open"}]
cv = gv.coupling_view(cs, KUNDE)
ok("die Seite Kopplungen ordnet: offen, eingelöst, beendet",
   [c["id"] for c in cv["open"]] == ["0pen0001"] and cv["redeemed"] and cv["ended"]
   and all(c["state"] in ("expired", "withdrawn") for c in cv["ended"]), cv)
html = tpl("COUPLINGS_BODY").render(v=cv, label="kunde", q="", msg=None, error=None)
ok("... mit Zurückziehen nur für offene, und dem Weg zurück dorthin",
   html.count('action="/tore/withdraw') == 1 and 'name="back" value="kopplungen"' in html
   and "Mensch (Link)" in html and "Programm (Code)" in html and "zurückgezogen" in html, html[:500])
ok("... und nie ein Code oder Schlüssel", "c1_" not in html and "oaapk_" not in html)
doc = m.load_gates()
v = gv.tenant_view(doc, KUNDE, m.load_registry()["instances"], [], lambda x: x,
                   lambda n: f"https://kunde.example/gate/{n}", couplings=cs)
gh = tpl("GATES_BODY").render(v=v, label="kunde", kinds=dict(gv.CASE_KINDS), q="", msg=None, error=None,
                              field_types=gv.FIELD_TYPES, field_targets=gv.FIELD_TARGETS,
                              windows=cp.WINDOW_CHOICES)
ok("die Torseite: „Kopplung erzeugen“ ist der Standard, „Schlüssel sofort zeigen“ der zweite Knopf",
   'action="/tore/couple' in gh and 'formaction="/tore/access' in gh and "Kopplung erzeugen" in gh
   and "Schlüssel sofort zeigen" in gh and '<option value="10080">7 Tage' in gh, gh[gh.find("/tore/couple"):][:500])
ok("... die offene Kopplung steht als „wartet auf Einlösung“ beim Tor, mit Zurückziehen",
   "Wartet auf Einlösung" in gh and "0pen0001" in gh and "eine Kopplung wartet" in v["gates"][0]["why"], v["gates"][0])
d_ = dict(d, pin=cp.pin_of(FX_DER), ca=cp.pem_of(FX_DER))
t_ = cp.gate_ticket(d_, "c1_" + "x" * 43, "11111111-2222-4333-8444-555555555555")
ch = tpl("COUPLE_BODY").render(name="sap-support", t=t_, d=d_, line=cp.encode(t_), q="",
                               file=base64.b64encode(json.dumps(t_).encode()).decode())
ok("die Ticketseite: die Zeile, die Datei, der Link für den Menschen, der POST für das Programm",
   cp.encode(t_) in ch and 'download="kopplung-sap-support-' in ch
   and "https://kunde.example/platform/couple/11111111-2222-4333-8444-555555555555" in ch
   and "/platform/couple/redeem" in ch and "einmal" in ch, ch[:600])
ok("... mit pin für curl und dem Hinweis für Node-RED und STRUST", "--pinnedpubkey 'sha256//" in ch and "application/json' \\\n  -d" in ch
   and "STRUST" in ch and "Node-RED" in ch, ch[ch.find("curl"):][:300])
src = open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read()
ok("das Portal schickt nur die Hashes von Code und Link in den Spool",
   'args["code_hash"], args["link_hash"] = coupling.digest(code), coupling.digest(link)' in src
   and '"code": code' not in src.split("# --- Tore")[1])
nav = open(os.path.join(PORTAL, "nav_view.py"), encoding="utf-8").read()
ok("„Kopplungen“ steht in der Navigation", '("couplings", "/kopplungen", "Kopplungen", "is_user_admin")' in nav)
for svc in ("portal", "identity"):
    df = open(os.path.join(SERVICES, svc, "Dockerfile"), encoding="utf-8").read()
    ok(f"coupling.py kommt ins Abbild von {svc}", "COPY coupling.py ." in df)

print("")
print("ALL PASS" if not fails else f"{fails} FAILURES")
sys.exit(1 if fails else 0)
