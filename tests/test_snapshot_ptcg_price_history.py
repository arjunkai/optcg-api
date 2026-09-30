"""Unit tests for the pure extraction in scripts/snapshot_ptcg_price_history.py."""

from scripts.snapshot_ptcg_price_history import history_rows


def test_all_tcgplayer_variants_one_cardmarket_series():
    pj = {
        "tcgplayer": {"holofoil": {"market": 12.5}, "reverseHolofoil": {"market": 3}, "normal": {"market": 0}},
        "cardmarket": {"avg": 9.1, "trend": 8.7, "avg7": 9.0, "lowFoil": 4.0},
    }
    assert history_rows(pj) == [
        ("tcgplayer", "holofoil", 12.5, None),
        ("tcgplayer", "reverseHolofoil", 3.0, None),
        ("cardmarket", "avg", None, 9.1),
    ]


def test_cardmarket_falls_back_in_chart_priority_order():
    assert history_rows({"cardmarket": {"avg": 0, "trend": None, "avg7": 2.2, "low": 1.0}}) == [
        ("cardmarket", "avg7", None, 2.2),
    ]


def test_single_price_sources_take_the_first_positive_key():
    pj = {"manual": {"price": 99}, "ebay": {"price": 0, "market": 5.5}, "yuyutei": {"note": "x"}}
    assert history_rows(pj) == [("manual", "market", 99.0, None), ("ebay", "market", 5.5, None)]


def test_junk_is_ignored():
    assert history_rows({"tcgplayer": "nope", "cardmarket": {"avg": True}, "manual": None}) == []
