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
- `src/images.js` — `/images/:card_id` proxy. Order: R2 (`cards/{id}.png`) → DON TCGPlayer CDN via D1 `tcg_ids` → official site fallback for regular cards
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
- `scripts/import-jp-exclusives.js` — seeds JP-exclusive Championship variants from `data/jp_exclusives.json` into `cards` + `card_sets`, inheriting stats from the base row
- `scripts/price_jp_exclusives.py` — eBay Browse API pricing for the JP exclusives, uses each entry's `note` or `image_search_query` as the search and stamps `price_source='ebay_jp'`
- `scripts/fetch_card_image.py` — eBay-sourced card images for cards Bandai doesn't publish cleanly (JP exclusives, DON cards). Adaptive card-bounds detection, aspect-aware scoring (`card_area × sharpness × card_fill × aspect_bonus`), blocklist of slabbed/sealed listings, and a `--min-card-px` floor so it never downgrades an existing image. Uploads to R2 at `optcg-images/cards/{id}.png` then calls `/cards/all?refresh=1` to purge the edge cache. Flags: `--all` (all JP exclusives), `--all-dons` (all DON rows from D1), `<card_id>` (single card with D1 fallback), `--dry-run`, `--force`.
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
Synthetic IDs `DON-001` .. `DON-195`, `category='Don'`. Built by deduping TCGPlayer DON rows across 50 sets. Canonical set attribution prefers regular packs over PRB reprint bundles.

**Image serving:** DON `image_url` points at `/images/DON-NNN` on this API. The route checks R2 bucket `optcg-images` first (curated high-res PDF images, key = `cards/DON-NNN.png`), falls back to TCGPlayer CDN via `tcg_ids` lookup in D1 if not in R2. This means curated + uncurated DONs use the same URL and images transparently upgrade once the R2 object exists.

**R2 curation workflow** (run when adding more PDF-sourced DON images):
1. `scripts/curate_don_images.html` — open locally (`python -m http.server 8080` in repo root, then visit `http://localhost:8080/scripts/curate_don_images.html`). Click candidates to map `DON-NNN → don_NNN.png`; export JSON when done.
2. Save exported JSON as `data/don_image_mapping.json`.
3. `node scripts/upload_don_images_r2.js --dry-run` to verify, then without flag to upload.

**Bulk eBay-sourced DON images** (alternative to PDF curation): run `python -m scripts.fetch_card_image --all-dons` to auto-pick the highest-scoring seller photo for every DON. 140/195 DONs had viable photos on the 2026-04-23 batch; the rest stayed on the TCGPlayer fallback because no listing beat the `--min-card-px` floor (default 800k pixels). Re-run any time to try to improve coverage as listings change.

**Important:** `scripts/build_don_cards.py` writes the API proxy URL into `image_url`. Don't revert it to `tcgplayer-cdn.tcgplayer.com` or the weekly refresh will break the proxy behavior.

## Price history
- `card_price_history` table captures one row per card per weekly price refresh. Populated by `scripts/import-prices-d1.js` alongside the existing cards.price UPDATE. Seeded once in `migrations/005_price_history.sql` from `cards.price` at deploy time.
- Served to OPBindr via `GET /cards/{id}/price-history?range=…` (see `src/cards.js`). Schema docs in `src/docs.js`.
- Backfill was attempted via `scripts/backfill_price_history.js` against TCGPlayer's `infinite-api.tcgplayer.com/price/history/{tcg_id}/detailed` endpoint. Endpoint works and returns clean JSON, but rate-limits at AWS ELB after ~10 requests per IP. Script is checked in for future use (see its header) but not currently run. Charts populate forward from the weekly snapshot.

## Pricing
- `price` REAL, `foil_price` REAL (unused), `delta_price`/`delta_7d_price` (future), `tcg_ids` TEXT (JSON array), `price_updated_at` INTEGER, `price_source` TEXT
- Priority chain: **manual > tcgplayer > dotgg > ebay > web**. Each backfill step only fills rows where `price IS NULL`, so first-write-wins. Manual pins via `data/manual_prices.json` and overrides everything (stamps `price_source='manual'`, skipped by every other importer via `AND price_source != 'manual'` or `AND price IS NULL` guards).
- eBay backfill (`scripts/backfill_prices_ebay.py`) uses the shared `scripts/ebay_client.py` — OAuth client-credentials against `api.ebay.com`, searches Browse API per card, applies title-blocklist + consensus-of-3 + 20% trimmed median before writing. Requires `EBAY_APP_ID` and `EBAY_CERT_ID` env vars (GitHub secrets for CI, or in `.env` for local runs).
- JP-exclusive Championship variants (`P-001_jp1` etc.) are seeded from `data/jp_exclusives.json` via `scripts/import-jp-exclusives.js` with a manual `price` floor stamped as `manual_jp`. `scripts/price_jp_exclusives.py` later replaces those with real consensus medians stamped `ebay_jp`. If no consensus is found (too few listings) the `manual_jp` floor stays so the UI never shows null.
- Rollback any source with `UPDATE cards SET price=NULL, tcg_ids=NULL, price_updated_at=NULL, price_source=NULL WHERE price_source='<source>'`
- Parallel mapping heuristic: TCGPlayer label → our `variant_type` via `VARIANT_LABEL_TO_TYPE` in `map_prices_to_cards.py`

## Gotchas
- D1 remote writes batch at 900 statements max; the import scripts handle batching
- Windows `node` crashed mid-batch historically — re-run individual `scripts/import_batch_N.sql` files via `wrangler d1 execute` if Node exits
- Playwright needs `state="attached"` (not default `"visible"`) when waiting for TCGPlayer table cells since they can be below fold
- `--wait-for 5000` on Firecrawl calls — some set pages JS-render the table late
