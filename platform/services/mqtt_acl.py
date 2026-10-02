"""Who may touch which MQTT topic -- the pure rules (RFC-0054 stage 2).

`/mqtt-auth/aclcheck` in app.py asks `decide()`. Nothing here reads a file
or a request: a key record, a topic, the access type and the metrics root
go in, a verdict and a sentence come out, so every rule can be tested
without Flask, Mosquitto or a clock.

Three kinds of key reach the broker:

  tenant    the RFC-0027 key of a principal of a tenant. Its tree is
            `oaap/<tenant-id>/...`, read and write, and nothing else.
            A record without a `kind` is one of these -- every key that
            existed before this module behaves exactly as it did.
  node      one OAAP node (RFC-0052). May publish under
            `<root>/<node>/#` and do nothing else: it cannot subscribe,
            so a stolen node key reads nothing, not even its own branch.
  operator  the operator's own people, tools and devices: a list of
            grants (a topic filter and read / write / readwrite) on trees
            OUTSIDE `oaap/`. Writing is allowed -- the broker is not a
            read-only mirror.

The metrics branch `<root>/#` is protected HERE, where the decision is
made, not where a key is issued: nobody but the matching node key writes
under the root, whatever grants another key carries. A value on that
branch is therefore always a value the node sent.
"""
import re

ACC_READ, ACC_WRITE, ACC_SUBSCRIBE, ACC_UNSUBSCRIBE = 1, 2, 4, 8
# Mosquitto's own numbers, passed on by the go-auth plugin: a delivery to
# a subscriber is a READ, a subscription is a SUBSCRIBE, a publish a
# WRITE. Anything else is not a question this module can answer, and the
# answer to an unknown question is no.
_READS = (ACC_READ, ACC_SUBSCRIBE, ACC_UNSUBSCRIBE)

BROKER_KINDS = ("node", "operator")
DEFAULT_ROOT = "oaap-node"
ACCESS = ("read", "write", "readwrite")
MAX_GRANTS = 50
MAX_FILTER = 200

NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
_LEVEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


def need(acc):
    """'read', 'write' or None for the access type the plugin sent."""
    try:
        acc = int(acc)
    except (TypeError, ValueError):
        return None
    if acc == ACC_WRITE:
        return "write"
    if acc in _READS:
        return "read"
    return None


def valid_root(root):
    if not isinstance(root, str) or not root or len(root) > 64:
        return False
    if root.startswith("/") or root.endswith("/"):
        return False
    return all(_LEVEL_RE.match(level) for level in root.split("/"))


def valid_filter(f):
    """An MQTT topic filter: `+` and `#` only as whole levels, `#` only
    last, no empty level, no NUL."""
    if not isinstance(f, str) or not f or len(f) > MAX_FILTER or "\x00" in f:
        return False
    levels = f.split("/")
    for i, level in enumerate(levels):
        if level == "":
            return False
        if level == "#":
            if i != len(levels) - 1:
                return False
        elif "#" in level or ("+" in level and level != "+"):
            return False
    return True


def overlaps(a, b):
    """Could ONE topic match both filters?"""
    la, lb = a.split("/"), b.split("/")
    i = 0
    while True:
        if i >= len(la) and i >= len(lb):
            return True
        if i < len(la) and la[i] == "#":
            return True
        if i < len(lb) and lb[i] == "#":
            return True
        if i >= len(la) or i >= len(lb):
            return False
        if la[i] != "+" and lb[i] != "+" and la[i] != lb[i]:
            return False
        i += 1


def covers(grant, requested):
    """Is EVERY topic the `requested` filter can match also matched by
    `grant`? (A publish topic is a filter without wildcards.) A request
    that is wider than the grant -- `#` against `a/#`, `+` against a
    literal -- is not covered."""
    lg, lr = grant.split("/"), requested.split("/")
    i = 0
    while True:
        if i < len(lg) and lg[i] == "#":
            return True
        if i >= len(lr):
            return i >= len(lg)
        if i >= len(lg):
            return False
        if lr[i] == "#":
            return False
        if lg[i] == "+":
            i += 1
            continue
        if lr[i] != lg[i]:
            return False
        i += 1


def parse_grants(raw, root=None):
    """Validate the grants of an operator key. Returns the clean list;
    raises ValueError with a sentence an operator can act on.

    `root` is the metrics root. A grant that WRITES where only the node
    itself may (`decide` ignores it for everybody else) is refused here,
    so that a key never says something the broker will not do."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("an operator key needs at least one grant")
    if len(raw) > MAX_GRANTS:
        raise ValueError(f"at most {MAX_GRANTS} grants per key")
    out = []
    for g in raw:
        if not isinstance(g, dict):
            raise ValueError("a grant is a filter and an access")
        f, a = g.get("filter"), g.get("access")
        if a not in ACCESS:
            raise ValueError(f"access must be one of {', '.join(ACCESS)}: "
                             f"{a!r}")
        if not valid_filter(f):
            raise ValueError(f"not a valid MQTT topic filter: {f!r}")
        first = f.split("/")[0]
        if first.startswith("$"):
            raise ValueError("the broker's own '$' topics cannot be granted")
        if first in ("#", "+", "oaap"):
            # `#` and `+` as the first level reach every tree, including
            # the tenants'; `oaap/` IS the tenants' tree.
            raise ValueError(
                f"{f!r} reaches the tenant trees ('oaap/...'). Operator "
                "grants live outside them: start with a name of your own, "
                "e.g. 'home/#'")
        if root and a in ("write", "readwrite") and overlaps(f"{root}/#", f):
            raise ValueError(
                f"{f!r} reaches the metrics branch '{root}/...', which only "
                "the node itself may write. Grant 'read' there, or choose "
                "another tree")
        out.append({"filter": f, "access": a})
    return out


def decide(rec, topic, acc, root, tenant_allowed):
    """(allowed, reason) for one key record, one topic, one access.

    `tenant_allowed(topic, tenant)` is the pre-existing rule for a tenant
    key (the `oaap/<tenant-id>/` prefix), passed in so that nothing about
    it changes.
    """
    what = need(acc)
    if what is None:
        return False, "unknown access type"
    if not isinstance(topic, str) or not topic or topic.startswith("$"):
        return False, "not a topic this key may use"
    kind = rec.get("kind") or "tenant"
    if what == "write" and overlaps(f"{root}/#", topic):
        # The metrics branch: only the node itself writes there.
        if kind != "node":
            return False, ("the metrics branch is written by the node "
                           "itself, nobody else")
    if kind == "node":
        node = rec.get("node") or ""
        if not NODE_RE.match(node):
            return False, "a node key without a valid node name"
        if what == "write" and covers(f"{root}/{node}/#", topic):
            return True, ""
        return False, ("a node key may only publish under "
                       f"{root}/{node}/")
    if kind == "operator":
        for g in rec.get("grants") or []:
            if g.get("access") in ("readwrite", what) and \
                    isinstance(g.get("filter"), str) and \
                    covers(g["filter"], topic):
                return True, ""
        return False, "no grant of this key covers that topic"
    if tenant_allowed(topic, rec.get("tenant", "")):
        return True, ""
    return False, "topic outside this principal's tenant"
