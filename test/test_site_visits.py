#!/usr/bin/env python3
"""Besuche und geprüfte Suchmaschinen in der Seitenstatistik (0.1.399,
I-81 ohne A/B -- Jörg 27.11.: „Tagessalz, 30 Min“).

    - Aufrufe eines Browsers mit höchstens 30 Min Pause = ein Besuch
    - Kennung = Hash aus Tagessalz, Instanz, Adresse, User-Agent; das Salz ist
      je UTC-Tag neu, ein neuer Tag beendet alle offenen Besuche
    - der Zustand liegt nur im run-Verzeichnis; site.json bekommt nur Zahlen
      (vis, multi, dur, vpg) -- keine Adresse, kein Hash, kein User-Agent
    - ein Besuch zählt am Tag seines Beginns; Dauer = erste bis letzte Seite
    - ohne nutzbares run-Verzeichnis: kein Besuch, sonst alles wie bisher
    - Suchmaschinen: Rückwärts- und Vorwärts-DNS; durchgefallen ->
      „angebliche Suchmaschine“, nie bot_last des echten Namens
    - Urteil je Adresse für den Tag gemerkt (gehasht), höchstens DNS_BUDGET
      neue Abfragen je Minute, unbekannt -> zählt wie angegeben
    - check_dns mit nachgestelltem DNS: Name, Rückweg, kein Name, Zeitüberschreitung
    - read() und Portal zeigen Besuche und „nicht echt“
    - OAAP_INSTANCE_NAMES nennt auch hinter einer Kante https

Aufruf: python3 test/test_site_visits.py
"""
import json
import os
import socket
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
SAF = "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15 Safari/605.1.15"
GOOGLE = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def line(uri="/", ua=FF, ip="203.0.113.7", ts=NOW, status=200, host="www.example.org", edge=None):
    req = {"remote_ip": edge or ip, "proto": "HTTP/1.1", "method": "GET", "host": host,
           "uri": uri, "headers": {"User-Agent": [ua]}}
    if edge:
        req["client_ip"] = ip
    return json.dumps({"ts": ts, "request": req, "status": status,
                       "resp_headers": {"Content-Type": ["text/html; charset=utf-8"]}})


def idir(b, tenant, key):
    return os.path.join(b, "tenants", tenant, key)


TARGETS = {"web": {"tenant": "T1", "hosts": ["www.example.org"]}}
base = tempfile.mkdtemp(prefix="oaap-visits-")
RUN = os.path.join(base, "run")
asked = []


def fake_dns(ip, suffixes):
    asked.append(ip)
    return {"66.249.66.1": True, "198.51.100.9": False}.get(ip)


def run(t, lines, run_dir=RUN):
    ss.sample(base, t, lines, TARGETS, idir, run_dir=run_dir, resolve=fake_dns)
    try:
        return json.load(open(os.path.join(idir(base, "T1", "web"), ss.FILE)))
    except OSError:
        return {}


# --- Besuche --------------------------------------------------------------
# A: drei Seiten in 10 Minuten; B: eine Seite; A (andere IP) = anderer Besuch
run(NOW, [line("/", ts=NOW), line("/a", ts=NOW + 120), line("/b", ts=NOW + 600),
          line("/", ua=SAF, ip="203.0.113.8", ts=NOW + 30)])
state = json.load(open(os.path.join(RUN, ss.RUN_FILE)))
ok("Zustand im run-Verzeichnis: Salz, Tag, zwei offene Besuche",
   state["day"] == DAY and len(state["salt"]) == 32 and len(state["visits"]["web"]) == 2, state)
d = run(NOW + 900, [])
ok("vor 30 Min Pause: noch nichts gezählt", not d["days"][DAY].get("vis"), d["days"][DAY])
d = run(NOW + 600 + ss.VISIT_IDLE + 61, [])
day = d["days"][DAY]
ok("nach 30 Min Pause: zwei Besuche, einer mit mehr als einer Seite",
   day.get("vis") == 2 and day.get("multi") == 1, day)
ok("Dauer = erste bis letzte Seite (600 s), Seiten 3 + 1",
   day.get("dur") == 600 and day.get("vpg") == 4, day)
blob = open(os.path.join(idir(base, "T1", "web"), ss.FILE), encoding="utf-8").read()
ok("site.json ohne Adresse, Hash oder User-Agent",
   "203.0.113" not in blob and "Firefox" not in blob and state["salt"] not in blob
   and not any(sid in blob for sid in state["visits"]["web"]), blob[:300])
ok("beendete Besuche verschwinden aus dem Zustand",
   not json.load(open(os.path.join(RUN, ss.RUN_FILE)))["visits"], "")

# derselbe Browser kommt nach 40 Min wieder: zwei Besuche
t = NOW + 3 * 3600
run(t, [line("/x", ts=t)])
run(t + 60, [line("/y", ts=t + 2400)])        # 40 Min Pause, gleiche Zeile -> neuer Besuch
st = json.load(open(os.path.join(RUN, ss.RUN_FILE)))
ok("Rückkehr nach 40 Min: alter Besuch beendet, neuer offen",
   len(st["visits"]["web"]) == 1 and list(st["visits"]["web"].values())[0][2] == 1, st["visits"])
d = json.load(open(os.path.join(idir(base, "T1", "web"), ss.FILE)))
ok("... und gezählt (jetzt 3 Besuche)", d["days"][DAY]["vis"] == 3, d["days"][DAY])

# neuer UTC-Tag: neues Salz, offener Besuch endet
salt1 = st["salt"]
nxt = NOW + 86400
run(nxt, [])
st2 = json.load(open(os.path.join(RUN, ss.RUN_FILE)))
d = json.load(open(os.path.join(idir(base, "T1", "web"), ss.FILE)))
ok("neuer Tag: neues Salz, keine offenen Besuche, DNS-Urteile vergessen",
   st2["salt"] != salt1 and not st2["visits"] and not st2["dns"], st2)
ok("der offene Besuch vom Vortag zählt am Vortag", d["days"][DAY]["vis"] == 4, d["days"][DAY])
ok("Beginn der Besuchszählung vermerkt (vis_from)", d.get("vis_from") == DAY, d.get("vis_from"))

# Kante: client_ip zählt, nicht die Adresse der Kante
b2 = tempfile.mkdtemp(prefix="oaap-visits-edge-")
r2 = os.path.join(b2, "run")
ss.sample(b2, NOW, [line("/", ip="203.0.113.20", edge="10.10.10.75", ts=NOW),
                    line("/", ip="203.0.113.21", edge="10.10.10.75", ts=NOW + 5)],
          TARGETS, idir, run_dir=r2, resolve=fake_dns)
ok("hinter der Kante: zwei Adressen = zwei Besuche",
   len(json.load(open(os.path.join(r2, ss.RUN_FILE)))["visits"]["web"]) == 2, "")

# run-Verzeichnis unbrauchbar: keine Besuche, sonst unverändert
b3 = tempfile.mkdtemp(prefix="oaap-visits-norun-")
blocker = os.path.join(b3, "datei")
open(blocker, "w").write("x")
ss.sample(b3, NOW, [line("/", ts=NOW)], TARGETS, idir, run_dir=os.path.join(blocker, "run"),
          resolve=fake_dns)
d3 = json.load(open(os.path.join(idir(b3, "T1", "web"), ss.FILE)))
ok("ohne run-Verzeichnis: Aufruf gezählt, kein Besuch, kein vis_from",
   d3["days"][DAY]["views"] == 1 and "vis_from" not in d3, d3)

# --- Suchmaschinen prüfen -------------------------------------------------
b4 = tempfile.mkdtemp(prefix="oaap-visits-dns-")
r4 = os.path.join(b4, "run")
asked.clear()
L4 = [line("/", ua=GOOGLE, ip="66.249.66.1", ts=NOW),
      line("/a", ua=GOOGLE, ip="66.249.66.1", ts=NOW + 1),
      line("/", ua=GOOGLE, ip="198.51.100.9", ts=NOW + 2),
      line("/", ua=GOOGLE, ip="192.0.2.50", ts=NOW + 3)]       # DNS weiß nichts
ss.sample(b4, NOW, L4, TARGETS, idir, run_dir=r4, resolve=fake_dns)
d4 = json.load(open(os.path.join(idir(b4, "T1", "web"), ss.FILE)))
day4 = d4["days"][DAY]
ok("echter Googlebot zählt als Googlebot (2 + 1 ungeprüft)", day4["bots"].get("Googlebot") == 3, day4["bots"])
ok("falscher Googlebot zählt als angebliche Suchmaschine",
   day4["bots"].get(ss.FAKE_NAME) == 1 and day4["fake"] == {"Googlebot": 1}, day4)
ok("geprüft: 2 Anfragen bestätigt", day4.get("checked") == {"Googlebot": 2}, day4.get("checked"))
ok("jede Adresse einmal gefragt", sorted(asked) == ["192.0.2.50", "198.51.100.9", "66.249.66.1"], asked)
st4 = json.load(open(os.path.join(r4, ss.RUN_FILE)))
ok("Urteile gemerkt, nur gehasht (2, das unbekannte nicht)",
   len(st4["dns"]) == 2 and "66.249" not in json.dumps(st4), st4["dns"])
asked.clear()
ss.sample(b4, NOW + 60, [line("/b", ua=GOOGLE, ip="66.249.66.1", ts=NOW + 60)], TARGETS, idir,
          run_dir=r4, resolve=fake_dns)
ok("bekannte Adresse wird nicht noch einmal gefragt", asked == [], asked)

# ein falscher Googlebot hält site.google nicht ruhig
b5 = tempfile.mkdtemp(prefix="oaap-visits-g-")
ss.sample(b5, NOW, [line("/", ua=GOOGLE, ip="198.51.100.9", ts=NOW)], TARGETS, idir,
          run_dir=os.path.join(b5, "run"), resolve=fake_dns)
d5 = json.load(open(os.path.join(idir(b5, "T1", "web"), ss.FILE)))
ok("falscher Googlebot setzt kein bot_last für Googlebot",
   "Googlebot" not in d5["bot_last"] and ss.FAKE_NAME in d5["bot_last"], d5["bot_last"])

# Budget
b6 = tempfile.mkdtemp(prefix="oaap-visits-b-")
asked.clear()
many = [line("/", ua=GOOGLE, ip=f"192.0.2.{i}", ts=NOW + i) for i in range(1, 40)]
ss.sample(b6, NOW, many, TARGETS, idir, run_dir=os.path.join(b6, "run"), resolve=fake_dns)
ok(f"höchstens {ss.DNS_BUDGET} neue Abfragen je Minute", len(asked) == ss.DNS_BUDGET, len(asked))

# Zeitüberschreitung: hängende Abfrage hält die Minute nicht auf
slow = lambda ip, suf: time.sleep(3) or True        # noqa: E731
t0 = time.monotonic()
got = ss.resolve_all([("Googlebot", "192.0.2.1")], slow, wait=0.3)
ok("hängende Abfrage: None nach der Wartezeit", got == {("Googlebot", "192.0.2.1"): None}
   and time.monotonic() - t0 < 1.5, got)

# check_dns mit nachgestelltem socket
real = (socket.gethostbyaddr, socket.getaddrinfo)


def fake_socket(ptr, fwd):
    def gha(ip):
        if isinstance(ptr, Exception):
            raise ptr
        return (ptr, [], [ip])

    def gai(host, port):
        if isinstance(fwd, Exception):
            raise fwd
        return [(socket.AF_INET, 1, 6, "", (a, 0)) for a in fwd]
    socket.gethostbyaddr, socket.getaddrinfo = gha, gai


try:
    fake_socket("crawl-66-249-66-1.googlebot.com.", ["66.249.66.1"])
    ok("check_dns: Name passt, Rückweg passt -> echt",
       ss.check_dns("66.249.66.1", ss.VERIFY["Googlebot"]) is True)
    fake_socket("crawl.googlebot.com.evil.example", ["66.249.66.1"])
    ok("check_dns: Domain nur vorgetäuscht -> falsch",
       ss.check_dns("66.249.66.1", ss.VERIFY["Googlebot"]) is False)
    fake_socket("x.googleusercontent.com", ["66.249.66.1"])
    ok("check_dns: googleusercontent.com (fremde Cloud-Maschine) -> falsch",
       ss.check_dns("66.249.66.1", ss.VERIFY["Googlebot"]) is False)
    fake_socket("crawl.googlebot.com", ["66.249.66.2"])
    ok("check_dns: Rückweg zeigt woanders hin -> falsch",
       ss.check_dns("66.249.66.1", ss.VERIFY["Googlebot"]) is False)
    fake_socket(socket.herror(1, "Unknown host"), [])
    ok("check_dns: kein Name -> falsch", ss.check_dns("198.51.100.9", ss.VERIFY["Googlebot"]) is False)
    fake_socket(socket.herror(2, "Try again"), [])
    ok("check_dns: Server weiß es gerade nicht -> unbekannt",
       ss.check_dns("198.51.100.9", ss.VERIFY["Googlebot"]) is None)
finally:
    socket.gethostbyaddr, socket.getaddrinfo = real

# --- read() und Portal ----------------------------------------------------
r = ss.read(idir(base, "T1", "web"), 7, now=NOW + 3600)
ok("read: Besuche summiert", r["visits"]["n"] == 4 and r["visits"]["multi"] >= 1
   and r["visits"]["from"] == DAY, r["visits"])
r4r = ss.read(idir(b4, "T1", "web"), 7, now=NOW + 3600)
ok("read: nicht echt / geprüft je Name", r4r["fake"] == {"Googlebot": 1}
   and r4r["checked"].get("Googlebot") == 3, (r4r["fake"], r4r["checked"]))

os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-visits-portal-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
try:
    import app as portal  # noqa: E402
    from jinja2 import Environment
    src = portal.INSTANCE_EDIT_BODY
    a = src.index("<section class=\"panel {{ 'active' if tab == 'statistik' }}\">")
    b = src.index("</section>", a) + len("</section>")
    tpl = Environment(autoescape=True).from_string(src[a:b])
    html = tpl.render(i={"key": "web", "site": r, "site_days": 7, "mode": portal._mode_view({})},
                      tab="statistik", device_titles=ss.DEVICE_TITLES, kind_titles=ss.KIND_TITLES,
                      site_keep_days=ss.DAYS)
    ok("Portal: Kacheln Besuche, mehr als eine Seite, Dauer, Seiten je Besuch",
       "Besuche" in html and "mehr als einer Seite" in html and "10 Min 0 s" in html and "Seiten je Besuch" in html
       and "Min" in html, html[html.find("st-tiles"):html.find("st-tiles") + 1400])
    html4 = tpl.render(i={"key": "web", "site": r4r, "site_days": 7, "mode": portal._mode_view({})},
                       tab="statistik", device_titles=ss.DEVICE_TITLES, kind_titles=ss.KIND_TITLES,
                       site_keep_days=ss.DAYS)
    ok("Portal: Spalte „nicht echt“ und die angebliche Suchmaschine",
       "nicht echt" in html4 and ss.FAKE_NAME in html4 and "Rückwärts-DNS" in html4, "")
except ImportError as e:
    print("SKIP  Portal (Flask fehlt):", e)

print()
print("ALLE PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
