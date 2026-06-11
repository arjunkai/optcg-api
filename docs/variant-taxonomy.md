# OPTCG print / variant taxonomy

**Why this exists:** one card *number* (e.g. `OP03-114`) maps to many distinct
market *products* whose prices span $3 → $880+. Pricing that keys on the bare
number conflates them. This is the root cause behind the phantom prices fixed
on 2026-06-11 (e.g. `OP05-119_p2` showing $87,500). **The cardinal rule:**

> Price by **TCGPlayer product ID + product name**, never by card number alone.
> Two cards must never share a `tcg_ids` value. A price that is an order of
> magnitude off its variant-class siblings is a conflation until proven otherwise.

---

## 1. Base rarity (intrinsic to the card)
`L` Leader · `C` Common · `UC` Uncommon · `R` Rare · `SR` Super Rare ·
`SEC` Secret Rare · `P`/`PR` Promo · `DON!!` (synthetic `DON-NNN` ids).

## 2. In-set parallels (same product, alternate treatment) — our `_pN` / `_rN`
| Treatment | What it is | Value tier | Example |
|---|---|---|---|
| **Parallel / Alternate Art (AA)** | pack-pulled foil with NEW art | $-$$ | most `_pN` |
| **Standard foil parallel** | same art, foil | $ | — |
| **Manga Art (Manga Rare)** | B&W comic-panel SEC treatment | $$$ | OP05-119 Gear5 Luffy, OP09-118 Roger |
| **Colored "Super" alt art** (e.g. **Red** Super Alt Art / "Red Manga") | newer ultra-premium treatment, tier above standard manga | $$$$ | OP13-118_p3 (Red Manga, tcg 657401) ≠ OP13-118_p2 (standard Manga, tcg 657402) |
| **Reprint** | same card reprinted in a later set/product | varies | our `_rN` |

## 3. Promo cards (`OP-PR` set) — **the conflation minefield**
All share the base card number but are separate TCGPlayer products:
- **Pre-release** promos — set pre-release events (often a foil event stamp).
- **Errata / pre-errata** — a card reprinted after a rules-text correction; the
  original ("pre-errata") and corrected prints are distinct products & prices.
- **Championship prize cards** (e.g. *Championship 2024*, *Online Regional*) —
  tournament top-prize. Scarce, **$$$**. → `OP05-091_p3` (Rebecca), `OP03-114_p3` (Big Mom).
- **Winner / Finalist / Top-N** placement promos.
- **Store / Regional / Treasure Cup** event promos.
- **Tournament / Promotion Pack** participation promos — usually low value.

## 4. Special collections / gift products
- **(SP)** treatments — *Wings of the Captain* gift collection, anniversary sets.
- **PRB / Premium Booster** reprints (the "PRB sort-last" quirk in the pipeline).
- **Serialized / Serial** — numbered (e.g. /500); our `Serial` variant_type.

---

## Our current `variant_type` vocabulary vs. reality
Live D1 (2026-06-11): `null` 2842 · `Alternate Art` 1540 · `Reprint` 362 ·
`Manga Art` 35 · `Serial` 3.

**Gap:** Championship / Pre-release / (SP) / Winner promos are all collapsed into
`Alternate Art` or `null`. That coarse vocabulary is *why* `OP05-091_p3`
(a Championship 2024 promo) and `OP03-114_p3` (likewise) looked like ordinary
alt-arts and inherited asking-price garbage. Enriching `variant_type` from the
TCGPlayer product name (which carries `(Championship 2024)`, `(Manga)`, `(SP)`,
`(Red Super Alternate Art)`, …) would (a) fix UI labels and (b) let the audit
flag any price that is far from its variant-class median.

## Pricing-source rule of thumb
- **TCGPlayer market price** (real sales) → authoritative. `price_source='tcgplayer'`.
- **dotgg fallback** → only for LOW-value gap cards. Capped at **$300**
  (`DOTGG_PRICE_CEILING`); above that dotgg is almost always echoing the
  TCGPlayer *listed median* (an asking price, not a sale). Rejected entries land
  in `data/dotgg_rejected_high.json`.
- **No TCGPlayer sale + genuinely valuable** → curate by hand in
  `data/manual_prices.json` from eBay-sold / PriceCharting (graded excluded),
  `price_source='manual'` (protected from weekly overwrite). Never invent a
  number; a thin/uncertain card stays `NULL`.
