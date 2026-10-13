import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createClient, parseError, errorText } from '../src/api/client.js';
import { COPY, t } from '../src/copy.js';

const TOO_LARGE = 'This save holds more text than this server checks, so it cannot be opened here. Keep it as a file.';
const capped = (scope) => ({ detail: { error: 'text_too_large', provider: null, ...(scope === undefined ? {} : { scope }) } });

function stub(status, body) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  return { calls, fetchImpl };
}

async function refused(mode, body, kind = 'turn') {
  const { fetchImpl } = stub(413, body);
  const client = createClient({ fetchImpl });
  client.configure({ mode });
  const r = kind === 'check' ? await client.checkSave({}) : await client.turn({}, 'look');
  assert.equal(r.ok, false);
  return r.error;
}

// saves.jsx holds JSX, so refusedText and REFUSED are read out of the source
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
const at = SRC.indexOf('const REFUSED = {');
const REFUSED = new Function('t', 'return ' + SRC.slice(at + 'const REFUSED = '.length, SRC.indexOf('\n};', at) + 2))(t);
const refusedText = new Function('REFUSED', 't', 'errorText', 'return (' + source('refusedText') + ')')(REFUSED, t, errorText);

test('hosted: a save over the history cap gets the plain too-large text', async () => {
  assert.equal(COPY['error.text_too_large'], TOO_LARGE);
  for (const via of ['check', 'turn']) {
    const error = await refused('hosted', capped('history'), via);
    assert.equal(error.kind, 'text_too_large');
    assert.notEqual(error.hosted, true);
    assert.equal(errorText(error), TOO_LARGE);
    assert.ok(!errorText(error).includes('500,000'));
    assert.equal(refusedText(error), TOO_LARGE);
  }
});

test('hosted: any other text_too_large keeps the hosted text', async () => {
  for (const scope of [undefined, 'History', 'other', ['history']]) {
    const error = await refused('hosted', capped(scope));
    assert.equal(error.hosted, true, String(scope));
    assert.equal(errorText(error), COPY['error.text_too_large_hosted']);
  }
});

test('selfhost: the generic text, with or without scope', async () => {
  for (const scope of [undefined, 'history']) {
    const error = await refused('selfhost', capped(scope));
    assert.notEqual(error.hosted, true);
    assert.equal(errorText(error), COPY['error.text_too_large']);
  }
});

test('parseError keeps its shape when a scope is present', () => {
  assert.deepEqual(parseError(413, { detail: { error: 'text_too_large', scope: 'history' } }),
    { status: 413, kind: 'text_too_large', provider: null, retryAfter: null, stored: [] });
});

test('stress: the hosted flag is set exactly for hosted text_too_large without scope history', async () => {
  let seed = 7;
  const next = (n) => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; };
  const modes = ['hosted', 'selfhost'];
  const kinds = ['text_too_large', 'body_too_large', 'context_too_large', 'rate_limited'];
  const scopes = [undefined, 'history', 'History', 'other', ['history'], null, 7];
  for (let i = 0; i < 10 ** 3; i++) {
    const mode = modes[next(modes.length)];
    const kind = kinds[next(kinds.length)];
    const scope = scopes[next(scopes.length)];
    const body = { detail: { error: kind, provider: null, ...(scope === undefined ? {} : { scope }) } };
    const error = await refused(mode, body);
    const want = mode === 'hosted' && kind === 'text_too_large' && scope !== 'history';
    assert.equal(error.hosted === true, want, JSON.stringify({ mode, kind, scope }));
  }
});
