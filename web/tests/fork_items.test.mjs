import test from 'node:test';
import assert from 'node:assert/strict';
import { forkDoc } from '../src/turnactions.js';

// opening, then three turns: 7 entries, doc.turn 3
const history = [];
for (let i = 0; i < 7; i += 1) history.push({ role: i % 2 === 0 ? 'assistant' : 'user', content_en: 'x' + i });

function makeDoc(extra) {
  return {
    name: 'Test',
    history,
    turn: 3,
    state: { at: 3 },
    undo_stack: [
      { state: { at: 0 }, history_len: 1, turn: 0, items: [] },
      { state: { at: 1 }, history_len: 3, turn: 1, items: [{ name: 'wire' }] },
      { state: { at: 2 }, history_len: 5, turn: 2, items: [{ name: 'wire' }, { name: 'lockpick' }] },
    ],
    items: [{ name: 'wire' }, { name: 'lockpick' }],
    memory: [0, 1, 2, 3].map((t) => ({ turn: t, role: 'user', text: 'note-' + t })),
    ...extra,
  };
}

test('fork takes the items of the forked turn and drops memory of later turns', () => {
  const d = makeDoc();
  const f = forkDoc(d, 2);
  assert.equal(f.turn, 1);
  assert.deepEqual(f.items, [{ name: 'wire' }]);
  assert.deepEqual(f.memory.map((e) => e.turn), [0, 1]);
  assert.deepEqual(d.memory.map((e) => e.turn), [0, 1, 2, 3]);
  f.items[0].name = 'changed';
  assert.equal(d.undo_stack[1].items[0].name, 'wire');
  console.log('PASS fork restores the snapshot items as a copy and keeps memory up to the forked turn');
});

test('fork without snapshot items or doc memory leaves them as they are', () => {
  const d = makeDoc();
  delete d.undo_stack[1].items;
  delete d.memory;
  const f = forkDoc(d, 2);
  assert.equal(f.items, d.items);
  assert.equal('memory' in f, false);
  console.log('PASS a legacy snapshot keeps the current items and a doc without memory gains none');
});
