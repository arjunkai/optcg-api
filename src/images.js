export function registerImageRoutes(app) {
  app.get('/images/:card_id', async (c) => {
    const cardId = c.req.param('card_id');

    // Check R2 first (Don cards and any manually uploaded images)
    if (c.env.IMAGES) {
      const r2Key = `cards/${cardId}.png`;
      const r2Object = await c.env.IMAGES.get(r2Key);
      if (r2Object) {
        return new Response(r2Object.body, {
          headers: {
            'Content-Type': 'image/png',
            'Cache-Control': 'public, max-age=86400',
            'Access-Control-Allow-Origin': '*',
          },
        });
      }
    }

    // Fallback: proxy from official site (regular cards)
    const url = `https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;

    const cacheKey = new Request(url);
    const cache = caches.default;
    let response = await cache.match(cacheKey);

    if (!response) {
      const upstream = await fetch(url, {
        headers: { Referer: 'https://en.onepiece-cardgame.com/' },
      });

      if (upstream.status !== 200) {
        return c.body(null, 404);
      }

      response = new Response(upstream.body, {
        headers: {
          'Content-Type': 'image/png',
          'Cache-Control': 'public, max-age=86400',
          'Access-Control-Allow-Origin': '*',
        },
      });

      c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()));
    }

    return response;
  });
}
