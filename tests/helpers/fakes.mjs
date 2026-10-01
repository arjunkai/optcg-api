export function fakeLimiter(limit) {
  const counts = new Map();
  return {
    calls: 0,
    throws: false,
    async limit({ key }) {
      this.calls++;
      if (this.throws) throw new Error('binding down');
      const n = (counts.get(key) || 0) + 1;
      counts.set(key, n);
      return { success: n <= limit };
    },
    reset() { counts.clear(); },
  };
}

export function fakeCtx() {
  const waits = [];
  return {
    waits,
    waitUntil(p) { waits.push(Promise.resolve(p).catch(() => {})); },
    passThroughOnException() {},
    async drain() { while (waits.length) await waits.shift(); },
  };
}

// Fresh caches.default backed by a Map. Call in each test's setup.
export function installCaches() {
  const store = new Map();
  const k = (r) => (typeof r === 'string' ? r : r.url);
  globalThis.caches = {
    default: {
      match: async (r) => store.get(k(r))?.clone(),
      put: async (r, res) => { store.set(k(r), res); },
      delete: async (r) => store.delete(k(r)),
    },
  };
  return store;
}

export function makeClock(start) {
  let t = new Date(start).getTime();
  return { now: () => t, advance(ms) { t += ms; }, set(v) { t = new Date(v).getTime(); } };
}
