// Edge cache + per-IP limit for the gated data routes.
//
// Card data changes weekly, but every data request used to run its D1 query
// fresh. OPCanvs pages fan out one request per tile (a set/character/
// illustrator grid fires 100-700 of them), so a handful of page views could
// burn the D1 free-tier daily read quota. This middleware puts every 200 JSON
// response from a gated GET route into the Workers Cache API, so repeat
// requests in the same colo skip D1 entirely.
//
// Order in index.js: cors -> gate (auth) -> edgeCache -> routes. Cache hits
// return before the per-IP limiter, so only D1-bound misses count.
//
// refresh=1 purges the entry and re-runs the query, but only for X-API-Key
// callers whose key holds the `admin` scope. Browser callers can't be
// authenticated (Origin is forgeable), so for them refresh=1 is ignored and
// they get the cached copy — otherwise a loop of refresh=1 requests would
// force a D1 query every time.

import { OUTSIDE_UNITS_DAILY, secondsUntilUtcMidnight } from './limits.js';
import { createUnitMeter } from './usage.js';
import { tooMany, unavailable } from './auth.js';

// Routes that manage their own cache (R2 snapshot + edge, see snapshot.js).
const SELF_CACHED = new Set([
  '/cards/all',
  '/cards/index',
  '/pokemon/cards/all',
  '/pokemon/cards/index',
  '/representatives',
]);

// Query params each route actually reads. Everything else is dropped from the
// cache key, so `?_=123`-style junk can't turn every request into a D1 miss.
// A route missing here caches with no params at all, so a new route that
// reads a param MUST be added (the tests in tests/edge-cache-keys.test.mjs
// check this list against the route handlers).
const CARD_FILTERS = [
  'set_id', 'color', 'category', 'rarity', 'name', 'parallel', 'variant_type', 'finish',
  'min_power', 'max_power', 'min_cost', 'max_cost', 'min_price', 'max_price',
  'sort', 'order', 'page', 'page_size',
];
const ROUTE_PARAMS = [
  [/^\/cards$/, CARD_FILTERS],
  [/^\/cards\/[^/]+\/price-history$/, ['range']],
  [/^\/cards\/[^/]+$/, ['lang']],
  [/^\/illustrators$/, ['page', 'page_size', 'sort']],
  [/^\/characters$/, ['page', 'page_size', 'q', 'sort']],
  [/^\/artwork$/, ['page', 'page_size', 'artist', 'character']],
  [/^\/artwork\/gallery$/, ['page', 'page_size', 'collection']],
  [/^\/pokemon\/cards\/[^/]+\/price-history$/, ['range']],
  [/^\/pokemon\/cards\/[^/]+$/, ['lang']],
  [/^\/pokemon\/sets$/, ['lang']],
  [/^\/pokemon\/sets\/[^/]+\/cards$/, ['lang']],
  // Self-cached routes, used by snapshot.js for its edge key.
  [/^\/(pokemon\/)?cards\/(all|index)$/, ['lang']],
  [/^\/representatives$/, ['kind']],
];

export function paramsFor(pathname) {
  for (const [re, params] of ROUTE_PARAMS) {
    if (re.test(pathname)) return params;
  }
  return [];
}

// Routes whose cache misses cost thousands of D1 rows (unindexed LIKE /
// json_each scans plus a COUNT). They get a much lower per-IP miss limit
// (RL_IP_HEAVY) than the cheap indexed lookups.
export function isHeavyMiss(pathname, url) {
  const p = url.searchParams;
  // A set_id filter is index-driven (~450 rows); anything else scans cards.
  if (pathname === '/cards') return !p.get('set_id');
  // Unfiltered /artwork pages OFFSET-scan cards; artist/character are indexed.
  if (pathname === '/artwork') return !p.get('artist') && !p.get('character');
  // Ordered by a CASE expression, so every page sorts the whole table.
  if (pathname === '/artwork/gallery') return true;
  return false;
}

// D1 cost units per cache miss (about the rows the route's query reads),
// charged to API keys before the route runs so outside keys can't exhaust the
// Free plan's 5M rows/day. Light routes (a few indexed rows, or answered from
// an in-isolate snapshot) cost 0. tests/unit-weights.test.mjs requires a rule
// for every gated route. Browser callers are not charged (Phase 2 covers them).
const SNAPSHOT_ROUTES = new Set(['/cards/all', '/cards/index', '/pokemon/cards/all', '/pokemon/cards/index', '/representatives']);
const HEAVY_UNITS = 15_000;
const UNIT_RULES = [
  [/^\/openapi\.json$/, () => 0],
  [/^\/cards$/, (u) => (u.searchParams.get('set_id') ? 1_000 : HEAVY_UNITS)],
  [/^\/artwork$/, (u) => (u.searchParams.get('artist') || u.searchParams.get('character') ? 1_000 : HEAVY_UNITS)],
  [/^\/artwork\/gallery$/, () => 1_000],
  [/^\/characters$/, () => 0], // answered from the in-isolate roster snapshot (canvs.js)
  [/^\/(illustrators|products)$/, () => 3_000],
  [/^\/(illustrators|characters)\/[^/]+$/, () => 1_000],
  [/^\/(pokemon\/)?sets\/[^/]+\/cards$/, () => 1_000],
  [/^\/(pokemon\/)?cards\/[^/]+\/price-history$/, (u) => (u.searchParams.get('range') === 'all' ? 1_000 : 500)],
  [/^\/(pokemon\/)?cards\/[^/]+$/, () => 0],
  [/^\/(pokemon\/)?sets$/, () => 0],
];
export const DEFAULT_UNITS = 3_000;
export const MIN_CHARGED_UNITS = 500;

export function explicitUnits(pathname, url) {
  if (SNAPSHOT_ROUTES.has(pathname)) return 0;
  for (const [re, fn] of UNIT_RULES) if (re.test(pathname)) return fn(url);
  return null;
}

export function unitsFor(pathname, url) {
  return explicitUnits(pathname, url) ?? DEFAULT_UNITS;
}

const EDGE_TTL_S = 3600;   // how long a colo keeps a response
const BROWSER_TTL_S = 300; // how long a browser may reuse it

// CORS headers are per-request (they echo the caller's Origin), so they must
// never be stored — hono/cors re-adds the right ones on every response.
const PER_REQUEST_HEADERS = [
  'access-control-allow-origin',
  'access-control-allow-credentials',
  'access-control-expose-headers',
  'vary',
];

// True when this request may bypass/purge caches. Shared with snapshot.js.
export function wantsRefresh(c) {
  return c.req.query('refresh') === '1' && c.get('caller') === 'key' && c.get('admin') === true;
}

// Cache key for a request URL: only the params the route reads, sorted, so
// junk params and param order don't fragment the cache. refresh is never
// part of it, so a refresh purges the same entry a normal hit reads. The
// first value of a repeated param is kept, which is the one Hono's
// c.req.query() hands the route.
export function cacheKeyFor(rawUrl) {
  const url = new URL(rawUrl);
  const keep = paramsFor(url.pathname);
  const out = new URL(url.origin + url.pathname);
  for (const name of [...keep].sort()) {
    const value = url.searchParams.get(name);
    if (value !== null) out.searchParams.set(name, value);
  }
  return new Request(out.toString(), { method: 'GET' });
}

// Rate-limit key for a client IP. IPv6 clients usually control a whole /64,
// so they're limited per /64 rather than per address.
export function ipLimitKey(ip) {
  if (!ip.includes(':')) return `ip:${ip}`;
  // IPv4-mapped (::ffff:1.2.3.4) is really a v4 client.
  if (ip.includes('.')) return `ip:${ip.slice(ip.lastIndexOf(':') + 1)}`;
  // Groups before any '::' are the leading ones; missing groups are zero.
  const groups = ip.split('::')[0].split(':');
  const prefix = [];
  for (let i = 0; i < 4; i++) prefix.push((groups[i] || '0').toLowerCase());
  return `ip6:${prefix.join(':')}`;
}

function limited(detail) {
  return new Response(JSON.stringify({ error: 'rate_limited', detail }), {
    status: 429,
    headers: { 'Content-Type': 'application/json', 'Retry-After': '60', 'Cache-Control': 'no-store' },
  });
}

// Per-IP limit for browser callers (RL_IP / RL_IP_HEAVY bindings,
// wrangler.toml). Returns a 429 response when over, otherwise null. Keyed
// callers are limited per key in auth.js instead. Fails open if a binding is
// missing or errors.
export async function limitBrowser(c, { heavy = false } = {}) {
  if (c.get('caller') !== 'browser') return null;
  const ip = c.req.header('cf-connecting-ip');
  if (!ip) return null;
  const key = ipLimitKey(ip);
  const checks = [[c.env?.RL_IP, 'too many uncached requests from this IP']];
  if (heavy) checks.push([c.env?.RL_IP_HEAVY, 'too many uncached searches from this IP']);
  for (const [binding, detail] of checks) {
    if (!binding) continue;
    try {
      const { success } = await binding.limit({ key });
      if (!success) return limited(detail);
    } catch {
      // fail open
    }
  }
  return null;
}

// Cache-miss limits. Keys: the tier heavy binding, then D1 units against the
// key's cap and the outside share (exempt keys skip units; a failed charge
// is a 503). Browsers: today's per-IP limits only.
export async function limitCaller(c, { heavy = false, units = 0, meter } = {}) {
  if (c.get('caller') !== 'key') return limitBrowser(c, { heavy });
  const tier = c.get('tier');
  const prefix = c.get('keyPrefix');
  const resetIn = () => secondsUntilUtcMidnight(Date.now());
  if (heavy && tier) {
    const binding = c.env?.[tier.heavyBinding];
    if (binding) {
      try {
        const { success } = await binding.limit({ key: prefix });
        if (!success) {
          return tooMany(c, 'heavy_rate_limited', tier, tier.heavyPerMinute,
            `too many uncached searches for this key (${tier.heavyPerMinute}/min)`, 60);
        }
      } catch { /* fail open */ }
    }
  }
  if (units <= 0 || c.get('exempt') || !meter || !tier) return null;
  const outsideFull = () => tooMany(c, 'outside_daily_capacity', tier, OUTSIDE_UNITS_DAILY,
    'all API keys together used today\'s database share; resets at 00:00 UTC', resetIn());
  if (meter.isExhausted('u:outside')) return outsideFull();
  const db = c.env?.DB;
  try {
    const mine = await meter.charge(db, `u:${prefix}`, units, tier.dailyUnits);
    if (!mine.ok) {
      return tooMany(c, 'daily_quota_exceeded', tier, tier.dailyUnits,
        `daily database budget reached (${tier.dailyUnits} units/day)`, resetIn());
    }
    const all = await meter.charge(db, 'u:outside', units, OUTSIDE_UNITS_DAILY);
    if (!all.ok) return outsideFull();
  } catch (err) {
    console.error('unit charge failed:', err?.message || err);
    return unavailable(c);
  }
  return null;
}

export function edgeCache({ meter = createUnitMeter() } = {}) {
  return async (c, next) => {
    const caller = c.get('caller');
    const url = new URL(c.req.url);
    const path = url.pathname;
    // Hono answers HEAD with the GET handler, so HEAD must take the same
    // cached + limited path or it becomes an uncached, unlimited D1 query.
    const method = c.req.method;
    // caller is unset on public paths (images/docs), which the gate lets
    // through early; those have their own caching.
    if ((method !== 'GET' && method !== 'HEAD') || !caller || SELF_CACHED.has(path)) {
      await next();
      return;
    }

    const cache = caches.default;
    const cacheKey = cacheKeyFor(c.req.url);

    if (wantsRefresh(c)) {
      await cache.delete(cacheKey);
    } else {
      const hit = await cache.match(cacheKey);
      if (hit) {
        const res = new Response(hit.body, hit);
        res.headers.set('Cache-Control', `public, max-age=${BROWSER_TTL_S}`);
        res.headers.set('X-Cache', 'HIT');
        return res;
      }
    }

    const blocked = await limitCaller(c, { heavy: isHeavyMiss(path, url), units: unitsFor(path, url), meter });
    if (blocked) return blocked;

    await next();

    const res = c.res;
    if (res.status !== 200) return;
    if (!(res.headers.get('content-type') || '').includes('application/json')) return;

    const headers = new Headers(res.headers);
    for (const h of PER_REQUEST_HEADERS) headers.delete(h);
    headers.set('Cache-Control', `public, max-age=${EDGE_TTL_S}`);
    const body = await res.clone().arrayBuffer();
    c.executionCtx.waitUntil(
      cache.put(cacheKey, new Response(body, { status: 200, headers })).catch(() => {})
    );
    c.header('Cache-Control', `public, max-age=${BROWSER_TTL_S}`);
    c.header('X-Cache', 'MISS');
  };
}
