"""Page statistics per instance, from the gateway's access log
(I-81 idea 2 + 3, Jörg 2026-11-27: "Bau die Seitenstatistik aus I-81").

No cookie, no script, nothing stored in the visitor's browser: the minutely
sampler already reads the external access log (traffic.measure) and hands
the lines over; this module counts them per instance and UTC day.

Per instance and day:

    views    page views: GET, answer 2xx/304, response Content-Type
             text/html, not /platform/*, not a known automaton
    hours    the same views per UTC hour (24 numbers)
    pages    views per path (query string dropped -- it may carry a token)
    ref      views per referring host, only hosts that are not the
             instance's own (a search engine shows up as www.google.com)
    nf       GET answers 404 per path, people and automata alike
    dev      views per device class: desktop, mobil, tablet (from the
             User-Agent, coarse)
    bots     every request of a known automaton per name (search engines,
             AI fetchers, others) -- "springt Google an?"

    fake     (0.1.399) requests that claimed a search engine whose reverse
             DNS says otherwise, per claimed name -- counted under
             FAKE_NAME in `bots`, never as the engine
    checked  (0.1.399) requests of such an engine that reverse DNS confirmed
    vis, multi, dur, vpg
             (0.1.399) visits: how many ended that day (counted on the day
             they began), how many had more than one page, their seconds
             from the first to the last page, and their pages

and over all days `bot_last`: when each named automaton was last seen.

What is NOT kept: an IP address, a user, a cookie, a query string, the
User-Agent itself.

**Visits** (0.1.399, Jörg 2026-11-27: "Tagessalz, 30 Min"). Views of one
browser belong to one visit while no more than 30 minutes lie between them.
"One browser" is a hash of a salt, the instance, the address and the
User-Agent; the salt is random and new every UTC day, so yesterday's hash
cannot be linked to today's and no visitor can be followed across days.
The open visits and the salt live in /run (RUN_DIR) -- memory, gone with a
reboot, never on the disk -- and only the numbers per day reach `site.json`.
A visit open at midnight ends there. A visit's duration runs from its first
to its last page: how long the last page was read no access log can see.

**Search engines are checked** (0.1.399, I-81 idea 3). Anybody can claim to
be Googlebot. For the engines that publish how (VERIFY), the address is
looked up backwards and forwards: its name must end in the engine's domain
and that name must point back at the address. A request that fails is
counted as FAKE_NAME -- so a fake Googlebot never keeps `site.google` quiet.
The verdict per address is kept in /run for the UTC day, under the day's
salt, never the address itself; at most DNS_BUDGET new addresses per minute
are looked up -- one beyond it counts as what it claims, unchecked.

The log is DATA from the internet: every field is parsed, never trusted;
only hosts the registry knows are counted, every list per day is bounded.

`fold` is pure; `sample` reads and writes one file per instance beside its
history (instance_metrics), so it goes away with the instance.
"""
import calendar
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

FORMAT = 1
FILE = "site.json"
DAYS = 90
MAX_KEYS = 100                    # paths / referrers / 404s per day
OTHER = "(weitere)"
PATH_LEN = 120
DEVICES = ("desktop", "mobil", "tablet")
DEVICE_TITLES = {"desktop": "Desktop", "mobil": "Mobil", "tablet": "Tablet"}

# (name shown, kind, needle in the User-Agent, lower case). Order matters:
# the first match wins, the specific before the generic.
KIND_TITLES = {"search": "Suchmaschine", "ai": "KI", "other": "Automat"}
BOTS = (
    ("Googlebot", "search", "googlebot"),
    ("Google (andere)", "search", "google-inspectiontool"),
    ("Bingbot", "search", "bingbot"),
    ("DuckDuckBot", "search", "duckduckbot"),
    ("YandexBot", "search", "yandex"),
    ("Baiduspider", "search", "baiduspider"),
    ("Applebot", "search", "applebot"),
    ("Qwant", "search", "qwantbot"),
    ("Ecosia", "search", "ecosia"),
    ("GPTBot", "ai", "gptbot"),
    ("OAI-SearchBot", "ai", "oai-searchbot"),
    ("ChatGPT-User", "ai", "chatgpt-user"),
    ("ClaudeBot", "ai", "claudebot"),
    ("Claude-User", "ai", "claude-user"),
    ("Claude-SearchBot", "ai", "claude-searchbot"),
    ("anthropic-ai", "ai", "anthropic-ai"),
    ("PerplexityBot", "ai", "perplexitybot"),
    ("Perplexity-User", "ai", "perplexity-user"),
    ("CCBot", "ai", "ccbot"),
    ("Bytespider", "ai", "bytespider"),
    ("Amazonbot", "ai", "amazonbot"),
    ("Meta", "ai", "meta-externalagent"),
    ("Google-Extended", "ai", "google-extended"),
    ("Mistral", "ai", "mistralai-user"),
    ("Cohere", "ai", "cohere-ai"),
    # RFC-0141 F3: the platform's own site check -- an automaton, never a
    # visit (stage 0 counted 63 of its requests as page views)
    ("Seiten-Check (OAAP)", "other", "oaap-seiten-check"),
)
BOT_KIND = {b[0]: b[1] for b in BOTS}

# The operator's switch "KI-Abrufer" (oaap.core.gateway 0.2.27): which AI
# fetchers a level shuts out. Per AI row of BOTS its robots.txt token and
# its purpose -- "training" collects for a model, "answers" fetches for a
# search answer or for a person asking a chat right now. Search engines
# are never touched. One table: the gateway's 403, the robots.txt it
# writes and the portal's list all read it, so they cannot disagree.
AI_LEVELS = ("training", "all")
AI_LEVEL_TITLES = {"": "zulassen", "training": "Training sperren",
                   "all": "alle KI-Abrufer sperren"}
AI_ROBOTS = {
    "GPTBot": ("GPTBot", "training"),
    "ClaudeBot": ("ClaudeBot", "training"),
    "anthropic-ai": ("anthropic-ai", "training"),
    "CCBot": ("CCBot", "training"),
    "Bytespider": ("Bytespider", "training"),
    "Amazonbot": ("Amazonbot", "training"),
    "Meta": ("meta-externalagent", "training"),
    "Google-Extended": ("Google-Extended", "training"),
    "Cohere": ("cohere-ai", "training"),
    "OAI-SearchBot": ("OAI-SearchBot", "answers"),
    "ChatGPT-User": ("ChatGPT-User", "answers"),
    "Claude-User": ("Claude-User", "answers"),
    "Claude-SearchBot": ("Claude-SearchBot", "answers"),
    "PerplexityBot": ("PerplexityBot", "answers"),
    "Perplexity-User": ("Perplexity-User", "answers"),
    "Mistral": ("MistralAI-User", "answers"),
}
# Tokens that only ever appear in robots.txt -- read by a search engine's
# own crawler (Applebot) to decide on training; no request carries them.
AI_ROBOTS_ONLY = (("Applebot-Extended", "training"),)


def ai_level(value):
    """The level a record's value names, or "" (allow)."""
    return value if value in AI_LEVELS else ""


def ai_blocked(level):
    """(robots.txt tokens, User-Agent needles, display names) a level
    shuts out; all empty for "" (allow)."""
    level = ai_level(level)
    if not level:
        return [], [], []
    want = ("training",) if level == "training" else ("training", "answers")
    tokens, needles, names = [], [], []
    for name, _kind, needle in BOTS:
        tok = AI_ROBOTS.get(name)
        if tok and tok[1] in want:
            tokens.append(tok[0])
            needles.append(needle)
            names.append(name)
    tokens += [t for t, purpose in AI_ROBOTS_ONLY if purpose in want]
    return tokens, needles, names


def robots_txt(level):
    """The robots.txt the gateway answers while the switch is on: one group
    for the shut-out tokens, everybody else allowed."""
    tokens = ai_blocked(level)[0]
    if not tokens:
        return ""
    lines = ["# OAAP: KI-Abrufer gesperrt (" + AI_LEVEL_TITLES[ai_level(level)] + ")"]
    lines += ["User-agent: " + t for t in tokens]
    lines += ["Disallow: /", "", "User-agent: *", "Allow: /", ""]
    return "\n".join(lines)
GENERIC = ("bot", "crawler", "spider", "slurp", "curl/", "wget/", "python-",
           "go-http-client", "httpclient", "okhttp", "java/", "libwww",
           "headless", "scrapy", "uptime", "monitor", "facebookexternalhit",
           "preview", "fetch")
GENERIC_NAME = "andere Automaten"
BOT_KIND[GENERIC_NAME] = "other"

# I-81 idea 3 (0.1.399): the engines that say how to check them by reverse
# DNS -- the name of the address must end in one of these. Not
# googleusercontent.com: that is anybody's machine in Google's cloud.
VERIFY = {
    "Googlebot": (".googlebot.com", ".google.com"),
    "Google (andere)": (".googlebot.com", ".google.com"),
    "Bingbot": (".search.msn.com",),
    "Applebot": (".applebot.apple.com",),
    "YandexBot": (".yandex.ru", ".yandex.net", ".yandex.com"),
    "Baiduspider": (".baidu.com", ".baidu.jp"),
}
VERIFY_NEEDLES = tuple(b[2] for b in BOTS if b[0] in VERIFY)
FAKE_NAME = "angebliche Suchmaschine"
BOT_KIND[FAKE_NAME] = "other"
DNS_BUDGET = 16           # new addresses looked up per minute
DNS_WAIT = 4.0            # seconds the minute waits for them, all together

# Visits (0.1.399): the short-lived state lives in memory (/run is tmpfs)
RUN_DIR = os.environ.get("OAAP_SITE_RUN_DIR") or "/run/oaap-site"
RUN_FILE = "state.json"
VISIT_IDLE = 30 * 60      # a pause longer than this ends a visit
VISIT_CAP = 5000          # open visits per instance; beyond, not followed


def empty():
    return {"v": FORMAT, "days": {}, "bot_last": {}}


def bot_of(ua):
    """The automaton a User-Agent names, or "" for a browser. No User-Agent
    at all is an automaton too -- no browser sends none."""
    if not isinstance(ua, str) or not ua.strip():
        return GENERIC_NAME
    low = ua.lower()
    for name, _kind, needle in BOTS:
        if needle in low:
            return name
    if any(g in low for g in GENERIC):
        return GENERIC_NAME
    return ""


def device_of(ua, mobile_hint=""):
    low = (ua or "").lower()
    if "ipad" in low or "tablet" in low or ("android" in low and "mobi" not in low):
        return "tablet"
    if "mobi" in low or "iphone" in low or str(mobile_hint).strip() == "?1":
        return "mobil"
    return "desktop"


def _hdr(headers, name):
    """First value of a header in Caddy's log form ({"Name": ["v"]})."""
    if not isinstance(headers, dict):
        return ""
    v = headers.get(name)
    if isinstance(v, list):
        v = v[0] if v else ""
    return v if isinstance(v, str) else ""


def _host(h):
    h = h.strip().lower() if isinstance(h, str) else ""
    if h.startswith("["):
        return h
    return h.split(":", 1)[0]


def _ip(req):
    """The visitor's address: behind an edge Caddy writes `client_ip` from
    the edge's X-Forwarded-For (trusted_proxies), else `remote_ip`. Only
    ever an address, normalised -- or ""."""
    for k in ("client_ip", "remote_ip"):
        v = req.get(k)
        if isinstance(v, str) and v:
            try:
                return str(ipaddress.ip_address(v.strip("[]")))
            except ValueError:
                continue
    return ""


def _path(uri):
    if not isinstance(uri, str) or not uri.startswith("/"):
        return ""
    p = uri.split("?", 1)[0].split("#", 1)[0]
    return p[:PATH_LEN]


def _bump(d, key, cap=MAX_KEYS):
    if key not in d and len(d) >= cap:
        key = OTHER
    d[key] = d.get(key, 0) + 1


def _day(stats, day):
    d = stats["days"].get(day)
    if not isinstance(d, dict):
        d = {"views": 0, "hours": [0] * 24, "pages": {}, "ref": {}, "nf": {},
             "dev": {}, "bots": {}}
        stats["days"][day] = d
    return d


def classify(d, hostmap):
    """(instance key, record) of one parsed log line, or None.
    record: {"ts", "what": view|notfound|bot|other, "path", "ref", "dev",
    "bot"}."""
    if not isinstance(d, dict):
        return None
    req = d.get("request")
    if not isinstance(req, dict):
        return None
    key = hostmap.get(_host(req.get("host")))
    if not key:
        return None
    status = d.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    ts = d.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else time.time()
    headers = req.get("headers")
    ua = _hdr(headers, "User-Agent")
    rec = {"ts": ts, "what": "other", "path": _path(req.get("uri")), "ref": "",
           "dev": "", "bot": bot_of(ua), "ip": _ip(req), "ua": ua}
    method = req.get("method")
    if rec["bot"]:
        rec["what"] = "bot"
        if status == 403:
            rec["denied"] = True
        if method == "GET" and status == 404 and rec["path"]:
            rec["nf"] = True
        return key, rec
    if method != "GET" or not rec["path"]:
        return key, rec
    if status == 404:
        rec["what"] = "notfound"
        return key, rec
    if rec["path"].startswith("/platform/"):
        return key, rec
    if not (200 <= status < 300 or status == 304):
        return key, rec
    ctype = _hdr(d.get("resp_headers"), "Content-Type").lower()
    # 304 carries no Content-Type: a page if the browser asked for a document
    if status == 304 and not ctype:
        if _hdr(headers, "Sec-Fetch-Dest") == "document":
            ctype = "text/html"
    if not ctype.startswith("text/html"):
        return key, rec
    rec["what"] = "view"
    rec["dev"] = device_of(ua, _hdr(headers, "Sec-Ch-Ua-Mobile"))
    ref = _hdr(headers, "Referer")
    if ref:
        try:
            rh = _host(urlsplit(ref).hostname or "")
        except ValueError:
            rh = ""
        if rh and hostmap.get(rh) != key:
            rec["ref"] = rh[:80]
    return key, rec


def fold(stats_by_key, lines, hostmap, now=None, verdict=None, visit=None):
    """Add the log lines to {instance key: stats}; returns the set of keys
    that changed. Old days fall out (DAYS).

    `verdict(bot, ip)` -> True / False / None (unknown): the reverse DNS
    check of a search engine (VERIFY). `visit(key, rec)` is told every
    view, to follow visits. Both optional -- without them nothing is
    checked and no visit is counted."""
    changed = set()
    for line in lines or []:
        line = line.strip() if isinstance(line, str) else ""
        if not line or '"request"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        got = classify(d, hostmap)
        if not got:
            continue
        key, rec = got
        if rec["what"] == "other" and not rec.get("nf"):
            continue
        stats = stats_by_key.get(key)
        if not isinstance(stats, dict) or stats.get("v") != FORMAT:
            stats = stats_by_key[key] = empty()
        gm = time.gmtime(rec["ts"])
        day = _day(stats, time.strftime("%Y-%m-%d", gm))
        if rec["what"] == "view":
            day["views"] += 1
            day["hours"][gm.tm_hour] += 1
            _bump(day["pages"], rec["path"])
            _bump(day["dev"], rec["dev"])
            if rec["ref"]:
                _bump(day["ref"], rec["ref"])
        elif rec["what"] == "notfound" or rec.get("nf"):
            _bump(day["nf"], rec["path"])
            if rec["what"] == "notfound":
                # I-81 idea 5: a person's 404 -- a scanner's flood of
                # /wp-login.php is no reason to mail anybody
                day["nfp"] = (day.get("nfp") or 0) + 1
        if rec["what"] == "bot" and verdict and rec["bot"] in VERIFY:
            v = verdict(rec["bot"], rec.get("ip"))
            if v is False:
                _bump(day.setdefault("fake", {}), rec["bot"], cap=len(BOT_KIND) + 1)
                rec["bot"] = FAKE_NAME
            elif v:
                _bump(day.setdefault("checked", {}), rec["bot"], cap=len(BOT_KIND) + 1)
        if rec["what"] == "view" and visit:
            visit(key, rec)
        if rec["what"] == "bot":
            _bump(day["bots"], rec["bot"], cap=len(BOT_KIND) + 1)
            if rec.get("denied"):
                # the gateway's 403 of the switch "KI-Abrufer" (and any
                # other refusal) -- so the page shows whether it works
                _bump(day.setdefault("denied", {}), rec["bot"],
                      cap=len(BOT_KIND) + 1)
            last = stats["bot_last"].get(rec["bot"], 0)
            if rec["ts"] > (last if isinstance(last, (int, float)) else 0):
                stats["bot_last"][rec["bot"]] = int(rec["ts"])
        changed.add(key)
    cut = time.strftime("%Y-%m-%d", time.gmtime((now or time.time()) - DAYS * 86400))
    today = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    for key, stats in stats_by_key.items():
        if isinstance(stats, dict) and not stats.get("nfp_from"):
            # the first day whose people's 404 (`nfp`) are counted: before
            # it a missing `nfp` means "not counted", after it "none"
            stats["nfp_from"] = today
            changed.add(key)
        days = stats.get("days") if isinstance(stats, dict) else None
        if isinstance(days, dict):
            old = [k for k in days if k <= cut]
            for k in old:
                del days[k]
            if old:
                changed.add(key)
    return changed


def close_visits(stats_by_key, closed, load=None):
    """Count ended visits [(key, first ts, last ts, pages)] on the day each
    began. `load(key)` gives the stats of a key not in `stats_by_key` yet.
    Returns the keys that changed."""
    changed = set()
    for key, first, last, pages in closed:
        stats = stats_by_key.get(key)
        if not isinstance(stats, dict) and load:
            stats = stats_by_key[key] = load(key)
        if not isinstance(stats, dict):
            continue
        day = _day(stats, time.strftime("%Y-%m-%d", time.gmtime(first)))
        day["vis"] = (day.get("vis") or 0) + 1
        day["vpg"] = (day.get("vpg") or 0) + max(1, int(pages))
        if pages > 1:
            day["multi"] = (day.get("multi") or 0) + 1
            day["dur"] = (day.get("dur") or 0) + max(0, int(last - first))
        changed.add(key)
    return changed


class Visits:
    """The open visits of a run, from and to the state in RUN_DIR."""

    def __init__(self, state):
        self.state = state
        self.open = state.setdefault("visits", {})
        self.ended = []           # visits a returning browser ended this run

    def sid(self, key, rec):
        h = hashlib.sha256("|".join((self.state["salt"], key, rec.get("ip") or "",
                                     rec.get("ua") or "")).encode("utf-8", "replace"))
        return h.hexdigest()[:20]

    def see(self, key, rec):
        mine = self.open.setdefault(key, {})
        sid = self.sid(key, rec)
        v = mine.get(sid)
        ts = rec["ts"]
        if v and ts - v[1] > VISIT_IDLE:
            self.ended.append((key, v[0], v[1], v[2]))
            v = None
        if v is None:
            if len(mine) >= VISIT_CAP:
                return
            mine[sid] = [ts, ts, 1]
            return
        v[1] = max(v[1], ts)
        v[2] += 1

    def expire(self, now, everything=False):
        out = []
        for key, mine in list(self.open.items()):
            for sid, v in list(mine.items()):
                if everything or now - v[1] > VISIT_IDLE:
                    out.append((key, v[0], v[1], v[2]))
                    del mine[sid]
            if not mine:
                del self.open[key]
        return out


def run_state(run_dir, now):
    """(state, ended visits) -- the salt is new every UTC day, and a new
    day ends every open visit and forgets every DNS verdict. None as
    state when RUN_DIR cannot be used: then no visit is followed."""
    if not run_dir:
        return None, []
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        with open(os.path.join(run_dir, RUN_FILE), encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except (OSError, ValueError):
        st = {}
        if not os.path.isdir(run_dir) or not os.access(run_dir, os.W_OK):
            return None, []
    ended = []
    if st.get("day") != today or not isinstance(st.get("salt"), str):
        if isinstance(st.get("visits"), dict) and isinstance(st.get("salt"), str):
            ended = Visits(st).expire(now, everything=True)
        st = {"day": today, "salt": secrets.token_hex(16), "dns": {}, "visits": {}}
    if not isinstance(st.get("dns"), dict):
        st["dns"] = {}
    return st, ended


def save_run_state(run_dir, st):
    if not run_dir or st is None:
        return
    try:
        p = os.path.join(run_dir, RUN_FILE)
        tmp = p + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, separators=(",", ":"))
        os.replace(tmp, p)
    except OSError:
        pass


def check_dns(ip, suffixes):
    """True when the address's name ends in one of `suffixes` and that name
    points back at the address; False when it does not (or has no name);
    None when DNS could not say (a timeout, a server failure)."""
    try:
        host = socket.gethostbyaddr(ip)[0].lower().rstrip(".")
    except socket.herror as e:
        return False if e.errno in (1, 4) else None      # not found / no data
    except (socket.gaierror, OSError, UnicodeError):
        return None
    if not host.endswith(tuple(suffixes)):
        return False
    try:
        back = {str(ipaddress.ip_address(a[4][0].split("%", 1)[0]))
                for a in socket.getaddrinfo(host, None)}
    except socket.gaierror as e:
        return False if e.errno == socket.EAI_NONAME else None
    except (OSError, UnicodeError, ValueError):
        return None
    return ip in back


def resolve_all(asks, resolve=check_dns, wait=DNS_WAIT):
    """{(bot, ip): verdict} for the asks, in parallel, all together at most
    `wait` seconds. A lookup still running then is None -- its thread is a
    daemon and does not hold the minute up."""
    out = {}
    lock = threading.Lock()

    def one(bot, ip):
        try:
            v = resolve(ip, VERIFY[bot])
        except Exception:                                  # noqa: BLE001
            v = None
        with lock:
            out[(bot, ip)] = v

    threads = [threading.Thread(target=one, args=a, daemon=True) for a in asks]
    for t in threads:
        t.start()
    end = time.monotonic() + wait
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    with lock:
        return {a: out.get(a) for a in asks}


def dns_verdicts(lines, hostmap, st, resolve=check_dns, budget=DNS_BUDGET, wait=DNS_WAIT):
    """The verdict function for `fold`: looks up the new addresses of this
    run's search engine requests (at most `budget`) and keeps each verdict
    in the run state for the day, under the day's salt."""
    cache = st["dns"] if st is not None else {}
    salt = (st or {}).get("salt") or ""

    def h(bot, ip):
        # the same domains, the same verdict: Googlebot and "Google (andere)" share it
        return hashlib.sha256(f"{salt}|{','.join(VERIFY[bot])}|{ip}".encode()).hexdigest()[:20]

    asks = []
    for line in lines or []:
        if not isinstance(line, str):
            continue
        low = line.lower()
        if not any(n in low for n in VERIFY_NEEDLES):
            continue
        try:
            got = classify(json.loads(line), hostmap)
        except ValueError:
            continue
        if not got or got[1]["bot"] not in VERIFY or not got[1]["ip"]:
            continue
        a = (got[1]["bot"], got[1]["ip"])
        if h(*a) not in cache and a not in asks:
            asks.append(a)
    for a, v in resolve_all(asks[:budget], resolve, wait).items():
        if v is not None:
            cache[h(*a)] = v

    def verdict(bot, ip):
        if not ip:
            return None
        return cache.get(h(bot, ip))
    return verdict


# --- disk (the sampler) ------------------------------------------------

def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("v") == FORMAT else empty()
    except (OSError, ValueError):
        return empty()


def _save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def hostmap_of(targets):
    """{host: instance key} from instance_metrics' targets. A host two
    instances claim is counted for neither -- a guess is not a count."""
    seen, dup = {}, set()
    for key, t in (targets or {}).items():
        for h in t.get("hosts") or []:
            h = _host(h)
            if not h:
                continue
            if h in seen and seen[h] != key:
                dup.add(h)
            seen[h] = key
    return {h: k for h, k in seen.items() if h not in dup}


def sample(base, now, lines, targets, instance_dir, run_dir=RUN_DIR, resolve=check_dns):
    """Fold this run's lines into the instances' files. `instance_dir` is
    instance_metrics.instance_dir (base, tenant, key) -> path. Writes only
    the files that changed, and once a day each (old days fall out).
    `run_dir` holds the open visits and the day's DNS verdicts (memory);
    `resolve` is the reverse DNS check (a test hands its own)."""
    if not targets:
        return
    hostmap = hostmap_of(targets)
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    touched, stats, paths = {}, {}, {}
    for key in set(hostmap.values()):
        t = targets[key]
        tenant = t.get("tenant") or ""
        if not tenant or "/" in tenant or "/" in key or key.startswith("."):
            continue
        d = instance_dir(base, tenant, key)
        paths[key] = os.path.join(d, FILE)
    # Read only the files this run can change: an instance whose host is in
    # the lines, and once a day every one (old days fall out). A minute on
    # a small node with forty instances is then one or two files, not forty.
    marker = os.path.join(base, ".site-folded")
    try:
        with open(marker, encoding="utf-8") as f:
            daily = f.read().strip() != today
    except OSError:
        daily = True
    all_paths = dict(paths)
    if not daily:
        blob = "\n".join(lines or [])
        hit = {k for h, k in hostmap.items()
               if '"' + h + '"' in blob or '"' + h + ':' in blob}
        paths = {k: p for k, p in paths.items() if k in hit}
    st, ended = run_state(run_dir, now)
    if st is not None:
        # an instance with an open visit must be read when the visit ends
        paths.update({k: all_paths[k] for k in (st.get("visits") or {}) if k in all_paths})
    paths.update({k: all_paths[k] for k, *_r in ended if k in all_paths})
    full_hostmap = hostmap
    hostmap = {h: k for h, k in hostmap.items() if k in paths}
    for key, p in paths.items():
        stats[key] = _load(p)
    visits = Visits(st) if st is not None else None
    verdict = dns_verdicts(lines, full_hostmap, st, resolve)
    changed = fold(stats, lines or [], hostmap, now, verdict=verdict,
                   visit=visits.see if visits else None)
    if visits:
        ended += visits.ended + visits.expire(now)
        for key in stats:
            if not stats[key].get("vis_from"):
                stats[key]["vis_from"] = today
                changed.add(key)
    changed |= close_visits(stats, [e for e in ended if e[0] in paths])
    save_run_state(run_dir, st)
    for key, p in paths.items():
        s = stats[key]
        if key in changed or (s.get("days") and s.get("folded") != today):
            s["folded"] = today
            touched[key] = s
    for key, s in touched.items():
        p = paths[key]
        if not s.get("days") and not s.get("bot_last") and not os.path.exists(p):
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        _save(p, s)
    if daily:
        os.makedirs(base, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(today)


# --- reading (the portal) ----------------------------------------------

def read(base_dir, days=30, now=None):
    """The summary the portal shows, from one instance's directory:
    {"days": [(day, views)], "views", "pages", "ref", "nf", "dev", "bots":
    [(name, kind, n, last ts)], "denied": {name: refused}, "hours"} over the last `days` UTC days,
    or None when nothing was ever counted."""
    s = _load(os.path.join(base_dir, FILE))
    if not s.get("days") and not s.get("bot_last"):
        return None
    now = now or time.time()
    want = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
            for i in range(days - 1, -1, -1)]
    out = {"days": [], "views": 0, "pages": {}, "ref": {}, "nf": {}, "dev": {},
           "bots": {}, "denied": {}, "fake": {}, "checked": {}, "hours": [0] * 24,
           # 0.1.399: visits (n, more than one page, their seconds, pages)
           "visits": {"n": 0, "multi": 0, "dur": 0, "pages": 0,
                      "from": s.get("vis_from") or ""},
           # I-81 idea 5: two more numbers per day for the portal's bars
           "series": {"search": [], "nf": []}}
    for day in want:
        d = s["days"].get(day) or {}
        v = d.get("views") or 0
        out["days"].append((day, v))
        out["series"]["search"].append(
            (day, sum(n for b, n in (d.get("bots") or {}).items()
                      if isinstance(n, int) and BOT_KIND.get(b) == "search")))
        out["series"]["nf"].append(
            (day, sum(n for n in (d.get("nf") or {}).values() if isinstance(n, int))))
        out["views"] += v
        for k, src in (("n", "vis"), ("multi", "multi"), ("dur", "dur"), ("pages", "vpg")):
            n = d.get(src)
            if isinstance(n, int):
                out["visits"][k] += n
        for k in ("pages", "ref", "nf", "dev", "bots", "denied", "fake", "checked"):
            for name, n in (d.get(k) or {}).items():
                if isinstance(n, int):
                    out[k][name] = out[k].get(name, 0) + n
        for i, n in enumerate((d.get("hours") or [])[:24]):
            if isinstance(n, int):
                out["hours"][i] += n
    last = s.get("bot_last") or {}
    names = set(out["bots"]) | {n for n in last if n in BOT_KIND}
    out["bots"] = sorted(((n, BOT_KIND.get(n, "other"), out["bots"].get(n, 0),
                           _when(last.get(n))) for n in names),
                         key=lambda b: (b[1] == "other", -b[2], b[0]))
    for k in ("pages", "ref", "nf", "dev"):
        out[k] = sorted(out[k].items(), key=lambda kv: (-kv[1], kv[0]))
    return out


# --- watching (I-81 idea 5: notifications) -----------------------------
#
# Two conditions an operator can ask to hear about, per instance (the switch
# "Hinweise zur Webseite" on the record). Pure: the stats, when the switch
# was turned on, now -- and whether the matter is open already (hysteresis).
# appctl turns them into the matters `site.google` and `site.notfound`
# (oaap.core.notifications 0.17).

GOOGLE = "Googlebot"
GOOGLE_DAYS = 14          # "Google war seit 14 Tagen nicht da"
NF_WINDOW = 7             # the last seven full UTC days ...
NF_BASE_DAYS = 28         # ... against up to four weeks before them
NF_BASE_MIN = 7           # a baseline needs at least a week of counting
NF_MIN = 20               # opens at 20 people's 404 in a week ...
NF_FACTOR = 2.0           # ... and twice the weekly mean before
NF_KEEP_MIN = 10          # stays open while >= 10 ...
NF_KEEP_FACTOR = 1.5      # ... and >= 1.5 x the mean


def _day_str(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def google_absent(stats, since_ts, now, is_open=False):
    """(days, text) when Googlebot has not fetched for GOOGLE_DAYS -- counted
    from its last visit, or from when the switch was turned on, whichever
    is later (a new site was never visited; that is no 14 days' absence on
    the first minute). None otherwise. `is_open` is accepted for symmetry:
    a visit closes it at once."""
    s = stats if isinstance(stats, dict) else {}
    last = (s.get("bot_last") or {}).get(GOOGLE)
    last = last if isinstance(last, (int, float)) and not isinstance(last, bool) else 0
    since_ts = since_ts if isinstance(since_ts, (int, float)) else 0
    ref = max(last, since_ts)
    if not ref:
        return None
    days = int((now - ref) // 86400)
    if days < GOOGLE_DAYS:
        return None
    if last and last >= since_ts:
        text = (f"Google (Googlebot) hat die Seite seit {days} Tagen nicht abgerufen — "
                f"zuletzt am {_when(last)}.")
    elif last:
        text = (f"Google (Googlebot) hat die Seite nicht abgerufen, seit die Hinweise "
                f"eingeschaltet sind ({days} Tage); zuletzt davor am {_when(last)}.")
    else:
        text = (f"Google (Googlebot) hat die Seite in den {days} Tagen, seit die "
                "Hinweise eingeschaltet sind, nicht abgerufen — und davor auch nicht, "
                "soweit die Statistik zurückreicht.")
    text += (" Mögliche Gründe: die Seite ist neu und nirgends verlinkt, sie sendet "
             "noindex, oder robots.txt sperrt Google aus. Die Google Search Console "
             "sagt, was Google sieht.")
    return days, text


def notfound_rising(stats, now, is_open=False):
    """(n, text) when people's 404 rose: the last NF_WINDOW full UTC days
    against the weekly mean of up to NF_BASE_DAYS before them. Only days
    from `nfp_from` on count; without a week of baseline there is no
    judgment. Today does not count -- a half day would make the matter
    flicker. Hysteresis: an open matter stays while the lower bar holds."""
    s = stats if isinstance(stats, dict) else {}
    start = s.get("nfp_from")
    if not isinstance(start, str):
        return None
    days = s.get("days") if isinstance(s.get("days"), dict) else {}
    today = int(now // 86400) * 86400

    def count(i):                      # i = 1 is yesterday
        d = days.get(_day_str(today - i * 86400)) or {}
        n = d.get("nfp")
        return n if isinstance(n, int) and not isinstance(n, bool) else 0

    first_window = _day_str(today - NF_WINDOW * 86400)
    if first_window < start:
        return None
    base = [i for i in range(NF_WINDOW + 1, NF_WINDOW + NF_BASE_DAYS + 1)
            if _day_str(today - i * 86400) >= start]
    if len(base) < NF_BASE_MIN:
        return None
    n = sum(count(i) for i in range(1, NF_WINDOW + 1))
    mean = sum(count(i) for i in base) * NF_WINDOW / len(base)
    lo, fac = (NF_KEEP_MIN, NF_KEEP_FACTOR) if is_open else (NF_MIN, NF_FACTOR)
    if n < lo or n < fac * mean:
        return None
    paths = {}
    for i in range(1, NF_WINDOW + 1):
        for p, k in ((days.get(_day_str(today - i * 86400)) or {}).get("nf") or {}).items():
            if isinstance(k, int):
                paths[p] = paths.get(p, 0) + k
    top = sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    text = (f"In den letzten {NF_WINDOW} Tagen riefen Besucher {n}-mal eine Seite auf, "
            f"die es nicht gibt (404); in den {len(base)} Tagen davor waren es im Mittel "
            f"{mean:.0f} je Woche.")
    if top:
        text += ("\nAm häufigsten (alle Abrufer, auch Automaten):\n"
                 + "\n".join(f"  - {p} ({k})" for p, k in top))
    text += ("\nOft ist es ein geänderter oder gelöschter Link, auf den noch "
             "verwiesen wird — eine Weiterleitung oder die Seite zurück hilft.")
    return n, text


def _when(ts):
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return ""
    return time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(ts))


def day_epoch(day):
    return calendar.timegm(time.strptime(day, "%Y-%m-%d"))
