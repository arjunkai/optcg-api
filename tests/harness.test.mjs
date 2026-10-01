import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createD1, seedKey, setCount, getCount } from './helpers/d1.mjs';
import { fakeLimiter, fakeCtx } from './helpers/fakes.mjs';

test('atomic upsert with RETURNING sums', async () => {
  const d1 = createD1();
  const sql = 'INSERT INTO api_key_usage (api_key, day, count, updated_at) VALUES (?, ?, ?, ?) ' +
    'ON CONFLICT(api_key, day) DO UPDATE SET count = api_key_usage.count + excluded.count RETURNING count';
  assert.equal((await d1.prepare(sql).bind('u:a', '2026-10-01', 500, 1).first()).count, 500);
  assert.equal((await d1.prepare(sql).bind('u:a', '2026-10-01', 1000, 2).first()).count, 1500);
});

test('seedKey, setCount, getCount', async () => {
  const d1 = createD1();
  const { hash, prefix } = await seedKey(d1, { raw: 'opt_testkey000000000000', tier: 'free' });
  const row = await d1.prepare('SELECT key_prefix, tier FROM api_keys WHERE key_hash = ?').bind(hash).first();
  assert.deepEqual([row.key_prefix, row.tier], [prefix, 'free']);
  setCount(d1, 'x', '2026-10-01', 7);
  setCount(d1, 'x', '2026-10-01', 9);
  assert.equal(getCount(d1, 'x', '2026-10-01'), 9);
  assert.equal(getCount(d1, 'nope', '2026-10-01'), 0);
});

test('failWhen and fakes', async () => {
  const d1 = createD1();
  d1.failWhen = () => true;
  await assert.rejects(() => d1.prepare('SELECT 1').first(), /injected/);
  const l = fakeLimiter(1);
  assert.equal((await l.limit({ key: 'a' })).success, true);
  assert.equal((await l.limit({ key: 'a' })).success, false);
  const ctx = fakeCtx();
  let done = false;
  ctx.waitUntil(new Promise((r) => setTimeout(() => { done = true; r(); }, 5)));
  await ctx.drain();
  assert.equal(done, true);
});
