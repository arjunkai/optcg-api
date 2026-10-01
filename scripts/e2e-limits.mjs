#!/usr/bin/env node
// scripts/e2e-limits.mjs: end-to-end checks against a LOCAL `wrangler dev`.
// Uses --local only and refuses non-localhost targets.
//
//   Terminal A (first time only): npx wrangler d1 execute optcg-cards --local --file=schema.sql
//   Terminal A:                   npx wrangler dev
//   Terminal B:                   npm run e2e:limits

import { spawnSync } from 'node:child_process';
import { writeFileSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { webcrypto } from 'node:crypto';

const BASE = process.env.E2E_BASE || 'http://localhost:8787';
if (!/^http:\/\/(localhost|127\.0\.0\.1):\d+$/.test(BASE)) {
  console.error(`Refusing to run against ${BASE}: localhost only.`);
  process.exit(1);
}
const NPX = process.platform === 'win32' ? 'npx.cmd' : 'npx';
function localSql(sql) {
  const f = join(tmpdir(), `e2e-${process.pid}-${Date.now()}.sql`);
  writeFileSync(f, sql, 'utf8');
  try {
    const r = spawnSync(NPX, ['wrangler', 'd1', 'execute', 'optcg-cards', '--local', `--file=${f}`], {
      encoding: 'utf8', shell: process.platform === 'win32',
    });
    if (r.status !== 0) throw new Error(r.stderr || r.stdout);
  } finally { unlinkSync(f); }
}
const hash = async (raw) => Buffer.from(await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(raw))).toString('hex');

const run = Date.now().toString(36);
const KEYS = {
  free: [`opt_e2efre${run}`.padEnd(28, '0'), 'free', 'optcg'],
  standard: [`opt_e2estd${run}`.padEnd(28, '0'), 'standard', 'optcg'],
  bot: [`opt_e2ebot${run}`.padEnd(28, '0'), 'standard', 'optcg,ptcg,firstparty'],
};
const day = new Date().toISOString().slice(0, 10);
let failures = 0;
const check = (name, ok, info = '') => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  ' + info}`); if (!ok) failures++; };
async function get(path, headers = {}) {
  const res = await fetch(BASE + path, { headers });
  let body = null; try { body = await res.json(); } catch { /* not JSON */ }
  return { status: res.status, body, headers: res.headers };
}
const as = (k) => ({ 'x-api-key': KEYS[k][0] });
const pre = (k) => KEYS[k][0].slice(0, 12);

const rows = [];
for (const [name, [raw, tier, scopes]] of Object.entries(KEYS)) {
  rows.push(`INSERT INTO api_keys (key_hash, key_prefix, owner_name, tier, status, created_at, scopes) VALUES ('${await hash(raw)}', '${raw.slice(0, 12)}', 'e2e ${name}', '${tier}', 'active', ${Date.now()}, '${scopes}');`);
}
rows.push(`INSERT INTO api_key_usage (api_key, day, count, updated_at) VALUES ('${pre('free')}', '${day}', 498, 0);`);
rows.push(`INSERT INTO api_key_usage (api_key, day, count, updated_at) VALUES ('u:${pre('standard')}', '${day}', 190000, 0);`);
localSql(rows.join('\n'));

check('no key -> 401', (await get('/sets')).status === 401);
check('unknown key -> 401', (await get('/sets', { 'x-api-key': 'opt_nope' })).status === 401);
const s = await get('/sets', as('standard'));
check('standard -> 200, Limit-Day 2000', s.status === 200 && s.headers.get('x-ratelimit-limit-day') === '2000', `${s.status} ${s.headers.get('x-ratelimit-limit-day')}`);
check('free 499th -> 200', (await get('/sets', as('free'))).status === 200);
check('free 500th -> 200', (await get('/sets', as('free'))).status === 200);
const f3 = await get('/sets', as('free'));
check('free 501st -> 429 daily_quota_exceeded', f3.status === 429 && f3.body?.error === 'daily_quota_exceeded', JSON.stringify(f3.body));
const h = await get(`/cards?name=e2e${run}`, as('standard'));
check('standard heavy search past its units -> 429 daily_quota_exceeded', h.status === 429 && h.body?.error === 'daily_quota_exceeded', JSON.stringify(h.body));
const b = await get(`/cards?name=e2ebot${run}`, as('bot'));
check('firstparty heavy search -> 200, no day header', b.status === 200 && b.headers.get('x-ratelimit-limit-day') === null, `${b.status}`);
check('browser origin -> 200', (await get('/sets', { origin: 'https://opbindr.com' })).status === 200);
check('public /healthz -> 200', (await get('/healthz')).status === 200);

localSql(`DELETE FROM api_keys WHERE owner_name LIKE 'e2e %'; DELETE FROM api_key_usage WHERE api_key LIKE 'opt_e2e%' OR api_key LIKE 'u:opt_e2e%' OR api_key = 'u:outside';`);
console.log(failures ? `\n${failures} check(s) FAILED` : '\nAll checks passed');
process.exit(failures ? 1 : 0);
