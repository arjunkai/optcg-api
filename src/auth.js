// Origin + API key gate for the OPTCG API, sized for the Workers Free plan.
// Design: docs/superpowers/specs/2026-10-01-api-limits-phase1-design.md
//
// Browser callers on the Origin allowlist need no key; edgeCache.js limits
// them per IP. Key callers, in order: key cache -> (miss) D1 lookup, with
// unknown keys consuming a per-IP limiter -> scope -> tier per-minute ->
// tier daily request cap. 'firstparty' and 'admin' keys keep the per-minute
// limit but skip the daily cap and are never counted. edgeCache.js charges
// D1 units for costly cache misses.

import { tierFor, secondsUntilUtcMidnight } from './limits.js';
import { createKeyCache } from './keyCache.js';
import { createRequestCounter } from './usage.js';
import { ipLimitKey } from './edgeCache.js';

const ALLOWED_EXACT = new Set([
  'https://opbindr.com',
  'https://www.opbindr.com',
  'https://opbindr.pages.dev',
  'https://opcanvs.com',
  'https://www.opcanvs.com',
  'https://opcanvs.pages.dev',
  'http://localhost:5173',
  'http://localhost:4173',
]);

// Cloudflare Pages preview deploys.
const ALLOWED_REGEX = [
  /^https:\/\/[a-z0-9-]+\.opbindr\.pages\.dev$/,
  /^https:\/\/[a-z0-9-]+\.opcanvs\.pages\.dev$/,
];

const PUBLIC_PREFIXES = ['/images/', '/pokemon/images/'];
// /openapi.json is NOT public: it needs a key (src/docs.js serves /docs).
const PUBLIC_EXACT = new Set(['/', '/docs', '/healthz']);

// last_used_at is informational and every D1 write counts against Free's
// 100k/day, so at most once per 6h per key per colo.
const LAST_USED_THROTTLE_S = 6 * 3600;
const IP_BLOCK_MS = 60_000;

function isPublicPath(pathname) {
  return PUBLIC_EXACT.has(pathname) || PUBLIC_PREFIXES.some((p) => pathname.startsWith(p));
}

// Also drives the CORS middleware in index.js, so the allowlists can't drift.
export function isAllowedOrigin(origin) {
  if (!origin) return false;
  return ALLOWED_EXACT.has(origin) || ALLOWED_REGEX.some((r) => r.test(origin));
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function lookupKey(db, hash) {
  if (!db) return null;
  return await db.prepare(
    "SELECT key_prefix, tier, scopes FROM api_keys WHERE key_hash = ? AND status = 'active'"
  ).bind(hash).first();
}

function requiredScope(pathname) {
  return pathname.startsWith('/pokemon/') ? 'ptcg' : 'optcg';
}

function hasScope(scopesStr, required) {
  if (!scopesStr) return false;
  return scopesStr.split(',').map((s) => s.trim()).filter(Boolean).includes(required);
}

export function tooMany(c, error, tier, limit, detail, retryAfterS) {
  return c.json(
    { error, tier: tier.name, limit, detail },
    429,
    { 'Retry-After': String(retryAfterS), 'Cache-Control': 'no-store' }
  );
}

export function unavailable(c) {
  return c.json(
    { error: 'temporarily_unavailable', detail: 'usage check failed, retry shortly' },
    503,
    { 'Retry-After': '30', 'Cache-Control': 'no-store' }
  );
}

function tooManyAttempts(c) {
  return c.json(
    { error: 'too_many_key_attempts', detail: 'too many unknown API keys from this IP' },
    429,
    { 'Retry-After': '60', 'Cache-Control': 'no-store' }
  );
}

async function touchLastUsed(c, keyHash) {
  if (!c.env?.DB) return;
  const cacheKey = new Request(`https://rl.local/lastused/${keyHash}`);
  const cache = caches.default;
  if (await cache.match(cacheKey)) return;
  c.executionCtx.waitUntil(Promise.all([
    c.env.DB.prepare('UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?')
      .bind(Date.now(), keyHash).run().catch(() => {}),
    cache.put(cacheKey, new Response('1', { headers: { 'Cache-Control': `max-age=${LAST_USED_THROTTLE_S}` } })),
  ]));
}

// Options exist for tests; production uses one cache/counter per isolate.
export function gate({
  keyCache = createKeyCache(),
  requests = createRequestCounter(),
  now = Date.now,
} = {}) {
  const blockedIps = new Map(); // ipLimitKey -> blocked-until ms

  return async (c, next) => {
    const url = new URL(c.req.url);
    if (isPublicPath(url.pathname)) {
      await next();
      return;
    }

    const origin = c.req.header('origin');
    if (origin) {
      if (isAllowedOrigin(origin)) {
        c.set('caller', 'browser');
        await next();
        return;
      }
      return c.json({ error: 'origin not allowed' }, 403);
    }

    const trimmed = (c.req.header('x-api-key') || '').trim();
    if (!trimmed) return c.json({ error: 'api key required' }, 401);
    const hash = await sha256Hex(trimmed);

    let keyRow;
    const cached = keyCache.get(hash);
    if (cached) {
      keyRow = cached.row;
    } else {
      const ip = c.req.header('cf-connecting-ip');
      const ipKey = ip ? ipLimitKey(ip) : null;
      if (ipKey && (blockedIps.get(ipKey) || 0) > now()) return tooManyAttempts(c);
      try {
        keyRow = await lookupKey(c.env?.DB, hash);
      } catch (err) {
        console.error('key lookup failed:', err?.message || err);
        return unavailable(c);
      }
      keyCache.set(hash, keyRow ?? null);
      if (!keyRow && ipKey && c.env?.RL_KEY_LOOKUP) {
        try {
          const { success } = await c.env.RL_KEY_LOOKUP.limit({ key: ipKey });
          if (!success) {
            if (blockedIps.size > 10_000) blockedIps.clear();
            blockedIps.set(ipKey, now() + IP_BLOCK_MS);
            return tooManyAttempts(c);
          }
        } catch { /* fail open: still a 401 below */ }
      }
    }
    if (!keyRow) return c.json({ error: 'api key required' }, 401);

    const needed = requiredScope(url.pathname);
    if (!hasScope(keyRow.scopes, needed)) {
      return c.json({ error: 'scope_required', detail: `key does not have ${needed} access` }, 403);
    }

    const tier = tierFor(keyRow.tier);
    const admin = hasScope(keyRow.scopes, 'admin');
    const exempt = admin || hasScope(keyRow.scopes, 'firstparty');
    const prefix = keyRow.key_prefix;
    const db = c.env?.DB;
    const waitUntil = (p) => c.executionCtx.waitUntil(p);

    const minute = c.env?.[tier.minuteBinding];
    if (minute) {
      try {
        const { success } = await minute.limit({ key: prefix });
        if (!success) {
          return tooMany(c, 'rate_limited', tier, tier.perMinute, `per-minute cap exceeded (${tier.perMinute}/min)`, 60);
        }
      } catch { /* binding down: the daily cap still applies */ }
    }

    let used = 0;
    if (!exempt) {
      used = await requests.peek(db, prefix, waitUntil);
      if (used + 1 > tier.daily) {
        return tooMany(c, 'daily_quota_exceeded', tier, tier.daily,
          `daily request cap reached (${tier.daily}/day)`, secondsUntilUtcMidnight(now()));
      }
      await requests.add(db, prefix, waitUntil);
    }

    await touchLastUsed(c, hash);
    c.set('caller', 'key');
    c.set('admin', admin); // refresh=1 stays admin-only (wantsRefresh)
    c.set('exempt', exempt);
    c.set('tier', tier);
    c.set('keyPrefix', prefix);
    await next();

    if (!exempt) {
      try {
        c.res.headers.set('X-RateLimit-Limit-Day', String(tier.daily));
        c.res.headers.set('X-RateLimit-Remaining-Day', String(Math.max(0, tier.daily - used - 1)));
      } catch { /* immutable headers */ }
    }
  };
}
