#!/usr/bin/env bash
# Helper for any remote node configured in test/.env — the generic
# sibling of vm.sh (see docs/external-nodes.md).
#
#   bash test/node.sh <node> run <command...>   # run a command on the node
#   bash test/node.sh <node> ssh                # interactive shell
#   bash test/node.sh <node> push               # sync this repo to ~/oaap-reference
#   bash test/node.sh <node> update             # sudo oaap update
#
# <node> is the lower-case prefix of the .env variables, e.g. "oaapx01"
# expects OAAPX01_HOST, OAAPX01_USER, OAAPX01_SSH_KEY.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$DIR/.env" ] || { echo "Missing $DIR/.env (copy .env.example and adjust)"; exit 1; }
. "$DIR/.env"
NODE="${1:-}"; [ -n "$NODE" ] || { echo "Usage: test/node.sh <node> run|ssh|push|update ..." >&2; exit 2; }
P="$(printf '%s' "$NODE" | tr '[:lower:]-' '[:upper:]_')"
HOST="$(eval "printf '%s' \"\${${P}_HOST:-}\"")"
USER_="$(eval "printf '%s' \"\${${P}_USER:-oaap-admin}\"")"
KEY="$(eval "printf '%s' \"\${${P}_SSH_KEY:-}\"")"
[ -n "$HOST" ] && [ -n "$KEY" ] || { echo "No ${P}_HOST/${P}_SSH_KEY in $DIR/.env" >&2; exit 1; }
KEY="${KEY/#\~/$HOME}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$USER_@$HOST")
shift
case "${1:-}" in
  run)    shift; "${SSH[@]}" "$@" ;;
  ssh)    "${SSH[@]}" ;;
  push)   tar -C "$DIR/.." --exclude .git --exclude test/.env -czf - . \
            | "${SSH[@]}" 'rm -rf ~/oaap-reference && mkdir -p ~/oaap-reference && tar -xzf - -C ~/oaap-reference' \
            && echo "Repo synced to ~/oaap-reference on $HOST" ;;
  update) "${SSH[@]}" 'sudo oaap update' ;;
  *) echo "Usage: test/node.sh <node> run <command...> | ssh | push | update" >&2; exit 2 ;;
esac
