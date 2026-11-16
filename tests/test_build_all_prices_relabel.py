"""correct_shared_tcg_ids in scripts/build_all_prices.py.

dotgg mapped the OP-17 Newgate and Linlin mangas to their base / alt-art
products (shown at $0.70 and $14.82 against ~$1,058 and ~$709). These pin the
fix, and that a right shared mapping (OP01-120_p2, the OP-01 Shanks manga,
shares its product with the PRB-01 manga reprint) is never moved.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_all_prices import correct_shared_tcg_ids  # noqa: E402


def card(cid, vt=None):
    base = cid.split("_")[0]
    par = "_" in cid
    return cid, {"id": cid, "base_id": base if par else None, "parallel": 1 if par else 0, "variant_type": vt}


def prod(tid, number, suffix, name="X", price=1.0):
    return {"tcg_id": tid, "number": number, "name_suffix": suffix, "name": name, "price": price}


def run(cards, assign, rows):
    cards = dict(cards)
    products = {r["tcg_id"]: r for r in rows}
    by_num = {}
    for r in rows:
        by_num.setdefault(r["number"], []).append(r)
    return correct_shared_tcg_ids(assign, cards, products, by_num)


def test_op17_newgate_both_parallels_move_off_the_base_product():
    rows = [prod(711499, "OP17-005", None, price=0.7),
            prod(708077, "OP17-005", "Alternate Art", price=8.48),
            prod(712083, "OP17-005", "Manga", price=1058.07)]
    cards = [card("OP17-005"), card("OP17-005_p1", "alt_art"), card("OP17-005_p2", "manga")]
    assign = {"OP17-005": 711499, "OP17-005_p1": 711499, "OP17-005_p2": 711499}
    assert run(cards, assign, rows) == {"OP17-005_p1": 708077, "OP17-005_p2": 712083}


def test_op17_linlin_manga_moves_off_the_alt_art_product():
    rows = [prod(711325, "OP17-112", None),
            prod(711324, "OP17-112", "Alternate Art", price=14.82),
            prod(708072, "OP17-112", "Manga", price=709.14)]
    cards = [card("OP17-112"), card("OP17-112_p1", "alt_art"), card("OP17-112_p2", "manga")]
    assign = {"OP17-112": 711325, "OP17-112_p1": 711324, "OP17-112_p2": 711324}
    assert run(cards, assign, rows) == {"OP17-112_p2": 708072}


def test_shared_manga_product_with_manga_label_in_name_is_left_alone():
    # "Shanks (Parallel) (Manga) (Alternate Art)": the parser keeps only the
    # last label, but the product is the manga. _p2 and _p4 both own it.
    rows = [prod(454664, "OP01-120", None),
            prod(454666, "OP01-120", "Alternate Art", name="Shanks (Parallel) (Manga)", price=2038.87),
            prod(587708, "OP01-120", "Manga", price=1237.49)]
    cards = [card("OP01-120"), card("OP01-120_p2", "manga"), card("OP01-120_p4", "manga")]
    assign = {"OP01-120": 454664, "OP01-120_p2": 454666, "OP01-120_p4": 454666}
    assert run(cards, assign, rows) == {}


def test_unique_mapping_is_never_touched_even_if_labels_disagree():
    # OP17-079_p2 is "Super Leader Alternate Art" on TCGPlayer, manga for us.
    rows = [prod(712114, "OP17-079", "Super Leader Alternate Art", price=1214.98),
            prod(999, "OP17-079", "Manga")]
    cards = [card("OP17-079_p2", "manga")]
    assert run(cards, {"OP17-079_p2": 712114}, rows) == {}


def test_promo_labels_are_never_targets():
    rows = [prod(454667, "OP01-121", None), prod(552709, "OP01-121", "Treasure Cup", price=1483.0)]
    cards = [card("OP01-121"), card("OP01-121_p3", "alt_art")]
    assert run(cards, {"OP01-121": 454667, "OP01-121_p3": 454667}, rows) == {}


def test_two_candidates_keeps_dotgg():
    rows = [prod(1, "OP09-001", None), prod(2, "OP09-001", "Reprint"), prod(3, "OP09-001", "Reprint")]
    cards = [card("OP09-001"), card("OP09-001_r1", "reprint")]
    assert run(cards, {"OP09-001": 1, "OP09-001_r1": 1}, rows) == {}


# ── match_promo_labels: official card-set label -> TCGPlayer product label ──

from build_all_prices import match_promo_labels  # noqa: E402


def _rbn(rows):
    out = {}
    for r in rows:
        out.setdefault(r["number"], []).append(r)
    return out


def test_sanji_celebration_pack_matches_its_labeled_product():
    rows = [prod(544542, "EB01-014", None, price=0.87),
            prod(607976, "EB01-014", "Tournament Pack 2025 Vol. 1", price=0.50),
            prod(607971, "EB01-014", "Winner Pack 2025 Vol. 1", price=8.80),
            prod(649632, "EB01-014", "CS 25-26 Celebration Pack", price=67.58)]
    cards = dict([card("EB01-014"), card("EB01-014_p1", "alt_art")])
    dotgg = {"EB01-014": {"CardSets": "-Memorial Collection- [EB-01]"},
             "EB01-014_p1": {"CardSets": "CS 25-26 Celebration Pack"}}
    assert match_promo_labels(cards, dotgg, _rbn(rows)) == {"EB01-014_p1": 649632}


def test_set_text_is_not_a_promo_label():
    rows = [prod(1, "OP07-109", None)]
    cards = dict([card("OP07-109_p1", "alt_art")])
    dotgg = {"OP07-109_p1": {"CardSets": "-500 YEARS IN THE FUTURE- [OP-07]"}}
    assert match_promo_labels(cards, dotgg, _rbn(rows)) == {}


def test_two_cards_with_the_same_label_are_ambiguous():
    rows = [prod(9, "OP01-016", "CS 25-26 Celebration Pack")]
    cards = dict([card("OP01-016_p5", "alt_art"), card("OP01-016_p6", "alt_art")])
    dotgg = {"OP01-016_p5": {"CardSets": "CS 25-26 Celebration Pack"},
             "OP01-016_p6": {"CardSets": "CS 25-26 Celebration Pack"}}
    assert match_promo_labels(cards, dotgg, _rbn(rows)) == {}
