"""
Backfill images on TCGdex's vintage JA sets (E1-E5 + PMCG1-PMCG6) by
pulling the equivalent pkmnbindr catalog (ecard1_ja..gym2_ja).

Why this is its own script: the generic pkmnbindr importer
(import_pkmnbindr_jp_catalog.py) writes rows under pkmnbindr's set IDs
(ECARD1, BASE1, GYM1) instead of TCGdex's IDs (E1, PMCG1, PMCG5), and
TCGdex pads local_ids ('001') while pkmnbindr doesn't ('1'). So the
rows ended up duplicated across both ID conventions. This script does
the explicit mapping + zero-padding so images land on the canonical
TCGdex rows.

Mapping (verified against TCGdex JA API + pkmnbindr sets.json):
    E1    <- ecard1_ja  (基本拡張パック, 128 cards, 2001-12-01)
    E2    <- ecard2_ja  (地図にない町, 92 cards, 2002-03-08)
    E3    <- ecard3_ja  (海からの風, 90 cards, 2002-05-24)
    E4    <- ecard4_ja  (裂けた大地, 91 cards, 2002-08-23)
    E5    <- ecard5_ja  (神秘なる山, 91 cards, 2002-10-04)
    PMCG1 <- base1_ja   (拡張パック, 102 cards, 1996-10-20)
    PMCG2 <- base2_ja   (ポケモンジャングル, 48 cards, 1997-03-05)
    PMCG3 <- base3_ja   (化石の秘密, 48 cards, 1997-06-21)
    PMCG4 <- base4_ja   (ロケット団, 65 cards, 1997-11-21)
    PMCG5 <- gym1_ja    (リーダーズスタジアム, 96 cards, 1998-10-28)
    PMCG6 <- gym2_ja    (闇からの挑戦, 99 cards, 1999-06-25)

Usage:
    python -m scripts.backfill_pkmnbindr_vintage_jp
    python -m scripts.backfill_pkmnbindr_vintage_jp --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx


DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)
BASE = "https://www.pkmnbindr.com/data/jpNew"
USER_AGENT = "opbindr-ptcg-importer/1.0 (+https://opbindr.com; contact arjun@neuroplexlabs.com)"
REQ_INTERVAL_S = 0.4

MAPPING = [
    ("E1",    "ecard1_ja"),
    ("E2",    "ecard2_ja"),
    ("E3",    "ecard3_ja"),
    ("E4",    "ecard4_ja"),
    ("E5",    "ecard5_ja"),
    ("PMCG1", "base1_ja"),
    ("PMCG2", "base2_ja"),
    ("PMCG3", "base3_ja"),
    ("PMCG4", "base4_ja"),
    ("PMCG5", "gym1_ja"),
    ("PMCG6", "gym2_ja"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("1. Fetching pkmnbindr per-set catalogs...")
    updates: list[tuple[str, str, str, str]] = []  # (tcgdex_set, padded_local, image_high, image_low)
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
        for tcgdex_set, pkm_id in MAPPING:
            time.sleep(REQ_INTERVAL_S)
            try:
                resp = client.get(f"{BASE}/cards/{pkm_id}.json")
                if resp.status_code != 200:
                    print(f"   [{pkm_id}] not found ({resp.status_code}), skipping")
                    continue
                cards = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                print(f"   [{pkm_id}] fetch error: {exc}")
                continue

            filled = 0
            for c in cards:
                number = str(c.get("number") or "").strip()
                if not number:
                    continue
                # Zero-pad to 3 digits to match TCGdex's local_id convention.
                # TCGdex stores '001'..'128' for these vintage sets, pkmnbindr
                # uses bare integers — pad on lookup so the join finds them.
                padded = number.zfill(3)

                images = c.get("images") or []
                front = next((i for i in images if i.get("type") == "front"), images[0] if images else {})
                image_high = front.get("large") or front.get("medium") or front.get("small")
                image_low = front.get("small") or front.get("medium") or front.get("large")
                if not image_high:
                    continue

                updates.append((tcgdex_set, padded, image_high, image_low or image_high))
                filled += 1
            print(f"   [{pkm_id} -> {tcgdex_set}] {filled} cards with images (catalog has {len(cards)})")

    if not updates:
        print("Nothing to update. Exiting.")
        return

    print(f"\n2. Building SQL for {len(updates)} updates...")
    sql_path = OUT_DIR / "pkmnbindr_vintage_jp_images.sql"
    with sql_path.open("w", encoding="utf-8") as f:
        for set_id, local_id, hi, lo in updates:
            f.write(
                "UPDATE ptcg_cards SET "
                f"image_high = COALESCE(image_high, {sql_lit(hi)}), "
                f"image_low  = COALESCE(image_low,  {sql_lit(lo)}) "
                f"WHERE set_id={sql_lit(set_id)} AND lang='ja' "
                f"AND local_id={sql_lit(local_id)} AND image_high IS NULL;\n"
            )
    print(f"   SQL written to {sql_path}")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    print(f"\n3. Executing {len(updates)} UPDATEs against remote D1...")
    result = subprocess.run(WRANGLER_BIN + ["--remote", f"--file={sql_path}"])
    if result.returncode != 0:
        print("Execute failed.")
        sys.exit(result.returncode)
    print("Done.")


def sql_lit(val) -> str:
    if val is None:
        return "NULL"
    return "'" + str(val).replace("'", "''") + "'"


if __name__ == "__main__":
    main()
