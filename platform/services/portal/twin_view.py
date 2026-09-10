"""Twin browser rules (RFC-0031 Bauplan Schritt 5) — no web framework,
no HTTP call: everything here is a decision about WHAT to show, given
data `app.py` already fetched from the twin service's `/internal/*`
API. Read `store_view.py`'s own docstring for why this split exists —
the same reason applies here, unchanged.

`app.py` supplies the I/O (calling twin, holding the person's session)
and renders the result.
"""
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
        attrs.append({"key": key, "title": title, "value_type": vt})
    if not attrs:
        return None, "Mindestens ein Attribut ist nötig."
    return {"on": on_type, "group_key": group_key,
           "group_title": group_title or group_key, "attributes": attrs}, None
