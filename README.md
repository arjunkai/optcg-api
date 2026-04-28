# OPTCG API

A REST API for the One Piece Trading Card Game. Provides card and set data for all 4,347+ cards across 51 sets, plus DON cards and TCGPlayer market prices, with filtering, pagination, and multi-set support for binder apps.

**Live API:** `https://optcg-api.arjunbansal-ai.workers.dev`  
**Docs:** `https://optcg-api.arjunbansal-ai.workers.dev/docs`

---

## Endpoints

### Cards
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/cards` | All cards with filters |
| HEAD | `/cards` | Same as GET but headers only (for uptime checks) |
| GET | `/cards/all` | Single-shot dump of every card. Edge-cached for 1h via the Workers Cache API. Pass `?refresh=1` to purge the cached entry and re-run the D1 query (used by the image-refresh script after an upload). |
| GET | `/cards/{id}` | Single card by ID |
| GET | `/cards/{id}/price-history` | Historical prices, one point per weekly snapshot. Optional `?range=1m\|3m\|6m\|1y\|all` (default `1y`). |

### Sets
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/sets` | All sets |
| HEAD | `/sets` | Same as GET but headers only (for uptime checks) |
| GET | `/sets/{id}/cards` | All cards in a set |

### Images
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/images/{card_id}` | Card image with CORS headers. Lookup order: Cloudflare R2 bucket `optcg-images/cards/{id}.png` first (curated high-res scans for DON cards and JP-exclusive variants), then TCGPlayer CDN for uncurated DONs, then `en.onepiece-cardgame.com` as the last resort for regular cards. Served with `Cache-Control: public, max-age=86400`. |

---

## Filters (`GET /cards`)

| Parameter | Type | Example | Description |
|-----------|------|---------|-------------|
| `set_id` | string | `OP-01` | Filter by set |
| `color` | string | `Red` | Filter by color |
| `category` | string | `Leader` | Leader, Character, Event, Stage, Don |
| `rarity` | string | `SuperRare` | Common, Uncommon, Rare, SuperRare, SecretRare, Leader |
| `name` | string | `Luffy` | Partial match on card name or types (traits like `East Blue`, `Straw Hat Crew`) |
| `parallel` | boolean | `true` | true = parallel only, false = base only |
| `variant_type` | string | `manga` | alt_art, reprint, manga, serial |
| `min_power` | int | `5000` | Minimum power |
| `max_power` | int | `9000` | Maximum power |
| `min_cost` | int | `1` | Minimum cost |
| `max_cost` | int | `5` | Maximum cost |
| `min_price` | number | `1.00` | Minimum market price (USD) |
| `max_price` | number | `100.00` | Maximum market price (USD) |
| `sort` | string | `price` | Sort field: `id`, `name`, `price`, `power`, `cost` |
| `order` | string | `desc` | Sort direction: `asc`, `desc` |
| `page` | int | `1` | Page number |
| `page_size` | int | `50` | Results per page (max 500) |

---

## Example Requests

```
GET /cards?color=Red&category=Leader
GET /cards?name=Luffy&min_power=5000
GET /cards?name=East%20Blue
GET /cards?name=Straw%20Hat%20Crew&color=Red
GET /cards?set_id=OP-01&rarity=SecretRare
GET /cards?variant_type=manga
GET /cards/OP01-001
GET /sets/OP-01/cards
```

## Example Response (`GET /cards/OP01-001`)

```json
{
  "id": "OP01-001",
  "name": "Roronoa Zoro",
  "rarity": "Leader",
  "category": "Leader",
  "colors": ["Red"],
  "cost": 5,
  "power": 5000,
  "counter": null,
  "attributes": ["Slash"],
  "types": ["Supernovas", "Straw Hat Crew"],
  "effect": "[DON!! x1] [Your Turn] All of your Characters gain +1000 power.",
  "trigger": null,
  "parallel": false,
  "variant_type": null,
  "image_url": "https://en.onepiece-cardgame.com/images/cardlist/card/OP01-001.png",
  "price": 1.23,
  "tcg_ids": [482196],
  "price_updated_at": 1776432551,
  "price_source": "tcgplayer",
  "sets": [{ "id": "OP-01", "label": "BOOSTER PACK -ROMANCE DAWN- [OP-01]" }]
}
```

### Prices & DON Cards

- Every priced card has `price` (USD), `tcg_ids` (array of TCGPlayer product IDs), `price_updated_at` (unix timestamp), and `price_source` for provenance.
- DON cards have synthetic IDs `DON-001` through `DON-195` and `category: "Don"`. Filter with `?category=Don`.
- DON `image_url` points at `/images/DON-NNN` on this API. The image route checks a Cloudflare R2 bucket first (curated scans), then falls back to TCGPlayer CDN for uncurated cards. Curated + uncurated DONs use the same URL.
- Prices refresh weekly via the scheduled GitHub Actions workflow (Mondays 6am UTC).

### JP-exclusive variants

A handful of card IDs like `P-001_jp1`, `P-003_jp1`, `ST03-008_jp1` are JP-exclusive Championship prize variants that the official site (`en.onepiece-cardgame.com`) does not publish. They are seeded from `data/jp_exclusives.json` via `scripts/import-jp-exclusives.js`, inherit stats from their base card, and follow the `{base_id}_jpN` suffix convention so they never collide with Bandai's own `_pN`/`_rN` parallel IDs.

Images for these variants come from real eBay seller photos uploaded to R2 at `optcg-images/cards/{id}.png`. The image endpoint serves them the same way it serves DON card images. Prices come from `scripts/price_jp_exclusives.py` (eBay Browse API, consensus-of-3 with trimmed median, stamps `price_source='ebay_jp'`).

### Price sources

Prices are aggregated from multiple sources. Every row is tagged with the source it came from, so you can filter or audit.

| `price_source` | Description | Priority |
|---|---|---|
| `manual` | Pinned override from `data/manual_prices.json` (always wins) | Highest |
| `manual_jp` | Seed price for JP-exclusive variants from `data/jp_exclusives.json`. Replaced by `ebay_jp` when the pricing script finds a consensus. | Highest |
| `web_tcgplayer`, `web_cardmarket`, `web_pricecharting`, `web_ebay`, etc. | Firecrawl web-search fallback for cards the primary sources don't cover | High |
| `tcgplayer` | Scraped from TCGPlayer price guides. Default source for ~90% of cards | Medium |
| `dotgg` | Fetched from api.dotgg.gg as a fallback | Low |
| `ebay` | eBay Browse API gap-fill for cards still null after the other sources. Title-filtered and 20%-trimmed median across 3+ listings. | Low |
| `ebay_jp` | eBay Browse API run specifically for JP-exclusive variants, using each entry's `note` or `image_search_query` as the search. | Low |

Each higher-priority source skips rows already populated by a higher tier on re-runs, so a refresh never clobbers a manual override.

---

## Data Coverage

- **4,566 unique cards** across **51 sets** + **195 DON cards**
- Booster packs OP-01 through OP-15
- Starter decks ST-01 through ST-29
- Extra Boosters, Premium Boosters, Promos
- Parallel/alt-art cards tracked with `base_id` reference
- Variant types auto-classified: `alt_art`, `reprint`, `manga`, `serial`
- Cards appearing in multiple sets fully supported via junction table
- **~99.6% price coverage** via TCGPlayer + dotgg.gg + Firecrawl web-search + manual overrides, refreshed weekly

---

## Tech Stack

- **Card scraper:** Python + Playwright (official site)
- **Price scraper:** Python + Playwright (TCGPlayer), Firecrawl (web search fallback)
- **Variant classifier:** httpx + Limitless TCG
- **Database:** Cloudflare D1 (SQLite)
- **API:** Hono (JavaScript) on Cloudflare Workers
- **CI:** GitHub Actions, weekly cron
- **Hosting:** Cloudflare Workers (zero cold starts)

---

## Running Locally

```bash
git clone https://github.com/arjunkai/optcg-api.git
cd optcg-api
npm install
npm run dev
```

Open `http://localhost:8787/docs`

---

## Code, Data, and Access

**Code.** The Worker code in this repo is MIT-licensed. Clone it, modify it, deploy your own instance against your own Cloudflare account, D1 database, and R2 bucket. See `LICENSE` for the full text.

**Card data.** Card data is derived from public upstream sources: Bandai's official [One Piece Card Game website](https://en.onepiece-cardgame.com/cardlist/), TCGPlayer price guides, dotgg.gg, and eBay listings. The MIT license on the code does not grant any rights to the data itself. If you need a card-data API for your own service, run the scrape pipeline in `scraper.py` and `scripts/` against the same upstream sources rather than reusing this project's D1 contents.

**Card images.** Card art is © Eiichiro Oda / Shueisha, Toei Animation, Bandai Namco Entertainment Inc. The `/images/*` endpoint is a proxy. No rights to the images are claimed by this project.

**Deployed API access.** `https://optcg-api.arjunbansal-ai.workers.dev` is gated to opbindr.com origins (and a small allowlist of approved partners). Browser callers from non-allowed origins receive `403 origin not allowed`; non-browser callers without a valid `X-API-Key` receive `401 api key required`. Public endpoints (`/`, `/docs`, `/openapi.json`, and `/images/*`) stay open so the API stays discoverable and binder thumbnails shared on Discord or Twitter still render. Non-commercial development access is available on request: open an issue or email arjun@neuroplexlabs.com.