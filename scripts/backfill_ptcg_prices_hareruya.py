"""
Hareruya JP price backfill — production version of poc_hareruya_jp.py.

Pulls Hareruya2 product listings via Shopify's public /products.json,
parses (set_id, local_id) from titles, FX-converts JPY → USD, and writes
to D1's `ptcg_cards` table for cards where price_source IS NULL.

Priority position (in pickPrice chain): below tcgplayer/yuyutei, above
ebay_jp / cardmarket. Hareruya is JP retail — more stable than auction
prices, narrower than eBay JP, so it sits between Yuyutei (most authoritative
JP retail) and eBay JP (auction noise).

Stamps:
  pricing_json.hareruya = {price_jpy, price_usd, fetched_at, n_listings}
  price_source = 'hareruya'

Rollback:
  wrangler d1 execute optcg-cards --remote \\
    --command "UPDATE ptcg_cards SET price=NULL, price_source=NULL,
               pricing_json = json_remove(pricing_json, '$.hareruya')
               WHERE price_source='hareruya'"

Usage:
  python -m scripts.backfill_ptcg_prices_hareruya            # full run
  python -m scripts.backfill_ptcg_prices_hareruya --dry-run  # preview
  python -m scripts.backfill_ptcg_prices_hareruya --use-cached  # skip walk
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)

BASE = "https://www.hareruya2.com"
HEADERS = {
    "User-Agent": "OPBindr-pricing/1.0 (https://opbindr.com; arjun@neuroplexlabs.com)",
    "Accept": "application/json",
}
DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill/hareruya")
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_CACHE = Path("data/poc_hareruya/products_raw.jsonl")  # PoC's cache, reused

# 18 series-level collections covering every Pokemon TCG era.
COLLECTIONS = [
    "pmcg", "neo", "vs", "e", "adv", "pcg", "dp", "dpt", "legend",
    "bw", "xy", "generations", "sm", "sunmoon", "ss", "sv", "svm", "mega",
]

# Sanity bounds — anything outside is rejected as a parse / data anomaly.
MIN_USD = 0.01
MAX_USD = 100_000

TITLE_RE = re.compile(
    r"〈\s*(?P<lid>[A-Za-z0-9\-/]+?)\s*(?:/\s*[\dA-Za-z\-]+)?\s*〉\s*\[\s*(?P<setid>[^\]]+?)\s*\]"
)

HARERUYA_PROMO_MAP = {
    "S-P": "SWSHP", "SM-P": "SMP", "XY-P": "XYP", "BW-P": "BWP",
    "DP-P": "DPP",  "SV-P": "SVP", "P-P":  "PP",
}


def candidate_setids(s: str) -> list[str]:
    cands = {s, s.upper(), s.lower()}
    if s in HARERUYA_PROMO_MAP:
        cands.add(HARERUYA_PROMO_MAP[s])
    if re.match(r"^S\d", s):
        cands.add("SWSH" + s[1:].upper())
        cands.add("SWSH" + s[1:])
    if re.match(r"^M\d", s):
        cands.add("MEGA" + s[1:].upper())
        cands.add(s.upper())
    return list(cands)


def normalize_lid(lid: str) -> list[str]:
    out = [lid]
    if lid.lstrip("0") and lid.lstrip("0") != lid:
        out.append(lid.lstrip("0"))
    return out


def query_d1(query: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", query],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=str(REPO_ROOT),
    )
    data = json.loads(out.stdout)
    if not data or not data[0].get("success"):
        return []
    return data[0]["results"] or []


def fetch_jpy_to_usd() -> float:
    try:
        url = "https://api.frankfurter.app/latest?from=JPY&to=USD"
        req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=10) as r:
            return float(json.load(r)["rates"]["USD"])
    except Exception as e:
        print(f"FX fetch failed ({e}), using fallback 0.0067", file=sys.stderr)
        return 0.0067


def walk_collection(handle: str) -> list[dict]:
    products = []
    page = 1
    seen = set()
    while True:
        url = f"{BASE}/collections/{handle}/products.json?limit=250&page={page}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                break
            time.sleep(5)
            continue
        except Exception:
            time.sleep(5)
            continue
        page_products = data.get("products", [])
        if not page_products:
            break
        new = [p for p in page_products if p["id"] not in seen]
        if not new:
            break
        for p in new:
            seen.add(p["id"])
        products.extend(new)
        page += 1
        time.sleep(0.7)
        if page > 100:
            break
    return products


def walk_all() -> list[dict]:
    print(f"1. Walking {len(COLLECTIONS)} series collections...")
    all_products = []
    seen = set()
    for handle in COLLECTIONS:
        products = walk_collection(handle)
        new = [p for p in products if p["id"] not in seen]
        for p in new:
            seen.add(p["id"])
        all_products.extend(new)
        print(f"   [{handle:15s}] +{len(new):>5d}  total={len(all_products):>6d}", flush=True)
    print(f"   total: {len(all_products)}")
    return all_products


def index_products(products: list[dict]) -> dict[tuple[str, str], list[float]]:
    by_card = defaultdict(list)
    for p in products:
        m = TITLE_RE.search(p.get("title", "") or "")
        if not m:
            continue
        setid, lid = m.group("setid").strip(), m.group("lid").strip()
        prices = []
        for v in p.get("variants", []):
            if v.get("available"):
                try:
                    prices.append(float(v["price"]))
                except (KeyError, TypeError, ValueError):
                    pass
        if not prices:
            continue
        for sid in candidate_setids(setid):
            for l in normalize_lid(lid):
                by_card[(sid, l)].append(min(prices))
    return by_card


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--use-cached", action="store_true",
                    help="Reuse data/poc_hareruya/products_raw.jsonl from a prior PoC walk")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap UPDATEs (sample mode). Useful for first-deploy sanity check.")
    args = ap.parse_args()

    t0 = time.time()
    if args.use_cached and RAW_CACHE.exists():
        print(f"1. Reusing cached products from {RAW_CACHE}")
        products = []
        with RAW_CACHE.open(encoding="utf-8") as f:
            for line in f:
                products.append(json.loads(line))
        print(f"   total: {len(products)}")
    else:
        products = walk_all()
        # Cache for next run.
        with RAW_CACHE.open("w", encoding="utf-8") as f:
            for p in products:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")

    print("\n2. Indexing (set_id, local_id) → price...")
    by_card = index_products(products)
    print(f"   indexed pairs: {len(by_card)}")

    print("\n3. Pulling unpriced JA cards from D1...")
    cards = query_d1(
        "SELECT card_id, set_id, local_id FROM ptcg_cards "
        "WHERE lang='ja' AND price_source IS NULL AND name IS NOT NULL"
    )
    print(f"   {len(cards)} unpriced JA candidates")

    print("\n4. FX + match...")
    fx = fetch_jpy_to_usd()
    print(f"   FX: 1 JPY = {fx:.6f} USD")

    matches = []
    for c in cards:
        for lid in normalize_lid(c["local_id"]):
            jpy_list = by_card.get((c["set_id"], lid))
            if jpy_list:
                jpy = statistics.median(jpy_list)
                usd = round(jpy * fx, 2)
                if MIN_USD <= usd <= MAX_USD:
                    matches.append({
                        "card_id": c["card_id"],
                        "jpy": int(jpy),
                        "usd": usd,
                        "n_listings": len(jpy_list),
                    })
                break

    print(f"   matched: {len(matches)} ({100*len(matches)/max(1,len(cards)):.1f}%)")

    if args.limit:
        matches = matches[:args.limit]
        print(f"   --limit applied: capping at {args.limit}")

    if not matches:
        print("Nothing to write.")
        return

    # Build SQL UPDATEs. Use json_set for the pricing.hareruya sub-object so
    # we don't stomp other sources' data. The CASE WHEN price_source='manual'
    # guard preserves manual overrides — never stomped by automated runs.
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sql_lines = [
        "-- Hareruya JP price backfill (auto-generated). Only fills rows where",
        "-- price_source IS NULL. Manual overrides are preserved by the",
        "-- WHERE clause; tcgplayer/yuyutei rows are also preserved (they have",
        "-- price_source != NULL).",
        f"-- Generated: {fetched_at}",
        f"-- FX: 1 JPY = {fx:.6f} USD",
        f"-- Matches: {len(matches)}",
    ]
    for m in matches:
        hareruya_obj = json.dumps({
            "price_jpy": m["jpy"],
            "price_usd": m["usd"],
            "fetched_at": fetched_at,
            "n_listings": m["n_listings"],
        }, ensure_ascii=False).replace("'", "''")
        # ptcg_cards has no `price` column — value lives in pricing_json
        # and is extracted by the API's rowToSlim. Mirroring the yuyutei
        # script's CASE WHEN guard so we never stomp manual / pokemontcg
        # sources even if the WHERE narrows oddly.
        sql_lines.append(
            "UPDATE ptcg_cards SET "
            f"pricing_json = json_patch(COALESCE(pricing_json, '{{}}'), json_object('hareruya', json('{hareruya_obj}'))), "
            f"price_source = CASE WHEN price_source IN ('manual', 'pokemontcg') THEN price_source ELSE 'hareruya' END "
            f"WHERE lang='ja' AND card_id='{m['card_id']}' AND price_source IS NULL;"
        )

    sql_path = OUT_DIR / "hareruya_prices.sql"
    sql_path.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    matches_path = OUT_DIR / "matches.json"
    matches_path.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSQL written: {sql_path} ({len(sql_lines) - 7} statements)")
    print(f"Matches: {matches_path}")
    print(f"Walltime: {time.time()-t0:.1f}s")

    if args.dry_run:
        print("(dry run — D1 not touched)")
        return

    print("\n5. Applying to D1...")
    apply = subprocess.run(
        WRANGLER_BIN + ["--remote", "--file", str(sql_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT),
    )
    if apply.returncode != 0:
        print(f"D1 apply FAILED: {apply.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    # Print the meta block for visibility on rows_written count
    if "rows_written" in apply.stdout:
        for line in apply.stdout.split("\n"):
            if "rows_written" in line or "rows_read" in line or "duration" in line:
                print(f"   {line.strip()}")
    print(f"   apply complete in {time.time()-t0:.1f}s total")


if __name__ == "__main__":
    main()
