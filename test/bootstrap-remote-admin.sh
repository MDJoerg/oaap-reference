#!/usr/bin/env bash
# Prepare a fresh Debian machine for remote administration by Claude
# (see docs/external-nodes.md). Run ONCE as root on the target:
#
#   bash bootstrap-remote-admin.sh 'ssh-ed25519 AAAA... comment' [hostname]
#
# What it does (idempotent — safe to re-run):
#   1. installs sudo, git, curl (a Debian netinstall ships without sudo/git)
#   2. creates user oaap-admin WITHOUT a password (key-only login)
#   3. grants passwordless sudo via /etc/sudoers.d/oaap-admin
#      (needed because Claude works non-interactively over SSH)
#   4. installs the given public key into ~oaap-admin/.ssh/authorized_keys
#   5. optionally sets the hostname
# It does NOT touch sshd: password login stays as the provider left it
# (cloud images usually ship PasswordAuthentication no) and root's own
# key access is preserved — the operator decides about hardening.
set -euo pipefail
ADMIN_USER="${OAAP_REMOTE_ADMIN:-oaap-admin}"
PUBKEY="${1:-}"
NEW_HOSTNAME="${2:-}"
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
case "$PUBKEY" in
  ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*|sk-*) ;;
  *) echo "Usage: $0 '<public key line>' [hostname]" >&2; exit 2 ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq sudo git curl >/dev/null

id "$ADMIN_USER" >/dev/null 2>&1 \
  || adduser --disabled-password --gecos "OAAP Admin" "$ADMIN_USER"
usermod -aG sudo "$ADMIN_USER"

echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$ADMIN_USER"
chmod 0440 "/etc/sudoers.d/$ADMIN_USER"
visudo -cf "/etc/sudoers.d/$ADMIN_USER" >/dev/null

HOME_DIR="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "$HOME_DIR/.ssh"
AUTH="$HOME_DIR/.ssh/authorized_keys"
touch "$AUTH"
grep -qxF "$PUBKEY" "$AUTH" || echo "$PUBKEY" >> "$AUTH"
chown "$ADMIN_USER:$ADMIN_USER" "$AUTH"
chmod 600 "$AUTH"

if [ -n "$NEW_HOSTNAME" ]; then
  hostnamectl set-hostname "$NEW_HOSTNAME"
  grep -qE "^127\.0\.1\.1\s" /etc/hosts \
    && sed -i -E "s/^127\.0\.1\.1\s.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts \
    || echo -e "127.0.1.1\t$NEW_HOSTNAME" >> /etc/hosts
fi

echo "== $ADMIN_USER ready on $(hostname)"
id "$ADMIN_USER"
sudo -u "$ADMIN_USER" sudo -n true && echo "sudo without password: OK"
echo "authorized_keys: $(wc -l < "$AUTH") key(s)"
