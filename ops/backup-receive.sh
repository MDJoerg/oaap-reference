#!/usr/bin/env bash
# Forced command on the PULLER for a pushing source (RFC-0053 3.5, D5):
# the other direction. The source's key may CREATE one new archive file in a
# bounded inbox and nothing else -- no list, no read, no overwrite, no
# delete, no name that exists.
#
# In ~/.ssh/authorized_keys of the account that owns the inbox:
#
#   command="/usr/local/bin/oaap-backup-receive --source oaap-bernd",restrict ssh-ed25519 AAAA... push@oaap-bernd
#
# The source's name is fixed HERE, on the puller; the client cannot name
# another one. The client's whole vocabulary is:   put <file name>
# with the file on stdin. The file name must be this source's own pattern.
#
# The inbox is on the puller's own disk, because the vault is closed when a
# push arrives. In vault mode `luks` an archive waits there in clear until
# the run -- which is why the encrypted modes have the SOURCE encrypt first.
set -uo pipefail

SOURCE=""; INBOX="${OAAP_INBOX:-/var/lib/oaap-vault/inbox}"; MAX_ARCHIVES=3; MAX_BYTES=$((64 * 1024 * 1024 * 1024))
while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --inbox) INBOX="$2"; shift 2 ;;
    --max-archives) MAX_ARCHIVES="$2"; shift 2 ;;
    --max-bytes) MAX_BYTES="$2"; shift 2 ;;
    *) echo "oaap-backup-receive: bad option $1" >&2; exit 2 ;;
  esac
done
CMD="${SSH_ORIGINAL_COMMAND:-}"
deny() {
  echo "oaap-backup-receive: refused ($1)" >&2
  logger -t oaap-backup-receive "refused from ${SSH_CONNECTION%% *}: $CMD ($1)" 2>/dev/null || true
  exit 126
}
case "$SOURCE" in ""|*[!A-Za-z0-9._-]*) echo "oaap-backup-receive: --source is required (letters, digits . _ -)" >&2; exit 2 ;; esac
[ -n "$CMD" ] || deny "this key carries no shell"

# Exactly:  put <name>   -- no other word, no second argument, no path.
case "$CMD" in
  "put "*) name="${CMD#put }" ;;
  *) deny "the only request is: put <file name>" ;;
esac
# The name is the source's own pattern: archive (clear or age) or its checksum.
pat="^oaap-backup-${SOURCE}-[0-9]{8}-[0-9]{6}\.tar\.gz(\.age)?(\.sha256)?$"
printf '%s' "$name" | grep -Eq "$pat" || deny "not a file name of source $SOURCE"

DIR="$INBOX/$SOURCE"
[ -d "$DIR" ] || { mkdir -p "$DIR" && chmod 700 "$DIR"; } || deny "no inbox"
[ ! -e "$DIR/$name" ] || deny "$name already exists (the inbox never overwrites)"

# A new ARCHIVE counts against the bound; its checksum file does not. A full
# inbox refuses -- it never deletes to make room. The reader of the refusal
# is the source's log, so it says what to do.
case "$name" in
  *.sha256) [ -e "$DIR/${name%.sha256}" ] || deny "checksum without its archive" ;;
  *)
    n="$(ls -1 "$DIR"/oaap-backup-*.tar.gz "$DIR"/oaap-backup-*.tar.gz.age 2>/dev/null | wc -l)"
    if [ "$n" -ge "$MAX_ARCHIVES" ]; then
      echo "oaap-backup-receive: inbox full ($n/$MAX_ARCHIVES) -- the puller has not run its vault; nothing was deleted" >&2
      logger -t oaap-backup-receive "inbox of $SOURCE full ($n/$MAX_ARCHIVES)" 2>/dev/null || true
      exit 75
    fi ;;
esac

# Write under a temporary name, publish only when complete and within the
# size bound. `head -c MAX+1` lets an oversize stream be recognised.
tmp="$DIR/.$name.part"
rm -f "$tmp"
( umask 077; head -c $((MAX_BYTES + 1)) > "$tmp" ) || { rm -f "$tmp"; deny "write failed"; }
size="$(stat -c %s "$tmp")"
[ "$size" -le "$MAX_BYTES" ] || { rm -f "$tmp"; deny "larger than $MAX_BYTES bytes"; }
[ "$size" -gt 0 ] || { rm -f "$tmp"; deny "empty file"; }
# ln fails if the name appeared meanwhile: the no-overwrite rule holds under a race too.
ln "$tmp" "$DIR/$name" 2>/dev/null || { rm -f "$tmp"; deny "$name appeared meanwhile"; }
rm -f "$tmp"
logger -t oaap-backup-receive "received $SOURCE/$name ($size bytes)" 2>/dev/null || true
echo "oaap-backup-receive: stored $name ($size bytes)"
