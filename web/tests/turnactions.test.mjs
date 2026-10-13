import test from 'node:test';
import assert from 'node:assert/strict';
import { actionsFor, turnOf, forkDoc } from '../src/turnactions.js';

// opening, then three turns: 7 entries, doc.turn 3
const history = [];
for (let i = 0; i < 7; i += 1) history.push({ role: i % 2 === 0 ? 'assistant' : 'user', content_en: 'x' + i });
const doc = (extra) => ({ history, turn: 3, undo_stack: [{}, {}, {}], ...extra });

test('entry index maps to the turn it belongs to', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6].map((i) => turnOf(doc(), i)), [0, 1, 1, 2, 2, 3, 3]);
  assert.equal(turnOf(doc(), -1), 0);
  assert.equal(turnOf(doc(), 1.5), 0);
});

test('undo is allowed for turns inside the undo depth', () => {
  assert.equal(actionsFor(1, doc()).undo, true);
  assert.equal(actionsFor(5, doc()).undo, true);
  assert.equal(actionsFor(0, doc()).undo, false);
  assert.equal(actionsFor(1, doc({ undo_stack: [{}, {}] })).undo, false);
  assert.equal(actionsFor(3, doc({ undo_stack: [{}, {}] })).undo, true);
});

test('comment and flag are for the last GM entry only', () => {
  assert.equal(actionsFor(6, doc()).comment, true);
  assert.equal(actionsFor(6, doc()).flag, true);
  assert.equal(actionsFor(4, doc()).comment, false);
  assert.equal(actionsFor(5, doc()).flag, false);
  assert.equal(actionsFor(6, doc({ undo_stack: [] })).comment, false);
  assert.equal(actionsFor(0, { history: [history[0]], turn: 0, undo_stack: [] }).comment, false);
});

test('rate is on for a real entry; nothing for a missing one', () => {
  assert.equal(actionsFor(2, doc()).rate, true);
  assert.deepEqual(actionsFor(9, doc()), { undo: false, fork: false, comment: false, flag: false, rate: false });
  assert.deepEqual(actionsFor(0, null), { undo: false, fork: false, comment: false, flag: false, rate: false });
});

test('fork cuts the save after the last complete turn, using the held snapshot', () => {
  const snaps = [0, 1, 2].map((n) => ({ turn: n, state: { at: n }, history: history.slice(0, 2 * n + 1) }));
  const d = { name: 'Test', history, turn: 3, state: { at: 3 }, undo_stack: snaps };
  const f = forkDoc(d, 2);
  assert.equal(f.turn, 1);
  assert.equal(f.history.length, 3);
  assert.deepEqual(f.state, { at: 1 });
  assert.equal(f.undo_stack.length, 1);
  assert.equal(f.name, 'Test (fork)');
  assert.equal(forkDoc(d, 3).turn, 1, 'a player entry keeps the turns before it');
  assert.equal(forkDoc(d, 6).turn, 3);
  assert.equal(forkDoc(d, 6).history.length, 7);
  assert.equal(forkDoc({ ...d, undo_stack: snaps.slice(2) }, 2), null);
  assert.equal(forkDoc(d, 9), null);
  assert.equal(actionsFor(2, d).fork, true);
  assert.equal(actionsFor(2, doc()).fork, false);
});

function fullCopy(doc) {
  return {
    ...doc,
    undo_stack: doc.undo_stack.map((snap) => {
      const { history_len, ...rest } = snap;
      return { ...rest, history: Array.isArray(snap.history) ? snap.history : doc.history.slice(0, history_len) };
    }),
  };
}

test('old, length-form and mixed snapshots fork like full copies', () => {
  const old = [0, 1, 2].map((n) => ({ turn: n, state: { at: n }, history: history.slice(0, 2 * n + 1) }));
  const lengths = old.map(({ history: h, ...snap }) => ({ ...snap, history_len: h.length }));
  const base = { name: 'Test', history, turn: 3, state: { at: 3 } };
  const expected = { ...base, undo_stack: old };
  for (const undo_stack of [old, lengths, [old[0], lengths[1], { ...old[2], history_len: 999 }]]) {
    const save = { ...base, undo_stack };
    const before = structuredClone(save);
    for (let i = 0; i < history.length; i += 1) {
      assert.deepEqual(fullCopy(forkDoc(save, i)), forkDoc(expected, i));
      assert.deepEqual(actionsFor(i, save), actionsFor(i, expected));
    }
    assert.deepEqual(save, before);
  }
});

test('an empty snapshot history wins over a stale length', () => {
  const save = { history, turn: 3, undo_stack: [{ turn: 1, state: { at: 1 }, history: [], history_len: 999 }] };
  assert.deepEqual(forkDoc(save, 2).history, []);
});

test('a checkpoint beyond the fork is materialised without changing the source', () => {
  for (const snap of [
    { turn: 1, state: { at: 1 }, history_len: 3 },
    { turn: 1, state: { at: 1 }, history: history.slice(0, 3) },
    { turn: 1, state: { at: 1 }, history: history.slice(0, 3), history_len: 999 },
  ]) {
    const save = {
      history, turn: 3, undo_stack: [snap], checkpoint_len: 5,
      checkpoint_history: [{ role: 'assistant', content_en: 'old checkpoint' }],
      checkpoint_state: { at: 2 }, checkpoint_turn: 2,
    };
    const before = structuredClone(save);
    const fork = forkDoc(save, 2);
    assert.deepEqual(fork.checkpoint_history, history.slice(0, 5));
    assert.equal(Object.hasOwn(fork, 'checkpoint_len'), false);
    assert.deepEqual(fork.checkpoint_state, { at: 2 });
    assert.equal(fork.checkpoint_turn, 2);
    assert.deepEqual(save, before);
  }
});

test('a checkpoint within the fork is materialised; legacy checkpoints keep their copy', () => {
  const base = { history, turn: 3, undo_stack: [{ turn: 1, state: { at: 1 }, history_len: 3 }], checkpoint_history: history.slice(0, 3) };
  for (const checkpoint_len of [0, 1, 3]) {
    const save = { ...base, checkpoint_len };
    const before = structuredClone(save);
    const fork = forkDoc(save, 2);
    assert.equal(Object.hasOwn(fork, 'checkpoint_len'), false);
    assert.deepEqual(fork.checkpoint_history, save.history.slice(0, checkpoint_len));
    assert.deepEqual(save, before);
  }
  for (const checkpoint_len of [null, undefined]) {
    const fork = forkDoc({ ...base, checkpoint_len }, 2);
    assert.equal(fork.checkpoint_len, checkpoint_len);
    assert.deepEqual(fork.checkpoint_history, base.checkpoint_history);
  }
  const legacy = forkDoc(base, 2);
  assert.equal(Object.hasOwn(legacy, 'checkpoint_len'), false);
  assert.deepEqual(legacy.checkpoint_history, base.checkpoint_history);
});

// after compression: a summary entry, then turns 7 to 11 (11 entries)
const pairs = (from, to) => {
  const out = [];
  for (let n = from; n <= to; n += 1) {
    out.push({ role: 'user', content_en: 'u' + n }, { role: 'assistant', content_en: 'a' + n });
  }
  return out;
};
const stack11 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => ({ turn: n, state: { at: n }, history: [] }));

test('a fork of a longer snapshot keeps the checkpoint from the compressed source history', () => {
  const save = {
    history: [{ role: 'assistant', content_en: 'summary' }, ...pairs(7, 11)],
    turn: 11, state: { at: 11 },
    undo_stack: [{ turn: 8, state: { at: 8 }, history: [{ role: 'assistant', content_en: 'opening' }, ...pairs(1, 8)] }],
    checkpoint_len: 9, checkpoint_history: [], checkpoint_state: { at: 10 }, checkpoint_turn: 10,
  };
  const before = structuredClone(save);
  const fork = forkDoc(save, 4);
  assert.equal(fork.turn, 8);
  assert.ok(fork.history.length > save.history.length);
  assert.equal(Object.hasOwn(fork, 'checkpoint_len'), false);
  assert.deepEqual(fork.checkpoint_history, save.history.slice(0, save.checkpoint_len));
  assert.deepEqual(fork.checkpoint_state, save.checkpoint_state);
  assert.equal(fork.checkpoint_turn, save.checkpoint_turn);
  assert.deepEqual(save, before);
});

test('after compression each entry maps to its own turn', () => {
  const d = { name: 'C', history: [{ role: 'assistant', content_en: 'summary' }, ...pairs(7, 11)], turn: 11, state: { at: 11 }, undo_stack: stack11 };
  assert.equal(d.history.length, 11);
  assert.deepEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) => turnOf(d, i)), [7, 7, 8, 8, 9, 9, 10, 10, 11, 11]);
  assert.equal(turnOf(d, 0), 0);
  assert.equal(actionsFor(0, d).undo, false);
  assert.equal(actionsFor(5, d).undo, true);
  const f = forkDoc(d, 4);
  assert.equal(f.turn, 8);
  assert.deepEqual(f.state, { at: 8 });
  assert.equal(f.undo_stack.length, 8);
  assert.equal(forkDoc(d, 3).turn, 7);
  assert.equal(forkDoc(d, 0).turn, 6);
});

test('after a fallback compression with no summary the first entry is a player entry', () => {
  const d2 = { name: 'F', history: pairs(7, 11), turn: 11, state: { at: 11 }, undo_stack: stack11 };
  assert.equal(turnOf(d2, 0), 7);
  assert.equal(forkDoc(d2, 0).turn, 6);
});

test('after two compressions the turns still line up', () => {
  const d3 = { history: [{ role: 'assistant', content_en: 'summary' }, ...pairs(13, 17)], turn: 17, undo_stack: [] };
  assert.equal(turnOf(d3, 1), 13);
});

test('a history that does not end with a GM entry offers no undo and no fork', () => {
  const g = { history: history.slice(0, 6), turn: 3, state: { at: 3 }, undo_stack: [0, 1, 2].map((n) => ({ turn: n, state: { at: n }, history: [] })) };
  for (const i of [0, 1, 2, 3, 4, 5]) {
    assert.equal(turnOf(g, i), 0);
    assert.equal(actionsFor(i, g).undo, false);
    assert.equal(actionsFor(i, g).fork, false);
  }
});
