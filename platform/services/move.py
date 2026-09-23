"""The move: a club leaving a shared node for one of its own.

RFC-0041 K6, and the complement of a refusal rather than its reversal.
`oaap.data.backup` 2.1.1 produces a tenant archive and deliberately
does NOT promise to restore one into a running node: merging a tenant
into a machine that already has other customers on it means answering,
badly and silently, what to do about an instance that exists now and
did not then, a port somebody else has taken, a name another tenant
has claimed since, a person who is in both.

This file is the other direction, the one that is tractable for
exactly the reason the other one is not: **the target is empty.** The
same insight RFC-0030 had about a rehearsal.

Which makes "empty" the load-bearing word, and so it is not asserted
here -- it is LOOKED FOR, item by item, and every collision found is
named with the thing that collides. A node that is not empty enough is
refused by name rather than merged by hope. That is the whole of the
difference between this and the restore nobody promised.

Three rules came out of building it, and two of them came out of the
machine:

* **Nothing is written before every collision has been looked for.**
  `plan_refusal` here is the sibling of the one in `idp_admin.py`: the
  same shape, a different first question. A half-adopted tenant would
  be a tenant nobody can finish adopting and nobody can undo.
* **What the archive cannot carry, it must not seem to carry.** The
  client secret is not in a tenant archive -- on purpose, since
  RFC-0041 K3 -- so an adopted tenant's provider would point at the
  node it just left, with no way to sign anybody in. Left in force it
  would be a sentence a reader believes and nothing backs up. So it is
  PARKED: carried, visible, and explicitly not in force.
* **An export is judged by what is in the file, not by what the tool
  said it did.** Measured 2026-09-23: two of the three doors to a
  realm export produce a file that looks perfect and has no people in
  it, and neither of them fails. `export_count_refusal` is the rule
  that catches both, and it catches them at the file.

Pure functions. Nothing here reads a disk, runs a container or speaks
HTTP -- `appctl.py` does that, and everything worth judging is here so
that a test can judge it without a node.
"""

import os
import re


# ---------------------------------------------------------------------------
# The archive
#
# `tenant-0.1` (RFC-0029 D5) carried a tenant's data. It did not carry
# the tenant's own RECORD -- the label, the face, the first-login
# policy, the provider. Nobody noticed, because until the move there
# was nothing that read an archive back.
#
# `tenant-0.2` adds `tenant-record.json`, and 0.1 is refused rather
# than adopted with the record missing. An adoption that quietly lost
# a club's colours and its sign-in policy would be found out weeks
# later by the club, which is the worst possible reader of that
# discovery.

ARCHIVE_FORMAT = "tenant-0.2"
OLD_FORMATS = ("tenant-0.1",)

ARCHIVE_PARTS = ("tenant-manifest.json", "tenant-record.json",
                 "tenant-registry.json", "tenant-users.json",
                 "tenant-audit.jsonl")

# The node archive's manifest, named here only so that handing one to
# `tenant adopt` gets an answer about what it IS.
NODE_FORMATS = ("1.0", "1.1", "2.0", "2.1")


def format_refusal(manifest):
    """Why this file may not be adopted at all. '' when it may.

    Asked of the manifest and not of the file name, because a name is
    what somebody typed and a manifest is what the archive says about
    itself.
    """
    if not isinstance(manifest, dict) or not manifest:
        return ("this file carries no tenant manifest -- it is not an "
                "archive `oaap backup create --tenant` wrote")
    fmt = (manifest.get("backup_format") or "").strip()
    if not fmt:
        return "this archive does not say what format it is"
    if fmt == ARCHIVE_FORMAT:
        return ""
    if fmt in OLD_FORMATS:
        return (f"this archive is {fmt}, which carried a tenant's DATA "
                "but not the tenant's own record -- its label, its face, "
                "its first-login policy and its provider are simply not "
                "in the file. Adopting it would lose them quietly. Make "
                f"a fresh one on the node it is leaving ({ARCHIVE_FORMAT} "
                "carries the record)")
    if fmt in NODE_FORMATS or (manifest.get("scope") or "") == "node":
        return ("this is a whole-NODE archive, not a tenant archive. A "
                "node archive replaces a machine and the installer does "
                "that: `sudo ./install.sh restore <archive>`")
    return f"'{fmt}' is not a tenant archive format this build reads"


def version_refusal(archive_version, node_version):
    """An archive written by a NEWER build than this node runs. '' if fine.

    The one direction that cannot be argued away. A newer build may
    have written fields in the record that this one drops on the first
    save -- and dropping them is exactly what nobody would see.
    """
    def parts(v):
        got = re.findall(r"\d+", str(v or ""))
        return tuple(int(x) for x in got[:3]) if got else ()

    a, n = parts(archive_version), parts(node_version)
    if not a or not n or a <= n:
        return ""
    return (f"this archive was written by OAAP {archive_version} and this "
            f"node runs {node_version}. A record from a newer build can "
            "hold fields this one does not know, and saving it here "
            "would drop them without a word: `sudo oaap update` first")


# ---------------------------------------------------------------------------
# "Empty", looked for rather than asserted

def collisions(manifest, record, instances, users,
               tenants, registry_instances, node_users, ports_taken=()):
    """Everything on this node that the archive would land on top of.

    A list of `{what, name, why}`, in the order an operator would want
    to clear them. Empty means the adoption is a COPY: nothing here is
    touched, nothing there is merged.

    Every entry names the thing, because "the node is not empty" is
    not something anybody can act on and "the label 'hbvp' is already
    a tenant here" is.

    The former labels count too (RFC-0026 3.3): a node still answers
    at a name a tenant was renamed away from, and adopting a club onto
    that name would put two clubs behind one address for as long as
    the grace lasts.
    """
    out = []
    tid = (manifest.get("tenant") or "").strip()
    label = (manifest.get("tenant_label") or "").strip().lower()

    for other_id, t in sorted((tenants or {}).items()):
        if other_id == tid:
            out.append({
                "what": "tenant", "name": other_id,
                "why": f"a tenant with this id is already here "
                       f"('{t.get('label', '?')}') -- this archive is from "
                       "this node, or from one it was cloned from"})
        names = [t.get("label", "")] + list(t.get("former_labels") or [])
        for n in names:
            n = (n or "").strip().lower()
            if n and n == label:
                out.append({
                    "what": "label", "name": label,
                    "why": ("this node already answers at this name"
                            if n == (t.get("label") or "").strip().lower()
                            else "a tenant here was renamed away from this "
                                 "name and the node still answers at it")})

    have = set(registry_instances or {})
    for name in sorted(instances or {}):
        if name in have:
            out.append({
                "what": "instance", "name": name,
                "why": "an app instance of this name is already installed "
                       "here, and it belongs to somebody else"})

    taken = set(ports_taken or ())
    for name, inst in sorted((instances or {}).items()):
        port = inst.get("port") or inst.get("host_port")
        if port and int(port) in taken:
            out.append({
                "what": "port", "name": f"{name} ({port})",
                "why": "this node already publishes that port"})

    mine = {(u.get("username") or "").strip().lower()
            for u in (node_users or []) if u.get("username")}
    for u in sorted((users or []), key=lambda x: x.get("username", "")):
        name = (u.get("username") or "").strip().lower()
        if name and name in mine:
            out.append({
                "what": "user", "name": name,
                "why": "somebody of this name already signs in here, and "
                       "OAAP will not decide whether they are the same "
                       "person"})
    return out


def collision_lines(found):
    """The collisions as an operator reads them."""
    out = []
    for c in found:
        out.append(f"  {c['what']:<9}{c['name']}")
        out.append(f"            {c['why']}")
    return out


def collision_refusal(found):
    """The sentence that refuses an adoption, or '' when nothing collides."""
    if not found:
        return ""
    kinds = sorted({c["what"] for c in found})
    return (f"this node is not empty for this tenant: {len(found)} "
            f"collision(s) ({', '.join(kinds)}). Adopting would MERGE, and "
            "merging a tenant into a node that has other customers on it "
            "is the thing `oaap backup create --tenant` refuses to promise "
            "(RFC-0029 D5). Clear what is named above, or adopt onto an "
            "empty node")


# ---------------------------------------------------------------------------
# The plan
#
# Data, for the same two reasons as the connector's: `--dry-run` can
# print it, and a test can assert the ORDER without a node.

def adopt_plan(manifest, record, instances, users, carried=False):
    """Every step an adoption takes, in order.

    The first step looks for collisions and the first step writes
    nothing. That is not a formality: everything after it assumes the
    target is empty, and an adoption that discovered a taken port
    halfway through would leave a tenant that can neither be finished
    nor undone.
    """
    label = (manifest.get("tenant_label") or "?").strip()
    n_inst, n_users = len(instances or {}), len(users or [])
    plan = [
        {"step": "check", "writes": False,
         "what": f"is this node empty for '{label}'?",
         "why": "every name, every port and every person in the archive, "
                "looked for here -- before anything is written"},
        {"step": "record", "writes": True,
         "what": f"write the tenant record of '{label}'",
         "why": "its label, its face, its first-login policy and its "
                "former names -- the part `tenant-0.1` did not carry"},
        {"step": "data", "writes": True,
         "what": "unpack this tenant's data",
         "why": "its instance subtree and its share of the byte store; "
                "nothing outside those two paths is touched"},
        {"step": "registry", "writes": True,
         "what": f"add {n_inst} instance record(s)",
         "why": "the registry entries as the other node held them, so "
                "the apps can be brought up as they were"},
        {"step": "users", "writes": True,
         "what": f"add {n_users} user record(s)",
         "why": "the identity service is stopped for this and started "
                "again -- appctl does not write that file beside a "
                "running owner"},
        {"step": "log", "writes": True,
         "what": "append this tenant's audit log",
         "why": "what happened to this tenant before it came here does "
                "not stop being what happened"},
        {"step": "instances", "writes": True,
         "what": f"bring up {n_inst} instance(s)",
         "why": "from the registry entries, the way a node restore does"},
    ]
    if carried:
        plan.append(
            {"step": "provider", "writes": True,
             "what": "PARK the identity provider, not put it in force",
             "why": "it points at the node this tenant is leaving, and "
                    "its client secret is deliberately not in the archive "
                    "(RFC-0041 K3) -- so it cannot work and must not look "
                    "as though it does"})
    return plan


def plan_refusal(plan):
    """Why an adoption plan may not be carried out. '' when it may.

    One rule, and it is the only one that matters here: the looking
    comes first, and nothing writes before it.
    """
    if not plan:
        return "there is no plan for this archive"
    where = next((i for i, s in enumerate(plan)
                  if s.get("step") == "check"), -1)
    if where != 0:
        return ("an adoption must look for collisions before it does "
                "anything else (RFC-0041 K6)")
    for i, step in enumerate(plan):
        if step.get("writes") and i <= where:
            return (f"'{step.get('step')}' would write before the node has "
                    "been checked for collisions (RFC-0041 K6)")
        if not (step.get("what") or ""):
            return f"'{step.get('step')}' does not say what it does"
    return ""


def plan_lines(plan):
    """The plan as an operator reads it."""
    out = []
    for s in plan:
        out.append(f"  {'writes' if s['writes'] else 'reads ':<8}{s['what']}")
        out.append(f"            {s['why']}")
    return out


# ---------------------------------------------------------------------------
# The provider that came along and cannot work

def provider_parked(record):
    """(provider, sentence) for a provider carried in an archive.

    The sharpest rule of step 7, and it is step 6's rule applied to a
    record instead of a switch: a sentence a reader believes has to
    have something in the world behind it.

    An adopted tenant's provider points at the Keycloak of the node it
    LEFT, and the client secret that would let this node use it is not
    in the archive -- deliberately, since a secret in a backup is a
    secret in every copy of that backup. Left in the record it would
    say, to a tenant_admin reading `oaap tenant idp`, that the club
    signs in through an address this node cannot use and should not be
    using anyway.

    So it is carried and parked: visible, named, and not in force.
    """
    idp = (record or {}).get("idp") or {}
    if not idp:
        return {}, ""
    issuer = (idp.get("issuer") or "").strip()
    return dict(idp), (
        f"This tenant signed in through {issuer} on the node it is "
        "leaving. That provider is CARRIED here and is NOT in force: it "
        "is the other node's address, and the client secret that would "
        "let this one use it is not in a tenant archive on purpose "
        "(RFC-0041 K3). Import the club's realm into this node's "
        "identity service, then `oaap idp provision <connector> "
        f"--tenant {(record or {}).get('label', '?')}` -- one command, "
        "and nobody has to register again: the members' identities and "
        "their passwords travel in the realm export (RFC-0041 §5.0).")


def parked_lines(parked):
    """What `tenant idp` prints about a parked provider."""
    if not parked:
        return []
    return [
        "  CARRIED, NOT IN FORCE (from a move, RFC-0041 K6):",
        f"    issuer     {parked.get('issuer', '?')}",
        f"    client     {parked.get('client_id', '?')}",
        "    This is the address of the node this tenant came FROM.",
        "    Nobody can sign in through it here, and that is the honest",
        "    state rather than a record that reads as though they could.",
    ]


# ---------------------------------------------------------------------------
# The realm export -- the other half of the move
#
# What made this worth its own set of rules is not the export. It is
# that a realm export can be WRONG without failing.

def export_target_refusal(path, data_dir):
    """Where a realm export may be written. '' when this path is fine.

    Measured 2026-09-23: the file carries the OIDC client secret and
    every member's password hash. So it is under the rule a backup
    target is under (RFC-0041 §3, RFC-0034 §3.2), and the one place it
    may never be is inside the platform's own data directory -- where
    an instance's storage is, where the byte store is, and where the
    next node archive would swallow it.
    """
    p = (path or "").strip()
    if not p:
        return "where should the export be written? `--out <file>`"
    if not os.path.isabs(p):
        return ("a realm export needs an absolute path -- it is a file "
                "full of credentials, and a relative one lands wherever "
                "the command happened to be run")
    real = os.path.realpath(p)
    inside = os.path.realpath(data_dir or "/var/lib/oaap")
    if real == inside or real.startswith(inside + os.sep):
        return (f"not inside {inside}: this file carries the club's client "
                "secret and every member's password hash, and that "
                "directory is what apps mount, what the byte store lives "
                "in and what the next node archive copies whole "
                "(RFC-0041 §3)")
    if os.path.exists(real):
        return (f"{real} already exists. A realm export is never written "
                "over something: if the old one is done with, remove it "
                "deliberately -- it is a file full of credentials, not a "
                "temporary")
    parent = os.path.dirname(real) or "/"
    if not os.path.isdir(parent):
        return f"there is no directory {parent}"
    return ""


def export_count_refusal(said, found, space_word="space", product="the server"):
    """Why an export file may not be called an export of that space.

    The rule this step exists for, and it came from the machine.
    Measured 2026-09-23 against Keycloak 26.7.4, three doors to a realm
    export:

    * the admin API's partial export answers 200 and carries the
      clients, the client secret and NOT ONE PERSON;
    * the product's own export tool, run beside the serving container,
      finds no database configuration there, falls back to its built-in
      empty one, and writes a perfect file for a realm nobody has ever
      used -- with no error at all;
    * the same tool, given the database the server actually runs on,
      writes the realm with its people and their password hashes.

    Two of the three produce a file that looks exactly like the third.
    A move built on either would arrive at the new node with a realm
    and no members, and it would be found out by the members.

    So the file is not judged by which door it came through, and not by
    what the tool printed. It is judged by COUNTING what is in it and
    asking the space how many there should be. That check does not care
    how the next door is built, which is why it is here and not in the
    connector.
    """
    if said is None:
        return (f"{product} was asked how many people are in this "
                f"{space_word} and did not answer. An export nobody can "
                "count against is an export nobody should move on")
    if found == said:
        return ""
    if found == 0 and said > 0:
        return (f"the export carries NO people and the {space_word} has "
                f"{said}. This is the failure this check exists for: such "
                "a file looks exactly like a good one, and the club would "
                "find out on the new node when nobody can sign in. "
                "Nothing was kept")
    return (f"the export carries {found} people and the {space_word} says "
            f"it has {said}. OAAP does not move a difference it cannot "
            "explain. Nothing was kept")


def export_words(path, space, space_word, count):
    """What an operator is told once the file exists.

    Said every time rather than in a README, because the thing being
    said is that the file in front of them is not a configuration file.
    """
    return [
        f"{path}",
        f"  {count} person(s) from the {space_word} '{space}', counted in "
        "the file and against the provider.",
        "",
        "This file is a SECRET, in the same class as a node backup: it",
        "carries the OIDC client secret and every member's password hash",
        "(measured, RFC-0041 §5.0). It is 0600 and owned by root.",
        "",
        "Move it the way a backup is moved, import it on the new node,",
        "and then REMOVE IT. It is not a configuration file that happens",
        "to contain accounts; it is every credential of that club in one",
        "file.",
    ]
