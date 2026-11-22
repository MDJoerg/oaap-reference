"""Twin browser rules (RFC-0031 Bauplan Schritt 5) — no web framework,
no HTTP call: everything here is a decision about WHAT to show, given
data `app.py` already fetched from the twin service's `/internal/*`
API. Read `store_view.py`'s own docstring for why this split exists —
the same reason applies here, unchanged.

`app.py` supplies the I/O (calling twin, holding the person's session)
and renders the result.
"""
import datetime
import html
import re

ORIGIN_LABELS = {"tenant": "Mandant"}


def origin_label(origin):
    """A short, human label for a group's or type's origin string —
    'app:crm' -> 'App crm', 'model:kundenzufriedenheit' -> 'Datenmodell
    kundenzufriedenheit', 'tenant' or 'tenant:<id>' -> 'Mandant'. Falls
    back to the raw string for anything this has not seen yet — the
    same "never hide an unknown value" posture `store_view.py` already
    uses for its own vocabulary."""
    if origin in ORIGIN_LABELS:
        return ORIGIN_LABELS[origin]
    if origin == "tenant" or origin.startswith("tenant:"):
        return "Mandant"
    if origin.startswith("app:"):
        return f"App {origin[4:]}"
    if origin.startswith("model:"):
        return f"Datenmodell {origin[6:]}"
    return origin or "?"


def object_types(types):
    """Every active object_type, sorted by title — the tree's own root
    list (Bauplan Schritt 5: "Baum je Objekttyp")."""
    return sorted(
        (t for t in types if t["kind"] == "object_types"),
        key=lambda t: t["title"].lower())


def group_types_on(types, object_type_key):
    """Every active group_type attached to one object type ('on' in its
    own definition) — used both to render existing groups' titles and
    to offer new ones on the object page."""
    return [t for t in types if t["kind"] == "group_types"
           and (t["definition"] or {}).get("on") == object_type_key]


def attribute_defs(types):
    """{key: definition} for every active attribute_type — looked up
    when rendering a group's fields (title, value_type) or a
    type-creation form."""
    return {t["key"]: t["definition"] for t in types if t["kind"] == "attribute_types"}


def base_group_key(display_key):
    """A merged object's OWN loader may suffix a colliding group_key
    with '+<short-id>' (twin/app.py's `_load_object`) — strip that back
    off before comparing against the model registry's real key."""
    return display_key.split("+", 1)[0]


def addable_group_types(types, object_type_key, existing_group_keys):
    """Group types this tenant could add to THIS object right now: on
    the right object type, active, not already present. The object
    page's own "+ Gruppe hinzufügen" offer."""
    present = {base_group_key(k) for k in existing_group_keys}
    return [t for t in group_types_on(types, object_type_key)
           if t["key"] not in present]


def timeline(groups):
    """Every activity across every group of one object, flattened and
    time-sorted — Bauplan Schritt 5: "Zeitleiste der Aktivitäten". The
    best single timestamp per activity, in the order §3.4 itself ranks
    them: what happened, over what is only planned."""
    out = []
    for group_key, g in groups.items():
        for a in g.get("activities") or []:
            when = (a.get("finished_at") or a.get("started_at")
                   or a.get("planned_start") or a.get("planned_end"))
            out.append(dict(a, group_key=group_key, when=when))
    return sorted(out, key=lambda a: (a["when"] is None, a["when"]))


GROUP_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")
TYPE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def parse_new_type_form(form):
    """The "Typ anlegen" form (Bauplan Schritt 5: "Typen des Mandanten
    anlegen") into the body twin/app.py's `twin_create_type` expects —
    or a plain-German error, checked here so an obviously bad form
    never makes the round trip. One attribute line per non-empty
    'attr_key' -- rows with an empty key are simply skipped, the same
    tolerant-form reading the rest of this portal already uses.
    """
    on_type = (form.get("on") or "").strip()
    group_key = (form.get("group_key") or "").strip()
    group_title = (form.get("group_title") or "").strip()
    if not on_type:
        return None, "Bitte einen Objekttyp auswählen."
    if not group_key or not GROUP_KEY_RE.match(group_key):
        return None, ("Der Gruppen-Schlüssel muss wie 'namespace.name' "
                      "aussehen, klein geschrieben (z. B. 'crm.notiz').")
    keys = form.getlist("attr_key")
    titles = form.getlist("attr_title")
    value_types = form.getlist("attr_value_type")
    periodics = form.getlist("attr_periodic")
    units = form.getlist("attr_unit")
    attrs = []
    for i, key in enumerate(keys):
        key = (key or "").strip()
        if not key:
            continue
        if not TYPE_KEY_RE.match(key):
            return None, (f"Der Attribut-Schlüssel '{key}' ist ungültig "
                          "(Buchstaben/Ziffern, mit einem Buchstaben beginnend).")
        title = (titles[i] if i < len(titles) else "").strip() or key
        vt = (value_types[i] if i < len(value_types) else "").strip() or "text"
        a = {"key": key, "title": title, "value_type": vt}
        # RFC-0120 stage 2: one select per row (a checkbox list would lose its
        # row), "ja" = values with a period, plus the unit they carry
        if (periodics[i] if i < len(periodics) else "") == "ja":
            a["periodic"] = True
        unit = (units[i] if i < len(units) else "").strip()
        if unit:
            if len(unit) > 40:
                return None, f"Die Einheit von '{key}' ist zu lang (höchstens 40 Zeichen)."
            a["unit"] = unit
        attrs.append(a)
    if not attrs:
        return None, "Mindestens ein Attribut ist nötig."
    return {"on": on_type, "group_key": group_key,
           "group_title": group_title or group_key, "attributes": attrs}, None


# ------------------------------------------------- values with a period (RFC-0120 stage 2)
MONTHS = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")
QUALITIES = (("", "–"), ("measured", "gemessen"), ("calculated", "berechnet"),
             ("estimated", "geschätzt"), ("reported", "gemeldet"))
QUALITY_LABELS = dict(QUALITIES)
PARTS = ([("", "ganzes Jahr")] + [(f"q{i}", f"Q{i}") for i in range(1, 5)]
         + [(f"m{i}", MONTHS[i - 1]) for i in range(1, 13)])


def is_periodic(adefs, key):
    return bool((adefs.get(key) or {}).get("periodic"))


def period_slots(types, object_type_key, groups):
    """Where a person may enter values with a period on this object: every
    group type on its type that has periodic attributes and is the tenant's
    (present with origin tenant, or not present yet -- the first period
    creates it). [{key, title, attributes: [{key, title, unit, value_type}]}]"""
    adefs = attribute_defs(types)
    present = {base_group_key(k): g for k, g in groups.items()}
    out = []
    for gt in group_types_on(types, object_type_key):
        g = present.get(gt["key"])
        if g is not None and g.get("origin") != "tenant":
            continue
        attrs = [a for a in ((gt.get("definition") or {}).get("attributes") or [])
                 if is_periodic(adefs, a)]
        if attrs:
            out.append({"key": gt["key"], "title": gt.get("title") or gt["key"],
                        "attributes": [{"key": a, "title": adefs[a].get("title") or a,
                                        "unit": adefs[a].get("unit") or "",
                                        "value_type": adefs[a].get("value_type") or "text"}
                                       for a in attrs]})
    return out


def _dt(raw):
    return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _add_months(d, n):
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1)


def period_label(valid_from, valid_to):
    """'2025', 'Q1 2025', 'Mär 2025' or '01.01.2025 – 15.02.2025' (the last
    day the period still covers) -- how a person names a period."""
    try:
        a, b = _dt(valid_from), _dt(valid_to)
    except (TypeError, ValueError):
        return f"{valid_from} – {valid_to}"
    if all(x.hour == x.minute == x.second == 0 for x in (a, b)):
        if a.day == 1:
            if a.month == 1 and b == a.replace(year=a.year + 1):
                return str(a.year)
            if a.month in (1, 4, 7, 10) and b == _add_months(a, 3):
                return f"Q{(a.month - 1) // 3 + 1} {a.year}"
            if b == _add_months(a, 1):
                return f"{MONTHS[a.month - 1]} {a.year}"
        last = b - datetime.timedelta(days=1)
        return f"{a:%d.%m.%Y} – {last:%d.%m.%Y}"
    return f"{a:%d.%m.%Y %H:%M} – {b:%d.%m.%Y %H:%M} UTC"


def parse_period_form(form):
    """The form "Werte mit Zeitraum" into the body of a period write, or a
    plain-German error. Either a year with an optional quarter or month, or
    two dates (the second is the last day the period covers)."""
    attr = (form.get("attribute") or "").strip()
    if not TYPE_KEY_RE.match(attr):
        return None, "Bitte ein Attribut wählen."
    frm_raw, to_raw = (form.get("from") or "").strip(), (form.get("to") or "").strip()
    if frm_raw or to_raw:
        try:
            frm = datetime.date.fromisoformat(frm_raw)
            last = datetime.date.fromisoformat(to_raw)
        except ValueError:
            return None, "Von und Bis bitte beide als Datum angeben."
        if last < frm:
            return None, "Bis liegt vor Von."
    else:
        year = (form.get("year") or "").strip()
        if not re.fullmatch(r"\d{4}", year):
            return None, "Bitte ein Jahr angeben (oder Von und Bis)."
        y = int(year)
        part = (form.get("part") or "").strip()
        start = datetime.date(y, 1, 1)
        if not part:
            frm, months = start, 12
        elif re.fullmatch(r"q[1-4]", part):
            frm, months = _add_months(start, 3 * (int(part[1]) - 1)), 3
        elif re.fullmatch(r"m([1-9]|1[0-2])", part):
            frm, months = _add_months(start, int(part[1:]) - 1), 1
        else:
            return None, "Unbekannter Teil des Jahres."
        last = _add_months(frm, months) - datetime.timedelta(days=1)
    value = (form.get("value") or "").strip()
    if not value:
        return None, "Bitte einen Wert angeben."
    body = {"attribute": attr, "value": value, "valid_from": frm.isoformat(),
            "valid_to": last.isoformat()}
    for k in ("unit", "source"):
        v = (form.get(k) or "").strip()
        if v:
            body[k] = v
    q = (form.get("quality") or "").strip()
    if q:
        if q not in QUALITY_LABELS:
            return None, "Unbekannte Güte."
        body["quality"] = q
    return body, None


def periods_by_attribute(periods, slots):
    """{(group, attr): [period + label, oldest first]} for the slots shown."""
    want = {(sl["key"], a["key"]) for sl in slots for a in sl["attributes"]}
    out = {k: [] for k in want}
    for p in periods:
        k = (p["group"], p["attribute"])
        if k in out:
            out[k].append(dict(p, label=period_label(p["valid_from"], p["valid_to"]),
                               last_day=(_dt(p["valid_to"]) - datetime.timedelta(days=1))
                               .date().isoformat(),
                               first_day=_dt(p["valid_from"]).date().isoformat(),
                               quality_label=QUALITY_LABELS.get(p.get("quality") or "", "")))
    return out


def _num(v):
    try:
        if isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def svg_bars(periods, w=520, h=110):
    """Bars of a periodic attribute's numeric values, one per period (oldest
    left) -- '' when fewer than two are numbers. Inline SVG, no script."""
    pts = [(p["label"], _num(p["value"])) for p in periods]
    pts = [x for x in pts if x[1] is not None]
    if len(pts) < 2:
        return ""
    top = max(max(v for _l, v in pts), 0.0)
    low = min(min(v for _l, v in pts), 0.0)
    span = (top - low) or 1.0
    pad, base_h = 18, h - 18
    bw = (w - 2 * pad) / len(pts)
    zero = pad + (top / span) * (base_h - pad)
    parts = []
    for i, (label, v) in enumerate(pts):
        x = pad + i * bw + bw * 0.15
        y = pad + ((top - max(v, 0)) / span) * (base_h - pad)
        bh = abs(v) / span * (base_h - pad)
        parts.append(f'<rect x="{x:.1f}" y="{(y if v >= 0 else zero):.1f}" '
                     f'width="{bw * 0.7:.1f}" height="{max(bh, 1):.1f}" class="bar">'
                     f'<title>{html.escape(label)}: {v:g}</title></rect>')
        parts.append(f'<text x="{x + bw * 0.35:.1f}" y="{h - 4}" text-anchor="middle" '
                     f'class="axis">{html.escape(label)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" class="tchart" role="img" '
            f'aria-label="Werte je Zeitraum">{"".join(parts)}</svg>')


RANGES = (("24h", "24 Stunden", 86400), ("7d", "7 Tage", 7 * 86400),
          ("30d", "30 Tage", 30 * 86400), ("1y", "1 Jahr", 365 * 86400))


def range_seconds(key):
    return next((sec for k, _l, sec in RANGES if k == key), RANGES[0][2])


def svg_line(buckets, frm, to, w=520, h=110):
    """A line of a number series' bucket means (min..max as a band) between
    `frm` and `to` (epoch seconds) -- '' without points. Inline SVG."""
    pts = []
    for b in buckets or []:
        try:
            t = _dt(b["t"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if b.get("mean") is None:
            continue
        pts.append((t, float(b["mean"]), float(b.get("min", b["mean"])),
                    float(b.get("max", b["mean"]))))
    if not pts:
        return ""
    lo = min(p[2] for p in pts)
    hi = max(p[3] for p in pts)
    span = (hi - lo) or 1.0
    pad = 14

    def x(t):
        return pad + (t - frm) / max(to - frm, 1) * (w - 2 * pad)

    def y(v):
        return pad + (hi - v) / span * (h - 2 * pad)
    line = " ".join(f"{x(t):.1f},{y(m):.1f}" for t, m, _a, _b in pts)
    band = (" ".join(f"{x(t):.1f},{y(b):.1f}" for t, _m, _a, b in pts) + " "
            + " ".join(f"{x(t):.1f},{y(a):.1f}" for t, _m, a, _b in reversed(pts)))
    return (f'<svg viewBox="0 0 {w} {h}" class="tchart" role="img" aria-label="Verlauf">'
            f'<polygon points="{band}" class="band"/>'
            f'<polyline points="{line}" class="line" fill="none"/>'
            f'<text x="{pad}" y="{pad - 3}" class="axis">{hi:g}</text>'
            f'<text x="{pad}" y="{h - 2}" class="axis">{lo:g}</text></svg>')
