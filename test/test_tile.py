#!/usr/bin/env python3
"""Who gets a launchpad tile (RFC-0012 §1.2 + §1.3 addendum, spec 2.10).

Two rules, and both are decisions of the RFC rather than of the code:
the app's own manifest decides, and the operator overrides per instance.
`instance_view.py` carries no Flask, so this runs without a request, a
container or a node.

The rule that matters most here is the one that is easy to break by
accident: a hidden tile is NOT access control. Nothing in this file may
start behaving like a permission check.

Run: python3 test/test_tile.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-tile-")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl  # noqa: E402
import instance_view as iv  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {detail}")


def inst(**kw):
    base = {"app_name": "Demo", "version": "1.0.0", "channel": "production"}
    base.update(kw)
    return base


print("=== the app's own class decides ===")
ok("an app with a user interface gets a tile",
   iv.tile_visible(inst(app_class="frontend")))
ok("a background service does not",
   not iv.tile_visible(inst(app_class="service")))

# Every instance installed before this existed carries no class at all.
# Those must keep their tile: an update that silently emptied somebody's
# launchpad would be the worst possible way to ship this.
print("\n=== instances that predate the field ===")
ok("an instance without a class keeps its tile", iv.tile_visible(inst()))
ok("and an unknown class counts as frontend, never as service",
   iv.tile_visible(inst(app_class="kuehlschrank")))
ok("an empty class too", iv.tile_visible(inst(app_class="")))

print("\n=== the operator overrides, per instance ===")
ok("'on' gives a service a tile",
   iv.tile_visible(inst(app_class="service", tile="on")))
ok("'off' takes one away from a frontend",
   not iv.tile_visible(inst(app_class="frontend", tile="off")))
ok("'auto' is the same as no override at all",
   iv.tile_visible(inst(app_class="frontend", tile="auto"))
   and not iv.tile_visible(inst(app_class="service", tile="auto")))
ok("a mode nobody defined falls back to auto, it does not hide anything",
   iv.tile_mode(inst(tile="vielleicht")) == "auto"
   and iv.tile_visible(inst(app_class="frontend", tile="vielleicht")))

print("\n=== the page has to say WHY, not just what ===")
# An operator hunting a missing app needs the reason. Without it the
# only way to find out is to read the source, which is not a UI.
for case in (inst(app_class="service"), inst(app_class="frontend"),
             inst(app_class="service", tile="on"),
             inst(app_class="frontend", tile="off")):
    reason = iv.tile_reason(case)
    ok(f"class={case.get('app_class')!r} tile={case.get('tile', '-')!r} "
       f"is explained in a sentence", len(reason) > 30 and reason.endswith("."),
       reason)
ok("an explicit setting says so, so nobody blames the app",
   "ausdrücklich" in iv.tile_reason(inst(app_class="service", tile="on")))

# Found on oaap-test, 2026-08-09: every instance on the whole fleet
# predates manifest 0.2, and both the CLI and the page told each of them
# "the app declares itself frontend" — about apps that declare nothing.
# A small untruth on an admin page costs somebody an hour later.
ok("an app that declared nothing is not credited with having declared",
   "keine Angabe" in iv.tile_reason(inst())
   and "declares no class" in appctl.class_phrase(inst()),
   f"{iv.tile_reason(inst())} / {appctl.class_phrase(inst())}")
ok("...and one that did declare is quoted as declaring",
   "bezeichnet sich selbst" in iv.tile_reason(inst(app_class="frontend"))
   and "declares itself 'service'" in appctl.class_phrase(inst(app_class="service")))
# Drei Fälle, nicht zwei — gefunden auf dem Raspi, 2026-08-09: Die
# Installation schrieb den NORMALISIERTEN Wert in die Registry, also
# stand dort 'frontend' für eine App, die nichts erklärt hatte. Damit
# war der Unterschied für jeden Leser für immer weg.
ok("ein unbekannter Wert wird als unbekannt gemeldet, nicht als Erklärung",
   "kennt diese Plattform nicht" in iv.tile_reason(inst(app_class="kuehlschrank"))
   and "does not know" in appctl.class_phrase(inst(app_class="kuehlschrank")),
   f"{iv.tile_reason(inst(app_class='kuehlschrank'))}")
ok("...aber die Kachel bleibt, weil unbekannt als frontend zählt",
   iv.tile_visible(inst(app_class="kuehlschrank"))
   and appctl.tile_visible(inst(app_class="kuehlschrank")))
ok("die Installation schreibt VERBATIM, was das Manifest sagte",
   appctl.declared_class({"class": "service"}) == "service"
   and appctl.declared_class({}) == ""
   and appctl.declared_class({"class": "kuehlschrank"}) == "kuehlschrank",
   "sonst ist 'erklärt frontend' von 'erklärt nichts' nicht mehr zu "
   "unterscheiden")
ok("...während das Verhalten weiterhin normalisiert entscheidet",
   appctl.app_class_of({"class": "kuehlschrank"}) == "frontend"
   and appctl.instance_class({"app_class": ""}) == "frontend"
   and appctl.instance_class({"app_class": "service"}) == "service")
ok("and the override text names the class without repeating itself",
   iv.tile_reason(inst(tile="off")).count("Kachel") == 1,
   iv.tile_reason(inst(tile="off")))

print("\n=== the launchpad can report what it is hiding ===")
# A node running only background services would otherwise have a
# launchpad indistinguishable from a broken one (portal spec 2.2).
registry = {"kuma": inst(app_class="frontend"),
            "ollama": inst(app_class="service"),
            "hub": inst(app_class="service"),
            "alt": inst()}
ok("exactly the tileless ones are named",
   iv.hidden_instances(registry) == ["hub", "ollama"],
   str(iv.hidden_instances(registry)))
ok("a node full of frontends hides nothing",
   iv.hidden_instances({"a": inst(app_class="frontend")}) == [])

print("\n=== host and portal answer identically ===")
# The rule exists twice — the CLI on the host, the portal in a container
# that cannot import appctl. Two copies drift; this is what catches it.
for case in (inst(), inst(app_class="frontend"), inst(app_class="service"),
             inst(app_class="service", tile="on"),
             inst(app_class="frontend", tile="off"),
             inst(app_class="service", tile="auto"),
             inst(app_class="unbekannt", tile="quatsch")):
    ok(f"same answer for {case.get('app_class', '-')}/{case.get('tile', '-')}",
       appctl.tile_visible(case) == iv.tile_visible(case)
       and appctl.tile_mode_of(case) == iv.tile_mode(case),
       f"appctl={appctl.tile_visible(case)} portal={iv.tile_visible(case)}")

print("\n=== hiding a tile is not access control ===")
# Whatever the tile says, the instance keeps everything the gateway
# enforces on. If this ever fails, someone has confused display with
# permission — the exact mistake visibility groups (RFC-0007) exist for.
guarded = inst(app_class="service", tile="off",
               roles=["admin"], visibility={"groups": ["buero"]},
               routes=[{"path": "/", "roles": ["admin"]}], port=8101)
before = dict(guarded)
iv.tile_visible(guarded)
iv.tile_reason(guarded)
ok("deciding about the tile changes nothing about the instance",
   guarded == before)
ok("roles, visibility, routes and port are untouched by the rule",
   guarded["roles"] == ["admin"]
   and guarded["visibility"] == {"groups": ["buero"]}
   and guarded["routes"][0]["roles"] == ["admin"]
   and guarded["port"] == 8101)


# --------------------------------------------------------------------------
# From here on the real appctl runs against a throwaway data directory:
# the CLI a technician types, and the queued path the portal uses. Not
# covered here because it needs Docker: that the override survives a
# redeploy while the CLASS is re-read from the new manifest. That one is
# proven on a real node.

def run(fn, *a):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fn(*a)
        return True, buf.getvalue()
    except SystemExit:
        return False, buf.getvalue()


class Args:
    def __init__(self, **kw):
        self.name = None
        self.mode = ""
        self.__dict__.update(kw)


def registry():
    return appctl.load_registry()["instances"]


os.makedirs(appctl.APPS_DIR, exist_ok=True)
appctl.save_registry({"instances": {
    "ollama": inst(app_class="service", port=8101),
    "kuma": inst(app_class="frontend", port=8102),
}})

print("\n=== oaap app tile (the technician's path) ===")
okr, out = run(appctl.cmd_tile, Args(name="ollama"))
ok("asking says what it is and why", okr and "not shown" in out
   and "service" in out, out)
okr, out = run(appctl.cmd_tile, Args(name="ollama", mode="on"))
ok("switching it on is recorded", okr and registry()["ollama"]["tile"] == "on",
   out)
ok("and the launchpad would now show it",
   iv.tile_visible(registry()["ollama"]))
okr, out = run(appctl.cmd_tile, Args(name="ollama", mode="auto"))
ok("back to auto REMOVES the override rather than storing 'auto'",
   okr and "tile" not in registry()["ollama"], str(registry()["ollama"]))
ok("and it is hidden again", not iv.tile_visible(registry()["ollama"]))
okr, out = run(appctl.cmd_tile, Args(name="kuma", mode="off"))
ok("a frontend can be hidden too", okr and not iv.tile_visible(registry()["kuma"]))
ok("every answer repeats that this is not a way to keep people out",
   "visibility" in out, out)
okr, out = run(appctl.cmd_tile, Args(name="gibtsnicht", mode="on"))
ok("an unknown instance is refused", not okr, out)
# Found on oaap-test, 2026-08-09: argparse `choices` validated the
# DEFAULT of the omitted mode on Python 3.13 and refused `oaap app tile
# <name>` outright, while 3.14 accepted it. The check belongs in here,
# where it behaves the same on every node in the fleet.
okr, out = run(appctl.cmd_tile, Args(name="kuma", mode="unsichtbar"))
ok("a mode nobody defined is refused by the command, not by argparse",
   not okr and "auto | on | off" in out, out)
okr, out = run(appctl.cmd_tile, Args(name="kuma"))
ok("...and asking without a mode still works", okr, out)

print("\n=== the queued path the portal uses ===")
# The portal cannot write the registry — its mount is read-only — so it
# drops a request in the spool and the host applies it. The host
# re-checks the mode: the spool is data, not trust.
QUEUE = os.path.join(appctl.SPOOL_DIR, "queue")


def queue(req):
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{req['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(req, f)
    run(appctl.cmd_process_deploys, None)
    with open(os.path.join(appctl.SPOOL_DIR, "results", f"{req['id']}.json"),
              encoding="utf-8") as f:
        return json.load(f)


res = queue({"id": "t1", "instance": "ollama", "action": "tile", "mode": "on",
             "by": "joerg"})
ok("the portal can switch a tile on",
   res["ok"] and registry()["ollama"]["tile"] == "on", str(res))
res = queue({"id": "t2", "instance": "ollama", "action": "tile",
             "mode": "auto", "by": "joerg"})
ok("...and back to auto", res["ok"] and "tile" not in registry()["ollama"],
   str(res))
res = queue({"id": "t3", "instance": "ollama", "action": "tile",
             "mode": "unsichtbar", "by": "joerg"})
ok("a mode the host does not know is refused, not stored",
   not res["ok"] and "tile" not in registry()["ollama"], str(res))
res = queue({"id": "t4", "instance": "gibtsnicht", "action": "tile",
             "mode": "off", "by": "joerg"})
ok("and so is a request for an instance that does not exist",
   not res["ok"], str(res))
ok("the change is in the deploy log like every other portal action",
   json.loads(open(appctl.DEPLOY_LOG, encoding="utf-8")
              .read().strip().splitlines()[0]).get("via") == "portal")

print("\n=== benutzt das Portal nur, was es auch importiert? ===")
# Gefunden auf oaap-test, 2026-08-09: app.py rief `iv.tile_visible(...)`
# auf, ohne `instance_view` zu importieren. `py_compile` sieht das nicht
# (es ist ein NameError zur Laufzeit, kein Syntaxfehler), Flask ist hier
# nicht installiert, also laesst sich app.py auch nicht einfach
# importieren — und so ging es bis auf den Knoten durch, wo das
# Launchpad mit 500 antwortete. Diese Pruefung ist bewusst grob: Sie
# sammelt JEDEN Namen, der irgendwo im Modul gebunden wird, und meldet
# nur die, die nirgends herkommen.
import ast  # noqa: E402
import builtins  # noqa: E402

PORTAL = os.path.join(HERE, "..", "platform", "services", "portal")
for fn in sorted(f for f in os.listdir(PORTAL) if f.endswith(".py")):
    tree = ast.parse(open(os.path.join(PORTAL, fn), encoding="utf-8").read())
    bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args:
                for a in (args.posonlyargs + args.args + args.kwonlyargs
                          + [args.vararg, args.kwarg]):
                    if a:
                        bound.add(a.arg)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    used = {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    missing = sorted(used - bound)
    ok(f"{fn}: jeder benutzte Modulname ist auch importiert",
       not missing, f"nirgends gebunden: {missing}")

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
