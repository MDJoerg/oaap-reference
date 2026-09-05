#!/usr/bin/env bash
# Install the nightly pull on the INTERNAL node (systemd service + timer).
#
#   sudo bash ops/install-backup-pull.sh --node oaapx01 --host oaap.joomp.de \
#        --user oaap-admin --key /home/oaap-admin/.ssh/oaap_backup_pull \
#        --to /mnt/backup [--at 04:30] [--remove]
set -euo pipefail

NODE=""; HOST=""; USER_="oaap-admin"; KEY=""; TO="/mnt/backup"; AT="04:30"
DAILY=7; WEEKLY=4; MONTHLY=6; REMOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --user) USER_="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    --at) AT="$2"; shift 2 ;;
    --daily) DAILY="$2"; shift 2 ;;
    --weekly) WEEKLY="$2"; shift 2 ;;
    --monthly) MONTHLY="$2"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    *) echo "Usage: install-backup-pull.sh --node N --host H --key K [--to DIR] [--at HH:MM] [--remove]" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "ERROR: requires root (sudo)." >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$REMOVE" -eq 1 ]; then
  [ -n "$NODE" ] || { echo "ERROR: --remove wants --node." >&2; exit 2; }
  systemctl disable --now "oaap-backup-pull@$NODE.timer" 2>/dev/null || true
  rm -f "/etc/systemd/system/oaap-backup-pull@$NODE.timer.d/when.conf"
  rmdir "/etc/systemd/system/oaap-backup-pull@$NODE.timer.d" 2>/dev/null || true
  rm -f "/etc/oaap-backup-pull/$NODE.conf"
  systemctl daemon-reload
  echo "Pull for $NODE removed. Archives under $TO/$NODE were kept."
  exit 0
fi

[ -n "$NODE" ] && [ -n "$HOST" ] && [ -n "$KEY" ] || {
  echo "ERROR: --node, --host and --key are required." >&2; exit 2; }
[ -r "$KEY" ] || { echo "ERROR: cannot read the key $KEY." >&2; exit 1; }
case "$AT" in [0-9][0-9]:[0-9][0-9]) ;; *) echo "ERROR: --at wants HH:MM." >&2; exit 1 ;; esac
command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync is not installed (apt install rsync)." >&2; exit 1; }

# The target must be a mount point NOW, or the very first run would
# write onto the local disk -- checked here so the mistake is caught
# while somebody is watching, not at 04:30.
mountpoint -q "$TO" || {
  echo "ERROR: $TO is not a mount point. Mount the off-site storage first" >&2
  echo "       (a permanent entry in /etc/fstab, not a hand-made mount)." >&2
  exit 1; }

install -d -m 0700 /etc/oaap-backup-pull
cat > "/etc/oaap-backup-pull/$NODE.conf" <<EOF
# Written by install-backup-pull.sh -- one file per source node.
NODE=$NODE
HOST=$HOST
USER=$USER_
KEY=$KEY
TO=$TO
DAILY=$DAILY
WEEKLY=$WEEKLY
MONTHLY=$MONTHLY
EOF
chmod 600 "/etc/oaap-backup-pull/$NODE.conf"

install -m 0700 "$HERE/backup-pull.sh" /usr/local/bin/oaap-backup-pull

# A template unit (@) so a second source node is one more timer, not a
# second copy of everything.
cat > /etc/systemd/system/oaap-backup-pull@.service <<EOF
[Unit]
Description=Fetch OAAP backups from %i
Documentation=file://$HERE/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/oaap-backup-pull/%i.conf
ExecStart=/bin/sh -c '/usr/local/bin/oaap-backup-pull --node "\$NODE" --host "\$HOST" --user "\$USER" --key "\$KEY" --to "\$TO" --daily "\$DAILY" --weekly "\$WEEKLY" --monthly "\$MONTHLY"'
TimeoutStartSec=3h
EOF

cat > /etc/systemd/system/oaap-backup-pull@.timer <<'EOF'
[Unit]
Description=Fetch OAAP backups from %i

[Timer]
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

# The hour is per source node, so it lives in a drop-in rather than in
# the shared template.
install -d "/etc/systemd/system/oaap-backup-pull@$NODE.timer.d"
cat > "/etc/systemd/system/oaap-backup-pull@$NODE.timer.d/when.conf" <<EOF
[Timer]
OnCalendar=*-*-* $AT:00
EOF

systemctl daemon-reload
systemctl enable --now "oaap-backup-pull@$NODE.timer"

echo ""
echo "Pull installed on $(hostname):"
echo "  source: $NODE ($USER_@$HOST:/var/backups/oaap)"
echo "  when:   every day at $AT, missed runs are caught up"
echo "  target: $TO/$NODE/{daily,weekly,monthly}  (keep $DAILY/$WEEKLY/$MONTHLY)"
echo "  state:  $TO/$NODE/status.json, transcript /var/log/oaap-backup-pull.log"
echo ""
echo "On $NODE, this key should be able to do nothing else. Install"
echo "ops/backup-serve.sh there as /usr/local/bin/oaap-backup-serve and"
echo "put the public key in ~/.ssh/authorized_keys as:"
echo ""
echo "  command=\"/usr/local/bin/oaap-backup-serve\",restrict ssh-ed25519 AAAA... backup-pull@$(hostname)"
echo ""
systemctl list-timers "oaap-backup-pull@$NODE.timer" --no-pager || true
echo ""
echo "Run it once now with:  sudo systemctl start oaap-backup-pull@$NODE"
