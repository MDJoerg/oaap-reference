#!/usr/bin/env python3
"""Der Zwillings-Browser im Portal (RFC-0031 Bauplan Schritt 5).

Zwei Teile, wie schon bei Store/`store_view.py` (`test_store_view.py`)
und Store-Zugriff (`test_store_access.py`):

    `twin_view.py` ist reine Logik -- Regeln, keine Anfrage, kein
    Flask, kein Netzwerk -- und direkt testbar, importiert.

    `platform/services/portal/app.py`s eigene '/twin*'-Routen werden an
    der QUELLE gelesen (kein Flask-Client, kein laufender Knoten -- die
    Routen rufen den 'twin'-Dienst über das Netz, das hier nicht
    existiert): jede Route ruft den richtigen Wächter, und keine Route
    unter '/twin' fällt zwischen sie durch, genau das Muster
    `test_store_access.py` schon für '/store' durchsetzt.

Aufruf: python3 test/test_twin_browser.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL_DIR = os.path.join(HERE, "..", "platform", "services", "portal")
sys.path.insert(0, PORTAL_DIR)

import twin_view  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


# =========================================================== twin_view.py

print("=== origin_label: bekannte Formen, und niemals stumm ===")
ok("'tenant' (die Gruppen-Herkunft, schema-eigen) -> 'Mandant'",
   twin_view.origin_label("tenant") == "Mandant")
ok("'tenant:<id>' (die Typ-Registrierung, knotenweit) -> auch 'Mandant'",
   twin_view.origin_label("tenant:demo") == "Mandant")
ok("'app:crm' -> 'App crm'", twin_view.origin_label("app:crm") == "App crm")
ok("'model:kundenzufriedenheit' -> 'Datenmodell kundenzufriedenheit'",
   twin_view.origin_label("model:kundenzufriedenheit")
   == "Datenmodell kundenzufriedenheit")
ok("ein unbekannter Wert wird gezeigt, nicht verschluckt",
   twin_view.origin_label("mystery:x") == "mystery:x")
ok("leer wird '?' , nicht ein leerer String, der wie ein Fehler aussieht",
   twin_view.origin_label("") == "?")

print("\n=== object_types/group_types_on/attribute_defs: nach 'kind' und "
      "'on' gefiltert, sonst nichts angefasst ===")
TYPES = [
    {"key": "Firma", "kind": "object_types", "origin": "app:partnerverwaltung",
     "title": "Firma", "definition": {"key": "Firma"}},
    {"key": "Mitarbeiter", "kind": "object_types", "origin": "app:mitarbeiterverwaltung",
     "title": "Mitarbeiter", "definition": {"key": "Mitarbeiter"}},
    {"key": "crm.satisfaction", "kind": "group_types", "origin": "model:kundenzufriedenheit",
     "title": "Kundenzufriedenheit",
     "definition": {"key": "crm.satisfaction", "on": "Firma",
                    "attributes": ["SatisfactionScore"]}},
    {"key": "hr.core", "kind": "group_types", "origin": "app:mitarbeiterverwaltung",
     "title": "Personalkern",
     "definition": {"key": "hr.core", "on": "Mitarbeiter", "attributes": ["Email"]}},
    {"key": "SatisfactionScore", "kind": "attribute_types", "origin": "model:kundenzufriedenheit",
     "title": "Zufriedenheits-Punktzahl",
     "definition": {"key": "SatisfactionScore", "title": "Zufriedenheits-Punktzahl",
                    "value_type": "int"}},
]
ots = twin_view.object_types(TYPES)
ok("nur die beiden object_types, sortiert nach Titel",
   [t["key"] for t in ots] == ["Firma", "Mitarbeiter"])
gts = twin_view.group_types_on(TYPES, "Firma")
ok("nur die Gruppe, die WIRKLICH an 'Firma' hängt -- 'hr.core' (an "
   "'Mitarbeiter') fehlt", [t["key"] for t in gts] == ["crm.satisfaction"])
ok("attribute_defs liefert genau die Definition, unverändert",
   twin_view.attribute_defs(TYPES)["SatisfactionScore"]["value_type"] == "int")

print("\n=== base_group_key: der Merge-Suffix eines Zwillings-Loaders "
      "wird abgestreift, ein normaler Schlüssel bleibt unberührt ===")
ok("'crm.core' bleibt 'crm.core'", twin_view.base_group_key("crm.core") == "crm.core")
ok("'crm.core+1a2b3c4d' wird zu 'crm.core'",
   twin_view.base_group_key("crm.core+1a2b3c4d") == "crm.core")

print("\n=== addable_group_types: nur was zum Objekttyp passt UND noch "
      "nicht existiert (auch über den Merge-Suffix hinweg erkannt) ===")
ok("frisches Objekt vom Typ Firma, keine Gruppen -- crm.satisfaction ist addable",
   [t["key"] for t in twin_view.addable_group_types(TYPES, "Firma", [])]
   == ["crm.satisfaction"])
ok("schon vorhanden (exakter Schlüssel) -- nicht mehr addable",
   twin_view.addable_group_types(TYPES, "Firma", ["crm.satisfaction"]) == [])
ok("schon vorhanden, aber unter einem Merge-Suffix -- IMMER NOCH erkannt, "
   "sonst würde die Seite nach einem Merge dieselbe Gruppe doppelt anbieten",
   twin_view.addable_group_types(TYPES, "Firma", ["crm.satisfaction+deadbeef"]) == [])
ok("ein Objekt vom Typ Mitarbeiter bekommt crm.satisfaction NICHT "
   "angeboten (nur seine eigene 'hr.core')",
   [t["key"] for t in twin_view.addable_group_types(TYPES, "Mitarbeiter", [])]
   == ["hr.core"])

print("\n=== timeline: Aktivitäten aller Gruppen, ein flacher, "
      "zeitsortierter Strom ===")
groups = {
    "crm.core": {"origin": "app:partnerverwaltung", "attributes": {}, "relations": [],
                 "activities": [{"key": "PhoneCall", "status": "done",
                                 "started_at": None, "finished_at": "2026-01-05",
                                 "planned_start": None, "planned_end": None}]},
    "raci.assignments": {"origin": "app:raci", "attributes": {}, "relations": [],
                         "activities": [{"key": "Task", "status": "open",
                                        "started_at": None, "finished_at": None,
                                        "planned_start": "2026-01-01", "planned_end": None}]},
}
tl = twin_view.timeline(groups)
ok("beide Aktivitäten erscheinen, aus BEIDEN Gruppen", len(tl) == 2)
ok("zeitlich sortiert -- die geplante (früher) vor der erledigten "
   "(später) -- und jede trägt ihre eigene group_key mit",
   tl[0]["when"] == "2026-01-01" and tl[0]["group_key"] == "raci.assignments"
   and tl[1]["when"] == "2026-01-05")

print("\n=== parse_new_type_form: eine leere Zeile wird übersprungen, "
      "ein schlechter Schlüssel wird plain-Deutsch abgelehnt ===")


class FakeForm(dict):
    """Nachbildet nur, was parse_new_type_form von Werkzeugs MultiDict
    tatsächlich benutzt -- .get() (von dict) und .getlist()."""
    def getlist(self, key):
        return self.get(key, [])


body, err = twin_view.parse_new_type_form(FakeForm({
    "on": "Firma", "group_key": "crm.notiz", "group_title": "Notizen",
    "attr_key": ["Kommentar", ""], "attr_title": ["Kommentar", ""],
    "attr_value_type": ["text", ""],
}))
ok("eine leere Attribut-Zeile wird übersprungen, nicht als Fehler gemeldet",
   err is None and len(body["attributes"]) == 1
   and body["attributes"][0]["key"] == "Kommentar")
ok("die 'on'/'group_key'/'attributes'-Form passt exakt zu twin/app.py's "
   "eigenem twin_create_type-Body", set(body.keys()) == {"on", "group_key",
   "group_title", "attributes"})

_, err = twin_view.parse_new_type_form(FakeForm({
    "on": "Firma", "group_key": "NichtDotiert", "attr_key": ["X"],
    "attr_title": [""], "attr_value_type": [""]}))
ok("ein Gruppen-Schlüssel ohne Punkt wird auf Deutsch abgelehnt, VOR dem "
   "Aufruf des Zwillings", err is not None and "namespace.name" in err)

_, err = twin_view.parse_new_type_form(FakeForm({
    "on": "Firma", "group_key": "crm.notiz", "attr_key": [], "attr_title": [],
    "attr_value_type": []}))
ok("kein einziges Attribut -- abgelehnt, nicht eine leere Gruppe angelegt",
   err is not None and "Attribut" in err)

_, err = twin_view.parse_new_type_form(FakeForm({
    "on": "", "group_key": "crm.notiz", "attr_key": ["X"],
    "attr_title": [""], "attr_value_type": [""]}))
ok("kein Objekttyp gewählt -- abgelehnt", err is not None)


# ================================================== portal app.py Routen

APP_PY = os.path.join(PORTAL_DIR, "app.py")
with open(APP_PY, encoding="utf-8") as f:
    SRC = f.read()
TREE = ast.parse(SRC)


def function(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def body_of(name):
    node = function(name)
    return ast.get_source_segment(SRC, node) if node else ""


def routes():
    """{route path: function name} for every @app.get/@app.post -- same
    helper as test_store_access.py, read fresh here (no import between
    test files)."""
    found = {}
    for node in TREE.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("get", "post")
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "app"
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                found.setdefault(dec.args[0].value, node.name)
    return found


print("\n=== app.py: 'twin' Modul importiert, TWIN_INTERNAL trägt "
      "denselben Schlüssel wie INTERNAL (identity) ===")
ok("import twin_view", "import twin_view" in SRC)
ok("TWIN_INTERNAL liest INTERNAL_API_KEY, wie INTERNAL es tut",
   'TWIN_INTERNAL.headers["X-OAAP-Internal-Key"] = '
   'os.environ.get("INTERNAL_API_KEY", "")' in SRC)
call = body_of("_twin_call")
ok("_twin_call trägt die verifizierte Person weiter -- User/Tenant/Rollen, "
   "niemals aus der Anfrage selbst, sondern aus caller_name()/caller_"
   "scope()/caller_roles()", call and "caller_name()" in call
   and "caller_scope()[1]" in call and "caller_roles()" in call)

print("\n=== Drei Wächter, drei Rollenmengen, keine Überlappung "
      "die nicht gewollt ist ===")
g1, g2, g3 = body_of("require_twin"), body_of("require_twin_write"), body_of("require_twin_admin")
ok("require_twin: jede Mandantenrolle sieht (Bauplan: 'user sieht')",
   all(r in g1 for r in ("user", "keyuser", "admin", "tenant_admin")))
ok("require_twin_write: user fehlt -- nur admin/keyuser/tenant_admin "
   "pflegen Mandantengruppen", "keyuser" in g2 and "tenant_admin" in g2
   and '"user"' not in g2.replace("caller_roles()", ""))
ok("require_twin_admin: ausschließlich tenant_admin (Typen, Merge)",
   '"tenant_admin"' in g3 and '"admin"' not in g3 and '"keyuser"' not in g3)

print("\n=== Jede /twin-Route ruft einen der drei Wächter, keine fällt "
      "durch (dasselbe Muster wie test_store_access.py für /store) ===")
R = routes()
EXPECTED_GUARD = {
    "/twin": "require_twin",
    "/twin/<type_key>": "require_twin",
    "/twin/object/<obj_id>": "require_twin",
    "/twin/object/<obj_id>/save/<group_key>": "require_twin_write",
    "/twin/duplicates": "require_twin_admin",
    "/twin/duplicates/merge": "require_twin_admin",
    "/twin/duplicates/unmerge": "require_twin_admin",
    "/twin/types/new": "require_twin_admin",
}
for path, guard in EXPECTED_GUARD.items():
    fn = R.get(path)
    ok(f"{path} -> {guard}", bool(fn) and guard in body_of(fn), f"Funktion={fn}")

unguarded = []
for path, fn in R.items():
    if not path.startswith("/twin"):
        continue
    text = body_of(fn)
    if "require_twin" not in text:
        unguarded.append(f"{path} -> {fn}")
ok("jede /twin-Route ruft einen 'require_twin*'-Wächter", not unguarded, unguarded)

# /twin/types/new existiert zweimal (GET und POST) -- beide müssen halten.
new_type_fns = [n.name for n in TREE.body
               if isinstance(n, ast.FunctionDef)
               and any(isinstance(d, ast.Call) and d.args
                       and isinstance(d.args[0], ast.Constant)
                       and d.args[0].value == "/twin/types/new"
                       for d in n.decorator_list)]
ok("GET und POST auf /twin/types/new sind beide gesichert",
   len(new_type_fns) == 2
   and all("require_twin_admin" in body_of(fn) for fn in new_type_fns),
   new_type_fns)

print("\n=== Die Navigation zeigt 'Zwilling' nur mit einer Mandantenrolle "
      "-- ein server_admin ohne eigenen Mandanten sieht keinen Zwilling "
      "eines Mandanten, den er nicht hat ===")
ok("can_twin in der Navigation verdrahtet",
   '{% if can_twin %}<a href="/twin"' in SRC)
page_fn = body_of("page")
ok("page() setzt can_twin/can_twin_write/can_twin_admin aus denselben "
   "Rollen wie die drei Wächter oben, nicht aus einer eigenen Liste",
   "can_twin=" in page_fn and "can_twin_write=" in page_fn
   and "can_twin_admin=" in page_fn)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
