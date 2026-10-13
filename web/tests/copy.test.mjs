import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { COPY, t } from '../src/copy.js';
import { PROVIDERS, errorText, parseError } from '../src/api/client.js';

const src = fileURLToPath(new URL('../src/', import.meta.url));
const files = (dir) => readdirSync(dir).flatMap((f) => {
  const p = join(dir, f);
  return statSync(p).isDirectory() ? files(p) : /\.(js|jsx)$/.test(f) ? [p] : [];
});
const KINDS = ['byok_required', 'byok_rejected', 'rate_limited', 'bad_save', 'bad_session',
  'provider_unavailable', 'provider_rate_limited', 'body_too_large', 'wrong_passphrase', 'content_blocked',
  'intervention_missing', 'bad_pack', 'bad_request', 'unknown_provider', 'internal', 'busy', 'key_store_unavailable',
  'output_blocked', 'save_blocked', 'text_too_large', 'context_too_large', 'actions_rate_limited', 'key_store_locked', 'key_other_provider'];

test('every key referenced in src exists in COPY', () => {
  const missing = [];
  let seen = 0;
  for (const file of files(src)) {
    if (file.endsWith('copy.js')) continue;
    const text = readFileSync(file, 'utf8');
    for (const m of text.matchAll(/\bt\('([a-z_.]+)'\)/g)) {
      seen += 1;
      if (!(m[1] in COPY)) missing.push(m[1]);
    }
  }
  assert.ok(seen > 20);
  assert.deepEqual(missing, []);
});

test('dynamic keys are covered: providers and error kinds', () => {
  for (const p of PROVIDERS) assert.ok(`provider.${p}.label` in COPY, p);
  for (const k of KINDS) assert.ok(`error.${k}` in COPY, k);
  assert.ok('error.unknown' in COPY);
});

test('every value is a non-empty ASCII string', () => {
  for (const [k, v] of Object.entries(COPY)) {
    assert.equal(typeof v, 'string', k);
    assert.ok(v.length > 0, k);
    assert.ok(/^[\x20-\x7e]*$/.test(v), k);
  }
});

test('no key carries a pending marker', () => {
  const pending = Object.entries(COPY).filter(([, v]) => v.includes('[pending copy:'));
  assert.deepEqual(pending, []);
});

test('the approved provider and disclosure wording is in place', () => {
  assert.equal(COPY['provider.anthropic.label'], 'Claude (tuned)');
  assert.equal(COPY['provider.hint'], "The game's prompts were tuned on Claude.");
  assert.equal(COPY['hosted.disclosure.title'], 'How your key is handled');
  const lines = [1, 2, 3, 4, 5, 6].map((n) => COPY['hosted.disclosure.' + n]);
  assert.deepEqual(lines, [
    "Your API key stays in this browser tab's memory. It is not saved, and reloading the page forgets it.",
    'Each move sends your key along with the request. The server uses it only to pass that request to your provider and does not keep it.',
    'The server does not write your key or your game to disk or to its logs. An automated test in the repository checks this.',
    'Your provider charges your key for each move sent to the model. Most providers let you set a spending limit in their console.',
    'Set a passphrase in the menu and your game is saved in this browser. Use "Save to file" to keep a copy elsewhere.',
    'You can make one move every 15 seconds.',
  ]);
  assert.ok(!('hosted.disclosure.7' in COPY));
});

test('t() fills placeholders and returns unknown keys as they are', () => {
  assert.equal(t('error.rate_limited', { seconds: 15, retry_after: 3 }), 'You can make one move every 15 seconds. Try again in 3 s.');
  assert.equal(t('no.such.key'), 'no.such.key');
});

test('unknown error kinds fall back to error.unknown', () => {
  const err = parseError(500, { detail: { error: 'something_new' } });
  assert.equal(errorText(err), COPY['error.unknown']);
});

test('save slot copy: confirmations gone, list and passphrase wording in place', () => {
  for (const k of ['saves.replace_confirm', 'saves.replace', 'saves.open_confirm']) assert.ok(!(k in COPY), k);
  assert.equal(COPY['saves.row.remove'], 'Save to file and remove');
  assert.equal(COPY['saves.locked'], 'Some saved games in this browser use a different passphrase. Enter that passphrase to list them.');
  assert.equal(COPY['saves.new_passphrase'], 'Start saving with this new passphrase');
  assert.equal(COPY['saves.found'], 'This browser has saved games. Enter the save passphrase to open them.');
  assert.equal(COPY['saves.saving'], 'Saving in this browser...');
  assert.equal(COPY['saves.armed'], 'Autosave is on. Your next new or loaded game is saved in this browser.');
  const order = Object.keys(COPY);
  assert.equal(order.indexOf('saves.saving'), order.indexOf('saves.off') + 1);
  assert.equal(order.indexOf('saves.armed'), order.indexOf('saves.found') + 1);
  assert.equal(COPY['saves.open'], 'Open the latest saved game');
  assert.equal(COPY['saves.passphrase.hint'], 'Browser saves are always encrypted with this passphrase; save files are too, unless you choose plain JSON. It is never stored, so you enter it again after a reload.');
});

test('Settings copy covers only the shipped sections', () => {
  assert.deepEqual(Object.keys(COPY).filter((k) => k.startsWith('settings.')), [
    'settings.title', 'settings.apikey', 'settings.playmode', 'settings.playmode.text',
    'settings.playmode.visual', 'settings.submit', 'settings.submit.enter',
    'settings.submit.shift_enter', 'settings.lock_store']);
});

test('the content filter and save check copy is in place', () => {
  assert.deepEqual(Object.fromEntries([
    'error.output_blocked', 'error.output_blocked_regenerate', 'error.output_blocked_new', 'error.output_blocked_other',
    'error.save_blocked', 'error.text_too_large', 'error.text_too_large_hosted', 'error.context_too_large', 'error.actions_rate_limited',
    'saves.check_unavailable', 'saves.row.unchecked', 'saves.row.blocked',
  ].map((k) => [k, COPY[k]])), {
    'error.output_blocked': 'The reply to this move was blocked by the content filter, so it was not shown or saved. Your move was not played; try wording it differently.',
    'error.output_blocked_regenerate': 'The new reply was blocked by the content filter, so it was not shown or saved. The earlier reply stays as it was.',
    'error.output_blocked_new': 'The new game could not start: text written for it was blocked by the content filter, so nothing was shown or saved. Try different choices or wording.',
    'error.output_blocked_other': "The game's response was blocked by the content filter, so it was not shown or saved. Your game did not change.",
    'error.save_blocked': 'This save holds text the content filter blocks, so it cannot be opened or played.',
    'error.text_too_large': 'This save holds more text than this server checks, so it cannot be opened here. Keep it as a file.',
    'error.text_too_large_hosted': "This save holds more text than this server checks (its limit is 500,000 characters), so it cannot be opened here. To keep playing it, run Coherence on your own computer (see Quickstart in the README).",
    'error.context_too_large': "This story's world state has grown too large for the game to send to the model in one move, so your move was not played. Load an earlier save or start a new game.",
    'error.actions_rate_limited': 'Too many requests in a short time. Try again in {retry_after} s.',
    'saves.check_unavailable': 'The game server could not check this save, so it was not opened. Try again when the server is reachable.',
    'saves.row.unchecked': 'Saved game',
    'saves.row.blocked': 'Blocked save',
  });
  assert.equal(COPY['alert.451.label'], '451 - Turn blocked');
  assert.equal(COPY['alert.451.body'], "That action crosses a line this realm can't. Rephrase and send again.");
});
