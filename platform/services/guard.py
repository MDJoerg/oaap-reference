"""Secret files: the gateway refuses them, the node looks for them (RFC-0084).

Two layers, one list:

  1. The gateway answers a request for a well-known secret path with 404
     before the app is reached -- whether the file exists or not. The app
     never sees the request, so it cannot hand the file out by mistake.
  2. Once a day the node asks every instance directly (past the gateway)
     for the same paths. Real content is a finding: the file is in the
     wrong place, and the next gateway, proxy or copy of the app would
     serve it.

Pure: patterns, the probe list, the judgement. The acting (site lines,
scan, notifications) is in appctl.py, the counting in identity.

A finding needs CONTENT that looks like the file -- a key=value line for
.env, "[core]" for .git/config, "PRIVATE KEY" for a key. Many apps answer
every path with their start page (a single-page app's fallback); that is a
200 with HTML and never a finding.
"""
import hashlib
import re

# category -> (path regex fragment, German words). The fragment matches the
# END of the path (after any prefix), case-insensitively.
CATEGORIES = {
    "env": (r"\.env(\.[^/]*)?", "Umgebungsdateien (.env, .env.local …)"),
    "vcs": (r"\.(git|svn|hg)(/.*)?", "Versionsverwaltung (.git/, .svn/, .hg/)"),
    "keys": (r"(id_rsa|id_ecdsa|id_ed25519|id_dsa)(\.pub)?|\.ssh/.*|[^/]*\.(pem|key|p12|pfx)",
             "Schlüssel und Zertifikate (id_rsa, *.pem, *.key …)"),
    "cloud": (r"\.aws/.*|\.npmrc|\.pypirc|\.netrc|\.docker/config\.json|\.git-credentials"
              r"|\.htpasswd|\.htaccess",
              "Zugangsdateien (.aws/, .npmrc, .htpasswd …)"),
    "deploy": (r"(vercel|now|netlify)\.json|serverless\.(json|ya?ml)"
               r"|docker-compose(\.[^/]*)?\.ya?ml|\.travis\.yml|\.gitlab-ci\.yml",
               "Bau- und Deploy-Konfiguration (vercel.json, docker-compose.yml …)"),
    "php": (r"wp-config\.php[^/]*|config\.php\.(bak|old|orig|save|swp)|[^/]*\.php~",
            "PHP-Konfiguration und Sicherungskopien (wp-config.php …)"),
    "dumps": (r"[^/]*\.(sql|sqlite3?|db|dump|bak)(\.(gz|zip|bz2|xz))?",
              "Datenbank-Abzüge und Sicherungen (*.sql, *.sqlite, *.bak …)"),
}
CATEGORY_ORDER = tuple(CATEGORIES)


def path_regex(allowed=()):
    """The gateway's matcher for every category not allowed for this
    instance; '' when all are allowed (then no block at all)."""
    parts = [CATEGORIES[c][0] for c in CATEGORY_ORDER if c not in set(allowed or ())]
    if not parts:
        return ""
    return "(?i)(^|/)(" + "|".join(parts) + ")$"


def blocked(path, allowed=()):
    rx = path_regex(allowed)
    return bool(rx) and re.search(rx, path or "") is not None


# --------------------------------------------------------------- the probe

# (path, category, check). Small on purpose: these are the files the
# scanners of 2026 ask for first; the gateway pattern covers far more.
PROBES = (
    ("/.env", "env", "env"), ("/.env.local", "env", "env"),
    ("/.env.production", "env", "env"), ("/.env.bak", "env", "env"),
    ("/.git/config", "vcs", "gitconfig"), ("/.git/HEAD", "vcs", "githead"),
    ("/.aws/credentials", "cloud", "aws"), ("/.npmrc", "cloud", "npmrc"),
    ("/.htpasswd", "cloud", "htpasswd"), ("/.git-credentials", "cloud", "gitcred"),
    ("/id_rsa", "keys", "key"), ("/server.key", "keys", "key"),
    ("/wp-config.php.bak", "php", "wpconfig"),
    ("/backup.sql", "dumps", "sql"), ("/dump.sql", "dumps", "sql"),
    ("/database.sqlite", "dumps", "sqlite"), ("/db.sqlite3", "dumps", "sqlite"),
    ("/vercel.json", "deploy", "json"), ("/serverless.yml", "deploy", "yaml"),
    ("/docker-compose.yml", "deploy", "compose"),
)
# Under each declared route prefix too (an app mounted at /api with its own
# web root): the files that matter most.
PREFIX_PROBES = (("/.env", "env", "env"), ("/.git/config", "vcs", "gitconfig"))

CHECKS = {
    "env": re.compile(r"^\s*(export\s+)?[A-Z][A-Z0-9_]{1,63}\s*=", re.M),
    "gitconfig": re.compile(r"^\s*\[(core|remote)", re.M),
    "githead": re.compile(r"^ref: refs/|^[0-9a-f]{40}\s*$", re.M),
    "aws": re.compile(r"aws_access_key_id|aws_secret_access_key", re.I),
    "npmrc": re.compile(r"_authToken|_auth\s*=|registry\s*=", re.I),
    "htpasswd": re.compile(r"^[^:\s]+:(\$apr1\$|\$2[aby]\$|\{SHA\}|\$[156]\$)", re.M),
    "gitcred": re.compile(r"https?://[^:\s]+:[^@\s]+@"),
    "key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "wpconfig": re.compile(r"DB_PASSWORD|DB_USER|AUTH_KEY"),
    "sql": re.compile(r"\b(CREATE TABLE|INSERT INTO|DROP TABLE)\b", re.I),
    "sqlite": re.compile(r"^SQLite format 3"),
    "json": re.compile(r"^\s*\{"),
    "yaml": re.compile(r"^\s*(service|provider|functions)\s*:", re.M),
    "compose": re.compile(r"^\s*(services|version)\s*:", re.M),
}

HTML_START = re.compile(r"^\s*(<!doctype|<html|<head|<body|<\?xml|<!--)", re.I)
MAX_BODY = 64 * 1024


def signature(status, body):
    """What a 'nothing here' answer looks like -- compared with every probe,
    so an app that answers everything identically produces no finding."""
    return (status, hashlib.sha256((body or b"")[:MAX_BODY]).hexdigest())


def judge(path, check, status, body, fallback=None):
    """-> a sentence why this is a finding, or ''. Only a 200 whose body
    looks like the file counts; an HTML page, the app's fallback answer and
    an empty body never do."""
    if status != 200 or not body:
        return ""
    if fallback is not None and signature(status, body) == fallback:
        return ""
    text = body[:MAX_BODY].decode("utf-8", "replace")
    if check != "sqlite" and HTML_START.match(text):
        return ""
    rx = CHECKS.get(check)
    if not rx or not rx.search(text):
        return ""
    return {"env": "Variablen der Form NAME=…", "gitconfig": "Konfiguration eines Git-Repositorys",
            "githead": "Git-Verweis", "aws": "AWS-Zugangsdaten", "npmrc": "npm-Zugangsdaten",
            "htpasswd": "Passwort-Hashes", "gitcred": "Git-Zugangsdaten mit Passwort",
            "key": "ein privater Schlüssel", "wpconfig": "Datenbank-Zugangsdaten",
            "sql": "ein SQL-Abzug", "sqlite": "eine SQLite-Datenbank",
            "json": "eine JSON-Konfiguration", "yaml": "eine Serverless-Konfiguration",
            "compose": "eine Docker-Compose-Datei"}.get(check, check)


def probes_for(route_paths):
    """The probe list for an instance with these declared route prefixes."""
    out = list(PROBES)
    seen = {p for p, _c, _k in out}
    for prefix in sorted({p.rstrip("/") for p in route_paths or () if p and p != "/"}):
        for p, c, k in PREFIX_PROBES:
            q = prefix + p
            if q not in seen:
                seen.add(q)
                out.append((q, c, k))
    return out
