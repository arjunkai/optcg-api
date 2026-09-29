// R2-backed snapshots for the bulk index endpoints (/cards/index, /cards/all,
// /pokemon/cards/index, /pokemon/cards/all, /representatives).
//
// These are full-table reads (up to ~46k D1 rows for the PTCG EN index) of
// data that changes weekly. The Cache API alone is per-colo, so every
// Cloudflare data center re-ran the query every hour. Instead the built JSON
// is stored once in R2 (snapshots/{name}.json) and shared by every colo:
//
//   edge cache (per colo, 1h) -> R2 snapshot (global, SNAPSHOT_TTL_MS) -> D1
//
// If the snapshot is stale and the D1 rebuild fails (e.g. the daily read
// quota is exhausted), the stale snapshot is served instead of a 500, so the
// OPBindr registry keeps loading through a D1 outage.
//
// refresh=1 (X-API-Key callers only, see wantsRefresh) skips both caches and
// rebuilds. The weekly workflows also delete snapshots/ after importing.

import { cacheKeyFor, wantsRefresh } from './edgeCache.js';

const SNAPSHOT_TTL_MS = 6 * 3600 * 1000;
const RESPONSE_HEADERS = {
  'Content-Type': 'application/json',
  'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400',
};

// `name` must change whenever the response shape changes (it doubles as the
// cache version). `build` returns the response object; it runs only when
// neither cache can answer.
export async function serveSnapshot(c, name, build) {
  const cache = caches.default;
  const url = new URL(c.req.url);
  url.searchParams.set('_snap', name);
  const cacheKey = cacheKeyFor(url.toString());
  const refresh = wantsRefresh(c);

  if (refresh) {
    await cache.delete(cacheKey);
  } else {
    const hit = await cache.match(cacheKey);
    if (hit) return new Response(hit.body, hit);
  }

  const r2 = c.env.IMAGES;
  const r2Key = `snapshots/${name}.json`;
  let stale = null;
  let body = null;

  if (r2 && !refresh) {
    try {
      const obj = await r2.get(r2Key);
      if (obj) {
        const bytes = await obj.arrayBuffer();
        const builtAt = Number(obj.customMetadata?.builtAt) || 0;
        if (Date.now() - builtAt < SNAPSHOT_TTL_MS) body = bytes;
        else stale = bytes;
      }
    } catch { /* R2 hiccup: rebuild from D1 below */ }
  }

  if (!body) {
    try {
      // Kept as a string (no byte copy): /pokemon/cards/all is large and the
      // Worker has 128 MB. R2 put and Response both take strings.
      body = JSON.stringify(await build());
      if (r2) {
        c.executionCtx.waitUntil(
          r2.put(r2Key, body, {
            httpMetadata: { contentType: 'application/json' },
            customMetadata: { builtAt: String(Date.now()) },
          }).catch(() => {})
        );
      }
    } catch (err) {
      if (!stale) throw err;
      console.error(`snapshot ${name}: rebuild failed, serving stale copy:`, err?.message || err);
      // Serve it, but don't pin the stale copy in the edge cache for an hour.
      return new Response(stale, { headers: { ...RESPONSE_HEADERS, 'Cache-Control': 'public, max-age=60' } });
    }
  }

  const response = new Response(body, { status: 200, headers: RESPONSE_HEADERS });
  c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()).catch(() => {}));
  return response;
}
