import test from 'node:test';
import assert from 'node:assert/strict';
import { npcCards, NO_ITEMS, UNKNOWN_PLACE } from '../src/npcs.js';

const doc = {
  state: {
    npcs: {
      mira: { npc_id: 'mira', name: 'Mira', color_hue: 2, location: 'Village square', last_seen_turn: 3, persona: 'Keeps a tally.' },
      well: { npc_id: 'well', name: 'The well-keeper', color_hue: 200, location: ' North road ', last_seen_turn: 2 },
      ferry: { npc_id: 'ferry', name: 'Ferryman', color_hue: 130, location: '' },
    },
  },
  items: [
    { name: 'ledger', emoji: 'scroll', holder: 'npc:mira' },
    { name: 'bucket', emoji: 'container', holder: 'npc:the well-keeper' },
    { name: 'rope', emoji: 'rope', holder: 'player' },
    { name: 'crate', emoji: '', holder: 'scene', location: 'Chapel' },
  ],
};

test('an NPC shows the items whose holder is that NPC', () => {
  const [mira, well] = npcCards(doc);
  assert.deepEqual(mira.items, [{ emoji: 'scroll', name: 'ledger' }]);
  assert.equal(mira.itemsText, 'ledger');
  assert.equal(well.itemsText, 'bucket');
});

test('player and scene items never show on an NPC card', () => {
  const cards = npcCards(doc);
  for (const c of cards) assert.ok(!c.itemsText.includes('rope') && !c.itemsText.includes('crate'));
});

test('an NPC with no items shows the empty mark', () => {
  const ferry = npcCards(doc)[2];
  assert.deepEqual(ferry.items, []);
  assert.equal(ferry.itemsText, NO_ITEMS);
});

test('several items are joined with a middle dot, and holder names match ignoring case', () => {
  const d = {
    state: { npcs: { a: { name: '  Mira ' } } },
    items: [{ name: 'ledger', emoji: 'key', holder: 'npc:mira' }, { name: 'quill', holder: 'npc:mira' }, null, { holder: 'npc:mira' }],
  };
  assert.equal(npcCards(d)[0].itemsText, 'ledger | quill');
});

test('last seen is the place, trimmed, and Unknown when there is none', () => {
  const [mira, well, ferry] = npcCards(doc);
  assert.equal(mira.lastSeen, 'Village square');
  assert.equal(well.lastSeen, 'North road');
  assert.equal(ferry.lastSeen, UNKNOWN_PLACE);
});

test('card fields come from the NPC record; no save gives no cards', () => {
  const mira = npcCards(doc)[0];
  assert.equal(mira.key, 'mira');
  assert.equal(mira.name, 'Mira');
  assert.equal(mira.hue, 2);
  assert.equal(mira.persona, 'Keeps a tally.');
  assert.deepEqual(npcCards(null), []);
  assert.deepEqual(npcCards({}), []);
  assert.deepEqual(npcCards({ state: { npcs: { x: null } }, items: 5 }), []);
});
