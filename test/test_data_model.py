#!/usr/bin/env python3
"""The registry says what a type IS; it never holds one (oaap.data.model 0.1).

RFC-0031 Schritt 2: object/attribute/group/relation/activity types with
an origin (app:/model:/tenant), registered from the manifest's
`data_model` section, bound from `contributes`/`consumes`. This file
defends what is provable without Postgres -- the pure comparison and
matching logic, and the manifest-shape validation -- the same split
`test_data_store.py` uses for `oaap.data.store`:

    The additive/destructive comparison (§2.5) is pure: two Python
    dicts in, a verdict out. No registry needed to prove it refuses a
    removed group member and allows a new one.
    The binding match (§2.4) is pure the same way: a word and a list of
    {key, aliases} in, a candidate list out.
    The declaration sentence (§2.7) is built from the manifest alone,
    by design -- it must show even without a store.
    Without the profile, or without the service running, every model
    action refuses the same two-step way `oaap data store` already
    does -- checked here without Docker, exactly like that file.

What this file CANNOT check, because it would need Postgres: that
`register`/`alias`/`types`/`show`/`bindings` actually read and write
real rows, and that an app install really registers and binds. That
belongs on `oaap-test`, like `oaap.data.store`'s own live findings.

Run: python3 test/test_data_model.py
"""
import os
import sys
import tempfile
from argparse import Namespace

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
DATA = tempfile.mkdtemp(prefix="oaap-data-model-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)

import appctl as m  # noqa: E402

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


def read(name):
    with open(os.path.join(PLATFORM, name), encoding="utf-8") as f:
        return f.read()


def call(fn, **kw):
    import contextlib
    import io
    buf = io.StringIO()
    exited = False
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            fn(Namespace(**kw))
        except SystemExit:
            exited = True
    return exited, buf.getvalue()


print("=== manifest 0.3 admits data_model/contributes/consumes ===")
ok("MANIFEST_MINOR raised to at least 3", m.MANIFEST_MINOR >= 3)

print("\n=== validate_data_model_sections: the RFC-0031 §5 examples ===")
CRM_DATA_MODEL = {
    "object_types": [
        {"key": "Customer", "title": "Kunde", "title_plural": "Kunden",
         "identifying": ["VatId", "Email"]},
        {"key": "Person", "title": "Person"},
    ],
    "attribute_types": [
        {"key": "VatId", "title": "VAT-ID", "value_type": "text"},
        {"key": "Email", "title": "E-Mail", "value_type": "text"},
        {"key": "Location", "title": "Location", "value_type": "text"},
        {"key": "Phone", "title": "Phone", "value_type": "text"},
    ],
    "group_types": [
        {"key": "crm.core", "on": "Customer",
         "attributes": ["Location", "VatId", "Phone"],
         "relations": ["isContactOf"], "activities": ["PhoneCall"]},
    ],
    "relation_types": [
        {"key": "isContactOf", "title": "is contact of",
         "title_inverse": "has contact", "from": "Person", "to": "Customer",
         "valid": True},
    ],
    "activity_types": [
        {"key": "PhoneCall", "title": "Phone call", "is_task": False},
    ],
}
CRM_MANIFEST = {
    "data_model": CRM_DATA_MODEL,
    "contributes": [{"type": "Customer", "role": "owner"},
                     {"type": "Person", "role": "owner"}],
    "consumes": [{"type": "Project", "as": "Projekt", "fields": ["reference"]}],
}
ok("the CRM example (RFC-0031 §5) validates cleanly",
   m.validate_data_model_sections(CRM_MANIFEST) == [])

RACI_MANIFEST = {
    "contributes": [{"type": "Customer", "role": "contributor",
                      "group": "raci.assignments"}],
    "consumes": [{"type": "Customer", "as": "Kunde"},
                 {"type": "Person", "as": "Mitarbeiter",
                  "fields": ["reference", "Email"]}],
}
ok("the RACI example (RFC-0031 §5) validates cleanly",
   m.validate_data_model_sections(RACI_MANIFEST) == [])

print("\n=== validate_data_model_sections: catches what it must ===")
ok("attribute_types needs a real value_type",
   any("value_type" in e for e in m.validate_data_model_sections(
       {"data_model": {"attribute_types": [{"key": "X", "value_type": "money"}]}})))
ok("group_types needs 'on'",
   any("'on'" in e for e in m.validate_data_model_sections(
       {"data_model": {"group_types": [{"key": "crm.core"}]}})))
ok("group_types key must be dotted-lowercase, unlike other kinds",
   any("crm.core" in e or "dotted" in e for e in m.validate_data_model_sections(
       {"data_model": {"group_types": [{"key": "CrmCore", "on": "Customer"}]}})))
ok("object_types key rejects a dotted (group-shaped) word",
   any("invalid" in e for e in m.validate_data_model_sections(
       {"data_model": {"object_types": [{"key": "crm.core"}]}})))
ok("relation_types needs 'from' and 'to'",
   any("from" in e and "to" in e for e in m.validate_data_model_sections(
       {"data_model": {"relation_types": [{"key": "Foo", "title": "x"}]}})))
ok("contributes needs 'type'",
   any("contributes[0]" in e for e in m.validate_data_model_sections(
       {"contributes": [{"role": "owner"}]})))
ok("contributes role 'contributor' needs a 'group'",
   any("'group'" in e for e in m.validate_data_model_sections(
       {"contributes": [{"type": "Customer", "role": "contributor"}]})))
ok("consumes needs 'type'",
   any("consumes[0]" in e for e in m.validate_data_model_sections(
       {"consumes": [{"as": "Kunde"}]})))
ok("no sections at all is valid -- both are optional",
   m.validate_data_model_sections({}) == [])

print("\n=== type_change_kind: additive vs. destructive (§2.5, D2) ===")
BASE_GROUP = {"key": "crm.core", "on": "Customer",
             "attributes": ["Location", "VatId"], "relations": [], "activities": []}
ok("unchanged stays unchanged", m.type_change_kind(BASE_GROUP, dict(BASE_GROUP)) == "unchanged")
ADD_ATTR = dict(BASE_GROUP, attributes=["Location", "VatId", "Phone"])
ok("a group type GAINING an attribute is additive",
   m.type_change_kind(BASE_GROUP, ADD_ATTR) == "additive")
REMOVE_ATTR = dict(BASE_GROUP, attributes=["Location"])
ok("a group type LOSING an attribute is destructive",
   m.type_change_kind(BASE_GROUP, REMOVE_ATTR) == "destructive")
BASE_ATTR = {"key": "VatId", "title": "VAT-ID", "value_type": "text"}
RETITLED = dict(BASE_ATTR, title="Vat-ID neu")
ok("retitling (presentation only) is additive, not destructive",
   m.type_change_kind(BASE_ATTR, RETITLED) == "additive")
CHANGED_VALUE_TYPE = dict(BASE_ATTR, value_type="int")
ok("changing value_type is destructive",
   m.type_change_kind(BASE_ATTR, CHANGED_VALUE_TYPE) == "destructive")
BASE_REL = {"key": "isContactOf", "from": "Person", "to": "Customer"}
CHANGED_TO = dict(BASE_REL, to="Project")
ok("changing a relation type's 'to' is destructive",
   m.type_change_kind(BASE_REL, CHANGED_TO) == "destructive")

print("\n=== bind_candidates: exact key beats alias, ambiguity is visible ===")
INDEX = [{"key": "Customer", "aliases": ["Kunde", "Account"]},
        {"key": "Account", "aliases": ["Kunde"]}]
ok("an exact key match wins outright, ignoring that the same word is "
   "ALSO an alias elsewhere ('Account' is Customer's alias too)",
   m.bind_candidates("Account", INDEX) == ["Account"])
ok("a case-insensitive key match also wins outright",
   m.bind_candidates("customer", INDEX) == ["Customer"])
ok("an alias claimed by two DIFFERENT types (neither is the word itself) "
   "is reported as BOTH candidates, never guessed",
   sorted(m.bind_candidates("Kunde", INDEX)) == ["Account", "Customer"])
ok("no match at all is an empty list, not an exception",
   m.bind_candidates("Nonexistent", INDEX) == [])

print("\n=== declaration_text: the sentence shown before install (D7) ===")
ok("built from the manifest alone -- CRM example",
   m.declaration_text(CRM_MANIFEST["contributes"], CRM_MANIFEST["consumes"])
   == "this app reads Project and writes into Customer and Person.")
ok("the RACI example names the contributor's group",
   m.declaration_text(RACI_MANIFEST["contributes"], RACI_MANIFEST["consumes"])
   == "this app reads Customer and Person and writes into Customer "
      "(contributor: raci.assignments).")
ok("neither section present -- empty, not a sentence about nothing",
   m.declaration_text([], []) == "")
ok("only consumes -- no dangling 'and'",
   m.declaration_text([], [{"type": "Project"}]) == "this app reads Project.")

print("\n=== the manifest 0.3 sections are documented as tolerant, not must_understand ===")
ok("'data_model'/'contributes'/'consumes' are not in MANIFEST_FEATURES -- "
   "an older node ignores them rather than refusing the install",
   not ({"data_model", "contributes", "consumes"} & m.MANIFEST_FEATURES))

print("\n=== 'oaap data model' is registered as an object of the 'data' verb ===")
appctl_src = read("appctl.py")
ok("'model' is a choice alongside 'store'",
   'choices=["store", "model"]' in appctl_src)
ok("cmd_data dispatches 'model' to its own function",
   'if args.object == "model":' in appctl_src
   and "return cmd_data_model(args)" in appctl_src)

print("\n=== without the profile: refused, never a raw docker/psql error ===")
exited, out = call(m.cmd_data, object="model", action="types",
                   arg1=None, arg2=None, yes=False, tenant="")
ok("model actions refuse cleanly without the 'store' profile",
   exited and "no profile 'store'" in out, out)

print("\n=== with the profile, service not running: still refused, not a crash ===")
exited, out = call(m.cmd_node, action="add-profile", profile="store")
ok("add-profile store runs through without Docker", not exited, out)
exited, out = call(m.cmd_data, object="model", action="types",
                   arg1=None, arg2=None, yes=False, tenant="")
ok("model actions refuse cleanly when the service is not running",
   exited and "not running" in out, out)
call(m.cmd_node, action="remove-profile", profile="store")

print("\n=== the install path hooks oaap.data.model at exactly one place ===")
install_body = appctl_src.split("def _install_from_dir")[1].split("\ndef ")[0]
build_marker = install_body.index('print(f"Building')
ok("_install_from_dir prints the D7 sentence before any image is built",
   "declaration_text(" in install_body
   and install_body.index("declaration_text(") < build_marker)
ok("registration happens only when the store actually answers",
   "has_profile(\"store\") and _store_running()" in install_body)
ok("a node without a working store is told plainly, not left to guess",
   "NOT registered" in install_body and "bound (oaap.data.model" in install_body)
ok("--bind overrides are read from the install args, defensively "
   "(the artifact-deploy path builds its own Namespace without one)",
   'getattr(args, "bind", None) or []' in install_body)

print("\n=== 'oaap app install' carries --bind (§2.4's CLI dialog stand-in) ===")
ok("'--bind' is a repeatable install flag",
   '"--bind"' in appctl_src and 'action="append"' in appctl_src)

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
