#!/usr/bin/env bash
# Nightly full platform backup on the node it runs on.
#
# Wraps `oaap backup create` with the three things it does not do
# (oaap.data.backup 0.1 leaves them to "a later version"): a schedule,
# a local retention, and a state file that says what happened.
#
# The state file is deliberately more than a log line. It is the seed of
# "Zustand sichtbar" (RFC-0029): whoever later shows backups in the
# portal or in FleetView reads THIS, and does not have to parse a
# transcript. Its shape is therefore part of the trial run.
#
# Installed as /usr/local/bin/oaap-backup-nightly by
# ops/install-backup-timer.sh. Safe to run by hand at any time.
set -uo pipefail

TARGET="${OAAP_BACKUP_TARGET:-/var/backups/oaap}"
KEEP="${OAAP_BACKUP_KEEP:-2}"
STATE="$TARGET/status.json"
LOG="/var/log/oaap-backup.log"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: requires root." >&2; exit 1; }

exec > >(tee -a "$LOG") 2>&1
chmod 600 "$LOG" 2>/dev/null || true

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# The state file is written on EVERY exit path, success or not. A state
# that only appears when things went well is the one that cannot tell
# "no backup last night" from "nobody ever set this up" -- which is the
# exact confusion this file exists to end.
STARTED="$(now)"
RESULT="failed"
MESSAGE="interrupted before it began"
ARCHIVE=""
BYTES=0
SHA=""
SECONDS_TAKEN=0

write_state() {
  mkdir -p "$TARGET"
  local tmp="$STATE.tmp"
  cat > "$tmp" <<EOF
{
  "schema": "0.1",
  "node": "$(hostname)",
  "platform_version": "$(cat /var/lib/oaap/app/VERSION 2>/dev/null || echo unknown)",
  "started": "$STARTED",
  "finished": "$(now)",
  "seconds": $SECONDS_TAKEN,
  "result": "$RESULT",
  "message": $(printf '%s' "$MESSAGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""'),
  "archive": "$ARCHIVE",
  "bytes": $BYTES,
  "sha256": "$SHA",
  "kept_locally": $(ls -1 "$TARGET"/oaap-backup-*.tar.gz 2>/dev/null | wc -l),
  "next": "$(systemctl show -p NextElapseUSecRealtime --value oaap-backup.timer 2>/dev/null || echo unknown)"
}
EOF
  mv "$tmp" "$STATE"
  chmod 600 "$STATE"
}
trap write_state EXIT

echo "== oaap-backup-nightly $STARTED on $(hostname)"

command -v oaap >/dev/null 2>&1 || {
  MESSAGE="the oaap command is not installed on this node"
  echo "ERROR: $MESSAGE" >&2; exit 1; }

mkdir -p "$TARGET"
chmod 700 "$TARGET"          # the archive carries every secret in cleartext

# Free space before, not a broken archive after: an aborted tar leaves a
# file that looks like a backup. Ask for twice the platform data, which
# is generous for a gzip and cheap to check.
need_kb=$(( $(du -sk /var/lib/oaap 2>/dev/null | cut -f1) * 2 ))
free_kb=$(df -Pk "$TARGET" | awk 'NR==2 {print $4}')
if [ "${free_kb:-0}" -lt "${need_kb:-0}" ]; then
  MESSAGE="not enough space in $TARGET: $((free_kb/1024)) MB free, about $((need_kb/1024)) MB wanted"
  echo "ERROR: $MESSAGE" >&2
  exit 1
fi

t0=$(date +%s)
before="$(ls -1 "$TARGET"/oaap-backup-*.tar.gz 2>/dev/null || true)"
# App containers pause for the COPY only (RFC-0029 D3); compression
# runs with them back up. Said here too, because whoever reads this log
# at 02:00 is looking for exactly that explanation of a gap in some
# app's connections -- and needs to know it was seconds, not minutes.
echo "-- app containers pause for the copy (oaap.data.backup 0.2) --"
if ! oaap backup create --to "$TARGET"; then
  SECONDS_TAKEN=$(( $(date +%s) - t0 ))
  MESSAGE="oaap backup create failed -- see $LOG"
  echo "ERROR: $MESSAGE" >&2
  exit 1
fi
SECONDS_TAKEN=$(( $(date +%s) - t0 ))

# The archive this run produced: the newest file that was not there
# before. Not simply "the newest file" -- a failed run that left an old
# archive as the newest would then be reported as today's.
ARCHIVE="$(ls -1t "$TARGET"/oaap-backup-*.tar.gz 2>/dev/null | head -1)"
if [ -z "$ARCHIVE" ] || printf '%s\n' "$before" | grep -qxF "$ARCHIVE"; then
  MESSAGE="the command reported success but produced no new archive"
  echo "ERROR: $MESSAGE" >&2
  exit 1
fi

# Readable end to end before it is called a backup. gzip -t decompresses
# the whole stream, so a truncated or corrupted archive is found HERE,
# on the machine that can simply make another one -- not in six months
# on the machine that no longer exists.
echo "-- verifying the archive is readable end to end --"
if ! gzip -t "$ARCHIVE"; then
  MESSAGE="the archive is not readable ($(basename "$ARCHIVE")) -- it was NOT counted as a backup"
  echo "ERROR: $MESSAGE" >&2
  mv "$ARCHIVE" "$ARCHIVE.corrupt"
  ARCHIVE="$ARCHIVE.corrupt"
  exit 1
fi

BYTES=$(stat -c %s "$ARCHIVE")
SHA="$(sha256sum "$ARCHIVE" | cut -d' ' -f1)"
printf '%s  %s\n' "$SHA" "$(basename "$ARCHIVE")" > "$ARCHIVE.sha256"
chmod 600 "$ARCHIVE" "$ARCHIVE.sha256"

# Retention, only after a good archive exists: pruning first would trade
# a proven copy for an unproven one.
if [ "$KEEP" -gt 0 ]; then
  ls -1t "$TARGET"/oaap-backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) |
    while read -r old; do
      echo "-- removing $old (keeping the $KEEP newest here)"
      rm -f "$old" "$old.sha256"
    done
fi

RESULT="ok"
MESSAGE="$(basename "$ARCHIVE"), $((BYTES / 1024 / 1024)) MB, ${SECONDS_TAKEN}s"
echo "OK: $MESSAGE"
exit 0
