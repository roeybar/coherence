import test from 'node:test';
import assert from 'node:assert/strict';
import { createClient, newSessionId, parseError, errorText, gateOf } from '../src/api/client.js';
import { COPY } from '../src/copy.js';

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function stub(status, body) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  return { calls, fetchImpl };
}

test('the session id is a UUIDv4 and stays constant across calls', async () => {
  assert.match(newSessionId(), UUID4);
  const { calls, fetchImpl } = stub(200, {});
  const client = createClient({ fetchImpl });
  assert.match(client.sessionId, UUID4);
  await client.config();
  await client.packs();
  await client.turn({}, 'look');
  const ids = calls.map((c) => c.init.headers['X-Coherence-Session']);
  assert.deepEqual(new Set(ids), new Set([client.sessionId]));
});

test('hosted sends X-Provider and X-Provider-Key on every request', async () => {
  const { calls, fetchImpl } = stub(200, {});
  const client = createClient({ fetchImpl });
  client.configure({ mode: 'hosted', provider: 'openai', key: 'test-canary-key-7f3a' });
  await client.turn({ name: 'x' }, 'look');
  await client.packs();
  for (const c of calls) {
    assert.equal(c.init.headers['X-Provider'], 'openai');
    assert.equal(c.init.headers['X-Provider-Key'], 'test-canary-key-7f3a');
  }
  assert.equal(calls[0].url, '/api/game/turn');
  assert.deepEqual(JSON.parse(calls[0].init.body), { save: { name: 'x' }, player_input: 'look' });
});

test('self-host never sends X-Provider-Key', async () => {
  const { calls, fetchImpl } = stub(200, {});
  const client = createClient({ fetchImpl });
  client.configure({ mode: 'selfhost', provider: 'gemini', key: 'test-canary-key-7f3a' });
  await client.turn({}, 'look');
  await client.keys.status();
  for (const c of calls) {
    assert.equal(c.init.headers['X-Provider'], 'gemini');
    assert.ok(!('X-Provider-Key' in c.init.headers));
  }
});

test('a 429 renders error.rate_limited with its seconds', async () => {
  const { fetchImpl } = stub(429, { detail: { error: 'rate_limited', retry_after: 3 } });
  const client = createClient({ fetchImpl });
  const r = await client.turn({}, 'look');
  assert.equal(r.ok, false);
  assert.equal(r.data, undefined);
  assert.equal(r.error.kind, 'rate_limited');
  assert.equal(r.error.retryAfter, 3);
  assert.equal(errorText(r.error, 15), 'You can make one move every 15 seconds. Try again in 3 s.');
});

test('every engine error kind renders its copy and nothing reloads', async () => {
  const kinds = ['byok_required', 'byok_rejected', 'bad_save', 'bad_session',
    'provider_unavailable', 'provider_rate_limited', 'body_too_large'];
  for (const kind of kinds) {
    const { fetchImpl } = stub(428, { detail: { error: kind, provider: 'anthropic' } });
    const r = await createClient({ fetchImpl }).turn({}, 'look');
    assert.equal(r.error.kind, kind);
    assert.equal(r.error.provider, 'anthropic');
    assert.equal(errorText(r.error), COPY['error.' + kind]);
  }
});

const NEW_KINDS = [
  [403, 'wrong_passphrase'], [451, 'content_blocked'], [409, 'intervention_missing'],
  [400, 'bad_pack'], [422, 'bad_request'], [404, 'unknown_provider'],
  [500, 'internal'], [503, 'busy'], [500, 'key_store_unavailable'],
];
for (const [status, kind] of NEW_KINDS) {
  test(`a ${status} ${kind} renders error.${kind}`, async () => {
    const body = { detail: { error: kind, provider: null } };
    if (kind === 'content_blocked') body.detail.category = 'violence';
    const { fetchImpl } = stub(status, body);
    const r = await createClient({ fetchImpl }).turn({}, 'look');
    assert.equal(r.ok, false);
    assert.equal(r.error.kind, kind);
    assert.equal(errorText(r.error), COPY['error.' + kind]);
  });
}

test('the three server error kinds render their exact text', () => {
  const text = (error) => errorText(parseError(500, { detail: { error, provider: null } }));
  assert.equal(text('internal'), 'Something went wrong on the server. Your move was not played. Try again.');
  assert.equal(text('busy'), 'The server is busy right now. Try again in a moment.');
  assert.equal(text('key_store_unavailable'), "The key store could not be opened. Check the server's keys volume.");
});

test('a locked key store (428 on status) is an error result, not data', async () => {
  const { fetchImpl } = stub(428, { detail: { error: 'byok_required', provider: null } });
  const r = await createClient({ fetchImpl }).keys.status();
  assert.equal(r.ok, false);
  assert.equal(r.error.status, 428);
});

test('network failures are engine_down; odd bodies become error.unknown', async () => {
  const down = createClient({ fetchImpl: async () => { throw new TypeError('offline'); } });
  assert.equal((await down.turn({}, 'look')).error.kind, 'engine_down');
  const html = createClient({ fetchImpl: async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError('html'); } }) });
  assert.equal((await html.turn({}, 'look')).error.kind, 'unknown');
  assert.deepEqual(parseError(500, { error: 'rate_limited' }), { status: 500, kind: 'unknown', provider: null, retryAfter: null, stored: [] });
});

test('key error parsing keeps only known stored providers', () => {
  const detail = { error: 'key_other_provider', provider: 'xai', stored: ['anthropic', 'fake', 'openai', null] };
  const err = parseError(428, { detail });
  assert.equal(err.kind, 'key_other_provider');
  assert.deepEqual(err.stored, ['anthropic', 'openai']);
  for (const stored of [null, 'anthropic', {}, undefined]) assert.deepEqual(parseError(428, { detail: { ...detail, stored } }).stored, []);
  assert.equal(parseError(428, { detail: { error: 'key_store_locked' } }).kind, 'key_store_locked');
  assert.equal(errorText(err), 'No API key is set for xAI Grok. Stored keys: Claude (tuned), OpenAI. Choose one of those providers in Settings, or add a key for xAI Grok.');
});

test('keys calls reject unknown provider ids without a request', async () => {
  const { calls, fetchImpl } = stub(200, {});
  const client = createClient({ fetchImpl });
  assert.equal((await client.keys.put('../admin', 'k')).ok, false);
  assert.equal(calls.length, 0);
});

test('gateOf maps gate payloads to options and passbacks', () => {
  assert.equal(gateOf({ kind: 'narrative' }), null);
  const addr = gateOf({ kind: 'addressee_clarification_needed', message: 'Who?', options: ['Mira', 'Jun'] });
  assert.deepEqual(addr.options.map((o) => o.extra), [{ addressee: 'Mira' }, { addressee: 'Jun' }]);
  const intent = gateOf({ kind: 'ability_intent_clarification_needed', options: [{ value: 'internal', label: 'Within you' }] });
  assert.deepEqual(intent.options, [{ label: 'Within you', extra: { ability_intent: 'internal' } }]);
  const esc = gateOf({ kind: 'escalation_consent_needed', consent_token: 'tok' });
  assert.deepEqual(esc.options.map((o) => o.extra), [{ consent_token: 'tok' }, { decline_escalation: true }]);
  assert.deepEqual(gateOf({ kind: 'dice_roll_needed', dice_sides: 20 }).roll, 'dice_roll');
  assert.equal(gateOf({ kind: 'craft_roll_needed' }).roll, 'craft_roll');
});

test('the web server engine_unavailable answer is engine_down', async () => {
  const { fetchImpl } = stub(502, { detail: { error: 'engine_unavailable' } });
  const r = await createClient({ fetchImpl }).turn({}, 'look');
  assert.equal(r.error.kind, 'engine_down');
  assert.equal(r.error.status, 502);
});

test('undoTo and regenerate post the engine body shapes', async () => {
  const { calls, fetchImpl } = stub(200, {});
  const client = createClient({ fetchImpl });
  await client.undoTo({ turn: 3 }, 2);
  await client.regenerate({ turn: 3 }, 'x'.repeat(1200));
  await client.health();
  assert.equal(calls[0].url, '/api/game/undo-to-turn');
  assert.deepEqual(JSON.parse(calls[0].init.body), { save: { turn: 3 }, turn_index: 2 });
  assert.equal(calls[1].url, '/api/game/regenerate');
  assert.equal(JSON.parse(calls[1].init.body).feedback.length, 1000);
  assert.equal(calls[2].url, '/api/health');
  assert.equal(calls[2].init.method, 'GET');
});

test('the moderation and action-limit kinds parse and render their copy', async () => {
  for (const [status, kind] of [[451, 'output_blocked'], [451, 'save_blocked'], [413, 'text_too_large'], [413, 'context_too_large'], [429, 'actions_rate_limited']]) {
    const { fetchImpl } = stub(status, { detail: { error: kind, provider: null, retry_after: 7 } });
    const r = await createClient({ fetchImpl }).turn({}, 'look');
    assert.equal(r.error.kind, kind);
    assert.equal(r.error.status, status);
    assert.equal(r.error.hosted, undefined);
    assert.equal(errorText(r.error), COPY['error.' + kind].replace('{retry_after}', '7'));
  }
});

test('a hosted text_too_large picks the hosted copy', async () => {
  const { fetchImpl } = stub(413, { detail: { error: 'text_too_large', provider: null } });
  const client = createClient({ fetchImpl });
  client.configure({ mode: 'hosted', provider: 'anthropic', key: 'test-canary-key-7f3a' });
  const r = await client.checkSave({});
  assert.equal(r.error.hosted, true);
  assert.equal(errorText(r.error), COPY['error.text_too_large_hosted']);
  client.configure({ mode: 'selfhost' });
  assert.equal(errorText((await client.checkSave({})).error), COPY['error.text_too_large']);
});

test('checkSave posts the save to /api/game/check', async () => {
  const { calls, fetchImpl } = stub(200, { ok: true });
  const r = await createClient({ fetchImpl }).checkSave({ turn: 3 });
  assert.deepEqual(r, { ok: true, data: { ok: true } });
  assert.equal(calls[0].url, '/api/game/check');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), { save: { turn: 3 } });
});

test('a 413 context_too_large on regenerate renders its exact text', async () => {
  const { fetchImpl } = stub(413, { detail: { error: 'context_too_large', provider: null } });
  const r = await createClient({ fetchImpl }).regenerate({ turn: 3 }, '');
  assert.equal(r.error.kind, 'context_too_large');
  assert.equal(r.error.status, 413);
  assert.equal(errorText(r.error), "This story's world state has grown too large for the game to send to the model in one move, so your move was not played. Load an earlier save or start a new game.");
});
