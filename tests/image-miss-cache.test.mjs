// node --test tests/*.test.mjs
// /images/:card_id miss handling with fetch, the Cache API and R2 mocked.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { Hono } from 'hono';
import { registerImageRoutes } from '../src/images.js';

let upstream;   // (url) => Response | 'timeout'
let calls;      // upstream URLs fetched
const store = new Map();
globalThis.caches = {
  default: {
    match: async (req) => store.get(typeof req === 'string' ? req : req.url)?.clone(),
    put: async (req, res) => { store.set(typeof req === 'string' ? req : req.url, res); },
    delete: async (req) => store.delete(typeof req === 'string' ? req : req.url),
  },
};
globalThis.fetch = async (url, init = {}) => {
  calls.push(String(url));
  const r = upstream(String(url));
  if (r === 'timeout') {
    await new Promise((_, reject) => init.signal?.addEventListener('abort', () => reject(new Error('aborted'))));
  }
  return r;
};

const r2 = new Map();
const IMAGES = {
  get: async (k) => (r2.has(k) ? { size: r2.get(k).bytes.byteLength, body: r2.get(k).bytes, httpMetadata: r2.get(k).meta } : null),
  head: async (k) => (r2.has(k) ? {} : null),
  put: async (k, bytes, opts) => { r2.set(k, { bytes, meta: opts?.httpMetadata }); },
};
const DB = {
  prepare: () => ({ bind: () => ({ first: async () => ({ tcg_ids: '[12345]' }) }) }),
};

const app = new Hono();
registerImageRoutes(app);
async function get(path) {
  const waits = [];
  const res = await app.request(path, {}, { IMAGES, DB }, { waitUntil: (p) => waits.push(p), passThroughOnException() {} });
  await Promise.all(waits);
  return res;
}
const png = () => new Response(new Uint8Array([1, 2, 3]), { headers: { 'content-type': 'image/png' } });
const jpg = () => new Response(new Uint8Array([9, 9]), { headers: { 'content-type': 'image/jpeg' } });

beforeEach(() => { store.clear(); r2.clear(); calls = []; });

test('a clean 404 everywhere is remembered, so the next request skips upstream', async () => {
  upstream = () => new Response('no', { status: 404 });
  assert.equal((await get('/images/ZZ01-001')).status, 404);
  const before = calls.length;
  const again = await get('/images/ZZ01-001');
  assert.equal(again.status, 404);
  assert.equal(calls.length, before, 'no upstream fetch on the remembered miss');
  assert.match(again.headers.get('cache-control'), /max-age=1800/);
});

test('a transient failure is not remembered and is not cacheable', async () => {
  upstream = (u) => (u.includes('wsrv.nl') ? new Response('bad gateway', { status: 502 }) : new Response('no', { status: 404 }));
  const res = await get('/images/ZZ01-002');
  assert.equal(res.status, 404);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  // Upstream recovers: the very next request serves the image.
  upstream = () => png();
  const ok = await get('/images/ZZ01-002');
  assert.equal(ok.status, 200);
  assert.ok(r2.has('cards/ZZ01-002.png'));
});

test('a DON without curated art keeps its TCGPlayer image in R2 and stops fetching it', async () => {
  upstream = () => jpg();
  const first = await get('/images/DON-004');
  assert.equal(first.status, 200);
  assert.equal(first.headers.get('content-type'), 'image/jpeg');
  assert.ok(r2.has('cards/fallback/DON-004'));
  store.clear(); // even with a cold edge cache
  const before = calls.length;
  const second = await get('/images/DON-004');
  assert.equal(second.status, 200);
  assert.equal(calls.length, before, 'served from R2, no TCGPlayer fetch');
});

test('a DON whose TCGPlayer fetch times out is retried on the next request', async () => {
  upstream = (u) => (u.includes('tcgplayer') && !u.includes('wsrv') ? 'timeout' : new Response('x', { status: 500 }));
  const res = await get('/images/DON-005');
  assert.equal(res.status, 404);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  upstream = () => jpg();
  assert.equal((await get('/images/DON-005')).status, 200);
});
