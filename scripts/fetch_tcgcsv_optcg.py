"""Fetch every One Piece product + TCGPlayer market price from tcgcsv.com.

tcgcsv mirrors TCGPlayer's catalog API daily (category 68 = One Piece Card
Game). Compared with scraping the price-guide pages it is:
  * the same numbers: `marketPrice` is TCGPlayer's market price, the value
    the price guide shows (OP17-005 manga: $1,058.07 on both);
  * complete: every group, including the release-event, pre-release,
    anniversary, demo-deck and promo groups that have no entry in
    data/sets.json and so were never scraped;
  * structured: each product carries its card Number and Rarity, and an
    image URL used by image_match.py to verify promo mappings.

Output: data/tcgcsv_optcg.json — a list of rows shaped like
parse_tcgplayer_prices.parse_row output (tcg_id, name, name_suffix, number,
rarity, price) plus group_id, group_name, image_url. build_all_prices.py
reads it in place of the price-guide markdown when it exists.

Usage:
  python scripts/fetch_tcgcsv_optcg.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_tcgplayer_prices import NUMBER_RE, split_name  # noqa: E402

CATEGORY = 68
BASE = f"https://tcgcsv.com/tcgplayer/{CATEGORY}"
OUT = Path("data/tcgcsv_optcg.json")
UA = "optcg-api-pipeline/1.0"


def get_json(url: str, attempts: int = 4):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001 — transient network; retried
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"tcgcsv GET {url} failed after {attempts} attempts: {last}")


def pick_price(subtypes: list[dict]) -> float | None:
    """One market price per product, matching what the price guide shows.

    Most One Piece products have a single subtype (Normal or Foil). When both
    exist, TCGPlayer's guide shows the printing that has sales; prefer Normal
    when it has a market price (base commons), else Foil.
    """
    by = {s.get("subTypeName"): s.get("marketPrice") for s in subtypes}
    for name in ("Normal", "Foil"):
        if by.get(name) is not None:
            return by[name]
    return next((v for v in by.values() if v is not None), None)


def extended(product: dict, key: str) -> str | None:
    for e in product.get("extendedData") or []:
        if e.get("name") == key:
            return (e.get("value") or "").strip() or None
    return None


def rows_for_group(group: dict, products: list[dict], prices: list[dict]) -> list[dict]:
    by_product: dict[int, list[dict]] = {}
    for p in prices:
        by_product.setdefault(p["productId"], []).append(p)
    out = []
    for p in products:
        number = extended(p, "Number")
        if not number or not NUMBER_RE.match(number):
            continue  # sealed product, or a card without a printed number
        name, suffix = split_name(p["name"])
        out.append({
            "tcg_id": p["productId"],
            "name": name,
            "name_suffix": suffix,
            "number": number,
            "rarity": extended(p, "Rarity"),
            "price": pick_price(by_product.get(p["productId"], [])),
            "group_id": group["groupId"],
            "group_name": group["name"],
            "image_url": p.get("imageUrl"),
        })
    return out


def main() -> None:
    groups = get_json(f"{BASE}/groups")["results"]
    rows: list[dict] = []
    for g in groups:
        gid = g["groupId"]
        products = get_json(f"{BASE}/{gid}/products")["results"]
        prices = get_json(f"{BASE}/{gid}/prices")["results"]
        got = rows_for_group(g, products, prices)
        rows.extend(got)
        print(f"  {gid:6} {g.get('abbreviation') or '':12} {len(got):4} cards  {g['name'][:50]}")
        time.sleep(0.2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    priced = sum(r["price"] is not None for r in rows)
    print(f"\nWrote {len(rows)} card products ({priced} priced) from {len(groups)} groups to {OUT}")


if __name__ == "__main__":
    main()
