// Origin + API key gate for the OPTCG API, with per-key rate limiting.
//
// CORS Origin allowlist: only OPBindr frontend origins get full data access.
// Anyone else either supplies a valid API key (X-API-Key header) or gets 403/401.
//
// Public paths (root, docs, OpenAPI, image proxy) bypass the gate so the API
// stays discoverable and binder thumbnails shared on Discord/Twitter still
// render cleanly.
//
// Rate limiting (X-API-Key callers only — OPBindr's CORS path is unaffected):
//   * 300 req/min via the native Workers Rate Limit binding (RL_MINUTE).
//   * 100k req/day via Cache API counter, lazy-flushed to D1 (api_key_usage)
//     every 60 requests so the D1 free-tier 100k-write budget holds.

const ALLOWED_EXACT = new Set([
  'https://opbindr.com',
  'https://www.opbindr.com',
  'https://opbindr.pages.dev',
  'http://localhost:5173',
  'http://localhost:4173',
]);

// Regex for Cloudflare Pages preview deploys: <branch>.opbindr.pages.dev
const ALLOWED_REGEX = [
  /^https:\/\/[a-z0-9-]+\.opbindr\.pages\.dev$/,
];

// Path prefixes that bypass the gate entirely. These are the discoverable /
// public-facing endpoints that need to work without auth.
const PUBLIC_PREFIXES = [
  '/images/',          // OPTCG image proxy
  '/pokemon/images/',  // Future PTCG image proxy (Phase 2.2)
];

// Exact public paths.
const PUBLIC_EXACT = new Set([
  '/',
  '/docs',
  '/openapi.json',
  '/healthz',
]);

const DAILY_LIMIT = 100_000;
const DAILY_FLUSH_EVERY = 60;

function isPublicPath(pathname) {
  if (PUBLIC_EXACT.has(pathname)) return true;
  for (const p of PUBLIC_PREFIXES) {
    if (pathname.startsWith(p)) return true;
  }
  return false;
}

function isAllowedOrigin(origin) {
  if (!origin) return false;
  if (ALLOWED_EXACT.has(origin)) return true;
  for (const r of ALLOWED_REGEX) {
    if (r.test(origin)) return true;
  }
  return false;
}

function matchApiKey(provided, allKeys) {
  if (!provided || !allKeys) return null;
  const trimmed = provided.trim();
  const keys = allKeys.split(',').map(k => k.trim()).filter(Boolean);
  return keys.includes(trimmed) ? trimmed : null;
}

function secondsUntilUtcMidnight() {
  const now = new Date();
  const next = new Date(now);
  next.setUTCHours(24, 0, 0, 0);
  return Math.ceil((next.getTime() - now.getTime()) / 1000);
}

// Best-effort per-key daily counter. Cache API per-colo so a key fanning
// across many CF data centers can drift over the limit slightly — the D1
// flush gives cross-colo visibility on the next minute boundary.
async function bumpDailyCount(c, apiKey) {
  const today = new Date().toISOString().slice(0, 10);
  const cacheKey = new Request(`https://rl.local/daily/${encodeURIComponent(apiKey)}/${today}`);
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  const prev = cached ? parseInt(await cached.text(), 10) || 0 : 0;
  const count = prev + 1;

  c.executionCtx.waitUntil(cache.put(
    cacheKey,
    new Response(String(count), {
      headers: { 'Cache-Control': 'max-age=86400' },
    })
  ));

  if (count % DAILY_FLUSH_EVERY === 0 && c.env?.DB) {
    c.executionCtx.waitUntil(
      c.env.DB.prepare(
        'INSERT INTO api_key_usage (api_key, day, count, updated_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(api_key, day) DO UPDATE SET ' +
        '  count = MAX(api_key_usage.count, excluded.count), ' +
        '  updated_at = excluded.updated_at'
      ).bind(apiKey, today, count, Date.now()).run().catch(() => {})
    );
  }

  return count;
}

// Hono middleware. Place this BEFORE the route registrations.
// Returns 403 for disallowed origins, 401 for missing/invalid api keys,
// 429 for rate-limited keys.
export function gate() {
  return async (c, next) => {
    const url = new URL(c.req.url);
    if (isPublicPath(url.pathname)) {
      await next();
      return;
    }

    const origin = c.req.header('origin');
    if (origin) {
      if (isAllowedOrigin(origin)) {
        await next();
        return;
      }
      return c.json({ error: 'origin not allowed' }, 403);
    }

    // No Origin header — server-to-server caller. Require an API key.
    const provided = c.req.header('x-api-key');
    const apiKey = matchApiKey(provided, c.env?.API_KEYS);
    if (!apiKey) {
      return c.json({ error: 'api key required' }, 401);
    }

    if (c.env?.RL_MINUTE) {
      const { success } = await c.env.RL_MINUTE.limit({ key: apiKey });
      if (!success) {
        return c.json(
          { error: 'rate_limited', detail: 'per-minute cap exceeded (300/min)' },
          429,
          { 'Retry-After': '60' }
        );
      }
    }

    const dailyCount = await bumpDailyCount(c, apiKey);
    if (dailyCount > DAILY_LIMIT) {
      return c.json(
        { error: 'daily_quota_exceeded', limit: DAILY_LIMIT, count: dailyCount },
        429,
        { 'Retry-After': String(secondsUntilUtcMidnight()) }
      );
    }

    await next();
  };
}
