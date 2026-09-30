// R2-backed snapshots for the bulk index endpoints (/cards/index, /cards/all,
// /pokemon/cards/index, /pokemon/cards/all, /representatives) and the small
// derived datasets some routes answer from in JS (the /characters roster).
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
// OPBindr registry keeps loading through a D1 outage. Every successful build
// also writes snapshots/lkg/{name}.json (last known good), which the weekly
// purge (scripts/purge-snapshots.mjs) leaves alone — so even right after a
// purge there is a copy to fall back on.
//
// refresh=1 (admin X-API-Key callers only, see wantsRefresh) skips both
// caches and rebuilds. The weekly workflows also delete snapshots/ after
// importing.

import { wantsRefresh } from './edgeCache.js';

const SNAPSHOT_TTL_MS = 6 * 3600 * 1000;
const RESPONSE_HEADERS = {
  'Content-Type': 'application/json',
  'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400',
};

// The response depends only on the snapshot name (which already encodes lang
// and kind), so the edge entry is keyed by name alone: junk query params
// can't miss the cache and pull the whole object out of R2 again.
function edgeKey(c, name) {
  const origin = new URL(c.req.url).origin;
  return new Request(`${origin}/__snapshot/${encodeURIComponent(name)}`, { method: 'GET' });
}

async function readR2(r2, key) {
  try {
    const obj = await r2.get(key);
    if (!obj) return null;
    return { bytes: await obj.arrayBuffer(), builtAt: Number(obj.customMetadata?.builtAt) || 0 };
  } catch {
    return null; // R2 hiccup: caller rebuilds from D1
  }
}

function writeR2(c, r2, key, body) {
  c.executionCtx.waitUntil(
    r2.put(key, body, {
      httpMetadata: { contentType: 'application/json' },
      customMetadata: { builtAt: String(Date.now()) },
    }).catch((err) => console.error(`snapshot: R2 put ${key} failed:`, err?.message || err))
  );
}

// Returns { body, stale } where body is the JSON string/bytes. Order: fresh R2
// copy -> rebuild from D1 (and store) -> stale R2 copy -> last-known-good.
// Throws only when D1 fails and no copy exists at all.
async function snapshotBody(c, name, build, refresh) {
  const r2 = c.env.IMAGES;
  const key = `snapshots/${name}.json`;
  const lkgKey = `snapshots/lkg/${name}.json`;
  let stale = null;

  if (r2 && !refresh) {
    const hit = await readR2(r2, key);
    if (hit) {
      if (Date.now() - hit.builtAt < SNAPSHOT_TTL_MS) return { body: hit.bytes, stale: false };
      stale = hit.bytes;
    }
  }

  try {
    // Kept as a string (no byte copy): /pokemon/cards/all is large and the
    // Worker has 128 MB. R2 put and Response both take strings.
    const body = JSON.stringify(await build());
    if (r2) {
      writeR2(c, r2, key, body);
      writeR2(c, r2, lkgKey, body);
    }
    return { body, stale: false };
  } catch (err) {
    if (!stale && r2) stale = (await readR2(r2, lkgKey))?.bytes ?? null;
    if (!stale) throw err;
    console.error(`snapshot ${name}: rebuild failed, serving stale copy:`, err?.message || err);
    return { body: stale, stale: true };
  }
}

// `name` must change whenever the response shape changes (it doubles as the
// cache version). `build` returns the response object; it runs only when
// neither cache can answer.
export async function serveSnapshot(c, name, build) {
  const cache = caches.default;
  const cacheKey = edgeKey(c, name);
  const refresh = wantsRefresh(c);

  if (refresh) {
    await cache.delete(cacheKey);
  } else {
    const hit = await cache.match(cacheKey);
    if (hit) return new Response(hit.body, hit);
  }

  const { body, stale } = await snapshotBody(c, name, build, refresh);
  if (stale) {
    // Serve it, but don't pin the stale copy in the edge cache for an hour.
    return new Response(body, { headers: { ...RESPONSE_HEADERS, 'Cache-Control': 'public, max-age=60' } });
  }
  const response = new Response(body, { status: 200, headers: RESPONSE_HEADERS });
  c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()).catch(() => {}));
  return response;
}

// Parsed snapshot data for routes that answer from it in JS. Memoized per
// isolate for a few minutes, so a warm isolate does no R2 read or JSON parse.
const MEMO_MS = 10 * 60 * 1000;
const memo = new Map();

export async function loadSnapshotData(c, name, build) {
  const refresh = wantsRefresh(c);
  const held = memo.get(name);
  if (!refresh && held && Date.now() - held.at < MEMO_MS) return held.data;
  const { body, stale } = await snapshotBody(c, name, build, refresh);
  const data = JSON.parse(typeof body === 'string' ? body : new TextDecoder().decode(body));
  // A stale copy is only held briefly so the next request retries D1.
  memo.set(name, { data, at: stale ? Date.now() - MEMO_MS + 60_000 : Date.now() });
  return data;
}
