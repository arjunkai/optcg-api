"""
Yuyutei (yuyu-tei.jp) is a Japanese trading-card retailer. Their public
per-set listing pages display every Pokemon card they currently stock,
each with a high-quality scan image (no hands / shadows / sleeve glare
like eBay listings) and a JPY retail price. Free to use as a data
source — public website, no auth, polite scraping pattern.

Industry standard: Dex (dextcg.com) explicitly credits Yuyutei as their
JP source. We do the same — pkmnbindr is our primary JP catalog;
Yuyutei layers on top to (a) backfill cards pkmnbindr's snapshot
hasn't ingested yet, and (b) add JP prices that pkmnbindr's catalog
doesn't carry.

Per-set flow:
  GET https://yuyu-tei.jp/sell/poc/s/{setcode}        # HTML listing page
    -> parse each <div class="card-product"> for:
        card number  (e.g. "130/098")
        image URL    (https://card.yuyu-tei.jp/poc/100_140/{setcode}/{id}.jpg)
        price JPY    ("7,980 円" -> 7980)
        sold-out     (skip pricing for these but keep image)
  -> COALESCE-fill ptcg_cards.image_high (lang='ja')
  -> json_patch pricing.yuyutei.{price_jpy, price_usd, url}
  -> flip price_source to 'yuyutei' unless already 'manual'

JPY -> USD via frankfurter.app (free, ECB rates) — same FX layer the
eBay backfill uses.

Set mapping reuses data/ptcg_jp_set_mapping.json. The pkmnbindr ID
(`sv10_ja`) strips its `_ja` suffix to get Yuyutei's setcode (`sv10`).

Usage:
    python -m scripts.backfill_yuyutei_jp
    python -m scripts.backfill_yuyutei_jp --set=sv10
    python -m scripts.backfill_yuyutei_jp --dry-run --limit=3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx
from bs4 import BeautifulSoup


DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
MAPPING_PATH = Path("data/ptcg_jp_set_mapping.json")
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)
LISTING_BASE = "https://yuyu-tei.jp/sell/poc/s"
IMAGE_HOST = "card.yuyu-tei.jp"
REQ_INTERVAL_S = 1.0
USER_AGENT = "opbindr-ptcg-importer/1.0 (+https://opbindr.com; contact arjun@neuroplexlabs.com)"

FX_CACHE_PATH = Path("data/.fx_jpy_usd.json")
FX_CACHE_TTL_S = 2 * 24 * 60 * 60
JPY_TO_USD_FALLBACK = 0.0067


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", help="Only run this TCGdex set id (e.g. SV10)")
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't run it")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of TCGdex sets processed (smoke tests)")
    args = ap.parse_args()

    if not MAPPING_PATH.exists():
        print(f"Missing {MAPPING_PATH}. Run scripts/build-ptcg-set-mapping.js first.")
        sys.exit(1)

    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    mapping.pop("_doc", None)
    # Yuyutei uses the bare lowercase setcode (no `_ja` suffix). pkmnbindr
    # uses `{setcode}_ja`. Strip the suffix to get Yuyutei's setcode.
    yuyutei_for = {tcgdex: pkm.replace("_ja", "") for tcgdex, pkm in mapping.items()}

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
                # COALESCE image_high. card.image_url is the canonical
                # 100x140 thumb; Yuyutei doesn't expose a higher-res
                # version on their public CDN.
                image_url = card["image_url"]
                if image_url:
                    images_filled += 1
                    for cid in card_id_candidates:
                        sql_lines.append(image_update_sql(cid, image_url))
                # Price: JPY -> USD, only if in stock with a real number.
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
    result = subprocess.run(WRANGLER_BIN + ["--remote", f"--file={sql_file}"])
    if result.returncode != 0:
        sys.exit(result.returncode)
    print("Done.")


def scrape_set_listing(client: httpx.Client, setcode: str) -> list[dict] | None:
    """Parse a Yuyutei per-set listing page. Returns a list of
    {card_number, image_url, price_jpy, sold_out} dicts. Returns None if
    the page 404s (set doesn't exist on Yuyutei)."""
    try:
        r = client.get(f"{LISTING_BASE}/{setcode}")
    except httpx.HTTPError as exc:
        print(f"    fetch error: {exc}")
        return None
    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    cards: list[dict] = []
    for div in soup.find_all("div", class_="card-product"):
        text = div.get_text(separator=" ", strip=True)
        # Card number: "130/098" — we want the numerator.
        num_match = re.search(r"(\d{1,4})\s*/\s*\d{1,4}", text)
        if not num_match:
            continue
        card_number = num_match.group(1)
        # Image URL.
        img_tag = div.find("img", src=lambda s: s and "card.yuyu-tei.jp/poc" in s)
        image_url = img_tag["src"] if img_tag else None
        # Price: "7,980 円" — strip commas, parse int. Sold-out cards
        # have a "sold-out" class on the parent div and either no
        # price or a struck-through one; we still keep the image.
        sold_out = "sold-out" in (div.get("class") or [])
        price_jpy: int | None = None
        if not sold_out:
            price_match = re.search(r"(\d[\d,]*)\s*円", text)
            if price_match:
                try:
                    price_jpy = int(price_match.group(1).replace(",", ""))
                except ValueError:
                    pass
        cards.append({
            "card_number": card_number,
            "image_url": image_url,
            "price_jpy": price_jpy,
            "sold_out": sold_out,
        })
    return cards


def build_card_id_candidates(tcgdex_id: str, number: str) -> list[str]:
    """TCGdex's JA card_id is `{setid}-{localid}`. Yuyutei's number is
    unpadded numeric. Try the same multi-padding pattern as our other
    imports for TCGdex's varying conventions."""
    seen: list[str] = []
    for variant in (number, number.lstrip("0") or number, number.zfill(3)):
        cid = f"{tcgdex_id}-{variant}"
        if cid not in seen:
            seen.append(cid)
    return seen


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
    # the row is already manual or pokemontcg (both stronger sources).
    return (
        f"UPDATE ptcg_cards SET "
        f"pricing_json = json_patch(COALESCE(pricing_json, '{{}}'), json_object('yuyutei', json('{payload_json}'))), "
        f"price_source = CASE "
        f"  WHEN price_source IN ('manual', 'pokemontcg') THEN price_source "
        f"  ELSE 'yuyutei' "
        f"END "
        f"WHERE card_id = '{cid}' AND lang = 'ja' "
        f"AND (price_source IS NULL OR price_source IN ('cardmarket', 'ebay_jp'));"
    )


def get_jpy_to_usd_rate() -> float:
    """Same FX cache layer as the eBay backfill. ECB rates via
    frankfurter.app, 2-day cache, hardcoded fallback."""
    if FX_CACHE_PATH.exists():
        try:
            cached = json.loads(FX_CACHE_PATH.read_text())
            if time.time() - cached.get("ts", 0) < FX_CACHE_TTL_S:
                return float(cached["rate"])
        except Exception:
            pass
    try:
        req = urllib.request.Request(
            "https://api.frankfurter.app/latest?from=JPY&to=USD",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        rate = float(data["rates"]["USD"])
        FX_CACHE_PATH.write_text(json.dumps({"rate": rate, "ts": time.time()}))
        return rate
    except Exception as exc:
        print(f"  FX fetch failed ({exc}); using fallback {JPY_TO_USD_FALLBACK}")
        return JPY_TO_USD_FALLBACK


if __name__ == "__main__":
    main()
