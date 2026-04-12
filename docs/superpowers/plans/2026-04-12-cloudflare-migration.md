# OPTCG API Cloudflare Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the OPTCG API from Render (FastAPI/Python) to Cloudflare Workers (Hono/JS) + D1 (SQLite), preserving all endpoints, query params, and response formats.

**Architecture:** Hono web framework on Cloudflare Workers handles routing and JSON responses. D1 SQLite stores cards/sets/card_sets. Array fields (colors, attributes, types) stored as JSON strings, parsed to arrays in responses. Image proxy uses Workers fetch + Cache API. Import script generates SQL and executes via Wrangler CLI.

**Tech Stack:** Cloudflare Workers, Hono, D1 (SQLite), Wrangler CLI

---

### Task 1: Initialize Cloudflare Worker project

**Files:**
- Create: `wrangler.toml`
- Create: `src/index.js`
- Create: `package.json`

- [ ] **Step 1: Create `package.json`**

```json
{
  "name": "optcg-api",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "wrangler dev",
    "deploy": "wrangler deploy"
  },
  "dependencies": {
    "hono": "^4.0.0"
  },
  "devDependencies": {
    "wrangler": "^3.0.0"
  }
}
```

- [ ] **Step 2: Create `wrangler.toml`**

```toml
name = "optcg-api"
main = "src/index.js"
compatibility_date = "2024-01-01"

[[d1_databases]]
binding = "DB"
database_name = "optcg-cards"
database_id = "" # filled after creation
```

- [ ] **Step 3: Create minimal `src/index.js`**

```js
import { Hono } from 'hono';
import { cors } from 'hono/cors';

const app = new Hono();

app.use('*', cors({
  origin: '*',
  allowMethods: ['GET', 'HEAD'],
  allowHeaders: ['*'],
}));

app.get('/', (c) => {
  return c.json({
    name: 'OPTCG API',
    version: '1.0.0',
    docs: '/docs',
    endpoints: [
      'GET /sets',
      'GET /sets/{id}/cards',
      'GET /cards',
      'GET /cards/{id}',
      'GET /images/{card_id}',
    ],
  });
});

export default app;
```

- [ ] **Step 4: Install dependencies**

Run: `npm install`
Expected: `node_modules` created, `hono` and `wrangler` installed

- [ ] **Step 5: Create D1 database**

Run: `npx wrangler d1 create optcg-cards`
Expected: Output includes `database_id`. Copy it into `wrangler.toml`.

- [ ] **Step 6: Test locally**

Run: `npm run dev`
Expected: Worker starts on `http://localhost:8787`. Visiting `/` returns the JSON info object.

- [ ] **Step 7: Commit**

```bash
git add package.json wrangler.toml src/index.js
git commit -m "feat: initialize Cloudflare Worker project with Hono"
```

---

### Task 2: Create D1 schema and import script

**Files:**
- Create: `schema.sql`
- Create: `scripts/import-d1.js`

- [ ] **Step 1: Create `schema.sql`**

```sql
DROP TABLE IF EXISTS card_sets;
DROP TABLE IF EXISTS cards;
DROP TABLE IF EXISTS sets;

CREATE TABLE sets (
  id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL,
  label TEXT NOT NULL,
  card_count INTEGER NOT NULL
);

CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  base_id TEXT,
  parallel INTEGER NOT NULL DEFAULT 0,
  variant_type TEXT,
  name TEXT NOT NULL,
  rarity TEXT,
  category TEXT,
  image_url TEXT,
  colors TEXT,
  cost INTEGER,
  power INTEGER,
  counter INTEGER,
  attributes TEXT,
  types TEXT,
  effect TEXT,
  trigger_text TEXT
);

CREATE TABLE card_sets (
  card_id TEXT NOT NULL REFERENCES cards(id),
  set_id TEXT NOT NULL REFERENCES sets(id),
  pack_id TEXT,
  PRIMARY KEY (card_id, set_id)
);

CREATE INDEX idx_cards_category ON cards(category);
CREATE INDEX idx_cards_rarity ON cards(rarity);
CREATE INDEX idx_cards_parallel ON cards(parallel);
CREATE INDEX idx_cards_variant_type ON cards(variant_type);
CREATE INDEX idx_card_sets_set_id ON card_sets(set_id);
CREATE INDEX idx_card_sets_card_id ON card_sets(card_id);
```

- [ ] **Step 2: Apply schema to D1**

Run: `npx wrangler d1 execute optcg-cards --file=schema.sql --remote`
Expected: Tables and indexes created.

- [ ] **Step 3: Create `scripts/import-d1.js`**

This script reads `data/cards.json` and `data/sets.json`, applies variant_type overrides, generates a SQL file, and executes it against D1 via Wrangler.

```js
/**
 * import-d1.js — reads data/cards.json + data/sets.json, generates SQL,
 * and imports into D1 via Wrangler CLI.
 *
 * Usage: node scripts/import-d1.js
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { execFileSync } from 'child_process';

const DATA_DIR = 'data';

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return String(val);
  if (typeof val === 'boolean') return val ? '1' : '0';
  return `'${String(val).replace(/'/g, "''")}'`;
}

function jsonOrNull(val) {
  if (val === null || val === undefined) return 'NULL';
  return escSql(JSON.stringify(val));
}

// ── Load data ──────────────────────────────────────────────────────────────
const sets = JSON.parse(readFileSync(`${DATA_DIR}/sets.json`, 'utf-8'));
const cards = JSON.parse(readFileSync(`${DATA_DIR}/cards.json`, 'utf-8'));

// Apply variant_type overrides
const overridePath = `${DATA_DIR}/variant_types.json`;
if (existsSync(overridePath)) {
  const overrides = JSON.parse(readFileSync(overridePath, 'utf-8'));
  for (const card of cards) {
    if (overrides[card.id]) {
      card.variant_type = overrides[card.id];
    }
  }
  console.log(`Applied ${Object.keys(overrides).length} variant_type overrides`);
}

// ── Generate SQL ───────────────────────────────────────────────────────────
const lines = [];

// Sets
for (const s of sets) {
  lines.push(
    `INSERT INTO sets (id, pack_id, label, card_count) VALUES (${escSql(s.set_id)}, ${escSql(s.pack_id)}, ${escSql(s.label)}, ${escSql(s.count)}) ON CONFLICT(id) DO UPDATE SET pack_id=excluded.pack_id, label=excluded.label, card_count=excluded.card_count;`
  );
}

// Cards
for (const c of cards) {
  lines.push(
    `INSERT INTO cards (id, base_id, parallel, variant_type, name, rarity, category, image_url, colors, cost, power, counter, attributes, types, effect, trigger_text) VALUES (${escSql(c.id)}, ${escSql(c.base_id)}, ${c.parallel ? 1 : 0}, ${escSql(c.variant_type)}, ${escSql(c.name)}, ${escSql(c.rarity)}, ${escSql(c.category)}, ${escSql(c.image_url)}, ${jsonOrNull(c.colors)}, ${escSql(c.cost)}, ${escSql(c.power)}, ${escSql(c.counter)}, ${jsonOrNull(c.attributes)}, ${jsonOrNull(c.types)}, ${escSql(c.effect)}, ${escSql(c.trigger)}) ON CONFLICT(id) DO UPDATE SET name=excluded.name, variant_type=excluded.variant_type, rarity=excluded.rarity, category=excluded.category, image_url=excluded.image_url, colors=excluded.colors, cost=excluded.cost, power=excluded.power, counter=excluded.counter, attributes=excluded.attributes, types=excluded.types, effect=excluded.effect, trigger_text=excluded.trigger_text;`
  );
}

// Card-set relationships
for (const c of cards) {
  lines.push(
    `INSERT INTO card_sets (card_id, set_id, pack_id) VALUES (${escSql(c.id)}, ${escSql(c.set_id)}, ${escSql(c.pack_id)}) ON CONFLICT(card_id, set_id) DO NOTHING;`
  );
}

const sqlFile = 'scripts/import.sql';
writeFileSync(sqlFile, lines.join('\n'), 'utf-8');
console.log(`Generated ${lines.length} SQL statements -> ${sqlFile}`);

// ── Execute against D1 ────────────────────────────────────────────────────
console.log('Importing to D1 (remote)...');
execFileSync('npx', ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${sqlFile}`, '--remote'], { stdio: 'inherit' });
console.log('Done!');
```

- [ ] **Step 4: Import existing data**

Run: `node scripts/import-d1.js`
Expected: All sets, cards, and card_sets imported to D1. Output shows counts.

- [ ] **Step 5: Verify data in D1**

Run: `npx wrangler d1 execute optcg-cards --command="SELECT COUNT(*) FROM cards" --remote`
Expected: ~4346 rows

Run: `npx wrangler d1 execute optcg-cards --command="SELECT COUNT(*) FROM sets" --remote`
Expected: ~51 rows

- [ ] **Step 6: Commit**

```bash
git add schema.sql scripts/import-d1.js
git commit -m "feat: add D1 schema and import script"
```

---

### Task 3: Implement sets and cards endpoints

**Files:**
- Create: `src/db.js`
- Create: `src/sets.js`
- Create: `src/cards.js`
- Modify: `src/index.js`

- [ ] **Step 1: Create `src/db.js`**

Helper to parse D1 rows — converts JSON string fields to arrays and `parallel` from 0/1 to boolean.

```js
/**
 * Parse a card row from D1 — JSON string fields to arrays, parallel to boolean
 */
export function parseCard(row) {
  if (!row) return null;
  return {
    ...row,
    parallel: Boolean(row.parallel),
    colors: row.colors ? JSON.parse(row.colors) : null,
    attributes: row.attributes ? JSON.parse(row.attributes) : null,
    types: row.types ? JSON.parse(row.types) : null,
    trigger: row.trigger_text,
    trigger_text: undefined,
  };
}

/**
 * Parse an array of card rows
 */
export function parseCards(rows) {
  return rows.map(parseCard);
}
```

- [ ] **Step 2: Create `src/sets.js`**

```js
import { parseCards } from './db.js';

export function registerSetRoutes(app) {
  // GET /sets — all sets ordered newest first
  app.get('/sets', async (c) => {
    const { results } = await c.env.DB.prepare(
      'SELECT * FROM sets ORDER BY pack_id DESC'
    ).all();
    return c.json({ count: results.length, data: results });
  });

  // GET /sets/:set_id/cards — cards in a specific set
  app.get('/sets/:set_id/cards', async (c) => {
    const setId = c.req.param('set_id').toUpperCase();

    const set = await c.env.DB.prepare(
      'SELECT * FROM sets WHERE id = ?'
    ).bind(setId).first();

    if (!set) return c.json({ detail: `Set '${setId}' not found` }, 404);

    const { results } = await c.env.DB.prepare(`
      SELECT c.* FROM cards c
      JOIN card_sets cs ON cs.card_id = c.id
      WHERE cs.set_id = ?
      ORDER BY c.id
    `).bind(setId).all();

    return c.json({ set, count: results.length, data: parseCards(results) });
  });
}
```

- [ ] **Step 3: Create `src/cards.js`**

```js
import { parseCard, parseCards } from './db.js';

export function registerCardRoutes(app) {
  // GET /cards/:card_id — single card with sets
  app.get('/cards/:card_id', async (c) => {
    const cardId = c.req.param('card_id').toUpperCase();

    const card = await c.env.DB.prepare(
      'SELECT * FROM cards WHERE id = ?'
    ).bind(cardId).first();

    if (!card) return c.json({ detail: `Card '${cardId}' not found` }, 404);

    const { results: sets } = await c.env.DB.prepare(`
      SELECT s.* FROM sets s
      JOIN card_sets cs ON cs.set_id = s.id
      WHERE cs.card_id = ?
      ORDER BY s.pack_id
    `).bind(cardId).all();

    return c.json({ ...parseCard(card), sets });
  });

  // GET /cards — search with filters + pagination
  app.get('/cards', async (c) => {
    const q = c.req.query();
    const conditions = [];
    const params = [];

    if (q.set_id) {
      conditions.push('EXISTS (SELECT 1 FROM card_sets cs WHERE cs.card_id = c.id AND cs.set_id = ?)');
      params.push(q.set_id.toUpperCase());
    }

    if (q.color) {
      conditions.push("EXISTS (SELECT 1 FROM json_each(c.colors) WHERE json_each.value = ?)");
      params.push(q.color.charAt(0).toUpperCase() + q.color.slice(1).toLowerCase());
    }

    if (q.category) {
      conditions.push('c.category = ? COLLATE NOCASE');
      params.push(q.category);
    }

    if (q.rarity) {
      conditions.push('c.rarity = ? COLLATE NOCASE');
      params.push(q.rarity);
    }

    if (q.name) {
      conditions.push("c.name LIKE ? COLLATE NOCASE");
      params.push(`%${q.name}%`);
    }

    if (q.parallel !== undefined) {
      conditions.push('c.parallel = ?');
      params.push(q.parallel === 'true' ? 1 : 0);
    }

    if (q.variant_type) {
      conditions.push('c.variant_type = ? COLLATE NOCASE');
      params.push(q.variant_type);
    }

    if (q.min_power) {
      conditions.push('c.power >= ?');
      params.push(Number(q.min_power));
    }

    if (q.max_power) {
      conditions.push('c.power <= ?');
      params.push(Number(q.max_power));
    }

    if (q.min_cost) {
      conditions.push('c.cost >= ?');
      params.push(Number(q.min_cost));
    }

    if (q.max_cost) {
      conditions.push('c.cost <= ?');
      params.push(Number(q.max_cost));
    }

    const page = Math.max(1, Number(q.page) || 1);
    const pageSize = Math.min(500, Math.max(1, Number(q.page_size) || 50));
    const offset = (page - 1) * pageSize;

    const where = conditions.length ? 'WHERE ' + conditions.join(' AND ') : '';

    const countRow = await c.env.DB.prepare(
      `SELECT COUNT(*) AS total FROM cards c ${where}`
    ).bind(...params).first();

    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c ${where} ORDER BY c.id LIMIT ? OFFSET ?`
    ).bind(...params, pageSize, offset).all();

    return c.json({
      count: results.length,
      totalCount: countRow.total,
      page,
      pageSize,
      data: parseCards(results),
    });
  });
}
```

- [ ] **Step 4: Update `src/index.js` to wire up routes**

```js
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { registerSetRoutes } from './sets.js';
import { registerCardRoutes } from './cards.js';

const app = new Hono();

app.use('*', cors({
  origin: '*',
  allowMethods: ['GET', 'HEAD'],
  allowHeaders: ['*'],
}));

app.get('/', (c) => {
  return c.json({
    name: 'OPTCG API',
    version: '1.0.0',
    docs: '/docs',
    endpoints: [
      'GET /sets',
      'GET /sets/{id}/cards',
      'GET /cards',
      'GET /cards/{id}',
      'GET /images/{card_id}',
    ],
  });
});

registerSetRoutes(app);
registerCardRoutes(app);

export default app;
```

- [ ] **Step 5: Test locally**

Run: `npm run dev`

Test:
- `http://localhost:8787/sets` — returns all sets
- `http://localhost:8787/sets/OP01/cards` — returns OP01 cards
- `http://localhost:8787/cards/OP01-001` — returns single card with sets
- `http://localhost:8787/cards?name=Luffy&page_size=5` — returns filtered results
- `http://localhost:8787/cards?color=Red&parallel=false` — color filter works
- `http://localhost:8787/cards?set_id=OP01&category=Leader` — combined filters

Verify response format matches current API exactly (same field names, same types, arrays not strings).

- [ ] **Step 6: Commit**

```bash
git add src/db.js src/sets.js src/cards.js src/index.js
git commit -m "feat: implement sets and cards endpoints on D1"
```

---

### Task 4: Implement image proxy and docs endpoints

**Files:**
- Create: `src/images.js`
- Create: `src/docs.js`
- Modify: `src/index.js`

- [ ] **Step 1: Create `src/images.js`**

```js
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
```

- [ ] **Step 2: Create `src/docs.js`**

```js
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
              { name: 'name', in: 'query', schema: { type: 'string' }, description: 'Partial name search' },
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
```

- [ ] **Step 3: Update `src/index.js`**

```js
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { registerSetRoutes } from './sets.js';
import { registerCardRoutes } from './cards.js';
import { registerImageRoutes } from './images.js';
import { registerDocsRoutes } from './docs.js';

const app = new Hono();

app.use('*', cors({
  origin: '*',
  allowMethods: ['GET', 'HEAD'],
  allowHeaders: ['*'],
}));

app.get('/', (c) => {
  return c.json({
    name: 'OPTCG API',
    version: '1.0.0',
    docs: '/docs',
    endpoints: [
      'GET /sets',
      'GET /sets/{id}/cards',
      'GET /cards',
      'GET /cards/{id}',
      'GET /images/{card_id}',
    ],
  });
});

registerSetRoutes(app);
registerCardRoutes(app);
registerImageRoutes(app);
registerDocsRoutes(app);

export default app;
```

- [ ] **Step 4: Test locally**

Run: `npm run dev`

Test:
- `http://localhost:8787/docs` — Scalar docs page loads
- `http://localhost:8787/openapi.json` — returns OpenAPI spec JSON
- `http://localhost:8787/images/OP01-001` — returns PNG image (note: Cache API won't work locally, but fetch should)

- [ ] **Step 5: Commit**

```bash
git add src/images.js src/docs.js src/index.js
git commit -m "feat: add image proxy and Scalar docs endpoints"
```

---

### Task 5: Deploy Worker and update opbindr

**Files:**
- Modify (opbindr): `src/hooks/useCardCache.jsx`
- Modify (opbindr): `src/components/BinderFeedCard.jsx`
- Modify (opbindr): `src/components/LandingHero.jsx`
- Modify (opbindr): `src/pages/Resources.jsx`

- [ ] **Step 1: Deploy the Worker**

Run: `npx wrangler deploy`
Expected: Deployed to `https://optcg-api.<your-subdomain>.workers.dev`. Note the URL.

- [ ] **Step 2: Verify deployed API**

Test all endpoints against the live URL:
- `https://optcg-api.<subdomain>.workers.dev/` — API info
- `https://optcg-api.<subdomain>.workers.dev/sets` — all sets
- `https://optcg-api.<subdomain>.workers.dev/cards?name=Luffy&page_size=3` — search
- `https://optcg-api.<subdomain>.workers.dev/cards/OP01-001` — single card
- `https://optcg-api.<subdomain>.workers.dev/images/OP01-001` — image proxy
- `https://optcg-api.<subdomain>.workers.dev/docs` — Scalar docs

Compare response format against current Render API to confirm they match.

- [ ] **Step 3: Update opbindr API URL references**

In `c:\Users\arjun\OneDrive\Documents\projects\opbindr`, update these 4 files to replace `https://optcg-api-rm6b.onrender.com` with the new Workers URL:

**`src/hooks/useCardCache.jsx` (lines 4-5):**
```js
const API_BASE = import.meta.env.DEV
  ? '/optcg-api'
  : 'https://optcg-api.<subdomain>.workers.dev';
```

**`src/components/BinderFeedCard.jsx` (line 59):**
Replace `https://optcg-api-rm6b.onrender.com` with the new Workers URL.

**`src/components/LandingHero.jsx` (line 156):**
Replace `https://optcg-api-rm6b.onrender.com` with the new Workers URL.

**`src/pages/Resources.jsx` (line 17):**
Replace `https://optcg-api-rm6b.onrender.com/docs` with `https://optcg-api.<subdomain>.workers.dev/docs`.

- [ ] **Step 4: Test opbindr locally**

Run `npm run dev` in the opbindr directory. Verify:
- Card search in AddCardsModal works (calls /cards endpoint)
- Card images load on the explore feed
- Landing page hero images load
- Resources page API Docs link points to new URL

- [ ] **Step 5: Commit opbindr changes**

```bash
cd /path/to/opbindr
git add src/hooks/useCardCache.jsx src/components/BinderFeedCard.jsx src/components/LandingHero.jsx src/pages/Resources.jsx
git commit -m "feat: switch API from Render to Cloudflare Workers"
```

- [ ] **Step 6: Commit optcg-api deployment**

```bash
cd /path/to/optcg-api
git add -A
git commit -m "feat: deploy OPTCG API to Cloudflare Workers"
```

---

### Task 6: Update GitHub Actions scraper workflow

**Files:**
- Modify: `.github/workflows/scrape.yml`

- [ ] **Step 1: Update `.github/workflows/scrape.yml`**

```yaml
name: Weekly Card Scrape

on:
  schedule:
    - cron: '0 6 * * 1' # Every Monday at 6am UTC
  workflow_dispatch: # Allow manual trigger

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install Python dependencies
        run: |
          pip install -r requirements.txt
          pip install playwright
          playwright install chromium

      - name: Install Node dependencies
        run: npm install

      - name: Run scraper
        run: python scraper.py

      - name: Classify variants
        run: python classify_variants.py

      - name: Import to D1
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
        run: node scripts/import-d1.js
```

- [ ] **Step 2: Add GitHub secrets**

Go to GitHub repo Settings > Secrets > Actions, add:
- `CLOUDFLARE_API_TOKEN` — create an API token at dash.cloudflare.com with D1 edit + Workers edit permissions
- `CLOUDFLARE_ACCOUNT_ID` — your Cloudflare account ID (found in dashboard URL or Workers overview)

- [ ] **Step 3: Test workflow manually**

Go to GitHub Actions > "Weekly Card Scrape" > "Run workflow" > trigger manually.
Expected: Workflow runs successfully, scrapes cards, imports to D1.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/scrape.yml
git commit -m "feat: update scraper workflow to import to D1"
```

---

### Task 7: Cleanup — decommission Render

- [ ] **Step 1: Verify everything works end-to-end**

Check:
- opbindr.com card search works
- opbindr.com card images load
- API docs accessible at new URL
- All endpoints return correct data
- Image proxy works with caching

- [ ] **Step 2: Delete the Render service**

Go to Render dashboard > select the optcg-api service > Settings > Delete Service.

- [ ] **Step 3: Remove old Python server files (optional)**

The following files are no longer needed for the API server but may be kept for reference:
- `main.py` — old FastAPI server
- `import.py` — old Supabase import

Keep `requirements.txt` since the scraper still needs it. Optionally remove `main.py` and `import.py`, or leave them.

- [ ] **Step 4: Update README**

Update the README to reflect the new Cloudflare Workers hosting, new base URL, and remove Render references.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: decommission Render, update docs for Cloudflare"
```
