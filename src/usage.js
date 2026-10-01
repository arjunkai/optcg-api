// Daily usage counters in api_key_usage (api_key = counter name).
//
// createRequestCounter: a key's daily request count ('opt_xxxxxxxx'). Cheap
// resource, so batched: each isolate flushes count = count + delta every
// flushEvery requests, or on the next request once flushAfterMs has passed
// since its last flush. It re-reads the D1 total every refreshAfterMs. Writes
// are at most min(requests, keys x isolates x day/flushAfterMs).
//
// createUnitMeter: D1 cost units for costly cache misses ('u:<prefix>',
// 'u:outside'). Exact: one atomic INSERT ... RETURNING per charge, so the
// cap can't be overshot by batching. Once a counter is over its cap the
// isolate remembers until UTC midnight and stops writing it.

const FLUSH_SQL =
  'INSERT INTO api_key_usage (api_key, day, count, updated_at) VALUES (?, ?, ?, ?) ' +
  'ON CONFLICT(api_key, day) DO UPDATE SET count = api_key_usage.count + excluded.count, updated_at = excluded.updated_at';
const CHARGE_SQL = FLUSH_SQL + ' RETURNING count';
const READ_SQL = 'SELECT count FROM api_key_usage WHERE api_key = ? AND day = ?';

export function utcDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

export function createRequestCounter({
  now = Date.now,
  flushEvery = 25,
  flushAfterMs = 300_000,
  refreshAfterMs = 300_000,
} = {}) {
  const counters = new Map(); // name -> { day, pending, sinceRead, known, readAt, lastFlushAt }

  function flush(db, name, s, waitUntil, t) {
    if (!db || s.pending <= 0) return;
    const delta = s.pending;
    const day = s.day;
    s.pending = 0;
    s.lastFlushAt = t;
    waitUntil(
      db.prepare(FLUSH_SQL).bind(name, day, delta, t).run()
        .catch(() => { if (s.day === day) s.pending += delta; })
    );
  }

  function state(db, name, waitUntil, t) {
    const day = utcDay(t);
    let s = counters.get(name);
    if (s && s.day !== day) {
      flush(db, name, s, waitUntil, t); // the old day's tail goes to the old day
      s = undefined;
    }
    if (!s) {
      s = { day, pending: 0, sinceRead: 0, known: 0, readAt: -Infinity, lastFlushAt: t };
      counters.set(name, s);
    }
    return s;
  }

  async function refresh(db, name, s, t) {
    if (!db || t - s.readAt < refreshAfterMs) return;
    s.readAt = t; // before the await: concurrent callers don't re-read
    try {
      const row = await db.prepare(READ_SQL).bind(name, s.day).first();
      s.known = row?.count || 0;
      s.sinceRead = s.pending; // unflushed requests aren't in D1 yet
    } catch { /* keep the last estimate */ }
  }

  return {
    async peek(db, name, waitUntil) {
      const t = now();
      const s = state(db, name, waitUntil, t);
      await refresh(db, name, s, t);
      return s.known + s.sinceRead;
    },
    async add(db, name, waitUntil) {
      const t = now();
      const s = state(db, name, waitUntil, t);
      await refresh(db, name, s, t);
      s.pending += 1;
      s.sinceRead += 1;
      if (s.pending >= flushEvery || t - s.lastFlushAt >= flushAfterMs) flush(db, name, s, waitUntil, t);
      return s.known + s.sinceRead;
    },
  };
}

export function createUnitMeter({ now = Date.now } = {}) {
  let memoDay = null;
  const exhausted = new Set(); // counter names over their cap on memoDay
  function roll(day) {
    if (memoDay !== day || exhausted.size > 10_000) {
      memoDay = day;
      exhausted.clear();
    }
  }
  return {
    isExhausted(name) {
      roll(utcDay(now()));
      return exhausted.has(name);
    },
    async charge(db, name, units, cap) {
      if (!db) return { ok: true, total: 0 };
      const t = now();
      const day = utcDay(t);
      roll(day);
      if (exhausted.has(name)) return { ok: false, total: cap };
      const row = await db.prepare(CHARGE_SQL).bind(name, day, units, t).first();
      const total = row?.count ?? units;
      if (total > cap) {
        exhausted.add(name);
        return { ok: false, total };
      }
      return { ok: true, total };
    },
  };
}
