// Per-isolate cache of API key lookups, found or not found, so a seen key
// costs no D1 read. Revocations and tier changes apply within ttlMs.
export function createKeyCache({ ttlMs = 300_000, max = 1_000, now = Date.now } = {}) {
  const entries = new Map(); // hash -> { row, expiresAt }; insertion-ordered
  return {
    get(hash) {
      const e = entries.get(hash);
      if (!e) return undefined;
      if (e.expiresAt <= now()) {
        entries.delete(hash);
        return undefined;
      }
      return { row: e.row };
    },
    set(hash, row) {
      entries.delete(hash);
      entries.set(hash, { row, expiresAt: now() + ttlMs });
      while (entries.size > max) entries.delete(entries.keys().next().value);
    },
    get size() { return entries.size; },
  };
}
