import test from 'node:test';
import assert from 'node:assert/strict';
import { knownPlaces, MAX_PLACES } from '../src/places.js';
import { pickAlert } from '../src/alerts.js';
import { loadPrefs, DEFAULTS, PREFS_KEY } from '../src/prefs.js';

const KINDS = ['maint', '451', '500', 'grant', 'xmas'];

function mulberry(seed) {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test('stress: places on 2000 npcs and 5000 items stay capped and fast', () => {
  const npcs = {};
  for (let i = 0; i < 2000; i += 1) npcs['n' + i] = { name: 'n' + i, location: 'Place ' + (i % 900), last_seen_turn: i };
  const items = [];
  for (let i = 0; i < 5000; i += 1) items.push({ holder: i % 3 ? 'scene' : 'player', location: 'Spot ' + i });
  const t0 = performance.now();
  const out = knownPlaces({ state: { location: 'Here', npcs }, items });
  const ms = performance.now() - t0;
  console.log(`${out.length <= MAX_PLACES && ms < 100 ? 'PASS' : 'FAIL'} places ${out.length} chips in ${ms.toFixed(1)} ms`);
  assert.ok(out.length <= MAX_PLACES);
  assert.ok(ms < 100);
  assert.equal(out[0].name, 'Here');
});

test('stress: 1000 random alert transitions keep one kind and the priority', () => {
  const rnd = mulberry(7);
  const errs = [null, { kind: 'engine_down' }, { kind: 'content_blocked' }, { kind: 'unknown' }, { kind: 'rate_limited' }];
  const days = [new Date(2026, 5, 1), new Date(2026, 11, 25)];
  let bad = 0;
  for (let i = 0; i < 1000; i += 1) {
    const error = errs[Math.floor(rnd() * errs.length)];
    const engineDown = rnd() < 0.2;
    const rose = rnd() < 0.5;
    const r = pickAlert({ error, engineDown, milestone: rose, now: days[Math.floor(rnd() * 2)] });
    let want;
    if (engineDown || (error && error.kind === 'engine_down')) want = 'maint';
    else if (error && error.kind === 'content_blocked') want = '451';
    else if (error) want = '500';
    else want = rose ? 'celebrate' : 'none';
    if (want === 'none') {
      if (r !== null) bad += 1;
    } else if (!r || !KINDS.includes(r.kind)) {
      bad += 1;
    } else if (want === 'celebrate' ? (r.kind !== 'grant' && r.kind !== 'xmas') : r.kind !== want) {
      bad += 1;
    }
  }
  console.log(`${bad === 0 ? 'PASS' : 'FAIL'} alerts 1000 transitions, ${bad} violations`);
  assert.equal(bad, 0);
});

test('stress: 500 corrupt stored prefs fall back to defaults', () => {
  const rnd = mulberry(11);
  const pool = ['{', '[1,2', 'null', '"x"', '{"submitKey":null}', '{"submitKey":{}}', '\u0000\u0001', '{"__proto__":{"submitKey":"enter"}}'];
  let bad = 0;
  for (let i = 0; i < 500; i += 1) {
    let raw = pool[Math.floor(rnd() * pool.length)];
    if (rnd() < 0.5) raw += Math.floor(rnd() * 1e9).toString(36);
    const got = loadPrefs({ getItem: (k) => (k === PREFS_KEY ? raw : null), setItem() {} });
    if (JSON.stringify(got) !== JSON.stringify(DEFAULTS)) bad += 1;
  }
  console.log(`${bad === 0 ? 'PASS' : 'FAIL'} prefs 500 corrupt values, ${bad} escaped`);
  assert.equal(bad, 0);
});
