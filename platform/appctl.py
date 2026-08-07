#!/usr/bin/env python3
"""OAAP app runtime, increment 1 (oaap.apps.runtime, spec draft 0.1).

Host-side app manager invoked via `oaap app ...`:

    oaap app install <package-dir> [--name NAME] [--channel production|test]
    oaap app list
    oaap app remove <name> [--purge]

Implements: manifest validation (subset of the published JSON Schema),
build on device, named instances with channels, per-instance storage/
secret/port, gateway wiring (generated Caddy site + reload).
Limitations of increment 1: exactly one service per app; no portal
tiles yet; role `public` supported but discouraged.
"""

import argparse
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
DEPLOY_TOKENS = os.path.join(APPS_DIR, "deploy-tokens.json")
DEPLOY_LOG = os.path.join(APPS_DIR, "deploy-log.jsonl")
SPOOL_DIR = os.path.join(DATA_DIR, "data", "deploy-spool")
PORT_RANGE = range(8100, 8200)
ROLES = {"admin", "keyuser", "user", "guest", "partner", "public"}
GATEWAY_CONTAINER = "oaap-gateway-1"


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


def validate_manifest(m):
    """Minimal validation mirroring oaap-spec/schema/oaap-app.schema.json."""
    errs = []
    if m.get("oaap_manifest") != "0.1":
        errs.append("oaap_manifest must be \"0.1\"")
    app = m.get("app") or {}
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", str(app.get("id", ""))):
        errs.append("app.id: lowercase [a-z0-9-], 3-40 chars")
    if not re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?", str(app.get("version", ""))):
        errs.append("app.version: semver required")
    if app.get("type") not in ("native", "image", "wrapped"):
        errs.append("app.type: native | image | wrapped")
    if not app.get("name"):
        errs.append("app.name: required")
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


def site_body(routes, container, svc_port):
    """Shared handler block for one app instance (LAN and external sites)."""
    lines = []
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
            lines.append("\t\tforward_auth identity:8000 {")
            lines.append(f"\t\t\turi /verify?roles={','.join(sorted(set(roles)))}")
            lines.append("\t\t\tcopy_headers X-OAAP-User X-OAAP-Roles")
            lines.append("\t\t}")
        else:
            # Public route: nothing overwrites the headers, so strip
            # client-sent identity headers explicitly (contract guarantee 1).
            lines.append("\t\trequest_header -X-OAAP-User")
            lines.append("\t\trequest_header -X-OAAP-Roles")
        lines.append(f"\t\treverse_proxy {container}:{svc_port}")
        lines.append("\t}")
    if not any(r["path"] == "/" for r in routes):
        lines.append("\thandle {")
        lines.append("\t\trespond 404")
        lines.append("\t}")
    return lines


def caddy_site(port, routes, container, svc_port):
    """Generate a LAN gateway listener for one app instance."""
    lines = [f":{port} {{"] + site_body(routes, container, svc_port) + ["}"]
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
        lines += site_body(routes, inst["container"], inst["svc_port"])
        lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return skipped


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
        write_external_caddy()
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
        lines.append(f"\treverse_proxy {target}")
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

    inst_dir = os.path.join(APPS_DIR, name)
    env_path = os.path.join(inst_dir, "instance.env")
    os.makedirs(inst_dir, exist_ok=True)

    # stable per-instance secret, never inside storage mounts
    env = {}
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            env = dict(l.strip().split("=", 1) for l in f if "=" in l)
    env.setdefault("OAAP_APP_SECRET", secrets.token_hex(32))
    for c in m.get("config") or []:
        env.setdefault(c["key"], c.get("default", ""))

    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())

    # per-instance storage, writable for the container user (guarantee 4)
    uid = image_uid(image)
    mounts = []
    for s in m.get("storage") or []:
        host = os.path.join(inst_dir, "storage", s["name"])
        os.makedirs(host, exist_ok=True)
        if uid is not None:
            os.chown(host, uid, uid)
        mounts += ["-v", f"{host}:{s['mount']}"]

    container = f"oaap-app-{name}"
    subprocess.run(["docker", "rm", "-f", container],
                   capture_output=True, text=True)
    run(["docker", "run", "-d", "--name", container,
         "--restart", "unless-stopped", "--network", "oaap_default",
         "--env-file", env_path, *mounts, image])

    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w", encoding="utf-8") as f:
        f.write(caddy_site(port, m["routes"], container, svc["port"]))
    reload_gateway()

    reg["instances"][name] = {
        "app_id": app["id"], "app_name": app["name"],
        "version": app["version"], "channel": channel,
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
        "description": app.get("description", ""),
        # roles that may see/open the app — the portal filters tiles
        # with this; the gateway enforces it regardless (spec 2.5)
        "roles": sorted({r for rt in m["routes"] for r in rt["roles"] if r != "public"}),
    }
    save_registry(reg)
    if channel == "production":
        # moving to production invalidates any deploy token (spec 2.5)
        drop_token(name, "instance is on the production channel")
    if load_external():
        write_external_caddy()
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


def cmd_remove(args):
    reg = load_registry()
    inst = reg["instances"].pop(args.name, None)
    if not inst:
        die(f"no instance named '{args.name}'")
    subprocess.run(["docker", "rm", "-f", inst["container"]],
                   capture_output=True, text=True)
    site = os.path.join(CADDY_APPS_DIR, f"{args.name}.caddy")
    if os.path.isfile(site):
        os.remove(site)
    reload_gateway()
    save_registry(reg)
    drop_token(args.name, "instance removed")
    if args.purge:
        import shutil
        shutil.rmtree(os.path.join(APPS_DIR, args.name), ignore_errors=True)
        print(f"Removed '{args.name}' including data.")
    else:
        print(f"Removed '{args.name}'. Data kept at {os.path.join(APPS_DIR, args.name)}.")


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


def _store_lookup(app_id):
    """Resolve an app id against the CONFIGURED store sources (spec 2.6).

    The spool request names only the app id — the source of truth for
    what gets installed is this host-side lookup, never the request.
    Returns (source_dict, version) or (None, "") if no configured
    source lists the app.
    """
    import urllib.request

    try:
        with open(STORE_SOURCES, encoding="utf-8") as f:
            sources = json.load(f).get("sources", [])
    except (OSError, ValueError):
        return None, ""
    for src in sources:
        try:
            with urllib.request.urlopen(src["url"], timeout=5) as r:
                data = json.load(r)
        except Exception:
            continue
        for a in data.get("apps", []):
            pkg = a.get("package") or {}
            if a.get("id") == app_id and pkg.get("git"):
                return ({"kind": "git", "url": pkg["git"],
                         "path": pkg.get("path", ""),
                         "ref": pkg.get("ref", "")},
                        a.get("version", ""))
    return None, ""


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
                ref=src.get("ref", ""), name=name, channel=channel)
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
        if action == "install":
            # One-click store install (spec 2.6): the request names only
            # the app id; what gets installed is decided by resolving it
            # against the CONFIGURED store sources, here on the host.
            src, _listed_version = _store_lookup(name)
            if not src:
                msg = "app is not listed in any configured store source"
            else:
                revision = _resolve_revision(src)
                channel = inst["channel"] if inst else "production"
                ok, msg = run_install(src, channel)
                if ok:
                    msg = "installed from store"
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
        audit_deploy({"instance": name, "ok": ok, "message": msg,
                      "revision": revision, "version": version,
                      "via": "store" if action == "install" else "deploy-hook"})
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

    inst_dir = os.path.join(APPS_DIR, name)
    env_path = os.path.join(inst_dir, "instance.env")
    if not os.path.isfile(env_path):
        print(f"SKIPPED {name}: no instance.env in the restored data.")
        return False
    uid = image_uid(image)
    mounts = []
    for s in inst.get("storage") or []:
        host = os.path.join(inst_dir, "storage", s["name"])
        os.makedirs(host, exist_ok=True)
        if uid is not None:
            os.chown(host, uid, uid)
        mounts += ["-v", f"{host}:{s['mount']}"]

    container = inst["container"]
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)
    run(["docker", "run", "-d", "--name", container,
         "--restart", "unless-stopped", "--network", "oaap_default",
         "--env-file", env_path, *mounts, image])
    with open(os.path.join(CADDY_APPS_DIR, f"{name}.caddy"), "w", encoding="utf-8") as f:
        f.write(caddy_site(inst["port"], inst["routes"], container, inst["svc_port"]))
    print(f"Restored '{name}' ({inst['app_name']} {inst['version']}, "
          f"channel {inst['channel']}, port {inst['port']})")
    return True


def cmd_restore_instances(_args):
    """Used by 'install.sh restore': re-create every registered instance."""
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
    write_external_caddy()
    reload_gateway()
    print(f"App instances: {ok} restored, {skipped} skipped.")


def cmd_store(args):
    """Manage store sources (list URLs the portal's Store page reads)."""
    try:
        with open(STORE_SOURCES, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"sources": []}
    sources = data.get("sources", [])

    if args.action == "list":
        if not sources:
            print("No store sources configured. Add one with: sudo oaap store add-source <url>")
        for i, s in enumerate(sources, 1):
            name = s.get("name") or "(unbenannt)"
            print(f"{i}. {name} — {s['url']}")
        return
    if not args.url:
        die(f"'{args.action}' needs a URL (or index for remove-source)")
    if args.action == "add-source":
        if any(s["url"] == args.url for s in sources):
            die("this source is already configured")
        sources.append({"url": args.url, "name": args.name or ""})
        print(f"Store source added ({args.url}).")
    elif args.action == "remove-source":
        before = len(sources)
        if args.url.isdigit() and 1 <= int(args.url) <= len(sources):
            sources.pop(int(args.url) - 1)
        else:
            sources = [s for s in sources if s["url"] != args.url]
        if len(sources) == before:
            die("no matching source")
        print("Store source removed.")
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp = STORE_SOURCES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sources": sources}, f, indent=2)
    os.replace(tmp, STORE_SOURCES)


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
    ps.add_argument("action", choices=["list", "add-source", "remove-source"])
    ps.add_argument("url", nargs="?")
    ps.add_argument("--name")
    ps.set_defaults(fn=cmd_store)
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
    if args.cmd != "convert" and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        die("requires root (sudo oaap app ...)")
    try:
        args.fn(args)
    except subprocess.CalledProcessError as e:
        die(f"command failed: {' '.join(e.cmd)}\n{e.stderr}")


if __name__ == "__main__":
    main()
