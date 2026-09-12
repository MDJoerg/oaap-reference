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

# --- superuser secret for the managed Postgres (oaap.data.store 0.1) ---
# install.sh generates STORE_SUPERUSER_PASSWORD on every fresh install,
# whether or not the node carries the 'store' profile -- but a node that
# reaches 0.1.86 by UPDATING, not installing, has no such line in its
# .env at all. Found on oaap-test, 2026-09-09: the very first
# 'add-profile store' recreated the store service with an EMPTY
# password, which the official Postgres image refuses outright
# ("Database is uninitialized and superuser password is not
# specified") -- a clean crash loop, not a silent one, but a crash
# loop all the same, and on the first node that ever tried this step.
# Generated here unconditionally, exactly like install.sh, so the
# secret exists before anyone runs 'add-profile store' for the first
# time on an updated node.
if [ -f "$ENVF" ] && ! grep -q '^STORE_SUPERUSER_PASSWORD=' "$ENVF"; then
  say ""
  say "Adding the managed-Postgres superuser secret (oaap.data.store 0.1) ..."
  umask 077
  printf 'STORE_SUPERUSER_PASSWORD=%s\n' \
    "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$ENVF"
  say "  Done."
fi

# --- the event relay's broker secret (oaap.data.twin 0.3, RFC-0032 step 2) ---
# The outbox relay logs in to the broker as the platform principal
# 'oaap.relay' with this node secret (Jörg, 2026-09-12); identity checks
# it, the relay presents it, both read it from this .env. Generated
# unconditionally, like the store secret above, so it is in place the day
# a node gains 'broker'.
#
# The update's own 'up -d' ran BEFORE this file, with the line still
# missing: identity (and a relay, if running) came up with an EMPTY key
# and refuse the relay -- fail closed, but a relay that never publishes.
# Recreate exactly those two. '--no-deps' is not decoration: without it
# 'up -d relay' would also recreate 'broker' from docker-compose.yml
# alone and drop the raw device port of a node that carries 'exposed'.
if [ -f "$ENVF" ] && ! grep -q '^BROKER_RELAY_KEY=' "$ENVF"; then
  say ""
  say "Adding the event relay's broker secret (oaap.data.twin 0.3) ..."
  umask 077
  printf 'BROKER_RELAY_KEY=%s\n' \
    "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$ENVF"
  docker compose --project-directory "$APP_DIR" --project-name oaap \
    up -d --no-deps identity >/dev/null 2>&1 \
    || say "  WARNING: identity could not be recreated — run 'docker compose --project-directory $APP_DIR --project-name oaap up -d identity'."
  if grep -q '"broker"' "$OAAP_DATA_DIR/apps/node.json" 2>/dev/null; then
    docker compose --project-directory "$APP_DIR" --project-name oaap \
      --profile broker up -d --no-deps relay >/dev/null 2>&1 \
      || say "  WARNING: the relay could not be recreated — see 'docker compose ps'."
  fi
  say "  Done."
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

# --- the schedule as systemd actually holds it (RFC-0029 D1) ---
# Since 0.1.78 backup-schedule.json is a VIEW derived from the timer,
# not a note the installer left behind. Written once here so a node that
# changes nothing after the update still shows the truth -- and so a
# schedule someone edited by hand on the machine appears in the portal.
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" backup schedule --refresh   2>&1 | sed 's/^/  /' || say "  WARNING: the backup schedule could not be read."

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

# --- the rehearsal sweep timer (RFC-0030 D4) ---
# A rehearsal holds a COPY OF LIVE CUSTOMER DATA and disappears on a
# date. On a node updated rather than freshly installed there is no
# timer to make that happen, and an expiry nothing enforces is a
# promise, not a mechanism. Written here so every node in the fleet
# gets it -- idempotent: the units are rewritten from the same text
# and enabling an enabled timer changes nothing.
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
  PYTHON3="$(command -v python3 || echo /usr/bin/python3)"
  if [ ! -f /etc/systemd/system/oaap-rehearsal-sweep.timer ]; then
    say ""
    say "Installing the rehearsal sweep (expired copies of production data) ..."
  fi
  cat > /etc/systemd/system/oaap-rehearsal-sweep.service <<EOF
[Unit]
Description=OAAP rehearsal sweep (removes expired rehearsal instances and their data)

[Service]
Type=oneshot
Environment=OAAP_DATA_DIR=$OAAP_DATA_DIR
ExecStart=$PYTHON3 $APP_DIR/appctl.py rehearsal sweep
EOF
  cat > /etc/systemd/system/oaap-rehearsal-sweep.timer <<'EOF'
[Unit]
Description=OAAP rehearsal sweep, daily

[Timer]
OnCalendar=*-*-* 04:20:00
# A node that was off at 04:20 still sweeps: an expired copy of
# production data must not survive because the machine slept.
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable --now oaap-rehearsal-sweep.timer >/dev/null 2>&1 \
    || say "  WARNING: the rehearsal sweep timer could not be enabled."
fi

# --- managed Postgres for a profiled node (RFC-0031 Schritt 1, oaap.data.store 0.1) ---
# 'oaap node add-profile store' already starts the service immediately
# (appctl.py cmd_node) -- this is the idempotent safety net for what
# that command cannot cover by itself: a node that gained the profile
# while appctl or docker was unavailable, a Compose recreate on update
# that does not pass --profile (services without one are simply left
# alone, per Compose's own semantics, so this step supplies it), or a
# future migration bug. Quiet when the profile is absent or the
# service already answers.
#
# Checked with `pg_isready`, NOT `docker inspect .State.Running` — found
# on oaap-test, 2026-09-09: a container stuck in a restart loop flickers
# between Running=true (the instant the process starts) and
# Running=false (waiting to retry), so a Running-based check can catch
# it in the wrong instant and skip the very repair it exists for.
# `pg_isready` asks the one question that actually matters here.
if [ -f "$OAAP_DATA_DIR/apps/node.json" ] \
   && grep -q '"store"' "$OAAP_DATA_DIR/apps/node.json" 2>/dev/null; then
  if ! docker exec oaap-store-1 pg_isready -U postgres >/dev/null 2>&1; then
    say ""
    say "Ensuring the managed Postgres (profile 'store') is up ..."
    if docker compose --project-directory "$APP_DIR" --project-name oaap \
         --profile store up -d store >/dev/null 2>&1; then
      say "  Done."
    else
      say "  WARNING: 'store' service could not be started — check 'docker compose ps'."
    fi
  fi
fi

# --- the digital twin service for a profiled node (RFC-0031 Schritt 3, oaap.data.twin 0.1) ---
# 'twin' is a new service in this version's compose file. A node that
# already carries 'store' AND already has Postgres running skips the
# check above entirely (pg_isready succeeds) -- without a check of its
# own, 'twin' would never be created on such a node's update. Checked
# by container presence, not a health endpoint: unlike Postgres, 'twin'
# has no data of its own to be healthy or not about; it either runs or
# it does not.
if [ -f "$OAAP_DATA_DIR/apps/node.json" ] \
   && grep -q '"store"' "$OAAP_DATA_DIR/apps/node.json" 2>/dev/null; then
  if [ -z "$(docker ps -q -f name=^oaap-twin-1$ -f status=running)" ]; then
    say ""
    say "Ensuring the digital twin service (oaap.data.twin 0.1) is up ..."
    if docker compose --project-directory "$APP_DIR" --project-name oaap \
         --profile store up -d twin >/dev/null 2>&1; then
      say "  Done."
    else
      say "  WARNING: 'twin' service could not be started — check 'docker compose ps'."
    fi
  fi
  # Every EXISTING tenant's twin_<id> schema gets whatever table or
  # grant this version's _twin_ensure_schema now adds (0.2's 'aliases'
  # table and its GRANT INSERT, Schritt 5) -- an already-provisioned
  # tenant otherwise has no way to reach that function again short of
  # an app redeploy that bumps its version. Found missing on oaap-test
  # while live-verifying Schritt 5, 2026-09-10; harmless to run on a
  # node with no twin schema yet (prints "No twin schemas to migrate.").
  if docker exec oaap-store-1 pg_isready -U postgres >/dev/null 2>&1; then
    say "Migrating existing twin schemas (oaap.data.twin) ..."
    OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" \
      data store migrate-twin 2>&1 | sed 's/^/  /' \
      || say "  WARNING: twin schemas could not be migrated."
  fi
fi

# --- the MQTT broker for a profiled node (RFC-0032 D2, oaap.events.broker 0.1) ---
# Same safety net as 'store'/'twin' above, and for the same reasons:
# 'oaap node add-profile broker' already starts it immediately
# (appctl.py cmd_node) -- this covers a Compose recreate that skips a
# profiled service on update, or a profile gained while docker was
# unavailable. Checked by container presence, like 'twin' -- the broker
# has no data of its own to be healthy or not about.
#
# Must pass the SAME file set as appctl.py's _broker_compose_files(),
# or a node holding both 'broker' and 'exposed' would silently lose the
# raw device port on every update -- Compose recreates the container
# from whatever '-f' files this call names, nothing more.
if [ -f "$OAAP_DATA_DIR/apps/node.json" ] \
   && grep -q '"broker"' "$OAAP_DATA_DIR/apps/node.json" 2>/dev/null; then
  BROKER_FILES=(-f "$APP_DIR/docker-compose.yml")
  if grep -q '"exposed"' "$OAAP_DATA_DIR/apps/node.json" 2>/dev/null; then
    BROKER_FILES+=(-f "$APP_DIR/docker-compose.broker-exposed.yml")
  fi
  if [ -z "$(docker ps -q -f name=^oaap-broker-1$ -f status=running)" ]; then
    say ""
    say "Ensuring the MQTT broker (profile 'broker') is up ..."
    if docker compose --project-directory "$APP_DIR" --project-name oaap \
         "${BROKER_FILES[@]}" --profile broker up -d broker >/dev/null 2>&1; then
      say "  Done."
    else
      say "  WARNING: 'broker' service could not be started — check 'docker compose ps'."
    fi
  elif [ "${#BROKER_FILES[@]}" -gt 2 ]; then
    # Running, but maybe WITHOUT its raw port: an update.sh older than
    # 0.1.98 recreated the broker from docker-compose.yml alone (that
    # update.sh is the one running this very update -- see this file's
    # header). With the same file set this is a no-op when the port is
    # already published, and puts it back when it is not.
    docker compose --project-directory "$APP_DIR" --project-name oaap \
      "${BROKER_FILES[@]}" --profile broker up -d --no-deps broker >/dev/null 2>&1 \
      || say "  WARNING: could not re-apply the broker's raw device port — check 'docker ps'."
  fi
  # The outbox relay rides on the same profile (oaap.data.twin 0.3). Same
  # safety net as the broker's, '--no-deps' so it never touches 'broker'.
  if [ -z "$(docker ps -q -f name=^oaap-relay-1$ -f status=running)" ]; then
    say ""
    say "Ensuring the event relay (profile 'broker') is up ..."
    if docker compose --project-directory "$APP_DIR" --project-name oaap \
         --profile broker up -d --no-deps relay >/dev/null 2>&1; then
      say "  Done."
    else
      say "  WARNING: 'relay' service could not be started — check 'docker compose ps'."
    fi
  fi
fi

# --- what a rehearsal would cost, where the portal can read it (2.15.3) ---
# The portal has no view of the tenant tree, so the sizes and the list of
# archives are written beside the registry. Written once here so a node
# that changes nothing after the update can still draw the form -- same
# reason as apps/artifacts.json above.
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" rehearsal-index   2>&1 | sed 's/^/  /' || say "  WARNING: the rehearsal view could not be written."
