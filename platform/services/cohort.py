"""The cohort: template and plan (RFC-0046 §2-§3).

Pure on purpose -- no docker, no registry, no identity. What a seat is
made of, how it is called, what a template may say and what it may not
are questions a test can ask without a node; `appctl.py` does the acting.
Beside `place.py` and `idp.py` for the same reason they are: one judgement,
written once, and the path is the same in the repository and in
$APP_DIR after an update.
"""
import csv
import datetime
import os
import re
import shutil

import yaml

FORMAT = "0.1"
TEMPLATE_FILE = "cohort.yaml"

# Short on purpose: the name prefixes users, groups AND instances, and a
# group tag may be 40 characters at most (identity, RFC-0007).
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?")
PART_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,14}[a-z0-9])?")
GROUP_MAX = 40
USER_MAX = 40
MAX_SEATS = 99
CHANNELS = ("test", "production")
# A config key that looks like a credential may not carry a literal
# value in a template (§2): the file lives in somebody's repository.
SECRET_KEY = re.compile(r"(PASS|SECRET|TOKEN|KEY)", re.I)
PLACEHOLDERS = ("nn", "name", "user", "seat", "app")
TOP_KEYS = {"oaap_cohort", "name", "tenant", "seats", "participants",
            "lifetime", "resources", "apps", "users", "handout"}
APP_KEYS = {"id", "git", "path", "ref", "source", "name", "channel", "config",
            "seed", "seed_to", "material", "start", "resources", "shared"}


class TemplateError(Exception):
    """A template that must not be used, with every problem found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def parse_days(text):
    """`30d` / `12w` -> days, or None when the text is not one."""
    m = re.fullmatch(r"(\d{1,4})([dw])", str(text or "").strip().lower())
    if not m:
        return None
    return int(m.group(1)) * (7 if m.group(2) == "w" else 1)


def parse_date(text):
    if isinstance(text, datetime.datetime):
        return text.date()
    if isinstance(text, datetime.date):
        return text
    try:
        return datetime.datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def seed_path_refusal(path):
    """Why a seed path may not be used ('' when it may) -- §10.

    A seed is written under the instance's own directory and nowhere
    else: absolute paths, drive letters and any `..` are refused by
    what they say, before a file system is asked.
    """
    p = str(path or "")
    if not p.strip():
        return "leerer Pfad"
    if "\x00" in p or "\\" in p:
        return "Zeichen im Pfad nicht erlaubt"
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return "absoluter Pfad"
    parts = p.split("/")
    if any(x in ("..", "") for x in parts):
        return "'..' oder leeres Pfadstück"
    return ""


def _inside(base, candidate):
    base = os.path.realpath(base)
    real = os.path.realpath(candidate)
    return real == base or real.startswith(base + os.sep)


def _seat_ids(raw):
    """Seat ids from `seats: N` or `participants: [...]`."""
    if isinstance(raw.get("participants"), list):
        ids = []
        for who in raw["participants"]:
            slug = re.sub(r"[^a-z0-9]+", "-", str(who).strip().lower()).strip("-")
            ids.append(slug)
        return ids, [str(w).strip() for w in raw["participants"]]
    n = raw.get("seats")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_SEATS:
        return None, None
    width = max(2, len(str(n)))
    ids = [f"{i:0{width}d}" for i in range(1, n + 1)]
    return ids, [None] * n


def load(directory):
    """Read and check a template directory -> normalized dict.

    Everything wrong is collected and raised together: a trainer fixing a
    file one error per run would give up before the tenth.
    """
    path = os.path.join(directory, TEMPLATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError:
        raise TemplateError([f"{TEMPLATE_FILE} nicht gefunden in {directory}"])
    except yaml.YAMLError as e:
        raise TemplateError([f"{TEMPLATE_FILE} ist kein gültiges YAML: {e}"])
    if not isinstance(raw, dict):
        raise TemplateError([f"{TEMPLATE_FILE} muss ein Objekt sein"])
    return validate(raw, directory)


def validate(raw, directory):
    bad = []
    unknown = sorted(set(raw) - TOP_KEYS)
    if unknown:
        bad.append("unbekannte Schlüssel: " + ", ".join(unknown))
    if str(raw.get("oaap_cohort", "")) != FORMAT:
        bad.append(f"oaap_cohort muss \"{FORMAT}\" sein")
    name = str(raw.get("name", "") or "")
    if not NAME_RE.fullmatch(name):
        bad.append("name: Kleinbuchstaben, Ziffern, '-' (höchstens 24 Zeichen)")
    tenant = str(raw.get("tenant", "") or "").strip()
    ids, labels = _seat_ids(raw)
    if ids is None:
        bad.append(f"seats: eine Zahl von 1 bis {MAX_SEATS} (oder participants:)")
        ids, labels = [], []
    if len(ids) > MAX_SEATS:
        bad.append(f"höchstens {MAX_SEATS} Plätze")
    for sid in ids:
        if not sid or not PART_RE.fullmatch(sid):
            bad.append(f"Platzname '{sid}' ungültig (Kleinbuchstaben, Ziffern, '-')")
    if len(set(ids)) != len(ids):
        bad.append("Platznamen doppelt")

    life = raw.get("lifetime") or {}
    if not isinstance(life, dict):
        bad.append("lifetime muss ein Objekt sein")
        life = {}
    ends = life.get("ends")
    if ends is not None and parse_date(ends) is None:
        bad.append("lifetime.ends: ein Datum JJJJ-MM-TT")
    d_after = life.get("deactivate_users_after")
    x_after = life.get("delete_users_after")
    for key, val in (("deactivate_users_after", d_after),
                     ("delete_users_after", x_after)):
        if val is not None and parse_days(val) is None:
            bad.append(f"lifetime.{key}: z. B. 30d oder 12w")
    if (d_after is not None or x_after is not None) and ends is None:
        bad.append("lifetime: die Termine der Benutzer zählen ab `ends`")
    if (d_after is not None and x_after is not None
            and parse_days(d_after) is not None and parse_days(x_after) is not None
            and parse_days(x_after) <= parse_days(d_after)):
        bad.append("lifetime: delete_users_after muss nach deactivate_users_after liegen")

    res = raw.get("resources") or {}
    if not isinstance(res, dict) or set(res) - {"memory", "cpus", "pids"}:
        bad.append("resources: nur memory, cpus, pids")
        res = {}

    users_raw = raw.get("users") or {}
    if not isinstance(users_raw, dict):
        bad.append("users muss ein Objekt sein")
        users_raw = {}
    prefix = str(users_raw.get("prefix", "tn"))
    if not PART_RE.fullmatch(prefix):
        bad.append("users.prefix: Kleinbuchstaben, Ziffern, '-'")
    roles = users_raw.get("roles", ["user"])
    if (not isinstance(roles, list) or not roles
            or any(not isinstance(r, str) for r in roles)):
        bad.append("users.roles: eine Liste")
        roles = ["user"]
    elif "server_admin" in roles:
        # Refused where the template is read, not only where identity
        # would refuse later (§10): a template that asks for it is wrong.
        bad.append("users.roles: server_admin wird nicht vergeben")
    if str(users_raw.get("first_login", "change_password")) != "change_password":
        bad.append("users.first_login: nur change_password")
    users = {"prefix": prefix, "roles": [str(r) for r in roles],
             "display_name": str(users_raw.get("display_name", "Teilnehmer {nn}")),
             "first_login": "change_password"}

    apps_raw = raw.get("apps")
    apps = []
    if not isinstance(apps_raw, list) or not apps_raw:
        bad.append("apps: mindestens eine App")
        apps_raw = []
    seen = set()
    for i, a in enumerate(apps_raw, 1):
        apps.append(_validate_app(a, i, directory, bad, seen))

    handout = str(raw.get("handout", "handout.csv") or "")
    if handout and (os.path.isabs(handout) or ".." in handout.replace("\\", "/").split("/")):
        bad.append("handout: ein Dateiname, kein Pfad nach außen")

    tpl = {"format": FORMAT, "name": name, "tenant": tenant,
           "seat_ids": ids, "seat_labels": labels,
           "lifetime": {"ends": str(parse_date(ends) or "") if ends else "",
                        "deactivate_users_after": str(d_after or ""),
                        "delete_users_after": str(x_after or "")},
           "resources": {k: str(v) for k, v in res.items()},
           "apps": apps, "users": users, "handout": handout}
    if not bad:
        bad += _length_problems(tpl)
    if bad:
        raise TemplateError(bad)
    return tpl


def _validate_app(a, i, directory, bad, seen):
    where = f"apps[{i}]"
    if not isinstance(a, dict):
        bad.append(f"{where}: muss ein Objekt sein")
        return {}
    unknown = sorted(set(a) - APP_KEYS)
    if unknown:
        bad.append(f"{where}: unbekannte Schlüssel: {', '.join(unknown)}")
    if bool(a.get("id")) == bool(a.get("git")):
        bad.append(f"{where}: genau eins von id (Store) oder git")
    app_id = str(a.get("id") or a.get("git") or "")
    name = str(a.get("name") or a.get("id") or "")
    if not PART_RE.fullmatch(name):
        bad.append(f"{where}: name (Kleinbuchstaben, Ziffern, '-')")
    if name in seen:
        bad.append(f"{where}: Name '{name}' kommt doppelt vor")
    seen.add(name)
    channel = str(a.get("channel", "test"))
    if channel not in CHANNELS:
        bad.append(f"{where}: channel test oder production")

    config = {}
    raw_cfg = a.get("config") or {}
    if not isinstance(raw_cfg, dict):
        bad.append(f"{where}.config: muss ein Objekt sein")
        raw_cfg = {}
    for key, val in raw_cfg.items():
        key = str(key)
        if isinstance(val, dict):
            if set(val) != {"secret"} or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}",
                                                          str(val.get("secret", ""))):
                bad.append(f"{where}.config.{key}: {{secret: <Name>}}")
                continue
            config[key] = {"secret": str(val["secret"])}
        elif val is None or isinstance(val, (str, int, float, bool)):
            text = "" if val is None else (
                ("ja" if val else "nein") if isinstance(val, bool) else str(val))
            if text and SECRET_KEY.search(key):
                # By field name, as §2 says: a key that reads like a
                # credential with a literal value is refused, not warned.
                bad.append(f"{where}.config.{key}: sieht nach einem Geheimnis aus "
                           "— {secret: <Name>} statt des Werts (§2)")
                continue
            config[key] = text
        else:
            bad.append(f"{where}.config.{key}: Text, Zahl oder {{secret: …}}")

    seed = {}
    raw_seed = a.get("seed") or {}
    if not isinstance(raw_seed, dict):
        bad.append(f"{where}.seed: muss ein Objekt sein")
        raw_seed = {}
    for target, src in raw_seed.items():
        why = seed_path_refusal(target)
        if why:
            bad.append(f"{where}.seed '{target}': {why}")
            continue
        src = str(src or "")
        full = os.path.join(directory, src)
        if seed_path_refusal(src) or not os.path.isfile(full) \
                or os.path.islink(full) or not _inside(directory, full):
            bad.append(f"{where}.seed '{target}': Quelle '{src}' fehlt, ist "
                       "ein Link oder liegt außerhalb der Vorlage")
            continue
        seed[str(target)] = src

    material = str(a.get("material") or "")
    if material:
        full = os.path.join(directory, material)
        if seed_path_refusal(material.rstrip("/") or ".") or not os.path.isdir(full) \
                or not _inside(directory, full):
            bad.append(f"{where}.material: Verzeichnis '{material}' fehlt oder "
                       "liegt außerhalb der Vorlage")
    res = a.get("resources") or {}
    if not isinstance(res, dict) or set(res) - {"memory", "cpus", "pids"}:
        bad.append(f"{where}.resources: nur memory, cpus, pids")
        res = {}
    return {"id": app_id, "kind": "git" if a.get("git") else "store",
            "path": str(a.get("path") or ""), "ref": str(a.get("ref") or ""),
            "source": str(a.get("source") or ""), "name": name,
            "channel": channel, "config": config, "seed": seed,
            "seed_to": str(a.get("seed_to") or "home"),
            "material": material.rstrip("/"), "start": str(a.get("start") or ""),
            "resources": {k: str(v) for k, v in res.items()},
            "shared": bool(a.get("shared"))}


def _length_problems(tpl):
    """Names are composed from several parts: check the composed ones."""
    bad = []
    for sid in tpl["seat_ids"][:1] + tpl["seat_ids"][-1:]:
        for label, text, limit in (
                ("Benutzername", user_name(tpl, sid), USER_MAX),
                ("Gruppe", seat_group(tpl, sid), GROUP_MAX)):
            if len(text) > limit:
                bad.append(f"{label} '{text}' ist länger als {limit} Zeichen — "
                           "kürzerer Name oder Präfix")
        for a in tpl["apps"]:
            inst = instance_name(tpl, a, sid)
            if len(inst) > 40:
                bad.append(f"Instanzname '{inst}' ist länger als 40 Zeichen")
    return bad


# ---------------------------------------------------------------- the seat

def user_name(tpl, sid):
    return f"{tpl['name']}-{tpl['users']['prefix']}-{sid}"


def seat_group(tpl, sid):
    return f"{tpl['name']}-{sid}"


def cohort_group(tpl):
    return tpl["name"]


def instance_name(tpl, app, sid):
    """`kurs-2026-10-ide-07`; a shared app has no seat: `kurs-2026-10-forgejo`."""
    if app.get("shared"):
        return f"{tpl['name']}-{app['name']}"
    return f"{tpl['name']}-{app['name']}-{sid}"


def seat_label(tpl, sid):
    i = tpl["seat_ids"].index(sid)
    return tpl["seat_labels"][i]


def context(tpl, sid, app=None):
    """The words a seed or a display name may use."""
    if not sid:  # a shared instance belongs to no seat
        return {"nn": "", "name": tpl["name"], "user": "", "seat": "",
                "app": (app or {}).get("name", "")}
    label = seat_label(tpl, sid)
    return {"nn": sid, "name": tpl["name"], "user": user_name(tpl, sid),
            "seat": label or sid, "app": (app or {}).get("name", "")}


def expand(text, ctx):
    """Replace `{nn}`, `{name}`, `{user}`, `{seat}`, `{app}` and nothing
    else: a settings file is full of braces of its own."""
    def sub(m):
        return ctx[m.group(1)] if m.group(1) in ctx else m.group(0)
    return re.sub(r"\{(" + "|".join(PLACEHOLDERS) + r")\}", sub, text)


def display_name(tpl, sid):
    return expand(tpl["users"]["display_name"], context(tpl, sid))


def user_dates(tpl):
    """(deactivate_at, delete_at) for a seat's user, '' where none (§5).

    Dates only: nothing acts on them here, and no date ever touches an
    instance (RFC-0030 D4).
    """
    life = tpl["lifetime"]
    ends = parse_date(life["ends"]) if life["ends"] else None
    out = []
    for key in ("deactivate_users_after", "delete_users_after"):
        days = parse_days(life[key])
        out.append((ends + datetime.timedelta(days=days)).isoformat()
                   if ends and days else "")
    return tuple(out)


# ------------------------------------------------------- what lands on disk

def _chown_tree(path, uid):
    if uid is None:
        return
    os.chown(path, uid, uid)


def write_seed(root, rel, data, uid=None, overwrite=False):
    """Write one seed file below `root` -> True when it wrote.

    Never outside `root` (a symlink the participant made inside their home
    must not carry a re-seed to somewhere else), and never over a file that
    is there unless `overwrite` -- `create` and `add` only fill, `reset`
    replaces (§2).
    """
    why = seed_path_refusal(rel)
    if why:
        raise ValueError(f"Saat-Pfad '{rel}': {why}")
    dest = os.path.join(root, *rel.split("/"))
    parent = os.path.dirname(dest)
    if not _inside(root, parent):
        raise ValueError(f"Saat-Pfad '{rel}' führt aus der Ablage hinaus")
    if os.path.islink(dest) or (os.path.exists(dest) and not overwrite):
        return False
    # create missing directories one by one so each gets the owner
    missing, walk = [], parent
    while not os.path.isdir(walk):
        missing.append(walk)
        walk = os.path.dirname(walk)
    for d in reversed(missing):
        os.mkdir(d)
        _chown_tree(d, uid)
    with open(dest, "wb") as f:
        f.write(data)
    _chown_tree(dest, uid)
    return True


def read_seed(template_dir, src, ctx):
    """The bytes of a seed with placeholders expanded (text only)."""
    with open(os.path.join(template_dir, src), "rb") as f:
        raw = f.read()
    try:
        return expand(raw.decode("utf-8"), ctx).encode("utf-8")
    except UnicodeDecodeError:
        return raw


def copy_material(src, dest, uid=None, replace=False):
    """Copy the course directory into a seat's `material` storage.

    `replace` empties the destination first (`material update`); without it
    files are only added. Links in the source are skipped, never followed.
    """
    os.makedirs(dest, exist_ok=True)
    _chown_tree(dest, uid)
    if replace:
        for entry in os.listdir(dest):
            p = os.path.join(dest, entry)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    count = 0
    for base, dirs, files in os.walk(src):
        rel = os.path.relpath(base, src)
        target = dest if rel == "." else os.path.join(dest, rel)
        for d in list(dirs):
            if os.path.islink(os.path.join(base, d)):
                dirs.remove(d)
                continue
            os.makedirs(os.path.join(target, d), exist_ok=True)
            _chown_tree(os.path.join(target, d), uid)
        for fn in files:
            s = os.path.join(base, fn)
            if os.path.islink(s):
                continue
            d = os.path.join(target, fn)
            if not replace and os.path.exists(d):
                continue
            shutil.copyfile(s, d)
            _chown_tree(d, uid)
            count += 1
    return count


# ---------------------------------------------------------------- the handout

HANDOUT_HEAD = ["seat", "username", "password", "address"]


class Handout:
    """The one-time list, written row by row as seats are made.

    Opened exclusively (`O_EXCL`) and BEFORE the first user exists: a
    handout that cannot be written must stop the run while there is
    nothing yet whose password only this file would hold (§10). Every row
    is flushed at once, so an interrupted run has lost no password of a
    seat it finished.
    """

    def __init__(self, path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.path = path
        self._f = os.fdopen(fd, "w", encoding="utf-8", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(HANDOUT_HEAD)
        self._f.flush()

    def add(self, row):
        self._w.writerow([row.get(k, "") for k in HANDOUT_HEAD])
        self._f.flush()

    def close(self):
        self._f.close()


def handout_path_refusal(path, template_dir):
    """Why a handout may not go there ('' when it may).

    Not into the template's own directory: that is somebody's repository,
    and passwords in a repository are the thing the template rules exist
    to prevent (§2).
    """
    if os.path.exists(path):
        return f"{path} gibt es schon — ein zweites Handout wäre eine zweite Kopie"
    if _inside(template_dir, os.path.dirname(os.path.abspath(path))):
        return "nicht in das Verzeichnis der Vorlage (das ist ein Repository)"
    return ""
