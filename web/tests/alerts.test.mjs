import test from 'node:test';
import assert from 'node:assert/strict';
import { pickAlert, isSeason, milestone, shudders, maintCopy, blockedKey, KEY_KINDS, opensSettings, newGameKey } from '../src/alerts.js';
import { COPY, t } from '../src/copy.js';

const now = new Date(2026, 5, 10);
const KINDS = ['maint', '451', '500', 'grant', 'xmas'];

test('key errors offer Settings and other errors do not', () => {
  assert.deepEqual(KEY_KINDS, ['byok_required', 'byok_rejected', 'key_store_locked', 'key_other_provider']);
  for (const kind of KEY_KINDS) assert.equal(opensSettings({ kind }), true);
  for (const kind of ['internal', 'content_blocked', 'no_game', 'engine_down', 'unknown']) assert.equal(opensSettings({ kind }), false);
  assert.equal(opensSettings(null), false);
});

test('new game errors name the creation and keep key errors distinct', () => {
  assert.equal(newGameKey(null), null);
  assert.equal(newGameKey({ kind: 'output_blocked' }), 'error.output_blocked_new');
  assert.equal(newGameKey({ kind: 'content_blocked' }), 'error.content_blocked_new');
  for (const kind of ['provider_unavailable', 'internal', 'unknown', 'busy']) assert.equal(newGameKey({ kind }), 'error.new_failed');
  for (const kind of [...KEY_KINDS, 'rate_limited', 'engine_down']) assert.equal(newGameKey({ kind }), null);
});

test('priority is maintenance, then 451, then 500, then a celebration', () => {
  assert.equal(pickAlert({ error: { kind: 'content_blocked' }, engineDown: true, milestone: true, now }).kind, 'maint');
  assert.equal(pickAlert({ error: { kind: 'engine_down' }, now }).kind, 'maint');
  assert.equal(pickAlert({ error: { kind: 'content_blocked' }, milestone: true, now }).kind, '451');
  assert.equal(pickAlert({ error: { kind: 'unknown' }, milestone: true, now }).kind, '500');
  assert.equal(pickAlert({ milestone: true, now }).kind, 'grant');
  assert.equal(pickAlert({ now }), null);
});

test('the season is Dec 24 to 26 on the given date', () => {
  assert.equal(isSeason(new Date(2026, 11, 23)), false);
  for (const d of [24, 25, 26]) assert.equal(isSeason(new Date(2026, 11, d)), true);
  assert.equal(isSeason(new Date(2026, 11, 27)), false);
  assert.equal(isSeason(new Date(2026, 10, 25)), false);
  assert.equal(pickAlert({ milestone: true, now: new Date(2026, 11, 25) }).kind, 'xmas');
});

test('a milestone is the chapter going up', () => {
  const at = (n) => ({ state: { chapter: n } });
  assert.equal(milestone(at(1), at(2)), true);
  assert.equal(milestone(at(2), at(2)), false);
  assert.equal(milestone(at(3), at(2)), false);
  assert.equal(milestone(null, at(2)), false);
  assert.equal(milestone(at(1), { state: {} }), false);
});

test('every alert kind has its copy, and no draft mentions admins or keepers', () => {
  for (const k of KINDS) {
    assert.ok(`alert.${k}.label` in COPY, k);
    if (k !== '500') assert.ok(`alert.${k}.body` in COPY, k);
  }
  for (const [key, v] of Object.entries(COPY)) {
    if (key.startsWith('alert.')) assert.ok(!/admin|keeper/i.test(v), key);
  }
  assert.equal(t('alert.grant.body', { chapter: 3 }), 'The story turns a page. Chapter 3 begins.');
});

test('only a 5xx error wears the realm-shuddered label', () => {
  assert.equal(shudders({ status: 500, kind: 'internal' }), true);
  assert.equal(shudders({ status: 502, kind: 'engine_unavailable' }), true);
  for (const status of [400, 413, 428, 429]) assert.equal(shudders({ status, kind: 'unknown' }), false, String(status));
  assert.equal(shudders({ kind: 'unknown' }), false);
  assert.equal(shudders(null), false);
});

test('maintenance keeps the saved text only for a stored game, and offers no reload without one', () => {
  assert.deepEqual(maintCopy(true), { body: 'alert.maint.body', refresh: true });
  assert.deepEqual(maintCopy(false), { body: 'maintenance.unsaved', refresh: false });
  assert.equal(t('maintenance.unsaved'), 'Your story is still open in this tab. Use "Save to file" before you reload.');
  assert.equal(t('alert.maint.body'), 'The realm is being reshaped - your story is saved.');
});

test('the three blocked kinds give the 451 alert', () => {
  for (const kind of ['content_blocked', 'output_blocked', 'save_blocked']) {
    assert.equal(pickAlert({ error: { kind }, now }).kind, '451', kind);
  }
  assert.equal(pickAlert({ error: { kind: 'text_too_large' }, now }).kind, '500');
});

test('the 451 body names what was blocked', () => {
  assert.equal(blockedKey(null), 'alert.451.body');
  assert.equal(blockedKey({ kind: 'content_blocked', where: 'turn' }), 'alert.451.body');
  const out = (where) => blockedKey({ kind: 'output_blocked', where });
  assert.equal(out('turn'), 'error.output_blocked');
  assert.equal(out('regenerate'), 'error.output_blocked_regenerate');
  assert.equal(out('new'), 'error.output_blocked_new');
  assert.equal(out('other'), 'error.output_blocked_other');
  assert.equal(out(undefined), 'error.output_blocked_other');
  assert.equal(blockedKey({ kind: 'save_blocked', where: 'turn' }), 'error.save_blocked');
  for (const w of ['turn', 'regenerate', 'new', 'other']) assert.ok(out(w) in COPY, w);
});
