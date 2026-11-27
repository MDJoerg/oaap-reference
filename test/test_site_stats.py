#!/usr/bin/env python3
"""Seitenstatistik je Instanz aus dem Zugriffsprotokoll (I-81 Idee 2+3,
Jörg 27.11.: „Bau die Seitenstatistik aus I-81“).

    - ein Aufruf ist GET, 2xx/304, text/html, nicht /platform/*, kein Automat
    - Bilder/Skripte, POST, Weiterleitungen zur Anmeldung zählen nicht
    - 404 je Pfad, Suchteil der Adresse wird nie abgelegt
    - Herkunft nur fremde Hosts; eigene Adressen und Aliasse nicht
    - Geräteklasse grob aus dem User-Agent
    - Suchmaschinen/KI nach Kennung, „zuletzt gesehen“ über alle Tage
    - unbekannter Host, kaputte Zeile, falsche Typen: nichts gezählt
    - ein Host, den zwei Instanzen beanspruchen, zählt für keine
    - Listen je Tag begrenzt, alte Tage fallen nach DAYS heraus
    - keine IP, kein Benutzer, kein User-Agent in der Datei
    - sample(): schreibt neben die Verlaufsdaten, liest nur betroffene Dateien
    - read(): Zeitraum, Summen, Reihenfolge
    - Portal: Reiter Seitenstatistik zeigt die Zahlen, leer einen Hinweis

Aufruf: python3 test/test_site_stats.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import site_stats as ss  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


NOW = 1795780800.0          # 2026-11-27 12:00 UTC
DAY = time.strftime("%Y-%m-%d", time.gmtime(NOW))
FF = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
IPAD = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
GOOGLE = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
GPT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"
CLAUDE = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"


def line(host="www.example.org", uri="/", status=200, method="GET", ua=FF, ctype="text/html; charset=utf-8",
         ref=None, ts=NOW, extra_hdr=None, ip="203.0.113.7", user=""):
    h = {"User-Agent": [ua]} if ua is not None else {}
    if ref:
        h["Referer"] = [ref]
    h.update(extra_hdr or {})
    rh = {"Content-Type": [ctype]} if ctype else {}
    return json.dumps({"level": "info", "ts": ts, "logger": "http.log.access", "msg": "handled request",
                       "request": {"remote_ip": ip, "client_ip": ip, "proto": "HTTP/2.0", "method": method,
                                   "host": host, "uri": uri, "headers": h},
                       "user_id": user, "duration": 0.01, "size": 1234, "status": status,
                       "resp_headers": rh})


HM = {"www.example.org": "web-site", "example.org": "web-site", "site.oaap.test": "web-site",
      "wiki.example.org": "web-wiki"}

L = [
    line(),
    line(uri="/kontakt?token=geheim123", ref="https://www.google.com/"),
    line(uri="/kontakt", ua=IPHONE, ref="https://example.org/"),        # eigener Alias -> keine Herkunft
    line(uri="/preise", ua=IPAD, ref="https://news.example.net/artikel?id=5"),
    line(uri="/style.css", ctype="text/css"),
    line(uri="/logo.png", ctype="image/png"),
    line(uri="/kontakt", method="POST"),
    line(uri="/intern", status=303, ctype=""),
    line(uri="/platform/login", ctype="text/html"),
    line(uri="/gibtsnicht", status=404),
    line(uri="/gibtsnicht?x=1", status=404, ua=GOOGLE),
    line(uri="/", ua=GOOGLE),
    line(uri="/robots.txt", ua=GPT, ctype="text/plain"),
    line(uri="/", ua=CLAUDE, ts=NOW - 3600),
    line(uri="/", ua=None),                                           # ohne User-Agent: Automat
    line(uri="/", ua="curl/8.5.0"),
    line(uri="/", status=304, ctype="", extra_hdr={"Sec-Fetch-Dest": ["document"]}),
    line(uri="/", status=304, ctype=""),                              # 304 eines Bildes o. ä.
    line(host="fremd.example.com"),                                   # Host gehört niemandem
    line(host="WWW.EXAMPLE.ORG:443", uri="/gross"),                   # Groß/Port
    "kein json",
    json.dumps({"request": "x", "status": 200}),
    json.dumps({"request": {"host": "www.example.org", "method": "GET", "uri": "/"}, "status": "200"}),
    line(host="wiki.example.org", uri="/start", ua=FF, user="anna"),
]

stats = {}
changed = ss.fold(stats, L, HM, NOW)
d = stats.get("web-site", {}).get("days", {}).get(DAY, {})
ok("geändert: genau die zwei Instanzen", changed == {"web-site", "web-wiki"}, changed)
ok("Aufrufe: /, /kontakt x2, /preise, 304 als Dokument, /gross = 6", d.get("views") == 6, d)
ok("Seiten ohne Suchteil", d.get("pages") == {"/": 2, "/kontakt": 2, "/preise": 1, "/gross": 1}, d.get("pages"))
ok("Herkunft: nur fremde Hosts, ohne Pfad",
   d.get("ref") == {"www.google.com": 1, "news.example.net": 1}, d.get("ref"))
ok("Geräte grob", d.get("dev") == {"desktop": 4, "mobil": 1, "tablet": 1}, d.get("dev"))
ok("404 je Pfad, auch die des Automaten, ohne Suchteil", d.get("nf") == {"/gibtsnicht": 2}, d.get("nf"))
ok("Automaten nach Name",
   d.get("bots") == {"Googlebot": 2, "GPTBot": 1, "ClaudeBot": 1, "andere Automaten": 2}, d.get("bots"))
ok("Stunde des Aufrufs (UTC 12)", d.get("hours", [0] * 24)[12] == 6, d.get("hours"))
bl = stats["web-site"]["bot_last"]
ok("zuletzt gesehen je Automat", bl.get("Googlebot") == int(NOW) and bl.get("ClaudeBot") == int(NOW - 3600), bl)
blob = json.dumps(stats)
ok("keine IP, kein Benutzer, kein User-Agent, kein Suchteil in den Daten",
   "203.0.113" not in blob and "anna" not in blob and "Firefox" not in blob
   and "geheim" not in blob and "id=5" not in blob, blob[:300])
ok("angemeldeter Aufruf zählt als Aufruf, ohne Namen", stats["web-wiki"]["days"][DAY]["views"] == 1)

# geteilter Host
hm = ss.hostmap_of({"a": {"hosts": ["x.example", "a.example"]}, "b": {"hosts": ["X.example"]}})
ok("ein Host, den zwei Instanzen nennen, zählt für keine", hm == {"a.example": "a"}, hm)

# Grenzen
many = [line(uri=f"/seite{i}") for i in range(ss.MAX_KEYS + 20)]
st2 = {}
ss.fold(st2, many, HM, NOW)
pages = st2["web-site"]["days"][DAY]["pages"]
ok("Seiten je Tag begrenzt, Rest in (weitere)",
   len(pages) == ss.MAX_KEYS + 1 and pages.get(ss.OTHER) == 20 and st2["web-site"]["days"][DAY]["views"] == ss.MAX_KEYS + 20,
   (len(pages), pages.get(ss.OTHER)))
ok("langer Pfad gekürzt", len(ss._path("/" + "a" * 500)) == ss.PATH_LEN)

# Alte Tage
old_day = time.strftime("%Y-%m-%d", time.gmtime(NOW - (ss.DAYS + 2) * 86400))
st3 = {"web-site": ss.empty()}
st3["web-site"]["days"][old_day] = {"views": 3}
ch = ss.fold(st3, [], HM, NOW)
ok("Tage älter als DAYS fallen heraus", old_day not in st3["web-site"]["days"] and "web-site" in ch)

# sample() auf der Platte
base = tempfile.mkdtemp(prefix="oaap-site-")


def idir(b, tenant, key):
    return os.path.join(b, "tenants", tenant, key)


targets = {"web-site": {"tenant": "T1", "hosts": ["www.example.org", "example.org"]},
           "web-wiki": {"tenant": "T1", "hosts": ["wiki.example.org"]},
           "web-still": {"tenant": "T1", "hosts": ["still.example.org"]},
           "kaputt": {"tenant": "../x", "hosts": ["k.example.org"]}}
ss.sample(base, NOW, L, targets, idir)
f1 = os.path.join(idir(base, "T1", "web-site"), ss.FILE)
ok("Datei liegt neben den Verlaufsdaten der Instanz", os.path.isfile(f1), f1)
ok("eine Instanz ohne Zugriff bekommt keine leere Datei",
   not os.path.exists(os.path.join(idir(base, "T1", "web-still"), ss.FILE)))
ok("Mandant mit / wird nicht als Pfad benutzt", not os.path.exists(os.path.join(base, "x")))
ok("Tagesmarke gesetzt", open(os.path.join(base, ".site-folded")).read() == DAY)
before = os.path.getmtime(f1)
os.utime(f1, (before - 100, before - 100))
ss.sample(base, NOW + 60, [line(host="wiki.example.org", uri="/b", ts=NOW + 60)], targets, idir)
ok("nach der Tagesrunde: nur die betroffene Datei wird geschrieben",
   os.path.getmtime(f1) == before - 100, (os.path.getmtime(f1), before))
w = json.load(open(os.path.join(idir(base, "T1", "web-wiki"), ss.FILE)))
ok("betroffene Datei fortgeschrieben", w["days"][DAY]["views"] == 2, w["days"][DAY])
ss.sample(base, NOW + 120, [line(ts=NOW + 120)], targets, idir)
r1 = json.load(open(f1))
ok("zweite Runde addiert", r1["days"][DAY]["views"] == 7, r1["days"][DAY]["views"])

# read()
s = ss.read(idir(base, "T1", "web-site"), 7, now=NOW)
ok("read: 7 Tage, letzter ist heute", len(s["days"]) == 7 and s["days"][-1] == (DAY, 7), s["days"])
ok("read: Seiten absteigend", s["pages"][0] == ("/", 3), s["pages"][:3])
ok("read: Suchmaschinen und KI vor anderen Automaten",
   [b[0] for b in s["bots"]][-1] == "andere Automaten" and s["bots"][0][1] in ("search", "ai"), s["bots"])
ok("read: zuletzt als Datum lesbar", "UTC" in dict((b[0], b[3]) for b in s["bots"])["Googlebot"], s["bots"])
ok("read: nie gezählt -> None", ss.read(idir(base, "T1", "web-still"), 30, now=NOW) is None)

# Portal
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-site-portal-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
try:
    import app as portal  # noqa: E402
    from jinja2 import Environment
    src = portal.INSTANCE_EDIT_BODY
    a = src.index("<section class=\"panel {{ 'active' if tab == 'statistik' }}\">")
    b = src.index("</section>", a) + len("</section>")
    t = Environment(autoescape=True).from_string(src[a:b])
    html = t.render(i={"key": "web-site", "site": s, "site_days": 7, "mode": portal._mode_view({})}, tab="statistik",
                    device_titles=ss.DEVICE_TITLES, kind_titles=ss.KIND_TITLES, site_keep_days=ss.DAYS)
    ok("Reiter zeigt Aufrufe, Seiten, Herkunft, Bots",
       "Seitenaufrufe" in html and "<code>/kontakt</code>" in html and "www.google.com" in html
       and "Googlebot" in html and "Suchmaschine" in html and "active" in html, html[:400])
    # I-81 Idee 5: dazu je Tag Suchmaschinen und 404 -- drei Reihen
    ok("Balken je Tag (3 Reihen x 7)", html.count('title="2026-') == 21, html.count('title="2026-'))
    html0 = t.render(i={"key": "x", "site": None, "site_days": 30, "mode": portal._mode_view({})}, tab="diagnose",
                     device_titles=ss.DEVICE_TITLES, kind_titles=ss.KIND_TITLES, site_keep_days=ss.DAYS)
    ok("leer: Hinweis statt Zahlen, Reiter nicht aktiv",
       "Noch nichts gezählt" in html0 and "panel active" not in html0, html0[:300])
    ok("Reiter steht in der Liste", ("statistik", "Seitenstatistik") in portal.iv.TABS)
except ImportError as e:
    print("SKIP  Portal (Flask fehlt):", e)

print(f"\n{'OK' if not fails else f'{fails} FEHLER'}")
sys.exit(1 if fails else 0)
