#!/usr/bin/env node
// scripts/list-keys.mjs
//
// List API keys with status, owner, last-used, and today's request count.
// Default: active keys only. Pass --all to include revoked rows.
//
// Usage:
//   node scripts/list-keys.mjs
//   node scripts/list-keys.mjs --all

import { parseArgs } from 'node:util';
import { d1Query } from './_d1.mjs';

const { values } = parseArgs({
  options: { all: { type: 'boolean', default: false } },
});

const today = new Date().toISOString().slice(0, 10);
const where = values.all ? '1=1' : "k.status = 'active'";

const sql = `SELECT k.key_prefix, k.owner_name, k.owner_contact, k.tier, k.status, k.created_at, k.last_used_at, k.revoked_at, COALESCE(u.count, 0) AS today_count FROM api_keys k LEFT JOIN api_key_usage u ON u.api_key = k.key_prefix AND u.day = '${today}' WHERE ${where} ORDER BY k.created_at DESC;`;

let rows;
try {
  rows = d1Query(sql);
} catch (err) {
  console.error('D1 query failed:', err.message);
  process.exit(1);
}

if (rows.length === 0) {
  console.log(values.all ? 'No keys in the database.' : 'No active keys.');
  process.exit(0);
}

const fmtTs = (ts) => ts ? new Date(ts).toISOString().replace('T', ' ').slice(0, 16) : '-';
const pad = (s, n) => String(s ?? '').slice(0, n).padEnd(n);

console.log('');
console.log(`${pad('Prefix', 13)} | ${pad('Status', 8)} | ${pad('Tier', 9)} | ${pad('Owner', 22)} | ${pad('Created', 16)} | ${pad('Last used', 16)} | Today`);
console.log('-'.repeat(13) + '-+-' + '-'.repeat(8) + '-+-' + '-'.repeat(9) + '-+-' + '-'.repeat(22) + '-+-' + '-'.repeat(16) + '-+-' + '-'.repeat(16) + '-+--------');
for (const r of rows) {
  console.log(
    `${pad(r.key_prefix, 13)} | ${pad(r.status, 8)} | ${pad(r.tier, 9)} | ${pad(r.owner_name, 22)} | ${pad(fmtTs(r.created_at), 16)} | ${pad(fmtTs(r.last_used_at), 16)} | ${String(r.today_count || 0).padStart(6)}`
  );
}
console.log('');
