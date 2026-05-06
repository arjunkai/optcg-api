"""
Shared eBay API client for the optcg-api pricing pipeline.

Currently used:
  - scripts/backfill_prices_ebay.py            (Browse / active listings)
  - scripts/backfill_ptcg_prices_ebay.py       (Browse / active listings)
  - scripts/backfill_ptcg_images_ebay.py       (Browse / item photos)

Available helpers (use as needed):
  - EbayClient.search()          Browse API item_summary/search (active)
  - EbayClient.get_items()       Browse API item/getItems (batch up to 20)
  - EbayClient.search_sales()    Marketplace Insights item_sales/search (sold,
                                 last 90 days). Limited Release — your app
                                 must be approved via Application Growth
                                 Check before this works in production.
  - EbayClient.translate()       Commerce Translation translate (e.g. ja→en
                                 for query-string normalization). Open to
                                 every registered dev.

Auth:
  client_credentials grant. Each call passes its required scope; tokens
  are cached per-scope on disk so an approved partner can rotate
  Marketplace Insights tokens independently of Browse tokens.

Defenses (apply to every search-derived price):
  apply_title_filters() — drop proxy/fake/replica listings, optional
                          require_any whitelist for must-include terms.
  consensus_price()     — require ≥3 listings, take 20% trimmed median.

Marketplace caveats:
  - Browse API rejects EBAY_JP with HTTP 409 (verified 2026-05). For JA
    pricing on Browse alone, query EBAY_US with English-translated card
    names — US sellers routinely list JA cards in English.
  - Marketplace Insights DOES support EBAY_JP. Once you have access,
    JA pricing should switch to search_sales(marketplace_id="EBAY_JP").
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from statistics import median
from typing import Iterable

import httpx


TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
BROWSE_GET_ITEMS_URL = "https://api.ebay.com/buy/browse/v1/item"  # /?item_ids=...
MARKETPLACE_INSIGHTS_SEARCH_URL = (
    "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
)
TRANSLATION_URL = "https://api.ebay.com/commerce/translation/v1_beta/translate"

# OAuth scopes. Browse + getItems share the default api_scope.
SCOPE_BROWSE = "https://api.ebay.com/oauth/api_scope"
SCOPE_MARKETPLACE_INSIGHTS = (
    "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"
)
SCOPE_TRANSLATION = "https://api.ebay.com/oauth/api_scope/commerce.translation"

DEFAULT_TOKEN_DIR = Path("data/.ebay_tokens")


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
    price_field: str = "price",
) -> tuple[float | None, int]:
    """Extract prices from listings, trim outliers, return (median, sample_size).

    Returns (None, sample_size) when fewer than `min_count` usable listings
    are present. `price_field` switches between Browse API ('price') and
    Marketplace Insights ('lastSoldPrice') response shapes.
    """
    prices: list[float] = []
    for item in items:
        price = item.get(price_field) or {}
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


def _scope_cache_path(scope: str, base: Path) -> Path:
    """Map a scope URL to a stable cache filename. Browse scope hashes to
    'default'; specialized scopes use the trailing path segment so the
    file name is human-readable on disk."""
    if scope == SCOPE_BROWSE:
        return base / "default.json"
    tail = re.sub(r"[^a-zA-Z0-9._-]", "_", scope.rsplit("/", 1)[-1] or "scope")
    return base / f"{tail}.json"


class EbayAccessDeniedError(RuntimeError):
    """Raised when eBay returns 401/403 for a scope your app isn't approved for.

    Distinct from generic RuntimeError so callers can offer a friendly
    message ("Marketplace Insights requires Application Growth Check
    approval; falling back to Browse API search").
    """


class EbayClient:
    """eBay API client with cached client-credentials OAuth, per-scope.

    Tokens live ~2h. We cache one file per scope under
    `data/.ebay_tokens/<scope>.json` so an approved Marketplace
    Insights scope and the default Browse scope don't collide.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        cert_id: str | None = None,
        token_dir: Path | None = None,
    ) -> None:
        self.app_id = app_id or os.environ.get("EBAY_APP_ID")
        self.cert_id = cert_id or os.environ.get("EBAY_CERT_ID")
        if not self.app_id or not self.cert_id:
            raise RuntimeError(
                "EBAY_APP_ID and EBAY_CERT_ID must be set as env vars "
                "or passed to EbayClient()"
            )
        self.token_dir = Path(token_dir) if token_dir else DEFAULT_TOKEN_DIR

    # ── OAuth ──

    def get_token(self, scope: str = SCOPE_BROWSE) -> str:
        cache_path = _scope_cache_path(scope, self.token_dir)
        cached = self._read_cache(cache_path)
        if cached and cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]

        auth = base64.b64encode(f"{self.app_id}:{self.cert_id}".encode()).decode()
        resp = httpx.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": scope},
            timeout=30,
        )
        if resp.status_code in (401, 403) or (
            resp.status_code == 400 and "invalid_scope" in resp.text
        ):
            # Most common cause: scope not enabled on this keyset.
            # Open-access scopes (commerce.translation, commerce.catalog)
            # need to be ticked on at https://developer.ebay.com/my/keys.
            # Limited-Release scopes (buy.marketplace.insights, buy.feed)
            # additionally need Application Growth Check approval.
            raise EbayAccessDeniedError(
                f"eBay rejected scope {scope!r}: {resp.status_code} {resp.text[:300]}\n"
                "Likely fixes:\n"
                "  - Open-access scopes: enable on the OAuth Scopes page of your\n"
                "    keyset at https://developer.ebay.com/my/keys\n"
                "  - Limited-Release scopes (Marketplace Insights, Feed): file an\n"
                "    Application Growth Check at\n"
                "    https://developer.ebay.com/grow/application-growth-check"
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"eBay OAuth failed for scope {scope!r}: {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        expires_at = time.time() + int(payload.get("expires_in", 7200))
        self._write_cache(cache_path, {"access_token": payload["access_token"], "expires_at": expires_at})
        return payload["access_token"]

    def _read_cache(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _write_cache(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    # ── Browse API ──

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        category_ids: str | None = None,
        max_retries: int = 4,
        marketplace_id: str = "EBAY_US",
        filter_str: str | None = None,
        fieldgroups: str | None = None,
    ) -> list[dict]:
        """Browse API item_summary/search. Active listings only.

        `filter_str` lets the caller pass eBay's filter syntax directly,
        e.g. 'conditions:{NEW|LIKE_NEW}' or 'price:[10..100],priceCurrency:USD'.
        Multiple filters comma-separated.

        marketplace_id selects the regional site. Common: EBAY_US (USD),
        EBAY_GB, EBAY_DE, EBAY_FR. EBAY_JP is REJECTED with HTTP 409 by
        Browse — use search_sales() with EBAY_JP instead.
        """
        token = self.get_token(SCOPE_BROWSE)
        params: dict[str, str] = {"q": query, "limit": str(limit)}
        if category_ids:
            params["category_ids"] = category_ids
        if filter_str:
            params["filter"] = filter_str
        if fieldgroups:
            params["fieldgroups"] = fieldgroups
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
        }

        for attempt in range(max_retries):
            resp = httpx.get(BROWSE_SEARCH_URL, params=params, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json().get("itemSummaries", []) or []
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"eBay search failed: {resp.status_code} {resp.text[:300]}")
        raise RuntimeError(f"eBay search rate limited after {max_retries} attempts")

    def get_items(
        self,
        item_ids: list[str],
        *,
        marketplace_id: str = "EBAY_US",
        fieldgroups: str = "COMPACT",
    ) -> list[dict]:
        """Browse API getItems — batch up to 20 items per call. 20x cheaper
        than getItem-per-item against the rate limit.

        fieldgroups:
          COMPACT  — id/price/availability only. Best for re-checking known
                     items. Use this when you just want to confirm a price
                     hasn't changed.
          PRODUCT  — full product info (description, aspects, images).
        """
        if not item_ids:
            return []
        if len(item_ids) > 20:
            raise ValueError("getItems takes at most 20 item_ids per call")
        token = self.get_token(SCOPE_BROWSE)
        params = {"item_ids": ",".join(item_ids), "fieldgroups": fieldgroups}
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
        }
        resp = httpx.get(f"{BROWSE_GET_ITEMS_URL}/", params=params, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("items", []) or []
        raise RuntimeError(f"eBay getItems failed: {resp.status_code} {resp.text[:300]}")

    # ── Marketplace Insights API (Limited Release) ──

    def search_sales(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        category_ids: str | None = None,
        marketplace_id: str = "EBAY_US",
        filter_str: str | None = None,
        last_sold_window_days: int | None = 90,
        max_retries: int = 4,
    ) -> list[dict]:
        """Marketplace Insights item_sales/search — sold listings, last 90 days.

        Returns itemSales[] with {itemId, title, lastSoldPrice, lastSoldDate,
        condition, conditionId, epid, image, ...}. lastSoldPrice swaps in
        for Browse's price field — pass price_field='lastSoldPrice' to
        consensus_price().

        Will raise EbayAccessDeniedError unless your app is approved for
        the buy.marketplace.insights scope. File an Application Growth
        Check at https://developer.ebay.com/grow/application-growth-check
        before relying on this in production.

        last_sold_window_days clips results to the last N days via the
        lastSoldDate filter. Pass None to skip (returns the full 90-day
        window the API itself caps at).
        """
        token = self.get_token(SCOPE_MARKETPLACE_INSIGHTS)
        params: dict[str, str] = {"q": query, "limit": str(limit), "offset": str(offset)}
        if category_ids:
            params["category_ids"] = category_ids

        # Compose filter string. eBay's filter syntax is comma-separated.
        filters: list[str] = []
        if last_sold_window_days is not None:
            from datetime import datetime, timedelta, timezone

            now = datetime.now(timezone.utc)
            since = now - timedelta(days=last_sold_window_days)
            filters.append(
                f"lastSoldDate:[{since.strftime('%Y-%m-%dT%H:%M:%S.000Z')}..{now.strftime('%Y-%m-%dT%H:%M:%S.000Z')}]"
            )
        if filter_str:
            filters.append(filter_str)
        if filters:
            params["filter"] = ",".join(filters)

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
        }

        for attempt in range(max_retries):
            resp = httpx.get(
                MARKETPLACE_INSIGHTS_SEARCH_URL, params=params, headers=headers, timeout=30
            )
            if resp.status_code == 200:
                return resp.json().get("itemSales", []) or []
            if resp.status_code in (401, 403):
                raise EbayAccessDeniedError(
                    f"Marketplace Insights denied: {resp.status_code} {resp.text[:300]}"
                )
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"eBay search_sales failed: {resp.status_code} {resp.text[:300]}"
            )
        raise RuntimeError(f"eBay search_sales rate limited after {max_retries} attempts")

    # ── Translation API ──

    def translate(
        self,
        texts: list[str],
        *,
        from_lang: str = "ja",
        to_lang: str = "en",
        context: str = "ITEM_TITLE",
    ) -> list[str]:
        """Commerce Translation translate — bulk text translation.

        Open to every registered dev (no Application Growth Check). Useful
        for normalizing JA card names into search-friendly EN queries
        before hitting Browse's EBAY_US fallback.

        context: 'ITEM_TITLE' or 'ITEM_DESCRIPTION'. Title context is
        tuned for short retail strings — what we want for card names.

        Returns translated strings in the same order as `texts`. If
        eBay returns fewer items, missing slots are filled with the
        original input (so callers can't accidentally lose data).
        """
        if not texts:
            return []
        token = self.get_token(SCOPE_TRANSLATION)
        body = {
            "from": from_lang,
            "to": to_lang,
            "text": texts,
            "translationContext": context,
        }
        resp = httpx.post(
            TRANSLATION_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            translations = resp.json().get("translations", []) or []
            out = [t.get("translatedText", "") for t in translations]
            # Pad with originals so length matches input.
            while len(out) < len(texts):
                out.append(texts[len(out)])
            return out
        if resp.status_code in (401, 403):
            raise EbayAccessDeniedError(
                f"Translation denied: {resp.status_code} {resp.text[:300]}"
            )
        raise RuntimeError(f"eBay translate failed: {resp.status_code} {resp.text[:300]}")
