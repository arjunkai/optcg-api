# Scrapling Pricing Pipeline — Phase 5 Plan

**Status:** Spec, not implemented. Updated 2026-05-03.
**Predecessor:** `WEEKLY_SCRAPER_PLAN.md` (Phase 4 — Mercari/YAJ/Cardmarket via curl_cffi + playwright cookie harvest).

This plan supersedes Phase 4. Adopting [Scrapling](https://github.com/D4Vinci/Scrapling) (BSD-3, 42.8k stars, monthly releases) replaces three separate tooling stacks (curl_cffi, playwright, manual cookie management) with one library that handles TLS impersonation, Cloudflare Turnstile bypass, and full-browser automation under a single API.

## Goals

1. Push **JA pricing coverage from 81% → 92%+** by adding Hareruya as the primary JA retailer source.
2. **Replace the 6-source image patchwork** (Bulbagarden, malie.io, Scrydex, pokemontcg-data, eBay, tcgplayer-cdn) with TCG Collector as the canonical English-print library where it covers; keep existing as fallback.
3. **Eliminate the Cardmarket cookie-harvest hack** from Phase 4 — `StealthyFetcher` solves Turnstile programmatically.
4. **Reduce ongoing maintenance** by leveraging Scrapling's adaptive selectors so site reorganizations don't break weekly cron silently.

## Non-goals

- **Collectr.** Their ToS explicitly prohibits scraping; their data is derivative of public sources we can hit directly; reputational risk in a small community is not justified by marginal data gain. Permanently skipped.
- **pkmncards.com.** Project policy says don't.
- **PSA/CGC behind login.** Auth wall + ToS violation.
- **AI image upscaling.** Per existing project memory.
- **Any user-generated content from any platform.** Privacy + ToS regardless of technical feasibility.

## Phase 5.0 — Hareruya PoC

**Status:** Completed 2026-05-04. **Verdict: PARTIAL** — ship as supplementary tier, not transformative.

### Findings

Surprise 1: **Hareruya2 is Shopify-backed, no Cloudflare**. The whole `StealthyFetcher` plan was unnecessary for this source — `/products.json` and per-collection `/collections/{handle}/products.json` are public REST endpoints. Plain HTTP works.

Surprise 2: **Title format encodes (set_id, local_id) cleanly** in `〈lid〉[setid]` — direct ID matching, no fuzzy name matching needed. Set-code conventions diverge from TCGdex's by case (`SM12a` vs `SM12A`), prefix expansion (`S12a` ↔ `SWSH12A`), and promo-set naming (`S-P` ↔ `SWSHP`); all handled by a candidate-set generator.

Surprise 3 (the load-bearing one): **the unpriced JA tail is mostly structural, not a sourcing gap.**

| Cohort | Hareruya hit rate |
|---|---|
| Already-priced JA cards (control) | 59.6% |
| Unpriced JA cards (target) | 19.7% projected on full DB (923 / 4682) |

The cards we lack prices on are cards Hareruya doesn't carry either — they don't trade actively. Mercari JP and Yahoo Auctions JP are likely to show the same pattern (same liquid universe). The realistic JA pricing ceiling across all these sources is ~85%, not the 95%+ originally hypothesized.

### Revised commitment

- **Adopt Hareruya as a supplementary tier** (`price_source = 'hareruya'`), priority below TCGCSV cat 85 and Yuyutei.
- **Use as cross-validation** on the 60% of priced cards it overlaps — log anomalies where Hareruya disagrees by >2x with our existing source.
- **Coverage gain:** ~+4% absolute (923 of 4682 unpriced cards filled). Not the 11% gain originally claimed.
- **No Scrapling needed** for this source. Plain Python `urllib`. Saves us the ~500MB Chromium dependency cost on the runner for the Hareruya path.

### Implementation status

- Walker: `scripts/poc_hareruya_jp.py` — production-ready as written. 18 series collections, ~31k unique products fetched in ~4 min, polite 0.7s throttle.
- D1 query, FX conversion, set-id remapping all verified working.
- Next step (when ready to ship): rename to `backfill_hareruya_jp.py`, write the matched cards' prices into D1 with `price_source='hareruya'` (preserving manual + ranking below tcgplayer_jp / yuyutei), wire into weekly cron.

### Original (pre-PoC) hypothesis

The original plan assumed Hareruya was Cloudflare-protected and needed Scrapling's `StealthyFetcher`, with a 50% sample-hit-rate GO threshold and an 81% → 92%+ JA pricing projection. Both assumptions were wrong (no Cloudflare; the unpriced tail is structural). Kept here only as a record — see Findings above for the actual results.

## Phase 5.1 — TCG Collector image PoC (1 session, parallel to 5.0)

**Why:** consolidates the 6-source image patchwork. The mcd17/mcd18 JP-text bug we just fixed (2026-05-03) was a direct consequence of source heterogeneity — Bulbapedia files for SunMoon-era cards are shared between English Sun & Moon and Japanese Collection Sun, and we picked the wrong language print. TCG Collector keeps language scans separate by design.

### Setup

```python
# scripts/poc_tcgcollector_images.py
from scrapling.fetchers import StealthyFetcher

# Target: 50 cards across (a) zh-tw image gaps, (b) mcd17/mcd18, (c) JA ecard era
TEST_IDS = [...]
```

### Success criteria

- **Coverage ≥ 80%** of 50-card test (vs ~60% for Bulbagarden hand-mapping)
- **Language correctness:** 100% language match (no JP-text leaking into EN view, no EN-text leaking into JA)
- **Image quality:** ≥1000px on long edge for cards from 2019+, ≥500px for vintage

### Decision tree

```
Coverage ≥ 80%, language clean
  → COMMIT: replace Bulbagarden + malie.io + Scrydex JA priorities with TCG Collector primary
  → Keep pokemontcg.io as English-print fallback
  → Retire: bulbagarden script, malie.io script, possibly Scrydex backfill

Coverage 50-80%
  → ADD as supplementary tier — not a wholesale replacement

Coverage <50%
  → ABANDON, keep current pipeline
```

## Phase 5.2 — Cardmarket via StealthyFetcher (replaces Phase 4 Week 3)

If 5.0 succeeds, Cardmarket follows the same pattern. The Phase 4 plan was: human cookie-harvest in Playwright every 30 days, then curl_cffi against the per-set CSV exports. Scrapling's `StealthyFetcher` solves Turnstile programmatically — no human in the loop.

### Implementation

```python
with StealthySession(headless=True, solve_cloudflare=True) as session:
    for set_id in CARDMARKET_SETS:
        page = session.fetch(f"https://www.cardmarket.com/en/Pokemon/Products/Singles/{set_id}")
        # Parse the per-set price table
```

**Estimated coverage gain:** 800-1500 EU EUR prices for cards Cardmarket has but TCGdex's `pricing.cardmarket` field misses (low-volume / niche).

## Phase 5.3 — Mercari JP + Yahoo Auctions JP (Phase 4 Weeks 1-2 unchanged)

These don't need StealthyFetcher — Scrapling's plain `Fetcher` with TLS impersonation handles them. Move from `curl_cffi` to Scrapling's `Fetcher` only to consolidate dependencies; coverage estimates unchanged from Phase 4 plan.

## Architecture changes

### Dependency consolidation

**Removed:**
- `curl_cffi`
- `playwright` (replaced by Scrapling's bundled Chromium)
- Manual cookie-management glue scripts

**Added:**
- `scrapling[fetchers]==1.x.x`
- `scrapling install` step in CI (downloads Chromium ~500MB, cached via `actions/cache@v4`)

### Source priority (post-5.0/5.1 commit)

```
       IMAGE                              PRICE
       ──────                             ──────
  1   manual override                  manual override
  2   TCG Collector (Scrapling)        Hareruya JA (Scrapling)
  3   pokemontcg-data (EN)             tcgplayer/TCGCSV (EN)
  4   Scrydex (JA fallback)            pokemontcg.io live (EN USD)
  5   TCGdex (multi-lang)              eBay JP/US (Scrapling Fetcher)
  6   eBay residual (Scrapling)        Cardmarket (Scrapling Stealthy)
                                       Yuyutei (existing)
                                       Mercari JP (Scrapling Fetcher)
                                       Yahoo Auctions JP (Scrapling Fetcher)
                                       PriceCharting (existing UA-spoof)
                                       cardmarket EUR baseline (TCGdex baked-in)
```

Manual overrides always top priority on both axes — never stomped.

### Weekly cron impact

Adds ~10 minutes to the Monday refresh:
- Hareruya: ~7 min for 5,000 unpriced JA cards (Cloudflare solve + 1 req/s polite throttle)
- TCG Collector: ~3 min for image gap residuals
- Cardmarket: ~5 min for ~50 sets

Total cron wallclock: 35 min → ~50 min. Within GitHub Actions free tier comfortably.

### Audit gates (from Phase 4, retained)

```python
def is_acceptable_price(card_id, new_price_usd, prior_price_usd, new_source):
    if prior_price_usd and abs(new_price_usd - prior_price_usd) / prior_price_usd > 5:
        log_anomaly(card_id, new_price_usd, prior_price_usd, new_source)
        return False
    if new_price_usd < 0.01: return False
    if new_price_usd > 100_000: return False
    return True
```

Anomalies → `data/backfill/anomalies.log` for human review before D1 write.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Scrapling project abandoned | Low (active May 2026, 42.8k stars) | BSD-3 license — fork if needed |
| Single-dep failure breaks all scrapers | Medium | Per-source fallbacks; existing Phase 1-3 scripts stay as backup tier |
| Hareruya tightens anti-bot | Medium | DynamicFetcher fallback (slower but more authentic) |
| Cloudflare cross-site fingerprint correlation | Low-Medium | Per-source separate proxy if needed (Scrapling supports rotation) |
| Scrapling's chromium adds 500MB to runner | Certain | `actions/cache@v4` keeps it warm, only first run pays |
| Sites we add later add CAPTCHAs | Possible | Stop adding sources past acceptable run time |

## Decision: GO / NO-GO criteria

Build Phase 5.0 PoC as defined above. After PoC:

- **GO** if Hareruya hit rate ≥ 50% and TCG Collector coverage ≥ 80%. Commit to full pipeline build (~2 weeks elapsed, ~6 hours of focused work spread across).
- **NO-GO** if either PoC underperforms. Fall back to Phase 4 plan (curl_cffi + manual cookie harvest), keep current image pipeline, accept JA pricing tail.

## Out-of-scope follow-ups

If Phase 5 succeeds, these become candidates:
- **Bigweb / Cardrush** (additional JA retail tail, ~1k-2k more cards)
- **Snkrdunk** (chase-card secondary market, requires DynamicFetcher)
- **Mandarake / Surugaya** (vintage JP rare promo coverage)
- **Pokellector** (vintage image gaps where TCG Collector misses)

Track these in a TODO list, don't commit until 5.0/5.1 land.

## Appendix: why not Collectr

Documented for future-self / handoff:

1. **ToS prohibition.** Their [API terms](https://getcollectr.com/api-terms-and-conditions.html) explicitly forbid scraping content or pricing.
2. **Direct competitor.** OPBindr and Collectr serve overlapping users. Scraping a competitor is asymmetric risk — worst case for not scraping is "missing derivative data," worst case for scraping is "C&D + reputational damage in a 50k-person community."
3. **Data is derivative.** Their pricing pulls from TCGPlayer + eBay solds + Cardmarket — sources we can hit directly without ToS violation.
4. **Audit policy.** Collectr's ToS reserves the right to monitor and audit access patterns. Even a successful scrape is at risk of detection over time.

This is a permanent skip, not a "later." If Collectr's situation changes (open API, partnership, etc.), revisit then.
