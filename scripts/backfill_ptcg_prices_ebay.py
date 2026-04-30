"""
Last-resort price source for Pokemon cards: query eBay's Browse API for
each card without a price, take a consensus median of active listings,
write to D1. EN cards search EBAY_US (USD direct), JA cards search
EBAY_JP (JPY, FX-converted to USD via frankfurter.app).

Mirrors the OPTCG `backfill_prices_ebay.py` flow — same OAuth, same
trimmed-median consensus, same blocklist — adapted for Pokemon's
multi-marketplace + multi-language reality.

Does NOT overwrite manual / pokemontcg / cardmarket prices. Only writes
to rows where the existing price_source is NULL or 'cardmarket'
(cardmarket is EUR-only TCGdex baseline, eBay USD is more authoritative
for chase-card valuation). Every write stamps price_source='ebay_us' or
'ebay_jp' so it's auditable and rollback-able:

    -- rollback (per marketplace):
    wrangler d1 execute optcg-cards --remote \\
        --command "UPDATE ptcg_cards SET price_source=NULL,
                   pricing_json=json_remove(pricing_json,'$.ebay')
                   WHERE price_source='ebay_us'"

Requires env vars EBAY_APP_ID + EBAY_CERT_ID (set as GitHub secrets for
the weekly workflow; set locally for dev runs). Optional
CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID for the D1 HTTP query path
— if absent, falls back to wrangler CLI.

Usage (run as a module from optcg-api root so scripts.ebay_client resolves):
    python -m scripts.backfill_ptcg_prices_ebay --lang=en
    python -m scripts.backfill_ptcg_prices_ebay --lang=ja
    python -m scripts.backfill_ptcg_prices_ebay --lang=en --limit=10 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from statistics import median as stat_median
from typing import Iterable

from scripts.ebay_client import EbayClient, apply_title_filters, consensus_price


# Pokemon Individual Cards on eBay US.
EBAY_CATEGORY_POKEMON = "183454"
DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Two-day FX rate cache. ECB-backed via frankfurter.app — no auth, free,
# rate-limited politely. Refreshes on first call after the cache file is
# older than this.
FX_CACHE_PATH = Path("data/.fx_jpy_usd.json")
FX_CACHE_TTL_S = 2 * 24 * 60 * 60

# Pokemon-specific blocklist on top of the shared default. Pokemon cards
# attract MORE noise than One Piece (proxy farms, lots, graded resales,
# damaged singles passing as mint).
POKEMON_TITLE_BLOCKLIST: tuple[str, ...] = (
    "proxy", "custom art", "fan made", "fan-made", "replica", "fake",
    "not authentic", "art only", "fanart", "fan art",
    "lot", "bulk", "bundle", "box of", "complete set", "playset",
    "damaged", "creased", "miscut", "misprint", "error",
    "grading", "submission",  # ungraded "ready for grading" listings, distinct from PSA-graded singles
    "stamped",  # Tournament-stamped variants are differently priced
)

# eBay JP site search expects JPY in price.value. We FX-convert before
# storing so the frontend always sees USD.
JPY_TO_USD_FALLBACK = 0.0067  # last-resort hardcoded rate ≈ ¥150/$1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["en", "ja"], required=True,
                    help="Card language to backfill (en→EBAY_US, ja→EBAY_JP)")
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't run it")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of cards queried (smoke tests)")
    ap.add_argument("--min-count", type=int, default=5,
                    help="Min listings required for consensus (default: 5)")
    args = ap.parse_args()

    print(f"1. Querying D1 for {args.lang} cards without price...")
    cards = query_unpriced_cards(args.lang)
    if args.limit:
        cards = cards[:args.limit]
    print(f"   {len(cards)} cards in scope\n")
    if not cards:
        print("Nothing to backfill. Exiting.")
        return

    print("2. Initializing eBay client + FX rate...")
    client = EbayClient()
    client.get_token()
    fx_jpy_to_usd = get_jpy_to_usd_rate() if args.lang == "ja" else 1.0
    if args.lang == "ja":
        print(f"   FX rate: 1 JPY = {fx_jpy_to_usd:.6f} USD")

    marketplace = "EBAY_US" if args.lang == "en" else "EBAY_JP"
    target_currency = "USD" if marketplace == "EBAY_US" else "JPY"
    print(f"   Marketplace: {marketplace}, target currency: {target_currency}\n")

    print("3. Pricing cards via eBay...")
    matches: list[dict] = []
    for i, card in enumerate(cards, start=1):
        result = price_card(client, card, args.lang, marketplace, target_currency,
                            fx_jpy_to_usd, min_count=args.min_count)
        if result:
            matches.append(result)
            print(f"   [{i}/{len(cards)}] {card['card_id']}: ${result['price_usd']} "
                  f"(n={result['sample_size']}, src={result['price_source']})")
        else:
            print(f"   [{i}/{len(cards)}] {card['card_id']}: no consensus")
        time.sleep(0.25)  # well under eBay's per-app rate limit
    print(f"\n   {len(matches)} cards priced via eBay\n")

    if not matches:
        print("No prices found. Nothing to write.")
        return

    sql_lines = build_update_sql(matches)
    sql_file = OUT_DIR / f"ebay_ptcg_{args.lang}.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / f"ebay_ptcg_{args.lang}_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"4. SQL written to {sql_file}")
    print(f"   Matches written to {matches_file}")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    print(f"\n5. Executing {len(sql_lines)} UPDATEs against remote D1...")
    result = subprocess.run(
        WRANGLER_BIN + ["--remote", f"--file={sql_file}"],
    )
    if result.returncode != 0:
        print("Execute failed.")
        sys.exit(result.returncode)
    print("Done.")


def query_unpriced_cards(lang: str) -> list[dict]:
    """Returns [{card_id, name, set_id}, ...] for cards in the given lang
    that don't have a useful price. We treat null and cardmarket-EUR as
    'priced gap' since eBay USD beats those for chase-card valuation."""
    sql = (
        f"SELECT card_id, name, set_id FROM ptcg_cards WHERE lang = '{lang}' "
        "AND (price_source IS NULL OR price_source = 'cardmarket') "
        "ORDER BY set_id, local_id"
    )
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print("wrangler query failed:", out.stderr[:500])
        sys.exit(1)
    payload_start = out.stdout.find("[")
    if payload_start < 0:
        return []
    payload = json.loads(out.stdout[payload_start:])
    rows = payload[0].get("results", []) if isinstance(payload, list) else payload.get("results", [])
    return rows or []


def price_card(client: EbayClient, card: dict, lang: str, marketplace: str,
               target_currency: str, fx_jpy_to_usd: float, *, min_count: int) -> dict | None:
    query = build_query(card, lang)
    try:
        items = client.search(
            query, limit=50,
            marketplace_id=marketplace,
            category_ids=EBAY_CATEGORY_POKEMON if marketplace == "EBAY_US" else None,
        )
    except RuntimeError as exc:
        print(f"  [skip] {card['card_id']}: {exc}")
        return None
    filtered = apply_title_filters(items, blocklist=POKEMON_TITLE_BLOCKLIST)
    median, sample_size = consensus_price(filtered, min_count=min_count, currency=target_currency)
    if median is None:
        return None

    price_usd = median if target_currency == "USD" else median * fx_jpy_to_usd
    return {
        "card_id": card["card_id"],
        "lang": lang,
        "price_native": round(median, 2),
        "price_currency": target_currency,
        "price_usd": round(price_usd, 2),
        "sample_size": sample_size,
        "price_source": "ebay_us" if marketplace == "EBAY_US" else "ebay_jp",
    }


def build_query(card: dict, lang: str) -> str:
    """Card-specific eBay search query.
    EN: "{name} {setCode} pokemon" (set code is everything before the
        last hyphen of card_id; the local-id suffix is unhelpful in
        listing titles).
    JA: "{japaneseName} {setCode} ポケモン" — JP marketplace search
        understands Japanese natively. The set code is uppercase
        (matches eBay JP listing titles).
    """
    set_code = card["set_id"] or card["card_id"].split("-")[0]
    name = (card.get("name") or "").strip()
    if lang == "en":
        return f"{name} {set_code} pokemon"
    return f"{name} {set_code} ポケモン"


def build_update_sql(matches: list[dict]) -> list[str]:
    """One UPDATE per card. price_source flips to ebay_us or ebay_jp;
    pricing_json gets a new {ebay: {...}} key with the native price,
    sample size, and FX rate (if JP). Manual rows are guarded by the
    WHERE clause."""
    lines = []
    for m in matches:
        card_id = m["card_id"].replace("'", "''")
        lang = m["lang"]
        price_source = m["price_source"]
        price_usd = m["price_usd"]
        ebay_payload = json.dumps({
            "price_usd": price_usd,
            "price_native": m["price_native"],
            "currency": m["price_currency"],
            "sample_size": m["sample_size"],
            "marketplace": "EBAY_US" if price_source == "ebay_us" else "EBAY_JP",
            "updated_at": int(time.time()),
        }).replace("'", "''")
        # Patch into pricing_json under .ebay; flip price_source unless
        # it's already 'manual' (manual always wins).
        lines.append(
            "UPDATE ptcg_cards SET "
            f"pricing_json = json_patch(COALESCE(pricing_json, '{{}}'), json_object('ebay', json('{ebay_payload}'))), "
            f"price_source = CASE WHEN price_source = 'manual' THEN 'manual' ELSE '{price_source}' END "
            f"WHERE card_id = '{card_id}' AND lang = '{lang}' "
            "AND (price_source IS NULL OR price_source = 'cardmarket');"
        )
    return lines


def get_jpy_to_usd_rate() -> float:
    """Read JPY→USD from frankfurter.app (free, ECB-backed, no auth).
    Caches in data/.fx_jpy_usd.json for FX_CACHE_TTL_S. Falls back to
    a hardcoded rate if the API is down."""
    if FX_CACHE_PATH.exists():
        try:
            cached = json.loads(FX_CACHE_PATH.read_text())
            if time.time() - cached.get("ts", 0) < FX_CACHE_TTL_S:
                return float(cached["rate"])
        except Exception:
            pass
    try:
        with urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=JPY&to=USD",
            timeout=10,
        ) as r:
            data = json.loads(r.read())
        rate = float(data["rates"]["USD"])
        FX_CACHE_PATH.write_text(json.dumps({"rate": rate, "ts": time.time()}))
        return rate
    except Exception as exc:
        print(f"  FX fetch failed ({exc}); using fallback {JPY_TO_USD_FALLBACK}")
        return JPY_TO_USD_FALLBACK


if __name__ == "__main__":
    main()
