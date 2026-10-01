import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { Hono } from 'hono';
import { gate } from '../src/auth.js';
import { createKeyCache } from '../src/keyCache.js';
import { createRequestCounter } from '../src/usage.js';
import { createD1, seedKey, setCount, getCount } from './helpers/d1.mjs';
import { fakeLimiter, fakeCtx, installCaches, makeClock } from './helpers/fakes.mjs';

const DAY = '2026-10-10';
const KEYS = {
  free: 'opt_freekey000000000000', standard: 'opt_stdkey0000000000000',
  partner: 'opt_partkey000000000000', admin: 'opt_admkey0000000000000',
  bot: 'opt_botkey0000000000000', ptcgOnly: 'opt_ptcgkey000000000000',
};
let d1, env, clock, app;

beforeEach(async () => {
  installCaches();
  d1 = createD1();
  await seedKey(d1, { raw: KEYS.free, tier: 'free' });
  await seedKey(d1, { raw: KEYS.standard, tier: 'standard' });
  await seedKey(d1, { raw: KEYS.partner, tier: 'partner' });
  await seedKey(d1, { raw: KEYS.admin, scopes: 'optcg,ptcg,admin' });
  await seedKey(d1, { raw: KEYS.bot, scopes: 'optcg,ptcg,firstparty' });
  await seedKey(d1, { raw: KEYS.ptcgOnly, scopes: 'ptcg' });
  d1.queries.length = 0;
  env = {
    DB: d1, RL_KEY_FREE: fakeLimiter(20), RL_MINUTE: fakeLimiter(60),
    RL_KEY_PARTNER: fakeLimiter(120), RL_KEY_LOOKUP: fakeLimiter(30),
  };
  clock = makeClock(`${DAY}T12:00:00Z`);
  app = new Hono();
  app.use('*', gate({ keyCache: createKeyCache({ now: clock.now }), requests: createRequestCounter({ now: clock.now }), now: clock.now }));
  app.get('/cards', (c) => c.json({
    tier: c.get('tier')?.name ?? null, prefix: c.get('keyPrefix') ?? null,
    admin: c.get('admin') ?? null, exempt: c.get('exempt') ?? null,
  }));
  app.get('/images/:id', (c) => c.text('img'));
});

async function call(path, headers = {}) {
  const ctx = fakeCtx();
  const res = await app.request(path, { headers }, env, ctx);
  await ctx.drain();
  return res;
}
const key = (k, ip = '203.0.113.9') => ({ 'x-api-key': KEYS[k], 'cf-connecting-ip': ip });
const prefix = (k) => KEYS[k].slice(0, 12);

test('valid key: context and day headers', async () => {
  const res = await call('/cards', key('standard'));
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { tier: 'standard', prefix: prefix('standard'), admin: false, exempt: false });
  assert.equal(res.headers.get('X-RateLimit-Limit-Day'), '2000');
  assert.equal(res.headers.get('X-RateLimit-Remaining-Day'), '1999');
});

test('missing, empty, whitespace key: 401, no D1, no limiter', async () => {
  for (const v of [undefined, '', '   ']) {
    assert.equal((await call('/cards', v === undefined ? {} : { 'x-api-key': v })).status, 401, JSON.stringify(v));
  }
  assert.equal(d1.queries.length, 0);
  assert.equal(env.RL_KEY_LOOKUP.calls, 0);
});

test('known keys never consume the lookup limiter', async () => {
  for (let i = 0; i < 40; i++) assert.equal((await call('/cards', key('standard'))).status, 200);
  assert.equal(env.RL_KEY_LOOKUP.calls, 0);
});

test('unknown keys: limiter on each miss, then the IP is blocked without D1', async () => {
  for (let i = 0; i < 30; i++) {
    assert.equal((await call('/cards', { 'x-api-key': `opt_spray${i}`, 'cf-connecting-ip': '198.51.100.1' })).status, 401);
  }
  const r31 = await call('/cards', { 'x-api-key': 'opt_spray30', 'cf-connecting-ip': '198.51.100.1' });
  assert.equal(r31.status, 429);
  assert.equal((await r31.json()).error, 'too_many_key_attempts');
  const before = d1.queries.length;
  const r32 = await call('/cards', { 'x-api-key': 'opt_spray31', 'cf-connecting-ip': '198.51.100.1' });
  assert.equal(r32.status, 429);
  assert.equal(d1.queries.length, before, 'blocked IP costs no D1');
  clock.advance(60_000);
  assert.equal((await call('/cards', { 'x-api-key': 'opt_spray32', 'cf-connecting-ip': '198.51.100.1' })).status, 429, 'limiter still over');
});

test('a repeated unknown key is cached: one D1 read', async () => {
  const h = { 'x-api-key': 'opt_doesnotexist0000000', 'cf-connecting-ip': '203.0.113.9' };
  await call('/cards', h); await call('/cards', h); await call('/cards', h);
  assert.equal(d1.queries.filter((q) => q.includes('FROM api_keys')).length, 1);
});

test('legacy env-var keys are refused', async () => {
  env.API_KEYS = 'opt_legacykey00000000000';
  assert.equal((await call('/cards', { 'x-api-key': 'opt_legacykey00000000000', 'cf-connecting-ip': '203.0.113.5' })).status, 401);
});

test('scope check', async () => {
  const res = await call('/cards', key('ptcgOnly'));
  assert.equal(res.status, 403);
  assert.equal((await res.json()).error, 'scope_required');
});

test('per-minute limit uses the tier binding', async () => {
  for (let i = 0; i < 20; i++) assert.equal((await call('/cards', key('free'))).status, 200);
  const res = await call('/cards', key('free'));
  assert.equal(res.status, 429);
  const b = await res.json();
  assert.deepEqual([b.error, b.tier, b.limit], ['rate_limited', 'free', 20]);
  assert.equal(res.headers.get('Retry-After'), '60');
  assert.equal(env.RL_MINUTE.calls, 0);
});

test('daily cap per tier, Retry-After to midnight', async () => {
  setCount(d1, prefix('free'), DAY, 499);
  assert.equal((await call('/cards', key('free'))).status, 200);
  const res = await call('/cards', key('free'));
  assert.equal(res.status, 429);
  const b = await res.json();
  assert.deepEqual([b.error, b.tier, b.limit], ['daily_quota_exceeded', 'free', 500]);
  assert.equal(res.headers.get('Retry-After'), '43200');
});

test('a throwing per-minute binding still enforces the daily cap', async () => {
  env.RL_KEY_FREE.throws = true;
  setCount(d1, prefix('free'), DAY, 500);
  assert.equal((await (await call('/cards', key('free'))).json()).error, 'daily_quota_exceeded');
});

test('firstparty and admin: exempt, not counted, no day headers', async () => {
  setCount(d1, prefix('bot'), DAY, 10_000_000);
  for (const k of ['bot', 'admin']) {
    const res = await call('/cards', key(k));
    assert.equal(res.status, 200, k);
    const b = await res.json();
    assert.equal(b.exempt, true);
    assert.equal(b.admin, k === 'admin');
    assert.equal(res.headers.get('X-RateLimit-Limit-Day'), null);
  }
  for (let i = 0; i < 30; i++) await call('/cards', key('admin'));
  assert.equal(getCount(d1, prefix('admin'), DAY), 0);
});

test('requests are counted and flushed in batches of 25', async () => {
  for (let i = 0; i < 25; i++) await call('/cards', key('standard'));
  assert.equal(getCount(d1, prefix('standard'), DAY), 25);
});

test('browser and public paths ignore key limits; bad origin 403', async () => {
  assert.equal((await call('/cards', { origin: 'https://opbindr.com' })).status, 200);
  assert.equal((await call('/images/OP01-001')).status, 200);
  assert.equal((await call('/cards', { origin: 'https://evil.example' })).status, 403);
});

test('D1 failure on lookup: 503 for keys, browsers fine', async () => {
  d1.failWhen = (sql) => sql.includes('FROM api_keys');
  const res = await call('/cards', key('standard'));
  assert.equal(res.status, 503);
  assert.equal((await res.json()).error, 'temporarily_unavailable');
  assert.equal((await call('/cards', { origin: 'https://opbindr.com' })).status, 200);
});

test('no D1 binding: keys 401, browsers fine', async () => {
  env.DB = undefined;
  assert.equal((await call('/cards', key('standard'))).status, 401);
  assert.equal((await call('/cards', { origin: 'https://opbindr.com' })).status, 200);
});

test('last_used_at at most once per 6 hours', async () => {
  for (let i = 0; i < 5; i++) await call('/cards', key('standard'));
  assert.equal(d1.queries.filter((q) => q.startsWith('UPDATE api_keys SET last_used_at')).length, 1);
});
