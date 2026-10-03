#!/usr/bin/env bash
# What the puller knows about its set of vaults (RFC-0053 3.3, 4).
#
#   oaap-vault-status            a table, and exit 1 if a swap is due or a vault is missing
#   oaap-vault-status --json     the same, machine-readable
#   oaap-vault-status --forget NAME   record a vault as lost
#
# "Swap due":  the inserted vault has been in longer than the rhythm.
# "Missing":   a vault not seen for longer than two rhythms.
# Both are MEASURED here from the last runs' records, never from intention.
set -uo pipefail
CONF_DIR="${OAAP_VAULT_CONF:-/etc/oaap-vault}"
STATE="${OAAP_VAULT_STATE:-/var/lib/oaap-vault}"
RHYTHM="${OAAP_VAULT_RHYTHM_DAYS:-7}"
JSON=0; FORGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --forget) FORGET="$2"; shift 2 ;;
    *) echo "Usage: vault-status.sh [--json] [--forget NAME]" >&2; exit 2 ;;
  esac
done

rec() { [ -f "$STATE/vaults/$1.rec" ] && ( . "$STATE/vaults/$1.rec"; eval "printf '%s' \"\${$2:-}\"" ); }
nowe=$(date +%s)
days_since() { [ -n "$1" ] && echo $(( (nowe - $(date -d "$1" +%s)) / 86400 )) || echo ""; }

if [ -n "$FORGET" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: requires root." >&2; exit 1; }
  [ -f "$CONF_DIR/$FORGET.conf" ] || { echo "ERROR: no vault called $FORGET." >&2; exit 1; }
  install -d -m 0700 "$STATE/vaults"
  touch "$STATE/vaults/$FORGET.rec"; chmod 600 "$STATE/vaults/$FORGET.rec"
  grep -v '^FORGOTTEN=' "$STATE/vaults/$FORGET.rec" > "$STATE/vaults/$FORGET.rec.new" 2>/dev/null || true
  printf 'FORGOTTEN=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATE/vaults/$FORGET.rec.new"
  mv "$STATE/vaults/$FORGET.rec.new" "$STATE/vaults/$FORGET.rec"
  echo "$FORGET recorded as LOST. Last complete: $(rec "$FORGET" last_complete)."
  echo "Its mode protects the finder, not you: rotate any secret that was on the lost disk's machine."
  exit 0
fi

ATTENTION=0; ROWS=""; JROWS=""
# no nullglob (see vault-run.sh): an unmatched glob must not turn `ls` into a listing of the cwd
for c in "$CONF_DIR"/*.conf; do
  [ -e "$c" ] || continue
  n="$( . "$c"; echo "$NAME")"; mode="$( . "$c"; echo "$MODE")"; uuid="$( . "$c"; echo "$UUID")"
  ins="$(rec "$n" inserted)"; since="$(rec "$n" inserted_since)"; seen="$(rec "$n" last_seen)"
  comp="$(rec "$n" last_complete)"; forg="$(rec "$n" FORGOTTEN)"
  free="$(rec "$n" free_bytes)"; gens="$(rec "$n" generations)"
  state="ok"
  if [ -n "$forg" ]; then state="LOST"
  elif [ "$ins" = yes ]; then
    d="$(days_since "$since")"
    [ -n "$d" ] && [ "$d" -ge "$RHYTHM" ] && state="SWAP DUE (in for ${d} d)"
  else
    d="$(days_since "$seen")"
    if [ -z "$seen" ]; then state="never seen"
    elif [ "$d" -gt $((RHYTHM * 2)) ]; then state="MISSING (unseen ${d} d)"; fi
  fi
  case "$state" in "SWAP DUE"*|MISSING*) ATTENTION=1 ;; esac
  case "$mode" in
    luks) can="read back: file on disk equals the source's checksum" ;;
    luks+age) can="ciphertext intact; clear checksum verified before encrypting; decryptability NOT proven" ;;
    age) can="ciphertext intact; clear checksum verified before encrypting; decryptability NOT proven; names/sizes visible" ;;
  esac
  ROWS="${ROWS}$(printf '%-10s %-9s %-9s %-26s last complete: %-21s gens: %-3s free: %s\n' "$n" "$mode" "$([ "$ins" = yes ] && echo inserted || echo away)" "$state" "${comp:-never}" "${gens:--}" "$([ -n "$free" ] && echo $((free / 1048576))" MB" || echo -)")
"
  JROWS="${JROWS}${JROWS:+,}{\"name\":\"$n\",\"mode\":\"$mode\",\"uuid\":\"$uuid\",\"inserted\":$([ "$ins" = yes ] && echo true || echo false),\"inserted_since\":\"$since\",\"last_seen\":\"$seen\",\"last_complete\":\"$comp\",\"state\":\"$state\",\"generations\":${gens:-0},\"free_bytes\":${free:-0},\"proof_this_mode_allows\":\"$can\"}"
done
# Sources: how old is the newest archive the vaults hold? (A green run proves
# the pipeline, not that the source is still giving.)
SRCROWS=""
for r in "$STATE"/sources/*.rec; do
  [ -e "$r" ] || continue
  sn="$(basename "$r" .rec)"; st="$( . "$r"; echo "$STAMP")"; sv="$( . "$r"; echo "$VAULT")"
  sd="$(date -d "${st:0:4}-${st:4:2}-${st:6:2} ${st:9:2}:${st:11:2}:${st:13:2}" +%s 2>/dev/null || echo 0)"
  age=$(( (nowe - sd) / 86400 )); flag="ok"; [ "$age" -ge 2 ] && { flag="STALE (newest archive $age d old)"; ATTENTION=1; }
  SRCROWS="${SRCROWS}$(printf 'source %-12s newest archive %s  (in %s)  %s\n' "$sn" "$st" "$sv" "$flag")
"
done
nextrun="$(systemctl list-timers oaap-vault-run.timer --no-pager 2>/dev/null | awk 'NR==2{print $1" "$2" "$3}')"
last="$STATE/last-run.json"
if [ "$JSON" -eq 1 ]; then
  printf '{"schema":"0.1","rhythm_days":%s,"attention":%s,"next_run":"%s","vaults":[%s],"last_run":%s}\n' \
    "$RHYTHM" "$([ $ATTENTION -eq 1 ] && echo true || echo false)" "$nextrun" "$JROWS" "$([ -f "$last" ] && cat "$last" || echo null)"
else
  printf "%s" "$ROWS"; printf "%s" "$SRCROWS"
  echo "next run: ${nextrun:-no timer}"
  [ -f "$last" ] && echo "last run: $(sed -n 's/.*"finished":"\([^"]*\)".*"seconds":\([0-9]*\),"result":"\([^"]*\)".*/\1  \2 s  \3/p' "$last")"
fi
exit $ATTENTION
