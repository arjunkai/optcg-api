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
