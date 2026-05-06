"""
Smoke-probe for the Marketplace Insights API.

Hits item_sales/search for a single test query against EBAY_US and
EBAY_JP. Useful for two things:

  1. Confirming whether your app's keyset has been approved for the
     buy.marketplace.insights scope. If access is denied you'll get
     EbayAccessDeniedError with the exact eBay response text.
  2. Eyeballing the response shape (sold prices, sold dates, condition)
     before wiring it into the weekly refresh.

Run:
    python -m scripts.probe_marketplace_insights "Charizard ex 199 PSA 10"
    python -m scripts.probe_marketplace_insights "リザードンex" --marketplace=EBAY_JP

Required env: EBAY_APP_ID, EBAY_CERT_ID.
"""

from __future__ import annotations

import argparse
import json
import sys
from statistics import median

from scripts.ebay_client import (
    EbayAccessDeniedError,
    EbayClient,
    apply_title_filters,
    consensus_price,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="Search string (English for EBAY_US, can be Japanese for EBAY_JP)")
    ap.add_argument("--marketplace", default="EBAY_US", help="EBAY_US, EBAY_JP, EBAY_GB, ...")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--days", type=int, default=90, help="Sold-within window in days")
    ap.add_argument("--currency", default="USD", help="Currency to extract for the median")
    ap.add_argument("--show-raw", action="store_true", help="Print raw itemSales JSON")
    args = ap.parse_args()

    client = EbayClient()

    print(f"Probing Marketplace Insights for {args.query!r} on {args.marketplace}...")
    try:
        items = client.search_sales(
            args.query,
            limit=args.limit,
            marketplace_id=args.marketplace,
            last_sold_window_days=args.days,
        )
    except EbayAccessDeniedError as e:
        print("ACCESS DENIED:", e, file=sys.stderr)
        print(
            "\nNext step: file an Application Growth Check at "
            "https://developer.ebay.com/grow/application-growth-check\n"
            "Marketplace Insights is a Limited Release API — eBay reviews "
            "every request. Approval is not guaranteed for non-partner apps.",
            file=sys.stderr,
        )
        return 2
    except Exception as e:
        print("FAILED:", e, file=sys.stderr)
        return 1

    if args.show_raw:
        print(json.dumps(items, indent=2))

    print(f"  raw items: {len(items)}")

    cleaned = apply_title_filters(items)
    print(f"  after blocklist: {len(cleaned)}")

    consensus, sample = consensus_price(
        cleaned, min_count=3, currency=args.currency, price_field="lastSoldPrice"
    )
    if consensus is not None:
        print(f"  consensus median ({args.currency}): {consensus:.2f} from {sample} listings")
    else:
        print(f"  not enough listings for consensus (need 3, got {sample})")

    # Quick eyeball of recent sales
    if cleaned:
        prices = []
        for it in cleaned[:10]:
            p = (it.get("lastSoldPrice") or {})
            if p.get("currency") == args.currency and p.get("value"):
                try:
                    prices.append(float(p["value"]))
                except ValueError:
                    pass
            print(
                f"    {it.get('lastSoldDate', '?')}  "
                f"{(p.get('value') or '?'):>8} {p.get('currency', '?')}  "
                f"{(it.get('condition') or '?'):<14}  "
                f"{(it.get('title') or '?')[:80]}"
            )
        if prices:
            print(f"  raw median of first 10: {median(prices):.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
