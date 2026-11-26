#!/usr/bin/env python3
"""Tore -- der Weg hinein fuer andere Systeme (RFC-0135 Stufe 1).

Was hier festgehalten wird -- jede Regel mit ihrer Gegenprobe:

- gates.py: die drei Vorlagen kommen aus der Feldliste; die Pruefung nimmt
  JSON-Werte und Formularwerte gleich, lehnt Unbekanntes mit Namen ab;
- identity, die Tuer des Tors: Basic mit dem Schluessel als Passwort und dem
  Benutzer des Tors, oder Bearer; ein unbekanntes Tor, ein falscher Schluessel,
  ein Schluessel eines anderen Tors, eines anderen Mandanten, ein Schluessel
  ohne Grenze, ein falscher Benutzer, eine Person -- ALLE dieselbe Antwort
  401; `sap-user` wird nie gelesen; Ping und Vorlagen; JSON auf Wunsch;
- /verify?gate=: dieselbe Regel, und die Kopfzeilen des Zugangs;
- appctl: der Block auf dem Ort des Mandanten (Lieferung -> identity fragt ->
  Ziel mit Beschreibung; Ping/Vorlagen -> identity), die Kopfzeilen des Tors
  werden auf jeder Instanz-Site gestrichen, ein Tor ohne Ziel hat keinen
  Lieferweg; das Ziel ist die Produktions-Instanz desselben Mandanten.

Aufruf: python3 test/test_gates.py  (identity-Teil braucht flask, sonst SKIP)
"""
import base64
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
DATA = tempfile.mkdtemp(prefix="oaap-gates-test-")
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


import gates  # noqa: E402

FIELDS = gates.check_fields(gates.DEFAULT_FIELDS)

# ---------------------------------------------------------------------------
print("")
print("gates.py: Vorlagen und Pruefung aus einer Liste")
sample = json.loads(gates.template(FIELDS, "sample"))
ok("Muster: jedes Feld mit Beispielwert", list(sample) == [f["name"] for f in FIELDS]
   and sample["ref"] == "ZSUP-000815", sample)
ph = gates.template(FIELDS, "placeholders")
ok("Platzhalter: \"{feld}\" je Feld, Hinweis zum Maskieren und zu Formularfeldern",
   '"ref": "{ref}"' in ph and '"occurred": "{occurred}"' in ph and "maskieren" in ph
   and "x-www-form-urlencoded" in ph, ph)
body = "\n".join(ln for ln in ph.splitlines() if not ln.startswith("#"))
ok("Platzhalter-Vorlage ist (mit Platzhaltern) gueltiges JSON", isinstance(json.loads(body), dict))
tab = gates.template(FIELDS, "fields").splitlines()
ok("Feldtabelle: Kopf + eine Zeile je Feld, Tabulator",
   tab[0].split("\t") == ["Feld", "Typ", "Format", "Pflicht", "Beschreibung"]
   and len(tab) == 1 + len(FIELDS) and tab[1].startswith("ref\ttext\t<= 120"), tab)
good = {"ref": "Z-1", "title": "t", "text": "x", "system": "A4H/001",
        "occurred": "2026-11-26T09:14:03Z"}
v, e = gates.validate(FIELDS, good)
ok("gueltig: keine Fehler, Werte als Text", not e and v["ref"] == "Z-1", e)
v, e = gates.validate(FIELDS, dict(good, zzz=1, aaa=2))
ok("unbekannte Felder mit Namen, sortiert", e and e[0] == "unbekannte Felder: aaa, zzz", e)
v, e = gates.validate(FIELDS, {"ref": "Z-1", "title": "  "})
ok("Pflichtfelder fehlen mit Namen (leer zaehlt als fehlend)",
   "Pflichtfelder fehlen: title, text" in e, e)
num = gates.check_fields(gates.DEFAULT_FIELDS + [
    {"name": "menge", "type": "number", "to": "tech"},
    {"name": "eilig", "type": "yesno", "to": "tech"}])
v, e = gates.validate(num, dict(good, menge=3, eilig=True))
ok("JSON-Zahl und -Wahrheitswert wie Formularwerte", not e and v["menge"] == "3" and v["eilig"] == "X", e)
v, e = gates.validate(num, dict(good, menge="3.5", eilig="X"))
ok("dieselben als Text (ABAP)", not e and v["menge"] == "3.5" and v["eilig"] == "X", e)
v, e = gates.validate(num, dict(good, menge="3,5"))
ok("Komma als Dezimaltrenner abgelehnt", e == ["menge: eine Zahl"], e)
ok("pack/unpack: Header ohne Anfuehrungszeichen, rueckwaerts gleich",
   gates.unpack(gates.pack(FIELDS)) == FIELDS and '"' not in gates.pack(FIELDS)
   and "=" not in gates.pack(FIELDS))
for bad, why in (([{"name": "a", "to": "ref", "required": True}], "ohne title"),
                 ([{"name": "a", "to": "ref", "required": True},
                   {"name": "b", "to": "title"}], "title nicht Pflicht"),
                 ([{"name": "A", "to": "ref", "required": True}], "Name gross"),
                 ([{"name": "a", "to": "ref", "type": "number", "required": True},
                   {"name": "b", "to": "title", "required": True}], "ref keine Zahl")):
    try:
        gates.check_fields(bad)
        ok(f"Feldliste abgelehnt: {why}", False)
    except ValueError:
        ok(f"Feldliste abgelehnt: {why}", True)

# ---------------------------------------------------------------------------
print("")
print("identity: die Tuer des Tors")
try:
    import flask  # noqa: F401
except ImportError:
    flask = None
if flask is None:
    skip("identity", "flask fehlt")
else:
    idata = tempfile.mkdtemp(prefix="oaap-gates-identity-")
    gfile = os.path.join(idata, "gates.json")
    os.environ.update(SESSION_SECRET="s", SETUP_TOKEN="t", INTERNAL_API_KEY="k",
                      OAAP_IDENTITY_DATA_DIR=idata, OAAP_GATES_FILE=gfile,
                      ASSERTION_KEY_FILE=os.path.join(idata, "assertion.key"))
    sys.path.insert(0, os.path.join(SERVICES, "identity"))
    sys.modules.pop("app", None)
    ident = importlib.import_module("app")
    TENANTS = {"T-bec": "bec", "T-other": "other"}
    ident.resolve_tenant = lambda ref: ref if ref in TENANTS else None
    ident._label_of = lambda tid: TENANTS.get(tid, "")
    store = gates.empty_store()
    store["gates"]["T-bec/sap-support"] = {"tenant": "T-bec", "name": "sap-support",
                                           "user": "sap-support", "process": "sap-case",
                                           "fields": FIELDS}
    store["gates"]["T-bec/formular"] = {"tenant": "T-bec", "name": "formular",
                                        "user": "formular", "process": "sap-case", "fields": FIELDS}
    store["processes"]["T-bec/sap-case"] = {"tenant": "T-bec", "name": "sap-case", "kind": "case",
                                            "queue": "sap-support", "label": "SAP-Support"}
    with open(gfile, "w", encoding="utf-8") as f:
        json.dump(store, f)
    users = [
        {"username": "gate-sap-support-1", "kind": "machine", "roles": ["user"], "groups": [],
         "tenant": "T-bec", "active": True, "id": "u-1", "display_name": "SAP A4H 001"},
        {"username": "gate-formular-1", "kind": "machine", "roles": ["user"], "groups": [],
         "tenant": "T-bec", "active": True, "id": "u-2"},
        {"username": "gate-other-1", "kind": "machine", "roles": ["user"], "groups": [],
         "tenant": "T-other", "active": True, "id": "u-3"},
        {"username": "anna", "kind": "human", "roles": ["user"], "groups": [],
         "tenant": "T-bec", "active": True, "id": "u-4"},
    ]
    ident.load_users = lambda: users
    keys = []

    def mkkey(kid, principal, inst, label=""):
        from werkzeug.security import generate_password_hash
        keys.append({"id": kid, "principal": principal, "roles": ["user"], "instance": inst,
                     "label": label, "hash": generate_password_hash("s" * 22), "revoked": False,
                     "expires": "2027-02-24T00:00:00Z"})
        return f"oaapk_{kid}_" + "s" * 22
    ident.load_keys = lambda: keys
    ident._touch_key = lambda kid: None
    ident._login_failed = lambda key: None
    ident._login_blocked = lambda key: False
    SC = gates.scope("T-bec", "sap-support")
    k_good = mkkey("aaaa0001", "gate-sap-support-1", SC, "SAP A4H 001")
    k_formular = mkkey("aaaa0002", "gate-formular-1", gates.scope("T-bec", "formular"))
    k_wide = mkkey("aaaa0003", "gate-sap-support-1", "")
    k_tenant = mkkey("aaaa0004", "gate-other-1", SC)
    k_person = mkkey("aaaa0005", "anna", SC)
    k_instance = mkkey("aaaa0006", "gate-sap-support-1", "vorgaenge")
    cl = ident.app.test_client()

    def basic(user, pw):
        return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    def door(path="/gate-door/sap-support", auth=None, tenant="T-bec", **hdr):
        h = dict(hdr)
        if auth:
            h["Authorization"] = auth
        if tenant:
            h["X-OAAP-Gate-Tenant"] = tenant
        h.setdefault("X-OAAP-Gate-Target", "vorgaenge")
        return cl.get(path, headers=h)

    r = door(auth=basic("sap-support", k_good), **{"sap-user": "probe", "sap-language": "E"})
    t = r.get_data(as_text=True)
    ok("Basic mit Schluessel als Passwort: Ping 200, Klartext", r.status_code == 200
       and r.headers["Content-Type"].startswith("text/plain") and "OAAP-Tor: sap-support (Mandant bec)" in t
       and "Zugang: SAP A4H 001" in t and "2027-02-24" in t and "SAP-Support" in t
       and "Felder: 5 (3 Pflicht)" in t, (r.status_code, t))
    r = door(auth="Bearer " + k_good)
    ok("Bearer mit demselben Schluessel: Ping 200", r.status_code == 200, r.status_code)
    r = door(auth="Bearer " + k_good, Accept="application/json")
    d = r.get_json(silent=True) or {}
    ok("Accept JSON: der Ping als JSON", r.status_code == 200 and d.get("gate") == "sap-support"
       and d.get("required") == 3, d)
    r = door(auth="Bearer " + k_good, **{"X-OAAP-Gate-Target": "-"})
    ok("ohne Ziel sagt der Ping es", "Ziel nicht erreichbar" in r.get_data(as_text=True))

    want = door(path="/gate-door/gibtsnicht", auth=basic("gibtsnicht", k_good))
    ref_body = want.get_data(as_text=True).replace("gibtsnicht", "X")
    ok("unbekanntes Tor: 401 mit Basic-Aufforderung", want.status_code == 401
       and want.headers.get("WWW-Authenticate", "").startswith("Basic realm=\"OAAP-Tor"), want.status_code)
    cases = {
        "ohne Anmeldung": door(),
        "falscher Schluessel": door(auth=basic("sap-support", "oaapk_aaaa0001_" + "x" * 22)),
        "falscher Benutzer, richtiger Schluessel": door(auth=basic("probe", k_good)),
        "leerer Benutzer": door(auth=basic("", k_good)),
        "Schluessel eines anderen Tors": door(auth=basic("sap-support", k_formular)),
        "Schluessel ohne Grenze": door(auth="Bearer " + k_wide),
        "Schluessel einer Instanz": door(auth="Bearer " + k_instance),
        "Zugang eines anderen Mandanten": door(auth="Bearer " + k_tenant),
        "eine Person": door(auth="Bearer " + k_person),
        "Basic-Passwort ist kein Schluessel": door(auth=basic("sap-support", "geheim")),
        "Tor im anderen Mandanten gesucht": door(auth="Bearer " + k_good, tenant="T-other"),
    }
    for label, r in cases.items():
        ok(f"{label}: 401, dieselbe Antwort wie ein unbekanntes Tor",
           r.status_code == 401 and r.get_data(as_text=True).replace("sap-support", "X") == ref_body,
           (r.status_code, r.get_data(as_text=True)))
    r = door(auth="Bearer " + k_good, tenant="")
    ok("ohne Mandant vom Gateway: 404", r.status_code == 404, r.status_code)
    r = door(path="/gate-door/sap-support/template?form=placeholders", auth="Bearer " + k_good)
    ok("Vorlage Platzhalter", r.status_code == 200 and '"title": "{title}"' in r.get_data(as_text=True))
    r = door(path="/gate-door/sap-support/template", auth="Bearer " + k_good)
    ok("Vorlage Muster als JSON", r.status_code == 200 and r.headers["Content-Type"].startswith(
        "application/json") and r.get_json()["system"] == "A4H/001")
    r = door(path="/gate-door/sap-support/template?form=fields", auth="Bearer " + k_good)
    ok("Vorlage Feldtabelle", r.status_code == 200 and r.get_data(as_text=True).startswith("Feld\tTyp"))
    r = door(path="/gate-door/sap-support/template?form=xml", auth="Bearer " + k_good)
    ok("unbekannte Form: 400", r.status_code == 400)
    r = door(path="/gate-door/gibtsnicht/template", auth="Bearer " + k_good)
    ok("Vorlage eines unbekannten Tors: 401 (beschreibt den Prozess eines Mandanten)",
       r.status_code == 401)
    r = cl.post("/gate-door/sap-support", headers={"Authorization": "Bearer " + k_good,
                                                   "X-OAAP-Gate-Tenant": "T-bec"})
    ok("Lieferung an identity (Tor ohne Ziel): 503 nach der Anmeldung",
       r.status_code == 503 and "kein Ziel" in r.get_data(as_text=True), r.status_code)
    r = cl.post("/gate-door/sap-support", headers={"X-OAAP-Gate-Tenant": "T-bec"})
    ok("... ohne Anmeldung: 401", r.status_code == 401)

    r = cl.get("/verify?gate=T-bec/sap-support",
               headers={"Authorization": basic("sap-support", k_good), "sap-user": "MMUSTER"})
    ok("verify?gate: 204 mit Prinzipal und Zugang", r.status_code == 204
       and r.headers.get("X-OAAP-User") == "gate-sap-support-1"
       and r.headers.get("X-OAAP-Gate-Access") == "gate-sap-support-1"
       and r.headers.get("X-OAAP-Gate-Label") == "SAP%20A4H%20001", dict(r.headers))
    ok("verify?gate: keine Zusicherung (ein Schluessel ist keine Person)",
       not r.headers.get("X-OAAP-User-Assertion"))
    ok("verify?gate: sap-user taucht nirgends auf", "MMUSTER" not in str(dict(r.headers)))
    r = cl.get("/verify?gate=T-bec/sap-support", headers={"Authorization": "Bearer " + k_formular})
    ok("verify?gate: Schluessel eines anderen Tors 401", r.status_code == 401)
    r = cl.get("/verify?gate=T-other/sap-support", headers={"Authorization": "Bearer " + k_good})
    ok("verify?gate: anderer Mandant 401", r.status_code == 401)
    r = cl.get("/verify?instance=vorgaenge", headers={"Authorization": "Bearer " + k_good})
    ok("der Schluessel des Tors oeffnet keine Instanz (seine Grenze ist das Tor)",
       r.status_code == 403, r.status_code)

# ---------------------------------------------------------------------------
print("")
print("appctl: der Block auf dem Ort des Mandanten")
import appctl as m  # noqa: E402

m.resolve_tenant = lambda ref: ref if ref in ("T-bec", "T-other") else None
INSTS = {
    "vorgaenge": {"app_id": "vorgaenge", "tenant": "T-bec", "channel": "production",
                  "container": "oaap-vorgaenge", "svc_port": 8000},
    "vorgaenge-test": {"app_id": "vorgaenge", "tenant": "T-bec", "channel": "test",
                       "container": "oaap-vorgaenge-test", "svc_port": 8000},
    "fremd": {"app_id": "vorgaenge", "tenant": "T-other", "channel": "production",
              "container": "oaap-fremd", "svc_port": 8000},
}
doc = gates.empty_store()
doc["gates"]["T-bec/sap-support"] = {"tenant": "T-bec", "name": "sap-support", "user": "sap-support",
                                     "process": "sap-case", "fields": FIELDS}
doc["gates"]["T-bec/ohne-ziel"] = {"tenant": "T-bec", "name": "ohne-ziel", "user": "ohne-ziel",
                                   "process": "fehlt", "fields": FIELDS}
doc["processes"]["T-bec/sap-case"] = {"tenant": "T-bec", "name": "sap-case", "kind": "case",
                                      "queue": "sap-support", "case_kind": "question"}
site = "\n".join(m.gate_site_lines("T-bec", INSTS, doc))
ok("ein Block /gate/* mit Groessengrenze", "\thandle /gate/* {" in site
   and f"max_size {gates.BODY_MAX_KB}KB" in site, site[:400])
ok("die Kopfzeilen des Tors und die der Identitaet werden zuerst gestrichen",
   all(f"request_header -{h}" in site for h in m.GATE_HEADERS + m.IDENTITY_HEADERS))
ok("Lieferung: identity fragt mit gate=Mandant/Name, kopiert Zugang und Bezeichnung",
   "uri /verify?gate=T-bec/sap-support" in site
   and m._COPY_IDENTITY + " X-OAAP-Gate-Access X-OAAP-Gate-Label" in site, site)
ok("... dann an /platform/cases/gate der Produktions-Instanz desselben Mandanten",
   "rewrite * /platform/cases/gate{uri}" in site and "reverse_proxy oaap-vorgaenge:8000" in site
   and "oaap-vorgaenge-test" not in site and "oaap-fremd" not in site, site)
ok("... mit Beschreibung (Felder, Prozess), Herkunft gate:<name>, ohne den Schluessel",
   f"header_up X-OAAP-Gate-Fields {gates.pack(FIELDS)}" in site
   and "header_up X-OAAP-Source-Instance gate:sap-support" in site
   and "header_up -Authorization" in site
   and gates.unpack(site.split("X-OAAP-Gate-Process ")[1].split()[0])["queue"] == "sap-support")
ok("Ping und Vorlagen: GET/HEAD auf die Adresse und /template gehen an identity",
   "not path /gate/sap-support/template" in site and "method GET HEAD" in site
   and "rewrite * /gate-door{uri}" in site and "header_up X-OAAP-Gate-Tenant T-bec" in site)
blk = site.split("@gatedoor1")[0] if "@gatedoor1" in site else site
ok("ein Tor ohne Ziel hat keinen Lieferweg, nur die Tuer bei identity",
   "uri /verify?gate=T-bec/ohne-ziel" not in site and "path /gate/ohne-ziel /gate/ohne-ziel/*" in site)
ok("ein anderer Mandant: kein Block", m.gate_site_lines("T-other", INSTS, doc) == [])
d2 = json.loads(json.dumps(doc))
d2["processes"]["T-bec/sap-case"]["instance"] = "vorgaenge-test"
ok("der Prozess kann eine Instanz nennen", "reverse_proxy oaap-vorgaenge-test:8000" in
   "\n".join(m.gate_site_lines("T-bec", INSTS, d2)))
d2["processes"]["T-bec/sap-case"]["instance"] = "fremd"
ok("... aber nie eine eines anderen Mandanten",
   "oaap-fremd" not in "\n".join(m.gate_site_lines("T-bec", INSTS, d2)))

inst_site = "\n".join(m._platform_site_lines([], [], "vorgaenge", "T-bec", False))
ok("jede Instanz-Site streicht die Kopfzeilen des Tors",
   all(f"\trequest_header -{h}" in inst_site for h in m.GATE_HEADERS))
full = "\n".join(m.site_body([{"path": "/", "roles": ["user"]}], "c", 8000,
                             scope="vorgaenge", tenant="T-bec"))
ok("... und eine ganze Site gilt deswegen nicht als veraltet (Identitaets-Kopfzeilen)",
   "request_header -X-OAAP-Gate-Name" in full and not m._site_identity_stale(full))
ok("eine Site mit der Kopfzeilenliste von vorher (ohne Tor-Streichung) ist veraltet (Marker)",
   m.cases_marker({"vorgaenge": INSTS["vorgaenge"]}, "vorgaenge").startswith(
       f"{m.CASES_MARKER}13 "))
ok("Blockformat 13", m.CASES_BLOCK_FORMAT == 13)

# store round trip
m.save_gates(doc)
ok("gates.json: lesbar fuer identity (0644), dieselben Daten", m.load_gates() == doc
   and (os.name == "nt" or oct(os.stat(m.GATES_FILE).st_mode & 0o777) == "0o644"))

print("")
print(f"{'OK' if not fails else 'FAILED'} -- {fails} failure(s), {skips} skipped")
sys.exit(1 if fails else 0)
