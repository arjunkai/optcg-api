const IMG_HEADERS = {
  'Content-Type': 'image/png',
  'Cache-Control': 'public, max-age=86400',
  'Access-Control-Allow-Origin': '*',
};

async function proxyAndCache(url, requestHeaders = {}) {
  const cacheKey = new Request(url);
  const cache = caches.default;
  let response = await cache.match(cacheKey);
  if (response) return response;

  const upstream = await fetch(url, { headers: requestHeaders });
  if (upstream.status !== 200) return null;

  response = new Response(upstream.body, { headers: IMG_HEADERS });
  return response;
}

export function registerImageRoutes(app) {
  app.get('/images/:card_id', async (c) => {
    const cardId = c.req.param('card_id');

    // 1. R2 first (high-res curated images, including DON PDFs)
    if (c.env.IMAGES) {
      const r2Object = await c.env.IMAGES.get(`cards/${cardId}.png`);
      if (r2Object) {
        return new Response(r2Object.body, { headers: IMG_HEADERS });
      }
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

    // 3. Regular cards proxy from official site
    const url = `https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
    const res = await proxyAndCache(url, { Referer: 'https://en.onepiece-cardgame.com/' });
    if (res) {
      c.executionCtx.waitUntil(caches.default.put(new Request(url), res.clone()));
      return res;
    }
    return c.body(null, 404);
  });
}
