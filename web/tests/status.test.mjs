import test from 'node:test';
import assert from 'node:assert/strict';
import { linkStatus, storeFromRead, LOCKED_KINDS, statusText } from '../src/status.js';
import { COPY } from '../src/copy.js';
import { PROVIDERS } from '../src/api/client.js';

const SELF = { mode: 'selfhost' };
const HOSTED = { mode: 'hosted' };
const open = (keys) => ({ unlocked: true, keys, error: null });
const at = (o) => linkStatus({ cfg: SELF, engineDown: false, hostedKey: '', store: null, provider: 'p', ...o });
const fail = (kind) => ({ ok: false, error: { kind } });

test('each branch of the status, in order', () => {
  assert.deepEqual(at({ engineDown: true, cfg: null }), { level: 'error', key: 'status.engine_down' });
  assert.equal(at({ cfg: null }), null);
  assert.deepEqual(at({ cfg: HOSTED, hostedKey: 'k' }), { level: 'ok', key: 'status.ok.hosted' });
  assert.deepEqual(at({ cfg: HOSTED }), { level: 'attention', key: 'status.no_key.hosted' });
  assert.equal(at({ store: null }), null);
  assert.deepEqual(at({ store: storeFromRead(fail('engine_down')) }), { level: 'error', key: 'status.engine_down' });
  assert.deepEqual(at({ store: storeFromRead(fail('key_store_unavailable')) }), { level: 'attention', key: 'status.store_unreadable' });
  assert.deepEqual(at({ store: storeFromRead(fail('internal')) }), { level: 'attention', key: 'status.unchecked' });
  assert.deepEqual(at({ store: { unlocked: false, keys: {}, error: null } }), { level: 'attention', key: 'status.locked' });
  assert.deepEqual(at({ store: open({ q: true }), provider: 'p' }), { level: 'attention', key: 'status.no_key', vars: { provider: 'p' } });
  assert.deepEqual(at({ store: open({ p: true }) }), { level: 'ok', key: 'status.ok' });
});

test('engine down wins over everything', () => {
  for (const cfg of [null, SELF, HOSTED]) {
    assert.equal(linkStatus({ cfg, engineDown: true, hostedKey: 'k', store: open({ p: true }), provider: 'p' }).key, 'status.engine_down');
  }
});

test('a status read is sorted into one of the store states', () => {
  for (const kind of LOCKED_KINDS) assert.deepEqual(storeFromRead(fail(kind)), { unlocked: false, keys: {}, error: null });
  assert.equal(storeFromRead(fail('engine_down')).error, 'engine_down');
  assert.equal(storeFromRead(fail('key_store_unavailable')).error, 'unreadable');
  assert.equal(storeFromRead(fail('wrong_passphrase')).error, 'unchecked');
  for (const kind of ['busy', 'internal', 'bad_session', 'rate_limited', 'unknown']) assert.equal(storeFromRead(fail(kind)).error, 'unchecked', kind);
  assert.equal(storeFromRead(null).error, 'unchecked');
  assert.deepEqual(storeFromRead({ ok: true, data: { keys: { p: true }, unlocked: true } }), open({ p: true }));
  assert.deepEqual(storeFromRead({ ok: true, data: { p: true } }), { unlocked: true, keys: { p: true }, error: null });
});

test('a locked or missing store reads as locked, never as unreadable', () => {
  for (const kind of LOCKED_KINDS) assert.equal(at({ store: storeFromRead(fail(kind)) }).key, 'status.locked');
});

test('every key the status can show exists in COPY and is ASCII', () => {
  const keys = ['status.ok', 'status.ok.hosted', 'status.locked', 'status.no_key', 'status.no_key.hosted',
    'status.store_unreadable', 'status.engine_down', 'status.unchecked', 'status.check_again', 'status.other_key'];
  for (const k of keys) assert.ok(/^[\x20-\x7e]+$/.test(COPY[k] || ''), k);
});

test('a key for another provider names the stored providers with their labels', () => {
  const s = at({ store: open({ anthropic: true, openai: true }), provider: 'xai' });
  assert.deepEqual(s, { level: 'attention', key: 'status.other_key', vars: { provider: 'xai', stored: ['anthropic', 'openai'] } });
  assert.equal(statusText(s), 'No key for xAI Grok (stored keys: Claude (tuned), OpenAI): add one in Settings.');
  assert.equal(statusText(at({ store: open({}), provider: 'anthropic' })), 'No key for Claude (tuned): add one in Settings.');
  assert.equal(statusText({ key: 'status.no_key' }), 'No key for {provider}: add one in Settings.');
  assert.equal(statusText(null), '');
});

test('hosted: a key held for another provider names the provider that has none', () => {
  const s = at({ cfg: HOSTED, provider: 'openai', hostedKeys: { anthropic: 'k' } });
  assert.deepEqual(s, { level: 'attention', key: 'status.no_key', vars: { provider: 'openai' } });
  assert.equal(statusText(s), 'No key for OpenAI: add one in Settings.');
  assert.deepEqual(at({ cfg: HOSTED, hostedKeys: {} }), { level: 'attention', key: 'status.no_key.hosted' });
  // an empty string is not a held key
  assert.deepEqual(at({ cfg: HOSTED, hostedKeys: { anthropic: '' } }), { level: 'attention', key: 'status.no_key.hosted' });
});

test('stress: hosted, every provider with every set of held keys', () => {
  let n = 0;
  for (const provider of PROVIDERS) for (let mask = 0; mask < 16; mask += 1) {
    const hostedKeys = Object.fromEntries(PROVIDERS.filter((_, i) => mask & (1 << i)).map((p) => [p, 'k-' + p]));
    const hostedKey = hostedKeys[provider] || '';
    const r = linkStatus({ cfg: HOSTED, engineDown: false, hostedKey, hostedKeys, store: null, provider });
    n += 1;
    const held = Object.keys(hostedKeys).length > 0;
    assert.equal(r.key === 'status.ok.hosted', !!hostedKey, JSON.stringify({ provider, hostedKeys }));
    assert.equal(r.key === 'status.no_key.hosted', !held, JSON.stringify({ provider, hostedKeys }));
    if (held && !hostedKey) assert.deepEqual(r, { level: 'attention', key: 'status.no_key', vars: { provider } });
  }
  assert.equal(n, 4 * 16);
});

test('stress: every combination keeps ok, error and unreadable where they belong', () => {
  const kinds = [...LOCKED_KINDS, 'engine_down', 'key_store_unavailable', 'wrong_passphrase', 'busy', 'internal', 'bad_session', 'rate_limited', 'unknown'];
  const reads = [null, ...kinds.map(fail), { ok: true, data: { keys: {}, unlocked: false } }, { ok: true, data: { keys: {}, unlocked: true } },
    ...PROVIDERS.map((p) => ({ ok: true, data: { keys: { [p]: true }, unlocked: true } }))];
  let n = 0;
  for (const cfg of [null, HOSTED, SELF]) for (const engineDown of [true, false]) for (const hostedKey of ['', 'k'])
    for (const read of reads) for (const provider of PROVIDERS) {
      const store = read && storeFromRead(read);
      const args = { cfg, engineDown, hostedKey, store, provider };
      const r = linkStatus(args);
      assert.deepEqual(linkStatus(args), r);
      n += 1;
      if (!r) continue;
      assert.ok(COPY[r.key], r.key);
      const self = cfg === SELF;
      if (r.level === 'ok') {
        assert.ok(!engineDown);
        if (self) assert.ok(read.ok && store.unlocked && store.keys[provider]);
      }
      assert.equal(r.level === 'error', engineDown || (self && !!store && store.error === 'engine_down'));
      if (r.key === 'status.store_unreadable') assert.ok(read.error.kind === 'key_store_unavailable');
      if (r.key === 'status.other_key') assert.ok(self && store.unlocked && !store.keys[provider] && r.vars.stored.length);
    }
  assert.equal(n, 3 * 2 * 2 * reads.length * PROVIDERS.length);
});
