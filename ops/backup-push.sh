#!/usr/bin/env bash
# Push this node's newest archive into a puller's inbox (RFC-0053 3.5).
# Runs on the SOURCE, after the nightly archive. The key it uses may create
# one new file on the puller and nothing else (ops/backup-receive.sh).
#
#   oaap-backup-push --to oaap-inbox@10.10.10.136 --key /root/.ssh/oaap_push \
#                    [--age-recipient FILE] [--dir /var/backups/oaap]
#
# With --age-recipient the archive is encrypted HERE, before it leaves, so
# the inbox never holds a clear one. The checksum file then carries the
# CIPHERTEXT's checksum; the clear one is not sent, so the puller can only
# take the source's word that the clear bytes were whole when it encrypted.
set -uo pipefail
TO=""; KEY=""; AGE=""; DIR="/var/backups/oaap"
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TO="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --age-recipient) AGE="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    *) echo "Usage: backup-push.sh --to USER@HOST --key KEY [--age-recipient FILE] [--dir DIR]" >&2; exit 2 ;;
  esac
done
[ -n "$TO" ] && [ -r "$KEY" ] || { echo "ERROR: --to and a readable --key are required." >&2; exit 2; }
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new "$TO")

newest="$(ls -1t "$DIR"/oaap-backup-*.tar.gz 2>/dev/null | head -1)"
[ -n "$newest" ] || { echo "ERROR: no archive in $DIR." >&2; exit 1; }
base="$(basename "$newest")"
want="$(cut -d' ' -f1 "$newest.sha256" 2>/dev/null)"
[ -n "$want" ] || { echo "ERROR: $base has no checksum file -- the nightly did not finish." >&2; exit 1; }
# Do not push something the nightly did not verify as its own.
[ "$(sha256sum "$newest" | cut -d' ' -f1)" = "$want" ] || { echo "ERROR: $base does not match its checksum -- not pushing." >&2; exit 1; }

if [ -n "$AGE" ]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  send="$base.age"; age -R "$AGE" < "$newest" > "$tmp/$send" || { echo "ERROR: age failed." >&2; exit 1; }
  sum="$(sha256sum "$tmp/$send" | cut -d' ' -f1)"; file="$tmp/$send"
else
  send="$base"; sum="$want"; file="$newest"
fi
echo "-- pushing $send to $TO"
# Archive first, checksum second: the puller only takes an archive that has
# its checksum beside it.
"${SSH[@]}" "put $send" < "$file" || { echo "ERROR: the puller refused or failed (see its log)." >&2; exit 1; }
printf '%s  %s\n' "$sum" "$send" | "${SSH[@]}" "put $send.sha256" || { echo "ERROR: checksum file not stored." >&2; exit 1; }
echo "OK: pushed $send"
