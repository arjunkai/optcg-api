# Scope: PTCG-JA card-index load performance

The one real remaining speed lever (everything else is already optimized: routes
code-split, images lazy+srcset, index Brotli'd to ~720 KB wire, per game+lang).

## Problem (measured 2026-06-01)

`GET /pokemon/cards/index?lang=ja` → 21,869 cards.
- **Wire: ~720 KB** (Brotli) — fine, NOT the problem.
- **Decompressed: 11.53 MB JSON** (~527 B/card) — this is what the client must
  `JSON.parse` → `.map(normalizeCard)` (21.8k objects) → write to IndexedDB
  (21.8k `put`s). Only PTCG-JA users pay this (OPTCG / PTCG-EN load their own,
  smaller indices). It's a one-time-per-`DB_VERSION` cost, then served from IDB.

### Per-field byte breakdown of the 11.53 MB (where it actually goes)
| field | MB | % | note |
|---|---|---|---|
| pricing | 1.53 | 13.3 | needed for price display |
| **image_low** | **1.45** | **12.6** | **redundant — see Tier 1** |
| image_high | 1.47 | 12.8 | needed |
| variants | 0.84 | 7.3 | needed (finish/filter) |
| name | 0.57 | 5.0 | needed |
| price_source | 0.57 | 4.9 | needed |
| category/types/name_en/local_id/set_id/id | ~2.3 | ~20 | needed |
| stage/rarity/lang/retreat/hp | ~1.3 | ~11 | needed (filters) |
| dominant_color / campaign / distribution_method | 0.27 | 2.3 | **100% null in JA** |

## The decisive unknown — MEASURE BEFORE CHOOSING

The fix depends on which stage dominates on a real mid-range phone:
`fetch+decompress` vs `JSON.parse` vs `.map(normalizeCard)` vs `21.8k IndexedDB puts`.
**My hypothesis: the 21.8k individual IndexedDB `put`s dominate, not parse** —
in which case shrinking the payload helps less than batching writes / storing
raw + normalizing lazily. Do not guess; instrument first.

**Measurement step (do this first):** add `performance.now()` marks around each
stage in `useCardCache.jsx` (fetch→json→map→IDB-commit), behind a `?perf=1`
flag, and run on a throttled device (DevTools CPU 4×, "Slow 4G"). Record the
breakdown for a cold load. That number picks the tier below.

## Tier 1 — payload trim (do regardless; safe, ~13–15%)

**Drop `image_low` from the slim index.** Its ONLY consumer is a redundant
`extractSeries(image_high) || extractSeries(image_low)` fallback in
`opbindr/src/lib/normalize/ptcg.js` — `image_high` always wins when present, and
for the 77% scrydex rows `series` is null either way. Not used for rendering.
- Change: remove `image_low` from `rowToSlim()` (`src/pokemon/cards.js:49`) and
  the SQL selects (`cards.js:196/207`, `sets.js:52`). Leave the normalizer's
  `|| extractSeries(card.image_low)` as a harmless no-op (undefined).
- Saves **~1.45 MB decompressed (−12.6%)**, trivial wire change (Brotli already
  ate it), but real `JSON.parse` + IDB-write reduction.
- Optionally drop `dominant_color` from the PTCG slim (100% null today;
  placeholder already falls back to type tint). Keep `campaign` /
  `distribution_method` — they feed the AddCardsModal filter + search and may be
  populated in other langs.
- **Cost:** must bump the edge `_v` (cards.js index version) AND frontend
  `DB_VERSION` so clients refetch — same cache discipline as every data-shape
  change ([[feedback-pwa-sw-cache-trap]]). One-time full refetch for PTCG-JA users.
- Effort **S**, risk **low**.

## Tier 2 — gated on the measurement

- **If `JSON.parse` / payload dominates →** columnar / key-dictionary payload:
  emit one array per field (keys written once, not 21.8k×). Cuts decompressed
  size ~30–40% and parse time. Needs a format-versioned server serializer + a
  client decoder before `normalizeCard`. Effort **M**. NOTE: MessagePack is NOT
  obviously better — V8's native `JSON.parse` often beats JS MessagePack decoders
  for large arrays; only adopt if measured faster.
- **If the 21.8k IndexedDB writes dominate (likely) →** don't chase payload size;
  instead (a) batch puts in one transaction with fewer commits, (b) store the
  raw rows and normalize lazily on read, or (c) store the whole array under one
  key and index in memory. Effort **M**, biggest likely win, independent of payload.
- **If `.map(normalizeCard)` dominates →** trim the normalizer / precompute
  `card_image` + `series` server-side so the client maps less. Effort **S–M**.

## Tier 3 — per-set sharding (probably NOT)

Load only opened sets. Rejected unless Tiers 1–2 are insufficient: AddCardsModal
searches/filters ACROSS all sets, so it needs the full set loaded anyway, and
[[feedback-card-loading]] cautions against set-lazy-loading for the feed. High
UX cost for a binder app whose mental model is the whole collection.

## Recommended sequence
1. Instrument the load (½ day) → get the stage breakdown. **This decides everything.**
2. Ship Tier 1 (image_low trim + `_v`/`DB_VERSION` bump) — safe win regardless.
3. Implement the ONE Tier-2 fix the measurement points to; re-measure on device.
4. Stop. Don't do Tier 3 unless the device number is still bad.

## Honest expectation
Tier 1 ≈ −13% payload. The real user-felt win is almost certainly Tier 2's
IndexedDB-write fix, not payload size — which is exactly why we measure first
instead of assuming "the 11 MB" is the enemy.
