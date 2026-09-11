#!/usr/bin/env python3
"""Launchpad hints: `launchpad.group` and `launchpad.embeddable` (RFC-0036).

Two developer hints about how a tile is DISPLAYED, never about who may
see it (RFC-0007 stays the access-control layer, unchanged). This file
defends what is provable without Docker or a running node:

    validate_manifest accepts and rejects the new section correctly,
    and manifest 0.4 exists.
    _install_from_dir writes it into the registry from the manifest,
    re-read on every install like app_class -- checked at the SOURCE,
    the same technique test_data_model.py uses for _install_from_dir,
    because actually running it needs Docker.
    grouped_tiles (instance_view.py, no Flask) buckets tiles by label
    correctly: ungrouped first with no heading, labelled sections
    sorted, order within a section preserved.
    The portal's dashboard route wires launchpad_tiles into
    grouped_tiles, and its template renders a heading only for a
    labelled section -- an unlabelled one must render exactly as
    before this field existed.
    The self-service profile route touches display_name and nothing
    else -- no roles/groups/tenant/active/username field.

Run: python3 test/test_launchpad_hints.py
"""
import ast
import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl  # noqa: E402
import instance_view as iv  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


BASE = {
    "oaap_manifest": "0.1",
    "app": {"id": "demo-app", "name": "Demo", "version": "1.0.0",
            "type": "image"},
    "services": {"web": {"image": "nginx:1", "port": 80}},
    "routes": [{"path": "/", "roles": ["user"]}],
    "health": {"path": "/healthz"},
}


def manifest(**over):
    doc = json.loads(json.dumps(BASE))
    doc.update(over)
    return doc


def validate(doc):
    """(accepted, output) — validate_manifest reports by dying."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            appctl.validate_manifest(doc)
        return True, buf.getvalue()
    except SystemExit:
        return False, buf.getvalue()


print("=== manifest 0.4 exists, additive like 0.2/0.3 before it ===")
ok("this platform's own manifest minor is at least 4",
   appctl.MANIFEST_MINOR >= 4, appctl.MANIFEST_MINOR)

print("\n=== validate_manifest: launchpad is optional and additive ===")
okr, out = validate(manifest())
ok("a manifest with no launchpad section installs, as always", okr, out)

okr, out = validate(manifest(launchpad={"group": "Verwaltung"}))
ok("a manifest declaring a group label installs", okr, out)

okr, out = validate(manifest(launchpad={"embeddable": True}))
ok("a manifest declaring embeddable installs", okr, out)

okr, out = validate(manifest(launchpad={"group": "Verwaltung",
                                        "embeddable": False}))
ok("both fields together install", okr, out)

okr, out = validate(manifest(launchpad="Verwaltung"))
ok("launchpad must be an object, not a bare string",
   not okr and "launchpad: object expected" in out, out)

okr, out = validate(manifest(launchpad={"group": "x" * 41}))
ok("a group label over 40 characters is refused",
   not okr and "launchpad.group" in out, out)

okr, out = validate(manifest(launchpad={"group": 42}))
ok("a group label that is not a string is refused",
   not okr and "launchpad.group" in out, out)

okr, out = validate(manifest(launchpad={"embeddable": "ja"}))
ok("embeddable must be a boolean, not a string",
   not okr and "launchpad.embeddable" in out, out)

okr, out = validate(manifest(launchpad={}))
ok("an empty launchpad object is fine (both fields are optional)", okr, out)

print("\n=== not a must_understand feature (RFC-0036: display only) ===")
ok("must_understand stays empty for launchpad -- a node that ignores it "
   "shows the tile without a heading, not a broken install",
   "launchpad" not in appctl.MANIFEST_FEATURES)


print("\n=== _install_from_dir writes launchpad into the registry, re-read "
      "from the manifest like app_class (source check -- running this "
      "needs Docker, proven live on oaap-test) ===")
appctl_src = open(os.path.join(HERE, "..", "platform", "appctl.py"),
                  encoding="utf-8").read()
install_body = appctl_src.split("def _install_from_dir")[1].split("\ndef ")[0]
ok("the registry row carries a 'launchpad' key",
   '"launchpad":' in install_body)
ok("group is read from the manifest section, not the operator override "
   "(that stays 'visibility', 'tile' -- separate keys)",
   'm.get("launchpad") or {}).get("group")' in install_body)
ok("embeddable is read the same way, coerced to a real boolean",
   'bool((m.get("launchpad") or {}).get("embeddable"))' in install_body)
# app_class is the field this one is modelled on -- re-read every
# install, never carried over from the previous `inst`. Same rule here:
# there must be no `inst.get("launchpad")` fallback anywhere near it.
launchpad_line = next(l for l in install_body.splitlines()
                      if '"launchpad": {"group"' in l)
ok("unlike overrides such as 'tile', launchpad is never read from the "
   "PREVIOUS instance -- it describes the app, not an operator decision",
   "inst.get(\"launchpad\")" not in launchpad_line, launchpad_line)


print("\n=== grouped_tiles (instance_view.py, no Flask) ===")


def tile(name, group=""):
    return {"name": name, "group": group}


ok("no tiles -> no sections at all",
   iv.grouped_tiles([]) == [])
ok("tiles with no group land in one unlabelled, headingless section",
   iv.grouped_tiles([tile("a"), tile("b")])
   == [("", [tile("a"), tile("b")])])
ok("a single labelled group gets its own section, sorted after the "
   "unlabelled one",
   iv.grouped_tiles([tile("a"), tile("b", "Verwaltung")])
   == [("", [tile("a")]), ("Verwaltung", [tile("b", "Verwaltung")])])
ok("two tiles sharing a label share one section",
   iv.grouped_tiles([tile("a", "X"), tile("b", "X")])
   == [("X", [tile("a", "X"), tile("b", "X")])])
ok("sections are sorted by label",
   [g for g, _ in iv.grouped_tiles(
       [tile("a", "Werkzeuge"), tile("b", "Verwaltung")])]
   == ["Verwaltung", "Werkzeuge"])
ok("when every tile is labelled, there is no unlabelled section at all "
   "(not an empty one)",
   iv.grouped_tiles([tile("a", "X")]) == [("X", [tile("a", "X")])])
ok("order WITHIN a section is preserved, not re-sorted",
   iv.grouped_tiles([tile("z", "X"), tile("a", "X")])
   == [("X", [tile("z", "X"), tile("a", "X")])])


print("\n=== the portal wires launchpad_tiles into grouped_tiles "
      "(source check, mirrors test_data_twin's page()-wiring regression) ===")
PORTAL_APP = os.path.join(HERE, "..", "platform", "services", "portal",
                          "app.py")
portal_src = open(PORTAL_APP, encoding="utf-8").read()
tree = ast.parse(portal_src)
dashboard = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "dashboard")
dashboard_body = ast.get_source_segment(portal_src, dashboard)
ok("dashboard() passes grouped, not flat, tiles to the template",
   "iv.grouped_tiles(tiles)" in dashboard_body, dashboard_body)
ok("the DASHBOARD_BODY template iterates 'sections', not a flat 'tiles' "
   "variable -- an unlabelled section renders no <h2>, so a plain app "
   "looks exactly as it did before this field existed",
   "{% for label, tiles in sections %}" in portal_src
   and '{% if label %}<h2 class="section">' in portal_src)

print("\n=== launchpad_tiles() carries the manifest hint per tile, "
      "the same offline-answer reason as app_class ===")
lt = next(n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "launchpad_tiles")
lt_body = ast.get_source_segment(portal_src, lt)
ok("each tile's group comes from the INSTANCE registry, not a store list",
   '(inst.get("launchpad") or {}).get("group")' in lt_body, lt_body)


print("\n=== self-service display name touches nothing else (RFC-0036 D3) "
      "===")
IDENTITY_APP = os.path.join(HERE, "..", "platform", "services", "identity",
                            "app.py")
identity_src = open(IDENTITY_APP, encoding="utf-8").read()
itree = ast.parse(identity_src)


def identity_fn(name):
    node = next((n for n in ast.walk(itree)
                if isinstance(n, ast.FunctionDef) and n.name == name), None)
    return ast.get_source_segment(identity_src, node) if node else ""


profile_form = identity_fn("profile_form")
profile_change = identity_fn("profile_change")
ok("GET /auth/profile requires a session, like /auth/password",
   "session_username()" in profile_form and "/auth/login" in profile_form,
   profile_form)
ok("POST /auth/profile writes display_name and nothing else -- no roles, "
   "groups, tenant, active or username field is ASSIGNED in this route "
   "(reading u[\"active\"] to refuse a deactivated caller is fine)",
   'u["display_name"] =' in profile_change
   and 'u["roles"] =' not in profile_change
   and 'u["groups"] =' not in profile_change
   and 'u["tenant"] =' not in profile_change
   and 'u["active"] =' not in profile_change
   and 'u["username"] =' not in profile_change,
   profile_change)
ok("an over-long display name is refused with no change made",
   "len(name) > 80" in profile_change
   and profile_change.index("len(name) > 80")
   < profile_change.index('u["display_name"] ='),
   profile_change)


def routes():
    """{route path: function name} for every @app.get/@app.post."""
    found = {}
    for node in itree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("get", "post")
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "app"
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                found.setdefault((dec.args[0].value, dec.func.attr), node.name)
    return found


R = routes()
ok("GET /auth/profile is wired to profile_form",
   R.get(("/auth/profile", "get")) == "profile_form", R)
ok("POST /auth/profile is wired to profile_change",
   R.get(("/auth/profile", "post")) == "profile_change", R)

print("\n=== the portal header links to it, next to Passwort ===")
portal_header = portal_src.split('<header class="oaap">')[1].split(
    "</header>")[0]
ok("a 'Profil' link to /auth/profile sits in the header",
   'href="/auth/profile"' in portal_header, portal_header)


print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
