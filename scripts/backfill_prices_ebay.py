"""
Third-tier price source: fill cards neither TCGPlayer nor dotgg listed by
searching the eBay Browse API and taking a consensus median of active
listings.

Does NOT overwrite TCGPlayer/dotgg/manual prices. Only writes to rows where
price IS NULL. Every write sets price_source='ebay' so it's auditable and
fully rollback-able.

Safety model:
  - Read-only probe: python scripts/backfill_prices_ebay.py --dry-run
  - Local write:     python scripts/backfill_prices_ebay.py --local
  - Remote write:    python scripts/backfill_prices_ebay.py
  - Full rollback:   wrangler d1 execute optcg-cards --remote \
                       --command "UPDATE cards SET price=NULL, tcg_ids=NULL, \
                                  price_updated_at=NULL, price_source=NULL \
                                  WHERE price_source='ebay'"

Requires env vars EBAY_APP_ID and EBAY_CERT_ID (set as GitHub secrets for
the weekly workflow; set in your shell for local runs).

Usage:
  python scripts/backfill_prices_ebay.py [--dry-run] [--local] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.ebay_client import EbayClient, apply_title_filters, consensus_price


WRANGLER_CMD = ["npx", "wrangler", "d1", "execute", "optcg-cards"]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def query_unpriced_cards(local: bool) -> list[dict]:
    """Returns [{id, name}, ...] for every card with no price."""
    flag = "--local" if local else "--remote"
    result = subprocess.run(
        WRANGLER_CMD + [flag, "--json", "--command",
                        "SELECT id, name FROM cards WHERE price IS NULL"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        print("wrangler query failed:", result.stderr[:500])
        sys.exit(1)
    payload = json.loads(result.stdout)
    rows = payload[0]["results"] if isinstance(payload, list) else payload["results"]
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def build_query(card_id: str, name: str) -> str:
    """eBay search query. Set code is the piece before the first dash of the
    card id (OP01-001 -> OP01). Adding "One Piece" as a discriminator keeps
    unrelated TCG matches out of the result set."""
    set_code = card_id.split("-")[0] if "-" in card_id else card_id
    return f"{name} {set_code} One Piece"


def price_card(client: EbayClient, card: dict) -> dict | None:
    """Query eBay for one card. Returns {card_id, price, sample_size} on a
    consensus hit, None otherwise."""
    query = build_query(card["id"], card["name"])
    try:
        items = client.search(query, limit=50)
    except RuntimeError as exc:
        print(f"  [skip] {card['id']}: {exc}")
        return None
    filtered = apply_title_filters(items)
    median, sample_size = consensus_price(filtered, min_count=3)
    if median is None:
        return None
    return {
        "card_id": card["id"],
        "price": round(median, 2),
        "sample_size": sample_size,
    }


def build_update_sql(matches: list[dict], now: int) -> list[str]:
    lines = []
    for m in matches:
        card_id = m["card_id"].replace("'", "''")
        lines.append(
            "UPDATE cards SET "
            f"price={m['price']}, "
            f"price_updated_at={now}, "
            f"price_source='ebay' "
            f"WHERE id='{card_id}' AND price IS NULL;"
        )
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't run it")
    ap.add_argument("--local", action="store_true", help="Target local D1")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of cards queried (for smoke tests)")
    args = ap.parse_args()

    print("1. Querying D1 for unpriced cards...")
    unpriced = query_unpriced_cards(args.local)
    if args.limit:
        unpriced = unpriced[:args.limit]
    print(f"   {len(unpriced)} cards without price\n")
    if not unpriced:
        print("Nothing to backfill. Exiting.")
        return

    print("2. Initializing eBay client...")
    client = EbayClient()  # reads EBAY_APP_ID / EBAY_CERT_ID from env
    # Prime the token cache so per-card calls reuse it.
    client.get_token()

    print("3. Pricing cards via eBay...")
    matches: list[dict] = []
    for i, card in enumerate(unpriced, start=1):
        result = price_card(client, card)
        if result:
            matches.append(result)
            print(f"   [{i}/{len(unpriced)}] {card['id']}: ${result['price']} "
                  f"(n={result['sample_size']})")
        else:
            print(f"   [{i}/{len(unpriced)}] {card['id']}: no consensus")
        time.sleep(0.2)  # be polite, well under rate limit
    print(f"\n   {len(matches)} cards priced via eBay\n")

    now = int(time.time())
    sql_lines = build_update_sql(matches, now)

    sql_file = OUT_DIR / "ebay_backfill.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    print(f"4. SQL written to {sql_file}")

    (OUT_DIR / "ebay_matches.json").write_text(
        json.dumps(matches, indent=2), encoding="utf-8"
    )

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    if not matches:
        print("No prices found. Nothing to execute.")
        return

    target = "--local" if args.local else "--remote"
    print(f"\n5. Executing {len(sql_lines)} UPDATEs against {target[2:]} D1...")
    result = subprocess.run(
        WRANGLER_CMD + [target, f"--file={sql_file}"],
        shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        print("Execute failed.")
        sys.exit(result.returncode)
    print("Done.")


if __name__ == "__main__":
    main()
