# TCG Price Aggregation Framework

## Summary

A reproducible, multi-source pipeline for sourcing card prices and images
across English and Japanese TCG catalogs. Started as a backfill for OPBindr's
PTCG residual gap; designed to be reusable for any TCG (Pokémon, OP, MTG, etc.)
and releasable as an open-source library.

**Current coverage achieved**:

| Lang | Images | Prices |
|------|--------|--------|
| EN   | 100% (20,679 cards) | 99.94% (13 unpriced) |
| JA   | 100% (24,649 cards) | 72.2% (6,858 unpriced) |

The 6,858 JA unpriced are concentrated in genuinely-rare cards that don't
trade publicly in any market — see [Reality of 100%](#the-reality-of-100).

---

## Architecture

```
                 ┌────────────────────┐
                 │   ptcg_cards (D1)  │
                 │  card_id, set_id,  │
                 │  local_id, name,   │
                 │  pricing_json,     │
                 │  price_source      │
                 └─────────┬──────────┘
                           │
                           │ scoped query
                           ▼
        ┌──────────────────────────────────┐
        │       Source Aggregator          │
        │                                  │
        │ 1. set_id → source mapping       │
        │ 2. fetch products + prices       │
        │ 3. card_id → product matcher     │
        │ 4. variant/subtype price selector│
        │ 5. source priority + write       │
        └──────┬───────────┬────────┬──────┘
               │           │        │
   ┌───────────▼─┐ ┌───────▼──┐ ┌───▼───────────┐
   │ TCGCSV cat 3│ │TCGCSV    │ │PriceCharting  │
   │ (TCGPlayer  │ │cat 85    │ │HTML scrape    │
   │ EN)         │ │(Pokemon  │ │               │
   │             │ │Japan)    │ │               │
   └─────────────┘ └──────────┘ └───────────────┘
   ┌─────────────┐ ┌──────────┐ ┌───────────────┐
   │ Yuyutei     │ │TCGdex    │ │ Bulbagarden   │
   │ HTML scrape │ │REST API  │ │ Archives (img)│
   │ (current JP │ │          │ │               │
   │ inventory)  │ │          │ │               │
   └─────────────┘ └──────────┘ └───────────────┘

                 ┌──────────────────┐
                 │ JA→EN Translator │
                 │ - PokeAPI species│
                 │ - TCGdex /v2/en/ │
                 │ - per-card cache │
                 └──────────────────┘
```

---

## Source Catalog

### Image sources (100% achieved)

| # | Source | Coverage | Free? | Auth? | Notes |
|---|--------|----------|-------|-------|-------|
| 1 | TCGdex API | ~85% catalog baseline | ✓ | none | First fetch; both EN+JA |
| 2 | Bulbagarden Archives MediaWiki | classics + vintage | ✓ | UA only | Per-set categories; ~150 cards/HTTP |
| 3 | TCGCSV/TCGPlayer CDN | trainer kits + modern promos | ✓ | none | Closed final 5% |
| 4 | Bulbapedia wiki pages | per-card disambiguation | ✓ | UA only | Used for cel25 reprints |
| 5 | eBay Browse API | last-resort residuals | ✓ | OAuth | Low yield (~1% on long-tail) |

### Price sources

| # | Source | Coverage | Free? | Auth? | Status |
|---|--------|----------|-------|-------|--------|
| 1 | TCGdex pricing.cardmarket | ~30% all langs | ✓ | none | Pulled via cards-database import |
| 2 | TCGCSV cat 3 (TCGPlayer EN) | 99.94% EN | ✓ | none | tcgcsv.com/tcgplayer/3/{group}/prices |
| 3 | TCGCSV cat 85 (Pokemon Japan) | 58% JA | ✓ | none | Same API, different category |
| 4 | Yuyutei (yuyu-tei.jp) | current JP retail | ✓ | UA only | Stocks current inventory only |
| 5 | PriceCharting | vintage JP + modern fallback | ✓ | UA only | HTML scrape, ~150 cards/page |
| 6 | eBay Browse API | trans-marketplace residuals | ✓ | OAuth | Low yield |
| 7 | Cardmarket direct | could expand cardmarket | — | — | 403 to programmatic; needs paid API |
| 8 | TCG Collector | comprehensive JP DB | — | API auth required | Free tier uses HTML render |
| 9 | JustTCG / PokemonPriceTracker | full JP coverage | semi | API key | Free tier: 100 req/day, 100 days for 10k |

### Sources investigated and ruled out

- **Cardmarket scraping** — 403 across all attempts (Cloudflare bot block)
- **Limitless TCG** — aggregates TCGPlayer/Cardmarket only, no new data
- **TCG Collector HTML** — JS-rendered, prices not in initial HTML
- **Pokellector** — URL routing returns wrong cards from direct deep-links
- **Mercari JP / Yahoo Auctions JP** — possible but require multi-day scraper engineering

---

## Source-by-Source Lessons

### TCGCSV (the winner)

`tcgcsv.com` is a free unauthenticated mirror of TCGPlayer's product catalog.
Two crucial categories for Pokemon:

- `categoryId=3` — Pokemon (English)
- `categoryId=85` — **Pokemon Japan** (this was the unlock)

Endpoints:
- `/tcgplayer/{cat}/groups` — list all sets in a category
- `/tcgplayer/{cat}/{group_id}/products` — cards in a set with productId, name, image
- `/tcgplayer/{cat}/{group_id}/prices` — marketPrice/low/mid/high per subType per product

Image URL: `tcgplayer-cdn.tcgplayer.com/product/{id}_in_1000x1000.jpg` (high-res direct).

**Coverage limit**: Pokemon Japan cat 85 has 446 groups but only goes back to ~2002.
Pre-2002 vintage (1996-1999 BS / Carddass / TopSun) is not in cat 85.

### PriceCharting (vintage closer)

`pricecharting.com/console/pokemon-japanese-{slug}` returns a paginated HTML
table with 150ish cards per set, each with NM ungraded / CIB / PSA 10 prices.

URL pattern: `pokemon-japanese-{slug}` where slug is the set's English-y name
(Mirage Forest, Holon Phantom, etc.). Their slug list (~120 JP sets) is at
`/category/pokemon-cards`.

Parsing approach: regex on `<tr id="product-{n}">` rows. NM ungraded price
is in `<td class="price numeric used_price"><span class="js-price">$X.XX</span>`.

**Coverage limit**: PriceCharting omits sets that don't get listings on eBay,
and JP-set numbering doesn't always match TCGdex (e.g., PCG sets are split
across multiple PC slugs because PC follows actual JP release names while
TCGdex uses the consolidated PCG abbreviation).

### Yuyutei (current JP only)

Yuyutei is a JP retailer. Their per-set page lists current stock with prices.
URL: `yuyu-tei.jp/sell/poc/s/{set_code}` returns HTML. Polite scraping at
~1 sec per set.

**Coverage limit**: They only stock currently-trading sets. Vintage sets
they don't carry return 0 listings. Yields ~1,500 cards across modern JP.

### TCGdex

Multilingual REST API at `api.tcgdex.net/v2/{lang}/cards/{id}`. Full
metadata + images for the cards they have. Pricing in `pricing.cardmarket`
for ~30% of catalog (cardmarket EUR-only, sourced upstream).

---

## Set Mapping Registry

The hardest non-engineering problem is maintaining the cross-reference
between TCGdex set_ids and each external source's set identifier.

### EN sets (cat 3)

Most match by abbreviation directly. 27 EN sets are unmappable in TCGCSV —
all are Pokemon TCG Pocket virtual cards (no physical market). These are
expected to be removed from the catalog.

Manual override map in [scripts/backfill_ptcg_images_tcgcsv.py:SET_TO_GROUP](../scripts/backfill_ptcg_images_tcgcsv.py).

### JA sets (cat 85 + PriceCharting)

Three layers of override:

1. **Auto-discovery by abbreviation** (case-insensitive, hyphen-tolerant):
   - `SVP` (TCGdex) ↔ `SV-P` (TCGCSV) — hyphens stripped before compare
   - `SV2A` ↔ `SV2a` — case-insensitive match

2. **Manual JA_OVERRIDES** in [scripts/backfill_ptcg_prices_tcgcsv.py:JA_OVERRIDES](../scripts/backfill_ptcg_prices_tcgcsv.py)
   covers sets where abbr alone doesn't work:
   - SWSH JP sets: `SWSH4A` → `S4a` (TCGCSV uses S-prefix)
   - SM enhanced packs: `SM1P` → `SM1+`
   - Vintage classics: `PMCG1` → group 23721 (Expansion Pack)

3. **PriceCharting slug map** in [scripts/backfill_ptcg_prices_pricecharting.py:SET_TO_PC_SLUG](../scripts/backfill_ptcg_prices_pricecharting.py)
   covers ~80 JA sets. PriceCharting uses fully-spelled set names rather
   than abbreviations.

---

## Matcher Algorithm

For each card_id in scope:

1. **Set resolution** — look up TCGCSV groupId / PC slug for the set_id
2. **Product fetch** — get all products in that group (cached per-process)
3. **Number alignment** — compare local_id to product number with these
   normalization variants:
   - As-is (`5`)
   - Strip leading alpha (`H05` → `5`)
   - Strip trailing alpha (`5a` → `5`)
   - Letter-suffix translation for special cases (`exu-1` → `A` for Unown)
4. **Name validation** — normalize both card name and product name
   (lowercase, strip spaces/apostrophes/hyphens/periods/ampersands), then:
   - Either: card name is in product name (substring)
   - Or: product name starts with first 5 chars of card name
   - Or: card name starts with first 5 chars of product name
5. **JA→EN translation** (JA only) — replace card name with EN equivalent
   from `data/ja_card_id_to_en_name.json` cache before matching. Fallback
   to canonical JP→EN Pokémon name dict (`data/jp_to_en_pokemon.json`).
6. **Multi-candidate disambiguation** — when multiple products match name+number:
   - Prefer Normal subtype over Holofoil/Reverse Holofoil
   - For JP→EN-aliased sets (Gym Heroes vs Gym Challenge), use group_priority
   - Otherwise take the first valid match

This gets us 100% on most well-mapped sets. Failures are typically:
- TCGCSV product structure has variants we don't have card_ids for (Poke Ball
  Pattern, Master Ball Pattern in SV2a 151)
- JP card has a name our translator can't resolve (sloppy phonetic katakana)

---

## Weekly Scraper Plan

### Schedule

Run weekly (Mondays 06:00 UTC) via GitHub Actions cron. Same cadence as the
existing OPTCG TCGPlayer pipeline.

### Pipeline stages

1. **Cache invalidation** — drop TCGCSV cache files older than 7 days
2. **Source ingestion** (parallel where possible):
   - TCGCSV cat 3 (Pokemon EN): 220 group fetches
   - TCGCSV cat 85 (Pokemon JP): 446 group fetches
   - PriceCharting: 80+ set page fetches
   - Yuyutei: 30+ active sets
3. **Matcher run** — for each card lacking a price newer than 7 days,
   apply the matcher algorithm against the freshly cached source data
4. **Audit gates** (block bad data from reaching D1):
   - Price spike detector: reject if new price > 5x prior or < 0.2x prior
     (unless prior was clearly stale, >30 days)
   - Coverage delta: alert if any set drops >5% from prior week
   - Multi-source agreement: where 2+ sources agree, weight higher
5. **D1 write** — single batched UPDATE per source via wrangler --file
6. **Audit log** — write a per-run report to `data/backfill/runs/YYYY-WW.md`
   showing yields per source, residuals, and any anomalies

### Source priority (when multiple sources have a price)

For EN cards: pokemontcg (TCGdex's tcgplayer field) > ebay_us > tcgplayer (cat 3) > cardmarket
For JA cards: tcgplayer (cat 85) > yuyutei > pricecharting > cardmarket

This priority is encoded in the `price_source` flag and the matcher only
overwrites lower-priority sources with higher-priority ones.

### Estimated weekly cost

- TCGCSV: ~600 HTTP requests/week, free, ~5 minutes wall-time
- PriceCharting: ~80 page fetches/week, free + UA, ~2 minutes
- Yuyutei: ~30 set pages/week, free + UA, ~30 seconds
- D1 writes: ~5,000 UPDATEs/week, well within Workers free tier

---

## Beating Bot Detection For Free (Cardmarket, Yahoo Auctions JP, Mercari JP)

The blocker on Cardmarket / Yahoo Auctions JP / Mercari JP wasn't the data —
it was Cloudflare's TLS-fingerprint bot detection rejecting plain Python
HTTP clients. Standard `urllib`/`requests`/`httpx` use a different TLS
handshake than real browsers, and Cloudflare detects that in ~50ms.

**The free, weekly-sustainable answer is `curl_cffi`.** It's a Python
library that uses libcurl with **real Chrome/Firefox TLS fingerprints**:

```python
from curl_cffi import requests
r = requests.get("https://www.cardmarket.com/en/Pokemon/Products/Singles/Mirage-Forest",
                 impersonate="chrome120")
# Sails past Cloudflare because the JA3 + ALPN + cipher-order matches Chrome 120
```

No API key, no per-request cost, no browser process. Just import and go.
Production scrapers (CryptoCompare, ccxt, scrapy-impersonate) use this
pattern.

### Architecture for free weekly scraping

```
weekly cron (Mondays 06:00 UTC)
   │
   ├─→ TCGCSV        (plain httpx, free)    — modern stuff
   ├─→ PriceCharting (UA spoof, free)        — vintage US-traded
   ├─→ Yuyutei       (UA spoof, free)        — current JP retail
   ├─→ Cardmarket    (curl_cffi chrome120)   — JP cardmarket prices
   ├─→ Yahoo Auctions JP (curl_cffi safari)  — vintage JP sold listings
   └─→ Mercari JP    (curl_cffi chrome120 + GraphQL) — current JP marketplace

   For each source:
      - load cache if present and < 7 days old
      - else: throttled fetch (1-2s between requests)
      - parse with BeautifulSoup or regex
      - emit (card_id, price_usd, source) records

   audit gates → D1 batch write → run report
```

### When to use firecrawl vs curl_cffi

| Tool | Cost | Use case |
|---|---|---|
| `urllib`/`httpx` (plain) | free | Sites without bot detection (TCGCSV, PriceCharting with UA) |
| `curl_cffi` | free | Cloudflare-protected (Cardmarket, YAJ, Mercari, TCG Collector) |
| `playwright` MCP | free but heavy (browser process) | JS-rendered sites that curl_cffi can't crack |
| firecrawl | paid credits | Bootstrap / one-shot research, NOT weekly production |

For our weekly cron, **never use firecrawl as a primary source** — credits
deplete. Use it only for:
1. One-time bootstrap of a new source's set mapping (research mode)
2. Manual investigation of a card we can't auto-resolve
3. Fallback when curl_cffi suddenly stops working (Cloudflare changes)

### Concrete next-build: Yahoo Auctions JP scraper

```python
# scripts/backfill_ptcg_prices_yahooauctions_jp.py
from curl_cffi import requests
from bs4 import BeautifulSoup
import time, json, re

# YAJ closed/sold listings have realized prices — what we want
SEARCH_URL = "https://auctions.yahoo.co.jp/closedsearch/closedsearch"
POKEMON_CATEGORY = 21336  # Pokemon TCG category

def search_card(query: str) -> list[dict]:
    """Search YAJ closed listings for a card. Return realized prices."""
    params = {"p": query, "auccat": POKEMON_CATEGORY, "n": 50, "b": 1}
    r = requests.get(SEARCH_URL, params=params, impersonate="chrome120", timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    listings = []
    for row in soup.select("li.Product"):
        title = row.select_one(".Product__title").get_text(strip=True)
        price = row.select_one(".Product__priceValue")
        if price:
            jpy = int(re.sub(r"\D", "", price.get_text()))
            listings.append({"title": title, "price_jpy": jpy})
    return listings

# Per-card: search "{name} {set_code}", filter by title, take median of last 5 sales
# Throttle: 2s between searches → 22k cards × 2s = ~12 hours wall time, runs over weekend
```

### Mercari JP scraper

Mercari has a GraphQL API — easier than HTML scraping:

```python
# Their public search endpoint: /v2/entities:search
# Cloudflare-protected; curl_cffi handles it
GQL = "https://api.mercari.jp/v2/entities:search"
r = requests.post(GQL, json={"query": {...}, "filterValues": {"pageSize": 30}},
                  impersonate="chrome120")
```

### Cardmarket scraper

Cardmarket actually offers **free CSV downloads of price guides** for
registered users:

```
GET /Files/Pricelist/{set_id}    # JSON dump of set-wide prices
                                  # requires authenticated session cookie
```

That's the right path for Cardmarket — a one-time login session in a
playwright headed browser harvests cookies, then weekly crons re-use the
session via curl_cffi until they expire. Re-harvest cookies monthly.

### Operational cost

All free. The only "cost" is wall-time for the polite-throttle delays:
- TCGCSV: 5 min
- PriceCharting: 2 min
- Yuyutei: 30 sec
- Cardmarket (CSV): 1 min for 100 sets
- Yahoo Auctions JP: ~12 hours (run as background async task)
- Mercari JP: ~3 hours

YAJ + Mercari run weekly during a low-traffic window (Saturday night JST).
Everything writes to D1 with the same audit gates.

### Stop criteria

The pipeline only re-fetches a card if its current price is older than
7 days. After the first full sweep, weekly runs only update ~10-15% of
the catalog (cards that traded in the last week), keeping each run
under 1 hour for everything except the YAJ vintage sweep.

---

## The Reality of 100%

For OPBindr's specific use case, **JA pricing has a hard ceiling around 75-80%**
via free public sources. Cards above that ceiling are:

- **Vintage promos that never trade** (TopSun 1995, JP Vending Machine cards)
- **Pre-constructed deck cards** (Gift Box / Half Deck inserts that aren't sold individually)
- **Regional / convention exclusives** with private collector circles only
- **Test prints / rare promos** that genuinely have no public market

These cards correctly show "no price" in the UI. The honest UX is NULL,
not an invented value.

To push higher requires either:
- Multi-day Yahoo Auctions JP / Mercari JP scraper development
- Paying for JustTCG or PokemonPriceTracker (~$10-30/mo)
- Manual curation for the residual

---

## Releasable as an Open-Source Library

This pipeline could ship as `tcg-price-aggregator`:

```
tcg-price-aggregator/
├── sources/
│   ├── tcgcsv.py        # TCGCSV/TCGPlayer (cat 3 + 85)
│   ├── pricecharting.py # PriceCharting HTML scrape
│   ├── yuyutei.py       # Yuyutei HTML scrape
│   ├── tcgdex.py        # TCGdex REST API
│   └── base.py          # Source interface
├── matchers/
│   ├── pokemon.py       # Pokemon-specific normalization
│   ├── one_piece.py     # One Piece TCG
│   └── base.py          # Matcher interface
├── translators/
│   ├── pokeapi.py       # JP→EN Pokémon names via PokeAPI
│   └── tcgdex.py        # Per-card EN name from TCGdex
├── audit/
│   └── price_gates.py   # Spike detector, coverage alarms
├── pipelines/
│   ├── pokemon_weekly.py
│   └── one_piece_weekly.py
├── data/
│   └── set_mappings/    # Per-game JSON of set_id → source mappings
└── README.md
```

The novel contribution is the **set mapping registry** — months of
research-validated cross-references between TCGdex, TCGCSV, PriceCharting,
and Yuyutei. That's the moat for a price aggregator.

Open questions before release:
- Cardmarket Terms of Service (likely restrict commercial scraping)
- TCGCSV rate limits (currently lenient; commercial use TBD)
- Whether to host the cache (would need infra) or be a library only
