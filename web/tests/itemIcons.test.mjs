import test from 'node:test';
import assert from 'node:assert/strict';
import { ITEM_KINDS, ITEM_ICONS, itemIcon } from '../src/itemIcons.js';

const ENGINE_KINDS = [
  'weapon', 'shield', 'armor', 'bow', 'container', 'coin', 'scroll', 'key', 'jewel',
  'food', 'herb', 'stone', 'rope', 'light', 'potion', 'staff', 'construct', 'item',
];

test('every kind word the engine sends maps to its own icon', () => {
  assert.deepEqual(ITEM_KINDS, ENGINE_KINDS);
  for (const k of ENGINE_KINDS) {
    const paths = itemIcon(k);
    assert.equal(paths, ITEM_ICONS[k], k);
    assert.ok(Array.isArray(paths) && paths.length > 0 && paths.every((d) => typeof d === 'string' && d.length > 0), k);
  }
  assert.deepEqual(Object.keys(ITEM_ICONS).sort(), [...ENGINE_KINDS].sort());
});

test('an unknown or missing kind gets the generic item icon', () => {
  for (const v of ['', 'sparkles', String.fromCodePoint(0x1f5e1), null, undefined, 7, 'constructor', '__proto__']) {
    assert.equal(itemIcon(v), ITEM_ICONS.item, String(v));
  }
  assert.equal(itemIcon(' Potion '), ITEM_ICONS.potion);
});
