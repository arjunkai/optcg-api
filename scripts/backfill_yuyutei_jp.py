"""
Yuyutei (yuyu-tei.jp) is a Japanese trading-card retailer. Their public
per-set listing pages display every Pokemon card they currently stock,
each with a high-quality scan image (no hands / shadows / sleeve glare
like eBay listings) and a JPY retail price. Free to use as a data
source — public website, no auth, polite scraping pattern.

This script is the UPDATE-only consumer: COALESCE image_high/image_low
and json_patch pricing into pricing_json for ptcg_cards rows that
already exist with lang='ja'. Rows that DON'T exist yet are handled by
the sibling scripts/backfill_yuyutei_catalog.py (INSERT consumer).

Set mapping reuses data/ptcg_jp_set_mapping.json. The pkmnbindr ID
(`sv10_ja`) strips its `_ja` suffix to get Yuyutei's setcode (`sv10`).

Usage:
    python -m scripts.backfill_yuyutei_jp
    python -m scripts.backfill_yuyutei_jp --set=SV10
    python -m scripts.backfill_yuyutei_jp --dry-run --limit=3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from scripts.lib.yuyutei_scraper import (
    IMAGE_HOST,
    REQ_INTERVAL_S,
    USER_AGENT,
    build_card_id_candidates,
    get_jpy_to_usd_rate,
    load_mapping,
    scrape_set_listing,
)
from scripts.wrangler_retry import run_wrangler


DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", help="Only run this TCGdex set id (e.g. SV10)")
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't run it")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of TCGdex sets processed (smoke tests)")
    args = ap.parse_args()

    try:
        yuyutei_for = load_mapping()
    except FileNotFoundError as exc:
        print(exc)
        sys.exit(1)

    sets = [args.set] if args.set else list(yuyutei_for.keys())
    if args.limit:
        sets = sets[: args.limit]
    print(f"Backfilling {len(sets)} TCGdex JA sets via Yuyutei...")

    fx = get_jpy_to_usd_rate()
    print(f"FX rate: 1 JPY = {fx:.6f} USD\n")

    sql_lines: list[str] = []
    images_filled = 0
    prices_filled = 0
    sets_skipped = 0
    sets_seen = 0

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=True) as client:
        for tcgdex_id in sets:
            yuyutei_code = yuyutei_for.get(tcgdex_id)
            if not yuyutei_code:
                continue
            time.sleep(REQ_INTERVAL_S)
            cards = scrape_set_listing(client, yuyutei_code)
            if cards is None:
                sets_skipped += 1
                print(f"  [{tcgdex_id} -> {yuyutei_code}] not on Yuyutei (likely vintage), skipping")
                continue
            sets_seen += 1
            print(f"  [{tcgdex_id} -> {yuyutei_code}] {len(cards)} listings parsed")

            for card in cards:
                card_id_candidates = build_card_id_candidates(tcgdex_id, card["card_number"])
                image_url = card["image_url"]
                if image_url:
                    images_filled += 1
                    for cid in card_id_candidates:
                        sql_lines.append(image_update_sql(cid, image_url))
                if card["price_jpy"] is not None:
                    price_usd = round(card["price_jpy"] * fx, 2)
                    prices_filled += 1
                    payload = json.dumps({
                        "price_jpy": card["price_jpy"],
                        "price_usd": price_usd,
                        "url": f"https://{IMAGE_HOST.replace('card.', '')}/sell/poc/s/{yuyutei_code}",
                        "marketplace": "yuyutei",
                        "updated_at": int(time.time()),
                    }).replace("'", "''")
                    for cid in card_id_candidates:
                        sql_lines.append(price_update_sql(cid, payload))

    print(f"\nSets: {sets_seen} parsed, {sets_skipped} skipped (not on Yuyutei)")
    print(f"Images to write: {images_filled}")
    print(f"Prices to write: {prices_filled}")
    print(f"SQL statements: {len(sql_lines)}")
    if not sql_lines:
        print("Nothing to write.")
        return

    sql_file = OUT_DIR / "yuyutei_jp.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    print(f"\nSQL written to {sql_file}")

    if args.dry_run:
        print("--dry-run: skipping D1 execution")
        return

    print(f"\nExecuting against remote D1...")
    result = run_wrangler(WRANGLER_BIN + ["--remote", f"--file={sql_file}"])
    if result.returncode != 0:
        print("Execute failed:", (result.stderr or "")[:400])
        sys.exit(result.returncode)
    print("Done.")


def image_update_sql(card_id: str, image_url: str) -> str:
    cid = card_id.replace("'", "''")
    img = image_url.replace("'", "''")
    return (
        f"UPDATE ptcg_cards SET "
        f"image_high = COALESCE(image_high, '{img}'), "
        f"image_low = COALESCE(image_low, '{img}') "
        f"WHERE card_id = '{cid}' AND lang = 'ja';"
    )


def price_update_sql(card_id: str, payload_json: str) -> str:
    cid = card_id.replace("'", "''")
    # Patch into pricing_json under .yuyutei; flip price_source unless
    # the row is already manual / pokemontcg / tcgplayer (the three
    # strongest sources we trust over JP retail).
    #
    # Pricecharting was added to the OR-list 2026-05-19 after a spot
    # check on 12 random JA cards with both yuyutei + pricecharting
    # prices showed pricecharting consistently overshoots Yuyutei by
    # 5-10x and outright wrong-prices variant-conflated rows (e.g.
    # SV11W-003 Leavanny: $10.12 on PC's leavanny-master-ball-3 URL
    # vs $0.32 actual JP retail). Yuyutei is the more authoritative
    # signal for modern JA cards in active circulation; pricecharting
    # still wins on truly-vintage rows because Yuyutei doesn't stock
    # them at all and so this script won't UPDATE them.
    return (
        f"UPDATE ptcg_cards SET "
        f"pricing_json = json_patch(COALESCE(pricing_json, '{{}}'), json_object('yuyutei', json('{payload_json}'))), "
        f"price_source = CASE "
        f"  WHEN price_source IN ('manual', 'pokemontcg', 'tcgplayer') THEN price_source "
        f"  ELSE 'yuyutei' "
        f"END "
        f"WHERE card_id = '{cid}' AND lang = 'ja' "
        f"AND (price_source IS NULL OR price_source IN ('cardmarket', 'ebay_jp', 'pricecharting'));"
    )


if __name__ == "__main__":
    main()
