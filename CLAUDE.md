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
- `scripts/ptcg-fetch.js` / `scripts/ptcg-import-d1.js` — Pokémon TCG bulk import. Fetch caches TCGdex API responses to `data/ptcg_cache/{sets,cards}-{lang}.json`; import generates batched SQL in `scripts/ptcg_batches/` and runs them via `wrangler d1 execute --remote`. Resumable (re-running fetch only fills missing cards). See "Pokémon TCG import" below.

## Pokémon TCG import

Bulk-loads card + set data for all four languages (`en`, `ja`, `zh-cn`, `zh-tw`) into `ptcg_cards` and `ptcg_sets`. Source: TCGdex public API (https://api.tcgdex.net).

**Scope:** ~22k EN cards plus several thousand each in ja/zh-cn/zh-tw — total approaching 88k. Fetch wallclock at concurrency=8 is roughly 30–60 minutes per language. Import (D1 writes) runs in a few minutes once cached.

**Why a Node script vs. a Worker Cron Trigger:** Workers cap at ~15-min wallclock; the initial bulk fetch is hours. The Cron Trigger pattern documented in the multi-game plan is the right shape for the *daily delta* once initial data lands — not for the initial seed.

**Steps:**
```
node scripts/ptcg-fetch.js                          # all 4 langs, ~hours
node scripts/ptcg-import-d1.js                      # all cached langs → D1
```

**Test/partial runs:**
```
node scripts/ptcg-fetch.js --lang=en --set=base1    # 102 cards, seconds
node scripts/ptcg-import-d1.js --lang=en --dry-run  # write SQL, skip D1
```

**Resume:** re-running `ptcg-fetch.js` skips cards already in `data/ptcg_cache/cards-{lang}.json`. The fetch flushes to disk every 200 cards so a crash doesn't lose progress.

**Verify after import:**
```
npx wrangler d1 execute optcg-cards --remote --command "select lang, count(*) from ptcg_cards group by lang"
```

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

## Migration log
- migration 001: card indexes
- migration 002: variant_type column on cards
- migration 003: pricing columns on cards
- migration 004: price_source column on cards
- migration 005: price_history table + seed from cards.price
- migration 006: ptcg_cards and ptcg_sets tables (Pokémon TCG schema)
- migration 007: adds `price_source` to `ptcg_cards`. Backfilled to 'cardmarket' for existing priced rows. Future writes must set this: pokemontcg.io merge sets 'pokemontcg', manual overrides set 'manual'.
- migration 008: fixup for 007 — clears 3 mis-stamped cards (xyp-XY124,
  xyp-XY84, xyp-XY89 all have null prices in their cardmarket object)
  and adds an index on `ptcg_cards(price_source)` matching the OPTCG
  precedent.
- migration 009: adds `retreat INTEGER` to `ptcg_cards`, backfilled from
  the raw TCGdex JSON. Closes a sort-pill gap surfaced by the A.2 code
  review.

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

## pokemontcg-data submodule

`data/pokemontcg-data/` is a git submodule pointing at
[PokemonTCG/pokemon-tcg-data](https://github.com/PokemonTCG/pokemon-tcg-data).
Public JSON dump backing pokemontcg.io. Used as the secondary English
**image** source when TCGdex is missing artwork. The static dump no
longer carries TCGplayer prices (verified 0 / 20,202 cards have
`tcgplayer.prices` as of 2026-04-29) — pricing is deferred to manual
overrides and a possible future live-API pull.

Update with:

    git submodule update --remote data/pokemontcg-data

Or `git pull --recurse-submodules` for combined repo + submodule pull.
The submodule SHA is pinned in this repo so the import is reproducible.
Fresh clones need `git submodule update --init` once.

## PTCG data pipeline — single source of truth

This section is the authoritative reference for what data we pull, from
where, when, and what we do with it. The weekly `ptcg-refresh` workflow
runs everything below in order. Frontend reads `image_high`, `image_low`,
and `pricing_json` straight from D1 via the `/pokemon/cards/index` slim
endpoint.

### Data home

D1 table `ptcg_cards`. Composite primary key `(card_id, lang)`. One row
per card per language. ~37k rows total: EN 23,159 / JA 5,935 / zh-cn 829
/ zh-tw 7,363.

Frontend reads:
- `image_high` / `image_low` — wsrv.nl proxy URLs in front of these
- `pricing_json` — JSON object, sub-keyed by source (`manual`, `tcgplayer`, `cardmarket`)
- `price_source` — flag identifying which source the displayed price came from
- (others: `name`, `rarity`, `hp`, `retreat`, `types_csv`, `variants_json`, `set_id`, `local_id`, `category`, `stage`)

### The 5 sources, by priority

```
        IMAGE                       PRICE
        ──────                      ──────
priority │ source              │ source
───── │ ────                   │ ────
1     │ manual (future R2)     │ manual override (data/ptcg_manual_prices.json)
2     │ flibustier (Pocket)    │ pokemontcg.io live API (TCGplayer USD + Cardmarket EUR)
3     │ pokemontcg-data        │ TCGdex (Cardmarket EUR baked in)
4     │ TCGdex                 │ —
5     │ none → tinted          │ none → PricePill hides
```

Both chains are first-write-wins for IMAGE (COALESCE), refresh-overwrite
for PRICE (live data has to roll). Manual overrides can never be stomped
by automated runs — every script that touches `price_source` checks
`CASE WHEN price_source = 'manual' THEN 'manual' ELSE …`.

#### 1. TCGdex — primary, multi-lang

- Endpoint: `https://api.tcgdex.net`
- Covers EN / JA / zh-cn / zh-tw
- Auth: none, no rate limit documented (we cap concurrency at 8)
- Image URL pattern: `https://assets.tcgdex.net/{lang}/{series}/{set}/{localId}/{quality}.{ext}`
- Pricing: Cardmarket EUR (`pricing.cardmarket.avg/trend/avg7/...`)
- **Sparseness**: EN 94% images / 81% prices baseline, JA 53% / 28%, zh-cn 0% images / 27% prices, zh-tw 28% / 15%. Upstream limitation, not ours.
- Script: `scripts/ptcg-fetch.js` (resumable disk cache at `data/ptcg_cache/`) → `scripts/ptcg-import-d1.js` (UPSERT into ptcg_cards)
- The TCGdex cache is what makes `ptcg-fetch.js` fast on re-run; in CI it's persisted via `actions/cache@v4` (see workflow)

#### 2. pokemontcg-data — English image gap-fill

- Source: git submodule of `github.com/PokemonTCG/pokemon-tcg-data` at `data/pokemontcg-data/`
- Covers EN only (sets/en.json, cards/en/{setId}.json)
- Image URL: `https://images.pokemontcg.io/{setId}/{number}_hires.png`
- **Pricing: NONE.** Verified 0 / 20,202 cards have `tcgplayer.prices` in the static dump. The dump dropped pricing fields some time after 2019; only the live API has them now.
- Script: `scripts/import-pokemontcg-d1.js` — COALESCE-fills `image_high` / `image_low` for cards in mapped sets where TCGdex has no image
- Updated weekly via `git submodule update --remote data/pokemontcg-data`

#### 3. Live `api.pokemontcg.io` (Scrydex) — English prices

- Endpoint: `https://api.pokemontcg.io/v2/cards?q=set.id:{X}&pageSize=250&select=id,tcgplayer,cardmarket`
- Covers EN main TCG (no TCG Pocket, no very recent promos like svp-175+ / mep / mfb)
- Auth: optional `X-Api-Key` header. Free tier: 1,000 req/day, 30/min. With key: 20,000/day. Free signup at the docs site.
- Returns: `tcgplayer.prices.{normal|holofoil|reverseHolofoil|...}.{low,mid,high,market}` USD + `cardmarket.prices.*` EUR
- Script: `scripts/fetch-pokemontcg-prices.js` — bulk-by-set, 165 requests per weekly run, rate-limited client-side at 2s intervals. Stamps `price_source = 'pokemontcg'` (preserving 'manual' rows).
- Optional env: `POKEMONTCG_API_KEY` (read in workflow as a secret of the same name)

#### 3b. pkmnbindr.com JP catalog — Japanese image gap-fill

- Source: pkmnbindr.com hosts a public static JSON catalog at
  `/data/jpNew/cards/{setCode}_ja.json` and `/data/jpNew/sets/sets.json`
  (verified `application/json`, no auth, served via Cloudflare). Each
  card carries a Scrydex image URL — the same Scrydex CDN that powers
  pokemontcg.io.
- We use pkmnbindr only as the **catalog/index** to discover Scrydex
  card IDs; the actual image bandwidth lands on Scrydex's free public
  CDN (`images.scrydex.com/pokemon/{setCode}_ja-{n}/{small|large}`).
  Their JSON files total ~50MB across 160 sets — once weekly is
  negligible load.
- Mapping: `data/ptcg_jp_set_mapping.json` maps TCGdex JA set ids to
  pkmnbindr ids (mostly mechanical: lowercase + `_ja` suffix). 160
  TCGdex JA sets covered; pkmnbindr-only sets not in TCGdex are
  ignored.
- Script: `scripts/import-pkmnbindr-jp-d1.js` — COALESCE-fills
  `image_high` / `image_low` only where TCGdex was null. Skips sets
  pkmnbindr doesn't have (their per-set JSON 404s gracefully — vintage
  pre-SV-era sets like e-card / Legend / PMCG mostly aren't there).
- Polite throttle: 500ms between fetches.

#### 4. flibustier TCG Pocket database — TCG Pocket image gap-fill

- Source: git submodule of `github.com/flibustier/pokemon-tcg-pocket-database` at `data/pokemon-tcg-pocket-database/`. Covers all 19 Pocket sets (A1–B3, PROMO-A/B); we map 15 to TCGdex IDs in `data/ptcg_pocket_set_mapping.json` (the 4 unmapped — A4b, B2b, B3, PROMO-B — are newer than our last TCGdex fetch and start matching automatically once `ptcg-fetch.js` pulls them).
- Image URL pattern: `https://cdn.jsdelivr.net/gh/flibustier/pokemon-tcg-exchange@main/public/images/cards-by-set/{set}/{n}.webp` (predictable filenames in a sibling repo, served via JSDelivr).
- **Pricing: NONE.** TCG Pocket has no secondary-market pricing yet.
- Script: `scripts/import-tcgpocket-d1.js` — COALESCE-fills `image_high` / `image_low` only.
- Updated weekly via `git submodule update --remote data/pokemon-tcg-pocket-database`.

#### 5. Manual overrides — top priority for chase cards

- File: `data/ptcg_manual_prices.json` — `{ "card_id": 99.99 }` keyed by exact `card_id`
- Script: `scripts/import-ptcg-manual-prices.js` — JSON-patches `pricing.manual.price`, flips `price_source` to `'manual'`. Touches all language rows for that card_id.
- Removing an entry from the JSON does NOT unset D1 — there's a wrangler-command rollback breadcrumb in the script header.

### Set mapping (TCGdex ↔ pokemontcg.io)

TCGdex and pokemontcg.io use different set IDs in many cases (e.g.
TCGdex `2011bw` = pokemontcg `mcd11`). The mapping lives in
`data/ptcg_set_mapping.json` and feeds both `import-pokemontcg-d1.js`
and `fetch-pokemontcg-prices.js`.

- Builder: `scripts/build-ptcg-set-mapping.js`. Three passes: identical IDs → exact normalized-name+year → name+year ±1.
- Re-runs preserve hand-curated entries (existing mapping wins).
- Output also lists everything the algorithm couldn't match into `data/ptcg_set_mapping.unmatched.json`.
- **Today's mapping: 165 of 208 TCGdex EN sets covered.** The 43 unmapped ones are TCG Pocket sets (different game; pokemontcg-data scope is main TCG only), DP/HS/BW/XY/SM trainer kits, and recent McDonald's/MEP/promo sets that pokemontcg-data hasn't ingested yet. Cards in unmapped sets keep their TCGdex data — the imports skip them.

### Weekly refresh order

`.github/workflows/ptcg-refresh.yml` — runs Mondays 08:00 UTC (offset
2h from the OPTCG `scrape.yml` to avoid Cloudflare API contention).
TCGdex cache persisted via `actions/cache@v4`. Auto-commits the
pokemontcg-data submodule pointer if it bumped.

```bash
# 1. Restore TCGdex disk cache (CI only — actions/cache)
# 2. Submodule bumps
git submodule update --remote data/pokemontcg-data
git submodule update --remote data/pokemon-tcg-pocket-database

# 3. Fetch TCGdex (incremental — only fills gaps in the disk cache)
node scripts/ptcg-fetch.js

# 4. UPSERT TCGdex data into D1 (writes images/prices for every
#    TCGdex card; non-EN flows through this step alone)
node scripts/ptcg-import-d1.js

# 5. Image gap-fill from pokemontcg-data (COALESCE — main TCG)
node scripts/import-pokemontcg-d1.js

# 6. Image gap-fill from flibustier (COALESCE — TCG Pocket only)
node scripts/import-tcgpocket-d1.js

# 7. Live USD price refresh for EN main TCG
node scripts/fetch-pokemontcg-prices.js

# 8. Manual overrides last (top priority — wins over step 7)
node scripts/import-ptcg-manual-prices.js
```

The order matters: pokemontcg overlays TCGdex; manual overrides win
over both. Run set-mapping rebuild only when a TCGdex or pokemontcg
release adds a new set we don't recognize.

### Verifying coverage

Run these any time to see the live state:

```bash
# Per-language total + image + price coverage
npx wrangler d1 execute optcg-cards --remote --command \
  "SELECT lang, COUNT(*) AS total,
   SUM(CASE WHEN image_high IS NOT NULL THEN 1 ELSE 0 END) AS with_image,
   SUM(CASE WHEN price_source IS NOT NULL THEN 1 ELSE 0 END) AS with_price
   FROM ptcg_cards GROUP BY lang ORDER BY lang"

# Price source breakdown for English
npx wrangler d1 execute optcg-cards --remote --command \
  "SELECT price_source, COUNT(*) FROM ptcg_cards
   WHERE lang='en' GROUP BY price_source"

# Image host breakdown for English
npx wrangler d1 execute optcg-cards --remote --command \
  "SELECT CASE
     WHEN image_high LIKE '%pokemontcg%' THEN 'pokemontcg'
     WHEN image_high LIKE '%tcgdex%' THEN 'tcgdex'
     WHEN image_high IS NULL THEN 'null'
     ELSE 'other' END AS host, COUNT(*) AS n
   FROM ptcg_cards WHERE lang='en' GROUP BY host"
```

After every refresh, also bust the Worker edge cache:

```bash
for lang in en ja zh-cn zh-tw; do
  curl -s -o /dev/null -w "$lang: %{http_code}\n" \
    -H "Origin: http://localhost:5173" \
    "https://optcg-api.arjunbansal-ai.workers.dev/pokemon/cards/index?lang=$lang&refresh=1"
done
```

### Coverage today (2026-04-30)

| Lang | Image | Price |
|---|---|---|
| EN | 22,398 / 23,159 = **96.7%** | 20,125 / 23,159 = **86.9%** (19,069 USD via live API + 1,056 EUR Cardmarket) |
| JA | 3,194 / 5,935 = 53.8% | 1,689 / 5,935 = 28.5% (Cardmarket EUR) |
| zh-cn | 0 / 829 = 0% | 224 / 829 = 27.0% |
| zh-tw | 2,126 / 7,363 = 28.9% | 1,121 / 7,363 = 15.2% |

### Gaps and follow-ups

- **TCG Pocket sets** (A1–B2a + PROMO-A): shipped 2026-04-30 via flibustier submodule. EN with-image jumped 22,398 → 22,557 (97.4%). The 4 newest Pocket sets (A4b, B2b, B3, PROMO-B) start matching after the next TCGdex fetch.
- **Recent promos** (svp-175+, mep, mfb, McDonald's 2023/2024): pokemontcg-data lags TCGdex; will catch up via weekly submodule bumps.
- **Variant suffixes** (`cel25-2A`-style): TCGdex has the row but no image; only ~7 EN cards. Not worth automating.
- **Non-EN coverage**: upstream-limited. No free source has multi-language data at the scale we'd need. Evaluated and skipped: JustTCG (paid), PokemonPriceTracker (paid), Yahoo Auctions/Mercari (scrape effort), pkmncards (forbidden), Bulbapedia (manual). Manual overrides remain the chase-card hatch for any language.
- **Trainer Kit subsets** (`tk-xy-*` etc.): TCGdex breaks them out per-character; pokemontcg-data only has the EX-era kits as `tk1a`/`tk2a`. Mapping isn't 1:1, leaving as-is.

### How to add a new data source

1. Decide where it sits in the priority chain (manual / pokemontcg / TCGdex / new tier).
2. Add a new `price_source` enum value or reuse an existing one. Update the B.3 normalizer in `opbindr/src/lib/normalize/ptcg.js` (`pickPrice`) so the new flag is consumed.
3. Write a fetcher script following the pattern in `scripts/fetch-pokemontcg-prices.js` (rate limit, batch, idempotent UPDATEs, `CASE WHEN price_source = 'manual'` guard).
4. Wire it into `.github/workflows/ptcg-refresh.yml` between the existing steps; manual overrides always run last.
5. Document the source in this section.
