export function registerDocsRoutes(app) {
  // OpenAPI spec. Gated behind X-API-Key (see src/auth.js — /openapi.json
  // is NOT in PUBLIC_EXACT). Keyholders fetch this and import it into
  // their OpenAPI viewer of choice. Public-bypass routes (/, /healthz,
  // /images/*, /pokemon/images/*) are documented here too so keyholders
  // see the whole surface; they're explicitly marked `security: []`.
  app.get('/openapi.json', (c) => {
    const Error401 = {
      description: 'Missing or invalid X-API-Key header.',
      content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' }, example: { error: 'api key required' } } },
    };
    const Error403 = {
      description: 'Key lacks the scope required for this route.',
      content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' }, example: { error: 'scope_required', detail: 'key does not have ptcg access' } } },
    };
    const Error429 = {
      description: 'Per-minute (300/min) or daily (100k/day) limit exceeded. Includes a Retry-After header.',
      content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' }, example: { error: 'rate_limited', detail: 'per-minute cap exceeded (300/min)' } } },
    };

    return c.json({
      openapi: '3.0.0',
      info: {
        title: 'OPTCG API',
        version: '1.0.0',
        description:
          '**Private REST API for One Piece TCG + Pokémon TCG card data and prices.**\n\n' +
          'Powers [OPBindr](https://opbindr.com); access for third-party developers is granted on a per-project basis.\n\n' +
          '## Authentication\n' +
          'Every non-public route requires an `X-API-Key` header. Keys are scoped:\n' +
          '- `optcg` scope grants access to `/sets`, `/cards/*`, `/images/*`.\n' +
          '- `ptcg` scope grants access to `/pokemon/*` routes.\n' +
          '- A key may hold either or both.\n\n' +
          '## Rate limits\n' +
          '- **300 requests / minute** per key (Cloudflare native rate limit, returns 429 with `Retry-After: 60`).\n' +
          '- **100,000 requests / day** per key (UTC midnight reset, returns 429 with `Retry-After` to next midnight).\n' +
          'Cache `/cards/index` and `/pokemon/cards/index` (recommended: 7+ days) so a routine binder render is one slim request rather than thousands of per-card calls.\n\n' +
          '## Conventions\n' +
          '- Pagination on `/cards` follows the [Pokemon TCG API](https://pokemontcg.io/) convention (`page` / `page_size`).\n' +
          '- OPTCG set/base IDs are uppercase (`OP01-001`, `ST01-001`); variant suffixes lowercase (`OP05-119_p8`, `OP05-119_r1`, `OP05-119_jp1`).\n' +
          '- DON cards use synthetic IDs `DON-001` through `DON-195` with `category=Don`.\n' +
          '- PTCG endpoints require a `lang` query (`en` default, also `ja`, `zh-cn`, `zh-tw`).\n' +
          '- Image proxies serve CORS-friendly bytes from R2.',
      },
      servers: [{ url: '/' }],
      tags: [
        { name: 'Info', description: 'API status and metadata.' },
        { name: 'OPTCG · Sets', description: 'One Piece booster packs and starter decks.' },
        { name: 'OPTCG · Cards', description: 'One Piece card search, detail, history, image proxy.' },
        { name: 'PTCG · Sets', description: 'Pokémon sets per language.' },
        { name: 'PTCG · Cards', description: 'Pokémon card search, detail, image proxy.' },
      ],
      components: {
        securitySchemes: {
          ApiKeyAuth: {
            type: 'apiKey',
            in: 'header',
            name: 'X-API-Key',
            description: 'Issued per-project. Contact arjun@opbindr.com to request access.',
          },
        },
        schemas: {
          Error: {
            type: 'object',
            properties: {
              error: { type: 'string', example: 'api key required' },
              detail: { type: 'string', nullable: true, example: 'per-minute cap exceeded (300/min)' },
            },
            required: ['error'],
          },
          Card: {
            type: 'object',
            description: 'One Piece TCG card. Returned by /cards, /cards/{id}, /sets/{id}/cards.',
            properties: {
              id: { type: 'string', example: 'OP01-001' },
              base_id: { type: 'string', nullable: true, description: 'For parallels, the base card id.' },
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
              price: { type: 'number', nullable: true, description: 'Market price in USD.' },
              tcg_ids: { type: 'array', items: { type: 'integer' }, nullable: true, description: 'TCGPlayer product IDs.' },
              price_updated_at: { type: 'integer', nullable: true, description: 'Unix timestamp of last refresh.' },
              price_source: {
                type: 'string',
                nullable: true,
                enum: ['tcgplayer', 'dotgg', 'manual', 'web_tcgplayer', 'web_cardmarket', 'web_pricecharting', 'web_ebay', 'web_tcgking', 'web_collectr', 'web_gamenerdz', 'web_cardkingdom', null],
                description: 'Where this price came from.',
              },
            },
          },
          CardSlim: {
            type: 'object',
            description: 'Reduced shape returned by /cards/index — drops effect text, image_url, tcg_ids, and set membership so a full-catalog fetch is ~80% smaller. Use /cards/{id} for the heavy fields.',
            properties: {
              id: { type: 'string', example: 'OP01-001' },
              name: { type: 'string' },
              category: { type: 'string' },
              rarity: { type: 'string' },
              colors: { type: 'array', items: { type: 'string' }, nullable: true },
              attributes: { type: 'array', items: { type: 'string' }, nullable: true },
              types: { type: 'array', items: { type: 'string' }, nullable: true },
              cost: { type: 'integer', nullable: true },
              power: { type: 'integer', nullable: true },
              parallel: { type: 'boolean' },
              variant_type: { type: 'string', nullable: true },
              finish: { type: 'string', nullable: true },
              price: { type: 'number', nullable: true },
              price_source: { type: 'string', nullable: true },
              dominant_color: { type: 'string', nullable: true, description: 'Hex color reserved for placeholder rendering; currently null.' },
            },
          },
          Set: {
            type: 'object',
            properties: {
              id: { type: 'string', example: 'OP01' },
              name: { type: 'string' },
              pack_id: { type: 'integer', nullable: true },
              release_date: { type: 'string', nullable: true },
            },
          },
          PokemonCard: {
            type: 'object',
            description: 'Pokémon TCG card (slim shape). Returned by /pokemon/cards/index and /pokemon/sets/{id}/cards. The detail endpoint /pokemon/cards/{id} additionally spreads the raw TCGdex payload (effect, abilities, attacks).',
            properties: {
              id: { type: 'string', example: 'sv1-1' },
              lang: { type: 'string', enum: ['en', 'ja', 'zh-cn', 'zh-tw'] },
              set_id: { type: 'string', example: 'sv1' },
              local_id: { type: 'string', example: '1' },
              name: { type: 'string' },
              name_en: { type: 'string', nullable: true, description: 'EN-name alias for JA rows so latin queries hit Japanese cards. Null on EN/zh-* rows.' },
              category: { type: 'string', example: 'Pokemon' },
              rarity: { type: 'string', nullable: true },
              hp: { type: 'integer', nullable: true },
              retreat: { type: 'integer', nullable: true },
              types: { type: 'array', items: { type: 'string' } },
              stage: { type: 'string', nullable: true },
              variants: { type: 'object', additionalProperties: true, description: 'Map of variant flags from TCGdex (e.g. holo, reverse).' },
              image_high: { type: 'string', nullable: true },
              image_low: { type: 'string', nullable: true },
              pricing: {
                type: 'object',
                description: 'Slim pricing — manual price + TCGplayer (holofoil/normal/reverseHolofoil .market) + Cardmarket (avg/trend/avg7/avg30/avg1/low). The detail endpoint returns the full pricing object.',
                additionalProperties: true,
              },
              price_source: { type: 'string', nullable: true },
              dominant_color: { type: 'string', nullable: true },
            },
          },
          PokemonSet: {
            type: 'object',
            properties: {
              id: { type: 'string', example: 'sv1' },
              lang: { type: 'string' },
              name: { type: 'string' },
              series: { type: 'string', nullable: true },
              release_date: { type: 'string', nullable: true },
              card_count_total: { type: 'integer', nullable: true },
              card_count_official: { type: 'integer', nullable: true },
              logo_url: { type: 'string', nullable: true },
              symbol_url: { type: 'string', nullable: true },
            },
          },
        },
      },
      security: [{ ApiKeyAuth: [] }],
      paths: {
        '/': {
          get: {
            tags: ['Info'],
            summary: 'API Info',
            description: 'API name, version, docs URL, and available endpoints. Public.',
            security: [],
            responses: { 200: { description: 'API metadata' } },
          },
        },
        '/healthz': {
          get: {
            tags: ['Info'],
            summary: 'Health Check',
            description: 'Liveness probe. Public.',
            security: [],
            responses: { 200: { description: 'OK' } },
          },
        },
        '/sets': {
          get: {
            tags: ['OPTCG · Sets'],
            summary: 'List All OPTCG Sets',
            description: 'Every One Piece set ordered newest first. Requires `optcg` scope.',
            responses: {
              200: {
                description: 'All sets',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/Set' } },
                } } } },
              },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/sets/{set_id}/cards': {
          get: {
            tags: ['OPTCG · Sets'],
            summary: 'Get Cards in an OPTCG Set',
            description: 'All cards belonging to one set. Requires `optcg` scope.',
            parameters: [{ name: 'set_id', in: 'path', required: true, schema: { type: 'string' }, example: 'OP01' }],
            responses: {
              200: {
                description: 'Cards in set',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/Card' } },
                } } } },
              },
              401: Error401,
              403: Error403,
              404: { description: 'Set not found' },
              429: Error429,
            },
          },
        },
        '/cards/index': {
          get: {
            tags: ['OPTCG · Cards'],
            summary: 'Slim OPTCG Card Index',
            description:
              '**Recommended bootstrap endpoint.** Returns every card in the slim shape (drops effect text, image URLs, set membership, TCGPlayer IDs) — roughly 80% smaller than `/cards/all`. Use `/cards/{card_id}` to hydrate the heavy fields when a user opens a card.\n\n' +
              'Edge-cached for 1 hour with 24-hour stale-while-revalidate. **Clients should also cache locally for 7+ days** to stay well under the daily quota. Requires `optcg` scope.',
            parameters: [
              { name: 'refresh', in: 'query', schema: { type: 'string', enum: ['1'] }, description: 'Set to `1` to bypass the edge cache (debug/ops use only — counts against your daily quota).' },
            ],
            responses: {
              200: {
                description: 'Slim card list',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/CardSlim' } },
                } } } },
              },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/cards/all': {
          get: {
            tags: ['OPTCG · Cards'],
            summary: 'Full OPTCG Card Dump',
            description:
              'Every card in the full shape (heavy — ~5x the size of `/cards/index`). Prefer `/cards/index` for routine sync; this endpoint exists for debugging and one-off bulk exports. Edge-cached identically. Requires `optcg` scope.',
            parameters: [
              { name: 'refresh', in: 'query', schema: { type: 'string', enum: ['1'] }, description: 'Bypass edge cache.' },
            ],
            responses: {
              200: {
                description: 'Full card list',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/Card' } },
                } } } },
              },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/cards/{card_id}': {
          get: {
            tags: ['OPTCG · Cards'],
            summary: 'Get a Single OPTCG Card',
            description: 'Full card data plus every set the card appears in. Supports variant suffixes like `_p8`, `_r1`, `_jp1` (lowercase). Requires `optcg` scope.',
            parameters: [{ name: 'card_id', in: 'path', required: true, schema: { type: 'string' }, example: 'OP05-119_p8' }],
            responses: {
              200: {
                description: 'Card with sets',
                content: {
                  'application/json': {
                    schema: {
                      allOf: [
                        { $ref: '#/components/schemas/Card' },
                        { type: 'object', properties: { sets: { type: 'array', items: { $ref: '#/components/schemas/Set' } } } },
                      ],
                    },
                  },
                },
              },
              401: Error401,
              403: Error403,
              404: { description: 'Card not found' },
              429: Error429,
            },
          },
        },
        '/cards/{card_id}/price-history': {
          get: {
            tags: ['OPTCG · Cards'],
            summary: 'Get OPTCG Card Price History',
            description: 'Historical TCGPlayer market prices captured on each weekly refresh. Requires `optcg` scope.',
            parameters: [
              { name: 'card_id', in: 'path', required: true, schema: { type: 'string' }, example: 'OP05-119' },
              { name: 'range', in: 'query', schema: { type: 'string', enum: ['1m', '3m', '6m', '1y', 'all'], default: '1y' } },
            ],
            responses: {
              200: {
                description: 'Price history points in chronological order',
                content: {
                  'application/json': {
                    schema: {
                      type: 'object',
                      properties: {
                        card_id: { type: 'string' },
                        range: { type: 'string' },
                        current_price: { type: 'number', nullable: true },
                        current_updated_at: { type: 'integer', nullable: true },
                        points: {
                          type: 'array',
                          items: {
                            type: 'object',
                            properties: {
                              price: { type: 'number' },
                              t: { type: 'integer', description: 'Unix timestamp in milliseconds' },
                            },
                          },
                        },
                      },
                    },
                  },
                },
              },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/cards': {
          get: {
            tags: ['OPTCG · Cards'],
            summary: 'Search OPTCG Cards',
            description: 'Search and filter with pagination. Requires `optcg` scope.',
            parameters: [
              { name: 'set_id', in: 'query', schema: { type: 'string' }, description: 'Filter by set' },
              { name: 'color', in: 'query', schema: { type: 'string' }, description: 'Red, Blue, Green, Purple, Black, Yellow' },
              { name: 'category', in: 'query', schema: { type: 'string' }, description: 'Leader, Character, Event, Stage, Don' },
              { name: 'rarity', in: 'query', schema: { type: 'string' }, description: 'Leader, Common, Uncommon, Rare, SuperRare, SecretRare' },
              { name: 'name', in: 'query', schema: { type: 'string' }, description: 'Partial match on card name or types (traits like "East Blue", "Straw Hat Crew")' },
              { name: 'parallel', in: 'query', schema: { type: 'boolean' }, description: 'true=parallel only, false=base only' },
              { name: 'variant_type', in: 'query', schema: { type: 'string' }, description: 'alt_art, reprint, manga, serial' },
              { name: 'finish', in: 'query', schema: { type: 'string' }, description: 'Filter by finish (Holo, Foil, etc.)' },
              { name: 'min_power', in: 'query', schema: { type: 'integer' } },
              { name: 'max_power', in: 'query', schema: { type: 'integer' } },
              { name: 'min_cost', in: 'query', schema: { type: 'integer' } },
              { name: 'max_cost', in: 'query', schema: { type: 'integer' } },
              { name: 'min_price', in: 'query', schema: { type: 'number' }, description: 'Min market price (USD)' },
              { name: 'max_price', in: 'query', schema: { type: 'number' }, description: 'Max market price (USD)' },
              { name: 'sort', in: 'query', schema: { type: 'string', enum: ['id', 'name', 'price', 'power', 'cost'] }, description: 'Default: id' },
              { name: 'order', in: 'query', schema: { type: 'string', enum: ['asc', 'desc'] }, description: 'Default: asc' },
              { name: 'page', in: 'query', schema: { type: 'integer', default: 1 } },
              { name: 'page_size', in: 'query', schema: { type: 'integer', default: 50 }, description: 'Max 500' },
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
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/images/{card_id}': {
          get: {
            tags: ['OPTCG · Cards'],
            summary: 'Proxy OPTCG Card Image',
            description: 'Fetches card image from official site or R2 with CORS headers. **Public** — image URLs can be embedded in shared binders, Discord posts, etc.',
            security: [],
            parameters: [{ name: 'card_id', in: 'path', required: true, schema: { type: 'string' }, example: 'OP01-001' }],
            responses: {
              200: { description: 'Image bytes (PNG / JPEG / WebP depending on source)' },
              404: { description: 'Image not found' },
            },
          },
        },
        '/pokemon/sets': {
          get: {
            tags: ['PTCG · Sets'],
            summary: 'List Pokémon Sets',
            description: 'Sets in one language, newest first. Requires `ptcg` scope.',
            parameters: [
              { name: 'lang', in: 'query', schema: { type: 'string', enum: ['en', 'ja', 'zh-cn', 'zh-tw'], default: 'en' } },
            ],
            responses: {
              200: {
                description: 'Sets list',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/PokemonSet' } },
                } } } },
              },
              400: { description: 'Invalid lang' },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/pokemon/sets/{set_id}/cards': {
          get: {
            tags: ['PTCG · Sets'],
            summary: 'Get Cards in a Pokémon Set',
            description: 'Slim cards in one set + language. Requires `ptcg` scope.',
            parameters: [
              { name: 'set_id', in: 'path', required: true, schema: { type: 'string' }, example: 'sv1' },
              { name: 'lang', in: 'query', schema: { type: 'string', enum: ['en', 'ja', 'zh-cn', 'zh-tw'], default: 'en' } },
            ],
            responses: {
              200: {
                description: 'Cards in set',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/PokemonCard' } },
                } } } },
              },
              400: { description: 'Invalid lang' },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/pokemon/cards/index': {
          get: {
            tags: ['PTCG · Cards'],
            summary: 'Slim Pokémon Card Index',
            description:
              '**Recommended bootstrap endpoint for Pokémon.** Returns every card for the chosen language in slim shape. Edge-cached for 1h + 24h stale-while-revalidate. Cache locally for 7+ days. JA queries auto-join EN names for latin-script search. Requires `ptcg` scope.',
            parameters: [
              { name: 'lang', in: 'query', schema: { type: 'string', enum: ['en', 'ja', 'zh-cn', 'zh-tw'], default: 'en' } },
              { name: 'refresh', in: 'query', schema: { type: 'string', enum: ['1'] }, description: 'Bypass edge cache.' },
            ],
            responses: {
              200: {
                description: 'Slim card list',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/PokemonCard' } },
                } } } },
              },
              400: { description: 'Invalid lang' },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/pokemon/cards/all': {
          get: {
            tags: ['PTCG · Cards'],
            summary: 'Full Pokémon Card Dump',
            description: 'Full-shape list (slim + raw TCGdex payload spread). Prefer `/pokemon/cards/index` for routine sync. Requires `ptcg` scope.',
            parameters: [
              { name: 'lang', in: 'query', schema: { type: 'string', enum: ['en', 'ja', 'zh-cn', 'zh-tw'], default: 'en' } },
            ],
            responses: {
              200: {
                description: 'Full card list',
                content: { 'application/json': { schema: { type: 'object', properties: {
                  count: { type: 'integer' },
                  data: { type: 'array', items: { $ref: '#/components/schemas/PokemonCard' } },
                } } } },
              },
              400: { description: 'Invalid lang' },
              401: Error401,
              403: Error403,
              429: Error429,
            },
          },
        },
        '/pokemon/cards/{card_id}': {
          get: {
            tags: ['PTCG · Cards'],
            summary: 'Get a Single Pokémon Card',
            description: 'Full row including raw TCGdex JSON (effect, abilities, attacks). Requires `ptcg` scope.',
            parameters: [
              { name: 'card_id', in: 'path', required: true, schema: { type: 'string' }, example: 'sv1-1' },
              { name: 'lang', in: 'query', schema: { type: 'string', enum: ['en', 'ja', 'zh-cn', 'zh-tw'], default: 'en' } },
            ],
            responses: {
              200: {
                description: 'Card detail',
                content: { 'application/json': { schema: { $ref: '#/components/schemas/PokemonCard' } } },
              },
              400: { description: 'Invalid lang' },
              401: Error401,
              403: Error403,
              404: { description: 'Card not found' },
              429: Error429,
            },
          },
        },
        '/pokemon/images/{lang}/{series}/{set_id}/{local_id}/{filename}': {
          get: {
            tags: ['PTCG · Cards'],
            summary: 'Proxy Pokémon Card Image',
            description: 'CORS-friendly proxy fronting TCGdex assets, cached in R2. Filename pattern: `{quality}.{ext}` where quality is `low` or `high` and ext is `webp`, `png`, or `jpg`. **Public** — used for thumbnails in shared binders.',
            security: [],
            parameters: [
              { name: 'lang', in: 'path', required: true, schema: { type: 'string' }, example: 'en' },
              { name: 'series', in: 'path', required: true, schema: { type: 'string' }, example: 'sv' },
              { name: 'set_id', in: 'path', required: true, schema: { type: 'string' }, example: 'sv1' },
              { name: 'local_id', in: 'path', required: true, schema: { type: 'string' }, example: '1' },
              { name: 'filename', in: 'path', required: true, schema: { type: 'string' }, example: 'high.webp' },
            ],
            responses: {
              200: { description: 'Image bytes' },
              400: { description: 'Bad filename / lang / series / setId / localId' },
              404: { description: 'Image not found upstream' },
            },
          },
        },
      },
    });
  });

  // /docs is intentionally a static "request access" landing page.
  // The real Scalar viewer used to live here and read /openapi.json,
  // but that let anyone browsing the URL enumerate every endpoint,
  // parameter, and response schema without holding a key. Now the
  // OpenAPI document itself is gated behind X-API-Key (see src/auth.js)
  // and keyholders are expected to fetch it via curl / Postman /
  // Insomnia / Scalar-CLI and view it in their own tool of choice.
  app.get('/docs', (c) => {
    return c.html(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OPTCG API — Access Required</title>
  <style>
    :root { color-scheme: dark; }
    body {
      background: #0d0d0d;
      color: #e5e7eb;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      max-width: 560px;
      background: #111;
      border: 1px solid #2a2a2a;
      border-radius: 16px;
      padding: 40px;
    }
    h1 { margin: 0 0 8px; font-size: 24px; }
    p { color: #9ca3af; line-height: 1.6; margin: 12px 0; }
    .cta {
      display: inline-block;
      margin-top: 16px;
      padding: 10px 18px;
      background: #3b82f6;
      color: white;
      text-decoration: none;
      border-radius: 8px;
      font-weight: 600;
    }
    .cta:hover { background: #2563eb; }
    code {
      background: #1a1a1a;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 13px;
      color: #C9A84C;
    }
    .keyholder {
      margin-top: 28px;
      padding-top: 20px;
      border-top: 1px solid #2a2a2a;
      font-size: 14px;
    }
    .keyholder p { font-size: 14px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>OPTCG API</h1>
    <p>A private REST API for One Piece TCG and Pokémon TCG card data and prices. Access is granted on a per-project basis after a short review.</p>
    <a class="cta" href="https://forms.gle/56bcJgdKKSVRzjtA7" target="_blank" rel="noopener noreferrer">Request access</a>
    <div class="keyholder">
      <p><strong>Already have a key?</strong> Fetch the OpenAPI spec with your <code>X-API-Key</code> header and import it into any OpenAPI viewer (Scalar, Swagger UI, Postman, Insomnia).</p>
      <p><code>curl -H "X-API-Key: opt_your_key" https://optcg-api.arjunbansal-ai.workers.dev/openapi.json</code></p>
    </div>
  </div>
</body>
</html>`);
  });
}
