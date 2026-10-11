// Saved games in the browser store, each in its own slot. No call here overwrites another
// game: writes are compare-and-swap, and a game whose slot changed elsewhere moves to a new id.
import { baseKey, keyring, WrongPassphraseOrCorrupt } from './crypto.js';
import { generations, readSlot, persist, newSlotId } from './state.js';

// Unlocking opens the latest stored game only when no game is open or being created.
export function shouldAutoOpen({ hasDoc, away, latest }) {
  return !hasDoc && !away && !!latest;
}

// The game a shown doc belongs to: a doc from open or file load is set to its game before it
// is shown; any other doc (a turn result, undo, undo-to, regenerate) inherits the game of the
// doc shown before it.
export function ownerOf(owners, doc, before) {
  if (!doc) return null;
  return owners.has(doc) ? owners.get(doc) : before;
}

const newer = (a, b) => {
  const ua = (a.doc && a.doc.updated) || '';
  const ub = (b.doc && b.doc.updated) || '';
  if (ua !== ub) return ua < ub ? 1 : -1;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
};

export function createSlots(storeP, { bus = null, onChange = () => {} } = {}) {
  let ring = null;
  let key = null;
  let openGid = null;
  let n = 0;
  let scanGen = 0;
  let refreshGen = 0;
  let keyGen = 0; // bumped when an unlock commits its key
  let pending = null; // the one switch under way: open, file load, new game or remove
  let away = false;
  let count = 0;
  const gids = new Map(); // game handle -> slot id
  const moved = new Map(); // slot id -> { to, at }: where its game moved after a conflict, and when
  let seq = 0;
  const kept = new Map(); // slot id -> game held in memory, not yet written
  const written = new Map(); // slot id -> game last written or read as stored
  const texts = new Map(); // slot id -> envelope text this page last read or wrote
  let listed = new Map(); // slot id -> game decrypted from the store
  let locked = new Set(); // slot ids with a valid envelope that does not open
  const queue = new Map();
  const gens = new Map();
  // games the engine has checked: only these show their name and summary
  const vetted = new WeakSet();
  const blocked = new WeakSet();
  const statusOf = (doc) => (blocked.has(doc) ? 'blocked' : vetted.has(doc) ? 'ok' : 'unchecked');

  const openSlot = () => (openGid === null ? null : gids.get(openGid));
  const gensFor = (id) => {
    if (!gens.has(id)) gens.set(id, generations());
    return gens.get(id);
  };
  const handle = (id) => {
    for (const [g, s] of gids) if (s === id) return g;
    const g = ++n;
    gids.set(g, id);
    return g;
  };

  async function doWrite(id, doc, live, mine) {
    // only writes queued before the move follow it; a later write to this id is another game
    for (let m = moved.get(id); m && mine <= m.at; m = moved.get(id)) id = m.to;
    const store = await storeP;
    if (!store || !key) return { id, r: null };
    const r = await persist(store, id, doc, key, live, texts.has(id) ? texts.get(id) : null);
    if (r.ok) {
      texts.set(id, r.text);
      written.set(id, doc);
      listed.set(id, doc);
      locked.delete(id);
      if (kept.get(id) === doc) kept.delete(id);
      if (bus) bus.post(id);
    } else if (r.kind === 'conflict') {
      // another tab changed this slot: its version stays, this game moves to a new id
      const fresh = newSlotId();
      moved.set(id, { to: fresh, at: seq });
      for (const [g, s] of gids) if (s === id) gids.set(g, fresh);
      if (kept.get(id) === doc) { kept.delete(id); kept.set(fresh, doc); }
      return writeSlot(fresh, doc);
    }
    return { id, r };
  }

  // writes to one slot run one after another; a newer write supersedes a queued older one
  function writeSlot(id, doc) {
    const live = gensFor(id).next();
    const mine = ++seq;
    const run = (queue.get(id) || Promise.resolve()).then(() => doWrite(id, doc, live, mine));
    queue.set(id, run.catch(() => {}));
    return run;
  }

  async function scan(r) {
    const store = await storeP;
    const ids = store ? await store.ids() : [];
    const found = new Map();
    const lockedIds = new Set();
    for (const id of ids) {
      try {
        found.set(id, await readSlot(store, id, r));
      } catch (e) {
        if (e instanceof WrongPassphraseOrCorrupt) lockedIds.add(id);
      }
    }
    return { ids, found, locked: lockedIds };
  }

  // leaves the open game at once, so a result that returns meanwhile lands in its slot;
  // the returned switch lets back() undo it; one switch at a time, so while another is under
  // way this refuses with 'busy'
  function keep(doc) {
    if (pending) return 'busy';
    const sw = { kind: 'leave', g: openGid, doc, away, landed: null, to: null };
    pending = sw;
    const id = openSlot();
    openGid = null;
    away = true;
    if (doc && id && written.get(id) !== doc) {
      kept.set(id, doc);
      if (key) writeSlot(id, doc);
    }
    return sw;
  }

  // a switch that failed: the game it left is open again; returns a result that landed for
  // that game meanwhile, which the page then shows
  function back(sw) {
    if (pending !== sw) return null;
    pending = null;
    openGid = sw.g;
    away = sw.away;
    if (sw.g === null) return null;
    kept.delete(gids.get(sw.g));
    return sw.landed;
  }

  const api = {
    keep,
    back,
    // a switch that ended without opening a game (a new game that did not start)
    settle(sw) {
      if (pending === sw) pending = null;
    },
    // a new or file game is open: this ends the switch that left the game before it
    adopt() {
      if (pending && pending.kind === 'leave') pending = null;
      const g = ++n;
      gids.set(g, newSlotId());
      openGid = g;
      away = false;
      return g;
    },
    // current is the open game, or a function returning it; the open game is left before the
    // read, and a failed read puts it back, calling restore with a result that landed meanwhile;
    // 'busy' while another switch is under way. vet checks the game before it opens; a
    // rejection is a failed read, and with no vet nothing opens
    async open(id, current, restore = () => {}, vet = async () => { throw new Error('unvetted'); }) {
      const now = () => (typeof current === 'function' ? current() : current);
      if (pending) return 'busy';
      if (id === openSlot()) return now();
      const sw = keep(now());
      sw.to = id;
      let doc;
      let text = null;
      try {
        if (kept.has(id)) doc = kept.get(id);
        else ({ doc, text } = await readSlot(await storeP, id, ring));
        await vet(doc);
      } catch (e) {
        const d = back(sw);
        if (d) restore(d);
        throw e;
      }
      vetted.add(doc);
      if (text !== null) {
        texts.set(id, text);
        written.set(id, doc);
        listed.set(id, doc);
        locked.delete(id);
      }
      kept.delete(id);
      openGid = handle(id);
      away = false;
      pending = null;
      return doc;
    },
    // a result for a game that is no longer open goes back to that game's slot
    land(gid, doc) {
      let id = gid === null ? undefined : gids.get(gid);
      if (id === undefined) {
        id = newSlotId();
        gids.set(++n, id);
      }
      // a result for the game the pending switch left: back() shows it if the switch fails
      if (gid !== null && pending && pending.kind === 'leave' && pending.g === gid) pending.landed = doc;
      kept.set(id, doc);
      away = false;
      if (key) writeSlot(id, doc);
    },
    write(doc) {
      const id = openSlot();
      return id ? writeSlot(id, doc) : Promise.resolve({ id: null, r: null });
    },
    // rule (i): with a key set, a passphrase that opens none of this page's slots is refused;
    // rule (ii): with no key, a passphrase that opens nothing while a valid envelope is locked
    // is refused, unless force is set
    async unlock(pass, hasDoc, { force = false } = {}) {
      const g = ++scanGen;
      const r = keyring(await baseKey(pass));
      const s = await scan(r);
      if (g !== scanGen) return { ok: false, stale: true };
      const mine = new Set([...texts.keys(), ...listed.keys()]);
      if (key && mine.size > 0 && ![...mine].some((id) => s.found.has(id))) return { ok: false, rule: 'i' };
      if (!key && !force && s.found.size === 0 && s.locked.size > 0) return { ok: false, rule: 'ii' };
      let best = null;
      for (const [id, f] of s.found) if (!best || newer({ id, doc: f.doc }, best) < 0) best = { id, doc: f.doc, text: f.text };
      let k;
      if (best) {
        const env = JSON.parse(best.text);
        k = await r.keyFor(env.kdf.salt, env.kdf.iterations);
      } else {
        k = await r.fresh();
      }
      if (g !== scanGen) return { ok: false, stale: true };
      ring = r;
      key = k;
      keyGen += 1;
      count = s.ids.length;
      listed = new Map();
      written.clear();
      for (const [id, f] of s.found) {
        listed.set(id, f.doc);
        written.set(id, f.doc);
        if (!texts.has(id)) texts.set(id, f.text);
      }
      locked = s.locked;
      for (const [id, d] of kept) writeSlot(id, d);
      return { ok: true, key: k, latest: api.latestId(), hasDoc: !!hasDoc };
    },
    // a scan applies only while no unlock started or committed since it began
    async refresh() {
      const u = scanGen;
      const k = keyGen;
      const g = ++refreshGen;
      const live = () => u === scanGen && k === keyGen && g === refreshGen;
      const store = await storeP;
      const ids = store ? await store.ids() : [];
      if (!live()) return;
      count = ids.length;
      if (!ring) return;
      const s = await scan(ring);
      if (!live()) return;
      count = s.ids.length;
      listed = new Map([...s.found].map(([id, f]) => [id, f.doc]));
      locked = s.locked;
    },
    // Save to file and remove: only a stored game that is not open and not held in memory;
    // the record goes only after the download started, and only if no tab changed it since;
    // it is a switch too, so 'busy' while another switch (such as an open of this id) is under way
    remove(id, download) {
      if (pending) return Promise.resolve('busy');
      const sw = { kind: 'remove', to: id };
      pending = sw;
      const run = (queue.get(id) || Promise.resolve()).then(async () => {
        try {
          if (id === openSlot() || kept.has(id)) return 'refused';
          const store = await storeP;
          const { doc, text } = await readSlot(store, id, ring);
          let ok = false;
          try {
            ok = await download(doc);
          } catch {
            ok = false;
          }
          if (!ok) return 'kept';
          // the game was entered while the file was written: its record stays
          if (id === openSlot() || kept.has(id)) return 'kept';
          const r = await store.cas(id, text, null);
          if (r === 'ok') {
            texts.delete(id);
            written.delete(id);
            listed.delete(id);
            locked.delete(id);
            count = Math.max(0, count - 1);
            if (bus) bus.post(id);
          }
          return r;
        } finally {
          if (pending === sw) pending = null;
        }
      });
      queue.set(id, run.catch(() => {}));
      return run;
    },
    // another tab changed one slot: re-read only that one
    async refreshOne(id) {
      const u = scanGen;
      const k = keyGen;
      const live = () => u === scanGen && k === keyGen;
      const store = await storeP;
      if (!store) return;
      const n = (await store.ids()).length;
      if (!live()) return;
      count = n;
      if (!ring || id === openSlot() || kept.has(id)) return;
      const r = ring;
      const text = await store.get(id);
      if (!live()) return;
      if (text === null) {
        listed.delete(id);
        locked.delete(id);
        return;
      }
      if (text === texts.get(id) && listed.has(id)) return;
      try {
        const { doc } = await readSlot(store, id, r);
        if (!live()) return;
        listed.set(id, doc);
        locked.delete(id);
      } catch (e) {
        if (e instanceof WrongPassphraseOrCorrupt && live()) locked.add(id);
      }
    },
    rows() {
      const open = openSlot();
      const ids = new Set([...listed.keys(), ...kept.keys()]);
      ids.delete(open);
      return [...ids]
        .map((id) => {
          const doc = kept.get(id) || listed.get(id);
          return { id, doc, kept: kept.has(id), removable: !kept.has(id), status: statusOf(doc) };
        })
        .sort(newer);
    },
    markVetted: (doc) => { if (doc) vetted.add(doc); },
    markBlocked: (doc) => { if (doc) blocked.add(doc); },
    isVetted: (doc) => !!doc && vetted.has(doc),
    isBlocked: (doc) => !!doc && blocked.has(doc),
    docOf: (id) => kept.get(id) || listed.get(id) || null,
    latestId() {
      const row = api.rows().find((x) => !x.kept);
      return row ? row.id : null;
    },
    // resolves once every queued write and removal has settled
    async idle() {
      for (let i = 0; i < 4; i += 1) await Promise.all(queue.values());
    },
    lockedCount: () => locked.size,
    stored: () => count > 0,
    slotNow: () => openGid,
    // true from the synchronous start of a switch until it settles; the page disables every
    // other switch and every game action meanwhile
    get switching() { return pending !== null; },
    slotOf: (gid) => (gid === null ? null : gids.get(gid) ?? null),
    isWritten: (doc) => openSlot() !== null && written.get(openSlot()) === doc,
    get away() { return away; },
    get key() { return key; },
  };

  if (bus) bus.on((id) => (typeof id === 'string' ? api.refreshOne(id) : api.refresh()).then(onChange));
  return api;
}
