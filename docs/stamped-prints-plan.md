# Stamped promo prints: tags and new card rows

Status: proposed (2026-11-16). Nothing below is built yet.

## Why

The official card list has one row per promo art (`EB01-014_p1`). TCGPlayer
sells each **stamp** of that art as its own product, at very different prices:

| EB01-014 Sanji print | TCGPlayer |
|---|---|
| Tournament Pack 2025 Vol. 1 | $0.50 |
| Winner Pack 2025 Vol. 1 | $8.80 |
| CS 25-26 Celebration Pack | $67.58 |

Since PRs #21–#23, our rows are priced from the print their picture actually
shows. Two gaps remain:

1. **No stamp on the card.** Nothing in the API says a row is the
   "CS 25-26 Celebration Pack" print or a "Winner" print, so clients can't
   show it, filter by it or explain a price.
2. **Prints we don't list.** 426 stamped TCGPlayer products have no row of
   ours (2026-11-16 report: $53.6k total; 121 at ≥ $50). 412 of them belong to
   a card number we already have.

| Stamp | Prints we don't list | Value |
|---|---|---|
| Championship / CS | 100 | $21,017 |
| Treasure Cup | 31 | $14,439 |
| Finalist | 41 | $6,848 |
| Regional | 23 | $5,650 |
| Winner | 73 | $2,685 |
| Anniversary | 25 | $2,216 |
| Store | 2 | $341 |
| Judge | 89 | $346 |
| Tournament / participation | 42 | $90 |

## Constraints that shape the design

- **IDs are permanent.** OPBindr stores card IDs as plain strings inside each
  binder's JSON (`binders.data`, `slots[].cardId`; also localStorage binders and
  the tray). A row we add can never be renamed or deleted without breaking
  binders. That means retiring a row instead of removing it.
- **OPBindr's ID rules** (from a read of the repo):
  - The publish check requires `^((OP|ST|EB|PRB)NN|P|DON)-N{1,4}[a-z]?(_[a-zA-Z]+[0-9]+)?$`
    (supabase migrations 028/029).
  - IDs longer than 40 characters are dropped (migration 026).
  - The eBay base-id fallback strips `_[a-z]+\d+`.
  - So the suffix must be **letters then digits**, with only one suffix.
- **OPBindr displays images from our `/images/{id}` route**, not from
  `image_url`. New rows need that route to serve them.
- **D1 free tier.** A few hundred inserts are trivial. Importers must stay
  no-op on re-run (`tests/import-writes.test.mjs`).

## Design

### 1. Stamp tag on every card (no new IDs)

Migration `022_card_stamps.sql` adds two nullable columns to `cards`, plus an
index on `stamp`:

- `print_label TEXT`: the TCGPlayer product label of the row's matched print,
  verbatim, e.g. "CS 25-26 Celebration Pack" or "2025 New Year Event Winner".
  It is NULL for plain set prints.
- `stamp TEXT`: a small fixed vocabulary derived from the label:
  `championship`, `winner`, `finalist`, `top_players`, `treasure_cup`,
  `regional`, `store`, `judge`, `tournament`, `anniversary`, `pre_release`,
  `release_event`, `collection`, `other`.

`build_all_prices.py` already knows each card's final product, so it writes
both fields into `card_prices_all.json`, and `import-prices-d1.js` writes them
with the price, with the same changed-value guard as today. Existing promo
rows get tagged on the first run. This is useful on its own: the client's
Sanji would come back as `print_label: "CS 25-26 Celebration Pack"`,
`stamp: "championship"`.

API changes:
- return both fields on `/cards`, `/cards/:id` and `/sets/:id/cards`;
- add a `?stamp=` filter on `/cards`, with `ROUTE_PARAMS` and edge-cache-key
  tests updated;
- document them in `src/docs.js`;
- add them to the card snapshots, which means a snapshot name bump and a
  `snapshotDefs.js` entry.

### 2. New rows for prints we don't list

**ID format: `{base}_t{tcgplayer product id}`**, e.g. `EB01-014_t649632`.

- **Deterministic:** the same product always gets the same ID. No counter to
  allocate and no CI commit needed, and two runs can never disagree.
- **Never collides** with the official site's `_pN` / `_rN` or our `_jpN`.
- **Fits OPBindr's rules:** one suffix, letters then digits, at most 18
  characters. `_t…` also sorts after `_p` and `_r` in OPBindr's ID sort, so
  stamped prints sit after a card's parallels.
- Readability doesn't matter: clients show `print_label`, not the ID.

**Rows are added only from a reviewed list,** `data/stamped_prints.json`,
keyed by TCGPlayer product ID, for example:

```json
{ "649632": { "base": "EB01-014", "label": "CS 25-26 Celebration Pack", "added": "2026-11-20" } }
```

A weekly job never creates IDs on its own. Because IDs are permanent, a human
approves each batch.

**New importer `scripts/import-stamped-prints.js`,** modeled on
`import-jp-exclusives.js`:
- `INSERT … SELECT` from the base row, so the new row inherits name, colors,
  cost, power, counter, types, effect and trigger;
- sets `parallel=1`, `base_id`, `variant_type='alt_art'`, `print_label`,
  `stamp`, `rarity` (from TCGPlayer), `tcg_ids=[id]` and
  `image_url=/images/{id}`;
- joins `card_sets` to `569901` (Promotion card);
- is idempotent, with a rollback breadcrumb in the header.

**Prices:** `build_all_prices.py` maps each `_t` row straight to its own
product (it is the exact print), so it skips the dotgg, label and picture
steps.

**Images:** `src/images.js` treats `_t{id}` like DON rows: R2 first
(`cards/{id}.png`), then the TCGPlayer CDN (`product/{id}_in_1000x1000.jpg`).
Curated or eBay images can upgrade a row later through
`fetch_card_image.py`, as with DONs.

### 3. Avoiding duplicates

The risk: a print we add as `_t…` turns out to be the same print as an
official row, either now or after the official site adds it later.

- **Before approving,** the candidate list (built by the price build) compares
  each candidate's photo with **every** existing row of that card number,
  using `image_match.py`, not only the linked product. A candidate scoring
  ≥ 0.95 against an existing row is not a new print; that row's link gets
  fixed instead. Japan-site rows are excluded (a Japanese print isn't the
  English product).
- **Weekly,** the "Promo & variant pricing" summary flags any official row
  whose picture matches a `_t` row's product. The resolution is to mark the
  `_t` row retired: a new `retired_into` column pointing at the official row.
  The API keeps serving it, with the price copied from the official row, so
  binders keep working. OPBindr can then show "same as {official id}".

### 4. Weekly flow

1. The price build writes candidates to the run summary and to
   `data/price_mapping_report.json` (it already lists the unrepresented
   prints). The duplicate pre-check adds a "ready to approve" list.
2. Approving means a PR that adds entries to `data/stamped_prints.json`. I can
   draft it from the report, and you merge it.
3. The scrape runs `import-stamped-prints.js` after `import-d1.js`, and the
   price build prices the new rows.

### 5. OPBindr changes (separate repo)

- **Cache:** bump `DB_VERSION` (`useCardCache.jsx:120`) and the service-worker
  cache name `optcg-api-v10` (`vite.config.js`) so clients refetch the
  catalog.
- **Display:** show `print_label` as a chip in `CardEnlargeModal.jsx`
  (chip row ~531–560), and a short stamp pill on grid tiles next to the
  "✦ Alt Art" pill (`AddCardsModal.jsx:162-168`, `CardSlot.jsx:640-655`).
  Map labels in `src/lib/variants.js`.
- **Filter:** add a stamp pill row in AddCardsModal, alongside the variant
  pills (`gameProfile.js:55-60`, predicate ~630).
- **Search:** add `print_label` to `buildSearchKey` (`useCardCache.jsx:~305`)
  so "celebration pack" finds the card.
- **eBay link:** add the stamp words to `ebayLink.js:46-60` keywords
  ("Winner", "Championship") so eBay searches hit the right print.
- **Nothing needed** for ID parsing: every prefix-based helper ignores the
  suffix, and `_t…` passes the publish regex.

## Rollout

| Phase | What | Risk | Rough size |
|---|---|---|---|
| **A** | Migration 022 + stamp tags on existing rows + API fields/filter/docs + snapshot bump | Low: additive columns, no new IDs | 1 PR here + run migration |
| **B** | OPBindr: chips, pill, filter, search, cache bump | Low | 1 PR in opbindr |
| **C** | `_t` rows: importer, `/images` route, price-build direct mapping, duplicate pre-check, candidate list | Medium: permanent IDs | 1 PR here |
| **D** | First approved batch: start with the 121 prints ≥ $50 (Championship, Treasure Cup, Finalist, Regional), check them by eye, then the rest | IDs go live | data PR + scrape run |
| **E** | `retired_into` handling for later duplicates | Low | small PR, when first needed |

Phase A alone fixes "what print is this?" for every promo we already list.
Phase D is the step that can't be undone, so it waits for A–C and a visual
check of the first batch.

## Decisions needed

1. **First batch scope:** ≥ $50 only (121), or every print with a known base
   card (412, including $0.10 Judge and participation prints)? Collectors fill
   binders with cheap prints too, but the cheap ones are more numerous and more
   likely to be near-duplicates. Recommended: ≥ $50 first, then the rest after
   a week of the duplicate report.
2. **Set membership for new rows:** `569901` (Promotion card, recommended,
   matches how the official site files promos), or the base card's set?
3. **API consumers:** new rows appear in `/cards/index` and `/cards/all` for
   every key holder (alpha430, Delta Protocol). They're additive, but worth a
   heads-up message when Phase D ships.
