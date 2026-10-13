import test from 'node:test';
import assert from 'node:assert/strict';
import { seal, open, makeKey, b64, unb64, WrongPassphraseOrCorrupt, baseKey, keyring, isEnvelope } from '../src/crypto.js';

const CANARY_TEXT = 'canary-player-text-91c2';
const CANARY_KEY = 'test-canary-key-7f3a';
const plain = { save: { name: 'x', note: CANARY_TEXT, key: CANARY_KEY } };

function flip(str) {
  const bytes = unb64(str);
  bytes[bytes.length - 1] ^= 1;
  return b64(bytes);
}

test('round trip', async () => {
  const env = await seal(plain, 'correct horse');
  assert.deepEqual(await open(env, 'correct horse'), plain);
  assert.equal(env.format, 'coherence-save');
  assert.equal(env.kdf.iterations, 600000);
});

test('wrong passphrase throws one error', async () => {
  const env = await seal(plain, 'correct horse');
  await assert.rejects(open(env, 'battery staple'), WrongPassphraseOrCorrupt);
});

test('flipping a byte in each field fails', async () => {
  const env = await seal(plain, 'pw');
  const cases = {
    salt: (e) => { e.kdf.salt = flip(e.kdf.salt); },
    iv: (e) => { e.cipher.iv = flip(e.cipher.iv); },
    ct: (e) => { const b = unb64(e.ct); b[0] ^= 1; e.ct = b64(b); },
    tag: (e) => { e.ct = flip(e.ct); },
    header: (e) => { e.kdf.iterations += 1; },
  };
  for (const [name, mutate] of Object.entries(cases)) {
    const copy = structuredClone(env);
    mutate(copy);
    await assert.rejects(open(copy, 'pw'), WrongPassphraseOrCorrupt, name);
  }
});

test('truncated ciphertext fails', async () => {
  const env = await seal(plain, 'pw');
  env.ct = b64(unb64(env.ct).slice(0, 8));
  await assert.rejects(open(env, 'pw'), WrongPassphraseOrCorrupt);
});

test('out-of-range iterations are rejected before deriving', async () => {
  const env = await seal(plain, 'pw');
  const subtle = globalThis.crypto.subtle;
  let calls = 0;
  const original = subtle.importKey;
  subtle.importKey = (...args) => { calls += 1; return original.apply(subtle, args); };
  try {
    for (const iterations of [599999, 2000001]) {
      const copy = structuredClone(env);
      copy.kdf.iterations = iterations;
      await assert.rejects(open(copy, 'pw'), WrongPassphraseOrCorrupt);
    }
  } finally {
    delete subtle.importKey;
  }
  assert.equal(calls, 0);
});

test('wrong format, version and lengths are rejected', async () => {
  const env = await seal(plain, 'pw');
  const bad = [
    (e) => { e.format = 'other'; },
    (e) => { e.v = 2; },
    (e) => { e.kdf.salt = b64(new Uint8Array(15)); },
    (e) => { e.cipher.iv = b64(new Uint8Array(13)); },
    (e) => { e.kdf.name = 'scrypt'; },
  ];
  for (const mutate of bad) {
    const copy = structuredClone(env);
    mutate(copy);
    await assert.rejects(open(copy, 'pw'), WrongPassphraseOrCorrupt);
  }
});

test('two seals of the same data differ, and neither holds canaries', async () => {
  const key = await makeKey('pw');
  const a = await seal(plain, key);
  const b = await seal(plain, key);
  assert.notEqual(a.ct, b.ct);
  assert.notEqual(a.cipher.iv, b.cipher.iv);
  const text = JSON.stringify(a);
  assert.ok(!text.includes(CANARY_TEXT));
  assert.ok(!text.includes(CANARY_KEY));
});

test('a keyring derives once per salt and rejects a wrong passphrase', async () => {
  const key = await makeKey('pw');
  const a = await seal(plain, key);
  const b = await seal(plain, key);
  const ring = keyring(await baseKey('pw'));
  const p1 = ring.keyFor(a.kdf.salt, a.kdf.iterations);
  assert.equal(ring.keyFor(b.kdf.salt, b.kdf.iterations), p1);
  assert.deepEqual(await open(a, ring), plain);
  assert.deepEqual(await open(b, ring), plain);
  await assert.rejects(open(a, keyring(await baseKey('wrong'))), WrongPassphraseOrCorrupt);
  const fresh = await ring.fresh();
  assert.deepEqual(await open(await seal(plain, fresh), ring), plain);
});

test('isEnvelope accepts seal output and nothing else', async () => {
  assert.equal(isEnvelope(await seal(plain, 'pw')), true);
  for (const bad of [null, {}, { format: 'coherence-save', v: 1 }, 'text']) assert.equal(isEnvelope(bad), false);
});
