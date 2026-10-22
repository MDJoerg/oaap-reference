#!/usr/bin/env bash
# Tools for disks and archives as node resources (RFC-0063, oaap.data.disks 0.1).
#
# Opt-in, not part of install.sh: a node that never gets a USB disk needs
# none of this, and ratarmount comes from PyPI, not from Debian. The
# version is pinned to the one measured (M3-M11, 2026-10-22).
#
#   sudo bash ops/install-disk-tools.sh
set -euo pipefail
RATARMOUNT_VERSION="1.3.0"

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
  cryptsetup exfatprogs fuse3 smartmontools pipx e2fsprogs psmisc >/dev/null
if ! /usr/local/bin/ratarmount --version 2>/dev/null | grep -q "ratarmount $RATARMOUNT_VERSION"; then
  PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
    pipx install --force "ratarmount==$RATARMOUNT_VERSION" >/dev/null
fi
echo "cryptsetup $(cryptsetup --version | awk '{print $2}'), $(/usr/local/bin/ratarmount --version | head -1)"
# The first `oaap disk` command writes the portal's view; from then on the
# minute timer keeps it current (a node without one never runs lsblk).
if command -v oaap >/dev/null; then
  oaap disk list
else
  echo "Next: oaap disk list"
fi
