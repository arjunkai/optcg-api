"""
Build card_id -> price mapping from TCGPlayer scrapes, using dotgg.gg's
authoritative tcg_id<->card_id map to avoid positional-guess errors.

Pipeline:
  1. Parse every data/tcgplayer_raw/*.md into (tcg_id, price, variant_label) rows.
  2. Index them as a flat dict {tcg_id: {price, name, ...}}.
  3. Load dotgg catalog (data/dotgg_catalog.json) which maps every card_id to
     its definitive tcg_id(s). This is the ground truth — our _p8 vs _p6
     can't be figured out from TCGPlayer naming alone because cheap promo
     variants get interleaved with expensive alt-arts.
  4. For every card in cards.json:
     a. If dotgg knows the card, use its tcg_id to look up TCGPlayer's price
        (TCGPlayer is still our price-of-record since it's the live market).
     b. If that tcg_id isn't in our TCGPlayer scrape, fall back to dotgg's
        own price data (less fresh but correct).
     c. If dotgg doesn't have the card at all, fall back to the old
        positional matching as last resort.

The old positional matching is kept only for cards dotgg doesn't cover
(~2% of DB) — mostly brand-new sets, truly unique event promos.

Usage:
  python scripts/fetch_dotgg_catalog.py          # run once or before build
  python scripts/build_all_prices.py

Outputs:
  data/card_prices_all.json            {card_id: {price, tcg_ids, source_set}}
  data/card_prices_all.unmatched.json  list of unmappable TCGPlayer rows
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from parse_tcgplayer_prices import parse_file as parse_md
from map_prices_to_cards import VARIANT_LABEL_TO_TYPE, load_cards

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "tcgplayer_raw"
DOTGG_FILE = DATA_DIR / "dotgg_catalog.json"

# Sanity ceiling for dotgg-only fallback prices (no TCGPlayer sale exists for
# the card). Above this, dotgg's number is almost always the TCGPlayer listed
# median (an asking price), not a real sale — the source of phantom values like
# $87,500. Rejected entries are logged for manual curation in
# data/manual_prices.json. See also backfill_prices_dotgg.py.
DOTGG_PRICE_CEILING = 300.0


def to_float(v) -> float:
    try:
        return float(v) if v not in (None, "", "0", "0.000000") else 0.0
    except (TypeError, ValueError):
        return 0.0


def to_tcg_ids(v) -> list[int]:
    if not v:
        return []
    return [int(x) for x in str(v).split(",") if str(x).strip().isdigit()]


def main() -> None:
    if not DOTGG_FILE.exists():
        print(f"ERROR: {DOTGG_FILE} not found. Run `python scripts/fetch_dotgg_catalog.py` first.")
        return

    sets_meta = json.loads((DATA_DIR / "sets.json").read_text(encoding="utf-8"))
    by_id, parallels_by_base = load_cards()
    dotgg = json.loads(DOTGG_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(dotgg)} cards from dotgg catalog")

    # ── 1. Parse all TCGPlayer scrapes into a flat tcg_id -> {price, set_id, name} map.
    tcg_products: dict[int, dict] = {}
    # Order: regulars first (newer to older), then PRB last. Keeps the original-set
    # price as canonical when the same tcg_id appears on multiple guides.
    PREMIUM_SET_PREFIXES = ("PRB-",)
    sets_meta.sort(key=lambda s: (s["set_id"].startswith(PREMIUM_SET_PREFIXES), s["pack_id"]))

    total_rows = 0
    for s in sets_meta:
        md_path = RAW_DIR / f"{s['set_id']}.md"
        if not md_path.exists():
            continue
        for row in parse_md(md_path):
            total_rows += 1
            tcg_id = row["tcg_id"]
            if tcg_id in tcg_products:
                continue  # first-seen wins (original set)
            tcg_products[tcg_id] = {
                "price": row["price"],
                "set_id": s["set_id"],
                "name": row["name"],
                "name_suffix": row["name_suffix"],
            }
    print(f"Indexed {len(tcg_products)} unique TCGPlayer products across {total_rows} rows")

    # ── 2. Authoritative pass: dotgg tells us which tcg_id each card_id maps to.
    matched: dict[str, dict] = {}
    now = int(time.time())

    def upsert(card_id: str, price: float, tcg_ids: list[int], source_set: str, method: str) -> None:
        matched[card_id] = {
            "price": round(price, 2),
            "tcg_ids": tcg_ids,
            "source_set": source_set,
            "match_method": method,
            "price_updated_at": now,
        }

    authoritative = 0
    dotgg_fallback = 0
    dotgg_rejected_high = []
    no_dotgg_entry = []

    for card_id in by_id.keys():
        dotgg_row = dotgg.get(card_id)
        if not dotgg_row:
            no_dotgg_entry.append(card_id)
            continue

        tcg_ids = to_tcg_ids(dotgg_row.get("tcg_ids"))
        # Try TCGPlayer first — fresher, it's our primary market.
        tcg_match = next((tcg_products[tid] for tid in tcg_ids if tid in tcg_products), None)
        if tcg_match and tcg_match["price"] is not None:
            upsert(card_id, tcg_match["price"], tcg_ids, tcg_match["set_id"], "dotgg+tcgplayer")
            authoritative += 1
            continue

        # Dotgg price fallback (uses their cached value).
        price = to_float(dotgg_row.get("price"))
        if price <= 0:
            price = to_float(dotgg_row.get("foilPrice"))
        # Reject asking-price noise — see DOTGG_PRICE_CEILING note above.
        if price > DOTGG_PRICE_CEILING:
            dotgg_rejected_high.append({"card_id": card_id, "dotgg_price": round(price, 2), "tcg_ids": tcg_ids})
            continue
        if price > 0:
            upsert(card_id, price, tcg_ids, "dotgg", "dotgg-only")
            dotgg_fallback += 1

    print(f"\nAuthoritative pass (dotgg tcg_id -> TCGPlayer scrape): {authoritative}")
    print(f"Dotgg-own-price fallback:                              {dotgg_fallback}")
    print(f"Cards NOT in dotgg:                                    {len(no_dotgg_entry)}")
    if dotgg_rejected_high:
        rej = DATA_DIR / "dotgg_rejected_high.json"
        rej.write_text(json.dumps(dotgg_rejected_high, indent=2), encoding="utf-8")
        print(f"Dotgg prices rejected above ${DOTGG_PRICE_CEILING:.0f} (asking-price noise): "
              f"{len(dotgg_rejected_high)} -> {rej}; curate in data/manual_prices.json")

    # ── 3. Positional fallback ONLY for cards dotgg doesn't know about.
    # Uses the old logic — builds claim-state fresh, iterates sets in release order.
    claimed: set[str] = set()
    unmatched: list[dict] = []
    by_id_no_dotgg = set(no_dotgg_entry)
    # Skip a tcg_id once it has claimed a parallel. Otherwise a product
    # cross-listed across multiple guide MD files (e.g. an OP-01 alt in
    # 569901.md the universal Promotion bucket AND OP-01.md the set guide)
    # claims candidates[0] in the first iteration, then candidates[0]-after-
    # claim in the second, stamping the same tcg_id on two different
    # parallels. Saw this on 2026-05-22 for OP01-001 (485262 stamped on
    # both _p1 and _p2). See audit_optcg_positional_conflation.py.
    seen_tcg_ids: set[int] = set()

    # Re-use all scraped rows (not just deduped products)
    for s in sets_meta:
        md_path = RAW_DIR / f"{s['set_id']}.md"
        if not md_path.exists():
            continue
        for row in parse_md(md_path):
            suffix = row["name_suffix"]
            number = row["number"]
            # Base card fallback
            if suffix is None:
                if number in by_id_no_dotgg and number not in matched:
                    upsert(number, row["price"], [row["tcg_id"]], s["set_id"], "positional-base")
                continue
            if row["tcg_id"] in seen_tcg_ids:
                continue
            # Variant fallback — only among parallels not in dotgg
            target_type = VARIANT_LABEL_TO_TYPE.get(suffix, "alt_art")
            candidates = [
                c for c in parallels_by_base.get(number, [])
                if c["id"] in by_id_no_dotgg
                and c["id"] not in claimed
                and c.get("variant_type") == target_type
            ]
            if not candidates:
                continue
            chosen = candidates[0]
            claimed.add(chosen["id"])
            seen_tcg_ids.add(row["tcg_id"])
            upsert(chosen["id"], row["price"], [row["tcg_id"]], s["set_id"], "positional-parallel")

    positional = sum(1 for v in matched.values() if v["match_method"].startswith("positional"))
    print(f"Positional fallback (cards not in dotgg):              {positional}")

    # ── 4. Write outputs
    (DATA_DIR / "card_prices_all.json").write_text(
        json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DATA_DIR / "card_prices_all.unmatched.json").write_text(
        json.dumps(unmatched, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    methods = defaultdict(int)
    for v in matched.values():
        methods[v["match_method"]] += 1
    print(f"\nTotal matched cards: {len(matched)}")
    for method, count in sorted(methods.items(), key=lambda x: -x[1]):
        print(f"  {count:5}  {method}")
    print()
    print("Wrote data/card_prices_all.json")


if __name__ == "__main__":
    main()
