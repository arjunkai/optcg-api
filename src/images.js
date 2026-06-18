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
  return new Response(upstream.body, { headers: IMG_HEADERS });
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
    // Japanese art lives under a separate R2 prefix (cards/ja/:id) and comes
    // from the JA official host. DON!! images are language-neutral synthetic
    // scans, so they ignore ?lang and always use the EN path.
    const lang = (c.req.query('lang') === 'ja' && !cardId.startsWith('DON-')) ? 'ja' : 'en';
    const r2Key = lang === 'ja' ? `cards/ja/${cardId}.png` : `cards/${cardId}.png`;

    // 1. R2 first (high-res curated images, including DON PDFs). Lang-keyed.
    if (c.env.IMAGES) {
      const r2Object = await c.env.IMAGES.get(r2Key);
      if (r2Object) {
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
        if (enObj) return new Response(enObj.body, { headers: IMG_HEADERS });
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

    // 3. Regular cards proxy from official site, then PERSIST to R2 so we
    //    only ever fetch each card from Bandai once. R2 is checked first
    //    (step 1 above), so once a card is stored it never touches Bandai
    //    again — this is what prevents the recurring hot-link IP block:
    //    repeat traffic to Bandai drops to ~zero after the first fetch.
    //    Falls back to the ephemeral edge cache only if R2 is unbound.
    const url = `https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
    const res = await proxyAndCache(url, { Referer: 'https://en.onepiece-cardgame.com/' });
    if (res) {
      const buf = await res.arrayBuffer();
      c.executionCtx.waitUntil(
        c.env.IMAGES
          ? c.env.IMAGES.put(`cards/${cardId}.png`, buf, { httpMetadata: { contentType: 'image/png' } })
          : caches.default.put(new Request(url), new Response(buf, { headers: IMG_HEADERS })),
      );
      return new Response(buf, { headers: IMG_HEADERS });
    }
    return c.body(null, 404);
  });
}
