import test from 'node:test';
import assert from 'node:assert/strict';
import { mask, applyEdit, reconcile, MASK, CLEAR } from '../src/keyMask.js';

test('the mask shows the first four characters and one star for each further one', () => {
  assert.equal(MASK, '*');
  assert.equal(CLEAR, 4);
  assert.equal(mask(''), '');
  assert.equal(mask('abc'), 'abc');
  assert.equal(mask('abcd'), 'abcd');
  assert.equal(mask('abcdef'), 'abcd**');
  const long = 'abcdEFGHIJKLMNOPQRSTUVWXYZ0123';
  assert.equal(long.length, 30);
  const shown = mask(long);
  for (const ch of long.slice(4)) assert.ok(!shown.includes(ch), ch);
});

test('typing, deleting and pasting edit the real value at the same positions', () => {
  assert.deepEqual(applyEdit('abcd', { type: 'insertText', data: 'X', start: 4, end: 4 }), { real: 'abcdX', caret: 5 });
  assert.deepEqual(applyEdit('abcdX', { type: 'deleteContentBackward', start: 5, end: 5 }), { real: 'abcd', caret: 4 });
  assert.deepEqual(applyEdit('abcdX', { type: 'deleteContentBackward', start: 0, end: 5 }), { real: '', caret: 0 });
  assert.deepEqual(applyEdit('abcdX', { type: 'insertFromPaste', data: 'pk-\r\nzz', start: 0, end: 5 }), { real: 'pk-zz', caret: 5 });
  assert.deepEqual(applyEdit('abcdXY', { type: 'insertText', data: 'z', start: 5, end: 5 }), { real: 'abcdXzY', caret: 6 });
  assert.deepEqual(applyEdit('abcdXY', { type: 'deleteWordBackward', start: 5, end: 5 }), { real: 'Y', caret: 0 });
  assert.deepEqual(applyEdit('abcdXY', { type: 'deleteContentForward', start: 4, end: 4 }), { real: 'abcdY', caret: 4 });
  assert.deepEqual(applyEdit('abcdXY', { type: 'deleteContentBackward', start: 0, end: 0 }), { real: 'abcdXY', caret: 0 });
});

test('a reversed or out-of-range selection is ordered and clamped', () => {
  assert.deepEqual(applyEdit('abcdXY', { type: 'deleteContentBackward', start: 6, end: 4 }), { real: 'abcd', caret: 4 });
  assert.deepEqual(applyEdit('abc', { type: 'insertText', data: 'Z', start: -5, end: 99 }), { real: 'Z', caret: 1 });
});

test('line breaks and undo or redo leave the value as it is', () => {
  assert.equal(applyEdit('abcdXY', { type: 'insertLineBreak', start: 2, end: 2 }).real, 'abcdXY');
  assert.equal(applyEdit('abcdXY', { type: 'historyUndo', start: 2, end: 2 }).real, 'abcdXY');
  assert.equal(applyEdit('abcdXY', { type: 'historyRedo', start: 2, end: 2 }).real, 'abcdXY');
});

test('a change the browser made itself is carried over from the field text', () => {
  assert.deepEqual(reconcile('abcdX', 'abcd*', 'abcd*Q', 6), { real: 'abcdXQ', caret: 6 });
  assert.deepEqual(reconcile('', '', 'sk-filled', 9), { real: 'sk-filled', caret: 9 });
  assert.deepEqual(reconcile('abcdXY', 'abcd**', 'abcd***', 6), { real: 'abcdX*Y', caret: 6 });
});

// a reference model: the plain string edit the browser would make on an unmasked field
function model(real, { type, data, start, end }) {
  const len = real.length;
  const c = (n) => Math.min(len, Math.max(0, n));
  let a = c(start);
  let b = c(end);
  if (a > b) [a, b] = [b, a];
  const cut = (x, y, s = '') => real.slice(0, x) + s + real.slice(y);
  if (type === 'insertText' || type === 'insertFromPaste') return cut(a, b, String(data).replace(/[\r\n]/g, ''));
  if (type === 'historyUndo' || type === 'insertLineBreak') return real;
  if (a !== b) return cut(a, b);
  if (type === 'deleteContentBackward') return a > 0 ? cut(a - 1, a) : real;
  if (type === 'deleteContentForward') return cut(a, a + 1);
  if (type === 'deleteWordBackward') return cut(0, a);
  if (type === 'deleteWordForward') return cut(a, len);
  return real;
}

test('stress: 5000 random edits match a plain-string model and keep the mask length', () => {
  let seed = 12345;
  const rnd = (n) => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed % n;
  };
  const types = ['insertText', 'insertFromPaste', 'deleteContentBackward', 'deleteContentForward',
    'deleteWordBackward', 'deleteWordForward', 'historyUndo', 'insertLineBreak'];
  const chars = 'abcXYZ019*-_\r\n';
  let real = '';
  for (let i = 0; i < 5000; i += 1) {
    const data = Array.from({ length: rnd(5) }, () => chars[rnd(chars.length)]).join('');
    const edit = { type: types[rnd(types.length)], data, start: rnd(real.length + 6) - 3, end: rnd(real.length + 6) - 3 };
    const want = model(real, edit);
    real = applyEdit(real, edit).real;
    assert.equal(real, want, JSON.stringify(edit));
    assert.equal(mask(real).length, real.length);
  }
});

test('stress: a 100,000-character paste is masked quickly with nothing past four in clear', () => {
  const big = 'abcd' + 'Q'.repeat(99996);
  const t0 = performance.now();
  const r = applyEdit('', { type: 'insertFromPaste', data: big, start: 0, end: 0 });
  const shown = mask(r.real);
  assert.ok(performance.now() - t0 < 200);
  assert.equal(r.real, big);
  assert.equal(shown.length, 100000);
  assert.ok(!shown.slice(4).includes('Q'));
});
