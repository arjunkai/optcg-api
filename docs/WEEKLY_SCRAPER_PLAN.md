# Weekly Free TCG Price Scraper — Implementation Plan

**Goal**: a Monday-morning cron that updates pricing for ~45,000 Pokemon
TCG cards (EN+JA) using only **free public sources**, no paid API
credits, no surprise bills.

**Current state baseline**: 100% images, 99.9% EN prices, 81% JA prices.
Plan target: push JA to 90%+ by adding two more sources.

## What's already working (no changes needed)

These five sources all run in <10 minutes weekly with plain `httpx` /
browser-UA-spoofed `urllib`. They're free, reliable, and give us the
pieces above.

| Source | Coverage | Mechanism | Free? |
|---|---|---|---|
| TCGCSV cat 3 | 99.9% EN | unauth REST API | ✓ |
| TCGCSV cat 85 | ~60% JA | unauth REST API | ✓ |
| PriceCharting | vintage US-traded JA | UA-spoofed HTML scrape | ✓ |
| Yuyutei | current JP retail | UA-spoofed HTML scrape | ✓ |
| TCGdex (cardmarket EUR) | baseline | unauth REST API | ✓ |

## What blocks us from 100% — and the verified path through

The remaining ~19% JA gap (~4,700 cards) is in:
1. **Vintage classics** that don't trade on US TCGPlayer (eBay/PriceCharting)
2. **Pre-constructed deck cards** that don't trade individually
3. **Modern JP-exclusive promos** outside Yuyutei's current stock
4. **Truly rare** cards that only trade on JP marketplaces

The deepest free sources for JP-only data are:
- **Yahoo Auctions Japan** (sold listings — realized prices)
- **Mercari Japan** (current asks — biggest C2C market)
- **Cardmarket** (EUR-based JP coverage that TCGdex's `pricing.cardmarket` field misses)

### Verified accessibility (tested 2026-05-01)

I tested each with `curl_cffi` (TLS-fingerprint impersonation, free Python lib):

| Source | Status | Notes |
|---|---|---|
| Mercari JP HTML search | ✅ **200 OK**, ¥ prices visible | Direct HTML scrape works with `impersonate="chrome131"` |
| Mercari JP API (GraphQL) | ❌ 400 (version header) | Needs Mercari app version header — minor reverse-engineering |
| Yahoo Auctions JP | ⚠️ 404 status, body 200KB | URL pattern changed since older docs; actual data returned, just need to update parsers |
| Cardmarket | ❌ 403 Cloudflare JS challenge | curl_cffi alone insufficient; needs playwright OR cookie-harvest from a real browser session |

### The architecture

```
┌─────────────────────────────────────┐
│    Sunday 23:00 UTC: scraper run    │
└──────────────┬──────────────────────┘
               │
   ┌───────────┴───────────┐
   │                       │
   ▼                       ▼
[fast tier]            [slow tier]
   │                       │
   ├─ TCGCSV cat 3         ├─ Mercari JP (curl_cffi)
   ├─ TCGCSV cat 85        ├─ Yahoo Auctions JP (curl_cffi, fix URL)
   ├─ PriceCharting        └─ Cardmarket weekly CSV
   │                          (auth cookie harvested
   ├─ Yuyutei                   monthly via playwright,
   │                            cron uses it via curl_cffi)
   ├─ TCGdex (refresh)
   │
   ~10 min wall time      ~3-4 hours wall time
                           (rate-limited polite scraping)
```

### Slow tier deep-dive

**Mercari JP scraper** (`scripts/backfill_ptcg_prices_mercari_jp.py`):
- For each unpriced JA card, search `jp.mercari.com/search?keyword=...`
- Parse HTML with BeautifulSoup, extract median ¥ price from first 10 listings
- Filter results: title must contain card name + set abbreviation
- 2-second delay between searches (polite), retry on 429
- Estimated runtime for 4,700 cards: **~3 hours**
- Estimated yield: 1,500–2,500 cards (cards that actively trade on Mercari)

**Yahoo Auctions JP scraper** (`scripts/backfill_ptcg_prices_yahooauctions_jp.py`):
- YAJ closed-listings ("falled" auctions) show realized prices
- URL pattern needs refresh — recent change broke older scrapers
- Once URL fixed: same flow as Mercari (search → parse → median)
- Estimated runtime for 4,700 cards: **~6 hours**
- Estimated yield: 1,000–2,000 vintage cards

**Cardmarket weekly CSV via session-cookie**:
- Cardmarket offers free CSV per-set price guides to logged-in users
- One-time setup: human user logs in once, captures session cookie
- Weekly cron: re-uses cookie via curl_cffi, downloads each set's CSV
- Cookie expires every ~30-90 days; re-harvest manually when it does
- Estimated runtime for ~50 sets: **~5 minutes**
- Estimated yield: 800–1,500 cards (everything cardmarket has + we don't)

### Rate limit / politeness budget

Per-host throttle: 1 request per 2 seconds. With concurrent runs against
different hosts, total wall time is bound by the slowest source.

Cron schedule: **Sunday 23:00 UTC** (= Monday 08:00 JST = low traffic
on JP sites). Run completes by ~Monday 04:00 UTC (Monday 13:00 JST).

### Cost ceiling

Zero. Stack is:
- `curl_cffi` (BSD license, free)
- `httpx`, `beautifulsoup4`, `lxml` (free)
- GitHub Actions (free for public repos, 2,000 min/month for private)
- Cloudflare Workers + D1 (free tier covers our scale)

## Implementation order

1. **Week 1**: Mercari JP scraper (verified accessible). Build, test on
   500 cards, expand to full unpriced JA. Estimated +1,500–2,500 prices.
2. **Week 2**: Fix Yahoo Auctions JP URL pattern. Same scraper template
   as Mercari with different parser. Estimated +1,000–2,000 prices.
3. **Week 3**: Cardmarket session-cookie pipeline. Playwright once-off
   for login, curl_cffi weekly. Estimated +800–1,500 prices.
4. **Week 4**: Audit gates + GitHub Actions cron. Wire it all into
   weekly automation.

After all four phases: realistic JA coverage **92-95%**, all free,
reproducible weekly.

## Audit gates (block bad data)

Before any UPDATE hits D1:

```python
def is_acceptable_price(card_id, new_price_usd, prior_price_usd, new_source):
    # Sanity ceiling — no card should jump 10x in a week without manual review
    if prior_price_usd and abs(new_price_usd - prior_price_usd) / prior_price_usd > 5:
        log_anomaly(card_id, new_price_usd, prior_price_usd, new_source)
        return False
    # Sanity floor — TCGPlayer market price < $0.01 is usually a parse error
    if new_price_usd < 0.01:
        return False
    # Range check — no Pokemon card costs more than $100K
    if new_price_usd > 100_000:
        return False
    return True
```

Anomalies write to `data/backfill/anomalies.log` for human review.

## Source priority for `price_source` flag

When multiple sources have a price for the same card, pick by priority:

1. **manual** — never overwritten (curated by us)
2. **tcgplayer** — TCGCSV cat 3 (TCGPlayer EN), most authoritative for English
3. **tcgplayer_jp** — TCGCSV cat 85 (Pokemon Japan)
4. **yuyutei** — current JP retail
5. **mercari_jp** — current JP marketplace asks
6. **yahoo_auctions_jp** — realized JP sale prices
7. **pricecharting** — historical eBay-derived
8. **cardmarket** — EUR baseline
9. **ebay_us** — last-resort

Higher-priority source's price wins when both are fresh (<7 days old).

## Open-source release path

Once the YAJ + Mercari + Cardmarket scrapers exist, the whole thing
becomes shippable as `tcg-price-aggregator`. The novel parts:

- Per-source extractor classes (URL patterns, parsers, throttle config)
- The set-mapping registry (research-validated cross-references)
- The card_id matcher (number alignment + name validation +
  JA→EN translation)
- The audit gates

Each component is replaceable, so the same framework can serve One
Piece TCG, Magic, Yu-Gi-Oh — anywhere there are public marketplaces
with structured listings.

## What I won't do

- Use firecrawl credits in production cron (paid; user has limited credits).
- Use Scrydex / JustTCG / PokemonPriceTracker (paid).
- Scrape pkmncards.com (per project policy — memory says don't).
- Synthesize prices for cards that don't trade publicly (would mislead users).

NULL is a correct answer for cards that genuinely don't trade. The UI
already handles this.
