"""Image-based promo verification and the tcgcsv One Piece fetcher."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from image_match import decide, similarity, tcgplayer_image  # noqa: E402
import build_all_prices  # noqa: E402
from build_all_prices import labels_agree, load_overrides, verify_by_image  # noqa: E402
from fetch_tcgcsv_optcg import pick_price, rows_for_group  # noqa: E402


# ── decide() ────────────────────────────────────────────────────────────────

def test_clear_better_match_moves():
    assert decide({1: 0.50, 2: 0.99}, current=1) == 2


def test_current_already_best_keeps():
    assert decide({1: 0.99, 2: 0.50}, current=1) is None


def test_weak_best_keeps():
    assert decide({1: 0.40, 2: 0.80}, current=1) is None


def test_two_identical_products_keep():
    # normal + foil listings with the same photo: can't tell them apart
    assert decide({1: 0.30, 2: 0.99, 3: 0.985}, current=1) is None


def test_small_margin_over_current_keeps():
    assert decide({1: 0.90, 2: 0.99}, current=1) is None


def test_unmapped_card_gets_clear_match():
    assert decide({7: 0.99, 8: 0.4}, current=None) == 7


def test_similarity_is_correlation():
    a = np.array([1.0, -1.0, 1.0, -1.0])
    assert similarity(a, a) == 1.0
    assert similarity(a, -a) == -1.0
    assert similarity(a, None) is None


# ── verify_by_image() with fake pictures ────────────────────────────────────

class FakeVectors:
    """url -> vector; similarity of two 'pictures' is set by shared components."""

    def __init__(self, table):
        self.table = table
        self.fetched = 0

    def ensure(self, urls):
        pass

    def get(self, url):
        return self.table.get(url)


def pic(seed):
    v = np.random.default_rng(seed).normal(size=792)
    return (v - v.mean()) / v.std()


def test_nami_winner_pair_is_kept_and_a_swap_is_fixed():
    winner, plain = pic(1), pic(2)
    cards = {
        "ST01-007_p4": {"id": "ST01-007_p4", "base_id": "ST01-007", "parallel": 1, "image_url": "off/p4"},
        "ST01-007_p5": {"id": "ST01-007_p5", "base_id": "ST01-007", "parallel": 1, "image_url": "off/p5"},
    }
    rows = {"ST01-007": [{"tcg_id": 610801, "price": 24.34}, {"tcg_id": 610802, "price": 2249.99}]}
    vecs = FakeVectors({"off/p4": plain, "off/p5": winner,
                        tcgplayer_image(610801): plain, tcgplayer_image(610802): winner})
    right = {"ST01-007_p4": 610801, "ST01-007_p5": 610802}
    assert verify_by_image(right, cards, rows, vecs) == {}
    swapped = {"ST01-007_p4": 610802, "ST01-007_p5": 610801}
    assert verify_by_image(swapped, cards, rows, vecs) == {"ST01-007_p4": 610801, "ST01-007_p5": 610802}


def test_does_not_take_a_product_another_print_owns():
    art = pic(3)
    cards = {
        "OP01-001": {"id": "OP01-001", "base_id": None, "parallel": 0, "image_url": "off/base"},
        "OP01-001_r1": {"id": "OP01-001_r1", "base_id": "OP01-001", "parallel": 1, "image_url": "off/r1"},
    }
    rows = {"OP01-001": [{"tcg_id": 1, "price": 1.0}, {"tcg_id": 2, "price": 0.5}]}
    # reprint looks exactly like the base product; its own product has no photo
    vecs = FakeVectors({"off/base": art, "off/r1": art, tcgplayer_image(1): art})
    assign = {"OP01-001": 1, "OP01-001_r1": 2}
    assert verify_by_image(assign, cards, rows, vecs) == {}


def mix(a, b, w):
    """A picture whose similarity to `a` is about w."""
    v = w * a + (1 - w * w) ** 0.5 * b
    return (v - v.mean()) / v.std()


def test_unresolved_mismatch_is_reported():
    # Another product looks more like the card (~0.6) than the linked one,
    # but not clearly enough to move it.
    card = pic(10)
    cards = {"P-001_p1": {"id": "P-001_p1", "base_id": "P-001", "parallel": 1, "image_url": "off/x"}}
    rows = {"P-001": [{"tcg_id": 5, "price": 3.0}, {"tcg_id": 6, "price": 4.0}]}
    vecs = FakeVectors({"off/x": card, tcgplayer_image(5): pic(11), tcgplayer_image(6): mix(card, pic(12), 0.6)})
    unresolved = []
    assert verify_by_image({"P-001_p1": 5}, cards, rows, vecs, unresolved) == {}
    assert [u["card_id"] for u in unresolved] == ["P-001_p1"]


def test_poor_photo_of_the_linked_product_is_not_reported():
    # The linked product is already the best match; its photo is just poor.
    card = pic(13)
    cards = {"ST10-006_p2": {"id": "ST10-006_p2", "base_id": "ST10-006", "parallel": 1, "image_url": "off/x"}}
    rows = {"ST10-006": [{"tcg_id": 5, "price": 399.91}, {"tcg_id": 6, "price": 1.0}]}
    vecs = FakeVectors({"off/x": card, tcgplayer_image(5): mix(card, pic(14), 0.75), tcgplayer_image(6): pic(15)})
    unresolved = []
    assert verify_by_image({"ST10-006_p2": 5}, cards, rows, vecs, unresolved) == {}
    assert unresolved == []


def test_labels_agree():
    assert labels_agree("Japanese 2nd Anniversary Set", "One Piece Japanese Version 2nd Anniversary Set")
    assert labels_agree("Premium Card Collection -Best Selection-", "Premium Card Collection -Best Selection Vol. 1-")
    assert labels_agree("OP-12 Release Event", "OP12 Release Event")
    # an extra stamp word is a different print
    assert not labels_agree("OP-12 Release Event", "OP12 Release Event Winner")
    assert not labels_agree("CS 2024 Event Pack", "CS 2024 Event Pack Finalist")
    # set texts name a pack, not a print
    assert not labels_agree("-ROMANCE DAWN- [OP01]", "Romance Dawn OP01")
    assert not labels_agree(None, "Alternate Art")


def test_official_label_backs_a_weaker_picture_match():
    # OP05-030_p2: official "Japanese 2nd Anniversary Set", linked to the
    # sibling's Alternate Art product. The anniversary photo scores ~0.65,
    # under MATCH, but the label names it.
    card = pic(30)
    cards = {"OP05-030_p2": {"id": "OP05-030_p2", "base_id": "OP05-030", "parallel": 1, "image_url": "off/x"}}
    rows = {"OP05-030": [
        {"tcg_id": 1, "price": 4.63, "name_suffix": "Alternate Art"},
        {"tcg_id": 2, "price": 41.15, "name_suffix": "One Piece Japanese Version 2nd Anniversary Set"},
    ]}
    vecs = FakeVectors({"off/x": card, tcgplayer_image(1): mix(card, pic(31), 0.45),
                        tcgplayer_image(2): mix(card, pic(32), 0.65)})
    official = {"OP05-030_p2": "Japanese 2nd Anniversary Set"}
    assert verify_by_image({"OP05-030_p2": 1}, cards, rows, vecs, None, official) == {"OP05-030_p2": 2}
    # without the label it stays put
    assert verify_by_image({"OP05-030_p2": 1}, cards, rows, vecs) == {}
    # a label naming a stamp the product lacks doesn't back it
    assert verify_by_image({"OP05-030_p2": 1}, cards, rows, vecs, None,
                           {"OP05-030_p2": "Japanese 2nd Anniversary Set Winner"}) == {}


def test_load_overrides(tmp_path, monkeypatch):
    f = tmp_path / "overrides.json"
    f.write_text('{"_readme": "x", "A_p1": {"tcg_id": 7, "note": "n"}, "B": 8, "gone": 7, "A_p2": 99}',
                 encoding="utf-8")
    monkeypatch.setattr(build_all_prices, "OVERRIDES_FILE", f)
    cards = {"A_p1": {}, "B": {}, "A_p2": {}}
    products = {7: {"price": 1.0}, 8: {"price": 2.0}}
    assert load_overrides(cards, products) == {"A_p1": 7, "B": 8}


# ── tcgcsv rows ─────────────────────────────────────────────────────────────

def test_pick_price_prefers_normal_then_foil():
    assert pick_price([{"subTypeName": "Foil", "marketPrice": 8.0}]) == 8.0
    assert pick_price([{"subTypeName": "Foil", "marketPrice": 8.0},
                       {"subTypeName": "Normal", "marketPrice": 0.5}]) == 0.5
    assert pick_price([{"subTypeName": "Normal", "marketPrice": None},
                       {"subTypeName": "Foil", "marketPrice": 9.0}]) == 9.0
    assert pick_price([]) is None


def test_rows_for_group_labels_and_skips_sealed():
    group = {"groupId": 24736, "name": "The World's Strongest Warriors"}
    products = [
        {"productId": 712083, "name": "Edward.Newgate (005) (Manga)", "imageUrl": "u",
         "extendedData": [{"name": "Number", "value": "OP17-005"}, {"name": "Rarity", "value": "SR"}]},
        {"productId": 1, "name": "Booster Box", "extendedData": []},
    ]
    prices = [{"productId": 712083, "subTypeName": "Foil", "marketPrice": 1058.07}]
    rows = rows_for_group(group, products, prices)
    assert rows == [{"tcg_id": 712083, "name": "Edward.Newgate (005)", "name_suffix": "Manga",
                     "number": "OP17-005", "rarity": "SR", "price": 1058.07,
                     "group_id": 24736, "group_name": "The World's Strongest Warriors", "image_url": "u"}]


def test_japan_exclusive_rows_are_never_image_matched():
    art = pic(20)
    cards = {"OP07-064_p3": {"id": "OP07-064_p3", "base_id": "OP07-064", "parallel": 1,
                             "image_url": "https://www.onepiece-cardgame.com/images/cardlist/card/OP07-064_p3.png"}}
    rows = {"OP07-064": [{"tcg_id": 9, "price": 799.99}]}
    vecs = FakeVectors({cards["OP07-064_p3"]["image_url"]: art, tcgplayer_image(9): art})
    assert verify_by_image({}, cards, rows, vecs) == {}
