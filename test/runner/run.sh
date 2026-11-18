#!/usr/bin/env bash
# Run the reference tests in a Linux container (see Dockerfile here).
# The whole oaap-root checkout is mounted, so tests that read the
# neighbouring oaap-spec / oaap-apps find them. test/.env is never
# read by the tests and is hidden from the container anyway.
#
#   bash test/runner/run.sh                 # every test
#   bash test/runner/run.sh test_x.py ...   # just these
#
# Exit code 0 only when every file passed. A file that reports SKIP
# (exit 2) counts as red: a skipped catalogue is not a green one.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REF="$(cd "$HERE/../.." && pwd)"
ROOT="$(cd "$REF/.." && pwd)"
CTX="$(mktemp -d)"
NET="oaap-ref-tests-$$"
PG="oaap-ref-tests-pg-$$"
cleanup() {
  docker rm -f "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$CTX"
}
trap cleanup EXIT
mkdir -p "$CTX/requirements"
for f in "$REF"/platform/services/*/requirements.txt; do
  cp "$f" "$CTX/requirements/$(basename "$(dirname "$f")").txt"
done
cp "$HERE/Dockerfile" "$CTX/"
docker build -q -t oaap-ref-tests "$CTX" >/dev/null
TIMEOUT="${OAAP_TEST_TIMEOUT:-300}"
# MSYS: keep the container paths as they are
export MSYS_NO_PATHCONV=1
EMPTY="$CTX/empty.env"; : > "$EMPTY"
# Git Bash on Windows: hand Docker a Windows path, not /c/... or /tmp/...
hostpath() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else echo "$1"; fi; }

# The time-series catalogue (RFC-0086) needs a real PostgreSQL 16: a
# throwaway one on a network of its own, gone when this script ends.
# OAAP_TEST_NO_PG=1 leaves it out (those two files then report SKIP).
PGENV=()
if [ -z "${OAAP_TEST_NO_PG:-}" ]; then
  docker network create "$NET" >/dev/null
  docker run -d --name "$PG" --network "$NET" -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=ts postgres:16 >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$PG" pg_isready -U postgres -d ts >/dev/null 2>&1 && break
    sleep 1
  done
  PGENV=(--network "$NET"
         -e "TS_DSN=host=$PG dbname=ts user=postgres password=test")
fi

docker run --rm -v "$(hostpath "$ROOT"):/w" \
  -v "$(hostpath "$EMPTY"):/w/oaap-reference/test/.env:ro" "${PGENV[@]}" \
  -w /w/oaap-reference/test -e TIMEOUT="$TIMEOUT" oaap-ref-tests bash -c '
    files=("$@"); [ ${#files[@]} -eq 0 ] && files=(test_*.py)
    mkdir -p /tmp/logs; failed=()
    for t in "${files[@]}"; do
      if timeout "$TIMEOUT" python3 "$t" > "/tmp/logs/$t.log" 2>&1; then :; else
        rc=$?; failed+=("$t"); echo "RC=$rc $t"; tail -n 15 "/tmp/logs/$t.log" | sed "s/^/    /"
      fi
    done
    echo "== ${#files[@]} Dateien, ${#failed[@]} rot"
    [ ${#failed[@]} -eq 0 ]' _ "$@"
