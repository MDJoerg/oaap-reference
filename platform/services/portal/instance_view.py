"""What the portal shows about one installed instance.

Two things live here: which instances belong on the launchpad (runtime
spec 2.10), and how the instance object page is cut into sections
(design guidelines 6.2.1/6.2.2).

Launchpad rule below; the sections rule in one sentence: **an object
page with more than three cards gets a reading head and tabs**, and the
server — not JavaScript — decides which tab is open.

Like `store_view.py`, this carries no Flask: these are RFC and design
decisions (RFC-0012 §1.2 and its §1.3 addendum) and should be readable
and testable without a request, a container or a node.

The launchpad rule in one sentence: **the app's own manifest decides,
the operator overrides.** An app that declares itself a background `service` gets no
tile, because a tile leading to a machine interface serves nobody. The
class comes from the manifest the node installed — never from a store
list, which may be disabled, unreachable, or written by a stranger.

What this is NOT is access control. A hidden tile changes nothing about
the instance's routes, roles or URL, and the gateway keeps enforcing
them on every request. Hiding an app from a person is what visibility
groups (RFC-0007) are for. Anything here that starts to feel like a
permission check is a bug.
"""

TILE_MODES = ("auto", "on", "off")
DEFAULT_APP_CLASS = "frontend"

# German, because it is rendered (design guidelines: German reference UI)
MODE_LABEL = {
    "auto": "Automatisch",
    "on": "Immer zeigen",
    "off": "Nie zeigen",
}
CLASS_LABEL = {
    "frontend": "App mit Oberfläche",
    "service": "Hintergrunddienst",
}


def app_class(inst):
    """The instance's declared class, normalised.

    Instances installed before this existed carry no class at all, and
    an unknown value is treated like a missing one — both mean
    `frontend`. That is the safe direction: a tile too many is untidy,
    a missing tile hides a working app from the person who installed it.
    """
    value = str(inst.get("app_class") or "").strip()
    return value if value in CLASS_LABEL else DEFAULT_APP_CLASS


def tile_mode(inst):
    """The operator's override; absent means "follow the app"."""
    mode = str(inst.get("tile") or "").strip()
    return mode if mode in TILE_MODES else "auto"


def class_phrase(inst):
    """How to talk about the class without overclaiming.

    Every app installed before manifest 0.2 declares nothing at all, and
    that is most of the fleet today. "Die App bezeichnet sich selbst
    als …" would be a small untruth about all of them, and small
    untruths on an admin page cost somebody an hour later.
    """
    declared = str(inst.get("app_class") or "").strip()
    if declared in CLASS_LABEL:
        return f"Die App bezeichnet sich selbst als {CLASS_LABEL[declared]}."
    if declared:
        return (f"Die App bezeichnet sich als „{declared}“ — das kennt diese "
                f"Plattform nicht, sie gilt deshalb als "
                f"{CLASS_LABEL[DEFAULT_APP_CLASS]}.")
    return ("Die App macht dazu keine Angabe und gilt deshalb als "
            f"{CLASS_LABEL[DEFAULT_APP_CLASS]}.")


def tile_visible(inst):
    """Does this instance get a launchpad tile?"""
    mode = tile_mode(inst)
    if mode != "auto":
        return mode == "on"
    return app_class(inst) != "service"


def tile_reason(inst):
    """One sentence for the instance page: why it is (not) on the launchpad.

    An operator looking for a missing app needs the reason, not just the
    state — otherwise the only way to find out is to read the source.
    """
    mode, shown = tile_mode(inst), tile_visible(inst)
    if mode != "auto":
        state = "eingeschaltet" if shown else "abgeschaltet"
        return (f"Die Kachel ist ausdrücklich {state}, unabhängig davon, was "
                f"die App über sich sagt. {class_phrase(inst)}")
    if not shown:
        return ("Die App bezeichnet sich selbst als Hintergrunddienst — sie "
                "wird von anderer Software benutzt, nicht von einem Menschen "
                "— und bekommt deshalb keine Kachel.")
    if str(inst.get("app_class") or "").strip() in CLASS_LABEL:
        return ("Die App bezeichnet sich selbst als Anwendung mit Oberfläche "
                "und erscheint deshalb im Launchpad.")
    return class_phrase(inst) + " Sie erscheint im Launchpad."


# --------------------------------------------------------------------
# Sections of the instance object page (design guidelines 6.2.2)

# Order is deliberate: read first ("Überblick" carries no form, so
# nobody lands in one), then the daily business, and the single
# irreversible action last and alone.
TABS = (
    ("ueberblick", "Überblick"),
    ("zugang", "Zugang"),
    ("netz", "Netz & Adressen"),
    ("deployment", "Deployment"),
    ("konfiguration", "Konfiguration"),
    ("verwaltung", "Verwaltung"),
)
DEFAULT_TAB = TABS[0][0]
TAB_KEYS = tuple(k for k, _ in TABS)


def valid_tab(raw, default=""):
    """The requested section, or the fallback.

    An unknown value is not an error worth a message — the tab comes
    from a link or a hidden field, so a wrong one means a stale
    bookmark, not a wrong decision. Falling back to the reading tab is
    the honest answer. The empty default means "no tab in the URL",
    which is what a redirect after a save uses.
    """
    tab = str(raw or "").strip()
    return tab if tab in TAB_KEYS else default


SOURCE_LABELS = {
    "git": "Git-Repository",
    "artifact": "Hochgeladenes Paket (ZIP)",
    "local": "Lokaler Pfad auf dem Knoten",
    "store": "Store-Eintrag",
}


def source_view(inst):
    """Where this instance's code came from: a label for the head and a
    few lines for the overview.

    An unknown kind is named as unknown rather than guessed, and an
    instance installed before the platform recorded its origin says so —
    „unbekannt" is a fact, an invented Git URL would be a lie.
    """
    src = inst.get("source") or {}
    kind = str(src.get("kind") or "")
    label = SOURCE_LABELS.get(kind, "Unbekannte Herkunft")
    lines = []
    if kind == "git":
        lines.append(f"Repository {src.get('url', '?')}")
        if src.get("path"):
            lines.append(f"Pfad im Repository: {src['path']}")
        lines.append(f"Branch oder Tag: {src.get('ref') or 'Standardbranch'}")
    elif kind == "artifact":
        if src.get("promoted_from"):
            # RFC-0020: „was läuft hier?" wird mit einem Teststand und
            # einer Prüfsumme beantwortet, nicht mit einer Versionshoffnung
            label = f"Aus dem Teststand „{src['promoted_from']}“ übernommen"
        lines.append(f"Version {src.get('version', '?')} "
                     f"aus {src.get('stored', '?')}")
        if src.get("received"):
            lines.append("Empfangen "
                         + src["received"].replace("T", " ").rstrip("Z"))
        if src.get("sha256"):
            lines.append(f"Prüfsumme {src['sha256'][:16]}…")
    elif kind == "local":
        lines.append(f"Verzeichnis {src.get('url', '?')}")
    elif not kind:
        lines.append("Diese Instanz wurde installiert, bevor die Plattform "
                     "die Herkunft festgehalten hat.")
    return label, lines


def route_rows(inst):
    """The app's routes with their roles, in words instead of JSON.

    `public` wins over everything else on a route: if one role is
    "no login at all", naming the others next to it would read as a
    restriction that does not exist.
    """
    rows = []
    for r in inst.get("routes") or []:
        roles = r.get("roles") or []
        if "public" in roles:
            who = "ohne Anmeldung (öffentlich)"
        elif roles:
            who = ", ".join(roles)
        else:
            who = "jede angemeldete Person"
        rows.append({"path": r.get("path", "/"), "who": who})
    return rows


def visibility_label(inst):
    """Who may see this instance, for the head (RFC-0007)."""
    groups = (inst.get("visibility") or {}).get("groups") or []
    return ("Gruppen " + ", ".join(groups)) if groups else "alle mit passender Rolle"


def hidden_instances(instances):
    """Names of instances that exist but carry no tile, sorted.

    The launchpad shows a `server_admin` how many there are. Without it,
    a node running only background services has a launchpad that is
    indistinguishable from a broken one (portal spec 2.2).
    """
    return sorted(name for name, inst in instances.items()
                  if not tile_visible(inst))


# --------------------------------------------------- mehrzeilige Konfiguration
#
# Manche Konfigurationswerte sind Listen: die Knoten von FleetView, die
# Bezugsquellen und Aliasse des KI-Gateways. Bis 0.1.48 bot das Portal
# dafür ein einzeiliges Eingabefeld an, und beide Apps erfanden
# unabhängig voneinander dieselbe Notlösung — Einträge mit ';' trennen.
# Zweimal dieselbe Notlösung ist das Zeichen, dass sie in die Plattform
# gehört.
#
# Warum die Zeilen NICHT als Zeilen gespeichert werden: Die Werte einer
# Instanz liegen in `instance.env` und gehen als `--env-file` an Docker.
# Beides ist zeilenweise — ein Zeilenumbruch im Wert zerreißt die Datei
# und würde beim nächsten Lesen still die halbe Konfiguration
# verschlucken. Deshalb bleibt die Übertragung einzeilig: Das Portal
# zeigt Zeilen, gespeichert wird die mit ';' verbundene Form. Apps, die
# heute schon an ';' und Zeilenumbruch trennen, ändern sich nicht.
LIST_SEPARATOR = ";"


def value_to_lines(value):
    """Gespeicherte Listenform -> was im Textfeld steht (eine Zeile je Eintrag)."""
    return "\n".join(part.strip() for part in (value or "").split(LIST_SEPARATOR)
                     if part.strip())


def lines_to_value(text):
    """Textfeld -> gespeicherte Listenform. Gibt (wert, fehler) zurück.

    Ein Eintrag, der selbst ein ';' enthält, wird **abgelehnt statt
    zerschnitten**: Stillschweigend zu zerteilen hieße, eine Adresse
    oder einen Schlüssel zu zerstören und den Anwender raten zu lassen,
    warum die App nichts mehr findet.
    """
    lines = [l.strip() for l in (text or "").replace("\r", "").split("\n")]
    lines = [l for l in lines if l]
    bad = [l for l in lines if LIST_SEPARATOR in l]
    if bad:
        return "", (f"Ein Eintrag darf kein '{LIST_SEPARATOR}' enthalten — "
                    f"damit werden die Einträge getrennt. Betroffen: {bad[0]!r}")
    return LIST_SEPARATOR.join(lines), ""
