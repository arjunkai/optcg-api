from scripts.ebay_client import apply_title_filters
from scripts.ebay_client import trimmed_median
from scripts.ebay_client import consensus_price


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


def _priced(*prices):
    # Shape matches eBay Browse API item_summary shape (we only care about
    # the `price.value` and `title` fields).
    return [
        {"title": f"Card {i}", "price": {"value": str(p), "currency": "USD"}}
        for i, p in enumerate(prices)
    ]


def test_consensus_price_below_minimum_returns_none():
    items = _priced(10.0, 12.0)
    price, sample_size = consensus_price(items, min_count=3)
    assert price is None
    assert sample_size == 2


def test_consensus_price_meets_minimum_returns_trimmed_median():
    items = _priced(10.0, 12.0, 14.0)
    price, sample_size = consensus_price(items, min_count=3)
    assert price == 12.0
    assert sample_size == 3


def test_consensus_price_skips_items_missing_price():
    items = _priced(10.0, 12.0, 14.0)
    items.append({"title": "No price field"})
    items.append({"title": "Empty price", "price": {"value": "", "currency": "USD"}})
    price, sample_size = consensus_price(items, min_count=3)
    assert price == 12.0
    assert sample_size == 3


def test_consensus_price_currency_filter_drops_non_usd():
    items = [
        {"title": "A", "price": {"value": "10.00", "currency": "USD"}},
        {"title": "B", "price": {"value": "12.00", "currency": "USD"}},
        {"title": "C", "price": {"value": "14.00", "currency": "USD"}},
        {"title": "D", "price": {"value": "100.00", "currency": "JPY"}},
    ]
    price, sample_size = consensus_price(items, min_count=3)
    assert price == 12.0
    assert sample_size == 3


import json
import time
from pathlib import Path

import pytest
import respx
import httpx

from scripts.ebay_client import EbayClient, TOKEN_URL


@pytest.fixture
def token_path(tmp_path):
    return tmp_path / "ebay_token.json"


@respx.mock
def test_get_token_fetches_and_caches(token_path):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "abc123", "expires_in": 7200, "token_type": "Bearer"},
        )
    )
    client = EbayClient(app_id="app", cert_id="cert", token_path=token_path)
    token = client.get_token()
    assert token == "abc123"

    cached = json.loads(token_path.read_text())
    assert cached["access_token"] == "abc123"
    assert cached["expires_at"] > time.time() + 7000  # 2hr ttl with some slack


@respx.mock
def test_get_token_uses_cache_when_fresh(token_path):
    token_path.write_text(json.dumps({
        "access_token": "cached",
        "expires_at": time.time() + 3600,
    }))
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))
    client = EbayClient(app_id="app", cert_id="cert", token_path=token_path)
    assert client.get_token() == "cached"
    assert not route.called  # never hit the network


@respx.mock
def test_get_token_refreshes_when_expired(token_path):
    token_path.write_text(json.dumps({
        "access_token": "stale",
        "expires_at": time.time() - 10,
    }))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "fresh", "expires_in": 7200, "token_type": "Bearer"},
        )
    )
    client = EbayClient(app_id="app", cert_id="cert", token_path=token_path)
    assert client.get_token() == "fresh"


@respx.mock
def test_get_token_raises_on_auth_failure(token_path):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    client = EbayClient(app_id="bad", cert_id="bad", token_path=token_path)
    with pytest.raises(RuntimeError, match="eBay OAuth failed"):
        client.get_token()
