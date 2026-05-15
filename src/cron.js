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

const DAILY_LIMIT = 100_000;
const ALERT_THRESHOLD_PCT = 0.8;

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
