// Daily-quota usage alerts.
//
// Runs on the wrangler cron schedule (see wrangler.toml [triggers]).
// Posts a Discord message via webhook when an active key crosses 80% of its
// tier's daily requests or database units, or when all keys together cross
// 80% of the outside units share (src/limits.js).
//
// Dedup: an R2 marker `state/alerts/{day}/{id}` is written on every
// alert, so each (thing, day) pair only triggers one notification regardless
// of how many times the cron runs that day. (It used to live in the Cache
// API, which is per colo, and cron runs land in different colos.)
//
// Setup:
//   1. Create a Discord webhook in the target channel.
//      Server Settings -> Integrations -> Webhooks -> New Webhook.
//   2. Copy the webhook URL.
//   3. npx wrangler secret put DISCORD_USAGE_WEBHOOK_URL
//      (paste the URL when prompted)
// If the secret isn't set the cron is a no-op — safe to deploy first.

import { warmCardImage } from './images.js';
import { tierFor, OUTSIDE_UNITS_DAILY } from './limits.js';

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
// rather than a one-off. It sweeps the catalog in bounded batches (keyset
// paging on id, cursor kept in R2 at state/warm-cursor so it survives cron
// runs landing in different colos) and caps live fetches per run to stay
// well under the Worker subrequest limit. Purely additive: only writes to the R2 image cache, never
// touches card rows or counts.
const WARM_SCAN = 300;       // catalog rows R2-head-checked per run (cheap binding ops)
const WARM_FETCH_CAP = 20;   // cold cards per run; each takes 1-2 wsrv fetches (EN, then JA), so <= 40 of the 50-subrequest budget
const WARM_CONCURRENCY = 6;  // concurrent warms (gentle on wsrv; bounded wall-clock)

const CURSOR_KEY = 'state/warm-cursor';

export async function warmColdImages(env) {
  if (!env?.DB || !env?.IMAGES) return;

  // Last card id examined by the previous run ('' = start of catalog).
  let after = '';
  try {
    const cur = await env.IMAGES.get(CURSOR_KEY);
    if (cur) after = (await cur.text()).trim();
  } catch { after = ''; }

  let rows = [];
  try {
    // Keyset paging reads only the window (an OFFSET re-reads every row
    // before it, up to the whole catalog).
    const res = await env.DB.prepare(
      "SELECT id FROM cards WHERE id > ? AND id NOT LIKE 'DON-%' ORDER BY id LIMIT ?"
    ).bind(after, WARM_SCAN).all();
    rows = res.results || [];
  } catch (err) {
    console.error('warm: D1 query failed:', err?.message || err);
    return;
  }

  // Phase 1: find cold cards (R2 head is a binding op, not a subrequest). Stop
  // at the fetch cap and resume from the last id examined, so cards past the
  // cap aren't skipped until the next full sweep.
  const cold = [];
  let lastExamined = after;
  for (const { id } of rows) {
    if (cold.length >= WARM_FETCH_CAP) break;
    lastExamined = id;
    try {
      if (!(await env.IMAGES.head(`cards/${id}.png`))) cold.push(id);
    } catch { /* head failure -> treat as not-cold; next sweep retries */ }
  }
  // Wrap to the start once the end of the catalog has been examined.
  const reachedEnd = rows.length < WARM_SCAN && lastExamined === (rows.at(-1)?.id ?? after);
  const next = reachedEnd ? '' : lastExamined;

  // Phase 2: warm cold cards with bounded concurrency.
  let warmed = 0;
  for (let i = 0; i < cold.length; i += WARM_CONCURRENCY) {
    const batch = cold.slice(i, i + WARM_CONCURRENCY);
    const results = await Promise.all(batch.map((id) => warmCardImage(env, id)));
    warmed += results.filter((s) => s === 'warmed').length;
  }

  try {
    await env.IMAGES.put(CURSOR_KEY, next);
  } catch { /* cursor advance is best-effort; worst case we re-scan the window */ }

  console.log(`warm: after=${after || '-'} scanned=${rows.length} cold=${cold.length} warmed=${warmed} next=${next || '-'}`);
}

// Usage alerts (6-hourly cron). One Discord message per (day, thing):
//   a key at 80% of its tier's daily requests or database units,
//   all keys together at 80% of the outside units share.
export async function checkUsageAlerts(env, { today = new Date().toISOString().slice(0, 10) } = {}) {
  if (!env?.DB || !env?.DISCORD_USAGE_WEBHOOK_URL) return;
  const { results: usage = [] } = await env.DB.prepare(
    'SELECT api_key, count FROM api_key_usage WHERE day = ?'
  ).bind(today).all();
  const { results: keys = [] } = await env.DB.prepare(
    "SELECT key_prefix, owner_name, tier FROM api_keys WHERE status = 'active'"
  ).all();
  const counts = new Map(usage.map((r) => [r.api_key, r.count]));
  const fmt = (n) => n.toLocaleString('en-US');
  const alerts = [];

  for (const k of keys) {
    const t = tierFor(k.tier);
    const req = counts.get(k.key_prefix) || 0;
    const units = counts.get(`u:${k.key_prefix}`) || 0;
    if (req >= t.daily * ALERT_THRESHOLD_PCT) {
      alerts.push([`${k.key_prefix}-req`, `Key \`${k.key_prefix}\` (${k.owner_name}, ${t.name}) is at ${fmt(req)}/${fmt(t.daily)} requests today.`]);
    }
    if (units >= t.dailyUnits * ALERT_THRESHOLD_PCT) {
      alerts.push([`${k.key_prefix}-units`, `Key \`${k.key_prefix}\` (${k.owner_name}, ${t.name}) is at ${fmt(units)}/${fmt(t.dailyUnits)} database units today.`]);
    }
  }
  const outside = counts.get('u:outside') || 0;
  if (outside >= OUTSIDE_UNITS_DAILY * ALERT_THRESHOLD_PCT) {
    alerts.push(['outside-units', `All API keys together are at ${fmt(outside)}/${fmt(OUTSIDE_UNITS_DAILY)} database units today. At 100% every key gets 429 until 00:00 UTC; OPBindr/OPCanvs are unaffected.`]);
  }

  for (const [id, text] of alerts) {
    const dedupKey = `state/alerts/${today}/${encodeURIComponent(id)}`;
    try { if (env.IMAGES && await env.IMAGES.head(dedupKey)) continue; } catch { /* alert anyway */ }
    try {
      await fetch(env.DISCORD_USAGE_WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: `**[OPTCG API] Usage alert**\n${text}\nRun \`npm run key:list\` to inspect.` }),
      });
      if (env.IMAGES) await env.IMAGES.put(dedupKey, '1');
    } catch (err) {
      console.error('usage-alert webhook failed:', err?.message || err);
    }
  }
}
