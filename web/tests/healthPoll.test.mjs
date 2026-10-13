import test from 'node:test';
import assert from 'node:assert/strict';
import { period, nextLeft, downAfter, DOWN_SECONDS, UP_SECONDS } from '../src/healthPoll.js';

test('the check comes every 5 s while down and every 30 s while up', () => {
  assert.equal(DOWN_SECONDS, 5);
  assert.equal(UP_SECONDS, 30);
  assert.equal(period(true), 5);
  assert.equal(period(false), 30);
});

test('the countdown reaches a check once per period', () => {
  for (const down of [true, false]) {
    let left = period(down);
    const due = [];
    for (let i = 1; i <= period(down) * 3; i += 1) {
      const s = nextLeft(left, down);
      left = s.left;
      if (s.check) due.push(i);
    }
    const p = period(down);
    assert.deepEqual(due, [p, 2 * p, 3 * p]);
  }
});

test('only an ok answer counts as up', () => {
  assert.equal(downAfter(true, { ok: true }), false);
  assert.equal(downAfter(false, { ok: false }), true);
  assert.equal(downAfter(false, null), true);
  assert.equal(downAfter(false, undefined), true);
});

test('stress: 10,000 ticks with the engine flipping give one check per period', () => {
  let seed = 7;
  const rnd = (n) => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed % n;
  };
  let down = false;
  let left = period(down);
  let since = 0;
  for (let i = 0; i < 10000; i += 1) {
    const s = nextLeft(left, down);
    left = s.left;
    since += 1;
    if (s.check) {
      assert.equal(since, period(down));
      since = 0;
      // the answer may flip the state; the countdown restarts at the new period
      const was = down;
      down = downAfter(was, rnd(4) === 0 ? { ok: was } : { ok: !was });
      left = period(down);
    }
    assert.ok(since < period(down) || since === 0);
  }
});
