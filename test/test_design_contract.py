#!/usr/bin/env python3
"""The gateway serves one shared stylesheet; nothing else changes for
an app (RFC-0035, App Design Contract Teil A).

No new service, no new container -- Caddy serves a static file
directly. This file checks the three places that make that true
(the file itself, the gateway route, the volume that gets it there),
the same source-text style `test_data_twin.py` uses for its own
gateway route. What it cannot check without Docker: that a request to
the running gateway for /platform/theme.css actually returns 200 with
these variables -- that belongs on `oaap-test`.

Run: python3 test/test_design_contract.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


def read(*parts):
    with open(os.path.join(PLATFORM, *parts), encoding="utf-8") as f:
        return f.read()


theme_css = read("static", "theme.css")
caddy_src = read("Caddyfile")
compose_src = read("docker-compose.yml")

print("=== theme.css: the variables RFC-0035 §1.1 names ===")
REQUIRED_VARS = [
    "--oaap-color-bg", "--oaap-color-surface", "--oaap-color-text",
    "--oaap-color-text-muted", "--oaap-color-primary",
    "--oaap-color-primary-text", "--oaap-color-border",
    "--oaap-color-danger", "--oaap-color-success", "--oaap-font-family",
    "--oaap-font-size-base", "--oaap-space-1", "--oaap-space-2",
    "--oaap-space-3", "--oaap-space-4", "--oaap-radius",
    "--oaap-header-height",
]
for var in REQUIRED_VARS:
    ok(f"defines {var}", re.search(rf"{re.escape(var)}\s*:", theme_css) is not None)

print("\n=== theme.css: the D4 header convention (a class, not a layout) ===")
ok("a '.oaap-header' class exists for the mini header bar",
   ".oaap-header" in theme_css)
ok("no sidebar, tile or grouping CLASS -- Part B (RFC-0036), not here "
   "(a mention of 'navigation' in a comment is fine; a rule is not)",
   not re.search(r"\.(oaap-)?(sidebar|nav|tile)\b", theme_css))

print("\n=== Caddyfile: /platform/* is a public, static route ===")
ok("/platform/* is listed among the public routes in the header comment",
   "/platform/*" in caddy_src.split("Everything else requires")[0])
platform_block = caddy_src.split("handle /platform/*")[1].split("\n\thandle")[0]
ok("strips identity headers like every other public route",
   "-X-OAAP-User" in platform_block and "-X-OAAP-Roles" in platform_block)
ok("serves files directly -- no reverse_proxy, no app-facing service",
   "file_server" in platform_block and "reverse_proxy" not in platform_block)
ok("strips the /platform prefix before looking the file up",
   "strip_prefix /platform" in platform_block)

print("\n=== docker-compose.yml: the gateway mounts the static directory ===")
gateway_block = compose_src.split("gateway:")[1].split("\n  ")[0]
ok("./static is mounted read-only into the gateway container",
   "./static:/etc/caddy/static:ro" in compose_src)

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
import sys
sys.exit(1 if fail_n else 0)
