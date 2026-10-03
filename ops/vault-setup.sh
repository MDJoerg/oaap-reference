#!/usr/bin/env bash
# Initialise ONE vault (RFC-0053, stage 0): a disk that holds backups and is
# open only while a run needs it.
#
#   sudo bash ops/vault-setup.sh --device /dev/sda --name vault-a \
#        --mode luks|luks+age|age  --yes-erase
#
# Three modes, chosen per vault and then fixed (RFC-0053 3.1, D2):
#   luks      LUKS2 over the whole disk, ext4 inside, archives in clear
#   luks+age  the same, and every archive is age-encrypted as well
#   age       plain ext4, every archive age-encrypted (names/sizes visible)
#
# The SECRET for the paper is generated here, shown ONCE, and its re-entry is
# demanded: a secret nobody wrote down is a vault nobody can open after the
# puller is gone. For the age modes the private key is shown once and then
# exists nowhere on this machine -- only the public half stays.
#
# Records: /etc/oaap-vault/<name>.conf (+ .key, .header, .age-recipient)
set -euo pipefail

DEVICE=""; NAME=""; MODE="luks"; YES=0; MNT="/mnt/vault"; CONF_DIR="/etc/oaap-vault"
PAPER_FROM_STDIN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --device) DEVICE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --mnt) MNT="$2"; shift 2 ;;
    --conf-dir) CONF_DIR="$2"; shift 2 ;;
    --yes-erase) YES=1; shift ;;
    # Re-entry of the paper secrets is read from stdin (one line each,
    # in the order shown) instead of the terminal. For the drill only.
    --paper-from-stdin) PAPER_FROM_STDIN=1; shift ;;
    *) echo "Usage: vault-setup.sh --device /dev/sdX --name NAME [--mode luks|luks+age|age] --yes-erase" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "ERROR: requires root (sudo)." >&2; exit 1; }
[ -b "$DEVICE" ] || { echo "ERROR: $DEVICE is not a block device." >&2; exit 2; }
case "$NAME" in ""|*[!A-Za-z0-9._-]*) echo "ERROR: --name wants letters, digits . _ -" >&2; exit 2 ;; esac
case "$MODE" in luks|luks+age|age) ;; *) echo "ERROR: --mode is luks, luks+age or age." >&2; exit 2 ;; esac
[ ! -e "$CONF_DIR/$NAME.conf" ] || { echo "ERROR: a vault called $NAME already exists." >&2; exit 1; }
for t in cryptsetup mkfs.ext4 blkid; do
  case "$MODE" in age) [ "$t" = cryptsetup ] && continue ;; esac
  command -v "$t" >/dev/null 2>&1 || { echo "ERROR: $t is missing." >&2; exit 1; }
done
case "$MODE" in *age) command -v age-keygen >/dev/null 2>&1 || { echo "ERROR: age is missing (apt install age)." >&2; exit 1; } ;; esac

# Never format the disk the system runs from, and never something mounted.
root_src="$(findmnt -n -o SOURCE / || true)"
if lsblk -nlo NAME,MOUNTPOINT "$DEVICE" | awk '$2!=""' | grep -q .; then
  echo "ERROR: something on $DEVICE is mounted:" >&2
  lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT "$DEVICE" >&2
  echo "       Unmount it first; this script does not do that for you." >&2; exit 1
fi
case "$root_src" in "$DEVICE"*) echo "ERROR: $DEVICE carries the root filesystem." >&2; exit 1 ;; esac
if [ "$YES" -ne 1 ]; then
  echo "This ERASES $DEVICE:"; lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL "$DEVICE"
  echo "Run again with --yes-erase to go on."; exit 1
fi

install -d -m 0700 "$CONF_DIR"
MAPPER="oaap-vault-setup-$$"
cleanup() {
  umount "/mnt/.vault-setup-$$" 2>/dev/null || true
  rmdir "/mnt/.vault-setup-$$" 2>/dev/null || true
  [ -e "/dev/mapper/$MAPPER" ] && cryptsetup close "$MAPPER" 2>/dev/null || true
}
trap cleanup EXIT

# One random secret: 24 characters from an alphabet without look-alikes
# (no 0/O, 1/I/L), in six groups of four so it can be copied by hand.
gen_paper() {
  local raw; raw="$(LC_ALL=C tr -dc 'ABCDEFGHJKMNPQRSTUVWXYZ23456789' < /dev/urandom | head -c 24)"
  printf '%s-%s-%s-%s-%s-%s' "${raw:0:4}" "${raw:4:4}" "${raw:8:4}" "${raw:12:4}" "${raw:16:4}" "${raw:20:4}"
}
ask_again() {   # $1 = what, $2 = the secret; refuses until it matches
  local what="$1" secret="$2" tries=0 answer=""
  while [ $tries -lt 3 ]; do
    if [ "$PAPER_FROM_STDIN" -eq 1 ]; then IFS= read -r answer || answer=""
    else read -r -p "Type the $what again to prove it is on paper: " answer < /dev/tty || answer=""; fi
    [ "$answer" = "$secret" ] && return 0
    tries=$((tries + 1)); echo "That is not the same. ($tries/3)" >&2
  done
  return 1
}

PAPER=""; AGE_ID=""; AGE_PUB=""
case "$MODE" in luks|luks+age) PAPER="$(gen_paper)" ;; esac
case "$MODE" in
  *age)
    AGE_ID="$(age-keygen 2>/dev/null | grep '^AGE-SECRET-KEY-')"
    AGE_PUB="$(printf '%s\n' "$AGE_ID" | age-keygen -y)"
    ;;
esac

echo
echo "================  PAPER  -- shown ONCE, never stored here  ================"
[ -n "$PAPER" ]  && echo "  Vault passphrase:  $PAPER"
[ -n "$AGE_ID" ] && echo "  age private key:   $AGE_ID"
echo "  Write it down and keep it where the vault disks are NOT."
case "$MODE" in
  luks)     echo "  Two keys open this vault: the one on this machine and the one on paper." ;;
  luks+age) echo "  Two keys open the disk (machine + paper); only the age key reads the archives." ;;
  age)      echo "  Only the age key reads the archives. There is no key on this machine that can." ;;
esac
echo "  If every copy of the paper is lost, the vault is lost."
echo "==========================================================================="
echo
[ -n "$PAPER" ]  && { ask_again "vault passphrase" "$PAPER" || { echo "ERROR: passphrase not confirmed -- nothing was written." >&2; exit 1; }; }
[ -n "$AGE_ID" ] && { ask_again "age private key" "$AGE_ID" || { echo "ERROR: age key not confirmed -- nothing was written." >&2; exit 1; }; }

# From here on the disk is touched.
UUID=""
case "$MODE" in
  luks|luks+age)
    KEYFILE="$CONF_DIR/$NAME.key"
    ( umask 077; head -c 64 /dev/urandom > "$KEYFILE" )
    chmod 0400 "$KEYFILE"
    wipefs -a "$DEVICE" >/dev/null
    printf '%s' "$PAPER" | cryptsetup luksFormat --type luks2 --batch-mode --key-file - "$DEVICE"
    printf '%s' "$PAPER" | cryptsetup luksAddKey --batch-mode --key-file - "$DEVICE" "$KEYFILE"
    # The tool refuses a vault with one key slot (RFC-0053 section 6).
    slots="$(cryptsetup luksDump "$DEVICE" | grep -cE '^ +[0-9]+: luks2')"
    [ "$slots" -ge 2 ] || { echo "ERROR: only $slots key slot -- refusing." >&2; exit 1; }
    cryptsetup open --test-passphrase --key-file "$KEYFILE" "$DEVICE" || { echo "ERROR: the key file does not open the vault." >&2; exit 1; }
    printf '%s' "$PAPER" | cryptsetup open --test-passphrase --key-file - "$DEVICE" || { echo "ERROR: the passphrase does not open the vault." >&2; exit 1; }
    cryptsetup luksHeaderBackup "$DEVICE" --header-backup-file "$CONF_DIR/$NAME.header"
    chmod 0400 "$CONF_DIR/$NAME.header"
    UUID="$(cryptsetup luksUUID "$DEVICE")"
    cryptsetup open --key-file "$KEYFILE" "$DEVICE" "$MAPPER"
    mkfs.ext4 -q -L "$NAME" -m 1 "/dev/mapper/$MAPPER"
    ;;
  age)
    wipefs -a "$DEVICE" >/dev/null
    mkfs.ext4 -q -F -L "$NAME" -m 1 "$DEVICE"
    UUID="$(blkid -s UUID -o value "$DEVICE")"
    ;;
esac
if [ -n "$AGE_PUB" ]; then
  printf '%s\n' "$AGE_PUB" > "$CONF_DIR/$NAME.age-recipient"; chmod 0644 "$CONF_DIR/$NAME.age-recipient"
fi
AGE_ID=""   # gone from this process; it was never written anywhere

cat > "$CONF_DIR/$NAME.conf" <<EOF
# Written by vault-setup.sh. One file per vault; MODE cannot change without
# initialising the vault anew (RFC-0053 3.1).
NAME=$NAME
MODE=$MODE
UUID=$UUID
MNT=$MNT
CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
chmod 0600 "$CONF_DIR/$NAME.conf"
cleanup; trap - EXIT

echo "Vault $NAME ready: mode=$MODE, UUID=$UUID, closed."
case "$MODE" in luks|luks+age)
  echo "The LUKS header was backed up to $CONF_DIR/$NAME.header -- copy it to the"
  echo "place where the paper is kept; it is useless without a key, and it is the"
  echo "only way back if the disk's own header is damaged." ;;
esac
