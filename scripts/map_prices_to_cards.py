"""
Map TCGPlayer raw price rows -> our card_id schema.

Usage:
  python scripts/map_prices_to_cards.py <prices.json> <output.json>

Reads data/cards.json once to resolve parallels.

Output row per matched card_id:
{
  "price":        0.78,           # Normal printing (base) OR null
  "foil_price":   1.40,           # Foil printing (base) OR parallel price
  "tcg_ids":      [596948, 596949],
  "source":       "tcgplayer",
}

Heuristic for mapping variant rows to _pN:
  Manga         -> first parallel where variant_type == 'manga'
  all others    -> first unmatched parallel where variant_type == 'alt_art'
                   (iteration order = card_id ascending, so _p1 first)

Unmatched rows are written to <output>.unmatched.json for review.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

CARDS_JSON = Path("data/cards.json")

# Which of our variant_type values a TCGPlayer variant label maps to.
# Unknown TCG labels fall back to 'alt_art'.
VARIANT_LABEL_TO_TYPE = {
    "Manga": "manga",
    "Manga Rare": "manga",
    "Parallel": "alt_art",
    "Alternate Art": "alt_art",
    "Wanted Poster": "alt_art",
    "SP": "alt_art",
    "Special": "alt_art",
    "Full Art": "alt_art",
    "Treasure Rare": "alt_art",
    "Box Topper": "alt_art",
    "Reprint": "reprint",
    "Pirate Foil": "reprint",
}


# Legacy variant labels that still appear on some cards (from raw scraper
# output before variant_types.json overrides). Normalize to snake_case codes.
LEGACY_VARIANT_NORMALIZE = {
    "Alternate Art": "alt_art",
    "Alt Art": "alt_art",
    "Manga Art": "manga",
    "Manga": "manga",
    "Reprint": "reprint",
    "Serial": "serial",
}


def normalize_variant(v):
    if not v:
        return None
    return LEGACY_VARIANT_NORMALIZE.get(v, v)


def load_cards():
    with CARDS_JSON.open(encoding="utf-8") as f:
        cards = json.load(f)
    # Normalize variant_type so parallel matching works regardless of whether
    # cards.json carries legacy display labels ("Alternate Art") or snake_case.
    for c in cards:
        c["variant_type"] = normalize_variant(c.get("variant_type"))
    # index by id for O(1) lookup
    by_id = {c["id"]: c for c in cards}
    # parallels grouped by base_id -> list sorted by id (p1, p2, ...)
    parallels_by_base: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        if c.get("parallel") and c.get("base_id"):
            parallels_by_base[c["base_id"]].append(c)
    for group in parallels_by_base.values():
        group.sort(key=lambda x: x["id"])
    return by_id, parallels_by_base


def map_rows(rows: list[dict], by_id: dict, parallels_by_base: dict):
    """Resolve each row to a card_id. Returns (matched, unmatched).

    matched[card_id] = {price, foil_price, tcg_ids, ...}
    """
    matched: dict[str, dict] = {}
    unmatched: list[dict] = []

    # Track which parallels have already been claimed so two TCG rows
    # don't both grab the same _pN.
    claimed_parallels: set[str] = set()

    def upsert(card_id: str, row: dict) -> None:
        entry = matched.setdefault(card_id, {
            "price": None,
            "tcg_ids": [],
        })
        if row["tcg_id"] not in entry["tcg_ids"]:
            entry["tcg_ids"].append(row["tcg_id"])
        # Each card gets one market price. TCGPlayer's markdown only exposes
        # the default printing's price; Normal-vs-Foil dropdowns are collapsed.
        # If a second row resolves to the same card_id, keep the first price.
        if entry["price"] is None:
            entry["price"] = row["price"]

    for row in rows:
        number = row["number"]
        suffix = row["name_suffix"]

        # Base (no variant suffix)
        if suffix is None:
            if number in by_id:
                upsert(number, row)
            else:
                unmatched.append({**row, "reason": "base number not in our DB"})
            continue

        # Variant row — find the right parallel
        target_type = VARIANT_LABEL_TO_TYPE.get(suffix, "alt_art")
        candidates = [
            c for c in parallels_by_base.get(number, [])
            if c.get("variant_type") == target_type
            and c["id"] not in claimed_parallels
        ]
        if not candidates:
            unmatched.append({
                **row,
                "reason": f"no unclaimed {target_type} parallel for {number}",
            })
            continue

        chosen = candidates[0]
        claimed_parallels.add(chosen["id"])
        upsert(chosen["id"], row)

    return matched, unmatched


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: map_prices_to_cards.py <prices.json> <output.json>")
        sys.exit(2)

    prices_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    with prices_path.open(encoding="utf-8") as f:
        blob = json.load(f)

    by_id, parallels_by_base = load_cards()
    matched, unmatched = map_rows(blob["rows"], by_id, parallels_by_base)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({
            "set_id": blob.get("set_id"),
            "cards": matched,
        }, f, ensure_ascii=False, indent=2)

    unmatched_path = out_path.with_suffix(".unmatched.json")
    with unmatched_path.open("w", encoding="utf-8") as f:
        json.dump(unmatched, f, ensure_ascii=False, indent=2)

    print(f"  {prices_path.name}")
    print(f"  Matched: {len(matched)} cards")
    print(f"  Unmatched: {len(unmatched)} rows")
    print(f"  Wrote {out_path} (+ .unmatched.json)")


if __name__ == "__main__":
    main()
