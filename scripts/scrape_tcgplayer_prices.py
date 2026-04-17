"""
Scrape TCGPlayer price guides for every set, via the firecrawl CLI.

Produces data/tcgplayer_raw/{set_id}.md for each successful scrape.
Maintains data/tcgplayer_raw/_checkpoint.json so we can resume mid-run.

Usage:
  python scripts/scrape_tcgplayer_prices.py           # scrape missing only
  python scripts/scrape_tcgplayer_prices.py --force   # re-scrape all
  python scripts/scrape_tcgplayer_prices.py OP-09     # single set

Slug resolution order:
  1. SLUG_OVERRIDES — hand-curated mapping for oddballs
  2. Match the set's label slugified against KNOWN_SLUGS (scraped from
     TCGPlayer's category landing)
  3. Fallback: label slugified as-is (may 404; we log and move on)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "tcgplayer_raw"
CHECKPOINT = RAW_DIR / "_checkpoint.json"
BASE_URL = "https://www.tcgplayer.com/categories/trading-and-collectible-card-games/one-piece-card-game/price-guides"
FIRECRAWL_CMD = ["npx", "firecrawl-cli@1.14.8"]

# Hand-curated mappings where the slug can't be derived from the label.
SLUG_OVERRIDES: dict[str, str] = {
    # Older booster packs (not on category landing)
    "OP-01": "romance-dawn",
    "OP-02": "paramount-war",
    "OP-03": "pillars-of-strength",
    "OP-04": "kingdoms-of-intrigue",
    # Later boosters confirmed from category landing
    "OP-05": "awakening-of-the-new-era",
    "OP-06": "wings-of-the-captain",
    "OP-07": "500-years-in-the-future",
    "OP-08": "two-legends",
    "OP-09": "emperors-in-the-new-world",
    "OP-10": "royal-blood",
    "OP-11": "a-fist-of-divine-speed",
    "OP-12": "legacy-of-the-master",
    "OP-13": "carrying-on-his-will",
    "OP14-EB04": "the-azure-seas-seven",
    "OP15-EB04": "adventure-on-kamis-island",
    # Extra / premium boosters
    "EB-01": "extra-booster-memorial-collection",
    "EB-02": "extra-booster-anime-25th-collection",
    "EB-03": "extra-booster-one-piece-heroines-edition",
    "PRB-01": "premium-booster-the-best",
    "PRB-02": "premium-booster-the-best-vol-2",
    # Older starter decks — guessed by convention; verified on scrape
    "ST-01": "starter-deck-1-straw-hat-crew",
    "ST-02": "starter-deck-2-worst-generation",
    "ST-03": "starter-deck-3-the-seven-warlords-of-the-sea",
    "ST-04": "starter-deck-4-animal-kingdom-pirates",
    "ST-05": "starter-deck-5-film-edition",
    "ST-06": "starter-deck-6-absolute-justice",
    "ST-07": "starter-deck-7-big-mom-pirates",
    "ST-08": "starter-deck-8-monkeydluffy",
    "ST-09": "starter-deck-9-yamato",
    "ST-10": "ultra-deck-the-three-captains",
    # Newer starter decks from category landing
    "ST-11": "starter-deck-11-uta",
    "ST-12": "starter-deck-12-zoro-and-sanji",
    "ST-13": "ultra-deck-the-three-brothers",
    "ST-14": "starter-deck-14-3d2y",
    "ST-15": "starter-deck-15-red-edwardnewgate",
    "ST-16": "starter-deck-16-green-uta",
    "ST-17": "starter-deck-17-blue-donquixote-doflamingo",
    "ST-18": "starter-deck-18-purple-monkeydluffy",
    "ST-19": "starter-deck-19-black-smoker",
    "ST-20": "starter-deck-20-yellow-charlotte-katakuri",
    "ST-21": "starter-deck-ex-gear-5",
    "ST-22": "starter-deck-22-ace-and-newgate",
    "ST-23": "starter-deck-23-red-shanks",
    "ST-24": "starter-deck-24-green-jewelry-bonney",
    "ST-25": "starter-deck-25-blue-buggy",
    "ST-26": "starter-deck-26-purple-and-black-monkeydluffy",
    "ST-27": "starter-deck-27-black-marshalldteach",
    "ST-28": "starter-deck-28-green-and-yellow-yamato",
    "ST-29": "starter-deck-29-egghead",
    # Promos / other
    "569901": "one-piece-promotion-cards",
    "569801": "one-piece-collection-sets",
}


def load_sets() -> list[dict]:
    with (DATA_DIR / "sets.json").open(encoding="utf-8") as f:
        return json.load(f)


def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        with CHECKPOINT.open(encoding="utf-8") as f:
            return json.load(f)
    return {"completed": [], "failed": [], "skipped": []}


def save_checkpoint(cp: dict) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT.open("w", encoding="utf-8") as f:
        json.dump(cp, f, indent=2)


def slug_for(set_id: str) -> str | None:
    return SLUG_OVERRIDES.get(set_id)


def is_valid_scrape(md_path: Path) -> bool:
    """A valid priceguide scrape should have at least one row."""
    if not md_path.exists() or md_path.stat().st_size < 5_000:
        return False
    text = md_path.read_text(encoding="utf-8", errors="ignore")
    return bool(re.search(r"Select table row\s+\d+", text))


def scrape_one(set_id: str, slug: str) -> tuple[bool, str]:
    """Run firecrawl scrape for a single set. Returns (ok, msg)."""
    url = f"{BASE_URL}/{slug}"
    out_path = RAW_DIR / f"{set_id}.md"

    print(f"  -> {set_id:12} {slug}")
    # --wait-for 5000 ensures the price table has time to JS-render.
    # Without it, ~10% of sets come back with an empty table.
    result = subprocess.run(
        FIRECRAWL_CMD + ["scrape", url, "--wait-for", "5000", "-o", str(out_path)],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        return False, f"firecrawl exit {result.returncode}: {result.stderr.strip()[:200]}"

    if not is_valid_scrape(out_path):
        return False, "no price rows found (likely 404 or wrong slug)"

    return True, f"ok ({out_path.stat().st_size:,} bytes)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("set_id", nargs="?", help="Scrape only this set")
    ap.add_argument("--force", action="store_true", help="Re-scrape already-completed sets")
    args = ap.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cp = load_checkpoint()
    sets = load_sets()

    if args.set_id:
        sets = [s for s in sets if s["set_id"] == args.set_id]
        if not sets:
            print(f"Set {args.set_id} not found in sets.json")
            sys.exit(1)

    for s in sets:
        set_id = s["set_id"]
        slug = slug_for(set_id)
        if not slug:
            print(f"  [skip] {set_id:12} no slug mapped")
            if set_id not in cp["skipped"]:
                cp["skipped"].append(set_id)
            continue

        out_path = RAW_DIR / f"{set_id}.md"
        if not args.force and set_id in cp["completed"] and is_valid_scrape(out_path):
            print(f"  [done] {set_id:12} {slug}")
            continue

        ok, msg = scrape_one(set_id, slug)
        if ok:
            print(f"         {msg}")
            if set_id not in cp["completed"]:
                cp["completed"].append(set_id)
            if set_id in cp["failed"]:
                cp["failed"].remove(set_id)
        else:
            print(f"         FAIL: {msg}")
            if set_id not in cp["failed"]:
                cp["failed"].append(set_id)

        save_checkpoint(cp)

    print()
    print(f"Completed: {len(cp['completed'])}  Failed: {len(cp['failed'])}  Skipped: {len(cp['skipped'])}")
    if cp["failed"]:
        print("Failed sets:", ", ".join(cp["failed"]))


if __name__ == "__main__":
    main()
