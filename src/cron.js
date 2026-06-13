// Daily-quota usage alerts.
//
// Runs on the wrangler cron schedule (see wrangler.toml [triggers]).
// Queries D1 for any active key whose daily request count has crossed
// the alert threshold (80% of the 100k daily limit) and posts a one-line
// Discord message via webhook.
//
// Dedup: Cache API key `https://rl.local/alert/{prefix}/{day}` is set on
// every alert with a 24h TTL, so each (key, day) pair only triggers one
// notification regardless of how many times the cron runs that day.
//
// Setup:
//   1. Create a Discord webhook in the target channel.
//      Server Settings -> Integrations -> Webhooks -> New Webhook.
//   2. Copy the webhook URL.
//   3. npx wrangler secret put DISCORD_USAGE_WEBHOOK_URL
//      (paste the URL when prompted)
// If the secret isn't set the cron is a no-op — safe to deploy first.

import { warmCardImage } from './images.js';

const DAILY_LIMIT = 100_000;
const ALERT_THRESHOLD_PCT = 0.8;

// Self-healing card-image warm sweep.
//
// optcg-api `/images/:id` serves from R2 first, but a card NOT yet in R2 falls
// back to a live fetch (Bandai -> wsrv) that intermittently 404s because Bandai
// hot-link-blocks the CF Worker IPs. That made some cards show a tinted
// placeholder, with the failing set shifting on every reload. Each successful
// fetch persists to R2, after which that card is served reliably forever.
//
// This cron proactively pulls cold cards into R2 so NO card depends on a live
// fetch — including newly-released sets, which is what makes it permanent
// rather than a one-off. It sweeps the catalog in bounded batches via a Cache
// API cursor and caps live fetches per run to stay well under the Worker
// subrequest limit. Purely additive: only writes to the R2 image cache, never
// touches card rows or counts.
const WARM_SCAN = 300;       // catalog rows R2-head-checked per run (cheap binding ops)
const WARM_FETCH_CAP = 24;   // max live wsrv fetches per run (subrequest budget)
const WARM_CONCURRENCY = 6;  // concurrent warms (gentle on wsrv; bounded wall-clock)

export async function warmColdImages(env) {
  if (!env?.DB || !env?.IMAGES) return;

  const cache = caches.default;
  const cursorKey = new Request('https://warm.local/img-cursor');
  let offset = 0;
  try {
    const cur = await cache.match(cursorKey);
    if (cur) offset = parseInt(await cur.text(), 10) || 0;
  } catch { offset = 0; }

  let rows = [];
  try {
    const res = await env.DB.prepare(
      "SELECT id FROM cards WHERE id NOT LIKE 'DON-%' ORDER BY id LIMIT ? OFFSET ?"
    ).bind(WARM_SCAN, offset).all();
    rows = res.results || [];
  } catch (err) {
    console.error('warm: D1 query failed:', err?.message || err);
    return;
  }

  // Wrap the cursor at the end of the catalog so the sweep keeps cycling.
  const nextOffset = rows.length < WARM_SCAN ? 0 : offset + WARM_SCAN;

  // Phase 1: find cold cards (R2 head is a binding op, not a subrequest), capped.
  const cold = [];
  for (const { id } of rows) {
    if (cold.length >= WARM_FETCH_CAP) break;
    try {
      if (!(await env.IMAGES.head(`cards/${id}.png`))) cold.push(id);
    } catch { /* head failure -> treat as not-cold; next sweep retries */ }
  }

  // Phase 2: warm cold cards with bounded concurrency.
  let warmed = 0;
  for (let i = 0; i < cold.length; i += WARM_CONCURRENCY) {
    const batch = cold.slice(i, i + WARM_CONCURRENCY);
    const results = await Promise.all(batch.map((id) => warmCardImage(env, id)));
    warmed += results.filter((s) => s === 'warmed').length;
  }

  try {
    await cache.put(cursorKey, new Response(String(nextOffset), {
      headers: { 'Cache-Control': 'max-age=2592000' },
    }));
  } catch { /* cursor advance is best-effort; worst case we re-scan the window */ }

  console.log(`warm: offset=${offset} scanned=${rows.length} cold=${cold.length} warmed=${warmed} next=${nextOffset}`);
}

export async function checkUsageAlerts(env) {
  if (!env?.DB || !env?.DISCORD_USAGE_WEBHOOK_URL) return;

  const today = new Date().toISOString().slice(0, 10);
  const threshold = Math.floor(DAILY_LIMIT * ALERT_THRESHOLD_PCT);

  const { results = [] } = await env.DB.prepare(
    `SELECT k.key_prefix, k.owner_name, u.count
     FROM api_keys k
     JOIN api_key_usage u ON u.api_key = k.key_prefix
     WHERE k.status = 'active' AND u.day = ? AND u.count >= ?`
  ).bind(today, threshold).all();

  if (results.length === 0) return;

  const cache = caches.default;

  for (const row of results) {
    const dedupKey = new Request(`https://rl.local/alert/${encodeURIComponent(row.key_prefix)}/${today}`);
    if (await cache.match(dedupKey)) continue;

    const pct = ((row.count / DAILY_LIMIT) * 100).toFixed(1);
    const overLimit = row.count >= DAILY_LIMIT;
    const headline = overLimit
      ? `**[OPTCG API] DAILY LIMIT EXHAUSTED**`
      : `**[OPTCG API] Usage alert**`;

    const content =
      `${headline}\n` +
      `Key \`${row.key_prefix}\` (${row.owner_name}) is at ` +
      `${row.count.toLocaleString()}/${DAILY_LIMIT.toLocaleString()} ` +
      `requests today (${pct}%).\n` +
      (overLimit
        ? `Requests are now returning 429 until UTC midnight.`
        : `Run \`npm run key:list\` to inspect or \`npm run key:revoke -- ${row.key_prefix}\` to cut access.`);

    try {
      await fetch(env.DISCORD_USAGE_WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      await cache.put(dedupKey, new Response('1', {
        headers: { 'Cache-Control': 'max-age=86400' },
      }));
    } catch (err) {
      // Swallow — next cron tick will retry. Logging only.
      console.error('usage-alert webhook failed:', err?.message || err);
    }
  }
}
