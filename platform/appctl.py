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
import subprocess
import sys

import yaml

DATA_DIR = os.environ.get("OAAP_DATA_DIR", "/var/lib/oaap")
APP_DIR = os.path.join(DATA_DIR, "app")            # platform installation
APPS_DIR = os.path.join(DATA_DIR, "apps")          # app instances
CADDY_APPS_DIR = os.path.join(APP_DIR, "apps-caddy")
REGISTRY = os.path.join(APPS_DIR, "registry.json")
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


def caddy_site(port, routes, container, svc_port):
    """Generate a gateway listener for one app instance."""
    lines = [f":{port} {{"]
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
    lines.append("}")
    return "\n".join(lines) + "\n"


def reload_gateway():
    run(["docker", "exec", GATEWAY_CONTAINER, "caddy", "reload",
         "--config", "/etc/caddy/Caddyfile"])


def cmd_install(args):
    pkg = os.path.abspath(args.package)
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
        "description": app.get("description", ""),
        # roles that may see/open the app — the portal filters tiles
        # with this; the gateway enforces it regardless (spec 2.5)
        "roles": sorted({r for rt in m["routes"] for r in rt["roles"] if r != "public"}),
    }
    save_registry(reg)
    print(f"Installed '{name}' ({app['name']} {app['version']}, channel {channel})")
    print(f"Entry point: port {port} (through the gateway, login required)")


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
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
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


def main():
    p = argparse.ArgumentParser(prog="oaap app")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install")
    pi.add_argument("package")
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
    args = p.parse_args()
    if args.cmd != "convert" and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        die("requires root (sudo oaap app ...)")
    try:
        args.fn(args)
    except subprocess.CalledProcessError as e:
        die(f"command failed: {' '.join(e.cmd)}\n{e.stderr}")


if __name__ == "__main__":
    main()
