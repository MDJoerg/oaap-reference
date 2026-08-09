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

Node-level, not per app:

    oaap node show | add-profile <p> | remove-profile <p>  (RFC-0011)
    oaap store list                                        (RFC-0012)
    oaap store add-source <url> [--name N] [--trust verified|unverified]
    oaap store remove-source <id|url|n> | enable <id> | disable <id>
    oaap store trust <id> verified|unverified
    oaap store reconcile          (run by `oaap update`, RFC-0012 §4)

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
import subprocess
import sys
import tempfile

import yaml

DATA_DIR = os.environ.get("OAAP_DATA_DIR", "/var/lib/oaap")
APP_DIR = os.path.join(DATA_DIR, "app")            # platform installation
APPS_DIR = os.path.join(DATA_DIR, "apps")          # app instances
CADDY_APPS_DIR = os.path.join(APP_DIR, "apps-caddy")
REGISTRY = os.path.join(APPS_DIR, "registry.json")
STORE_SOURCES = os.path.join(APPS_DIR, "store-sources.json")
EXTERNAL_FILE = os.path.join(APPS_DIR, "external.json")
EDGE_FILE = os.path.join(APPS_DIR, "edge.json")
NODE_FILE = os.path.join(APPS_DIR, "node.json")   # node profiles (RFC-0011)
DEPLOY_TOKENS = os.path.join(APPS_DIR, "deploy-tokens.json")
DEPLOY_LOG = os.path.join(APPS_DIR, "deploy-log.jsonl")
SPOOL_DIR = os.path.join(DATA_DIR, "data", "deploy-spool")
PORT_RANGE = range(8100, 8200)
ROLES = {"admin", "keyuser", "user", "guest", "partner", "public"}
GATEWAY_CONTAINER = "oaap-gateway-1"
IDENTITY_CONTAINER = "oaap-identity-1"

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
MANIFEST_MINOR = 2      # 0.2 adds app.class (runtime spec 2.10)

# What an app IS, as opposed to app.type, which says how it is packaged.
# 'service' means "used by other software" and costs the instance its
# launchpad tile. An unknown or missing value counts as 'frontend': a
# tile too many is untidy, a missing tile hides a working app.
APP_CLASSES = ("frontend", "service")
DEFAULT_APP_CLASS = "frontend"


def app_class_of(app):
    """The declared application class, normalised (runtime spec 2.10)."""
    value = str(app.get("class") or "").strip()
    return value if value in APP_CLASSES else DEFAULT_APP_CLASS

# Features a manifest may declare under 'must_understand' -- the exception
# that makes "ignore what you do not know" safe (RFC-0012 §8.2 rule 2). A
# manifest naming anything not listed here is refused with a clear message
# rather than installed half-understood. Empty on purpose: the names
# reserved in RFC-0012 §8.3 land here as they are actually implemented.
MANIFEST_FEATURES = set()


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def load_registry():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"instances": {}}


def save_registry(reg):
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2)
    os.replace(tmp, REGISTRY)


# ------------------------------------------------ node profiles (RFC-0011)
# What this node is FOR. Zero or more, maintained by the operator on the
# machine itself, empty by default -- a node that says nothing behaves
# exactly as before. Only profiles that have an effect today exist
# (RFC-0011 decision 2): a settable profile that does nothing would
# invite typos and false expectations.

PROFILES = {
    "dev": "development node — the portal may create test instances and "
           "install from a source no store list carries yet (RFC-0011)",
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
    else:
        if profile not in profiles:
            die(f"node does not have profile '{profile}'")
        save_profiles([p for p in profiles if p != profile])
        print(f"Node profile '{profile}' removed.")


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
    if app.get("type") not in ("native", "image", "wrapped"):
        errs.append("app.type: native | image | wrapped")
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
    services = m.get("services") or {}
    if len(services) != 1:
        errs.append("exactly one service is supported in runtime increment 1")
    for sname, svc in services.items():
        if not isinstance(svc.get("port"), int):
            errs.append(f"services.{sname}.port: integer required")
        if bool(svc.get("build")) == bool(svc.get("image")):
            errs.append(f"services.{sname}: exactly one of build/image")
    routes = m.get("routes") or []
    if not routes:
        errs.append("routes: at least one route")
    for r in routes:
        if not str(r.get("path", "")).startswith("/"):
            errs.append(f"routes: path must start with / ({r.get('path')})")
        roles = set(r.get("roles") or [])
        if not roles or not roles <= ROLES:
            errs.append(f"routes {r.get('path')}: roles must be non-empty subset of {sorted(ROLES)}")
    for s in m.get("storage") or []:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(s.get("name", ""))) or not str(s.get("mount", "")).startswith("/"):
            errs.append(f"storage entry invalid: {s}")
    if not str((m.get("health") or {}).get("path", "")).startswith("/"):
        errs.append("health.path: required, must start with /")
    if errs:
        die("manifest invalid:\n  - " + "\n  - ".join(errs))


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


def site_body(routes, container, svc_port, groups=None, scope="", throttle=None,
              edge=""):
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
    """
    lines = []
    lines.append("\thandle /auth/* {")
    lines.append("\t\trequest_header -X-OAAP-User")
    lines.append("\t\trequest_header -X-OAAP-Roles")
    lines.append("\t\treverse_proxy identity:8000")
    lines.append("\t}")
    # longest prefix first; catch-all "/" last
    ordered = sorted(routes, key=lambda r: len(r["path"]), reverse=True)
    for r in ordered:
        matcher = "" if r["path"] == "/" else f" {r['path']}*"
        lines.append(f"\thandle{matcher} {{")
        roles = [x for x in r["roles"] if x != "public"]
        if roles or "public" not in r["roles"]:
            # No explicit strip here: Caddy's directive order runs
            # request_header AFTER forward_auth, which would wipe the
            # verified headers again. forward_auth's copy_headers
            # replaces any client-sent values (anti-spoofing), matching
            # the main gateway Caddyfile.
            uri = f"/verify?roles={','.join(sorted(set(roles)))}"
            if groups:
                uri += f"&groups={','.join(sorted(set(groups)))}"
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
        lines.append(f"\t\treverse_proxy {container}:{svc_port}")
        lines.append("\t}")
    if not any(r["path"] == "/" for r in routes):
        lines.append("\thandle {")
        lines.append("\t\trespond 404")
        lines.append("\t}")
    return lines


def caddy_site(port, routes, container, svc_port, groups=None, scope="",
               throttle=None):
    """Generate a LAN gateway listener for one app instance.

    The throttle scope is the instance name on every entry point, so a
    caller cannot multiply its budget by rotating between the LAN port,
    the node subdomain and the instance's own hostname.
    """
    lines = ([f":{port} {{"]
             + site_body(routes, container, svc_port, groups, scope, throttle)
             + ["}"])
    return "\n".join(lines) + "\n"


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
        lines.append(f"http://{host}, http://*.{host} {{")
        lines.append("\tredir https://{host}{uri} permanent")
        lines.append("}")
    skipped = []
    for name, inst in sorted(reg["instances"].items()):
        routes = inst.get("routes")
        if not routes or not inst.get("svc_port"):
            skipped.append(name)
            continue
        lines.append(f"{scheme}://{name}.{host} {{")
        if edge:
            lines += _edge_guard(edge)
        lines += _LOG_BLOCK
        groups = (inst.get("visibility") or {}).get("groups")
        lines += site_body(routes, inst["container"], inst["svc_port"], groups,
                           name, throttle_of(inst), edge)
        lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return skipped


def write_instance_address_caddy():
    """(Re)generate sites for instances carrying their own public name.

    An instance's automatic external name is a subdomain of the NODE
    (`<instance>.<node host>`, write_external_caddy above). That ties a
    published address to the machine it happens to run on, which does
    not survive a move — so an instance may additionally register a
    hostname of its own (RFC-0009). Mode follows the node's external
    configuration: direct means TLS via ACME plus an HTTP redirect,
    behind-edge means plain HTTP with the edge guard and no ACME
    (the edge terminates TLS for the name).
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
        host = inst["address"]
        lines.append(f"# {host} -> instance {name}")
        lines.append(f"{scheme}://{host} {{")
        if edge:
            lines += _edge_guard(edge)
        lines += _LOG_BLOCK
        lines += site_body(inst["routes"], inst["container"], inst["svc_port"],
                           (inst.get("visibility") or {}).get("groups"),
                           name, throttle_of(inst), edge)
        lines.append("}")
        if not edge:
            lines.append(f"http://{host} {{")
            # {host}/{uri} are Caddy placeholders — not Python formatting
            lines.append("\tredir https://{host}{uri} permanent")
            lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def refresh_generated_sites():
    """Regenerate every site file derived from the registry."""
    write_external_caddy()
    write_instance_address_caddy()


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

RESERVED_ENV = {"OAAP_APP_SECRET"}  # platform-owned, never operator-editable


def instance_dir(name):
    return os.path.join(APPS_DIR, name)


def env_path(name):
    return os.path.join(instance_dir(name), "instance.env")


def load_env(name):
    try:
        with open(env_path(name), encoding="utf-8") as f:
            return dict(l.strip().split("=", 1) for l in f if "=" in l)
    except OSError:
        return {}


def save_env(name, env):
    path = env_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())
    os.replace(tmp, path)


def start_instance_container(name, container, image, storage):
    """(Re)create an instance container from its recorded shape."""
    uid = image_uid(image)
    mounts = []
    for s in storage or []:
        host = os.path.join(instance_dir(name), "storage", s["name"])
        os.makedirs(host, exist_ok=True)
        if uid is not None:
            os.chown(host, uid, uid)
        mounts += ["-v", f"{host}:{s['mount']}"]
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)
    run(["docker", "run", "-d", "--name", container,
         "--restart", "unless-stopped", "--network", "oaap_default",
         "--env-file", env_path(name), *mounts, image])


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
    start_instance_container(name, inst["container"], inst["image"],
                             inst.get("storage"))
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
    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w", encoding="utf-8") as f:
        f.write(caddy_site(inst["port"], inst["routes"], inst["container"],
                           inst["svc_port"],
                           (inst.get("visibility") or {}).get("groups"), name,
                           throttle_of(inst)))
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
        raise ValueError(f"names under {ext_host} are already generated "
                         f"automatically — '{name}' is reachable at "
                         f"{name}.{ext_host} without this")
    for r in load_edge():
        if host == r["host"] or host.endswith(f".{r['host']}"):
            raise ValueError(f"{host} is covered by the edge route for "
                             f"{r['host']} (forwarded to {r['target']}) — "
                             "remove that route first")
    taken = next((n for n, i in reg["instances"].items()
                  if n != name and i.get("address") == host), "")
    if taken:
        raise ValueError(f"{host} is already registered for instance '{taken}'")
    if not inst.get("routes") or not inst.get("svc_port"):
        raise ValueError(f"'{name}' predates route capture — reinstall it "
                         "once, then set its address")
    return host


def cmd_address(args):
    """Give one instance a public hostname of its own (RFC-0009)."""
    reg = load_registry()
    name = args.name
    inst = reg["instances"].get(name)
    if not inst:
        die(f"no instance named '{name}'")
    ext_host, edge = load_external_conf()

    if args.action == "show":
        addr = inst.get("address")
        print(f"{name}: {addr}" if addr else f"{name}: no own hostname registered")
        if ext_host:
            print(f"Automatic node address: {name}.{ext_host}")
        return

    if args.action == "remove":
        if not inst.get("address"):
            die(f"'{name}' has no own hostname registered")
        old = inst.pop("address")
        save_registry(reg)
        write_instance_address_caddy()
        reload_gateway()
        print(f"Removed {old} from '{name}'.")
        if ext_host:
            print(f"Still reachable at https://{name}.{ext_host}/")
        return

    try:
        host = check_instance_address(reg, name, inst, args.hostname)
    except ValueError as e:
        die(str(e))

    inst["address"] = host
    save_registry(reg)
    write_instance_address_caddy()
    reload_gateway()
    print(f"'{name}' now answers for {host}.")
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
    print("The automatic node address keeps working — clients can move over "
          "at their own pace.")


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
    name = args.name or app["id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        die("instance name: lowercase [a-z0-9-]")
    channel = args.channel
    reg = load_registry()
    inst = reg["instances"].get(name)
    if inst and inst["channel"] == "production" and inst["version"] == app["version"]:
        die(f"production instance '{name}' already runs version {app['version']} — bump the version (spec: redeploy semantics)")

    (sname, svc), = m["services"].items()
    image = f"oaap-app/{app['id']}:{app['version']}"
    if app["type"] == "native" or svc.get("build"):
        print(f"Building {image} on this node ...")
        run(["docker", "build", "-q", "-t", image,
             os.path.join(pkg, svc.get("build", "."))])
    else:
        image = svc["image"]
        print(f"Pulling {image} ...")
        run(["docker", "pull", "-q", image])

    # port: keep existing assignment (RFC-0005), else allocate
    used = {i["port"] for i in reg["instances"].values()}
    port = inst["port"] if inst else next(p for p in PORT_RANGE if p not in used)

    os.makedirs(instance_dir(name), exist_ok=True)

    # stable per-instance secret, never inside storage mounts. Existing
    # values win over manifest defaults: a redeploy must not undo what
    # the operator configured ('oaap app config').
    env = load_env(name)
    env.setdefault("OAAP_APP_SECRET", secrets.token_hex(32))
    for c in m.get("config") or []:
        env.setdefault(c["key"], c.get("default", ""))
    save_env(name, env)

    # per-instance storage, writable for the container user (guarantee 4)
    container = f"oaap-app-{name}"
    start_instance_container(name, container, image, m.get("storage") or [])

    # visibility (RFC-0007) survives reinstall, same as the port above —
    # a redeploy must not silently reopen a group-restricted instance
    visibility = (inst.get("visibility") or {}) if inst else {}

    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w", encoding="utf-8") as f:
        f.write(caddy_site(port, m["routes"], container, svc["port"],
                           visibility.get("groups"), name,
                           throttle_of(inst or {})))
    reload_gateway()

    reg["instances"][name] = {
        "app_id": app["id"], "app_name": app["name"],
        "version": app["version"], "channel": channel,
        # what the app IS (runtime spec 2.10) — decides the launchpad
        # tile. Read from the MANIFEST at install time, never from a
        # store list: the node must answer this offline, for an app
        # installed straight from Git, and without a foreign list being
        # able to rearrange somebody else's launchpad. Re-read on every
        # install, because it describes the app and the app may change.
        "app_class": app_class_of(app),
        "port": port, "image": image, "container": container,
        # for the portal's health page: where to reach the service on
        # the internal network and which path confirms liveness
        "svc_port": svc["port"],
        "health_path": (m.get("health") or {}).get("path", ""),
        # for regenerating gateway sites (external hostname, RFC-0005 L3)
        "routes": m["routes"],
        # for restore (oaap.data.backup) and the deploy hook (runtime
        # spec 2.5): where the package came from and how to rebuild it
        "source": source,
        "build": svc.get("build", "") if (app["type"] == "native" or svc.get("build")) else "",
        "storage": m.get("storage") or [],
        # declared config keys (labels + secret flags) so the CLI and the
        # portal can offer them for editing without the manifest at hand
        "config": [{"key": c["key"], "label": c.get("label", ""),
                    "secret": bool(c.get("secret")),
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
    save_registry(reg)
    if channel == "production":
        # moving to production invalidates any deploy token (spec 2.5)
        drop_token(name, "instance is on the production channel")
    refresh_generated_sites()
    reload_gateway()
    print(f"Installed '{name}' ({app['name']} {app['version']}, channel {channel})")
    print(f"Entry point: port {port} (through the gateway, login required)")
    ext = load_external()
    if ext:
        print(f"External:    https://{name}.{ext}/")


def cmd_list(_args):
    reg = load_registry()
    if not reg["instances"]:
        print("No app instances installed.")
        return
    for name, i in sorted(reg["instances"].items()):
        print(f"{name}: {i['app_name']} {i['version']} [{i['channel']}] port {i['port']} ({i['container']})")


def remove_instance(reg, name, purge):
    """Tear down one instance; returns a human-readable outcome.

    Shared by the CLI and the portal's host-side worker. Storage is only
    touched when purge is asked for — keeping it is the safe default,
    and the operator can still delete the directory later.
    """
    inst = reg["instances"].pop(name)
    subprocess.run(["docker", "rm", "-f", inst["container"]],
                   capture_output=True, text=True)
    site = os.path.join(CADDY_APPS_DIR, f"{name}.caddy")
    if os.path.isfile(site):
        os.remove(site)
    save_registry(reg)
    # generated sites are derived from the registry — regenerate them,
    # or the node keeps proxying its external name to a dead container
    refresh_generated_sites()
    reload_gateway()
    drop_token(name, "instance removed")
    if purge:
        shutil.rmtree(os.path.join(APPS_DIR, name), ignore_errors=True)
        return f"removed '{name}' including data"
    return f"removed '{name}'; data kept at {os.path.join(APPS_DIR, name)}"


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
    with open(os.path.join(CADDY_APPS_DIR, f"{args.name}.caddy"), "w", encoding="utf-8") as f:
        f.write(caddy_site(inst["port"], inst["routes"], inst["container"],
                           inst["svc_port"], groups, args.name,
                           throttle_of(inst)))
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
    return (inst.get("app_class") or DEFAULT_APP_CLASS) != "service"


def cmd_tile(args):
    reg = load_registry()
    inst = reg["instances"].get(args.name)
    if not inst:
        die(f"no instance named '{args.name}'")
    if args.mode and args.mode not in TILE_MODES:
        die(f"tile: '{args.mode}' is not one of {' | '.join(TILE_MODES)}")
    if not args.mode:
        mode = tile_mode_of(inst)
        cls = inst.get("app_class") or DEFAULT_APP_CLASS
        print(f"'{args.name}' tile: {mode} ({TILE_EXPLAIN[mode]}) — "
              f"the app declares itself '{cls}'")
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
        cls = inst.get("app_class") or DEFAULT_APP_CLASS
        print(f"'{args.name}' tile follows the app again (it declares "
              f"itself '{cls}') — " + ("shown." if tile_visible(inst)
                                       else "not shown."))
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


def audit_deploy(entry):
    import datetime
    entry["when"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(DEPLOY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


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


def load_sources():
    """Store sources in the object form of RFC-0012 §2.

    Old `{url, name}` entries are migrated **in memory on every read**,
    so a node resolves correctly the moment this version lands — before
    anyone runs an update. The migration reaches disk the next time
    something writes the file.

    Returns (sources, removed_shipped, migrated).
    """
    try:
        with open(STORE_SOURCES, encoding="utf-8") as f:
            data = json.load(f)
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


def cmd_process_deploys(_args):
    """Run queued deploy requests (invoked by the oaap-deployd path unit)."""
    import argparse as _argparse
    import contextlib
    import io
    import time

    queue = os.path.join(SPOOL_DIR, "queue")
    results = os.path.join(SPOOL_DIR, "results")
    os.makedirs(queue, exist_ok=True)
    os.makedirs(results, exist_ok=True)
    # prune stale result files (the requester picks them up within seconds)
    now = time.time()
    for f in os.listdir(results):
        p = os.path.join(results, f)
        if now - os.path.getmtime(p) > 3600:
            os.remove(p)

    for req_file in sorted(os.listdir(queue)):
        req_path = os.path.join(queue, req_file)
        try:
            with open(req_path, encoding="utf-8") as f:
                req = json.load(f)
        except (OSError, ValueError):
            os.remove(req_path)
            continue
        name = req.get("instance", "")
        rid = req.get("id", "")
        action = req.get("action", "redeploy")
        reg = load_registry()
        inst = reg["instances"].get(name)
        tokens = load_tokens()
        ok, msg, revision = False, "", ""

        def run_install(src, channel):
            ns = _argparse.Namespace(
                package=src["url"], path=src.get("path", ""),
                ref=src.get("ref", ""), name=name, channel=channel,
                store_source=src.get("store_source", ""))
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
        if action == "install":
            # One-click store install (spec 2.6): the request names an
            # app id and at most a source id; what gets installed is
            # decided by resolving it against the CONFIGURED store
            # sources, here on the host. A request may pick among the
            # sources the server_admin chose; it can never add one.
            src, _listed_version, store_src = _store_lookup(
                name, req.get("source_id", ""),
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
                msg = f"an instance named '{name}' already exists"
            elif not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
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
                with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w", encoding="utf-8") as f:
                    f.write(caddy_site(inst["port"], inst["routes"], inst["container"],
                                       inst["svc_port"], groups, name,
                                       throttle_of(inst)))
                refresh_generated_sites()
                reload_gateway()
                ok = True
                msg = ("visibility set to groups: " + ", ".join(groups)) if groups else "visibility set to all"
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
            # Own public hostname (RFC-0009). Validation runs here, with
            # the same function the CLI uses — the portal's copy of the
            # rules would only drift.
            if not inst:
                msg = "unknown instance"
            elif req.get("op") == "remove":
                old = inst.pop("address", "")
                save_registry(reg)
                write_instance_address_caddy()
                reload_gateway()
                ok = True
                msg = f"address {old} removed" if old else "no address was set"
            else:
                try:
                    host = check_instance_address(reg, name, inst,
                                                  req.get("hostname", ""))
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
                    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w",
                              encoding="utf-8") as f:
                        f.write(caddy_site(inst["port"], inst["routes"],
                                           inst["container"], inst["svc_port"],
                                           (inst.get("visibility") or {}).get("groups"),
                                           name, throttle_of(inst)))
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
        elif not inst or name not in tokens:
            msg = "unknown instance or no deploy token"
        elif inst["channel"] != "test":
            msg = "not a test instance"
        elif not inst.get("source") or inst["source"].get("kind") not in ("git", "local"):
            msg = "no usable package source recorded"
        else:
            src = inst["source"]
            revision = _resolve_revision(src)
            ok, msg = run_install(src, "test")
        version = (load_registry()["instances"].get(name) or {}).get("version", "")
        via = {"install": "store", "visibility": "portal",
               "tile": "portal",
               "config": "portal", "token": "portal",
               "address": "portal", "throttle": "portal",
               "remove": "portal", "create": "portal",
               "source": "portal", "node": "setup wizard"}.get(action, "deploy-hook")
        record = {"instance": name or "(dieser Knoten)", "ok": ok,
                  "message": msg, "revision": revision,
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
        if rid:
            res_tmp = os.path.join(results, f"{rid}.tmp")
            with open(res_tmp, "w", encoding="utf-8") as f:
                json.dump({"ok": ok, "message": msg, "revision": revision,
                           "version": version}, f)
            os.replace(res_tmp, os.path.join(results, f"{rid}.json"))
        os.remove(req_path)
        print(f"deploy {name}: {'OK' if ok else 'FAILED'} — {msg}")


# --------------------------------------------- backup & restore (oaap.data.backup)

def cmd_backup(args):
    """Offline-consistent platform backup: one self-contained archive."""
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

    print("Offline-consistent backup: app containers are stopped for the copy")
    print("and restarted right after (core services keep running).")
    running = run(["docker", "ps", "-q", "--filter", "name=^oaap-app-"]).stdout.split()
    stage = tempfile.mkdtemp(prefix="oaap-backup-")
    t0 = time.monotonic()
    try:
        if running:
            run(["docker", "stop", *running])
        with open(os.path.join(stage, "backup-manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        tmp_out = out_path + ".tmp"
        run(["tar", "--numeric-owner", "-czpf", tmp_out,
             "--exclude=data/identity/login-throttle.json",
             # node profiles stay with the machine (RFC-0011 decision 4):
             # a 'dev' backup restored onto a production box must not
             # bring developer powers along. The manifest still records
             # them so the operator can set them again deliberately.
             "--exclude=apps/node.json",
             "-C", stage, "backup-manifest.json",
             "-C", DATA_DIR, "app/.env", "apps", "data/identity"])
        os.chmod(tmp_out, 0o600)
        os.replace(tmp_out, out_path)
    finally:
        if running:
            subprocess.run(["docker", "start", *running], capture_output=True, text=True)
        shutil.rmtree(stage, ignore_errors=True)
    downtime = time.monotonic() - t0
    size = os.path.getsize(out_path)
    size_h = f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"
    print(f"Backup written: {out_path} ({size_h}, "
          f"{len(manifest['instances'])} app instance(s), app downtime {downtime:.0f}s)")
    print("SECURITY: this file contains ALL platform secrets, password hashes")
    print("and app data — guard it like a master key (permissions are 0600).")
    print("Restore on a prepared machine with: sudo ./install.sh restore <file>")


def _deploy_from_registry(name, inst):
    """Bring one restored instance back: image, container, gateway site.

    Returns False (with an explanation) when the instance cannot come
    back automatically; its data stays restored either way.
    """
    image = inst["image"]
    if image.startswith("oaap-app/"):
        src = inst.get("source") or {}
        tmp = None
        pkg = ""
        try:
            if src.get("kind") == "git":
                tmp = tempfile.mkdtemp(prefix="oaap-restore-")
                print(f"Fetching {src['url']} ...")
                branch = ["--branch", src["ref"]] if src.get("ref") else []
                run(["git", "clone", "--depth", "1", *branch, src["url"], tmp])
                pkg = os.path.join(tmp, src.get("path") or "")
            elif src.get("kind") == "local":
                pkg = os.path.join(src.get("url", ""), src.get("path") or "")
            if not pkg or not os.path.isdir(pkg):
                print(f"SKIPPED {name}: image {image} must be rebuilt, but its package "
                      f"source is not available on this machine "
                      f"({src.get('url') or 'no source recorded'}). Data is restored — "
                      f"copy the package here or reinstall it under the same name.")
                return False
            print(f"Building {image} ...")
            run(["docker", "build", "-q", "-t", image,
                 os.path.join(pkg, inst.get("build") or ".")])
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"Pulling {image} ...")
        run(["docker", "pull", "-q", image])

    if not os.path.isfile(env_path(name)):
        print(f"SKIPPED {name}: no instance.env in the restored data.")
        return False
    container = inst["container"]
    start_instance_container(name, container, image, inst.get("storage"))
    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w", encoding="utf-8") as f:
        f.write(caddy_site(inst["port"], inst["routes"], container, inst["svc_port"],
                           (inst.get("visibility") or {}).get("groups"), name,
                           throttle_of(inst)))
    print(f"Restored '{name}' ({inst['app_name']} {inst['version']}, "
          f"channel {inst['channel']}, port {inst['port']})")
    # The instance's own public address travels with it (RFC-0009): it
    # belongs to the app, not to the machine. Say so out loud — on a new
    # machine that name still points at the OLD one until DNS is moved.
    if inst.get("address"):
        print(f"         own address {inst['address']} came along — it must be "
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


def cmd_restore_instances(_args):
    """Used by 'install.sh restore': re-create every registered instance."""
    _report_dropped_profiles()
    reg = load_registry()
    if not reg["instances"]:
        print("No app instances in the restored registry.")
        return
    ok = skipped = 0
    for name, inst in sorted(reg["instances"].items()):
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
    sources, removed, migrated = load_sources()
    target = args.target or ""

    if args.action == "list":
        if not sources:
            print("No store sources configured. Add one with: sudo oaap store add-source <url>")
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
    pi.set_defaults(fn=cmd_install)
    pl = sub.add_parser("list")
    pl.set_defaults(fn=cmd_list)
    pr = sub.add_parser("remove")
    pr.add_argument("name")
    pr.add_argument("--purge", action="store_true")
    pr.set_defaults(fn=cmd_remove)
    pv = sub.add_parser("visibility")
    pv.add_argument("name")
    pv.add_argument("mode", choices=["all", "groups"])
    pv.add_argument("groups", nargs="?", default="",
                    help="comma-separated group tags, e.g. buero,finanzen (with 'groups')")
    pv.set_defaults(fn=cmd_visibility)
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
    pcf.set_defaults(fn=cmd_config)
    pa = sub.add_parser("address")
    pa.add_argument("action", choices=["show", "set", "remove"])
    pa.add_argument("name")
    pa.add_argument("hostname", nargs="?",
                    help="public hostname of its own, e.g. hub.example.org")
    pa.set_defaults(fn=cmd_address)
    pth = sub.add_parser("throttle")
    pth.add_argument("action", choices=["show", "set", "off"])
    pth.add_argument("name")
    pth.add_argument("rate", nargs="?",
                     help="<requests>/<seconds> per client address, e.g. 300/60")
    pth.set_defaults(fn=cmd_throttle)
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
    pt.set_defaults(fn=cmd_token)
    pd = sub.add_parser("process-deploys")
    pd.set_defaults(fn=cmd_process_deploys)
    pb = sub.add_parser("backup")
    pb.add_argument("action", choices=["create"])
    pb.add_argument("--to", default="", help="target directory or .tar.gz file (outside the data dir)")
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
                 or (args.cmd == "store" and args.action == "list"))
    if not read_only and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        die("requires root (sudo oaap app ...)")
    try:
        args.fn(args)
    except subprocess.CalledProcessError as e:
        die(f"command failed: {' '.join(e.cmd)}\n{e.stderr}")


if __name__ == "__main__":
    main()
