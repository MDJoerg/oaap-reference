#!/usr/bin/env bash
#
# OAAP reference installer — implements oaap.core.host (bootstrap mode).
# Spec: oaap-spec/spec/oaap.core.host.md
#
# Usage:  sudo ./install.sh [bootstrap|prepare|restore <backup.tar.gz>]
#
#   bootstrap  install a new platform on this machine (default)
#   prepare    server readiness only (keep-awake, static address);
#              re-runnable, also on machines that are already installed
#   restore    recreate a platform from an 'oaap backup create' archive
#              (oaap.data.backup) — no setup wizard, users come along
#
# Configuration via environment (all optional):
#   OAAP_HTTP_PORT    HTTP port of the gateway         (default: 80)
#   OAAP_DATA_DIR     platform state & app directory   (default: /var/lib/oaap)
#   OAAP_HOST         hostname/IP used in the setup URL (default: first local IP)
#   OAAP_SERVER_MODE  1 = apply keep-awake without asking, 0 = skip
#   OAAP_STATIC_IP    current = pin the current address, <address> = use
#                     that address, skip = leave DHCP untouched
#   OAAP_ADMIN_SUDO   1 = set up sudo for the invoking user without
#                     asking (fresh netinstall via 'su'), 0 = skip
#   OAAP_WLAN_WATCHDOG 1 = on a wireless node, install the reconnect
#                     watchdog without asking, 0 = skip
#   OAAP_SETUP_TOKEN  pre-generated setup token (install medium);
#                     default: generated here

set -euo pipefail
# apt must never try to ask questions here — under systemd (firstboot)
# there is no stdin, and "dpkg-preconfigure: unable to re-open stdin"
# was the first visible line on the console (alpha.6 boot test).
export DEBIAN_FRONTEND=noninteractive

MODE="${1:-bootstrap}"
RESTORE_FILE="${2:-}"
OAAP_HTTP_PORT_EXPLICIT="${OAAP_HTTP_PORT:-}"
OAAP_HTTP_PORT="${OAAP_HTTP_PORT:-80}"
OAAP_DATA_DIR="${OAAP_DATA_DIR:-/var/lib/oaap}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SCRIPT_DIR/VERSION")"
MARKER="$OAAP_DATA_DIR/.oaap-installed"

say()  { printf '%s\n' "$*"; }
fail() { say "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- transcript
# Mirror the full run into /var/log/oaap-install.log (best effort) —
# the feedback channel for installs that go wrong on foreign hardware.
# Contains the setup token, hence root-only permissions.
INSTALL_LOG="/var/log/oaap-install.log"
if [ "$(id -u)" -eq 0 ] && touch "$INSTALL_LOG" 2>/dev/null; then
  chmod 600 "$INSTALL_LOG"
  exec > >(tee -a "$INSTALL_LOG") 2>&1
  say "== install.sh $VERSION mode=$MODE $(date -u +%Y-%m-%dT%H:%M:%SZ) — transcript: $INSTALL_LOG"
fi

# ---------------------------------------------------------------- mode gate
case "$MODE" in
  bootstrap|prepare|restore) ;;
  uninstall)
    fail "Uninstall is a node command: run 'sudo oaap uninstall' (add --purge to also delete data)." ;;
  join|remote-join)
    fail "Mode '$MODE' is reserved (RFC-0003) and not yet available in $VERSION. Only 'bootstrap' is supported." ;;
  *)
    fail "Unknown mode '$MODE'. Supported: bootstrap, prepare, restore (join/remote-join are reserved, see RFC-0003)." ;;
esac

# Restore keeps the backed-up gateway port unless the caller overrides it —
# read it early so the preflight checks the right port.
if [ "$MODE" = "restore" ] && [ -z "$OAAP_HTTP_PORT_EXPLICIT" ] && [ -r "$RESTORE_FILE" ]; then
  bp="$(tar -xzOf "$RESTORE_FILE" app/.env 2>/dev/null | grep '^OAAP_HTTP_PORT=' | cut -d= -f2- || true)"
  [ -n "$bp" ] && OAAP_HTTP_PORT="$bp"
fi

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

if [ "$MODE" != "prepare" ] && [ "$(id -u)" -eq 0 ] && ! command -v docker >/dev/null 2>&1; then
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

# ------------------------------------------- server readiness (spec 2.2 step 2)
# A platform node must behave like a server: never sleep, keep its
# address, and have a working admin path. Consumer hardware and default
# installs often have none of that (a Debian netinstall with a root
# password ships without sudo). Every change needs explicit consent;
# 'prepare' runs only this part.

admin_access() {
  # Running as root via 'su' on a fresh netinstall: offer to set up
  # sudo for the regular user so that all later docs ('sudo oaap ...')
  # just work. The target user is whoever owns the installer directory.
  user="${SUDO_USER:-}"
  [ -n "$user" ] || user="$(stat -c %U "$SCRIPT_DIR" 2>/dev/null || true)"
  [ -n "$user" ] && [ "$user" != "root" ] || return 0
  needs=""
  command -v sudo >/dev/null 2>&1 || needs="install sudo"
  if command -v sudo >/dev/null 2>&1 && ! id -nG "$user" 2>/dev/null | grep -qw sudo; then
    needs="${needs:+$needs and }add '$user' to the sudo group"
  fi
  [ -n "$needs" ] || return 0
  consent="${OAAP_ADMIN_SUDO:-}"
  if [ -z "$consent" ] && [ -t 0 ]; then
    say ""
    say "This system has no working 'sudo' path for user '$user' yet."
    read -r -p "Set it up now ($needs)? [Y/n] " answer
    case "$answer" in n|N|no|NO) consent=0 ;; *) consent=1 ;; esac
  fi
  if [ "$consent" != "1" ]; then
    say "Admin access: skipped (set OAAP_ADMIN_SUDO=1 to apply it non-interactively)."
    return 0
  fi
  command -v sudo >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq sudo; }
  usermod -aG sudo "$user"
  say "Admin access: '$user' is in the sudo group (takes effect at the next login)."
}

keep_awake() {
  if [ "$(systemctl is-enabled sleep.target 2>/dev/null)" = "masked" ]; then
    say "Keep-awake: already configured (sleep targets are masked)."
    return 0
  fi
  consent="${OAAP_SERVER_MODE:-}"
  if [ -z "$consent" ] && [ -t 0 ]; then
    say ""
    say "Mini-PCs and laptops often suspend after a while — a server must not."
    read -r -p "Keep this machine permanently awake (recommended)? [Y/n] " answer
    case "$answer" in n|N|no|NO) consent=0 ;; *) consent=1 ;; esac
  fi
  if [ "$consent" != "1" ]; then
    say "Keep-awake: skipped (set OAAP_SERVER_MODE=1 to apply it non-interactively)."
    return 0
  fi
  systemctl mask --now sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
  mkdir -p /etc/systemd/logind.conf.d
  cat > /etc/systemd/logind.conf.d/oaap-server.conf <<'EOF'
# OAAP server mode: never sleep on lid close or power/suspend keys.
# Rollback: delete this file and run
#   systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target
[Login]
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
  systemctl kill -s HUP systemd-logind >/dev/null 2>&1 || true
  say "Keep-awake: suspend/hibernate disabled, lid close and power key are ignored."
}

wlan_resilience() {
  # A wireless node must not lose the network permanently over one
  # failed handshake. Real incident (Raspberry Pi 400, 2026-08-07/08):
  # roaming inside a mesh network, the 4-way handshake fails
  # occasionally (IEEE reason 17); NetworkManager reads that as a wrong
  # password, asks for credentials, gets no answer on a machine without
  # a screen, and gives up for good after two minutes
  # ("failed (reason 'no-secrets')"). It then never retries on its own.
  # The node was offline twice in two days, once for 38 hours, while
  # the machine itself ran the whole time.
  #
  # Two changes, neither of which touches the radio link itself:
  #   1. unlimited autoconnect retries — remove the dead end
  #   2. a two-minute watchdog that pings the default gateway and
  #      brings the connection back when it does not answer
  # This does not fix the cause (access points and NetworkManager
  # disagreeing); it stops one bad handshake from locking a headless
  # node out of its own network.
  defroute="$(ip -4 route show default 2>/dev/null | head -1)"
  [ -n "$defroute" ] || return 0
  iface="$(printf '%s\n' "$defroute" | awk '{for(i=1;i<NF;i++) if($i=="dev") print $(i+1)}')"
  [ -n "$iface" ] && [ -d "/sys/class/net/$iface/wireless" ] || return 0
  systemctl is-active --quiet NetworkManager 2>/dev/null && command -v nmcli >/dev/null 2>&1 \
    || { say "WLAN resilience: $iface is wireless but not managed by NetworkManager — skipped."; return 0; }
  if [ "$(systemctl is-enabled oaap-wlan-watchdog.timer 2>/dev/null)" = "enabled" ]; then
    say "WLAN resilience: already configured (oaap-wlan-watchdog.timer is enabled)."
    return 0
  fi
  conn="$(nmcli -g GENERAL.CONNECTION device show "$iface" 2>/dev/null || true)"
  gateway="$(printf '%s\n' "$defroute" | awk '{for(i=1;i<NF;i++) if($i=="via") print $(i+1)}')"
  # The watchdog decides by "does the gateway answer a ping". A router
  # that drops ICMP would make it reconnect every two minutes forever —
  # so prove the assumption here instead of shipping a reconnect loop.
  if [ -z "$gateway" ] || ! ping -c 2 -W 3 "$gateway" >/dev/null 2>&1; then
    say "WLAN resilience: the default gateway (${gateway:-none}) does not answer a"
    say "ping, so a ping-based watchdog would reconnect endlessly — skipped."
    return 0
  fi

  consent="${OAAP_WLAN_WATCHDOG:-}"
  if [ -z "$consent" ] && [ -t 0 ]; then
    say ""
    say "This node is on WLAN ($iface). A single failed handshake can make"
    say "NetworkManager give up permanently — on a machine without a screen"
    say "that means offline until someone walks over to it."
    read -r -p "Install the WLAN watchdog (recommended)? [Y/n] " answer
    case "$answer" in n|N|no|NO) consent=0 ;; *) consent=1 ;; esac
  fi
  if [ "$consent" != "1" ]; then
    say "WLAN resilience: skipped (set OAAP_WLAN_WATCHDOG=1 to apply it non-interactively)."
    return 0
  fi

  [ -n "$conn" ] && nmcli connection modify "$conn" connection.autoconnect-retries 0 >/dev/null 2>&1 || true
  printf 'OAAP_WLAN_IFACE=%s\nOAAP_WLAN_CONN=%s\n' "$iface" "$conn" > /etc/default/oaap-wlan-watchdog
  cat > /usr/local/sbin/oaap-wlan-watchdog.sh <<'EOF'
#!/bin/sh
# OAAP WLAN watchdog — see 'Stable network' in oaap.core.host 2.2.
# Checks whether the default gateway still answers and brings the
# wireless connection back when it does not. Does nothing while the
# network is fine. Rollback:
#   systemctl disable --now oaap-wlan-watchdog.timer
#   rm /usr/local/sbin/oaap-wlan-watchdog.sh /etc/default/oaap-wlan-watchdog \
#      /etc/systemd/system/oaap-wlan-watchdog.{service,timer}
set -u
[ -r /etc/default/oaap-wlan-watchdog ] && . /etc/default/oaap-wlan-watchdog
IFACE="${OAAP_WLAN_IFACE:-}"
CONN="${OAAP_WLAN_CONN:-}"

gw="$(ip -4 route show default | awk '{print $3; exit}')"
# Two pings, so a single lost packet does not trigger a reconnect.
if [ -n "$gw" ] && ping -c 2 -W 3 "$gw" >/dev/null 2>&1; then
    exit 0
fi

logger -t oaap-wlan-watchdog "network unreachable (gateway '${gw:-none}') - bringing the WLAN connection back"
if [ -n "$CONN" ] && nmcli connection up "$CONN" >/dev/null 2>&1; then
    logger -t oaap-wlan-watchdog "connection '$CONN' restored"
elif [ -n "$IFACE" ] && nmcli device connect "$IFACE" >/dev/null 2>&1; then
    logger -t oaap-wlan-watchdog "device '$IFACE' reconnected"
else
    logger -t oaap-wlan-watchdog "recovery failed - retrying on the next run"
fi
EOF
  chmod 755 /usr/local/sbin/oaap-wlan-watchdog.sh
  cat > /etc/systemd/system/oaap-wlan-watchdog.service <<'EOF'
[Unit]
Description=OAAP WLAN watchdog (bring the wireless connection back)
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/oaap-wlan-watchdog.sh
EOF
  cat > /etc/systemd/system/oaap-wlan-watchdog.timer <<'EOF'
[Unit]
Description=Check every two minutes that this node still has network

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable --now oaap-wlan-watchdog.timer >/dev/null 2>&1 || true
  say "WLAN resilience: unlimited reconnect attempts${conn:+ for '$conn'} and a"
  say "two-minute watchdog installed (journalctl -t oaap-wlan-watchdog)."
}

static_ip() {
  # Detect the primary interface and its current IPv4 configuration.
  defroute="$(ip -4 route show default 2>/dev/null | head -1)"
  [ -n "$defroute" ] || { say "Stable address: no default route found — skipped."; return 0; }
  iface="$(printf '%s\n' "$defroute" | awk '{for(i=1;i<NF;i++) if($i=="dev") print $(i+1)}')"
  # Wireless interfaces (Raspberry Pi over WLAN, laptops): our simple
  # static rewrite breaks wpa_supplicant/NetworkManager setups — leave
  # the network configuration untouched (2026-08-06, aborted installs
  # on two WLAN machines).
  if [ -d "/sys/class/net/$iface/wireless" ]; then
    say "Stable address: $iface is a WLAN interface — leaving the network"
    say "configuration untouched. For a fixed address, reserve one in your"
    say "router instead (Fritzbox: 'Diesem Netzwerkgerät immer die gleiche"
    say "IPv4-Adresse zuweisen')."
    return 0
  fi
  gateway="$(printf '%s\n' "$defroute" | awk '{for(i=1;i<NF;i++) if($i=="via") print $(i+1)}')"
  cidr="$(ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{print $4; exit}')"
  [ -n "$iface" ] && [ -n "$gateway" ] && [ -n "$cidr" ] || { say "Stable address: could not detect the network setup — skipped."; return 0; }
  curip="${cidr%/*}"; prefix="${cidr#*/}"
  dns="$(awk '/^nameserver/ {print $2}' /etc/resolv.conf 2>/dev/null | grep -v '^127\.' | tr '\n' ' ' | sed 's/ $//' || true)"
  [ -n "$dns" ] || dns="$gateway"

  # Which tool owns this interface, and is it on DHCP?
  method=""
  if systemctl is-active --quiet NetworkManager 2>/dev/null && command -v nmcli >/dev/null 2>&1; then
    nm_con="$(nmcli -g GENERAL.CONNECTION device show "$iface" 2>/dev/null || true)"
    if [ -n "$nm_con" ]; then
      method=nm
      if [ "$(nmcli -g ipv4.method connection show "$nm_con" 2>/dev/null)" = "manual" ]; then
        say "Stable address: already configured ($curip is static via NetworkManager)."
        return 0
      fi
    fi
  elif [ -d /etc/netplan ] && ls /etc/netplan/*.y*ml >/dev/null 2>&1; then
    method=netplan
    if [ -f /etc/netplan/99-oaap-static.yaml ]; then
      say "Stable address: already configured (/etc/netplan/99-oaap-static.yaml)."
      return 0
    fi
  elif grep -qs "iface $iface inet static" /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null; then
    say "Stable address: already configured ($curip is static in /etc/network/interfaces)."
    return 0
  elif grep -qs "iface $iface inet dhcp" /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null; then
    method=ifupdown
  fi
  [ -n "$method" ] || { say "Stable address: could not determine how $iface is configured — please set a static address manually or reserve one in your router."; return 0; }

  choice="${OAAP_STATIC_IP:-}"
  if [ -z "$choice" ] && [ -t 0 ]; then
    say ""
    say "Network: $iface gets its address via DHCP — currently $curip/$prefix (gateway $gateway)."
    say "A server should keep a fixed address; with DHCP it can change after a reboot."
    read -r -p "Make the address permanent? [Enter = keep $curip / type another address / n = leave DHCP] " answer
    case "$answer" in ""|y|Y|yes|YES) choice=current ;; n|N|no|NO) choice=skip ;; *) choice="$answer" ;; esac
  fi
  case "$choice" in
    ""|skip|0)
      say "Stable address: skipped (set OAAP_STATIC_IP=current or an address to apply it non-interactively)."
      return 0 ;;
    current) newip="$curip" ;;
    *) newip="$choice" ;;
  esac

  if [ "$newip" != "$curip" ]; then
    command -v python3 >/dev/null 2>&1 || { say "Stable address: python3 is needed to validate a custom address — keeping DHCP."; return 0; }
    python3 -c "import ipaddress,sys; ipaddress.ip_address(sys.argv[1])" "$newip" 2>/dev/null \
      || { say "Stable address: '$newip' is not a valid IPv4 address — keeping DHCP."; return 0; }
    python3 -c "import ipaddress,sys; sys.exit(0 if ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_interface(sys.argv[2]).network else 1)" "$newip" "$cidr" \
      || { say "Stable address: $newip is not inside this network ($cidr) — keeping DHCP."; return 0; }
    # Reserved addresses can NOT be checked by ping alone (routers and
    # hosts with firewalls often don't answer) — refuse them outright.
    if [ "$newip" = "$gateway" ]; then
      say "Stable address: $newip is the gateway's address — keeping DHCP."
      return 0
    fi
    python3 -c "import ipaddress,sys; n=ipaddress.ip_interface(sys.argv[2]).network; a=ipaddress.ip_address(sys.argv[1]); sys.exit(1 if a in (n.network_address, n.broadcast_address) else 0)" "$newip" "$cidr" \
      || { say "Stable address: $newip is the network/broadcast address — keeping DHCP."; return 0; }
    # Best effort only: a silent machine may still own the address.
    if ping -c1 -W1 "$newip" >/dev/null 2>&1; then
      say "Stable address: $newip already answers on the network (in use) — keeping DHCP."
      return 0
    fi
  fi

  case "$method" in
    nm)
      nmcli connection modify "$nm_con" ipv4.method manual \
        ipv4.addresses "$newip/$prefix" ipv4.gateway "$gateway" ipv4.dns "$dns"
      if [ "$newip" = "$curip" ]; then
        nmcli connection up "$nm_con" >/dev/null 2>&1 || true
      else
        say "The new address $newip becomes active at the next boot — please reboot, then reach the machine at $newip."
      fi
      say "Stable address: $newip/$prefix set via NetworkManager (rollback: nmcli connection modify \"$nm_con\" ipv4.method auto)."
      ;;
    netplan)
      dns_list="$(printf '%s' "$dns" | sed 's/ /, /g')"
      cat > /etc/netplan/99-oaap-static.yaml <<EOF
# Written by the OAAP installer (server readiness).
# Rollback: delete this file, then run 'netplan apply'.
network:
  version: 2
  ethernets:
    $iface:
      dhcp4: false
      addresses: [$newip/$prefix]
      routes:
        - to: default
          via: $gateway
      nameservers:
        addresses: [$dns_list]
EOF
      chmod 600 /etc/netplan/99-oaap-static.yaml
      if [ "$newip" = "$curip" ]; then
        netplan apply >/dev/null 2>&1 || true
      else
        say "The new address $newip becomes active at the next boot — please reboot, then reach the machine at $newip."
      fi
      say "Stable address: $newip/$prefix set via netplan (/etc/netplan/99-oaap-static.yaml)."
      ;;
    ifupdown)
      ts="$(date +%Y%m%d-%H%M%S)"
      files="$(grep -ls "iface $iface inet dhcp" /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null || true)"
      for f in $files; do
        cp -a "$f" "$f.oaap-backup-$ts"
        sed -i "s|^[[:space:]]*iface $iface inet dhcp.*|iface $iface inet static\n    address $newip/$prefix\n    gateway $gateway\n    dns-nameservers $dns|" "$f"
        say "Stable address: $newip/$prefix written to $f (backup: $f.oaap-backup-$ts)."
      done
      # Keep name resolution alive: once the interface is static, no
      # DHCP client maintains /etc/resolv.conf anymore — and Debian's
      # dhcpcd even empties it at boot. Pin the resolvers we detected
      # (resolv.conf.head is dhcpcd's supported override hook).
      if [ ! -L /etc/resolv.conf ]; then
        { for d in $dns; do echo "nameserver $d"; done; } > /etc/resolv.conf.head
        { echo "# Written by the OAAP installer (static address; see interfaces backup)."
          for d in $dns; do echo "nameserver $d"; done; } > /etc/resolv.conf
      fi
      if [ "$newip" = "$curip" ]; then
        say "The address stays the same; the static setting takes effect at the next boot."
      else
        # Never switch the address live: it would cut remote sessions
        # and, if the address is silently taken, strand the machine.
        say "The new address $newip becomes active at the next boot — please reboot, then reach the machine at $newip."
      fi
      ;;
  esac
}

if [ "$(id -u)" -eq 0 ]; then
  # Server readiness is best effort: a failure here (odd network tool,
  # exotic hardware) must never abort the whole installation — under
  # `set -e` an unguarded call would (2026-08-06: nmcli on a WLAN
  # machine killed the bootstrap mid-run).
  admin_access || say "WARNING: admin access step failed — set up sudo manually if needed."
  keep_awake   || say "WARNING: keep-awake step failed — check power settings manually."
  static_ip    || say "WARNING: stable-address step failed — keeping the current network configuration (DHCP)."
  wlan_resilience || say "WARNING: WLAN resilience step failed — a wireless node may stay offline after a failed handshake."
elif [ "$MODE" = "prepare" ]; then
  fail "Must run as root (sudo ./install.sh prepare)."
fi

if [ "$MODE" = "prepare" ]; then
  say ""
  say "Server preparation finished. (Re-run 'sudo ./install.sh prepare' anytime.)"
  exit 0
fi

# ---------------------------------------------------------------- preflight
# All checks run BEFORE anything is changed on the system (spec 2.2 step 3);
# runtime/server-readiness changes above were explicitly consented.
errors=()
warnings=()

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    errors+=("Must run as root (sudo ./install.sh).")
  else
    # Fresh Debian netinstall with a root password: no sudo on board.
    errors+=("Must run as root, and 'sudo' is not installed yet (normal on a fresh Debian). Run:  su -c \"bash $(basename "$0")\"  — the installer then offers to set up sudo for your user.")
  fi
fi

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

if [ "$MODE" = "restore" ]; then
  if [ -z "$RESTORE_FILE" ]; then
    errors+=("Mode 'restore' needs the backup file: sudo ./install.sh restore <backup.tar.gz>")
  elif [ ! -r "$RESTORE_FILE" ]; then
    errors+=("Cannot read backup file '$RESTORE_FILE'.")
  elif ! tar -tzf "$RESTORE_FILE" backup-manifest.json >/dev/null 2>&1; then
    errors+=("'$RESTORE_FILE' is not an OAAP backup (no backup-manifest.json inside).")
  fi
fi

# Idempotence (spec test 5) and restore protection (backup spec test 4):
# never touch an existing installation.
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
mkdir -p "$APP_DIR" "$OAAP_DATA_DIR/data/identity" "$OAAP_DATA_DIR/apps" \
         "$OAAP_DATA_DIR/data/gateway/logs" "$OAAP_DATA_DIR/data/gateway/caddy-data" \
         "$OAAP_DATA_DIR/data/audit"
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

# Secrets: generated locally, random, unique per installation (spec 2.2 step 5).
# OAAP_SETUP_TOKEN may be pre-generated by the install medium (which
# hands it to the user, e.g. as a file on the USB stick) — it is still
# random per installation, never a default credential.
gen_secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
SETUP_TOKEN="${OAAP_SETUP_TOKEN:-$(gen_secret)}"
SESSION_SECRET="$(gen_secret)"
# Proves to identity that a caller of its /internal/* API is the portal
# (RFC-0015 addendum A4). Unlike the two above it carries no state, so a
# fresh one is always fine — including on restore.
INTERNAL_API_KEY="$(gen_secret)"

if [ "$MODE" = "restore" ]; then
  say "Restoring platform state from $RESTORE_FILE ..."
  tar --numeric-owner -xzpf "$RESTORE_FILE" -C "$OAAP_DATA_DIR" app/.env apps data/identity
  tar -xzOf "$RESTORE_FILE" backup-manifest.json > "$OAAP_DATA_DIR/last-restore-manifest.json"
  # No silent secret regeneration (oaap.data.backup §4): carry the old
  # ones over — sessions and the identity store stay consistent.
  SESSION_SECRET="$(grep '^SESSION_SECRET=' "$APP_DIR/.env" | cut -d= -f2- || true)"
  SETUP_TOKEN="$(grep '^SETUP_TOKEN=' "$APP_DIR/.env" | cut -d= -f2- || true)"
  [ -n "$SESSION_SECRET" ] && [ -n "$SETUP_TOKEN" ] || fail "The backup's app/.env is incomplete — cannot restore."
  BACKUP_VERSION="$(grep -o '"platform_version": *"[^"]*"' "$OAAP_DATA_DIR/last-restore-manifest.json" | cut -d'"' -f4 || true)"
  [ -z "$BACKUP_VERSION" ] || [ "$BACKUP_VERSION" = "$VERSION" ] \
    || say "NOTE: the backup was taken on platform version $BACKUP_VERSION; this installer is $VERSION."
fi

umask 077
# Recorded platform source (oaap.core.updates 2.2): `oaap update`
# always pulls from here; a non-git source (install medium payload)
# makes the update engine clone the repository on first use.
PLATFORM_REPO="${OAAP_PLATFORM_REPO:-https://github.com/MDJoerg/oaap-reference}"
PLATFORM_REF="${OAAP_PLATFORM_REF:-main}"
cat > "$APP_DIR/.env" <<EOF
OAAP_VERSION=$VERSION
OAAP_HTTP_PORT=$OAAP_HTTP_PORT
OAAP_DATA_DIR=$OAAP_DATA_DIR
OAAP_PLATFORM_REPO=$PLATFORM_REPO
OAAP_PLATFORM_REF=$PLATFORM_REF
OAAP_PLATFORM_SOURCE=$SCRIPT_DIR
SESSION_SECRET=$SESSION_SECRET
SETUP_TOKEN=$SETUP_TOKEN
INTERNAL_API_KEY=$INTERNAL_API_KEY
EOF
# Baseline for `oaap update`: which revision is installed right now.
git -c safe.directory="$SCRIPT_DIR" -C "$SCRIPT_DIR" rev-parse --short HEAD > "$APP_DIR/REVISION" 2>/dev/null \
  || rm -f "$APP_DIR/REVISION"

# Store sources (RFC-0012 §2/§4). The sources this version ships are
# defined once, in appctl.py — 'store reconcile' writes them here and
# keeps them in step later, including when one of our lists moves.
# Both our lists ship enabled, the platform list first (RFC-0012
# decision 1); Jörgs decision of 2026-08-06 — a plain git install comes
# with a working store — still holds and now covers both.
#
# OAAP_STORE_SOURCES (install medium's oaap-setup.env or the
# environment) adds further lists as comma-separated URLs; they are
# 'unverified' and take a confirmation at every install. Opt out of the
# shipped ones with OAAP_STORE_SOURCES=none.
if [ "${OAAP_STORE_SOURCES:-}" = "none" ]; then
  say "Store sources: none preconfigured (OAAP_STORE_SOURCES=none)."
else
  OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" store reconcile \
    | sed 's/^/  /' || say "WARNING: could not preconfigure store sources."
  for u in $(printf '%s' "${OAAP_STORE_SOURCES:-}" | tr ',' ' '); do
    OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" store add-source "$u" \
      >/dev/null 2>&1 || say "WARNING: store source '$u' was not added."
  done
  say "Store sources preconfigured."
fi

# Containers must not start before the network has DNS (DHCP boot race
# — suspected cause of 'store source unreadable' right after a reboot):
# make network-online.target real for ifupdown; docker-ce already
# Wants/After network-online.target.
if [ -f /lib/systemd/system/ifupdown-wait-online.service ] \
   || [ -f /usr/lib/systemd/system/ifupdown-wait-online.service ]; then
  systemctl enable ifupdown-wait-online.service >/dev/null 2>&1 || true
fi

say "Building and starting core services (gateway, identity, portal) ..."
docker compose --project-directory "$APP_DIR" --project-name oaap build --quiet
docker compose --project-directory "$APP_DIR" --project-name oaap up -d

# Node CLI (spec 2.3)
install -m 0755 "$SCRIPT_DIR/bin/oaap" /usr/local/bin/oaap

# Deploy-hook worker (oaap.apps.runtime 2.5): the portal queues deploy
# requests in the spool; a systemd path unit runs the host-side worker
# the moment a request arrives.
mkdir -p "$OAAP_DATA_DIR/data/deploy-spool/queue" "$OAAP_DATA_DIR/data/deploy-spool/results"
PYTHON3="$(command -v python3 || echo /usr/bin/python3)"
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
  cat > /etc/systemd/system/oaap-deployd.service <<EOF
[Unit]
Description=OAAP deploy worker (processes queued app deployments)
# systemd's default rate limit (5 starts in 10 s) is meant for services
# that crash-loop. This one is a queue drainer: every request in the
# portal starts it once, so a handful of clicks in a row looks exactly
# like a crash loop from the outside. When the limit hits, systemd puts
# the SERVICE and the watching PATH unit into 'failed' and stops looking
# at the queue at all -- the node then silently accepts requests and
# processes none of them until someone runs 'systemctl reset-failed' by
# hand. Found on oaap-test, 2026-08-09, with six portal actions in six
# seconds. The worker itself drains the whole queue per run and is
# harmless to start often, so the limit buys nothing here.
StartLimitIntervalSec=0

[Service]
Type=oneshot
Environment=OAAP_DATA_DIR=$OAAP_DATA_DIR
# git needs HOME for root's .gitconfig/.git-credentials (private
# package sources); systemd services get none by default
Environment=HOME=/root
ExecStart=$PYTHON3 $OAAP_DATA_DIR/app/appctl.py process-deploys
EOF
  cat > /etc/systemd/system/oaap-deployd.path <<EOF
[Unit]
Description=OAAP deploy queue watcher

[Path]
DirectoryNotEmpty=$OAAP_DATA_DIR/data/deploy-spool/queue

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now oaap-deployd.path >/dev/null 2>&1 || true
else
  say "WARNING: systemd not found — deploy-hook requests will queue up but nothing will process them."
fi

# Login greeting, console + SSH. The console banner uses agetty's \4
# escape — the shown address is expanded at every login prompt, so it
# survives DHCP address changes (alpha.4 boot test: stale IP in
# /etc/issue). The SSH/motd variant runs per login and reflects
# whether the setup wizard is still open.
mkdir -p /etc/issue.d
cat > /etc/issue.d/oaap.issue <<EOF

===============================================
 OAAP-Server ($VERSION)
 Portal:                  http://\4/
 Einrichtung (falls offen): http://\4/setup
 Setup-Token anzeigen:    sudo oaap setup-token
===============================================

EOF
mkdir -p /etc/update-motd.d
cat > /etc/update-motd.d/50-oaap <<EOF
#!/bin/sh
# OAAP login greeting (SSH + console via pam_motd) — state per login.
ip="\$(hostname -I 2>/dev/null | awk '{print \$1}')"
v="\$(cat "$OAAP_DATA_DIR/app/VERSION" 2>/dev/null)"
if [ -s "$OAAP_DATA_DIR/data/identity/users.json" ]; then
  echo ""
  echo "OAAP \${v:-?}  —  Portal:  http://\${ip:-<adresse>}/   (Status: oaap status)"
else
  echo ""
  echo "OAAP \${v:-?}  —  Einrichtung noch offen:  http://\${ip:-<adresse>}/setup"
  echo "Setup-Token anzeigen:  sudo oaap setup-token"
fi
EOF
chmod 755 /etc/update-motd.d/50-oaap

date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"

if [ "$MODE" = "restore" ]; then
  say "Restoring app instances ..."
  OAAP_DATA_DIR="$OAAP_DATA_DIR" python3 "$APP_DIR/appctl.py" restore-instances \
    || say "WARNING: some app instances could not be restored automatically — see the messages above."
fi

# ---------------------------------------------------------------- handover
if [ -z "${OAAP_HOST:-}" ]; then
  OAAP_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$OAAP_HOST" ] || OAAP_HOST="localhost"
fi
PORT_SUFFIX=""
[ "$OAAP_HTTP_PORT" != "80" ] && PORT_SUFFIX=":$OAAP_HTTP_PORT"

say ""
say "=============================================================="
if [ "$MODE" = "restore" ]; then
  say " OAAP $VERSION restored from backup."
  say ""
  say " Sign in with your existing users (no setup wizard):"
  say ""
  say "   URL:  http://$OAAP_HOST$PORT_SUFFIX/"
  EXT_HOST=""
  [ -f "$OAAP_DATA_DIR/apps/external.json" ] \
    && EXT_HOST="$(grep -o '"host": *"[^"]*"' "$OAAP_DATA_DIR/apps/external.json" | cut -d'"' -f4 || true)"
  if [ -n "$EXT_HOST" ]; then
    say ""
    say " External hostname '$EXT_HOST' is still registered. When this"
    say " machine shall take over: stop the old platform first, then point"
    say " DNS and the router's port forwarding (80/443) here — certificates"
    say " are re-issued automatically."
  fi
else
  say " OAAP $VERSION is running."
  say ""
  say " Finish setup in your browser (creates the first admin user):"
  say ""
  say "   URL:          http://$OAAP_HOST$PORT_SUFFIX/setup"
  say "   Setup token:  $SETUP_TOKEN"
  say ""
  say " The token is valid once, until the first admin exists."
  say " Lost this output? Show it again with:  sudo oaap setup-token"

  # The same handover data as a file — like the install stick's
  # oaap-setup.txt, but for manual installs: it lands in the invoking
  # user's home so nothing must be copied off the screen. (On stick
  # installs SUDO_USER is unset → /root/oaap-setup.txt, a fallback for
  # sticks pulled before the log was written.)
  SETUP_HOME="$(getent passwd "${SUDO_USER:-root}" 2>/dev/null | cut -d: -f6 || true)"
  { [ -n "$SETUP_HOME" ] && [ -d "$SETUP_HOME" ]; } || SETUP_HOME="/root"
  SETUP_FILE="$SETUP_HOME/oaap-setup.txt"
  if {
    echo "OAAP-Installation — Zugangsdaten fuer die Einrichtung"
    echo "====================================================="
    echo ""
    echo "Installiert am:          $(date '+%Y-%m-%d %H:%M')"
    echo "Rechnername:             $(hostname)"
    echo "Adresse:                 $OAAP_HOST"
    echo ""
    echo "Einrichtung im Browser:  http://$OAAP_HOST$PORT_SUFFIX/setup"
    echo "Setup-Token:             $SETUP_TOKEN"
    echo ""
    echo "Hinweise:"
    echo "- Das Token ist EINMAL gueltig, bis der erste Admin-Benutzer"
    echo "  angelegt ist. Danach kann diese Datei geloescht werden."
    echo "- Token verloren? 'sudo oaap setup-token' zeigt es erneut."
  } > "$SETUP_FILE" 2>/dev/null; then
    chmod 600 "$SETUP_FILE"
    [ -z "${SUDO_USER:-}" ] || chown "$SUDO_USER": "$SETUP_FILE" 2>/dev/null || true
    say ""
    say " These details were also written to:  $SETUP_FILE"
  fi
fi
say " Check this node anytime with:  oaap status"
say "=============================================================="
