#!/usr/bin/env python3
"""Die automatische Adresse einer Instanz traegt das Mandanten-Kuerzel.

Der Befund dahinter (2026-09-02, `oaapx01`): Das Gateway schreibt die
Site fuer eine Instanz eines Mandanten unter
`<instanz>.<kuerzel>.<knoten>` — das Portal rechnete aber ueberall
`<instanz>.<knoten>`. Die Launchpad-Kachel eines Kundenmandanten
verlinkte damit auf einen Namen, den es nicht gibt, und die Objektseite
zeigte eine Adresse zum Abschreiben, die nirgends antwortet.

Geprueft wird deshalb BEIDES, und der Vergleich ist der eigentliche
Test: die Regel im Portal (`instance_view.auto_host`) und der Name, den
`appctl` wirklich in die Caddy-Datei schreibt. Zwei Stellen, eine
Antwort — weichen sie ab, ist genau das der Fehler von heute wieder da.

Braucht kein Docker, kein Flask und keinen Knoten.

Aufruf: python3 test/test_auto_address.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-autoaddr-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import instance_view as iv                                    # noqa: E402
import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


HOST = "oaap.joomp.de"
TENANTS = {
    "tid-default": {"label": "default", "name": ""},
    "tid-cls": {"label": "cls", "name": "PoC 3D-Viewer (Grosskunde)"},
}

print("\nDie Regel im Portal")

ok("Standard-Mandant: Name bleibt zweistufig",
   iv.auto_host("bdt-hub", {"tenant": "tid-default"}, TENANTS, HOST)
   == "bdt-hub.oaap.joomp.de")

ok("Kundenmandant: Kuerzel steht dazwischen",
   iv.auto_host("studio", {"tenant": "tid-cls"}, TENANTS, HOST)
   == "studio.cls.oaap.joomp.de")

ok("ohne Mandanten-Angabe = Standard-Mandant (spec 2.5)",
   iv.auto_host("bdt-app", {}, TENANTS, HOST) == "bdt-app.oaap.joomp.de")

ok("unbekannter Mandant bekommt GAR KEINEN Namen (fail closed)",
   iv.auto_host("fremd", {"tenant": "tid-weg"}, TENANTS, HOST) == "",
   "ein unbekannter Verweis darf nicht als Standard-Mandant gelesen werden")

ok("Knoten ohne externen Namen: keine automatische Adresse",
   iv.auto_host("studio", {"tenant": "tid-cls"}, TENANTS, "") == "")

ok("Knoten ganz ohne Mandanten (vor der Migration) rechnet wie frueher",
   iv.auto_host("crm", {}, {}, HOST) == "crm.oaap.joomp.de")

print("\nDieselbe Antwort wie im Gateway")

# Der echte Knoten: zwei Mandanten, zwei Instanzen, eine Caddy-Datei.
m.reload_gateway = lambda: None
m.ensure_default_tenant()
default_id = m.default_tenant_id()
cls_id = m.create_tenant("cls", title="PoC 3D-Viewer (Grosskunde)") \
    if hasattr(m, "create_tenant") else None
if cls_id is None:                       # angelegt wie das CLI es tut
    import argparse
    args = argparse.Namespace(action="create", name="cls", target=None,
                              title="PoC", account="", account_name="",
                              grace_days=30, yes=True, count=50)
    import contextlib
    import io as _io
    with contextlib.redirect_stdout(_io.StringIO()):
        m.cmd_tenant(args)
    cls_id, _t = m.tenant_by_label("cls")

tenants = m.load_tenants()
ok("Testaufbau: zwei Mandanten vorhanden", len(tenants) == 2, sorted(tenants))

for label, tid, expected in (
        ("Standard-Mandant", default_id, f"viewer.{HOST}"),
        ("Mandant cls", cls_id, f"viewer.cls.{HOST}")):
    prefixes = m.tenant_host_prefixes(tid)
    gateway_name = (f"viewer.{prefixes[0]}.{HOST}" if prefixes and prefixes[0]
                    else f"viewer.{HOST}")
    portal_name = iv.auto_host("viewer", {"tenant": tid}, tenants, HOST)
    ok(f"{label}: Portal und Gateway nennen denselben Namen",
       portal_name == gateway_name == expected,
       f"Portal={portal_name!r} Gateway={gateway_name!r} erwartet={expected!r}")

print("")
print("Dieselbe Adresse spricht auch die Kommandozeile aus")

# Vier Stellen im CLI nannten die automatische Adresse: `app address
# show`, die Meldung nach dem Entfernen einer eigenen Adresse, die
# Zeile nach dem Installieren und die Ablehnung eines Namens, den es
# ohnehin schon automatisch gibt. Alle vier rechneten `<instanz>.<knoten>`.
inst_cls = {"tenant": cls_id}
inst_std = {"tenant": default_id}
ok("CLI: Standard-Mandant",
   m.instance_auto_hosts("viewer", inst_std, ext_host=HOST)
   == ["viewer." + HOST])
ok("CLI: Kundenmandant traegt das Kuerzel",
   m.instance_auto_hosts("viewer", inst_cls, ext_host=HOST)
   == ["viewer.cls." + HOST])
ok("CLI: unbekannter Mandant nennt gar keine Adresse",
   m.instance_auto_hosts("viewer", {"tenant": "weg"}, ext_host=HOST) == [])
ok("CLI: ohne externen Namen des Knotens ebenfalls keine",
   m.instance_auto_hosts("viewer", inst_cls, ext_host="") == [])
ok("CLI und Portal sagen dasselbe",
   m.instance_auto_hosts("viewer", inst_cls, ext_host=HOST)[0]
   == iv.auto_host("viewer", inst_cls, tenants, HOST))

with open(os.path.join(HERE, "..", "platform", "appctl.py"),
          encoding="utf-8") as f:
    cli = f.read()
import ast                                                     # noqa: E402

helper = next(n for n in ast.parse(cli).body
              if isinstance(n, ast.FunctionDef)
              and n.name == "instance_auto_hosts")
own = set(range(helper.lineno, (helper.end_lineno or helper.lineno) + 1))
leftovers = [ln.strip() for i, ln in enumerate(cli.splitlines(), 1)
             if i not in own
             and ("{name}.{ext_host}" in ln or "{name}.{ext}" in ln)]
ok("keine handgebaute Adresse mehr im CLI", not leftovers, leftovers)

print("")
print("Keine zweite Rechenstelle im Portal")

APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
with open(APP_PY, encoding="utf-8") as f:
    src = f.read()

# Der Fehler von heute war nicht eine falsche Formel, sondern DREI
# Kopien davon. Wer die Adresse kuenftig wieder von Hand zusammensetzt,
# faellt hier auf -- auch wenn seine Kopie zufaellig richtig waere.
handmade = [ln.strip() for ln in src.splitlines()
            if "{external_host()}" in ln and "def external_host" not in ln]
ok("keine Adresse mehr direkt aus external_host() gebaut",
   not handmade, handmade)

ok("alle Aufrufer gehen ueber instance_auto_host (Definition + 3 Stellen)",
   src.count("instance_auto_host(") >= 4,
   "gefunden: %d" % src.count("instance_auto_host("))

print(f"\n{'FEHLER' if fails else 'Alles gruen'} — {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
