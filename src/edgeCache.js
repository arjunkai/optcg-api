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
// callers. Browser callers can't be authenticated (Origin is forgeable), so
// for them refresh=1 is ignored and they get the cached copy — otherwise a
// loop of refresh=1 requests would force a D1 query every time.

// Routes that manage their own cache (R2 snapshot + edge, see snapshot.js).
const SELF_CACHED = new Set([
  '/cards/all',
  '/cards/index',
  '/pokemon/cards/all',
  '/pokemon/cards/index',
  '/representatives',
]);

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
  return c.req.query('refresh') === '1' && c.get('caller') === 'key';
}

// Cache key for a request URL, with refresh stripped so a refresh purges the
// same entry a normal hit reads.
export function cacheKeyFor(rawUrl) {
  const url = new URL(rawUrl);
  url.searchParams.delete('refresh');
  return new Request(url.toString(), { method: 'GET' });
}

// Per-IP limit for browser callers (RL_IP binding, wrangler.toml). Returns a
// 429 response when over, otherwise null. Keyed callers are limited per key
// in auth.js instead. Fails open if the binding is missing or errors.
export async function limitBrowser(c) {
  if (c.get('caller') !== 'browser' || !c.env?.RL_IP) return null;
  const ip = c.req.header('cf-connecting-ip');
  if (!ip) return null;
  try {
    const { success } = await c.env.RL_IP.limit({ key: `ip:${ip}` });
    if (success) return null;
  } catch {
    return null;
  }
  return c.json(
    { error: 'rate_limited', detail: 'too many uncached requests from this IP' },
    429,
    { 'Retry-After': '60' }
  );
}

export function edgeCache() {
  return async (c, next) => {
    const caller = c.get('caller');
    const path = new URL(c.req.url).pathname;
    // caller is unset on public paths (images/docs), which the gate lets
    // through early; those have their own caching.
    if (c.req.method !== 'GET' || !caller || SELF_CACHED.has(path)) {
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

    const limited = await limitBrowser(c);
    if (limited) return limited;

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
