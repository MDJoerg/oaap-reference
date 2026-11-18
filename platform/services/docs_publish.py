"""The documentation an app brings (manifest 0.17 `docs:`, RFC-0094,
oaap.apps.runtime 2.36).

Pure functions, no registry and no Docker, so the rules can be tested on
paper:

    declaration_error(m)   '' or the sentence for the operator
    products_of(m)         the normalised list of help products
    extract(pkg, products, dest)
                           copy exactly the declared folders, within the
                           limits, into dest/<product>/; returns
                           (published, dropped)

The package is data. Nothing outside a declared folder is read, no link is
followed, no path leaves its folder, only `.md` and the declared media
types are copied. A product that breaks a limit is DROPPED with a reason --
the install never fails because of its documentation.
"""
import json
import os
import re
import shutil
import stat

KINDS = ("overview", "concept", "function", "tutorial", "howto", "faq",
         "reference", "cli", "api", "troubleshooting", "admin-guide",
         "release-notes", "glossary", "presentation", "unspecified")
READERS = ("end-user", "power-user", "admin", "developer", "consultant",
           "operator")
MEDIA = ("png", "svg", "jpg", "jpeg", "webp", "gif", "html")
DEFAULT_MEDIA = ("png", "svg", "jpg", "jpeg", "webp")
DEFAULT_AUDIENCE = ("admin",)
FIELDS = ("product", "title", "path", "entry", "icon", "color", "lang",
          "audience", "readers", "kinds", "media", "context")
PRODUCT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
LANG_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_PRODUCTS = 8
MAX_FILES = 5000
MAX_BYTES = 50 * 1024 * 1024
MAX_HTML_BYTES = 20 * 1024 * 1024


def _local_path(p, folder=False):
    """A relative path inside the package: no leading /, no .., no
    backslash, no empty segment."""
    if not isinstance(p, str) or not p.strip():
        return False
    p = p.strip()
    if p.startswith("/") or "\\" in p or "\x00" in p:
        return False
    parts = p.rstrip("/").split("/") if folder else p.split("/")
    return all(x not in ("", ".", "..") for x in parts)


def _entries(m):
    d = m.get("docs")
    if isinstance(d, dict):
        return [d]
    return d if isinstance(d, list) else None


def declaration_error(m):
    """'' when `docs:` (and `docs_sources:`) are valid, else one sentence."""
    if "docs_sources" in m:
        ds = m["docs_sources"]
        if not isinstance(ds, dict):
            return "docs_sources: object expected"
        if set(ds) - {"read"}:
            return "docs_sources: only 'read' is known"
        if "read" in ds and not isinstance(ds["read"], bool):
            return "docs_sources.read: true or false"
    if "docs" not in m:
        return ""
    items = _entries(m)
    if items is None:
        return "docs: a list of help products (or one) expected"
    if not 1 <= len(items) <= MAX_PRODUCTS:
        return f"docs: one to {MAX_PRODUCTS} help products"
    app_id = str((m.get("app") or {}).get("id") or "")
    seen = set()
    for i, e in enumerate(items):
        where = f"docs[{i}]"
        if not isinstance(e, dict):
            return f"{where}: object expected"
        unknown = set(e) - set(FIELDS)
        if unknown:
            return f"{where}: unknown field(s) {', '.join(sorted(unknown))}"
        if not _local_path(e.get("path"), folder=True):
            return f"{where}.path: a folder inside the package (relative, no ..)"
        pid = e.get("product", app_id)
        if not isinstance(pid, str) or not PRODUCT_RE.match(pid):
            return f"{where}.product: lowercase [a-z0-9-], up to 40 characters"
        if pid in seen:
            return f"{where}.product: '{pid}' declared twice"
        seen.add(pid)
        for k in ("title",):
            if k in e and (not isinstance(e[k], str) or len(e[k]) > 80):
                return f"{where}.{k}: a string up to 80 characters"
        for k in ("entry", "icon"):
            if k in e and not _local_path(e[k]):
                return f"{where}.{k}: a file path (relative, no ..)"
        if "color" in e and not (isinstance(e["color"], str) and COLOR_RE.match(e["color"])):
            return f"{where}.color: #rrggbb"
        if "lang" in e and not (isinstance(e["lang"], str) and LANG_RE.match(e["lang"])):
            return f"{where}.lang: a language like de or en"
        if "audience" in e:
            a = e["audience"]
            if not isinstance(a, list) or not a or any(
                    not isinstance(x, str) or not (ROLE_RE.match(x) or x == "public") for x in a):
                return f"{where}.audience: a list of role names (or public)"
        if "readers" in e:
            r = e["readers"]
            if not isinstance(r, list) or any(x not in READERS for x in r):
                return f"{where}.readers: a list of {', '.join(READERS)}"
        if "kinds" in e:
            k = e["kinds"]
            if not isinstance(k, dict):
                return f"{where}.kinds: a map of path → document type"
            for p, kind in k.items():
                if not _local_path(str(p), folder=str(p).endswith("/")):
                    return f"{where}.kinds: '{p}' is not a path inside the folder"
                if kind not in KINDS:
                    return f"{where}.kinds: '{kind}' is not a document type ({', '.join(KINDS)})"
        if "media" in e:
            md = e["media"]
            if not isinstance(md, list) or any(x not in MEDIA for x in md):
                return f"{where}.media: a list of {', '.join(MEDIA)}"
        if "context" in e and not isinstance(e["context"], dict):
            return f"{where}.context: a map (read in a later stage)"
    return ""


def products_of(m):
    """The normalised help products of a manifest; [] when there are none
    or the declaration is invalid."""
    if declaration_error(m) or "docs" not in m:
        return []
    app = m.get("app") or {}
    out = []
    for e in _entries(m):
        path = e["path"].strip().rstrip("/") + "/"
        out.append({
            "product": e.get("product", str(app.get("id") or "")),
            "title": e.get("title") or str(app.get("name") or app.get("id") or ""),
            "path": path,
            "entry": e.get("entry") or "",
            "icon": e.get("icon") or "",
            "color": e.get("color") or "",
            "lang": e.get("lang") or "",
            "audience": list(e.get("audience") or DEFAULT_AUDIENCE),
            "readers": list(e.get("readers") or []),
            "kinds": {str(k): v for k, v in (e.get("kinds") or {}).items()},
            "media": list(e.get("media") or DEFAULT_MEDIA),
            "context": dict(e.get("context") or {}),
        })
    return out


def docs_sources_wanted(m):
    d = m.get("docs_sources")
    return isinstance(d, dict) and d.get("read") is True


def _walk(root):
    """(relative path, absolute path) of every regular file under root,
    never following a link -- a link is skipped, not resolved."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            st = os.lstat(full)
            if not stat.S_ISREG(st.st_mode):
                continue
            yield os.path.relpath(full, root).replace(os.sep, "/"), full, st.st_size


def extract(pkg, products, dest):
    """Copy each product's folder from the unpacked package `pkg` into
    `dest/<product>/`. Returns (published, dropped): published entries are
    the product dicts plus `files` and `bytes`; dropped are
    {product, reason}. `dest` is created fresh by the caller."""
    published, dropped = [], []
    real_pkg = os.path.realpath(pkg)
    for p in products:
        src = os.path.join(pkg, p["path"])
        if os.path.islink(src.rstrip("/")) or not os.path.isdir(src):
            dropped.append({"product": p["product"],
                            "reason": f"the folder '{p['path']}' is not in the package"})
            continue
        if not os.path.realpath(src).startswith(real_pkg + os.sep):
            dropped.append({"product": p["product"], "reason": "the folder leaves the package"})
            continue
        allowed = {"md"} | set(p["media"])
        files, total, why = [], 0, ""
        for rel, full, size in _walk(src):
            ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
            if ext not in allowed:
                continue
            if ext == "html" and size > MAX_HTML_BYTES:
                why = f"'{rel}' is larger than {MAX_HTML_BYTES // (1024 * 1024)} MB"
                break
            files.append((rel, full))
            total += size
            if len(files) > MAX_FILES:
                why = f"more than {MAX_FILES} files"
                break
            if total > MAX_BYTES:
                why = f"more than {MAX_BYTES // (1024 * 1024)} MB"
                break
        if why:
            dropped.append({"product": p["product"], "reason": why})
            continue
        if not any(r.lower().endswith(".md") for r, _f in files):
            dropped.append({"product": p["product"], "reason": "no Markdown file in the folder"})
            continue
        out = os.path.join(dest, p["product"])
        for rel, full in files:
            target = os.path.join(out, *rel.split("/"))
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            shutil.copyfile(full, target, follow_symlinks=False)
            os.chmod(target, 0o644)
        # icon and entry are paths relative to the product folder; one
        # that was not copied (absent, or a type not taken) is dropped
        # from the record rather than pointing at nothing.
        entry = dict(p, files=len(files), bytes=total)
        have = {r for r, _f in files}
        for k in ("icon", "entry"):
            if entry.get(k) and entry[k] not in have:
                entry[k] = ""
        published.append(entry)
    return published, dropped


def write_json(path, doc):
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
