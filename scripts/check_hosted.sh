#!/usr/bin/env bash
# Brings up the hosted stack with the fake provider, runs the hosted browser
# spec, probes the rate limit, then checks that nothing reached disk or logs.
# Run it from anywhere; it always removes its own stack.
set -u
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

KEY=test-canary-key-7f3a
TEXT=canary-player-text-91c2
failed=0

dc() {
  docker compose -p coherence-standalone-test \
    -f docker-compose.yml -f docker-compose.hosted.yml -f scripts/hosted-check.override.yml "$@"
}

fail() {
  echo "FAIL: $*"
  failed=1
}

teardown() {
  docker compose -p coherence-standalone-test \
    -f docker-compose.yml -f docker-compose.hosted.yml -f scripts/hosted-check.override.yml \
    --profile test down -v --remove-orphans
}
trap teardown EXIT

PROBE=$(cat <<'JS'
const base = process.env.BASE_URL;
const post = (session, extra = {}) => fetch(base + '/api/game/turn', {
  method: 'POST',
  headers: {
    'content-type': 'application/json',
    'X-Coherence-Session': session,
    'X-Provider': 'anthropic',
    'X-Provider-Key': 'test-canary-key-7f3a',
    ...extra,
  },
  body: JSON.stringify({ save: {}, player_input: 'canary-player-text-91c2' }),
}).then((r) => r.status);
(async () => {
  const a = crypto.randomUUID();
  const b = crypto.randomUUID();
  const first = await post(a);
  const sameSession = await post(a);
  const otherSession = await post(b);
  // forwarding headers from a direct client must not open a new bucket
  const forged = await post(crypto.randomUUID(), {
    'X-Forwarded-For': '198.51.100.9',
    Forwarded: 'for=198.51.100.9',
    'X-Real-IP': '198.51.100.9',
    'CF-Connecting-IP': '198.51.100.9',
  });
  const forgedTwo = await post(crypto.randomUUID(), { 'X-Forwarded-For': '198.51.100.9, 198.51.100.10' });
  console.log(JSON.stringify({ first, sameSession, otherSession, forged, forgedTwo }));
  const ok = first !== 429 && first !== 428 && sameSession === 429 && otherSession === 429
    && forged === 429 && forgedTwo === 429;
  process.exit(ok ? 0 : 1);
})();
JS
)

BADSAVE=$(cat <<'JS'
const text = 'canary-player-text-91c2';
fetch(process.env.BASE_URL + '/api/game/turn', {
  method: 'POST',
  headers: {
    'content-type': 'application/json',
    'X-Coherence-Session': crypto.randomUUID(),
    'X-Provider': 'anthropic',
    'X-Provider-Key': 'test-canary-key-7f3a',
  },
  body: JSON.stringify({
    save: { turn: text, state: { npcs: { mira: { first_seen_turn: text, last_seen_turn: text } } } },
    player_input: text,
  }),
}).then(async (r) => {
  const body = await r.json().catch(() => null);
  const detail = body && !('error' in body) && body.detail && typeof body.detail === 'object' ? body.detail : null;
  const error = detail && typeof detail.error === 'string' ? detail.error : null;
  console.log(JSON.stringify({ malformedSave: r.status, error }));
  // the probe checks log redaction; only 400 bad_save or the redacted 500 internal is fine
  const ok = (r.status === 400 && error === 'bad_save') || (r.status === 500 && error === 'internal');
  process.exit(ok ? 0 : 1);
}, () => process.exit(1));
JS
)

dc up -d --build --wait engine web || { fail "the hosted stack did not become healthy"; exit 1; }

dc run --rm -T e2e-hosted npx playwright test specs/hosted.spec.ts || fail "hosted.spec.ts"

# one move per 15 s per session and per peer; wait out the spec's last move first
sleep 16
dc run --rm -T --no-deps e2e-hosted node -e "$PROBE" || fail "rate-limit probe"

# a malformed save whose fields carry the player-text canary; the log grep below must not find it
sleep 16
dc run --rm -T --no-deps e2e-hosted node -e "$BADSAVE" || fail "malformed-save turn"

for svc in engine web; do
  id=$(dc ps -q "$svc")
  if [ -z "$id" ]; then
    fail "$svc is not running"
    continue
  fi

  changes=$(docker diff "$id")
  [ -z "$changes" ] || fail "$svc: docker diff is not empty: $changes"

  [ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$id")" = "true" ] || fail "$svc: root filesystem is writable"

  mounts=$(docker inspect -f '{{range .Mounts}}{{.Type}}{{"\n"}}{{end}}' "$id" | sed '/^$/d')
  other=$(printf '%s\n' "$mounts" | grep -vx tmpfs || true)
  [ -z "$other" ] || fail "$svc: it has mounts that are not tmpfs: $other"
  tmpfs=$(docker inspect -f '{{range $k, $v := .HostConfig.Tmpfs}}{{$k}}{{"\n"}}{{end}}' "$id" | sed '/^$/d')
  [ "$tmpfs" = "/tmp" ] || fail "$svc: tmpfs is not exactly /tmp: $tmpfs"

  # Only empty /tmp/mat-debug-<n>.log files (written by onnxruntime) are allowed.
  files=$( { docker exec "$id" find /tmp -type f ! -empty; docker exec "$id" find /tmp -type f | grep -Ev '^/tmp/mat-debug-[0-9]+\.log$'; } || true)
  [ -z "$files" ] || fail "$svc: files left in /tmp: $files"
done

logs=$(dc logs --no-color engine web)
for canary in "$KEY" "$TEXT"; do
  if printf '%s\n' "$logs" | grep -qF -- "$canary"; then
    fail "a canary reached the engine or web logs: $canary"
  fi
done

if [ "$failed" -eq 0 ]; then echo "hosted check passed"; fi
exit "$failed"
