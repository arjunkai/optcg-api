"""
Fallback price source: fill in cards TCGPlayer doesn't list by pulling from
api.dotgg.gg (free, no key). Runs AFTER the primary TCGPlayer import.

Does NOT overwrite TCGPlayer prices. Only writes to rows where price IS NULL.
Every write sets price_source='dotgg' so we can audit or roll back.

Safety model:
  - Read-only probe: python scripts/backfill_prices_dotgg.py --dry-run
  - Local write:     python scripts/backfill_prices_dotgg.py --local
  - Remote write:    python scripts/backfill_prices_dotgg.py
  - Full rollback:   wrangler d1 execute optcg-cards --remote \
                       --command "UPDATE cards SET price=NULL, tcg_ids=NULL, \
                                  price_updated_at=NULL, price_source=NULL \
                                  WHERE price_source='dotgg'"

Usage:
  python scripts/backfill_prices_dotgg.py [--dry-run] [--local]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

DOTGG_BASE = "https://api.dotgg.gg/cgfw"
BATCH_SIZE = 200  # dotgg pagination limit
WRANGLER_CMD = ["npx", "wrangler", "d1", "execute", "optcg-cards"]

OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_all_dotgg_prices() -> dict[str, dict]:
    """Returns {card_id: {price, foilPrice, tcg_ids, ...}} for every OP card."""
    catalog: dict[str, dict] = {}
    page = 1
    while True:
        # getcardsfiltered returns priced rows with tcg_ids and pagination.
        # We page through with rq={"page":N,"pageSize":200}.
        rq = json.dumps({"page": page, "pageSize": BATCH_SIZE})
        url = f"{DOTGG_BASE}/getcardsfiltered?game=onepiece&rq={urllib.parse.quote(rq)}"
        data = _fetch_json(url)
        # Response can be a bare list OR {data: [...]}. Normalize.
        if isinstance(data, list):
            rows = data
        else:
            rows = data.get("data") or data.get("cards") or data.get("results") or []
        if page == 1:
            (OUT_DIR / "dotgg_first_payload.json").write_text(
                json.dumps(data, indent=2, default=str)[:20_000], encoding="utf-8"
            )
        if not rows:
            break
        for row in rows:
            cid = row.get("cardid") or row.get("id")
            if not cid:
                continue
            catalog[cid] = row
        print(f"  page {page}: +{len(rows)} rows (total {len(catalog)})")
        if len(rows) < BATCH_SIZE:
            break
        page += 1
        time.sleep(0.5)  # be polite
    return catalog


def query_unpriced_ids(local: bool) -> list[str]:
    """Pull the list of card_ids we still need prices for from D1."""
    flag = "--local" if local else "--remote"
    result = subprocess.run(
        WRANGLER_CMD + [flag, "--json", "--command",
                        "SELECT id FROM cards WHERE price IS NULL"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        print("wrangler query failed:", result.stderr[:500])
        sys.exit(1)
    # Wrangler emits a JSON array of query results
    payload = json.loads(result.stdout)
    rows = payload[0]["results"] if isinstance(payload, list) else payload["results"]
    return [r["id"] for r in rows]


def build_update_sql(matches: list[dict], now: int) -> list[str]:
    lines = []
    for m in matches:
        card_id = m["card_id"]
        price = m["price"]
        tcg_ids = json.dumps(m.get("tcg_ids") or [])
        lines.append(
            "UPDATE cards SET "
            f"price={price}, "
            f"tcg_ids='{tcg_ids.replace(chr(39), chr(39)*2)}', "
            f"price_updated_at={now}, "
            f"price_source='dotgg' "
            f"WHERE id='{card_id}' AND price IS NULL;"
        )
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't run it")
    ap.add_argument("--local", action="store_true", help="Target local D1")
    args = ap.parse_args()

    print("1. Querying D1 for unpriced cards...")
    unpriced = query_unpriced_ids(args.local)
    print(f"   {len(unpriced)} cards without price\n")
    if not unpriced:
        print("Nothing to backfill. Exiting.")
        return

    print("2. Fetching dotgg.gg catalog...")
    catalog = fetch_all_dotgg_prices()
    print(f"   {len(catalog)} cards from dotgg.gg\n")

    def to_float(v):
        try:
            return float(v) if v not in (None, "", "0", "0.000000") else 0.0
        except (TypeError, ValueError):
            return 0.0

    def to_tcg_ids(v):
        # dotgg stores as comma-separated string of ints.
        if not v:
            return []
        return [int(x) for x in str(v).split(",") if x.strip().isdigit()]

    print("3. Matching unpriced -> dotgg...")
    matches = []
    for cid in unpriced:
        row = catalog.get(cid)
        if not row:
            continue
        # If normal price is zero, fall back to foil price (cards like SECs
        # are foil-only and dotgg reports their value in foilPrice).
        price = to_float(row.get("price"))
        if price <= 0:
            price = to_float(row.get("foilPrice"))
        if price <= 0:
            continue
        matches.append({
            "card_id": cid,
            "price": round(price, 2),
            "tcg_ids": to_tcg_ids(row.get("tcg_ids")),
            "dotgg_slug": row.get("slug"),
        })
    print(f"   {len(matches)} backfill-able cards\n")

    now = int(time.time())
    sql_lines = build_update_sql(matches, now)

    sql_file = OUT_DIR / "dotgg_backfill.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    print(f"4. SQL written to {sql_file}")

    # Save the match report for audit
    (OUT_DIR / "dotgg_matches.json").write_text(
        json.dumps(matches, indent=2), encoding="utf-8"
    )

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
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
