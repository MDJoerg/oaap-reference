#!/usr/bin/env bash
# Helper for working with the OAAP test VM (see docs/test-vm.md).
# Needs test/.env — copy .env.example and adjust VM_HOST after reboots.

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$DIR/.env" ] || { echo "Missing $DIR/.env (copy .env.example and adjust)"; exit 1; }
# Read only the connection settings, literally -- never run the file as
# shell: it also holds passwords, and an unquoted value with a space was
# executed as a command and shown in the error (2026-11-06).
while IFS='=' read -r k v; do
  case "$k" in *_HOST|*_USER|*_SSH_KEY|*_PORT) ;; *) continue ;; esac
  v="${v%$'\r'}"; v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
  printf -v "$k" '%s' "$v"
done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$DIR/.env")
KEY="${VM_SSH_KEY/#\~/$HOME}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$VM_USER@$VM_HOST")

case "${1:-}" in
  run)     shift; "${SSH[@]}" "$@" ;;
  push)    tar -C "$DIR/.." --exclude .git --exclude test/.env -czf - . \
             | "${SSH[@]}" 'rm -rf ~/oaap-reference && mkdir -p ~/oaap-reference && tar -xzf - -C ~/oaap-reference' \
             && echo "Repo synced to ~/oaap-reference on $VM_HOST" ;;
  install) "${SSH[@]}" 'cd ~/oaap-reference && sudo OAAP_INSTALL_RUNTIME=1 bash install.sh' ;;
  reset)   "${SSH[@]}" 'sudo oaap uninstall --purge --yes' ;;
  *) echo "Usage: test/vm.sh push | install | reset | run <command...>" >&2; exit 2 ;;
esac
