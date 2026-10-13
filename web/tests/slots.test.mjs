import test from 'node:test';
import assert from 'node:assert/strict';
import { createSlots, shouldAutoOpen, ownerOf } from '../src/slots.js';
import { makeKey, seal, open, baseKey, keyring } from '../src/crypto.js';
import { validate, exportFile } from '../src/state.js';
import { prepareStore, LEGACY_KEY } from '../src/slots-db.js';
import { memStore, memBus, fakeLs, tick } from './memstore.mjs';

const doc = (name, turn = 0) => ({
  name, profile: { name: 'Mira' }, state: { location: 'dock' }, config: { pack_id: 'p' },
  history: [{ role: 'assistant', content_en: 'Rain on ' + name + '.' }], turn, updated: String(turn).padStart(8, '0'),
});
const make = (store, opts) => createSlots(Promise.resolve(store), opts);
const okVet = async () => {};
const dec = async (text, pass = 'pw') => validate(await open(JSON.parse(text), pass));
const names = (c) => c.rows().map((r) => r.doc.name).sort();
const snapshot = (store) => new Map(store.map);

async function stored(store) {
  const out = [];
  for (const t of store.map.values()) out.push((await dec(t)).name);
  return out.sort();
}

test('Start: a new game is kept beside the stored one, and a fresh page lists both', async () => {
  const st = memStore();
  const c = make(st);
  assert.equal((await c.unlock('pw', false)).ok, true);
  const a = doc('A');
  c.adopt();
  await c.write(a);
  const aText = [...st.map.values()][0];
  c.keep(a);
  c.adopt();
  await c.write(doc('B'));
  await c.idle();
  assert.equal(st.map.size, 2);
  assert.ok([...st.map.values()].includes(aText));
  const fresh = make(st);
  await fresh.unlock('pw', false);
  assert.deepEqual(names(fresh), ['A', 'B']);
});

test('Start with no passphrase: the open game is kept in memory and written once one is set', async () => {
  const st = memStore();
  const c = make(st);
  const a = doc('A');
  c.adopt();
  c.settle(c.keep(a));
  assert.deepEqual(c.rows().map((r) => [r.doc.name, r.kept]), [['A', true]]);
  const id = c.rows()[0].id;
  assert.equal(await c.open(id, null, undefined, okVet), a);
  c.keep(a);
  c.adopt();
  assert.equal(st.map.size, 0);
  await c.unlock('pw', true);
  await c.idle();
  assert.deepEqual(await stored(st), ['A']);
  assert.deepEqual(c.rows().map((r) => r.kept), [false]);
});

test('Load from file: the open game is kept when a file game is adopted', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  const a = doc('A');
  c.adopt();
  await c.write(a);
  c.keep(a);
  c.adopt();
  await c.idle();
  assert.deepEqual(names(c), ['A']);
  assert.deepEqual(await stored(st), ['A']);
});

test('Open a stored game: the unsaved open game is kept and written; a failed open changes nothing', async () => {
  const st = memStore();
  const c1 = make(st);
  await c1.unlock('pw', false);
  c1.adopt();
  await c1.write(doc('A'));
  const c = make(st);
  const b = doc('B');
  c.adopt();
  const gid = c.slotNow();
  const res = await c.unlock('pw', true);
  assert.equal(res.ok, true);
  await assert.rejects(c.open('ffffffffffffffff', b, undefined, okVet));
  assert.equal(c.slotNow(), gid);
  const got = await c.open(res.latest, b, undefined, okVet);
  assert.equal(got.name, 'A');
  await c.idle();
  assert.deepEqual(await stored(st), ['A', 'B']);
  assert.deepEqual(names(c), ['B']);
});

test('Passphrase: rule (ii) refusal changes nothing; force writes under a new key and leaves old records', async () => {
  const st = memStore();
  st.map.set('0000000000000001', JSON.stringify(await seal({ save: doc('Old') }, 'other')));
  st.map.set('legacy', '{"format":"coherence-save","v":1}');
  const before = snapshot(st);
  const c = make(st);
  const b = doc('B');
  c.adopt();
  c.keep(b);
  c.adopt();
  const r = await c.unlock('pw', true);
  assert.deepEqual(r, { ok: false, rule: 'ii' });
  await c.idle();
  assert.deepEqual(st.map, before);
  assert.equal(c.lockedCount(), 0);
  assert.deepEqual(names(c), ['B']);
  const f = await c.unlock('pw', true, { force: true });
  assert.equal(f.ok, true);
  await c.write(doc('C'));
  await c.idle();
  for (const [k, v] of before) assert.equal(st.map.get(k), v);
  assert.equal(st.map.size, 4);
  assert.equal(c.lockedCount(), 1);
});

test('Passphrase: damaged records alone do not refuse; a typo with a key set is refused (rule i)', async () => {
  const st = memStore();
  st.map.set('legacy', 'not json');
  const c = make(st);
  assert.equal((await c.unlock('pw', false)).ok, true);
  assert.equal(c.lockedCount(), 0);
  c.adopt();
  await c.write(doc('A'));
  const before = snapshot(st);
  assert.deepEqual(await c.unlock('pwx', true), { ok: false, rule: 'i' });
  const c2 = make(st);
  await c2.unlock('pw', false);
  assert.deepEqual(await c2.unlock('pwx', false), { ok: false, rule: 'i' });
  assert.deepEqual(st.map, before);
});

test('Fork and Save to file leave every stored record unchanged', async () => {
  const st = memStore();
  const c = make(st);
  const { key } = await c.unlock('pw', false);
  c.adopt();
  const a = doc('A', 2);
  await c.write(a);
  const before = snapshot(st);
  await exportFile({ ...a, turn: 1 }, key);
  await exportFile(a, null);
  assert.deepEqual(st.map, before);
});

test('A late result lands in the game it started from while another game is open', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  const from = c.slotNow();
  await c.write(doc('A'));
  c.keep(doc('A', 0));
  c.adopt();
  const now = c.slotNow();
  c.land(from, doc('A', 5));
  await c.idle();
  assert.equal(c.slotNow(), now);
  assert.equal((await dec(st.map.get(c.slotOf(from)))).turn, 5);
  c.land(999, doc('Z'));
  await c.idle();
  assert.ok((await stored(st)).includes('Z'));
});

test('Migration: the old save opens as legacy and is written in place', async () => {
  const env = JSON.stringify(await seal({ save: doc('Old') }, 'pw'));
  const db = memStore();
  const ls = fakeLs({ [LEGACY_KEY]: env });
  const c = createSlots(prepareStore(db, ls));
  const res = await c.unlock('pw', false);
  assert.equal(res.latest, 'legacy');
  const d = await c.open('legacy', null, undefined, okVet);
  await c.write({ ...d, turn: 3 });
  assert.deepEqual([...db.map.keys()], ['legacy']);
  assert.equal((await dec(db.map.get('legacy'))).turn, 3);
  assert.equal(ls.getItem(LEGACY_KEY), null);
});

test('Two tabs: a conflicting write moves to a new slot, and the other tab keeps its version', async () => {
  const st = memStore();
  const c1 = make(st);
  await c1.unlock('pw', false);
  c1.adopt();
  await c1.write(doc('A', 1));
  const c2 = make(st);
  const { latest } = await c2.unlock('pw', false);
  const gid2 = c2.slotNow();
  assert.equal(gid2, null);
  await c2.open(latest, null, undefined, okVet);
  const g = c2.slotNow();
  await c1.write(doc('A', 2));
  const mine = st.map.get(latest);
  const { id } = await c2.write(doc('A', 3));
  assert.notEqual(id, latest);
  assert.equal(c2.slotNow(), g);
  assert.equal(c2.slotOf(g), id);
  assert.equal(st.map.get(latest), mine);
  assert.equal((await dec(st.map.get(latest))).turn, 2);
  assert.equal((await dec(st.map.get(id))).turn, 3);
});

test('A conflict during a turn in flight: the game keeps its handle and the result applies in place', async () => {
  const st = memStore();
  const c1 = make(st);
  await c1.unlock('pw', false);
  c1.adopt();
  await c1.write(doc('A', 1));
  const c2 = make(st);
  const { latest } = await c2.unlock('pw', false);
  await c2.open(latest, null, undefined, okVet);
  const from = c2.slotNow();
  await c1.write(doc('A', 2));
  await c2.write(doc('A', 3));
  assert.equal(c2.slotNow(), from);
  await c2.write(doc('A', 4));
  assert.equal((await dec(st.map.get(c2.slotOf(from)))).turn, 4);
});

test('After a conflict move, opening and playing the other tab\'s version never writes into the moved game', async () => {
  const st = memStore();
  const c1 = make(st);
  await c1.unlock('pw', false);
  c1.adopt();
  await c1.write(doc('A', 1));
  const c2 = make(st);
  const { latest } = await c2.unlock('pw', false);
  await c2.open(latest, null, undefined, okVet);
  await c1.write(doc('A', 2));
  const { id } = await c2.write(doc('A', 3));
  c2.settle(c2.keep(doc('A', 3)));
  const back = await c2.open(latest, null, undefined, okVet);
  assert.equal(back.turn, 2);
  await c2.write(doc('A', 4));
  assert.equal((await dec(st.map.get(id))).turn, 3);
  assert.equal((await dec(st.map.get(latest))).turn, 4);
});

test('Queued writes: the newer game wins', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  const w = [c.write(doc('A', 1)), c.write(doc('A', 2)), c.write(doc('A', 3))];
  await Promise.all(w);
  assert.equal((await dec([...st.map.values()][0])).turn, 3);
  assert.equal(st.map.size, 1);
});

test('A refused write keeps the last good record and the game in memory', async () => {
  let refuse = false;
  const st = memStore({ fail: () => refuse });
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  await c.write(doc('A', 1));
  const other = make(st);
  await other.unlock('pw', false);
  other.adopt();
  await other.write(doc('B', 1));
  const before = snapshot(st);
  refuse = true;
  const a2 = doc('A', 2);
  const { r } = await c.write(a2);
  assert.deepEqual(r, { ok: false, kind: 'storage_full' });
  c.keep(a2);
  await c.idle();
  assert.deepEqual(st.map, before);
  assert.ok(c.rows().some((x) => x.kept && x.doc === a2));
});

test('Save to file and remove: refuses the open and in-memory games; removes only after the download', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  await c.write(doc('A', 1));
  const aId = c.slotOf(c.slotNow());
  c.keep(doc('A', 1));
  c.adopt();
  await c.write(doc('B', 1));
  const bId = c.slotOf(c.slotNow());
  assert.equal(await c.remove(bId, async () => true), 'refused');
  const nokey = make(st);
  nokey.adopt();
  nokey.keep(doc('K'));
  nokey.adopt();
  const kId = nokey.rows().find((x) => x.kept).id;
  assert.equal(await nokey.remove(kId, async () => true), 'refused');
  await c.idle();
  const aText = st.map.get(aId);
  assert.equal(await c.remove(aId, async () => false), 'kept');
  assert.equal(await c.remove(aId, async () => { throw new Error('no'); }), 'kept');
  assert.equal(st.map.get(aId), aText);
  const other = make(st);
  await other.unlock('pw', false);
  await other.open(aId, null, undefined, okVet);
  assert.equal(await c.remove(aId, async () => { await other.write(doc('A', 9)); return true; }), 'conflict');
  assert.equal((await dec(st.map.get(aId))).turn, 9);
  let file = null;
  const c3 = make(st);
  await c3.unlock('pw', false);
  assert.equal(await c3.remove(aId, async (d) => { file = d; return true; }), 'ok');
  assert.equal(st.map.has(aId), false);
  assert.deepEqual(file, validate({ save: doc('A', 9) }));
});

test('Remove during a turn in flight: the late result re-creates its record', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  const from = c.slotNow();
  await c.write(doc('A', 1));
  c.keep(doc('A', 1));
  c.adopt();
  const aId = c.slotOf(from);
  assert.equal(await c.remove(aId, async () => true), 'ok');
  c.land(from, doc('A', 2));
  await c.idle();
  assert.equal((await dec(st.map.get(c.slotOf(from)))).turn, 2);
});

test('A turn result that returns while another game opens lands in its own game', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  const b1 = doc('B', 1);
  await c.write(b1);
  const bId = c.slotOf(c.slotNow());
  c.keep(b1);
  c.adopt();
  const from = c.slotNow();
  let shown = doc('A', 2);
  await c.write(shown);
  const aId = c.slotOf(from);
  const go = st.pause();
  const opening = c.open(bId, () => shown, (d) => { shown = d; }, okVet);
  // the page applies a result in place only while its game is still the open one
  if (c.slotNow() !== from) c.land(from, doc('A', 3));
  go();
  const got = await opening;
  await c.idle();
  assert.equal(got.name, 'B');
  assert.equal(c.slotOf(c.slotNow()), bId);
  assert.equal((await dec(st.map.get(aId))).turn, 3);
  assert.deepEqual(c.rows().map((r) => [r.doc.name, r.doc.turn]), [['A', 3]]);
  // a failed open puts the game back and shows the result that landed meanwhile
  const gB = c.slotNow();
  shown = got;
  const go2 = st.pause();
  const failing = c.open('ffffffffffffffff', () => shown, (d) => { shown = d; }, okVet);
  const b4 = doc('B', 4);
  if (c.slotNow() !== gB) c.land(gB, b4);
  go2();
  await assert.rejects(failing);
  assert.equal(c.slotNow(), gB);
  assert.equal(shown, b4);
  await c.idle();
  assert.equal((await dec(st.map.get(bId))).turn, 4);
  assert.deepEqual(names(c), ['A']);
});

test('Load from file: a result that returns while the file is read lands in its own game; a bad file puts the game back', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  const from = c.slotNow();
  const a1 = doc('A', 1);
  await c.write(a1);
  const aId = c.slotOf(from);
  // the page leaves the open game, reads the file, then adopts the file's game
  let sw = c.keep(a1);
  if (c.slotNow() !== from) c.land(from, doc('A', 2));
  c.adopt();
  await c.idle();
  assert.notEqual(c.slotNow(), from);
  assert.equal((await dec(st.map.get(aId))).turn, 2);
  const g = c.slotNow();
  const f1 = doc('F', 1);
  await c.write(f1);
  sw = c.keep(f1);
  const f2 = doc('F', 2);
  if (c.slotNow() !== g) c.land(g, f2);
  assert.equal(c.back(sw), f2);
  assert.equal(c.slotNow(), g);
  await c.idle();
  assert.equal((await dec(st.map.get(c.slotOf(g)))).turn, 2);
  assert.equal(c.back(c.keep(f2)), null);
  assert.equal(c.slotNow(), g);
  assert.deepEqual(names(c), ['A']);
});

test('Save to file and remove: a game cannot be entered while its file is written', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  const a = doc('A', 1);
  await c.write(a);
  const aId = c.slotOf(c.slotNow());
  c.keep(a);
  c.adopt();
  const gB = c.slotNow();
  const b = doc('B', 1);
  await c.write(b);
  let tried = null;
  assert.equal(await c.remove(aId, async () => { tried = await c.open(aId, b, undefined, okVet); return true; }), 'ok');
  assert.equal(tried, 'busy');
  await c.idle();
  assert.equal(c.slotNow(), gB);
  assert.equal(st.map.has(aId), false);
});

// a stored game A that is open, and a stored game B listed beside it
async function twoGames() {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  c.adopt();
  const b1 = doc('B', 1);
  await c.write(b1);
  const bId = c.slotOf(c.slotNow());
  c.keep(b1);
  c.adopt();
  const gA = c.slotNow();
  const a1 = doc('A', 1);
  await c.write(a1);
  return { st, c, gA, aId: c.slotOf(gA), bId, a1 };
}

test('One switch at a time: a second open is refused while the first is pending; a failed first open changes nothing', async () => {
  const { st, c, gA, bId, a1 } = await twoGames();
  await c.idle();
  const before = snapshot(st);
  const go = st.pause();
  const first = c.open('ffffffffffffffff', () => a1, undefined, okVet);
  assert.equal(c.switching, true);
  assert.equal(await c.open(bId, () => a1, undefined, okVet), 'busy');
  assert.equal(c.keep(a1), 'busy');
  go();
  await assert.rejects(first);
  assert.equal(c.switching, false);
  assert.equal(c.slotNow(), gA);
  await c.idle();
  assert.deepEqual(st.map, before);
  assert.deepEqual(names(c), ['B']);
});

test('A turn result for the open game lands in it across a failed open and a later switch', async () => {
  const { st, c, gA, aId, bId, a1 } = await twoGames();
  let shown = a1;
  const go = st.pause();
  const failing = c.open('ffffffffffffffff', () => shown, (d) => { shown = d; }, okVet);
  // the page applies a result in place only while its game is still the open one
  const a2 = doc('A', 2);
  if (c.slotNow() !== gA) c.land(gA, a2);
  go();
  await assert.rejects(failing);
  assert.equal(c.slotNow(), gA);
  assert.equal(shown, a2);
  // a turn begun on A returns after B has opened
  const from = c.slotNow();
  shown = await c.open(bId, () => shown, (d) => { shown = d; }, okVet);
  assert.equal(shown.name, 'B');
  if (c.slotNow() !== from) c.land(from, doc('A', 3));
  await c.idle();
  const back = await c.open(aId, () => shown, undefined, okVet);
  assert.equal(back.turn, 3);
  assert.equal(c.slotNow(), from);
  await c.idle();
  assert.equal((await dec(st.map.get(aId))).turn, 3);
});

test('Save to file and remove: refused while its game is being opened', async () => {
  const { st, c, bId, a1 } = await twoGames();
  await c.idle();
  const text = st.map.get(bId);
  const go = st.pause();
  const opening = c.open(bId, () => a1, undefined, okVet);
  let asked = false;
  assert.equal(await c.remove(bId, async () => { asked = true; return true; }), 'busy');
  go();
  assert.equal((await opening).name, 'B');
  await c.idle();
  assert.equal(asked, false);
  assert.equal(st.map.get(bId), text);
  assert.equal(c.slotOf(c.slotNow()), bId);
});

test('Save to file and remove: a result that lands in the game during the download keeps its record', async () => {
  const st = memStore();
  const c = make(st);
  await c.unlock('pw', false);
  const gB = c.adopt();
  const b1 = doc('B', 1);
  await c.write(b1);
  const bId = c.slotOf(gB);
  c.keep(b1);
  c.adopt();
  await c.write(doc('A', 1));
  await c.idle();
  assert.equal(await c.remove(bId, async () => { c.land(gB, doc('B', 2)); return true; }), 'kept');
  await c.idle();
  const got = await dec(st.map.get(bId));
  assert.equal(got.name, 'B');
  assert.equal(got.turn, 2);
});

test('switching holds from the start of every switch until it settles', async () => {
  const { st, c, aId, bId, a1 } = await twoGames();
  const go = st.pause();
  const opening = c.open(bId, () => a1, undefined, okVet);
  assert.equal(c.switching, true);
  go();
  await opening;
  assert.equal(c.switching, false);
  await assert.rejects(c.open('ffffffffffffffff', null, undefined, okVet));
  assert.equal(c.switching, false);
  let sw = c.keep(null);
  assert.equal(c.switching, true);
  c.adopt();
  assert.equal(c.switching, false);
  sw = c.keep(null);
  c.back(sw);
  assert.equal(c.switching, false);
  sw = c.keep(null);
  c.settle(sw);
  assert.equal(c.switching, false);
  let mid = null;
  const removing = c.remove(aId, async () => { mid = c.switching; return true; });
  assert.equal(c.switching, true);
  assert.equal(await removing, 'ok');
  assert.equal(mid, true);
  assert.equal(c.switching, false);
});

test('A refresh begun during an unlock with another key does not overwrite the list', async () => {
  const st = memStore();
  st.map.set('0000000000000001', JSON.stringify(await seal({ save: doc('Y') }, 'other')));
  const c = make(st);
  assert.equal((await c.unlock('pw', false, { force: true })).ok, true);
  assert.equal(c.lockedCount(), 1);
  const go1 = st.pause();
  const unlocking = c.unlock('other', false);
  await tick(50);
  const go2 = st.pause();
  const refreshing = c.refresh();
  go1();
  assert.equal((await unlocking).ok, true);
  go2();
  await refreshing;
  assert.deepEqual(names(c), ['Y']);
  assert.equal(c.lockedCount(), 0);
});

test('ownerOf: turn results, undo, undo-to and regenerate inherit the game; open and file load set it', () => {
  const owners = new WeakMap();
  const opened = doc('A');
  const loaded = doc('F');
  owners.set(opened, 1);
  owners.set(loaded, 2);
  assert.equal(ownerOf(owners, opened, 7), 1);
  assert.equal(ownerOf(owners, loaded, 1), 2);
  for (const kind of ['turn', 'undo', 'undo-to', 'regenerate']) assert.equal(ownerOf(owners, { ...opened, turn: 1, kind }, 1), 1, kind);
  assert.equal(ownerOf(owners, doc('B'), null), null);
  assert.equal(ownerOf(owners, null, 1), null);
});

test('Another tab lists a new game after its write, through the bus', async () => {
  const st = memStore();
  const link = memBus();
  let changes = 0;
  const a = make(st, { bus: link() });
  const b = make(st, { bus: link(), onChange: () => { changes += 1; } });
  await a.unlock('pw', false);
  await b.unlock('pw', false);
  a.adopt();
  await a.write(doc('New'));
  for (let i = 0; i < 250 && !names(b).length; i += 1) await tick(20);
  assert.deepEqual(names(b), ['New']);
  assert.ok(changes > 0);
});

test('Auto-open only with no game open and none being made', () => {
  assert.equal(shouldAutoOpen({ hasDoc: false, away: false, latest: 'x' }), true);
  assert.equal(shouldAutoOpen({ hasDoc: true, away: false, latest: 'x' }), false);
  assert.equal(shouldAutoOpen({ hasDoc: false, away: true, latest: 'x' }), false);
  assert.equal(shouldAutoOpen({ hasDoc: false, away: false, latest: null }), false);
});

function mulberry(seed) {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Two tabs of the page on one store. Some operations are not awaited before the next one
// starts, turn results come back late (also while another game is being opened), and the
// invariant is checked each time everything has settled.
test('stress: 400 overlapping operations on two tabs never lose the latest version of a game', async () => {
  const rand = mulberry(7);
  const st = memStore({ fail: () => rand() < 0.1 });
  const link = memBus();
  const tabs = [0, 1].map((i) => ({ i, c: make(st, { bus: link() }), doc: null, gids: [], busy: false }));
  for (const t of tabs) await t.c.unlock('pw', false);
  const ring = keyring(await baseKey('pw'));
  const model = new Map(); // lineage -> latest doc
  const owner = new Map(); // lineage -> the tab that plays it
  const forks = []; // lineages started from another lineage's latest version
  const files = [];
  const problems = [];
  const pending = [];
  let clock = 0;
  let lineages = 0;
  const at = (n) => Math.floor(rand() * n);
  // a new version of base; a tab that plays a version it does not own, or not the latest,
  // starts a new lineage
  const next = (t, base) => {
    let name = base ? base.name : null;
    if (!base || owner.get(name) !== t.i || model.get(name).turn !== base.turn) {
      const from = base && model.has(base.name) && model.get(base.name).turn === base.turn ? base : null;
      name = 'L' + (++lineages);
      owner.set(name, t.i);
      if (from) forks.push({ name, from: from.name, turn: from.turn });
    }
    const d = { ...(base || doc(name)), name, turn: ++clock, updated: String(clock).padStart(8, '0') };
    model.set(name, d);
    return d;
  };
  // the page shows d at once, and autosave writes it while its game is open
  const show = (t, d) => {
    t.doc = d;
    return t.c.slotNow() === null ? null : t.c.write(d);
  };
  // one switch at a time: a switch begun while another is under way must be refused
  let refused = 0;
  const refusal = (t, busy, r) => {
    if (busy && r !== 'busy') problems.push('a switch started while another was under way');
    if (!busy && r === 'busy') problems.push('a switch was refused with none under way');
    if (r === 'busy') refused += 1;
    return r === 'busy';
  };
  const leave = (t) => {
    const busy = t.c.switching;
    const sw = t.c.keep(t.doc);
    return refusal(t, busy, sw) ? null : sw;
  };
  const opened = (t) => {
    t.gids.push(t.c.slotNow());
  };
  const play = (t) => {
    if (!t.doc || t.busy || t.c.slotNow() === null) return null;
    t.busy = true;
    const from = t.c.slotNow();
    const base = t.doc;
    return tick(at(4)).then(() => {
      t.busy = false;
      const d = next(t, base);
      if (t.c.slotNow() !== from) t.c.land(from, d);
      else return show(t, d);
      return null;
    });
  };
  const create = (t) => {
    const sw = leave(t);
    if (!sw) return null;
    t.doc = null;
    return tick(at(4)).then(() => {
      const d = next(t, null);
      if (t.c.slotNow() !== null) problems.push('a game opened while a new game was being made');
      t.c.adopt();
      opened(t);
      return show(t, d);
    });
  };
  const openRow = (t) => {
    const rows = t.c.rows();
    if (!rows.length) return null;
    const row = rows[at(rows.length)];
    const was = t.c.slotNow();
    const busy = t.c.switching;
    return t.c.open(row.id, () => t.doc, (d) => show(t, d), okVet).then((d) => {
      if (refusal(t, busy, d)) return;
      t.doc = d;
      opened(t);
    }, () => {
      // the other tab removed it meanwhile: the game that was open is open again
      if (t.c.slotNow() !== was) problems.push('a failed open changed the open game');
    });
  };
  const load = (t) => {
    const was = t.c.slotNow();
    const sw = leave(t);
    if (!sw) return null;
    return tick(at(4)).then(() => {
      if (rand() < 0.2) {
        // a file that does not load
        const d = t.c.back(sw);
        if (d) show(t, d);
        if (t.c.slotNow() !== was) problems.push('a failed load changed the open game');
        return null;
      }
      if (t.c.slotNow() !== null) problems.push('a game opened while a file was read');
      t.c.adopt();
      opened(t);
      return show(t, next(t, files.length ? files[at(files.length)] : null));
    });
  };
  const ops = {
    create,
    play,
    play2: play,
    // a new game that did not start: nothing is open afterwards
    keep: (t) => {
      const sw = leave(t);
      if (sw) { t.doc = null; t.c.settle(sw); }
      return null;
    },
    open: openRow,
    load,
    land: (t) => {
      if (t.busy || !t.gids.length) return null;
      const gid = t.gids[at(t.gids.length)];
      const row = gid !== t.c.slotNow() && t.c.rows().find((r) => r.id === t.c.slotOf(gid));
      if (row) t.c.land(gid, next(t, row.doc));
      return null;
    },
    // a turn whose result comes back while another game is being opened
    turnOpen: (t) => {
      const p = play(t);
      const go = st.pause();
      const q = openRow(t);
      setTimeout(go, 8);
      return Promise.all([p, q]);
    },
    remove: (t) => {
      const rows = t.c.rows().filter((r) => r.removable);
      if (!rows.length) return null;
      const busy = t.c.switching;
      return t.c.remove(rows[at(rows.length)].id, async (d) => { await tick(at(3)); files.push(d); return true; })
        .then((r) => refusal(t, busy, r), () => null);
    },
    wrong: (t) => {
      if (!t.c.rows().some((r) => !r.kept)) return null;
      return t.c.unlock('not-the-passphrase', !!t.doc).then((r) => { if (r.ok) problems.push('a wrong passphrase was accepted'); });
    },
  };
  const kinds = Object.keys(ops);
  let checks = 0;
  for (let i = 0; i < 400; i += 1) {
    const t = tabs[at(2)];
    const op = kinds[at(kinds.length)];
    const w = ops[op](t);
    if (w) {
      const safe = Promise.resolve(w).catch((e) => problems.push(`${i} ${op}: ${e}`));
      if (rand() < 0.5) await safe;
      else pending.push(safe);
    }
    // each operation is its own event, as a click is; work it started may still be running
    await tick(0);
    if (rand() > 0.25 && i < 399) continue;
    await Promise.all(pending);
    pending.length = 0;
    for (const x of tabs) await x.c.idle();
    await tick(1);
    for (const x of tabs) await x.c.idle();
    checks += 1;
    // every lineage's latest version is open, kept, stored or in a downloaded file
    const seen = [...files];
    for (const x of tabs) {
      // settled: no switch is left under way, and a tab shows a game only while one is open
      if (x.c.switching) problems.push(`${i} ${op}: a switch did not settle`);
      if (x.c.slotNow() === null && x.doc) problems.push(`${i} ${op}: a game is shown with none open`);
      if (x.doc && x.c.slotNow() !== null) seen.push(x.doc);
      for (const r of x.c.rows()) if (r.kept) seen.push(r.doc);
    }
    for (const text of st.map.values()) seen.push(validate(await open(JSON.parse(text), ring)));
    // a version that is still the latest of its lineage also lives on in a lineage started
    // from it (the other tab continued a stored game), when that one is reachable
    const reached = (name) => seen.some((s) => s.name === name && s.turn === model.get(name).turn)
      || forks.some((f) => f.from === name && f.turn === model.get(name).turn && reached(f.name));
    for (const [name, d] of model) {
      if (!reached(name)) problems.push(`${i} ${op}: ${name} turn ${d.turn} lost`);
    }
    if (problems.length) break;
  }
  console.log(`${problems.length ? 'FAIL' : 'PASS'} slots stress: 400 operations, ${refused} refused, ${checks} checks, ${model.size} lineages, ${st.map.size} records, ${files.length} files${problems.length ? ': ' + problems[0] : ''}`);
  assert.deepEqual(problems, []);
});

test('makeKey is still a fresh salt per call', async () => {
  const a = await makeKey('pw');
  const b = await makeKey('pw');
  assert.notDeepEqual(a.salt, b.salt);
});

async function twoStored(st) {
  const w = make(st);
  await w.unlock('pw', false);
  w.adopt();
  const a = doc('A');
  await w.write(a);
  w.keep(a);
  w.adopt();
  await w.write(doc('B'));
  await w.idle();
  return w;
}

test('stored games start unchecked after unlock, refresh and refreshOne; marks set the status', async () => {
  const st = memStore();
  const w = await twoStored(st);
  const c = make(st);
  await c.unlock('pw', false);
  const status = () => c.rows().map((r) => [r.doc.name, r.status]).sort();
  assert.deepEqual(status(), [['A', 'unchecked'], ['B', 'unchecked']]);
  const [a, b] = ['A', 'B'].map((n) => c.rows().find((r) => r.doc.name === n));
  c.markVetted(a.doc);
  c.markBlocked(b.doc);
  assert.deepEqual(status(), [['A', 'ok'], ['B', 'blocked']]);
  assert.equal(c.docOf(a.id), a.doc);
  assert.equal(c.isVetted(a.doc) && c.isBlocked(b.doc), true);
  await c.refresh();
  assert.deepEqual(status(), [['A', 'unchecked'], ['B', 'unchecked']]);
  c.markVetted(c.docOf(b.id));
  await w.write(doc('B', 1));
  await w.idle();
  await c.refreshOne(b.id);
  assert.equal(c.docOf(b.id).turn, 1);
  assert.deepEqual(status(), [['A', 'unchecked'], ['B', 'unchecked']]);
});

test('open with no vet refuses and puts the open game back', async () => {
  const st = memStore();
  await twoStored(st);
  const c = make(st);
  await c.unlock('pw', false);
  const aId = c.rows().find((r) => r.doc.name === 'A').id;
  c.adopt();
  const gB = c.slotNow();
  await assert.rejects(c.open(aId, doc('C')), /unvetted/);
  assert.equal(c.slotNow(), gB);
  assert.equal(c.switching, false);
  assert.equal(c.rows().find((r) => r.id === aId).status, 'unchecked');
});

test('a rejecting vet puts the game back with what landed; a resolving one opens and marks', async () => {
  const st = memStore();
  await twoStored(st);
  const c = make(st);
  await c.unlock('pw', false);
  const aId = c.rows().find((r) => r.doc.name === 'A').id;
  c.adopt();
  const gC = c.slotNow();
  let shown = doc('C');
  const landed = doc('C', 5);
  const refuse = async () => { c.land(gC, landed); throw new Error('refused'); };
  await assert.rejects(c.open(aId, () => shown, (d) => { shown = d; }, refuse), /refused/);
  assert.equal(c.slotNow(), gC);
  assert.equal(shown, landed);
  const seen = [];
  const got = await c.open(aId, () => shown, (d) => { shown = d; }, async (d) => { seen.push(d); });
  assert.equal(got.name, 'A');
  assert.deepEqual(seen, [got]);
  assert.equal(c.slotOf(c.slotNow()), aId);
  assert.equal(c.isVetted(got), true);
  await c.idle();
});
