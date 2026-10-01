import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkUsageAlerts } from '../src/cron.js';
import { createD1, seedKey, setCount } from './helpers/d1.mjs';

const DAY = '2026-10-10';
async function run(d1) {
  const posts = [];
  const marks = new Set();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (_u, init) => { posts.push(JSON.parse(init.body).content); return new Response('ok'); };
  const IMAGES = { head: async (k) => (marks.has(k) ? {} : null), put: async (k) => { marks.add(k); } };
  try {
    await checkUsageAlerts({ DB: d1, DISCORD_USAGE_WEBHOOK_URL: 'https://discord.test/x', IMAGES }, { today: DAY });
    await checkUsageAlerts({ DB: d1, DISCORD_USAGE_WEBHOOK_URL: 'https://discord.test/x', IMAGES }, { today: DAY });
  } finally { globalThis.fetch = realFetch; }
  return posts;
}

test('per-key alerts at 80% of the key\'s tier requests and units; deduped', async () => {
  const d1 = createD1();
  const f = await seedKey(d1, { raw: 'opt_freealert00000000000', tier: 'free' });
  const s = await seedKey(d1, { raw: 'opt_stdalert000000000000', tier: 'standard' });
  setCount(d1, f.prefix, DAY, 450);            // 90% of 500
  setCount(d1, s.prefix, DAY, 450);            // 22% of 2,000
  setCount(d1, 'u:' + s.prefix, DAY, 170_000); // 85% of 200k units
  const posts = await run(d1);
  assert.equal(posts.length, 2, posts.join('\n---\n'));
  assert.ok(posts.some((p) => p.includes(f.prefix) && /requests/.test(p)));
  assert.ok(posts.some((p) => p.includes(s.prefix) && /database/.test(p)));
});

test('outside share alert at 80%', async () => {
  const d1 = createD1();
  setCount(d1, 'u:outside', DAY, 1_500_000);
  const posts = await run(d1);
  assert.equal(posts.length, 1);
  assert.match(posts[0], /all API keys/i);
});
