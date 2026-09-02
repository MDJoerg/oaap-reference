#!/usr/bin/env python3
"""Wer den Store sehen und daraus installieren darf.

RFC-0022 §4 gibt dem `tenant_admin` ausdruecklich "install and remove
app instances of their tenant". Solange der Katalog `server_admin`
verlangte, war dieses Recht nicht ausuebbar: Der Kunde sah keine App,
die er haette installieren koennen. Seit oaap.core.portal 0.3.12 ist
der Katalog fuer beide offen — die QUELLENLISTE dagegen nicht: Woher
Pakete kommen duerfen, entscheidet der Knotenbetreiber (2.6), und ein
Mandant darf sich keine eigene Quelle eintragen.

Zwei Rechte, zwei Waechter, und die Probe darauf, dass keine Route
zwischen ihnen durchfaellt: Eine NEUE Route unter /store muss einen von
beiden aufrufen — genau der Fehlerfall (jemand ergaenzt eine Route und
vergisst die Pruefung), gegen den `test_internal_api_guard.py` dasselbe
Muster verteidigt.

Geprueft wird an der Quelle, ohne Flask, ohne Container, ohne Knoten.

Aufruf: python3 test/test_store_access.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


with open(APP_PY, encoding="utf-8") as f:
    SRC = f.read()
TREE = ast.parse(SRC)


def function(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def body(name):
    node = function(name)
    return ast.get_source_segment(SRC, node) if node else ""


def routes():
    """{route path: function name} for every @app.get/@app.post."""
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


print("\nDie beiden Waechter")

cat_guard = body("require_store")
ok("den Katalog darf server_admin ODER tenant_admin sehen",
   "server_admin" in cat_guard and "tenant_admin" in cat_guard, cat_guard)
ok("und niemand sonst",
   "403" in cat_guard and "caller_roles()" in cat_guard, cat_guard)

src_guard = body("require_store_sources")
ok("Quellen bleiben allein beim server_admin",
   "server_admin" in src_guard and "tenant_admin" not in src_guard, src_guard)
ok("auch die Quellen antworten mit 403", "403" in src_guard, src_guard)

print("\nJede Route am richtigen Waechter")

R = routes()
EXPECTED = {
    "/store": "require_store",
    "/store/<source_id>/<app_id>": "require_store",
    "/store/install": "require_store",
    "/store/sources": "require_store_sources",
}
for path, guard in EXPECTED.items():
    fn = R.get(path)
    ok(f"{path} ruft {guard}", bool(fn) and guard in body(fn),
       f"Funktion={fn}")

# /store/sources gibt es zweimal (GET und POST) — beide muessen halten.
sources_fns = [n.name for n in TREE.body
               if isinstance(n, ast.FunctionDef)
               and any(isinstance(d, ast.Call) and d.args
                       and isinstance(d.args[0], ast.Constant)
                       and d.args[0].value == "/store/sources"
                       for d in n.decorator_list)]
ok("GET und POST auf /store/sources sind beide gesichert",
   len(sources_fns) == 2
   and all("require_store_sources" in body(fn) for fn in sources_fns),
   sources_fns)

print("\nKeine Route faellt zwischen die beiden")

unguarded = []
for path, fn in R.items():
    if not path.startswith("/store"):
        continue
    text = body(fn)
    if "require_store" not in text:
        unguarded.append(f"{path} -> {fn}")
ok("jede /store-Route ruft einen der beiden Waechter", not unguarded, unguarded)

print("\nDer Katalog liest durch die Mandantengrenze")

cat = body("store_catalogue")
ok("'installiert' kommt aus visible_instances(), nicht aus dem ganzen Register",
   "visible_instances()" in cat and "load_instances()" not in cat, cat)

pend = body("pending_installs")
ok("laufende Installationen ebenso", "visible_instances()" in pend, pend)

print("\nDie Navigation zeigt, was das Recht hergibt")

ok("der Menuepunkt Store haengt an can_store",
   '{% if can_store %}<a href="/store"' in SRC)
ok("der Knopf 'Quellen' haengt an can_sources",
   "{% if can_sources %}" in SRC and 'href="/store/sources">Quellen' in SRC)
ok("can_store meint server_admin oder tenant_admin",
   'can_store=bool(caller & {"server_admin", "tenant_admin"})' in SRC)
ok("can_sources meint nur server_admin",
   'can_sources="server_admin" in caller_roles()' in SRC)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
