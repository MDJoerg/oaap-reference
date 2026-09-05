#!/usr/bin/env bash
# Bring an installed node in line with the code that is installed on it.
#
# WHY THIS IS A SEPARATE FILE. `oaap update` runs the update.sh that was
# on disk when the operator typed the command — the OLD one. Anything a
# new version adds after the copy step is therefore skipped by exactly
# the update that introduces it, and skipped forever after, because the
# next run exits early as "already up to date".
#
# Found on oaap-demo, 2026-08-09: it jumped 0.1.18 -> 0.1.26 in one go
# and got neither the store-source migration (RFC-0012 §4) nor the
# deploy-worker repair. Both had been written, tested and shipped. The
# node just never ran them, and nothing said so.
#
# So the steps live here and update.sh calls "$APP_DIR/migrate.sh" —
# a path that holds the NEW file by the time it is called. It is also
# called on the "already up to date" path, which is what heals a node
# that missed a step under the old scheme.
#
# EVERY STEP IN HERE MUST BE IDEMPOTENT AND QUIET WHEN THERE IS NOTHING
# TO DO. This runs on every `oaap update`, including the ones that
# change nothing.
set -euo pipefail

OAAP_DATA_DIR="${OAAP_DATA_DIR:-/var/lib/oaap}"
APP_DIR="$OAAP_DATA_DIR/app"

say() { printf '%s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "ERROR: migrate.sh requires root." >&2; exit 1; }

# --- the deploy worker's start rate limit (found on oaap-test, 0.1.22) ---
# The unit is written by the installer and was never touched again, so a
# node installed before that version keeps systemd's default limit: five
# starts in ten seconds. That is meant for services that crash-loop.
# This one is a queue drainer — every portal action starts it once, so a
# handful of clicks in a row looks exactly like a crash loop, systemd
# fails the watching path unit, and the node quietly stops processing
# ANY queued request until somebody resets it by hand.
UNIT=/etc/systemd/system/oaap-deployd.service
if [ -f "$UNIT" ] && ! grep -q '^StartLimitIntervalSec=' "$UNIT"; then
  say ""
  say "Repairing the deploy worker's start rate limit ..."
  if sed -i '/^Description=OAAP deploy worker/a StartLimitIntervalSec=0' "$UNIT"; then
    systemctl daemon-reload
    systemctl reset-failed oaap-deployd.service oaap-deployd.path >/dev/null 2>&1 || true
    systemctl start oaap-deployd.path >/dev/null 2>&1 || true
    say "  Done — queued requests are processed again after a burst of clicks."
  else
    say "  WARNING: could not update $UNIT."
  fi
fi

# --- key for identity's internal API (RFC-0015 addendum A4) ---
# Existing installations have no INTERNAL_API_KEY in their .env, and
# identity fails closed without one — so this step must run, and it must
# recreate the two services itself. `oaap update` calls migrate.sh AFTER
# `docker compose up -d`, so by the time we get here identity is already
# running with the new code and an empty key. Writing the value alone
# would change nothing until the next restart.
#
# The window this leaves is a few seconds of a portal that cannot manage
# users, on the node of an operator who is watching an update run. Login
# and app traffic are unaffected — they never touch /internal/*.
ENVF="$APP_DIR/.env"
if [ -f "$ENVF" ] && ! grep -q '^INTERNAL_API_KEY=' "$ENVF"; then
  say ""
  say "Securing identity's internal API (RFC-0015 A4) ..."
  umask 077
  printf 'INTERNAL_API_KEY=%s\n' \
    "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$ENVF"
  if docker compose --project-directory "$APP_DIR" --project-name oaap \
       up -d identity portal >/dev/null 2>&1; then
    say "  Done — /internal/* now requires the platform key."
  else
    say "  WARNING: the key was written but identity/portal could not be"
    say "  recreated. Run: docker compose --project-directory $APP_DIR \\"
    say "    --project-name oaap up -d identity portal"
  fi
fi

# --- per-app networks + gateway links (RFC-0016) ---
# Two jobs, both idempotent: isolate any app still on the flat platform
# network onto its own (one-time for apps installed before 0.1.30), and
# reconnect the gateway to EVERY app network. The second is not a
# one-time migration -- it must run on every update, because the compose
# `up -d` a few steps earlier RECREATED the gateway, which drops its
# manual network links. Without this, all apps would 502 after an update.
say ""
say "Checking app network isolation (RFC-0016) ..."
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" migrate-networks \
  2>&1 | sed 's/^/  /' || say "  WARNING: app network migration could not complete — check 'oaap status'."

# --- fleet status route on external sites (RFC-0021) ---
# The external site config is generated at 'oaap external set' time and
# never touched again. A node that registered its name before 0.1.41
# would serve /fleet/status under the external name into the catch-all
# (login redirect) instead of the key-checked route. Regenerate once;
# quiet forever after.
EXTC="$APP_DIR/apps-caddy/external.caddy"
if [ -f "$EXTC" ] && ! grep -q 'handle /fleet/\*' "$EXTC"; then
  say ""
  say "Adding the fleet status route to the external gateway sites (RFC-0021) ..."
  if OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 -c "import sys; sys.path.insert(0, '$APP_DIR'); import appctl; appctl.write_external_caddy()" >/dev/null \
     && docker restart oaap-gateway-1 >/dev/null; then
    say "  Done — /fleet/status answers under the external name too."
  else
    say "  WARNING: could not regenerate the external sites — run 'oaap external set <name>' once by hand."
  fi
fi

# --- shipped store sources (RFC-0012 §4) ---
# Sources used to be written once, at installation, and never touched
# again — so the day one of our lists moves, every node in the field
# strands, visibly only as an empty store. Reconcile carries a moved
# list along where the operator has not edited the URL, leaves it alone
# where they have, and says which. It also writes the id and trust class
# of entries that predate RFC-0012.
say ""
say "Checking store sources ..."
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" store reconcile \
  2>&1 | sed 's/^/  /' || say "  WARNING: store sources could not be checked."

# --- tenants (RFC-0022 stage 2, oaap.core.tenant 1.5) ---
# Give the node its default tenant and assign what already exists to it.
# Silent when there is nothing to do, which is every run after the first.
# It has to run BEFORE identity stamps its own users: identity reads the
# tenant store through a read-only mount and skips its migration while
# the file is not there yet.
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" migrate-tenants \
  2>&1 | sed 's/^/  /' || say "  WARNING: the tenant migration did not complete."

# The tenant audit log (oaap.core.tenant 1.7) is written by two
# processes -- this host and the identity container -- so the directory
# has to exist before either is asked to append to it. Created here
# rather than in install.sh alone, because every node in the field
# reaches this file by updating, not by installing.
mkdir -p "$OAAP_DATA_DIR/data/audit"

# --- instance data under its tenant (RFC-0026) ---
# The one migration here that moves DATA. Written to be interruptible:
# one instance at a time, moved with a rename (a directory-entry change
# within one filesystem, never a copy), the registry saved after each,
# nothing deleted. Each moved instance is recreated immediately, because
# a bind mount follows the inode until something restarts the container
# and Docker re-resolves the old path into an empty directory.
# Silent after the first run, like every step in here.
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" migrate-instance-dirs   2>&1 | sed 's/^/  /' || say "  WARNING: instance data could not be moved — check 'oaap app list'."

# --- the portal's view of the retained packages (oaap.apps.runtime 2.14) ---
# The packages moved into the tenant tree with RFC-0026, where the
# portal has no mount and must not get one. Since 0.1.76 the host lists
# them into apps/artifacts.json instead. Written here once, because a
# node that changes nothing after the update would otherwise keep an
# empty card until its next deployment -- which is exactly the state
# this fixes.
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" artifact-index   2>&1 | sed 's/^/  /' || say "  WARNING: the package index could not be written."

# --- the tenant boundary in the generated gateway sites (0.2, spec 3.1) ---
# The boundary is enforced at the gateway: every authenticated route
# carries its instance's tenant. Sites generated before 0.2 do not, so
# they are rewritten once. Quiet and idempotent afterwards -- and it
# runs before anyone can create a second tenant, which is what keeps
# there from being a window in which the boundary is merely intended.
say ""
say "Checking the tenant boundary in the gateway sites ..."
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" migrate-tenant-routes \
  2>&1 | sed 's/^/  /' || say "  WARNING: the gateway sites could not be rewritten — run 'oaap status'."
