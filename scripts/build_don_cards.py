"""
Build a DON card catalog from all scraped TCGPlayer price guides.

DON rows are identified by rarity == 'DON!!'. The Number column is always
empty for DON cards, so we dedupe by TCGPlayer product id (tcg_id) and assign
synthetic IDs (DON-001, DON-002, ...).

Processing order prioritizes ORIGINAL sets over PRB reprint bundles so a DON
card introduced in OP-09 gets set_id='OP-09', not 'PRB-01'.

Output:
  data/don_cards.json   — [{id, name, set_id, price, tcg_id, image_url, ...}]
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROW_RE = re.compile(r"^\|\s*Select table row\s+\d+", re.I)
NAME_LINK_RE = re.compile(r"\[([^\]]+)\]\(https://www\.tcgplayer\.com/product/(\d+)/")
PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "tcgplayer_raw"
OUT_PATH = DATA_DIR / "don_cards.json"

# DON images are served through the API proxy, which checks R2 first (curated
# high-res PDF images) and falls back to TCGPlayer CDN for uncurated cards.
# See src/images.js for the routing logic.
API_BASE = "https://optcg-api.arjunbansal-ai.workers.dev"
# Bump when R2 image contents change meaningfully. Must match IMAGE_VERSION
# in scripts/update_don_image_urls.js — the query param busts wsrv.nl + browser
# caches without requiring us to change the R2 keys.
IMAGE_VERSION = 4

# Sets to process last (reprint bundles). A DON that also appears in a regular
# set will be attributed to the regular set, which matches release history.
DEPRIORITIZED_PREFIXES = ("PRB-",)


def set_sort_key(set_id: str, pack_id: str) -> tuple:
    is_priority_low = set_id.startswith(DEPRIORITIZED_PREFIXES)
    return (is_priority_low, pack_id)


def clean_cell(text: str) -> str:
    if ":" in text and "<br>" in text:
        text = text.split("<br>", 1)[1]
    return text.split("<br>", 1)[0].strip()


def parse_don_rows(md_path: Path) -> list[dict]:
    """Extract DON rows from a single price-guide markdown."""
    out: list[dict] = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if not ROW_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 8:
            continue
        rarity = clean_cell(cells[5])
        if rarity != "DON!!":
            continue
        m = NAME_LINK_RE.search(cells[2])
        if not m:
            continue
        name = m.group(1).replace(" Thumbnail", "").strip()
        tcg_id = int(m.group(2))
        price_match = PRICE_RE.search(cells[7])
        price = float(price_match.group(1).replace(",", "")) if price_match else None
        out.append({
            "name": name,
            "tcg_id": tcg_id,
            "price": price,
        })
    return out


def main() -> None:
    sets_meta = json.loads((DATA_DIR / "sets.json").read_text(encoding="utf-8"))
    sets_meta.sort(key=lambda s: set_sort_key(s["set_id"], s["pack_id"]))

    catalog: dict[int, dict] = {}
    now = int(time.time())

    for s in sets_meta:
        md_path = RAW_DIR / f"{s['set_id']}.md"
        if not md_path.exists():
            continue
        for row in parse_don_rows(md_path):
            tcg_id = row["tcg_id"]
            if tcg_id in catalog:
                # Just add this set to tcg_ids history if we want — for now, skip
                continue
            catalog[tcg_id] = {
                "name": row["name"],
                "set_id": s["set_id"],
                "price": row["price"],
                "tcg_id": tcg_id,
            }

    # Assign synthetic IDs in insertion order (stable: original-set order)
    don_cards: list[dict] = []
    for i, (tcg_id, data) in enumerate(catalog.items(), start=1):
        don_id = f"DON-{i:03d}"
        don_cards.append({
            "id": don_id,
            "name": data["name"],
            "set_id": data["set_id"],
            "category": "Don",
            "rarity": "Don",
            "image_url": f"{API_BASE}/images/{don_id}?v={IMAGE_VERSION}",
            "price": data["price"],
            "tcg_ids": [tcg_id],
            "price_updated_at": now,
        })

    OUT_PATH.write_text(json.dumps(don_cards, ensure_ascii=False, indent=2), encoding="utf-8")

    # Summary
    from collections import Counter
    by_set = Counter(c["set_id"] for c in don_cards)
    print(f"Unique DON cards: {len(don_cards)}")
    print(f"Wrote {OUT_PATH}")
    print()
    print("By canonical set:")
    for s, n in sorted(by_set.items(), key=lambda x: -x[1])[:20]:
        print(f"  {s:12}  {n}")


if __name__ == "__main__":
    main()
