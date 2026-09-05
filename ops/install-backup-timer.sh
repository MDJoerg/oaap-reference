#!/usr/bin/env bash
# Install the nightly backup on this node (systemd service + timer).
#
# A systemd timer is Linux's scheduled job -- the counterpart of a
# background job in SAP: a unit says WHAT runs, a timer says WHEN, and
# `systemctl list-timers` shows the plan and the last run.
#
#   sudo bash ops/install-backup-timer.sh [--at 03:30] [--keep 2]
#                                         [--to /var/backups/oaap] [--remove]
set -euo pipefail

AT="03:30"
KEEP="2"
TO="/var/backups/oaap"
REMOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --at)     AT="$2"; shift 2 ;;
    --keep)   KEEP="$2"; shift 2 ;;
    --to)     TO="$2"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    *) echo "Usage: install-backup-timer.sh [--at HH:MM] [--keep N] [--to DIR] [--remove]" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "ERROR: requires root (sudo)." >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$REMOVE" -eq 1 ]; then
  systemctl disable --now oaap-backup.timer 2>/dev/null || true
  rm -f /etc/systemd/system/oaap-backup.{service,timer} /usr/local/bin/oaap-backup-nightly
  systemctl daemon-reload
  echo "Nightly backup removed. Existing archives in $TO were kept."
  exit 0
fi

case "$AT" in
  [0-9][0-9]:[0-9][0-9]) ;;
  *) echo "ERROR: --at wants HH:MM (24h), got '$AT'." >&2; exit 1 ;;
esac

# The target must not live inside the platform data directory -- the
# same rule `oaap backup create` enforces, checked here so the timer is
# never installed pointing at a place that would die with the machine.
data_abs="$(readlink -f /var/lib/oaap)"
to_abs="$(readlink -f "$TO" 2>/dev/null || echo "$TO")"
case "$to_abs/" in
  "$data_abs"/*) echo "ERROR: $TO lies inside $data_abs -- a backup that dies with the machine is not a backup." >&2; exit 1 ;;
esac

install -m 0700 "$HERE/backup-nightly.sh" /usr/local/bin/oaap-backup-nightly

cat > /etc/systemd/system/oaap-backup.service <<EOF
[Unit]
Description=OAAP nightly platform backup
Documentation=file://$HERE/README.md
# App containers are stopped for the copy, so docker has to be there.
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
Environment=OAAP_BACKUP_TARGET=$TO
Environment=OAAP_BACKUP_KEEP=$KEEP
ExecStart=/usr/local/bin/oaap-backup-nightly
# A backup that hangs must not block the next night's run.
TimeoutStartSec=2h
EOF

cat > /etc/systemd/system/oaap-backup.timer <<EOF
[Unit]
Description=OAAP nightly platform backup at $AT

[Timer]
OnCalendar=*-*-* $AT:00
# A node that was off at $AT backs up when it comes back, rather than
# skipping a night in silence.
Persistent=true
# Ten minutes of spread, so a fleet of nodes does not all stop their
# apps in the same second.
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now oaap-backup.timer

echo ""
echo "Nightly backup installed on $(hostname):"
echo "  when:   every day at $AT (local time), missed runs are caught up"
echo "  target: $TO  (keeping the $KEEP newest archives here)"
echo "  state:  $TO/status.json, transcript /var/log/oaap-backup.log"
echo ""
echo "The app containers are STOPPED for the COPY and started again before"
echo "the archive is compressed (RFC-0029 D3). Connections through them"
echo "break at $AT for that copy -- measured 32s for 8.0 GB on oaapx01,"
echo "14s for 899 MB on oaap-test. After the first run, this node's own"
echo "figure is in $TO/../oaap/backup-last.json; use that one, not these."
echo ""
systemctl list-timers oaap-backup.timer --no-pager || true
echo ""
echo "Run it once now with:  sudo /usr/local/bin/oaap-backup-nightly"
