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
