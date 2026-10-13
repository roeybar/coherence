import test from 'node:test';
import assert from 'node:assert/strict';
import { prepareStore, LEGACY_KEY } from '../src/slots-db.js';
import { memStore, fakeLs } from './memstore.mjs';

const OLD = '{"format":"coherence-save","v":1,"ct":"old"}';

test('migration: the old save is copied byte for byte, then removed from localStorage', async () => {
  const db = memStore();
  const ls = fakeLs({ [LEGACY_KEY]: OLD });
  const store = await prepareStore(db, ls);
  assert.equal(db.map.get('legacy'), OLD);
  assert.equal(ls.getItem(LEGACY_KEY), null);
  assert.deepEqual(await store.ids(), ['legacy']);
  assert.equal(await store.get('legacy'), OLD);
});

test('migration: an equal record already in the database finishes the move', async () => {
  const db = memStore();
  db.map.set('legacy', OLD);
  const ls = fakeLs({ [LEGACY_KEY]: OLD });
  await prepareStore(db, ls);
  assert.equal(ls.getItem(LEGACY_KEY), null);
  assert.equal(db.map.get('legacy'), OLD);
});

test('migration: a different record in the database keeps both; localStorage is served as legacy-local', async () => {
  const db = memStore();
  db.map.set('legacy', 'other');
  const ls = fakeLs({ [LEGACY_KEY]: OLD });
  const store = await prepareStore(db, ls);
  assert.equal(ls.getItem(LEGACY_KEY), OLD);
  assert.equal(db.map.get('legacy'), 'other');
  assert.deepEqual(await store.ids(), ['legacy', 'legacy-local']);
  assert.equal(await store.get('legacy-local'), OLD);
});

test('migration: a refused copy leaves the old save in place as legacy, and writes go there', async () => {
  const db = memStore({ fail: () => true });
  const ls = fakeLs({ [LEGACY_KEY]: OLD });
  const store = await prepareStore(db, ls);
  assert.equal(ls.getItem(LEGACY_KEY), OLD);
  assert.equal(db.map.size, 0);
  assert.equal(await store.cas('legacy', OLD, 'next'), 'ok');
  assert.equal(ls.getItem(LEGACY_KEY), 'next');
  assert.equal(await store.cas('legacy', OLD, 'again'), 'conflict');
});

test('migration: a read-back that differs keeps both', async () => {
  const db = memStore();
  const get = db.get;
  db.get = async (id) => (db.map.has(id) ? 'garbled' : get(id));
  const ls = fakeLs({ [LEGACY_KEY]: OLD });
  const store = await prepareStore(db, ls);
  assert.equal(ls.getItem(LEGACY_KEY), OLD);
  assert.ok((await store.ids()).includes('legacy-local'));
  assert.equal(await store.get('legacy-local'), OLD);
});

test('migration: localStorage changing between the copy and the removal keeps the new text', async () => {
  const db = memStore();
  const ls = fakeLs({ [LEGACY_KEY]: OLD });
  const cas = db.cas;
  db.cas = async (...a) => { const r = await cas(...a); ls.setItem(LEGACY_KEY, 'newer'); return r; };
  const store = await prepareStore(db, ls);
  assert.equal(ls.getItem(LEGACY_KEY), 'newer');
  assert.equal(db.map.get('legacy'), OLD);
  assert.equal(await store.get('legacy-local'), 'newer');
});

test('no database: the old save is read in place and other writes are refused', async () => {
  const ls = fakeLs({ [LEGACY_KEY]: OLD });
  const store = await prepareStore(null, ls);
  assert.deepEqual(await store.ids(), ['legacy']);
  assert.equal(await store.get('legacy'), OLD);
  assert.equal(await store.cas('0123456789abcdef', null, 'x'), 'full');
  assert.equal(ls.getItem(LEGACY_KEY), OLD);
});

test('no old save: nothing changes', async () => {
  const db = memStore();
  const ls = fakeLs({ 'coherence.prefs': '{}' });
  const store = await prepareStore(db, ls);
  assert.equal(db.map.size, 0);
  assert.deepEqual([...ls.map.keys()], ['coherence.prefs']);
  assert.deepEqual(await store.ids(), []);
});
