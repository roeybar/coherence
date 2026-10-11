#!/usr/bin/env bash
# Runs the whole suite in containers, one step at a time. Each step is
# followed by teardown. The full log goes to test-output.log, and the exit
# code is non-zero if any step failed.
set -u
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

LOG=test-output.log
: > "$LOG"
failed=0

# The default engine service on a fresh keys volume: the first unlock must
# create the store. .env.example stands in for a user's .env.
FRESH_OVERRIDE='services:
  engine:
    env_file: !override
      - .env.example'
UNLOCK_PY='import json, urllib.error, urllib.request as u
req = u.Request("http://127.0.0.1:8000/api/keys/unlock", method="POST",
                data=json.dumps({"passphrase": "fresh-volume-check"}).encode(),
                headers={"content-type": "application/json"})
try:
    print(u.urlopen(req, timeout=5).status)
except urllib.error.HTTPError as e:
    print(e.code)'
# The key store status on a fresh volume, before any unlock: code and error kind.
STATUS_PY='import json, urllib.error, urllib.request as u
try:
    print(u.urlopen("http://127.0.0.1:8000/api/keys/status", timeout=5).status, "-")
except urllib.error.HTTPError as e:
    print(e.code, json.loads(e.read() or b"{}").get("detail", {}).get("error", "-"))'

fresh_engine() {
  printf '%s\n' "$FRESH_OVERRIDE" | docker compose -p coherence-standalone-test -f docker-compose.yml -f - "$@"
}

fresh_unlock() {
  fresh_engine up -d --build engine || return 1
  local id code i
  id=$(fresh_engine ps -q engine)
  [ -n "$id" ] || { echo "engine is not running"; return 1; }
  local status
  for i in $(seq 1 60); do
    status=$(docker exec "$id" python -c "$STATUS_PY" 2>/dev/null) && break
    sleep 2
  done
  echo "key store status on a fresh volume: ${status:-no answer}"
  [ "$status" = "428 key_store_locked" ] || return 1
  for i in $(seq 1 60); do
    code=$(docker exec "$id" python -c "$UNLOCK_PY" 2>/dev/null) && break
    sleep 2
  done
  echo "first unlock on a fresh volume: ${code:-no answer}"
  [ "$code" = "200" ]
}

step() {
  local name=$1
  shift
  # ONLY="<step name>" runs that one step.
  if [ -n "${ONLY:-}" ] && [ "$name" != "$ONLY" ]; then
    return 0
  fi
  echo "=== $name" | tee -a "$LOG"
  "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  docker compose -p coherence-standalone-test --profile test down -v 2>&1 | tee -a "$LOG"
  if [ "$rc" -ne 0 ]; then
    echo "=== FAILED: $name (exit $rc)" | tee -a "$LOG"
    failed=1
  fi
}

build_images() {
  docker compose -p coherence-standalone-test --profile test build &&
    docker compose -p coherence-standalone-test -f docker-compose.yml -f docker-compose.hosted.yml \
      -f scripts/hosted-check.override.yml --profile test build e2e-hosted
}

# Rebuild first, so no step runs an image older than the tree.
step "build images" \
  build_images
step "compose checks" \
  docker compose -p coherence-standalone-test --profile test run --rm -T repo-check python scripts/check_compose.py
step "engine tests" \
  docker compose -p coherence-standalone-test --profile test run --rm -T engine-test
step "lint" \
  docker compose -p coherence-standalone-test --profile test run --rm -T engine-test sh -c 'ruff check . && ruff format --check . && vulture --min-confidence 80 app tests'
step "fresh keys volume unlock" \
  fresh_unlock
step "engine tests, hosted" \
  docker compose -p coherence-standalone-test --profile test run --rm -T engine-test-hosted
step "web tests" \
  docker compose -p coherence-standalone-test --profile test run --rm -T web-test
step "e2e, selfhost and screenshot" \
  docker compose -p coherence-standalone-test --profile test run --rm -T e2e npx playwright test specs/selfhost.spec.ts specs/screenshot.spec.ts specs/content.spec.ts specs/design.spec.ts specs/newgame.spec.ts
step "hosted check" \
  bash scripts/check_hosted.sh
step "repo checks" \
  docker compose -p coherence-standalone-test --profile test run --rm -T repo-check sh -c 'python tools/leak_scan.py --generic && python -m pytest -p no:cacheprovider -q tools/test_leak_scan.py && python scripts/check_readme.py'

if [ "$failed" -eq 0 ]; then
  echo "all steps passed" | tee -a "$LOG"
else
  echo "some steps failed" | tee -a "$LOG"
fi
exit "$failed"
