// D1-shaped wrapper over node:sqlite so tests run the real migrations and SQL
// (including ON CONFLICT ... RETURNING).
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';

const MIGRATIONS = ['012_api_key_usage.sql', '013_api_keys_table.sql', '014_api_keys_scopes.sql'];

export function createD1() {
  const db = new DatabaseSync(':memory:');
  for (const m of MIGRATIONS) db.exec(readFileSync(new URL(`../../migrations/${m}`, import.meta.url), 'utf8'));
  const d1 = {
    db,
    queries: [],
    failWhen: null,
    prepare(sql) {
      let params = [];
      const exec = (fn) => {
        d1.queries.push(sql);
        if (d1.failWhen && d1.failWhen(sql)) throw new Error('D1_ERROR: injected');
        return fn(db.prepare(sql));
      };
      const stmt = {
        bind(...p) { params = p; return stmt; },
        async first() { return exec((s) => s.get(...params) ?? null); },
        async all() { return { results: exec((s) => s.all(...params)) }; },
        async run() { return exec((s) => { s.run(...params); return { success: true }; }); },
      };
      return stmt;
    },
  };
  return d1;
}

export async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function seedKey(d1, { raw, tier = 'standard', scopes = 'optcg', status = 'active' }) {
  const hash = await sha256Hex(raw);
  const prefix = raw.slice(0, 12);
  d1.db.prepare(
    'INSERT INTO api_keys (key_hash, key_prefix, owner_name, tier, status, created_at, scopes) VALUES (?, ?, ?, ?, ?, ?, ?)'
  ).run(hash, prefix, `owner of ${prefix}`, tier, status, Date.now(), scopes);
  return { hash, prefix };
}

export function setCount(d1, name, day, count) {
  d1.db.prepare(
    'INSERT INTO api_key_usage (api_key, day, count, updated_at) VALUES (?, ?, ?, 0) ' +
    'ON CONFLICT(api_key, day) DO UPDATE SET count = excluded.count'
  ).run(name, day, count);
}

export function getCount(d1, name, day) {
  return d1.db.prepare('SELECT count FROM api_key_usage WHERE api_key = ? AND day = ?').get(name, day)?.count ?? 0;
}
