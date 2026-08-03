#!/usr/bin/env bash
#
# OAAP reference installer — implements oaap.core.host (bootstrap mode).
# Spec: oaap-spec/spec/oaap.core.host.md
#
# Usage:  sudo ./install.sh [bootstrap]
#
# Configuration via environment (all optional):
#   OAAP_HTTP_PORT   HTTP port of the gateway         (default: 80)
#   OAAP_DATA_DIR    platform state & app directory   (default: /var/lib/oaap)
#   OAAP_HOST        hostname/IP used in the setup URL (default: first local IP)

set -euo pipefail

MODE="${1:-bootstrap}"
OAAP_HTTP_PORT="${OAAP_HTTP_PORT:-80}"
OAAP_DATA_DIR="${OAAP_DATA_DIR:-/var/lib/oaap}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SCRIPT_DIR/VERSION")"
MARKER="$OAAP_DATA_DIR/.oaap-installed"

say()  { printf '%s\n' "$*"; }
fail() { say "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- mode gate
case "$MODE" in
  bootstrap) ;;
  uninstall)
    fail "Uninstall is a node command: run 'sudo oaap uninstall' (add --purge to also delete data)." ;;
  join|remote-join)
    fail "Mode '$MODE' is reserved (RFC-0003) and not yet available in $VERSION. Only 'bootstrap' is supported." ;;
  *)
    fail "Unknown mode '$MODE'. Supported: bootstrap (join/remote-join are reserved, see RFC-0003)." ;;
esac

# ---------------------------------------------------------------- os info
# Parse os-release in subshells — sourcing it directly would clobber our
# own variables (its VERSION overwrote the platform version once).
OS_ID="" OS_CODENAME="" OS_LIKE=""
if [ -r /etc/os-release ]; then
  OS_ID="$(. /etc/os-release && echo "${ID:-}")"
  OS_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
  OS_LIKE="$(. /etc/os-release && echo "${ID_LIKE:-}")"
fi

# ------------------------------------------- runtime provisioning (optional)
# Spec 2.2 step 1: the only hard prerequisite is a supported Linux with
# root access. If Docker is missing, offer to install it — never silently.
install_runtime() {
  case "$OS_ID" in
    debian|ubuntu) ;;
    *) say "Automatic Docker installation supports Debian and Ubuntu only — please install Docker manually (see README)."; return 1 ;;
  esac
  say "Installing Docker Engine from Docker's official APT repository ..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$OS_ID $OS_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker >/dev/null 2>&1 || true
  say "Docker Engine installed."
}

if [ "$(id -u)" -eq 0 ] && ! command -v docker >/dev/null 2>&1; then
  consent="${OAAP_INSTALL_RUNTIME:-}"
  if [ -z "$consent" ] && [ -t 0 ]; then
    read -r -p "Docker Engine is not installed. Install it now from Docker's official repository? [y/N] " answer
    case "$answer" in y|Y|yes|YES) consent=1 ;; esac
  fi
  if [ "$consent" = "1" ]; then
    install_runtime || true
  else
    say "Skipped Docker installation (confirm when asked, or set OAAP_INSTALL_RUNTIME=1)."
  fi
fi

# ---------------------------------------------------------------- preflight
# All checks run BEFORE anything is changed on the system (spec 2.2 step 2);
# a runtime installed above was an explicitly requested change.
errors=()
warnings=()

[ "$(id -u)" -eq 0 ] || errors+=("Must run as root (sudo ./install.sh).")

case "$OS_ID $OS_LIKE" in
  *debian*) : ;;  # Tier 1 (Debian) or Tier 2 (Ubuntu et al.), see ADR-0005
  *) warnings+=("Distribution '${OS_ID:-unknown}' is not Debian-based — Tier 3, community support only (ADR-0005).") ;;
esac

if command -v docker >/dev/null 2>&1; then
  docker info >/dev/null 2>&1 || errors+=("Docker is installed but the daemon is not reachable (is it running?).")
  docker compose version >/dev/null 2>&1 || errors+=("Docker Compose v2 plugin is missing ('docker compose' does not work).")
else
  errors+=("Docker Engine is not installed. Re-run and confirm the automatic installation, or set OAAP_INSTALL_RUNTIME=1.")
fi

free_kb="$(df -Pk "$(dirname "$OAAP_DATA_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
[ "${free_kb:-0}" -ge 2097152 ] || errors+=("Less than 2 GB free disk space at $(dirname "$OAAP_DATA_DIR").")

if command -v ss >/dev/null 2>&1; then
  if ss -ltn "( sport = :$OAAP_HTTP_PORT )" 2>/dev/null | grep -q LISTEN; then
    errors+=("Port $OAAP_HTTP_PORT is already in use (set OAAP_HTTP_PORT to change it).")
  fi
fi

# Idempotence (spec test 5): never touch an existing installation.
[ -e "$MARKER" ] && fail "An OAAP platform is already installed at $OAAP_DATA_DIR. Nothing was changed. (Remove it deliberately before reinstalling.)"

for w in "${warnings[@]:-}"; do [ -n "$w" ] && say "WARNING: $w"; done
if [ "${#errors[@]}" -gt 0 ]; then
  say ""
  say "Preflight failed — the system was NOT changed:"
  for e in "${errors[@]}"; do say "  - $e"; done
  exit 1
fi
say "Preflight OK."

# ---------------------------------------------------------------- install
APP_DIR="$OAAP_DATA_DIR/app"
mkdir -p "$APP_DIR" "$OAAP_DATA_DIR/data/identity" "$OAAP_DATA_DIR/apps"
cp -r "$SCRIPT_DIR/platform/." "$APP_DIR/"
cp "$SCRIPT_DIR/VERSION" "$APP_DIR/VERSION"

# App-site directory for gateway listeners (oaap.apps.runtime); the
# placeholder keeps Caddy's import glob happy before the first app.
mkdir -p "$APP_DIR/apps-caddy"
[ -f "$APP_DIR/apps-caddy/00-init.caddy" ] || echo "# app sites are generated here by appctl.py" > "$APP_DIR/apps-caddy/00-init.caddy"

# appctl.py needs PyYAML (Debian/Ubuntu package; best effort elsewhere)
if command -v apt-get >/dev/null 2>&1 && ! python3 -c "import yaml" >/dev/null 2>&1; then
  apt-get install -y -qq python3-yaml || say "WARNING: could not install python3-yaml — 'oaap app' will not work until it is present."
fi

# Secrets: generated locally, random, unique per installation (spec 2.2 step 3).
gen_secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
SETUP_TOKEN="$(gen_secret)"
SESSION_SECRET="$(gen_secret)"

umask 077
cat > "$APP_DIR/.env" <<EOF
OAAP_VERSION=$VERSION
OAAP_HTTP_PORT=$OAAP_HTTP_PORT
OAAP_DATA_DIR=$OAAP_DATA_DIR
SESSION_SECRET=$SESSION_SECRET
SETUP_TOKEN=$SETUP_TOKEN
EOF

say "Building and starting core services (gateway, identity, portal) ..."
docker compose --project-directory "$APP_DIR" --project-name oaap build --quiet
docker compose --project-directory "$APP_DIR" --project-name oaap up -d

# Node CLI (spec 2.3)
install -m 0755 "$SCRIPT_DIR/bin/oaap" /usr/local/bin/oaap

date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"

# ---------------------------------------------------------------- handover
if [ -z "${OAAP_HOST:-}" ]; then
  OAAP_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$OAAP_HOST" ] || OAAP_HOST="localhost"
fi
PORT_SUFFIX=""
[ "$OAAP_HTTP_PORT" != "80" ] && PORT_SUFFIX=":$OAAP_HTTP_PORT"

say ""
say "=============================================================="
say " OAAP $VERSION is running."
say ""
say " Finish setup in your browser (creates the first admin user):"
say ""
say "   URL:          http://$OAAP_HOST$PORT_SUFFIX/setup"
say "   Setup token:  $SETUP_TOKEN"
say ""
say " The token is valid once, until the first admin exists."
say " Lost this output? Show it again with:  sudo oaap setup-token"
say " Check this node anytime with:  oaap status"
say "=============================================================="
