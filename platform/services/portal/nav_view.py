"""Portal navigation: the side bar and the jump marks.

Design guidelines §5 and 6.2.3 (oaap-design). Two pure functions, no
Flask, so the rules can be tested without a request:

* `nav_groups` -- which entries the side bar shows, in which group.
  Roles decide visibility exactly as the old header row did; this only
  sorts the same entries into groups.
* `jump_sections` -- a long page WITHOUT tabs gets a row of jump marks
  built from its card headings. Built from the RENDERED body, so a
  section a role does not see has no mark either, and a new page gets
  the marks without anyone remembering to add them.
"""

import re

# A page needs this many sections before marks help (the same threshold
# at which an object page gets tabs, guidelines 6.2.2).
MIN_SECTIONS = 4

# (group title, ((active key, href, label, flag name or None), ...)).
# None = everyone who sees the portal at all.
GROUPS = (
    ("Betrieb", (
        ("apps", "/", "Apps", None),
        ("instances", "/instances", "Instanzen", "is_user_admin"),
        ("health", "/health", "Gesundheit", "can_health"),
        ("build", "/aufbau", "Aufbau", "can_build"),
        ("disks", "/datentraeger", "Datenträger", "can_build"),
    )),
    ("Menschen & Zugänge", (
        ("users", "/users", "Benutzer", "is_user_admin"),
        ("keys", "/keys", "Zugänge", "is_user_admin"),
        ("cohorts", "/kohorten", "Kohorten", "has_cohorts"),
        ("tenant", "/tenant", "Mandant", "show_tenant_admin"),
    )),
    ("Katalog & Daten", (
        ("store", "/store", "Store", "can_store"),
        ("twin", "/zwilling", "Zwilling", "can_twin"),
    )),
    ("Kommunikation", (
        ("messages", "/nachrichten", "Nachrichten", "is_user_admin"),
    )),
)


def nav_groups(flags, active):
    """The side bar: groups with at least one visible entry.

    `flags` maps flag names to truthiness; a missing flag is False, so a
    flag nobody computed hides its entry instead of showing it.
    """
    out = []
    for title, items in GROUPS:
        links = [{"key": k, "href": h, "label": label, "active": k == active}
                 for k, h, label, flag in items
                 if flag is None or flags.get(flag)]
        if links:
            out.append({"title": title, "links": links})
    return out


def show_side(groups):
    """A side bar with one entry is a frame around the launchpad: a plain
    user sees only "Apps" and gets no side bar at all."""
    return sum(len(g["links"]) for g in groups) >= 2


_H2 = re.compile(r"<h2(?P<attrs>[^>]*)>(?P<inner>.*?)</h2>", re.S)
_CARD = re.compile(r'<div class="(?P<cls>card[^"]*)"')
_TAG = re.compile(r"<[^>]+>")
_UMLAUT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
           "Ä": "ae", "Ö": "oe", "Ü": "ue"}


def _slug(label):
    s = "".join(_UMLAUT.get(c, c) for c in label).lower()
    s = re.sub(r"&[a-z]+;|&#\d+;", "-", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return "abschnitt-" + (s or "x")


def jump_sections(html):
    """Add ids to the card headings of a long page and put a row of jump
    marks in front of the first such card.

    Returns (html, [(id, label), ...]); the list is empty and the html
    unchanged when the page has tabs or fewer than MIN_SECTIONS sections.

    Left out on purpose:
    * pages with tabs -- one sticky bar per page (guidelines 6.2.3);
    * headings in a warning card -- a decision that is due stands ABOVE
      the navigation, it is not one place among others (6.2.2);
    * `h2.section` -- group titles of the launchpad, not sections;
    * headings that already carry an id.
    """
    if 'class="tabs"' in html:
        return html, []
    cards = [(m.start(), m.group("cls").split()) for m in _CARD.finditer(html)]
    found = []
    for m in _H2.finditer(html):
        attrs = m.group("attrs")
        if "id=" in attrs or "section" in attrs:
            continue
        before = [c for c in cards if c[0] < m.start()]
        if not before or "warn" in before[-1][1]:
            continue
        label = " ".join(_TAG.sub("", m.group("inner")).split())
        if label:
            found.append((m, before[-1][0], label))
    if len(found) < MIN_SECTIONS:
        return html, []

    used, marks = set(), []
    for m, _, label in found:
        base = sid = _slug(label)
        n = 2
        while sid in used:
            sid, n = f"{base}-{n}", n + 1
        used.add(sid)
        marks.append((sid, label))

    # Back to front, so earlier offsets stay valid.
    for (m, _, _), (sid, _) in reversed(list(zip(found, marks))):
        html = html[:m.start()] + f'<h2 id="{sid}"' + html[m.start() + 3:]
    at = found[0][1]
    bar = ('<nav class="jumps" aria-label="Abschnitte">'
           + "".join(f'<a href="#{sid}">{label}</a>' for sid, label in marks)
           + "</nav>\n")
    return html[:at] + bar + html[at:], marks
