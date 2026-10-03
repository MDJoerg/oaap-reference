#!/usr/bin/env bash
# Install the nightly pull on the INTERNAL node (systemd service + timer).
#
#   sudo bash ops/install-backup-pull.sh --node oaapx01 --host oaap.joomp.de \
#        --user oaap-admin --key /home/oaap-admin/.ssh/oaap_backup_pull \
#        --to /mnt/backup [--at 04:30] [--remove]
set -euo pipefail

NODE=""; HOST=""; USER_="oaap-admin"; KEY=""; TO="/mnt/backup"; AT="04:30"
DAILY=7; WEEKLY=4; MONTHLY=6; REMOVE=0; LOCAL=0; VAULT=0
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
    # This machine is its own source (see backup-pull.sh --local). For
    # the node that fetches for everyone and has nobody to fetch it.
    --local) LOCAL=1; shift ;;
    # RFC-0053 stage 0: TO is a vault that is CLOSED between runs. The
    # timer then runs oaap-vault-run (open, pull, read back, close) instead
    # of the plain pull, and "is TO mounted now?" is the wrong question.
    --vault) VAULT=1; shift ;;
    *) echo "Usage: install-backup-pull.sh --node N (--host H --key K | --local) [--to DIR] [--at HH:MM] [--vault] [--remove]" >&2; exit 2 ;;
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
  # The vault timer belongs to no single source: it goes when the last
  # source that writes to a vault goes.
  if ! grep -ls '^VAULT=1' /etc/oaap-backup-pull/*.conf >/dev/null 2>&1; then
    systemctl disable --now oaap-vault-run.timer 2>/dev/null || true
    rm -f /etc/systemd/system/oaap-vault-run.{service,timer}
  fi
  systemctl daemon-reload
  echo "Pull for $NODE removed. Archives under $TO/$NODE were kept."
  exit 0
fi

if [ "$LOCAL" -eq 1 ]; then
  NODE="${NODE:-$(hostname)}"; HOST="$(hostname)"; KEY="-"
else
  [ -n "$NODE" ] && [ -n "$HOST" ] && [ -n "$KEY" ] || {
    echo "ERROR: --node, --host and --key are required (or --local)." >&2; exit 2; }
  [ -r "$KEY" ] || { echo "ERROR: cannot read the key $KEY." >&2; exit 1; }
  command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync is not installed (apt install rsync)." >&2; exit 1; }
fi
case "$AT" in [0-9][0-9]:[0-9][0-9]) ;; *) echo "ERROR: --at wants HH:MM." >&2; exit 1 ;; esac

# The target must be a mount point NOW, or the very first run would
# write onto the local disk -- checked here so the mistake is caught
# while somebody is watching, not at 04:30.
if [ "$VAULT" -eq 1 ]; then
  # A vault is closed between runs, so TO is NOT a mount point now -- and
  # must not be one by fstab either: a vault in fstab is a vault that is
  # open whenever the machine is on. What has to exist is the vault.
  ls /etc/oaap-vault/*.conf >/dev/null 2>&1 || {
    echo "ERROR: no vault is set up. Run ops/vault-setup.sh first." >&2; exit 1; }
  command -v cryptsetup >/dev/null 2>&1 || { echo "ERROR: cryptsetup is not installed (apt install cryptsetup)." >&2; exit 1; }
  if mountpoint -q "$TO"; then
    echo "NOTE: $TO is mounted right now -- left alone; the run will refuse to open a vault over it."
  fi
else
  mountpoint -q "$TO" || {
    echo "ERROR: $TO is not a mount point. Mount the off-site storage first" >&2
    echo "       (a permanent entry in /etc/fstab, not a hand-made mount)." >&2
    exit 1; }
fi

install -d -m 0700 /etc/oaap-backup-pull
cat > "/etc/oaap-backup-pull/$NODE.conf" <<EOF
# Written by install-backup-pull.sh -- one file per source node.
NODE=$NODE
HOST=$HOST
USER=$USER_
KEY=$KEY
TO=$TO
LOCAL=$LOCAL
VAULT=$VAULT
DAILY=$DAILY
WEEKLY=$WEEKLY
MONTHLY=$MONTHLY
EOF
chmod 600 "/etc/oaap-backup-pull/$NODE.conf"

install -m 0700 "$HERE/backup-pull.sh" /usr/local/bin/oaap-backup-pull

if [ "$VAULT" -eq 1 ]; then
  install -m 0700 "$HERE/vault-run.sh" /usr/local/bin/oaap-vault-run
  install -m 0700 "$HERE/vault-status.sh" /usr/local/bin/oaap-vault-status
  cat > /etc/systemd/system/oaap-vault-run.service <<EOF
[Unit]
Description=OAAP vault run (open, pull, read back, close)
Documentation=file://$HERE/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/oaap-vault-run
# systemd stops the whole group; the run closes the vault on SIGTERM.
TimeoutStartSec=3h
TimeoutStopSec=5min
EOF
  cat > /etc/systemd/system/oaap-vault-run.timer <<EOF
[Unit]
Description=OAAP vault run at $AT

[Timer]
OnCalendar=*-*-* $AT:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now oaap-vault-run.timer
  echo ""
  echo "Vault run installed on $(hostname):"
  echo "  source: $NODE ($USER_@$HOST:/var/backups/oaap)"
  echo "  when:   every day at $AT; the vault is open for the minutes of the run only"
  echo "  state:  /var/lib/oaap-vault/last-run.json, history runs.jsonl, per vault: oaap-vault-status"
  echo "  keep:   $DAILY/$WEEKLY/$MONTHLY per vault"
  echo ""
  systemctl list-timers oaap-vault-run.timer --no-pager || true
  echo ""
  echo "On $NODE, the key must be limited by ops/backup-serve.sh (see README)."
  echo "Run it once now with:  sudo systemctl start oaap-vault-run"
  exit 0
fi

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
ExecStart=/bin/sh -c 'set -- --node "\$NODE" --to "\$TO" --daily "\$DAILY" --weekly "\$WEEKLY" --monthly "\$MONTHLY"; [ "\$LOCAL" = 1 ] && set -- "\$@" --local || set -- "\$@" --host "\$HOST" --user "\$USER" --key "\$KEY"; exec /usr/local/bin/oaap-backup-pull "\$@"'
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
if [ "$LOCAL" -eq 1 ]; then
  echo "  source: this machine itself (/var/backups/oaap) -- no key, no network"
else
  echo "  source: $NODE ($USER_@$HOST:/var/backups/oaap)"
fi
echo "  when:   every day at $AT, missed runs are caught up"
echo "  target: $TO/$NODE/{daily,weekly,monthly}  (keep $DAILY/$WEEKLY/$MONTHLY)"
echo "  state:  $TO/$NODE/status.json, transcript /var/log/oaap-backup-pull.log"
echo ""
if [ "$LOCAL" -eq 1 ]; then
  echo "No key and no forced command here: there is no second machine in"
  echo "this path. Note what that means -- this node's off-site copy is"
  echo "only as separate as the share it writes to. Whoever takes over"
  echo "this machine can reach $TO."
else
  echo "On $NODE, this key should be able to do nothing else. Install"
  echo "ops/backup-serve.sh there as /usr/local/bin/oaap-backup-serve and"
  echo "put the public key in ~/.ssh/authorized_keys as:"
  echo ""
  echo "  command=\"/usr/local/bin/oaap-backup-serve\",restrict ssh-ed25519 AAAA... backup-pull@$(hostname)"
fi
echo ""
systemctl list-timers "oaap-backup-pull@$NODE.timer" --no-pager || true
echo ""
echo "Run it once now with:  sudo systemctl start oaap-backup-pull@$NODE"
