#!/usr/bin/env python3
"""Instanzliste sortieren (Jörg, 27.11.: „sortierte Liste für die Übersicht
der Instanzen“).

Vorher galt die Reihenfolge der Registry-Schlüssel (`<mandant>-<name>`),
angezeigt wird aber der Name ohne Mandant -- mit mehreren Mandanten sah die
Liste ungeordnet aus.

    - ohne Wahl: nach angezeigtem Namen, Gleichstand nach Mandant
    - jede Spalte sortiert nach ihrem Inhalt, Gleichstand nach Name
    - absteigend dreht nur die Spalte, der Gleichstand bleibt aufsteigend
    - Pflege: Sicherheitsmeldung vor Update vor Basis vor nichts
    - unbekannte Spalte -> Name
    - Kopfzeilen-Links behalten den Filter, zweiter Klick dreht die Richtung
    - Mandantenspalte nur für den Betreiber mit mehreren Mandanten
    - die Wahl wird mit dem Filter gemerkt (Cookie) und überlebt „Filtern“

Aufruf (im Portal-Abbild): python3 test/test_instances_sort_portal.py
"""
import os
import re
import sys
import tempfile
from urllib.parse import parse_qsl, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-inst-sort-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal  # noqa: E402
import store_view as sv  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


def row(key, name, tenant, app="Chat", state=("ok", "Läuft"), channel="production",
        care=None):
    return {"key": key, "name": name, "tenant": tenant, "app_name": app,
            "status": {"state": state[0], "label": state[1], "detail": ""},
            "channel": channel,
            "channel_label": {"production": "Produktiv", "test": "Test"}[channel],
            "care": care or {"newer": "", "open": 0, "behind": False}}


R = [
    row("pxx-wiki", "wiki", "pxx", app="Wiki"),
    row("csa-chat", "chat", "csa"),
    row("pxx-chat", "chat", "pxx", state=("down", "Gestoppt")),
    row("csa-Alpha", "Alpha", "csa", app="Vorgänge", channel="test",
        care={"newer": "1.2", "open": 0, "behind": False}),
    row("ais-zeta", "zeta", "ais", app="Wiki",
        care={"newer": "", "open": 3, "behind": False}),
]


def keys(rows):
    return [r["key"] for r in rows]


ok("Standard: nach Name (ohne Groß/Klein), Gleichstand Mandant",
   keys(sv.sort_instances(R, "name")) == ["csa-Alpha", "csa-chat", "pxx-chat", "pxx-wiki", "ais-zeta"],
   keys(sv.sort_instances(R, "name")))
ok("Name absteigend",
   keys(sv.sort_instances(R, "name", True)) == ["ais-zeta", "pxx-wiki", "pxx-chat", "csa-chat", "csa-Alpha"],
   keys(sv.sort_instances(R, "name", True)))
ok("App, Gleichstand nach Name",
   keys(sv.sort_instances(R, "app")) == ["csa-chat", "pxx-chat", "csa-Alpha", "pxx-wiki", "ais-zeta"],
   keys(sv.sort_instances(R, "app")))
ok("App absteigend: Spalte dreht, Gleichstand bleibt aufsteigend",
   keys(sv.sort_instances(R, "app", True)) == ["pxx-wiki", "ais-zeta", "csa-Alpha", "csa-chat", "pxx-chat"],
   keys(sv.sort_instances(R, "app", True)))
ok("Mandant",
   keys(sv.sort_instances(R, "tenant")) == ["ais-zeta", "csa-Alpha", "csa-chat", "pxx-chat", "pxx-wiki"],
   keys(sv.sort_instances(R, "tenant")))
ok("Status nach Text",
   keys(sv.sort_instances(R, "status"))[0] == "pxx-chat", keys(sv.sort_instances(R, "status")))
ok("Kanal: Produktiv vor Test",
   keys(sv.sort_instances(R, "channel"))[-1] == "csa-Alpha", keys(sv.sort_instances(R, "channel")))
ok("Pflege: Sicherheitsmeldung, dann Update, dann der Rest nach Name",
   keys(sv.sort_instances(R, "care")) == ["ais-zeta", "csa-Alpha", "csa-chat", "pxx-chat", "pxx-wiki"],
   keys(sv.sort_instances(R, "care")))
ok("unbekannte Spalte -> Name",
   sv.sort_state({"sort": "evil"}) == ("name", False) and sv.sort_state({"sort": "app", "dir": "desc"}) == ("app", True))

L = sv.sort_links([("tenant", "pxx"), ("sort", "app"), ("q", "")], "app", False)
q = dict(parse_qsl(urlparse(L["app"]["href"]).query))
ok("aktive Spalte: Link dreht auf absteigend, Filter bleibt, leere Felder fallen weg",
   q == {"tenant": "pxx", "sort": "app", "dir": "desc"} and L["app"]["arrow"] == "▲", L["app"])
q2 = dict(parse_qsl(urlparse(L["name"]["href"]).query))
ok("andere Spalte: aufsteigend, ohne Pfeil", q2 == {"tenant": "pxx", "sort": "name"}
   and not L["name"]["arrow"] and not L["name"]["active"], L["name"])
L2 = sv.sort_links([], "app", True)
ok("absteigend aktiv: Pfeil ▼, Klick geht zurück auf aufsteigend",
   L2["app"]["arrow"] == "▼" and "dir=desc" not in L2["app"]["href"], L2["app"])

# ---- im Portal ----
INST = {
    "pxx-wiki": {"app_id": "wiki", "app_name": "Wiki", "version": "1", "tenant": "pxx"},
    "csa-chat": {"app_id": "chat", "app_name": "Chat", "version": "1", "tenant": "csa"},
    "pxx-chat": {"app_id": "chat", "app_name": "Chat", "version": "1", "tenant": "pxx"},
    "ais-alpha": {"app_id": "vorgaenge", "app_name": "Vorgänge", "version": "1",
                  "tenant": "ais", "channel": "test"},
}
portal.visible_instances = lambda: dict(INST)
portal.load_instances = lambda: dict(INST)
portal.care_facts = lambda: {}
portal.catalogue_versions = lambda: {}
portal.pending_installs = lambda: set()
portal.node_profiles = lambda: []
portal.caller_roles = lambda: {"server_admin"}
portal.caller_scope = lambda: ("server_admin", None)
portal.multi_tenant = lambda: True
portal.resolve_tenant = lambda ref: ref or ""
portal.tenant_label = lambda t: t or ""
portal.local_name = lambda name, inst: name.split("-", 1)[1]
portal.fleet_view.probe_all = lambda pairs, probe: {n: ("ok", "Läuft", "") for n, _ in pairs}

c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}


def order(html):
    return re.findall(r'href="/instances/([a-z-]+)">Bearbeiten', html)


r = c.get("/instances", headers=H)
html = r.get_data(as_text=True)
ok("Seite ohne Wahl: nach Name", order(html) == ["ais-alpha", "csa-chat", "pxx-chat", "pxx-wiki"],
   order(html))
ok("Kopfzeile hat Sortier-Links inkl. Mandant", 'href="/instances?sort=tenant"' in html
   and 'aria-sort="ascending"' in html, re.findall(r'<th[^>]*>.{0,120}', html)[:3])
r = c.get("/instances?sort=app&dir=desc", headers=H)
html = r.get_data(as_text=True)
ok("App absteigend im Portal", order(html) == ["pxx-wiki", "ais-alpha", "csa-chat", "pxx-chat"],
   order(html))
ok("Filter-Formular trägt die Sortierung weiter",
   'name="sort" value="app"' in html and 'name="dir" value="desc"' in html)
cookie = r.headers.get("Set-Cookie", "")
ok("Sortierung wird mit dem Filter gemerkt", "sort%3Dapp" in cookie or "sort=app" in cookie, cookie)
c2 = portal.app.test_client()
c2.set_cookie("oaap_instances_filter" if not hasattr(portal, "INSTANCES_FILTER_COOKIE")
              else portal.INSTANCES_FILTER_COOKIE, "sort=app&dir=desc", path="/instances")
r = c2.get("/instances?back=1", headers=H)
loc = r.headers.get("Location", "")
ok("Rückweg (back=1) stellt die Sortierung wieder her",
   dict(parse_qsl(urlparse(loc).query)) == {"sort": "app", "dir": "desc"}, loc)

portal.caller_scope = lambda: ("tenant_admin", "pxx")
portal.caller_roles = lambda: {"tenant_admin"}
html = c.get("/instances", headers={"X-OAAP-User": "m", "X-OAAP-Roles": "tenant_admin"}).get_data(as_text=True)
ok("Mandanten-Admin: keine Mandantenspalte zum Sortieren", "sort=tenant" not in html)

print(f"\n{'OK' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
