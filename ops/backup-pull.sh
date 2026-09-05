#!/usr/bin/env bash
# Fetch a node's backup archives to this machine's off-site storage.
#
# Runs on the INTERNAL node and reaches out to the external one. That
# direction is the security statement: the node being backed up holds no
# credential pointing inward, so taking it over does not reach the
# backups (ops/README.md, "Die Aufteilung").
#
#   oaap-backup-pull --node oaapx01 --host oaap.joomp.de --user oaap-admin \
#                    --key ~/.ssh/oaap_backup_pull --to /mnt/backup
#
# Retention is by GENERATION, not by backup kind: every archive is full.
# The newest lands in daily/; a Sunday archive is also kept in weekly/,
# one from the 1st of a month in monthly/ -- as hard links, so a kept
# generation costs no second copy of the same bytes.
set -uo pipefail

NODE=""; HOST=""; USER_="oaap-admin"; KEY=""; TO="/mnt/backup"
DAILY=7; WEEKLY=4; MONTHLY=6; REMOTE_DIR="/var/backups/oaap"
while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --user) USER_="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --daily) DAILY="$2"; shift 2 ;;
    --weekly) WEEKLY="$2"; shift 2 ;;
    --monthly) MONTHLY="$2"; shift 2 ;;
    *) echo "Usage: backup-pull.sh --node NAME --host H --key K [--to DIR] [--user U] [--daily 7] [--weekly 4] [--monthly 6]" >&2; exit 2 ;;
  esac
done
[ -n "$NODE" ] && [ -n "$HOST" ] && [ -n "$KEY" ] || {
  echo "ERROR: --node, --host and --key are required." >&2; exit 2; }

DEST="$TO/$NODE"
STATE="$DEST/status.json"
LOG="/var/log/oaap-backup-pull.log"
exec > >(tee -a "$LOG") 2>&1
chmod 600 "$LOG" 2>/dev/null || true

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
STARTED="$(now)"; RESULT="failed"; MESSAGE="interrupted before it began"
FETCHED=""; BYTES=0; VERIFIED="no"

write_state() {
  mkdir -p "$DEST" 2>/dev/null || true
  local tmp="$STATE.tmp"
  cat > "$tmp" <<EOF
{
  "schema": "0.1",
  "kind": "pull",
  "source_node": "$NODE",
  "source_host": "$HOST",
  "puller": "$(hostname)",
  "started": "$STARTED",
  "finished": "$(now)",
  "result": "$RESULT",
  "message": $(printf '%s' "$MESSAGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""'),
  "fetched": "$FETCHED",
  "bytes": $BYTES,
  "checksum_verified": "$VERIFIED",
  "generations": {
    "daily": $(ls -1 "$DEST/daily" 2>/dev/null | grep -c '\.tar\.gz$' || echo 0),
    "weekly": $(ls -1 "$DEST/weekly" 2>/dev/null | grep -c '\.tar\.gz$' || echo 0),
    "monthly": $(ls -1 "$DEST/monthly" 2>/dev/null | grep -c '\.tar\.gz$' || echo 0)
  },
  "keep": {"daily": $DAILY, "weekly": $WEEKLY, "monthly": $MONTHLY},
  "free_bytes": $(df -PB1 "$TO" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
}
EOF
  mv "$tmp" "$STATE" 2>/dev/null || true
  chmod 600 "$STATE" 2>/dev/null || true
}
trap write_state EXIT

echo "== oaap-backup-pull $STARTED  $NODE -> $(hostname):$DEST"

SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20
     -o StrictHostKeyChecking=accept-new "$USER_@$HOST")

# The target has to be MOUNTED, not merely present. A network share that
# failed to mount leaves an empty directory on the local disk, and a
# backup written into it looks perfectly fine right up to the moment it
# is needed -- while the local root fills up quietly.
if ! mountpoint -q "$TO"; then
  MESSAGE="$TO is not a mount point -- refusing to write backups onto the local disk"
  echo "ERROR: $MESSAGE" >&2; exit 1
fi
mkdir -p "$DEST"/{daily,weekly,monthly}
chmod 700 "$DEST" "$DEST"/{daily,weekly,monthly} 2>/dev/null || true

# What does the source have? Newest first.
remote_list="$("${SSH[@]}" "ls -1t $REMOTE_DIR/oaap-backup-*.tar.gz 2>/dev/null" || true)"
newest="$(printf '%s\n' "$remote_list" | head -1)"
if [ -z "$newest" ]; then
  MESSAGE="the source node has no archive in $REMOTE_DIR -- did its nightly run fail?"
  echo "ERROR: $MESSAGE" >&2; exit 1
fi
base="$(basename "$newest")"
echo "-- newest on $NODE: $base"

if [ -f "$DEST/daily/$base" ]; then
  RESULT="ok"; FETCHED=""; VERIFIED="already here"
  BYTES=$(stat -c %s "$DEST/daily/$base")
  MESSAGE="nothing new -- $base was already fetched"
  echo "$MESSAGE"
else
  # --partial + a temporary name: an interrupted transfer must never
  # leave something under the final name that looks like a backup.
  echo "-- fetching --"
  if ! rsync -a --partial --inplace --timeout=1800 \
        -e "ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
        "$USER_@$HOST:$newest" "$DEST/daily/.$base.part"; then
    MESSAGE="transfer failed"
    echo "ERROR: $MESSAGE" >&2; rm -f "$DEST/daily/.$base.part"; exit 1
  fi

  # Verified against the checksum the SOURCE recorded, not one computed
  # here from the file we just received -- that would only prove the
  # file is a copy of itself.
  want="$("${SSH[@]}" "cat $newest.sha256 2>/dev/null" | cut -d' ' -f1 || true)"
  got="$(sha256sum "$DEST/daily/.$base.part" | cut -d' ' -f1)"
  if [ -z "$want" ]; then
    VERIFIED="no checksum at the source"
    echo "WARNING: the source recorded no checksum for $base"
  elif [ "$want" != "$got" ]; then
    MESSAGE="checksum mismatch for $base -- the copy was discarded"
    echo "ERROR: $MESSAGE" >&2; rm -f "$DEST/daily/.$base.part"; exit 1
  else
    VERIFIED="yes"
  fi
  mv "$DEST/daily/.$base.part" "$DEST/daily/$base"
  printf '%s  %s\n' "$got" "$base" > "$DEST/daily/$base.sha256"
  chmod 600 "$DEST/daily/$base" "$DEST/daily/$base.sha256"
  FETCHED="$base"
  BYTES=$(stat -c %s "$DEST/daily/$base")
  echo "-- fetched $base ($((BYTES / 1024 / 1024)) MB), checksum: $VERIFIED"
fi

# Generations. The archive names itself after the moment it was made
# (oaap-backup-<host>-YYYYMMDD-HHMMSS.tar.gz), so the day it belongs to
# is read from the NAME rather than from this file's mtime -- a copy
# made today of last week's archive still belongs to last week.
stamp="$(printf '%s' "$base" | grep -oE '[0-9]{8}-[0-9]{6}' | head -1)"
day="${stamp%%-*}"
if [ -n "$day" ]; then
  dow="$(date -d "$day" +%u 2>/dev/null || echo 0)"   # 7 = Sunday
  dom="$(date -d "$day" +%d 2>/dev/null || echo 00)"
  link_into() {
    local gen="$1"
    [ -f "$DEST/$gen/$base" ] && return 0
    # A hard link: the same bytes counted once. On a filesystem that
    # refuses them (some CIFS shares do), fall back to a real copy
    # rather than silently keeping no generation at all.
    ln "$DEST/daily/$base" "$DEST/$gen/$base" 2>/dev/null \
      || cp -p "$DEST/daily/$base" "$DEST/$gen/$base"
    cp -p "$DEST/daily/$base.sha256" "$DEST/$gen/$base.sha256" 2>/dev/null || true
    echo "-- kept as $gen generation"
  }
  [ "$dow" = "7" ] && link_into weekly
  [ "$dom" = "01" ] && link_into monthly
fi

prune() {
  local gen="$1" keep="$2"
  [ "$keep" -gt 0 ] || return 0
  ls -1t "$DEST/$gen"/oaap-backup-*.tar.gz 2>/dev/null | tail -n +$((keep + 1)) |
    while read -r old; do
      echo "-- pruning $gen/$(basename "$old")"
      rm -f "$old" "$old.sha256"
    done
}
prune daily "$DAILY"
prune weekly "$WEEKLY"
prune monthly "$MONTHLY"

RESULT="ok"
[ -n "$FETCHED" ] && MESSAGE="$FETCHED, $((BYTES / 1024 / 1024)) MB, checksum $VERIFIED"
echo "OK: $MESSAGE"
echo "   daily=$(ls -1 "$DEST/daily"/*.tar.gz 2>/dev/null | wc -l)" \
     "weekly=$(ls -1 "$DEST/weekly"/*.tar.gz 2>/dev/null | wc -l)" \
     "monthly=$(ls -1 "$DEST/monthly"/*.tar.gz 2>/dev/null | wc -l)"
exit 0
