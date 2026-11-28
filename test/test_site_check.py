#!/usr/bin/env python3
"""Seiten-Check (RFC-0141 Stufen 1–3, Jörg 28.11.: „Ok i-83“, D1–D7 wie empfohlen).

    Regeln     §3.1 Sicherheit, §3.2 Auffindbarkeit, §3.3 Tempo -- grün, gelb,
               rot und wer behebt (Plattform/Seite), an Beobachtungen ohne Netz
    Grenzen    höchstens 30 eigene Dateien, 5 Umleitungen auf demselben Namen,
               fremde Namen werden nie gelesen
    Befunde    F1 Gruppen, F2 no-transform = Seite, F3 Automat in site_stats,
               F4 Server-Alarm = abgelehnt, Client-Fehler = nicht prüfbar
    Schalter   `site_check` am Satz, nur Produktion, Update übernimmt ihn;
               CLI, Spool, Wochentag aus dem Namen, Knopf höchstens stündlich
    Portal     Karte „Seiten-Check“ im Reiter Seitenstatistik
    Stufe 2    §3.4 Recht (Impressum, Datenschutz, fremde Server, Cookies),
               „neu“/„behoben“, Meldung `site.check` nur über „Hinweise zur
               Webseite“ (öffnet bei neuem Rot, schließt ohne Rot), Wochenbericht
    Stufe 3    bis zu zehn Unterseiten aus Sitemap und Links: Verweise ins
               Leere, fehlende Titel, noindex in der Sitemap; Verlauf der Läufe

Aufruf: python3 test/test_site_check.py
"""
import json
import re
import os
import ssl
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-site-check-")
os.environ["OAAP_DATA_DIR"] = DATA
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, PLATFORM)
sys.path.insert(0, os.path.join(PLATFORM, "services"))

import site_check as sc                                       # noqa: E402
import site_stats as ss                                       # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def ids(findings):
    return {f["id"]: f for f in findings}


HOST = "verein.example.org"
GOOD_PAGE = (b'<!doctype html><html lang="de"><head><title>Verein</title>'
             b'<meta name="description" content="Unser Verein.">'
             b'<link rel="canonical" href="https://verein.example.org/">'
             b'<meta property="og:image" content="https://verein.example.org/logo.png">'
             b'<meta property="og:url" content="https://verein.example.org/">'
             b'<script type="application/ld+json">{}</script>'
             b'<link rel="stylesheet" href="/app.3f9a1c2e.css">'
             b'<script src="/app.7b2d9e41.js"></script>'
             b'</head><body><img src="/logo.png">'
             b'<footer><a href="/impressum">Impressum</a> <a href="/datenschutz">Datenschutz</a>'
             b'<a href="#oben">nach oben</a><a href="mailto:x@example.org">Mail</a></footer>'
             b'</body></html>')
SUB_PAGE = b'<html lang="de"><head><title>Unterseite</title></head><body>x</body></html>'

GOOD_HDR = {"content-type": "text/html; charset=utf-8", "content-encoding": "gzip",
            "strict-transport-security": "max-age=31536000",
            "x-content-type-options": "nosniff", "referrer-policy": "strict-origin",
            "content-security-policy": "default-src 'self'; frame-ancestors 'self'",
            "server": "Caddy"}
CACHED = {"cache-control": "public, max-age=31536000, immutable", "content-encoding": "gzip"}


class FakeProbe:
    """Answers from a table; counts and records what was asked."""

    def __init__(self, table, cert=(60, "TLSv1.3", ""), old="refused", v6="none", cookies=()):
        self.table, self._cert, self._old, self._v6 = table, cert, old, v6
        self._cookies = list(cookies)
        self.requests, self.bytes, self.asked = 0, 0, []

    def get(self, url, limit=sc.MAX_BYTES, method="GET"):
        self.requests += 1
        self.asked.append((method, url))
        status, h, body = self.table.get(url, (404, {"content-type": "text/plain"}, b""))
        raw = len(body)
        self.bytes += raw
        return {"status": status, "h": dict(h), "raw": raw, "body": body, "ttfb": 0.1,
                "big": False, "cookies": self._cookies if url == f"https://{HOST}/" else []}

    def cert(self, host):
        return self._cert

    def old_tls(self, host):
        return self._old

    def ipv6(self, host):
        return self._v6


def good_table():
    b = f"https://{HOST}/"
    return {
        b: (200, GOOD_HDR, GOOD_PAGE),
        f"http://{HOST}/": (308, {"location": b}, b""),
        b + "robots.txt": (200, {"content-type": "text/plain"},
                           b"User-agent: *\nAllow: /\nSitemap: https://verein.example.org/sitemap.xml\n"),
        b + "sitemap.xml": (200, {"content-type": "application/xml"},
                            b"<urlset><url><loc>https://verein.example.org/</loc></url></urlset>"),
        b + "app.3f9a1c2e.css": (200, dict(CACHED, **{"content-type": "text/css"}), b"x" * 2000),
        b + "app.7b2d9e41.js": (200, dict(CACHED, **{"content-type": "text/javascript"}), b"x" * 2000),
        b + "logo.png": (200, {"content-type": "image/png", "cache-control": "max-age=3600"},
                         b"p" * 1000),
        b + "impressum": (200, {"content-type": "text/html"}, SUB_PAGE),
        b + "datenschutz": (200, {"content-type": "text/html"}, SUB_PAGE),
    }


print("Eine gepflegte Seite")
fp = FakeProbe(good_table())
obs = sc.collect(HOST, [], probe=fp)
res = sc.result_of(obs, {"instance": "web", "compress": True, "headers": True})
ok("keine Punkte, alles grün", not res["findings"]
   and set(res["lights"].values()) == {"green"}, res["findings"])
ok("Anker, Mail-Links und Sprungmarken werden nicht als Unterseite gelesen",
   not any("#" in u or "mailto" in u for _m, u in fp.asked), fp.asked)
ok("Zahlen behalten, kein Text der Seite", res["n"]["requests"] == fp.requests
   and "Verein" not in json.dumps(res) and "body" not in json.dumps(obs), res["n"])
ok("Ziel steht im Ergebnis", res["host"] == HOST)

print("\nSicherheit (§3.1)")


def run(table=None, ctx=None, aliases=(), **kw):
    p = FakeProbe(table or good_table(), **kw)
    o = sc.collect(HOST, list(aliases), probe=p)
    return ids(sc.evaluate(o, ctx or {"instance": "web", "compress": True, "headers": True})), o, p


f, _o, _p = run(cert=(10, "TLSv1.3", ""))
ok("Zertifikat < 14 Tage: rot, Plattform", f.get("cert.expiry", {}).get("level") == "red"
   and f["cert.expiry"]["fix"] == "plattform", f.get("cert.expiry"))
f, _o, _p = run(cert=(20, "TLSv1.3", ""))
ok("Zertifikat < 30 Tage: gelb", f.get("cert.expiry", {}).get("level") == "yellow")
f, _o, _p = run(cert=(None, None, "SSLCertVerificationError: hostname mismatch"))
ok("Zertifikat ungültig: rot", f.get("cert.invalid", {}).get("level") == "red")
f, _o, _p = run(old="accepted")
ok("TLS 1.0/1.1 angenommen: rot", f.get("tls.old", {}).get("level") == "red")
f, o, _p = run(old="unknown")
ok("TLS nicht prüfbar: kein Punkt, aber Hinweis", "tls.old" not in f
   and any("TLS 1.0/1.1" in n for n in sc.result_of(o)["notes"]))
t = good_table()
t[f"http://{HOST}/"] = (200, {"content-type": "text/html"}, b"")
f, _o, _p = run(t)
ok("http:// liefert die Seite: rot, Plattform", f.get("http.serves", {}).get("level") == "red")
t = good_table()
t[f"https://{HOST}/"] = (200, {k: v for k, v in GOOD_HDR.items()
                               if k not in ("strict-transport-security", "content-security-policy")},
                         GOOD_PAGE)
f, _o, _p = run(t)
ok("HSTS fehlt bei an-Schalter: gelb, Seite", f.get("hdr.base", {}).get("fix") == "seite"
   and "HSTS" in f["hdr.base"]["found"], f.get("hdr.base"))
ok("keine CSP: gelb, Seite; damit auch kein Einbettschutz",
   f.get("hdr.csp", {}).get("level") == "yellow" and "hdr.frame" in f)
f, _o, _p = run(t, ctx={"instance": "web", "headers": False, "compress": True})
ok("Grund-Header ausgeschaltet: Plattform mit Befehl", f["hdr.base"]["fix"] == "plattform"
   and "oaap app hardening web --headers on" in f["hdr.base"]["todo"])
t = good_table()
t[f"https://{HOST}/"] = (200, dict(GOOD_HDR, server="marketplace/0.5.2 Python/3.13.18"), GOOD_PAGE)
f, _o, _p = run(t)
ok("F5: Server mit Version: gelb, Seite, informativ",
   f.get("hdr.version", {}).get("level") == "yellow" and "Kein Fehler" in f["hdr.version"]["why"])

print("\nF4: TLS-Urteil")


class E(ssl.SSLError):
    pass


e1 = ssl.SSLError(1, "[SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version")
e2 = ssl.SSLError(1, "[SSL: NO_PROTOCOLS_AVAILABLE] no protocols available")
ok("Alarm des Servers = abgelehnt", sc.tls_verdict(e1) == "refused")
ok("Fehler auf Client-Seite = nicht prüfbar, nie grün", sc.tls_verdict(e2) == "unknown")

print("\nAuffindbarkeit (§3.2)")
t = good_table()
t[f"https://{HOST}/"] = (200, dict(GOOD_HDR, **{"x-robots-tag": "noindex"}), GOOD_PAGE)
f, _o, _p = run(t)
ok("noindex: rot, Seite", f.get("seo.noindex", {}).get("level") == "red"
   and f["seo.noindex"]["fix"] == "seite")
t = good_table()
t[f"https://{HOST}/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /\n")
f, _o, _p = run(t)
ok("robots.txt sperrt alles: rot", f.get("seo.robots_block", {}).get("level") == "red")
t = good_table()
t[f"https://{HOST}/robots.txt"] = (200, {}, b"User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
f, _o, _p = run(t)
ok("Sperre nur für einen Bot: kein Rot", "seo.robots_block" not in f, f.keys())
t = good_table()
del t[f"https://{HOST}/robots.txt"]
del t[f"https://{HOST}/sitemap.xml"]
f, _o, _p = run(t)
ok("ohne robots.txt und Sitemap: zwei gelbe", f.get("seo.robots", {}).get("level") == "yellow"
   and f.get("seo.sitemap", {}).get("level") == "yellow")
t = good_table()
t[f"https://{HOST}/sitemap.xml"] = (200, {}, b"<urlset><url><loc>http://alt.example.net/a</loc></url></urlset>")
f, _o, _p = run(t)
ok("Sitemap nennt anderen Host: rot; http: gelb", f.get("seo.sitemap_foreign", {}).get("level") == "red"
   and "alt.example.net" in f["seo.sitemap_foreign"]["found"] and "seo.sitemap_http" in f)
t = good_table()
t[f"https://{HOST}/robots.txt"] = (200, {}, b"Sitemap: https://cdn.example.net/sitemap.xml\n")
f, _o, p = run(t)
ok("Sitemap auf fremdem Namen: gelb, nicht gelesen", "seo.sitemap_host" in f
   and not any("cdn.example.net" in u for _m, u in p.asked))
bare = (b'<html><head><link rel="canonical" href="https://www.example.org/">'
        b'<meta property="og:image" content="/logo.png"><title>' + b"T" * 80 + b"</title>"
        b"</head><body></body></html>")
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, bare)
f, _o, _p = run(t)
ok("Canonical auf anderen Host: rot", f.get("seo.canonical_host", {}).get("level") == "red")
ok("Titel > 70: gelb", f.get("seo.title_long", {}).get("level") == "yellow")
ok("ohne Beschreibung, og:url, ld+json, lang: gelb",
   all(k in f for k in ("seo.desc", "seo.og_url", "seo.ld", "seo.lang")), sorted(f))
ok("og:image relativ: gelb", "seo.og_image_host" in f)
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, b"<html><head></head><body>x</body></html>")
f, _o, _p = run(t)
ok("ohne Titel: rot", f.get("seo.title", {}).get("level") == "red")
t = good_table()
t[f"https://{HOST}/"] = (404, dict(GOOD_HDR, **{"x-robots-tag": "noindex"}), b"<html><body>weg</body></html>")
f, _o, _p = run(t)
ok("Startseite 404: ein Punkt dafür, keine Seitenregeln auf der Fehlerseite",
   "start.status" in f and not any(k in f for k in ("seo.title", "seo.canonical", "seo.desc")), sorted(f))
t[f"https://{HOST}/"] = (404, dict(GOOD_HDR, server="oaap-wegweiser/0.6.2"), b"<html></html>")
f, o404, _p = run(t)
ok("Header zählen auch auf der Fehlerseite (Server mit Version)", "hdr.version" in f, sorted(f))
r404 = sc.result_of(o404)
ok("Startseite 404: Recht grau mit Hinweis, nie grün (wegweiser auf oaap-test)",
   r404["lights"]["Recht"] == "grey" and any("Recht nicht geprüft" in n for n in r404["notes"]),
   (r404["lights"], r404["notes"]))
t = good_table()
t["https://www.verein.example.org/"] = (200, GOOD_HDR, GOOD_PAGE)
f, _o, _p = run(t, aliases=["www.verein.example.org"])
ok("Alias liefert aus: gelb, Plattform, Link zum Reiter",
   f.get("seo.alias", {}).get("fix") == "plattform" and f["seo.alias"]["link"] == "netz")
t["https://www.verein.example.org/"] = (301, {"location": f"https://{HOST}/"}, b"")
f, _o, _p = run(t, aliases=["www.verein.example.org"])
ok("Alias leitet um: kein Punkt", "seo.alias" not in f)
f, _o, _p = run(v6="refused")
ok("AAAA zeigt ins Leere: gelb", f.get("dns.ipv6", {}).get("level") == "yellow")
f, _o, _p = run(ctx={"instance": "web", "google_absent": True})
ok("Google lange nicht da: gelb, ohne Behebenden", f.get("seo.google", {}).get("fix") == "")

print("\nUmleitungen")
t = good_table()
t[f"https://{HOST}/"] = (302, {"location": "/de/"}, b"")
t[f"https://{HOST}/de/"] = (200, GOOD_HDR, GOOD_PAGE)
f, o, _p = run(t)
ok("Umleitung auf demselben Namen wird verfolgt", o["final"] == "/de/" and o["hops"] == 1
   and not f, (o.get("final"), sorted(f)))
t = good_table()
for i in range(10):
    t[f"https://{HOST}/r{i}" if i else f"https://{HOST}/"] = (302, {"location": f"/r{i + 1}"}, b"")
f, o, p = run(t)
ok("höchstens 5 Umleitungen", o["hops"] == sc.MAX_REDIRECTS
   and sum(1 for _m, u in p.asked if re.search(r"/r\d+$", u)) == sc.MAX_REDIRECTS, o.get("hops"))
t = good_table()
t[f"https://{HOST}/"] = (302, {"location": "https://login.example.net/"}, b"")
f, o, p = run(t)
ok("Umleitung auf fremden Namen: nicht verfolgt, gelber Punkt",
   o.get("offsite") == "login.example.net" and "start.offsite" in f
   and not any("login.example.net" in u for _m, u in p.asked))

print("\nTempo (§3.3)")


class SlowFirst(FakeProbe):
    """The first answer of the start page slow, every later one fast
    (oaap-test: a new name, the gateway still fetching its certificate)."""

    def __init__(self, table, slow=8.2, second=0.03, **kw):
        super().__init__(table, **kw)
        self.slow, self.second, self.n_start = slow, second, 0

    def get(self, url, limit=sc.MAX_BYTES, method="GET"):
        r = super().get(url, limit, method)
        if url == f"https://{HOST}/":
            self.n_start += 1
            r["ttfb"] = self.slow if self.n_start == 1 else self.second
        return r


sp = SlowFirst(good_table())
o = sc.collect(HOST, [], probe=sp)
f = ids(sc.evaluate(o, {"instance": "web"}))
ok("langsames erstes Byte: einmal nachgemessen, der bessere Wert zählt (kein Fehlalarm)",
   "speed.ttfb" not in f and o["ttfb"] == 0.03 and o["ttfb_first"] == 8.2 and sp.n_start == 2, o)
sp = SlowFirst(good_table(), slow=4.0, second=3.5)
o = sc.collect(HOST, [], probe=sp)
f = ids(sc.evaluate(o, {"instance": "web"}))
ok("bleibt langsam: rot", f.get("speed.ttfb", {}).get("level") == "red", f.get("speed.ttfb"))
fp_ = FakeProbe(good_table())
sc.collect(HOST, [], probe=fp_)
ok("schnelle Seite: keine zweite Anfrage", [u for _m, u in fp_.asked].count(f"https://{HOST}/") == 1)
many = (b'<html lang="de"><head><title>x</title></head><body>'
        + b"".join(b'<img src="/b%d.png">' % i for i in range(45)) + b"</body></html>")
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, many)
for i in range(45):
    t[f"https://{HOST}/b{i}.png"] = (200, {"content-type": "image/png"}, b"i" * 10)
f, o, p = run(t)
ok("höchstens 30 eigene Dateien gelesen, Rest gezählt",
   o["assets"]["named"] == 45 and o["assets"]["fetched"] == 30
   and sum(1 for _m, u in p.asked if "/b" in u) == 30, o["assets"])
t = good_table()
t[f"https://{HOST}/logo.png"] = (200, {"content-type": "image/png"}, b"p" * (2500 * 1024))
f, _o, _p = run(t)
ok("Gewicht > 2 MB: gelb", f.get("speed.weight", {}).get("level") == "yellow")
ok("Bild > 500 KB: gelb mit Name", "logo.png" in f.get("speed.images", {}).get("found", ""))
t[f"https://{HOST}/logo.png"] = (200, {"content-type": "image/png"}, b"p" * (6 * 1024 * 1024))
f, _o, _p = run(t)
ok("Gewicht > 5 MB: rot", f.get("speed.weight", {}).get("level") == "red")
t = good_table()
t[f"https://{HOST}/app.7b2d9e41.js"] = (200, {"content-type": "text/javascript"}, b"x" * 5000)
f, _o, _p = run(t)
ok("unkomprimiertes JS bei Kompression an: gelb, Seite",
   f.get("speed.plain", {}).get("fix") == "seite" and "app.7b2d9e41.js" in f["speed.plain"]["found"])
ok("gehashte Datei ohne Cache-Angabe: gelb", f.get("speed.cache", {}).get("level") == "yellow")
f, _o, _p = run(t, ctx={"instance": "web", "compress": False, "headers": True})
ok("Kompression aus: Plattform mit Befehl", f["speed.plain"]["fix"] == "plattform"
   and "--compress on" in f["speed.plain"]["todo"])
t[f"https://{HOST}/app.7b2d9e41.js"] = (200, {"content-type": "text/javascript",
                                               "cache-control": "max-age=60, no-transform"},
                                         b"x" * 5000)
f, _o, _p = run(t, ctx={"instance": "web", "compress": False, "headers": True})
ok("F2: no-transform der App: Seite, nie Plattform", f["speed.plain"]["fix"] == "seite"
   and "no-transform" in f["speed.plain"]["todo"])
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(b'<img src="/logo.png">',
                                                         b'<img src="/a.avif"><picture><img src="/b.webp"></picture>'))
f, _o, _p = run(t)
ok("AVIF ohne <picture>: gelb; im <picture>: nicht", "a.avif" in f.get("speed.formats", {}).get("found", "")
   and "b.webp" not in f["speed.formats"]["found"])
ok("Name mit Hash erkannt, ohne nicht", sc.hashed_name("/assets/index-B3x9kQ2a.js")
   and sc.hashed_name("/app.3f9a1c2e.css") and not sc.hashed_name("/logo.png")
   and not sc.hashed_name("/jquery.min.js"))

print("\nGrau und Ampel")
fp = FakeProbe({})
fp.get = lambda url, limit=0, method="GET": {"status": 0, "err": "TimeoutError: timed out",
                                             "h": {}, "raw": 0, "body": b"", "ttfb": 15,
                                             "big": False, "cookies": []}
r0 = sc.result_of(sc.collect(HOST, [], probe=fp))
ok("nicht erreichbar: grau, nicht rot", r0["grey"] and not r0["findings"]
   and set(r0["lights"].values()) == {"grey"}, r0)
lt = sc.lights([{"area": "Tempo", "level": "yellow"}, {"area": "Tempo", "level": "red"},
                {"area": "Sicherheit", "level": "yellow"}])
ok("Ampel: rot schlägt gelb", lt == {"Sicherheit": "yellow", "Auffindbarkeit": "green", "Tempo": "red",
                                    "Recht": "green"}, lt)

print("\nF1: Gruppen")
t = good_table()
t[f"https://{HOST}/"] = (200, dict(GOOD_HDR, **{"x-robots-tag": "noindex"}),
                         b"<html><head><title>App</title></head></html>")
del t[f"https://{HOST}/robots.txt"]
del t[f"https://{HOST}/sitemap.xml"]
f, o, _p = run(t)
rows = sc._rows(sc.evaluate(o, {"instance": "web"}))
grp = [r for r in rows if r["group"] and r["area"] == "Auffindbarkeit"]
ok("gelbe Auffindbarkeit/Seite gefaltet, Zahl im Titel", grp and grp[0]["title"].startswith(
   "Für Suchmaschinen kaum vorbereitet:") and len(grp[0]["points"]) >= 7, [r.get("title") for r in rows])
ok("rot bleibt einzeln", any(not r["group"] and r["f"]["id"] == "seo.noindex" for r in rows))

print("\nSpeicher und Wochentag")
d = tempfile.mkdtemp()
for i in range(15):
    sc.record(d, dict(res, ts=1000 + i))
ok("nur die letzten 12", len(sc.load(d)["runs"]) == sc.KEEP and sc.last_ts(d) == 1014)
v = sc.view(d, "web")
ok("Ansicht: letzter Lauf, Wochentag", v["last"]["ts"] == 1014 and v["weekday"] in sc.WEEKDAYS)
ok("leeres Verzeichnis: keine Ansicht", sc.view(tempfile.mkdtemp(), "x")["last"] is None)
spread = {sc.weekday_of(f"web{i}") for i in range(60)}
ok("Wochentage verteilt", len(spread) == 7, spread)
ok("Wochentag stabil", sc.weekday_of("bdt") == sc.weekday_of("bdt"))
DAY = 86400
now = 1764000000  # a fixed moment
wd_now = time.gmtime(now).tm_wday
name_today = next(f"w{i}" for i in range(100) if sc.weekday_of(f"w{i}") == wd_now)
name_other = next(f"w{i}" for i in range(100) if sc.weekday_of(f"w{i}") != wd_now)
ok("nie gelaufen: fällig", sc.due(name_other, 0, now))
ok("eigener Tag, vor 7 Tagen: fällig", sc.due(name_today, now - 7 * DAY, now))
ok("eigener Tag, vor 2 Tagen (Knopf): nicht", not sc.due(name_today, now - 2 * DAY, now))
ok("fremder Tag, vor 7 Tagen: nicht", not sc.due(name_other, now - 7 * DAY, now))
ok("fremder Tag, vor 8 Tagen (verschlafen): fällig", sc.due(name_other, now - 8 * DAY, now))

print("\nF3: Automat in der Seitenstatistik")
ua = sc.user_agent("oaap.joomp.de")
ok("Kennung ehrlich, mit Knotennamen", ua == "OAAP-Seiten-Check/1 (+oaap.joomp.de)", ua)
ok("site_stats: Automat „Seiten-Check (OAAP)“, Art other",
   ss.bot_of(ua) == "Seiten-Check (OAAP)" and ss.BOT_KIND["Seiten-Check (OAAP)"] == "other")
FF = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
ok("ein Browser bleibt ein Besuch", ss.bot_of(FF) == "")

print("\nRecht (§3.4, Stufe 2)")
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(
    b"</head>", b'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto">'
                b'<script src="https://www.googletagmanager.com/gtag/js"></script>'
                b'<link rel="icon" href="https://cdn.verein.example.org/i.png"></head>'))
f, o, p = run(t)
ok("fremde Server: gelb, Seite, Google Fonts vorn, Abmahnsatz",
   f.get("law.foreign", {}).get("level") == "yellow" and f["law.foreign"]["fix"] == "seite"
   and f["law.foreign"]["found"].index("fonts.googleapis.com")
   < f["law.foreign"]["found"].index("googletagmanager") and "2022" in f["law.foreign"]["why"],
   f.get("law.foreign"))
ok("eigene Unterdomain zählt nicht als fremd", "cdn.verein" not in f["law.foreign"]["found"])
ok("fremde Server werden nie gelesen",
   not any(h in u for _m, u in p.asked for h in ("googleapis", "googletagmanager", "cdn.verein")),
   p.asked)
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(b"</head>",
                         b"<style>@import url(https://fonts.googleapis.com/css?family=X);</style></head>"))
f, _o, _p = run(t)
ok("Google Fonts per @import im Style erkannt", "fonts.googleapis.com" in f.get("law.foreign", {})
   .get("found", ""), f.get("law.foreign"))
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(b'<a href="/impressum">Impressum</a>', b"")
                         .replace(b'<a href="/datenschutz">Datenschutz</a>', b""))
f, _o, _p = run(t)
ok("ohne Impressum und Datenschutz: zwei gelbe, Seite",
   f.get("law.imprint", {}).get("level") == "yellow" and f.get("law.privacy", {}).get("fix") == "seite")
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(
    b'<a href="/impressum">Impressum</a>', b'<a href="/kontakt/legal-notice"><span>Kontakt</span></a>')
    .replace(b'<a href="/datenschutz">Datenschutz</a>', b'<a href="/p">Privacy <b>Policy</b></a>'))
f, _o, _p = run(t)
ok("Impressum am Pfad, Datenschutz am verschachtelten Text erkannt",
   "law.imprint" not in f and "law.privacy" not in f, sorted(f))
f, o, _p = run(cookies=["PHPSESSID", "_ga", "_fbp", "XSRF-TOKEN"])
ok("Cookies: nur nicht-technische als Punkt", "_ga" in f.get("law.cookies", {}).get("found", "")
   and "_fbp" in f["law.cookies"]["found"] and "PHPSESSID" not in f["law.cookies"]["found"]
   and "XSRF" not in f["law.cookies"]["found"], f.get("law.cookies"))
ok("technische Cookies als Hinweis, nie als Punkt",
   any("PHPSESSID" in n for n in sc.result_of(o)["notes"]))
f, _o, _p = run(cookies=["session_id", "csrftoken"])
ok("nur technische Cookies: kein Punkt", "law.cookies" not in f)
r_ok = sc.result_of(sc.collect(HOST, [], probe=FakeProbe(good_table())))
ok("gepflegte Seite: Recht grün, vier Ampeln", r_ok["lights"].get("Recht") == "green"
   and len(r_ok["lights"]) == 4, r_ok["lights"])

print("\nUnterseiten (Stufe 3)")
t = good_table()
t[f"https://{HOST}/sitemap.xml"] = (200, {}, (
    "<urlset>" + "".join(f"<url><loc>https://{HOST}/{x}</loc></url>"
                         for x in ("", "verein", "termine", "versteckt")) + "</urlset>").encode())
t[f"https://{HOST}/verein"] = (200, {"content-type": "text/html"}, SUB_PAGE)
t[f"https://{HOST}/termine"] = (200, {"content-type": "text/html"},
                                 b"<html><head></head><body>ohne Titel</body></html>")
t[f"https://{HOST}/versteckt"] = (200, {"content-type": "text/html", "x-robots-tag": "noindex"},
                                   SUB_PAGE)
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(
    b"<footer>", b'<nav><a href="/alte-seite">Alt</a><a href="https://verein.example.org/verein#x">V</a>'
                 b'<a href="https://anderswo.example.net/">fremd</a></nav><footer>'))
f, o, p = run(t)
ok("der gepflanzte tote Link wird gefunden: gelb, mit Pfad und Herkunft",
   f.get("pages.broken", {}).get("level") == "yellow"
   and "/alte-seite (404, Link der Startseite)" in f["pages.broken"]["found"], f.get("pages.broken"))
ok("Unterseite ohne Titel: gelb", "/termine" in f.get("pages.title", {}).get("found", ""))
ok("Sitemap nennt noindex-Seite: gelb", "/versteckt" in f.get("pages.noindex", {}).get("found", ""))
ok("jede Seite einmal, Sitemap zuerst, fremde Namen nie",
   [u for _m, u in p.asked].count(f"https://{HOST}/verein") == 1
   and not any("anderswo" in u for _m, u in p.asked)
   and o["pages"]["checked"][0]["from"] == "sitemap", o["pages"])
ok("Startseite nicht doppelt gelesen", [u for _m, u in p.asked].count(f"https://{HOST}/") == 1)
t[f"https://{HOST}/alte-seite"] = (502, {}, b"")
f, _o, _p = run(t)
ok("Unterseite 5xx: rot", f.get("pages.broken", {}).get("level") == "red")
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(
    b"<footer>", b"".join(b'<a href="/s%d">s</a>' % i for i in range(25)) + b"<footer>"))
for i in range(25):
    t[f"https://{HOST}/s{i}"] = (200, {"content-type": "text/html"}, SUB_PAGE)
f, o, p = run(t)
ok("höchstens zehn Unterseiten, Rest gezählt", len(o["pages"]["checked"]) == sc.MAX_PAGES
   and o["pages"]["more"] == 27 - sc.MAX_PAGES
   and sum(1 for _m, u in p.asked if re.search(r"/s\d+$", u)) == sc.MAX_PAGES, o["pages"])
ok("Hinweis nennt gelesene und übrige Seiten",
   any("10 weitere Seiten gelesen, 17 nicht" in n for n in sc.result_of(o)["notes"]),
   sc.result_of(o)["notes"])
t = good_table()
t[f"https://{HOST}/"] = (200, GOOD_HDR, GOOD_PAGE.replace(b"<footer>", b'<a href="/weiter">w</a><footer>'))
t[f"https://{HOST}/weiter"] = (301, {"location": "/neu"}, b"")
f, o, p = run(t)
ok("Umleitung einer Unterseite ist kein toter Link, wird nicht verfolgt",
   "pages.broken" not in f and not any(u.endswith("/neu") for _m, u in p.asked))

print("\nNeu und behoben, Verlauf (§6, Stufe 3)")
A = {"findings": [{"id": "x", "level": "yellow"}, {"id": "y", "level": "yellow"}], "grey": ""}
B = {"findings": [{"id": "y", "level": "red"}, {"id": "z", "level": "yellow"}], "grey": ""}
new, gone = sc.diff(A, B)
ok("neu: neuer Punkt und Aufstieg zu Rot; behoben: verschwundener", new == {"y", "z"}
   and [g["id"] for g in gone] == ["x"], (new, gone))
ok("grauer Lauf vergleicht nichts", sc.diff(A, {"grey": "weg", "findings": []}) == (set(), []))
ok("ohne Vorlauf nichts neu", sc.diff(None, B) == (set(), []))
d3 = tempfile.mkdtemp()
t = good_table()
r1 = sc.result_of(sc.collect(HOST, [], probe=FakeProbe(t)), now=time.time() - 9000)
sc.record(d3, r1)
t[f"https://{HOST}/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /\n")
del t[f"https://{HOST}/impressum"]
r2 = sc.result_of(sc.collect(HOST, [], probe=FakeProbe(t)), now=time.time() - 5000)
sc.record(d3, r2)
v3 = sc.view(d3, "web")
ok("Ansicht: verglichen, Zahl neu", v3["compared"] and v3["new"] == 2, (v3.get("new"), v3.get("rows")))
ok("Ansicht: Zeilen tragen „neu“", any(not r["group"] and r["f"]["new"]
                                        and r["f"]["id"] == "seo.robots_block" for r in v3["rows"]))
sc.record(d3, r1)
v3 = sc.view(d3, "web")
ok("Ansicht: behoben nennt die verschwundenen", {f["area"] for f in v3["fixed"]} == {"Auffindbarkeit"}
   and len(v3["fixed"]) == 2, v3["fixed"])
h3 = v3["history"]
ok("Verlauf: ein Balken je Lauf, Rot unten", len(h3) == 3 and h3[1]["red"] == 1
   and h3[1]["yr"] + h3[1]["hr"] == 40 and h3[1]["yy"] + h3[1]["hy"] == h3[1]["yr"], h3)
old = dict(r1, lights={a: "green" for a in ("Sicherheit", "Auffindbarkeit", "Tempo")})
d4 = tempfile.mkdtemp()
sc.record(d4, old)
ok("Lauf aus Stufe 1: Recht leer, nicht grün", sc.view(d4, "x")["lights"]["Recht"] == "")

print("\nMeldung site.check (Stufe 2)")
G = {"findings": [{"id": "a", "level": "yellow", "area": "Tempo", "found": "x"}], "grey": "",
     "at": "2026-11-28 10:00", "host": HOST}
R = {"findings": [{"id": "seo.noindex", "level": "red", "area": "Auffindbarkeit",
                   "found": "Die Startseite sagt `noindex`."}], "grey": "",
     "at": "2026-11-28 11:00", "host": HOST}
GREY = {"findings": [], "grey": "nicht erreichbar"}
ok("neues Rot öffnet", sc.matter([G, R]) is not None and sc.matter([G, R])[0] == "1")
ok("Text nennt Punkt ohne Backticks und den Reiter", "noindex" in sc.matter([G, R])[1]
   and "`" not in sc.matter([G, R])[1] and "Seitenstatistik" in sc.matter([G, R])[1])
ok("erster Lauf schon rot: öffnet", sc.matter([R]) is not None)
ok("Rot wie im Vorlauf, nicht offen: öffnet nicht", sc.matter([R, R]) is None)
ok("Rot bleibt, offen: hält", sc.matter([R, R], is_open=True) is not None)
ok("Lauf ohne Rot: schließt", sc.matter([R, G], is_open=True) is None)
ok("grauer Lauf hält und öffnet nichts", sc.matter([G, R, GREY], is_open=True) is not None
   and sc.matter([R, GREY, R]) is None and sc.matter([G, R, GREY]) is not None)
ok("gelb meldet nie", sc.matter([GREY, G]) is None and sc.matter([]) is None)

print("\nSchalter, CLI, Spool")
import appctl as m                                            # noqa: E402
SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
m.reload_gateway = lambda: None
m.refresh_destinations = lambda: None
spawned = []
m.site_check_spawn = lambda name: spawned.append(name) or True
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)


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
reg["instances"]["web"] = record("web", tenant=tid, address=HOST, aliases=["www." + HOST])
reg["instances"]["probe"] = record("probe", tenant=tid, channel="test")
m.save_registry(reg)
msg = m.site_check_set("web", m.load_registry()["instances"]["web"], True, who="joerg")
rec = m.load_registry()["instances"]["web"]
ok("an: Satz trägt an, wer, seit; erster Lauf gestartet",
   rec.get("site_check", {}).get("on") is True and rec["site_check"]["by"] == "joerg"
   and spawned == ["web"] and "first run" in msg, (rec.get("site_check"), spawned, msg))
ok("Wiederholung ändert nichts", "already" in m.site_check_set("web", rec, True, who="joerg"))
try:
    m.site_check_set("probe", m.load_registry()["instances"]["probe"], True, who="joerg")
    ok("Test-Instanz: abgewiesen", False)
except m.DiagnoseRefused as e:
    ok("Test-Instanz: abgewiesen", "production" in str(e))
ok("Ziel: Hauptname und Aliasse", m.site_check_target("web", rec) == (HOST, ["www." + HOST]))
ok("Update übernimmt den Schalter",
   'reg["instances"][name]["site_check"] = dict(inst["site_check"])' in SRC)
ok("Spool-Tür und CLI", 'action == "site-check"' in SRC and 'sub.add_parser("site-check"' in SRC
   and '"site-check", "portal"' not in SRC and '"site-check": "portal"' in SRC)
ok("Namensauflösung kennt das Verb", "site-check" in m.INSTANCE_VERBS)
ok("losgelöster Lauf ruft appctl ohne „app“ (gemessen auf oaap-test: invalid choice)",
   '"site-check", name, "run"' in SRC and '"app", "site-check"' not in SRC)
ok("Timer in install.sh", "appctl.py site-check --due" in open(
   os.path.join(HERE, "..", "install.sh"), encoding="utf-8").read())
# the button: at most once an hour
sc.record(m.site_check_dir("web", rec), dict(res, ts=int(time.time()) - 600))
try:
    m.site_check_button("web", rec)
    ok("Knopf vor Ablauf der Stunde: abgewiesen", False)
except m.DiagnoseRefused as e:
    ok("Knopf vor Ablauf der Stunde: abgewiesen", "again in" in str(e), str(e))
sc.record(m.site_check_dir("web", rec), dict(res, ts=int(time.time()) - 4000))
ok("Knopf nach einer Stunde: startet", "started" in m.site_check_button("web", rec)
   and spawned[-1] == "web")
# the daily job: run only whose weekday it is (collect stubbed: no network)
ran = []
m.site_check_run = lambda n, i, by="cli": ran.append((n, by)) or {"lights": {}}
sc.record(m.site_check_dir("web", rec), dict(res, ts=int(time.time()) - 9 * DAY))
m.site_check_due()
ok("Tageslauf: verschlafene Woche läuft, Test-Instanz nie", ran == [("web", "wochenlauf")], ran)
m.site_check_set("web", m.load_registry()["instances"]["web"], False, who="joerg")
ok("aus: Satz ohne Schalter, Ergebnisse bleiben",
   "site_check" not in m.load_registry()["instances"]["web"]
   and sc.load(m.site_check_dir("web", rec))["runs"])

print("\nMeldung über die Hinweise, Wochenbericht (Stufe 2)")
import notify                                                 # noqa: E402
import report                                                 # noqa: E402
ok("Katalog: site.check, Betrieb, Mandant, Lage", notify.CATALOGUE.get("site.check", {}).get("matter") is True
   and notify.CATALOGUE["site.check"]["scope"] == "tenant"
   and notify.CATALOGUE["site.check"]["category"] == "operations", notify.CATALOGUE.get("site.check"))
ok("Mail führt in den Reiter Seitenstatistik",
   m.NOTIFY_PAGES.get("site.check") == "/instances/{instanz}?tab=statistik")
scd = m.site_check_dir("web", rec)
os.remove(os.path.join(scd, sc.FILE))
sc.record(scd, dict(G, ts=int(time.time()) - 7200))
sc.record(scd, dict(R, ts=int(time.time()) - 3600))
now_dt = notify.now_utc()
ok("Check aus: keine Lage", "site.check/web" not in (
   m.site_watch_conditions(now_dt).get(tid) or {}))
reg = m.load_registry()
reg["instances"]["web"]["site_check"] = {"on": True, "since": "2026-11-28T10:00:00Z", "by": "joerg"}
m.save_registry(reg)
ok("Check an, Hinweise aus: keine Lage", "site.check/web" not in (
   m.site_watch_conditions(now_dt).get(tid) or {}))
m.site_watch_set("web", m.load_registry()["instances"]["web"], True, who="joerg")
cond = (m.site_watch_conditions(now_dt).get(tid) or {}).get("site.check/web")
ok("beide an: Lage site.check mit Zahl", cond and cond["type"] == "site.check" and cond["kurz"] == "1",
   cond)
q1 = m.notify_matters({}, now_dt)
open1 = (m._notify_state_rw().get("matters") or {}).get(tid) or {}
ok("Lage geöffnet, eine Mail", "site.check/web" in open1 and q1 >= 1, (q1, list(open1)))
sc.record(scd, dict(R, ts=int(time.time()) - 60))
m.notify_matters({}, now_dt)
ok("Rot bleibt: Lage bleibt offen", "site.check/web" in (
   (m._notify_state_rw().get("matters") or {}).get(tid) or {}))
sc.record(scd, dict(G, ts=int(time.time())))
q2 = m.notify_matters({}, now_dt)
open2 = (m._notify_state_rw().get("matters") or {}).get(tid) or {}
ok("Lauf ohne Rot: Lage geschlossen, mit Mail", "site.check/web" not in open2 and q2 >= 1, (q2, list(open2)))
ok("Check aus: Schließen ohne „Behoben“", 'rec.get("type") == "site.check"' in SRC
   and "not site_check_of(reg.get(rec.get(\"instanz\")))" in SRC)
sc.record(scd, dict(R, ts=int(time.time())))
rep_sc = m._report_sitecheck(m.load_registry())
ok("Wochenbericht: letzter Lauf je geprüfter Instanz", rep_sc.get("web", {}).get("red") == 1
   and "probe" not in rep_sc, rep_sc)
md = report.build([{"day": "2026-11-27", "sitecheck": rep_sc}], ["2026-11-27"], "Test",
                  scope_is_node=False)
ok("Wochenbericht: Abschnitt mit vier Ampeln und Zusammenfassung", "## Seiten-Check" in md
   and "| web |" in md and "Recht" in md and "Seiten-Check: rote Punkte bei web" in md, md)
md0 = report.build([{"day": "2026-11-27"}], ["2026-11-27"], "Test", scope_is_node=False)
ok("ohne geprüfte Instanz: kein Abschnitt", "Seiten-Check" not in md0)
ok("Tageszeile trägt sitecheck nur für den gemessenen Tag",
   'parts["sitecheck"]' in SRC and "sitecheck_all = _report_sitecheck(reg) if measured_now else {}" in SRC)

print("\nPortal")
sys.path.insert(0, os.path.join(PLATFORM, "services", "portal"))
try:
    import app as portal  # noqa: E402
    from jinja2 import Environment
    src = portal.INSTANCE_EDIT_BODY
    a = src.index("<section class=\"panel {{ 'active' if tab == 'statistik' }}\">")
    bb = src.index("</section>", a) + len("</section>")
    tpl = Environment(autoescape=True).from_string(src[a:bb])
    kw = dict(tab="statistik", device_titles=ss.DEVICE_TITLES, kind_titles=ss.KIND_TITLES,
              site_keep_days=ss.DAYS)
    t = good_table()
    t[f"https://{HOST}/"] = (200, dict(GOOD_HDR, **{"x-robots-tag": "noindex"}),
                             b"<html><head><title>App</title></head></html>")
    del t[f"https://{HOST}/robots.txt"]
    pd = tempfile.mkdtemp()
    sc.record(pd, sc.result_of(sc.collect(HOST, [], probe=FakeProbe(t)), {"instance": "web"},
                               now=time.time() - 7200))
    mode = portal._mode_view({"channel": "production", "site_check": {
        "on": True, "since": "2026-11-28T10:00:00Z", "by": "joerg"}})
    html = tpl.render(i={"key": "web", "site": None, "site_days": 7, "mode": mode,
                         "check": sc.view(pd, "web")}, **kw)
    ok("Karte: an, wer, Wochentag, Ampeln", "Seiten-Check" in html and "joerg" in html
       and "Geprüft wird jeden" in html and "Auffindbarkeit: <strong>rot</strong>" in html
       and "Recht: <strong>" in html, html[-3000:])
    ok("rot einzeln, gelb gefaltet", "noindex" in html and "<details>" in html
       and "Für Suchmaschinen kaum vorbereitet" in html)
    ok("Knöpfe: Jetzt prüfen, Ausschalten", 'value="run"' in html
       and "Seiten-Check ausschalten" in html and 'action="/instances/web/site-check"' in html)
    ok("Recht: keine Rechtsberatung", "keine Rechtsberatung" in html and "folgt" not in html)
    ok("Mail-Satz von Stufe 2", "nur bei einem neuen roten Punkt" in html)
    sc.record(pd, sc.result_of(sc.collect(HOST, [], probe=FakeProbe(good_table())),
                               {"instance": "web"}, now=time.time() - 3700))
    t[f"https://{HOST}/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /\n")
    sc.record(pd, sc.result_of(sc.collect(HOST, [], probe=FakeProbe(t)), {"instance": "web"},
                               now=time.time() - 3650))
    html2 = tpl.render(i={"key": "web", "site": None, "site_days": 7, "mode": mode,
                          "check": sc.view(pd, "web")}, **kw)
    ok("Karte: neu markiert, Verlauf als SVG", 'class="sc-new"' in html2 and "<svg" in html2
       and "Verlauf der letzten 3 Läufe" in html2, html2[-4000:])
    sc.record(pd, sc.result_of(sc.collect(HOST, [], probe=FakeProbe(good_table())),
                               {"instance": "web"}, now=time.time() - 3600))
    html3 = tpl.render(i={"key": "web", "site": None, "site_days": 7, "mode": mode,
                          "check": sc.view(pd, "web")}, **kw)
    ok("Karte: behoben", "Seit dem letzten Lauf behoben" in html3 and "alles grün" in html3)
    dold = tempfile.mkdtemp()
    sc.record(dold, dict(sc.result_of(sc.collect(HOST, [], probe=FakeProbe(good_table()))),
                         lights={"Sicherheit": "green", "Auffindbarkeit": "green", "Tempo": "green"}))
    html4 = tpl.render(i={"key": "web", "site": None, "site_days": 7, "mode": mode,
                          "check": sc.view(dold, "web")}, **kw)
    ok("Lauf aus Stufe 1: Recht beim nächsten Lauf", "beim nächsten Lauf" in html4)
    ok("Code als <code>, keine rohen Backticks", "`" not in html[html.index("seiten-check"):])
    html0 = tpl.render(i={"key": "web", "site": None, "site_days": 30,
                          "mode": portal._mode_view({"channel": "production"}),
                          "check": sc.view("", "web")}, **kw)
    ok("aus: Einschalten, kein Prüfknopf", "Seiten-Check einschalten" in html0
       and 'value="run"' not in html0)
    html1 = tpl.render(i={"key": "web", "site": None, "site_days": 30,
                          "mode": portal._mode_view({"channel": "test"})}, **kw)
    ok("Test-Instanz ohne Daten: kein Einschalten, Satz", "Seiten-Check einschalten" not in html1
       and "geprüft werden nur Produktivinstanzen" in html1)
    ok("Tür im Portal", any(r.rule == "/instances/<name>/site-check"
                            for r in portal.app.url_map.iter_rules()))
except ImportError as e:
    print("SKIP  Portal (Flask fehlt):", e)

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
