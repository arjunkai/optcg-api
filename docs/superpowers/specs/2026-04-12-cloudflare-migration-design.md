# OPTCG API: Render to Cloudflare Workers + D1 Migration

## Overview

Migrate the OPTCG API from Render (FastAPI/Python + Supabase PostgreSQL) to Cloudflare Workers (Hono/JS + D1 SQLite). Zero cold starts, no memory limits, free tier handles expected traffic.

## Current Architecture

- **Runtime:** Render free tier — FastAPI + uvicorn (Python)
- **Database:** Supabase PostgreSQL (cards, sets, card_sets tables)
- **Scraper:** GitHub Actions weekly — Playwright scrapes official site, imports to Supabase
- **Image proxy:** `/images/{card_id}` fetches from official site, serves with CORS + 24h cache
- **Docs:** Scalar API docs at `/docs`

## New Architecture

- **Runtime:** Cloudflare Worker (JavaScript)
- **Framework:** Hono (lightweight, FastAPI-like routing with middleware)
- **Database:** Cloudflare D1 (SQLite)
- **Image proxy:** Worker fetch + Cloudflare Cache API (automatic edge caching)
- **Docs:** Scalar API docs (same UI, served from Worker)
- **Scraper:** Same GitHub Actions workflow, but import step uses Wrangler CLI to write to D1

## Endpoints (unchanged)

All endpoints keep the same paths, params, and response shapes.

### GET /
Returns API info, version, docs URL, endpoints list.

### GET /sets
Returns all sets ordered by pack_id DESC.

### GET /sets/{set_id}/cards
Returns all cards in a set, ordered by card ID. Case-insensitive set_id.

### GET /cards/{card_id}
Returns a single card with all sets it belongs to. Case-insensitive card_id.

### GET /cards
Search and filter with pagination. Query params:

| Param | Type | Filter |
|-------|------|--------|
| set_id | string | EXISTS subquery on card_sets |
| color | string | JSON array contains (D1 has no native arrays — see Data Model) |
| category | string | LIKE (case-insensitive) |
| rarity | string | LIKE (case-insensitive) |
| name | string | LIKE %name% |
| parallel | boolean | = true/false |
| variant_type | string | LIKE |
| min_power | int | >= |
| max_power | int | <= |
| min_cost | int | >= |
| max_cost | int | <= |
| page | int | default 1 |
| page_size | int | default 50, max 500 |

Response: `{ count, totalCount, page, pageSize, data }`

### GET /images/{card_id}
Proxy image from official site. Uses Cloudflare Cache API for edge caching (24h TTL). Returns PNG with CORS headers.

## Data Model (D1/SQLite)

SQLite doesn't have PostgreSQL's TEXT[] array type. Array fields (colors, attributes, types) are stored as JSON strings.

### cards table
```sql
CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  base_id TEXT,
  parallel INTEGER NOT NULL DEFAULT 0,  -- boolean as 0/1
  variant_type TEXT,
  name TEXT NOT NULL,
  rarity TEXT,
  category TEXT,
  image_url TEXT,
  colors TEXT,      -- JSON array string: '["Red","Yellow"]'
  cost INTEGER,
  power INTEGER,
  counter INTEGER,
  attributes TEXT,  -- JSON array string: '["Slash"]'
  types TEXT,       -- JSON array string: '["Supernovas","Straw Hat Crew"]'
  effect TEXT,
  trigger_text TEXT  -- "trigger" is reserved in SQLite
);
```

### sets table
```sql
CREATE TABLE sets (
  id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL,
  label TEXT NOT NULL,
  card_count INTEGER NOT NULL
);
```

### card_sets table
```sql
CREATE TABLE card_sets (
  card_id TEXT NOT NULL REFERENCES cards(id),
  set_id TEXT NOT NULL REFERENCES sets(id),
  pack_id TEXT,
  PRIMARY KEY (card_id, set_id)
);
```

### Indexes
```sql
CREATE INDEX idx_cards_category ON cards(category);
CREATE INDEX idx_cards_rarity ON cards(rarity);
CREATE INDEX idx_cards_parallel ON cards(parallel);
CREATE INDEX idx_cards_variant_type ON cards(variant_type);
CREATE INDEX idx_card_sets_set_id ON card_sets(set_id);
CREATE INDEX idx_card_sets_card_id ON card_sets(card_id);
```

### Array filtering in SQLite
Color filter uses JSON functions: `EXISTS (SELECT 1 FROM json_each(c.colors) WHERE json_each.value = ?)` instead of PostgreSQL's `= ANY(colors)`.

## API Response Format

Responses parse JSON array strings back into real arrays before returning, so consumers see the same format as before:

```json
{
  "id": "OP01-001",
  "colors": ["Red"],
  "attributes": ["Slash"],
  "types": ["Supernovas", "Straw Hat Crew"],
  "parallel": false,
  ...
}
```

The `parallel` field is converted from 0/1 to boolean. Array fields are parsed from JSON strings to arrays.

## Import Pipeline

The current `import.py` writes to Supabase via psycopg2. The new import script:

1. Reads `data/cards.json` and `data/sets.json` (same scraper output)
2. Applies `data/variant_types.json` overrides (same as before)
3. Converts PostgreSQL arrays to JSON strings
4. Writes to D1 via Wrangler CLI: `wrangler d1 execute <db-name> --file=import.sql`

The import script generates a SQL file with INSERT ... ON CONFLICT DO UPDATE statements, then executes it against D1.

### GitHub Actions changes
- Add Wrangler CLI setup step
- Add `CLOUDFLARE_API_TOKEN` secret
- Replace `python import.py` with the new D1 import step
- Scraper and classifier remain unchanged

## CORS

Same as current: allow all origins for GET/HEAD methods.

## Project Structure

```
optcg-api/
  src/
    index.js          -- Worker entry, Hono app, all routes
    db.js             -- D1 query helpers (fetch, fetchOne)
    cards.js          -- /cards and /cards/{id} route handlers
    sets.js           -- /sets and /sets/{id}/cards route handlers
    images.js         -- /images/{id} proxy handler
    docs.js           -- /docs Scalar page handler
  scripts/
    import-d1.js      -- Generate SQL + import to D1
  wrangler.toml       -- Cloudflare Worker config
  scraper.py          -- Unchanged
  classify_variants.py -- Unchanged
  data/               -- Scraper output (unchanged)
  .github/workflows/
    scrape.yml         -- Updated to import to D1
```

## Migration Steps (high level)

1. Create D1 database via Wrangler
2. Create tables and indexes
3. Build the Worker with all endpoints
4. Import existing card data to D1
5. Deploy Worker
6. Update opbindr frontend to point at new Worker URL
7. Update GitHub Actions to import to D1
8. Verify everything works
9. Decommission Render

## What Stays the Same

- Scraper (scraper.py) — no changes
- Classifier (classify_variants.py) — no changes
- All endpoint paths and query parameters
- All response formats
- Public API with Scalar docs
- Weekly GitHub Actions scrape schedule

## What Changes

- Runtime: Python/FastAPI → JavaScript/Hono on Cloudflare Workers
- Database: Supabase PostgreSQL → Cloudflare D1 (SQLite)
- Array storage: native TEXT[] → JSON strings (transparent to consumers)
- Import: psycopg2 → Wrangler D1 CLI
- Hosting: Render → Cloudflare Workers
- Image caching: manual Cache-Control header → Cloudflare Cache API (edge cached)
