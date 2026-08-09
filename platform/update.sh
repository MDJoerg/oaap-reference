#!/usr/bin/env bash
# OAAP platform update engine — CLI trigger of oaap.core.updates 0.1.
# Spec: oaap-spec/spec/oaap.core.updates.md
#
# Invoked via `sudo oaap update [--check]`. Always updates from the
# RECORDED platform source (spec 2.2) — no trigger can supply another
# source. Build before switch: a failed image build leaves the old
# containers running.
set -euo pipefail

# Applying an update overwrites THIS file — run from a temp copy so
# bash never reads a half-replaced script.
if [ "${OAAP_UPDATE_REEXEC:-}" != "1" ]; then
  tmp="$(mktemp /tmp/oaap-update.XXXXXX.sh)"
  cp "$0" "$tmp"
  OAAP_UPDATE_REEXEC=1 exec bash "$tmp" "$@"
fi
trap 'rm -f "$0"' EXIT

OAAP_DATA_DIR="${OAAP_DATA_DIR:-/var/lib/oaap}"
APP_DIR="$OAAP_DATA_DIR/app"
SRC_DEFAULT="$OAAP_DATA_DIR/platform-src"
UPDATE_LOG="/var/log/oaap-update.log"

say()  { printf '%s\n' "$*"; }
fail() { say "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Requires root (sudo oaap update)."
[ -e "$OAAP_DATA_DIR/.oaap-installed" ] || fail "OAAP is not installed on this node ($OAAP_DATA_DIR)."
command -v git >/dev/null 2>&1 || fail "git is required (apt install git)."

CHECK=0
case "${1:-}" in
  --check) CHECK=1 ;;
  "") ;;
  *) fail "Usage: oaap update [--check]" ;;
esac

# Transcript (spec 2.3) — no secrets in here, but keep it tidy.
if touch "$UPDATE_LOG" 2>/dev/null; then
  chmod 600 "$UPDATE_LOG"
  exec > >(tee -a "$UPDATE_LOG") 2>&1
fi
say "== oaap update $(date -u +%Y-%m-%dT%H:%M:%SZ) $([ "$CHECK" -eq 1 ] && echo '(check)')"

# ------------------------------------------------ recorded source (2.2)
ENVF="$APP_DIR/.env"
# tolerate missing keys (existing installations predate these entries)
get_env() { grep "^$1=" "$ENVF" 2>/dev/null | head -1 | cut -d= -f2- || true; }
set_env() {
  if grep -q "^$1=" "$ENVF" 2>/dev/null; then
    sed -i "s|^$1=.*|$1=$2|" "$ENVF"
  else
    echo "$1=$2" >> "$ENVF"
  fi
}

REPO="$(get_env OAAP_PLATFORM_REPO)"; REPO="${REPO:-https://github.com/MDJoerg/oaap-reference}"
REF="$(get_env OAAP_PLATFORM_REF)";   REF="${REF:-main}"
SRC="$(get_env OAAP_PLATFORM_SOURCE)"

# root may operate on a working copy owned by the admin user
g() { git -c safe.directory="$SRC" -C "$SRC" "$@"; }

usable() { [ -n "$1" ] && [ -d "$1/.git" ]; }

if ! usable "$SRC"; then
  SRC="$SRC_DEFAULT"
  if ! usable "$SRC"; then
    # Medium-installed node (payload without .git): acquire a working
    # copy from the recorded repository (spec 2.2, conformance test 7).
    say "No git working copy recorded — cloning $REPO ($REF) to $SRC ..."
    rm -rf "$SRC"
    git clone --branch "$REF" "$REPO" "$SRC" 2>&1 | tail -1
  fi
  set_env OAAP_PLATFORM_SOURCE "$SRC"
fi

cur_ver="$(cat "$APP_DIR/VERSION" 2>/dev/null || echo '?')"
cur_rev="$(cat "$APP_DIR/REVISION" 2>/dev/null || echo '')"

say "Recorded source: $SRC ($REPO @ $REF)"
g fetch --quiet origin "$REF" || fail "fetch failed — is the network up and the recorded repository reachable?"
new_rev="$(g rev-parse --short "origin/$REF")"
new_ver="$(g show "origin/$REF:VERSION" 2>/dev/null | tr -d '[:space:]' || echo '?')"

say ""
say "  Installed:  $cur_ver (${cur_rev:-unknown revision})"
say "  Available:  $new_ver ($new_rev)"

if [ -n "$cur_rev" ] && [ "$cur_rev" = "$new_rev" ]; then
  # Self-heal: engines before 0.1.2 did not sync OAAP_VERSION into the
  # compose env, so the portal kept showing the old version. Repair
  # the display when it drifted (fault repair, not a normal no-op).
  env_ver="$(get_env OAAP_VERSION)"
  if [ "$CHECK" -eq 0 ] && [ -n "$cur_ver" ] && [ "$env_ver" != "$cur_ver" ]; then
    say ""
    say "Repairing the version display ($env_ver -> $cur_ver) ..."
    set_env OAAP_VERSION "$cur_ver"
    docker compose --project-directory "$APP_DIR" --project-name oaap up -d >/dev/null 2>&1 || true
  fi
  say ""
  say "Already up to date — nothing changed."
  exit 0
fi

say ""
say "Changes:"
range="origin/$REF"
if [ -n "$cur_rev" ] && g rev-parse -q --verify "$cur_rev^{commit}" >/dev/null 2>&1; then
  range="$cur_rev..origin/$REF"
  g log --oneline --no-decorate "$range" | sed 's/^/  /'
else
  say "  (no installed revision recorded — showing the latest commits)"
  g log --oneline --no-decorate -5 "origin/$REF" | sed 's/^/  /'
fi

if [ "$CHECK" -eq 1 ]; then
  say ""
  say "Check only — nothing was changed. Apply with: sudo oaap update"
  exit 0
fi

# ------------------------------------------------------------ apply (2.3)
say ""
g merge --ff-only "origin/$REF" >/dev/null 2>&1 \
  || fail "The working copy $SRC has local changes/commits and cannot fast-forward. Resolve there (git status) or remove OAAP_PLATFORM_SOURCE from $ENVF to use a platform-owned clone."

caddy_before="$(md5sum "$APP_DIR/Caddyfile" 2>/dev/null | cut -d' ' -f1)"

say "Copying platform files ..."
# cp keeps inodes — safe for the Caddyfile file bind-mount.
cp -r "$SRC/platform/." "$APP_DIR/"
cp "$SRC/VERSION" "$APP_DIR/VERSION"
# The portal shows OAAP_VERSION from the compose .env — keep it in
# sync, or the UI keeps reporting the old version after an update.
set_env OAAP_VERSION "$new_ver"
install -m 0755 "$SRC/bin/oaap" /usr/local/bin/oaap

say "Building core service images (the running services stay up) ..."
if ! docker compose --project-directory "$APP_DIR" --project-name oaap build --quiet; then
  fail "Image build failed — the platform keeps running on $cur_ver. Nothing was restarted."
fi

say "Restarting core services ..."
docker compose --project-directory "$APP_DIR" --project-name oaap up -d

caddy_after="$(md5sum "$APP_DIR/Caddyfile" 2>/dev/null | cut -d' ' -f1)"
if [ "$caddy_before" != "$caddy_after" ]; then
  say "Caddyfile changed — restarting the gateway ..."
  docker restart oaap-gateway-1 >/dev/null
fi

echo "$new_rev" > "$APP_DIR/REVISION"

# Shipped store sources (RFC-0012 §4). Sources used to be written once,
# at installation, and never touched again — so the day one of our lists
# moves, every node in the field strands, visibly only as an empty
# store. Reconcile carries a moved list along where the operator has not
# edited the URL, leaves it alone where they have, and says which.
# The deploy worker's unit is written by the installer and was never
# touched again, so a node installed before this version keeps systemd's
# default start rate limit — and with it the failure found on oaap-test:
# a handful of portal actions in a row look like a crash loop, systemd
# fails the watching path unit, and the node quietly stops processing
# ANY queued request until someone resets it by hand. Repair it here,
# and clear the failed state if this node already fell into it.
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

say ""
say "Checking store sources ..."
OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" store reconcile \
  2>&1 | sed 's/^/  /' || say "  WARNING: store sources could not be checked."

# ------------------------------------------------------------- verify
sleep 3
total="$(docker compose --project-directory "$APP_DIR" --project-name oaap ps --services | wc -l)"
running="$(docker compose --project-directory "$APP_DIR" --project-name oaap ps --services --filter status=running | wc -l)"
say ""
if [ "$running" -eq "$total" ] && [ "$total" -gt 0 ]; then
  say "Update complete: $cur_ver (${cur_rev:-?}) -> $new_ver ($new_rev). Core services: $running/$total running."
else
  say "WARNING: update applied ($new_ver, $new_rev), but only $running/$total core services are running — check 'oaap status' and 'docker compose logs'."
  exit 1
fi
