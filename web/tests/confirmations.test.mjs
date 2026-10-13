import test from 'node:test';
import assert from 'node:assert/strict';
import { COPY, t } from '../src/copy.js';

test('the confirmation copy is the pinned wording', () => {
  const toast = Object.fromEntries(Object.entries(COPY).filter(([k]) => k.startsWith('toast.')));
  assert.deepEqual(toast, {
    'toast.label': 'Done',
    'toast.keys.locked': 'Key store locked. Enter the store passphrase to unlock it.',
    'toast.keys.unlocked': 'Key store unlocked.',
    'toast.keys.saved': 'Key saved for {provider}.',
    'toast.keys.removed': 'Key removed for {provider}.',
    'toast.keys.forgotten': 'Key forgotten. This tab no longer holds a key.',
    'toast.saves.autosave_on': 'Autosave is on for this browser.',
    'toast.saves.file': 'Save file handed to your browser as a download.',
  });
});

test('a confirmation names the provider', () => {
  assert.equal(t('toast.keys.saved', { provider: 'P' }), 'Key saved for P.');
  assert.equal(t('toast.keys.removed', { provider: 'P' }), 'Key removed for P.');
});

test('every confirmation is ASCII', () => {
  for (const [k, v] of Object.entries(COPY)) if (k.startsWith('toast.')) assert.ok(/^[\x20-\x7e]+$/.test(v), k);
});
