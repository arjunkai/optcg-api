from scripts.ebay_client import apply_title_filters
from scripts.ebay_client import trimmed_median


def _items(*titles):
    return [{"title": t} for t in titles]


def test_apply_title_filters_empty_returns_input():
    items = _items("Monkey D. Luffy OP01-001")
    assert apply_title_filters(items) == items


def test_apply_title_filters_blocklist_drops_fake_markers():
    items = _items(
        "Monkey D. Luffy OP01-001",
        "Monkey D. Luffy OP01-001 proxy",
        "Monkey D. Luffy OP01-001 CUSTOM ART",
        "Monkey D. Luffy OP01-001 fan-made",
        "Monkey D. Luffy OP01-001 replica",
        "Monkey D. Luffy OP01-001 NOT authentic",
        "Monkey D. Luffy OP01-001 FANART",
        "Monkey D. Luffy OP01-001 with custom sleeves",
    )
    result = apply_title_filters(items)
    assert len(result) == 2
    assert result[0]["title"] == "Monkey D. Luffy OP01-001"
    assert result[1]["title"] == "Monkey D. Luffy OP01-001 with custom sleeves"


def test_apply_title_filters_blocklist_catches_fan_art_with_space():
    items = _items(
        "Custom fan art OP01-001",
        "Monkey D. Luffy OP01-001",
    )
    result = apply_title_filters(items)
    assert len(result) == 1
    assert result[0]["title"] == "Monkey D. Luffy OP01-001"


def test_apply_title_filters_require_any_keeps_matches():
    items = _items(
        "Trafalgar Law ST03-008 Japanese",
        "Trafalgar Law ST03-008 English",
        "Trafalgar Law ST03-008",
    )
    result = apply_title_filters(items, require_any=["Japanese", "JP", "JPN"])
    assert len(result) == 1
    assert result[0]["title"] == "Trafalgar Law ST03-008 Japanese"


def test_apply_title_filters_case_insensitive():
    items = _items(
        "CARD PROXY",
        "Card Proxy",
        "card proxy",
    )
    assert apply_title_filters(items) == []


def test_apply_title_filters_require_any_empty_list_means_no_requirement():
    items = _items("Trafalgar Law ST03-008")
    assert apply_title_filters(items, require_any=[]) == items


def test_trimmed_median_empty_returns_none():
    assert trimmed_median([]) is None


def test_trimmed_median_single_value():
    assert trimmed_median([5.0]) == 5.0


def test_trimmed_median_three_values_trim_drops_extremes():
    # 20% trim on 3 values drops 0 from each side, median of [1,2,3] = 2.
    assert trimmed_median([1.0, 2.0, 3.0]) == 2.0


def test_trimmed_median_ten_values_trims_top_and_bottom_20pct():
    # 20% of 10 = 2 per side; [1,2] and [9,10] dropped; median of 3..8 = 5.5.
    prices = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert trimmed_median(prices) == 5.5


def test_trimmed_median_rejects_outliers():
    # One huge outlier and one tiny one get trimmed before median.
    prices = [1.0, 100.0, 105.0, 110.0, 115.0, 120.0, 125.0, 130.0, 135.0, 9999.0]
    # Trim 2 per side → median of [105,110,115,120,125,130] = 117.5
    assert trimmed_median(prices) == 117.5
