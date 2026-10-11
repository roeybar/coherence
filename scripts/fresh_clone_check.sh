#!/usr/bin/env bash
# Usage: fresh_clone_check.sh <src> <ref> <log>
# Clones <ref> of <src> into a scratch directory, runs the README quickstart
# block as written, waits for the health check and the page, then stops what
# it started and removes the scratch directory.
set -u

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <src> <ref> <log>" >&2
  exit 2
fi
src=$1
ref=$2
log=$3
case $log in /*) ;; *) log=$PWD/$log ;; esac
: > "$log"

tmp=$(mktemp -d)
port=18080
export COMPOSE_PROJECT_NAME=coherence-standalone-fresh
export WEB_PORT=$port

cleanup() {
  if [ -d "$tmp/repo" ]; then
    (cd "$tmp/repo" && docker compose -p coherence-standalone-fresh down -v) >> "$log" 2>&1
  fi
  if [ -n "$tmp" ] && [ -d "$tmp" ]; then
    rm -rf "$tmp"
  fi
}
trap cleanup EXIT

run() {
  git clone -q --no-checkout "$src" "$tmp/repo" || return 1
  if git -C "$tmp/repo" rev-parse --verify -q "$ref^{commit}" > /dev/null; then
    git -C "$tmp/repo" checkout -q --detach "$ref" || return 1
  else
    git -C "$tmp/repo" checkout -q --detach "origin/$ref" || return 1
  fi
  echo "cloned $(git -C "$tmp/repo" rev-parse HEAD)"

  awk '/<!-- quickstart -->/{on=1; next} /<!-- \/quickstart -->/{on=0} on && !/^```/ && NF' \
    "$tmp/repo/README.md" > "$tmp/quickstart.sh"
  if [ ! -s "$tmp/quickstart.sh" ]; then
    echo "no quickstart block in README.md"
    return 1
  fi
  echo "quickstart:"
  cat "$tmp/quickstart.sh"
  (cd "$tmp/repo" && sh -e "$tmp/quickstart.sh") || return 1

  deadline=$(( $(date +%s) + 180 ))
  health=0
  page=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$health" = 1 ] || { curl -fsS -o /dev/null "http://127.0.0.1:$port/api/health" 2> /dev/null && health=1; }
    [ "$page" = 1 ] || { curl -fsS -o /dev/null "http://127.0.0.1:$port/" 2> /dev/null && page=1; }
    if [ "$health" = 1 ] && [ "$page" = 1 ]; then
      echo "PASS fresh clone: /api/health and / answered 200"
      return 0
    fi
    sleep 3
  done
  echo "FAIL: health=$health page=$page after 180 s"
  (cd "$tmp/repo" && docker compose -p coherence-standalone-fresh logs --tail 50)
  return 1
}

run 2>&1 | tee -a "$log"
exit "${PIPESTATUS[0]}"
