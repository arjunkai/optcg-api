from scripts.ebay_client import apply_title_filters


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
