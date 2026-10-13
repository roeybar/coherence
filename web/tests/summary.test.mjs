import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { t } from '../src/copy.js';
import { errorText, parseError } from '../src/api/client.js';

// saves.jsx holds JSX, so its two plain functions are read out of the source
const SRC = readFileSync(new URL('../src/saves.jsx', import.meta.url), 'utf8');
function source(name) {
  const start = SRC.indexOf('export function ' + name + '(');
  assert.ok(start >= 0, name);
  let depth = 0;
  let end = SRC.indexOf('{', SRC.indexOf(')', start));
  for (; end < SRC.length; end++) {
    if (SRC[end] === '{') depth++;
    else if (SRC[end] === '}' && --depth === 0) break;
  }
  return SRC.slice(start + 'export '.length, end + 1);
}
const summaryOf = new Function('return (' + source('summaryOf') + ')')();
const summaryGate = new Function('summaryOf', 'return (' + source('summaryGate') + ')')(summaryOf);

const settle = () => new Promise((r) => setTimeout(r, 0));
const docWith = (text) => ({ history: [{ role: 'user', content: 'x' }, { role: 'assistant', content_en: text }] });

// digits that end exactly at the cut: the whole text holds a longer number,
// the summary as shown holds the canary itself
const CANARY = ['123', '45', '6789'].join('-');
const CUT_TEXT = 'Rain '.repeat(44) + 'wet a ' + CANARY
  + '0 and the lamps burn low over the wet stones of the market. '.repeat(3);

test('the summary as shown, cut and with its dots, is the text that is checked', async () => {
  const sent = [];
  let changed = 0;
  const shown = summaryGate(async (save) => {
    sent.push(save);
    return { ok: false, status: 451, error: { kind: 'save_blocked' } };
  }, () => { changed++; });
  const doc = docWith(CUT_TEXT);
  const rendered = summaryOf(doc);
  assert.ok(rendered.endsWith(' ' + CANARY + '...') && rendered.length === 240);
  assert.ok(!CUT_TEXT.includes(CANARY + ' ') && CUT_TEXT.includes(CANARY + '0'));
  assert.equal(shown(doc), '');
  await settle();
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0], { history: [{ role: 'assistant', content: rendered }] });
  assert.equal(changed, 1);
  assert.equal(shown(doc), '');
  await settle();
  assert.equal(sent.length, 1);
});

test('a passed summary shows once checked; a failed check keeps it hidden and is not retried', async () => {
  let calls = 0;
  const ok = summaryGate(async () => { calls++; return { ok: true, data: { ok: true } }; }, () => {});
  const doc = docWith('A short opening in the rain.');
  assert.equal(ok(doc), '');
  await settle();
  assert.equal(ok(doc), 'A short opening in the rain.');
  assert.equal(calls, 1);
  let failed = 0;
  const down = summaryGate(async () => { failed++; throw new Error('offline'); }, () => {});
  assert.equal(down(doc), '');
  await settle();
  assert.equal(down(doc), '');
  await settle();
  assert.equal(failed, 1);
  let none = 0;
  const empty = summaryGate(async () => { none++; return { ok: true, data: { ok: true } }; }, () => {});
  assert.equal(empty({ history: [] }), '');
  assert.equal(none, 0);
});

test('a save the server was too busy to check says the server is busy', () => {
  const at = SRC.indexOf('const REFUSED = {');
  assert.ok(at >= 0);
  const REFUSED = new Function('t', 'return ' + SRC.slice(at + 'const REFUSED = '.length, SRC.indexOf('\n};', at) + 2))(t);
  const refusedText = new Function('REFUSED', 't', 'errorText', 'return (' + source('refusedText') + ')')(REFUSED, t, errorText);
  const refused = (status, error) => parseError(status, { detail: { error, provider: null } });
  assert.equal(refusedText(refused(503, 'busy')), 'The server is busy right now. Try again in a moment.');
  assert.equal(refusedText(refused(500, 'internal')), 'The game server could not check this save, so it was not opened. Try again when the server is reachable.');
  assert.equal(refusedText(refused(451, 'save_blocked')), t('error.save_blocked'));
  assert.equal(refusedText(refused(413, 'body_too_large')), t('error.body_too_large'));
});

test('a busy check is not cached and the next render asks again; other failures stay hidden', async () => {
  const doc = docWith('A short opening in the rain.');
  const answers = [{ ok: false, error: { status: 503, kind: 'busy' } }, { ok: true, data: { ok: true } }];
  let calls = 0;
  let changed = 0;
  const gate = summaryGate(async () => answers[calls++], () => { changed++; });
  assert.equal(gate(doc), '');
  await settle();
  assert.equal(calls, 1);
  assert.equal(changed, 0);
  assert.equal(gate(doc), '');
  await settle();
  assert.equal(calls, 2);
  assert.equal(changed, 1);
  assert.equal(gate(doc), 'A short opening in the rain.');
  for (const kind of ['internal', 'save_blocked', 'engine_down', 'unknown']) {
    let n = 0;
    const other = summaryGate(async () => { n++; return { ok: false, error: { status: 500, kind } }; }, () => {});
    assert.equal(other(doc), '');
    await settle();
    assert.equal(other(doc), '');
    await settle();
    assert.equal(n, 1, kind);
  }
});
