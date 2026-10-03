#!/usr/bin/env bash
# One vault run (RFC-0053, stage 0): find the inserted vault, open it, pull,
# read back, close, spin down.
#
#   oaap-vault-run [--vault NAME] [--scrub] [--no-standby]
#
# What a run proves depends on the vault's mode, and the record says so in
# that mode's own words -- "read back ok" is only ever said for luks.
#
# A failed step leaves the vault CLOSED (RFC-0053 section 6): the cleanup
# runs from an EXIT trap, so it also runs when a step dies. The one thing it
# cannot do is close a vault something else is holding open; then the run
# says so loudly and exits 3.
#
# Exit: 0 ok | 1 failed, vault closed | 3 failed and the vault is STILL OPEN
set -uo pipefail

CONF_DIR="${OAAP_VAULT_CONF:-/etc/oaap-vault}"
PULL_CONF_DIR="${OAAP_PULL_CONF:-/etc/oaap-backup-pull}"
STATE="${OAAP_VAULT_STATE:-/var/lib/oaap-vault}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PULL_BIN="${OAAP_PULL_BIN:-}"
[ -n "$PULL_BIN" ] || { [ -x /usr/local/bin/oaap-backup-pull ] && PULL_BIN=/usr/local/bin/oaap-backup-pull || PULL_BIN="$HERE/backup-pull.sh"; }
RHYTHM_DAYS="${OAAP_VAULT_RHYTHM_DAYS:-7}"

WANT=""; SCRUB=0; STANDBY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --vault) WANT="$2"; shift 2 ;;
    --scrub) SCRUB=1; shift ;;
    --no-standby) STANDBY=0; shift ;;
    *) echo "Usage: vault-run.sh [--vault NAME] [--scrub] [--no-standby]" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "ERROR: requires root (sudo)." >&2; exit 1; }
install -d -m 0700 "$STATE" "$STATE/vaults"

# Two runs at once would open the same vault twice.
exec 9> "$STATE/run.lock"
flock -n 9 || { echo "ERROR: another vault run is in progress." >&2; exit 1; }

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
jesc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n\t' '  '; }
T0=$(date +%s); STARTED="$(now)"
RESULT="failed"; STEPS=""; VNAME=""; VUUID=""; VMODE=""; PROOF=""; BYTES_NEW=0
MAPPER=""; MNT=""; DEV=""; DISK=""; OPENED=0; MOUNTED=0; CLOSED_OK=1
LOG="$STATE/last-run.log"; : > "$LOG"; chmod 600 "$LOG"

step() {   # $1 name, $2 ok|FAIL|skipped, $3 detail
  # Not through one long-lived tee: a stop signal to the whole process
  # group kills that tee, and the next echo would end this script before
  # it wrote its record (measured, K7).
  printf '[%s] %s: %s%s\n' "$(date +%H:%M:%S)" "$1" "$2" "${3:+ -- $3}" | tee -a "$LOG" 2>/dev/null || true
  STEPS="${STEPS}${STEPS:+,}{\"step\":\"$(jesc "$1")\",\"result\":\"$2\",\"detail\":\"$(jesc "${3:-}")\"}"
}

# --- the vault record (shell-sourceable key=value) -------------------------
rec_get() { [ -f "$STATE/vaults/$1.rec" ] && ( . "$STATE/vaults/$1.rec"; eval "printf '%s' \"\${$2:-}\"" ); }
rec_set() {   # $1 vault, then key=value ...
  local f="$STATE/vaults/$1.rec" n="$1"; shift
  touch "$f"; chmod 600 "$f"
  local kv k
  for kv in "$@"; do
    k="${kv%%=*}"
    grep -v "^$k=" "$f" > "$f.new" 2>/dev/null || true
    printf '%s=%q\n' "$k" "${kv#*=}" >> "$f.new"; mv "$f.new" "$f"
  done
}

# --- close: always, whatever happened --------------------------------------
close_vault() {
  [ "$MOUNTED" -eq 1 ] || [ "$OPENED" -eq 1 ] || return 0
  sync
  if [ "$MOUNTED" -eq 1 ]; then
    local i
    for i in 1 2 3; do
      umount "$MNT" 2>/dev/null && { MOUNTED=0; break; }
      sleep 2
    done
    if [ "$MOUNTED" -eq 1 ]; then
      CLOSED_OK=0; step close FAIL "$MNT is busy: $(fuser -vm "$MNT" 2>&1 | tr '\n' ' ')"
      return 1
    fi
  fi
  if [ "$OPENED" -eq 1 ]; then
    cryptsetup close "$MAPPER" 2>/dev/null || { sleep 2; cryptsetup close "$MAPPER" 2>/dev/null; } \
      || { CLOSED_OK=0; step close FAIL "cryptsetup could not close $MAPPER"; return 1; }
    OPENED=0
  fi
  step close ok "unmounted and closed"
}

finish() {
  local rc=$?
  trap - EXIT
  close_vault || true
  # Spin down the disk (best effort, measured not assumed: the result is
  # recorded either way). The disk's parent is remembered from before the
  # close, because afterwards there is no mapper to ask.
  if [ -n "$DISK" ] && [ "$STANDBY" -eq 1 ] && [ "$CLOSED_OK" -eq 1 ] && [ -b "$DISK" ]; then
    if out="$(hdparm -y "$DISK" 2>&1)"; then step standby ok "$(printf '%s' "$out" | tr '\n' ' ')"
    else step standby "not-supported" "$(printf '%s' "$out" | tr '\n' ' ')"; fi
  fi
  local dur=$(( $(date +%s) - T0 ))
  [ "$CLOSED_OK" -eq 1 ] || { RESULT="failed"; rc=3; }
  [ "$RESULT" = "ok" ] || { [ "$rc" -eq 0 ] && rc=1; }
  local json
  json="{\"schema\":\"0.1\",\"kind\":\"vault-run\",\"started\":\"$STARTED\",\"finished\":\"$(now)\",\"seconds\":$dur,\"result\":\"$RESULT\",\"vault\":\"$(jesc "$VNAME")\",\"uuid\":\"$VUUID\",\"mode\":\"$VMODE\",\"vault_closed\":$([ "$CLOSED_OK" -eq 1 ] && echo true || echo false),\"new_bytes\":$BYTES_NEW,\"proof\":\"$(jesc "$PROOF")\",\"steps\":[$STEPS]}"
  printf '%s\n' "$json" > "$STATE/last-run.json.new" && mv "$STATE/last-run.json.new" "$STATE/last-run.json"
  printf '%s\n' "$json" >> "$STATE/runs.jsonl"
  chmod 600 "$STATE/last-run.json" "$STATE/runs.jsonl"
  [ -n "$VNAME" ] && [ "$RESULT" = "ok" ] && rec_set "$VNAME" last_complete="$(now)"
  exit "$rc"
}
trap finish EXIT
trap 'exit 143' TERM INT HUP

# --- 1. which vault is in? -------------------------------------------------
# NOT nullglob: with it, `ls` of an unmatched glob has no arguments and lists the
# CURRENT DIRECTORY -- measured, it made a run treat the working directory as an
# inbox. Unmatched globs stay literal and every loop guards for that.
INSERTED=()
for c in "$CONF_DIR"/*.conf; do
  [ -e "$c" ] || continue
  ( . "$c"; echo "$NAME" ) >/dev/null || continue
  n="$( . "$c"; echo "$NAME")"; u="$( . "$c"; echo "$UUID")"
  [ -z "$(rec_get "$n" FORGOTTEN)" ] || continue
  [ -z "$WANT" ] || [ "$WANT" = "$n" ] || continue
  d="$(blkid -U "$u" 2>/dev/null || true)"
  if [ -n "$d" ]; then
    INSERTED+=("$n"); rec_set "$n" inserted=yes last_seen="$(now)"
    # "Since when" is the start of the CURRENT insertion: it only moves when the
    # vault was seen absent in between (a swap), never at every run.
    [ "$(rec_get "$n" was_inserted)" = yes ] || rec_set "$n" inserted_since="$(now)"
    rec_set "$n" was_inserted=yes
  else
    rec_set "$n" inserted=no was_inserted=no inserted_since=""
  fi
done
if [ "${#INSERTED[@]}" -eq 0 ]; then
  step find FAIL "no vault of the set is inserted (known: $(ls "$CONF_DIR"/*.conf 2>/dev/null | wc -l))"
  exit 1
fi
if [ "${#INSERTED[@]}" -gt 1 ]; then
  # Exactly one is inserted at a time (RFC-0053 2). Which of two to fill
  # is not a guess this script makes.
  step find FAIL "more than one vault is inserted: ${INSERTED[*]}"
  exit 1
fi
VNAME="${INSERTED[0]}"
. "$CONF_DIR/$VNAME.conf"
VUUID="$UUID"; VMODE="$MODE"
DEV="$(blkid -U "$UUID")"
DISK="/dev/$(lsblk -no PKNAME "$DEV" 2>/dev/null | head -1)"; [ "$DISK" = "/dev/" ] && DISK=""
[ -n "$DISK" ] || DISK="$DEV"
step find ok "$VNAME ($MODE) on $DEV"

# What this mode can prove -- stated in its own words (RFC-0053 3.1).
case "$MODE" in
  luks)     PROOF_OK="read back: the file on the disk equals the checksum the source recorded" ;;
  luks+age) PROOF_OK="ciphertext intact: read back equals what was written; clear checksum verified before encrypting; NOT proven: that it decrypts (the key is on paper)" ;;
  age)      PROOF_OK="ciphertext intact: read back equals what was written; clear checksum verified before encrypting; NOT proven: that it decrypts (the key is on paper); file names and sizes are visible on this disk" ;;
esac

# --- 2. open and mount -----------------------------------------------------
# A run that was killed hard (SIGKILL, the oom killer) cannot run its own
# cleanup and leaves the vault OPEN. The next run finds that first and
# closes it, and says so. A power cut needs no such step: after a reboot
# nothing is mapped.
STALE_MAPPER="oaap-vault-$VNAME"
if mountpoint -q "$MNT" 2>/dev/null || [ -e "/dev/mapper/$STALE_MAPPER" ]; then
  umount "$MNT" 2>/dev/null
  [ -e "/dev/mapper/$STALE_MAPPER" ] && cryptsetup close "$STALE_MAPPER" 2>/dev/null
  if mountpoint -q "$MNT" 2>/dev/null || [ -e "/dev/mapper/$STALE_MAPPER" ]; then step recover FAIL "a previous run left the vault open and it cannot be closed"; exit 3; fi
  step recover ok "a previous run left the vault open; closed it first"
fi
case "$MODE" in
  luks|luks+age)
    MAPPER="oaap-vault-$VNAME"; KEYFILE="$CONF_DIR/$VNAME.key"
    [ -r "$KEYFILE" ] || { step open FAIL "key file $KEYFILE is not readable"; exit 1; }
    if ! out="$(cryptsetup open --key-file "$KEYFILE" "$DEV" "$MAPPER" 2>&1)"; then
      step open FAIL "$out"; exit 1
    fi
    OPENED=1; FSDEV="/dev/mapper/$MAPPER"
    ;;
  age) FSDEV="$DEV" ;;
esac
mkdir -p "$MNT"
if ! out="$(mount -o noatime,nodev,nosuid,noexec "$FSDEV" "$MNT" 2>&1)"; then
  step mount FAIL "$out"; exit 1
fi
MOUNTED=1
step open ok "mounted on $MNT"

# --- 3. pull ---------------------------------------------------------------
# A source is either PULLED (the puller reaches it) or PUSHED (it left its
# archives in the inbox, INBOX=1) -- never both. Pushed archives are taken
# oldest first, one at a time through the same pull code, so checksum,
# generations and the vault record are the same for both ways in.
INBOX_ROOT="${OAAP_INBOX:-/var/lib/oaap-vault/inbox}"
INBOX_DONE=()
PULLED=0; PULL_FAIL=0; SOURCES=0
pull_once() {   # $1 node, $2 = remote-dir override or "", rest = extra args
  local node="$1" rd="$2"; shift 2
  local args=(--node "$node" --to "$MNT" "$@")
  [ -z "$rd" ] || args+=(--remote-dir "$rd")
  case "$MODE" in luks+age|age) [ "${SOURCE_AGE:-0}" = 1 ] || args+=(--age-recipient "$CONF_DIR/$VNAME.age-recipient") ;; esac
  "$PULL_BIN" "${args[@]}"
}
for c in "$PULL_CONF_DIR"/*.conf; do
  [ -e "$c" ] || continue
  ( . "$c"; [ "${VAULT:-0}" = 1 ] ) || continue
  SOURCES=$((SOURCES + 1))
  node="$( . "$c"; echo "$NODE")"
  inbox="$( . "$c"; echo "${INBOX:-0}")"; SOURCE_AGE="$( . "$c"; echo "${SOURCE_AGE:-0}")"
  common="$( . "$c"; a=(--daily "$DAILY" --weekly "$WEEKLY" --monthly "$MONTHLY"); printf '%s\n' "${a[@]}")"
  cargs=(); while IFS= read -r l; do cargs+=("$l"); done <<< "$common"
  [ "$SOURCE_AGE" = 1 ] && cargs+=(--source-age)
  ok_node=1
  if [ "$SOURCE_AGE" = 1 ] && [ "$MODE" = luks ]; then step "pull $node" FAIL "SOURCE_AGE needs an age vault (luks+age or age), this one is luks"; PULL_FAIL=$((PULL_FAIL + 1)); continue; fi
  if [ "$inbox" = 1 ]; then
    idir="$INBOX_ROOT/$node"
    mapfile -t items < <(ls -1tr "$idir"/oaap-backup-*.tar.gz "$idir"/oaap-backup-*.tar.gz.age 2>/dev/null)
    if [ "${#items[@]}" -eq 0 ]; then
      step "pull $node" "empty" "the inbox holds nothing -- did $node push since the last run?"
    fi
    for it in "${items[@]}"; do
      if [ ! -e "$it.sha256" ]; then step "pull $node" "skipped" "$(basename "$it") has no checksum file yet (push incomplete)"; continue; fi
      pick="$(mktemp -d "$STATE/pick.XXXXXX")"; ln -s "$it" "$pick/"; ln -s "$it.sha256" "$pick/"
      if pull_once "$node" "$pick" --local "${cargs[@]}"; then
        INBOX_DONE+=("$it" "$it.sha256"); PULLED=$((PULLED + 1))
        step "pull $node" ok "$(basename "$it"): $(sed -n 's/.*"message": "\(.*\)",/\1/p' "$MNT/$node/status.json" 2>/dev/null | head -1)"
        sz="$(sed -n 's/.*"bytes": \([0-9]*\),.*/\1/p' "$MNT/$node/status.json" | head -1)"
        fetched="$(sed -n 's/.*"fetched": "\(.*\)",/\1/p' "$MNT/$node/status.json" | head -1)"
        [ -n "$fetched" ] && BYTES_NEW=$((BYTES_NEW + ${sz:-0}))
      else
        ok_node=0; step "pull $node" FAIL "$(basename "$it"): $(sed -n 's/.*"message": "\(.*\)",/\1/p' "$MNT/$node/status.json" 2>/dev/null | head -1)"
      fi
      rm -rf "$pick"
    done
    [ "$ok_node" -eq 1 ] || PULL_FAIL=$((PULL_FAIL + 1))
    continue
  fi
  extra="$( . "$c"; a=(); [ "${LOCAL:-0}" = 1 ] && a+=(--local) || a+=(--host "$HOST" --user "$USER" --key "$KEY"); printf '%s\n' "${a[@]}")"
  eargs=(); while IFS= read -r l; do eargs+=("$l"); done <<< "$extra"
  rd="$( . "$c"; echo "${REMOTE_DIR:-}")"
  if pull_once "$node" "$rd" "${eargs[@]}" "${cargs[@]}"; then
    PULLED=$((PULLED + 1))
    step "pull $node" ok "$(sed -n 's/.*"message": "\(.*\)",/\1/p' "$MNT/$node/status.json" 2>/dev/null | head -1)"
    sz="$(sed -n 's/.*"bytes": \([0-9]*\),.*/\1/p' "$MNT/$node/status.json" | head -1)"
    fetched="$(sed -n 's/.*"fetched": "\(.*\)",/\1/p' "$MNT/$node/status.json" | head -1)"
    [ -n "$fetched" ] && BYTES_NEW=$((BYTES_NEW + ${sz:-0}))
  else
    PULL_FAIL=$((PULL_FAIL + 1)); step "pull $node" FAIL "$(sed -n 's/.*"message": "\(.*\)",/\1/p' "$MNT/$node/status.json" 2>/dev/null | head -1)"
  fi
done
[ "$SOURCES" -gt 0 ] || { step pull FAIL "no source is configured for the vault (VAULT=1 in $PULL_CONF_DIR/*.conf)"; exit 1; }
[ "$PULL_FAIL" -eq 0 ] || exit 1

# --- 4. read back ----------------------------------------------------------
# The page cache would answer for the disk. Dropping it makes the next read
# come from the medium -- without this the check can pass on a vault that
# never wrote anything (messen-statt-schliessen).
sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
case "$MODE" in luks) SFX="" ;; *) SFX=".age" ;; esac
BAD=0; CHECKED=0
for ddir in "$MNT"/*/daily; do
  [ -d "$ddir" ] || continue
  node="$(basename "$(dirname "$ddir")")"
  if [ "$SCRUB" -eq 1 ]; then mapfile -t files < <(ls -1 "$MNT/$node"/{daily,weekly,monthly}/oaap-backup-*.tar.gz$SFX 2>/dev/null)
  else mapfile -t files < <(ls -1t "$ddir"/oaap-backup-*.tar.gz$SFX 2>/dev/null | head -1); fi
  # Everything this run took from the inbox is read back too, not only the
  # newest: the inbox lets go of ALL of it afterwards.
  for d in "${INBOX_DONE[@]}"; do
    case "$d" in *.sha256) continue ;; esac
    b="$(basename "$d")"; case "$b" in *.age) ;; *) b="$b$SFX" ;; esac
    [ -e "$ddir/$b" ] && [[ " ${files[*]} " != *" $ddir/$b "* ]] && files+=("$ddir/$b")
  done
  [ "${#files[@]}" -gt 0 ] || { step "read-back $node" FAIL "no archive on the vault"; BAD=1; continue; }
  for f in "${files[@]}"; do
    want="$(cut -d' ' -f1 "$f.sha256" 2>/dev/null)"
    got="$(sha256sum "$f" | cut -d' ' -f1)"
    if [ -z "$want" ]; then step "read-back $(basename "$f")" FAIL "no checksum file beside it"; BAD=1
    elif [ "$want" != "$got" ]; then step "read-back $(basename "$f")" FAIL "the disk holds different bytes than were written"; BAD=1
    else
      case "$MODE" in
        luks) gzip -t "$f" 2>/dev/null && ok_extra="gzip stream ok" || { step "read-back $(basename "$f")" FAIL "checksum equal but not a gzip stream"; BAD=1; continue; } ;;
        *)    head -c 21 "$f" | grep -q '^age-encryption.org/v1' && ok_extra="age header ok" || { step "read-back $(basename "$f")" FAIL "checksum equal but no age header"; BAD=1; continue; } ;;
      esac
      CHECKED=$((CHECKED + 1)); step "read-back $(basename "$f")" ok "$ok_extra"
    fi
  done
done
[ "$BAD" -eq 0 ] && [ "$CHECKED" -gt 0 ] || { PROOF="NOT proven: read-back failed or checked nothing"; exit 1; }
PROOF="$PROOF_OK"
# Per source, the stamp of the newest archive this vault holds -- so that
# the status can say "bernd has not given anything for 4 days" even when
# every run was green. An empty inbox or an unchanged source looks like
# success to a run; only the age of the newest archive tells.
install -d -m 0700 "$STATE/sources"
for ddir in "$MNT"/*/daily; do
  [ -d "$ddir" ] || continue
  node="$(basename "$(dirname "$ddir")")"
  newest="$(ls -1t "$ddir"/oaap-backup-*.tar.gz$SFX 2>/dev/null | head -1)"
  [ -n "$newest" ] || continue
  stamp="$(basename "$newest" | grep -oE '[0-9]{8}-[0-9]{6}' | head -1)"
  printf 'NEWEST=%q\nSTAMP=%q\nVAULT=%q\nCHECKED=%q\n' "$(basename "$newest")" "$stamp" "$VNAME" "$(now)" > "$STATE/sources/$node.rec"
done
# Only now, with the read-back done, does the inbox let go: what was pushed
# is on the vault AND was read back from it. Never before.
if [ "${#INBOX_DONE[@]}" -gt 0 ]; then
  rm -f "${INBOX_DONE[@]}"
  step inbox ok "released $(( ${#INBOX_DONE[@]} / 2 )) archive(s) after the read-back"
fi

# What is left on the vault, for the record (free bytes included).
rec_set "$VNAME" free_bytes="$(df -PB1 "$MNT" | awk 'NR==2{print $4}')" \
  generations="$(ls -1 "$MNT"/*/{daily,weekly,monthly}/oaap-backup-*.tar.gz$SFX 2>/dev/null | wc -l)"
RESULT="ok"
exit 0
