#!/usr/bin/env python3
"""Schalter „KI-Abrufer“ je Instanz (gateway 0.2.27, Jörg 27.11.: „Ok 2“ --
robots.txt-Schalter für KI-Bots).

    zulassen   Standard: die Plattform greift nicht ein, eine robots.txt der
               App geht unverändert hinaus
    training   Crawler, die für Modelle sammeln (GPTBot, ClaudeBot, CCBot, …)
    all        dazu KI-Suchen und Abrufe für eine Frage im Chat
    zwei Hebel robots.txt von der Plattform (eine Gruppe für die gesperrten
               Kennungen, alle anderen erlaubt) UND 403 für deren User-Agent --
               eine robots.txt ist nur eine Bitte
    nie        /robots.txt selbst abweisen (4xx heißt dort „alles erlaubt“),
               nie eine Suchmaschine
    eine Liste site_stats: dieselbe Tabelle für 403, robots.txt, Portal und
               Statistik
    bleibt     ein Update der App übernimmt den Schalter
    Statistik  403 an einen Abrufer wird als „abgewiesen“ gezählt

Aufruf: python3 test/test_ai_fetchers.py
"""
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-ai-fetchers-")
os.environ["OAAP_DATA_DIR"] = DATA
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, PLATFORM)

import appctl as m                                            # noqa: E402
import site_stats as ss                                       # noqa: E402

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


UA = {
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot",
    "ChatGPT-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
    "Googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Browser": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
}

print("Eine Tabelle")
ai_rows = [b[0] for b in ss.BOTS if b[1] == "ai"]
ok("jede KI-Zeile der Statistik hat Kennung und Zweck", all(n in ss.AI_ROBOTS for n in ai_rows),
   [n for n in ai_rows if n not in ss.AI_ROBOTS])
ok("keine Suchmaschine in der Sperrtabelle",
   not any(ss.BOT_KIND.get(n) == "search" for n in ss.AI_ROBOTS))
t_tok, t_needle, t_names = ss.ai_blocked("training")
a_tok, a_needle, a_names = ss.ai_blocked("all")
ok("training ist Teil von all", set(t_tok) < set(a_tok) and set(t_names) < set(a_names))
ok("zulassen sperrt nichts", ss.ai_blocked("") == ([], [], []) and ss.robots_txt("") == ""
   and ss.ai_blocked("quatsch") == ([], [], []))
ok("training: GPTBot, ClaudeBot, CCBot, Google-Extended, Applebot-Extended",
   all(x in t_tok for x in ("GPTBot", "ClaudeBot", "CCBot", "Google-Extended", "Applebot-Extended")), t_tok)
ok("training: ChatGPT-User, Claude-User, OAI-SearchBot bleiben drin",
   not any(x in t_tok for x in ("ChatGPT-User", "Claude-User", "OAI-SearchBot", "PerplexityBot")), t_tok)
ok("all: auch ChatGPT-User, Claude-User, Perplexity-User",
   all(x in a_tok for x in ("ChatGPT-User", "Claude-User", "Perplexity-User", "OAI-SearchBot")), a_tok)
ok("Applebot-Extended nur in robots.txt, kein User-Agent-Muster",
   "applebot" not in " ".join(a_needle))
rb = ss.robots_txt("training")
ok("robots.txt: eine Gruppe Disallow, danach alle erlaubt",
   "User-agent: GPTBot\n" in rb and "Disallow: /\n\nUser-agent: *\nAllow: /" in rb
   and "ChatGPT-User" not in rb, rb)


def rx_of(lines):
    line = next(ln for ln in lines if "header_regexp" in ln)
    return re.search(r'"\(\?i\)\((.*)\)"', line).group(1)


print("\nWas das Gateway schreibt")
ROUTES = [{"path": "/", "roles": ["public"]}]


def record(name, port, **over):
    base = {
        "app_id": "website", "app_name": "Webseite", "version": "0.1.0",
        "channel": "production", "port": port, "container": f"oaap-app-{name}",
        "image": "oaap-app/website:0.1.0", "svc_port": 8080,
        "services": [{"service": "", "container": f"oaap-app-{name}",
                      "image": "oaap-app/website:0.1.0", "build": "", "port": 8080}],
        "routes": [dict(r) for r in ROUTES],
        "storage": [], "config": [], "roles": [], "visibility": {},
        "id": (name * 12)[:12], "name": name,
    }
    base.update(over)
    return base


tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["web"] = record("web", 8301, tenant=tid)
reg["instances"]["blog"] = record("blog", 8302, tenant=tid)
m.save_registry(reg)


def site(name):
    m.write_app_caddy(name, m.load_registry()["instances"][name])
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


ok("Standard: weder robots.txt noch Sperre", "robots.txt" not in site("web")
   and "oaap_ai" not in site("web"))
msg = m.ai_fetchers_set("web", m.load_registry()["instances"]["web"], "training",
                        "keine Trainingsdaten", who="joerg", role="server_admin")
rec = m.load_registry()["instances"]["web"]
ok("Satz trägt Stufe, wer, seit, Grund",
   rec.get("ai_fetchers", {}).get("block") == "training" and rec["ai_fetchers"]["by"] == "joerg"
   and rec["ai_fetchers"]["since"] and rec["ai_fetchers"]["reason"] == "keine Trainingsdaten", rec.get("ai_fetchers"))
ok("Antwort nennt die gesperrten", "GPTBot" in msg and "ClaudeBot" in msg, msg)
w = site("web")
lines = w.splitlines()
ok("robots.txt von der Plattform", "\thandle /robots.txt {" in lines and "User-agent: GPTBot" in w
   and "Allow: /" in w, w)
ok("403 für die Kennungen", f"\trewrite @oaap_ai {m.AI_RESERVED}" in lines and "` 403" in w, w)
ok("/robots.txt selbst nie abgewiesen", "\t\tnot path /robots.txt" in lines, w)
rx = re.compile("(?i)(" + rx_of(lines) + ")")
ok("Muster trifft GPTBot und ClaudeBot", rx.search(UA["GPTBot"]) and rx.search(UA["ClaudeBot"]))
ok("Muster lässt Browser, Googlebot und ChatGPT-User durch (training)",
   not rx.search(UA["Browser"]) and not rx.search(UA["Googlebot"]) and not rx.search(UA["ChatGPT-User"]))
ok("andere Instanz unberührt", "oaap_ai" not in site("blog"))
i_rw = next(n for n, ln in enumerate(lines) if ln.startswith("\trewrite @oaap_ai"))
i_auth = next(n for n, ln in enumerate(lines) if ln == "\thandle /auth/* {")
ok("vor /auth/* und jedem anderen handle-Block", i_rw < i_auth)
guard = [n for n, ln in enumerate(lines) if "@oaap_guard" in ln]
ok("vor der Wache (rewrite: der erste Treffer gilt)", not guard or i_rw < guard[0], guard)

m.ai_fetchers_set("web", m.load_registry()["instances"]["web"], "all", who="joerg")
rx = re.compile("(?i)(" + rx_of(site("web").splitlines()) + ")")
ok("all: auch ChatGPT-User, nie Googlebot", rx.search(UA["ChatGPT-User"]) and not rx.search(UA["Googlebot"]))
ok("Wiederholung ändert nichts", "already" in m.ai_fetchers_set(
    "web", m.load_registry()["instances"]["web"], "all"))
try:
    m.ai_fetchers_set("web", m.load_registry()["instances"]["web"], "alles")
    ok("unbekannte Stufe abgewiesen", False)
except m.DiagnoseRefused:
    ok("unbekannte Stufe abgewiesen", True)
body = "\n".join(m.site_body(ROUTES, "c", 8080, None, "web", None, edge="10.0.0.1"))
ok("auch hinter einer Kante (gemeinsamer Rumpf)", "oaap_ai" in body)
m.ai_fetchers_set("web", m.load_registry()["instances"]["web"], "allow", who="joerg")
ok("zulassen: Satz ohne Schalter, Site ohne Regel",
   "ai_fetchers" not in m.load_registry()["instances"]["web"] and "oaap_ai" not in site("web"))

SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("Update übernimmt den Schalter (wie login_required)",
   'reg["instances"][name]["ai_fetchers"] = dict(inst["ai_fetchers"])' in SRC)
ok("Spool-Tür des Portals", '"ai-fetchers", "site-watch", "site-check"):' in SRC and 'action == "ai-fetchers"' in SRC)
ok("CLI: oaap app ai-fetchers", 'sub.add_parser("ai-fetchers"' in SRC)

print("\nStatistik: abgewiesen")
hostmap = {"web.example.org": "web"}


def line(ua, status, path="/"):
    return json.dumps({"ts": NOW, "status": status, "request": {
        "host": "web.example.org", "uri": path, "method": "GET",
        "headers": {"User-Agent": [ua]}}, "resp_headers": {"Content-Type": ["text/plain"]}})


NOW = time.time()
stats = {}
ss.fold(stats, [line(UA["GPTBot"], 403), line(UA["GPTBot"], 403), line(UA["GPTBot"], 200, "/robots.txt"),
                line(UA["Googlebot"], 200)], hostmap, now=NOW)
day = next(iter(stats["web"]["days"].values()))
ok("403 je Abrufer gezählt, robots.txt-Abruf nicht", day["bots"]["GPTBot"] == 3
   and day.get("denied") == {"GPTBot": 2}, day)
base = tempfile.mkdtemp()
with open(os.path.join(base, ss.FILE), "w", encoding="utf-8") as f:
    json.dump(stats["web"], f)
s = ss.read(base, 7, now=NOW)
ok("read: denied je Name", s["denied"] == {"GPTBot": 2}, s.get("denied"))

print("\nPortal")
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-ai-portal-"))
sys.path.insert(0, os.path.join(PLATFORM, "services", "portal"))
try:
    import app as portal  # noqa: E402
    from jinja2 import Environment
    src = portal.INSTANCE_EDIT_BODY
    a = src.index("<section class=\"panel {{ 'active' if tab == 'statistik' }}\">")
    b = src.index("</section>", a) + len("</section>")
    t = Environment(autoescape=True).from_string(src[a:b])
    kw = dict(tab="statistik", device_titles=ss.DEVICE_TITLES, kind_titles=ss.KIND_TITLES,
              site_keep_days=ss.DAYS)
    mode = portal._mode_view({"ai_fetchers": {"block": "training", "since": "2026-11-27T10:00:00Z",
                                              "by": "joerg", "reason": "kein Training"}})
    html = t.render(i={"key": "web", "site": s, "site_days": 7, "mode": mode}, **kw)
    ok("Karte: Stufe, wer, Grund, gesperrte Namen",
       "KI-Abrufer" in html and "Training sperren" in html and "joerg" in html
       and "kein Training" in html and "GPTBot" in html, html[:300])
    ok("drei Stufen, die gesetzte gewählt",
       html.count('name="level"') == 3 and re.search(r'value="training"\s+checked', html))
    ok("Formular geht an die Tür, zurück in den Reiter",
       'action="/instances/web/ai-fetchers"' in html and 'name="tab" value="statistik"' in html)
    ok("Tabelle zeigt „abgewiesen“", "abgewiesen" in html and ">2<" in html.replace(" ", ""))
    html0 = t.render(i={"key": "web", "site": None, "site_days": 30, "mode": portal._mode_view({})}, **kw)
    ok("ohne Schalter: zulassen gewählt, Hinweis auf robots.txt der App",
       re.search(r'value=""\s+checked', html0) and "geht unverändert hinaus" in html0)
    ok("Tür im Portal", any(r.rule == "/instances/<name>/ai-fetchers" for r in portal.app.url_map.iter_rules()))
except ImportError as e:
    print("SKIP  Portal (Flask fehlt):", e)

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
