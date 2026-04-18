export function registerDocsRoutes(app) {
  // OpenAPI spec
  app.get('/openapi.json', (c) => {
    return c.json({
      openapi: '3.0.0',
      info: {
        title: 'OPTCG API',
        version: '1.0.0',
        description:
          '**Free REST API for One Piece TCG card data + TCGPlayer prices.**\n\n' +
          'Cards, sets, DON cards, prices, filters, and image proxying. Built for ' +
          '[OPBindr](https://opbindr.com) and the OPTCG community.\n\n' +
          '- **4,566 cards + 195 DON cards**, ~99.6% priced weekly\n' +
          '- Prices aggregated from TCGPlayer, dotgg.gg, and web search fallback. Each card has a `price_source` field so you can see where its price came from.\n' +
          '- Pagination follows the [Pokemon TCG API](https://pokemontcg.io/) convention (`page` / `page_size`)\n' +
          '- Set/base card IDs are uppercase (`OP01-001`, `ST01-001`); variant suffixes are lowercase (`OP05-119_p8`, `OP05-119_r1`)\n' +
          '- DON cards use synthetic IDs `DON-001` through `DON-195` with `category=Don`\n' +
          '- Image proxy adds CORS headers so you can use card art directly in the browser',
      },
      servers: [{ url: '/' }],
      tags: [
        { name: 'Info', description: 'API status and metadata.' },
        { name: 'Sets', description: 'Browse booster packs and starter decks.' },
        { name: 'Cards', description: 'Search, filter, and retrieve individual cards.' },
        { name: 'Images', description: 'Proxy card artwork from the official site.' },
      ],
      components: {
        schemas: {
          Card: {
            type: 'object',
            properties: {
              id: { type: 'string', example: 'OP01-001' },
              base_id: { type: 'string', nullable: true, description: 'For parallels, the base card id' },
              parallel: { type: 'boolean' },
              variant_type: { type: 'string', nullable: true, enum: ['alt_art', 'reprint', 'manga', 'serial', null] },
              name: { type: 'string' },
              rarity: { type: 'string' },
              category: { type: 'string', enum: ['Leader', 'Character', 'Event', 'Stage', 'Don'] },
              finish: { type: 'string', nullable: true },
              image_url: { type: 'string' },
              colors: { type: 'array', items: { type: 'string' }, nullable: true },
              cost: { type: 'integer', nullable: true },
              power: { type: 'integer', nullable: true },
              counter: { type: 'integer', nullable: true },
              attributes: { type: 'array', items: { type: 'string' }, nullable: true },
              types: { type: 'array', items: { type: 'string' }, nullable: true },
              effect: { type: 'string', nullable: true },
              trigger: { type: 'string', nullable: true },
              price: { type: 'number', nullable: true, description: 'Market price in USD' },
              tcg_ids: { type: 'array', items: { type: 'integer' }, nullable: true, description: 'TCGPlayer product IDs' },
              price_updated_at: { type: 'integer', nullable: true, description: 'Unix timestamp of last refresh' },
              price_source: {
                type: 'string',
                nullable: true,
                enum: ['tcgplayer', 'dotgg', 'manual', 'web_tcgplayer', 'web_cardmarket', 'web_pricecharting', 'web_ebay', 'web_tcgking', 'web_collectr', 'web_gamenerdz', 'web_cardkingdom', null],
                description: 'Where this price came from',
              },
            },
          },
        },
      },
      paths: {
        '/': {
          get: {
            tags: ['Info'],
            summary: 'API Info',
            description: 'Returns API name, version, docs URL, and available endpoints.',
            responses: { 200: { description: 'API metadata' } },
          },
        },
        '/sets': {
          get: {
            tags: ['Sets'],
            summary: 'List All Sets',
            description: 'Returns every set ordered newest first.',
            responses: { 200: { description: 'All sets' } },
          },
        },
        '/sets/{set_id}/cards': {
          get: {
            tags: ['Sets'],
            summary: 'Get Cards in a Set',
            description: 'Returns all cards belonging to a specific set.',
            parameters: [{ name: 'set_id', in: 'path', required: true, schema: { type: 'string' } }],
            responses: { 200: { description: 'Cards in set' }, 404: { description: 'Set not found' } },
          },
        },
        '/cards/{card_id}': {
          get: {
            tags: ['Cards'],
            summary: 'Get a Single Card',
            description: 'Returns full card data plus every set the card appears in. Supports variant suffixes like `_p8` or `_r1` (lowercase).',
            parameters: [{ name: 'card_id', in: 'path', required: true, schema: { type: 'string' }, example: 'OP05-119_p8' }],
            responses: {
              200: {
                description: 'Card with sets',
                content: {
                  'application/json': {
                    schema: {
                      allOf: [
                        { $ref: '#/components/schemas/Card' },
                        { type: 'object', properties: { sets: { type: 'array', items: { type: 'object' } } } },
                      ],
                    },
                  },
                },
              },
              404: { description: 'Card not found' },
            },
          },
        },
        '/cards': {
          get: {
            tags: ['Cards'],
            summary: 'Search Cards',
            description: 'Search and filter with pagination.',
            parameters: [
              { name: 'set_id', in: 'query', schema: { type: 'string' }, description: 'Filter by set' },
              { name: 'color', in: 'query', schema: { type: 'string' }, description: 'Red, Blue, Green, Purple, Black, Yellow' },
              { name: 'category', in: 'query', schema: { type: 'string' }, description: 'Leader, Character, Event, Stage, Don' },
              { name: 'rarity', in: 'query', schema: { type: 'string' }, description: 'Leader, Common, Uncommon, Rare, SuperRare, SecretRare' },
              { name: 'name', in: 'query', schema: { type: 'string' }, description: 'Partial match on card name or types (traits like "East Blue", "Straw Hat Crew")' },
              { name: 'parallel', in: 'query', schema: { type: 'boolean' }, description: 'true=parallel only, false=base only' },
              { name: 'variant_type', in: 'query', schema: { type: 'string' }, description: 'alt_art, reprint, manga, serial' },
              { name: 'min_power', in: 'query', schema: { type: 'integer' }, description: 'Min power' },
              { name: 'max_power', in: 'query', schema: { type: 'integer' }, description: 'Max power' },
              { name: 'min_cost', in: 'query', schema: { type: 'integer' }, description: 'Min cost' },
              { name: 'max_cost', in: 'query', schema: { type: 'integer' }, description: 'Max cost' },
              { name: 'min_price', in: 'query', schema: { type: 'number' }, description: 'Min market price (USD)' },
              { name: 'max_price', in: 'query', schema: { type: 'number' }, description: 'Max market price (USD)' },
              { name: 'sort', in: 'query', schema: { type: 'string', enum: ['id', 'name', 'price', 'power', 'cost'] }, description: 'Sort field (default: id)' },
              { name: 'order', in: 'query', schema: { type: 'string', enum: ['asc', 'desc'] }, description: 'Sort direction (default: asc)' },
              { name: 'page', in: 'query', schema: { type: 'integer', default: 1 }, description: 'Page number' },
              { name: 'page_size', in: 'query', schema: { type: 'integer', default: 50 }, description: 'Results per page (max 500)' },
            ],
            responses: {
              200: {
                description: 'Paginated card results',
                content: {
                  'application/json': {
                    schema: {
                      type: 'object',
                      properties: {
                        count: { type: 'integer' },
                        totalCount: { type: 'integer' },
                        page: { type: 'integer' },
                        pageSize: { type: 'integer' },
                        data: { type: 'array', items: { $ref: '#/components/schemas/Card' } },
                      },
                    },
                  },
                },
              },
            },
          },
        },
        '/images/{card_id}': {
          get: {
            tags: ['Images'],
            summary: 'Proxy Card Image',
            description: 'Fetches card image from official site with CORS headers and 24h cache.',
            parameters: [{ name: 'card_id', in: 'path', required: true, schema: { type: 'string' } }],
            responses: { 200: { description: 'PNG image' }, 404: { description: 'Image not found' } },
          },
        },
      },
    });
  });

  // Scalar docs UI
  app.get('/docs', (c) => {
    return c.html(`<!DOCTYPE html>
<html>
<head>
    <title>OPTCG API Docs</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
</head>
<body>
    <script id="api-reference" data-url="/openapi.json"></script>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body>
</html>`);
  });
}
