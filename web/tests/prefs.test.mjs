import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPrefs, savePrefs, validate, PREFS_KEY, DEFAULTS } from '../src/prefs.js';

const memory = (init) => {
  const m = new Map(init ? [[PREFS_KEY, init]] : []);
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, v) };
};

test('defaults when nothing is stored', () => {
  assert.deepEqual(loadPrefs(memory()), DEFAULTS);
});

test('a saved choice round-trips', () => {
  const s = memory();
  savePrefs(s, { submitKey: 'shift_enter' });
  assert.deepEqual(loadPrefs(s), { submitKey: 'shift_enter' });
});

test('bad values fall back to defaults', () => {
  for (const raw of ['not json', '[]', '7', 'null', '{"submitKey":"tab"}', '{"submitKey":3}']) {
    assert.deepEqual(loadPrefs(memory(raw)), DEFAULTS, raw);
  }
  assert.deepEqual(validate(undefined), DEFAULTS);
});

test('a throwing store never breaks the page', () => {
  const bad = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('full'); } };
  assert.deepEqual(loadPrefs(bad), DEFAULTS);
  savePrefs(bad, { submitKey: 'enter' });
});
