# eBay APIs for the OPTCG / PTCG pricing pipeline

What we use today, what's available in the developer program, and which
ones are worth applying for.

## TL;DR

| API | Status for us | Worth applying? |
|---|---|---|
| Browse API | Already in production | — |
| Browse API getItems batch | Implemented in client, not yet wired | Use it — 20× cheaper per call |
| Commerce Translation | Implemented, not wired | Use it — open to all, automates JA→EN query strings |
| Marketplace Insights | Implemented, gated on Application Growth Check | Apply, but expect denial: Limited Release, restricted to approved partners |
| Buy Feed | Not implemented | Same gating as Marketplace Insights; only worth it if approval lands |
| Catalog API | Not implemented | Optional — could improve EPID-based matching |
| Finding API (legacy) | Decommissioned 2025-02-05 | Don't use |
| Trading API (legacy) | Don't need | — |

## What you have today

`scripts/ebay_client.py` is a single client wrapping:

- `search()` — Browse API `item_summary/search`. Active listings only.
- `get_items()` — Browse API `item/getItems`. Up to 20 items per call.
- `search_sales()` — Marketplace Insights `item_sales/search`. Sold listings, last 90 days.
- `translate()` — Commerce Translation `translate`. Bulk text translation.

Token caching is per-scope (`data/.ebay_tokens/{scope}.json`) so an
approved Marketplace Insights token doesn't conflict with the always-on
Browse token.

Defenses (`apply_title_filters`, `consensus_price`) are shared across
search and search_sales.

## API-by-API rundown

### Browse API — already using

`https://api.ebay.com/buy/browse/v1/item_summary/search`

Active listings (asking prices, not sold prices). Default app rate
limit is 5,000 calls/day. Open to every registered dev.

**Marketplace caveat:** EBAY_JP returns HTTP 409. JA pricing currently
queries EBAY_US with English-translated card names because US sellers
relist JA cards. This is documented in
`reference_ebay_browse_api_jp.md`.

**Underused features:**

- `getItems` batches up to 20 items in one call. Re-checking known
  prices for 200 cards goes from 200 calls to 10. **20× rate-limit
  savings.** The `search_sales` method already supports this for the
  sold-listings flow; for active listings it's a one-line change in
  the existing backfill scripts.
- `fieldgroups=COMPACT` on getItem only returns id+price+availability.
  Smaller payload, faster, lighter on rate limits.
- `filter=` with `conditions:{NEW}|{LIKE_NEW}` narrows to near-mint
  cards. More relevant for collector pricing than the current
  unfiltered query.
- `filter=` with `buyingOptions:{FIXED_PRICE}` excludes auctions.
  Auction prices fluctuate and are noisier than fixed-price asks.
- `epid` parameter lets you search a specific eBay product page
  directly. For Pokemon TCG, eBay has cataloged most major cards;
  searching by EPID gives a much cleaner result set than keyword fuzz.

### Marketplace Insights API — apply for it, expect a wait

`https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search`

The right answer for sold-listing prices. Scope:
`https://api.ebay.com/oauth/api_scope/buy.marketplace.insights`.

**Status as of 2026-05:** eBay's own community forum classifies this as
"restricted, not open to new users." Multiple developers reporting
access-denied responses for direct scope requests; eBay tells them to
file an Application Growth Check.

**To apply:**

1. Sign your app's keyset into the eBay Partner Network if it isn't
   already (https://partnernetwork.ebay.com/). Affiliate enrollment is
   free.
2. File an Application Growth Check:
   https://developer.ebay.com/grow/application-growth-check
3. The form asks for: app description, expected daily call volume,
   expected hourly peak, the OAuth scopes you need, retry/error
   handling description, license-agreement compliance.
4. Approval timeline: weeks. eBay's review favors apps that drive
   revenue back into eBay (affiliate links, listings sourced from
   eBay). OPBindr's pitch is: pricing data displayed alongside a "buy
   on eBay" affiliate link in CardEnlargeModal would qualify.

**If approved, expected unlocks:**

- EBAY_JP works — closes the 8% JA pricing gap that EBAY_US can't
  reach (cards US sellers don't relist).
- Sold prices instead of asks. Asks on slow-moving cards skew high
  because sellers list optimistically and don't relist if they don't
  sell. Sold prices are what cards actually trade at.
- 90-day rolling window. Combined with the existing weekly snapshot,
  this gives us our own sold-listing history for charts.

**If denied, fallbacks:**

- Stay on Browse with EBAY_US fallback for JA. The current ~92% JA
  ceiling is structural; we already documented it.
- Pay for JustTCG or PokemonPriceTracker (~$50–200/mo). Per the memory
  this was evaluated and shelved on cost grounds, but cost-per-card
  versus building any other source is favorable.
- Consider Yahoo Auctions / Mercari scraping (ToS gray, heavy effort).

**Probe whether you have access:**

```
python -m scripts.probe_marketplace_insights "Charizard ex 199 PSA 10"
python -m scripts.probe_marketplace_insights "リザードンex" --marketplace=EBAY_JP
```

The script catches `EbayAccessDeniedError` and prints the eBay error
body verbatim so you can tell denial from a genuine failure.

### Commerce Translation API — wire it in, scope enable needed

`https://api.ebay.com/commerce/translation/v1_beta/translate`

Open to every registered dev, but the scope is opt-in per keyset.
Probed against this app's keyset on 2026-05-06 — got `invalid_scope`,
which means the scope isn't enabled. To enable:

1. Open https://developer.ebay.com/my/keys
2. Click **OAuth Scopes** next to the production keyset
3. Find "Commerce Translation API" in the list
4. Tick the checkbox, save
5. Delete `data/.ebay_tokens/` so the cached token is re-issued
6. Re-run `python -m scripts.probe_translation "リザードンex"`

Scope URL (should match what's in the eBay dashboard):
`https://api.ebay.com/oauth/api_scope/commerce.translation`

Useful for one specific thing: when querying EBAY_US for JA-card
prices, US sellers list with English titles. The pipeline currently
falls through to keyword fuzz from the EN side of the card record. If
the card is JA-only (no EN print), there's nothing good to query with.

`translate(['リザードンex'], from_lang='ja', to_lang='en')` gives back
search-friendly English. Plug into `backfill_ptcg_prices_ebay.py`'s
query builder for `lang=ja` cards that lack an EN counterpart.

**Probe:**

```
python -m scripts.probe_translation "リザードンex" "ピカチュウ" "ミュウツーGX"
```

### Buy Feed API — same gating as Marketplace Insights

`https://api.ebay.com/buy/feed/v1/item`

Bulk daily/weekly feed of active items in a category. Could replace
per-card searches for the EN main-set refresh — instead of 20k Browse
calls/week, one feed download covering the entire Pokemon TCG
category.

**Same Limited Release gating** as Marketplace Insights, requires the
same Application Growth Check, and additionally requires being an
eBay Partner Network affiliate. Skip the application unless
Marketplace Insights gets approved first — if Insights gets denied,
Feed will too.

### Catalog API — small accuracy bump, optional

`https://api.ebay.com/commerce/catalog/v1_beta/product_summary/search`

Maps free-text card names to EPIDs (eBay product IDs). EPIDs let
Browse and Marketplace Insights search a specific product instead of
fuzzy-matching keywords.

**Trade-off:** eBay's catalog coverage of Pokemon TCG is good for SV-era
cards but spotty for vintage. Auto-mapping every card_id to an EPID
once would take ~22k catalog calls. Probably worth it if rate limits
allow, but not a top priority — current keyword search is already
filtered through the title blocklist + consensus median.

### Finding API — don't use

`findCompletedItems` was the historical way to get sold listings.
**Decommissioned 2025-02-05.** Marketplace Insights is the official
successor. If you find old code or guides referencing
`api.ebay.com/services/search/FindingService/v1` it's broken now.

## Recommended next steps, in order

1. **File the Application Growth Check** for Marketplace Insights. Even
   if it takes weeks, you can't unlock JA sold-listing data any other
   legitimate way. Link the application to OPBindr.com so reviewers
   see real product context.
2. **Wire the Translation API** into `backfill_ptcg_prices_ebay.py`'s
   JA-query builder. No approval needed, ships today.
3. **Convert Browse API search-then-getItem flows to use `getItems`
   batch.** Touches `backfill_ptcg_images_ebay.py` and the price
   backfills. Saves ~20× on rate limits.
4. **Add filter parameters** (conditions, buyingOptions) to existing
   Browse calls. Cleaner sample, less title-blocklist work.
5. **Hold off on Catalog API + Buy Feed** until step 1 lands. Both are
   downstream of Application Growth Check approval.
