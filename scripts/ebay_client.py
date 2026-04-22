"""
Shared eBay Browse API client for the optcg-api pricing pipeline.

Consumers:
  - scripts/backfill_prices_ebay.py  (phase 1 — gap-fill)

Future consumers (not built yet — do not import anything they need):
  - scripts/cross_validate_prices.py  (phase 2)
  - scripts/verify_jp_exclusives_ebay.py  (phase 3)

Keeps OAuth, rate-limited search, and authenticity defenses in one place
so every consumer gets the same filtering behavior.
"""

from __future__ import annotations

from statistics import median
from typing import Iterable


DEFAULT_BLOCKLIST: tuple[str, ...] = (
    "proxy",
    "custom art",
    "fan made",
    "fan-made",
    "replica",
    "fake",
    "not authentic",
    "art only",
    "fanart",
    "fan art",
)


def apply_title_filters(
    items: list[dict],
    *,
    require_any: Iterable[str] | None = None,
    blocklist: Iterable[str] | None = None,
) -> list[dict]:
    """Filter eBay listing items by their `title` field.

    - Any item whose title contains a blocklist term (case-insensitive) is
      dropped.
    - If `require_any` is a non-empty iterable, items must also contain at
      least one of those terms to survive. An empty or None `require_any`
      applies no positive requirement.
    """
    block = tuple(t.lower() for t in (blocklist if blocklist is not None else DEFAULT_BLOCKLIST))
    required = tuple(t.lower() for t in (require_any or ()))

    out = []
    for item in items:
        title = (item.get("title") or "").lower()
        if any(term in title for term in block):
            continue
        if required and not any(term in title for term in required):
            continue
        out.append(item)
    return out


def trimmed_median(prices: list[float], trim_pct: float = 0.20) -> float | None:
    """Drop the top and bottom `trim_pct` of `prices`, return the median of
    what remains. Returns None for an empty input. For inputs of length 1 or
    2, returns the regular median (trim count would be zero)."""
    if not prices:
        return None
    sorted_prices = sorted(prices)
    n = len(sorted_prices)
    trim = int(n * trim_pct)
    trimmed = sorted_prices[trim : n - trim] if trim > 0 else sorted_prices
    return float(median(trimmed))


def consensus_price(
    items: list[dict],
    *,
    min_count: int = 3,
    currency: str = "USD",
) -> tuple[float | None, int]:
    """Extract USD prices from eBay item_summary listings, trim outliers,
    and return (median, sample_size). Returns (None, sample_size) when
    fewer than `min_count` usable listings are present.

    Expects eBay Browse API shape: items[i]["price"] = {"value": "12.34",
    "currency": "USD"}. Items missing a price or in a different currency
    are skipped, not counted toward min_count.
    """
    prices: list[float] = []
    for item in items:
        price = item.get("price") or {}
        if price.get("currency") != currency:
            continue
        raw = price.get("value")
        if not raw:
            continue
        try:
            prices.append(float(raw))
        except (TypeError, ValueError):
            continue

    if len(prices) < min_count:
        return None, len(prices)
    return trimmed_median(prices), len(prices)
