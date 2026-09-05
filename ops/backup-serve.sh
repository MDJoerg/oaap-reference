#!/usr/bin/env bash
# Forced command for the backup-pull key, installed on the node that IS
# backed up (/usr/local/bin/oaap-backup-serve).
#
# In ~/.ssh/authorized_keys of the account the puller logs into:
#
#   command="/usr/local/bin/oaap-backup-serve",restrict ssh-ed25519 AAAA... backup-pull@oaap-demo
#
# `restrict` switches off port forwarding, agent forwarding, X11 and a
# tty; `command=` replaces whatever the client asks for. Together they
# turn a general shell account into a key that can do exactly three
# things: list the archives, read one checksum, send one archive.
#
# Why this matters more here than elsewhere: an archive carries every
# secret on the node in cleartext. A key that may READ it is already
# powerful; a key that may also write or delete could destroy the
# backups from the machine whose failure they exist for.
set -uo pipefail

DIR="${OAAP_BACKUP_DIR:-/var/backups/oaap}"
CMD="${SSH_ORIGINAL_COMMAND:-}"

# The archives are 0600 root: they hold every secret on this node, and
# loosening that so a login account can read them would defeat the point
# of restricting the key at all. So the three permitted requests are run
# with sudo, and the CONSTRAINT is this file rather than the file mode.
# Nothing else this key sends ever reaches sudo.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || { echo "oaap-backup-serve: sudo is required" >&2; exit 1; }
  SUDO="sudo -n"
fi

deny() {
  echo "oaap-backup-serve: refused ($1)" >&2
  logger -t oaap-backup-serve "refused from ${SSH_CONNECTION%% *}: $CMD" 2>/dev/null || true
  exit 126
}

# No shell, ever -- an empty command is somebody trying to log in.
[ -n "$CMD" ] || deny "this key carries no shell"

# Nothing here parses the request: the two read forms must match a fixed
# string exactly (both end in the client's own `2>/dev/null`, which is
# why a blanket ban on shell metacharacters would refuse the permitted
# requests too), and the one form with a variable tail -- rsync -- is
# checked for them below, where it is the only thing that could chain a
# second command.
case "$CMD" in
  "ls -1t $DIR/oaap-backup-*.tar.gz 2>/dev/null")
    exec $SUDO sh -c "ls -1t '$DIR'/oaap-backup-*.tar.gz 2>/dev/null"
    ;;
  "cat $DIR/oaap-backup-"*".tar.gz.sha256 2>/dev/null")
    f="${CMD#cat }"; f="${f% 2>/dev/null}"
    case "$f" in "$DIR"/oaap-backup-*.tar.gz.sha256) ;; *) deny "not a checksum file" ;; esac
    exec $SUDO cat "$f"
    ;;
  "rsync --server --sender "*)
    # The puller's rsync decides the flags; what is fixed here is that
    # it may only SEND (--sender), never receive, and only one archive
    # out of $DIR. This is the one form with a variable tail, so it is
    # also the one that gets the metacharacter check.
    case "$CMD" in
      *';'*|*'&'*|*'|'*|*'`'*|*'$('*|*'>'*|*'<'*|*'..'*)
        deny "unexpected characters in an rsync request" ;;
    esac
    case "$CMD" in
      *" $DIR/oaap-backup-"*".tar.gz") ;;
      *) deny "rsync may only send an archive from $DIR" ;;
    esac
    exec $SUDO sh -c "$CMD"
    ;;
esac

deny "not one of the three permitted requests"
