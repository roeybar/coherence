import test from 'node:test';
import assert from 'node:assert/strict';
import { actionsFor, turnOf, forkDoc } from '../src/turnactions.js';

// Plays 60 turns the way the engine does (compress, snapshot, append) and checks every entry
// after every turn.
function run(withSummary, lengthForm = false) {
  const doc = { name: 'S', history: [{ role: 'assistant', content_en: 'opening', t: 0 }], turn: 0, state: { at: 0 }, undo_stack: [] };
  const copies = [];
  for (let T = 1; T <= 60; T += 1) {
    if (doc.history.length > 20) {
      if (lengthForm) {
        doc.undo_stack = doc.undo_stack.map((snap) => {
          const { history_len, ...rest } = snap;
          return { ...rest, history: Array.isArray(snap.history) ? snap.history : doc.history.slice(0, history_len) };
        });
      }
      const kept = doc.history.slice(-8);
      const folded = doc.history[doc.history.length - 9];
      doc.history = withSummary ? [{ role: 'assistant', content_en: 'summary', t: folded.t }, ...kept] : kept;
    }
    const snapshot = { turn: T - 1, state: { at: T - 1 }, history: doc.history.slice() };
    copies.push(snapshot);
    if (copies.length > 25) copies.shift();
    doc.undo_stack.push(lengthForm ? { turn: T - 1, state: { at: T - 1 }, history_len: doc.history.length } : snapshot);
    if (doc.undo_stack.length > 25) doc.undo_stack = doc.undo_stack.slice(-25);
    doc.history = [...doc.history, { role: 'user', content_en: 'u' + T, t: T }, { role: 'assistant', content_en: 'a' + T, t: T }];
    doc.turn = T;
    doc.state = { at: T };
    const depth = doc.undo_stack.length;
    doc.history.forEach((entry, i) => {
      const first = i === 0 && entry.role === 'assistant';
      const own = first ? 0 : entry.t;
      assert.equal(turnOf(doc, i), own, `T${T} i${i} turn`);
      const cut = first ? entry.t : (entry.role === 'assistant' ? entry.t : entry.t - 1);
      const f = forkDoc(doc, i);
      if (f !== null) {
        assert.equal(f.turn, cut, `T${T} i${i} fork`);
        const expected = cut === T ? doc : copies.find((snap) => snap.turn === cut);
        assert.deepEqual(f.history, expected.history, `T${T} i${i} history`);
        assert.deepEqual(f.state, expected.state, `T${T} i${i} state`);
      }
      assert.equal(actionsFor(i, doc).undo, own >= 1 && T - own + 1 <= depth, `T${T} i${i} undo`);
    });
  }
}

test('60 turns with summary compression keep every entry on its turn', () => {
  run(true);
});

test('60 turns with fallback compression keep every entry on its turn', () => {
  run(false);
});

test('60 turns with length snapshots and summary compression match full copies', () => {
  run(true, true);
});

test('60 turns with length snapshots and fallback compression match full copies', () => {
  run(false, true);
});
