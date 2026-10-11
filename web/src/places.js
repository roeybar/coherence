// Places the player has met so far: where they are, then where the people and
// things they have seen are. Everything comes from the save, nothing is fetched.
export const MAX_PLACES = 12;

const clean = (v) => (typeof v === 'string' ? v.trim() : '');

export function knownPlaces(doc) {
  const st = (doc && doc.state) || {};
  const out = [];
  const seen = new Set();
  const add = (value, here) => {
    const name = clean(value);
    const key = name.toLowerCase();
    if (!name || seen.has(key) || out.length >= MAX_PLACES) return;
    seen.add(key);
    out.push({ name, here });
  };
  add(st.location, true);
  const npcs = Object.values(st.npcs || {}).filter((n) => n && typeof n === 'object');
  npcs.sort((a, b) => (Number(b.last_seen_turn) || 0) - (Number(a.last_seen_turn) || 0));
  for (const n of npcs) {
    if (out.length >= MAX_PLACES) break;
    add(n.location, false);
  }
  const items = (doc && Array.isArray(doc.items)) ? doc.items : [];
  for (const it of items) {
    if (out.length >= MAX_PLACES) break;
    if (it && it.holder === 'scene') add(it.location, false);
  }
  return out;
}
