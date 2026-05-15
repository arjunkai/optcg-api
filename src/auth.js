// Origin + API key gate for the OPTCG API, with per-key rate limiting.
//
// CORS Origin allowlist: only OPBindr frontend origins get full data access.
// Anyone else either supplies a valid API key (X-API-Key header) or gets 403/401.
//
// Public paths (root, docs, OpenAPI, image proxy) bypass the gate so the API
// stays discoverable and binder thumbnails shared on Discord/Twitter still
// render cleanly.
//
// API key storage:
//   * Keys live in the D1 `api_keys` table (migration 013). Only the
//     SHA-256 hash is stored. Issuance via scripts/issue-key.mjs prints
//     the raw key once and never again.
//   * `key_prefix` (first 12 chars: `opt_xxxxxxxx`) is what we use as the
//     identifier for rate limiting and the api_key_usage daily counter.
//     Safe to log / display, not enough to authenticate alone.
//   * env.API_KEYS (legacy comma-separated env var) still works as a
//     transition fallback; logs a warning when used. Remove once all
//     active keys are migrated into D1.
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
const LAST_USED_THROTTLE_S = 60;

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

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const buf = await crypto.subtle.digest('SHA-256', bytes);
  const arr = Array.from(new Uint8Array(buf));
  return arr.map(b => b.toString(16).padStart(2, '0')).join('');
}

// Look up an active key by its SHA-256 hash. Returns { key_prefix, tier }
// or null. Filters status='active' so revoked rows are never honoured.
async function lookupKey(db, hash) {
  if (!db) return null;
  return await db.prepare(
    "SELECT key_prefix, tier FROM api_keys WHERE key_hash = ? AND status = 'active'"
  ).bind(hash).first();
}

function matchEnvVarKey(provided, allKeys) {
  if (!provided || !allKeys) return false;
  const keys = allKeys.split(',').map(k => k.trim()).filter(Boolean);
  return keys.includes(provided);
}

function secondsUntilUtcMidnight() {
  const now = new Date();
  const next = new Date(now);
  next.setUTCHours(24, 0, 0, 0);
  return Math.ceil((next.getTime() - now.getTime()) / 1000);
}

// Best-effort per-key daily counter, keyed on the key_prefix (not the
// raw key). Cache API is per-colo, so a key fanning across many CF data
// centers can drift slightly over the cap — the D1 flush gives cross-
// colo visibility on the next minute boundary.
async function bumpDailyCount(c, prefix) {
  const today = new Date().toISOString().slice(0, 10);
  const cacheKey = new Request(`https://rl.local/daily/${encodeURIComponent(prefix)}/${today}`);
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
      ).bind(prefix, today, count, Date.now()).run().catch(() => {})
    );
  }

  return count;
}

// Update last_used_at on the api_keys row, throttled to once per
// LAST_USED_THROTTLE_S seconds per key so we don't burn D1 writes.
async function touchLastUsed(c, keyHash) {
  if (!c.env?.DB) return;
  const cacheKey = new Request(`https://rl.local/lastused/${keyHash}`);
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) return;
  c.executionCtx.waitUntil(Promise.all([
    c.env.DB.prepare('UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?')
      .bind(Date.now(), keyHash).run().catch(() => {}),
    cache.put(cacheKey, new Response('1', {
      headers: { 'Cache-Control': `max-age=${LAST_USED_THROTTLE_S}` },
    })),
  ]));
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
    if (!provided) {
      return c.json({ error: 'api key required' }, 401);
    }
    const trimmed = provided.trim();
    const hash = await sha256Hex(trimmed);

    let keyRow = await lookupKey(c.env?.DB, hash);

    // Transition fallback for keys still in the legacy comma-separated
    // env var. Remove env.API_KEYS once everything is migrated to D1.
    if (!keyRow && matchEnvVarKey(trimmed, c.env?.API_KEYS)) {
      console.warn('legacy env-var key used, migrate to D1 (issue-key.mjs)');
      keyRow = { key_prefix: trimmed.slice(0, 12), tier: 'standard' };
    }

    if (!keyRow) {
      return c.json({ error: 'api key required' }, 401);
    }

    if (c.env?.RL_MINUTE) {
      const { success } = await c.env.RL_MINUTE.limit({ key: keyRow.key_prefix });
      if (!success) {
        return c.json(
          { error: 'rate_limited', detail: 'per-minute cap exceeded (300/min)' },
          429,
          { 'Retry-After': '60' }
        );
      }
    }

    const dailyCount = await bumpDailyCount(c, keyRow.key_prefix);
    if (dailyCount > DAILY_LIMIT) {
      return c.json(
        { error: 'daily_quota_exceeded', limit: DAILY_LIMIT, count: dailyCount },
        429,
        { 'Retry-After': String(secondsUntilUtcMidnight()) }
      );
    }

    await touchLastUsed(c, hash);
    await next();
  };
}
