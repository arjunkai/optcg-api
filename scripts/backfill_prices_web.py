"""
Last-resort price backfill via Firecrawl web search. For each card still
unpriced after TCGPlayer, dotgg, and manual, search the web for
"{card_name} {card_id} one piece card price" and extract the first real
price signal from the results.

Real prices only — no estimation. Skips cards where no source returns a price.

Sources we accept (in order of trust):
  Cardmarket, TCGPlayer (different search URL), TCGKing, PriceCharting,
  eBay, Collectr, GameNerdz, Card Kingdom.

Writes with price_source='web_{source}' so it's traceable.

Rollback:
  wrangler d1 execute optcg-cards --remote --command \
    "UPDATE cards SET price=NULL, tcg_ids=NULL, price_updated_at=NULL, price_source=NULL \
     WHERE price_source LIKE 'web_%'"

Usage:
  python scripts/backfill_prices_web.py [--dry-run] [--local]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

WRANGLER_CMD = ["npx", "wrangler", "d1", "execute", "optcg-cards"]
FIRECRAWL_CMD = ["npx", "firecrawl-cli@1.14.8"]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Prefer higher-trust sources when multiple results return prices.
SOURCE_PRIORITY = [
    ("cardmarket.com", "cardmarket"),
    ("tcgplayer.com", "tcgplayer"),
    ("tcgking.nl", "tcgking"),
    ("pricecharting.com", "pricecharting"),
    ("ebay.com", "ebay"),
    ("app.getcollectr.com", "collectr"),
    ("gamenerdz.com", "gamenerdz"),
    ("cardkingdom.com", "cardkingdom"),
]

# $X.XX or $XXX.XX or $X,XXX.XX — also euro (€X,XX European decimal)
DOLLAR_RE = re.compile(r"\$([\d,]+\.\d{2})")
EURO_RE = re.compile(r"([\d.,]+)\s*€|€\s*([\d.,]+)")
EUR_TO_USD = 1.08  # rough — good enough for a sanity-check price


def query_d1(flag: str, sql: str) -> list[dict]:
    result = subprocess.run(
        WRANGLER_CMD + [flag, "--json", "--command", sql],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        print("wrangler query failed:", result.stderr[:500])
        sys.exit(1)
    payload = json.loads(result.stdout)
    return payload[0]["results"] if isinstance(payload, list) else payload["results"]


def extract_price(text: str) -> float | None:
    """First USD price wins; fall back to EUR converted."""
    m = DOLLAR_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = EURO_RE.search(text)
    if m:
        raw = (m.group(1) or m.group(2) or "").replace(".", "").replace(",", ".")
        try:
            return round(float(raw) * EUR_TO_USD, 2)
        except ValueError:
            pass
    return None


def search_card(name: str, card_id: str) -> tuple[float, str, str] | None:
    """Returns (price, source_label, matched_url) or None."""
    query = f"{name} {card_id} one piece card price"
    try:
        proc = subprocess.run(
            FIRECRAWL_CMD + ["search", query, "--limit", "5"],
            capture_output=True, text=True, shell=(sys.platform == "win32"),
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    out = proc.stdout

    # Firecrawl prints each result as url + description text interleaved.
    # Split into rough blocks by URL lines.
    blocks = re.split(r"(https?://[^\s\)]+)", out)
    # rebuild as (url, following_text) pairs
    pairs: list[tuple[str, str]] = []
    for i in range(1, len(blocks) - 1, 2):
        pairs.append((blocks[i], blocks[i + 1]))

    # Try sources in priority order
    for domain, label in SOURCE_PRIORITY:
        for url, text in pairs:
            if domain not in url:
                continue
            price = extract_price(text)
            if price and 0.01 < price < 50_000:  # sanity bounds
                return price, label, url
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="Only process N cards (for testing)")
    args = ap.parse_args()
    flag = "--local" if args.local else "--remote"

    print("1. Querying unpriced cards from D1...")
    unpriced = query_d1(flag, "SELECT id, name FROM cards WHERE price IS NULL")
    if args.limit:
        unpriced = unpriced[: args.limit]
    print(f"   {len(unpriced)} cards to search\n")

    if not unpriced:
        print("Nothing to backfill.")
        return

    print("2. Searching each card...")
    matches: list[dict] = []
    misses: list[dict] = []
    for i, card in enumerate(unpriced, 1):
        cid = card["id"]
        name = card.get("name") or ""
        print(f"  [{i}/{len(unpriced)}] {cid}  {name[:40]}")
        result = search_card(name, cid)
        if result:
            price, source, url = result
            matches.append({
                "card_id": cid,
                "price": price,
                "source": source,
                "url": url,
            })
            print(f"      -> ${price} ({source})")
        else:
            misses.append({"card_id": cid, "name": name})
            print(f"      -> no match")
        time.sleep(1.0)  # be polite to Firecrawl

    print(f"\n  Found prices for {len(matches)}/{len(unpriced)}")

    (OUT_DIR / "web_matches.json").write_text(
        json.dumps({"matches": matches, "misses": misses}, indent=2),
        encoding="utf-8",
    )
    print(f"  Report -> data/backfill/web_matches.json")

    if not matches:
        return

    now = int(time.time())
    sql_lines = []
    for m in matches:
        src = f"web_{m['source']}"
        sql_lines.append(
            f"UPDATE cards SET price={m['price']}, price_updated_at={now}, "
            f"price_source='{src}' WHERE id='{m['card_id']}' AND price IS NULL;"
        )

    sql_file = OUT_DIR / "web_backfill.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    print(f"  SQL -> {sql_file}")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    print(f"\n3. Executing {len(sql_lines)} UPDATEs against {flag[2:]} D1...")
    result = subprocess.run(
        WRANGLER_CMD + [flag, f"--file={sql_file}"],
        shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        sys.exit(result.returncode)
    print("Done.")


if __name__ == "__main__":
    main()
