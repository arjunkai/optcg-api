// R2-backed snapshots for the bulk index endpoints (/cards/index, /cards/all,
// /pokemon/cards/index, /pokemon/cards/all, /representatives) and the small
// derived datasets some routes answer from in JS (the /characters roster).
//
// Requests never build a snapshot. Building one is a full-table D1 read plus
// a JSON.stringify of up to ~57 MB, far past the Workers Free 10 ms CPU limit
// (and a CPU kill can't fall back to anything). scripts/build-snapshots.mjs
// builds them outside the Worker from src/snapshotDefs.js and uploads
// snapshots/{name}.json plus snapshots/lkg/{name}.json (last known good). The
// weekly workflows run it after importing, and deploy.yml runs it before
// every deploy so a renamed snapshot exists before the code that serves it.
//
// Request path, never touching D1:
//
//   edge cache (per colo, 1h) -> R2 snapshot -> R2 last-known-good -> 503
//
// The R2 body streams straight into the response, so serving costs almost no
// CPU however large the object. refresh=1 (admin X-API-Key callers only, see
// wantsRefresh) skips the edge cache and the in-isolate memo and re-reads R2.

import { wantsRefresh } from './edgeCache.js';

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
    return await r2.get(key);
  } catch (err) {
    console.error(`snapshot: R2 get ${key} failed:`, err?.message || err);
    return null;
  }
}

// The built R2 object for `name`: { obj, fallback }, or null when neither the
// snapshot nor its last-known-good copy exists.
async function openSnapshot(c, name) {
  const r2 = c.env.IMAGES;
  if (!r2) return null;
  const obj = await readR2(r2, `snapshots/${name}.json`);
  if (obj) return { obj, fallback: false };
  const lkg = await readR2(r2, `snapshots/lkg/${name}.json`);
  if (lkg) {
    console.error(`snapshot ${name}: missing, serving last-known-good copy`);
    return { obj: lkg, fallback: true };
  }
  return null;
}

// 503 for a snapshot that has never been built (run scripts/build-snapshots.mjs).
export function snapshotUnavailable(name) {
  console.error(`snapshot ${name}: not built`);
  return new Response(JSON.stringify({ error: 'snapshot_unavailable', snapshot: name }), {
    status: 503,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'Retry-After': '300' },
  });
}

// `name` must change whenever the response shape changes (it doubles as the
// cache version), and must have an entry in src/snapshotDefs.js.
export async function serveSnapshot(c, name) {
  const cache = caches.default;
  const cacheKey = edgeKey(c, name);

  if (wantsRefresh(c)) {
    await cache.delete(cacheKey);
  } else {
    const hit = await cache.match(cacheKey);
    if (hit) return new Response(hit.body, hit);
  }

  const snap = await openSnapshot(c, name);
  if (!snap) return snapshotUnavailable(name);
  if (snap.fallback) {
    // Serve it, but don't pin the fallback copy in the edge cache for an hour.
    return new Response(snap.obj.body, { headers: { ...RESPONSE_HEADERS, 'Cache-Control': 'public, max-age=60' } });
  }
  const response = new Response(snap.obj.body, { status: 200, headers: RESPONSE_HEADERS });
  c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()).catch(() => {}));
  return response;
}

// Parsed snapshot data for routes that answer from it in JS (small datasets
// only: the parse runs inside the request). Memoized per isolate for a few
// minutes, so a warm isolate does no R2 read or JSON parse. Returns null when
// the snapshot has never been built; callers answer snapshotUnavailable(name).
const MEMO_MS = 10 * 60 * 1000;
const memo = new Map();

export async function loadSnapshotData(c, name) {
  const held = memo.get(name);
  if (!wantsRefresh(c) && held && Date.now() - held.at < MEMO_MS) return held.data;
  const snap = await openSnapshot(c, name);
  if (!snap) return null;
  const data = await snap.obj.json();
  // A fallback copy is only held briefly so the next request retries R2.
  memo.set(name, { data, at: snap.fallback ? Date.now() - MEMO_MS + 60_000 : Date.now() });
  return data;
}
