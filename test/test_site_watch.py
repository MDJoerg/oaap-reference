#!/usr/bin/env python3
"""Hinweise zur Webseite (I-81 Idee 5, Jörg 27.11.: „Idee 5“ --
Zeitreihen im Portal mit Benachrichtigungen).

    Schalter   `site_watch` am Instanz-Satz: an/aus, wer, seit; Update übernimmt
               ihn; CLI `oaap app site-watch`, Spool `site-watch`
    site.google    Googlebot seit 14 Tagen nicht da -- ab letztem Besuch,
                   frühestens ab Einschalten; ein Besuch schließt
    site.notfound  404 von Besuchern (nicht Automaten) der letzten 7 vollen
                   Tage >= 20 und >= 2x Wochenmittel von bis zu 28 Tagen davor;
                   offen bleibt sie bei >= 10 und >= 1,5x; erst ab 14 Tagen
                   Zählung (`nfp_from`)
    nur        Produktivinstanzen; ausgeschaltet schließt ohne Mail
    Portal     Karte „Hinweise zur Webseite“, Balken Suchmaschinen und 404 je Tag

Aufruf: python3 test/test_site_watch.py
"""
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-site-watch-")
os.environ["OAAP_DATA_DIR"] = DATA
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, PLATFORM)
sys.path.insert(0, os.path.join(PLATFORM, "services"))

import appctl as m                                            # noqa: E402
import notify                                                 # noqa: E402
import site_stats as ss                                       # noqa: E402
import instance_metrics                                       # noqa: E402

m.reload_gateway = lambda: None
m.refresh_destinations = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


DAY = 86400
NOW = (int(time.time()) // DAY) * DAY + 12 * 3600      # today, noon UTC


def dstr(i):
    """the day i days before today"""
    return time.strftime("%Y-%m-%d", time.gmtime(NOW - i * DAY))


print("Google lange nicht da")
ok("nie da, gerade eingeschaltet: nichts", ss.google_absent({}, NOW - 3600, NOW) is None)
ok("nie da, seit 14 Tagen an: offen", ss.google_absent({}, NOW - 14 * DAY, NOW) is not None)
st = {"bot_last": {"Googlebot": NOW - 20 * DAY}}
g = ss.google_absent(st, NOW - 30 * DAY, NOW)
ok("zuletzt vor 20 Tagen: 20 Tage, Datum im Text", g and g[0] == 20 and "zuletzt am" in g[1], g)
ok("frühestens ab Einschalten: vor 5 Tagen an -> nichts",
   ss.google_absent(st, NOW - 5 * DAY, NOW) is None)
ok("Besuch vor 13 Tagen: nichts",
   ss.google_absent({"bot_last": {"Googlebot": NOW - 13 * DAY}}, NOW - 99 * DAY, NOW) is None)
ok("Bing zählt nicht als Google",
   ss.google_absent({"bot_last": {"Bingbot": NOW}}, NOW - 30 * DAY, NOW) is not None)

print("\n404 steigen")


def upd(a, b):
    c = dict(a)
    c.update(b)
    return c


def nfstats(per_day, start_days_ago=40):
    """per_day: {days ago: people's 404}"""
    days = {dstr(i): {"nfp": n, "nf": {"/alt": n}} for i, n in per_day.items()}
    return {"nfp_from": dstr(start_days_ago), "days": days}


base = {i: 2 for i in range(8, 36)}                     # ~14 per week before
spike = upd(base, {i: 5 for i in range(1, 8)})       # 35 in the last 7 days
r = ss.notfound_rising(nfstats(spike), NOW)
ok("35 statt ~14 je Woche: offen, Zahl und Pfad", r and r[0] == 35 and "/alt" in r[1], r)
ok("Text: Mittel davor", r and "im Mittel 14 je Woche" in r[1], r and r[1])
ok("heute zählt nicht", ss.notfound_rising(nfstats(upd(spike, {0: 500})), NOW)[0] == 35)
low = upd(base, {i: 2 for i in range(1, 8)})
ok("gleichbleibend: nichts", ss.notfound_rising(nfstats(low), NOW) is None)
few = {i: 0 for i in range(8, 36)}
few.update({i: 2 for i in range(1, 8)})                 # 14 from 0: below 20
ok("unter 20 in der Woche: nichts, auch von 0 aus", ss.notfound_rising(nfstats(few), NOW) is None)
ok("ohne nfp_from: kein Urteil", ss.notfound_rising({"days": nfstats(spike)["days"]}, NOW) is None)
ok("erst 10 Tage gezählt: kein Urteil (Grundlinie < 7 Tage)",
   ss.notfound_rising(nfstats(spike, start_days_ago=10), NOW) is None)
ok("14 Tage gezählt: Urteil", ss.notfound_rising(nfstats(spike, start_days_ago=14), NOW) is not None)
keep = upd(base, {i: 3 for i in range(1, 8)})        # 21 -- 1.5x of 14 holds
ok("Hysterese: 21 öffnet nicht ...", ss.notfound_rising(nfstats(keep), NOW) is None)
ok("... hält aber offen", ss.notfound_rising(nfstats(keep), NOW, is_open=True) is not None)

print("\nZählung: 404 von Besuchern, nicht von Automaten")
FF = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"


def line(ua, status, path="/", ts=None):
    return json.dumps({"ts": ts or NOW, "status": status, "request": {
        "host": "web.example.org", "uri": path, "method": "GET",
        "headers": {"User-Agent": [ua]}}, "resp_headers": {"Content-Type": ["text/html"]}})


hostmap = {"web.example.org": "web"}
stats = {}
ss.fold(stats, [line(FF, 404, "/weg"), line(FF, 404, "/weg"),
                line("Mozilla/5.0 (compatible; Googlebot/2.1)", 404, "/weg"),
                line("python-requests/2.31", 404, "/wp-login.php")], hostmap, now=NOW)
d = stats["web"]["days"][dstr(0)]
ok("nfp zählt nur Besucher", d.get("nfp") == 2, d)
ok("nf zählt weiter alle", d["nf"] == {"/weg": 3, "/wp-login.php": 1}, d["nf"])
ok("nfp_from gesetzt", stats["web"].get("nfp_from") == dstr(0), stats["web"].get("nfp_from"))
b = tempfile.mkdtemp()
with open(os.path.join(b, ss.FILE), "w", encoding="utf-8") as f:
    json.dump(stats["web"], f)
s = ss.read(b, 7, now=NOW)
ok("read: Zeitreihen je Tag", len(s["series"]["nf"]) == 7 and s["series"]["nf"][-1] == (dstr(0), 4)
   and s["series"]["search"][-1] == (dstr(0), 1), s["series"])

print("\nKatalog")
for t in ("site.google", "site.notfound"):
    c = notify.CATALOGUE.get(t) or {}
    ok(f"{t}: Betrieb, Mandant, Lage, keine Erinnerung",
       c.get("category") == "operations" and c.get("scope") == "tenant" and c.get("matter") is True
       and notify.remind_hours(t) is None, c)
    ok(f"{t}: Mail führt in den Reiter", m.NOTIFY_PAGES.get(t) == "/instances/{instanz}?tab=statistik")
    ok(f"{t}: darf nur in den Wochenbericht", t not in notify.NEVER_REPORT_ONLY)
ok("Betreff mit Instanz und Zahl", notify.expand(notify.CATALOGUE["site.google"]["subject"],
   {"mandant": "bec", "instanz": "web", "kurz": "15"}) == "[bec] web: Google seit 15 Tagen nicht da")

print("\nSchalter am Satz")


def record(name, **over):
    r = {"app_id": "website", "app_name": "Webseite", "version": "0.1.0",
         "channel": "production", "port": 8400, "container": f"oaap-app-{name}",
         "image": "oaap-app/website:0.1.0", "svc_port": 8080, "services": [],
         "routes": [{"path": "/", "roles": ["public"]}], "storage": [], "config": [],
         "roles": [], "visibility": {}, "id": (name * 12)[:12], "name": name}
    r.update(over)
    return r


tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["web"] = record("web", tenant=tid)
reg["instances"]["probe"] = record("probe", tenant=tid, channel="test")
m.save_registry(reg)
msg = m.site_watch_set("web", m.load_registry()["instances"]["web"], True, who="joerg",
                       role="server_admin")
rec = m.load_registry()["instances"]["web"]
ok("an: Satz trägt an, wer, seit", rec.get("site_watch", {}).get("on") is True
   and rec["site_watch"]["by"] == "joerg" and rec["site_watch"]["since"], rec.get("site_watch"))
ok("Antwort nennt beide Lagen", "site.google" in msg and "site.notfound" in msg, msg)
ok("Wiederholung ändert nichts", "already" in m.site_watch_set(
    "web", m.load_registry()["instances"]["web"], True))
msg_t = m.site_watch_set("probe", m.load_registry()["instances"]["probe"], True, who="joerg")
ok("Test-Instanz: Antwort sagt, dass erst produktiv gemeldet wird", "noindex" in msg_t, msg_t)
SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("Update übernimmt den Schalter", 'reg["instances"][name]["site_watch"] = dict(inst["site_watch"])' in SRC)
ok("Spool-Tür und CLI", 'action == "site-watch"' in SRC and 'sub.add_parser("site-watch"' in SRC)

print("\nBedingungen der Minute")


def put_stats(name, d):
    p = instance_metrics.instance_dir(m.METRICS_DIR, tid, name)
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, ss.FILE), "w", encoding="utf-8") as f:
        json.dump(dict({"v": ss.FORMAT, "days": {}, "bot_last": {}}, **d), f)


# switched on 30 days ago; Google 20 days ago; 404 spike
reg = m.load_registry()
for n in ("web", "probe"):
    reg["instances"][n]["site_watch"]["since"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30 * DAY))
m.save_registry(reg)
sp = nfstats(spike)
put_stats("web", {"bot_last": {"Googlebot": NOW - 20 * DAY}, **sp})
put_stats("probe", {**sp})
now_dt = notify._parse(time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW)))
cur = m.site_watch_conditions(now_dt)
mine = cur.get(tid) or {}
ok("web: beide Lagen", set(mine) == {"site.google/web", "site.notfound/web"}, mine.keys())
ok("Test-Instanz nie", not any("probe" in k for k in mine))
ok("kurz: Tage und Zahl", mine.get("site.google/web", {}).get("kurz") == "20"
   and mine.get("site.notfound/web", {}).get("kurz") == "35", mine)
ok("in den Lagen des Minutentakts", "site.google/web" in (m.notify_conditions({}, now_dt).get(tid) or {}))
n_open = m.notify_matters({}, now_dt)
open_now = (m._notify_state_rw().get("matters") or {}).get(tid) or {}
ok("Lagen geöffnet", "site.google/web" in open_now and "site.notfound/web" in open_now, open_now.keys())
m.site_watch_set("web", m.load_registry()["instances"]["web"], False, who="joerg")
ok("aus: Satz ohne Schalter", "site_watch" not in m.load_registry()["instances"]["web"])
ok("aus: keine Bedingung mehr", not (m.site_watch_conditions(now_dt).get(tid) or {}))
outbox_before = json.dumps(m._notify_state_rw(), sort_keys=True, default=str)
q = m.notify_matters({}, now_dt)
open_after = (m._notify_state_rw().get("matters") or {}).get(tid) or {}
ok("aus: Lagen geschlossen, ohne Mail", q == 0 and "site.google/web" not in open_after,
   (q, list(open_after)))

print("\nPortal")
sys.path.insert(0, os.path.join(PLATFORM, "services", "portal"))
try:
    import app as portal  # noqa: E402
    from jinja2 import Environment
    src = portal.INSTANCE_EDIT_BODY
    a = src.index("<section class=\"panel {{ 'active' if tab == 'statistik' }}\">")
    bb = src.index("</section>", a) + len("</section>")
    t = Environment(autoescape=True).from_string(src[a:bb])
    kw = dict(tab="statistik", device_titles=ss.DEVICE_TITLES, kind_titles=ss.KIND_TITLES,
              site_keep_days=ss.DAYS)
    mode = portal._mode_view({"channel": "production", "site_watch": {
        "on": True, "since": "2026-11-27T10:00:00Z", "by": "joerg"}})
    html = t.render(i={"key": "web", "site": s, "site_days": 7, "mode": mode}, **kw)
    ok("Karte: an, wer, seit, Ausschalten",
       "Hinweise zur Webseite" in html and "joerg" in html and "2026-11-27 10:00:00" in html
       and "Hinweise ausschalten" in html and 'name="op" value="off"' in html, html[:300])
    ok("Formular an die Tür, zurück in den Reiter",
       'action="/instances/web/site-watch"' in html and 'name="tab" value="statistik"' in html)
    ok("zwei Zeitreihen", "Anfragen von Suchmaschinen je Tag" in html
       and "Seite nicht gefunden (404) je Tag" in html and html.count("st-small") >= 2)
    ok("Mehrzahl richtig", "Anfragen\"" in html and "Anfrageen" not in html)
    html0 = t.render(i={"key": "web", "site": None, "site_days": 30,
                        "mode": portal._mode_view({"channel": "test"})}, **kw)
    ok("aus + Test-Instanz: Einschalten, Hinweis noindex",
       "Hinweise einschalten" in html0 and "Test-Instanz" in html0, html0[:200])
    ok("Tür im Portal", any(r.rule == "/instances/<name>/site-watch" for r in portal.app.url_map.iter_rules()))
except ImportError as e:
    print("SKIP  Portal (Flask fehlt):", e)

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
