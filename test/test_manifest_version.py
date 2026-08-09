#!/usr/bin/env python3
"""Manifest version tolerance and `must_understand` (RFC-0012 §8.2).

The rule under test: a node reads a manifest whose MINOR is newer than
itself and ignores what it does not know; it refuses only a foreign
MAJOR, or a manifest that declares a feature the node has to understand
and does not. Without it, the first manifest 0.2 we publish would be
rejected by every node already in the field.

Run: python3 test/test_manifest_version.py
"""
import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl  # noqa: E402

BASE = {
    "oaap_manifest": "0.1",
    "app": {"id": "demo-app", "name": "Demo", "version": "1.0.0",
            "type": "image"},
    "services": {"web": {"image": "nginx:1", "port": 80}},
    "routes": [{"path": "/", "roles": ["user"]}],
    "health": {"path": "/healthz"},
}

fails = 0


def manifest(**over):
    doc = json.loads(json.dumps(BASE))
    doc.update(over)
    return doc


def check(doc):
    """(accepted, output) — validate_manifest reports by dying."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            appctl.validate_manifest(doc)
        return True, buf.getvalue()
    except SystemExit:
        return False, buf.getvalue()


def case(label, doc, want_ok, want_text="", unwanted_text=None):
    global fails
    got_ok, out = check(doc)
    good = (got_ok == want_ok and want_text in out
            and (unwanted_text is None or unwanted_text not in out))
    fails += not good
    print(f"{'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"      accepted={got_ok} (wanted {want_ok}) output={out.strip()!r}")


def with_class(value, version="0.2"):
    doc = manifest(oaap_manifest=version)
    doc["app"]["class"] = value
    return doc


case("today's 0.1 manifest installs", manifest(), True)
case("0.2 is this platform's own version, so no note",
     manifest(oaap_manifest="0.2"), True,
     unwanted_text="newer than this platform")
case("a newer MINOR is read, with a note", manifest(oaap_manifest="0.3"),
     True, "newer than this platform")
case("a much newer MINOR is still read", manifest(oaap_manifest="0.9"),
     True, "newer than this platform")
case("an unknown field is ignored, not refused",
     manifest(depends=["some-data-model"]), True)
case("a foreign MAJOR is refused", manifest(oaap_manifest="1.0"),
     False, "reads 0.x manifests")
case("a version that is not a version is refused",
     manifest(oaap_manifest="zero-one"), False, "MAJOR.MINOR")
case("a missing version is refused", manifest(oaap_manifest=None),
     False, "MAJOR.MINOR")
case("must_understand with an unknown feature is refused",
     manifest(must_understand=["dependencies"]),
     False, "does not understand: dependencies")
case("must_understand bites on a 0.1 manifest too",
     manifest(must_understand=["bundles"]), False, "does not understand")
case("an empty must_understand is fine", manifest(must_understand=[]), True)
case("a malformed must_understand is refused",
     manifest(must_understand="dependencies"), False, "list of feature names")
case("real breakage is still caught", {**manifest(), "routes": []},
     False, "at least one route")

# --- app.class, the first field manifest 0.2 adds (runtime spec 2.10) ---
# Deliberately NOT a must_understand feature: a node that ignores it
# shows one tile too many, which is untidy, not broken. So every case
# here installs — the only question is what the class ends up being.
print()
case("a manifest declaring itself a service installs",
     with_class("service"), True)
case("...and so does one declaring frontend", with_class("frontend"), True)
case("a class this platform never heard of installs anyway",
     with_class("kuehlschrank"), True, "Treating it as 'frontend'")
case("a 0.1 manifest using the 0.2 field is still read (tolerant runtime)",
     with_class("service", version="0.1"), True)


def class_case(label, value, want):
    global fails
    got = appctl.app_class_of({"class": value} if value is not None else {})
    fails += got != want
    print(f"{'PASS' if got == want else 'FAIL'}  {label}")
    if got != want:
        print(f"      got {got!r}, wanted {want!r}")


class_case("'service' is read as declared", "service", "service")
class_case("'frontend' is read as declared", "frontend", "frontend")
class_case("no class at all means frontend", None, "frontend")
class_case("an empty class means frontend", "", "frontend")
# The safe direction, and worth pinning down: a tile too many is untidy,
# a missing tile hides a working app from whoever installed it.
class_case("an unknown class means frontend, never service",
           "kuehlschrank", "frontend")
class_case("and neither does a wrong type sneak through", 42, "frontend")

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
