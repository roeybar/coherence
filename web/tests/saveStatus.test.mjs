import test from 'node:test';
import assert from 'node:assert/strict';
import { savedAfterWrite, noteKey, shouldWarnOnLeave } from '../src/saveStatus.js';

test('a finished write of the current document counts as saved', () => {
  const doc = { turn: 3 };
  assert.equal(savedAfterWrite(doc, doc, { ok: true }), true);
});

test('a write of an older document never counts as saved', () => {
  const older = { turn: 3 };
  const newer = { turn: 4 };
  assert.equal(savedAfterWrite(older, newer, { ok: true }), false);
});

test('a failed, full or missing result does not count as saved', () => {
  const doc = { turn: 3 };
  assert.equal(savedAfterWrite(doc, doc, { ok: false, kind: 'full' }), false);
  assert.equal(savedAfterWrite(doc, doc, { kind: 'full' }), false);
  assert.equal(savedAfterWrite(doc, doc, null), false);
  assert.equal(savedAfterWrite(doc, doc, undefined), false);
});

test('the note follows the passphrase, the open game and the write state', () => {
  const key = {};
  const base = { saving: false, saved: false, full: false, stored: false, key, hasDoc: true };
  assert.equal(noteKey({ ...base, key: null }), 'saves.off');
  assert.equal(noteKey({ ...base, key: null, stored: true }), 'saves.found');
  assert.equal(noteKey({ ...base, key: null, hasDoc: false }), 'saves.off');
  assert.equal(noteKey({ ...base, hasDoc: false }), 'saves.armed');
  assert.equal(noteKey({ ...base, hasDoc: false, stored: true, saved: true }), 'saves.armed');
  assert.equal(noteKey({ ...base, saving: true }), 'saves.saving');
  assert.equal(noteKey({ ...base, saving: true, saved: true }), 'saves.saving');
  assert.equal(noteKey({ ...base, saved: true }), 'saves.on');
  assert.equal(noteKey({ ...base }), 'saves.off');
  assert.equal(noteKey({ ...base, saved: true, full: true }), 'saves.off');
});

test('leaving warns only while an open game has changes not yet written anywhere', () => {
  const doc = { turn: 3 };
  assert.equal(shouldWarnOnLeave({ doc: null, saved: false, savedToFile: false }), false);
  assert.equal(shouldWarnOnLeave({ doc, saved: false, savedToFile: false }), true);
  assert.equal(shouldWarnOnLeave({ doc, saved: true, savedToFile: false }), false);
  assert.equal(shouldWarnOnLeave({ doc, saved: false, savedToFile: true }), false);
  assert.equal(shouldWarnOnLeave({ doc, saved: true, savedToFile: true }), false);
  assert.equal(shouldWarnOnLeave({}), false);
});
