"""
classify_variants.py — auto-classify parallel card variant types
Scrapes Limitless TCG search filters to identify manga and serial cards.
All other _p variants default to Alternate Art, _r variants to Reprint.

Run: python classify_variants.py
Output: data/variant_types.json
"""

import json
import re
import httpx
from pathlib import Path

LIMITLESS_BASE = "https://onepiece.limitlesstcg.com/cards"
OUT = Path("data/variant_types.json")

# Limitless search queries and the variant_type they map to
QUERIES = {
    "Manga Art": "is:manga format:english",
    "Serial": "is:serial format:english",
}


def fetch_card_ids(client: httpx.Client, query: str) -> list[str]:
    """Fetch a Limitless search page and extract card IDs from image URLs.

    Image URLs look like: .../OP15-118_p2_EN.webp
    We extract: OP15-118_p2
    """
    resp = client.get(LIMITLESS_BASE, params={"q": query})
    resp.raise_for_status()
    html = resp.text

    # Match image filenames like OP15-118_p2_EN.webp or OP01-016_p7_EN.webp
    pattern = r'([A-Z]{2}\d{2}-\d{3}_p\d+)_EN\.webp'
    matches = re.findall(pattern, html)
    return list(dict.fromkeys(matches))  # dedupe, preserve order


def main():
    mapping = {}

    with httpx.Client(
        headers={"User-Agent": "OPTCG-API-Classifier/1.0"},
        follow_redirects=True,
        timeout=30,
    ) as client:
        for variant_type, query in QUERIES.items():
            card_ids = fetch_card_ids(client, query)
            for card_id in card_ids:
                mapping[card_id] = variant_type
            print(f"  {variant_type}: {len(card_ids)} cards")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(mapping)} overrides to {OUT}")


if __name__ == "__main__":
    main()
