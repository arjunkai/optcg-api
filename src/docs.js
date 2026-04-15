export function registerDocsRoutes(app) {
  // OpenAPI spec
  app.get('/openapi.json', (c) => {
    return c.json({
      openapi: '3.0.0',
      info: {
        title: 'OPTCG API',
        version: '1.0.0',
        description:
          '**Free REST API for One Piece TCG card data.**\n\n' +
          'Cards, sets, filters, and image proxying — built for ' +
          '[OPBindr](https://opbindr.com) and the OPTCG community.\n\n' +
          '- Pagination follows the [Pokemon TCG API](https://pokemontcg.io/) convention (`page` / `pageSize`)\n' +
          '- All card IDs are uppercase (e.g. `OP01-001`, `ST01-001`)\n' +
          '- Image proxy adds CORS headers so you can use card art directly in the browser',
      },
      servers: [{ url: '/' }],
      tags: [
        { name: 'Info', description: 'API status and metadata.' },
        { name: 'Sets', description: 'Browse booster packs and starter decks.' },
        { name: 'Cards', description: 'Search, filter, and retrieve individual cards.' },
        { name: 'Images', description: 'Proxy card artwork from the official site.' },
      ],
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
            description: 'Returns full card data plus every set the card appears in.',
            parameters: [{ name: 'card_id', in: 'path', required: true, schema: { type: 'string' } }],
            responses: { 200: { description: 'Card with sets' }, 404: { description: 'Card not found' } },
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
              { name: 'page', in: 'query', schema: { type: 'integer', default: 1 }, description: 'Page number' },
              { name: 'page_size', in: 'query', schema: { type: 'integer', default: 50 }, description: 'Results per page (max 500)' },
            ],
            responses: { 200: { description: 'Paginated card results' } },
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
    <title>OPTCG API — Docs</title>
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
