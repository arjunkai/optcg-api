# OPTCG API

REST API serving One Piece TCG card + price data, backing OPBindr and public consumers.

## Stack
- **Runtime:** Cloudflare Workers (JavaScript, Hono router)
- **Database:** Cloudflare D1 (SQLite)
- **Scrapers:** Python + Playwright (official site for cards, TCGPlayer price guides for pricing)

Live: `https://optcg-api.arjunbansal-ai.workers.dev`  ·  Docs: `/docs`  ·  OpenAPI: `/openapi.json`

## Critical Rules
- Never hand-edit D1 schema — always add a numbered migration under `migrations/` and re-run `npx wrangler d1 execute optcg-cards --remote --file=migrations/NNN.sql`
- `schema.sql` mirrors a fresh-create state; keep it in sync with each migration
- TCGPlayer is the authoritative price source; don't swap to third-party APIs (dotgg.gg etc.) without a memory-backed reason
- CORS headers live in `src/index.js` — don't strip them from new routes

## Key files
- `src/index.js` — Hono router + CORS, registers all routes
- `src/cards.js` — `/cards` search + `/cards/:id` single-card endpoints
- `src/sets.js` — `/sets` list + `/sets/:id/cards` set-cards
- `src/images.js` — proxies card images from official site with CORS
- `src/docs.js` — OpenAPI spec + Scalar docs page
- `src/db.js` — row → JSON normalization (handles JSON-encoded columns)
- `schema.sql` — full schema snapshot
- `migrations/` — numbered D1 migrations
- `scraper.py` — official site card data scrape → `data/cards.json`
- `classify_variants.py` — manga/serial classification via Limitless TCG
- `scripts/scrape_tcgplayer_prices_pw.py` — Playwright price scrape (primary, no credits)
- `scripts/scrape_tcgplayer_prices.py` — Firecrawl fallback
- `scripts/build_all_prices.py` — parse + map prices → `data/card_prices_all.json`
- `scripts/build_don_cards.py` — deduped DON catalog → `data/don_cards.json`
- `scripts/import-d1.js` / `import-prices-d1.js` / `import-don-d1.js` — batched D1 writes
- `.github/workflows/scrape.yml` — weekly auto-refresh of everything

## Refresh pipeline

Automated: runs every Monday 6am UTC via GitHub Actions. Manual trigger: `gh workflow run "Weekly Card Scrape"`.

Local full refresh:
```
python scraper.py                                  # cards
python classify_variants.py                        # variant types
node scripts/import-d1.js                          # -> D1
.venv/Scripts/python.exe scripts/scrape_tcgplayer_prices_pw.py
python scripts/build_all_prices.py
node scripts/import-prices-d1.js                   # -> D1
python scripts/build_don_cards.py
node scripts/import-don-d1.js                      # -> D1
```

## DON cards
Synthetic IDs `DON-001` .. `DON-195`, `category='Don'`. Built by deduping TCGPlayer DON rows across 50 sets. Canonical set attribution prefers regular packs over PRB reprint bundles. Images currently point at TCGPlayer CDN; R2-hosted high-res migration deferred (see memory `project_pricing_pipeline`).

## Pricing
- `price` REAL, `foil_price` REAL (unused), `delta_price`/`delta_7d_price` (future), `tcg_ids` TEXT (JSON array), `price_updated_at` INTEGER, `price_source` TEXT
- Priority order: **manual > tcgplayer > dotgg**. Manual pins via `data/manual_prices.json`, overrides everything, never clobbered. TCGPlayer refreshes weekly and skips manual rows. dotgg fills whatever TCGPlayer/manual leaves NULL.
- Rollback any source with `UPDATE cards SET price=NULL, tcg_ids=NULL, price_updated_at=NULL, price_source=NULL WHERE price_source='<source>'`
- Parallel mapping heuristic: TCGPlayer label → our `variant_type` via `VARIANT_LABEL_TO_TYPE` in `map_prices_to_cards.py`

## Gotchas
- D1 remote writes batch at 900 statements max; the import scripts handle batching
- Windows `node` crashed mid-batch historically — re-run individual `scripts/import_batch_N.sql` files via `wrangler d1 execute` if Node exits
- Playwright needs `state="attached"` (not default `"visible"`) when waiting for TCGPlayer table cells since they can be below fold
- `--wait-for 5000` on Firecrawl calls — some set pages JS-render the table late
