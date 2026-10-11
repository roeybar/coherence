// The browser store for saved games: one IndexedDB record per game, holding only the
// envelope text. See state.js for the store interface.
import { ID_RE } from './state.js';

export const DB_NAME = 'coherence';
export const STORE = 'saves';
export const LEGACY_KEY = 'coherence.save';

function idbStore(db) {
  const tx = (mode, opts) => db.transaction(STORE, mode, opts);
  const read = (fn, fallback) => new Promise((res) => {
    try {
      const req = fn(tx('readonly').objectStore(STORE));
      req.onsuccess = () => res(req.result);
      req.onerror = () => res(fallback);
    } catch {
      res(fallback);
    }
  });
  return {
    async ids() {
      const keys = await read((os) => os.getAllKeys(), []);
      return (keys || []).filter((k) => typeof k === 'string' && ID_RE.test(k)).sort();
    },
    async get(id) {
      const v = await read((os) => os.get(id), null);
      return typeof v === 'string' ? v : null;
    },
    // compare and swap in one readwrite transaction; these are serialised across tabs
    cas(id, want, next, opts) {
      return new Promise((res) => {
        try {
          let out = 'full';
          const t = tx('readwrite', opts);
          const os = t.objectStore(STORE);
          const g = os.get(id);
          g.onsuccess = () => {
            const cur = g.result ?? null;
            if (cur !== want) { out = 'conflict'; return; }
            try {
              if (next === null) os.delete(id);
              else os.put(next, id);
              out = 'ok';
            } catch {
              out = 'full';
              t.abort();
            }
          };
          t.oncomplete = () => res(out);
          t.onabort = () => res(out === 'conflict' ? 'conflict' : 'full');
          t.onerror = t.onabort;
        } catch {
          res('full');
        }
      });
    },
  };
}

// Resolves to a store, or null when IndexedDB is missing, blocked, failing or slow.
export function openIdbStore(idb = globalThis.indexedDB, ms = 3000) {
  return new Promise((resolve) => {
    let done = false;
    let timer = null;
    const finish = (db) => {
      if (done) { if (db) db.close(); return; }
      done = true;
      clearTimeout(timer);
      resolve(db ? idbStore(db) : null);
    };
    timer = setTimeout(() => finish(null), ms);
    const attempt = (version) => {
      const req = version ? idb.open(DB_NAME, version) : idb.open(DB_NAME);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
      };
      req.onblocked = () => finish(null);
      req.onerror = () => finish(null);
      req.onsuccess = () => {
        const db = req.result;
        db.onversionchange = () => db.close();
        if (db.objectStoreNames.contains(STORE)) { finish(db); return; }
        // an existing database without the store: reopen one version up to create it
        const next = db.version + 1;
        db.close();
        if (version || done) finish(null);
        else {
          try {
            attempt(next);
          } catch {
            finish(null);
          }
        }
      };
    };
    try {
      attempt(0);
    } catch {
      finish(null);
    }
  });
}

const lsGet = (ls) => {
  try {
    return ls ? ls.getItem(LEGACY_KEY) : null;
  } catch {
    return null;
  }
};

// removes the record only if it still holds the copied text, in one synchronous block
const lsRemove = (ls, copied) => {
  try {
    if (ls.getItem(LEGACY_KEY) !== copied) return false;
    ls.removeItem(LEGACY_KEY);
    return true;
  } catch {
    return false;
  }
};

function lsCas(ls, want, next) {
  try {
    if (lsGet(ls) !== want) return 'conflict';
    if (next === null) ls.removeItem(LEGACY_KEY);
    else ls.setItem(LEGACY_KEY, next);
    return 'ok';
  } catch {
    return 'full';
  }
}

// Moves the single localStorage save into the database (copy, read back, compare, then
// remove), and returns the store the page uses. When the move cannot finish, both stay
// and the localStorage record is read and written in place.
export async function prepareStore(db, ls) {
  const text = lsGet(ls);
  let localId = null;
  if (text !== null) {
    let moved = false;
    if (db) {
      const cur = await db.get('legacy');
      if (cur === null) {
        moved = (await db.cas('legacy', null, text, { durability: 'strict' })) === 'ok' && (await db.get('legacy')) === text;
      } else {
        moved = cur === text;
      }
    }
    if (!(moved && lsRemove(ls, text))) localId = db && (await db.get('legacy')) !== null ? 'legacy-local' : 'legacy';
  }
  return {
    async ids() {
      const out = db ? await db.ids() : [];
      if (localId && lsGet(ls) !== null && !out.includes(localId)) out.push(localId);
      return out.sort();
    },
    async get(id) {
      if (id === localId) return lsGet(ls);
      return db ? db.get(id) : null;
    },
    async cas(id, want, next) {
      if (id === localId) return lsCas(ls, want, next);
      return db ? db.cas(id, want, next) : 'full';
    },
  };
}

// Tells other tabs that the store changed; null where BroadcastChannel is missing.
export function makeBus() {
  try {
    const ch = new BroadcastChannel('coherence.saves');
    return {
      post(id) {
        try {
          ch.postMessage(id);
        } catch {
          /* the other tabs re-read on their next unlock */
        }
      },
      on(fn) { ch.addEventListener('message', (e) => fn(e.data)); },
    };
  } catch {
    return null;
  }
}
