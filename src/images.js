const IMG_HEADERS = {
  'Content-Type': 'image/png',
  'Cache-Control': 'public, max-age=86400',
  'Access-Control-Allow-Origin': '*',
};

// Bandai's CDN occasionally hot-link-blocks the CF Worker IP ranges and
// fails the request slowly (30+ second hang then non-200). Without a
// timeout the user sees a 30s spinner on every uncached card image. We
// abort the direct fetch after 5s and fall through to wsrv.nl, which can
// reach Bandai on our behalf and serves the response from its own CDN.
// Lowered 5000 -> 2000 on 2026-05-31: when Bandai is actively hot-link-
// blocking the Worker IP, a 5s wait per uncached image stacks to a ~8s
// load (5s dead wait + ~3s wsrv). 2s fails fast to the wsrv fallback so
// blocked-period loads are ~3-4s instead of ~8s. Still ample for a
// healthy Bandai response. Revert toward 5s once the block clears if it
// causes premature wsrv fallback on slow-but-valid responses.
const UPSTREAM_TIMEOUT_MS = 2000;
const WSRV_TIMEOUT_MS = 10000;

async function fetchWithTimeout(url, init = {}, timeoutMs = UPSTREAM_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(id);
  }
}

async function proxyAndCache(url, requestHeaders = {}) {
  const cacheKey = new Request(url);
  const cache = caches.default;
  let cached = await cache.match(cacheKey);
  if (cached) return cached;

  // First try direct upstream (Bandai / TCGPlayer / whatever the caller
  // points at). Short timeout — if it doesn't respond in 5s, drop and try
  // the wsrv.nl fallback. Treat thrown errors and non-200 the same.
  let upstream = null;
  try {
    const res = await fetchWithTimeout(url, { headers: requestHeaders }, UPSTREAM_TIMEOUT_MS);
    if (res.status === 200) upstream = res;
  } catch (_e) {
    upstream = null;
  }

  // Fallback through wsrv.nl. It re-proxies arbitrary URLs through its
  // own CDN and absorbs intermittent upstream hot-link-blocking. We pass
  // output=png so the result matches IMG_HEADERS. maxage=30d to keep the
  // wsrv.nl edge cache warm.
  if (!upstream) {
    const proxied = `https://wsrv.nl/?url=${encodeURIComponent(url)}&output=png&maxage=30d`;
    try {
      const res = await fetchWithTimeout(proxied, {}, WSRV_TIMEOUT_MS);
      if (res.status === 200) upstream = res;
    } catch (_e) {
      upstream = null;
    }
  }

  if (!upstream) return null;
  // A 200 that isn't an image (an HTML soft-404, an error page) must not be
  // relabelled image/png and persisted to R2 forever.
  if (!(upstream.headers.get('content-type') || '').startsWith('image/')) return null;
  return new Response(upstream.body, { headers: IMG_HEADERS });
}

// Card and set ids are [A-Za-z0-9_-] (OP01-001, OP05-119_p8, P-001_jp1,
// DON-208, OP14-EB04, 550302). Anything else — slashes from a decoded %2F,
// dots, '?' — would be spliced into the upstream Bandai URL and the R2 key,
// so reject it before any fetch or write.
const SAFE_ID = /^[A-Za-z0-9_-]{1,40}$/;

// Fetch image bytes via proxyAndCache and reject empty bodies. Bandai (or wsrv
// on its behalf) can hand back a 200 with a 0-byte body when an id is absent on
// that host; caching that empty "success" used to poison R2/edge with a broken
// image. Returns an ArrayBuffer with real bytes, or null.
async function fetchImageBytes(url, referer) {
  const res = await proxyAndCache(url, referer ? { Referer: referer } : {});
  if (!res) return null;
  const buf = await res.arrayBuffer();
  return buf && buf.byteLength > 0 ? buf : null;
}

// Proactively warm a regular OPTCG card image into R2 (used by the cron sweep
// in cron.js). Goes STRAIGHT through wsrv.nl: the Worker's own IP is
// hot-link-blocked by Bandai, so the request-path direct fetch almost always
// dead-waits then falls through to wsrv anyway — skipping it here saves the
// 2s timeout per card and a subrequest. Idempotent (no-op if already in R2),
// never throws. Returns 'cached' | 'warmed' | 'failed'.
export async function warmCardImage(env, cardId) {
  try {
    if (!env?.IMAGES) return 'failed';
    if (await env.IMAGES.head(`cards/${cardId}.png`)) return 'cached';
    const bandai = `https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
    const proxied = `https://wsrv.nl/?url=${encodeURIComponent(bandai)}&output=png&maxage=30d`;
    const res = await fetchWithTimeout(proxied, {}, WSRV_TIMEOUT_MS);
    if (res.status !== 200) return 'failed';
    const buf = await res.arrayBuffer();
    await env.IMAGES.put(`cards/${cardId}.png`, buf, { httpMetadata: { contentType: 'image/png' } });
    return 'warmed';
  } catch {
    return 'failed';
  }
}

export function registerImageRoutes(app) {
  app.get('/images/:card_id', async (c) => {
    const cardId = c.req.param('card_id');
    if (!SAFE_ID.test(cardId)) return c.body(null, 404);
    // Japanese art lives under a separate R2 prefix (cards/ja/:id) and comes
    // from the JA official host. DON!! images are language-neutral synthetic
    // scans, so they ignore ?lang and always use the EN path.
    const lang = (c.req.query('lang') === 'ja' && !cardId.startsWith('DON-')) ? 'ja' : 'en';
    const r2Key = lang === 'ja' ? `cards/ja/${cardId}.png` : `cards/${cardId}.png`;

    // 1. R2 first (high-res curated images, including DON PDFs). Lang-keyed.
    //    Guard against 0-byte objects left by an earlier failed warm — an empty
    //    R2 hit used to serve a 200/0-byte "success" and mask the real image
    //    (this is what made JA-exclusive ids like ST05-015_r1 render blank).
    if (c.env.IMAGES) {
      const r2Object = await c.env.IMAGES.get(r2Key);
      if (r2Object && r2Object.size > 0) {
        return new Response(r2Object.body, { headers: IMG_HEADERS });
      }
    }

    // 1b. JA: proxy the Japanese official scan, persist under cards/ja/:id.
    //     If the JA host has no image at this id (the JA art is identical to
    //     EN, or simply absent), fall through to the EN image below so the JA
    //     binder still renders art — never a broken image. The EN bytes are
    //     cached under the EN key (cards/:id), NOT the JA key, so a future
    //     curated JA scan still wins once it exists.
    if (lang === 'ja') {
      const jaUrl = `https://www.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
      const jaRes = await proxyAndCache(jaUrl, { Referer: 'https://www.onepiece-cardgame.com/' });
      if (jaRes) {
        const buf = await jaRes.arrayBuffer();
        c.executionCtx.waitUntil(
          c.env.IMAGES
            ? c.env.IMAGES.put(r2Key, buf, { httpMetadata: { contentType: 'image/png' } })
            : caches.default.put(new Request(jaUrl), new Response(buf, { headers: IMG_HEADERS })),
        );
        return new Response(buf, { headers: IMG_HEADERS });
      }
      // JA art unavailable → serve the EN R2 object if we already have it.
      if (c.env.IMAGES) {
        const enObj = await c.env.IMAGES.get(`cards/${cardId}.png`);
        if (enObj && enObj.size > 0) return new Response(enObj.body, { headers: IMG_HEADERS });
      }
      // else fall through to the EN upstream block below.
    }

    // 2. DON cards fall back to TCGPlayer CDN (until mapped to R2)
    if (cardId.startsWith('DON-')) {
      const row = await c.env.DB
        .prepare('SELECT tcg_ids FROM cards WHERE id = ?')
        .bind(cardId)
        .first();
      if (!row || !row.tcg_ids) return c.body(null, 404);
      let tcgIds;
      try { tcgIds = JSON.parse(row.tcg_ids); } catch { return c.body(null, 404); }
      if (!tcgIds?.length) return c.body(null, 404);
      const url = `https://tcgplayer-cdn.tcgplayer.com/product/${tcgIds[0]}_in_1000x1000.jpg`;
      const res = await proxyAndCache(url);
      if (res) {
        c.executionCtx.waitUntil(caches.default.put(new Request(url), res.clone()));
        return res;
      }
      return c.body(null, 404);
    }

    // 3. Regular cards proxy from the official site, then PERSIST to R2 so we
    //    only ever fetch each card from Bandai once. R2 is checked first
    //    (step 1 above), so once a card is stored it never touches Bandai
    //    again — this is what prevents the recurring hot-link IP block:
    //    repeat traffic to Bandai drops to ~zero after the first fetch.
    //    Falls back to the ephemeral edge cache only if R2 is unbound.
    //
    //    Try the EN host first; JA-exclusive variants (_pN/_rN alt-art) 404 on
    //    the EN host but exist on the JA host, so fall back to it before giving
    //    up. Whichever wins is cached under the language-neutral EN key — it IS
    //    the canonical art for that id — so the default (no-lang) path serves it
    //    forever after. This is what fixes JA-only cards (and the OPCanvs
    //    character-page placeholders) without the caller needing ?lang=ja.
    const enUrl = `https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
    const jaUrl = `https://www.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
    const buf =
      (await fetchImageBytes(enUrl, 'https://en.onepiece-cardgame.com/')) ||
      (await fetchImageBytes(jaUrl, 'https://www.onepiece-cardgame.com/'));
    if (buf) {
      c.executionCtx.waitUntil(
        c.env.IMAGES
          ? c.env.IMAGES.put(`cards/${cardId}.png`, buf, { httpMetadata: { contentType: 'image/png' } })
          : caches.default.put(new Request(enUrl), new Response(buf, { headers: IMG_HEADERS })),
      );
      return new Response(buf, { headers: IMG_HEADERS });
    }
    return c.body(null, 404);
  });

  // GET /images/set/:set_id?kind=box|logo
  // Set banner art for OPCanvs. Source URLs (Bandai product pages) are stored on
  // sets.box_url / sets.logo_url; we proxy out-of-band through wsrv (Bandai
  // IP-blocks the Worker) and persist to R2 under sets/{kind}/{set_id}.png so
  // each set is only fetched from Bandai once. Same tiered pattern as card art.
  app.get('/images/set/:set_id', async (c) => {
    const setId = c.req.param('set_id');
    if (!SAFE_ID.test(setId)) return c.body(null, 404);
    const kind = c.req.query('kind') === 'logo' ? 'logo' : 'box';
    const r2Key = `sets/${kind}/${setId}.png`;

    if (c.env.IMAGES) {
      const obj = await c.env.IMAGES.get(r2Key);
      if (obj && obj.size > 0) return new Response(obj.body, { headers: IMG_HEADERS });
    }

    // kind is constrained to 'box'|'logo' above, so the column name is safe.
    const row = await c.env.DB
      .prepare(`SELECT ${kind}_url AS url FROM sets WHERE id = ?`)
      .bind(setId)
      .first();
    if (!row || !row.url) return c.body(null, 404);

    const buf = await fetchImageBytes(row.url, 'https://en.onepiece-cardgame.com/');
    if (!buf) return c.body(null, 404);
    c.executionCtx.waitUntil(
      c.env.IMAGES
        ? c.env.IMAGES.put(r2Key, buf, { httpMetadata: { contentType: 'image/png' } })
        : caches.default.put(new Request(row.url), new Response(buf, { headers: IMG_HEADERS })),
    );
    return new Response(buf, { headers: IMG_HEADERS });
  });
}
