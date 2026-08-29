#!/usr/bin/env python3
"""Mandant und Account, Stufe 2 (oaap.core.tenant 0.1, RFC-0022).

Diese Fassung baut eine Dimension ein, die **niemand sehen darf**. Das
macht die Prüfungen ungewöhnlich: Die meisten halten nicht fest, was
neu erscheint, sondern dass nichts erscheint — und die zwei Leseregeln,
an denen die ganze Sicherheit hängt.

Der Satz, der hier verteidigt wird:

    Ein **fehlender** Mandantenbezug bedeutet Standard-Mandant.
    Ein **unbekannter** bedeutet niemals Standard-Mandant.

Die zweite Hälfte ist der Grund für diese Datei. Einen unbekannten
Mandanten still auf `default` abzubilden verschöbe die Benutzer oder
Instanzen eines Kunden in den Mandanten des Betreibers — ein Datenleck
im Gewand der Robustheit.

Braucht kein Docker und keinen Knoten.

Run: python3 test/test_tenant.py
"""
import json
import os
import re
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-tenant-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {detail}")


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def capture(fn, *args):
    """Run a cmd_* function and give back (stdout, exit code)."""
    import io
    import contextlib
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            fn(*args)
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue(), code


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                     r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


print("\n=== der Standard-Mandant entsteht genau einmal ===")

check("vor der Migration hat der Knoten keinen Mandanten",
      m.load_tenants() == {} and m.default_tenant_id() == "")

tid = m.ensure_default_tenant()
check("ensure_default_tenant legt einen an", bool(tid))
check("seine Kennung ist eine UUID", bool(UUID_RE.match(tid)), tid)
check("sein Kürzel ist 'default'", m.tenant_label(tid) == "default")

rec = m.load_tenants()[tid]
check("er trägt eine Account-Referenz", bool(UUID_RE.match(rec.get("account", ""))),
      "der Account liegt auf dem Management-Knoten (RFC-0022 Q1) — "
      "hier steht nur eine undurchsichtige Referenz")
check("die Account-Referenz ist NICHT dieselbe wie die Mandanten-Kennung",
      rec["account"] != tid,
      "sonst wären Account und Mandant dasselbe Ding mit zwei Namen")
check("er hat einen Zeitstempel", bool(rec.get("created")))

again = m.ensure_default_tenant()
check("ein zweiter Aufruf legt keinen zweiten an",
      again == tid and len(m.load_tenants()) == 1)


print("\n=== zwei Knoten haben zwei verschiedene Standard-Mandanten ===")
# Der Grund (Spec 1.2): Eine gemeinsame, wohlbekannte Kennung lüde zu
# genau der Verschmelzung ein, die RFC-0022 D1 ausschließt — ein Mandant
# lebt auf genau einem Knoten.
other_dir = tempfile.mkdtemp(prefix="oaap-tenant-test2-")
saved = m.TENANTS_FILE
m.TENANTS_FILE = os.path.join(other_dir, "tenants.json")
other_tid = m.ensure_default_tenant()
m.TENANTS_FILE = saved
check("der zweite Knoten bekommt eine andere Kennung", other_tid != tid,
      f"{other_tid} == {tid}")


print("\n=== die zwei Leseregeln, und ihr Unterschied ===")

check("FEHLT: leerer Bezug bedeutet Standard-Mandant",
      m.resolve_tenant("") == tid and m.resolve_tenant(None) == tid)
unknown = str(uuid.uuid4())
check("UNBEKANNT: eine fremde Kennung bedeutet NICHT Standard-Mandant",
      m.resolve_tenant(unknown) is None,
      "sie würde sonst die Daten eines Kunden in den Mandanten des "
      "Betreibers verschieben")
check("die Kennung des Nachbarknotens ist hier ebenfalls unbekannt",
      m.resolve_tenant(other_tid) is None,
      "ein Mandant lebt auf genau einem Knoten (RFC-0022 D1)")
check("der eigene Bezug löst auf", m.resolve_tenant(tid) == tid)


print("\n=== die Migration stempelt, was appctl gehört ===")

reg = {"instances": {
    "alt-eins": {"app_id": "a", "channel": "test"},
    "alt-zwei": {"app_id": "b", "channel": "production"},
}}
m.save_registry(reg)
m.grant_create("create", "neue-app", "d" * 64, {"channel": "test", "by": "joerg"})

out, _ = capture(m.cmd_migrate_tenants, Args())
reg = m.load_registry()
check("jede vorhandene Instanz gehört danach dem Standard-Mandanten",
      all(i.get("tenant") == tid for i in reg["instances"].values()))
permits = [g for g in m.load_grants().values() if g.get("kind") == "create"]
check("jede offene Anlege-Erlaubnis auch",
      permits and all(g["payload"].get("tenant") == tid for g in permits),
      "sie ist der einzige Datensatz, der den Mandanten speichern MUSS — "
      "er wird ausgestellt, bevor die Instanz existiert")
check("die Migration sagt, was sie getan hat", "assigned to it" in out, out)

out2, _ = capture(m.cmd_migrate_tenants, Args())
check("ein zweiter Lauf schweigt", out2 == "", repr(out2))
check("und legt keinen zweiten Mandanten an", len(m.load_tenants()) == 1)


print("\n=== was bewusst KEINEN Mandanten trägt ===")

m.save_profiles(["dev"])
before_profiles = m.load_profiles()
capture(m.cmd_migrate_tenants, Args())
check("Knotenprofile bleiben unberührt", m.load_profiles() == before_profiles,
      "der Knoten gehört dem Betreiber, nie einem Mandanten")

with open(os.path.join(DATA, "apps", "node.json"), encoding="utf-8") as f:
    node = json.load(f)
check("node.json bekommt kein Mandantenfeld", "tenant" not in node)

m.save_tokens({"alt-eins": {"digest": "x" * 64}})
capture(m.cmd_migrate_tenants, Args())
check("ein Deploy-Token speichert keinen Mandanten",
      all("tenant" not in t for t in m.load_tokens().values()),
      "es hängt an genau einer Instanz — und die weiß es schon. Zweimal "
      "gespeichert heißt: kann sich selbst widersprechen")


print("\n=== ein Redeploy darf eine Instanz nie verschieben ===")

fremd = str(uuid.uuid4())
check("was die Instanz schon sagt, gewinnt",
      m.tenant_for_new_instance({"tenant": fremd}) == fremd,
      "sonst könnte ein Redeploy eine Instanz von einem Kunden zum "
      "nächsten tragen")
check("eine Erlaubnis zählt nur bei einer neuen Instanz",
      m.tenant_for_new_instance({"tenant": fremd},
                                {"tenant": tid}) == fremd)
check("eine neue Instanz folgt der Erlaubnis",
      m.tenant_for_new_instance(None, {"tenant": fremd}) == fremd)
check("ohne beides: der Standard-Mandant",
      m.tenant_for_new_instance(None) == tid)


print("\n=== 'tenant check' meldet und repariert nichts ===")

write_users([{"username": "joerg", "tenant": tid},
             {"username": "alt-ohne-feld"}])
out, code = capture(m.cmd_tenant, Args(action="check", name=None))
check("bei heilen Daten: alles löst auf, Rückgabewert 0",
      code == 0 and "All records resolve" in out, out)

reg = m.load_registry()
reg["instances"]["kaputt"] = {"app_id": "c", "channel": "test", "tenant": unknown}
m.save_registry(reg)
write_users([{"username": "fremder", "tenant": unknown}])
out, code = capture(m.cmd_tenant, Args(action="check", name=None))
check("ein unbekannter Mandant wird gemeldet", "kaputt" in out, out)
check("auch bei einem Benutzer", "fremder" in out, out)
check("und der Rückgabewert ist 1", code == 1)
check("aber nichts wurde umgeschrieben",
      m.load_registry()["instances"]["kaputt"]["tenant"] == unknown,
      "Reparieren hieße raten, wessen Daten das sind — und die einzige "
      "sichere Vermutung ist keine")

reg = m.load_registry()
del reg["instances"]["kaputt"]
m.save_registry(reg)
write_users([{"username": "joerg", "tenant": tid}, {"username": "alt"}])


print("\n=== solange es einen Mandanten gibt, sagt niemand ein Wort ===")

check("single_tenant() ist wahr", m.single_tenant() is True)
out, _ = capture(m.cmd_tenant, Args(action="list", name=None))
check("'tenant list' sagt ausdrücklich, dass hier keine benutzt werden",
      "not in use here" in out, out)

out, _ = capture(m.cmd_tenant, Args(action="show", name=None))
check("'tenant show' zählt Benutzer und Instanzen",
      "Users:" in out and "Instances:" in out, out)
check("und nennt dabei keine Namen",
      "joerg" not in out and "alt-eins" not in out,
      "eine Bestandsaufnahme, kein Datenexport")


print("\n=== die Unsichtbarkeit im Code der Oberflächen ===")
# Statische Prüfungen: Der Mandant darf in keiner Ausgabe auftauchen,
# die es vor dieser Fassung schon gab. Beide Stellen bauen bewusst als
# Whitelist — genau deshalb kostet das neue Feld hier nichts.

def read(path):
    with open(os.path.join(HERE, "..", "platform", path), encoding="utf-8") as f:
        return f.read()


ident = read("services/identity/app.py")
# Seit 0.2 gibt public_user() den Mandanten heraus -- aber nur an das
# Portal ueber die schluesselgeschuetzte interne Schnittstelle und an
# `oaap user list` auf der Maschine. Was zaehlt, ist die andere Haelfte:
# in eine APP darf er nie geraten. Die Antwort von /verify ist die
# einzige Stelle, an der Identitaet eine App ueberhaupt erreicht.
verify = ident[ident.find("def verify():"):]
verify = verify[:verify.find("@app.", 1)]
headers = verify[verify.find(", 204"):]
check("keine App erfaehrt je ihren Mandanten",
      "X-OAAP-Tenant" not in ident and "tenant" not in headers,
      "die Kopfzeilen an die App tragen Benutzer und Rollen, sonst nichts")
check("aber das Portal erfaehrt ihn — sonst koennte es nicht filtern",
      "u.get(\"tenant\", \"\")" in ident,
      "es muss einem tenant_admin dessen eigenen Mandanten zeigen koennen")

fleet = read("services/portal/fleet_view.py")
check("die Flotten-Auskunft nennt keinen Mandanten", "tenant" not in fleet,
      "sie beantwortet die Frage nach dem KNOTEN und gehört dem Betreiber")

check("identity schreibt den Mandantenspeicher nicht",
      "TENANTS_FILE" in ident and "_save(TENANTS_FILE" not in ident,
      "appctl auf dem Host besitzt die Datei; die Einbindung ist read-only")

appctl_src = read("appctl.py")
check("appctl schreibt den Benutzerspeicher nicht",
      "_identity_users_path" in appctl_src
      and "json.dump(users" not in appctl_src,
      "zwei Schreiber auf einer JSON-Datei sind ein verlorenes Update, "
      "das auf den Tag wartet, an dem zwei Admins gleichzeitig klicken")

migrate = read("migrate.sh")
check("migrate.sh ruft die Mandanten-Migration auf",
      "migrate-tenants" in migrate,
      "sonst bekommt ein Knoten im Feld den Schritt nie — der Fehler "
      "von oaap-demo, siehe test_migrate.py")


print(f"\n{ok} bestanden, {fail} fehlgeschlagen")
if fail:
    print("PRUEFUNGEN FEHLGESCHLAGEN")
    sys.exit(1)
print("ALLE PRUEFUNGEN BESTANDEN")
