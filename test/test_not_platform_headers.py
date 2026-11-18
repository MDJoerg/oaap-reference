"""Kopfzeilen mit unserem Praefix, die die Plattform nie setzt (0.1.296).

    python3 test/test_not_platform_headers.py

Anlass (gemessen auf oaap-test, 2026-11-17): ein vom Client geschicktes
X-OAAP-Groups und X-OAAP-Tenant kam unveraendert bei der App an. Der
Schutz gegen gefaelschte Kopfzeilen deckte nur die Namen ab, die das
Gateway kopiert; der Marketplace glaubte die Gruppen.

Was hier gehalten wird:

1. Jede Instanz-Site streicht NOT_PLATFORM_HEADERS auf Site-Ebene --
   also vor jedem handle-Block und damit vor forward_auth (Caddys
   Reihenfolge). Innerhalb eines handle-Blocks wuerde request_header
   NACH forward_auth laufen; dort darf diese Zeile nicht stehen.
2. Die Liste ist keine Praefix-Regel: die Act-Token-Kopfzeile einer
   MCP-Tuer schickt der Client an das Gateway, und identity liest sie.
3. Die Freigabe-Zone (Exposures) streicht dieselben Namen.
4. Die Migration erkennt Site-Dateien ohne die Zeilen, auch in den
   Sammeldateien, und haelt die Portal-Site (ohne Instanz-Marke) nicht
   fuer veraltet.
5. Die Plattform setzt selbst nirgends X-OAAP-Groups.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-notplatform-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


ROUTES = [{"path": "/", "roles": ["user", "admin"]},
          {"path": "/display", "roles": ["public"]}]
STRIPS = [f"request_header -{h}" for h in m.NOT_PLATFORM_HEADERS]

print("1. Site-Ebene, in keinem Block")
body = m.site_body(ROUTES, "c", 8000, scope="i", tenant="t")


def depth_at(lines, idx):
    """Klammertiefe vor Zeile idx: 0 = Site-Ebene. Caddy sortiert die
    Direktiven der Site-Ebene selbst -- request_header vor handle --, die
    Stelle in der Datei ist also gleich; ein Block ist es nicht."""
    d = 0
    for ln in lines[:idx]:
        d += ln.count("{") - ln.count("}")
    return d


for s in STRIPS:
    at = [i for i, ln in enumerate(body) if ln == "\t" + s]
    ok(f"genau einmal auf Site-Ebene: {s}", len(at) == 1, at)
    ok("  ... in keinem Block (Site-Ebene laeuft vor forward_auth)",
       bool(at) and depth_at(body, at[0]) == 0, at)
    ok("  ... und nie eingerueckt (in einem Block liefe sie nach forward_auth)",
       not any(ln.strip() == s and ln != "\t" + s for ln in body))
ok("X-OAAP-Groups und X-OAAP-Tenant stehen auf der Liste",
   set(m.NOT_PLATFORM_HEADERS) >= {"X-OAAP-Groups", "X-OAAP-Tenant"})
ok("die geschuetzte Route kopiert weiter die geprueften Kopfzeilen",
   "\t\t\t" + m._COPY_IDENTITY in body)

print("")
print("Auch die Generalprobe und eine Instanz in Wartung")
lo = m.site_body(ROUTES, "c", 8000, scope="i", tenant="t", login_only=True)
ok("login_only traegt die Zeilen", all("\t" + s in lo for s in STRIPS))

print("")
print("2. Keine Praefix-Regel")
ok("kein request_header -X-OAAP-* irgendwo",
   not any(ln.strip().startswith("request_header -X-OAAP-*") for ln in body))
ok("die Act-Token-Kopfzeile steht nicht auf der Liste",
   "X-OAAP-Act-Token" not in m.NOT_PLATFORM_HEADERS
   and "X-OAAP-Act-Assertion" not in m.NOT_PLATFORM_HEADERS)
ok("keine Identitaets-Kopfzeile steht auf der Liste",
   not set(m.NOT_PLATFORM_HEADERS) & set(m.IDENTITY_HEADERS))

print("")
print("3. Die Freigabe-Zone")
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
fn = src.split("def write_exposures_caddy():")[1].split("\ndef ")[0]
ok("write_exposures_caddy streicht die Namen", "strip_not_platform()" in fn)
ok("  ... auf Site-Ebene, vor dem ersten handle-Block",
   fn.index("strip_not_platform()") < fn.index('"\\thandle /auth/* {"'))

print("")
print("4. Die Migration")
joined = "\n".join(body)
ok("eine frisch erzeugte Datei gilt als aktuell",
   not m._site_identity_stale(joined))
old = "\n".join(ln for ln in body if ln.strip() not in STRIPS)
ok("eine Datei ohne die Zeilen gilt als alt", m._site_identity_stale(old))
ok("eine Sammeldatei mit zwei Koerpern und nur einer Zeile gilt als alt",
   m._site_identity_stale(joined + "\n" + old))
ok("eine Sammeldatei mit zwei aktuellen Koerpern ist aktuell",
   not m._site_identity_stale(joined + "\n" + joined))
portal = "\n".join(m._portal_site_body())
ok("die Portal-Site allein gilt nicht als alt",
   not m._site_identity_stale(portal),
   "sonst schreibt jedes Update alles neu und laedt das Gateway")
ok("Portal-Site plus aktuelle Instanz ist aktuell",
   not m._site_identity_stale(portal + "\n" + joined))
mig = src.split("def cmd_migrate_identity_headers")[1].split("\ndef ")[0]
ok("die Migration schreibt auch die Freigabe-Zone nach",
   "write_exposures_caddy()" in mig)

print("")
print("5. Die Plattform setzt kein X-OAAP-Groups")
for path in ("appctl.py", "Caddyfile"):
    s = open(os.path.join(HERE, "..", "platform", path), encoding="utf-8").read()
    ok(f"{path}: kein header_up/copy_headers mit X-OAAP-Groups",
       not any(("header_up X-OAAP-Groups" in ln or
                ("copy_headers" in ln and "X-OAAP-Groups" in ln))
               for ln in s.splitlines()))

print("")
print("ALLE BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
