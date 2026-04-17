"""
One-shot fetch of dotgg.gg's catalog — {card_id: tcg_id, price, foilPrice, ...}.
Saved to data/dotgg_catalog.json for the mapper + backfill scripts to consume.

dotgg is the authoritative tcg_id <-> card_id mapping. We use it to know
which TCGPlayer product corresponds to our _p8, _r2, etc. slots, which we
can't figure out positionally because TCGPlayer sometimes interleaves cheap
promo variants with expensive alt arts.

Usage:
  python scripts/fetch_dotgg_catalog.py
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0"
BASE = "https://api.dotgg.gg/cgfw"
BATCH_SIZE = 200
OUT_FILE = Path("data/dotgg_catalog.json")


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> None:
    catalog: dict[str, dict] = {}
    page = 1
    while True:
        rq = urllib.parse.quote(json.dumps({"page": page, "pageSize": BATCH_SIZE}))
        url = f"{BASE}/getcardsfiltered?game=onepiece&rq={rq}"
        data = fetch_json(url)
        rows = data if isinstance(data, list) else data.get("data") or []
        if not rows:
            break
        for r in rows:
            cid = r.get("id")
            if cid:
                catalog[cid] = r
        print(f"  page {page}: +{len(rows)} (total {len(catalog)})")
        if len(rows) < BATCH_SIZE:
            break
        page += 1
        time.sleep(0.5)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(catalog)} cards to {OUT_FILE}")


if __name__ == "__main__":
    main()
