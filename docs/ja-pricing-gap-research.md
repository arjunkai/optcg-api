# JA pricing gap — research (2026-06-01)

## The gap is overwhelmingly VINTAGE

Measured against the live JA index: **2,208 unpriced / 21,869 (~10%)**. By era:

| era | unpriced | addressable today? |
|---|---|---|
| WOTC/vintage + old promo (PMCG, neo, VS, BS, MP) | 645 | mostly NO |
| PCG / ADV / e-Card (E1–E5) | 635 | mostly NO |
| SWSH | 184 | partial |
| SV/Mega (in-print) | 169 | should be priceable (see below) |
| XY | 140 | partial |
| SM | 139 | partial |
| DP/Pt/LEGEND | 101 | NO |
| BW | 16 | — |
| other | 179 | — |

Top unpriced sets: **E1 (104), PMCG6 (98), PMCG5 (96), neo4 (96), E2 (80),
VS1 (80), PCG10 (78), E4 (71), E5 (70), PMCG1 (66), XY (66), ADV5 (59), E3 (59)**.

By category: Pokémon 1,400 · Trainer 628 · Energy 146 (energy = mostly no-market
bulk reprints; correct to leave "—").

**Only 199 are Aucfree-addressable** (known-token promos, just swept — thin tail).
**~2,009 are vintage Japanese singles** that TCGPlayer (US market), Yuyutei and
Hareruya (modern retail) structurally don't carry, and that PriceCharting only
partially covers (with the variant-conflation risk that withheld 171).

## Why this gap is hard (and why we haven't closed it)
Pre-2010 Japanese singles trade on JA-native marketplaces, not US/EU sources.
The pipeline's strong sources are all modern/US-leaning. The remaining gap needs
a **JA-native vintage retailer** with raw single-card listings.

## Recommended next source: Treca Sunrise (validated + still live)
`tcgsunrise.com` — confirmed live 2026-06-01 (HTTP 200, Makeshop SSR store at
`gigaplus.makeshop.jp/tcgsunrise`, category pages `/view/category/{catNN|ctNNN}`,
price as `item-price …円`). Its categories — **ADV・PCG / VS・web・eカード /
旧裏・neo / LEGEND / DP** — map almost exactly onto the 1,280-card e-card+WOTC gap.
~2,113 SKUs, raw (0 PSA). This was validated in the 2026-05-31 sweep but never
ingested (Fullahead promo got done; Treca + gamepedia did not).

### Confirmed site structure (recon 2026-06-01)
- **cat01 = ポケモンカード** — all Pokémon, RAW singles, 50 products/page,
  paginated. Product block: `<p class="item-name"><a href="/view/item/{12-digit-id}">NAME</a>`
  + `class="item-price">…円`. Crawl = cat01 pages 1..N.
- **ct109 = 鑑定品 (graded/PSA) is a SEPARATE category** → crawling cat01 only
  yields raw singles, sidestepping the graded-vs-raw trap (Torecacamp lesson).
- cat02/03/04 + other ctNNN = other games (Yu-Gi-Oh / One Piece / Union Arena /
  Weiss / hololive / Gundam) — ignore. The named era sub-cats from the May sweep
  aren't in the current top nav (site reorganized); filter cat01 to vintage by
  parsed set/era hints in the product name instead.
- **The hard part = parse `(name, set, number, printing)` out of the Japanese
  product NAME.** This is the conflation-risk surface — build it with TDD +
  spot-checks before any write. Measurement step can name-match for an upper-bound
  overlap; the WRITER must match strictly on set+number+name.

### Overlap measurement (crawled 2026-06-02, `scripts/measure_treca_overlap.py`)
Crawled cat01 to the 150-page cap = **7,500+ Pokémon products** (catalog is larger).
**Name-overlap with our 1,474 vintage unpriced = 302 (20.5%)** — an order of
magnitude above pokeca's 1.9% collapse → **clear GO**. But the crawl exposed that
recovery splits into two very different problems, and the 20.5% is an inflated
upper bound:
- **e-Card / PCG / ADV / DP era → STRUCTURED + safe.** Names encode set+number+
  printing: `【PCG】【004/052】フシギバナex`, `【1st】【PCG】【022/068】ボーマンダex`.
  Strictly matchable on 【SET】+【NUM/TOTAL】 → the safe ~635-card win. Build this first.
- **WOTC 旧裏 era → UNSTRUCTURED + risky.** `メタモンLV．15【旧裏】【状態C】` — name +
  LV only, NO set code or number; 初版/1st vs unlimited as tags. Can't be mapped to
  a specific PMCG/neo/VS set without high conflation risk. This inflates the 302.
  Match only with strict name+LV+printing gates and accept low recovery, or defer.
- **PSA/graded LEAK into cat01** (~5%: `【PSA10】【旧裏】ブラッキー ¥348,000`). The ct109
  graded category is NOT a clean separator — the ingester MUST exclude
  `【PSA\d】`/`鑑定`/`PSA10` tokens or a graded price lands on a raw base card.

### Ingester scope (mirror the yuyutei/hareruya/fullahead shared-lib pattern)
1. **Measure overlap FIRST** — crawl Treca's vintage category pages, parse
   (name, set hint, number, 円 price), and match against our 2,009 vintage
   unpriced IDs. Report the real overlap before building the writer (the
   pokeca 618→12 collapse is the cautionary precedent — low count ≠ low recall,
   but unmeasured ≠ recoverable either).
2. **Variant disambiguation is SEVERE for vintage** — 1ED / unlimited / holo
   share one local number and our `card_id` doesn't encode the printing. Match
   strictly on set+number+name; when a listing's printing is ambiguous, SKIP
   rather than mis-map ([[feedback-pricecharting-variant-conflation]],
   [[feedback-no-plausible-wrong-prices]]). Treca being raw-only (no PSA) removes
   the graded-vs-raw trap that killed Torecacamp.
3. **Polite + Scrapling** — Makeshop SSR, robots-permitting; reuse the
   `Fetcher.get(stealthy_headers=True)` + circuit-breaker pattern from the
   Aucfree work. Crawl by category (a few dozen pages), not per-card.
4. Stamp `price_source='treca'`, lowest of the JA retail tier (yuyutei >
   hareruya > fullahead > treca > yahoo_sold), guard `price_source IS NULL OR
   IN ('cardmarket')` so it never clobbers a real source.

### Secondary: gamepedia.jp/pokeca (breadth)
11,960 cards with dated 販売 (retail) prices — a breadth fill for the modern
tail (SWSH/SM/XY unpriced). Use the 販売 column, ignore 買取 (buylist floor).
Lower priority than Treca for THIS gap (the gap is vintage, not modern).

## The ~169 SV/Mega (in-print) unpriced — measured, NOT a simple bug
I assumed these would be a cheap resolution fix; the data says otherwise:
- **162 / 169 have NO pricing object at all** — a genuine ingestion gap, not a
  resolution miss. Concentrated in **SVK (44)** and **SVLN (10)** which look
  like special sub-products (decks/boxes/promos) that may be legitimately
  no-market, plus mainline SV3/SV7/SV9 cards (26/24/21) that need a
  TCGPlayer/cardmarket match audit to tell recoverable from genuinely-unlisted.
- **7 are a real quick fix**: they carry a `hareruya.price_usd` but
  `price_source` is `tcgplayer`/null with no tcgplayer market present, so
  `pickPrice` never surfaces the hareruya value. Re-stamp `price_source='hareruya'`
  for rows whose only resolvable price is under `pricing.hareruya` (a guarded
  one-shot UPDATE). Small but trivially correct.

So the SV/Mega tail is NOT a free win — audit SVK/SVLN for no-market first,
then match the mainline SV3/7/9 stragglers against TCGPlayer/dotgg.

## Honest ceiling
Treca + gamepedia + the SV/Mega audit could plausibly take JA from ~90% to the
low-mid 90s. 100% is not reachable — energy reprints, no-market WOTC commons, and
truly-untraded promos stay "—", which is the correct, honest outcome
([[feedback-no-plausible-wrong-prices]]). Wrong prices are worse than none.

## Recommended order
1. **Re-stamp the 7 stranded-hareruya SV rows** (trivial, correct).
2. **Build the Treca Sunrise vintage ingester** (overlap-measured, variant-gated)
   — the real lever for the ~1,280-card e-card+WOTC gap. Highest value.
3. **Audit SVK/SVLN + mainline SV3/7/9 stragglers** against TCGPlayer/dotgg
   (separate no-market special-products from recoverable misses).
4. gamepedia breadth fill for the modern tail, if still worth it after the above.
