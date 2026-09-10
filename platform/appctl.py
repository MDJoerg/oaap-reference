#!/usr/bin/env python3
"""OAAP app runtime, increment 1 (oaap.apps.runtime, spec draft 0.1).

Host-side app manager invoked via `oaap app ...`:

    oaap app install <package-dir> [--name NAME] [--channel production|test]
    oaap app list
    oaap app remove <name> [--purge]
    oaap app visibility <name> all | groups <g1,g2,...>   (RFC-0007)
    oaap app tile <name> [auto|on|off]                    (spec 2.10)
    oaap app config list|set|unset <name> [key] [value]   (spec 2.3/2.4)
    oaap app address show|set|remove <name> [hostname]    (RFC-0009)
    oaap app throttle show|set|off <name> [reqs/seconds]  (RFC-0010)
    oaap app rehearse <prod> --name <new> [--code-from T]  (RFC-0030)
    oaap app rehearsal list|extend|sweep [<name>]          (RFC-0030)

Node-level, not per app:

    oaap node show | add-profile <p> | remove-profile <p>  (RFC-0011)
    oaap store list                                        (RFC-0012)
    oaap store add-source <url> [--name N] [--trust verified|unverified]
    oaap store remove-source <id|url|n> | enable <id> | disable <id>
    oaap store trust <id> verified|unverified
    oaap store reconcile          (run by `oaap update`, RFC-0012 §4)

    oaap data store status|schemas                    (oaap.data.store 0.1)
    oaap data store create <purpose> <tenant-id>
    oaap data store copy <schema> <new-name> | drop <schema> --yes
    oaap data store restore <dump-file>                (RFC-0031 Schritt 1)

Implements: manifest validation (subset of the published JSON Schema),
build on device, named instances with channels, per-instance storage/
secret/port, gateway wiring (generated Caddy site + reload).
Limitations of increment 1: exactly one service per app; no portal
tiles yet; role `public` supported but discouraged.
"""

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

import yaml

DATA_DIR = os.environ.get("OAAP_DATA_DIR", "/var/lib/oaap")
APP_DIR = os.path.join(DATA_DIR, "app")            # platform installation
APPS_DIR = os.path.join(DATA_DIR, "apps")          # platform files (registry etc.)
# Where instance data lives (RFC-0026): tenants/<tenant-id>/instances/
# /<instance-id>/. Identities, not names -- so renaming a tenant or an
# instance moves nothing, and everything one tenant owns is one subtree.
TENANTS_DIR = os.path.join(DATA_DIR, "tenants")
CADDY_APPS_DIR = os.path.join(APP_DIR, "apps-caddy")
REGISTRY = os.path.join(APPS_DIR, "registry.json")
STORE_SOURCES = os.path.join(APPS_DIR, "store-sources.json")
EXTERNAL_FILE = os.path.join(APPS_DIR, "external.json")
EDGE_FILE = os.path.join(APPS_DIR, "edge.json")
NODE_FILE = os.path.join(APPS_DIR, "node.json")   # node profiles (RFC-0011)
# accounts and tenants of THIS node (oaap.core.tenant 0.1, RFC-0022).
# Lives beside the registry because identity and portal already mount
# this directory read-only -- both must read it, neither may write it.
TENANTS_FILE = os.path.join(APPS_DIR, "tenants.json")
DEPLOY_TOKENS = os.path.join(APPS_DIR, "deploy-tokens.json")
DEPLOY_LOG = os.path.join(APPS_DIR, "deploy-log.jsonl")
# fleet keys (RFC-0021): revocable bearer keys that grant exactly one
# thing — reading GET /fleet/status. Digests only, like deploy tokens.
FLEET_KEYS = os.path.join(APPS_DIR, "fleet-keys.json")
FLEET_LOG = os.path.join(APPS_DIR, "fleet-log.jsonl")
# short-lived, single-use permissions for artifact deployment (RFC-0019):
# upload grants and envelope confirmations. Nothing long-lived lives here.
ARTIFACT_GRANTS = os.path.join(APPS_DIR, "artifact-grants.json")
# the tenant audit log (oaap.core.tenant 1.7). NOT beside the registry:
# identity has to append to it too -- user administration is the one
# state change that never passes through the host -- and identity's
# view of the registry directory is read-only, deliberately. So the log
# gets its own directory, mounted writable into identity and read-only
# into the portal.
AUDIT_DIR = os.path.join(DATA_DIR, "data", "audit")
TENANT_LOG = os.path.join(AUDIT_DIR, "tenant-log.jsonl")
SPOOL_DIR = os.path.join(DATA_DIR, "data", "deploy-spool")
# A request the worker has picked up lives here until it is answered
# (RFC-0024 §5). It is the ONLY state the worker keeps between taking a
# request and finishing it — and it is what lets anyone else say
# "running" instead of guessing, and lets the next run clean up after a
# worker that died mid-build.
SPOOL_CLAIMS = os.path.join(SPOOL_DIR, "claims")
# How long one deploy request may take before it is called off. A build
# that hangs used to block every later deployment for every instance,
# with nothing anywhere saying so (RFC-0024). A recorded failure is
# strictly better than silence.
DEPLOY_MAX_SECONDS = int(os.environ.get("OAAP_DEPLOY_MAX_SECONDS") or 1200)
TIMED_OUT = (f"aborted after the {DEPLOY_MAX_SECONDS // 60} minute time "
             "limit — the build was stopped, nothing was left running")


class DeployTimeout(Exception):
    """One deploy request outlasted its budget (RFC-0024 §5).

    Raised in place of subprocess.TimeoutExpired so that every branch of
    the worker — each of which already turns an exception into the
    request's failure message — reports the time limit in words instead
    of a command line.
    """


PORT_RANGE = range(8100, 8200)
ROLES = {"admin", "keyuser", "user", "guest", "partner", "public"}
GATEWAY_CONTAINER = "oaap-gateway-1"
IDENTITY_CONTAINER = "oaap-identity-1"
# The compose default network, home of the three platform services
# (gateway, identity, portal). Before RFC-0016 every app instance also
# ran here — the flat network that let any app reach identity's internal
# API. Apps now each get their own network (below) and this one carries
# platform services only.
PLATFORM_NETWORK = "oaap_default"


# ---------------------------------------------- app networks (RFC-0016)
# One Docker network per app instance. The instance's container(s) live
# on it and resolve each other by name; the GATEWAY joins it so it can
# proxy in; identity and portal never do. An app therefore reaches the
# gateway and its own siblings, and nothing else — the escalation path
# closed by the 0.1.29 key (RFC-0015 A4) is now closed structurally too.
#
# The gateway's membership is manual (`docker network connect`), and a
# compose recreate of the gateway drops it — so `ensure_app_network` +
# `connect_gateway` are called on every path that (re)creates an app
# (install, config recreate, migration), and the migration step
# reconnects the gateway to EVERY app network on every `oaap update`,
# because that update recreated the gateway. See migrate.sh.
def app_network(name):
    return f"oaap-inst-{name}"


def _network_exists(net):
    return subprocess.run(["docker", "network", "inspect", net],
                          capture_output=True, text=True).returncode == 0


def ensure_app_network(name):
    net = app_network(name)
    if not _network_exists(net):
        run(["docker", "network", "create", net])
    return net


_GW_PRIORITY_OK = None


def _gw_priority_supported():
    """Docker >= 28 knows --gw-priority; probe once per process."""
    global _GW_PRIORITY_OK
    if _GW_PRIORITY_OK is None:
        r = subprocess.run(["docker", "network", "connect", "--help"],
                           capture_output=True, text=True)
        _GW_PRIORITY_OK = "--gw-priority" in (r.stdout or "")
    return _GW_PRIORITY_OK


def _gateway_endpoint_priority(net):
    """GwPriority of the gateway's endpoint in `net`, None if unknown."""
    r = subprocess.run(
        ["docker", "inspect", "-f",
         '{{(index .NetworkSettings.Networks "' + net + '").GwPriority}}',
         GATEWAY_CONTAINER], capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except (ValueError, TypeError):
        return None


def connect_gateway(net):
    """Attach the gateway to an app network. Idempotent — Docker returns
    non-zero if it is already connected, which is not an error here.

    The link is made with a LOWER gateway priority than the platform
    network's, so the NAT target of the published ports (80/443/app
    ports) stays on oaap_default. Without this Docker homes that target
    on the alphabetically first network the gateway is in: removing one
    instance then silently moved it into the next instance's subnet,
    and that app could no longer reach its own node's published ports
    (asymmetric return path — found live 2026-08-23 when FleetView on
    oaap-demo lost its own node after an unrelated instance was
    removed). Older Docker has no --gw-priority; there the historic
    behaviour remains."""
    cmd = ["docker", "network", "connect"]
    if _gw_priority_supported():
        cmd.append("--gw-priority=-1")
    subprocess.run(cmd + [net, GATEWAY_CONTAINER],
                   capture_output=True, text=True)


def remove_app_network(name):
    net = app_network(name)
    if not _network_exists(net):
        return
    # the gateway is the one lingering member once the app is gone
    subprocess.run(["docker", "network", "disconnect", "-f", net, GATEWAY_CONTAINER],
                   capture_output=True, text=True)
    subprocess.run(["docker", "network", "rm", net],
                   capture_output=True, text=True)


def container_networks(container):
    r = subprocess.run(
        ["docker", "inspect", "-f",
         "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", container],
        capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


def network_members(net):
    r = subprocess.run(
        ["docker", "network", "inspect", "-f",
         "{{range .Containers}}{{.Name}} {{end}}", net],
        capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


# ------------------------------------------- app-to-app links (RFC-0016)
# Default is isolation: no app can reach another. A link is a deliberate,
# operator-declared, revocable grant recorded in the registry (survives
# redeploy, like visibility and address). Mechanism (Jörg's decision):
# a DEDICATED network `oaap-link-<a>-<b>` that both instances' primary
# containers join — NOT connecting a into b's own network, which would
# expose all of b's internal containers. Revocation is a clean teardown.
#
# The link is directed in intent (a may reach b) but a shared L3 network
# is reachable both ways; the value is that it is a separate wire, scoped
# to the two named apps, and nothing reaches b's private siblings.
def link_network(a, b):
    # order-independent: one shared network per pair, so a link declared
    # either or both ways is the same wire (the direction lives in the
    # registry as intent, not in a second redundant network).
    x, y = sorted([a, b])
    return f"oaap-link-{x}-{y}"


def app_link_partners(reg, name):
    """Every instance linked with `name`, either direction."""
    inst = reg["instances"].get(name, {})
    partners = set(inst.get("links") or [])
    partners |= {other for other, i in reg["instances"].items()
                 if name in (i.get("links") or [])}
    partners.discard(name)
    return partners


def setup_link_network(reg, a, b):
    net = link_network(a, b)
    if not _network_exists(net):
        run(["docker", "network", "create", net])
    for who in (a, b):
        c = reg["instances"][who]["container"]
        subprocess.run(["docker", "network", "connect", net, c],
                       capture_output=True, text=True)


def teardown_link_network(a, b):
    """Remove the pair's link network, disconnecting any container still
    attached first (`docker network rm` refuses a network with endpoints)."""
    net = link_network(a, b)
    if not _network_exists(net):
        return
    for c in network_members(net):
        subprocess.run(["docker", "network", "disconnect", "-f", net, c],
                       capture_output=True, text=True)
    subprocess.run(["docker", "network", "rm", net],
                   capture_output=True, text=True)


def restore_links(name):
    """Die erklärten Verbindungen EINER Instanz wiederherstellen.

    Ein `docker run` setzt den Container auf genau ein Netz. Jedes
    Neuerzeugen — Installation, Konfigurationsänderung, erneutes
    Deployment — wirft ihn also aus seinen Link-Netzen, und die
    Verbindung ist ab dann still tot: In der Registry steht sie, das
    Netz existiert, aber der Container hängt nicht mehr daran. Genau
    dieselbe Falle wie beim Gateway (siehe `connect_gateway`), nur
    unauffälliger, weil sie erst beim nächsten Aufruf der anderen App
    auffällt.

    Deshalb hier dieselbe Antwort: auf jedem Weg, der Container
    (neu) erzeugt, die Verbindungen dieser Instanz wieder aufbauen.
    """
    try:
        reg = load_registry()
    except Exception:
        return
    if name not in reg.get("instances", {}):
        return          # frisch installiert: es gibt noch keine Verbindung
    for partner in app_link_partners(reg, name):
        if partner in reg.get("instances", {}):
            setup_link_network(reg, name, partner)


def reconcile_links(reg):
    """Bring the live link networks in line with the registry — used by
    the migration step so links survive a gateway/app recreate. Creates
    the network for every declared link and connects both primaries;
    quiet, idempotent."""
    for a, inst in reg["instances"].items():
        for b in inst.get("links") or []:
            if b in reg["instances"]:
                setup_link_network(reg, a, b)


# ------------------------------------------------ network migration (RFC-0016)
# Runs from migrate.sh on every `oaap update`. Two jobs, both idempotent
# and quiet when there is nothing to do:
#   1) move any instance still sharing the platform network onto its own
#      network (the one-time isolation of apps installed before 0.1.30);
#   2) reconnect the gateway to EVERY app network — a compose recreate of
#      the gateway (which every update performs) drops the manual link,
#      so without this step all apps would 502 after an update.
def cmd_migrate_networks(_args):
    reg = load_registry()
    isolated, reconnected = 0, 0
    for name, inst in sorted(reg["instances"].items()):
        net = app_network(name)
        nets = container_networks(inst["container"])
        if PLATFORM_NETWORK in nets or net not in nets:
            # still on the flat network (or missing its own) — isolate it.
            # recreate_instance_containers creates the network, connects
            # the gateway, and recreates all service containers on it.
            recreate_instance_containers(name, instance_services(inst),
                                         inst.get("storage") or [])
            isolated += 1
            print(f"  isolated '{name}' onto {net}")
        else:
            # already isolated; the gateway may have lost its link when a
            # platform update recreated it — restore it.
            ensure_app_network(name)
            if GATEWAY_CONTAINER not in network_members(net):
                connect_gateway(net)
                reconnected += 1
            elif (_gw_priority_supported()
                  and (_gateway_endpoint_priority(net) or 0) >= 0):
                # pre-0.1.45 link: re-attach once with low gw-priority so
                # the published-port NAT target can never land in an
                # instance subnet again (see connect_gateway).
                subprocess.run(["docker", "network", "disconnect", net,
                                GATEWAY_CONTAINER],
                               capture_output=True, text=True)
                connect_gateway(net)
                reconnected += 1
    reconcile_links(reg)
    # the internal health endpoint (RFC-0016) lives in a generated site;
    # (re)write it and reload so the portal can probe apps through the
    # gateway after this update — it cannot reach them directly anymore.
    health_before = _read_file(os.path.join(CADDY_APPS_DIR, "_internal-health.caddy"))
    write_internal_health_caddy()
    health_changed = _read_file(os.path.join(CADDY_APPS_DIR, "_internal-health.caddy")) != health_before
    if isolated or reconnected or health_changed:
        if health_changed:
            try:
                reload_gateway()
            except Exception:
                pass
        print(f"  network migration: {isolated} isolated, "
              f"{reconnected} gateway link(s) restored"
              f"{'; health endpoint updated' if health_changed else ''}.")
    else:
        print("  networks already isolated; gateway links intact.")


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None

# ------------------------------------------- manifest version (RFC-0012 §8.2)
# The manifest version is MAJOR.MINOR. A new MINOR only ever ADDS things,
# so a node may read a manifest newer than itself and ignore what it does
# not know. A new MAJOR changed something that cannot be ignored, and that
# is the only case where a node refuses.
#
# This is deliberately NOT the semver 0.x convention, where the minor
# carries breaking changes: manifest 0.2 has to stay readable on every
# node already in the field, or every extension to the format becomes a
# flag day for the whole fleet.
MANIFEST_MAJOR = 0
MANIFEST_MINOR = 3      # 0.2 adds app.class (runtime spec 2.10);
                        # 0.3 adds data_model/contributes/consumes
                        # (RFC-0031 Schritt 2, oaap.data.model 0.1)

# What an app IS, as opposed to app.type, which says how it is packaged.
# 'service' means "used by other software" and costs the instance its
# launchpad tile. An unknown or missing value counts as 'frontend': a
# tile too many is untidy, a missing tile hides a working app.
APP_CLASSES = ("frontend", "service")
DEFAULT_APP_CLASS = "frontend"


def declared_class(app):
    """What the manifest ACTUALLY said, verbatim — '' when it said nothing.

    This is what goes into the registry. Storing the normalised value
    instead would throw away the difference between "declares frontend"
    and "declares nothing", and then no reader could ever tell them
    apart again. Found on the Raspi, 2026-08-09: a freshly installed
    0.1 app was recorded as 'frontend' and the CLI duly credited it
    with a declaration it never made.
    """
    return str(app.get("class") or "").strip()


def app_class_of(app):
    """The declared application class, normalised (runtime spec 2.10)."""
    value = declared_class(app)
    return value if value in APP_CLASSES else DEFAULT_APP_CLASS


def instance_class(inst):
    """The effective class of an INSTALLED instance, normalised."""
    value = str(inst.get("app_class") or "").strip()
    return value if value in APP_CLASSES else DEFAULT_APP_CLASS

# Features a manifest may declare under 'must_understand' -- the exception
# that makes "ignore what you do not know" safe (RFC-0012 §8.2 rule 2). A
# manifest naming anything not listed here is refused with a clear message
# rather than installed half-understood. Empty on purpose: the names
# reserved in RFC-0012 §8.3 land here as they are actually implemented.
MANIFEST_FEATURES = set()

# --------------------------------------------- oaap.data.model (RFC-0031 §4/§5)
#
# The manifest's 'data_model' section names five kinds of type. A type
# key is PascalCase-ish like the RFC-0031 §5 examples (Customer, VatId,
# isContactOf); a GROUP type key is dotted-lowercase instead
# ('crm.core', 'raci.assignments') so it reads as <namespace>.<name> on
# the object page (§3.3) -- the one kind with a different shape because
# it is the one kind an app author is expected to prefix by hand.
MODEL_KINDS = ("object_types", "attribute_types", "group_types",
              "relation_types", "activity_types")
MODEL_VALUE_TYPES = ("text", "int", "decimal", "bool", "date", "datetime",
                     "enum", "ref")
_MODEL_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_MODEL_GROUP_KEY_RE = re.compile(r"[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+")


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# Set by the deploy worker for the duration of ONE request (RFC-0024
# §5). Every command below then inherits what is left of that request's
# time budget, so the limit bites where the time is actually spent — in
# a docker build — and the child process is killed rather than merely
# abandoned. None outside the worker: an interactive `oaap app install`
# waits as long as the operator lets it.
DEADLINE = None


def run(cmd, **kw):
    if DEADLINE is not None and "timeout" not in kw:
        kw["timeout"] = max(1.0, DEADLINE - time.time())
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)
    except subprocess.TimeoutExpired:
        raise DeployTimeout(TIMED_OUT) from None


def load_registry():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"instances": {}, "retained": {}}


def save_registry(reg):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2)
    os.replace(tmp, REGISTRY)
    artifact_index_write(reg)


# ------------------------------------------- tenancy (oaap.core.tenant 0.1)
# RFC-0022 stage 2, and the whole of it: every record that belongs to
# SOMEBODY carries a tenant, and while this node has exactly one tenant,
# nothing anywhere says so. Building it before it is needed is the point
# -- carrying the dimension costs a field, retrofitting it costs a
# weekend, and that weekend would fall on the day a second customer is
# waiting to be onboarded.
#
# Everything internal refers to the tenant's UUID, never to its label.
# From 0.2 the label appears in hostnames, and a hostname is public
# (Certificate Transparency), so it has to stay changeable without a
# data migration.

DEFAULT_TENANT_LABEL = "default"
TENANT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def _iso_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def load_tenants():
    """{uuid: record} -- empty dict on a node not yet migrated."""
    try:
        with open(TENANTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    tenants = data.get("tenants") if isinstance(data, dict) else None
    return tenants if isinstance(tenants, dict) else {}


def save_tenants(tenants):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = TENANTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tenants": tenants}, f, indent=2)
    os.replace(tmp, TENANTS_FILE)
    # identity and portal read this file through a read-only mount; it
    # carries no secret, only names and references.
    os.chmod(TENANTS_FILE, 0o644)


def default_tenant_id():
    """The default tenant's UUID, or '' when this node has none yet.

    Looked up by label rather than being a well-known constant on
    purpose: two nodes' default tenants are DIFFERENT tenants (spec
    1.2), and a shared identifier would invite exactly the merge that
    RFC-0022 D1 forbids.
    """
    for tid, t in sorted(load_tenants().items()):
        if t.get("label") == DEFAULT_TENANT_LABEL:
            return tid
    return ""


def ensure_default_tenant():
    """Create the account reference and the default tenant, once.

    Returns its id. Idempotent, and silent -- this runs on every update.
    """
    tid = default_tenant_id()
    if tid:
        return tid
    tenants = load_tenants()
    tid = str(uuid.uuid4())
    tenants[tid] = {
        "label": DEFAULT_TENANT_LABEL,
        "name": "",
        # The account lives on the central management node (RFC-0022
        # Q1). A node holds an opaque reference plus a cached display
        # name and nothing else: no members, no delegation, and above
        # all no cross-node write path.
        "account": str(uuid.uuid4()),
        "account_name": "",
        "created": _iso_now(),
    }
    save_tenants(tenants)
    return tid


def resolve_tenant(ref):
    """Resolve a stored tenant reference (spec 2.2). None if unknown.

    Two rules, and the difference between them is the entire safety
    argument of this version:

      - ABSENT means the default tenant. That is how every record
        written before this version reads, and it is what makes the
        migration a no-op for anyone who never asked for tenants.

      - UNKNOWN never means the default tenant. A reference this node
        does not have is refused, not healed. Mapping it onto `default`
        would move a customer's users or instances into the OPERATOR's
        own tenant -- a data leak dressed up as robustness.
    """
    ref = (ref or "").strip()
    if not ref:
        return default_tenant_id() or None
    return ref if ref in load_tenants() else None


def tenant_label(tid):
    return (load_tenants().get(tid) or {}).get("label", "")


# How long a renamed tenant keeps answering under its old label. An
# address change on this platform has cost a live customer once already
# (hub.bdt.joomp.de, 2026-08-23); the grace period is the lesson.
RENAME_GRACE_DAYS = 30


def _in_days(days):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=days)).isoformat(timespec="seconds")


def former_labels(t):
    """The unexpired former labels of a tenant record (spec 1.6).

    Compared as ISO strings, which is only sound because every
    timestamp here is written by _iso_now() in UTC with the same
    precision. Anything else in that field sorts as expired, which is
    the safe direction.
    """
    now = _iso_now()
    return [f.get("label", "") for f in (t.get("former_labels") or [])
            if f.get("label") and str(f.get("until", "")) > now]


def tenant_host_prefixes(tid):
    """The host label parts instances of this tenant answer under.

    The default tenant's prefix is the empty string -- its label IS the
    absence of a label, so `<instance>.<node>` stays exactly as RFC-0018
    describes it and nothing on an existing node moves. Every other
    tenant contributes its current label first, then each unexpired
    former one (spec 1.6, 2.4).

    Empty list for a tenant this node does not have: an instance whose
    tenant does not resolve gets no external name at all. Fail closed
    (spec 2.5) -- serving it under the operator's own names would be the
    exact substitution the resolution rules exist to prevent.
    """
    t = load_tenants().get(tid)
    if not t:
        return []
    if t.get("label") == DEFAULT_TENANT_LABEL:
        return [""]
    return [t.get("label", "")] + former_labels(t)


def instance_auto_hosts(name, inst, ext_host=None):
    """Every automatic address of an instance, the current one first.

    `<instance>.<node>` in the default tenant, `<instance>.<label>.<node>`
    in every other one, plus one name per unexpired former label
    (spec 2.4). This is the ONE place that composes the name -- the
    gateway writes its sites from it and every message that prints an
    address reads from it, so the two cannot drift apart.

    They did drift: until 0.1.54 the CLI and the portal both built
    `<instance>.<node>` by hand, which is right for the default tenant
    and names a host that answers nowhere for every other one.

    Empty list when the node has no external hostname, and empty when
    the instance names a tenant this node does not have -- fail closed,
    as `tenant_host_prefixes` explains.
    """
    ext = load_external() if ext_host is None else ext_host
    if not ext:
        return []
    # The address carries the name the tenant chose, never the node key:
    # `viewer.cls.<node>`, not `cls-viewer.cls.<node>` (RFC-0025 §8.1).
    # The key exists so identifiers do not collide; the address does not
    # need it, because the label already says which tenant this is.
    #
    # Current name first, then the ones it answered under until
    # recently -- the same grace a renamed tenant label gets, for the
    # same reason (RFC-0026 3.3).
    names = [instance_name(name, inst)] + former_names(inst)
    prefixes = tenant_host_prefixes(resolve_tenant((inst or {}).get("tenant")))
    return [f"{local}.{p}.{ext}" if p else f"{local}.{ext}"
            for local in names for p in prefixes]


def tenant_slug(tid):
    """What node-wide identifiers of this tenant's instances start with.

    The tenant's CURRENT label, and the default tenant's is the empty
    string -- exactly as its label is the absence of a label in a
    hostname (spec 1.2). That is what keeps every existing key, address
    and deploy URL on a single-tenant node unchanged.

    RFC-0025 §8.1 froze this so that a rename would not have to move
    data. RFC-0026 moves the data under an identity instead
    (`instance_dir`), and once it hangs off an id rather than a name,
    freezing bought nothing but drift between what a container is called
    and who owns it. A rename now re-keys and restarts, which the rename
    dialog says before it does it.
    """
    t = load_tenants().get(tid or "") or {}
    if t.get("label") == DEFAULT_TENANT_LABEL:
        return ""
    return t.get("label", "")


def instance_key(tenant, name):
    """The node-wide identifier of an instance (RFC-0025 §8.1).

    `<slug>-<name>`, or plain `<name>` in the default tenant. Everything
    node-scoped is keyed by this and nothing else: the registry, the
    containers, the per-app network, the gateway site file, the data
    directory, the deploy token, the creation permit and the deploy
    hook. Composed ONCE, at creation, and then stored as the registry
    key -- never recomputed, so a rename cannot move an instance.
    """
    slug = tenant_slug(tenant) if tenant else ""
    return f"{slug}-{name}" if slug else name


def instance_name(key, inst=None):
    """What the instance is called INSIDE its tenant.

    Stored since 0.1.58. An instance from before that has no `name` and
    its key is its name -- correct, because back then keys carried no
    slug. Keys only have to be unique, not uniform (RFC-0025 §8.3).
    """
    return ((inst or {}).get("name") or key)


def find_instance(reg, tenant, name):
    """(key, record) of the instance called `name` inside `tenant`.

    Not the same question as "is this key free": an instance created
    before 0.1.58 has a bare key, so a tenant could otherwise end up
    holding two instances both called `viewer` (RFC-0025 §8.4).
    """
    for key, inst in (reg.get("instances") or {}).items():
        if (instance_name(key, inst) == name
                and (resolve_tenant(inst.get("tenant")) or "") == (tenant or "")):
            return key, inst
    return "", None


# Which tenant the DATA under an instance directory belonged to
# (oaap.core.tenant 1.4). Not mounted into any container: only
# `<instance-dir>/storage/<name>` is, so an app can neither read nor
# forge this.
TENANT_MARKER = ".tenant"


def stamp_data_tenant(name, tid):
    """Record on disk whose data this directory holds.

    Written on every install, so a node heals forward: the file is the
    only thing that survives `oaap app remove` without --purge, and
    without it a directory left behind cannot be attributed to anyone.
    """
    if not tid:
        return
    path = os.path.join(instance_dir(name), TENANT_MARKER)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tid + "\n")
    os.replace(tmp, path)


def retained_data_tenant(name):
    """Whose data is lying under this instance name.

    Three answers, and they are not the same:

    * ``None`` -- there is no directory. Nothing to inherit.
    * ``""``   -- a directory without a marker: data written before
      0.1.56, which cannot be attributed to anyone.
    * a tenant id -- the data of that tenant.
    """
    d = instance_dir(name)
    if not os.path.isdir(d):
        return None
    try:
        with open(os.path.join(d, TENANT_MARKER), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def retained_data_refusal(name, tenant):
    """Why this tenant may not take over the data left under this name.

    `oaap app remove` keeps an instance's storage and its instance.env
    by default -- deliberately, because reinstalling under the same name
    is how an operator repairs an app without losing its data. With one
    tenant that is exactly right.

    Across tenants it is a leak. Instance names are unique per NODE and
    not per tenant (spec 2.4), so a name one customer gives up can be
    taken by the next -- and the install would mount the previous
    customer's storage and read their secrets out of instance.env,
    because existing values win over manifest defaults.

    Returns a refusal string, or "" when the install may proceed. The
    message never names the other tenant: that a name is encumbered is a
    fact the boundary cannot hide, whose it is is not (spec 2.4).
    """
    held = retained_data_tenant(name)
    if held is None or (tenant and held == tenant):
        return ""
    if held == "" and single_tenant():
        # A node with one tenant has nothing to cross. This is what
        # keeps every existing installation behaving exactly as before.
        return ""
    return (f"data of an earlier instance named '{name}' is still on this "
            f"node and cannot be handed to a new one — an administrator "
            f"removes it with 'sudo oaap app purge {name} --yes', or "
            f"choose a different name")


def former_names(inst):
    """The unexpired former names of an instance (RFC-0026 3.3).

    The sibling of `former_labels` on a tenant, and for the same reason:
    a name that was published is a name somebody wrote down. It keeps
    answering for a grace period instead of disappearing the moment
    somebody renames something.
    """
    now = _iso_now()
    return [f.get("name", "") for f in (inst or {}).get("former_names") or []
            if f.get("name") and str(f.get("until", "")) > now]


def former_keys(inst):
    """The unexpired former node keys of an instance.

    Kept apart from former_names on purpose: a key changes when the
    INSTANCE is renamed and also when its TENANT is, and the deploy
    address is built from the key. Deriving it from the names would
    have to guess which label was current when.
    """
    now = _iso_now()
    return [f.get("key", "") for f in (inst or {}).get("former_keys") or []
            if f.get("key") and str(f.get("until", "")) > now]


def resolve_deploy_target(reg, given):
    """Which instance a deploy address names (RFC-0026 3.4).

    The current key, or one this instance answered under until recently.
    Without the second, renaming anything would break every pipeline and
    every briefing that carries the address -- the failure this platform
    has already paid for once (hub.bdt.joomp.de, 2026-08-23).
    """
    if given in (reg.get("instances") or {}):
        return given
    for key, inst in (reg.get("instances") or {}).items():
        if given in former_keys(inst) or given == inst.get("id"):
            return key
    return ""


def retained_key(tenant, name):
    """How a retained instance is filed: by tenant and by the name the
    customer used. Those two are what a reinstall knows."""
    return f"{tenant}|{name}"


def retained_record(reg, tenant, name):
    return (reg.get("retained") or {}).get(retained_key(tenant, name))


def instance_identity(reg, key, tenant, name):
    """The identity this install writes under (RFC-0026 3.1).

    Three cases, in this order:

    * the instance exists -- its identity is its own and cannot change,
      which is what keeps a redeploy from moving anyone's data;
    * data was left behind by a removal of the same name in the same
      tenant -- the identity comes back with it, which is what keeps
      the promise that reinstalling under the same name recovers the
      data (`oaap app remove` without --purge says exactly that);
    * otherwise a new identity, minted once.

    Deliberately NOT persisted here: a half-finished install must not
    leave a stub in the registry that `oaap app list` then trips over.
    The caller passes the result down and the registry entry is written
    at the end, as it always was.
    """
    inst = (reg.get("instances") or {}).get(key)
    if inst and inst.get("id"):
        return {"id": inst["id"], "tenant": inst.get("tenant") or tenant}
    kept = retained_record(reg, tenant, name)
    if kept and kept.get("id"):
        return {"id": kept["id"], "tenant": tenant}
    return {"id": new_instance_id(), "tenant": tenant}


def tenant_by_label(label, include_former=True):
    """(id, record) for a label. Former labels resolve too, for as long
    as they are unexpired -- otherwise a rename would break the very
    commands an operator reaches for right after renaming."""
    label = (label or "").strip().lower()
    if not label:
        return None, None
    for tid, t in sorted(load_tenants().items()):
        if t.get("label") == label:
            return tid, t
        if include_former and label in former_labels(t):
            return tid, t
    return None, None


def label_is_free(label):
    """A label may be taken by a current tenant OR by an unexpired
    former one -- reusing a name that still routes somewhere else would
    silently hand one tenant another's traffic."""
    tid, _t = tenant_by_label(label, include_former=True)
    return tid is None


def single_tenant():
    """True while tenants must stay invisible (spec 2.3, the acceptance
    criterion). Every surface asks this before mentioning a tenant."""
    return len(load_tenants()) <= 1


def audit_tenant(action, tenant, subject="", result="ok", who="root",
                 role="root", detail=""):
    """Append one line to the tenant audit log (spec 1.7).

    Two processes append here: this file, for everything that happens
    on the host or through the portal's worker, and identity, for user
    administration -- the one state change that never passes the host.
    A single short line opened with "a" is appended atomically enough
    for that, and nothing ever rewrites the file, which is the other
    half of why two writers are safe.

    A server_admin action inside a tenant is filed in THAT tenant's
    log, not in a separate operator log. The customer has to be able to
    see it: it is the entire counterweight to "server_admin may do
    everything" (RFC-0022 D5), and a counterweight the customer cannot
    read is not one.
    """
    os.makedirs(AUDIT_DIR, exist_ok=True)
    entry = {"when": _iso_now(), "who": who or "root", "role": role or "root",
             "action": action, "tenant": tenant or "",
             "tenant_label": tenant_label(tenant), "subject": subject,
             "result": result}
    if detail:
        entry["detail"] = detail
    with open(TENANT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        os.chmod(TENANT_LOG, 0o644)
    except OSError:
        pass
    return entry


def read_tenant_log(tenant=None, limit=50):
    """The audit log, oldest first, optionally one tenant's entries.

    A damaged line is skipped, never fatal: this file is a record, and
    a record that refuses to be read because of one bad byte protects
    nobody.
    """
    out = []
    try:
        with open(TENANT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if tenant and entry.get("tenant") != tenant:
                    continue
                out.append(entry)
    except OSError:
        return []
    return out[-limit:] if limit else out


def acting_tenant(username, requested=""):
    """Which tenant a portal-initiated action happens in, and by what
    authority. Returns (tenant_id, role, error) -- tenant_id None means
    refuse, and `error` says why in one sentence.

    Spec 2.3 rule 3: the tenant comes from the ACTOR'S OWN RECORD, never
    from the request. A tenant that arrives in a request is a tenant the
    caller chose, and a caller who may choose their tenant has no
    boundary. The single exception is server_admin, who may do
    everything anyway (RFC-0022 D5) and whose choice is recorded.

    Resolved here on the host, from identity's own store, because the
    spool is data and not trust -- the same rule the store install path
    already follows.
    """
    u = next((x for x in (_read_identity_users() or [])
              if x.get("username") == (username or "")), None)
    roles = set((u or {}).get("roles") or [])
    if "server_admin" in roles:
        if requested:
            tid = resolve_tenant(requested)
            return (tid, "server_admin",
                    "" if tid else "that tenant does not exist on this node")
        return ensure_default_tenant(), "server_admin", ""
    if not u or "tenant_admin" not in roles:
        return None, "", "requires server_admin or tenant_admin"
    own = resolve_tenant(u.get("tenant"))
    if not own:
        return (None, "tenant_admin",
                "your account names a tenant this node does not have")
    if requested and requested != own:
        return (None, "tenant_admin",
                "a tenant_admin acts only in their own tenant")
    return own, "tenant_admin", ""


def cross_tenant_refusal(action, name):
    """What a request touching another tenant's instance is told.

    Two answers, and the difference is the whole point:

    * By default the instance is answered as one that does NOT EXIST.
      Telling a tenant_admin that the name is taken elsewhere on the
      node is already an answer across the boundary (spec 2.3 rule 2).
    * A store install is the exception, because it would CREATE an
      instance under that name, and a name taken on this node has to be
      refused as taken (spec 2.4) -- the collision is the fact, the
      owner is not. "unknown instance" there would send a tenant_admin
      hunting for a bug in their own tenant; the portal's create path
      has always answered this way, and the store path now agrees.
    """
    if action == "install":
        return f"an instance named '{name}' already exists"
    return "unknown instance"


def instance_tenant_ref(inst):
    """The tenant reference to hand the gateway for an instance.

    Deliberately the STORED value, unresolved: if it names a tenant
    this node does not have, identity refuses everyone but a
    server_admin, which is the fail-closed answer spec 2.5 asks for.
    Only a record with no reference at all reads as the default tenant.
    """
    return (inst or {}).get("tenant") or default_tenant_id()


def tenant_for_new_instance(inst, permit=None):
    """Which tenant an instance being (re)installed belongs to.

    Ordered so that a redeploy can never move an instance between
    tenants: what the instance already says wins over everything. Then
    the creation permit -- the only record that names a tenant before
    an instance exists (spec 1.4) -- and only then the default.

    One function on purpose: 0.2 adds "the tenant the installing admin
    is acting in", and this is the single place that has to learn it.
    """
    existing = (inst or {}).get("tenant")
    if existing:
        return existing
    granted = (permit or {}).get("tenant")
    if granted:
        return granted
    return ensure_default_tenant()


def _identity_users_path():
    return os.path.join(DATA_DIR, "data", "identity", "users.json")


def _read_identity_users():
    """Read-only peek at the user store, for counting.

    Returns None when the store EXISTS but could not be read -- which on
    a real node almost always means "not root", because identity keeps
    it at 0600. That is deliberately not the same answer as [], and the
    difference matters: counting an unreadable store as empty made
    `tenant check` report that every record resolves without having
    looked at a single user, and `tenant show` print "Users: 0" on a
    node with eight. A check that cannot see half of what it checks has
    to say so, not pass.

    A store that is simply not there yet reads as [] -- that is a node
    before its first user, and it is an honest zero.

    appctl never WRITES this file: identity owns it and rewrites it on
    every user change. Two writers to one JSON file is a lost update
    waiting for the day two admins click at the same moment.
    """
    try:
        with open(_identity_users_path(), encoding="utf-8") as f:
            users = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    return users if isinstance(users, list) else []


def name_links(reg=None, tenants=None):
    """The readable paths beside the identities (RFC-0026 3.2).

    Returns {link path: target path}. Pure, so the layout can be checked
    without a filesystem that supports symlinks -- Windows does not,
    without extra rights, and these rules should be testable anywhere.
    """
    reg = reg if reg is not None else load_registry()
    tenants = tenants if tenants is not None else load_tenants()
    links = {}
    for tid, t in tenants.items():
        label = t.get("label") or ""
        if label:
            links[os.path.join(TENANTS_DIR, "by-label", label)] = tenant_dir(tid)
    for key, inst in (reg.get("instances") or {}).items():
        tid, iid = resolve_tenant(inst.get("tenant")), inst.get("id")
        if not tid or not iid:
            continue
        links[os.path.join(tenant_dir(tid), "by-name", instance_name(key, inst))] =             os.path.join(tenant_dir(tid), "instances", iid)
    return links


def refresh_name_links(reg=None, tenants=None):
    """Lay the readable paths down, best effort.

    Best effort on purpose: a symlink is a convenience for whoever has
    to read a path at two in the morning, never something the platform
    depends on. A filesystem that refuses them changes nothing about
    how the node runs.
    """
    wanted = name_links(reg, tenants)
    for root in (os.path.join(TENANTS_DIR, "by-label"),):
        _prune_links(root, wanted)
    for tid in (tenants if tenants is not None else load_tenants()):
        _prune_links(os.path.join(tenant_dir(tid), "by-name"), wanted)
    made = 0
    for link, target in wanted.items():
        try:
            os.makedirs(os.path.dirname(link), exist_ok=True)
            if os.path.islink(link):
                if os.readlink(link) == target:
                    continue
                os.remove(link)
            elif os.path.exists(link):
                continue
            os.symlink(target, link)
            made += 1
        except OSError:
            return made          # unsupported here; not a failure
    return made


def _prune_links(root, wanted):
    """Drop readable paths that name something that is gone."""
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for e in entries:
        path = os.path.join(root, e)
        if os.path.islink(path) and path not in wanted:
            try:
                os.remove(path)
            except OSError:
                pass


def cmd_migrate_instance_dirs(_args):
    """Move instance data into the tenant tree (RFC-0026 3.2).

    The one migration in this platform that moves data rather than
    adding a field, so it is written to be interruptible: one instance
    at a time, each one moved with a rename (a directory-entry change
    within one filesystem, not a copy), the registry written after each
    move, and nothing deleted at any point. Interrupt it and the next
    run continues where it stopped, because an instance that has
    already moved carries its id and is skipped.

    Containers are NOT recreated here. They are still running with the
    old mount and keep working until the next deployment or a restart,
    which is why this can run on a live node -- but the caller is told,
    because a container that has not been recreated still writes to the
    old path.
    """
    reg = load_registry()
    moved, minted, recreated, skipped = 0, 0, 0, []
    for key, inst in sorted(reg.get("instances", {}).items()):
        tid = resolve_tenant(inst.get("tenant"))
        if not tid:
            skipped.append(f"{key} (names a tenant this node does not have)")
            continue
        if inst.get("id"):
            continue                       # already in the tree
        old = os.path.join(APPS_DIR, key)
        iid = new_instance_id()
        new = os.path.join(tenant_dir(tid), "instances", iid)
        if os.path.isdir(old):
            os.makedirs(os.path.dirname(new), exist_ok=True)
            try:
                os.rename(old, new)
            except OSError as e:
                # Across filesystems rename fails; copying gigabytes
                # silently is not this function's decision to make.
                skipped.append(f"{key} ({e})")
                continue
            marker = os.path.join(new, TENANT_MARKER)
            if os.path.exists(marker):
                os.remove(marker)          # the path says it now
            moved += 1
        else:
            os.makedirs(new, exist_ok=True)
        inst["id"] = iid
        minted += 1
        save_registry(reg)                 # after each one, on purpose
        # Recreated immediately, and this is not optional. A bind mount
        # follows the inode, so a container keeps writing to the moved
        # directory and nothing breaks -- until someone restarts it,
        # whereupon Docker re-resolves the OLD path, creates it empty,
        # and the app looks wiped. Leaving that window open would be the
        # worst kind of bug: silent, delayed, and data-shaped.
        #
        # It was. Until 0.1.71 this asked for `inst["services"]`, which
        # only instances installed since 0.1.31 have -- so every OLDER
        # instance kept its old mount, and precisely the oldest and
        # most-loved data was the data left one restart from looking
        # wiped (found 2026-09-05 on four nodes at once, Bernd's CRM
        # among them). `instance_services()` exists to make that
        # distinction disappear, and its own docstring says "so every
        # caller (recreate, migrate, config) treats old and new the
        # same" -- this caller then asked the question it was written
        # to remove.
        if os.path.isdir(new):
            try:
                recreate_instance_containers(key, instance_services(inst),
                                             inst.get("storage") or [],
                                             inst.get("endpoints") or [],
                                             inst=inst)
                recreated += 1
            except Exception as e:        # noqa: BLE001 - reported, not fatal
                skipped.append(f"{key} (moved, but not recreated: {e})")
    links = refresh_name_links(reg)
    if minted or skipped:
        print("")
        print("Moving instance data under its tenant (RFC-0026) ...")
        if moved:
            print(f"  {moved} instance director(ies) moved into "
                  f"tenants/<tenant>/instances/<id>/.")
        if minted:
            print(f"  {minted} instance(s) given their identity.")
        if links:
            print(f"  {links} readable path(s) laid down under "
                  f"tenants/by-label/.")
        for line in skipped:
            print(f"  NOT moved: {line}")
        if recreated:
            print(f"  {recreated} instance(s) recreated on the new path.")
        if moved:
            refresh_generated_sites()
            reload_gateway()
    _repair_stale_mounts(reg)


def _repair_stale_mounts(reg):
    """Put containers back on the tenant tree that were left on the old
    path -- the damage the guard above used to cause (0.1.71).

    Runs on every update, like every step in migrate.sh, and is silent
    when there is nothing to repair. It has to exist separately from
    the fix: four nodes already carried the fault when it was found,
    and a node cannot heal from a condition that is only removed for
    the next migration.

    Two things are repaired, in this order:

    1. The container is recreated on the instance's real directory.
    2. Whatever appeared at the OLD path is moved aside, never deleted.
       On a node whose container was never restarted there is nothing
       there; on one that was, there is a freshly initialised state the
       app wrote after Docker re-created the path empty. That is young
       data and almost certainly worthless -- and "almost certainly" is
       not a licence to delete somebody's directory.

    Deliberately conservative: it acts only when the instance HAS a
    directory in the tenant tree. Where the move itself failed, the old
    path may hold the only copy, and moving it aside would be the very
    accident this function exists to undo.
    """
    if not shutil.which("docker"):
        return
    repaired, aside = [], []
    for key, inst in sorted(reg.get("instances", {}).items()):
        new = instance_dir(key, inst)
        old = os.path.join(APPS_DIR, key)
        if not inst.get("id") or not os.path.isdir(new) or old == new:
            continue
        # Ask DOCKER where the container reads from, not the registry:
        # the registry has said the right thing all along, and it was
        # the container that disagreed.
        stale = False
        for svc in instance_services(inst):
            out = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{range .Mounts}}{{.Source}}\n{{end}}", svc["container"]],
                capture_output=True, text=True)
            if out.returncode != 0:
                continue
            if any(line.startswith(old + os.sep)
                   for line in out.stdout.splitlines()):
                stale = True
        if not stale:
            continue
        try:
            recreate_instance_containers(key, instance_services(inst),
                                         inst.get("storage") or [],
                                         inst.get("endpoints") or [],
                                         inst=inst)
            repaired.append(key)
        except Exception as e:            # noqa: BLE001 - reported, not fatal
            print(f"  WARNING: {key} still reads from the old path and could "
                  f"not be recreated ({e}).")
            continue
        if os.path.isdir(old):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                os.rename(old, f"{old}.abgeloest-{stamp}")
                aside.append(f"{key} -> apps/{key}.abgeloest-{stamp}")
            except OSError as e:
                print(f"  WARNING: could not set aside {old} ({e}).")
    if not repaired:
        return
    print("")
    print("Repairing instances left on the old data path (0.1.71) ...")
    for k in repaired:
        print(f"  {k}: now reads tenants/<tenant>/instances/<id>/.")
    for line in aside:
        print(f"  set aside, nothing deleted: {line}")
    print("  Their data was never lost -- it moved with the tenant tree,")
    print("  and it is what these containers read from now. What was set")
    print("  aside is what the app wrote after a restart re-created the")
    print("  old path empty; look at it, then remove it.")
    refresh_generated_sites()
    reload_gateway()


def cmd_migrate_tenants(_args):
    """Give this node its default tenant and stamp what appctl owns.

    Called from migrate.sh on every `oaap update`. Idempotent, and
    quiet when there is nothing to do -- like every step in there.

    What is stamped here: the instance registry and open creation
    permits. NOT the user store (identity migrates its own, see
    _migrate_tenant_once there) and NOT deploy tokens, which store no
    tenant at all -- a token is bound to one instance and the instance
    already knows. A tenant reference stored twice is one that can
    disagree with itself, and the day it does, the wrong copy decides
    who may write into whose instance.
    """
    created = not default_tenant_id()
    tid = ensure_default_tenant()

    # The frozen short name of RFC-0025 is withdrawn (RFC-0026 D2b):
    # identifiers follow the current label again, so a stored slug is
    # something that can only disagree with it. Cleared where it was
    # written, once.
    tenants = load_tenants()
    slugged = 0
    for t in tenants.values():
        if t.pop("slug", None) is not None:
            slugged += 1
    if slugged:
        save_tenants(tenants)

    reg = load_registry()
    stamped = 0
    for inst in reg.get("instances", {}).values():
        if not inst.get("tenant"):
            inst["tenant"] = tid
            stamped += 1
    # What each instance is called inside its tenant. Until now the key
    # WAS the name, so that is the honest starting value -- and it stays
    # right forever for instances that predate this.
    named = 0
    for key, inst in reg.get("instances", {}).items():
        if not inst.get("name"):
            inst["name"] = key
            named += 1
    if stamped or named:
        save_registry(reg)

    # And on disk, beside the data itself (1.4). The registry entry
    # disappears when an instance is removed; the data does not, and an
    # unattributed directory is exactly what the next tenant to take
    # that name must not inherit. Cheap, idempotent, and it makes every
    # instance that exists TODAY attributable -- only directories whose
    # instance was already gone stay anonymous, and those are refused.
    marked = 0
    for name, inst in reg.get("instances", {}).items():
        held = retained_data_tenant(name)
        own = resolve_tenant(inst.get("tenant")) or tid
        if held is not None and held != own:
            stamp_data_tenant(name, own)
            marked += 1

    grants = _grants_prune(load_grants())
    permits = 0
    for g in grants.values():
        if g.get("kind") == "create" and not (g.get("payload") or {}).get("tenant"):
            g.setdefault("payload", {})["tenant"] = tid
            permits += 1
    if permits:
        save_grants(grants)

    if created or stamped or permits or marked or slugged or named:
        print("")
        print("Preparing this node for tenants (RFC-0022 stage 2) ...")
        if created:
            print("  Default tenant created.")
        if stamped:
            print(f"  {stamped} app instance(s) assigned to it.")
        if permits:
            print(f"  {permits} open creation permit(s) assigned to it.")
        if marked:
            print(f"  {marked} instance data director(ies) marked with their "
                  f"tenant.")
        if slugged:
            print(f"  {slugged} tenant(s): the frozen short name of RFC-0025 "
                  f"withdrawn (RFC-0026).")
        if named:
            print(f"  {named} instance(s) recorded under the name they "
                  f"already had.")
        if single_tenant():
            print("  Nothing changes for anyone: this node has one tenant.")


def zone_probe(label):
    """Does `<label>.<node host>` resolve to this node? (spec 2.4)

    Returns a sentence for the operator, never a verdict that stops
    them. A two-level name below the node's own name is a property of
    the ZONE, not of DNS -- a wildcard matches exactly one label, so
    *.example.org does not cover a.b.example.org. Measured before a
    tenant is created rather than discovered when its apps are
    unreachable. A node with no external hostname has no zone to check,
    and an operator may be about to fix their DNS: this warns, it does
    not refuse.
    """
    host = load_external()
    if not host:
        return ("This node has no external hostname, so there is nothing to "
                "check yet. When you register one, verify that "
                f"<instance>.{label}.<node> resolves before publishing it.")
    import socket
    probe = f"probe.{label}.{host}"
    try:
        socket.getaddrinfo(probe, None)
    except OSError:
        return (f"WARNING: '{probe}' does not resolve. Instances of this "
                f"tenant will be published as <instance>.{label}.{host}, and "
                "a DNS wildcard covers exactly ONE label -- so *." + host +
                " does not cover this. Add a wildcard for *." + label + "." +
                host + " (or a record per instance) before publishing "
                "anything.")
    return (f"Two-level names under {host} resolve -- instances of this "
            f"tenant can be published as <instance>.{label}.{host}.")


def tenant_label_error(label, action):
    """Why this label may not be written, or "" when it may.

    Returns a sentence instead of exiting, because there are two callers
    now: the CLI, which turns it into `die()`, and the deploy worker,
    which has to answer a portal request rather than end the process
    (RFC-0025 8.1 lesson -- one rule, every reader, one place).
    """
    if not TENANT_LABEL_RE.fullmatch(label or ""):
        return ("tenant label: lowercase letters, digits and hyphens, "
                "starting with a letter or digit, at most 31 characters")
    if label == DEFAULT_TENANT_LABEL:
        return (f"'{DEFAULT_TENANT_LABEL}' is this node's own tenant and is "
                f"not available to {action}")
    if not label_is_free(label):
        return (f"the label '{label}' is already taken on this node (a former "
                "label still inside its grace period counts as taken)")
    return ""


def _tenant_write_common(label, action):
    err = tenant_label_error(label, action)
    if err:
        die(err)


def tenant_create(label, name="", account="", account_name=""):
    """Write one new tenant and log it. The label is already checked.

    Shared by `oaap tenant create` and the portal's create request, so
    the record cannot come out differently depending on which door it
    came through.
    """
    tenants = load_tenants()
    tid = str(uuid.uuid4())
    tenants[tid] = {
        "label": label,
        "name": name or "",
        # The account lives on the central management node (RFC-0022
        # Q1). Without one given, this tenant is its own account --
        # honest about what it is rather than pretending to a
        # registry that does not exist here.
        "account": account or str(uuid.uuid4()),
        "account_name": account_name or "",
        "created": _iso_now(),
        "former_labels": [],
    }
    save_tenants(tenants)
    return tid, tenants


def cmd_migrate_tenant_routes(_args):
    """Put the generated parameters into sites written before them.

    Every authenticated route is verified against its instance's tenant
    (spec 3.1), and that parameter is generated INTO the site files. A
    node updating from 0.1 has files without it, so they are rewritten
    once, here, on the update that brings the second tenant within
    reach -- there must be no window in which the boundary is merely
    intended.

    Also carries the OPTIONS preflight bypass (2026-09-09, aipc-service
    letter) to every existing role-gated route: that fix lives in the
    site GENERATOR, so an instance installed before it keeps the old
    file until something rewrites it. Same reasoning as the paragraph
    above applies word for word -- there must be no window in which a
    browser-direct API-key call is silently broken on an app that
    already shipped its own CORS handling.

    Idempotent and silent afterwards, like every step in migrate.sh:
    it looks at what is on disk and does nothing when the answer is
    already there.
    """
    reg = load_registry()
    stale = []
    for name, inst in sorted(reg.get("instances", {}).items()):
        if not inst.get("routes") or not inst.get("svc_port"):
            continue
        path = os.path.join(CADDY_APPS_DIR, f"{name}.caddy")
        body = _read_file(path) or ""
        # Not every forward_auth authenticates: a PUBLIC route with a
        # rate brake (RFC-0010) has one too, pointing at /throttle. Ask
        # for the authentication call by name, or a public throttled
        # instance is found "stale" on every single update, rewritten,
        # and found stale again -- it has no session to scope and will
        # never grow the parameter. (Found on oaap-test, 2026-08-29:
        # forgejo was rewritten by every run.)
        if "/verify?" not in body:
            continue
        # Three things are generated into an authenticated site now: the
        # tenant (0.2), the instance (RFC-0027 D5), and -- on a route
        # that actually carries roles -- a preflight bypass (2026-09-09).
        # A rehearsal (login_only, RFC-0030 2.15.2) never gets the last
        # one by design (every block of a rehearsal asks /verify, no
        # exceptions), so its absence there must NOT count as stale --
        # that would rewrite the same file, unchanged, on every update,
        # exactly the forgejo/throttle trap the comment above warns
        # about. All generated together rather than as separate
        # near-identical migrations -- one migration should stay one.
        gated = any(x for r in inst["routes"] for x in r["roles"] if x != "public")
        needs_preflight = gated and not is_rehearsal(inst) and "@preflight" not in body
        if "&tenant=" in body and "&instance=" in body and not needs_preflight:
            continue
        stale.append((name, inst, path))
    if not stale:
        return
    print("")
    print("Bringing the gateway sites up to date (tenant boundary, "
          "instance scope, preflight bypass) ...")
    for name, inst, path in stale:
        with open(path, "w", encoding="utf-8") as f:
            f.write(caddy_site(inst["port"], inst["routes"], inst["container"],
                               inst["svc_port"],
                               (inst.get("visibility") or {}).get("groups"),
                               name, throttle_of(inst),
                               services=route_targets(inst),
                               tenant=instance_tenant_ref(inst),
                               login_only=is_rehearsal(inst)))
    refresh_generated_sites()
    reload_gateway()
    print(f"  {len(stale)} instance site(s) rewritten.")
    if single_tenant():
        print("  Nothing changes for anyone: every route now names the")
        print("  tenant it already belonged to.")
    else:
        print("  Every authenticated route now names the tenant of its")
        print("  instance, and is refused to sessions from another one.")
    print("  A route with roles also gets its own OPTIONS preflight "
          "bypass now -- a browser-direct API-key call no longer dies "
          "on the unauthenticated preflight that precedes it.")


def cmd_tenant(args):
    """This node's tenants (spec 2.1/2.2).

    Deleting a tenant is deliberately absent: a tenant holds users,
    instances and their data, so removing it is an export-then-destroy
    operation and gets its own round.
    """
    tenants = load_tenants()
    if not tenants:
        die("this node has no tenant store yet -- run `oaap update`")

    if args.action == "create":
        label = (args.name or "").strip().lower()
        _tenant_write_common(label, "create")
        tid, tenants = tenant_create(label, args.title, args.account,
                                     args.account_name)
        audit_tenant("tenant.create", tid, label)
        print(f"Tenant '{label}' created.")
        print("")
        # Spec 3.4: said at the moment the label is chosen, not in a
        # document nobody opens on the day it would have mattered.
        print("The label is PUBLIC. It becomes part of the hostnames of this")
        print("tenant's instances and therefore appears in Certificate")
        print("Transparency logs, which anyone can read. If this customer is")
        print("confidential, choose a label that says nothing about them --")
        print(f"  sudo oaap tenant rename {label} <opaque-label>")
        print("changes it while the old one keeps working for a while.")
        print("")
        print(zone_probe(label))
        if len(tenants) == 2:
            print("")
            print("This node now has more than one tenant, so tenants become")
            print("visible: the portal shows every caller their own tenant")
            print("only, and this tenant's instances answer under their own")
            print("names. Nothing about the existing tenant changes.")
        print("")
        print("Next, give it its first administrator: create a user in the")
        print(f"portal with tenant '{label}' and role tenant_admin. From")
        print("there the tenant administers itself.")
        return

    if args.action == "rename":
        old = (args.name or "").strip().lower()
        new = (args.target or "").strip().lower()
        tid, _found = tenant_by_label(old, include_former=False)
        if not tid:
            die(f"no tenant with the current label '{old}'")
        # From the dict that gets saved, not from the lookup's own copy:
        # editing a record nobody writes back is a rename that reports
        # success and changes nothing.
        t = tenants[tid]
        if t.get("label") == DEFAULT_TENANT_LABEL:
            die("the default tenant belongs to this node itself and cannot be "
                "renamed -- its label is the ABSENCE of a label in every "
                "hostname, so renaming it would move every address on this "
                "node at once")
        _tenant_write_common(new, "rename to")
        grace = max(0, int(args.grace_days))
        host = load_external() or "<node>"
        insts = sorted(instance_name(k, i)
                       for k, i in load_registry().get("instances", {}).items()
                       if resolve_tenant(i.get("tenant")) == tid)
        # Named consequences before the act, in the same voice instance
        # address removal already uses. This platform has paid for a
        # silent address change once (hub.bdt.joomp.de, 2026-08-23).
        print(f"Renaming '{old}' to '{new}' changes every address of this "
              "tenant:")
        for n in insts:
            print(f"  {n}.{old}.{host}  ->  {n}.{new}.{host}")
        if not insts:
            print("  (no instances yet -- nothing is published under it today)")
        print("")
        print(f"The old label keeps answering for {grace} more day(s); after "
              "that,")
        print("anything still pointing at it stops resolving here.")
        print("A certificate for each new name is obtained on first contact.")
        if insts:
            print("")
            # The trade RFC-0026 D2b makes, named before it is made: no
            # drift between what a container is called and who owns it,
            # paid for with a restart of this tenant's apps.
            print(f"Identifiers follow the new label: containers, networks "
                  f"and deploy")
            print(f"addresses become '{new}-<name>'. These {len(insts)} app(s) "
                  f"are REBUILT and")
            print("therefore restart. The old deploy addresses keep working "
                  f"for {grace} day(s).")
            print("")
            print("Nothing moves on disk. The data hangs off each instance's "
                  "identity,")
            print("not off any name — which is why this is a restart and not "
                  "a migration.")
        print("")
        print(zone_probe(new))
        if not args.yes:
            print("")
            die("nothing was changed -- repeat with --yes when the list above "
                "is what you want")
        former = [f for f in (t.get("former_labels") or [])
                  if str(f.get("until", "")) > _iso_now()]
        if grace:
            former.append({"label": old, "until": _in_days(grace)})
        t["former_labels"] = former
        t["label"] = new
        save_tenants(tenants)
        # Identifiers follow the label, so every instance of this tenant
        # moves to a new key -- registry, containers, network, gateway
        # file, token and permits. The DATA stays exactly where it is.
        reg = load_registry()
        for key in [k for k, i in sorted(reg["instances"].items())
                    if resolve_tenant(i.get("tenant")) == tid]:
            local = instance_name(key, reg["instances"][key])
            rekey_instance(reg, key, instance_key(tid, local), grace)
        audit_tenant("tenant.rename", tid, new, detail=f"was '{old}'")
        refresh_generated_sites()
        refresh_name_links()
        reload_gateway()
        print("")
        print(f"Renamed. Instances of this tenant now answer under "
              f"<instance>.{new}.{host}"
              + (f" and, until the grace period ends, <instance>.{old}.{host}."
                 if grace else "."))
        return

    if args.action == "log":
        # Reading is not an event. Only state changes are recorded
        # (spec 1.7) -- a log that also logs its readers grows faster
        # than it is read and buries what matters.
        tid = None
        if args.name:
            tid, _t = tenant_by_label(args.name)
            if not tid:
                die(f"no tenant with label '{args.name}'")
        entries = read_tenant_log(tid, limit=max(1, int(args.count)))
        if not entries:
            print("No entries yet." if not tid else
                  f"No entries for '{args.name}' yet.")
            return
        for e in entries:
            who = f"{e.get('who','?')} ({e.get('role','?')})"
            where = e.get("tenant_label") or e.get("tenant", "")[:8] or "-"
            line = (f"{e.get('when','?')}  {where:<16} {who:<28} "
                    f"{e.get('action','?')}  {e.get('subject','')}")
            if e.get("result") != "ok":
                line += f"  [{e.get('result')}]"
            if e.get("detail"):
                line += f"  -- {e['detail']}"
            print(line.rstrip())
        return

    if args.action == "list":
        for _tid, t in sorted(tenants.items(), key=lambda kv: kv[1].get("label", "")):
            extra = ""
            aliases = former_labels(t)
            if aliases:
                extra = f"   (also answers as: {', '.join(aliases)})"
            print(f"{t.get('label','?'):<20} {t.get('name') or '(no name)'}{extra}")
        if single_tenant():
            print("")
            print("This node has one tenant, so tenants are not in use here:")
            print("no screen, no address and no command output mentions them.")
        return

    if args.action == "check":
        # The integrity check of spec 3.2. It REPORTS, it never repairs
        # -- repairing an unknown tenant means guessing whose data this
        # is, and the only safe guess is none.
        problems = []
        for name, inst in sorted(load_registry().get("instances", {}).items()):
            if resolve_tenant(inst.get("tenant")) is None:
                problems.append(f"app instance '{name}' names an unknown tenant "
                                f"({inst.get('tenant')})")
        users = _read_identity_users()
        if users is None:
            # Not a finding about the data -- a finding about this run.
            # Reported as a failure anyway: "everything resolves" would
            # be a claim about records nobody looked at.
            print("The user store could not be read, so users were not "
                  "checked at all.")
            print("Run it as root:  sudo oaap tenant check")
            sys.exit(1)
        for u in users:
            if resolve_tenant(u.get("tenant")) is None:
                problems.append(f"user '{u.get('username','?')}' names an unknown "
                                f"tenant ({u.get('tenant')})")
        for g in _grants_prune(load_grants()).values():
            if g.get("kind") != "create":
                continue
            ref = (g.get("payload") or {}).get("tenant")
            if resolve_tenant(ref) is None:
                problems.append(f"creation permit for '{g.get('instance','?')}' "
                                f"names an unknown tenant ({ref})")
        if not problems:
            print(f"All records resolve. Tenants on this node: {len(tenants)}.")
            return
        print("Records naming a tenant this node does not have:")
        for line in problems:
            print(f"  {line}")
        print("")
        print("Nothing was changed. An unknown tenant is never mapped onto the")
        print("default one -- that would move somebody's data into the")
        print("operator's own tenant. Restore the tenant store, or reassign")
        print("these records deliberately.")
        sys.exit(1)

    # action == "show"
    label = args.name or DEFAULT_TENANT_LABEL
    match = [(tid, t) for tid, t in tenants.items() if t.get("label") == label]
    if not match:
        die(f"no tenant with label '{label}'")
    tid, t = match[0]
    default = default_tenant_id()
    instances = sum(1 for i in load_registry().get("instances", {}).values()
                    if (i.get("tenant") or default) == tid)
    stored = _read_identity_users()
    users = ("(not readable — run as root)" if stored is None else
             sum(1 for u in stored if (u.get("tenant") or default) == tid))
    aliases = former_labels(t)
    print(f"Tenant:    {t.get('label','?')}")
    if aliases:
        print(f"Also as:   {', '.join(aliases)} (former labels, still routing)")
    print(f"Name:      {t.get('name') or '(none)'}")
    print(f"Account:   {t.get('account_name') or '(reference only)'}")
    print(f"Created:   {t.get('created','?')}")
    # Counts, not names: this is an inventory, not a data export.
    print(f"Users:     {users}")
    print(f"Instances: {instances}")


# ------------------------------------------------ node profiles (RFC-0011)
# What this node is FOR. Zero or more, maintained by the operator on the
# machine itself, empty by default -- a node that says nothing behaves
# exactly as before. Only profiles that have an effect today exist
# (RFC-0011 decision 2): a settable profile that does nothing would
# invite typos and false expectations.

PROFILES = {
    "dev": "development node — the portal may create test instances and "
           "install from a source no store list carries yet (RFC-0011)",
    "exposed": "exposed node — an operator may grant an app a non-HTTP "
               "port that bypasses the gateway (RFC-0015). Only meaningful "
               "where the node has (or will get) the router port forward.",
    "store": "data-platform node — carries the managed Postgres of "
             "oaap.data.store 0.1 (RFC-0031 Schritt 1). Without it, "
             "data-model and digital-twin capabilities are unavailable "
             "here. Unlike 'dev'/'exposed' this actually starts and stops "
             "a platform service (see cmd_node below).",
}


def load_profiles():
    """Profiles of this node, unknown entries ignored.

    Ignoring rather than trusting matters: a hand-edited or restored
    node.json must never grant a behaviour this version does not
    understand.
    """
    try:
        with open(NODE_FILE, encoding="utf-8") as f:
            stored = json.load(f).get("profiles") or []
    except (OSError, ValueError):
        return []
    return sorted({p for p in stored if p in PROFILES})


def save_profiles(profiles):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = NODE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"profiles": sorted(set(profiles))}, f, indent=2)
    os.replace(tmp, NODE_FILE)


def has_profile(name):
    return name in load_profiles()


def cmd_node(args):
    """Show or change what this node is for (RFC-0011).

    Deliberately CLI-only: the portal displays profiles but cannot
    change them (except in the first-run wizard, which is authorised by
    the setup token). Otherwise a compromised portal could simply grant
    itself the `dev` profile and with it the very powers the profile is
    meant to be a deliberate, per-node decision about.
    """
    profiles = load_profiles()
    if args.action == "show":
        print("Node profiles: " + (", ".join(profiles) if profiles else "(none)"))
        for p in profiles:
            print(f"  {p}: {PROFILES[p]}")
        if not profiles:
            print("  This node behaves like a plain production node.")
        print("\nAvailable: " + ", ".join(sorted(PROFILES)))
        return
    profile = (args.profile or "").strip().lower()
    if profile not in PROFILES:
        die(f"unknown profile '{profile or '(missing)'}' — available: "
            + ", ".join(sorted(PROFILES)))
    if args.action == "add-profile":
        if profile in profiles:
            print(f"Node already has profile '{profile}'.")
            return
        save_profiles(profiles + [profile])
        print(f"Node profile '{profile}' added: {PROFILES[profile]}")
        if profile == "dev":
            print("Note: on this node the portal may now install from any Git\n"
                  "source it is given — not only from the configured store\n"
                  "sources. That is the point of the profile, and it is a bad\n"
                  "trade on a machine holding customer data.")
        if profile == "store":
            # Unlike dev/exposed, this profile has nothing to check at
            # request time -- either the container is up or it is not.
            # So the effect happens now, not "on the next update".
            try:
                # 'twin' alongside 'store' (RFC-0031 Schritt 3): it is
                # nothing without Postgres and gated by the same profile
                # (docker-compose.yml), so the one command that starts
                # the capability starts both halves of it.
                _compose("--profile", "store", "up", "-d", "store", "twin")
                print("The managed Postgres and the digital twin service "
                      "are starting ('docker compose ... --profile store "
                      "up -d store twin').")
            except (subprocess.CalledProcessError, OSError) as e:
                err = (getattr(e, "stderr", "") or "").strip().splitlines()
                print(f"WARNING: could not start the 'store'/'twin' services"
                      + (f": {err[-1]}" if err else f": {e}") + ".")
                print("Check with: docker compose --project-directory "
                      f"{APP_DIR} --project-name oaap --profile store "
                      "up -d store twin")
    else:
        if profile not in profiles:
            die(f"node does not have profile '{profile}'")
        if profile == "store":
            existing = store_schemas()
            if existing:
                names = ", ".join(r["schema"] for r in existing)
                die(f"cannot remove profile 'store' — {len(existing)} "
                    f"schema(s) still exist ({names}). Drop them first: "
                    "'oaap data store drop <schema> --yes'.")
        save_profiles([p for p in profiles if p != profile])
        print(f"Node profile '{profile}' removed.")
        if profile == "store":
            try:
                _compose("stop", "store", "twin")
                print("The managed Postgres and digital-twin containers "
                      "were stopped (Postgres's data volume is kept).")
            except (subprocess.CalledProcessError, OSError):
                print("WARNING: could not stop the 'store'/'twin' "
                      "containers — check 'docker ps'.")


# -------------------------------------------- managed Postgres (oaap.data.store 0.1)
# One Postgres per node, one schema per tenant and purpose, gated by the
# 'store' node profile above. Apps never talk to this directly — only
# oaap.data.twin/oaap.data.model will (RFC-0031 §6) — so this CLI is
# operator/service tooling, nested like 'oaap fleet key ...':
# 'oaap data store <action>'. Deliberately NOT named 'oaap store' — that
# verb already means the app/package store (oaap.apps.runtime, RFC-0012).

STORE_CONTAINER = "oaap-store-1"  # compose v2 default: <project>-<service>-1


def _compose(*compose_args):
    return run(["docker", "compose", "--project-directory", APP_DIR,
                "--project-name", "oaap", *compose_args])


def _store_running():
    """Whether the store actually answers -- not whether the container
    happens to be in 'Running' at this instant. Found on oaap-test,
    2026-09-09: a container stuck in a restart loop flickers between
    Running=true (the moment the process starts) and Running=false
    (waiting to retry); '.State.Running' can catch it in the wrong
    instant. 'pg_isready' asks the question every caller here actually
    means."""
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "exec", STORE_CONTAINER,
                        "pg_isready", "-U", "postgres"],
                       capture_output=True, text=True)
    return r.returncode == 0


def _store_psql(sql, database="postgres"):
    """Run one statement as the 'postgres' OS/DB user inside the
    container — local trust auth over its own socket, the same reason
    'docker exec -u postgres ... psql' needs no password anywhere in
    this file. STORE_SUPERUSER_PASSWORD in .env is what the image used
    to initialise that user on first start; nothing here ever types it
    again (security requirement 1)."""
    return run(["docker", "exec", "-u", "postgres", STORE_CONTAINER,
                "psql", "-v", "ON_ERROR_STOP=1", "-tAc", sql, database])


def store_schemas():
    """[{schema, role, purpose, tenant_id}, ...] — read from Postgres
    itself, never from a side file, so this can never claim a schema
    exists that does not (the 0.1.4 backup lesson, applied here: ask
    the database, not a list that can drift away from it)."""
    if not _store_running():
        return []
    out = _store_psql(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT IN "
        r"('public','pg_catalog','information_schema') "
        r"AND nspname NOT LIKE 'pg\_%' ORDER BY 1"
    ).stdout
    rows = []
    for name in (n.strip() for n in out.splitlines()):
        if not name:
            continue
        purpose, _, tenant = name.partition("_")
        rows.append({"schema": name, "role": name,
                     "purpose": purpose or name, "tenant_id": tenant})
    return rows


def _schema_size_kb(schema):
    out = _store_psql(
        "SELECT COALESCE(SUM(pg_total_relation_size(c.oid)),0)::bigint "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = '{schema}'"
    ).stdout.strip()
    try:
        return int(out) // 1024
    except ValueError:
        return 0


def _store_free_kb():
    return shutil.disk_usage(os.path.join(DATA_DIR, "data", "store")).free // 1024


def cmd_data(args):
    """'oaap data store|model ...' — the managed Postgres (oaap.data.store
    0.1) and the type registry built on it (oaap.data.model 0.1).

    Nested like 'oaap fleet key ...': a top-level verb ('data') meant to
    carry oaap.data.twin too once it exists, an object, and an action.
    """
    if args.object == "model":
        return cmd_data_model(args)
    if args.object != "store":
        die(f"unknown object '{args.object}' — available: store, model")

    if args.action == "status":
        if not has_profile("store"):
            print("store: not carried — this node has no profile 'store'.")
            print("Data-model and digital-twin capabilities are "
                  "unavailable here. Add it with:")
            print("  sudo oaap node add-profile store")
            return
        # Not just _store_running() here: that function treats ANY
        # failure -- Postgres actually down, or Docker unreachable at
        # all -- as "not running", which is the right conservative
        # default for a gate before a destructive action, but the
        # wrong answer to show a human. Found running this by hand on
        # oaap-test without sudo: it said "NOT running" for a container
        # that was in fact healthy, because 'docker exec' itself needs
        # root/the 'docker' group here (same limitation 'oaap status'
        # already names) -- a permission error, reported as a crash.
        if shutil.which("docker"):
            r = subprocess.run(["docker", "exec", STORE_CONTAINER,
                                "pg_isready", "-U", "postgres"],
                               capture_output=True, text=True)
        else:
            r = None
        if r is not None and "permission denied" in (r.stderr or "").lower():
            print("store: carried, cannot tell from here -- no permission "
                  "to reach Docker (run with sudo, or add yourself to the "
                  "'docker' group).")
            return
        running = r is not None and r.returncode == 0
        print(f"store: carried, container "
              f"{'running' if running else 'NOT running'}")
        if running:
            print(f"schemas: {len(store_schemas())}")
        else:
            print("Check with: docker compose --project-directory "
                  f"{APP_DIR} --project-name oaap --profile store "
                  "up -d store")
        return

    if not has_profile("store"):
        die("this node has no profile 'store' — add it first: "
            "'sudo oaap node add-profile store'.")
    if not _store_running():
        die(f"the '{STORE_CONTAINER}' service is not running — see "
            "'oaap data store status'.")

    if args.action == "schemas":
        rows = store_schemas()
        if not rows:
            print("No schemas.")
            return
        for r in rows:
            print(f"{r['schema']}  purpose={r['purpose']}  "
                  f"tenant={r['tenant_id'] or '?'}")
        return

    if args.action == "create":
        purpose = (args.arg1 or "").strip().lower()
        tenant_id = (args.arg2 or "").strip()
        if not purpose or not tenant_id:
            die("usage: oaap data store create <purpose> <tenant-id>")
        if not re.fullmatch(r"[a-z][a-z0-9]*", purpose):
            die("purpose must be lowercase letters/digits, starting with "
                "a letter (it becomes part of the schema name)")
        if tenant_id not in load_tenants():
            die(f"unknown tenant '{tenant_id}' — 'oaap tenant list' shows "
                "the IDs. The schema name carries the tenant-ID, never "
                "the Kürzel (RFC-0025/0026).")
        schema = f"{purpose}_{tenant_id}"
        if schema in {r["schema"] for r in store_schemas()}:
            die(f"schema '{schema}' already exists")
        password = secrets.token_urlsafe(24)
        _store_psql(f'CREATE ROLE "{schema}" LOGIN PASSWORD \'{password}\';')
        _store_psql(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{schema}";')
        print(f"Schema '{schema}' created.")
        print(f"Role:     {schema}")
        print(f"Password: {password}")
        print("Shown once — a platform secret for the consuming service "
              "(oaap.data.twin), never for an app (security requirement 2).")
        return

    if args.action == "drop":
        schema = (args.arg1 or "").strip()
        if not schema:
            die("usage: oaap data store drop <schema> --yes")
        if schema not in {r["schema"] for r in store_schemas()}:
            die(f"no such schema '{schema}'")
        if not args.yes:
            die(f"dropping '{schema}' deletes its data permanently — "
                "pass --yes to confirm. Nothing was changed.")
        _store_psql(f'DROP SCHEMA "{schema}" CASCADE;')
        _store_psql(f'DROP ROLE IF EXISTS "{schema}";')
        print(f"Schema '{schema}' and its role dropped.")
        return

    if args.action == "copy":
        schema = (args.arg1 or "").strip()
        new_schema = (args.arg2 or "").strip()
        if not schema or not new_schema:
            die("usage: oaap data store copy <schema> <new-schema>")
        # Every schema/role name reaches SQL as a quoted identifier
        # built by f-string, not a parameter -- safe only because it is
        # constrained first. 'schema' is checked below against what
        # Postgres itself already lists (so it can only be a name a
        # validated 'create' produced); 'new_schema' is fresh input and
        # gets the same shape rule 'create' enforces.
        if not re.fullmatch(r"[a-z][a-z0-9_]*", new_schema):
            die("new schema name must be lowercase letters/digits/"
                "underscores, starting with a letter")
        existing = {r["schema"] for r in store_schemas()}
        if schema not in existing:
            die(f"no such schema '{schema}'")
        if new_schema in existing:
            die(f"schema '{new_schema}' already exists")
        size_kb = _schema_size_kb(schema)
        free_kb = _store_free_kb()
        if size_kb and free_kb < size_kb * 12 // 10:
            die(f"not enough room to copy '{schema}' (~{size_kb // 1024} MB "
                f"needed, {free_kb // 1024} MB free). Nothing was copied.")
        dump = run(["docker", "exec", "-u", "postgres", STORE_CONTAINER,
                    "pg_dump", "-n", schema, "postgres"]).stdout
        # Postgres has no "copy this schema" command (spec 2.3): dump,
        # rewrite the name inside the dump, restore under a NEW role.
        # A rehearsal never inherits production credentials (RFC-0030
        # D3 parity) -- so the role is created fresh, never reused.
        #
        # 'CREATE SCHEMA' is NOT run separately here -- the dump already
        # carries its own 'CREATE SCHEMA "..."' plus an
        # 'ALTER SCHEMA "..." OWNER TO "...";' (role name == schema name
        # in this design, so replacing the one string rewrites both).
        # Found on oaap-test, 2026-09-09: creating the schema twice made
        # the dump's own CREATE SCHEMA collide with mine, every time.
        # Only the ROLE has to exist beforehand -- the dump's OWNER TO
        # and GRANTs need it to already be there.
        rewritten = dump.replace(f'"{schema}"', f'"{new_schema}"')
        password = secrets.token_urlsafe(24)
        _store_psql(f'CREATE ROLE "{new_schema}" LOGIN PASSWORD \'{password}\';')
        run(["docker", "exec", "-i", "-u", "postgres", STORE_CONTAINER,
             "psql", "-v", "ON_ERROR_STOP=1", "postgres"], input=rewritten)
        print(f"Schema '{schema}' copied to '{new_schema}'.")
        print(f"Role:     {new_schema}  (a DIFFERENT role than '{schema}' — "
              "RFC-0030 D3 parity)")
        print(f"Password: {password}")
        return

    if args.action == "restore":
        dump_path = (args.arg1 or "").strip()
        if not dump_path or not os.path.isfile(dump_path):
            die("usage: oaap data store restore <dump-file> — file not found")
        before = {r["schema"] for r in store_schemas()}
        with open(dump_path, encoding="utf-8") as f:
            text = f.read()
        # Refuse rather than overwrite (spec 2.5): a restore must never
        # clobber a schema a fresh install already produced.
        clashes = sorted(s for s in before if f'"{s}"' in text or f" {s}." in text)
        if clashes:
            die("refusing to restore — these schemas already exist on "
                "this node: " + ", ".join(clashes))
        run(["docker", "exec", "-i", "-u", "postgres", STORE_CONTAINER,
             "psql", "-v", "ON_ERROR_STOP=1", "postgres"], input=text)
        after = {r["schema"] for r in store_schemas()} - before
        print(f"Restored {len(after)} schema(s): " + ", ".join(sorted(after))
              if after else "Restore ran; no new schema appeared — check "
                            "the dump was for this store.")
        return

    die(f"unknown action '{args.action}'")


def cmd_data_model(args):
    """'oaap data model ...' — the type registry (oaap.data.model 0.1).

    Every action needs the store the registry's tables actually live
    in (spec §2.1): a node without the profile, or with the profile but
    the service not running, gets the same two-step refusal 'store'
    itself uses -- never a raw docker/psql error.
    """
    if args.action not in ("types", "show", "register", "alias", "bindings"):
        die(f"unknown action '{args.action}' for 'data model' — available: "
            "types, show, register, alias, bindings")
    if not has_profile("store"):
        die("this node has no profile 'store' — the model registry lives "
            "in it (oaap.data.model 0.1 §2.1). Add it first: "
            "'sudo oaap node add-profile store'.")
    if not _store_running():
        die(f"the '{STORE_CONTAINER}' service is not running — see "
            "'oaap data store status'.")

    if args.action == "types":
        rows = model_types(tenant_id=args.tenant or None)
        if not rows:
            print("No types registered."
                  + (f" (activated for tenant '{args.tenant}')" if args.tenant else ""))
            return
        for t in rows:
            aliases = f"  aliases={','.join(t['aliases'])}" if t["aliases"] else ""
            print(f"{t['key']}  kind={t['kind']}  origin={t['origin']}  "
                  f"package={t['package']}  version={t['version']}{aliases}")
        return

    if args.action == "show":
        key = (args.arg1 or "").strip()
        if not key:
            die("usage: oaap data model show <type-key>")
        rec = model_show(key)
        if not rec:
            die(f"no such type '{key}' — see 'oaap data model types'")
        print(f"{rec['key']}  kind={rec['kind']}  origin={rec['origin']}  "
              f"package={rec['package']}  version={rec['version']}")
        print(f"aliases:           {', '.join(rec['aliases']) or '(none)'}")
        print(f"activated tenants: {', '.join(rec['activated_tenants']) or '(none)'}")
        print(f"definition:        {json.dumps(rec['definition'])}")
        return

    if args.action == "register":
        # The tenant's own CLI stand-in for the twin browser (Schritt 5,
        # RFC-0031 §4 third origin) -- one type, one kind, one file, until
        # the browser exists. 'kind' is read from the file itself so the
        # same shape used under a manifest's data_model.<kind> works here
        # unchanged: {"kind": "object_types", "key": ..., ...}.
        path = (args.arg1 or "").strip()
        tenant_id = (args.tenant or "").strip()
        if not path or not tenant_id:
            die("usage: oaap data model register <definition-file.yaml> "
                "--tenant <tenant-id>")
        if tenant_id not in load_tenants():
            die(f"unknown tenant '{tenant_id}' — 'oaap tenant list' shows the IDs")
        try:
            with open(path, encoding="utf-8") as f:
                entry = yaml.safe_load(f) or {}
        except OSError as e:
            die(f"cannot read '{path}': {e}")
        kind = entry.pop("kind", None)
        if kind not in MODEL_KINDS:
            die(f"'{path}': top-level 'kind' must be one of {', '.join(MODEL_KINDS)}")
        errs = validate_data_model_sections({"data_model": {kind: [entry]}})
        if errs:
            die("definition invalid:\n  - " + "\n  - ".join(errs))
        model_register_data_model("tenant", tenant_id, "1.0.0",
                                  {kind: [entry]}, tenant_id=tenant_id)
        print(f"Type '{entry['key']}' registered (origin: tenant, "
              f"tenant: {tenant_id}) and activated for that tenant.")
        return

    if args.action == "alias":
        key = (args.arg1 or "").strip()
        alias = (args.arg2 or "").strip()
        if not key or not alias:
            die("usage: oaap data model alias <type-key> <word>")
        model_add_alias(key, alias)
        print(f"Alias '{alias}' added for '{key}'.")
        return

    if args.action == "bindings":
        instance = (args.arg1 or "").strip()
        if not instance:
            die("usage: oaap data model bindings <instance>")
        rows = model_bindings_of(instance)
        if not rows:
            print(f"No bindings recorded for '{instance}'.")
            return
        for b in rows:
            extra = (f"  group={b['group_key']}" if b["group_key"]
                     else f"  fields={','.join(b['fields'])}" if b["fields"] else "")
            print(f"{b['direction']:11}  {b['type_key']}"
                  f"{'  as=' + b['declared_as'] if b['declared_as'] else ''}"
                  f"{'  role=' + b['role'] if b['role'] else ''}{extra}")
        return


def read_manifest_version(value):
    """Read 'oaap_manifest' tolerantly. Returns (minor, error).

    See MANIFEST_MAJOR above for why a higher minor is read rather than
    refused. Only a foreign major, or a version that is not a version at
    all, produces an error.
    """
    mv = re.fullmatch(r"(\d+)\.(\d+)", str(value or ""))
    if not mv:
        return -1, ('oaap_manifest: "MAJOR.MINOR" required, e.g. "0.1" '
                    f'(found: {value!r})')
    if int(mv.group(1)) != MANIFEST_MAJOR:
        return -1, (f'oaap_manifest "{value}": this platform reads '
                    f"{MANIFEST_MAJOR}.x manifests — update OAAP to install "
                    "this app")
    return int(mv.group(2)), ""


def validate_manifest(m):
    """Minimal validation mirroring oaap-spec/schema/oaap-app.schema.json.

    Deliberately more tolerant than that schema (RFC-0012 §8.2): the
    schema is an authoring tool, where a typo should be caught; a node in
    the field must still be able to read a manifest that is newer than
    itself. Strict schema, tolerant runtime.
    """
    errs = []
    minor, verr = read_manifest_version(m.get("oaap_manifest"))
    if verr:
        errs.append(verr)
    else:
        must = m.get("must_understand") or []
        if not isinstance(must, list) or any(not isinstance(f, str) for f in must):
            errs.append("must_understand: list of feature names expected")
        elif sorted(set(must) - MANIFEST_FEATURES):
            errs.append(
                "this app requires manifest features the platform does not "
                "understand: " + ", ".join(sorted(set(must) - MANIFEST_FEATURES))
                + " — update OAAP to install it")
        elif minor > MANIFEST_MINOR:
            print(f"Note: manifest {m['oaap_manifest']} is newer than this "
                  f"platform ({MANIFEST_MAJOR}.{MANIFEST_MINOR}). Installing "
                  "it anyway; anything it adds beyond that is ignored.")
    app = m.get("app") or {}
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", str(app.get("id", ""))):
        errs.append("app.id: lowercase [a-z0-9-], 3-40 chars")
    if not re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?", str(app.get("version", ""))):
        errs.append("app.version: semver required")
    if not app.get("name"):
        errs.append("app.name: required")
    # Not an error: an unrecognised class is ignored like any other
    # unknown value (RFC-0012 §8.1) and the app installs. But a typo
    # here silently costs or grants a launchpad tile, so say it out
    # loud rather than let it pass in silence.
    if app.get("class") and app["class"] not in APP_CLASSES:
        print(f"Note: app.class '{app['class']}' is not a class this "
              f"platform knows ({' | '.join(APP_CLASSES)}). Treating it as "
              f"'{DEFAULT_APP_CLASS}'.")
    # oaap.data.model 0.1 §2.8 / RFC-0012 §8.5: a 'data_models' artefact
    # is a package with data_model only -- no service, no route, no
    # health check, and (RFC-0031 §4) no app.type either, because it
    # never builds or runs a container. Recognised structurally (no
    # 'services' key at all), not by a declared class, because
    # app.class (2.10) is a launchpad-tile question, orthogonal to
    # whether a package has a container to begin with.
    is_artefact = bool(m.get("data_model")) and not m.get("services")
    if is_artefact:
        if m.get("routes") or m.get("endpoints") or m.get("storage"):
            errs.append("a data_models artefact (data_model, no services) must "
                        "not declare routes/endpoints/storage — it has no "
                        "service to attach them to (oaap.data.model 0.1 §2.8)")
    else:
        if app.get("type") not in ("native", "image", "wrapped"):
            errs.append("app.type: native | image | wrapped")
        # RFC-0016: more than one service is allowed. Each runs as its own
        # container on the instance's network; routes and storage may name a
        # target service, defaulting to the single one when there is only one.
        services = m.get("services") or {}
        if not services:
            errs.append("services: at least one service")
        for sname, svc in services.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(sname)):
                errs.append(f"service name invalid: '{sname}' (lowercase [a-z0-9-])")
            if not isinstance(svc.get("port"), int):
                errs.append(f"services.{sname}.port: integer required")
            if bool(svc.get("build")) == bool(svc.get("image")):
                errs.append(f"services.{sname}: exactly one of build/image")
        multi = len(services) > 1
        routes = m.get("routes") or []
        if not routes:
            errs.append("routes: at least one route")
        for r in routes:
            if not str(r.get("path", "")).startswith("/"):
                errs.append(f"routes: path must start with / ({r.get('path')})")
            roles = set(r.get("roles") or [])
            if not roles or not roles <= ROLES:
                errs.append(f"routes {r.get('path')}: roles must be non-empty subset of {sorted(ROLES)}")
            # a route's target service must exist; it may be omitted only when
            # there is exactly one service to mean (RFC-0016)
            rsvc = r.get("service")
            if rsvc is not None and rsvc not in services:
                errs.append(f"routes {r.get('path')}: unknown service '{rsvc}'")
            elif rsvc is None and multi:
                errs.append(f"routes {r.get('path')}: 'service' is required when the "
                            "app has more than one service")
        for s in m.get("storage") or []:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(s.get("name", ""))) or not str(s.get("mount", "")).startswith("/"):
                errs.append(f"storage entry invalid: {s}")
            ssvc = s.get("service")
            if ssvc is not None and ssvc not in services:
                errs.append(f"storage {s.get('name')}: unknown service '{ssvc}'")
            elif ssvc is None and multi:
                errs.append(f"storage {s.get('name')}: 'service' is required when the "
                            "app has more than one service")
        # RFC-0015: at most one non-HTTP endpoint, declared but not published
        # until an operator grants it on an 'exposed' node.
        endpoints = m.get("endpoints") or []
        if len(endpoints) > 1:
            errs.append("endpoints: at most one endpoint per app (RFC-0015)")
        for e in endpoints:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(e.get("name", ""))):
                errs.append(f"endpoint name invalid: '{e.get('name')}'")
            if e.get("protocol") not in ("udp", "tcp", "both"):
                errs.append(f"endpoint {e.get('name')}: protocol must be udp | tcp | both")
            if not isinstance(e.get("container_port"), int):
                errs.append(f"endpoint {e.get('name')}: container_port (integer) required")
            if e.get("wish") is not None and not isinstance(e.get("wish"), int):
                errs.append(f"endpoint {e.get('name')}: wish must be an integer port")
            # RFC-0015 §fixed (RFC-0017 §5.1): a server that advertises its own
            # port to clients (a media server: ICE candidates carry the exact
            # port) cannot accept a silently-reassigned one. `fixed: true` makes
            # the port a requirement — published unchanged on the host, grant
            # fails loudly if taken. Because host_port then equals container_port,
            # the number must live in the endpoint range so it cannot collide
            # with a platform port (80/443, gateway 8100-8199, internals).
            if e.get("fixed") is not None and not isinstance(e.get("fixed"), bool):
                errs.append(f"endpoint {e.get('name')}: fixed must be true or false")
            if e.get("fixed") and isinstance(e.get("container_port"), int) \
                    and e["container_port"] not in ENDPOINT_PORT_RANGE:
                errs.append(f"endpoint {e.get('name')}: a fixed endpoint's "
                            f"container_port must be in {ENDPOINT_PORT_RANGE.start}–"
                            f"{ENDPOINT_PORT_RANGE.stop - 1} (it is published "
                            "unchanged on the host, so it must not clash with a "
                            "platform port)")
            if not str(e.get("reason") or "").strip():
                errs.append(f"endpoint {e.get('name')}: a 'reason' is required — it is "
                            "shown to the operator verbatim at grant time")
            esvc = e.get("service")
            if esvc is not None and esvc not in services:
                errs.append(f"endpoint {e.get('name')}: unknown service '{esvc}'")
            elif esvc is None and multi:
                errs.append(f"endpoint {e.get('name')}: 'service' is required when the "
                            "app has more than one service")
        if not str((m.get("health") or {}).get("path", "")).startswith("/"):
            errs.append("health.path: required, must start with /")
    errs.extend(validate_data_model_sections(m))
    if errs:
        die("manifest invalid:\n  - " + "\n  - ".join(errs))


def validate_data_model_sections(m):
    """Structural checks for 'data_model'/'contributes'/'consumes'
    (manifest 0.3, RFC-0031 §5, oaap.data.model 0.1 §2.2). Pure — no
    registry lookup here; whether a 'type:' word actually RESOLVES to
    something is a binding-time question (§2.4), answered only on a
    node that carries the store, at install.

    Returns a list of error strings (empty = valid); the caller decides
    what to do with them, same shape as every other manifest.py check
    in validate_manifest above.
    """
    errs = []
    dm = m.get("data_model") or {}
    if dm and not isinstance(dm, dict):
        return ["data_model: mapping expected"]
    for kind in MODEL_KINDS:
        for entry in dm.get(kind) or []:
            if not isinstance(entry, dict):
                errs.append(f"data_model.{kind}: each entry must be a mapping")
                continue
            key = str(entry.get("key", ""))
            key_re = _MODEL_GROUP_KEY_RE if kind == "group_types" else _MODEL_KEY_RE
            if not key or not key_re.fullmatch(key):
                errs.append(f"data_model.{kind}: key '{key}' invalid "
                            + ("(dotted lowercase, e.g. 'crm.core')" if kind == "group_types"
                               else "([A-Za-z][A-Za-z0-9]*)"))
            if kind == "attribute_types" and entry.get("value_type") not in MODEL_VALUE_TYPES:
                errs.append(f"data_model.attribute_types.{key}: value_type must be one "
                            f"of {' | '.join(MODEL_VALUE_TYPES)}")
            if kind == "group_types" and not str(entry.get("on", "")).strip():
                errs.append(f"data_model.group_types.{key}: 'on' (the object type it "
                            "belongs to) is required")
            if kind == "relation_types":
                if not str(entry.get("from", "")).strip() or not str(entry.get("to", "")).strip():
                    errs.append(f"data_model.relation_types.{key}: 'from' and 'to' "
                                "(object type keys) are required")
    for i, c in enumerate(m.get("contributes") or []):
        if not str((c or {}).get("type", "")).strip():
            errs.append(f"contributes[{i}]: 'type' is required")
        role = (c or {}).get("role", "owner")
        if role not in ("owner", "contributor"):
            errs.append(f"contributes[{i}]: role must be owner | contributor")
        if role == "contributor" and not str((c or {}).get("group", "")).strip():
            errs.append(f"contributes[{i}]: role 'contributor' requires a 'group'")
    for i, c in enumerate(m.get("consumes") or []):
        if not str((c or {}).get("type", "")).strip():
            errs.append(f"consumes[{i}]: 'type' is required")
    return errs


def declaration_text(contributes, consumes):
    """The sentence RFC-0031 D7 asks to be shown before an install with
    data-region declarations proceeds -- built from the manifest ALONE
    (no registry lookup), so it must be produced even on a node without
    the store (oaap.data.model 0.1 §2.7). '' when neither section says
    anything, so the caller can skip printing rather than print silence.
    """
    def join_and(words):
        words = list(words)
        if not words:
            return ""
        if len(words) == 1:
            return words[0]
        return ", ".join(words[:-1]) + " and " + words[-1]

    consumes = consumes or []
    contributes = contributes or []
    parts = []
    reads = [str(c.get("type", "?")) for c in consumes]
    if reads:
        parts.append(f"reads {join_and(reads)}")
    writes = []
    for c in contributes:
        t = str(c.get("type", "?"))
        if c.get("role") == "contributor" and c.get("group"):
            writes.append(f"{t} (contributor: {c['group']})")
        else:
            writes.append(t)
    if writes:
        parts.append(f"writes into {join_and(writes)}")
    if not parts:
        return ""
    return "this app " + " and ".join(parts) + "."


def type_change_kind(old, new):
    """Compare two versions of the SAME type's definition (RFC-0031 §4,
    D2; oaap.data.model 0.1 §2.5). Returns 'unchanged' | 'additive' |
    'destructive'. Pure -- no registry access, so this is testable
    without a node at all.

    'old'/'new' are the entry mappings as they appear under one
    data_model.<kind> list (e.g. one object_types entry, one
    group_types entry). List-valued fields (a group type's own
    attributes/relations/activities) may only GROW; every other change
    to a value already present is destructive, on the conservative
    reading described in the spec (0.1 has no twin yet to ask whether
    a removed member actually has instances).
    """
    if old == new:
        return "unchanged"
    list_fields = ("attributes", "relations", "activities")
    scalar_old = {k: v for k, v in old.items() if k not in list_fields}
    scalar_new = {k: v for k, v in new.items() if k not in list_fields}
    for k, v in scalar_old.items():
        if k in ("title", "title_plural", "description", "tags"):
            continue  # presentation only -- never destructive
        if k not in scalar_new or scalar_new[k] != v:
            return "destructive"
    for field in list_fields:
        old_members = set(old.get(field) or [])
        new_members = set(new.get(field) or [])
        if not old_members <= new_members:
            return "destructive"
    return "additive" if new != old else "unchanged"


def bind_candidates(word, type_index):
    """Which registered type key(s) match a manifest 'type:'/'as:' word
    (RFC-0031 D4; oaap.data.model 0.1 §2.4). Pure -- 'type_index' is
    [{"key": ..., "aliases": [...]}, ...], already read from the
    registry by the caller, so this resolution logic is testable
    without Postgres.

    An exact (case-insensitive) key match wins outright, even if the
    same word is ALSO someone's alias elsewhere -- the platform's own
    name for a type is never ambiguous with a nickname. Otherwise every
    case-insensitive alias match is a candidate; zero or more than one
    is what the caller turns into a refusal.
    """
    word_l = word.strip().lower()
    for t in type_index:
        if t["key"].lower() == word_l:
            return [t["key"]]
    return sorted({t["key"] for t in type_index
                   for a in t.get("aliases", []) if a.lower() == word_l})


# ------------------------------------------------ oaap.data.model (Postgres)
#
# Four tables in schema 'oaap_model' of the SAME store oaap.data.store
# already manages -- this capability has no infrastructure of its own
# (spec §2.1). Every function below is a thin, single-purpose wrapper
# around _store_psql; the actual decisions (does this change conflict,
# does this word resolve) are the pure functions above, so they can be
# tested without Docker at all -- only these need a running store.
MODEL_SCHEMA = "oaap_model"


def _sql_lit(value):
    """A single-quoted SQL string literal. Safe under
    'standard_conforming_strings = on', Postgres's default since 9.1
    and unchanged by the pgvector image -- the only escaping this
    file's f-string SQL needs anywhere (see _store_psql call sites)."""
    return "'" + str(value).replace("'", "''") + "'"


def _model_ensure_schema():
    """Idempotent -- called before every read or write below, same
    posture as CREATE SCHEMA/TABLE IF NOT EXISTS everywhere else in
    this file. No migration framework needed for four tables that only
    ever gain columns one at a time, by hand, like the rest of appctl."""
    _store_psql("""
        CREATE SCHEMA IF NOT EXISTS oaap_model;
        CREATE TABLE IF NOT EXISTS oaap_model.type_definitions (
            key text PRIMARY KEY,
            kind text NOT NULL,
            origin text NOT NULL,
            package text NOT NULL,
            version text NOT NULL,
            definition text NOT NULL,
            registered_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS oaap_model.type_aliases (
            type_key text NOT NULL REFERENCES oaap_model.type_definitions(key)
                ON DELETE CASCADE,
            alias text NOT NULL,
            PRIMARY KEY (type_key, alias)
        );
        CREATE TABLE IF NOT EXISTS oaap_model.activations (
            tenant_id text NOT NULL,
            type_key text NOT NULL REFERENCES oaap_model.type_definitions(key)
                ON DELETE CASCADE,
            activated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, type_key)
        );
        CREATE TABLE IF NOT EXISTS oaap_model.bindings (
            instance text NOT NULL,
            type_key text NOT NULL REFERENCES oaap_model.type_definitions(key)
                ON DELETE CASCADE,
            direction text NOT NULL,
            declared_as text NOT NULL DEFAULT '',
            role text NOT NULL DEFAULT '',
            group_key text NOT NULL DEFAULT '',
            fields text NOT NULL DEFAULT '',
            bound_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (instance, type_key, direction)
        );
    """)


def model_types(tenant_id=None):
    """[{key, kind, origin, package, version, aliases: [...]}, ...] --
    all registered types, or only those activated for one tenant."""
    _model_ensure_schema()
    if tenant_id:
        rows = _store_psql(
            "SELECT d.key, d.kind, d.origin, d.package, d.version "
            "FROM oaap_model.type_definitions d "
            "JOIN oaap_model.activations a ON a.type_key = d.key "
            f"WHERE a.tenant_id = {_sql_lit(tenant_id)} ORDER BY d.key"
        ).stdout
    else:
        rows = _store_psql(
            "SELECT key, kind, origin, package, version "
            "FROM oaap_model.type_definitions ORDER BY key"
        ).stdout
    out = []
    for line in rows.splitlines():
        if not line.strip():
            continue
        key, kind, origin, package, version = line.split("|")
        out.append({"key": key, "kind": kind, "origin": origin,
                    "package": package, "version": version,
                    "aliases": _model_aliases_of(key)})
    return out


def _model_aliases_of(type_key):
    out = _store_psql(
        "SELECT alias FROM oaap_model.type_aliases WHERE type_key = "
        f"{_sql_lit(type_key)} ORDER BY alias"
    ).stdout
    return [a for a in (l.strip() for l in out.splitlines()) if a]


def model_type_index():
    """[{"key":, "aliases": [...]}, ...] for bind_candidates() -- one
    query pass, not one alias lookup per candidate."""
    _model_ensure_schema()
    keys = _store_psql("SELECT key FROM oaap_model.type_definitions ORDER BY key").stdout
    return [{"key": k, "aliases": _model_aliases_of(k)}
            for k in (l.strip() for l in keys.splitlines()) if k]


def model_show(type_key):
    """One type's full record, or None -- kind/origin/package/version/
    definition (parsed back to a dict)/aliases/activated tenants."""
    _model_ensure_schema()
    out = _store_psql(
        "SELECT kind, origin, package, version, definition FROM "
        f"oaap_model.type_definitions WHERE key = {_sql_lit(type_key)}"
    ).stdout.strip()
    if not out:
        return None
    kind, origin, package, version, definition = out.split("|", 4)
    tenants = _store_psql(
        "SELECT tenant_id FROM oaap_model.activations WHERE type_key = "
        f"{_sql_lit(type_key)} ORDER BY tenant_id"
    ).stdout
    return {"key": type_key, "kind": kind, "origin": origin, "package": package,
            "version": version, "definition": json.loads(definition),
            "aliases": _model_aliases_of(type_key),
            "activated_tenants": [t for t in (l.strip() for l in tenants.splitlines()) if t]}


def model_add_alias(type_key, alias):
    _model_ensure_schema()
    if not _store_psql(f"SELECT 1 FROM oaap_model.type_definitions "
                       f"WHERE key = {_sql_lit(type_key)}").stdout.strip():
        die(f"no such type '{type_key}' — see 'oaap data model types'")
    _store_psql(
        f"INSERT INTO oaap_model.type_aliases (type_key, alias) "
        f"VALUES ({_sql_lit(type_key)}, {_sql_lit(alias)}) "
        "ON CONFLICT (type_key, alias) DO NOTHING;")


def model_register_data_model(origin, package, version, data_model, tenant_id=None):
    """Register every entry of a 'data_model' section (spec §2.3).
    All-or-nothing: every entry is checked against the registry BEFORE
    any is written, so a conflict on entry five of six never leaves
    the first four registered on their own. Dies (via die()) naming the
    offending entry on any ownership violation or destructive change.

    tenant_id is set only for the CLI 'register' action (§2.3's
    tenant-origin case, where 'package' is the tenant-id itself); an
    app install passes None -- app-origin types are node-wide from the
    start (D2), never scoped to one tenant at registration time.
    """
    _model_ensure_schema()
    plan = []  # [(kind, key, action, entry)]
    for kind in MODEL_KINDS:
        for entry in data_model.get(kind) or []:
            key = entry["key"]
            existing = _store_psql(
                "SELECT origin, definition FROM oaap_model.type_definitions "
                f"WHERE key = {_sql_lit(key)}"
            ).stdout.strip()
            if not existing:
                plan.append((kind, key, "create", entry))
                continue
            ex_origin, ex_def = existing.split("|", 1)
            if ex_origin != origin:
                die(f"data_model.{kind}.{key}: already registered by "
                    f"'{ex_origin}' — an app cannot alter a type it does "
                    "not own (RFC-0031 D1)")
            change = type_change_kind(json.loads(ex_def), entry)
            if change == "destructive":
                die(f"data_model.{kind}.{key}: this change removes or "
                    "redefines part of a type already registered "
                    "(oaap.data.model 0.1 §2.5) — refused. 0.1 has no "
                    "twin yet to ask whether the removed part has "
                    "instances, so every destructive change is refused "
                    "unconditionally; add the new shape under a "
                    "different key instead.")
            plan.append((kind, key, change, entry))
    for kind, key, action, entry in plan:
        if action == "create":
            _store_psql(
                "INSERT INTO oaap_model.type_definitions "
                "(key, kind, origin, package, version, definition) VALUES ("
                f"{_sql_lit(key)}, {_sql_lit(kind)}, {_sql_lit(origin)}, "
                f"{_sql_lit(package)}, {_sql_lit(version)}, "
                f"{_sql_lit(json.dumps(entry))});")
        elif action == "additive":
            _store_psql(
                "UPDATE oaap_model.type_definitions SET version = "
                f"{_sql_lit(version)}, definition = {_sql_lit(json.dumps(entry))} "
                f"WHERE key = {_sql_lit(key)};")
        # 'unchanged' -- nothing to write
    if tenant_id:
        for _, key, _, _ in plan:
            _store_psql(
                "INSERT INTO oaap_model.activations (tenant_id, type_key) "
                f"VALUES ({_sql_lit(tenant_id)}, {_sql_lit(key)}) "
                "ON CONFLICT DO NOTHING;")
    return plan


def model_bind_declarations(instance, tenant_id, contributes, consumes, explicit=None):
    """Bind every contributes/consumes entry to a registered type,
    activate it for tenant_id, record the binding (spec §2.4).

    'explicit' is {manifest-word: platform-key} from '--bind' -- checked
    first, so an operator's answer to an earlier refusal always wins
    over automatic matching. Dies listing every entry that still has
    zero or more than one candidate, so ALL problems are shown at once
    rather than one refusal per re-run.
    """
    explicit = explicit or {}
    index = model_type_index()
    known = {t["key"] for t in index}
    resolved = []   # (direction, entry, type_key)
    problems = []
    for entry in contributes or []:
        word = entry["type"]
        key = explicit.get(word)
        if key and key not in known:
            problems.append(f"contributes '{word}': --bind names unknown type '{key}'")
            continue
        if not key:
            cands = bind_candidates(word, index)
            if len(cands) == 1:
                key = cands[0]
            else:
                problems.append(f"contributes '{word}': "
                                 + (f"{len(cands)} candidates ({', '.join(cands)})"
                                    if cands else "no matching registered type")
                                 + f" — resolve with --bind {word}=<type-key>")
                continue
        resolved.append(("contributes", entry, key))
    for entry in consumes or []:
        word = entry.get("as") or entry["type"]
        match_word = entry["type"]
        key = explicit.get(word) or explicit.get(match_word)
        if key and key not in known:
            problems.append(f"consumes '{match_word}': --bind names unknown type '{key}'")
            continue
        if not key:
            cands = bind_candidates(match_word, index)
            if len(cands) == 1:
                key = cands[0]
            else:
                problems.append(f"consumes '{match_word}': "
                                 + (f"{len(cands)} candidates ({', '.join(cands)})"
                                    if cands else "no matching registered type")
                                 + f" — resolve with --bind {match_word}=<type-key>")
                continue
        resolved.append(("consumes", entry, key))
    if problems:
        die("data-model binding failed:\n  - " + "\n  - ".join(problems))
    for direction, entry, key in resolved:
        _store_psql(
            "INSERT INTO oaap_model.activations (tenant_id, type_key) VALUES "
            f"({_sql_lit(tenant_id)}, {_sql_lit(key)}) ON CONFLICT DO NOTHING;")
        _store_psql(
            "INSERT INTO oaap_model.bindings "
            "(instance, type_key, direction, declared_as, role, group_key, fields) "
            f"VALUES ({_sql_lit(instance)}, {_sql_lit(key)}, {_sql_lit(direction)}, "
            f"{_sql_lit(entry.get('as', ''))}, {_sql_lit(entry.get('role', ''))}, "
            f"{_sql_lit(entry.get('group', ''))}, "
            f"{_sql_lit(','.join(entry.get('fields') or []))}) "
            "ON CONFLICT (instance, type_key, direction) DO UPDATE SET "
            "declared_as = EXCLUDED.declared_as, role = EXCLUDED.role, "
            "group_key = EXCLUDED.group_key, fields = EXCLUDED.fields, "
            "bound_at = now();")
    return resolved


def model_bindings_of(instance):
    _model_ensure_schema()
    out = _store_psql(
        "SELECT type_key, direction, declared_as, role, group_key, fields "
        f"FROM oaap_model.bindings WHERE instance = {_sql_lit(instance)} "
        "ORDER BY direction, type_key"
    ).stdout
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        type_key, direction, declared_as, role, group_key, fields = line.split("|")
        rows.append({"type_key": type_key, "direction": direction,
                    "declared_as": declared_as, "role": role,
                    "group_key": group_key,
                    "fields": fields.split(",") if fields else []})
    return rows


# ------------------------------------------ oaap.data.twin (RFC-0031 Schritt 3)
# The digital twin has its own service (platform/services/twin) reached
# through the gateway's '/twin/*' route -- appctl's job here stops at
# what only the host can do: mint the machine-principal credential an
# instance presents (E1), and provision the tenant's twin_<id> schema,
# role and tables the twin service will connect to (D8/oaap.data.store
# 0.1 §2: the schema role is shown once, to no app, only to this).
TWIN_SECRETS_FILE = os.path.join(APPS_DIR, "twin-secrets.json")


def _twin_secrets_load():
    try:
        with open(TWIN_SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _twin_secrets_save(secrets_map):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = TWIN_SECRETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(secrets_map, f, indent=2)
    os.replace(tmp, TWIN_SECRETS_FILE)
    # oaap.data.twin's own secret -- the twin container's read-only
    # mount is the only reader besides this host; never an app's.
    os.chmod(TWIN_SECRETS_FILE, 0o600)


def _twin_ensure_schema(tenant_id):
    """Idempotent: the tenant's twin_<id> schema, its own Postgres role,
    and the append-only tables/views this build's twin service reads
    and writes (RFC-0031 §3.4 -- recorded time always; validity carried,
    not yet filtered; no tree, no references, no merge in 0.1 -- see
    the capability spec's §2 for what that means).

    Called from _install_from_dir once an instance contributes or
    consumes a type, same gate as oaap.data.model's own registration
    (has_profile('store') and _store_running(), checked by the caller)."""
    schema = f"twin_{tenant_id}"
    existing = {r["schema"] for r in store_schemas()}
    secrets_map = _twin_secrets_load()
    if schema not in existing:
        password = secrets.token_urlsafe(24)
        _store_psql(f'CREATE ROLE "{schema}" LOGIN PASSWORD \'{password}\';')
        _store_psql(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{schema}";')
        secrets_map[schema] = {"role": schema, "password": password}
        _twin_secrets_save(secrets_map)
    elif schema not in secrets_map:
        # Self-healing, same posture as migrate.sh's OAAP_INTERNAL_KEY
        # repair: the schema survived (e.g. a restore) but the secret
        # file did not -- reset the role's password rather than guess
        # a lost one.
        password = secrets.token_urlsafe(24)
        _store_psql(f'ALTER ROLE "{schema}" PASSWORD \'{password}\';')
        secrets_map[schema] = {"role": schema, "password": password}
        _twin_secrets_save(secrets_map)
    # The registry the twin reads (oaap.data.model 0.1 §2 calls it
    # exactly that). Granted every time -- harmless once already
    # granted, and repairs a schema a human created by hand.
    _store_psql(
        f'GRANT USAGE ON SCHEMA oaap_model TO "{schema}"; '
        f'GRANT SELECT ON ALL TABLES IN SCHEMA oaap_model TO "{schema}"; '
        "ALTER DEFAULT PRIVILEGES IN SCHEMA oaap_model "
        f'GRANT SELECT ON TABLES TO "{schema}";')
    _twin_ensure_tables(schema)


def _twin_ensure_tables(schema):
    """objects/source_keys/groups/attributes/relations/activities/events,
    plus the current_* views the service actually queries -- append-only
    everywhere a row can change (D5), 'current' meaning 'superseded_by
    IS NULL', never a DELETE or UPDATE of the value itself."""
    _store_psql(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".objects (
            id uuid PRIMARY KEY,
            type_key text NOT NULL,
            title text NOT NULL,
            owner_origin text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            recorded_by text NOT NULL
        );
        CREATE TABLE IF NOT EXISTS "{schema}".source_keys (
            object_id uuid NOT NULL REFERENCES "{schema}".objects(id) ON DELETE CASCADE,
            origin text NOT NULL,
            source_key text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            recorded_by text NOT NULL,
            PRIMARY KEY (object_id, origin)
        );
        CREATE TABLE IF NOT EXISTS "{schema}".groups (
            object_id uuid NOT NULL REFERENCES "{schema}".objects(id) ON DELETE CASCADE,
            group_key text NOT NULL,
            origin text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            created_by text NOT NULL,
            PRIMARY KEY (object_id, group_key)
        );
        CREATE TABLE IF NOT EXISTS "{schema}".attributes (
            id bigserial PRIMARY KEY,
            object_id uuid NOT NULL,
            group_key text NOT NULL,
            attr_key text NOT NULL,
            value jsonb NOT NULL,
            valid_from date,
            valid_to date,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            recorded_by text NOT NULL,
            superseded_by bigint,
            FOREIGN KEY (object_id, group_key)
                REFERENCES "{schema}".groups(object_id, group_key)
        );
        CREATE TABLE IF NOT EXISTS "{schema}".relations (
            id bigserial PRIMARY KEY,
            object_id uuid NOT NULL,
            group_key text NOT NULL,
            rel_key text NOT NULL,
            target_id uuid NOT NULL,
            valid_from date,
            valid_to date,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            recorded_by text NOT NULL,
            superseded_by bigint,
            FOREIGN KEY (object_id, group_key)
                REFERENCES "{schema}".groups(object_id, group_key)
        );
        CREATE TABLE IF NOT EXISTS "{schema}".activities (
            id bigserial PRIMARY KEY,
            object_id uuid NOT NULL,
            group_key text NOT NULL,
            activity_key text NOT NULL,
            target_id uuid,
            status text NOT NULL DEFAULT 'open',
            planned_start timestamptz,
            planned_end timestamptz,
            started_at timestamptz,
            finished_at timestamptz,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            recorded_by text NOT NULL,
            superseded_by bigint,
            FOREIGN KEY (object_id, group_key)
                REFERENCES "{schema}".groups(object_id, group_key)
        );
        CREATE TABLE IF NOT EXISTS "{schema}".events (
            id bigserial PRIMARY KEY,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            kind text NOT NULL,
            object_id uuid,
            group_key text NOT NULL DEFAULT '',
            origin text NOT NULL
        );
        CREATE OR REPLACE VIEW "{schema}".current_attributes AS
            SELECT * FROM "{schema}".attributes WHERE superseded_by IS NULL;
        CREATE OR REPLACE VIEW "{schema}".current_relations AS
            SELECT * FROM "{schema}".relations WHERE superseded_by IS NULL;
        CREATE OR REPLACE VIEW "{schema}".current_activities AS
            SELECT * FROM "{schema}".activities WHERE superseded_by IS NULL;
        GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO "{schema}";
        GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{schema}";
        ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT ALL ON TABLES TO "{schema}";
        ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT ALL ON SEQUENCES TO "{schema}";
    """)



# RFC-0027 D5's --instance scoping limits a key to one app's own site,
# named by that app (app.id: '[a-z0-9][a-z0-9-]{1,38}[a-z0-9]', never a
# dot). 'oaap.twin' therefore names a scope no real app instance can
# EVER hold -- not a reservation this platform has to remember and
# enforce elsewhere, a syntactic impossibility. Used only here and in
# the Caddyfile's '/twin/*' block; the two must always agree.
TWIN_KEY_SCOPE = "oaap.twin"


def _twin_issue_instance_key(name, tenant_id):
    """Mint the machine principal 'instance:<name>' (RFC-0027 3.1) and an
    API key for it -- the credential this instance presents to
    oaap.data.twin so origin and tenant come from the credential, never
    the request (RFC-0031 §8, E1). Returns the full bearer token.

    Issued WITH --instance scoping, to the reserved value TWIN_KEY_SCOPE
    (RFC-0027 D5) -- not left unscoped. An unscoped key is refused
    nowhere except by role and tenant (that is what D5 is for: an app's
    own site always names itself in the forward_auth call), so an
    unscoped twin key would authenticate against every OTHER app's own
    route requiring only role 'user' in the same tenant, not only
    '/twin/*' -- found live on oaap-test 2026-09-10 (CURRENT_STATE 125)
    by testing the very thing this docstring used to claim was safe.
    """
    principal = f"instance:{name}"
    out = _identity_exec(
        "import json, os, app as m\n"
        "users = m.load_users()\n"
        "name = os.environ['OAAP_T_NAME']\n"
        "if not m.find_user(users, name):\n"
        "    users.append({'username': name,\n"
        "                  'display_name': os.environ['OAAP_T_INST'],\n"
        "                  'password_hash': '', 'kind': 'machine',\n"
        "                  'roles': ['user'], 'groups': [],\n"
        "                  'tenant': os.environ['OAAP_T_TENANT'], 'active': True})\n"
        "    m._save(m.USERS_FILE, users)\n"
        "rec, secret = m.issue_key(users, name, ['user'], os.environ['OAAP_T_SCOPE'],\n"
        "    'oaap.data.twin (RFC-0031 E1)', m.KEY_MAX_DAYS, 'root')\n"
        "print(json.dumps(secret))\n",
        {"OAAP_T_NAME": principal, "OAAP_T_INST": name, "OAAP_T_TENANT": tenant_id,
         "OAAP_T_SCOPE": TWIN_KEY_SCOPE})
    return json.loads(out)


def image_uid(image):
    try:
        out = run(["docker", "run", "--rm", "--entrypoint", "id", image, "-u"]).stdout.strip()
        return int(out)
    except Exception:
        return None


DEFAULT_THROTTLE = {"limit": 300, "window": 60}
# identity runs several gunicorn workers and each counts on its own
# (RFC-0010) — kept here so the CLI can state the real ceiling
IDENTITY_WORKERS = 2


def throttle_of(inst):
    """Effective throttle for an instance ({} = explicitly switched off)."""
    t = inst.get("throttle")
    return DEFAULT_THROTTLE if t is None else t


def _throttle_block(scope, throttle, edge):
    """Gateway-side request brake for a public route (RFC-0010).

    Public routes get no forward_auth for identity, so this is the only
    place the platform can still say "enough". The client address is
    handed to identity explicitly: in direct mode the TCP peer is the
    client, behind an edge the peer is the edge and the real client
    stands in X-Forwarded-For (which the edge overwrites, so it cannot
    be spoofed). Deriving it inside identity would get this wrong.
    """
    if not throttle:
        return []
    client = ("{http.request.header.X-Forwarded-For}" if edge
              else "{http.request.remote.host}")
    return [
        "\t\tforward_auth identity:8000 {",
        f"\t\t\turi /throttle?scope={scope}&limit={throttle['limit']}"
        f"&window={throttle['window']}",
        f"\t\t\theader_up X-OAAP-Client {client}",
        *_AUTH_NO_UPGRADE,
        "\t\t}",
    ]


# forward_auth hands the ORIGINAL request headers to the auth endpoint.
# For a WebSocket handshake that includes Connection: Upgrade and
# Upgrade: websocket, which makes the WSGI server reject the auth
# subrequest with 400 — and forward_auth passes that straight back to
# the client, so the handshake dies before the app ever sees it. The
# auth call is a plain GET; these hop-by-hop headers have no business
# in it. Without this, App Deployment Contract guarantee 7 (WebSocket
# and SSE pass through) does not hold on ANY authenticated route.
_AUTH_NO_UPGRADE = ["\t\t\theader_up -Connection", "\t\t\theader_up -Upgrade"]


# ------------------------------------------------- rehearsals (RFC-0030)
#
# A rehearsal instance carries the CODE of a test instance and a COPY of
# a production instance's data (spec 2.15). It is an ordinary instance
# on the production channel; the `rehearsal` block is the only thing
# that makes it one, and its presence is what every refusal below asks
# about.
#
# Default lifetime and the step every extension moves it by (D4). Seven
# days is long enough to look at a migration and short enough that a
# forgotten copy of live customer data does not become the weakest point
# on the node.
REHEARSAL_DAYS = 7
REHEARSAL_STEP = 7


def is_rehearsal(inst):
    """Is this instance a rehearsal? One reader for one question."""
    return bool((inst or {}).get("rehearsal"))


# Which registry keys are rehearsals, cached against the registry file's
# own mtime and size.
#
# Why a cache and not a parameter everywhere: the `public` refusal
# (2.15.2) has to hold at EVERY place a gateway site is written, and
# there are nine of them. A parameter threaded through nine call sites
# fails SILENTLY at the one that was forgotten -- and it fails towards
# "serve this route without a login on a copy of production data". So
# site_body() asks this instead, from the instance scope it already
# receives, and a caller that knows better (an install, where the
# registry entry does not exist yet) says so explicitly.
_REHEARSAL_CACHE = {"stamp": None, "keys": frozenset()}


def rehearsal_keys():
    try:
        st = os.stat(REGISTRY)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return frozenset()
    if _REHEARSAL_CACHE["stamp"] != stamp:
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, ValueError):
            return _REHEARSAL_CACHE["keys"]
        _REHEARSAL_CACHE["keys"] = frozenset(
            k for k, i in (reg.get("instances") or {}).items() if is_rehearsal(i))
        _REHEARSAL_CACHE["stamp"] = stamp
    return _REHEARSAL_CACHE["keys"]


def rehearsal_refusal(reg, name, what):
    """The refusal a rehearsal owes for an outward-facing change, or "".

    One sentence, and it names WHY -- an operator who meets it is doing
    something reasonable on the wrong instance, and needs to be told
    which instance that is rather than that the command is unavailable.
    """
    if not is_rehearsal((reg.get("instances") or {}).get(name)):
        return ""
    return (f"'{name}' is a rehearsal (RFC-0030) — {what} is not carried "
            f"over and cannot be added. A rehearsal holds a copy of "
            f"production data and reaches nothing outward; it is reachable "
            f"through the portal and this node's automatic name, and it "
            f"disappears on its own. Do this on the production instance "
            f"instead.")


def rehearsal_days_left(inst, now=None):
    """Whole days until this rehearsal expires; negative once it has.

    None for anything that is not a rehearsal, and None when the record
    carries no readable date -- a page that has to say "expired" must
    not say it because it failed to parse a string.
    """
    import datetime
    stamp = ((inst or {}).get("rehearsal") or {}).get("expires") or ""
    try:
        when = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (when - now).days if when >= now else -((now - when).days + 1)


def site_body(routes, container, svc_port, groups=None, scope="", throttle=None,
              edge="", services=None, tenant="", login_only=None):
    """Shared handler block for one app instance (LAN and external sites).

    /auth/* is reserved on every entry point, not only the portal apex
    (mirrors the main gateway Caddyfile and _portal_site_body()). Without
    it, a session started directly on an app's own port/subdomain — no
    prior visit to the portal — hits an infinite redirect loop: identity's
    /verify sends an unauthenticated visitor to the relative "/auth/login",
    which on a bare instance site has no dedicated handler and falls
    through to the app's own catch-all route, re-triggering forward_auth.
    Placing it first also protects it from any app route of the same
    name. Safe to expose everywhere: the session cookie is scoped to the
    whole registered external hostname (DomainAwareSessionInterface), so
    logging in here authenticates the user platform-wide, and login
    redirects to "/" — back to this same instance.

    groups: optional visibility restriction (RFC-0007) from the
    instance's registry entry — an ADDITIONAL check alongside roles,
    added to every non-public route's forward_auth call.

    tenant: the instance's tenant id (oaap.core.tenant 3.1). This is
    where the boundary of belonging is actually enforced — at the
    gateway, before the app is reached, never inside the app. It is
    written out ALWAYS, even while the node has one tenant: a file that
    already carries the answer needs no rewriting on the day a second
    tenant appears, and there is no window in which the boundary is
    merely intended. The RAW stored value is passed, not a resolved
    one, so an instance whose tenant this node does not have refuses
    everyone but a server_admin instead of quietly falling back to the
    operator's own tenant (spec 2.5).

    services: for a multi-container app (RFC-0016), a map service name ->
    (container, port); each route is proxied to the container of its
    declared `service`. None (single service) proxies every route to the
    given container:svc_port, exactly as before.

    login_only: a rehearsal serves NO public route (spec 2.15.2) — every
    route asks identity, including one the manifest declares `public`.
    Enforced HERE, where the site is written, and never by rewriting the
    stored routes: a manifest copy that no longer says what the manifest
    says is a second, untrue record of the app.

    Left to None it is looked up from `scope`, which every caller
    already passes. That is deliberate: a caller who forgets then still
    gets the safe answer, instead of the one call site nobody updated
    serving a public route off a copy of production data. Pass it
    explicitly where the registry cannot answer yet — the install that
    creates the instance.
    """
    if login_only is None:
        login_only = bool(scope) and scope in rehearsal_keys()
    lines = []
    lines.append("\thandle /auth/* {")
    lines.append("\t\trequest_header -X-OAAP-User")
    lines.append("\t\trequest_header -X-OAAP-Roles")
    lines.append("\t\treverse_proxy identity:8000")
    lines.append("\t}")
    # longest prefix first; catch-all "/" last
    ordered = sorted(routes, key=lambda r: len(r["path"]), reverse=True)
    for idx, r in enumerate(ordered):
        matcher = "" if r["path"] == "/" else f" {r['path']}*"
        roles = [x for x in r["roles"] if x != "public"]
        if roles and not login_only:
            # CORS preflight bypass, ahead of the role-gated handle below
            # (RFC-0027 follow-up, 2026-09-09 letter from aipc-service: a
            # browser calling a role-gated route with an API key sends an
            # unauthenticated OPTIONS preflight first. forward_auth's
            # session fallback answers that with 303 -> /auth/login, which
            # is not a 2xx -- the browser aborts before the real request
            # with the key is ever sent. An OPTIONS carries no credentials
            # and has no effect, so it is safe to hand straight to the app
            # (same header strip as a public route); every OTHER method on
            # this path still goes through /verify, unchanged below.
            #
            # NEVER on a rehearsal (login_only, RFC-0030 2.15.2): its rule
            # is that EVERY block on EVERY route asks /verify, no
            # exceptions -- a copy of production data does not get a
            # carve-out because the carved-out method happens to be
            # harmless today. test_rehearsal_shape.py holds this line.
            target_c, target_p = container, svc_port
            if services and r.get("service") in services:
                target_c, target_p = services[r["service"]]
            lines.append(f"\t@preflight{idx} {{")
            lines.append("\t\tmethod OPTIONS")
            if r["path"] != "/":
                lines.append(f"\t\tpath {r['path']}*")
            lines.append("\t}")
            lines.append(f"\thandle @preflight{idx} {{")
            lines.append("\t\trequest_header -X-OAAP-User")
            lines.append("\t\trequest_header -X-OAAP-Roles")
            lines.append(f"\t\treverse_proxy {target_c}:{target_p}")
            lines.append("\t}")
        lines.append(f"\thandle{matcher} {{")
        if roles or "public" not in r["roles"] or login_only:
            # No explicit strip here: Caddy's directive order runs
            # request_header AFTER forward_auth, which would wipe the
            # verified headers again. forward_auth's copy_headers
            # replaces any client-sent values (anti-spoofing), matching
            # the main gateway Caddyfile.
            uri = f"/verify?roles={','.join(sorted(set(roles)))}"
            if groups:
                uri += f"&groups={','.join(sorted(set(groups)))}"
            if tenant:
                uri += f"&tenant={tenant}"
            # RFC-0027 D5: which instance this is, so an API key can be
            # limited to one app. A key WITHOUT a limit ignores this; a
            # key WITH one refuses wherever the parameter is absent, so
            # a site generated before this version fails closed rather
            # than quietly granting the whole node.
            if scope:
                uri += f"&instance={scope}"
            lines.append("\t\tforward_auth identity:8000 {")
            lines.append(f"\t\t\turi {uri}")
            lines.append("\t\t\tcopy_headers X-OAAP-User X-OAAP-Roles")
            lines += _AUTH_NO_UPGRADE
            lines.append("\t\t}")
        else:
            # Public route: nothing overwrites the headers, so strip
            # client-sent identity headers explicitly (contract guarantee 1).
            lines += _throttle_block(scope, throttle, edge)
            lines.append("\t\trequest_header -X-OAAP-User")
            lines.append("\t\trequest_header -X-OAAP-Roles")
        target_c, target_p = container, svc_port
        if services and r.get("service") in services:
            target_c, target_p = services[r["service"]]
        lines.append(f"\t\treverse_proxy {target_c}:{target_p}")
        lines.append("\t}")
    if not any(r["path"] == "/" for r in routes):
        lines.append("\thandle {")
        lines.append("\t\trespond 404")
        lines.append("\t}")
    return lines


def caddy_site(port, routes, container, svc_port, groups=None, scope="",
               throttle=None, services=None, tenant="", login_only=None):
    """Generate a LAN gateway listener for one app instance.

    The throttle scope is the instance name on every entry point, so a
    caller cannot multiply its budget by rotating between the LAN port,
    the node subdomain and the instance's own hostname.
    """
    lines = ([f":{port} {{"]
             + site_body(routes, container, svc_port, groups, scope, throttle,
                         services=services, tenant=tenant,
                         login_only=login_only)
             + ["}"])
    return "\n".join(lines) + "\n"


def write_app_caddy(name, inst):
    """(Re)write the LAN gateway site of one instance, from its record.

    Seven places used to spell this out, each repeating the same six
    arguments; a rule that has to hold on all of them -- a rehearsal
    serves no public route (2.15.2) -- would have had to be added seven
    times, and the one that was missed would have failed silently and in
    the unsafe direction. One reader now, and every caller says WHICH
    instance instead of HOW to describe it.
    """
    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w",
              encoding="utf-8") as f:
        f.write(caddy_site(inst["port"], inst["routes"], inst["container"],
                           inst["svc_port"],
                           (inst.get("visibility") or {}).get("groups"), name,
                           throttle_of(inst), services=route_targets(inst),
                           tenant=instance_tenant_ref(inst),
                           login_only=is_rehearsal(inst)))



# --- registered external hostname (RFC-0005 level 3, hardening) -----------

_LOG_BLOCK = ["\tlog {", "\t\toutput file /logs/external-access.log",
              "\t\tformat json", "\t}"]


def load_external_conf():
    try:
        with open(EXTERNAL_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d.get("host", ""), d.get("edge", "")
    except (OSError, ValueError):
        return "", ""


def load_external():
    return load_external_conf()[0]


def _portal_site_body():
    """Handler block for the platform apex (portal, auth, setup, hook)."""
    lines = []
    lines.append("\thandle /auth/* {")
    lines.append("\t\trequest_header -X-OAAP-User")
    lines.append("\t\trequest_header -X-OAAP-Roles")
    lines.append("\t\treverse_proxy identity:8000")
    lines.append("\t}")
    lines.append("\thandle /setup* {")
    lines.append("\t\trequest_header -X-OAAP-User")
    lines.append("\t\trequest_header -X-OAAP-Roles")
    lines.append("\t\treverse_proxy portal:8000")
    lines.append("\t}")
    # deploy hook (runtime spec 2.5): bearer token instead of session —
    # the portal validates the token, so no forward_auth here
    lines.append("\thandle /deploy/* {")
    lines.append("\t\trequest_header -X-OAAP-User")
    lines.append("\t\trequest_header -X-OAAP-Roles")
    lines.append("\t\treverse_proxy portal:8000")
    lines.append("\t}")
    # fleet status (RFC-0021): read-only, guarded by a fleet key the
    # portal validates — no session, no identity headers
    lines.append("\thandle /fleet/* {")
    lines.append("\t\trequest_header -X-OAAP-User")
    lines.append("\t\trequest_header -X-OAAP-Roles")
    lines.append("\t\treverse_proxy portal:8000")
    lines.append("\t}")
    lines.append("\thandle {")
    lines.append("\t\tforward_auth identity:8000 {")
    lines.append("\t\t\turi /verify")
    lines.append("\t\t\tcopy_headers X-OAAP-User X-OAAP-Roles")
    lines += _AUTH_NO_UPGRADE
    lines.append("\t\t}")
    lines.append("\t\treverse_proxy portal:8000")
    lines.append("\t}")
    return lines


def _edge_guard(edge):
    """Behind-edge mode: only the edge may speak for the external names.

    First handle wins, so non-edge clients stop here with 403. This is
    also what makes trusting X-Forwarded-* headers acceptable — they
    can only originate from the configured edge (gateway spec, edge
    section, rule 3).
    """
    return [f"\t@notedge not remote_ip {edge}",
            "\thandle @notedge {",
            "\t\trespond 403",
            "\t}"]


def write_external_caddy():
    """(Re)generate gateway sites for the registered external hostname.

    Direct mode: TLS sites on 443 (portal on the apex, one subdomain
    per app instance, ACME automatic) plus an HTTP→HTTPS redirect.
    Behind-edge mode (RFC-0006): the edge terminates TLS, so the same
    sites are served over plain HTTP — no redirect (it would loop
    through the edge), no ACME, and only the edge address is accepted.
    Returns instance names skipped because their registry entry
    predates route capture.
    """
    host, edge = load_external_conf()
    path = os.path.join(CADDY_APPS_DIR, "external.caddy")
    if not host:
        if os.path.exists(path):
            os.remove(path)
        return []
    reg = load_registry()
    scheme = "http" if edge else "https"
    mode = f"behind edge {edge}" if edge else "direct (TLS via ACME)"
    lines = [f"# generated by appctl — external hostname: {host} — {mode}"]
    lines.append(f"{scheme}://{host} {{")
    if edge:
        lines += _edge_guard(edge)
    lines += _LOG_BLOCK
    lines += _portal_site_body()
    lines.append("}")
    if not edge:
        # The bare :80 catch-all would happily serve plain HTTP for the
        # external names — redirect them to HTTPS explicitly.
        # A DNS wildcard matches exactly one label, and so does
        # Caddy's: *.host does not cover <instance>.<label>.<host>.
        # Every tenant label therefore needs its own redirect line, or
        # those names answer plain HTTP into the catch-all.
        redirs = [f"http://{host}", f"http://*.{host}"]
        for _tid in sorted(load_tenants()):
            for _prefix in tenant_host_prefixes(_tid):
                if _prefix:
                    redirs.append(f"http://*.{_prefix}.{host}")
        lines.append(", ".join(redirs) + " {")
        lines.append("\tredir https://{host}{uri} permanent")
        lines.append("}")
    skipped = []
    for name, inst in sorted(reg["instances"].items()):
        routes = inst.get("routes")
        if not routes or not inst.get("svc_port"):
            skipped.append(name)
            continue
        # An instance of the default tenant keeps <instance>.<node>; any
        # other tenant puts its label in between, once per label it still
        # answers under (oaap.core.tenant 2.4). A tenant this node does
        # not have yields no prefix at all -- the instance then gets no
        # external name, which is the fail-closed direction: the
        # alternative is publishing somebody else's app under the
        # operator's own name.
        groups = (inst.get("visibility") or {}).get("groups")
        for fqdn in instance_auto_hosts(name, inst, ext_host=host):
            lines.append(f"{scheme}://{fqdn} {{")
            if edge:
                lines += _edge_guard(edge)
            lines += _LOG_BLOCK
            lines += site_body(routes, inst["container"], inst["svc_port"],
                               groups, name, throttle_of(inst), edge,
                               services=route_targets(inst),
                               tenant=instance_tenant_ref(inst),
                               login_only=is_rehearsal(inst))
            lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return skipped


def instance_names(inst):
    """Every public name this instance owns: the canonical address first
    (RFC-0009), then its aliases (RFC-0018). Empty if it has no own name.
    All names route to the same instance under the same protection — an
    alias is a front door, not a permission."""
    canon = inst.get("address")
    if not canon:
        return []
    return [canon] + [a for a in (inst.get("aliases") or []) if a and a != canon]


def write_instance_address_caddy():
    """(Re)generate sites for instances carrying their own public name(s).

    An instance's automatic external name is a subdomain of the NODE
    (`<instance>.<node host>`, write_external_caddy above). That ties a
    published address to the machine it happens to run on, which does
    not survive a move — so an instance may additionally register a
    hostname of its own (RFC-0009), and now several: one canonical name
    plus aliases (RFC-0018). One site is emitted per name, all with the
    identical body. Mode follows the node's external configuration:
    direct means TLS via ACME plus an HTTP redirect, behind-edge means
    plain HTTP with the edge guard and no ACME (the edge terminates TLS).
    """
    reg = load_registry()
    _, edge = load_external_conf()
    path = os.path.join(CADDY_APPS_DIR, "instance-addresses.caddy")
    entries = [(n, i) for n, i in sorted(reg["instances"].items())
               if i.get("address") and i.get("routes") and i.get("svc_port")]
    if not entries:
        if os.path.exists(path):
            os.remove(path)
        return
    scheme = "http" if edge else "https"
    mode = f"behind edge {edge}" if edge else "direct (TLS via ACME)"
    lines = [f"# generated by appctl — per-instance public hostnames — {mode}"]
    for name, inst in entries:
        for host in instance_names(inst):
            lines.append(f"# {host} -> instance {name}")
            lines.append(f"{scheme}://{host} {{")
            if edge:
                lines += _edge_guard(edge)
            lines += _LOG_BLOCK
            lines += site_body(inst["routes"], inst["container"], inst["svc_port"],
                               (inst.get("visibility") or {}).get("groups"),
                               name, throttle_of(inst), edge,
                               services=route_targets(inst),
                               tenant=instance_tenant_ref(inst),
                               login_only=is_rehearsal(inst))
            lines.append("}")
            if not edge:
                lines.append(f"http://{host} {{")
                # {host}/{uri} are Caddy placeholders — not Python formatting
                lines.append("\tredir https://{host}{uri} permanent")
                lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# The gateway is the only core service on the app networks (RFC-0016),
# so it is the only one that can reach an app to check its health. The
# portal used to probe apps by container name directly; after isolation
# it cannot. This internal site lets the portal ask the gateway to probe
# for it: a listener on :8099 — reachable only container-to-container on
# the platform network (never published, and apps are not on that
# network) — with one no-auth route per instance that proxies to the
# app's health endpoint. Health checks are the only thing it exposes.
HEALTH_PROBE_PORT = 8099


def write_internal_health_caddy():
    reg = load_registry()
    path = os.path.join(CADDY_APPS_DIR, "_internal-health.caddy")
    insts = [(n, i) for n, i in sorted(reg["instances"].items())
             if i.get("container") and i.get("svc_port")]
    if not insts:
        if os.path.exists(path):
            os.remove(path)
        return
    lines = [f"# generated by appctl — internal health probe endpoint "
             f"(RFC-0016); reachable only from the platform network",
             f":{HEALTH_PROBE_PORT} {{"]
    for name, inst in insts:
        hp = inst.get("health_path") or "/"
        lines.append(f"\thandle /h/{name} {{")
        lines.append(f"\t\trewrite * {hp}")
        lines.append(f"\t\treverse_proxy {inst['container']}:{inst['svc_port']}")
        lines.append("\t}")
    lines.append("\thandle {")
    lines.append("\t\trespond 404")
    lines.append("\t}")
    lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def refresh_generated_sites():
    """Regenerate every site file derived from the registry."""
    write_external_caddy()
    write_instance_address_caddy()
    write_internal_health_caddy()


HOSTNAME_RE = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"


def cmd_external(args):
    if args.action == "show":
        host, edge = load_external_conf()
        if not host:
            print("No external hostname registered.")
        elif edge:
            print(f"{host} (behind edge {edge})")
        else:
            print(f"{host} (direct, TLS via ACME)")
        return
    if args.action == "remove":
        if os.path.exists(EXTERNAL_FILE):
            os.remove(EXTERNAL_FILE)
        refresh_generated_sites()
        reload_gateway()
        print("External hostname removed; gateway reloaded.")
        return
    host = (args.hostname or "").lower().strip().rstrip(".")
    if not re.fullmatch(HOSTNAME_RE, host):
        die("'external set' needs a valid hostname, e.g. oaap-bernd.duckdns.org")
    edge = (getattr(args, "behind_edge", "") or "").strip()
    if edge and not re.fullmatch(r"[0-9a-fA-F.:]+", edge):
        die("--behind-edge needs the edge node's IP address, e.g. --behind-edge 10.10.10.97")
    os.makedirs(APPS_DIR, exist_ok=True)
    conf = {"host": host}
    if edge:
        conf["edge"] = edge
    tmp = EXTERNAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)
    os.replace(tmp, EXTERNAL_FILE)
    skipped = write_external_caddy()
    write_instance_address_caddy()
    reload_gateway()
    if edge:
        # behind-edge mode (RFC-0006 / gateway spec edge section): the
        # edge terminates TLS; this node serves plain HTTP, no redirect,
        # no ACME, and only accepts the edge for these names.
        print(f"External hostname registered: {host} — behind edge {edge}")
        print(f"  The edge terminates TLS and forwards {host} and *.{host} here.")
        print(f"  Requests for these names from any address other than {edge} get 403.")
    else:
        print(f"External hostname registered: {host}")
        print(f"  Portal:  https://{host}/")
        print(f"  Apps:    https://<instance>.{host}/")
    for s in skipped:
        print(f"NOTE: instance '{s}' predates route capture — reinstall it once to publish it externally.")
    if not edge:
        print("Certificates are fetched automatically as soon as ports 80 and 443")
        print("from the internet reach this node (router port forwarding).")


# ----------------------- edge routing (RFC-0006, gateway spec edge section)
# The node owning the shared public entry forwards requests for OTHER
# platforms' hostnames to the platform that owns the name. Only the
# entry is shared — users, apps, updates and backups stay per platform.

def load_edge():
    try:
        with open(EDGE_FILE, encoding="utf-8") as f:
            return json.load(f).get("routes", [])
    except (OSError, ValueError):
        return []


def save_edge(routes):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = EDGE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"routes": routes}, f, indent=2)
    os.replace(tmp, EDGE_FILE)


def write_edge_caddy():
    """(Re)generate the edge sites: TLS for foreign names, forwarded.

    Host stays unchanged (contract guarantee 2 through the chain);
    Caddy sets X-Forwarded-Proto/-For. HTTP is redirected to HTTPS at
    the edge; ACME challenges are handled by Caddy before user routes.
    """
    routes = load_edge()
    path = os.path.join(CADDY_APPS_DIR, "edge.caddy")
    if not routes:
        if os.path.exists(path):
            os.remove(path)
        return
    lines = ["# generated by appctl — edge routing (RFC-0006): foreign hostnames -> owning platform"]
    for r in routes:
        target = f"{r['target']}:{r.get('port', 80)}"
        lines.append(f"https://{r['host']}, https://*.{r['host']} {{")
        # Wildcard certificates need a DNS challenge we cannot do —
        # so certificates come on demand per requested name, approved
        # by the portal (only names under a configured edge route).
        lines.append("\ttls {")
        lines.append("\t\ton_demand")
        lines.append("\t}")
        lines += _LOG_BLOCK
        lines.append(f"\treverse_proxy {target} {{")
        # Overwrite instead of append: the edge is the outermost hop, so
        # the only trustworthy entry is the peer it sees itself. Caddy's
        # default would keep a client-supplied prefix, and everything
        # downstream that reads the first entry — access log, the public
        # route throttle (RFC-0010) — would believe the client.
        lines.append("\t\theader_up X-Forwarded-For {http.request.remote.host}")
        lines.append("\t}")
        lines.append("}")
        lines.append(f"http://{r['host']}, http://*.{r['host']} {{")
        lines.append("\tredir https://{host}{uri} permanent")
        lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cmd_edge(args):
    routes = load_edge()
    if args.action == "list":
        if not routes:
            print("No edge routes configured. Add one with: sudo oaap edge add <hostname> <target>")
        for i, r in enumerate(routes, 1):
            print(f"{i}. {r['host']} (+ *.{r['host']}) -> {r['target']}:{r.get('port', 80)}")
        return
    if args.action == "remove":
        key = (args.hostname or "").lower().strip()
        if not key:
            die("'edge remove' needs a hostname or index")
        before = len(routes)
        if key.isdigit() and 1 <= int(key) <= len(routes):
            routes.pop(int(key) - 1)
        else:
            routes = [r for r in routes if r["host"] != key]
        if len(routes) == before:
            die("no matching edge route")
        save_edge(routes)
        write_edge_caddy()
        reload_gateway()
        print("Edge route removed; gateway reloaded.")
        return
    # add
    host = (args.hostname or "").lower().strip().rstrip(".")
    if not re.fullmatch(HOSTNAME_RE, host):
        die("'edge add' needs a valid hostname, e.g. oaap-bernd.duckdns.org")
    target = (args.target or "").lower().strip()
    if not re.fullmatch(r"[0-9a-fA-F.:]+|" + HOSTNAME_RE, target):
        die("'edge add' needs the target platform's LAN address, e.g. 10.10.10.95")
    if host == load_external():
        die("this node's own external hostname cannot be an edge route")
    if any(r["host"] == host for r in routes):
        die(f"an edge route for {host} already exists (remove it first)")
    for n, i in load_registry()["instances"].items():
        addr = i.get("address") or ""
        if addr == host or addr.endswith(f".{host}"):
            die(f"{addr} is the public hostname of the local instance '{n}' "
                f"(RFC-0009) — an edge route for {host} would forward it away "
                "from this node")
    entry = {"host": host, "target": target}
    if args.port and args.port != 80:
        entry["port"] = args.port
    routes.append(entry)
    save_edge(routes)
    write_edge_caddy()
    reload_gateway()
    print(f"Edge route added: {host} (+ *.{host}) -> {target}:{args.port or 80}")
    print("This node now terminates TLS for these names and forwards them.")
    print("On the target platform, switch to behind-edge mode:")
    print(f"  sudo oaap external set {host} --behind-edge <this node's LAN address>")


def reload_gateway():
    run(["docker", "exec", GATEWAY_CONTAINER, "caddy", "reload",
         "--config", "/etc/caddy/Caddyfile"])


# ------------------------------------------- instance container & config
# Env values are baked into a container at 'docker run' time, so every
# config change needs a FRESH container -- 'docker restart' would keep
# the old values. install, restore and 'app config set' all go through
# start_instance_container so the container shape stays identical.

RESERVED_ENV = {"OAAP_APP_SECRET", "OAAP_PLATFORM_KEY", "OAAP_TWIN_URL"}  # platform-owned, never operator-editable


def tenant_dir(tid):
    return os.path.join(TENANTS_DIR, tid)


def new_instance_id():
    """An instance's identity: short hex, minted once, never changed.

    Short rather than a full UUID because it appears in a path an
    operator may have to read out loud, and 48 bits of randomness is
    plenty to keep the instances of one node apart.
    """
    return uuid.uuid4().hex[:12]


def instance_dir(name, inst=None):
    """Where this instance's data lives (RFC-0026 3.2).

    `tenants/<tenant-id>/instances/<instance-id>/`. The path hangs off
    IDENTITIES, never off names: that is what makes renaming a tenant or
    an instance a rename instead of a data migration, and what puts
    everything one tenant owns under one subtree.

    A record written before 0.1.60 has no id and keeps the flat
    `apps/<key>/` it was installed into. Reading those has to keep
    working until the migration has moved them -- and after that this
    branch is dead.
    """
    if inst is None:
        inst = load_registry()["instances"].get(name) or {}
    iid = (inst or {}).get("id")
    tid = resolve_tenant((inst or {}).get("tenant")) if inst else None
    if not iid or not tid:
        return os.path.join(APPS_DIR, name)
    return os.path.join(tenant_dir(tid), "instances", iid)


def env_path(name, inst=None):
    return os.path.join(instance_dir(name, inst), "instance.env")


def load_env(name, inst=None):
    try:
        with open(env_path(name, inst), encoding="utf-8") as f:
            return dict(l.strip().split("=", 1) for l in f if "=" in l)
    except OSError:
        return {}


def save_env(name, env, inst=None):
    path = env_path(name, inst)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())
    os.replace(tmp, path)


def instance_services(inst):
    """Normalised list of an instance's services, newest-shape first.

    RFC-0016 lets an instance have several services; each is recorded as
    {service, container, image, build, port}, primary first. Instances
    installed before 0.1.31 have no `services` list — synthesise a
    single one from the flat container/image/svc_port fields so every
    caller (recreate, migrate, config) treats old and new the same."""
    if inst.get("services"):
        return inst["services"]
    return [{"service": "", "container": inst["container"],
             "image": inst["image"], "build": inst.get("build", ""),
             "port": inst.get("svc_port")}]


def _endpoint_publish(endpoints, svc_name, primary):
    """docker -p / -e args for the granted endpoint (RFC-0015) attached to
    this service. At most one endpoint exists; it publishes a raw host
    port straight to the container (bypassing the gateway) and tells the
    app its assigned public port via OAAP_ENDPOINT_PORT."""
    args = []
    for e in endpoints or []:
        if (e.get("service") or primary) != svc_name:
            continue
        protos = ["udp", "tcp"] if e["protocol"] == "both" else [e["protocol"]]
        for proto in protos:
            args += ["-p", f"{e['host_port']}:{e['container_port']}/{proto}"]
        args += ["-e", f"OAAP_ENDPOINT_PORT={e['host_port']}",
                 "-e", f"OAAP_ENDPOINT_NAME={e['name']}"]
    return args


def recreate_instance_containers(name, services, storage, endpoints=None,
                                 inst=None):
    """(Re)create ALL of an instance's service containers on its own
    network (RFC-0016), from their recorded shape. Storage entries go to
    the service named in `service`, defaulting to the primary (services
    are ordered primary-first). A granted non-HTTP endpoint (RFC-0015)
    publishes a raw host port on its service's container."""
    primary = services[0]["service"]
    # RFC-0016: the instance's own network, gateway bridged in. Set up
    # before the containers so they land on the right network at
    # `docker run` time; the migration step restores the gateway link
    # after a platform update recreates the gateway.
    net = ensure_app_network(name)
    connect_gateway(net)
    for s in services:
        uid = image_uid(s["image"])
        mounts = []
        for st in storage or []:
            if (st.get("service") or primary) != s["service"]:
                continue
            host = os.path.join(instance_dir(name, inst), "storage", st["name"])
            os.makedirs(host, exist_ok=True)
            if uid is not None:
                os.chown(host, uid, uid)
            mounts += ["-v", f"{host}:{st['mount']}"]
        # A multi-service container also answers to its bare service name
        # on the instance network (--network-alias), because a wrapped
        # compose stack's containers refer to each other by service name
        # (e.g. a UI talking to "db"), not by our oaap-app-<inst>-<svc>
        # container name. Single-service apps need no alias.
        alias = ["--network-alias", s["service"]] if s["service"] else []
        publish = _endpoint_publish(endpoints, s["service"], primary)
        subprocess.run(["docker", "rm", "-f", s["container"]],
                       capture_output=True, text=True)
        run(["docker", "run", "-d", "--name", s["container"],
             "--restart", "unless-stopped", "--network", net, *alias,
             "--env-file", env_path(name, inst), *mounts, *publish, s["image"]])
    # Erklärte App-zu-App-Verbindungen zurückholen: `docker run` kennt nur
    # EIN Netz, also hat der neue Container seine Link-Netze verloren
    # (RFC-0016). Ohne das ist eine Verbindung nach jeder
    # Konfigurationsänderung still tot.
    restore_links(name)


ENDPOINT_PORT_RANGE = range(8200, 8300)


class EndpointPortTaken(Exception):
    """A fixed endpoint's required port is already in use (RFC-0017 §5.1).
    Raised rather than die()d so both callers can react in their own way:
    the CLI turns it into a die(), the spool worker into a queued-result
    message that does not kill the whole run."""


def assign_endpoint_port(reg, wish, exclude=None, fixed=False):
    """Pick a host port for a granted endpoint (RFC-0015). Default: the
    wished-for one if free, else the next free port in the reserved range —
    a wish, not a demand (Jörg's decision). With `fixed` (RFC-0017 §5.1),
    `wish` is a requirement: it is the port the app advertises to clients,
    so a collision must FAIL LOUDLY rather than silently reassign — media
    would break at a port the server never announced."""
    used = {e["host_port"] for i in reg["instances"].values()
            for e in (i.get("endpoints") or [])}
    used.discard(exclude)
    if fixed:
        if wish in used:
            raise EndpointPortTaken(
                f"endpoint requires fixed port {wish}, but it is already in "
                f"use on this node. A fixed port is a requirement, not a wish "
                f"(RFC-0017 §5.1): free it or change the app's port — the "
                f"platform will not silently reassign it.")
        return wish
    if wish and wish not in used:
        return wish
    for p in ENDPOINT_PORT_RANGE:
        if p not in used:
            return p
    die("no free endpoint port available on this node")


def route_targets(inst):
    """Map service name -> (container, port) for site generation, or None
    for a single-service instance (site_body then uses the primary)."""
    svcs = inst.get("services")
    if not svcs or len(svcs) == 1:
        return None
    return {s["service"]: (s["container"], s["port"]) for s in svcs}


def config_entries(name, inst):
    """Declared config keys of an instance, with current values.

    Instances installed before the manifest's config block was recorded
    have no declaration to go by; their keys are then read back from
    instance.env and treated as SECRET -- a key we cannot classify might
    well be one, and masking a harmless value is the cheaper mistake.
    """
    env = load_env(name)
    declared = inst.get("config")
    if declared is None:
        declared = [{"key": k, "label": k, "secret": True}
                    for k in env if k not in RESERVED_ENV]
    entries = []
    for c in declared:
        key = c["key"]
        if key in RESERVED_ENV:
            continue
        entries.append({
            "key": key,
            "label": c.get("label") or key,
            "secret": bool(c.get("secret")),
            "multiline": bool(c.get("multiline")),
            "generate": c.get("generate", ""),
            "default": c.get("default", ""),
            "value": env.get(key, ""),
        })
    return entries


def apply_config(name, inst, values):
    """Write config values and recreate the container. Returns a message.

    Only keys the instance actually declares are accepted -- a caller
    (CLI or the portal's spool request) can never introduce new
    environment variables into a container this way.
    """
    entries = {e["key"]: e for e in config_entries(name, inst)}
    unknown = [k for k in values if k not in entries]
    if unknown:
        raise ValueError(f"'{name}' does not declare config key(s): "
                         f"{', '.join(sorted(unknown))}")
    env = load_env(name)
    changed = []
    for key, value in values.items():
        if env.get(key, "") == value:
            continue
        env[key] = value
        changed.append(key)
    if not changed:
        return "no change"
    save_env(name, env)
    recreate_instance_containers(name, instance_services(inst),
                                 inst.get("storage") or [])
    return "changed: " + ", ".join(sorted(changed))


def cmd_config(args):
    reg = load_registry()
    name = args.name
    inst = reg["instances"].get(name)
    if not inst:
        die(f"no instance named '{name}'")
    entries = config_entries(name, inst)
    if args.action == "list":
        if not entries:
            print(f"'{name}' declares no configuration values.")
            return
        width = max(len(e["key"]) for e in entries)
        for e in entries:
            if e["secret"]:
                # never print a secret back -- 'set' is the way to change it
                shown = "******** (set)" if e["value"] else "(empty)"
            else:
                shown = e["value"] if e["value"] else "(empty)"
            print(f"{e['key']:<{width}}  {shown}")
            if e["label"] != e["key"]:
                print(f"{'':<{width}}  {e['label']}")
        if inst.get("config") is None:
            print("")
            print("NOTE: this instance predates config recording — keys were read")
            print("from instance.env and are all masked. A redeploy records the")
            print("manifest's real labels and secret flags.")
        return
    key = args.key or die(f"'app config {args.action}' needs a key name")
    if args.action == "unset":
        entry = next((e for e in entries if e["key"] == key), None)
        if not entry:
            die(f"'{name}' does not declare config key '{key}'")
        value = entry["default"]
    else:
        value = args.value
        if value is None:
            # keeps secrets out of the shell history (same as 'oaap user password')
            value = getpass.getpass(f"Value for {key} (hidden): ")
        if getattr(args, "append", False):
            # List-valued keys (';'-separated, e.g. FleetView's node and
            # key lists) grow one entry at a time — without the operator
            # having to re-enter every existing value, which for secrets
            # they cannot even read back.
            entry = next((e for e in entries if e["key"] == key), None)
            if not entry:
                die(f"'{name}' does not declare config key '{key}'")
            if entry["value"]:
                value = entry["value"].rstrip(";") + ";" + value.lstrip(";")
    try:
        msg = apply_config(name, inst, {key: value})
    except ValueError as e:
        die(str(e))
    if msg == "no change":
        print(f"{key} already had this value — nothing changed.")
        return
    audit_deploy({"instance": name, "ok": True, "message": f"config {msg}",
                  "revision": "", "version": inst.get("version", ""), "via": "cli"})
    print(f"Config updated for '{name}' ({msg}).")
    print("The container was recreated so the new value takes effect —")
    print("brief downtime, storage and address are unchanged.")


def cmd_throttle(args):
    """Rate brake for an instance's public routes (RFC-0010)."""
    reg = load_registry()
    name = args.name
    inst = reg["instances"].get(name)
    if not inst:
        die(f"no instance named '{name}'")
    has_public = any("public" in r["roles"] for r in inst.get("routes") or [])

    if args.action == "show":
        t = throttle_of(inst)
        state = (f"{t['limit']} requests per {t['window']} s and client address"
                 if t else "off")
        origin = "default" if inst.get("throttle") is None else "set for this instance"
        print(f"{name}: {state} ({origin})")
        if not has_public:
            print("No public route — the throttle never applies here; every "
                  "route of this instance is authenticated.")
        return

    if args.action == "off":
        inst["throttle"] = {}
        print(f"Throttle switched OFF for '{name}'.")
        if has_public:
            print("WARNING: this instance has a public route and now has no "
                  "platform-side rate brake at all.")
    else:
        m = re.fullmatch(r"(\d+)/(\d+)", args.rate or "")
        if not m:
            die("'app throttle set' needs <requests>/<seconds>, e.g. 300/60")
        limit, window = int(m.group(1)), int(m.group(2))
        if limit < 1 or window < 1:
            die("requests and seconds must both be at least 1")
        inst["throttle"] = {"limit": limit, "window": window}
        print(f"'{name}': at most {limit} requests per {window} s and client "
              "address on public routes.")
        print(f"Counted per identity worker, so the real ceiling is about "
              f"{limit * IDENTITY_WORKERS} — this is a volume brake against "
              "floods, not a substitute for the app's own key lockout "
              "(RFC-0010).")
    save_registry(reg)
    write_app_caddy(name, inst)
    refresh_generated_sites()
    reload_gateway()


def check_instance_address(reg, name, inst, hostname):
    """Validate a candidate public hostname; returns it normalised.

    Shared by the CLI and the portal's host-side worker, so both refuse
    exactly the same collisions (RFC-0009). Raises ValueError.
    """
    ext_host = load_external()
    host = (hostname or "").lower().strip().rstrip(".")
    if not re.fullmatch(HOSTNAME_RE, host):
        raise ValueError("needs a valid hostname, e.g. hub.example.org")
    if host == ext_host:
        raise ValueError(f"{host} is this node's own external hostname (the "
                         "portal answers there) — choose a different name")
    if ext_host and host.endswith(f".{ext_host}"):
        autos = instance_auto_hosts(name, inst, ext_host=ext_host)
        raise ValueError(
            f"names under {ext_host} are already generated automatically — "
            + (f"'{name}' is reachable at {autos[0]} without this" if autos
               else f"'{name}' has none, because it names a tenant this node "
                    f"does not have — repair that instead of setting an "
                    f"address under {ext_host}"))
    for r in load_edge():
        if host == r["host"] or host.endswith(f".{r['host']}"):
            raise ValueError(f"{host} is covered by the edge route for "
                             f"{r['host']} (forwarded to {r['target']}) — "
                             "remove that route first")
    # Collides with any name — canonical OR alias (RFC-0018) — already
    # held by ANOTHER instance. The caller checks against this instance's
    # own names, where it can give a clearer message (canonical vs alias).
    owner = {}
    for n, i in reg["instances"].items():
        if n == name:
            continue
        for h in instance_names(i):
            owner[h] = n
    if host in owner:
        raise ValueError(f"{host} is already registered for instance "
                         f"'{owner[host]}'")
    if not inst.get("routes") or not inst.get("svc_port"):
        raise ValueError(f"'{name}' predates route capture — reinstall it "
                         "once, then set its address")
    return host


def _address_setup_hint(host, edge):
    """The 'now point DNS here' lines, identical for a canonical name and
    an alias — the operator's job is the same for every name (RFC-0018)."""
    if edge:
        print(f"This node is behind edge {edge}: it serves the name as plain "
              "HTTP and accepts it only from the edge.")
        print("On the edge node, point the name here:")
        print(f"  sudo oaap edge add {host} <this node's LAN address>")
    else:
        print(f"Entry point: https://{host}/ — the certificate is requested on "
              "the first request.")
        print(f"Prerequisite: {host} must resolve to this node's public "
              "address, with ports 80 and 443 forwarded here.")


def cmd_address(args):
    """Give one instance public hostname(s) of its own: a canonical name
    (RFC-0009) plus aliases (RFC-0018)."""
    reg = load_registry()
    name = args.name
    inst = reg["instances"].get(name)
    if not inst:
        die(f"no instance named '{name}'")
    ext_host, edge = load_external_conf()
    aliases = list(inst.get("aliases") or [])
    # A rehearsal gets no name of its own (spec 2.15.2). Refused for the
    # WRITING actions only: `show` on a rehearsal is a fair question,
    # and its answer -- the automatic node address and nothing else --
    # is exactly what the rule says.
    if args.action in ("set", "alias-add"):
        refusal = rehearsal_refusal(reg, name, "an address of its own")
        if refusal:
            die(refusal)

    if args.action == "show":
        addr = inst.get("address")
        print(f"{name}: {addr}" if addr else f"{name}: no own hostname registered")
        for a in aliases:
            print(f"  alias: {a}")
        autos = instance_auto_hosts(name, inst, ext_host=ext_host)
        for auto in autos:
            print(f"Automatic node address: {auto}")
        if ext_host and not autos:
            print("Automatic node address: none — this instance names a "
                  "tenant this node does not have")
        return

    if args.action == "remove":
        if not inst.get("address"):
            die(f"'{name}' has no own hostname registered")
        if aliases:
            die(f"'{name}' still has aliases ({', '.join(aliases)}). Remove "
                f"them first, or promote one to the canonical name with "
                f"'oaap app address set {name} <alias>' — an instance must "
                f"not keep aliases without a canonical address.")
        old = inst.pop("address")
        save_registry(reg)
        write_instance_address_caddy()
        reload_gateway()
        print(f"Removed {old} from '{name}'.")
        for auto in instance_auto_hosts(name, inst, ext_host=ext_host):
            print(f"Still reachable at https://{auto}/")
        return

    if args.action == "alias-add":
        if not inst.get("address"):
            die(f"'{name}' has no canonical address yet. Set one first with "
                f"'oaap app address set {name} <hostname>', then add aliases.")
        try:
            host = check_instance_address(reg, name, inst, args.hostname)
        except ValueError as e:
            die(str(e))
        if host == inst.get("address"):
            die(f"{host} is already the canonical name of '{name}'")
        if host in aliases:
            die(f"{host} is already an alias of '{name}'")
        inst.setdefault("aliases", []).append(host)
        save_registry(reg)
        write_instance_address_caddy()
        reload_gateway()
        print(f"'{name}' now also answers for {host} (alias).")
        _address_setup_hint(host, edge)
        return

    if args.action == "alias-remove":
        host = (args.hostname or "").lower().strip().rstrip(".")
        if host not in aliases:
            die(f"'{host}' is not an alias of '{name}'"
                + (f" (canonical name is {inst['address']}; remove that with "
                   f"'oaap app address remove {name}')" if host == inst.get("address") else ""))
        inst["aliases"] = [a for a in aliases if a != host]
        if not inst["aliases"]:
            inst.pop("aliases", None)
        save_registry(reg)
        write_instance_address_caddy()
        reload_gateway()
        print(f"Removed alias {host} from '{name}'.")
        return

    # action == "set": (re)set the canonical name
    try:
        host = check_instance_address(reg, name, inst, args.hostname)
    except ValueError as e:
        die(str(e))
    if host in aliases:
        die(f"{host} is already an alias of '{name}'. Remove it as an alias "
            f"first if you want it as the canonical name: "
            f"'oaap app address alias-remove {name} {host}'.")
    inst["address"] = host
    save_registry(reg)
    write_instance_address_caddy()
    reload_gateway()
    print(f"'{name}' now answers for {host}.")
    _address_setup_hint(host, edge)
    print("The automatic node address keeps working — clients can move over "
          "at their own pace.")


def resolve_tenant_arg(label):
    """Turn a --tenant label from the command line into an id.

    Empty means "say nothing" -- the caller did not choose, so the
    ordinary rules decide. A label this node does not have is a typo
    worth stopping for: creating the instance in the operator's own
    tenant instead would be the silent substitution the whole
    resolution rule exists to prevent.
    """
    if not label:
        return ""
    tid, _t = tenant_by_label(label)
    if not tid:
        die(f"no tenant with label '{label}' on this node "
            "(see: oaap tenant list)")
    return tid


def cmd_install(args):
    # Store integration: the package may be a Git URL (+ --path inside
    # the repo) instead of a local directory.
    tmp_clone = None
    if re.match(r"^(https?://|git@)", args.package):
        if not shutil.which("git"):
            die("installing from a Git URL needs git on this node (apt install git)")
        tmp_clone = tempfile.mkdtemp(prefix="oaap-pkg-")
        ref = getattr(args, "ref", "") or ""
        print(f"Fetching {args.package}{' (' + ref + ')' if ref else ''} ...")
        branch = ["--branch", ref] if ref else []
        try:
            run(["git", "clone", "--depth", "1", *branch, args.package, tmp_clone])
        except subprocess.CalledProcessError as e:
            shutil.rmtree(tmp_clone, ignore_errors=True)
            die(f"git clone failed: {e.stderr.strip()}")
        pkg = os.path.join(tmp_clone, args.path) if args.path else tmp_clone
        source = {"kind": "git", "url": args.package, "path": args.path, "ref": ref}
        if getattr(args, "store_source", ""):
            # Which list this instance came from (RFC-0012 §3). A later
            # resolution prefers it, so an app installed from the
            # platform list cannot be silently re-pointed at a different
            # list after somebody adds a source. Changing the source
            # stays possible — it just becomes a deliberate act.
            source["store_source"] = args.store_source
    elif args.package.lower().endswith(".zip") and os.path.isfile(args.package):
        # an uploaded package (RFC-0019). The same path serves the CLI:
        # 'oaap app install ./bdt-app.zip --name bdt-app-test --channel test'
        # needs no repository and no credential for one.
        reg = load_registry()
        name = args.name
        if not name:
            probe = tempfile.mkdtemp(prefix="oaap-probe-")
            try:
                extract_artifact(args.package, probe)
                with open(os.path.join(package_root(probe, args.path),
                                       "oaap-app.yaml"), encoding="utf-8") as f:
                    name = (yaml.safe_load(f) or {})["app"]["id"]
            except (ArtifactRejected, KeyError, TypeError) as e:
                shutil.rmtree(probe, ignore_errors=True)
                die(str(e) if isinstance(e, ArtifactRejected)
                    else "the archive has no usable oaap-app.yaml")
            finally:
                shutil.rmtree(probe, ignore_errors=True)
        # Which tenant a NEW instance belongs to, and what it is called
        # inside it (RFC-0025 8.1). The Git and store paths get this
        # from the resolution below; this path returns early and so has
        # to do it itself -- without it `--tenant` was accepted on the
        # command line and then quietly ignored, and the instance landed
        # in the default tenant keyed by whatever was typed.
        local, permit = name, None
        inst = reg["instances"].get(name)
        if inst is None:
            owner = tenant_for_new_instance(
                None, permit={"tenant": resolve_tenant_arg(
                    getattr(args, "tenant", ""))})
            found_key, found = find_instance(reg, owner, local)
            if found is not None:
                name, inst = found_key, found
            else:
                name = instance_key(owner, local)
                if name in reg["instances"]:
                    # the name is taken, never by whom (spec 2.4)
                    die(f"an instance named '{local}' already exists")
                permit = {"tenant": owner, "name": local}
        if inst and args.channel == "test" and inst.get("channel") == "test":
            # the envelope rule applies to the CLI too — the difference is
            # that here a person is standing at the machine, so a widening
            # is reported and then proceeds
            probe = tempfile.mkdtemp(prefix="oaap-probe-")
            try:
                extract_artifact(args.package, probe)
                with open(os.path.join(package_root(probe, args.path),
                                       "oaap-app.yaml"), encoding="utf-8") as f:
                    notes = sum(envelope_review(inst, yaml.safe_load(f)), [])
            except ArtifactRejected as e:
                shutil.rmtree(probe, ignore_errors=True)
                die(str(e))
            finally:
                shutil.rmtree(probe, ignore_errors=True)
            # reported, not refused: the envelope rule protects the
            # UNATTENDED path. Here a person is at the machine, and that
            # person is the confirmation the rule asks for.
            for line in notes:
                print(f"NOTE: {line}")
        try:
            install_artifact(name, args.package, None, channel=args.channel,
                             path=args.path, permit=permit)
        except ArtifactRejected as e:
            die(str(e))
        return
    else:
        pkg = os.path.abspath(os.path.join(args.package, args.path)
                              if args.path else args.package)
        source = {"kind": "local", "url": pkg, "path": ""}
    try:
        _install_from_dir(pkg, args, source)
    finally:
        if tmp_clone:
            shutil.rmtree(tmp_clone, ignore_errors=True)


def _install_from_dir(pkg, args, source):
    mf_path = os.path.join(pkg, "oaap-app.yaml")
    if not os.path.isfile(mf_path):
        die(f"no oaap-app.yaml in {pkg}")
    with open(mf_path, encoding="utf-8") as f:
        m = yaml.safe_load(f)
    validate_manifest(m)

    app = m["app"]
    # What the caller asks for is the name INSIDE a tenant (RFC-0025
    # 8.1). What the node stores it under is the KEY, which carries the
    # tenant's frozen short name -- so two customers may both hold
    # `viewer` without their containers, networks or directories
    # colliding.
    local = args.name or app["id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", local):
        die("instance name: lowercase [a-z0-9-]")
    channel = args.channel
    # A tenant chosen for this install: a label from the command line, an
    # id from a creation permit, or nothing. Only consulted for a NEW
    # instance -- what an existing one says always wins, so a redeploy
    # can never carry an instance from one customer to another.
    chosen = str(getattr(args, "tenant", "") or "")
    chosen_tenant = (chosen if chosen in load_tenants()
                     else resolve_tenant_arg(chosen))
    reg = load_registry()
    # Which key this install writes under (RFC-0025 8.1). Three ways in,
    # and they must all land on the same instance:
    #
    #   * `--key` -- the caller already holds the key. The artifact path
    #     does: the creation permit fixed it before the instance
    #     existed, and the uploaded package is filed under it.
    #   * a name that IS a key in the registry -- a redeploy, and every
    #     instance that predates 0.1.58, whose key is its name.
    #   * otherwise a tenant-local name, which gets composed. Without
    #     this order a second delivery to `cls-viewer` would set out to
    #     create a `cls-cls-viewer`.
    explicit = str(getattr(args, "key", "") or "")
    if explicit:
        name = explicit
        inst = reg["instances"].get(name)
        if inst is not None:
            local = instance_name(name, inst)
    elif local in reg["instances"]:
        name, inst = local, reg["instances"][local]
        local = instance_name(name, inst)
    else:
        target = tenant_for_new_instance(None, permit={"tenant": chosen_tenant})
        found_key, found = find_instance(reg, target, local)
        if found is not None:
            name, inst = found_key, found
        else:
            name, inst = instance_key(target, local), None
            if name in reg["instances"]:
                # Says that the name is taken, never by whom
                # (oaap.core.tenant 2.4).
                die(f"an instance named '{local}' already exists")
    if not inst:
        # A NEW instance only: a redeploy keeps its own data by
        # definition, and its tenant cannot change (see
        # tenant_for_new_instance).
        refusal = retained_data_refusal(
            name, tenant_for_new_instance(None, permit={"tenant": chosen_tenant}))
        if refusal:
            die(refusal)
    if inst and inst["channel"] == "production" and inst["version"] == app["version"]:
        die(f"production instance '{name}' already runs version {app['version']} — bump the version (spec: redeploy semantics)")

    # A rehearsal is NOT redeployable (spec 2.15.1, RFC-0030 D2). Wrong
    # package means delete it and build another: it takes as long as the
    # copy did, and it is honest. A rehearsal that was patched until it
    # worked is no longer a rehearsal of anything -- and the thing it
    # would have been patched on is a copy of live customer data.
    if inst and is_rehearsal(inst) and not getattr(args, "rehearsal", None):
        die(f"'{name}' is a rehearsal (RFC-0030) and cannot be redeployed. "
            f"Delete it ('oaap app remove {name} --purge') and build a new "
            f"one from the test instance that has the package you want.")

    # The two recorded facts that make an instance a rehearsal (2.15.1).
    # Handed in by `oaap app rehearse` when it creates one, and kept from
    # the existing record otherwise -- a rehearsal must not lose what it
    # is, least of all its expiry date.
    rehearsal = (getattr(args, "rehearsal", None)
                 or ((inst or {}).get("rehearsal") if inst else None))

    # ---------------------------------------------- oaap.data.model (RFC-0031
    # Schritt 2, manifest 0.3): registration and binding happen HERE, before
    # any image is built or pulled, so a refusal (an ownership conflict, a
    # destructive change, an unresolved binding) leaves nothing built. This
    # is the ONE place every install path (local dir, Git, uploaded ZIP,
    # remote deploy hook) passes through -- see _install_from_dir's callers.
    dm_section = m.get("data_model")
    contributes = m.get("contributes") or []
    consumes = m.get("consumes") or []
    # RFC-0031 §4: a data_models artefact (no 'services') registers its
    # types under origin 'model:<id>', never 'app:<id>' -- it is not an
    # app, it never runs a container. Found while building the first
    # real one (Kundenzufriedenheit, RFC-0031 Schritt 4 second wave,
    # 2026-09-10): this call used 'app:' unconditionally before.
    is_artefact = not m.get("services")
    dm_origin = f"model:{app['id']}" if is_artefact else f"app:{app['id']}"
    # Needed below for oaap.data.model AND oaap.data.twin (E1) alike --
    # computed once, regardless of whether either section is present,
    # so it is ready the moment either block below needs it.
    twin_tenant = tenant_for_new_instance(inst, permit={"tenant": chosen_tenant})
    if dm_section or contributes or consumes:
        text = declaration_text(contributes, consumes)
        if text:
            print(f"NOTE (RFC-0031 D7): {text}")
        if has_profile("store") and _store_running():
            if dm_section:
                model_register_data_model(dm_origin, app["id"],
                                          app["version"], dm_section)
            if contributes or consumes:
                explicit_bind = {}
                for pair in (getattr(args, "bind", None) or []):
                    word, _, key = pair.partition("=")
                    if word and key:
                        explicit_bind[word] = key
                model_bind_declarations(name, twin_tenant, contributes, consumes,
                                        explicit=explicit_bind)
                # oaap.data.twin (RFC-0031 Schritt 3): the tenant's
                # twin_<id> schema must exist before the app ever calls
                # '/twin/*' -- provisioned here, the one place a binding
                # is created, not lazily inside the twin service itself
                # (which never holds the superuser credential this needs).
                #
                # NOT for a rehearsal: D8 (a rehearsal's OWN copy of the
                # tenant's twin schema) is not built yet, and this
                # tenant's schema is the PRODUCTION one -- a rehearsal
                # that could reach it would read and write live customer
                # data, which is the one thing RFC-0030 exists to
                # prevent. Bound above for the audit trail; refused the
                # working credential below instead.
                if not rehearsal:
                    _twin_ensure_schema(twin_tenant)
        else:
            print("NOTE: this node has no working 'store' (RFC-0011 profile, "
                  "or the service is not running) -- installing anyway, but "
                  "this app's data-model declarations were NOT registered or "
                  "bound (oaap.data.model 0.1 §2.1), and its twin schema was "
                  "NOT provisioned (oaap.data.twin 0.1).")

    # oaap.data.model 0.1 §2.8: a data_models artefact has no service, no
    # route, no health check -- and therefore no instance either. It
    # leaves no trace in the instance registry ('oaap app list' does not
    # show it, 'oaap app remove' has nothing to remove); its only
    # lasting effect is the type registration just above. Nothing below
    # this point applies (no image, no compose, no port, no registry
    # write) -- stop here.
    if is_artefact:
        print(f"Registered data_models artefact '{app['id']}' {app['version']} "
              "-- no service, no route, no health check (oaap.data.model "
              "0.1 §2.8). It carries no instance; see 'oaap data model "
              "types' for what it registered.")
        return

    # RFC-0016: an app may have several services, each its own container.
    # The PRIMARY service is the one serving "/" (or the first route, or
    # the first service) — it carries the health check and the flat
    # container/image/svc_port fields the rest of the code and old
    # backups still read.
    svc_items = list(m["services"].items())
    multi = len(svc_items) > 1
    service_names = [s for s, _ in svc_items]

    def route_service_name(r):
        return r.get("service") or service_names[0]
    root = [r for r in m["routes"] if r["path"] == "/"]
    primary_name = (route_service_name(root[0]) if root
                    else route_service_name(m["routes"][0]) if m["routes"]
                    else service_names[0])
    ordered = sorted(svc_items, key=lambda kv: kv[0] != primary_name)

    services = []
    for sname, svc in ordered:
        cname = f"oaap-app-{name}" if not multi else f"oaap-app-{name}-{sname}"
        if app["type"] == "native" or svc.get("build"):
            img = (f"oaap-app/{app['id']}:{app['version']}" if not multi
                   else f"oaap-app/{app['id']}-{sname}:{app['version']}")
            print(f"Building {img} on this node ...")
            run(["docker", "build", "-q", "-t", img,
                 os.path.join(pkg, svc.get("build", "."))])
            build = svc.get("build", "")
        else:
            img = svc["image"]
            print(f"Pulling {img} ...")
            run(["docker", "pull", "-q", img])
            build = ""
        services.append({"service": sname if multi else "", "container": cname,
                         "image": img, "build": build, "port": svc["port"]})

    primary = services[0]
    container, image = primary["container"], primary["image"]

    # port: keep existing assignment (RFC-0005), else allocate
    used = {i["port"] for i in reg["instances"].values()}
    port = inst["port"] if inst else next(p for p in PORT_RANGE if p not in used)

    # The identity BEFORE any path is touched (RFC-0026 3.1): the
    # directory hangs off it, so it has to be settled first. Handed in
    # by the artifact path, which already had to know it to file the
    # uploaded package.
    ident = getattr(args, "ident", None) or instance_identity(
        reg, name, tenant_for_new_instance(inst, permit={"tenant": chosen_tenant}),
        local)
    os.makedirs(instance_dir(name, ident), exist_ok=True)
    # The marker of 0.1.56 is written for as long as any instance still
    # lives in the flat layout. Under the tenant tree the path itself
    # says whose data this is, so the marker is redundant there -- and
    # the migration is what makes it so.
    if instance_dir(name, ident).startswith(APPS_DIR):
        stamp_data_tenant(name, ident["tenant"])

    # stable per-instance secret, never inside storage mounts. Existing
    # values win over manifest defaults: a redeploy must not undo what
    # the operator configured ('oaap app config').
    env = load_env(name, ident)
    # A rehearsal carries no secret (spec 2.15.2), and the rule is asked
    # HERE as well as before the copy -- because here it is asked against
    # the manifest that is actually being installed. The pre-copy scrub
    # only knows what the PRODUCTION instance declared secret, and a key
    # the new version has just started calling secret would otherwise
    # come up filled. `OAAP_APP_SECRET` is deliberately not touched here:
    # the creation removed it, the setdefault below mints a fresh one,
    # and rotating it on any later install would silently make the
    # instance's own stored data unreadable.
    if rehearsal:
        for c in m.get("config") or []:
            if c.get("secret"):
                env.pop(c["key"], None)
    env.setdefault("OAAP_APP_SECRET", secrets.token_hex(32))
    # oaap.data.twin (RFC-0031 Schritt 3, E1): the machine-principal key
    # this instance presents to '/twin/*'. Minted ONCE, like the secret
    # above -- a redeploy must not silently rotate a credential the app
    # may have stored, and reissuing it on every install would make
    # every earlier key an orphan nobody revoked. NOT for a rehearsal,
    # for the same reason its schema is not provisioned above: RFC-0030
    # rehearsal.
    if (contributes or consumes) and not rehearsal and "OAAP_PLATFORM_KEY" not in env:
        env["OAAP_PLATFORM_KEY"] = _twin_issue_instance_key(name, twin_tenant)
        env["OAAP_TWIN_URL"] = f"http://{GATEWAY_CONTAINER}/twin"
        print(f"Issued a machine-principal API key for '{name}' (RFC-0031 "
              "E1) -- OAAP_PLATFORM_KEY/OAAP_TWIN_URL are in its environment.")
    for c in m.get("config") or []:
        env.setdefault(c["key"], c.get("default", ""))
    save_env(name, env, ident)

    # a granted non-HTTP endpoint (RFC-0015) survives redeploy like the
    # address and visibility do — the operator's decision to open a port
    # must not be undone by a deployment. The declared endpoint(s) from
    # the manifest are re-read so 'endpoint list/allow' works offline.
    granted = (inst.get("endpoints") or []) if inst else []

    # per-instance storage, writable for the container user (guarantee 4);
    # every service container lands on the instance's own network (RFC-0016)
    recreate_instance_containers(name, services, m.get("storage") or [], granted,
                                 inst=ident)

    # visibility (RFC-0007) survives reinstall, same as the port above —
    # a redeploy must not silently reopen a group-restricted instance
    visibility = (inst.get("visibility") or {}) if inst else {}

    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w", encoding="utf-8") as f:
        f.write(caddy_site(port, m["routes"], container, primary["port"],
                           visibility.get("groups"), name,
                           throttle_of(inst or {}),
                           services=(route_targets({"services": services})
                                     if multi else None),
                           tenant=tenant_for_new_instance(
                               inst, permit={"tenant": chosen_tenant}),
                           # said explicitly: the registry entry below
                           # does not exist yet, so site_body cannot look
                           # this up for a rehearsal being created
                           login_only=bool(rehearsal)))
    reload_gateway()

    reg["instances"][name] = {
        "app_id": app["id"], "app_name": app["name"],
        # Which tenant this instance belongs to (oaap.core.tenant 1.4).
        # Note where it sits: ABOVE the "survives redeploy" block at the
        # end of this function, because it is not an operator override
        # that may be re-decided — it is who the instance BELONGS to. A
        # redeploy must never be able to move an instance from one
        # customer to another, so the existing value always wins. From
        # 0.2 a creation permit may name a different tenant here; today
        # there is only one to name.
        "tenant": ident["tenant"],
        # The identity everything on disk hangs off (RFC-0026 3.1).
        # Minted once and never changed -- not by a redeploy, not by a
        # rename, not by renaming the tenant. It is the reason a rename
        # is a rename and not a data migration.
        "id": ident["id"],
        # What this instance is called INSIDE its tenant (RFC-0025
        # 8.1). The registry KEY carries the tenant's frozen short name
        # so identifiers cannot collide across customers; this is the
        # word the customer typed, and the one their address is built
        # from.
        "name": local,
        "version": app["version"], "channel": channel,
        # what the app IS (runtime spec 2.10) — decides the launchpad
        # tile. Read from the MANIFEST at install time, never from a
        # store list: the node must answer this offline, for an app
        # installed straight from Git, and without a foreign list being
        # able to rearrange somebody else's launchpad. Re-read on every
        # install, because it describes the app and the app may change.
        # Stored VERBATIM ('' when the manifest is silent) — see
        # declared_class() for why the normalised value would be wrong.
        "app_class": declared_class(app),
        # flat fields describe the PRIMARY service — kept for the health
        # page, for legacy readers, and for backups written before 0.1.31
        "port": port, "image": image, "container": container,
        # for the portal's health page: where to reach the service on
        # the internal network and which path confirms liveness
        "svc_port": primary["port"],
        # all services (RFC-0016), primary first: {service, container,
        # image, build, port}. A single-service app has one entry with
        # service "" — route_targets() then returns None and every route
        # proxies to the primary, exactly as before.
        "services": services,
        "health_path": (m.get("health") or {}).get("path", ""),
        # for regenerating gateway sites (external hostname, RFC-0005 L3)
        "routes": m["routes"],
        # for restore (oaap.data.backup) and the deploy hook (runtime
        # spec 2.5): where the package came from and how to rebuild it
        "source": source,
        "build": primary["build"],
        "storage": m.get("storage") or [],
        # non-HTTP endpoints the app DECLARES (RFC-0015) — re-read from the
        # manifest on every install; publication needs a separate operator
        # grant (stored in "endpoints" below), which survives redeploy.
        "declared_endpoints": m.get("endpoints") or [],
        # declared config keys (labels + secret flags) so the CLI and the
        # portal can offer them for editing without the manifest at hand
        "config": [{"key": c["key"], "label": c.get("label", ""),
                    "secret": bool(c.get("secret")),
                    "multiline": bool(c.get("multiline")),
                    # spec 2.8: only a key whose value THIS app defines
                    # may ask the portal to produce it. Anything else is
                    # recorded as "no" -- a value we generate into a
                    # field meant for somebody else's credential is not
                    # a convenience, it is a wrong answer.
                    "generate": ("token" if c.get("generate") == "token"
                                 else ""),
                    "default": c.get("default", "")}
                   for c in (m.get("config") or [])
                   if c["key"] not in RESERVED_ENV],
        "description": app.get("description", ""),
        # roles that may see/open the app — the portal filters tiles
        # with this; the gateway enforces it regardless (spec 2.5)
        "roles": sorted({r for rt in m["routes"] for r in rt["roles"] if r != "public"}),
        # additional visibility restriction on top of roles (RFC-0007);
        # {} means "all" (no restriction) — set with 'oaap app visibility'
        "visibility": visibility,
    }
    # an own public hostname (RFC-0009) survives redeploy like port and
    # visibility — clients must not lose their address to a deployment
    if inst and inst.get("address"):
        reg["instances"][name]["address"] = inst["address"]
    # same for a throttle override (RFC-0010): a deployment must not
    # silently reset an operator's rate decision to the default
    if inst and inst.get("throttle") is not None:
        reg["instances"][name]["throttle"] = inst["throttle"]
    # and for the launchpad tile override (runtime spec 2.10). Note the
    # asymmetry with app_class right above: the CLASS is re-read from
    # every manifest because it describes the app, the OVERRIDE is kept
    # because it is the operator's decision about this instance.
    if inst and inst.get("tile"):
        reg["instances"][name]["tile"] = inst["tile"]
    # a granted non-HTTP endpoint (RFC-0015) is the operator's decision to
    # open a port — it survives redeploy like the address. The container
    # was already recreated with its publish mapping (via `granted`).
    if inst and inst.get("endpoints"):
        reg["instances"][name]["endpoints"] = inst["endpoints"]
    if inst and inst.get("links"):
        reg["instances"][name]["links"] = inst["links"]
    # What this instance is a rehearsal OF, and when it goes away
    # (spec 2.15.1). Written LAST and unconditionally when present, so a
    # record cannot end up a rehearsal by data and an ordinary instance
    # by gateway config.
    if rehearsal:
        reg["instances"][name]["rehearsal"] = rehearsal
    save_registry(reg)
    if channel == "production":
        # moving to production invalidates any deploy token (spec 2.5)
        # and with it every open artifact grant (RFC-0019)
        drop_token(name, "instance is on the production channel")
        grants_drop_for(name, "instance is on the production channel")
    refresh_generated_sites()
    reload_gateway()
    print(f"Installed '{name}' ({app['name']} {app['version']}, channel {channel})")
    print(f"Entry point: port {port} (through the gateway, login required)")
    for auto in instance_auto_hosts(name, reg["instances"][name]):
        print(f"External:    https://{auto}/")


def cmd_list(_args):
    reg = load_registry()
    if not reg["instances"]:
        print("No app instances installed.")
    for name, i in sorted(reg["instances"].items()):
        print(f"{name}: {i['app_name']} {i['version']} [{i['channel']}] port {i['port']} ({i['container']})")
    # Data an operator still owes somebody an answer about. It used to
    # be invisible: `oaap app remove` said where it kept it, once, and
    # after that nothing ever mentioned it again.
    kept = reg.get("retained") or {}
    if kept:
        print("")
        print("Data left behind by removed instances:")
        for v in sorted(kept.values(), key=lambda x: x.get("name", "")):
            label = tenant_label(v.get("tenant"))
            print(f"  {v.get('name', '?')}"
                  + (f" (tenant '{label}')" if label and label != DEFAULT_TENANT_LABEL else "")
                  + f" — removed {v.get('removed', '?')[:10]}, "
                    f"reinstalling under the same name recovers it")


def remove_instance(reg, name, purge):
    """Tear down one instance; returns a human-readable outcome.

    Shared by the CLI and the portal's host-side worker. Storage is only
    touched when purge is asked for — keeping it is the safe default,
    and the operator can still delete the directory later.
    """
    # RFC-0016: capture link partners before the instance leaves the
    # registry, so both the outgoing links it declared and the incoming
    # links others declared to it get torn down.
    partners = app_link_partners(reg, name)
    inst = reg["instances"].pop(name)
    data_dir = instance_dir(name, inst)
    # drop any link this instance held from the OTHER side's registry entry
    for other in reg["instances"].values():
        if name in (other.get("links") or []):
            other["links"] = [x for x in other["links"] if x != name]
    # remove every service container of the instance (RFC-0016)
    for s in instance_services(inst):
        subprocess.run(["docker", "rm", "-f", s["container"]],
                       capture_output=True, text=True)
    for other in partners:
        teardown_link_network(name, other)
    # drop the instance's own network (disconnects the gateway first)
    remove_app_network(name)
    site = os.path.join(CADDY_APPS_DIR, f"{name}.caddy")
    if os.path.isfile(site):
        os.remove(site)
    save_registry(reg)
    # generated sites are derived from the registry — regenerate them,
    # or the node keeps proxying its external name to a dead container
    refresh_generated_sites()
    # So are the readable paths (RFC-0026 3.2). Without this the
    # `by-name` link outlives the instance and points into nothing: the
    # tree still claims a name that is free, which is the one lie a
    # directory listing must not tell at two in the morning. Passing the
    # registry we already hold, so the answer does not depend on whether
    # the second save below has happened yet.
    refresh_name_links(reg)
    reload_gateway()
    drop_token(name, "instance removed")
    grants_drop_for(name, "instance removed")
    tid = resolve_tenant(inst.get("tenant")) or ""
    local = instance_name(name, inst)
    kept = reg.setdefault("retained", {})
    # A rehearsal ALWAYS takes its data with it (spec 2.15.4). Keeping it
    # is the safe default everywhere else -- here it is the opposite: what
    # would be left behind is a copy of live customer data in a directory
    # nobody looks at, and the retained record would even hand it to the
    # next instance of the same name. The expiry deletes for exactly this
    # reason; a manual removal must not be the way around it.
    if is_rehearsal(inst) and not purge:
        purge = True
        print(f"'{local}' is a rehearsal — its copy of production data is "
              "deleted with it (RFC-0030 D4).")
    if purge:
        shutil.rmtree(data_dir, ignore_errors=True)
        kept.pop(retained_key(tid, local), None)
        save_registry(reg)
        return f"removed '{local}' including data"
    # What is left behind, and under which identity. Without this note
    # the data would be unreachable the moment the registry entry is
    # gone: the directory hangs off the instance id, and a reinstall
    # would mint a new one. The promise in the sentence below -- that
    # reinstalling under the same name finds its data again -- is kept
    # by exactly this record (RFC-0026 3.2).
    if inst.get("id"):
        kept[retained_key(tid, local)] = {
            "id": inst["id"], "name": local, "tenant": tid,
            "app_name": inst.get("app_name", ""), "removed": _iso_now()}
        save_registry(reg)
    return f"removed '{local}'; data kept at {data_dir}"


def cmd_purge(args):
    """Delete data left behind by a removed instance (spec 1.4).

    `oaap app remove` keeps storage and instance.env unless --purge was
    asked for, and until 0.1.56 there was no command to get rid of them
    afterwards -- only `rm -rf` by hand. That mattered the moment
    instance names became reusable across tenants: the refusal to hand
    one tenant's data to another has to name a way out, and "delete this
    directory yourself" is not one.

    Deliberately root-only and deliberately not in the portal: this
    destroys data that a customer may still be owed, and the node
    operator is the one who can answer for it. The deletion is written
    to the audit log of the tenant the data belonged to (1.7) -- that
    log is the customer's, and this is exactly the kind of act they must
    be able to see.
    """
    name = args.name
    reg = load_registry()
    if name in reg["instances"]:
        die(f"'{name}' is an installed instance — remove it first "
            f"('oaap app remove {name} --purge' deletes it with its data)")

    # Two places to look, because two layouts exist while the migration
    # is unfinished: a note left by `oaap app remove` (the tenant tree),
    # and a bare directory under the old flat layout.
    matches = [(k, v) for k, v in (reg.get("retained") or {}).items()
               if v.get("name") == name]
    if len(matches) > 1:
        print(f"Several tenants left data behind under the name '{name}':")
        for k, v in sorted(matches):
            print(f"  tenant '{tenant_label(v.get('tenant'))}' — removed "
                  f"{v.get('removed', '?')}")
        die("name the tenant: oaap app purge <name> --tenant <label> --yes")
    if matches:
        rkey, rec = matches[0]
        held = rec.get("tenant") or ""
        d = os.path.join(tenant_dir(held), "instances", rec["id"])
    else:
        rkey, rec = "", None
        held = retained_data_tenant(name)
        if held is None:
            die(f"no data left behind under the name '{name}'")
        d = os.path.join(APPS_DIR, name)

    if not args.yes:
        label = tenant_label(held) if held else ""
        whose = f"tenant '{label}'" if label else "an unknown tenant"
        print(f"This deletes {d} and everything in it —")
        print(f"the storage and the configured secrets of {whose}.")
        print("There is no undo and no backup taken here.")
        print(f"Repeat with --yes to carry it out: oaap app purge {name} --yes")
        return
    shutil.rmtree(d, ignore_errors=True)
    if rkey:
        reg.setdefault("retained", {}).pop(rkey, None)
        save_registry(reg)
    audit_tenant("instance.purge", held or None, subject=name,
                 who=os.environ.get("SUDO_USER") or "root")
    print(f"Deleted the data left behind under '{name}' ({d}).")


def rekey_instance(reg, old_key, new_key, grace):
    """Move an instance from one node key to another (RFC-0026 3.5).

    Everything node-scoped is keyed by the key, so all of it moves
    together: the registry entry, the deploy token, any creation permit,
    the links other instances declared to it, the containers and the
    per-app network. The DATA does not move -- it hangs off the
    instance's identity, which is the whole reason this is offerable.

    The old key keeps answering the deploy hook for `grace` days.
    """
    inst = reg["instances"].pop(old_key)
    # Pin the name BEFORE the key moves. An instance from before 0.1.58
    # has no stored name and falls back to its key -- so re-keying it
    # would silently rename it too, and its address would follow. Caught
    # by test_tenant_boundary the first time a tenant rename re-keyed
    # anything (2026-09-02).
    inst["name"] = instance_name(old_key, inst)
    entry = {"key": old_key, "until": _in_days(grace)} if grace else None
    keys = [f for f in (inst.get("former_keys") or [])
            if str(f.get("until", "")) > _iso_now() and f.get("key") != new_key]
    if entry:
        keys.append(entry)
    inst["former_keys"] = keys
    reg["instances"][new_key] = inst

    # Links others declared TO this instance name the key.
    for other in reg["instances"].values():
        links = other.get("links") or []
        if old_key in links:
            other["links"] = [new_key if x == old_key else x for x in links]

    old_site = os.path.join(CADDY_APPS_DIR, f"{old_key}.caddy")
    if os.path.isfile(old_site):
        os.remove(old_site)

    # A record that never got containers -- one restored from a backup,
    # or half-installed -- has nothing to rebuild, and saying so beats
    # failing a rename over it.
    if inst.get("image") or inst.get("services"):
        # Old containers and network first: their names come from the
        # key, and two sets under two names would both be running.
        for svc in instance_services(inst):
            subprocess.run(["docker", "rm", "-f", svc["container"]],
                           capture_output=True, text=True)
        remove_app_network(old_key)
        # The stored service list carries container names built from the
        # old key; they are rebuilt from the new one.
        services = instance_services(inst)
        for svc in services:
            svc["container"] = (f"oaap-app-{new_key}" if len(services) == 1
                                else f"oaap-app-{new_key}-{svc['service']}")
        recreate_instance_containers(new_key, services,
                                     inst.get("storage") or [],
                                     inst.get("endpoints") or [], inst=inst)
        inst["services"] = services
        inst["container"] = services[0]["container"]

    token_rekey(old_key, new_key)
    grants_rekey(old_key, new_key)
    save_registry(reg)
    return inst


def rename_check(reg, given, new):
    """Resolve and validate a rename. Returns (key, error).

    Shared by the CLI and the portal's host-side worker so both refuse
    exactly the same things -- the spool is data, not trust, and a rule
    kept in two places eventually disagrees with itself.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", new or ""):
        return "", "instance name: lowercase letters, digits and hyphens"
    key = given if given in reg["instances"] else ""
    if not key:
        for k, i in reg["instances"].items():
            if instance_name(k, i) == given:
                key = k
                break
    if not key:
        return "", f"no instance named '{given}'"
    inst = reg["instances"][key]
    tid = resolve_tenant(inst.get("tenant"))
    if instance_name(key, inst) == new:
        return "", f"'{new}' is already its name"
    if find_instance(reg, tid, new)[1] is not None:
        return "", f"an instance named '{new}' already exists"
    if instance_key(tid, new) in reg["instances"]:
        return "", f"an instance named '{new}' already exists"
    return key, ""


def rename_instance(reg, key, new, grace, who="root"):
    """Carry out a checked rename. Returns the new key."""
    inst = reg["instances"][key]
    tid = resolve_tenant(inst.get("tenant"))
    local = instance_name(key, inst)
    names = [f for f in (inst.get("former_names") or [])
             if str(f.get("until", "")) > _iso_now() and f.get("name") != new]
    if grace:
        names.append({"name": local, "until": _in_days(grace)})
    inst["former_names"] = names
    inst["name"] = new
    new_key = instance_key(tid, new)
    rekey_instance(reg, key, new_key, grace)
    refresh_generated_sites()
    refresh_name_links()
    reload_gateway()
    audit_tenant("instance.rename", tid, subject=new, detail=f"was '{local}'",
                 who=who)
    return new_key


def cmd_rename(args):
    """Give an instance a different name (RFC-0026 3.3).

    What changes: what the portal shows, what the address publishes, and
    the node key every identifier is built from. What does NOT change:
    the data, because it hangs off the instance's identity and not off
    any name. That is what turns this from a migration into a restart.
    """
    old, new = args.name, (args.target or "").strip().lower()
    reg = load_registry()
    key, err = rename_check(reg, old, new)
    if err:
        die(err)
    inst = reg["instances"][key]
    tid = resolve_tenant(inst.get("tenant"))
    local = instance_name(key, inst)
    new_key = instance_key(tid, new)

    grace = max(0, int(args.grace_days))
    host = load_external() or "<node>"
    print(f"Renaming '{local}' to '{new}' changes what it is called and where "
          "it answers:")
    for a in instance_auto_hosts(key, inst, ext_host=host)[:1]:
        print(f"  {a}  ->  {instance_auto_hosts(new_key, dict(inst, name=new), ext_host=host)[0]}")
    print(f"  deploy hook /deploy/{key}  ->  /deploy/{new_key}")
    if inst.get("address"):
        print(f"  its own hostname {inst['address']} is NOT touched")
    print("")
    print(f"The old name keeps answering for {grace} more day(s) — the address "
          "and the")
    print("deploy hook both. A certificate for the new name is obtained on "
          "first contact.")
    print("")
    print("The container and the network are rebuilt, so this app restarts. "
          "Its DATA")
    print("is not moved and not touched: it hangs off the instance's identity, "
          "not")
    print("off its name.")
    if not args.yes:
        print("")
        die("nothing was changed — repeat with --yes when the above is what "
            "you want")

    rename_instance(reg, key, new, grace,
                    who=os.environ.get("SUDO_USER") or "root")
    print("")
    print(f"Renamed. '{new}' answers at "
          f"{instance_auto_hosts(new_key, inst, ext_host=host)[0]}"
          + (f", and at the old name for {grace} more day(s)." if grace else "."))


def cmd_remove(args):
    reg = load_registry()
    if args.name not in reg["instances"]:
        die(f"no instance named '{args.name}'")
    print(remove_instance(reg, args.name, args.purge).capitalize() + ".")


# ------------------------------------------------- app visibility (RFC-0007)
# Restricts, in ADDITION to the manifest's roles, who may see and reach an
# installed instance — a free-form group tag on users, an operator
# decision made per instance (never in the manifest). server_admin
# (RFC-0008) always bypasses it. The portal offers the same control
# through /instances, queued through the spool worker because its
# registry mount is read-only; this CLI writes directly (it already
# needs root).

GROUP_RE = r"[a-z0-9][a-z0-9._-]{0,39}"


def cmd_link(args):
    """Declare, drop or list app-to-app links (RFC-0016). server_admin
    territory (root here) — a deliberate hole in the default isolation,
    recorded in the registry and revocable."""
    reg = load_registry()
    if args.action in ("add", "remove") and not (args.source and args.target):
        die(f"'oaap app link {args.action}' needs a source and a target, "
            f"e.g. 'oaap app link {args.action} app-a app-b'")
    if args.action == "list":
        rows = [(a, b) for a in sorted(reg["instances"])
                for b in (reg["instances"][a].get("links") or [])]
        if not rows:
            print("No app-to-app links. Every app is isolated (RFC-0016).")
            return
        print("Declared links (source may reach target):")
        for a, b in rows:
            gone = "" if b in reg["instances"] else "  (target no longer installed)"
            print(f"  {a} -> {b}{gone}")
        return

    a, b = args.source, args.target
    for who in (a, b):
        if who not in reg["instances"]:
            die(f"no instance named '{who}'")
    if a == b:
        die("an instance cannot link to itself")
    # Neither end (spec 2.15.2). A rehearsal that still reaches
    # production B is not a copy, it is a second writer on live data --
    # and production wired to a rehearsal is wired to something that
    # deletes itself in a few days. `remove` stays open: taking a link
    # away is never the dangerous direction.
    if args.action == "add":
        for who in (a, b):
            refusal = rehearsal_refusal(reg, who, "an app-to-app link")
            if refusal:
                die(refusal)
    inst = reg["instances"][a]
    links = set(inst.get("links") or [])

    if args.action == "add":
        if b in links:
            print(f"'{a}' already links to '{b}'.")
            return
        inst["links"] = sorted(links | {b})
        save_registry(reg)
        setup_link_network(reg, a, b)
        print(f"Link added: '{a}' may reach '{b}' on a dedicated network "
              f"(RFC-0016).")
        print(f"  '{a}' resolves it by the container name "
              f"'{reg['instances'][b]['container']}'.")
    else:  # remove
        if b not in links:
            die(f"'{a}' has no link to '{b}'")
        inst["links"] = sorted(links - {b})
        save_registry(reg)
        # keep the shared network only if the reverse link still stands
        if a not in (reg["instances"][b].get("links") or []):
            teardown_link_network(a, b)
        print(f"Link removed: '{a}' can no longer reach '{b}'.")


def _endpoint_protos(proto):
    return ["udp", "tcp"] if proto == "both" else [proto]


def _print_endpoint_grant(name, ep):
    """The loud, mandatory warning + the router forwards to create
    (RFC-0015). Said in the operator's terms, not dressed up."""
    print(f"Endpoint '{ep['name']}' granted for instance '{name}'.")
    print("")
    print("  WHAT YOU JUST OPENED — read this:")
    print("  This is a RAW port straight to the app. It does NOT pass through")
    print("  the gateway, so it has NO login, NO role check, NO rate limit and")
    print("  NO access log. The app alone is responsible for who it lets in.")
    print(f"  The app author's stated reason: {ep.get('reason', '(none given)')}")
    print("")
    print("  Router port forward(s) to create on your internet router,")
    print("  pointed at THIS node:")
    for proto in _endpoint_protos(ep["protocol"]):
        print(f"    {proto.upper()}  {ep['host_port']}  ->  this node  ({proto} {ep['host_port']})")
    print("")
    print(f"  The app is told its public port via OAAP_ENDPOINT_PORT={ep['host_port']}.")
    if ep.get("fixed"):
        print(f"  This is a FIXED port: the app advertises {ep['host_port']} to its")
        print("  clients, so it was published unchanged (not reassignable).")
    print("  This address is node-local: the edge cannot forward it, and a")
    print("  restore on another machine will not bring it along.")


def cmd_endpoint(args):
    """Declare-time endpoints are in the manifest; this grants/denies them
    per instance (RFC-0015). Grant is gated on the 'exposed' node profile,
    because opening a gateway-bypassing port is exactly the power that
    profile marks a node as willing to give."""
    reg = load_registry()
    inst = reg["instances"].get(args.name)
    if not inst:
        die(f"no instance named '{args.name}'")
    declared = inst.get("declared_endpoints") or []
    granted = {e["name"]: e for e in (inst.get("endpoints") or [])}

    if args.action == "list":
        if not declared:
            print(f"'{args.name}' declares no non-HTTP endpoints.")
            return
        for d in declared:
            g = granted.get(d["name"])
            if g:
                protos = "+".join(_endpoint_protos(g["protocol"]))
                print(f"  {d['name']}: GRANTED — host port {g['host_port']} "
                      f"({protos} -> container {g['container_port']})")
            else:
                if d.get("fixed"):
                    want = f", fixed port {d['container_port']}"
                elif d.get("wish"):
                    want = f", wants {d['wish']}"
                else:
                    want = ""
                print(f"  {d['name']}: not granted ({d['protocol']}, "
                      f"container {d['container_port']}{want})")
            print(f"      reason: {d.get('reason', '').strip()}")
        return

    ep_name = args.endpoint
    if not ep_name:
        die(f"'oaap app endpoint {args.action}' needs an endpoint name "
            f"(see 'oaap app endpoint list {args.name}')")
    decl = next((d for d in declared if d["name"] == ep_name), None)
    if not decl:
        die(f"'{args.name}' declares no endpoint named '{ep_name}'")

    if args.action == "allow":
        if not has_profile("exposed"):
            die("this node has no profile 'exposed' — granting a "
                "gateway-bypassing port is refused. Set it deliberately on "
                "the machine with 'sudo oaap node add-profile exposed'.")
        if ep_name in granted:
            print(f"'{ep_name}' is already granted "
                  f"(host port {granted[ep_name]['host_port']}).")
            return
        fixed = bool(decl.get("fixed"))
        # A fixed endpoint is published unchanged (host_port == container_port),
        # so the app advertises the very port the world reaches (RFC-0017 §5.1).
        target = decl["container_port"] if fixed else decl.get("wish")
        try:
            host_port = assign_endpoint_port(reg, target, fixed=fixed)
        except EndpointPortTaken as e:
            die(str(e))
        entry = {"name": ep_name, "protocol": decl["protocol"],
                 "container_port": decl["container_port"],
                 "host_port": host_port, "service": decl.get("service", ""),
                 "fixed": fixed, "reason": decl.get("reason", "")}
        inst["endpoints"] = [e for e in (inst.get("endpoints") or [])
                             if e["name"] != ep_name] + [entry]
        save_registry(reg)
        recreate_instance_containers(args.name, instance_services(inst),
                                     inst.get("storage") or [], inst["endpoints"])
        _print_endpoint_grant(args.name, entry)
    else:  # deny
        if ep_name not in granted:
            die(f"'{ep_name}' is not currently granted")
        inst["endpoints"] = [e for e in inst["endpoints"] if e["name"] != ep_name]
        save_registry(reg)
        recreate_instance_containers(args.name, instance_services(inst),
                                     inst.get("storage") or [], inst["endpoints"])
        print(f"Endpoint '{ep_name}' denied for '{args.name}'. The raw port is "
              f"closed; you may remove its router forward.")


def cmd_visibility(args):
    reg = load_registry()
    inst = reg["instances"].get(args.name)
    if not inst:
        die(f"no instance named '{args.name}'")
    if args.mode == "all":
        groups = []
    else:
        groups = sorted({g.strip().lower() for g in (args.groups or "").split(",") if g.strip()})
        if not groups:
            die("'visibility groups' needs at least one group, "
                "e.g. 'oaap app visibility <name> groups buero,finanzen'")
        for g in groups:
            if not re.fullmatch(GROUP_RE, g):
                die(f"invalid group tag '{g}' (lowercase [a-z0-9._-], max 40 chars)")
    inst["visibility"] = {"groups": groups} if groups else {}
    save_registry(reg)
    write_app_caddy(args.name, inst)
    refresh_generated_sites()
    reload_gateway()
    if groups:
        print(f"'{args.name}' visibility set to groups: {', '.join(groups)}")
        print("Users need at least one of these groups (or the server_admin role) to see/reach it.")
    else:
        print(f"'{args.name}' visibility set to 'all' (role check only, as before).")


# ------------------------------------------- launchpad tile (runtime 2.10)
# Whether an instance appears on the launchpad. The app's own class
# decides by default — a background 'service' gets no tile — and this is
# the operator's override on top of it, per instance, kept in the
# registry alongside (never inside) the manifest-derived class. It is
# display only: routes, roles and URL are untouched and the gateway
# keeps enforcing them. To hide an app from a person, use visibility
# groups above.

TILE_MODES = ("auto", "on", "off")
TILE_EXPLAIN = {
    "auto": "follows the app itself",
    "on": "always shown",
    "off": "never shown",
}


def tile_mode_of(inst):
    """The instance's tile override; absent means 'auto'."""
    mode = str(inst.get("tile") or "").strip()
    return mode if mode in TILE_MODES else "auto"


def class_phrase(inst):
    """How to talk about an instance's class without overclaiming.

    Most of the fleet declares nothing at all — every app packaged
    before manifest 0.2. Saying "declares itself 'frontend'" about
    those is a small untruth that sends anybody debugging in the wrong
    direction, so distinguish the three cases that actually exist.
    """
    declared = str(inst.get("app_class") or "").strip()
    if declared in APP_CLASSES:
        return f"the app declares itself '{declared}'"
    if declared:
        return (f"the app declares '{declared}', which this platform does "
                f"not know, so it counts as '{DEFAULT_APP_CLASS}'")
    return f"the app declares no class, so it counts as '{DEFAULT_APP_CLASS}'"


def tile_visible(inst):
    """Does this instance belong on the launchpad? (runtime spec 2.10)

    Kept next to the registry it reads so the CLI and the portal cannot
    drift apart on the answer; the portal has its own copy of exactly
    this rule in services/portal/instance_view.py, because it runs in a
    container that cannot import this file.
    """
    mode = tile_mode_of(inst)
    if mode != "auto":
        return mode == "on"
    return instance_class(inst) != "service"


def cmd_tile(args):
    reg = load_registry()
    inst = reg["instances"].get(args.name)
    if not inst:
        die(f"no instance named '{args.name}'")
    if args.mode and args.mode not in TILE_MODES:
        die(f"tile: '{args.mode}' is not one of {' | '.join(TILE_MODES)}")
    if not args.mode:
        mode = tile_mode_of(inst)
        print(f"'{args.name}' tile: {mode} ({TILE_EXPLAIN[mode]}) — "
              f"{class_phrase(inst)}")
        print("Currently " + ("shown" if tile_visible(inst)
                              else "not shown") + " on the launchpad.")
        return
    if args.mode == "auto":
        inst.pop("tile", None)
    else:
        inst["tile"] = args.mode
    save_registry(reg)
    # No gateway work: unlike visibility, this changes nothing about
    # who may reach the instance, so no site is regenerated.
    if args.mode == "auto":
        print(f"'{args.name}' tile follows the app again "
              f"({class_phrase(inst)}) — "
              + ("shown." if tile_visible(inst) else "not shown."))
    else:
        print(f"'{args.name}' tile: {args.mode} ({TILE_EXPLAIN[args.mode]}).")
    print("This is display only — the app keeps its address, its routes "
          "and its roles. To keep people out, use 'oaap app visibility'.")


# --------------------------------------------------- user rescue (root only)
# `oaap user list|password` — a CLI-only path to see accounts and reset a
# password when the portal itself is unreachable or the caller is locked
# out. Root already implies full control of the platform (the user store
# is just a file on disk); this only saves the detour through docker
# exec + a hand-written script. Runs identity's OWN code (hashing,
# load/save) inside its container rather than reimplementing it on the
# host, so results are byte-for-byte what the app itself would produce.

def _identity_exec(script, env=None):
    cmd = ["docker", "exec"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IDENTITY_CONTAINER, "python3", "-c", script]
    try:
        return run(cmd).stdout
    except subprocess.CalledProcessError as e:
        die(f"cannot reach the identity container ({IDENTITY_CONTAINER}): "
            f"{(e.stderr or str(e)).strip()}")


def _key_row(k):
    scope = k.get("instance") or "all instances"
    used = (k.get("last_used") or "")[:10] or "never used"
    state = "REVOKED" if k.get("revoked") else f"until {k['expires'][:10]}"
    return (f"{k['id']:<10} {k['principal']:<22} {','.join(k['roles']):<24} "
            f"{scope:<20} {state:<18} {used}")


def cmd_machine(args):
    """Machine principals (RFC-0027 3.1) -- users that cannot log in.

    Deliberately its own verb even though the storage is the shared
    user store: creating one is a different act from creating a person
    (there is no password to set, and there never will be).
    """
    if args.action == "list":
        out = _identity_exec(
            "import json, app as m\n"
            "print(json.dumps([m.public_user(u) for u in m.load_users()\n"
            "                  if u.get('kind') == 'machine']))\n")
        machines = json.loads(out)
        if not machines:
            print("No machine principals on this node.")
            return
        for u in machines:
            groups = ",".join(u.get("groups") or []) or "-"
            status = "active" if u["active"] else "INACTIVE"
            print(f"{u['username']:<24} roles={','.join(u['roles']):<24} "
                  f"groups={groups:<18} {status}")
        return

    name = (args.name or "").strip().lower()
    if not name:
        die("'machine add' needs a name, e.g. 'oaap machine add terminal-3'")
    roles = sorted({r.strip() for r in (args.roles or "user").split(",")
                    if r.strip()})
    if "server_admin" in roles:
        die("a machine principal may not hold server_admin (RFC-0027 D2)")
    groups = sorted({g.strip().lower() for g in (args.groups or "").split(",")
                     if g.strip()})
    tid = resolve_tenant(args.tenant or "") if args.tenant else ensure_default_tenant()
    if tid is None:
        die(f"this node has no tenant '{args.tenant}'")
    out = _identity_exec(
        "import json, os, sys, app as m\n"
        "users = m.load_users()\n"
        "name = os.environ['OAAP_M_NAME']\n"
        "if m.find_user(users, name):\n"
        "    print('exists', file=sys.stderr); sys.exit(1)\n"
        "users.append({'username': name, 'display_name': os.environ['OAAP_M_TITLE'],\n"
        "              'password_hash': '', 'kind': 'machine',\n"
        "              'roles': json.loads(os.environ['OAAP_M_ROLES']),\n"
        "              'groups': json.loads(os.environ['OAAP_M_GROUPS']),\n"
        "              'tenant': os.environ['OAAP_M_TENANT'], 'active': True})\n"
        "m._save(m.USERS_FILE, users)\n"
        "print('ok')\n",
        {"OAAP_M_NAME": name, "OAAP_M_TITLE": args.title or "",
         "OAAP_M_ROLES": json.dumps(roles), "OAAP_M_GROUPS": json.dumps(groups),
         "OAAP_M_TENANT": tid})
    if "ok" not in out:
        die(f"a principal named '{name}' already exists")
    audit_tenant("machine.create", tid, subject=name,
                 detail="roles: " + ",".join(roles),
                 who=os.environ.get("SUDO_USER") or "root")
    print(f"Machine principal '{name}' created in tenant "
          f"'{tenant_label(tid) or 'default'}' with roles {','.join(roles)}.")
    print("It has no password and cannot use the login form. Give it a key:")
    print(f"  sudo oaap key issue {name}")


def cmd_key(args):
    """API keys (RFC-0027). The secret is printed once and never again."""
    if args.action == "list":
        out = _identity_exec(
            "import json, app as m\n"
            "print(json.dumps([m.public_key(k) for k in m.load_keys()]))\n")
        keys = json.loads(out)
        if not keys:
            print("No API keys on this node.")
            return
        print(f"{'ID':<10} {'PRINCIPAL':<22} {'ROLES':<24} {'SCOPE':<20} "
              f"{'VALID':<18} LAST USED")
        for k in sorted(keys, key=lambda k: (k["revoked"], k["principal"])):
            print(_key_row(k))
        return

    if args.action == "revoke":
        if not args.name:
            die("'key revoke' needs a key id -- see 'oaap key list'")
        out = _identity_exec(
            "import json, os, app as m\n"
            "rec = m.revoke_key(os.environ['OAAP_K_ID'])\n"
            "print(json.dumps(m.public_key(rec) if rec else None))\n",
            {"OAAP_K_ID": args.name})
        rec = json.loads(out)
        if rec is None:
            die(f"no key with id '{args.name}'")
        audit_tenant("key.revoke", rec.get("tenant") or "",
                     subject=rec["principal"], detail=f"key {rec['id']}",
                     who=os.environ.get("SUDO_USER") or "root")
        print(f"Key {rec['id']} for '{rec['principal']}' is revoked. Every "
              "request presenting it fails from now on.")
        return

    # issue
    if not args.name:
        die("'key issue' needs a principal, e.g. 'oaap key issue terminal-3'")
    roles = sorted({r.strip() for r in (args.roles or "user").split(",")
                    if r.strip()})
    out = _identity_exec(
        "import json, os, sys, app as m\n"
        "try:\n"
        "    rec, secret = m.issue_key(m.load_users(), os.environ['OAAP_K_P'],\n"
        "        json.loads(os.environ['OAAP_K_ROLES']), os.environ['OAAP_K_INST'],\n"
        "        os.environ['OAAP_K_LABEL'], int(os.environ['OAAP_K_DAYS']), 'root')\n"
        "except ValueError as e:\n"
        "    print(str(e), file=sys.stderr); sys.exit(1)\n"
        "print(json.dumps({'key': m.public_key(rec), 'secret': secret}))\n",
        {"OAAP_K_P": args.name, "OAAP_K_ROLES": json.dumps(roles),
         "OAAP_K_INST": args.instance or "", "OAAP_K_LABEL": args.label or "",
         "OAAP_K_DAYS": str(args.days)})
    res = json.loads(out)
    rec, secret = res["key"], res["secret"]
    audit_tenant("key.issue", rec.get("tenant") or "", subject=rec["principal"],
                 detail=f"key {rec['id']}, roles: " + ",".join(rec["roles"]),
                 who=os.environ.get("SUDO_USER") or "root")
    print("")
    print(f"Key {rec['id']} for '{rec['principal']}', roles "
          f"{','.join(rec['roles'])}, valid until {rec['expires'][:10]}.")
    if rec.get("instance"):
        print(f"Limited to instance '{rec['instance']}' -- it is refused "
              "anywhere else.")
    else:
        print("NOT limited to an instance. It reaches everything its roles "
              "allow --")
        print("and every app it reaches SEES this header, exactly as it sees "
              "a session")
        print("cookie today. Limit it with --instance unless something "
              "really needs")
        print("more than one app.")
    print("")
    print("  " + secret)
    print("")
    print("This is the ONLY time the secret is shown. It is stored hashed; "
          "nobody,")
    print("including this machine, can print it again. Lost it? Issue a new "
          "one and")
    print(f"revoke this: sudo oaap key revoke {rec['id']}")
    print("")
    print("Use it as:  Authorization: Bearer " + "oaapk_" + rec["id"] + "_...")


def cmd_user(args):
    if args.action == "list":
        out = _identity_exec(
            "import json, app as m\n"
            "print(json.dumps([m.public_user(u) for u in m.load_users()]))\n")
        users = json.loads(out)
        if not users:
            print("No users exist.")
            return
        for u in users:
            roles = ",".join(u["roles"]) or "-"
            groups = ",".join(u.get("groups") or []) or "-"
            status = "active" if u["active"] else "INACTIVE"
            print(f"{u['username']:<20} roles={roles:<32} groups={groups:<20} {status}")
        return

    # password
    if not args.username:
        die("'user password' needs a username, e.g. 'oaap user password joerg'")
    password = args.password or getpass.getpass("New password (min 8 chars, hidden): ")
    if len(password) < 8:
        die("password must be at least 8 characters")
    out = _identity_exec(
        "import os, sys, app as m\n"
        "from werkzeug.security import generate_password_hash\n"
        "users = m.load_users()\n"
        "u = m.find_user(users, os.environ['OAAP_CLI_USERNAME'])\n"
        "if not u:\n"
        "    print('no such user: ' + os.environ['OAAP_CLI_USERNAME'], file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "u['password_hash'] = generate_password_hash(os.environ['OAAP_CLI_PASSWORD'])\n"
        # invalidates every existing session for this user (same
        # mechanism as a self-service password change, spec 2.3) — a
        # rescue reset should not leave an old, possibly-compromised
        # session valid.
        "u['session_epoch'] = u.get('session_epoch', 0) + 1\n"
        "m._save(m.USERS_FILE, users)\n"
        "print('password reset for ' + u['username'] + ' -- existing sessions were signed out')\n",
        {"OAAP_CLI_USERNAME": args.username, "OAAP_CLI_PASSWORD": password})
    print(out.strip())


# ---------------------------------------------------------------- convert
# Compose converter (RFC-0004): import a docker-compose stack, emit one
# wrapped-app package per HTTP service plus a conversion report for
# human review. Compose "profiles" map to app SETS, not to apps.

NON_HTTP_PORTS = {27017, 6379, 5432, 5434, 3306, 3307, 1883, 9092,
                  9093, 29092, 2181, 9098}
SECRET_HINT = re.compile(r"(PASS|SECRET|TOKEN|KEY)", re.I)
ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _service_env(svc):
    env = svc.get("environment") or {}
    if isinstance(env, list):
        env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
    return {str(k): str(v) for k, v in env.items()}


def _container_port(svc, warns):
    candidates = []
    for p in svc.get("ports") or []:
        part = str(p).split(":")[-1].split("/")[0]
        if part.isdigit():
            candidates.append(int(part))
    for p in svc.get("expose") or []:
        part = str(p).split("/")[0]
        if str(part).isdigit():
            candidates.append(int(part))
    seen = list(dict.fromkeys(candidates))
    if not seen:
        return None
    if len(seen) > 1:
        warns.append(f"multiple ports {seen} — took {seen[0]}, review")
    return seen[0]


def cmd_convert(args):
    with open(args.compose, encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    services = compose.get("services") or {}
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    report = ["# Compose conversion report", ""]
    converted, skipped = [], []

    for sname, svc in sorted(services.items()):
        profiles = svc.get("profiles") or []
        if args.profile and args.profile not in profiles:
            continue
        warns = []
        image = svc.get("image")
        if not image:
            skipped.append((sname, "no image (build-based service)"))
            continue
        port = _container_port(svc, warns)
        if port is None:
            skipped.append((sname, "no port declared"))
            continue
        if port in NON_HTTP_PORTS:
            skipped.append((sname, f"port {port} is not HTTP — the gateway "
                            "routes HTTP(S) only; TCP passthrough is future work"))
            continue
        if port == 8443:
            warns.append("upstream is HTTPS — gateway->app TLS not supported in this increment")

        tag = image.rsplit(":", 1)[-1] if ":" in image.split("/")[-1] else "latest"
        version = tag if re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?", tag) else "0.1.0"
        if version == "0.1.0" and tag != "0.1.0":
            warns.append(f"image tag '{tag}' is not semver — set version manually (pin the image!)")

        app_id = re.sub(r"[^a-z0-9-]", "-", sname.lower()).strip("-")
        config, storage = [], []
        for k, v in _service_env(svc).items():
            m = ENV_REF.match(v)
            key = m.group(1) if m else k
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
                warns.append(f"env '{k}' skipped (key not manifest-compatible)")
                continue
            entry = {"key": k, "label": k.replace("_", " ").title()}
            if not m and v:
                entry["default"] = v
            if SECRET_HINT.search(k):
                entry["secret"] = True
            if "://" in v and not m:
                warns.append(f"env '{k}' references another service ({v}) — "
                             "grouped apps are not supported yet, review")
            config.append(entry)

        for vol in svc.get("volumes") or []:
            parts = str(vol).split(":")
            if len(parts) < 2:
                continue
            host, mount = parts[0], parts[1]
            name = re.sub(r"[^a-z0-9-]", "-", os.path.basename(mount).lower()).strip("-") or "data"
            if any(s["name"] == name for s in storage):
                name = f"{name}-{len(storage)}"
            storage.append({"name": name, "mount": mount})
            if "/config" in host or host.endswith((".yml", ".yaml", ".properties", ".conf")):
                warns.append(f"volume '{vol}' looks like a config FILE mount — "
                             "wrapped apps should move this to env config, review")

        if svc.get("depends_on"):
            deps = list(svc["depends_on"]) if isinstance(svc["depends_on"], (list, dict)) else []
            warns.append(f"depends_on {deps} — single-service apps only; "
                         "install dependencies as separate apps and wire via config")

        manifest = {
            "oaap_manifest": "0.1",
            "app": {"id": app_id, "name": sname, "version": version,
                    "type": "wrapped",
                    "description": f"Converted from docker-compose service '{sname}'"},
            "services": {sname: {"image": image, "port": port}},
            "routes": [{"path": "/", "roles": ["user", "keyuser", "admin"]}],
            "storage": storage,
            "config": config,
            "health": {"path": "/"},
        }
        pkg = os.path.join(out_root, app_id)
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
            f.write("# Generated by 'oaap app convert' — REVIEW BEFORE INSTALL:\n"
                    "# roles, health.path, storage and config are heuristics.\n")
            yaml.safe_dump(manifest, f, sort_keys=False, allow_unicode=True)
        converted.append((sname, app_id, warns))

    report.append(f"Converted: {len(converted)} — Skipped: {len(skipped)}")
    report.append("")
    for sname, app_id, warns in converted:
        report.append(f"## {sname} -> {app_id}/")
        for w in warns or ["no warnings"]:
            report.append(f"- {w}")
        report.append("")
    if skipped:
        report.append("## Skipped services")
        for sname, why in skipped:
            report.append(f"- {sname}: {why}")
    with open(os.path.join(out_root, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"Converted {len(converted)}, skipped {len(skipped)} services.")
    print(f"Packages and REPORT.md written to {out_root} — review before installing.")


# ----------------------- remote deployment (oaap.apps.runtime 0.2, spec 2.5)
# A deploy token is bound to exactly one TEST instance and authorizes
# exactly one action: redeploy that instance from its recorded package
# source. Tokens are stored as digests only; the portal serves the HTTP
# hook and drops a request file into the spool, a systemd path unit
# runs 'appctl.py process-deploys' on this host.

import hashlib
import hmac


def load_tokens():
    try:
        with open(DEPLOY_TOKENS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_tokens(tokens):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = DEPLOY_TOKENS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, DEPLOY_TOKENS)


def drop_token(name, reason):
    tokens = load_tokens()
    if name in tokens:
        del tokens[name]
        save_tokens(tokens)
        print(f"Deploy token for '{name}' invalidated ({reason}).")


def token_rekey(old, new):
    """Carry a deploy token over to a renamed instance.

    Dropping it instead would be the safe-looking choice and the wrong
    one: a rename is not a change of who may deploy, and forcing a new
    token would send an operator to re-paste a secret into a pipeline
    for a reason that has nothing to do with trust.
    """
    tokens = load_tokens()
    if old in tokens:
        tokens[new] = tokens.pop(old)
        save_tokens(tokens)


def audit_deploy(entry):
    import datetime
    entry["when"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(DEPLOY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ------------------------------------------------ fleet keys (RFC-0021)
# A fleet key grants exactly one thing: reading this node's status
# document (GET /fleet/status). Issued and revoked here by server_admin
# at the machine; the portal only ever sees the digests (the registry
# mount is read-only there). The label says WHO watches — that is what
# makes revocation meaningful.

def load_fleet_keys():
    try:
        with open(FLEET_KEYS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_fleet_keys(keys):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = FLEET_KEYS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
    os.replace(tmp, FLEET_KEYS)


def audit_fleet(entry):
    """Issue and revoke are state changes and get a line; polls do not."""
    import datetime
    entry["when"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(APPS_DIR, exist_ok=True)
    with open(FLEET_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def cmd_fleet(args):
    keys = load_fleet_keys()
    if args.action == "list":
        if not keys:
            print("No fleet keys exist.")
        for label, k in sorted(keys.items()):
            print(f"{label}: created {k.get('created', '?')} "
                  "(digest only — the key itself is not stored)")
        return
    label = args.label or die(
        "'fleet key {issue|revoke}' needs a label naming the consumer, "
        "e.g. fleetview@oaap-demo")
    if args.action == "revoke":
        if label not in keys:
            die(f"no fleet key labelled '{label}'")
        del keys[label]
        save_fleet_keys(keys)
        audit_fleet({"event": "revoke", "label": label})
        print(f"Fleet key '{label}' revoked — takes effect immediately.")
        return
    # issue
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,63}", label):
        die("label may use letters, digits, @ . _ - (max 64, starts "
            "with a letter or digit)")
    if label in keys:
        die(f"a fleet key labelled '{label}' already exists — revoke it "
            "first, or pick a label that names the new consumer")
    import datetime
    key = secrets.token_urlsafe(32)
    keys[label] = {
        "digest": hashlib.sha256(key.encode()).hexdigest(),
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_fleet_keys(keys)
    audit_fleet({"event": "issue", "label": label})
    ext = load_external()
    url = (f"https://{ext}/fleet/status" if ext
           else "http://<lan-address>/fleet/status")
    print(f"Fleet key '{label}' (shown ONCE — enter it as a secret "
          "config value of the fleet overview app):")
    print("")
    print(f"  {key}")
    print("")
    print("It grants exactly one thing — reading this node's status document:")
    print(f"  curl {url} -H \"Authorization: Bearer <key>\"")
    print("Revoke it any time with: sudo oaap fleet key revoke " + label)


def cmd_token(args):
    reg = load_registry()
    tokens = load_tokens()
    if args.action == "list":
        if not tokens:
            print("No deploy tokens exist.")
        for name, t in sorted(tokens.items()):
            print(f"{name}: created {t.get('created', '?')} (digest only — the token itself is not stored)")
        return
    name = args.name or die("'token {create|revoke}' needs an instance name")
    if args.action == "revoke":
        if name not in tokens:
            die(f"no deploy token exists for '{name}'")
        drop_token(name, "revoked")
        return
    inst = reg["instances"].get(name)
    if not inst:
        die(f"no instance named '{name}'")
    if inst["channel"] != "test":
        die(f"'{name}' is a production instance — deploy tokens exist only for "
            "the test channel (spec 2.5); promotion stays a human action.")
    if not inst.get("source"):
        die(f"'{name}' has no recorded package source — reinstall it once, then create the token.")
    import datetime
    token = secrets.token_urlsafe(32)
    tokens[name] = {
        "digest": hashlib.sha256(token.encode()).hexdigest(),
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_tokens(tokens)
    ext = load_external()
    hook = (f"https://{ext}/deploy/{name}" if ext else f"http://<lan-address>/deploy/{name}")
    print(f"Deploy token for '{name}' (shown ONCE — store it in the project's AI briefing):")
    print("")
    print(f"  {token}")
    print("")
    print("Usage (after pushing to the recorded source):")
    print(f"  curl -X POST {hook} -H \"Authorization: Bearer <token>\"")
    print("The token redeploys only this test instance from its recorded source.")


# -------------------------------------- artifact deployment (RFC-0019)
# A deployment may bring its own package instead of naming a source the
# node fetches. The reason is not convenience: fetching from a private
# repository forces this node to hold a FOREIGN credential in cleartext
# in the registry, and thus in every backup. An artifact is a package,
# not an access right — nothing foreign is kept.
#
# Three phases (RFC-0019 §2): announce (version, manifest, checksum,
# size) → the node validates and issues a single-use upload grant → the
# artifact is admitted only against that grant and checked again before
# anything is unpacked. The announcement is the contract: the manifest
# INSIDE the artifact must be byte-identical to the announced one, or
# phase 1 would be theatre.

ARTIFACT_KEEP = 4                 # current + three predecessors (decision 6)
ARTIFACT_MAX_BYTES = 256 * 1024 * 1024
ARTIFACT_MAX_UNPACKED = 1024 * 1024 * 1024
ARTIFACT_MAX_ENTRIES = 20000
GRANT_TTL = 900                   # 15 minutes
# An instance creation grant (RFC-0019, Studio section) is handed from
# the portal to a person, who then walks to another app, picks a file
# and uploads it. Fifteen minutes is enough for a machine and not for a
# human, so this one is longer — still short-lived, still single-use.
CREATE_GRANT_TTL = 1800           # 30 minutes
GRANT_MAX_ATTEMPTS = 3


class ArtifactRejected(Exception):
    """A refusal with a sentence its recipient can act on.

    The recipient is usually an AI without a person next to it, so the
    message has to say what to change, not merely that something is
    wrong.
    """


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_artifact(zip_path, dest):
    """Unpack an untrusted archive (RFC-0019 §5).

    Everything here guards against an archive that was built to escape
    its own extraction: absolute paths and '..' walk out of the target,
    symlinks point out of it after the fact, and a small file can expand
    to fill the disk. The expansion bound is therefore checked WHILE
    unpacking — the declared size in the header is the attacker's number.
    """
    dest = os.path.abspath(dest)
    total = 0
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ArtifactRejected("the upload is not a readable ZIP archive")
    with archive as z:
        infos = z.infolist()
        if len(infos) > ARTIFACT_MAX_ENTRIES:
            raise ArtifactRejected(
                f"archive has {len(infos)} entries, limit is {ARTIFACT_MAX_ENTRIES}")
        for zi in infos:
            entry = zi.filename
            if entry.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", entry):
                raise ArtifactRejected(f"absolute path in archive: {entry}")
            target = os.path.abspath(os.path.join(dest, entry))
            if target != dest and not target.startswith(dest + os.sep):
                raise ArtifactRejected(f"path escapes the package root: {entry}")
            if zi.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            mode = (zi.external_attr >> 16) & 0o170000
            # mode 0 = written by a tool that records no unix modes
            if mode not in (0, stat.S_IFREG):
                raise ArtifactRejected(
                    f"archive contains a link or special file: {entry}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(zi) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > ARTIFACT_MAX_UNPACKED:
                        raise ArtifactRejected(
                            "archive expands past the unpacked size limit "
                            f"({ARTIFACT_MAX_UNPACKED // (1024 * 1024)} MB)")
                    out.write(chunk)
    return total


def package_root(unpacked, path=""):
    """Where oaap-app.yaml lives inside an unpacked artifact.

    An explicit --path wins. Otherwise the root, and failing that a
    single top-level directory that holds it — 'zip up the project
    folder' is what people actually do, and refusing it would teach
    nothing.
    """
    if path:
        return os.path.join(unpacked, path)
    if os.path.isfile(os.path.join(unpacked, "oaap-app.yaml")):
        return unpacked
    entries = [e for e in os.listdir(unpacked)
               if os.path.isdir(os.path.join(unpacked, e))]
    if len(entries) == 1:
        nested = os.path.join(unpacked, entries[0])
        if os.path.isfile(os.path.join(nested, "oaap-app.yaml")):
            return nested
    raise ArtifactRejected(
        "no oaap-app.yaml found in the archive — it must sit at the root "
        "of the ZIP (or in a single top-level folder)")


def artifact_dir(name, inst=None):
    return os.path.join(instance_dir(name, inst), "artifacts")


def artifact_list(name, inst=None):
    """Retained artifacts, newest first."""
    d = artifact_dir(name, inst)
    try:
        files = [f for f in os.listdir(d) if f.endswith(".zip")]
    except OSError:
        return []
    # The name is the tiebreaker, so two packages received in the same
    # second still have ONE order -- the pruning below deletes by this
    # list, and "which one goes" must not depend on how the filesystem
    # felt about listing the directory.
    return sorted(files, key=lambda f: (os.path.getmtime(os.path.join(d, f)), f),
                  reverse=True)


def artifact_store(name, src_path, version, sha, inst=None):
    """Keep the artifact so it stays a real source (RFC-0019 §4)."""
    d = artifact_dir(name, inst)
    os.makedirs(d, exist_ok=True)
    fn = f"{version}-{sha[:12]}.zip"
    dst = os.path.join(d, fn)
    # a redeploy and a rollback install FROM the retained artifact, so
    # source and destination are the same file — copying it onto itself
    # would truncate it
    if os.path.abspath(src_path) != os.path.abspath(dst):
        shutil.copyfile(src_path, dst)
    else:
        os.utime(dst, None)     # keep retention ordering honest
    return fn


def artifact_prune(name, keep=ARTIFACT_KEEP, inst=None):
    d = artifact_dir(name, inst)
    for old in artifact_list(name, inst)[keep:]:
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass
    artifact_index_write()


# The portal cannot see a retained package. Since RFC-0026 an instance's
# data lives under `tenants/<tid>/instances/<iid>/`, and the portal's
# only registry mount is `apps/` -- deliberately, because the tenant
# tree holds every instance.env of every customer and the portal has no
# business reading that.
#
# So the host writes down what it found. This file is an INDEX, never
# the truth: the portal renders it, and the download re-asks the host,
# which checks the real directory again. A stale line therefore costs a
# refused download, never a wrong file.
#
# Written from save_registry() and from the two functions that change
# the stored packages without necessarily saving the registry. That it
# is one function called from three places is the point -- three
# listings would be three chances to drift.
ARTIFACT_INDEX = os.path.join(APPS_DIR, "artifacts.json")


def artifact_index_write(reg=None):
    """List every instance's retained packages where the portal can read."""
    try:
        reg = reg if reg is not None else load_registry()
        out = {}
        for name, inst in (reg.get("instances") or {}).items():
            if (inst.get("source") or {}).get("kind") != "artifact":
                continue
            d = artifact_dir(name, inst)
            files = []
            for fn in artifact_list(name, inst):
                try:
                    st = os.stat(os.path.join(d, fn))
                except OSError:
                    continue
                files.append({"file": fn, "bytes": st.st_size,
                              "received": _iso(st.st_mtime)})
            if files:
                out[name] = files
        tmp = ARTIFACT_INDEX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": "0.1", "instances": out}, f, indent=2)
        os.replace(tmp, ARTIFACT_INDEX)
        os.chmod(ARTIFACT_INDEX, 0o644)
    except OSError as e:
        # A listing that cannot be written must not fail the operation
        # that was actually asked for -- it is a view, not a record.
        print(f"WARNING: could not write {ARTIFACT_INDEX}: {e}", flush=True)


def _iso(epoch):
    import datetime
    return (datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .isoformat(timespec="seconds"))


def artifact_remove(inst, name, want):
    """Delete one retained package — never the one in service.

    RFC-0024 §6. The package an instance runs from is not a copy of
    something else: backup completeness (RFC-0019 §4), rollback and
    promotion (RFC-0020) all read exactly that file. Deleting it would
    leave a working instance that cannot be reproduced, so the refusal
    is by name and says why.
    """
    if want not in artifact_list(name):
        raise ValueError("no such retained artifact")
    running = ((inst or {}).get("source") or {}).get("stored") or ""
    if want == running:
        raise ValueError(
            f"'{want}' is the package this instance runs from — it stays. "
            "Backup, rollback and promotion all read it; deleting it would "
            "leave an instance nobody can rebuild. Deploy another version "
            "first, then this one can go.")
    os.remove(os.path.join(artifact_dir(name), want))
    artifact_index_write()
    return f"deleted retained package {want}"


# How long an authorised export may lie in the spool before the host
# takes it back. The portal fetches it within one request; anything
# older is a portal that died mid-download.
EXPORT_TTL_SECONDS = 900


def artifact_export(inst, name, want, rid):
    """Hand ONE retained package to the portal, for ONE download.

    The portal cannot read the tenant tree (see ARTIFACT_INDEX), and it
    must not start being able to just because a download exists: the
    tree holds every customer's instance.env. So the host puts the one
    authorised file where the portal already reads -- the spool it
    already uses in the other direction for uploads -- and the portal
    removes it when it has sent it.

    A hard link where the filesystem allows one: the two live under the
    same data directory, so this costs no second copy of a package that
    may be a quarter of a gigabyte, and removing the link in the spool
    leaves the retained package untouched.
    """
    if want not in artifact_list(name, inst):
        # Not a path. Never joined onto a directory, never stat'ed --
        # the name is checked against what is actually retained.
        raise ValueError(f"'{want}' is not a package this instance retains")
    d = os.path.join(SPOOL_DIR, "exports")
    os.makedirs(d, exist_ok=True)
    # Sweep first: a portal that died between authorisation and download
    # would otherwise leave the package lying in the spool forever.
    cutoff = time.time() - EXPORT_TTL_SECONDS
    for stale in os.listdir(d):
        p = os.path.join(d, stale)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass
    src = os.path.join(artifact_dir(name, inst), want)
    dst = os.path.join(d, f"{rid}.zip")
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
    return want


def source_package_arg(name, src):
    """The 'package' argument that reinstalls from a recorded source."""
    if (src or {}).get("kind") == "artifact":
        stored = src.get("stored") or ""
        return os.path.join(artifact_dir(name), stored) if stored else ""
    return (src or {}).get("url", "")


def _public_paths(routes):
    return {r["path"] for r in (routes or []) if "public" in (r.get("roles") or [])}


def _endpoint_keys(endpoints):
    return {(e.get("name", ""), e.get("protocol", ""), e.get("container_port"),
             bool(e.get("fixed"))) for e in (endpoints or [])}


def _storage_keys(storage):
    return {(s.get("name", ""), s.get("mount", "")) for s in (storage or [])}


def envelope_review(inst, m):
    """What a deployment may do on its own, and what needs a person.

    RFC-0019 §3: a deploy token redeploys within the envelope already
    granted to the instance; anything that WIDENS the envelope requires
    a human. Returns (hard, confirm) — two lists of sentences.
    """
    hard, confirm = [], []
    app = m["app"]
    if inst.get("app_id") and app["id"] != inst["app_id"]:
        hard.append(
            f"the artifact is app '{app['id']}', but this instance runs "
            f"'{inst['app_id']}' — an instance belongs to one app")
    if inst.get("version") and app["version"] == inst["version"]:
        hard.append(
            f"version {app['version']} is already installed — an artifact "
            "deployment must carry a different version, it is the only "
            "record of what is running")
    new_public = _public_paths(m.get("routes")) - _public_paths(inst.get("routes"))
    if new_public:
        confirm.append("routes become reachable without login: "
                       + ", ".join(sorted(new_public)))
    new_ep = _endpoint_keys(m.get("endpoints")) - _endpoint_keys(inst.get("declared_endpoints"))
    if new_ep:
        confirm.append("new declared endpoints (ports past the gateway): "
                       + ", ".join(sorted(e[0] for e in new_ep)))
    new_st = _storage_keys(m.get("storage")) - _storage_keys(inst.get("storage"))
    if new_st:
        confirm.append("new storage mounts: "
                       + ", ".join(sorted(s[0] for s in new_st)))
    return hard, confirm


# --- single-use grants -------------------------------------------------
# Nothing here is long-lived, and nothing here is held by whoever uses
# it: a grant is spent, not kept (RFC-0019, Studio section).

def load_grants():
    try:
        with open(ARTIFACT_GRANTS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_grants(grants):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = ARTIFACT_GRANTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(grants, f, indent=2)
    os.replace(tmp, ARTIFACT_GRANTS)


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _grants_prune(grants):
    now = _now()
    return {k: v for k, v in grants.items() if v.get("expires", 0) > now}


def grant_create(kind, instance, digest, payload, ttl=GRANT_TTL):
    grants = _grants_prune(load_grants())
    # one live grant per instance and kind — announcing again replaces
    # the previous announcement instead of accumulating open doors
    grants = {k: v for k, v in grants.items()
              if not (v.get("kind") == kind and v.get("instance") == instance)}
    grants[digest] = {"kind": kind, "instance": instance, "attempts": 0,
                      "expires": _now() + ttl, "payload": payload}
    save_grants(grants)


def grant_spend(kind, instance, digest):
    """Consume a grant. Returns its payload, or None if it does not hold."""
    grants = _grants_prune(load_grants())
    entry = grants.get(digest)
    if (not entry or entry.get("kind") != kind
            or entry.get("instance") != instance):
        save_grants(grants)
        return None
    entry["attempts"] = entry.get("attempts", 0) + 1
    if entry["attempts"] > GRANT_MAX_ATTEMPTS:
        del grants[digest]
        save_grants(grants)
        return None
    payload = entry["payload"]
    del grants[digest]          # single use
    save_grants(grants)
    return payload


def grant_check(kind, instance, digest):
    """Is this grant live, without spending it?

    Deliberately separate from grant_spend: an instance creation grant
    is checked when the package is ANNOUNCED but only spent when the
    package is actually installed. Spending it earlier would burn the
    operator's permission on a failed upload and force them back to the
    portal for a network hiccup.
    """
    entry = _grants_prune(load_grants()).get(digest) or {}
    if entry.get("kind") != kind or entry.get("instance") != instance:
        return None
    return entry.get("payload") or {}


def grants_of_kind(kind):
    """Live grants of one kind, for a page that shows what is open."""
    out = []
    for entry in _grants_prune(load_grants()).values():
        if entry.get("kind") == kind:
            out.append({"instance": entry.get("instance", ""),
                        "expires": entry.get("expires", 0)})
    return sorted(out, key=lambda e: e["instance"])


def grants_drop_for(instance, reason=""):
    grants = {k: v for k, v in _grants_prune(load_grants()).items()
              if v.get("instance") != instance}
    save_grants(grants)
    if reason:
        print(f"Artifact grants for '{instance}' dropped ({reason}).")


def grants_rekey(old, new):
    """Carry open grants over to a renamed instance -- same reasoning as
    token_rekey, plus one of its own: a creation permit names a key that
    was chosen before the instance existed, and letting it point at the
    old one would leave a licence to create something under a name that
    is now taken."""
    grants = _grants_prune(load_grants())
    changed = False
    for key in list(grants):
        g = grants[key]
        if g.get("instance") != old:
            continue
        g["instance"] = new
        if key.startswith("pending:"):
            grants[f"pending:{new}"] = grants.pop(key)
        changed = True
    if changed:
        save_grants(grants)


def announce_artifact(name, manifest_text, artifact_sha, artifact_bytes,
                      digest, confirmed=False, create_digest=""):
    """Phase 1+2: validate the announcement, issue the upload grant.

    With `create_digest` this announces the FIRST package for an
    instance that does not exist yet, against an instance creation
    grant an administrator issued in the portal (RFC-0019, Studio
    section). Everything else is identical — which is the point: the
    creation is the same handshake one level up, not a second path.

    Raises ArtifactRejected with a sentence the caller can act on.
    """
    reg = load_registry()
    inst = reg["instances"].get(name)
    creating = bool(create_digest)
    if creating:
        # The grant is only CHECKED here; it is spent when the package
        # is actually installed (phase 3), so a failed upload does not
        # cost the operator their permission.
        if inst:
            raise ArtifactRejected(f"an instance named '{name}' already exists "
                                   "— use its deploy token, not a creation grant")
        if not has_profile("dev"):
            raise ArtifactRejected(
                "this node has no profile 'dev' — creating instances is a "
                "development act (RFC-0011)")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
            raise ArtifactRejected(
                "instance name: lowercase letters, digits and hyphens")
        if grant_check("create", name, create_digest) is None:
            raise ArtifactRejected(
                "no valid creation grant for this instance — have an "
                "administrator issue one in the portal (it is single-use "
                f"and lasts {CREATE_GRANT_TTL // 60} minutes)")
    elif not inst:
        raise ArtifactRejected(f"no instance named '{name}'")
    elif inst.get("channel") != "test":
        raise ArtifactRejected(
            f"'{name}' is a production instance — artifact deployment exists "
            "only for the test channel; promotion stays a human action")
    if artifact_bytes > ARTIFACT_MAX_BYTES:
        raise ArtifactRejected(
            f"artifact is {artifact_bytes} bytes, limit is "
            f"{ARTIFACT_MAX_BYTES // (1024 * 1024)} MB")
    if not re.fullmatch(r"[0-9a-f]{64}", (artifact_sha or "").lower()):
        raise ArtifactRejected("artifact_sha256 must be a hex SHA-256 digest")
    try:
        m = yaml.safe_load(manifest_text)
    except yaml.YAMLError as e:
        raise ArtifactRejected(f"the announced manifest is not valid YAML: {e}")
    if not isinstance(m, dict):
        raise ArtifactRejected("the announced manifest is empty or not a mapping")
    import contextlib
    import io as _io
    buf = _io.StringIO()
    try:
        # the validator reports to the console and exits; here the report
        # IS the answer — the recipient is usually an AI with nobody
        # next to it to read a log
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            validate_manifest(m)
    except SystemExit:
        detail = buf.getvalue().strip().replace("ERROR: ", "")
        raise ArtifactRejected(
            "the announced manifest is invalid — "
            + (detail or "validate it against the published schema"))
    # A new instance has no envelope to widen: what the manifest asks
    # for IS the envelope the administrator agreed to when they issued
    # the grant. Reviewing it against nothing would refuse every first
    # package that declares a public route.
    hard, confirm = ([], []) if creating else envelope_review(inst, m)
    if hard:
        raise ArtifactRejected("; ".join(hard))
    manifest_sha = hashlib.sha256(manifest_text.encode()).hexdigest()
    if confirm and not confirmed:
        # remembered so an administrator can confirm exactly THIS
        # manifest in the portal — not "the next deployment, whatever
        # it turns out to be"
        pending = _grants_prune(load_grants())
        prev = (pending.get(f"pending:{name}") or {}).get("payload") or {}
        # A confirmation that was already given, for a DIFFERENT package.
        # This is the loop that cost an afternoon on oaapx01 (2026-08-24):
        # refuse -> human confirms -> the client raises the version as our
        # own deployment sheet told it to -> different manifest -> the
        # confirmation no longer applies -> refuse. Three rounds before a
        # human broke it by hand. The client never sees the portal, so the
        # way out has to be IN THIS SENTENCE.
        overtaken = (prev.get("confirmed")
                     and prev.get("manifest_sha") != manifest_sha)
        pending[f"pending:{name}"] = {
            "kind": "pending", "instance": name, "attempts": 0,
            "expires": _now() + 7 * 24 * 3600,
            "payload": {"manifest_sha": manifest_sha, "reasons": confirm,
                        "version": m["app"]["version"]}}
        save_grants(pending)
        if overtaken:
            raise ArtifactRejected(
                f"an administrator confirmed version {prev.get('version')}, "
                f"but you have now announced {m['app']['version']} — a "
                "confirmation covers exactly the manifest it was given for, "
                f"so it does not cover this one. Announce {prev.get('version')} "
                "again, byte for byte unchanged, and it will install. "
                "Raising the version does NOT help here: the rule that "
                "forbids an unchanged version compares against what is "
                f"INSTALLED, and {prev.get('version')} is not installed. "
                "Your new announcement is now the one waiting for "
                "confirmation (" + "; ".join(confirm) + ")")
        raise ArtifactRejected(
            "this deployment would widen what the instance may reach or who "
            "may reach it (" + "; ".join(confirm) + ") — it needs an "
            "administrator's confirmation in the portal. The confirmation is "
            "bound to THIS manifest: once it is given, announce and upload "
            "exactly this package again, unchanged. Do not raise the version "
            "in between — a different manifest is a different deployment and "
            "the confirmation would not cover it")
    grant_create("upload", name, digest,
                 {"manifest_sha": manifest_sha, "artifact_sha256": artifact_sha.lower(),
                  "bytes": int(artifact_bytes), "version": m["app"]["version"],
                  # carried so phase 3 knows this upload creates the
                  # instance, and which permission to spend for it
                  "create": creating, "create_digest": create_digest})
    return m["app"]["version"]


def install_artifact(name, zip_path, grant, channel="test", path="", origin="",
                     permit=None, ident=None, rehearsal=None):
    """Phase 3: verify the upload against its grant, then install.

    `grant` is positional and mandatory on purpose. It was optional
    once, and the worker forgot to pass it — every check below was
    silently skipped, and a package that contradicted its own
    announcement installed cleanly (found on oaap-demo, 2026-08-15).
    A caller with nothing to verify against — the CLI, a rollback, an
    administrator creating an instance — has to say so by passing None.
    """
    # Settled once, here, because the uploaded package is filed under
    # the instance's identity and the install that follows must use the
    # same one (RFC-0026 3.1). Minting it twice would file the package
    # in one directory and start the container on another.
    _reg = load_registry()
    _existing = _reg["instances"].get(name)
    _tenant = (tenant_for_new_instance(_existing, permit=permit)
               if _existing is None else _existing.get("tenant"))
    # A caller may hand the identity in: `oaap app rehearse` has already
    # laid the copied data down under it, and minting a second one here
    # would file the package in one directory and start the container on
    # another (the mistake this whole block exists to prevent).
    ident = ident or instance_identity(_reg, name, _tenant,
                                       (permit or {}).get("name", "") or name)
    size = os.path.getsize(zip_path)
    if grant is not None:
        if size != grant.get("bytes"):
            raise ArtifactRejected(
                f"upload is {size} bytes, {grant.get('bytes')} were announced")
        got = _sha256_file(zip_path)
        if not hmac.compare_digest(got, grant.get("artifact_sha256", "")):
            raise ArtifactRejected(
                "the upload does not match the announced checksum")
    elif size > ARTIFACT_MAX_BYTES:
        raise ArtifactRejected(
            f"artifact is {size} bytes, limit is "
            f"{ARTIFACT_MAX_BYTES // (1024 * 1024)} MB")
    sha = _sha256_file(zip_path)
    unpacked = tempfile.mkdtemp(prefix="oaap-artifact-")
    try:
        extract_artifact(zip_path, unpacked)
        pkg = package_root(unpacked, path)
        mf = os.path.join(pkg, "oaap-app.yaml")
        if not os.path.isfile(mf):
            raise ArtifactRejected(f"no oaap-app.yaml in {path or 'the archive'}")
        with open(mf, "rb") as f:
            manifest_bytes = f.read()
        if grant is not None:
            inner = hashlib.sha256(manifest_bytes).hexdigest()
            if not hmac.compare_digest(inner, grant.get("manifest_sha", "")):
                # without this the announcement would be decoration: one
                # could announce a harmless manifest and ship another
                raise ArtifactRejected(
                    "the manifest inside the archive differs from the "
                    "announced one — announce the manifest you are shipping")
        m = yaml.safe_load(manifest_bytes.decode("utf-8"))
        version = m["app"]["version"]
        stored = artifact_store(name, zip_path, version, sha, inst=ident)
        source = {"kind": "artifact", "version": version, "sha256": sha,
                  "stored": stored, "path": path,
                  "received": _stamp()}
        if origin:
            # where production got it from (RFC-0020) — so "what runs
            # here?" is answerable with a test instance and a checksum
            source["promoted_from"] = origin
        # The creation permit is the ONE record that names a tenant
        # before the instance exists (oaap.core.tenant 1.4), and this is
        # where that choice finally lands. Empty for a redeploy, which
        # is right: the instance already knows, and what it says wins.
        # `name` here is the KEY -- the uploaded package is already
        # filed under it (artifact_store above), and for a permit it was
        # fixed before the instance existed. The tenant-local name comes
        # from the permit, which is the only record that knows it that
        # early (RFC-0025 8.1).
        ns = argparse.Namespace(package=pkg, path="", ref="",
                                ident=ident, key=name,
                                name=(permit or {}).get("name", "") or name,
                                channel=channel, store_source="",
                                tenant=(permit or {}).get("tenant", ""),
                                # what makes this instance a rehearsal
                                # (spec 2.15.1) -- passed through so the
                                # gateway site is written login-only from
                                # the first second, not from the second
                                # write after the record exists
                                rehearsal=rehearsal)
        try:
            _install_from_dir(pkg, ns, source)
        except BaseException:
            try:
                os.remove(os.path.join(artifact_dir(name, ident), stored))
            except OSError:
                pass
            # ... and the directory the identity was minted for, when
            # the instance never came into being. Found on oaap-test
            # 2026-09-08: two rejected manifests left
            # `tenants/<tid>/instances/<iid>/artifacts/` behind, empty
            # and referenced by nothing -- the identity is deliberately
            # not persisted until the install finishes, so the next
            # attempt mints a new one and the old directory is litter
            # that nobody can ever attribute. `rmdir` is the guard: it
            # refuses on anything that is not empty, so a redeploy that
            # failed keeps every byte it had.
            if _existing is None:
                for d in (os.path.join(instance_dir(name, ident), "artifacts"),
                          instance_dir(name, ident)):
                    try:
                        os.rmdir(d)
                    except OSError:
                        break
            raise
        artifact_prune(name, inst=ident)
        return version, sha
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)


def _stamp():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- promotion to production (RFC-0020) --------------------------------
# Moving BYTES, not permissions: the artifact already lies on this node,
# was accepted once, and is installed again — unchanged — into the
# production instance. What goes live is exactly what was tested, and
# "exactly" means the same checksum, not the same version string.


class PromotionRefused(Exception):
    """A refusal a person can act on — same contract as ArtifactRejected."""


def promotion_target(reg, source, target_name):
    """(key, tenant, record) of the production instance a promotion aims at.

    A promotion stays inside ONE tenant: what goes live belongs to
    whoever owns the test instance it was tested in, and it is called
    what that tenant calls it -- never the node-wide key (RFC-0025 8.1,
    RFC-0022 D1). The record is None when the target does not exist yet,
    which is the ordinary case: promoting is how a production instance
    usually comes into being.

    One function, because three callers need the same answer and a
    second derivation would drift: the worker needs the KEY for the log
    and the registry, install_artifact needs the TENANT and the NAME,
    and the CLI needs to say which of the two it is about to create.
    """
    tenant = resolve_tenant((reg["instances"].get(source) or {}).get("tenant"))
    key, existing = find_instance(reg, tenant, target_name)
    if existing is not None:
        return key, tenant, existing
    key = instance_key(tenant, target_name)
    # A key that is taken belongs to somebody -- possibly another
    # tenant, if their slug and name happen to compose the same word.
    # Returning that record lets the caller refuse instead of writing
    # into it.
    return key, tenant, reg["instances"].get(key)


def promotion_review(reg, source, target_name):
    """Everything that must hold before a promotion may run.

    Returns (artifact_path, manifest, notes). `notes` are envelope
    widenings against the TARGET — not refusals: a server_admin is
    present, and that is the human the envelope rule asks for. They MUST
    be shown and confirmed, which is the caller's job.

    Deliberately a pure check that reads no request and writes nothing:
    the portal calls it to show the notes, the host calls it again
    before it acts, and both get the same answer.
    """
    src = reg["instances"].get(source)
    if not src:
        raise PromotionRefused(f"no instance named '{source}'")
    if is_rehearsal(src):
        # Said in its own words rather than left to the channel check
        # below, which would answer "not a test instance" and hide the
        # actual rule (spec 2.15.3).
        raise PromotionRefused(
            f"'{source}' is a rehearsal (RFC-0030) — a verdict, not a "
            "source. What goes live are the bytes of the TEST instance the "
            "rehearsal took its code from; promote that one.")
    if src.get("channel") != "test":
        raise PromotionRefused(
            f"'{source}' is not a test instance — promotion goes from test "
            "to production, never the other way")
    # Asked BEFORE the package is looked for: refusing a rehearsal target
    # needs no bytes, and a node whose retained package is gone would
    # otherwise answer the wrong question first.
    _k, _t, _target = promotion_target(reg, source, target_name)
    if _target is not None and is_rehearsal(_target):
        raise PromotionRefused(
            f"'{target_name}' is a rehearsal (RFC-0030) and is not "
            "redeployable. Promote into the production instance it was "
            "copied from, or delete the rehearsal and build a new one.")
    stored = (src.get("source") or {})
    if stored.get("kind") != "artifact":
        raise PromotionRefused(
            f"'{source}' does not run from an uploaded package — promotion "
            "guarantees the same BYTES, which only a retained artifact can "
            "prove. Install the production instance from its own source "
            "instead (store or 'oaap app install')")
    path = source_package_arg(source, stored)
    if not path or not os.path.isfile(path):
        raise PromotionRefused(
            f"the retained package of '{source}' is gone — deploy it once "
            "more, then promote")

    unpacked = tempfile.mkdtemp(prefix="oaap-promote-")
    try:
        extract_artifact(path, unpacked)
        pkg = package_root(unpacked, stored.get("path", ""))
        mf = os.path.join(pkg, "oaap-app.yaml")
        if not os.path.isfile(mf):
            raise PromotionRefused("the retained package has no oaap-app.yaml")
        with open(mf, encoding="utf-8") as f:
            m = yaml.safe_load(f)
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)

    _key, _tenant, target = promotion_target(reg, source, target_name)
    notes = []
    if target:
        if target.get("channel") != "production":
            raise PromotionRefused(
                f"'{target_name}' is not a production instance — promote into "
                "production, or pick another target")
        if target.get("app_id") != m["app"]["id"]:
            raise PromotionRefused(
                f"'{target_name}' runs app '{target.get('app_id')}', the "
                f"package is '{m['app']['id']}' — a promotion never turns one "
                "app into another")
        running, new = target.get("version", ""), m["app"]["version"]
        if not _version_gt(new, running):
            raise PromotionRefused(
                f"production runs {running}, the tested package is {new} — "
                "production takes a higher version only. Going back is a "
                f"rollback ('oaap app artifact rollback {target_name}'), "
                "which is a different and deliberate act")
        hard, confirm = envelope_review(target, m)
        # Hard refusals of RFC-0019 §3 do not apply here the way they do
        # to a token: an app id change is caught above, and the version
        # rule is production's own. What remains is the widening, and a
        # human decides it.
        notes = confirm + [h for h in hard if "version" not in h]
    elif not re.fullmatch(r"[a-z0-9][a-z0-9-]*", target_name or ""):
        raise PromotionRefused(
            "instance name: lowercase letters, digits and hyphens")
    return path, m, notes


def promote_artifact(source, target_name, confirmed=False):
    """Install the tested artifact of `source` into the production
    instance called `target_name` inside the test instance's tenant
    (RFC-0020). Returns (version, sha, notes, key).

    Nothing is fetched and nothing is uploaded: the file that installs
    is the one this node already accepted. `confirmed` is the human's
    answer to the envelope notes — without it a widening refuses, with
    it the promotion runs and the reasons are logged.
    """
    reg = load_registry()
    path, m, notes = promotion_review(reg, source, target_name)
    if notes and not confirmed:
        raise PromotionRefused(
            "this promotion would widen what the production instance may "
            "reach or who may reach it (" + "; ".join(notes)
            + ") — read it and confirm it explicitly")
    src_path = (reg["instances"][source].get("source") or {}).get("path", "")
    key, tenant, _existing = promotion_target(reg, source, target_name)
    version, sha = install_artifact(key, path, None,
                                    channel="production", path=src_path,
                                    origin=source,
                                    # The two facts nothing else knows
                                    # once the key is composed: whose
                                    # instance this is, and what the
                                    # human typed. Without them a
                                    # promotion out of a tenant landed
                                    # in the DEFAULT one and took the
                                    # key for its name -- invisible to
                                    # the very admin who had just
                                    # promoted it (oaapx01, 2026-09-03).
                                    permit={"tenant": tenant,
                                            "name": target_name})
    return version, sha, notes, key


def cmd_promote(args):
    """`oaap app promote <test-instance> [--to <name>] [--confirm]`."""
    reg = load_registry()
    # `--to` names the production instance the way its TENANT reads it,
    # like every other name a person types since RFC-0025 8.1 -- the
    # slug is the node's business, not theirs. So the default is derived
    # from the test instance's own name, never from its key: for
    # `cls-gliss-viewer-test` that is `gliss-viewer`, not
    # `cls-gliss-viewer`.
    local = instance_name(args.name, reg["instances"].get(args.name))
    target = args.to or (local[:-5] if local.endswith("-test") else "")
    if not target:
        die("name the production instance with --to "
            f"(a name ending in '-test' is shortened automatically, "
            f"'{local}' is not)")
    try:
        _path, m, notes = promotion_review(reg, args.name, target)
    except PromotionRefused as e:
        die(str(e))
    key, _tenant, existing = promotion_target(reg, args.name, target)
    print(f"Promoting {args.name} -> {key} "
          f"({'update' if existing is not None else 'new production instance'}), "
          f"app {m['app']['id']} {m['app']['version']}")
    for n in notes:
        print(f"NOTE: {n}")
    if notes and not args.confirm:
        die("this promotion widens the envelope (see NOTE above) — "
            "repeat with --confirm if that is intended")
    try:
        version, sha, _, key = promote_artifact(args.name, target,
                                                confirmed=bool(args.confirm))
    except (PromotionRefused, ArtifactRejected) as e:
        die(str(e))
    print(f"Promoted {version} to '{key}' (sha {sha[:12]}).")
    print("The previous package is retained — 'oaap app artifact rollback "
          f"{key}' is the way back.")


# --- building a rehearsal (RFC-0030 D1) --------------------------------
# The only genuinely new mechanism in RFC-0030: lift ONE instance's
# subtree out of a backup archive into a NEW, empty instance, and run
# the TEST instance's package on it.
#
# Deliberately not the restore that RFC-0029 D5 deferred. That one merges
# a tenant back INTO a running node and has to decide what happens to
# rows that changed since. Here the target is new and empty: nothing to
# merge, nothing to overwrite, no conflict to resolve. The hard half is
# absent, which is the whole reason this can exist today.


class RehearsalRefused(Exception):
    """A refusal a person can act on — same contract as PromotionRefused."""


BACKUP_DIR_DEFAULT = "/var/backups/oaap"


def backup_archive_dir():
    """Where this node's archives actually lie.

    Asked in the order of who knows best: the last run wrote one and
    knows its path; failing that the timer says where it sends them; and
    only then the built-in default. A node that backs up somewhere else
    entirely must not be told "no archive" because we looked in the
    wrong place.
    """
    last = (last_backup_run() or {}).get("archive") or ""
    if last:
        return os.path.dirname(last)
    try:
        with open(BACKUP_SCHEDULE_FILE, encoding="utf-8") as f:
            target = (json.load(f) or {}).get("target") or ""
        if target:
            return target
    except (OSError, ValueError):
        pass
    return BACKUP_DIR_DEFAULT


def newest_archive(where=""):
    """The newest complete archive in `where`, or "".

    `.part.tar` and `.tmp` are skipped by construction: they do not end
    in `.tar.gz`, which is exactly why `oaap backup create` names them
    that way. A half-written archive must not be mistakable for one, and
    least of all by this.
    """
    where = where or backup_archive_dir()
    try:
        files = [os.path.join(where, f) for f in os.listdir(where)
                 if f.startswith("oaap-backup-") and f.endswith(".tar.gz")]
    except OSError:
        return ""
    files = [f for f in files if os.path.isfile(f)]
    return max(files, key=os.path.getmtime) if files else ""


def archive_member(reg, key):
    """Where one instance's data sits INSIDE an archive.

    Derived from `instance_dir`, never spelled out a second time. The
    archive is written relative to DATA_DIR, so this is that path minus
    the data directory — and an instance from before RFC-0026 answers
    `apps/<key>` here for the same reason it does on disk.
    """
    inst = (reg.get("instances") or {}).get(key) or {}
    return os.path.relpath(instance_dir(key, inst), DATA_DIR).replace(os.sep, "/")


def archive_holds(archive, member):
    """Does the archive carry anything under `member`?

    The proof the RFC promises in passing: every rehearsal checks a
    backup, because a backup that turns out not to contain the instance
    is found HERE instead of on the night somebody actually needs it.
    """
    try:
        out = subprocess.run(["tar", "-tzf", archive], check=True, text=True,
                             capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return any(e.rstrip("/") == member or e.startswith(member + "/")
               for e in out.splitlines())


def archive_age_days(archive):
    return max(0, int((time.time() - os.path.getmtime(archive)) // 86400))


def rehearsal_code_default(local):
    """The test instance a rehearsal takes its code from, by default.

    The mirror image of `oaap app promote`, which turns `crm-test` into
    `crm`. Guessing is fine here because it is only the DEFAULT: when
    the guess is wrong the refusal names it, and `--code-from` is one
    word away.
    """
    return f"{local}-test"


def _size_phrase(kbytes):
    """A size in KiB as a person reads it -- and never as "0 MB".

    Found on the running node: a small instance was announced as
    "about 0 MB is copied". That is a small untruth about something
    that is not empty, and the whole point of this line is that the
    operator can trust the number in it.
    """
    try:
        kbytes = int(kbytes)
    except (TypeError, ValueError):
        return "an unknown amount"
    return f"{kbytes // 1024} MB" if kbytes >= 1024 else f"{kbytes} KB"


def rehearsal_review(reg, source, new_name, code_from="", archive="", days=0):
    """Everything that must hold before a rehearsal is built.

    A pure check: it reads, it computes, it writes nothing. The CLI
    calls it to print the plan, the host calls it again before it acts,
    and both get the same answer -- the same contract as
    `promotion_review`.

    Returns the plan as a dict: which archive, how old, which package,
    where the data goes, what it costs in disk space.
    """
    src = (reg.get("instances") or {}).get(source)
    if not src:
        raise RehearsalRefused(f"no instance named '{source}'")
    if is_rehearsal(src):
        raise RehearsalRefused(
            f"'{source}' is itself a rehearsal. A rehearsal is built from "
            "the PRODUCTION instance whose data is at stake.")
    if src.get("channel") != "production":
        raise RehearsalRefused(
            f"'{source}' is not a production instance. A rehearsal of test "
            "data is a test instance, and there already is one (RFC-0030).")

    tenant = resolve_tenant(src.get("tenant"))
    local_src = instance_name(source, src)
    new_name = (new_name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", new_name):
        raise RehearsalRefused("instance name: lowercase letters, digits and "
                               "hyphens")
    key = instance_key(tenant, new_name)
    found_key, found = find_instance(reg, tenant, new_name)
    if found is not None or key in (reg.get("instances") or {}):
        raise RehearsalRefused(f"an instance named '{new_name}' already exists")
    refusal = retained_data_refusal(key, tenant)
    if refusal:
        raise RehearsalRefused(refusal)

    # --- the code -------------------------------------------------------
    want = code_from or rehearsal_code_default(local_src)
    code_key, code = find_instance(reg, tenant, want)
    if code is None:
        code_key, code = want, (reg.get("instances") or {}).get(want)
    if code is None:
        raise RehearsalRefused(
            f"no test instance '{want}' in this tenant — name it with "
            f"--code-from. A rehearsal runs the code that is about to go "
            f"live, and that code lives in a test instance.")
    if code.get("channel") != "test":
        raise RehearsalRefused(
            f"'{code_key}' is not a test instance. The rehearsal runs the "
            "package that is about to be promoted, which is the one a test "
            "instance holds.")
    if code.get("app_id") != src.get("app_id"):
        raise RehearsalRefused(
            f"'{code_key}' runs app '{code.get('app_id')}' and '{source}' "
            f"runs '{src.get('app_id')}' — a rehearsal never turns one app "
            "into another.")
    stored = code.get("source") or {}
    if stored.get("kind") != "artifact":
        raise RehearsalRefused(
            f"'{code_key}' does not run from an uploaded package. A rehearsal "
            "carries the SAME BYTES that promotion would ship, and only a "
            "retained artifact can prove that (RFC-0020).")
    pkg = source_package_arg(code_key, stored)
    if not pkg or not os.path.isfile(pkg):
        raise RehearsalRefused(
            f"the retained package of '{code_key}' is gone — deploy it once "
            "more, then build the rehearsal")

    # --- the data -------------------------------------------------------
    archive = archive or newest_archive()
    if not archive:
        raise RehearsalRefused(
            "this node has no backup archive. Without a backup there is no "
            "rehearsal — the data comes from the last archive, and there is "
            "none. Run 'sudo oaap backup create' first (RFC-0029).")
    if not os.path.isfile(archive):
        raise RehearsalRefused(f"no archive at {archive}")
    member = archive_member(reg, source)
    if not archive_holds(archive, member):
        raise RehearsalRefused(
            f"the archive {os.path.basename(archive)} holds no data for "
            f"'{source}' ({member} is not in it). That is a finding about the "
            "BACKUP, not about this command — a rehearsal is also the drill "
            "that proves it. Take a fresh archive and look at why.")

    need = _dir_kbytes(instance_dir(source, src))
    free = shutil.disk_usage(DATA_DIR).free // 1024
    if need and free < need * 12 // 10:
        raise RehearsalRefused(
            f"not enough room: '{source}' holds about {need // 1024} MB and a "
            f"rehearsal is a second copy of it, while {free // 1024} MB is "
            "free on this node. Free some space first — discovering this at "
            "90 % full is discovering it too late.")

    days = int(days or REHEARSAL_DAYS)
    if days < 1 or days > 90:
        raise RehearsalRefused("a rehearsal lives between 1 and 90 days")
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "source": source, "source_local": local_src, "tenant": tenant,
        "key": key, "name": new_name,
        "code_key": code_key, "package": pkg,
        "package_path": stored.get("path", ""),
        "version": code.get("version", ""),
        "archive": archive, "archive_age_days": archive_age_days(archive),
        "archive_created": datetime.datetime.fromtimestamp(
            os.path.getmtime(archive),
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "member": member, "kbytes": need, "free_kbytes": free,
        "days": days,
        "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires": (now + datetime.timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }


def _extract_instance_subtree(archive, member, dest):
    """Lift one instance's subtree out of the archive into `dest`.

    Two things here have cost real money elsewhere and are not
    negotiable:

    * `--numeric-owner`, because that is how the archive was WRITTEN. An
      extraction that maps names instead hands the container a mount it
      can no longer write (contract guarantee 7), and nothing says so
      until the app fails to start.
    * `--strip-components`, because the paths in the archive carry the
      OLD instance's identity. Extracting them as they lie would put the
      copy where the original's data belongs -- which is the one
      outcome this whole feature must never produce.
    """
    depth = len([p for p in member.split("/") if p])
    os.makedirs(dest, exist_ok=True)
    run(["tar", "--numeric-owner", "-xpzf", archive, "-C", dest,
         f"--strip-components={depth}", member])


def _scrub_rehearsal_env(key, ident, secret_keys):
    """Take the secrets out of the copied `instance.env` (D3, spec 2.15.2).

    AFTER the extraction, never before and never by hoping: the archive
    carries the instance.env of the production instance, so the
    credentials arrive on this node whatever anyone intended. They are
    removed here, before a single container is created.

    `OAAP_APP_SECRET` goes too. The install mints a fresh one, which is
    the platform's ordinary behaviour and the safer one: an app that
    encrypted its stored data with the old value finds it unreadable in
    the rehearsal, and that is a real restore problem found early rather
    than a rehearsal failing.

    `OAAP_PLATFORM_KEY`/`OAAP_TWIN_URL` (oaap.data.twin 0.1, RFC-0031
    E1) go for a sharper reason than tidiness: the copied value is a
    live credential for the PRODUCTION tenant's twin schema, and D8 (a
    rehearsal's own copy of that schema) is not built yet. Left in
    place, a rehearsal of this app could read and write live customer
    data through the twin -- exactly what RFC-0030 exists to prevent.
    The install that follows does not reissue it for a rehearsal
    either (`_install_from_dir`), so the app simply cannot reach
    oaap.data.twin from here, honestly, until D8 exists.

    Returns the keys that were actually dropped, so the operator is told
    what to fill in rather than left to discover it from a crash loop.
    """
    platform_owned = {"OAAP_APP_SECRET", "OAAP_PLATFORM_KEY", "OAAP_TWIN_URL"}
    env = load_env(key, ident)
    dropped = sorted(k for k in list(env)
                     if k in platform_owned or k in secret_keys)
    for k in dropped:
        env.pop(k, None)
    save_env(key, env, ident)
    return [k for k in dropped if k not in platform_owned]


def create_rehearsal(plan, who="root", role="root"):
    """Build the rehearsal described by `plan`. Returns (key, dropped).

    Order matters and is the reverse of convenient: the data is in place
    and scrubbed BEFORE any container exists. An app that came up on the
    copied data with the original's credentials -- even for a second --
    would have been able to act on the world with them.
    """
    reg = load_registry()
    src = reg["instances"][plan["source"]]
    secret_keys = {c["key"] for c in (src.get("config") or [])
                   if c.get("secret")}
    ident = instance_identity(reg, plan["key"], plan["tenant"], plan["name"])
    dest = instance_dir(plan["key"], ident)
    if os.path.exists(dest) and os.listdir(dest):
        raise RehearsalRefused(
            f"{dest} already holds data — refusing to write a copy of "
            "production data over it")
    block = {"of": plan["source"], "code_from": plan["code_key"],
             "archive": plan["archive"],
             "archive_created": plan["archive_created"],
             "created": plan["created"], "expires": plan["expires"],
             "extensions": 0}
    try:
        _extract_instance_subtree(plan["archive"], plan["member"], dest)
        dropped = _scrub_rehearsal_env(plan["key"], ident, secret_keys)
        install_artifact(plan["key"], plan["package"], None,
                         channel="production", path=plan["package_path"],
                         permit={"tenant": plan["tenant"],
                                 "name": plan["name"]},
                         ident=ident, rehearsal=block)
    except BaseException:
        # A half-built rehearsal is a copy of live customer data lying on
        # the node under an instance nobody can see. Whatever went wrong,
        # that must not be what is left behind.
        shutil.rmtree(dest, ignore_errors=True)
        r = load_registry()
        if plan["key"] in r["instances"]:
            remove_instance(r, plan["key"], purge=True)
        raise
    rehearsal_view_write()
    audit_tenant("rehearsal.create", plan["tenant"], subject=plan["key"],
                 who=who, role=role,
                 detail=(f"copy of '{plan['source']}' from "
                         f"{os.path.basename(plan['archive'])} "
                         f"({plan['archive_age_days']} d old), code from "
                         f"'{plan['code_key']}' {plan['version']}, "
                         f"expires {plan['expires']}"))
    return plan["key"], dropped


def cmd_rehearse(args):
    """`oaap app rehearse <production-instance> --name <new> [...]`."""
    reg = load_registry()
    try:
        plan = rehearsal_review(reg, args.name, args.to,
                                code_from=getattr(args, "code_from", ""),
                                archive=getattr(args, "from_archive", ""),
                                days=getattr(args, "days", 0))
    except RehearsalRefused as e:
        die(str(e))
    print(f"Rehearsal '{plan['key']}' — the code of '{plan['code_key']}' "
          f"({plan['version']}) on a copy of '{plan['source']}'s data.")
    print(f"  data from  {plan['archive']}")
    # Said out loud, always. A rehearsal on a two-week-old archive is a
    # rehearsal on two-week-old data, and whoever reads the result has
    # to know which data it was.
    age = plan["archive_age_days"]
    print(f"             written {plan['archive_created']} — "
          + ("today" if age == 0 else f"{age} day(s) old"))
    if age >= 7:
        print("             NOTE: that is not recent. Anything entered since "
              "is not in this rehearsal.")
    print(f"  disk       about {_size_phrase(plan['kbytes'])} is copied; "
          f"{_size_phrase(plan['free_kbytes'])} free, "
          f"{_size_phrase(plan['free_kbytes'] - plan['kbytes'])} after")
    print(f"  expires    {plan['expires']} ({plan['days']} days) — the "
          "instance and its data are then deleted")
    print("  refuses    no own address, no public route, no app links, "
          "no copied secrets (RFC-0030 D3)")
    if not getattr(args, "yes", False):
        die("this creates a second copy of live customer data — repeat with "
            "--yes when that is what you want")
    try:
        key, dropped = create_rehearsal(plan)
    except (RehearsalRefused, ArtifactRejected) as e:
        die(str(e))
    print("")
    print(f"Rehearsal '{key}' is up.")
    if dropped:
        print("These configuration values were NOT copied and are empty — "
              "fill in what the rehearsal really needs:")
        for k in dropped:
            print(f"  {k}")
        print("An app that will not start without one is saying so on "
              "purpose (RFC-0030 D3).")
    print(f"It disappears on {plan['expires']} unless somebody extends it.")


# --- what the portal may know about rehearsals (RFC-0030 D5) -----------
#
# The portal must NOT read the tenant tree. Every customer's
# instance.env lives there, and its registry mount is `apps/` for
# exactly that reason. But the page owes an operator two numbers before
# it copies live customer data — how big the copy is and what is left
# afterwards — and both can only be measured where the data is.
#
# So the host writes a VIEW beside the registry, the same shape as
# `apps/artifacts.json` (0.1.76) and `apps/backup-schedule.json`
# (0.1.79): a file the portal reads, and an action the host checks
# again for itself, because the spool is data and not trust.
#
# It carries `written`, and the page says so. A measurement without a
# date is a number somebody will still believe next month.
REHEARSAL_VIEW = os.path.join(APPS_DIR, "rehearsal-options.json")


def rehearsal_view_write(reg=None):
    """Measure what a rehearsal would cost, where the portal can read it."""
    try:
        reg = reg if reg is not None else load_registry()
        instances = {}
        for name, inst in (reg.get("instances") or {}).items():
            if inst.get("channel") != "production" or is_rehearsal(inst):
                continue
            tenant = resolve_tenant(inst.get("tenant"))
            code = []
            for other, oi in sorted((reg.get("instances") or {}).items()):
                if (oi.get("channel") != "test"
                        or oi.get("app_id") != inst.get("app_id")
                        or resolve_tenant(oi.get("tenant")) != tenant):
                    continue
                src = oi.get("source") or {}
                pkg = source_package_arg(other, src) if src.get("kind") == "artifact" else ""
                code.append({"key": other, "name": instance_name(other, oi),
                             "version": oi.get("version", "?"),
                             # Offered but marked: a test instance without
                             # a retained package cannot prove "the same
                             # bytes", and saying why beats hiding it and
                             # letting somebody hunt for the missing entry.
                             "ready": bool(pkg and os.path.isfile(pkg))})
            instances[name] = {"kbytes": _dir_kbytes(instance_dir(name, inst)),
                               "code": code}
        archives = []
        where = backup_archive_dir()
        try:
            for fn in sorted(os.listdir(where)):
                if not (fn.startswith("oaap-backup-") and fn.endswith(".tar.gz")):
                    continue
                p = os.path.join(where, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                archives.append({"file": fn, "path": p, "bytes": st.st_size,
                                 "created": _iso(st.st_mtime)})
        except OSError:
            pass
        archives.sort(key=lambda a: a["created"], reverse=True)
        rec = {"schema": "0.1", "written": _iso_now(),
               "free_kbytes": shutil.disk_usage(DATA_DIR).free // 1024,
               "default_days": REHEARSAL_DAYS, "step_days": REHEARSAL_STEP,
               "archives": archives, "instances": instances}
        tmp = REHEARSAL_VIEW + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, REHEARSAL_VIEW)
        # Sizes and file names, no secret -- the same class of fact as
        # apps/artifacts.json, and the portal reads this mount read-only.
        os.chmod(REHEARSAL_VIEW, 0o644)
        return rec
    except OSError as e:
        print(f"WARNING: could not write {REHEARSAL_VIEW}: {e}", flush=True)
        return None


# --- the expiry (RFC-0030 D4) ------------------------------------------
# A copy of production data that expires into a directory nobody looks
# at is the worst of both worlds: not used, and still everything a
# customer has. So the expiry deletes, and the deletion is automatic.
#
# Which is exactly why the lock below is not decoration. An automatic
# deletion that can reach an ordinary instance is a foot-gun with a
# timer, and this one runs every night on a machine nobody is watching.


def rehearsals(reg=None):
    """Every rehearsal on this node, keyed as the registry keys them."""
    reg = reg if reg is not None else load_registry()
    return {k: i for k, i in (reg.get("instances") or {}).items()
            if is_rehearsal(i)}


def rehearsal_expired(inst, now=None):
    """Is this rehearsal's time up?

    False for anything that is not a rehearsal -- the lock, stated once,
    where the sweep reads it. And False for a rehearsal whose date
    cannot be read: an unreadable field is a reason to look, never a
    reason to delete somebody's data.
    """
    if not is_rehearsal(inst):
        return False
    import datetime
    stamp = (inst.get("rehearsal") or {}).get("expires") or ""
    try:
        due = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return False
    return due <= (now or datetime.datetime.now(datetime.timezone.utc))


def rehearsal_extend(key, days=REHEARSAL_STEP, who="root", role="root"):
    """Move a rehearsal's date out. Returns the new date.

    Counted and logged, both on purpose: a rehearsal extended six times
    has stopped being temporary, and the tenant's log is what makes that
    visible instead of a matter of somebody's memory.
    """
    import datetime
    reg = load_registry()
    inst = (reg.get("instances") or {}).get(key)
    if not inst:
        raise RehearsalRefused(f"no instance named '{key}'")
    if not is_rehearsal(inst):
        raise RehearsalRefused(
            f"'{key}' is not a rehearsal. An expiry date exists ONLY on a "
            "rehearsal (RFC-0030 D4) — an automatic deletion that can reach "
            "an ordinary instance is a trap with a timer.")
    if days < 1 or days > 90:
        raise RehearsalRefused("extend by between 1 and 90 days")
    block = inst["rehearsal"]
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        due = datetime.datetime.strptime(block.get("expires", ""),
                                         "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        due = now
    # From today when it has already lapsed: extending an expired
    # rehearsal from its old date would hand back something that is
    # still expired, and the next sweep would delete what somebody just
    # asked to keep.
    base = due if due > now else now
    new = (base + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    block["expires"] = new
    block["extensions"] = int(block.get("extensions") or 0) + 1
    save_registry(reg)
    audit_tenant("rehearsal.extend", resolve_tenant(inst.get("tenant")),
                 subject=key, who=who, role=role,
                 detail=f"+{days} d, now {new}, extension "
                        f"#{block['extensions']}")
    return new


def rehearsal_sweep(who="root", role="root"):
    """Remove every rehearsal whose time is up, with its data.

    Returns the list of (key, outcome). Reads the registry once per
    removal because `remove_instance` writes it -- the same shape the
    other host-side loops use.

    THE LOCK: `rehearsals()` is the only source of candidates, and
    `rehearsal_expired()` refuses anything without the block a second
    time. Two readers of the same rule, deliberately, because this
    function deletes data unattended.
    """
    out = []
    for key in sorted(rehearsals()):
        reg = load_registry()
        inst = (reg.get("instances") or {}).get(key)
        if not inst or not is_rehearsal(inst) or not rehearsal_expired(inst):
            continue
        tid = resolve_tenant(inst.get("tenant"))
        block = inst.get("rehearsal") or {}
        try:
            msg = remove_instance(reg, key, purge=True)
            result = "ok"
        except Exception as e:                       # noqa: BLE001
            msg, result = f"could not remove '{key}': {e}", "failed"
        audit_tenant("rehearsal.expire", tid, subject=key, result=result,
                     who=who, role=role,
                     detail=(f"expired {block.get('expires', '?')}, copy of "
                             f"'{block.get('of', '?')}', removed with its "
                             f"data"))
        out.append((key, msg))
    if out:
        rehearsal_view_write()
    return out


def cmd_rehearsal(args):
    """`oaap app rehearsal list|extend|sweep`."""
    if args.action == "sweep":
        done = rehearsal_sweep()
        if not done:
            print("No rehearsal has expired.")
            return
        for key, msg in done:
            print(f"{key}: {msg}")
        return

    if args.action == "extend":
        if not args.name:
            die("'oaap app rehearsal extend' needs the rehearsal's name")
        try:
            new = rehearsal_extend(args.name, days=args.days)
        except RehearsalRefused as e:
            die(str(e))
        print(f"'{args.name}' now expires {new}.")
        print("Recorded in the tenant's log — a rehearsal that keeps being "
              "extended is no longer temporary, and that has to be visible.")
        return

    found = rehearsals()
    if not found:
        print("No rehearsals on this node.")
        return
    print("Rehearsals (RFC-0030) — each holds a COPY OF PRODUCTION DATA:")
    for key, inst in sorted(found.items()):
        b = inst["rehearsal"]
        left = rehearsal_days_left(inst)
        when = ("expired" if left is not None and left < 0
                else f"{left} day(s) left" if left is not None
                else "expiry unreadable")
        print(f"  {key}  ({when}, expires {b.get('expires', '?')})")
        print(f"      data of '{b.get('of', '?')}' from "
              f"{os.path.basename(b.get('archive', '') or '?')} "
              f"(written {b.get('archive_created', '?')})")
        print(f"      code of '{b.get('code_from', '?')}' "
              f"{inst.get('version', '?')}"
              + (f", extended {b['extensions']}x" if b.get("extensions") else ""))


def _version_gt(new, old):
    """Is `new` later than `old`? Numeric where possible, else textual.

    Deliberately lenient: an app whose versions are not numbers still
    gets the "must change" guarantee, just not an ordering one.
    """
    def parts(v):
        return [int(p) if p.isdigit() else p
                for p in re.split(r"[.\-+]", str(v or "")) if p != ""]
    a, b = parts(new), parts(old)
    try:
        return a > b
    except TypeError:                      # mixed number/text — compare as text
        return str(new) > str(old)


def cmd_grant(args):
    """Show or revoke open instance creation grants (RFC-0019).

    The portal issues them and can revoke them, but an operator sitting
    at the machine has to be able to see what doors are open without a
    browser — and to shut one.
    """
    open_grants = grants_of_kind("create")
    if args.action == "list":
        if not open_grants:
            print("No open instance creation grants.")
            return
        now = _now()
        for g in open_grants:
            mins = max(0, int((g["expires"] - now) // 60))
            print(f"{g['instance']}  (single use, {mins} min left)")
        return
    # action == "revoke"
    if not args.name:
        die("name the instance whose grant should be revoked")
    if not any(g["instance"] == args.name for g in open_grants):
        die(f"no open creation grant for '{args.name}'")
    grants = {k: v for k, v in _grants_prune(load_grants()).items()
              if not (v.get("kind") == "create"
                      and v.get("instance") == args.name)}
    save_grants(grants)
    print(f"Creation grant for '{args.name}' revoked.")


def cmd_artifact(args):
    name = args.name
    reg = load_registry()
    if name not in reg["instances"]:
        die(f"no instance named '{name}'")
    files = artifact_list(name)
    if args.action == "list":
        if not files:
            print(f"No artifacts retained for '{name}' "
                  "(it was not installed from an uploaded package).")
            return
        current = ((reg["instances"][name].get("source") or {}).get("stored") or "")
        for f in files:
            mark = " <- running" if f == current else ""
            print(f"{f}{mark}")
        return
    # action == "rollback"
    if not files:
        die(f"no retained artifacts for '{name}'")
    target = args.artifact or ""
    if target:
        match = [f for f in files if f == target or f.startswith(target)]
        if not match:
            die(f"no retained artifact matching '{target}' — "
                f"'oaap app artifact list {name}' shows what is kept")
        target = match[0]
    else:
        current = ((reg["instances"][name].get("source") or {}).get("stored") or "")
        rest = [f for f in files if f != current]
        if not rest:
            die("only the running artifact is retained — nothing to roll back to")
        target = rest[0]
    print(f"Rolling '{name}' back to {target} ...")
    try:
        install_artifact(name, os.path.join(artifact_dir(name), target), None,
                         channel=reg["instances"][name]["channel"])
    except ArtifactRejected as e:
        die(str(e))
    audit_deploy({"instance": name, "ok": True, "via": "cli",
                  "message": f"rolled back to {target}"})


def _resolve_revision(source):
    if (source or {}).get("kind") != "git":
        return ""
    try:
        ref = source.get("ref") or "HEAD"
        out = run(["git", "ls-remote", source["url"], ref]).stdout.split()
        return out[0][:12] if out else ""
    except Exception:
        return ""


# --------------------------------------------------- store sources (RFC-0012)
# A source is an OBJECT, not a URL with a label: stable id, display name,
# URL, trust class, on/off, origin in plain text. The id is what
# everything else refers to — the resolution rule, the confirmation
# record, the reconcile step below. The URL is an ATTRIBUTE of a source,
# never its identity, and that distinction is the whole of finding B4: a
# list that moves must not strand every node that has it configured.

TRUST_CLASSES = ("platform", "verified", "unverified")
TRUST_RANK = {"platform": 3, "verified": 2, "unverified": 1}
TRUST_LABEL = {
    "platform": "von uns",
    "verified": "geprüft",
    "unverified": "muss bestätigt werden",
}

# What this version of the platform ships. Adding an entry here adds the
# source on every node at the next 'oaap update' — unless the operator
# removed it, which is remembered by id (RFC-0012 §4).
SHIPPED_SOURCES = [
    {
        "id": "oaap.platform",
        "name": "OAAP Plattform-Apps",
        "url": "https://raw.githubusercontent.com/MDJoerg/oaap-apps/main/oaap-store.json",
        "url_prefix": "https://raw.githubusercontent.com/MDJoerg/oaap-apps/",
        "trust": "platform",
        "origin": "MDJoerg / oaap-apps",
    },
    {
        "id": "oaap.community",
        "name": "OAAP Community-Liste",
        "url": "https://raw.githubusercontent.com/MDJoerg/oaap-store/main/oaap-store.json",
        "url_prefix": "https://raw.githubusercontent.com/MDJoerg/oaap-store/",
        # Curated by us, but the software in it is not ours. Keeping it
        # one class below 'platform' is what gives the resolution rule
        # teeth: when both lists carry the same app id, ours wins — no
        # matter which order the two happen to sit in on this node.
        "trust": "verified",
        "origin": "MDJoerg / oaap-store",
    },
]


def derived_source_id(url):
    """A stable, readable id for a source that predates RFC-0012.

    Host plus a short digest of the URL: readable enough to recognise in
    a log line, and stable across reads — which matters, because the
    migration runs in memory on every read until something writes the
    file back.
    """
    import hashlib
    import urllib.parse

    host = (urllib.parse.urlsplit(url).hostname or "unknown").lower()
    host = re.sub(r"^www\.", "", host)
    return f"{host}-{hashlib.sha256(url.encode()).hexdigest()[:6]}"


def migrate_source(entry):
    """Fill in what RFC-0012 §2 requires, for an entry that predates it.

    The URL prefix is used exactly as far as it is trustworthy: as a
    suggestion at THIS moment, written into the entry once, and never as
    a value recomputed on every lookup. Rename a repository later and
    the stored class stays what the operator last saw.

    Returns (source, migrated).
    """
    out = dict(entry)
    url = str(out.get("url") or "").strip()
    # The URL prefix may only speak for an entry that actually predates
    # RFC-0012 — one without an id or without a trust class. An entry
    # that already carries both was written by this format and is taken
    # at its word. Without this guard, every source an operator adds
    # from the same repository (a pinned commit, a second list) would be
    # mistaken for one the installation shipped: displayed as
    # "mitgeliefert", and its removal remembered forever in
    # removed_shipped.
    legacy = (not str(out.get("id") or "").strip()
              or out.get("trust") not in TRUST_CLASSES)
    known = next((k for k in SHIPPED_SOURCES
                  if url.startswith(k["url_prefix"])), None) if legacy else None
    before = dict(out)
    if not str(out.get("id") or "").strip():
        out["id"] = known["id"] if known else derived_source_id(url)
    if out.get("trust") not in TRUST_CLASSES:
        out["trust"] = known["trust"] if known else "unverified"
        if not known:
            # Everything we cannot recognise starts as unverified and is
            # marked for a look — never silently trusted.
            out["review"] = True
    if not isinstance(out.get("enabled"), bool):
        out["enabled"] = True
    if not str(out.get("name") or "").strip():
        out["name"] = (known or {}).get("name") or out["id"]
    if known and not str(out.get("origin") or "").strip():
        out["origin"] = known["origin"]
    if known and "shipped" not in out:
        out["shipped"] = True
        # There was no way to edit a source URL before this version —
        # add-source and remove-source were the only tools — so the
        # stored URL is what we shipped. Recording it as such lets the
        # reconcile step below carry it along when the list moves.
        out["shipped_url"] = url
    return out, out != before


class SourcesUnreadable(Exception):
    """The source list is there but this process may not read it."""


def load_sources():
    """Store sources in the object form of RFC-0012 §2.

    Old `{url, name}` entries are migrated **in memory on every read**,
    so a node resolves correctly the moment this version lands — before
    anyone runs an update. The migration reaches disk the next time
    something writes the file.

    Returns (sources, removed_shipped, migrated). Raises
    SourcesUnreadable when the file EXISTS but this process may not read
    it -- on a real node that means "not root", because the file is
    0600. Reporting that as "no sources configured" is the same mistake
    `tenant check` made with the user store (0.1.51): a reader that
    cannot see its subject must say so, not pass.
    """
    try:
        with open(STORE_SOURCES, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except PermissionError:
        raise SourcesUnreadable(STORE_SOURCES)
    except (OSError, ValueError):
        data = {}
    sources, migrated, seen = [], False, set()
    for entry in (data.get("sources") or []):
        if not isinstance(entry, dict) or not str(entry.get("url") or "").strip():
            migrated = True          # unusable line, dropped on next write
            continue
        src, changed = migrate_source(entry)
        if src["id"] in seen:        # two entries, one id — keep the first
            migrated = True
            continue
        seen.add(src["id"])
        sources.append(src)
        migrated = migrated or changed
    removed = sorted({str(x) for x in (data.get("removed_shipped") or [])})
    return sources, removed, migrated


def save_sources(sources, removed):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = STORE_SOURCES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sources": sources, "removed_shipped": sorted(set(removed))},
                  f, indent=2, ensure_ascii=False)
    os.replace(tmp, STORE_SOURCES)


def reconcile_shipped_sources():
    """Bring shipped sources in line with what this version ships (§4).

    Called by 'oaap update'. Returns human-readable lines for the update
    transcript — an operator should be TOLD that resolution changed on
    this node, not discover it later.
    """
    sources, removed, changed = load_sources()
    lines = []
    if changed:
        lines.append(
            "Store sources migrated to the object form (RFC-0012): they now "
            "carry a stable id and a trust class, and an app listed by "
            "several sources is resolved by trust instead of by the order "
            "the sources happen to sit in.")
    by_id = {s["id"]: s for s in sources}
    for shipped in SHIPPED_SOURCES:
        sid = shipped["id"]
        cur = by_id.get(sid)
        if cur is None:
            if sid in removed:
                continue        # a removal is remembered, never undone
            sources.append({
                "id": sid, "name": shipped["name"], "url": shipped["url"],
                "trust": shipped["trust"], "enabled": True,
                "origin": shipped["origin"], "shipped": True,
                "shipped_url": shipped["url"],
            })
            changed = True
            lines.append(f"Store source added: {shipped['name']} "
                         f"({sid}, {TRUST_LABEL[shipped['trust']]}).")
            continue
        cur["shipped"] = True
        url, was = str(cur.get("url") or ""), str(cur.get("shipped_url") or "")
        if url == shipped["url"]:
            if was != shipped["url"]:
                cur["shipped_url"] = shipped["url"]
                changed = True
        elif was and url == was:
            cur["url"], cur["shipped_url"] = shipped["url"], shipped["url"]
            changed = True
            lines.append(f"Store source '{sid}' moved to {shipped['url']} "
                         f"(was {url}).")
        else:
            # Differs from what we shipped: the operator edited it.
            # Leave it alone and say so — silently overwriting an
            # operator's URL is exactly the surprise B4 argues against.
            lines.append(f"Store source '{sid}' points at {url}, we now ship "
                         f"{shipped['url']}. Left unchanged because it was "
                         "edited on this node — adjust it yourself if you "
                         "want ours.")
    if changed:
        save_sources(sources, removed)
    return lines


def fetch_store_list(url, timeout=5):
    """Read one store list. Returns the parsed document or None."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _store_lookup(app_id, source_id="", prefer=""):
    """Resolve an app id against the CONFIGURED store sources (spec 2.6).

    The spool request names an app id and at most a source id — what
    gets installed is decided here, on the host, and only from sources
    the server_admin configured. A request can pick among them; it can
    never introduce one.

    Resolution follows RFC-0012 §3: the highest trust class wins, and
    only within one class does configured order decide. The old rule
    (first configured source wins) turned a foreign list into a takeover
    path — claim a known id, sit above the real list, collect the
    one-click install.

    `prefer` is the source an existing instance was installed from: it
    wins over the trust rule as long as that source still lists the app,
    so a redeploy cannot be silently re-pointed at a different list.

    Returns (package_dict, version, source) — source is the full source
    object, or (None, "", None) when nothing matches.
    """
    sources = [s for s in load_sources()[0] if s.get("enabled", True)]
    if source_id:
        sources = [s for s in sources if s["id"] == source_id]
    hits = []
    for pos, src in enumerate(sources):
        data = fetch_store_list(src["url"])
        if not data:
            continue
        for a in data.get("apps", []):
            pkg = a.get("package") or {}
            if a.get("id") == app_id and pkg.get("git"):
                hits.append((src, a, pos))
                break
    if not hits:
        return None, "", None
    hits.sort(key=lambda h: (h[0]["id"] != prefer,
                             -TRUST_RANK.get(h[0].get("trust"), 0),
                             h[2]))
    src, entry, _ = hits[0]
    pkg = entry["package"]
    return ({"kind": "git", "url": pkg["git"],
             "path": pkg.get("path", ""),
             "ref": pkg.get("ref", "")},
            entry.get("version", ""), src)


# Which worker actions are state changes worth a line in a tenant's
# audit log, and what to call them there (spec 1.7). Deliberately not
# "everything": reading is not an event, and a log nobody can read
# through protects nobody.
TENANT_AUDITED = {
    "install": "instance.install",
    "create": "instance.create",
    "redeploy": "instance.deploy",
    "artifact": "instance.deploy",
    "rollback": "instance.rollback",
    "artifact-remove": "instance.artifact",
    # A read, not a change -- and in the log for exactly that reason:
    # the operator may reach everything (RFC-0022 D5), so the tenant
    # has to be able to see when a copy of their app package left the
    # node. That the same operator could also take it by ssh is a
    # reason this log cannot be complete, not a reason to leave out the
    # line that can be written.
    "artifact-export": "instance.export",
    "promote": "instance.promote",
    "remove": "instance.remove",
    "rename": "instance.rename",
    "token": "token.change",
    "grant": "permit.change",
    "visibility": "instance.visibility",
    "endpoint": "instance.endpoint",
    "address": "instance.address",
    "config": "instance.config",
    # Filed in the tenant that was just born, not in the operator's --
    # the first line of its log is the record of its own creation, and
    # its administrator has to be able to read it (RFC-0022 §6).
    "tenant": "tenant.create",
}


def reap_stale_claims(results):
    """Answer requests whose worker died mid-flight (RFC-0024 §5).

    A claim older than the time limit belongs to a run that is gone: the
    limit is enforced on every command the worker issues, so a live run
    cannot outlast it. Without this, such a request would sit in
    `claims/` forever — the caller polling its id would be told "running"
    for all time, and the health page would report a queue that never
    drains. A recorded failure is the honest answer.
    """
    cutoff = time.time() - DEPLOY_MAX_SECONDS - 60
    try:
        claims = os.listdir(SPOOL_CLAIMS)
    except OSError:
        return
    for fn in claims:
        p = os.path.join(SPOOL_CLAIMS, fn)
        try:
            if os.path.getmtime(p) > cutoff:
                continue
            with open(p, encoding="utf-8") as f:
                req = json.load(f)
        except (OSError, ValueError):
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        rid = req.get("id", "") or fn[:-5]
        record = {"instance": req.get("instance", "") or "(dieser Knoten)",
                  "ok": False, "id": rid, "message": TIMED_OUT,
                  "revision": "", "version": "", "via": "deploy worker"}
        audit_deploy(record)
        res_tmp = os.path.join(results, f"{rid}.tmp")
        with open(res_tmp, "w", encoding="utf-8") as f:
            json.dump({"ok": False, "message": TIMED_OUT, "revision": "",
                       "version": "", "id": rid}, f)
        os.replace(res_tmp, os.path.join(results, f"{rid}.json"))
        os.remove(p)
        # the package that came with it has no owner left either
        try:
            os.remove(os.path.join(SPOOL_DIR, "uploads", f"{rid}.zip"))
        except OSError:
            pass
        print(f"deploy {req.get('instance', '')}: FAILED — {TIMED_OUT}")


def cmd_process_deploys(_args):
    """Run queued deploy requests (invoked by the oaap-deployd path unit)."""
    global DEADLINE
    import argparse as _argparse
    import contextlib
    import io

    queue = os.path.join(SPOOL_DIR, "queue")
    results = os.path.join(SPOOL_DIR, "results")
    os.makedirs(queue, exist_ok=True)
    os.makedirs(results, exist_ok=True)
    os.makedirs(SPOOL_CLAIMS, exist_ok=True)
    os.chmod(SPOOL_CLAIMS, 0o700)   # a request may carry configuration values
    # prune stale result files (the requester picks them up within seconds)
    now = time.time()
    for f in os.listdir(results):
        p = os.path.join(results, f)
        if now - os.path.getmtime(p) > 3600:
            os.remove(p)
    reap_stale_claims(results)

    handled = 0
    for req_file in sorted(os.listdir(queue)):
        req_path = os.path.join(queue, req_file)
        # Claim it FIRST, by moving it out of the queue in one atomic
        # step (RFC-0024 §5). Three things follow from that: the portal
        # can offer "Abbrechen" for anything still in the queue without
        # racing us -- whoever moves the file first wins and the other
        # side finds it gone; anybody can see that a request is running
        # and since when; and a worker that dies leaves a claim the next
        # run can answer instead of a request nobody ever hears about.
        claim_path = os.path.join(SPOOL_CLAIMS, req_file)
        try:
            os.replace(req_path, claim_path)
        except OSError:
            continue            # withdrawn, or another worker took it
        # A rename keeps the old timestamp, which would date the claim
        # to when the request was QUEUED. Then a request that waited
        # behind a long build would look overdue the moment it started.
        # The claim is stamped now, so "läuft seit" and the time limit
        # both count actual work.
        os.utime(claim_path, None)
        req_path = claim_path
        try:
            with open(req_path, encoding="utf-8") as f:
                req = json.load(f)
        except (OSError, ValueError):
            os.remove(req_path)
            continue
        name = req.get("instance", "")
        rid = req.get("id", "")
        action = req.get("action", "redeploy")
        DEADLINE = time.time() + DEPLOY_MAX_SECONDS
        reg = load_registry()
        inst = reg["instances"].get(name)
        tokens = load_tokens()
        ok, msg, revision = False, "", ""
        retry = False        # rollback onto the running package

        # Who queued this, and which tenant they may act in (spec 2.3
        # rule 3). Derived HERE, from identity's own user store, never
        # from the request -- the spool is data, not trust, the same
        # rule the store install path already follows. An unauthenticated
        # request (the deploy hook, which carries a token and no user)
        # names nobody and is left to its own checks.
        actor = str(req.get("by") or "")
        act_tenant, act_role, _act_err = (acting_tenant(actor) if actor
                                          else (None, "", ""))

        # The two actions that bring an instance into being name it the
        # way the CALLER thinks of it -- inside their tenant -- because
        # there is no key yet to name (RFC-0025 8.1). Everything else
        # names the key, which is what the portal links and what every
        # other record on this node is filed under. Resolved once, here,
        # so no branch below has to remember which kind of name it got.
        local_name = name
        if action in ("create", "install") and inst is None:
            found_key, found = find_instance(reg, act_tenant, name)
            if found is not None:
                name, inst = found_key, found
            else:
                name = instance_key(act_tenant, name)
                inst = reg["instances"].get(name)
        elif action == "promote":
            # A promotion names its target the way the tenant reads it,
            # ALWAYS -- there is no key to name, because the production
            # instance usually does not exist yet, and the tenant is the
            # one the test instance belongs to (never the actor's, so a
            # server_admin promoting a customer's test instance does not
            # move it into their own tenant).
            #
            # Without this the target was taken for a key: the record
            # fell back to the DEFAULT tenant and took the key for its
            # name, so the production instance was invisible to the
            # admin who had just made it (oaapx01, 2026-09-03 -- the
            # same shape as the artifact path fixed in 0.1.66).
            name, _prom_tenant, inst = promotion_target(
                reg, str(req.get("from") or ""), name)
        audit_tenant_id = None
        cross_tenant = (inst is not None and act_role == "tenant_admin"
                        and resolve_tenant(inst.get("tenant")) != act_tenant)
        if action == "promote" and act_role == "tenant_admin":
            # The SOURCE is re-checked here too, because the spool is
            # data and not trust: it decides which tenant the production
            # instance is created in, so a request naming somebody
            # else's test instance must not get that far.
            src_tenant = resolve_tenant(
                (reg["instances"].get(str(req.get("from") or "")) or {})
                .get("tenant"))
            cross_tenant = cross_tenant or src_tenant != act_tenant

        def run_install(src, channel):
            ns = _argparse.Namespace(
                package=source_package_arg(name, src), path=src.get("path", ""),
                ref=src.get("ref", ""),
                # The tenant-local name, not the key: cmd_install
                # composes the key itself, from the same tenant, so both
                # sides cannot drift (RFC-0025 8.1).
                name=local_name, channel=channel,
                store_source=src.get("store_source", ""),
                # Only consulted when the instance is NEW: a redeploy
                # keeps whatever the instance already says, so this can
                # never move one between tenants.
                tenant=act_tenant or "")
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    cmd_install(ns)
                return True, "deployed"
            except SystemExit:
                out = buf.getvalue().strip()
                return False, (out.splitlines()[-1] if out else "install failed")
            except subprocess.CalledProcessError as e:
                err = (e.stderr or "").strip()
                return False, (err.splitlines()[-1] if err else str(e))
            except Exception as e:  # a broken deploy must never kill the worker
                return False, str(e)

        # Re-validate on the host — the spool is data, not trust.
        store_src = None
        if cross_tenant:
            msg = cross_tenant_refusal(action, name)
        elif action == "install":
            # One-click store install (spec 2.6): the request names an
            # app id and at most a source id; what gets installed is
            # decided by resolving it against the CONFIGURED store
            # sources, here on the host. A request may pick among the
            # sources the server_admin chose; it can never add one.
            #
            # Resolved against `local_name`, never `name`: since 0.1.58
            # `name` is the node-wide KEY (`<slug>-<app>` outside the
            # default tenant), and no store list has ever heard of a
            # key. The app id is what the caller asked for, which is
            # exactly what `local_name` still holds (RFC-0025 §8.1).
            src, _listed_version, store_src = _store_lookup(
                local_name, req.get("source_id", ""),
                prefer=((inst or {}).get("source") or {}).get("store_source", ""))
            if not src:
                msg = "app is not listed in any configured store source"
            elif (store_src["trust"] == "unverified"
                  and req.get("confirm_source") != store_src["id"]):
                # A brake against inattention, not a security boundary —
                # what protects against a compromised portal is still
                # 2.6's rule that only configured sources resolve at all.
                msg = (f"'{store_src['name']}' is an unverified source — "
                       "installing from it has to be confirmed explicitly "
                       "(RFC-0012 §3)")
            else:
                revision = _resolve_revision(src)
                channel = inst["channel"] if inst else "production"
                src["store_source"] = store_src["id"]
                ok, msg = run_install(src, channel)
                if ok:
                    msg = f"installed from store source '{store_src['id']}'"
        elif action == "create":
            # New instance from the portal (RFC-0011). Only on a node
            # that says it is a workbench, and only on the test channel:
            # production installs keep going through the store's
            # one-click path (2.6), where the host resolves the app id
            # itself and no request can name a source.
            src = None
            if not has_profile("dev"):
                msg = ("this node has no profile 'dev' — creating instances "
                       "from the portal is a development act (RFC-0011)")
            elif inst:
                msg = f"an instance named '{local_name}' already exists"
            elif not re.fullmatch(r"[a-z0-9][a-z0-9-]*", local_name):
                msg = "instance name: lowercase letters, digits and hyphens"
            elif req.get("from") == "store":
                # Same resolution as the one-click install: the request
                # names an app id, the host decides where it comes from.
                src, _listed, store_src = _store_lookup(
                    req.get("app_id", ""), req.get("source_id", ""))
                if not src:
                    msg = "app is not listed in any configured store source"
                elif (store_src["trust"] == "unverified"
                      and req.get("confirm_source") != store_src["id"]):
                    src = None
                    msg = (f"'{store_src['name']}' is an unverified source — "
                           "installing from it has to be confirmed explicitly "
                           "(RFC-0012 §3)")
                else:
                    src["store_source"] = store_src["id"]
            elif req.get("from") == "artifact":
                # RFC-0019: the first instance from an uploaded package.
                # No source is fetched and no credential is needed —
                # which is the whole point for a private repository.
                up = os.path.join(SPOOL_DIR, "uploads", f"{rid}.zip")
                try:
                    if not os.path.isfile(up):
                        msg = "the upload did not arrive"
                    else:
                        version, sha = install_artifact(
                            name, up, None, channel="test",
                            path=(req.get("path") or "").strip(),
                            # There is no stored permit on this path --
                            # the operator IS the permit (RFC-0011):
                            # they are creating the instance right now,
                            # in the tenant their own record names. The
                            # two facts install_artifact takes from a
                            # permit are exactly the two nothing else
                            # knows once the key is composed: whose
                            # instance this is, and what the human
                            # typed. Without them a package uploaded
                            # inside a tenant landed in the DEFAULT one,
                            # keyed `<slug>-<name>` but owned by
                            # nobody's tenant -- invisible to the very
                            # admin who had just created it.
                            permit={"tenant": act_tenant,
                                    "name": local_name})
                        revision = sha[:12]
                        ok, msg = True, f"test instance created from artifact ({version})"
                except ArtifactRejected as e:
                    msg = str(e)
                except SystemExit as e:
                    msg = f"install refused: {e}"
                except subprocess.CalledProcessError as e:
                    err = (e.stderr or "").strip()
                    msg = err.splitlines()[-1] if err else str(e)
                except Exception as e:
                    msg = str(e)
                finally:
                    try:
                        os.remove(up)
                    except OSError:
                        pass
            else:
                url = (req.get("url") or "").strip()
                # No local paths, no plain http: a local path would let
                # the portal build an image from anything on this
                # machine, which is a different power than "install from
                # a repository the operator named".
                if not re.match(r"^(https://|git@)", url):
                    msg = "package source must be an https:// or git@ Git URL"
                else:
                    src = {"kind": "git", "url": url,
                           "path": (req.get("path") or "").strip(),
                           "ref": (req.get("ref") or "").strip()}
            if src:
                revision = _resolve_revision(src)
                ok, msg = run_install(src, "test")
                if ok:
                    msg = "test instance created"
        elif action == "announce":
            # Phase 1+2 of RFC-0019. The portal has already checked the
            # deploy token; it mints the upload token and sends only its
            # digest, exactly as it does for deploy tokens — the secret
            # never reaches this file, and the spool stays data, not trust.
            try:
                confirmed = False
                pend = (load_grants().get(f"pending:{name}") or {}).get("payload") or {}
                announced_sha = hashlib.sha256(
                    (req.get("manifest") or "").encode()).hexdigest()
                if (req.get("confirmed_sha")
                        and hmac.compare_digest(req["confirmed_sha"], announced_sha)):
                    confirmed = True
                elif pend.get("confirmed") and hmac.compare_digest(
                        pend.get("manifest_sha", ""), announced_sha):
                    confirmed = True
                version = announce_artifact(
                    name, req.get("manifest") or "",
                    req.get("artifact_sha256") or "",
                    int(req.get("artifact_bytes") or 0),
                    req.get("digest") or "", confirmed=confirmed,
                    create_digest=str(req.get("create_digest") or ""))
                ok, msg = True, f"announced {version}"
            except ArtifactRejected as e:
                msg = str(e)
            except (TypeError, ValueError) as e:
                msg = f"malformed announcement: {e}"
        elif action == "artifact":
            # Phase 3. The upload itself is a file the portal wrote next
            # to the request; it is verified against the grant BEFORE a
            # single entry is unpacked.
            up = os.path.join(SPOOL_DIR, "uploads", f"{rid}.zip")
            grant = grant_spend("upload", name, req.get("digest") or "")
            # An upload that CREATES the instance carries the operator's
            # single-use creation grant with it (RFC-0019, Studio
            # section). It is spent here, not at the announcement: only
            # now does an instance actually come into being.
            creating = bool(grant and grant.get("create"))
            try:
                if grant is None:
                    msg = ("no valid upload grant — announce the version "
                           "first, and upload within 15 minutes")
                elif creating and inst:
                    msg = f"an instance named '{name}' already exists"
                elif creating and not has_profile("dev"):
                    msg = ("this node has no profile 'dev' — creating "
                           "instances is a development act (RFC-0011)")
                elif creating and (create_permit := grant_spend(
                        "create", name,
                        grant.get("create_digest") or "")) is None:
                    msg = ("the creation grant is spent or expired — have an "
                           "administrator issue a new one in the portal")
                elif not creating and not inst:
                    msg = "unknown instance"
                elif not creating and inst.get("channel") != "test":
                    msg = "not a test instance"
                elif not os.path.isfile(up):
                    msg = "the upload did not arrive"
                else:
                    version, sha = install_artifact(
                        name, up, grant, channel="test",
                        path=req.get("path") or "",
                        permit=create_permit if creating else None)
                    revision = sha[:12]
                    ok = True
                    msg = (f"test instance created from uploaded artifact ({version})"
                           if creating else
                           f"deployed {version} from uploaded artifact")
            except ArtifactRejected as e:
                msg = str(e)
            except subprocess.CalledProcessError as e:
                err = (e.stderr or "").strip()
                msg = err.splitlines()[-1] if err else str(e)
            except SystemExit as e:
                msg = f"install refused: {e}"
            except Exception as e:
                msg = str(e)
            finally:
                try:
                    os.remove(up)
                except OSError:
                    pass
        elif action == "rollback":
            # Reinstalling a retained package. Nothing new is admitted
            # here — only something this node already accepted once.
            #
            # Rolling forward onto the package already in service is the
            # same act with a different intention: "Erneut ausrollen"
            # (RFC-0024 §4). It deliberately reuses this path, checks
            # included — but it is NOT called a rollback, because the log
            # has to preserve the difference between going back and
            # trying again.
            want = str(req.get("artifact") or "")
            path = os.path.join(artifact_dir(name), want)
            retry = (want == ((inst or {}).get("source") or {}).get("stored"))
            if not inst:
                msg = "unknown instance"
            elif want not in artifact_list(name):
                msg = "no such retained artifact"
            else:
                try:
                    version, sha = install_artifact(
                        name, path, None, channel=inst.get("channel", "test"),
                        path=(inst.get("source") or {}).get("path", ""))
                    revision = sha[:12]
                    ok = True
                    msg = (f"rolled out {want} again ({version})" if retry
                           else f"rolled back to {want} ({version})")
                except ArtifactRejected as e:
                    msg = str(e)
                except SystemExit as e:
                    msg = f"install refused: {e}"
                except Exception as e:
                    msg = str(e)
        elif action == "artifact-remove":
            # Deleting one retained package (RFC-0024 §6). Applied here
            # because the portal's registry mount is read-only — and
            # re-checked here, because the spool is data, not trust: the
            # package in service may have changed between the page the
            # operator looked at and the click.
            if not inst:
                msg = "unknown instance"
            else:
                try:
                    msg = artifact_remove(inst, name, str(req.get("artifact") or ""))
                    ok = True
                except (ValueError, OSError) as e:
                    msg = str(e)
        elif action == "backup-schedule":
            # The nightly schedule from the portal (RFC-0029 D1).
            # server_admin's alone: this stops EVERY app on the node at
            # the chosen hour, including the apps of tenants who did not
            # choose it. Re-checked here, not only at the button -- the
            # spool is data, not trust.
            #
            # Note what is NOT here: the target directory. Where a copy
            # of every secret on this machine is written stays a
            # decision made at the machine (RFC-0029 D1).
            if actor and act_role != "server_admin":
                msg = ("changing the backup schedule stops every app on this "
                       "node and therefore requires server_admin")
            else:
                try:
                    op = req.get("op", "set")
                    msg = backup_schedule_set(
                        str(req.get("at") or ""),
                        req.get("keep"),
                        True if op == "on" else False if op == "off" else None)
                    ok = True
                except (ValueError, OSError) as e:
                    msg = str(e)
        elif action == "artifact-export":
            # Authorising a DOWNLOAD -- the portal streams the file, but
            # only after this answer (spec oaap.apps.runtime 2.14).
            #
            # A read that crosses the tenant boundary is decided where
            # the registry is authoritative, and the audit line is
            # written by the same pass that says yes. Deciding it in the
            # portal would put the record and the decision in two
            # places, and the spool is data, not trust.
            if not inst:
                msg = "unknown instance"
            elif actor and act_role != "server_admin":
                # The one rule that is a decision rather than a
                # mechanism: for a server_admin this button grants
                # nothing new -- the same file is one scp away -- while
                # for a tenant_admin it would be a new power, taking
                # away software a supplier put into their tenant. That
                # is a question about ownership, and it is left open
                # rather than answered by a button.
                msg = ("downloading a package requires server_admin — "
                       "whether a customer may take away software that "
                       "was installed into their tenant is a question of "
                       "ownership, not one this node decides with a button")
            else:
                try:
                    msg = artifact_export(inst, name,
                                          str(req.get("artifact") or ""), rid)
                    ok = True
                except (ValueError, OSError) as e:
                    msg = str(e)
        elif action == "promote":
            # Ship the tested artifact to production (RFC-0020). The
            # request names the SOURCE instance; `name` is by now the
            # target's KEY, because everything downstream (log, result,
            # registry) is keyed on the instance that changes -- while
            # promote_artifact gets the tenant-local name the request
            # carried, so it composes the same key from the same tenant
            # and the two sides cannot drift.
            #
            # Every rule is re-checked here even though the portal
            # already showed them: the spool is data, not trust — and
            # between showing and clicking, a deployment may have
            # changed what the test instance runs.
            try:
                version, sha, notes, _key = promote_artifact(
                    str(req.get("from") or ""), local_name,
                    confirmed=bool(req.get("confirmed")))
                revision = sha[:12]
                ok = True
                msg = (f"promoted {version} from '{req.get('from')}'"
                       + (" (envelope widened: " + "; ".join(notes) + ")"
                          if notes else ""))
            except (PromotionRefused, ArtifactRejected) as e:
                msg = str(e)
            except SystemExit as e:
                msg = f"install refused: {e}"
            except subprocess.CalledProcessError as e:
                err = (e.stderr or "").strip()
                msg = err.splitlines()[-1] if err else str(e)
            except Exception as e:
                msg = str(e)
        elif action == "grant":
            # Instance creation grant (RFC-0019, Studio section): the
            # one privileged thing Studio can do that no deploy token
            # covers, because before the instance exists there is no
            # token. server_admin issues it in the portal for ONE name;
            # Studio spends it for ONE creation and holds nothing
            # afterwards.
            #
            # The portal mints the secret and sends only its digest,
            # exactly as for deploy tokens — and every gate is re-checked
            # here, because the spool is data, not trust.
            op = req.get("op", "create")
            if op == "revoke":
                # Same key the issue path composed, or a revocation
                # would quietly find nothing (RFC-0025 8.1).
                if act_tenant is not None and not inst:
                    name = instance_key(act_tenant, local_name)
                before = len(load_grants())
                grants = {k: v for k, v in _grants_prune(load_grants()).items()
                          if not (v.get("kind") == "create"
                                  and v.get("instance") == name)}
                save_grants(grants)
                ok = True
                msg = ("creation grant revoked" if len(grants) < before
                       else "no creation grant was open for this instance")
            elif not has_profile("dev"):
                msg = ("this node has no profile 'dev' — creating instances "
                       "is a development act (RFC-0011)")
            elif inst:
                msg = f"an instance named '{local_name}' already exists"
            elif not re.fullmatch(r"[a-z0-9][a-z0-9-]*", local_name or ""):
                msg = "instance name: lowercase letters, digits and hyphens"
            elif not re.fullmatch(r"[0-9a-f]{64}", req.get("digest", "")):
                msg = "malformed grant digest"
            else:
                # The permit is the ONE record that must store its
                # tenant: it is issued before the instance exists, so
                # there is nothing to derive it from (oaap.core.tenant
                # 1.4) -- and therefore the one place a human chooses.
                # The choice is bounded by who is choosing: a
                # server_admin may name any tenant, anyone else gets
                # their own and nothing else.
                permit_tenant, permit_err = act_tenant, _act_err
                if actor and act_role == "server_admin" and req.get("tenant"):
                    permit_tenant, _r, permit_err = acting_tenant(
                        actor, str(req.get("tenant")))
                if not permit_tenant and not actor:
                    permit_tenant = ensure_default_tenant()
                audit_tenant_id = permit_tenant
                if not permit_tenant:
                    # No permit at all rather than one in the operator's
                    # tenant: a permit is a licence to create an instance
                    # SOMEWHERE, and guessing where is the one mistake
                    # this record exists to make impossible.
                    msg = permit_err or "no tenant to issue this permit in"
                else:
                    # The permit fixes the KEY before the instance
                    # exists -- that is what makes the deploy address it
                    # names stable and unambiguous (RFC-0025 8.2). It
                    # carries the tenant-local name too, because after
                    # this nothing else knows what the human typed.
                    key = instance_key(permit_tenant, local_name)
                    if key in reg["instances"]:
                        msg = f"an instance named '{local_name}' already exists"
                    else:
                        name = key
                        grant_create("create", key, req["digest"],
                                     {"channel": "test",
                                      "by": req.get("by", "?"),
                                      "tenant": permit_tenant,
                                      "name": local_name},
                                     ttl=CREATE_GRANT_TTL)
                        ok = True
                        label = tenant_label(permit_tenant)
                        msg = (f"creation grant issued for '{local_name}', "
                               f"single use, {CREATE_GRANT_TTL // 60} minutes"
                               + ("" if single_tenant()
                                  else f", tenant '{label}', deploy under "
                                       f"'{key}'"))
        elif action == "rename":
            # RFC-0026 3.3. The tenant boundary is already handled: a
            # cross-tenant request never reaches here (see cross_tenant
            # above), and the new name is checked within THIS instance's
            # tenant, so one customer's choice of words cannot block
            # another's.
            new_name = str(req.get("new") or "").strip().lower()
            key, err = rename_check(reg, name, new_name)
            if err:
                msg = err
            else:
                try:
                    name = rename_instance(reg, key, new_name,
                                           RENAME_GRACE_DAYS,
                                           who=req.get("by", "root"))
                    ok = True
                    msg = (f"renamed to '{new_name}'; the old name keeps "
                           f"answering for {RENAME_GRACE_DAYS} days")
                except Exception as e:   # noqa: BLE001 - reported, not fatal
                    msg = f"rename failed: {e}"
        elif action == "envelope":
            # An administrator confirms one specific pending announcement
            # in the portal (RFC-0019 decision 5). Bound to the manifest
            # that was announced — never "the next deployment, whatever
            # that turns out to be".
            grants = load_grants()
            entry = grants.get(f"pending:{name}")
            if not entry:
                msg = "nothing is waiting for confirmation for this instance"
            elif req.get("op") == "reject":
                del grants[f"pending:{name}"]
                save_grants(grants)
                ok, msg = True, "pending deployment rejected"
            elif not hmac.compare_digest(
                    str(req.get("manifest_sha") or ""),
                    entry["payload"].get("manifest_sha", "")):
                msg = "the confirmation does not match the pending announcement"
            else:
                entry["payload"]["confirmed"] = True
                save_grants(grants)
                ok, msg = True, "deployment confirmed — the client may announce again"
        elif action == "node":
            # Node profiles (RFC-0011) are a CLI matter — with exactly
            # one exception: the first-run wizard, which asks what the
            # node is for. That request is authorised by the setup
            # token and only while no admin exists yet, so it cannot
            # become a way for a portal to promote its own node later.
            import hmac as _hmac
            env_token = ""
            try:
                with open(os.path.join(APP_DIR, ".env"), encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("SETUP_TOKEN="):
                            env_token = line.strip().split("=", 1)[1]
            except OSError:
                pass
            state_file = os.path.join(DATA_DIR, "data", "identity", "state.json")
            try:
                with open(state_file, encoding="utf-8") as f:
                    done = bool(json.load(f).get("setup_done"))
            except (OSError, ValueError):
                done = False
            wanted = [p for p in (req.get("profiles") or []) if p in PROFILES]
            if done:
                msg = ("setup is already complete — node profiles are changed "
                       "on the machine with 'sudo oaap node add-profile'")
            elif not env_token or not _hmac.compare_digest(
                    req.get("setup_token", ""), env_token):
                msg = "invalid setup token"
            else:
                save_profiles(wanted)
                ok = True
                msg = ("node profiles set: " + ", ".join(wanted)) if wanted \
                    else "node profiles cleared"
        elif action == "tenant":
            # Creating a tenant from the portal (oaap.core.tenant 2.2).
            #
            # Why this may leave the machine at all, in the same terms
            # the store-source branch below uses: a new tenant is empty.
            # It has no user, no instance, no permit and no address; it
            # is a reserved word plus a place to put things. What it is
            # NOT is reversible -- there is no `tenant remove`, by
            # decision -- so this stays server_admin's alone, and the
            # label rules are re-checked here because the spool is data,
            # not trust.
            #
            # `server_admin` and not `tenant_admin`: a tenant admin who
            # could make a tenant could make one and appoint themselves
            # in it, which is a two-step path out of their own boundary
            # (spec 2.3 rule 1).
            label = str(req.get("label") or "").strip().lower()
            # Named before the checks, so a REFUSAL says which label was
            # refused instead of filing an empty subject.
            name = label
            if act_role != "server_admin":
                msg = ("creating a tenant requires server_admin "
                       "(oaap.core.tenant 2.3)")
            elif err := tenant_label_error(label, "create"):
                msg = err
            else:
                had = len(load_tenants())
                tid, _t = tenant_create(label, str(req.get("title") or "").strip())
                audit_tenant_id = tid
                name = label          # what the log and the result name
                ok = True
                # The two sentences the CLI says at this moment, said
                # here too (spec 3.4): the label is public, and the
                # second tenant switches the whole capability on. A
                # portal that creates the record silently would be a
                # different act wearing the same name.
                msg = f"tenant '{label}' created. " + zone_probe(label)
                if had == 1:
                    msg += (" This node now has more than one tenant, so "
                            "tenants become visible: every caller sees their "
                            "own and no other. Nothing about the existing "
                            "tenant changes.")
        elif action == "source":
            # Store sources from the portal (RFC-0012 §7). Same reason as
            # visibility below: the portal's /apps-registry mount is
            # read-only, so the host applies it. And the same rules run
            # here as in the CLI — the spool is data, not trust, so the
            # checks cannot live only where the button is.
            #
            # Nothing here lets the portal grant ITSELF more reach than
            # the server_admin already granted: adding a source is
            # visible, reversible, and by itself installs nothing. That
            # is why this may move into the portal while setting a node
            # profile (RFC-0011) stayed on the machine.
            sources, removed, _mig = load_sources()
            op = req.get("op", "")
            src = find_source(sources, str(req.get("source_id") or ""))
            try:
                if op == "add":
                    msg = source_add(sources, removed,
                                     str(req.get("url") or "").strip(),
                                     name=str(req.get("name") or "").strip(),
                                     trust=str(req.get("trust") or "unverified"),
                                     origin=str(req.get("origin") or "").strip())
                elif not src:
                    raise ValueError("unknown store source")
                elif op == "remove":
                    msg = source_remove(sources, removed, src)
                elif op in ("enable", "disable"):
                    msg = source_enable(src, op == "enable")
                elif op == "rename":
                    msg = source_rename(src, req.get("name"))
                elif op == "trust":
                    msg = source_trust(src, req.get("trust"))
                else:
                    raise ValueError(f"unknown source operation '{op}'")
                save_sources(sources, removed)
                ok = True
            except ValueError as e:
                msg = str(e)
        elif action == "visibility":
            # App-instance visibility groups (RFC-0007): the portal's
            # /apps-registry mount is read-only, so this — like the
            # store install above — is applied here on the host.
            if not inst:
                msg = "unknown instance"
            else:
                groups = req.get("groups") or []
                inst["visibility"] = {"groups": groups} if groups else {}
                save_registry(reg)
                write_app_caddy(name, inst)
                refresh_generated_sites()
                reload_gateway()
                ok = True
                msg = ("visibility set to groups: " + ", ".join(groups)) if groups else "visibility set to all"
        elif action == "endpoint":
            # Non-HTTP endpoint grant/deny (RFC-0015). Applied on the host
            # like visibility (read-only registry mount in the portal),
            # and the same checks run here — the spool is data, not trust.
            # The 'exposed' profile gate is re-checked here, not only at
            # the button: a queued request must not open a raw port on a
            # node the operator never marked as exposed.
            op = req.get("op", "")
            ep_name = str(req.get("endpoint") or "")
            declared = (inst.get("declared_endpoints") if inst else None) or []
            decl = next((d for d in declared if d["name"] == ep_name), None)
            if not inst:
                msg = "unknown instance"
            elif actor and act_role != "server_admin":
                # Re-checked here, not only at the button: a port on the
                # host bypasses the gateway, and with it the tenant
                # boundary the gateway enforces. That makes it a node
                # decision (RFC-0011/RFC-0015), so a tenant_admin is
                # refused even on their own instance.
                msg = ("a gateway-bypassing port is a node decision and "
                       "requires server_admin")
            elif not decl:
                msg = f"no declared endpoint '{ep_name}'"
            elif op == "allow" and not has_profile("exposed"):
                msg = ("this node has no profile 'exposed' — a gateway-"
                       "bypassing port is refused (set it on the machine)")
            else:
                current = [e for e in (inst.get("endpoints") or [])
                           if e["name"] != ep_name]
                if op == "allow":
                    fixed = bool(decl.get("fixed"))
                    target = decl["container_port"] if fixed else decl.get("wish")
                    try:
                        # A fixed-port clash raises rather than exits, so it
                        # becomes this item's failure message instead of
                        # killing the whole spool run (RFC-0017 §5.1).
                        hp = assign_endpoint_port(reg, target, fixed=fixed)
                    except EndpointPortTaken as e:
                        hp = None
                        msg = str(e)
                    if hp is not None:
                        current.append({"name": ep_name, "protocol": decl["protocol"],
                                        "container_port": decl["container_port"],
                                        "host_port": hp, "service": decl.get("service", ""),
                                        "fixed": fixed, "reason": decl.get("reason", "")})
                        inst["endpoints"] = current
                        save_registry(reg)
                        recreate_instance_containers(name, instance_services(inst),
                                                     inst.get("storage") or [],
                                                     inst["endpoints"])
                        ok = True
                        msg = (f"endpoint '{ep_name}' granted on host port {hp} "
                               f"({'+'.join(_endpoint_protos(decl['protocol']))}) — "
                               f"raw port, no gateway; forward it on your router")
                elif op == "deny":
                    inst["endpoints"] = current
                    save_registry(reg)
                    recreate_instance_containers(name, instance_services(inst),
                                                 inst.get("storage") or [],
                                                 inst["endpoints"])
                    ok = True
                    msg = f"endpoint '{ep_name}' denied; raw port closed"
                else:
                    msg = f"unknown endpoint op '{op}'"
        elif action == "link":
            # App-to-app link (RFC-0016). Like visibility, the portal's
            # registry mount is read-only, so the host applies it — and
            # the same checks run here as in the CLI, because the spool
            # is data, not trust. server_admin territory (the portal
            # route already gates it).
            op = req.get("op", "")
            target = str(req.get("target") or "")
            if not inst:
                msg = "unknown instance"
            elif target not in reg["instances"]:
                msg = f"unknown target instance '{target}'"
            elif target == name:
                msg = "an instance cannot link to itself"
            else:
                links = set(inst.get("links") or [])
                if op == "add":
                    inst["links"] = sorted(links | {target})
                    save_registry(reg)
                    setup_link_network(reg, name, target)
                    ok = True
                    msg = f"linked: {name} may reach {target}"
                elif op == "remove":
                    inst["links"] = sorted(links - {target})
                    save_registry(reg)
                    if name not in (reg["instances"][target].get("links") or []):
                        teardown_link_network(name, target)
                    ok = True
                    msg = f"link {name} -> {target} removed"
                else:
                    msg = f"unknown link op '{op}'"
        elif action == "tile":
            # Launchpad tile override (runtime spec 2.10). Registry only
            # — no gateway work, because this changes nothing about who
            # may reach the instance. The mode is re-checked here rather
            # than trusted: the spool is data, not trust.
            mode = req.get("mode", "")
            if not inst:
                msg = "unknown instance"
            elif mode not in TILE_MODES:
                msg = f"unknown tile mode '{mode}'"
            else:
                if mode == "auto":
                    inst.pop("tile", None)
                else:
                    inst["tile"] = mode
                save_registry(reg)
                ok = True
                msg = (f"tile set to {mode} ({TILE_EXPLAIN[mode]})"
                       + (" — now shown" if tile_visible(inst)
                          else " — now not shown"))
        elif action == "token":
            # Deploy token from the portal (runtime spec 2.5/2.6). The
            # portal generates the token and sends only its digest, so
            # the readable value never reaches the filesystem; the host
            # re-checks here what the CLI checks, because the spool is
            # data, not trust: test channel only, recorded source
            # required.
            op = req.get("op", "")
            if not inst:
                msg = "unknown instance"
            elif op == "revoke":
                if name in tokens:
                    del tokens[name]
                    save_tokens(tokens)
                ok, msg = True, "deploy token revoked"
            elif inst["channel"] != "test":
                msg = ("production instances never carry a deploy token "
                       "(spec 2.5)")
            elif not inst.get("source"):
                msg = ("no package source recorded — reinstall the instance "
                       "once, then issue a token")
            elif not re.fullmatch(r"[0-9a-f]{64}", req.get("digest", "")):
                msg = "malformed token digest"
            else:
                import datetime
                tokens[name] = {
                    "digest": req["digest"],
                    "created": datetime.datetime.now(datetime.timezone.utc)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                save_tokens(tokens)
                ok, msg = True, "deploy token issued"
        elif action == "remove":
            # The only destructive operation the portal can request, so
            # the host re-checks the confirmation too: the request must
            # name the instance it claims to remove. A misdirected or
            # replayed request therefore cannot take down a different
            # app than the one the operator was looking at.
            if not inst:
                msg = "unknown instance"
            elif req.get("confirm", "") != name:
                msg = "confirmation did not match the instance name"
            else:
                msg = remove_instance(reg, name, bool(req.get("purge")))
                ok = True
        elif action == "address":
            # Own public hostname(s): canonical (RFC-0009) plus aliases
            # (RFC-0018). Validation runs here, with the same function the
            # CLI uses — the portal's copy of the rules would only drift.
            op = req.get("op", "set")
            aliases = list((inst or {}).get("aliases") or [])
            if not inst:
                msg = "unknown instance"
            elif op == "remove":
                if aliases:
                    msg = ("remove the aliases first, or promote one to the "
                           "canonical name — an instance must not keep aliases "
                           "without a canonical address")
                else:
                    old = inst.pop("address", "")
                    save_registry(reg)
                    write_instance_address_caddy()
                    reload_gateway()
                    ok = True
                    msg = f"address {old} removed" if old else "no address was set"
            elif op == "alias-remove":
                host = (req.get("hostname") or "").lower().strip().rstrip(".")
                if host not in aliases:
                    msg = f"'{host}' is not an alias of '{name}'"
                else:
                    inst["aliases"] = [a for a in aliases if a != host]
                    if not inst["aliases"]:
                        inst.pop("aliases", None)
                    save_registry(reg)
                    write_instance_address_caddy()
                    reload_gateway()
                    ok, msg = True, f"alias {host} removed"
            elif op == "alias-add":
                if not inst.get("address"):
                    msg = ("set a canonical address first, then add aliases")
                else:
                    try:
                        host = check_instance_address(reg, name, inst,
                                                      req.get("hostname", ""))
                        if host == inst.get("address"):
                            msg = f"{host} is already the canonical name"
                        elif host in aliases:
                            msg = f"{host} is already an alias"
                        else:
                            inst.setdefault("aliases", []).append(host)
                            save_registry(reg)
                            write_instance_address_caddy()
                            reload_gateway()
                            ok, msg = True, f"alias {host} added"
                    except ValueError as e:
                        msg = str(e)
            else:
                try:
                    host = check_instance_address(reg, name, inst,
                                                  req.get("hostname", ""))
                    if host in aliases:
                        msg = (f"{host} is already an alias — remove it as an "
                               f"alias first to make it the canonical name")
                    else:
                        inst["address"] = host
                        save_registry(reg)
                        write_instance_address_caddy()
                        reload_gateway()
                        ok, msg = True, f"address set to {host}"
                except ValueError as e:
                    msg = str(e)
        elif action == "throttle":
            # Public-route rate brake (RFC-0010).
            if not inst:
                msg = "unknown instance"
            else:
                mode = req.get("mode", "default")
                # value None means "no override" -> platform default
                apply_it, value = True, None
                if mode == "off":
                    value = {}
                elif mode == "custom":
                    m = re.fullmatch(r"(\d+)/(\d+)", (req.get("rate") or "").strip())
                    if not m or int(m.group(1)) < 1 or int(m.group(2)) < 1:
                        apply_it = False
                        msg = "rate must look like 300/60, both parts at least 1"
                    else:
                        value = {"limit": int(m.group(1)), "window": int(m.group(2))}
                if apply_it:
                    if value is None:
                        inst.pop("throttle", None)
                    else:
                        inst["throttle"] = value
                    save_registry(reg)
                    write_app_caddy(name, inst)
                    refresh_generated_sites()
                    reload_gateway()
                    t = throttle_of(inst)
                    ok = True
                    msg = (f"throttle set to {t['limit']}/{t['window']}" if t
                           else "throttle switched off")
        elif action == "config":
            # Instance configuration (spec 2.3/2.4.3). Same reason as
            # above: the portal cannot write the registry or talk to the
            # container runtime, so the host side applies it. Values are
            # re-checked against the DECLARED keys here -- the spool is
            # data, not trust -- and never end up in the audit log.
            if not inst:
                msg = "unknown instance"
            else:
                try:
                    msg = "config " + apply_config(name, inst, req.get("values") or {})
                    ok = True
                except ValueError as e:
                    msg = str(e)
                except subprocess.CalledProcessError as e:
                    msg = (e.stderr or str(e)).strip().splitlines()[-1]
        elif action == "rehearse":
            # Build a rehearsal of THIS production instance (RFC-0030 D5).
            # The request names the production instance, so the ordinary
            # cross-tenant check above already covers who may ask -- and
            # the new instance lands in the PRODUCTION instance's tenant,
            # never the actor's, so a server_admin building a customer's
            # rehearsal does not pull their data into their own tenant.
            #
            # Every rule is re-checked here, in full, by the same
            # rehearsal_review() the portal used to draw the form: the
            # spool is data, not trust, and between showing and clicking
            # the archive may have rotated away.
            try:
                plan = rehearsal_review(
                    reg, name, str(req.get("to") or ""),
                    code_from=str(req.get("code_from") or ""),
                    days=int(req.get("days") or REHEARSAL_DAYS))
                key, dropped = create_rehearsal(
                    plan, who=actor or "portal", role=act_role or "-")
                ok = True
                msg = (f"rehearsal '{key}' built from "
                       f"{os.path.basename(plan['archive'])} "
                       f"({plan['archive_age_days']} d old), expires "
                       f"{plan['expires']}")
                if dropped:
                    msg += ("; these values were NOT copied and are empty: "
                            + ", ".join(dropped))
                name = key
            except (RehearsalRefused, ArtifactRejected, ValueError) as e:
                msg = str(e)
            except SystemExit as e:
                msg = f"install refused: {e}"
            except Exception as e:  # a failed rehearsal must not kill the worker
                msg = str(e)
            if not ok:
                # One entry per outcome, under the action it belongs to.
                # create_rehearsal writes the successful one itself --
                # with the archive in it, which is what D5 asks for and
                # what only the host knows.
                audit_tenant("rehearsal.create",
                             resolve_tenant((inst or {}).get("tenant"))
                             or ensure_default_tenant(),
                             subject=str(req.get("to") or ""), result="denied",
                             who=actor or "portal", role=act_role or "-",
                             detail=msg)
        elif action == "rehearsal-extend":
            try:
                new_date = rehearsal_extend(
                    name, days=int(req.get("days") or REHEARSAL_STEP),
                    who=actor or "portal", role=act_role or "-")
                ok = True
                msg = f"rehearsal extended, now expires {new_date}"
            except (RehearsalRefused, ValueError) as e:
                msg = str(e)
        elif not inst or name not in tokens:
            msg = "unknown instance or no deploy token"
        elif inst["channel"] != "test":
            msg = "not a test instance"
        elif not inst.get("source") or inst["source"].get("kind") not in (
                "git", "local", "artifact"):
            msg = "no usable package source recorded"
        elif (inst["source"].get("kind") == "artifact"
              and not os.path.isfile(source_package_arg(name, inst["source"]))):
            # the retained artifact IS the source; without it there is
            # nothing to fetch and the hook must say so plainly
            msg = ("the retained artifact for this instance is gone — upload "
                   "a new one (announce, then PUT the package)")
        else:
            src = inst["source"]
            revision = _resolve_revision(src)
            ok, msg = run_install(src, "test")
        version = (load_registry()["instances"].get(name) or {}).get("version", "")
        via = {"install": "store", "visibility": "portal",
               "tile": "portal", "rehearse": "portal",
               "rehearsal-extend": "portal",
               "config": "portal", "token": "portal",
               "address": "portal", "throttle": "portal",
               "remove": "portal", "create": "portal",
               "endpoint": "portal", "link": "portal",
               "source": "portal", "node": "setup wizard",
               "envelope": "portal", "rollback": "portal",
               "artifact-remove": "portal",
               "grant": "portal", "promote": "portal"}.get(action, "deploy-hook")
        # The request id travels into the log, not only into the result
        # file (RFC-0024 §1). Result files are pruned after an hour; the
        # log is not — so a client that comes back the next morning can
        # still learn the outcome of the deployment it started.
        record = {"instance": name or "(dieser Knoten)", "ok": ok,
                  "id": rid, "message": msg, "revision": revision,
                  "version": version, "via": via}
        if store_src:
            # Which list an app came from, and — for an unverified
            # source — who accepted it, when, for which app. Without
            # this the confirmation would be a dialogue that leaves no
            # trace (RFC-0012 §3).
            record["source"] = store_src["id"]
            record["source_trust"] = store_src.get("trust", "")
            if (store_src.get("trust") == "unverified"
                    and req.get("confirm_source") == store_src["id"]):
                # Tied to the confirmation, NOT to the outcome: a run
                # that was accepted and then failed for some other
                # reason is exactly what one wants to find later, and
                # the refusal path must not name anybody as having
                # confirmed something they did not.
                record["confirmed_by"] = req.get("by", "?")
        audit_deploy(record)
        # ... and one line in the tenant's own audit log for the actions
        # that change who owns or reaches what (spec 1.7). Written HERE,
        # at the one point every worker action passes through, so a new
        # action cannot quietly forget to be recorded. Filed in the
        # tenant of the instance -- including when a server_admin did
        # it, which is the whole point: the customer has to be able to
        # see the operator's actions in their own log (RFC-0022 §6).
        # "Erneut ausrollen" reuses the rollback path but is a
        # deployment, not a step back — the tenant's log has to say which
        # of the two happened (RFC-0024 §4).
        audited = TENANT_AUDITED.get(
            "artifact" if (action == "rollback" and retry) else action)
        if audited:
            tid = audit_tenant_id
            if not tid:
                after = load_registry().get("instances", {}).get(name) or {}
                tid = (resolve_tenant(after.get("tenant"))
                       or resolve_tenant((inst or {}).get("tenant"))
                       or ensure_default_tenant())
            audit_tenant(audited, tid, name,
                         "ok" if ok else "denied",
                         who=actor or (req.get("via") or "deploy hook"),
                         role=act_role or "-",
                         detail="" if ok else msg)
        if rid:
            res_tmp = os.path.join(results, f"{rid}.tmp")
            with open(res_tmp, "w", encoding="utf-8") as f:
                # The KEY the request ended up on (RFC-0025 8.1). For a
                # create the caller only knew the tenant-local name, and
                # without this the portal could not link to what it just
                # made.
                json.dump({"ok": ok, "message": msg, "revision": revision,
                           "version": version, "id": rid, "key": name}, f)
            os.replace(res_tmp, os.path.join(results, f"{rid}.json"))
        os.remove(req_path)          # the claim: this request is answered
        DEADLINE = None
        print(f"deploy {name}: {'OK' if ok else 'FAILED'} — {msg}")
        handled += 1
    if handled:
        # What a rehearsal would cost, re-measured where the portal can
        # read it (spec 2.15.3). Here rather than in every branch: an
        # install, a removal and a config change all move these numbers,
        # and a view refreshed only by the action that thought of it is
        # the kind of drift this codebase keeps finding.
        rehearsal_view_write()


# --------------------------------------------- backup & restore (oaap.data.backup)

def _backup_missing_instances(archive, reg):
    """Which instances of `reg` have no data in `archive`.

    Found on oaap-test 2026-09-05: since the instance tree moved under
    `tenants/` (RFC-0026), `oaap backup create` archived the registry
    and the users and NOT one byte of app data or of any instance.env
    -- and said "Backup written" either way. The restore would have
    succeeded too, onto an empty platform.

    So completeness is measured against the archive itself. An instance
    counts as present when the archive holds at least one entry inside
    its data directory; an instance that genuinely has no directory yet
    (installed, never started) counts as present, because there is
    nothing of it to lose.
    """
    listing = subprocess.run(["tar", "-tzf", archive], check=True, text=True,
                             capture_output=True).stdout.splitlines()
    entries = {e.rstrip("/") for e in listing}
    missing = []
    for name, inst in sorted(reg.get("instances", {}).items()):
        d = instance_dir(name, inst)
        if not os.path.isdir(d):
            continue
        rel = os.path.relpath(d, DATA_DIR).replace(os.sep, "/")
        if not any(e == rel or e.startswith(rel + "/") for e in entries):
            missing.append(name)
    return missing


# Three files, one reader (RFC-0029 D2).
#
# The portal already mounts the registry directory read-only, so
# everything a backup page needs is put HERE rather than behind a new
# mount or an API. Three files, because there are three writers and
# each one knows something the others cannot:
#
#   backup-last.json      what THIS node's own backup did -- written by
#                         `oaap backup create`, which is the only thing
#                         that can measure it
#   backup-schedule.json  what is planned here -- written by whoever
#                         installs a schedule; absent means "nobody has
#                         set this up", which is a different answer from
#                         "it failed"
#   backup-pulls/<node>.json   what THIS node fetched from another one
#                         -- written by the puller, because only the
#                         puller knows whether the copy arrived and
#                         whether the checksum matched. The node that
#                         gave the archive away must never claim this.
BACKUP_RUN_FILE = os.path.join(APPS_DIR, "backup-last.json")
BACKUP_SCHEDULE_FILE = os.path.join(APPS_DIR, "backup-schedule.json")
BACKUP_PULLS_DIR = os.path.join(APPS_DIR, "backup-pulls")


def _dir_kbytes(path):
    """Size of a directory in KiB, or 0 when it cannot be measured.

    `du` rather than walking in Python: it is one process instead of
    tens of thousands of stat calls, and a backup preflight must not
    take longer than the thing it checks.
    """
    try:
        out = subprocess.run(["du", "-sk", path], check=True, text=True,
                             capture_output=True).stdout
        return int(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError):
        return 0


def _record_backup_run(state, **fields):
    """Write down what this node's backup is doing, or last did.

    Lives beside the registry, which the portal already mounts
    read-only, so a page can read it without a new mount or an API
    (RFC-0029 D1/D2).

    Written on EVERY outcome, not only on success. A record that
    appears only when things went well cannot tell "last night failed"
    apart from "nobody ever set this up" -- and those two need
    different answers from whoever is reading the page. `running` is
    written before the apps are stopped, so the page can say "seit N
    Minuten" while it is happening, which is exactly when somebody is
    looking for the reason their app is unreachable (the RFC-0024
    vocabulary, deliberately reused).

    What it records are MEASUREMENTS, never promises: the sentence the
    portal owes its operator -- "your apps will stop for about this
    long" -- has to be this node's own last figure. A number from a
    manual is always somebody else's machine.
    """
    try:
        prev = last_backup_run() or {}
        rec = {"schema": "0.2", "state": state,
               "started": prev.get("started") if state != "running" else _iso_now()}
        rec.update(fields)
        if state != "running":
            rec["finished"] = _iso_now()
        with open(BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        os.chmod(BACKUP_RUN_FILE, 0o644)   # names no secret, only sizes
    except OSError as e:
        print(f"NOTE: could not record the backup state ({e}).")


def last_backup_run():
    """What the last backup did, or None. Read by the portal."""
    try:
        with open(BACKUP_RUN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ----------------------------------------------- the schedule (RFC-0029 D1)
#
# WHO OWNS WHAT. The timer UNIT belongs to ops/install-backup-timer.sh --
# setting a node up for nightly backups is still the trial run, not a
# platform capability. What lives here is the SCHEDULE INSIDE it: the
# hour, how many archives stay, and whether it is armed at all. That is
# the half Jörg decided to have in the portal.
#
# The split is kept honest by a drop-in: ops writes the unit, the
# platform writes `portal.conf` beside it and never touches the unit.
# Two writers of one file would have been the same mistake this
# codebase keeps finding.
#
# And systemd is the TRUTH, not backup-schedule.json. The file is a
# view, written from what the timer actually reports -- a page must not
# claim a schedule that is not armed. Same shape as apps/artifacts.json.
BACKUP_UNIT = "oaap-backup.timer"
BACKUP_SERVICE = "oaap-backup.service"
SYSTEMD_DIR = "/etc/systemd/system"


def _systemctl(*args, check=False):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                          check=check)


def _unit_prop(unit, prop):
    r = _systemctl("show", "-p", prop, "--value", unit)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def backup_timer_installed():
    return os.path.isfile(os.path.join(SYSTEMD_DIR, BACKUP_UNIT))


def backup_schedule_write():
    """Write down what the timer ACTUALLY says. Returns the record.

    Absent file means "nobody set this up", which the health page has to
    be able to say -- a different answer from "it failed", needing a
    different action (RFC-0029 D2).
    """
    if not backup_timer_installed():
        try:
            os.remove(BACKUP_SCHEDULE_FILE)
        except OSError:
            pass
        return None
    enabled = _unit_prop(BACKUP_UNIT, "ActiveState") == "active"
    # The hour as systemd holds it, not as we last wrote it: a hand-made
    # edit on the machine has to show up in the portal, not be papered
    # over by our own memory of what we set.
    cal = _unit_prop(BACKUP_UNIT, "TimersCalendar")
    at = ""
    m = re.search(r"(\d{2}):(\d{2}):\d{2}", cal or "")
    if m:
        at = f"{m.group(1)}:{m.group(2)}"
    env = _unit_prop(BACKUP_SERVICE, "Environment")
    keep, target = 2, "/var/backups/oaap"
    for part in (env or "").split():
        if part.startswith("OAAP_BACKUP_KEEP="):
            try:
                keep = int(part.split("=", 1)[1])
            except ValueError:
                pass
        elif part.startswith("OAAP_BACKUP_TARGET="):
            target = part.split("=", 1)[1]
    rec = {"schema": "0.1", "enabled": enabled, "at": at, "keep": keep,
           "target": target, "unit": BACKUP_UNIT,
           "next": _unit_prop(BACKUP_UNIT, "NextElapseUSecRealtime")}
    try:
        tmp = BACKUP_SCHEDULE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, BACKUP_SCHEDULE_FILE)
        os.chmod(BACKUP_SCHEDULE_FILE, 0o644)
    except OSError as e:
        print(f"WARNING: could not write {BACKUP_SCHEDULE_FILE}: {e}", flush=True)
    return rec


def backup_schedule_set(at="", keep=None, enabled=None):
    """Change the hour, the retention, or whether it runs at all.

    Never creates the unit -- that is ops/install-backup-timer.sh's job,
    and a node without one is told so rather than silently given a
    schedule the operator never asked for.
    """
    if not backup_timer_installed():
        raise ValueError(
            "this node has no nightly backup yet. Setting one up is done on "
            "the machine (sudo bash ops/install-backup-timer.sh) — it decides "
            "WHERE the archives go, and that is deliberately not a portal "
            "setting")
    if at:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            raise ValueError("the time wants HH:MM in 24-hour form, e.g. 03:30")
        d = os.path.join(SYSTEMD_DIR, BACKUP_UNIT + ".d")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "portal.conf"), "w", encoding="utf-8") as f:
            # The empty assignment first: systemd ACCUMULATES OnCalendar,
            # so without it the node would back up at both hours.
            f.write("\n".join([
                "# Written by OAAP (RFC-0029 D1). The unit itself belongs",
                "# to ops/install-backup-timer.sh; only this file is ours.",
                "[Timer]",
                "OnCalendar=",
                f"OnCalendar=*-*-* {at}:00",
                ""]))
    if keep is not None:
        keep = int(keep)
        if keep < 1 or keep > 30:
            raise ValueError("keep: between 1 and 30 archives stay on this node")
        d = os.path.join(SYSTEMD_DIR, BACKUP_SERVICE + ".d")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "portal.conf"), "w", encoding="utf-8") as f:
            f.write("\n".join([
                "# Written by OAAP (RFC-0029 D1).",
                "[Service]",
                f"Environment=OAAP_BACKUP_KEEP={keep}",
                ""]))
    _systemctl("daemon-reload")
    if enabled is not None:
        _systemctl("enable" if enabled else "disable",
                   "--now" if enabled else "--now", BACKUP_UNIT)
    elif _unit_prop(BACKUP_UNIT, "ActiveState") == "active":
        # A running timer has to be restarted to pick up a new hour.
        _systemctl("restart", BACKUP_UNIT)
    rec = backup_schedule_write()
    if rec is None:
        return "the nightly backup is gone"
    if not rec["enabled"]:
        return "the nightly backup is switched off — nothing runs on its own"
    nxt = rec["next"] or "unknown"
    return (f"nightly backup at {rec['at']}, keeping {rec['keep']} archive(s) "
            f"on this node — next run {nxt}")


def cmd_backup(args):
    """Offline-consistent platform backup: one self-contained archive."""
    if args.action == "schedule":
        return cmd_backup_schedule(args)
    import datetime
    import socket
    import time

    target = args.to or "/var/backups/oaap"
    if target.endswith(".tar.gz"):
        out_dir = os.path.abspath(os.path.dirname(target) or ".")
        out_file = os.path.basename(target)
    else:
        out_dir = os.path.abspath(target)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_file = f"oaap-backup-{socket.gethostname()}-{stamp}.tar.gz"
    data_abs = os.path.abspath(DATA_DIR)
    if os.path.commonpath([out_dir, data_abs]) == data_abs:
        die(f"backup target {out_dir} lies inside the platform data directory "
            f"{DATA_DIR} — a backup that dies with the machine is not a backup. "
            "Choose an outside path with --to.")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, out_file)

    env = {}
    try:
        with open(os.path.join(APP_DIR, ".env"), encoding="utf-8") as f:
            env = dict(l.strip().split("=", 1) for l in f if "=" in l)
    except OSError:
        die(f"no platform installation found at {DATA_DIR}")
    reg = load_registry()
    manifest = {
        "backup_format": "0.1",
        "platform_version": env.get("OAAP_VERSION", "unknown"),
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hostname": socket.gethostname(),
        "http_port": env.get("OAAP_HTTP_PORT", "80"),
        "external_host": load_external(),
        # Recorded for the operator, NOT restored (RFC-0011 decision 4):
        # a profile describes the machine, not the service.
        "node_profiles": load_profiles(),
        "instances": {n: {"app_name": i["app_name"], "version": i["version"],
                          "channel": i["channel"]}
                      for n, i in sorted(reg["instances"].items())},
    }

    # RFC-0029 D3: the apps stop for the COPY, not for the compression.
    #
    # Measured on oaapx01 before this change: 487 seconds of downtime
    # for 8.0 GB, of which almost everything was gzip. Copying is
    # disk-bound and quick; compressing is CPU-bound and slow, and it
    # does not need the data to hold still -- so it happens afterwards,
    # with every app running again.
    #
    # The price is holding the data twice for a few minutes. On the
    # machine this was built for that costs nothing (913 GB free), but
    # it is not free everywhere, so it is checked below and refused
    # loudly rather than discovered as a full disk at 03:30.
    print("Offline-consistent backup (RFC-0029 D3): app containers are stopped")
    print("for the COPY only. Compression runs afterwards, with the apps back up.")
    need = _dir_kbytes(DATA_DIR)
    free = shutil.disk_usage(out_dir).free // 1024
    if need and free < need * 12 // 10:
        die(f"not enough room in {out_dir}: about {need // 1024} MB is copied "
            f"there before it is compressed, and {free // 1024} MB is free. "
            "Free some space, or choose another target with --to.")
    # Said out loud, because it is a real consequence of RFC-0030 and not
    # a detail: while a rehearsal exists, every archive carries a SECOND
    # copy of that production instance's data, and carries it for as long
    # as the archive is kept -- which outlives the rehearsal itself. The
    # archive stays complete either way; a node that restores one gets
    # the rehearsal back and the daily sweep removes it if its time is up.
    _reh = rehearsals(reg)
    if _reh:
        print(f"NOTE: {len(_reh)} rehearsal instance(s) on this node "
              f"({', '.join(sorted(_reh))}) — this archive therefore holds a "
              "second copy of their production data (RFC-0030).")
    running = run(["docker", "ps", "-q", "--filter", "name=^oaap-app-"]).stdout.split()
    stage = tempfile.mkdtemp(prefix="oaap-backup-")
    # The managed Postgres (oaap.data.store 0.1, spec 2.4): a DUMP, not
    # a copy of its data directory -- a dump survives a Postgres
    # major-version change across a relocation (RFC-0029 "Umzug"), a
    # directory copy does not. Taken here, BEFORE the app-stop window
    # below: 'store' is a platform service, not an app container (it is
    # never in `running`, which only lists 'oaap-app-*'), and pg_dumpall
    # is transactionally consistent against a live server -- no reason
    # to make apps wait for it.
    store_dumped = False
    if has_profile("store") and _store_running():
        try:
            dump = run(["docker", "exec", "-u", "postgres", STORE_CONTAINER,
                        "pg_dumpall"]).stdout
            with open(os.path.join(stage, "store-dump.sql"), "w",
                      encoding="utf-8") as f:
                f.write(dump)
            store_dumped = True
        except subprocess.CalledProcessError:
            pass  # caught by the completeness check below -- no archive
                  # is written rather than one that silently lacks it.
    # `<name>.part.tar` rather than something ending in .tar.gz: a
    # half-written file must not be mistakable for an archive, least of
    # all by the tired eye at two in the morning.
    tmp_tar = out_path[:-len(".tar.gz")] + ".part.tar"
    tmp_out = out_path + ".tmp"
    downtime = 0.0
    t0 = time.monotonic()
    # `tenants` carries every instance's data and its instance.env
    # since RFC-0026 (0.1.59). Before that the same things lay flat
    # under `apps/<key>/`, which is why this list used to be complete
    # without it -- and why leaving it out was invisible: the command
    # still succeeded, the archive still restored, and what came back
    # had no app data in it. Both are named here, so an archive from a
    # node mid-migration is complete either way.
    paths = ["app/.env", "apps", "data/identity"]
    if os.path.isdir(TENANTS_DIR):
        paths.append("tenants")
    # The tenant audit log (oaap.core.tenant 1.7). Found missing by the
    # first real restore drill, 2026-09-05: the restored node came back
    # complete and said "No entries yet". It is the counterweight to "a
    # server_admin may do everything on this node" (RFC-0022 D5) -- the
    # customer's own record of what the operator did in their tenant. A
    # restore that drops it silently hands the operator a clean slate.
    if os.path.isdir(AUDIT_DIR):
        paths.append("data/audit")

    def copy_phase():
        """The only part the apps have to stand still for."""
        try:
            if running:
                run(["docker", "stop", *running])
            with open(os.path.join(stage, "backup-manifest.json"), "w",
                      encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            stage_files = ["backup-manifest.json"]
            if store_dumped:
                stage_files.append("store-dump.sql")
            # No -z. Compression is CPU-bound and does not need the data
            # to hold still, so it happens after the apps are back.
            run(["tar", "--numeric-owner", "-cpf", tmp_tar,
                 "--exclude=data/identity/login-throttle.json",
                 # node profiles stay with the machine (RFC-0011
                 # decision 4): a 'dev' backup restored onto a
                 # production box must not bring developer powers along.
                 # The manifest still records them so the operator can
                 # set them again deliberately.
                 "--exclude=apps/node.json",
                 "-C", stage, *stage_files,
                 "-C", DATA_DIR, *paths])
            os.chmod(tmp_tar, 0o600)
        finally:
            # The apps come back HERE -- before compression, and also
            # when the copy failed. A backup that leaves the node
            # stopped has done more damage than the one it prevented.
            if running:
                subprocess.run(["docker", "start", *running],
                               capture_output=True, text=True)
            shutil.rmtree(stage, ignore_errors=True)

    written = False
    # Said before anything stops, so the page can answer the question
    # somebody is about to ask -- "why is my app not answering?" --
    # while it is still true (RFC-0029 D2).
    _record_backup_run("running", target=out_path,
                       instances=len(manifest["instances"]))
    try:
        copy_phase()
        downtime = time.monotonic() - t0
        print(f"Apps are running again after {downtime:.0f}s. Compressing ...")
        # pigz is gzip that uses every core, and writes an ordinary .gz.
        # It shortens the total run, never the downtime -- that is over.
        comp = "pigz" if shutil.which("pigz") else "gzip"
        run([comp, "-f", tmp_tar])
        os.replace(tmp_tar + ".gz", tmp_out)
        os.chmod(tmp_out, 0o600)

        # Does the archive actually hold what the registry says exists?
        #
        # This is the check whose absence let the 2026-09-05 gap live:
        # the list of paths was written once, an identifier moved
        # underneath it, and nothing ever compared the two. Asking the
        # ARCHIVE rather than the code is what makes the question
        # survive the next move -- it cannot be satisfied by a path that
        # is merely spelled correctly.
        missing = _backup_missing_instances(tmp_out, reg)
        # Same question asked of the managed Postgres (oaap.data.store
        # 0.1, spec 2.4): a node profiled for it that fails to dump must
        # not produce an archive that calls itself complete.
        if has_profile("store") and not missing and not store_dumped:
            missing = ["the managed Postgres (dump failed or 'store' "
                       "was not running)"]
        if not missing and (os.path.isfile(TENANT_LOG) or store_dumped):
            listing = subprocess.run(["tar", "-tzf", tmp_out], check=True,
                                     text=True, capture_output=True).stdout.split()
            rel = os.path.relpath(TENANT_LOG, DATA_DIR).replace(os.sep, "/")
            if os.path.isfile(TENANT_LOG) and rel not in listing:
                missing = ["the tenant audit log"]
            elif store_dumped and "store-dump.sql" not in listing:
                missing = ["the managed-Postgres dump"]
        if missing:
            reason = ("incomplete -- missing the data of: "
                      + ", ".join(missing))
            _record_backup_run("failed", message=reason, target=out_path)
            die("the archive does not contain the data of: "
                + ", ".join(missing)
                + ". Nothing was written -- an incomplete backup that "
                  "calls itself complete is worse than none. Please "
                  "report this with the platform version.")
        os.replace(tmp_out, out_path)
        written = True
    except (subprocess.CalledProcessError, OSError, DeployTimeout) as e:
        # Recorded before the process ends, so the page shows a failure
        # rather than a run that started last night and never finished.
        err = (getattr(e, "stderr", "") or "").strip().splitlines()
        reason = (f"{e.cmd[0]}: " + (err[-1] if err else f"exit {e.returncode}")
                  if isinstance(e, subprocess.CalledProcessError) else str(e))
        _record_backup_run("failed", message=reason, target=out_path)
        die(f"backup failed: {reason}")
    finally:
        # Neither temporary may outlive a failure: one is the size of the
        # whole platform, and a directory holding an 8 GB remnant of a
        # run nobody remembers is how a backup target fills up.
        if not written:
            for leftover in (tmp_tar, tmp_tar + ".gz", tmp_out):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    total = time.monotonic() - t0
    size = os.path.getsize(out_path)
    size_h = f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"
    print(f"Backup written: {out_path} ({size_h}, "
          f"{len(manifest['instances'])} app instance(s), "
          f"app downtime {downtime:.0f}s of {total:.0f}s total)")
    # What the downtime actually was, written down where a page can read
    # it. RFC-0029 D1 asks the portal to say how long the apps will be
    # stopped -- with THIS node's last measured figure, not a general
    # one, because the general one is always somebody else's machine.
    _record_backup_run("ok", archive=out_path, bytes=size,
                       instances=len(manifest["instances"]),
                       downtime_seconds=round(downtime),
                       total_seconds=round(total))
    print("SECURITY: this file contains ALL platform secrets, password hashes")
    print("and app data — guard it like a master key (permissions are 0600).")
    print("Restore on a prepared machine with: sudo ./install.sh restore <file>")


def _deploy_from_registry(name, inst):
    """Bring one restored instance back: image, container, gateway site.

    Returns False (with an explanation) when the instance cannot come
    back automatically; its data stays restored either way.
    """
    # RFC-0016: an instance may have several service containers; build or
    # pull each. The package source is fetched once if ANY service needs
    # a local build.
    services = instance_services(inst)
    needs_build = any(s["image"].startswith("oaap-app/") for s in services)
    tmp = None
    pkg = ""
    try:
        if needs_build:
            src = inst.get("source") or {}
            if src.get("kind") == "git":
                tmp = tempfile.mkdtemp(prefix="oaap-restore-")
                print(f"Fetching {src['url']} ...")
                branch = ["--branch", src["ref"]] if src.get("ref") else []
                run(["git", "clone", "--depth", "1", *branch, src["url"], tmp])
                pkg = os.path.join(tmp, src.get("path") or "")
            elif src.get("kind") == "local":
                pkg = os.path.join(src.get("url", ""), src.get("path") or "")
            elif src.get("kind") == "artifact":
                # the retained artifact travelled in the backup with the
                # instance directory — an artifact-deployed instance is
                # the first kind whose backup is genuinely self-contained
                zip_path = source_package_arg(name, src)
                if os.path.isfile(zip_path):
                    tmp = tempfile.mkdtemp(prefix="oaap-restore-")
                    try:
                        extract_artifact(zip_path, tmp)
                        pkg = package_root(tmp, src.get("path") or "")
                    except ArtifactRejected as e:
                        print(f"SKIPPED {name}: retained artifact unusable ({e}).")
                        return False
            if not pkg or not os.path.isdir(pkg):
                print(f"SKIPPED {name}: images must be rebuilt, but the package "
                      f"source is not available on this machine "
                      f"({src.get('url') or 'no source recorded'}). Data is restored — "
                      f"copy the package here or reinstall it under the same name.")
                return False
        for s in services:
            if s["image"].startswith("oaap-app/"):
                print(f"Building {s['image']} ...")
                run(["docker", "build", "-q", "-t", s["image"],
                     os.path.join(pkg, s.get("build") or ".")])
            else:
                print(f"Pulling {s['image']} ...")
                run(["docker", "pull", "-q", s["image"]])
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if not os.path.isfile(env_path(name)):
        print(f"SKIPPED {name}: no instance.env in the restored data.")
        return False
    # RFC-0015: a granted endpoint's PORT and router forward belong to the
    # old machine. Do not re-publish it here (the port may be taken and no
    # forward exists) — bring the instance back with the port CLOSED, drop
    # the stale grant, and report so the operator re-grants deliberately.
    had_endpoints = inst.get("endpoints") or []
    recreate_instance_containers(name, services, inst.get("storage") or [])
    if had_endpoints:
        r = load_registry()
        if name in r["instances"]:
            r["instances"][name].pop("endpoints", None)
            save_registry(r)
    write_app_caddy(name, inst)
    print(f"Restored '{name}' ({inst['app_name']} {inst['version']}, "
          f"channel {inst['channel']}, port {inst['port']})")
    # The instance's own public names travel with it (RFC-0009/0018): they
    # belong to the app, not to the machine. Say so out loud for EVERY name
    # — on a new machine each still points at the OLD one until DNS moves.
    for i, host in enumerate(instance_names(inst)):
        kind = "own address" if i == 0 else "alias"
        print(f"         {kind} {host} came along — it must be "
              f"pointed at THIS machine before it works again.")
    return True


def _report_dropped_profiles():
    """Name what the restore deliberately did NOT bring along (RFC-0011).

    Silence would be the wrong kind of safe: an operator restoring a
    workbench would wonder why the portal suddenly refuses to create
    instances.
    """
    try:
        with open(os.path.join(DATA_DIR, "last-restore-manifest.json"),
                  encoding="utf-8") as f:
            profiles = json.load(f).get("node_profiles") or []
    except (OSError, ValueError):
        return
    if not profiles:
        return
    print(f"Note: the backup came from a node with profile(s) "
          f"{', '.join(profiles)}. Profiles describe the machine, not the "
          f"service, so they are NOT restored. Set them again deliberately "
          f"with: sudo oaap node add-profile {profiles[0]}")


def cmd_backup_schedule(args):
    """Show or change the nightly schedule (RFC-0029 D1)."""
    if args.refresh:
        backup_schedule_write()
        return
    if not (args.at or args.keep is not None or args.on or args.off):
        rec = backup_schedule_write()
        if rec is None:
            print("No nightly backup on this node — nothing runs on its own.")
            print("Set one up on the machine: sudo bash ops/install-backup-timer.sh")
            return
        print(f"nightly backup: {'on' if rec['enabled'] else 'OFF'}")
        print(f"  at:     {rec['at'] or '?'}")
        print(f"  keep:   {rec['keep']} archive(s) on this node")
        print(f"  target: {rec['target']}")
        print(f"  next:   {rec['next'] or '—'}")
        last = last_backup_run() or {}
        if last.get("downtime_seconds") is not None:
            print(f"  the last run stopped this node's apps for "
                  f"{last['downtime_seconds']}s")
        return
    if args.on and args.off:
        die("--on and --off at the same time")
    try:
        print(backup_schedule_set(args.at, args.keep,
                                  True if args.on else False if args.off else None))
    except ValueError as e:
        die(str(e))


def cmd_restore_instances(_args):
    """Used by 'install.sh restore': re-create every registered instance."""
    _report_dropped_profiles()
    reg = load_registry()
    if not reg["instances"]:
        print("No app instances in the restored registry.")
        return
    ok = skipped = 0
    for name, inst in sorted(reg["instances"].items()):
        if is_rehearsal(inst):
            # A rehearsal is temporary by construction and it holds a
            # copy of customer data. Starting one on a machine that has
            # just been rebuilt would put unreleased code back on that
            # data at the worst possible moment -- while somebody is
            # still finding out what else came back. Its record and its
            # data are here; the daily sweep removes them when its date
            # passes, and until then it can be started deliberately.
            print(f"SKIPPED {name}: a rehearsal is not started by a restore "
                  "(RFC-0030) — it carries a copy of production data and "
                  "expires on its own.")
            skipped += 1
            continue
        if not inst.get("routes") or not inst.get("svc_port"):
            print(f"SKIPPED {name}: registry entry predates route capture — "
                  "reinstall it from its package.")
            skipped += 1
            continue
        try:
            if _deploy_from_registry(name, inst):
                ok += 1
            else:
                skipped += 1
        except subprocess.CalledProcessError as e:
            print(f"SKIPPED {name}: {(e.stderr or str(e)).strip()}")
            skipped += 1
    refresh_generated_sites()
    reload_gateway()
    print(f"App instances: {ok} restored, {skipped} skipped.")


def find_source(sources, target):
    """One source by id, URL, or 1-based position from 'store list'."""
    for s in sources:
        if s["id"] == target or s["url"] == target:
            return s
    if target.isdigit() and 1 <= int(target) <= len(sources):
        return sources[int(target) - 1]
    return None


# --- source operations, shared by the CLI and the portal --------------------
# Both paths run exactly these checks. The portal writes through the
# spool worker (its /apps-registry mount is read-only), and the spool is
# data, not trust — so the rules cannot live only where the button is.
# Each function mutates in place and returns a message; it raises
# ValueError when it refuses.


def source_add(sources, removed, url, name="", sid="", trust="unverified",
               origin=""):
    if not re.match(r"^https://", url or ""):
        raise ValueError("a store source must be an https:// URL")
    if any(s["url"] == url for s in sources):
        raise ValueError("this source is already configured")
    if trust == "platform":
        # RFC-0012 decision 4: "von uns" must mean the same thing on
        # every node, so it stays reserved for what the installation
        # shipped. An operator who vouches for a list uses 'verified'.
        raise ValueError("trust class 'platform' is reserved for sources the "
                         "installation ships — use 'verified' if you vouch "
                         "for this list yourself")
    if trust not in ("verified", "unverified"):
        raise ValueError("trust class must be 'verified' or 'unverified'")
    sid = sid or derived_source_id(url)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}[a-z0-9]", sid):
        raise ValueError("source id: lowercase letters, digits, dot and hyphen")
    if any(s["id"] == sid for s in sources):
        raise ValueError(f"a source with id '{sid}' already exists")
    sources.append({"id": sid, "name": name or sid, "url": url,
                    "trust": trust, "enabled": True, "origin": origin or ""})
    removed[:] = [r for r in removed if r != sid]
    msg = f"store source added: {sid} ({TRUST_LABEL[trust]})"
    if trust == "unverified":
        msg += " — installing from it asks for a confirmation each time"
    return msg


def source_remove(sources, removed, src):
    sources[:] = [s for s in sources if s["id"] != src["id"]]
    if src.get("shipped"):
        # Remembered by id: 'oaap update' must not quietly re-add what
        # the operator threw out (RFC-0012 §4).
        removed[:] = sorted(set(removed) | {src["id"]})
        return (f"store source '{src['id']}' removed — it ships with the "
                "platform, so the removal is remembered and updates will not "
                "bring it back")
    return f"store source '{src['id']}' removed"


def source_enable(src, on):
    src["enabled"] = bool(on)
    return f"store source '{src['id']}' " + ("enabled" if on else "disabled")


def source_rename(src, name):
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name: 1 to 120 characters")
    src["name"] = name
    return f"store source '{src['id']}' renamed to '{name}'"


def source_trust(src, want):
    want = (want or "").strip().lower()
    if want == "platform":
        raise ValueError("trust class 'platform' is reserved for sources the "
                         "installation ships (RFC-0012 decision 4) — use "
                         "'verified'")
    if want not in ("verified", "unverified"):
        raise ValueError("trust class must be 'verified' or 'unverified'")
    if src.get("trust") == "platform":
        raise ValueError(f"'{src['id']}' ships with the platform; its trust "
                         "class is not settable. Disable or remove it instead.")
    was = src.get("trust")
    src["trust"], src["review"] = want, False
    msg = f"store source '{src['id']}': {TRUST_LABEL[was]} -> {TRUST_LABEL[want]}"
    if want == "verified":
        # Raising a class is exactly the step that skips the
        # confirmation of §3, so it says what it costs.
        msg += (" — apps from it now install without a confirmation, and it "
                "outranks unverified sources when several list the same app")
    return msg


def cmd_store(args):
    """Manage store sources (RFC-0012 §2/§3/§4)."""
    try:
        sources, removed, migrated = load_sources()
    except SourcesUnreadable:
        die(f"cannot read the store source list ({STORE_SOURCES}) -- "
            f"try 'sudo oaap store {args.action}'")
    target = args.target or ""

    if args.action == "list":
        if not sources:
            print("No store sources configured. Add one with: "
                  "sudo oaap store add-source <url>")
        for i, s in enumerate(sources, 1):
            flags = [TRUST_LABEL[s["trust"]]]
            if not s.get("enabled", True):
                flags.append("deaktiviert")
            if s.get("shipped"):
                flags.append("mitgeliefert")
            if s.get("review"):
                flags.append("bitte prüfen")
            print(f"{i}. {s['name']} [{s['id']}] — {', '.join(flags)}")
            print(f"   {s['url']}")
            if s.get("origin"):
                print(f"   Herkunft: {s['origin']}")
        if sources:
            print("\nResolution: highest trust class wins; within a class the "
                  "order above decides (RFC-0012 §3).")
        if migrated:
            # Shown, not written: 'list' is the one store command that
            # changes nothing, so it stays usable without root. The
            # migration reaches disk at the next 'store reconcile',
            # which every 'oaap update' runs.
            print("\nThese entries predate RFC-0012; id and trust class are "
                  "derived above and written down by 'sudo oaap store "
                  "reconcile' (which 'sudo oaap update' runs for you).")
        return

    if args.action == "reconcile":
        lines = reconcile_shipped_sources()
        for line in lines:
            print(line)
        if not lines:
            print("Store sources already match what this version ships.")
        return

    if not target:
        die(f"'{args.action}' needs an argument")

    src = None
    if args.action != "add-source":
        src = find_source(sources, target)
        if not src:
            die("no matching source")
    try:
        if args.action == "add-source":
            msg = source_add(sources, removed, target, name=args.name or "",
                             sid=args.id or "", trust=args.trust or "unverified",
                             origin=args.origin or "")
        elif args.action == "remove-source":
            msg = source_remove(sources, removed, src)
        elif args.action in ("enable", "disable"):
            msg = source_enable(src, args.action == "enable")
        elif args.action == "rename":
            msg = source_rename(src, args.value or args.name or "")
        else:
            msg = source_trust(src, args.value)
    except ValueError as e:
        die(str(e))
    print(msg[0].upper() + msg[1:] + ".")
    save_sources(sources, removed)


def main():
    p = argparse.ArgumentParser(prog="oaap app")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install")
    pi.add_argument("package", help="package directory or Git URL")
    pi.add_argument("--path", default="", help="package path inside the directory/repo")
    pi.add_argument("--ref", default="", help="git branch/tag to install from (git sources)")
    pi.add_argument("--name")
    pi.add_argument("--channel", choices=["production", "test"], default="production")
    pi.add_argument("--tenant", default="",
                    help="tenant label for a NEW instance (default: this "
                         "node's own). Ignored on a redeploy — an instance "
                         "never changes tenant")
    pi.add_argument("--bind", action="append", default=[], metavar="WORD=TYPE",
                    help="resolve a 'contributes'/'consumes' entry the "
                         "registry could not match on its own (oaap.data."
                         "model 0.1 §2.4) — repeatable")
    pi.set_defaults(fn=cmd_install)
    pl = sub.add_parser("list")
    pl.set_defaults(fn=cmd_list)
    prn = sub.add_parser("rename",
                         help="give an instance a different name (RFC-0026)")
    prn.add_argument("name")
    prn.add_argument("target", help="the new name")
    prn.add_argument("--grace-days", dest="grace_days", type=int,
                     default=RENAME_GRACE_DAYS,
                     help=f"how long the old name keeps answering "
                          f"(default {RENAME_GRACE_DAYS})")
    prn.add_argument("--yes", action="store_true",
                     help="carry it out after reading the consequences")
    prn.set_defaults(fn=cmd_rename)
    pr = sub.add_parser("remove")
    pr.add_argument("name")
    pr.add_argument("--purge", action="store_true")
    pr.set_defaults(fn=cmd_remove)
    ppu = sub.add_parser("purge",
                         help="delete data left behind by a removed instance")
    ppu.add_argument("name")
    ppu.add_argument("--yes", action="store_true",
                     help="carry it out after reading what is deleted")
    ppu.set_defaults(fn=cmd_purge)
    pv = sub.add_parser("visibility")
    pv.add_argument("name")
    pv.add_argument("mode", choices=["all", "groups"])
    pv.add_argument("groups", nargs="?", default="",
                    help="comma-separated group tags, e.g. buero,finanzen (with 'groups')")
    pv.set_defaults(fn=cmd_visibility)
    pk = sub.add_parser("link", help="app-to-app links (RFC-0016)")
    pk.add_argument("action", choices=["add", "remove", "list"])
    pk.add_argument("source", nargs="?", help="the instance that may reach the target")
    pk.add_argument("target", nargs="?", help="the instance it may reach")
    pk.set_defaults(fn=cmd_link)
    pmn = sub.add_parser("migrate-networks",
                         help="internal: isolate app networks + reconnect the gateway (RFC-0016)")
    pmn.set_defaults(fn=cmd_migrate_networks)
    pmi = sub.add_parser("migrate-instance-dirs",
                         help="move instance data under its tenant (RFC-0026)")
    pmi.set_defaults(fn=cmd_migrate_instance_dirs)
    pmx = sub.add_parser("artifact-index",
                         help="internal: list retained packages where the "
                              "portal can read them (oaap.apps.runtime 2.14)")
    pmx.set_defaults(fn=lambda _a: artifact_index_write())
    pmv = sub.add_parser("rehearsal-index",
                         help="internal: measure what a rehearsal would cost, "
                              "where the portal can read it (spec 2.15)")
    pmv.set_defaults(fn=lambda _a: rehearsal_view_write())
    pmt = sub.add_parser("migrate-tenants",
                         help="internal: create the default tenant and stamp "
                              "what belongs to it (RFC-0022 stage 2)")
    pmt.set_defaults(fn=cmd_migrate_tenants)
    pmr = sub.add_parser("migrate-tenant-routes",
                         help="internal: put the tenant boundary into gateway "
                              "sites written before oaap.core.tenant 0.2")
    pmr.set_defaults(fn=cmd_migrate_tenant_routes)
    pten = sub.add_parser("tenant", help="accounts and tenants of this node "
                                         "(oaap.core.tenant)")
    pten.add_argument("action",
                      choices=["list", "show", "check", "log", "create", "rename"])
    pten.add_argument("name", nargs="?", help="tenant label (default: 'default')")
    pten.add_argument("target", nargs="?", help="the new label, for 'rename'")
    pten.add_argument("--name", dest="title", default="",
                      help="the customer's name in plain words, e.g. 'Kunde Meier GmbH'")
    pten.add_argument("--account", default="",
                      help="account reference (UUID) this tenant belongs to")
    pten.add_argument("--account-name", dest="account_name", default="",
                      help="cached display name of that account")
    pten.add_argument("--grace-days", dest="grace_days", type=int,
                      default=RENAME_GRACE_DAYS,
                      help=f"how long the old label keeps answering after a "
                           f"rename (default {RENAME_GRACE_DAYS})")
    pten.add_argument("--yes", action="store_true",
                      help="carry out a rename after reading its consequences")
    pten.add_argument("-n", dest="count", type=int, default=50,
                      help="how many audit entries to show (default 50)")
    pten.set_defaults(fn=cmd_tenant)
    pep = sub.add_parser("endpoint", help="non-HTTP endpoints (RFC-0015)")
    pep.add_argument("action", choices=["list", "allow", "deny"])
    pep.add_argument("name", help="instance name")
    pep.add_argument("endpoint", nargs="?", help="endpoint name (for allow/deny)")
    pep.set_defaults(fn=cmd_endpoint)
    pt = sub.add_parser("tile")
    pt.add_argument("name")
    # Deliberately no argparse `choices` here: whether it validates the
    # DEFAULT of an omitted optional positional differs between Python
    # versions (3.13 rejects "", 3.14 does not), and `oaap app tile
    # <name>` with no mode has to work on every node in the fleet.
    # Checked in cmd_tile instead, where the message is ours.
    pt.add_argument("mode", nargs="?", default="",
                    help="auto = follow the app's own class (default), "
                         "on = always show, off = never show; omit to ask")
    pt.set_defaults(fn=cmd_tile)
    pcf = sub.add_parser("config")
    pcf.add_argument("action", choices=["list", "set", "unset"])
    pcf.add_argument("name")
    pcf.add_argument("key", nargs="?")
    pcf.add_argument("value", nargs="?",
                     help="omit with 'set' to be prompted (hidden input)")
    pcf.add_argument("--append", action="store_true",
                     help="with 'set': append to the current value with ';' "
                          "instead of replacing it (list-valued keys)")
    pcf.set_defaults(fn=cmd_config)
    pa = sub.add_parser("address")
    pa.add_argument("action", choices=["show", "set", "remove",
                                       "alias-add", "alias-remove"])
    pa.add_argument("name")
    pa.add_argument("hostname", nargs="?",
                    help="public hostname of its own, e.g. hub.example.org; "
                         "with alias-add/alias-remove, the alias name")
    pa.set_defaults(fn=cmd_address)
    pth = sub.add_parser("throttle")
    pth.add_argument("action", choices=["show", "set", "off"])
    pth.add_argument("name")
    pth.add_argument("rate", nargs="?",
                     help="<requests>/<seconds> per client address, e.g. 300/60")
    pth.set_defaults(fn=cmd_throttle)
    pm = sub.add_parser("machine", help="machine principals (RFC-0027) -- "
                        "accounts that authenticate by key, never by password")
    pm.add_argument("action", choices=["add", "list"])
    pm.add_argument("name", nargs="?")
    pm.add_argument("--roles", default="user",
                    help="comma-separated, default 'user'; server_admin refused")
    pm.add_argument("--groups", default="",
                    help="comma-separated visibility groups (RFC-0007)")
    pm.add_argument("--tenant", default="",
                    help="tenant label or id; default is this node's default tenant")
    pm.add_argument("--title", default="", help="display name")
    pm.set_defaults(fn=cmd_machine)
    pk = sub.add_parser("key", help="API keys (RFC-0027) -- issue, list, revoke")
    pk.add_argument("action", choices=["issue", "list", "revoke"])
    pk.add_argument("name", nargs="?",
                    help="principal for 'issue', key id for 'revoke'")
    pk.add_argument("--roles", default="user", help="comma-separated")
    pk.add_argument("--instance", default="",
                    help="limit the key to one instance (recommended)")
    pk.add_argument("--label", default="", help="what this key is for")
    pk.add_argument("--days", type=int, default=90,
                    help="validity in days (1-365, default 90)")
    pk.set_defaults(fn=cmd_key)
    pu = sub.add_parser("user")
    pu.add_argument("action", choices=["list", "password"])
    pu.add_argument("username", nargs="?")
    pu.add_argument("password", nargs="?",
                    help="omit to be prompted (hidden input) -- 'password' action only")
    pu.set_defaults(fn=cmd_user)
    pc = sub.add_parser("convert")
    pc.add_argument("compose")
    pc.add_argument("--out", default="./oaap-converted")
    pc.add_argument("--profile")
    pc.set_defaults(fn=cmd_convert)
    pt = sub.add_parser("token")
    pt.add_argument("action", choices=["create", "revoke", "list"])
    pt.add_argument("name", nargs="?")

    par = sub.add_parser("artifact", help="uploaded packages (RFC-0019)")
    par.add_argument("action", choices=["list", "rollback"])
    par.add_argument("name", help="instance name")
    par.add_argument("artifact", nargs="?",
                     help="retained artifact (default: the one before the running version)")
    par.set_defaults(fn=cmd_artifact)
    pg = sub.add_parser("grant",
                        help="open instance creation grants (RFC-0019)")
    pg.add_argument("action", choices=["list", "revoke"])
    pg.add_argument("name", nargs="?", help="instance name -- 'revoke' only")
    pg.set_defaults(fn=cmd_grant)
    pp = sub.add_parser("promote",
                        help="ship the tested artifact to production (RFC-0020)")
    pp.add_argument("name", help="test instance to promote FROM")
    pp.add_argument("--to", default="",
                    help="production instance, named inside the test "
                         "instance's tenant and without its short name "
                         "(default: the test instance's name without "
                         "'-test')")
    pp.add_argument("--confirm", action="store_true",
                    help="accept an envelope widening reported as NOTE")
    pp.set_defaults(fn=cmd_promote)
    prh = sub.add_parser("rehearse",
                         help="build a rehearsal: the test instance's code on "
                              "a copy of production data (RFC-0030)")
    prh.add_argument("name", help="the PRODUCTION instance whose data is copied")
    prh.add_argument("--name", dest="to", default="",
                     help="name of the new rehearsal instance, inside the "
                          "same tenant")
    prh.add_argument("--code-from", dest="code_from", default="",
                     help="test instance whose retained package it runs "
                          "(default: <instance>-test)")
    prh.add_argument("--from", dest="from_archive", default="",
                     help="backup archive to take the data from "
                          "(default: the newest one on this node)")
    prh.add_argument("--days", type=int, default=REHEARSAL_DAYS,
                     help=f"how long it lives (default {REHEARSAL_DAYS})")
    prh.add_argument("--yes", action="store_true",
                     help="carry it out after reading what it copies")
    prh.set_defaults(fn=cmd_rehearse)
    prs = sub.add_parser("rehearsal",
                         help="list, extend or end rehearsals (RFC-0030)")
    prs.add_argument("action", choices=["list", "extend", "sweep"])
    prs.add_argument("name", nargs="?", help="rehearsal instance")
    prs.add_argument("--days", type=int, default=REHEARSAL_STEP,
                     help=f"extend by this many days (default {REHEARSAL_STEP})")
    prs.set_defaults(fn=cmd_rehearsal)
    pt.set_defaults(fn=cmd_token)
    pf = sub.add_parser("fleet",
                        help="fleet keys: read-only /fleet/status access (RFC-0021)")
    pf.add_argument("object", choices=["key"])
    pf.add_argument("action", choices=["issue", "list", "revoke"])
    pf.add_argument("label", nargs="?",
                    help="who watches, e.g. fleetview@oaap-demo")
    pf.set_defaults(fn=cmd_fleet)
    pd = sub.add_parser("process-deploys")
    pd.set_defaults(fn=cmd_process_deploys)
    pb = sub.add_parser("backup")
    pb.add_argument("action", choices=["create", "schedule"])
    pb.add_argument("--to", default="", help="target directory or .tar.gz file (outside the data dir)")
    pb.add_argument("--at", default="", help="schedule: hour HH:MM (24h)")
    pb.add_argument("--keep", type=int, default=None,
                    help="schedule: how many archives stay on this node")
    pb.add_argument("--on", action="store_true", help="schedule: arm the timer")
    pb.add_argument("--off", action="store_true", help="schedule: disarm it")
    pb.add_argument("--refresh", action="store_true",
                    help="schedule: only rewrite what the timer says")
    pb.set_defaults(fn=cmd_backup)
    pri = sub.add_parser("restore-instances")
    pri.set_defaults(fn=cmd_restore_instances)
    ps = sub.add_parser("store")
    ps.add_argument("action", choices=["list", "add-source", "remove-source",
                                       "enable", "disable", "trust", "rename",
                                       "reconcile"])
    ps.add_argument("target", nargs="?",
                    help="source URL, id, or position from 'store list'")
    ps.add_argument("value", nargs="?",
                    help="trust class with 'trust': verified | unverified")
    ps.add_argument("--name")
    ps.add_argument("--id", help="stable id (default: derived from the URL)")
    ps.add_argument("--origin", default="",
                    help="who publishes it, shown to the user verbatim")
    ps.add_argument("--trust", choices=["verified", "unverified"],
                    help="trust class for a new source (default: unverified)")
    ps.set_defaults(fn=cmd_store)
    pn = sub.add_parser("node")
    pn.add_argument("action", choices=["show", "add-profile", "remove-profile"])
    pn.add_argument("profile", nargs="?",
                    help="node profile, e.g. dev (RFC-0011)")
    pn.set_defaults(fn=cmd_node)
    pdt = sub.add_parser("data",
                         help="managed Postgres, type registry and other "
                              "data-platform services (oaap.data.store 0.1, "
                              "oaap.data.model 0.1, RFC-0031)")
    pdt.add_argument("object", choices=["store", "model"])
    pdt.add_argument("action", choices=["status", "schemas", "create",
                                        "copy", "drop", "restore",
                                        "types", "show", "register",
                                        "alias", "bindings"])
    pdt.add_argument("arg1", nargs="?",
                     help="create: purpose; copy/drop: schema; restore: "
                          "dump file; model show/register/alias: type key "
                          "or definition file; model bindings: instance")
    pdt.add_argument("arg2", nargs="?",
                     help="create: tenant-id; copy: new schema name; "
                          "model alias: the alias word")
    pdt.add_argument("--yes", action="store_true",
                     help="drop: confirm — deletes the schema permanently")
    pdt.add_argument("--tenant", default="",
                     help="model types/register: tenant-id (RFC-0031 §4 "
                          "third origin — the twin browser's CLI stand-in)")
    pdt.set_defaults(fn=cmd_data)
    pe = sub.add_parser("external")
    pe.add_argument("action", choices=["show", "set", "remove"])
    pe.add_argument("hostname", nargs="?")
    pe.add_argument("--behind-edge", dest="behind_edge", default="",
                    help="edge node's IP address (RFC-0006: TLS terminates at the edge)")
    pe.set_defaults(fn=cmd_external)
    pg = sub.add_parser("edge")
    pg.add_argument("action", choices=["add", "remove", "list"])
    pg.add_argument("hostname", nargs="?")
    pg.add_argument("target", nargs="?")
    pg.add_argument("--port", type=int, default=80,
                    help="target platform's HTTP port (default 80)")
    pg.set_defaults(fn=cmd_edge)
    args = p.parse_args()
    # 'convert' works on files the caller owns; 'node show' only prints
    # what 'oaap status' prints anyway — everything else changes the node.
    read_only = (args.cmd == "convert"
                 or (args.cmd == "node" and args.action == "show")
                 or (args.cmd == "store" and args.action == "list")
                 # `data store status`/`schemas` only read (oaap.data.store
                 # 0.1) -- create/copy/drop/restore change the node and
                 # need root like everything else that does. `data model
                 # types`/`show`/`bindings` are the same shape one level up
                 # (oaap.data.model 0.1) -- register/alias change the node.
                 or (args.cmd == "data" and args.action in
                     ("status", "schemas", "types", "show", "bindings"))
                 # `tenant` reads without root — including `check`, which
                 # reports and deliberately repairs nothing. Creating and
                 # renaming change the node and need root like everything
                 # else that does.
                 or (args.cmd == "tenant"
                     and args.action in ("list", "show", "check", "log")))
    if not read_only and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        die("requires root (sudo oaap app ...)")
    try:
        args.fn(args)
    except subprocess.CalledProcessError as e:
        die(f"command failed: {' '.join(e.cmd)}\n{e.stderr}")


if __name__ == "__main__":
    main()
