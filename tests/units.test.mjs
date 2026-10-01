import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { Hono } from 'hono';
import { edgeCache } from '../src/edgeCache.js';
import { tierFor } from '../src/limits.js';
import { createUnitMeter } from '../src/usage.js';
import { createD1, setCount, getCount } from './helpers/d1.mjs';
import { fakeLimiter, fakeCtx, installCaches, makeClock } from './helpers/fakes.mjs';

const DAY = '2026-10-10';
const P = 'opt_aaaaaaaa';
let d1, clock;
beforeEach(() => { installCaches(); d1 = createD1(); clock = makeClock(`${DAY}T12:00:00Z`); });

function appAs({ caller = 'key', tierName = 'standard', exempt = false, meter } = {}) {
  const app = new Hono();
  app.use('*', async (c, next) => {
    c.set('caller', caller);
    if (caller === 'key') { c.set('tier', tierFor(tierName)); c.set('keyPrefix', P); c.set('exempt', exempt); }
    await next();
  });
  app.use('*', edgeCache({ meter: meter || createUnitMeter({ now: clock.now }) }));
  app.get('/cards', (c) => c.json({ q: c.req.query('name') || null }));
  app.get('/cards/:id', (c) => c.json({ id: c.req.param('id') }));
  return app;
}
async function call(app, path, env, ip = '203.0.113.7') {
  const ctx = fakeCtx();
  const res = await app.request(path, { headers: { 'cf-connecting-ip': ip } }, env, ctx);
  await ctx.drain();
  return res;
}
const inserts = () => d1.queries.filter((q) => q.startsWith('INSERT')).length;

test('keys: costly misses charged exactly; light misses and hits free', async () => {
  const app = appAs();
  const env = { DB: d1, RL_KEY_HEAVY: fakeLimiter(10) };
  await call(app, '/cards/OP01-001', env);       // light: 0
  await call(app, '/cards?set_id=OP-01', env);   // 1,000
  await call(app, '/cards?set_id=OP-01', env);   // hit: 0
  await call(app, '/cards?name=zoro', env);      // 15,000
  assert.equal(getCount(d1, 'u:' + P, DAY), 16_000);
  assert.equal(getCount(d1, 'u:outside', DAY), 16_000);
});

test('keys: own cap refuses before the route runs; outside not charged', async () => {
  setCount(d1, 'u:' + P, DAY, 200_000 - 10_000);
  const res = await call(appAs(), '/cards?name=luffy', { DB: d1, RL_KEY_HEAVY: fakeLimiter(10) });
  assert.equal(res.status, 429);
  const b = await res.json();
  assert.deepEqual([b.error, b.tier, b.limit], ['daily_quota_exceeded', 'standard', 200000]);
  assert.equal(getCount(d1, 'u:outside', DAY), 0);
});

test('keys: outside share refuses all keys', async () => {
  setCount(d1, 'u:outside', DAY, 1_800_000 - 500);
  const res = await call(appAs(), '/cards?set_id=OP-02', { DB: d1 });
  assert.equal(res.status, 429);
  assert.equal((await res.json()).error, 'outside_daily_capacity');
});

test('keys: once the outside share is exhausted, no more writes', async () => {
  setCount(d1, 'u:outside', DAY, 1_800_000);
  const meter = createUnitMeter({ now: clock.now });
  const app = appAs({ meter });
  assert.equal((await call(app, '/cards?set_id=OP-04', { DB: d1 })).status, 429);
  const before = inserts();
  for (let i = 0; i < 5; i++) assert.equal((await call(app, `/cards?set_id=OP-0${i + 5}`, { DB: d1 })).status, 429);
  assert.equal(inserts(), before);
});

test('keys: heavy binding per tier', async () => {
  const app = appAs({ tierName: 'free' });
  const env = { DB: d1, RL_KEY_HEAVY_FREE: fakeLimiter(2) };
  await call(app, '/cards?name=a', env);
  await call(app, '/cards?name=b', env);
  const res = await call(app, '/cards?name=c', env);
  assert.equal(res.status, 429);
  const b = await res.json();
  assert.deepEqual([b.error, b.limit], ['heavy_rate_limited', 2]);
});

test('keys: exempt keys are never charged', async () => {
  await call(appAs({ exempt: true }), '/cards?name=x', { DB: d1, RL_KEY_HEAVY: fakeLimiter(10) });
  assert.equal(getCount(d1, 'u:' + P, DAY), 0);
});

test('keys: D1 failure on charge is a 503', async () => {
  d1.failWhen = (sql) => sql.startsWith('INSERT');
  const res = await call(appAs(), '/cards?set_id=OP-03', { DB: d1 });
  assert.equal(res.status, 503);
  assert.equal((await res.json()).error, 'temporarily_unavailable');
});

test('browser: never charged, today\'s per-IP limits unchanged', async () => {
  const app = appAs({ caller: 'browser' });
  assert.equal((await call(app, '/cards?name=b1', { DB: d1, RL_IP: fakeLimiter(1500), RL_IP_HEAVY: fakeLimiter(60) })).status, 200);
  assert.equal(d1.db.prepare('SELECT COUNT(*) AS n FROM api_key_usage').get().n, 0);
  const res = await call(app, '/cards?name=b2', { DB: d1, RL_IP: fakeLimiter(1500), RL_IP_HEAVY: fakeLimiter(0) });
  assert.equal(res.status, 429);
  assert.equal((await res.json()).error, 'rate_limited');
});
