// In-memory stand-ins for the browser store, the tab bus and localStorage.

// fail(id, op) returning true makes that write report 'full';
// pause() holds the next get until the function it returns is called
export function memStore({ fail = () => false } = {}) {
  const map = new Map();
  const holds = [];
  return {
    map,
    pause() {
      let go;
      holds.push(new Promise((r) => { go = r; }));
      return () => go();
    },
    async ids() { return [...map.keys()].sort(); },
    async get(id) {
      if (holds.length) await holds.shift();
      return map.has(id) ? map.get(id) : null;
    },
    async cas(id, want, next) {
      if (fail(id, next === null ? 'delete' : 'put')) return 'full';
      const cur = map.has(id) ? map.get(id) : null;
      if (cur !== want) return 'conflict';
      if (next === null) map.delete(id);
      else map.set(id, next);
      return 'ok';
    },
  };
}

// memBus() returns a factory; each bus it makes hears the others' messages
export function memBus() {
  const all = [];
  return () => {
    const mine = [];
    all.push(mine);
    return {
      post(id) { for (const fns of all) if (fns !== mine) for (const fn of fns) setTimeout(() => fn(id), 0); },
      on(fn) { mine.push(fn); },
    };
  };
}

export function fakeLs(init = {}) {
  const map = new Map(Object.entries(init));
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, v); },
    removeItem: (k) => { map.delete(k); },
  };
}

export const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));
