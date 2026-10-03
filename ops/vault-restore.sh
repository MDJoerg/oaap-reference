#!/usr/bin/env bash
# The drill (RFC-0053 D7): take an archive OUT of a vault the way a person
# would on the day the puller is gone -- with the PAPER, not with the key file.
#
#   sudo bash ops/vault-restore.sh --vault NAME --node NODE --out DIR \
#        [--paper-file F]      the vault passphrase (luks, luks+age); default: the key file
#        [--identity-file F]   the age private key (luks+age, age)
#        [--device DEV]        the disk, if this machine does not know the vault
#        [--header-backup F]   restore a damaged LUKS header from its backup first
#
# What comes out is a clear node archive in DIR, checked against the clear
# checksum where one was kept. What to do with it is the platform's:
#   sudo ./install.sh restore <file>            a node
#   sudo oaap tenant adopt <tenant archive>     a tenant onto an EMPTY node
# The vault is closed again, whatever happens.
set -uo pipefail
CONF_DIR="${OAAP_VAULT_CONF:-/etc/oaap-vault}"
VAULT=""; NODE=""; OUT=""; PAPER=""; IDENT=""; DEV=""; HDR=""; MNT="/mnt/vault-drill"
while [ $# -gt 0 ]; do
  case "$1" in
    --vault) VAULT="$2"; shift 2 ;; --node) NODE="$2"; shift 2 ;; --out) OUT="$2"; shift 2 ;;
    --paper-file) PAPER="$2"; shift 2 ;; --identity-file) IDENT="$2"; shift 2 ;;
    --device) DEV="$2"; shift 2 ;; --header-backup) HDR="$2"; shift 2 ;;
    --mnt) MNT="$2"; shift 2 ;;
    *) echo "Usage: vault-restore.sh --vault NAME --node NODE --out DIR [--paper-file F] [--identity-file F] [--device DEV] [--header-backup F]" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "ERROR: requires root." >&2; exit 1; }
[ -n "$VAULT" ] && [ -n "$NODE" ] && [ -n "$OUT" ] || { echo "ERROR: --vault, --node and --out are required." >&2; exit 2; }
[ -f "$CONF_DIR/$VAULT.conf" ] || { echo "ERROR: no record of vault $VAULT in $CONF_DIR." >&2; exit 1; }
. "$CONF_DIR/$VAULT.conf"
[ -n "$DEV" ] || DEV="$(blkid -U "$UUID" 2>/dev/null || true)"
[ -n "$DEV" ] && [ -b "$DEV" ] || { echo "ERROR: vault $VAULT (UUID $UUID) is not inserted." >&2; exit 1; }
MAPPER="oaap-vault-drill-$$"; OPENED=0; MOUNTED=0
cleanup() {
  [ "$MOUNTED" -eq 1 ] && umount "$MNT" 2>/dev/null
  [ "$OPENED" -eq 1 ] && cryptsetup close "$MAPPER" 2>/dev/null
  rmdir "$MNT" 2>/dev/null; true
}
trap cleanup EXIT
install -d -m 0700 "$OUT" "$MNT"

case "$MODE" in
  luks|luks+age)
    if [ -n "$HDR" ]; then
      cryptsetup luksHeaderRestore --batch-mode --header-backup-file "$HDR" "$DEV" || { echo "FAIL: header restore failed."; exit 1; }
      echo "-- LUKS header restored from $HDR"
    fi
    if [ -n "$PAPER" ]; then
      echo "-- opening with the PAPER passphrase (the key file is not used)"
      tr -d '\n' < "$PAPER" | cryptsetup open --key-file - "$DEV" "$MAPPER" || { echo "FAIL: the paper passphrase does not open the vault."; exit 1; }
    else
      cryptsetup open --key-file "$CONF_DIR/$VAULT.key" "$DEV" "$MAPPER" || { echo "FAIL: the key file does not open the vault."; exit 1; }
    fi
    OPENED=1; mount -o ro,noatime,nodev,nosuid,noexec "/dev/mapper/$MAPPER" "$MNT" || { echo "FAIL: mount."; exit 1; } ;;
  age) mount -o ro,noatime,nodev,nosuid,noexec "$DEV" "$MNT" || { echo "FAIL: mount."; exit 1; } ;;
esac
MOUNTED=1

f="$(ls -1t "$MNT/$NODE/daily"/oaap-backup-*.tar.gz* 2>/dev/null | grep -v '\.sha256$' | head -1)"
[ -n "$f" ] || { echo "FAIL: no archive of $NODE on the vault."; exit 1; }
b="$(basename "$f")"; echo "-- newest: $b"
case "$b" in
  *.age)
    [ -n "$IDENT" ] || { echo "FAIL: this archive is age-encrypted; --identity-file (the private key from paper) is required."; exit 1; }
    clear="$OUT/${b%.age}"
    age -d -i "$IDENT" -o "$clear" "$f" || { rm -f "$clear"; echo "FAIL: age could not decrypt (wrong key?)."; exit 1; }
    ;;
  *) clear="$OUT/$b"; cp -- "$f" "$clear" ;;
esac
chmod 600 "$clear"
if [ -f "$f.clear.sha256" ]; then want="$(cut -d' ' -f1 "$f.clear.sha256")"
elif [ "${b%.age}" = "$b" ]; then want="$(cut -d' ' -f1 "$f.sha256")"; else want=""; fi
got="$(sha256sum "$clear" | cut -d' ' -f1)"
if [ -z "$want" ]; then echo "NOTE: no clear checksum was kept for this archive (pushed with source-side age); clear bytes checked by gzip/tar only."
elif [ "$want" != "$got" ]; then echo "FAIL: clear checksum differs from the one recorded before encrypting."; rm -f "$clear"; exit 1
else echo "-- clear checksum equals the one verified at the source/before encrypting"; fi
gzip -t "$clear" && n="$(tar tzf "$clear" | wc -l)" && echo "-- gzip ok, $n entries" || { echo "FAIL: not a readable archive."; exit 1; }
echo "OK: $clear"
