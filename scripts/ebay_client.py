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

import base64
import json
import os
import time
from pathlib import Path
from statistics import median
from typing import Iterable

import httpx


TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
DEFAULT_TOKEN_PATH = Path("data/.ebay_token.json")


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


class EbayClient:
    """eBay Browse API client with cached client-credentials OAuth.

    The access token lives for ~2h; we cache it to disk so repeated script
    runs (weekly pipeline, local dry-runs) don't re-auth unnecessarily.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        cert_id: str | None = None,
        token_path: Path | None = None,
    ) -> None:
        self.app_id = app_id or os.environ.get("EBAY_APP_ID")
        self.cert_id = cert_id or os.environ.get("EBAY_CERT_ID")
        if not self.app_id or not self.cert_id:
            raise RuntimeError(
                "EBAY_APP_ID and EBAY_CERT_ID must be set as env vars "
                "or passed to EbayClient()"
            )
        self.token_path = Path(token_path) if token_path else DEFAULT_TOKEN_PATH

    def get_token(self) -> str:
        cached = self._read_cache()
        if cached and cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]

        auth = base64.b64encode(f"{self.app_id}:{self.cert_id}".encode()).decode()
        resp = httpx.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"eBay OAuth failed: {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        expires_at = time.time() + int(payload.get("expires_in", 7200))
        self._write_cache({"access_token": payload["access_token"], "expires_at": expires_at})
        return payload["access_token"]

    def _read_cache(self) -> dict | None:
        try:
            return json.loads(self.token_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _write_cache(self, data: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(data))

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        category_ids: str | None = None,
        max_retries: int = 4,
    ) -> list[dict]:
        """Search the Browse API. Returns a list of item_summary dicts.

        Retries on 429 with exponential backoff (1s, 2s, 4s, 8s). Raises
        RuntimeError if eBay keeps rate-limiting us past `max_retries`.
        """
        token = self.get_token()
        params = {"q": query, "limit": str(limit)}
        if category_ids:
            params["category_ids"] = category_ids
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        }

        for attempt in range(max_retries):
            resp = httpx.get(
                BROWSE_SEARCH_URL, params=params, headers=headers, timeout=30
            )
            if resp.status_code == 200:
                return resp.json().get("itemSummaries", []) or []
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"eBay search failed: {resp.status_code} {resp.text[:300]}"
            )
        raise RuntimeError(f"eBay search rate limited after {max_retries} attempts")
