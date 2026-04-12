export function registerImageRoutes(app) {
  app.get('/images/:card_id', async (c) => {
    const cardId = c.req.param('card_id');
    const url = `https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;

    // Check Cloudflare cache first
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

      // Store in Cloudflare edge cache
      c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()));
    }

    return response;
  });
}
