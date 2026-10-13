import test from 'node:test';
import assert from 'node:assert/strict';
import { knownPlaces, MAX_PLACES } from '../src/places.js';

const doc = (state, items) => ({ state, items });

test('here comes first, then npc places by last seen, then scene items', () => {
  const d = doc(
    { location: 'Village square', npcs: {
      a: { location: 'North road', last_seen_turn: 2 },
      b: { location: 'Ferry crossing', last_seen_turn: 9 },
    } },
    [{ holder: 'scene', location: 'Chapel' }, { holder: 'player', location: 'carried' }],
  );
  assert.deepEqual(knownPlaces(d), [
    { name: 'Village square', here: true },
    { name: 'Ferry crossing', here: false },
    { name: 'North road', here: false },
    { name: 'Chapel', here: false },
  ]);
});

test('duplicates ignore case and spacing; empty and odd values are skipped', () => {
  const d = doc(
    { location: ' Village Square ', npcs: { a: { location: 'village square' }, b: { location: '   ' }, c: { location: 7 }, d: null } },
    [null, { holder: 'scene', location: 'VILLAGE SQUARE' }],
  );
  assert.deepEqual(knownPlaces(d), [{ name: 'Village Square', here: true }]);
});

test('no save and an empty save give no places', () => {
  assert.deepEqual(knownPlaces(null), []);
  assert.deepEqual(knownPlaces({}), []);
});

test('the list is capped', () => {
  const npcs = {};
  for (let i = 0; i < 40; i += 1) npcs['n' + i] = { location: 'Place ' + i, last_seen_turn: i };
  const out = knownPlaces(doc({ location: 'Here', npcs }, []));
  assert.equal(out.length, MAX_PLACES);
  assert.equal(out[0].here, true);
});
