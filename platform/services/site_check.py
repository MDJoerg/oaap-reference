"""Site check per production instance (RFC-0141, stages 1-3).

Once a week and on a button the node reads an instance's public start page
the way a visitor's browser does and compares what it finds with a short
list of rules: Sicherheit, Auffindbarkeit, Tempo and -- hints, not legal
advice -- Recht (stage 2). Stage 3 reads up to ten more pages, from the
sitemap and the start page's own links, for links that lead nowhere and
pages without a title; the last twelve runs make a small history.

Reading only (RFC-0141 §2): GET / on the canonical name, following at most
five redirects on the same host; at most 30 same-origin scripts, styles and
images it names, each at most 5 MB; /robots.txt; the sitemap's head;
`http://<canonical>/` and each alias without following; up to ten pages
on the same name (stage 3). One request at a
time, 200 ms apart, 15 s timeout, an honest User-Agent. No secret paths, no
forms, no login. What is kept: findings and a few numbers -- never the
page's text, never a cookie.

Two halves, so the rules can be tested without a network:

    collect(canon, aliases, ...) -> observations (a small JSON dict)
    evaluate(obs, ctx)           -> findings

and the store the host writes and the portal reads:

    record(instance_dir, result) / read(instance_dir) / view(instance_dir)

Shared by the host (appctl) and the portal (its Dockerfile copies it).
Stdlib only.
"""
import datetime
import gzip
import hashlib
import html.parser
import http.client
import json
import os
import re
import socket
import ssl
import time
import urllib.parse
import zlib

FORMAT = 1
FILE = "site_check.json"
KEEP = 12                         # results kept per instance (§2.3)
UA_NAME = "OAAP-Seiten-Check"
UA_VERSION = 1
MAX_ASSETS = 30
MAX_BYTES = 5 * 1024 * 1024
MAX_SMALL = 64 * 1024             # robots.txt, the sitemap's head
MAX_REDIRECTS = 5
MAX_PAGES = 10                    # stage 3: pages beside the start page
GAP = 0.2                         # seconds between two requests
TIMEOUT = 15
RUN_EVERY = 6 * 86400             # the weekly run: not again within six days
LATE = 8 * 86400                  # ... and on any day once this late
BUTTON_EVERY = 3600               # "Jetzt prüfen" at most once an hour

AREAS = ("Sicherheit", "Auffindbarkeit", "Tempo", "Recht")
LAW_NOTE = "Hinweise, keine Rechtsberatung."
LEVELS = ("red", "yellow")
LIGHT_TITLES = {"red": "rot", "yellow": "gelb", "green": "grün", "grey": "grau"}
FIX_TITLES = {"plattform": "Plattform", "seite": "Seite", "": "—"}
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
            "Samstag", "Sonntag")
# F1 (stage 0): most production instances are apps, not web sites -- three
# or more yellow points of one area and one fixer fold into one line.
GROUP_MIN = 3
GROUP_TITLES = {
    ("Auffindbarkeit", "seite"): "Für Suchmaschinen kaum vorbereitet",
    ("Sicherheit", "seite"): "Schutzangaben der Seite fehlen",
    ("Tempo", "seite"): "Ladezeit: mehrere Punkte der Seite",
    ("Recht", "seite"): "Rechtliche Hinweise zur Seite",
}
# Stage 2 (§3.4): words in a link's text or path that mean the imprint or
# the privacy statement; cookie names that look technical.
IMPRINT_WORDS = ("impressum", "imprint", "legal-notice", "legal notice", "anbieterkennzeichnung")
PRIVACY_WORDS = ("datenschutz", "privacy", "data-protection", "data protection", "dsgvo")
TECH_COOKIE = re.compile(r"sess|csrf|xsrf|token|lang|locale|consent|cookie_?notice|cookieyes|"
                         r"borlabs|cmplz|^__host-|^__secure-|load.?balanc|^route$|^awsalb|^srv",
                         re.I)
GOOGLE_FONTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def user_agent(ext_host=""):
    return f"{UA_NAME}/{UA_VERSION} (+{ext_host or 'oaap'})"


def weekday_of(name):
    """The instance's weekday, 0 = Monday, spread by its name (§4)."""
    return int(hashlib.sha256((name or "").encode("utf-8")).hexdigest(), 16) % 7


def due(name, last_ts, now):
    """The daily job's question: run the weekly check now? On the
    instance's weekday once six days have passed, on any day once eight
    have (a node that slept through its day), and at once if never run."""
    if not last_ts:
        return True
    age = now - last_ts
    if age >= LATE:
        return True
    wd = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).weekday()
    return age >= RUN_EVERY and wd == weekday_of(name)


# --- fetching ---------------------------------------------------------

class Probe:
    """One request at a time, GAP apart, counted. IPv4 only when asked
    (behind an edge the AAAA record may name the router itself, M2)."""

    def __init__(self, ua, ipv4_only=False, gap=GAP, timeout=TIMEOUT,
                 sleep=time.sleep):
        self.ua, self.ipv4_only, self.gap, self.timeout = ua, ipv4_only, gap, timeout
        self.sleep = sleep
        self.requests = 0
        self.bytes = 0
        self._ips = {}

    def _ip(self, host):
        if host not in self._ips:
            fam = socket.AF_INET if self.ipv4_only else socket.AF_UNSPEC
            infos = socket.getaddrinfo(host, 443, fam, socket.SOCK_STREAM)
            # IPv4 first, as stage 0 showed the AAAA may lead nowhere
            infos.sort(key=lambda x: 0 if x[0] == socket.AF_INET else 1)
            self._ips[host] = infos[0][4][0]
        return self._ips[host]

    def _connect(self, host, port):
        return socket.create_connection((self._ip(host), port), self.timeout)

    def get(self, url, limit=MAX_BYTES, method="GET"):
        """One request, no redirect followed -> dict (status 0 = no answer)."""
        if self.requests:
            self.sleep(self.gap)
        self.requests += 1
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        t0 = time.time()
        c = None
        try:
            if u.scheme == "https":
                c = http.client.HTTPSConnection(host, u.port or 443, timeout=self.timeout,
                                                context=ssl.create_default_context())
            else:
                c = http.client.HTTPConnection(host, u.port or 80, timeout=self.timeout)
            c._create_connection = lambda addr, timeout, src=None: self._connect(host, addr[1])
            c.request(method, (u.path or "/") + ("?" + u.query if u.query else ""),
                      headers={"User-Agent": self.ua, "Accept-Encoding": "gzip",
                               "Accept": "text/html,*/*;q=0.8"})
            r = c.getresponse()
            ttfb = time.time() - t0
            raw = r.read(limit + 1) if method == "GET" and limit else b""
            self.bytes += len(raw)
            h = {k.lower(): v for k, v in r.getheaders()}
            cookies = [v.split("=", 1)[0].strip() for k, v in r.getheaders()
                       if k.lower() == "set-cookie"]
            body = raw[:limit]
            if h.get("content-encoding") == "gzip":
                try:
                    body = gzip.decompress(body)
                except (OSError, EOFError, zlib.error):
                    pass
            return {"status": r.status, "h": h, "raw": len(raw), "body": body,
                    "ttfb": ttfb, "big": len(raw) > limit, "cookies": cookies}
        except Exception as e:                      # noqa: BLE001
            return {"status": 0, "err": _short_err(e), "h": {}, "raw": 0, "body": b"",
                    "ttfb": time.time() - t0, "big": False, "cookies": []}
        finally:
            if c is not None:
                c.close()

    def cert(self, host):
        """-> (days left, TLS version, error)."""
        try:
            ctx = ssl.create_default_context()
            with self._connect(host, 443) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
                na = ss.getpeercert()["notAfter"]
                ver = ss.version()
            end = datetime.datetime.strptime(na, "%b %d %H:%M:%S %Y %Z")
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            return (end - now).days, ver, ""
        except Exception as e:                      # noqa: BLE001
            return None, None, _short_err(e)

    def old_tls(self, host):
        """F4: "accepted", "refused" (the SERVER said protocol version) or
        "unknown" (this machine's OpenSSL would not even offer it)."""
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.maximum_version = ssl.TLSVersion.TLSv1_1
            ctx.set_ciphers("ALL:@SECLEVEL=0")
        except (ValueError, ssl.SSLError, AttributeError):
            return "unknown"
        try:
            with self._connect(host, 443) as s, ctx.wrap_socket(s, server_hostname=host):
                return "accepted"
        except ssl.SSLError as e:
            return tls_verdict(e)
        except OSError:
            return "unknown"

    def ipv6(self, host):
        """"none" (no AAAA), "ok" or "refused" -- a TCP connect to 443."""
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            return "none"
        if not infos:
            return "none"
        try:
            with socket.create_connection((infos[0][4][0], 443), 5):
                return "ok"
        except OSError:
            return "refused"


def direct_here(host):
    """Is this node the machine the world reaches under `host`? True when
    the name's IPv4 address is one of this machine's own (oaapx01). Behind
    a router or an edge it is not -- then IPv4 only, and IPv6 is not
    judged from here: inside the LAN the AAAA may lead to the router
    itself (stage 0, M2)."""
    try:
        ip = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (OSError, IndexError):
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))         # only an address of this machine can be bound
        return True
    except OSError:
        return False
    finally:
        s.close()


def tls_verdict(err):
    """An alert the server sent ("tlsv1 alert protocol version", "...
    handshake failure") is a real refusal; an error raised before
    anything was sent ("no protocols available", "unsupported protocol")
    proves nothing (F4)."""
    text = f"{getattr(err, 'reason', '') or ''} {err}".lower()
    if "alert" in text or "handshake_failure" in text or "wrong_version" in text \
            or "wrong version" in text:
        return "refused"
    return "unknown"


def _short_err(e):
    return f"{type(e).__name__}: {e}"[:200]


# --- reading a page ---------------------------------------------------

class Page(html.parser.HTMLParser):
    """What the rules need from a start page, nothing of its text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.in_title = "", False
        self.meta, self.links, self.scripts, self.styles = {}, [], [], []
        self.imgs, self.iframes = [], []
        self.lang, self.canonical, self.ld, self.picture = None, None, 0, 0
        self.anchors, self._a = [], None      # [(href, text)] -- text only to match words

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k and k not in self.meta:
                self.meta[k] = a.get("content", "")
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            href = a.get("href", "")
            if "canonical" in rel and self.canonical is None:
                self.canonical = href
            if "stylesheet" in rel or ("preload" in rel
                                       and a.get("as") in ("style", "script", "font", "image")):
                self.styles.append(href)
            elif "icon" in rel or "apple-touch-icon" in rel:
                self.links.append(href)
        elif tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            if (a.get("type") or "").lower() == "application/ld+json":
                self.ld += 1
        elif tag == "picture":
            self.picture += 1
        elif tag == "img":
            self.imgs.append((a.get("src") or "", self.picture > 0))
        elif tag == "iframe":
            self.iframes.append(a.get("src", ""))
        elif tag == "a" and a.get("href"):
            if self._a is not None:
                self.anchors.append(tuple(self._a))
            self._a = [a["href"], (a.get("title") or a.get("aria-label") or "")]

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "picture":
            self.picture = max(0, self.picture - 1)
        elif tag == "a" and self._a is not None:
            self.anchors.append((self._a[0], self._a[1][:200]))
            self._a = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self._a is not None and len(self._a[1]) < 200:
            self._a[1] += " " + data

    def close(self):
        super().close()
        if self._a is not None:
            self.anchors.append((self._a[0], self._a[1][:200]))
            self._a = None


def _site(host):
    """The name a host belongs to, roughly: its last two labels
    (www.verein.de and cdn.verein.de are the site's own; fonts.googleapis.com
    is not). Good enough for a hint."""
    parts = (host or "").lower().rstrip(".").split(".")
    return ".".join(parts[-2:])


def _matches(words, href, text):
    h, t = (href or "").lower(), " ".join((text or "").lower().split())
    return any(w in h or w in t for w in words)


def tech_cookie(name):
    """Does a cookie's name look technical (session, CSRF, language,
    consent), so it needs no consent? A hint, not a verdict."""
    return bool(TECH_COOKIE.search(name or ""))


def _host_of(base, ref):
    try:
        return (urllib.parse.urlsplit(urllib.parse.urljoin(base, ref)).hostname or "").lower()
    except ValueError:
        return ""


_HASHED = re.compile(r"[.\-_~]([A-Za-z0-9_\-]{8,})\.(?:m?js|css|woff2?|ttf|png|jpe?g|gif|svg|webp|avif)$")


def hashed_name(path):
    """A file name with a build hash in it (app.3f9a1c2e.js): safe to
    cache for a long time, so it should say so."""
    m = _HASHED.search(path or "")
    return bool(m and re.search(r"\d", m.group(1)) and re.search(r"[A-Za-z]", m.group(1)))


def _cached(h):
    cc = (h.get("cache-control") or "").lower()
    if "no-store" in cc or "no-cache" in cc:
        return False
    return "max-age" in cc or "immutable" in cc or bool(h.get("expires"))


def _text_type(ct):
    return any(t in (ct or "") for t in ("javascript", "css", "svg", "json", "html", "text/plain",
                                         "xml"))


def collect(canon, aliases=(), ua=None, edge=False, probe=None):
    """Read the site (§2) -> observations. `edge`: the node sits behind an
    edge, so IPv4 only and IPv6 is not judged from here (M2)."""
    canon = (canon or "").lower()
    p = probe or Probe(ua or user_agent(), ipv4_only=edge)
    t0 = time.time()
    base = f"https://{canon}/"
    obs = {"canon": canon, "edge": bool(edge)}
    # the start page, redirects followed on the same host only
    url, hops, r = base, 0, None
    while True:
        r = p.get(url)
        loc = r["h"].get("location")
        if r["status"] in (301, 302, 303, 307, 308) and loc and hops < MAX_REDIRECTS:
            nxt = urllib.parse.urljoin(url, loc)
            nhost = _host_of(url, loc)
            if nhost != canon:
                obs["offsite"] = nhost
                break
            url, hops = nxt, hops + 1
            continue
        break
    ttfb = r["ttfb"]
    if r["status"] and ttfb > 1:
        # Measured on oaap-test: the very first request to a new name took
        # 8.2 s (the gateway was still getting its certificate), the next
        # 0.03 s. One more look before calling it slow -- the better wins.
        again = p.get(url, limit=0, method="HEAD")
        if again["status"]:
            obs["ttfb_first"] = round(ttfb, 3)
            ttfb = min(ttfb, again["ttfb"])
    obs.update({"final": urllib.parse.urlsplit(url).path or "/", "hops": hops,
                "status": r["status"], "err": r.get("err", ""),
                "ttfb": round(ttfb, 3)})
    if r["status"] == 0:
        obs.update({"requests": p.requests, "seconds": round(time.time() - t0, 1),
                    "kb": round(p.bytes / 1024)})
        return obs
    h = r["h"]
    keep = ("strict-transport-security", "x-content-type-options", "referrer-policy",
            "content-security-policy", "x-frame-options", "x-powered-by", "server",
            "x-robots-tag", "content-encoding", "cache-control", "content-type")
    obs["h"] = {k: h[k][:300] for k in keep if k in h}
    ctype = (h.get("content-type") or "").lower()
    # an error page is not the start page: its missing title or canonical
    # says nothing (wegweiser on oaap-test answers / with a 404)
    obs["html"] = "html" in ctype and not obs.get("offsite") and r["status"] < 400
    page = Page()
    if obs["html"]:
        try:
            page.feed(r["body"].decode("utf-8", "replace"))
            page.close()
        except Exception:                           # noqa: BLE001  (a broken page is still a page)
            pass
        title = " ".join(page.title.split())
        desc = " ".join((page.meta.get("description") or "").split())
        obs["page"] = {
            "title_len": len(title), "desc_len": len(desc),
            "robots": (page.meta.get("robots") or "")[:100],
            "canonical": page.canonical is not None,
            "canonical_host": _host_of(url, page.canonical) if page.canonical else "",
            "og": {k: ("" if not page.meta.get(k) else
                       ("abs:" + _host_of(url, page.meta[k]))
                       if page.meta[k].startswith("https://") else "rel")
                   for k in ("og:image", "og:url")},
            "ld": page.ld, "lang": bool(page.lang),
        }
    # Sicherheit
    days, ver, cerr = p.cert(canon)
    obs["cert"] = {"days": days, "tls": ver, "err": cerr}
    obs["old_tls"] = p.old_tls(canon)
    hr = p.get(f"http://{canon}/", limit=0, method="HEAD")
    obs["http"] = {"status": hr["status"], "to_https":
                   (hr["h"].get("location") or "").startswith("https://")}
    obs["ipv6"] = "skipped" if edge else p.ipv6(canon)
    # Auffindbarkeit
    rb = p.get(base + "robots.txt", limit=MAX_SMALL)
    robots = {"status": rb["status"], "block_all": False, "sitemap": ""}
    if rb["status"] == 200:
        agents, group_open, sitemap = [], False, ""
        for ln in rb["body"].decode("utf-8", "replace").splitlines():
            k, _, v = ln.split("#", 1)[0].partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                if not group_open:
                    agents = []
                agents.append(v)
                group_open = True
                continue
            group_open = False
            if k == "disallow" and "*" in agents and v == "/":
                robots["block_all"] = True
            elif k == "sitemap" and not sitemap:
                sitemap = v
        robots["sitemap"] = sitemap
    obs["robots"] = robots
    sm_url = robots["sitemap"] or base + "sitemap.xml"
    sm_host = _host_of(base, sm_url)
    sitemap = {"named": bool(robots["sitemap"]), "host": sm_host if sm_host != canon else ""}
    sm_locs = []
    if sm_host == canon:
        sm = p.get(sm_url, limit=MAX_SMALL)
        sitemap["status"] = sm["status"]
        if sm["status"] == 200:
            s = sm["body"].decode("utf-8", "replace")
            locs = [x.split("</loc>")[0].strip() for x in s.split("<loc>")[1:]]
            sm_locs = locs
            sitemap["count"] = len(locs)
            sitemap["hosts"] = sorted({(urllib.parse.urlsplit(x).hostname or "").lower()
                                       for x in locs} - {"", canon})[:5]
            sitemap["http"] = any(x.startswith("http://") for x in locs)
    obs["sitemap"] = sitemap
    obs["aliases"] = []
    for a in list(aliases)[:10]:
        ar = p.get(f"https://{a}/", limit=0, method="HEAD")
        obs["aliases"].append({"host": a, "status": ar["status"]})
    # Tempo
    total = r["raw"]
    plain, notransform = [], "no-transform" in (h.get("cache-control") or "").lower()
    if obs["html"] and r["raw"] > 1024 and not h.get("content-encoding"):
        plain.append(f"/ ({r['raw'] // 1024} KB)")
    named, seen = [], set()
    for src in page.scripts + page.styles + page.links + [s for s, _ in page.imgs]:
        if not src or src.startswith("data:"):
            continue
        u = urllib.parse.urljoin(url, src)
        if _host_of(base, u) == canon and u not in seen:
            seen.add(u)
            named.append(u)
    big, uncached, hashed = [], 0, 0
    for u in named[:MAX_ASSETS]:
        ar = p.get(u)
        total += ar["raw"]
        ct = (ar["h"].get("content-type") or "").lower()
        path = urllib.parse.urlsplit(u).path
        short = path.rsplit("/", 1)[-1][:60] or path[:60]
        if _text_type(ct) and ar["raw"] > 1024 and not ar["h"].get("content-encoding"):
            plain.append(f"{short} ({ar['raw'] // 1024} KB)")
            notransform = notransform or "no-transform" in (ar["h"].get("cache-control") or "").lower()
        if ct.startswith("image/") and ar["raw"] > 500 * 1024:
            big.append(f"{short} ({ar['raw'] // 1024} KB)")
        if hashed_name(path):
            hashed += 1
            if ar["status"] == 200 and not _cached(ar["h"]):
                uncached += 1
    modern = [s.rsplit("/", 1)[-1][:60] for s, in_pic in page.imgs
              if s.lower().split("?")[0].endswith((".avif", ".webp")) and not in_pic]
    obs["assets"] = {"named": len(named), "fetched": min(len(named), MAX_ASSETS),
                     "kb": round(total / 1024), "plain": plain[:8],
                     "notransform": notransform, "big": big[:8], "modern": modern[:8],
                     "hashed": hashed, "uncached": uncached}
    # Recht (§3.4, stage 2): only what the start page itself says and does
    if obs["html"]:
        own = _site(canon)
        foreign = set()
        for src in (page.scripts + page.styles + page.links + [s for s, _ in page.imgs]
                    + page.iframes):
            if not src or src.startswith("data:"):
                continue
            hst = _host_of(url, src)
            if hst and _site(hst) != own:
                foreign.add(hst)
        text = r["body"][:MAX_BYTES].decode("utf-8", "replace").lower()
        gfonts = any(g in text for g in GOOGLE_FONTS)
        if gfonts:
            foreign.update(g for g in GOOGLE_FONTS if g in text)
        obs["legal"] = {
            "imprint": any(_matches(IMPRINT_WORDS, hr, tx) for hr, tx in page.anchors),
            "privacy": any(_matches(PRIVACY_WORDS, hr, tx) for hr, tx in page.anchors),
            "foreign": sorted(foreign)[:12], "gfonts": gfonts,
            "iframes": sorted({_host_of(url, f) for f in page.iframes if f
                               and _site(_host_of(url, f)) != own} - {""})[:6],
        }
    obs["cookies"] = [c[:60] for c in (r.get("cookies") or [])][:12]
    # Stage 3: up to ten more pages on the same name -- the sitemap's first,
    # then the start page's own links. A redirect is an answer, not followed.
    if obs["html"]:
        start = urllib.parse.urlsplit(url)._replace(fragment="").geturl()
        cands, seen_p = [], {start, base}
        for src, origin in ([(x, "sitemap") for x in sm_locs]
                            + [(hr, "link") for hr, _t in page.anchors]):
            if not src or src.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            u = urllib.parse.urljoin(url, src)
            sp = urllib.parse.urlsplit(u)
            if sp.scheme not in ("http", "https") or (sp.hostname or "").lower() != canon:
                continue
            u = sp._replace(fragment="", scheme="https").geturl()
            if u in seen_p:
                continue
            seen_p.add(u)
            cands.append((u, origin))
        pages = []
        for u, origin in cands[:MAX_PAGES]:
            pr = p.get(u)
            ent = {"path": (urllib.parse.urlsplit(u).path or "/")[:80], "from": origin,
                   "status": pr["status"]}
            pct = (pr["h"].get("content-type") or "").lower()
            if pr["status"] == 200 and "html" in pct:
                pp = Page()
                try:
                    pp.feed(pr["body"].decode("utf-8", "replace"))
                    pp.close()
                except Exception:                   # noqa: BLE001
                    pass
                ent["html"] = True
                ent["title"] = bool(" ".join(pp.title.split()))
                ent["noindex"] = "noindex" in ((pp.meta.get("robots") or "") + " "
                                               + (pr["h"].get("x-robots-tag") or "")).lower()
            pages.append(ent)
        obs["pages"] = {"checked": pages, "more": max(0, len(cands) - MAX_PAGES)}
    obs.update({"requests": p.requests, "seconds": round(time.time() - t0, 1),
                "kb": round(p.bytes / 1024)})
    return obs


# --- the rules (§3.1-3.3) ---------------------------------------------

def _f(fid, level, area, fix, found, why, todo, link=""):
    return {"id": fid, "level": level, "area": area, "fix": fix, "found": found,
            "why": why, "todo": todo, "link": link}


def evaluate(obs, ctx=None):
    """observations + what the platform knows -> findings. ctx:
    compress, headers (RFC-0140 switches), alias_mode, google_absent,
    instance (the name, for the CLI hints)."""
    ctx = ctx or {}
    inst = ctx.get("instance") or "<instanz>"
    out = []
    add = out.append
    if obs.get("status", 0) == 0:
        return out
    h = obs.get("h") or {}
    st = obs.get("status")
    if obs.get("offsite"):
        add(_f("start.offsite", "yellow", "Auffindbarkeit", "seite",
               f"Die Startseite leitet auf einen anderen Namen um ({obs['offsite']}).",
               "Suchmaschinen und Besucher landen woanders; die Seite selbst wurde nicht geprüft.",
               "Wenn die Seite unter diesem Namen gefunden werden soll: die Startseite "
               "hier ausliefern, nicht umleiten."))
    elif st >= 400:
        add(_f("start.status", "red" if st >= 500 else "yellow", "Auffindbarkeit", "seite",
               f"Die Startseite antwortet mit {st}.",
               "Wer die Adresse aufruft, sieht keine Seite.",
               "Die App prüfen (Reiter Diagnose)."))
    # 3.1 Sicherheit
    cert = obs.get("cert") or {}
    if cert.get("err"):
        add(_f("cert.invalid", "red", "Sicherheit", "plattform",
               "Das Zertifikat ist ungültig oder nicht prüfbar.",
               "Browser zeigen eine Warnung statt der Seite.",
               "Der Knoten erneuert Zertifikate selbst -- zeigt der Name noch auf diesen "
               "Knoten? (Reiter Netz & Adressen)", "netz"))
    elif cert.get("days") is not None and cert["days"] < 30:
        add(_f("cert.expiry", "red" if cert["days"] < 14 else "yellow", "Sicherheit",
               "plattform", f"Das Zertifikat läuft in {cert['days']} Tagen ab.",
               "Danach zeigen Browser eine Warnung statt der Seite.",
               "Der Knoten erneuert es normalerweise 30 Tage vorher -- bleibt das aus, "
               "erreicht die Zertifizierungsstelle den Namen nicht mehr.", "netz"))
    if obs.get("old_tls") == "accepted":
        add(_f("tls.old", "red", "Sicherheit", "plattform",
               "Die Seite nimmt TLS 1.0/1.1 an.",
               "Diese alten Verfahren gelten als unsicher.",
               "Sache des Gateways oder des Edge-Knotens -- bitte dem Betreiber melden."))
    http_ = obs.get("http") or {}
    if http_.get("status") == 200:
        add(_f("http.serves", "red", "Sicherheit", "plattform",
               "http:// liefert die Seite aus, statt auf https:// umzuleiten.",
               "Ohne Verschlüsselung kann unterwegs mitgelesen und verändert werden.",
               "Sache des Gateways -- bitte dem Betreiber melden."))
    if st:                      # headers belong to every answer, an error page too
        missing = [n for k, n in (("strict-transport-security", "HSTS"),
                                  ("x-content-type-options", "nosniff"),
                                  ("referrer-policy", "Referrer-Policy")) if k not in h]
        if missing:
            off = ctx.get("headers") is False
            add(_f("hdr.base", "yellow", "Sicherheit", "plattform" if off else "seite",
                   "Es fehlt: " + ", ".join(missing) + ".",
                   "Diese Angaben schützen Besucher vor untergeschobenen Inhalten und "
                   "verraten weniger an fremde Seiten.",
                   f"Die Grund-Header sind für diese Instanz ausgeschaltet: "
                   f"`oaap app hardening {inst} --headers on` (Server-Admin)." if off else
                   "Die App sendet eigene, schwächere Werte -- sie zu streichen lässt die "
                   "Plattform ihre setzen."))
        csp = h.get("content-security-policy") or ""
        if not csp:
            add(_f("hdr.csp", "yellow", "Sicherheit", "seite",
                   "Keine Content-Security-Policy.",
                   "Sie begrenzt, was ein eingeschleustes Skript anrichten kann.",
                   "Die Seite legt fest, woher sie Skripte, Styles und Bilder lädt."))
        if "frame-ancestors" not in csp.lower() and "x-frame-options" not in h:
            add(_f("hdr.frame", "yellow", "Sicherheit", "seite",
                   "Kein Schutz gegen Einbetten (frame-ancestors / X-Frame-Options).",
                   "Eine fremde Seite könnte diese unsichtbar einbetten und Klicks abfangen.",
                   "Wenn die Seite nirgends eingebettet werden soll: "
                   "`frame-ancestors 'self'` in der CSP."))
        announced = []
        if h.get("x-powered-by"):
            announced.append("X-Powered-By: " + h["x-powered-by"][:80])
        if re.search(r"\d", h.get("server") or ""):
            announced.append("Server: " + h["server"][:80])
        if announced:
            add(_f("hdr.version", "yellow", "Sicherheit", "seite",
                   "Die Antwort nennt Software und Version (" + "; ".join(announced) + ").",
                   "Kein Fehler -- erleichtert nur das Suchen nach bekannten Lücken.",
                   "Die App kann die Angabe weglassen."))
    # 3.2 Auffindbarkeit
    pg = obs.get("page") or {}
    robots_meta = (pg.get("robots") or "") + " " + (h.get("x-robots-tag") or "")
    if "noindex" in robots_meta.lower():
        add(_f("seo.noindex", "red", "Auffindbarkeit", "seite",
               "Die Startseite sagt Suchmaschinen „noindex“.",
               "Google nimmt sie nicht auf -- die Seite wird nicht gefunden.",
               "Gewollt? Sonst die Angabe in der Seite streichen; die Plattform sendet auf "
               "einer Produktivinstanz keine."))
    rb = obs.get("robots") or {}
    if rb.get("status") != 200:
        add(_f("seo.robots", "yellow", "Auffindbarkeit", "seite",
               "Keine robots.txt.",
               "Suchmaschinen erfahren nicht, wo die Sitemap liegt.",
               "Eine robots.txt mit einer Zeile `Sitemap: https://…/sitemap.xml`."))
    elif rb.get("block_all"):
        add(_f("seo.robots_block", "red", "Auffindbarkeit", "seite",
               "Die robots.txt sperrt die ganze Seite für alle Suchmaschinen.",
               "Die Seite wird nicht gefunden.",
               "`Disallow: /` unter `User-agent: *` streichen."))
    sm = obs.get("sitemap") or {}
    if sm.get("host"):
        add(_f("seo.sitemap_host", "yellow", "Auffindbarkeit", "seite",
               f"Die robots.txt nennt eine Sitemap auf {sm['host']}.",
               "Suchmaschinen erwarten die Sitemap auf demselben Namen wie die Seite.",
               "Die Sitemap unter dem Hauptnamen anbieten."))
    elif sm.get("status") != 200:
        add(_f("seo.sitemap", "yellow", "Auffindbarkeit", "seite",
               "Keine Sitemap.",
               "Eine Sitemap sagt Suchmaschinen, welche Seiten es gibt.",
               "Eine sitemap.xml mit allen Seiten, in der robots.txt genannt."))
    else:
        if sm.get("hosts"):
            add(_f("seo.sitemap_foreign", "red", "Auffindbarkeit", "seite",
                   "Die Sitemap nennt Seiten auf anderen Namen: " + ", ".join(sm["hosts"]) + ".",
                   "Suchmaschinen verwerfen solche Einträge oder ordnen sie dem falschen Namen zu.",
                   "In der Sitemap nur Adressen unter dem Hauptnamen."))
        if sm.get("http"):
            add(_f("seo.sitemap_http", "yellow", "Auffindbarkeit", "seite",
                   "Die Sitemap listet http://-Adressen.",
                   "Jede führt erst über eine Umleitung.",
                   "In der Sitemap https:// schreiben."))
    if obs.get("html"):
        if not pg.get("canonical"):
            add(_f("seo.canonical", "yellow", "Auffindbarkeit", "seite",
                   "Kein Canonical-Link.",
                   "Er sagt Suchmaschinen, welche Adresse die richtige ist, wenn die Seite "
                   "unter mehreren erreichbar ist.",
                   '`<link rel="canonical" href="https://…/">` im Kopf der Seite.'))
        elif pg.get("canonical_host") and pg["canonical_host"] != obs.get("canon"):
            add(_f("seo.canonical_host", "red", "Auffindbarkeit", "seite",
                   f"Der Canonical-Link zeigt auf {pg['canonical_host']}.",
                   "Suchmaschinen werten dann den anderen Namen, nicht diesen.",
                   "Den Canonical-Link auf den Hauptnamen setzen."))
        if not pg.get("title_len"):
            add(_f("seo.title", "red", "Auffindbarkeit", "seite",
                   "Die Startseite hat keinen Titel.",
                   "Im Suchergebnis und im Browser-Reiter steht dann nur die Adresse.",
                   "Ein `<title>` von bis zu 70 Zeichen."))
        elif pg["title_len"] > 70:
            add(_f("seo.title_long", "yellow", "Auffindbarkeit", "seite",
                   f"Der Titel ist {pg['title_len']} Zeichen lang.",
                   "Im Suchergebnis wird er nach etwa 70 Zeichen abgeschnitten.",
                   "Kürzen."))
        if not pg.get("desc_len"):
            add(_f("seo.desc", "yellow", "Auffindbarkeit", "seite",
                   "Keine Beschreibung (meta description).",
                   "Suchmaschinen zeigen sonst einen zufälligen Ausschnitt.",
                   "Ein Satz von bis zu 160 Zeichen."))
        elif pg["desc_len"] > 160:
            add(_f("seo.desc_long", "yellow", "Auffindbarkeit", "seite",
                   f"Die Beschreibung ist {pg['desc_len']} Zeichen lang.",
                   "Sie wird nach etwa 160 Zeichen abgeschnitten.", "Kürzen."))
        for key in ("og:image", "og:url"):
            v = (pg.get("og") or {}).get(key, "")
            if not v:
                add(_f("seo." + key.replace(":", "_"), "yellow", "Auffindbarkeit", "seite",
                       f"{key} fehlt.",
                       "Beim Teilen in Messengern und sozialen Netzen fehlt Bild oder Adresse.",
                       f'`<meta property="{key}" content="https://…">`.'))
            elif v == "rel" or v[4:] != obs.get("canon"):
                add(_f("seo." + key.replace(":", "_") + "_host", "yellow", "Auffindbarkeit",
                       "seite", f"{key} ist relativ oder nennt einen anderen Namen.",
                       "Messenger brauchen eine volle https://-Adresse unter dem Hauptnamen.",
                       "Die volle Adresse eintragen."))
        if not pg.get("ld"):
            add(_f("seo.ld", "yellow", "Auffindbarkeit", "seite",
                   "Keine strukturierten Daten (application/ld+json).",
                   "Damit zeigt Google Name, Logo, Adresse oder Termine direkt im Ergebnis.",
                   "Ein kleiner JSON-LD-Block, z. B. vom Typ Organization."))
        if not pg.get("lang"):
            add(_f("seo.lang", "yellow", "Auffindbarkeit", "seite",
                   "Die Seite sagt nicht, in welcher Sprache sie ist (`<html lang>`).",
                   "Vorleseprogramme und Suchmaschinen raten sonst.", '`<html lang="de">`.'))
    serving = [a["host"] for a in obs.get("aliases") or [] if a.get("status") == 200]
    if serving:
        add(_f("seo.alias", "yellow", "Auffindbarkeit", "plattform",
               "Weitere Namen liefern dieselbe Seite aus: " + ", ".join(serving) + ".",
               "Suchmaschinen sehen doppelte Inhalte und teilen die Wertung auf.",
               "Reiter Netz & Adressen: „Aliasse auf den Hauptnamen umleiten“.", "netz"))
    if obs.get("ipv6") == "refused":
        add(_f("dns.ipv6", "yellow", "Auffindbarkeit", "seite",
               "Der IPv6-Eintrag des Namens zeigt ins Leere.",
               "Browser versuchen IPv6 zuerst und weichen erst nach einer Verzögerung aus.",
               "Den AAAA-Eintrag beim DNS-Anbieter löschen oder richtig setzen."))
    if ctx.get("google_absent"):
        add(_f("seo.google", "yellow", "Auffindbarkeit", "",
               "Google war länger nicht da (Hinweise zur Webseite).",
               "Die Seite taucht dann nicht oder veraltet in der Suche auf.",
               "Sitemap und robots.txt prüfen; die Seite in der Google Search Console anmelden."))
    # 3.3 Tempo
    ttfb = obs.get("ttfb") or 0
    if st and st < 400 and ttfb > 1:
        add(_f("speed.ttfb", "red" if ttfb > 3 else "yellow", "Tempo", "seite",
               f"Das erste Byte der Startseite kam nach {ttfb:.1f} s.",
               "Besucher warten, bevor überhaupt etwas erscheint.",
               "Die App prüfen (Reiter Diagnose: Last, Speicher)."))
    a = obs.get("assets") or {}
    mb = (a.get("kb") or 0) / 1024
    if mb > 2:
        add(_f("speed.weight", "red" if mb > 5 else "yellow", "Tempo", "seite",
               f"Die Startseite wiegt {mb:.1f} MB (übertragen"
               + (f", {a['fetched']} von {a['named']} Dateien gelesen" if a.get("named", 0)
                  > a.get("fetched", 0) else "") + ").",
               "Auf dem Handy unterwegs lädt das spürbar lange.",
               "Bilder verkleinern, Ungenutztes weglassen."))
    if a.get("plain"):
        if a.get("notransform"):
            fix, todo = "seite", ("Die App verbietet Umformung (Cache-Control: no-transform) -- "
                                  "die Plattform komprimiert dann nicht. Die App kann selbst "
                                  "komprimieren oder no-transform weglassen.")
        elif ctx.get("compress") is False:
            fix, todo = "plattform", (f"Die Kompression ist für diese Instanz ausgeschaltet: "
                                      f"`oaap app hardening {inst} --compress on` (Server-Admin).")
        else:
            fix, todo = "seite", "Die App liefert diese Dateien so aus, dass das Gateway sie nicht komprimiert."
        add(_f("speed.plain", "yellow", "Tempo", fix,
               "Unkomprimiert: " + ", ".join(a["plain"][:5]) + ".",
               "Text lässt sich auf ein Viertel packen -- schneller da, weniger Datenvolumen.",
               todo))
    if a.get("big"):
        add(_f("speed.images", "yellow", "Tempo", "seite",
               "Große Bilder: " + ", ".join(a["big"][:5]) + ".",
               "Ein Bild über 500 KB bremst die Seite auf dem Handy.",
               "Auf die angezeigte Größe verkleinern und komprimieren."))
    if a.get("modern"):
        add(_f("speed.formats", "yellow", "Tempo", "seite",
               "AVIF/WebP ohne Ersatz im `<picture>`: " + ", ".join(a["modern"][:3]) + ".",
               "Ältere Safari-Versionen zeigen dann gar nichts.",
               "In ein `<picture>` mit einem JPEG/PNG als `<img>` setzen."))
    if a.get("uncached"):
        add(_f("speed.cache", "yellow", "Tempo", "seite",
               f"{a['uncached']} von {a['hashed']} Dateien mit Versionsnummer im Namen "
               "haben keine Cache-Angabe.",
               "Der Browser lädt sie bei jedem Besuch neu, obwohl sie sich nie ändern.",
               "`Cache-Control: public, max-age=31536000, immutable` für diese Dateien."))
    # 3.2 continued, stage 3: the pages beside the start page
    pc = (obs.get("pages") or {}).get("checked") or []
    broken = [x for x in pc if x.get("status") == 0 or x.get("status", 0) >= 400]
    if broken:
        words = {"sitemap": "Sitemap", "link": "Link der Startseite"}
        add(_f("pages.broken", "red" if any(x.get("status", 0) >= 500 for x in broken)
               else "yellow", "Auffindbarkeit", "seite",
               "Verweise ins Leere: " + ", ".join(
                   f"{x['path']} ({x['status'] or 'keine Antwort'}, {words.get(x['from'], x['from'])})"
                   for x in broken[:5]) + ".",
               "Besucher landen auf einer Fehlerseite; Suchmaschinen werten das ab.",
               "Den Link korrigieren oder die Seite wiederherstellen; aus der Sitemap "
               "streichen, was es nicht mehr gibt."))
    untitled = [x["path"] for x in pc if x.get("html") and not x.get("title")]
    if untitled:
        add(_f("pages.title", "yellow", "Auffindbarkeit", "seite",
               "Unterseiten ohne Titel: " + ", ".join(untitled[:5]) + ".",
               "Im Suchergebnis steht dann nur die Adresse.",
               "Jeder Seite einen eigenen `<title>` geben."))
    hidden = [x["path"] for x in pc if x.get("noindex") and x.get("from") == "sitemap"]
    if hidden:
        add(_f("pages.noindex", "yellow", "Auffindbarkeit", "seite",
               "Die Sitemap nennt Seiten, die „noindex“ sagen: " + ", ".join(hidden[:5]) + ".",
               "Die Sitemap bittet um Aufnahme, die Seite lehnt sie ab -- Suchmaschinen "
               "werten das als Fehler.",
               "Die Seiten aus der Sitemap nehmen oder „noindex“ streichen."))
    # 3.4 Recht -- hints, not legal advice (stage 2)
    lg = obs.get("legal")
    if lg is not None:
        if not lg.get("imprint"):
            add(_f("law.imprint", "yellow", "Recht", "seite",
                   "Auf der Startseite kein Link zum Impressum.",
                   "Geschäftsmäßige Seiten in Deutschland brauchen ein leicht erreichbares "
                   "Impressum -- Vereine meist auch.",
                   "Einen Link „Impressum“ auf jeder Seite, üblich im Fuß."))
        if not lg.get("privacy"):
            add(_f("law.privacy", "yellow", "Recht", "seite",
                   "Auf der Startseite kein Link zur Datenschutzerklärung.",
                   "Wer Daten von Besuchern verarbeitet -- schon das Protokoll des Servers --, "
                   "muss darüber informieren.",
                   "Einen Link „Datenschutz“ auf jeder Seite, üblich im Fuß."))
        if lg.get("foreign"):
            hosts = list(lg["foreign"])
            if lg.get("gfonts"):
                hosts = [h for h in GOOGLE_FONTS if h in hosts] + [h for h in hosts
                                                                    if h not in GOOGLE_FONTS]
            add(_f("law.foreign", "yellow", "Recht", "seite",
                   "Die Startseite lädt Inhalte von fremden Servern: " + ", ".join(hosts[:6])
                   + (f" und {len(hosts) - 6} weitere" if len(hosts) > 6 else "") + ".",
                   "Beim Laden erfährt der fremde Server die Adresse des Besuchers; ohne "
                   "Einwilligung ist das oft unzulässig"
                   + (" -- wegen Google Fonts gab es 2022 viele Abmahnungen." if lg.get("gfonts")
                      else "."),
                   "Schriften und Skripte selbst ausliefern; Karten und Videos erst nach einem "
                   "Klick laden -- oder vorher um Einwilligung bitten."))
    odd = [c for c in obs.get("cookies") or [] if not tech_cookie(c)]
    if odd and st and st < 400:
        add(_f("law.cookies", "yellow", "Recht", "seite",
               "Die erste Antwort setzt Cookies, die nicht nach Technik aussehen: "
               + ", ".join(odd[:6]) + ".",
               "Cookies, die nicht technisch nötig sind (Statistik, Werbung), brauchen vorher "
               "eine Einwilligung.",
               "Erst nach Zustimmung setzen -- oder weglassen."))
    return out


def lights(findings, grey=False):
    """{area: "red" | "yellow" | "green" | "grey"}."""
    if grey:
        return {a: "grey" for a in AREAS}
    rank = {"green": 0, "yellow": 1, "red": 2}
    out = {a: "green" for a in AREAS}
    for f in findings:
        a = f["area"]
        if a in out and rank[f["level"]] > rank[out[a]]:
            out[a] = f["level"]
    return out


def diff(prev, cur):
    """"neu" and "behoben" against the previous run (§6): -> (ids new or
    risen to red, findings of the previous run that are gone). A grey run
    on either side compares nothing."""
    if not prev or not cur or prev.get("grey") or cur.get("grey"):
        return set(), []
    before = {f["id"]: f for f in prev.get("findings") or []}
    now_ = {f["id"]: f for f in cur.get("findings") or []}
    new = {i for i, f in now_.items()
           if i not in before or (f["level"] == "red" and before[i]["level"] != "red")}
    gone = [f for i, f in before.items() if i not in now_]
    return new, gone


def _judged(runs):
    """The runs that judged something -- a grey run (unreachable) says
    nothing about the site."""
    return [r for r in runs if not r.get("grey")]


def matter(runs, is_open=False):
    """§6, stage 2: the notification `site.check`. -> None or (kurz, text).
    Opens when the last judged run has a red point that was not red in
    the run before it; stays open while a run finds red; closes with the
    first run without red. Yellow never mails."""
    judged = _judged(runs or [])
    if not judged:
        return None
    last = judged[-1]
    reds = [f for f in last.get("findings") or [] if f["level"] == "red"]
    if not reds:
        return None
    if not is_open:
        prev = judged[-2] if len(judged) > 1 else None
        was = {f["id"] for f in (prev or {}).get("findings") or [] if f["level"] == "red"}
        if not [f for f in reds if f["id"] not in was]:
            return None
    text = ("Der Seiten-Check vom " + str(last.get("at", "")) + " UTC fand auf "
            + str(last.get("host", "")) + " "
            + ("einen roten Punkt" if len(reds) == 1 else f"{len(reds)} rote Punkte")
            + " -- das schadet jetzt:\n"
            + "\n".join(f"  - {f['area']}: {f['found'].replace('`', '')}" for f in reds)
            + "\nWas zu tun ist und wer es behebt, steht im Reiter Seitenstatistik.")
    return str(len(reds)), text


def result_of(obs, ctx=None, now=None, by="wochenlauf"):
    """observations -> the record kept (§2.3): findings, lights, numbers."""
    now = now or time.time()
    grey = ""
    if obs.get("status", 0) == 0:
        grey = ("Von diesem Knoten aus nicht erreichbar -- Ergebnis fehlt"
                + (f" ({obs['err']})" if obs.get("err") else "") + ".")
    findings = [] if grey else evaluate(obs, ctx)
    notes = []
    if obs.get("old_tls") == "unknown" and not grey:
        notes.append("TLS 1.0/1.1 von hier nicht prüfbar.")
    if obs.get("ipv6") == "skipped" and not grey:
        notes.append("IPv6 von hier nicht prüfbar (Knoten hinter einem Edge).")
    if obs.get("offsite"):
        notes.append(f"Startseite leitet auf {obs['offsite']} um.")
    pg3 = obs.get("pages") or {}
    if pg3.get("checked") and not grey:
        notes.append(f"{len(pg3['checked'])} weitere Seiten gelesen"
                     + (f", {pg3['more']} nicht (höchstens {MAX_PAGES})" if pg3.get("more") else "")
                     + ".")
    tech = [c for c in obs.get("cookies") or [] if tech_cookie(c)]
    if tech and not grey:
        notes.append("Technische Cookies (ohne Einwilligung erlaubt): " + ", ".join(tech[:6]) + ".")
    lt = lights(findings, bool(grey))
    # Recht reads the start page itself: no page (an error, a redirect
    # elsewhere) is "not checked", never green (wegweiser on oaap-test)
    if not grey and "legal" not in obs and lt.get("Recht") == "green":
        lt["Recht"] = "grey"
        notes.append("Recht nicht geprüft: Die Startseite liefert keine lesbare Seite.")
    return {"at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
                     .strftime("%Y-%m-%d %H:%M"),
            "ts": int(now), "by": by, "host": obs.get("canon", ""), "grey": grey,
            "findings": findings, "lights": lt, "notes": notes,
            "n": {"requests": obs.get("requests", 0), "seconds": obs.get("seconds", 0),
                  "kb": obs.get("kb", 0),
                  "cert_days": (obs.get("cert") or {}).get("days"),
                  "tls": (obs.get("cert") or {}).get("tls"),
                  "pages": len((obs.get("pages") or {}).get("checked") or [])}}


# --- the store ----------------------------------------------------------

def _path(instance_dir):
    return os.path.join(instance_dir, FILE)


def load(instance_dir):
    try:
        with open(_path(instance_dir), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("v") == FORMAT and isinstance(d.get("runs"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"v": FORMAT, "runs": []}


def record(instance_dir, result):
    os.makedirs(instance_dir, exist_ok=True)
    d = load(instance_dir)
    d["runs"] = (d["runs"] + [result])[-KEEP:]
    tmp = _path(instance_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, _path(instance_dir))
    return d


def last_ts(instance_dir):
    runs = load(instance_dir)["runs"]
    return runs[-1].get("ts", 0) if runs else 0


def _parts(text):
    """`code` in a finding's text -> [(text, is_code)] for the card; the
    CLI prints the backticks as they are."""
    return [(t, i % 2 == 1) for i, t in enumerate((text or "").split("`")) if t]


def _rows(findings, new=()):
    """F1: red always alone; three or more yellow points of one area and
    one fixer fold into one line. `new`: ids marked „neu“ (§6)."""
    findings = [dict(f, found_parts=_parts(f.get("found")), todo_parts=_parts(f.get("todo")),
                     new=f["id"] in new)
                for f in findings]
    rows = []
    for area in AREAS:
        mine = [f for f in findings if f["area"] == area]
        reds = [f for f in mine if f["level"] == "red"]
        yellows = [f for f in mine if f["level"] != "red"]
        rows += [{"group": False, "f": f} for f in reds]
        by_fix = {}
        for f in yellows:
            by_fix.setdefault(f["fix"], []).append(f)
        for fix, fs in by_fix.items():
            if len(fs) >= GROUP_MIN:
                title = GROUP_TITLES.get((area, fix)) or f"{area}: mehrere Punkte"
                rows.append({"group": True, "area": area, "fix": fix, "points": fs,
                             "title": f"{title}: {len(fs)} Punkte",
                             "new": sum(1 for f in fs if f["new"])})
            else:
                rows += [{"group": False, "f": f} for f in fs]
    return rows


def view(instance_dir, name=""):
    """What the portal's card shows: the last run, its lights and rows."""
    runs = load(instance_dir)["runs"] if instance_dir else []
    last = runs[-1] if runs else None
    out = {"last": last, "count": len(runs),
           "weekday": WEEKDAYS[weekday_of(name)] if name else "",
           "button_wait": 0}
    if last:
        prev = runs[-2] if len(runs) > 1 else None
        new, gone = diff(prev, last)
        out["rows"] = _rows(last.get("findings") or [], new)
        out["red"] = sum(1 for f in last.get("findings") or [] if f["level"] == "red")
        out["yellow"] = sum(1 for f in last.get("findings") or [] if f["level"] != "red")
        out["new"] = len(new)
        out["fixed"] = [{"found_parts": _parts(f.get("found")), "area": f["area"],
                         "level": f["level"]} for f in gone]
        out["compared"] = bool(prev) and not prev.get("grey") and not last.get("grey")
        # a run of stage 1 knows no Recht: "" = not checked in that run
        out["lights"] = {a: (last.get("lights") or {}).get(a, "") for a in AREAS}
        wait = BUTTON_EVERY - (time.time() - (last.get("ts") or 0))
        out["button_wait"] = max(0, int(wait // 60) + 1) if wait > 0 else 0
        out["history"] = history(runs)
    return out


def history(runs, height=40):
    """Stage 3: the kept runs as bars -- red below, yellow above, a grey
    run as a short grey bar. Heights in pixels for the card's SVG."""
    counts = []
    for r in runs:
        fs = r.get("findings") or []
        counts.append((r.get("at", ""), sum(1 for f in fs if f["level"] == "red"),
                       sum(1 for f in fs if f["level"] != "red"), bool(r.get("grey"))))
    top = max([red + yel for _a, red, yel, _g in counts] + [1])
    out = []
    for i, (at, red, yel, grey) in enumerate(counts):
        hr = round(red * height / top)
        hy = round(yel * height / top)
        out.append({"at": at, "red": red, "yellow": yel, "grey": grey, "x": i * 14,
                    "hr": hr, "hy": hy, "yr": height - hr, "yy": height - hr - hy})
    return out
