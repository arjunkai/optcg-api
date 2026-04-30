"""
Last-resort image source: scrape eBay listing thumbnails for cards
where every other free source we've tried (TCGdex, pokemontcg-data,
malie.io, flibustier, pkmnbindr/Scrydex catalog, Yuyutei) didn't have
the image.

Quality is lower than the dedicated card-scan sources — eBay listings
have hands, sleeves, glare, and condition variance — but it's better
than a placeholder for the visible-gap residual (~323 EN + ~951 JA
cards as of 2026-04-30).

Per card:
  1. Search eBay (US for EN, JP for JA) with the same query template
     as the price backfill.
  2. Apply the same Pokemon-tightened title blocklist (proxy / lot /
     bulk / damaged / etc).
  3. Group images by URL; the most-common URL among filtered listings
     is most likely a stock product photo (sellers often reuse the
     same listing image), which has the cleanest framing.
  4. Require ≥2 listings showing the same image — that's the floor
     for confidence the URL points at the right card.
  5. COALESCE-fill image_high in D1 (never overwrite a sourced image).

Stamps no special source flag — the URL host (ebayimg.com) makes the
provenance obvious for any future audit.

Rollback (per lang):
    wrangler d1 execute optcg-cards --remote \\
        --command \"UPDATE ptcg_cards SET image_high=NULL, image_low=NULL
                   WHERE image_high LIKE '%ebayimg%' AND lang='en'\"

Usage:
    python -m scripts.backfill_ptcg_images_ebay --lang=en
    python -m scripts.backfill_ptcg_images_ebay --lang=ja --limit=20 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from scripts.ebay_client import EbayClient, apply_title_filters


DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)
EBAY_CATEGORY_POKEMON = "183454"

# Same Pokemon-specific blocklist as the price backfill — same noise
# patterns hurt image quality just as much as price accuracy.
POKEMON_TITLE_BLOCKLIST: tuple[str, ...] = (
    "proxy", "custom art", "fan made", "fan-made", "replica", "fake",
    "not authentic", "art only", "fanart", "fan art",
    "lot", "bulk", "bundle", "box of", "complete set", "playset",
    "damaged", "creased", "miscut", "misprint", "error",
    "grading", "submission",
    "stamped",
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["en", "ja"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of cards queried (smoke tests)")
    ap.add_argument("--min-listings", type=int, default=2,
                    help="Min listings sharing the same image URL (default: 2)")
    args = ap.parse_args()

    print(f"1. Querying D1 for {args.lang} cards with no image...")
    cards = query_imageless_cards(args.lang)
    if args.limit:
        cards = cards[:args.limit]
    print(f"   {len(cards)} cards in scope\n")
    if not cards:
        print("Nothing to backfill. Exiting.")
        return

    print("2. Initializing eBay client...")
    client = EbayClient()
    client.get_token()
    marketplace = "EBAY_US" if args.lang == "en" else "EBAY_JP"
    print(f"   Marketplace: {marketplace}\n")

    print("3. Fetching listing images via eBay...")
    matches: list[dict] = []
    for i, card in enumerate(cards, start=1):
        result = find_image(client, card, args.lang, marketplace,
                            min_listings=args.min_listings)
        if result:
            matches.append(result)
            print(f"   [{i}/{len(cards)}] {card['card_id']}: "
                  f"{result['image_url'][:90]} (n={result['support']})")
        else:
            print(f"   [{i}/{len(cards)}] {card['card_id']}: no consensus image")
        time.sleep(0.25)
    print(f"\n   {len(matches)} cards filled via eBay listings\n")

    if not matches:
        print("No images found. Nothing to write.")
        return

    sql_lines = build_update_sql(matches, args.lang)
    sql_file = OUT_DIR / f"ebay_images_ptcg_{args.lang}.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / f"ebay_images_ptcg_{args.lang}_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    print(f"4. SQL written to {sql_file}")
    print(f"   Matches written to {matches_file}")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    print(f"\n5. Executing {len(sql_lines)} UPDATEs against remote D1...")
    result = subprocess.run(WRANGLER_BIN + ["--remote", f"--file={sql_file}"])
    if result.returncode != 0:
        print("Execute failed.")
        sys.exit(result.returncode)
    print("Done.")


def query_imageless_cards(lang: str) -> list[dict]:
    sql = (
        f"SELECT card_id, name, set_id FROM ptcg_cards "
        f"WHERE lang = '{lang}' AND image_high IS NULL "
        f"ORDER BY set_id, local_id"
    )
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print("D1 query failed:", out.stderr[:500])
        sys.exit(1)
    start = out.stdout.find("[")
    if start < 0:
        return []
    try:
        payload = json.loads(out.stdout[start:])
    except json.JSONDecodeError:
        return []
    rows = payload[0].get("results", []) if isinstance(payload, list) else payload.get("results", [])
    return rows or []


def find_image(client: EbayClient, card: dict, lang: str, marketplace: str,
               *, min_listings: int) -> dict | None:
    query = build_query(card, lang)
    try:
        items = client.search(
            query, limit=20,
            marketplace_id=marketplace,
            category_ids=EBAY_CATEGORY_POKEMON if marketplace == "EBAY_US" else None,
        )
    except RuntimeError as exc:
        print(f"  [skip] {card['card_id']}: {exc}")
        return None
    filtered = apply_title_filters(items, blocklist=POKEMON_TITLE_BLOCKLIST)
    if not filtered:
        return None

    # Group by image URL. Stock product photos appear repeatedly across
    # listings; private seller photos appear once. Most-common = highest
    # confidence.
    counts: Counter[str] = Counter()
    for item in filtered:
        url = (item.get("image") or {}).get("imageUrl")
        if isinstance(url, str) and url.startswith("http"):
            counts[url] += 1
    if not counts:
        return None
    top_url, top_count = counts.most_common(1)[0]
    if top_count < min_listings:
        return None
    return {
        "card_id": card["card_id"],
        "lang": lang,
        "image_url": top_url,
        "support": top_count,
    }


def build_query(card: dict, lang: str) -> str:
    set_code = card["set_id"] or card["card_id"].split("-")[0]
    name = (card.get("name") or "").strip()
    if lang == "en":
        return f"{name} {set_code} pokemon"
    return f"{name} {set_code} ポケモン"


def build_update_sql(matches: list[dict], lang: str) -> list[str]:
    lines = []
    for m in matches:
        cid = m["card_id"].replace("'", "''")
        url = m["image_url"].replace("'", "''")
        lines.append(
            f"UPDATE ptcg_cards SET "
            f"image_high = COALESCE(image_high, '{url}'), "
            f"image_low = COALESCE(image_low, '{url}') "
            f"WHERE card_id = '{cid}' AND lang = '{lang}' AND image_high IS NULL;"
        )
    return lines


if __name__ == "__main__":
    main()
