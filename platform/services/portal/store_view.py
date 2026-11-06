"""Store catalogue rules (RFC-0012 §1.2, §3, §6) — no web framework.

Everything here is a decision the RFC makes, not a rendering detail:
which entry wins when two lists carry the same app id, what a node does
with a vocabulary value it has never seen, which images it is willing to
fetch, and what is filtered by default. It lives apart from `app.py` so
those rules can be read and tested without Flask, a request, or a node.

`app.py` supplies the I/O — reading the sources file, fetching the
lists, knowing what is installed — and renders the result.
"""
from datetime import date

# Trust classes (RFC-0012 §3). appctl.py is the authority; the portal
# only reads, and reads tolerantly: an entry it cannot classify counts
# as unverified, which is the cautious direction to fall in.
TRUST_LABEL = {"platform": "von uns", "verified": "geprüft",
               "unverified": "muss bestätigt werden"}
TRUST_RANK = {"platform": 3, "verified": 2, "unverified": 1}

# ---- vocabulary (RFC-0012 §1.2) -------------------------------------------
# Labels only. An unknown value is never dropped and never a reason to
# refuse a list — it is shown verbatim and sorted under "Sonstiges", or
# every extension of the vocabulary would strand exactly the nodes that
# have not updated yet (decision 5, 2026-08-09).

CATEGORY_LABEL = {
    "business": "Geschäft", "productivity": "Produktivität",
    "documents": "Dokumente", "communication": "Kommunikation",
    "media": "Medien", "monitoring": "Überwachung", "iot": "IoT",
    "automation": "Automatisierung", "ai": "KI", "development": "Entwicklung",
    "security": "Sicherheit", "storage-backup": "Speicher & Sicherung",
    "infrastructure": "Infrastruktur",
}
CLASS_LABEL = {"frontend": "Mit Oberfläche", "service": "Hintergrunddienst"}
AUDIENCE_LABEL = {"everyone": "Alle", "operator": "Betreiber",
                  "developer": "Entwickler", "expert": "Experten"}
MATURITY_LABEL = {"alpha": "Alpha", "beta": "Beta", "preview": "Vorschau",
                  "stable": "Stabil"}
STATUS_LABEL = {"active": "Aktiv", "deprecated": "Veraltet",
                "archived": "Archiviert"}
REL_LABEL = {"homepage": "Website", "docs": "Dokumentation",
             "source": "Quellcode", "demo": "Demo",
             "changelog": "Änderungen", "support": "Hilfe",
             "privacy": "Datenschutz", "license": "Lizenz"}

NEW_FOR_DAYS = 30   # "neu" is computed from `released`, never stored (§1.1)


def label(value, table):
    return table.get(value, value)


def list_relative(list_url, path):
    """Resolve an icon/screenshot path against the list's own URL.

    RFC-0012 §1.1: paths only, never foreign URLs. Otherwise every node
    that opens the store page would contact hosts nobody chose — and
    announce its existence and address for the sake of a thumbnail. An
    entry that tries anyway is dropped here rather than rendered.
    """
    if not path or "://" in path or path.startswith("/") or ".." in path             or str(list_url).startswith("catalog:"):
        return ""
    return list_url.rsplit("/", 1)[0] + "/" + path


def entry_view(a, src, installed, pending, profiles, today=None):
    """One store list entry, normalised for display.

    Reads 0.1 and 0.2 alike: `homepage` becomes a link with rel
    `homepage`, and `description` stands in for a missing `summary`.
    What the format does not carry stays empty rather than invented.
    """
    pkg = a.get("package") or {}
    links = [{"rel": l.get("rel", ""), "url": l.get("url", ""),
              "label": l.get("label") or label(l.get("rel", ""), REL_LABEL)}
             for l in (a.get("links") or []) if l.get("url")]
    if a.get("homepage") and not any(l["rel"] == "homepage" for l in links):
        links.insert(0, {"rel": "homepage", "url": a["homepage"],
                         "label": REL_LABEL["homepage"]})
    description = a.get("description", "")
    released = a.get("released", "")
    is_new = False
    if released:
        try:
            age = ((today or date.today()) - date.fromisoformat(released)).days
            is_new = 0 <= age <= NEW_FOR_DAYS
        except ValueError:
            pass          # a date we cannot read is not a reason to refuse
    app_id = a.get("id", "")
    wanted = list(a.get("profiles") or [])
    audience = a.get("audience") or []
    categories = a.get("categories") or []
    command = ""
    if pkg.get("git"):
        command = f"sudo oaap app install {pkg['git']}"
        if pkg.get("path"):
            command += f" --path {pkg['path']}"
        if pkg.get("ref"):
            command += f" --ref {pkg['ref']}"
    return {
        "id": app_id, "name": a.get("name") or app_id,
        "type": a.get("type", ""), "version": a.get("version", "?"),
        "released": released, "is_new": is_new,
        "summary": a.get("summary") or description, "description": description,
        "license": a.get("license", ""), "links": links,
        "icon": list_relative(src["url"], a.get("icon")),
        "screenshots": [{"src": list_relative(src["url"], s.get("src")),
                         "caption": s.get("caption", "")}
                        for s in (a.get("screenshots") or [])
                        if list_relative(src["url"], s.get("src"))],
        "categories": categories,
        "category_labels": [label(c, CATEGORY_LABEL) for c in categories],
        "app_class": a.get("app_class", ""),
        "class_label": label(a.get("app_class", ""), CLASS_LABEL) if a.get("app_class") else "",
        "audience": audience,
        "audience_labels": [label(x, AUDIENCE_LABEL) for x in audience],
        "maturity": a.get("maturity", ""),
        "maturity_label": label(a.get("maturity", ""), MATURITY_LABEL),
        "status": a.get("status") or "active",
        "status_label": label(a.get("status") or "active", STATUS_LABEL),
        "tags": a.get("tags") or [],
        "roles": a.get("roles") or [],
        "profiles": wanted,
        # A profile this node does not have is a hint, never a refusal
        # (RFC-0011): filtered out by default, one click away.
        "profile_fit": not wanted or bool(set(wanted) & set(profiles)),
        "expert": "expert" in audience,
        "pinned": bool(pkg.get("ref")), "ref": pkg.get("ref", ""),
        "package": pkg, "command": command,
        "installed": installed.get(app_id),
        "pending": app_id in pending,
        "source": src, "also_in": [],
    }


def merge_catalogue(fetched, installed, pending, profiles, today=None):
    """All sources into one catalogue (§6).

    `fetched` is [(source, list_document)] in configured order. One entry
    per app id, resolved the way the HOST resolves it (§3): highest trust
    class wins, configured order within a class. Showing two entries for
    one id would only invite a choice the host would then overrule.

    The runners-up are not dropped silently — they are named in
    `also_in`, because "this app is also in that list" is exactly what an
    operator wants to know before changing sources.
    """
    best = {}
    for pos, (src, data) in enumerate(fetched):
        src = dict(src, trust_label=TRUST_LABEL[src["trust"]], pos=pos)
        for a in (data or {}).get("apps", []):
            pkg = a.get("package") or {}
            # Git for a list read from the network; `catalog` for a list
            # the host copied from a package catalog of this node
            # (RFC-0050) -- there is no package address to show, the
            # host resolves it from the app id.
            if not a.get("id") or not (pkg.get("git") or pkg.get("catalog")):
                continue
            view = entry_view(a, src, installed, pending, profiles, today)
            cur = best.get(view["id"])
            if cur is None:
                best[view["id"]] = view
            elif (TRUST_RANK[src["trust"]], -pos) \
                    > (TRUST_RANK[cur["source"]["trust"]], -cur["source"]["pos"]):
                view["also_in"] = cur["also_in"] + [cur["source"]["name"]]
                best[view["id"]] = view
            else:
                cur["also_in"].append(src["name"])
    return sorted(best.values(), key=lambda e: e["name"].lower())


def options(entries, field, table):
    """Filter choices that actually occur, known ones first."""
    seen = set()
    for e in entries:
        v = e[field]
        seen.update(v if isinstance(v, list) else ([v] if v else []))
    known = [v for v in table if v in seen]
    other = sorted(v for v in seen if v not in table)
    return ([{"value": v, "label": table[v]} for v in known]
            + [{"value": v, "label": f"{v} (Sonstiges)"} for v in other])


def matches(e, f):
    """Whether one entry survives the filter state `f`."""
    if f.get("q"):
        haystack = " ".join([e["name"], e["summary"], e["description"],
                             " ".join(e["tags"]), e["id"]]).lower()
        if f["q"] not in haystack:
            return False
    for field in ("categories", "audience"):
        if f.get(field) and f[field] not in e[field]:
            return False
    for field in ("app_class", "maturity", "license"):
        if f.get(field) and f[field] != e[field]:
            return False
    if f.get("trust") and f["trust"] != e["source"]["trust"]:
        return False
    if f.get("source") and f["source"] != e["source"]["id"]:
        return False
    if f.get("installed") == "yes" and not e["installed"]:
        return False
    if f.get("installed") == "no" and e["installed"]:
        return False
    # Care (update / advisories) is about the caller's instances, so it
    # wins over the two defaults below: an app with an open advisory must
    # not disappear because it is archived or wants another profile.
    if f.get("care"):
        return "update_count" in e and care_matches(e, f["care"])
    # Two defaults, each reversible with one click (§6): an archived
    # entry, and an app meant for a profile this node does not have, are
    # FILTERED, not hidden. `expert` is deliberately not filtered — it is
    # marked and costs a confirmation, and hiding it would turn a hint
    # into a gate through the back door.
    if not f.get("all_status") and e["status"] == "archived":
        return False
    if not f.get("all_profiles") and not e["profile_fit"]:
        return False
    return True


# ---- care at a glance (store filter, instance list) ------------------------
# Two questions an operator asks of a whole list, not of one page: "is a
# newer version in the catalogue?" and "does the node's image reading
# (RFC-0071 §3) report open advisories?". One rule for the store cards,
# the store filter and the instance list, so the three never disagree.

CARE_FILTER_LABEL = {"update": "Update verfügbar",
                     "security": "Sicherheitsmeldungen",
                     "attention": "Update oder Sicherheitsmeldungen"}


def _numeric(v):
    parts = str(v or "").split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def update_to(installed, offered):
    """The catalogue version if it is an update for `installed`, else "".

    Plain dotted numbers compare as numbers, so an OLDER catalogue entry
    (a test instance ahead of the list) is not called an update. Anything
    else -- a pre-release, a word -- falls back to "differs", the rule the
    store's object page has always used.
    """
    if not offered or not installed or offered == installed:
        return ""
    a, b = _numeric(installed), _numeric(offered)
    if a is not None and b is not None:
        return offered if b > a else ""
    return offered


def instance_care(key, inst, catalogue, images):
    """Care facts of one instance for a list row.

    `catalogue` is {app_id: {"version", "source_id"}} (the merged store),
    `images` the `images` block of the node's care file.
    """
    c = catalogue.get(inst.get("app_id") or "") or {}
    upd = update_to(str(inst.get("version") or ""), str(c.get("version") or ""))
    e = ((images or {}).get("instances") or {}).get(key) or {}
    services = e.get("services") or []
    open_n = sum(int(s.get("open") or 0) for s in services)
    behind = (e.get("state") == "behind"
              or any(s.get("state") == "behind" for s in services))
    return {"newer": upd, "update_source": c.get("source_id", "") if upd else "",
            "open": open_n, "behind": behind and not open_n,
            "image_state": e.get("state", ""),
            "attention": bool(upd or open_n)}


def annotate_care(entries, instances, images):
    """Per store entry: how many of the caller's instances have an update
    waiting, and how many open advisories they carry together."""
    catalogue = {e["id"]: {"version": e["version"], "source_id": e["source"]["id"]}
                 for e in entries}
    by_app = {}
    for key, inst in instances.items():
        by_app.setdefault(inst.get("app_id") or "", []).append(
            instance_care(key, inst, catalogue, images))
    for e in entries:
        rows = by_app.get(e["id"], [])
        e["update_count"] = sum(1 for r in rows if r["newer"])
        e["open_count"] = sum(r["open"] for r in rows)
        e["open_instances"] = sum(1 for r in rows if r["open"])
    return entries


def care_matches(item, want):
    """`want` is "", "update", "security" or "attention"; `item` a store
    entry (counts) or an instance row (care dict)."""
    if not want:
        return True
    if "update_count" in item:
        upd, sec = item["update_count"] > 0, item["open_count"] > 0
    else:
        upd, sec = bool(item["care"]["newer"]), item["care"]["open"] > 0
    return {"update": upd, "security": sec, "attention": upd or sec}.get(want, True)


def instance_matches(row, f):
    """Whether one instance-list row survives the filter state `f`."""
    if f.get("q"):
        hay = " ".join([row["key"], row["name"], row["app_name"]]).lower()
        if f["q"] not in hay:
            return False
    if f.get("state") and row["status"]["state"] != f["state"]:
        return False
    if f.get("channel") and row["channel"] != f["channel"]:
        return False
    if f.get("tenant") and (row.get("tenant") or "") != f["tenant"]:
        return False
    return care_matches(row, f.get("care", ""))
