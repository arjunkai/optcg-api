"""
Audit how many DON PDF images can be auto-matched to TCGPlayer DON cards.

Inputs:
  data/don_image_map.json   — 275 PDF images tagged with set_id
  data/don_cards.json       — 195 DON cards with tcg_id + canonical set_id

Output: printed summary showing, per set:
  - how many PDF images we have
  - how many TCGPlayer DON cards exist
  - whether counts match (1:1 auto-mappable) or diverge (manual pairing needed)
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


DATA = Path("data")


def main() -> None:
    images = json.loads((DATA / "don_image_map.json").read_text(encoding="utf-8"))
    cards = json.loads((DATA / "don_cards.json").read_text(encoding="utf-8"))

    # Images by PDF set_id (can be None if unknown, or a DON-* pseudo-set, or a real OP/PRB/etc set)
    images_by_set = defaultdict(list)
    for img in images:
        images_by_set[img.get("set_id")].append(img)

    # Cards by canonical set_id
    cards_by_set = defaultdict(list)
    for c in cards:
        cards_by_set[c["set_id"]].append(c)

    # Universe of all sets across both sources
    all_sets = set(images_by_set) | set(cards_by_set)

    # Tidy: put None/pseudo-DON sets first, real sets after
    def sort_key(s):
        if s is None:
            return (0, "")
        if s.startswith("DON-"):
            return (1, s)
        return (2, s)

    auto_match_total = 0
    pdf_total = 0
    card_total = 0
    unmatched_pdf_sets = []
    unmatched_card_sets = []
    mismatched_counts = []
    one_to_one_sets = []

    print(f"{'set_id':<20} {'pdf_imgs':>10} {'tcg_cards':>10}  status")
    print("-" * 70)
    for s in sorted(all_sets, key=sort_key):
        n_img = len(images_by_set.get(s, []))
        n_card = len(cards_by_set.get(s, []))
        pdf_total += n_img
        card_total += n_card

        if n_img == 0:
            status = "NO PDF IMAGES (TCGPlayer-only set)"
            unmatched_card_sets.append((s, n_card))
        elif n_card == 0:
            status = "NO TCGPLAYER CARDS (PDF-only pseudo-set)"
            unmatched_pdf_sets.append((s, n_img))
        elif n_img == n_card:
            status = "1:1 — auto-mappable by position"
            one_to_one_sets.append(s)
            auto_match_total += n_img
        else:
            status = f"MISMATCH: {n_img} imgs vs {n_card} cards — manual pairing"
            mismatched_counts.append((s, n_img, n_card))

        display = s if s is not None else "<none>"
        print(f"{display:<20} {n_img:>10} {n_card:>10}  {status}")

    print()
    print(f"Total PDF images:      {pdf_total}")
    print(f"Total TCGPlayer DONs:  {card_total}")
    print(f"Auto-mappable (1:1):   {auto_match_total}")
    print()
    print(f"PDF-only pseudo-sets ({len(unmatched_pdf_sets)} sets, {sum(n for _, n in unmatched_pdf_sets)} images):")
    for s, n in unmatched_pdf_sets:
        print(f"  {s}: {n}")
    print()
    print(f"TCGPlayer-only sets ({len(unmatched_card_sets)} sets, {sum(n for _, n in unmatched_card_sets)} cards):")
    for s, n in unmatched_card_sets:
        print(f"  {s}: {n}")
    print()
    print(f"Count mismatches needing manual pairing ({len(mismatched_counts)} sets):")
    for s, ni, nc in mismatched_counts:
        print(f"  {s}: {ni} imgs vs {nc} cards")


if __name__ == "__main__":
    main()
