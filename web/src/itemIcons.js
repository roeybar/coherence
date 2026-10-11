// Inventory tile icons, picked by the item's kind word (the "emoji" field).
// Each icon is a list of path strings for a 16x16 line drawing.
export const ITEM_KINDS = [
  'weapon', 'shield', 'armor', 'bow', 'container', 'coin', 'scroll', 'key', 'jewel',
  'food', 'herb', 'stone', 'rope', 'light', 'potion', 'staff', 'construct', 'item',
];

export const ITEM_ICONS = {
  weapon: ['M12.5 2.5l1 1-7.5 7.5-1-1z', 'M4 10l2 2', 'M5 11l-2.5 2.5'],
  shield: ['M8 2l5 2v4c0 3-2.2 5-5 6-2.8-1-5-3-5-6V4z'],
  armor: ['M5 2.5L3 4v4h2v5.5h6V8h2V4l-2-1.5', 'M5 2.5c.8 1.2 1.8 1.8 3 1.8s2.2-.6 3-1.8'],
  bow: ['M4 2c5 1.5 7 4 7 6s-2 4.5-7 6', 'M4 2v12', 'M2 8h11', 'M11.5 6.5L13 8l-1.5 1.5'],
  container: ['M3.5 6h9l-1 7.5h-7z', 'M6 6V4.5a2 2 0 0 1 4 0V6'],
  coin: ['M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11z', 'M8 5v6'],
  scroll: ['M4.5 3h8v8.5a2 2 0 0 1-2 2h-6', 'M4.5 3a1.5 1.5 0 0 0 0 3h1', 'M7 6.5h3.5M7 9h3.5'],
  key: ['M5.5 7.5a2.5 2.5 0 1 0 0 .01z', 'M8 8h6M12 8v2M14 8v1.5'],
  jewel: ['M4.5 3h7l2.5 3-6 7.5L2 6z', 'M2 6h12'],
  food: ['M3 9.5a5 3.5 0 0 1 10 0v2.5H3z', 'M6 7.5v1M10 7.5v1'],
  herb: ['M8 14V6', 'M8 9c-3 0-4.5-2-4.5-4.5C6.5 4.5 8 6 8 9z', 'M8 7c0-2.5 1.5-4 4.5-4 0 2.5-1.5 4-4.5 4z'],
  stone: ['M4 6l3-2.5h3.5L13 7l-1 5H4.5L3 9z'],
  rope: ['M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11z', 'M8 5a3 3 0 1 0 0 6a3 3 0 1 0 0-6z'],
  light: ['M8 2.5c1.5 2 2.5 3.2 2.5 4.6A2.5 2.5 0 0 1 5.5 7.1C5.5 5.7 6.5 4.5 8 2.5z', 'M6 10h4v3.5H6z'],
  potion: ['M6.5 2.5h3', 'M7 2.5v3.5L3.5 12a1.2 1.2 0 0 0 1 1.8h7a1.2 1.2 0 0 0 1-1.8L9 6V2.5', 'M5 10h6'],
  staff: ['M4 14L11 4', 'M11 4a1.8 1.8 0 1 0 0.01 0'],
  construct: ['M4 5h8v7H4z', 'M8 5V3M6.5 8h.01M9.5 8h.01M6.5 10.5h3'],
  item: ['M8 2l5.5 3v6L8 14l-5.5-3V5z', 'M2.5 5L8 8l5.5-3M8 8v6'],
};

// The icon for a kind word; anything else gets the generic item icon.
export function itemIcon(kind) {
  const k = typeof kind === 'string' ? kind.trim().toLowerCase() : '';
  return Object.prototype.hasOwnProperty.call(ITEM_ICONS, k) ? ITEM_ICONS[k] : ITEM_ICONS.item;
}
