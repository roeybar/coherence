// NPC book cards, built from the save: the items each NPC holds and the place
// they were last seen. Nothing is fetched.
export const NO_ITEMS = '-';
const DOT = ' | ';
export const UNKNOWN_PLACE = 'Unknown';

const clean = (v) => (typeof v === 'string' ? v.trim() : '');

// Items name their holder as "npc:" plus the NPC name, trimmed and lower-cased.
const holderOf = (npc) => 'npc:' + clean(npc.name).toLowerCase();

export function npcCards(doc) {
  const st = (doc && doc.state) || {};
  const items = (doc && Array.isArray(doc.items)) ? doc.items : [];
  const npcs = Object.values(st.npcs || {}).filter((n) => n && typeof n === 'object');
  return npcs.map((n) => {
    const holder = holderOf(n);
    const held = items
      .filter((it) => it && it.holder === holder && clean(it.name))
      .map((it) => ({ emoji: clean(it.emoji), name: clean(it.name) }));
    return {
      key: n.npc_id || n.name,
      name: n.name,
      hue: n.color_hue || 0,
      persona: n.persona,
      items: held,
      itemsText: held.length ? held.map((it) => it.name).join(DOT) : NO_ITEMS,
      lastSeen: clean(n.location) || UNKNOWN_PLACE,
    };
  });
}
