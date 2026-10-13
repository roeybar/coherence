import test from 'node:test';
import assert from 'node:assert/strict';
import { makeKey } from '../src/crypto.js';
import {
  validate, parseJson, persist, readSlot, newSlotId, exportFile, importFile,
  InvalidState, FILE_CAP, generations, autosaveStatus, forkPlain,
} from '../src/state.js';
import { memStore } from './memstore.mjs';

const CANARY = 'canary-player-text-91c2';
const makeDoc = (extra = {}) => ({
  name: 'Test', profile: { name: 'Mira' }, state: { location: 'dock' }, config: { pack_id: 'neon_rain' },
  history: [{ role: 'assistant', content_en: 'Rain.' }, { role: 'user', content_en: CANARY }],
  turn: 1, undo_stack: [], items: [], memory: [], physics_cache: [], ...extra,
});

const ID = '0123456789abcdef';

test('validate keeps allowlisted keys and drops the rest', () => {
  const clean = validate({ save: makeDoc({ admin: true, extra: 1 }) });
  assert.equal(clean.admin, undefined);
  assert.equal(clean.extra, undefined);
  assert.equal(clean.name, 'Test');
});

test('validate rejects wrong types and missing parts', () => {
  const bad = [
    { save: makeDoc({ turn: 'x' }) },
    { save: makeDoc({ state: [] }) },
    { save: makeDoc({ items: {} }) },
    { save: makeDoc({ history: 'no' }) },
    { save: makeDoc({ checkpoint_len: 'x' }) },
    { save: makeDoc({ checkpoint_len: Infinity }) },
    { save: makeDoc({ history: [{ content: 'no role' }] }) },
    { save: { name: 'x', history: [] } },
    { nothing: true },
    null,
  ];
  for (const plain of bad) assert.throws(() => validate(plain), InvalidState);
  assert.equal(validate({ save: makeDoc({ history: Array.from({ length: 500 }, () => ({ role: 'user' })) }) }).history.length, 500);
});

test('validate accepts 600 and 5000 history entries and keeps checkpoint lengths', () => {
  assert.equal(FILE_CAP, 134217728);
  for (const length of [600, 5000]) {
    const doc = makeDoc({ history: Array.from({ length }, () => ({ role: 'user' })), checkpoint_len: length });
    assert.deepEqual(validate({ save: doc }), doc);
  }
  assert.equal(validate({ save: makeDoc({ checkpoint_len: 0 }) }).checkpoint_len, 0);
});

test('prototype pollution keys are ignored', () => {
  const text = '{"save":' + JSON.stringify(makeDoc()).slice(0, -1) + ',"__proto__":{"polluted":1},"constructor":{"x":1}}}';
  const clean = validate(parseJson(text));
  assert.equal(({}).polluted, undefined);
  assert.equal(Object.keys(clean).includes('constructor'), false);
  assert.equal(clean.polluted, undefined);
});

test('persist then read round trip, with only an envelope in the store', async () => {
  const store = memStore();
  const key = await makeKey('pw');
  const doc = makeDoc();
  const r = await persist(store, ID, doc, key);
  assert.deepEqual(r, { ok: true, bytes: store.map.get(ID).length, text: store.map.get(ID) });
  assert.ok(!store.map.get(ID).includes(CANARY));
  assert.deepEqual(await readSlot(store, ID, 'pw'), { doc: validate({ save: doc }), text: r.text });
});

test('5000 entries and length-form snapshots survive browser storage and file export', async () => {
  const store = memStore();
  const doc = makeDoc({
    history: Array.from({ length: 5000 }, (_, i) => ({ role: i % 2 === 0 ? 'user' : 'assistant', content_en: 'x' + i })),
    checkpoint_len: 4000, checkpoint_history: [], checkpoint_state: { location: 'dock' },
    undo_stack: [{ turn: 2499, state: { location: 'dock' }, history_len: 4998 }],
  });
  const key = await makeKey('pw');
  const result = await persist(store, ID, doc, key);
  assert.equal(result.ok, true);
  assert.deepEqual((await readSlot(store, ID, 'pw')).doc, doc);
  assert.deepEqual(await importFile(await exportFile(doc, null)), doc);
});

test('a refused write reports storage_full and leaves the game unchanged', async () => {
  const store = memStore({ fail: () => true });
  const doc = makeDoc();
  const before = structuredClone(doc);
  assert.deepEqual(await persist(store, ID, doc, await makeKey('pw')), { ok: false, kind: 'storage_full' });
  assert.deepEqual(doc, before);
  assert.equal(store.map.size, 0);
});

test('a save over the size cap reports storage_full', async (t) => {
  const store = memStore();
  const doc = makeDoc();
  const key = await makeKey('pw');
  const stringify = JSON.stringify;
  const text = stringify({ save: doc }) + ' '.repeat(FILE_CAP);
  t.mock.method(JSON, 'stringify', (value, ...args) => (value && value.save === doc ? text : stringify(value, ...args)));
  assert.deepEqual(await persist(store, ID, doc, key), { ok: false, kind: 'storage_full' });
  assert.equal(store.map.size, 0);
});

test('a write against a changed record is a conflict and writes nothing', async () => {
  const store = memStore();
  const key = await makeKey('pw');
  const first = await persist(store, ID, makeDoc({ name: 'Mine' }), key);
  store.map.set(ID, 'changed elsewhere');
  assert.deepEqual(await persist(store, ID, makeDoc({ name: 'Next' }), key, () => true, first.text), { ok: false, kind: 'conflict' });
  assert.equal(store.map.get(ID), 'changed elsewhere');
  assert.deepEqual(await persist(store, 'fedcba9876543210', makeDoc(), key, () => true, 'x'), { ok: false, kind: 'conflict' });
  assert.equal(store.map.has('fedcba9876543210'), false);
});

test('slot ids are always 16 lowercase hex digits', () => {
  for (let i = 0; i < 1000; i += 1) assert.match(newSlotId(), /^[0-9a-f]{16}$/);
});

test('readSlot: missing and damaged records are bad_file, a locked one is a wrong passphrase', async () => {
  const store = memStore();
  await assert.rejects(readSlot(store, ID, 'pw'), { kind: 'bad_file' });
  store.map.set(ID, '{"format":"coherence-save","v":1}');
  await assert.rejects(readSlot(store, ID, 'pw'), { kind: 'bad_file' });
  await persist(store, 'legacy', makeDoc(), await makeKey('other'));
  await assert.rejects(readSlot(store, 'legacy', 'pw'), { name: 'WrongPassphraseOrCorrupt' });
});

test('encrypted and plain save files round trip', async () => {
  const doc = makeDoc();
  const encrypted = await exportFile(doc, await makeKey('pw'));
  assert.ok(!encrypted.includes(CANARY));
  assert.deepEqual(await importFile(encrypted, 'pw'), validate({ save: doc }));
  const plainText = await exportFile(doc, null);
  const parsed = JSON.parse(plainText);
  assert.equal(parsed.format, 'coherence-save');
  assert.equal(parsed.v, 1);
  assert.deepEqual(await importFile(plainText), validate({ save: doc }));
});

test('a plain save file larger than 20000000 characters opens', async () => {
  const doc = makeDoc({ memory: [{ text: 'x'.repeat(20000001) }] });
  const text = await exportFile(doc, null);
  assert.ok(text.length > 20000000);
  assert.deepEqual(await importFile(text), doc);
});

test('file load is all or nothing', async () => {
  await assert.rejects(importFile('not json'), InvalidState);
  await assert.rejects(importFile('{"format":"other","v":1}'), InvalidState);
  await assert.rejects(importFile(JSON.stringify({ format: 'coherence-save', v: 1, save: makeDoc({ turn: 'x' }) })), InvalidState);
  await assert.rejects(importFile(' '.repeat(FILE_CAP + 1)), { kind: 'file_too_large' });
  const encrypted = await exportFile(makeDoc(), await makeKey('pw'));
  await assert.rejects(importFile(encrypted, 'wrong'));
});


test('overlapping persists finishing out of order leave the newer game stored', async () => {
  const store = memStore();
  const key = await makeKey('pw');
  const gens = generations();
  const older = gens.next();
  const newer = gens.next();
  // the newer persist finishes first, then the older one finishes and must not write
  assert.equal((await persist(store, ID, makeDoc({ name: 'Newer' }), key, newer)).ok, true);
  assert.deepEqual(await persist(store, ID, makeDoc({ name: 'Older' }), key, older), { ok: false, kind: 'stale' });
  assert.equal((await readSlot(store, ID, 'pw')).doc.name, 'Newer');
  const last = gens.next();
  gens.cancel();
  assert.deepEqual(await persist(store, ID, makeDoc({ name: 'Cancelled' }), key, last), { ok: false, kind: 'stale' });
  assert.equal((await readSlot(store, ID, 'pw')).doc.name, 'Newer');
});

test('autosave status: only a landed write counts as saved; blocked storage counts as full', () => {
  assert.deepEqual(autosaveStatus({ ok: true, bytes: 10 }), { saved: true, full: false });
  assert.deepEqual(autosaveStatus({ ok: false, kind: 'storage_full' }), { saved: false, full: true });
  assert.deepEqual(autosaveStatus(null), { saved: false, full: true });
});

test('Save to file and Fork: plain ticked writes plain JSON; otherwise a passphrase is needed', () => {
  assert.equal(forkPlain(null, true), 'plain');
  assert.equal(forkPlain({}, true), 'plain');
  assert.equal(forkPlain({}, false), 'encrypted');
  assert.equal(forkPlain(null, false), 'need_passphrase');
});
