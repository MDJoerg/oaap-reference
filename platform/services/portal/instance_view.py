"""Which installed instances belong on the launchpad (runtime spec 2.10).

Like `store_view.py`, this carries no Flask: the rule is an RFC decision
(RFC-0012 §1.2 and its §1.3 addendum) and should be readable and
testable without a request, a container or a node.

The rule in one sentence: **the app's own manifest decides, the operator
overrides.** An app that declares itself a background `service` gets no
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
    if mode == "auto":
        if shown:
            return ("Diese App bringt eine Oberfläche mit und erscheint "
                    "deshalb im Launchpad.")
        return ("Diese App ist ein Hintergrunddienst — sie wird von anderer "
                "Software benutzt, nicht von einem Menschen — und bekommt "
                "deshalb keine Kachel.")
    if shown:
        return ("Die Kachel ist ausdrücklich eingeschaltet, unabhängig "
                "davon, was die App über sich sagt.")
    return ("Die Kachel ist ausdrücklich abgeschaltet, unabhängig davon, "
            "was die App über sich sagt.")


def hidden_instances(instances):
    """Names of instances that exist but carry no tile, sorted.

    The launchpad shows a `server_admin` how many there are. Without it,
    a node running only background services has a launchpad that is
    indistinguishable from a broken one (portal spec 2.2).
    """
    return sorted(name for name, inst in instances.items()
                  if not tile_visible(inst))
