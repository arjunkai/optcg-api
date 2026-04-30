"""
Full pkmnbindr JP catalog import — adds *new* card rows to ptcg_cards
that TCGdex's sparser JA catalog doesn't have. This is the "why does
JA only have 6k cards while EN has 23k" fix.

TCGdex catalogs ~5,935 JA cards out of the actual ~24,000 in print.
pkmnbindr publishes a comprehensive 20,550-card JP catalog (211 sets)
as static JSON — same source we already partner with for image gap-fill.
This script INSERTs the cards we don't yet have, plus their parent
ptcg_sets rows when needed. Idempotent via INSERT OR IGNORE — re-runs
only add genuinely new cards.

Field mapping pkmnbindr -> ptcg_cards:
  id ('m4_ja-1')        -> card_id 'M4-1'  (uppercase set + dash + number)
  number ('1')          -> local_id '1'
  expansion.id          -> set_id 'M4'
  name (Japanese)       -> name
  supertype             -> category (translated)
  rarity_code           -> rarity
  hp                    -> hp (int)
  types                 -> types_csv
  subtypes[0]           -> stage
  converted_retreat_cost-> retreat
  images[0].large       -> image_high
  images[0].small       -> image_low
  pricing_json          -> {} (pkmnbindr has no prices; Yuyutei/eBay fill later)

Co-existence with the existing import-pkmnbindr-jp-d1.js: that script
COALESCE-fills images on existing rows. THIS script INSERTs missing
rows. Both run weekly.

Usage:
    python -m scripts.import_pkmnbindr_jp_catalog
    python -m scripts.import_pkmnbindr_jp_catalog --dry-run --limit=5
"""

from __future__ import annotations

import argparse
import json
import os
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
INSERT_BATCH = 100  # cards per multi-row INSERT to keep SQL files manageable

# pkmnbindr's supertype is in Japanese; map back to the English values
# TCGdex uses for `category` so the existing filter UI keeps working.
SUPERTYPE_MAP = {
    "ポケモン": "Pokemon",
    "トレーナー": "Trainer",
    "エネルギー": "Energy",
}

# pkmnbindr's subtypes[0] is the stage (also Japanese). Map to English.
STAGE_MAP = {
    "たね": "Basic",
    "1進化": "Stage1",
    "2進化": "Stage2",
    "MEGA": "Mega",
    "VMAX": "VMAX",
    "VSTAR": "VSTAR",
    "V": "V",
    "EX": "ex",
    "GX": "GX",
    "ex": "ex",
    "TAG TEAM": "TAG TEAM",
    "BREAK": "BREAK",
    "ACE SPEC": "ACE SPEC",
    "Ω": "Omega",
    "α": "Alpha",
    # Trainer subtypes
    "サポート": "Supporter",
    "グッズ": "Item",
    "スタジアム": "Stadium",
    "ポケモンのどうぐ": "Pokemon Tool",
    # Energy subtypes
    "基本": "Basic",
    "特殊": "Special",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't run it")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of pkmnbindr sets processed (smoke tests)")
    args = ap.parse_args()

    print("1. Fetching pkmnbindr JP set list...")
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
        sets_resp = client.get(f"{BASE}/sets/sets.json")
        sets_resp.raise_for_status()
        sets = sets_resp.json()

    if args.limit:
        sets = sets[:args.limit]
    print(f"   {len(sets)} sets in catalog\n")

    print("2. Fetching existing (card_id, lang) tuples from D1 to dedupe...")
    existing = query_existing_card_ids()
    print(f"   {len(existing)} JA cards already in D1\n")

    print("3. Walking pkmnbindr per-set JSON files...")
    sets_inserts: list[str] = []
    cards_inserts: list[tuple[str, ...]] = []
    new_card_count = 0
    set_seen: set[str] = set()

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
        for s in sets:
            time.sleep(REQ_INTERVAL_S)
            pkm_id = s.get("id")
            if not pkm_id:
                continue
            base_set = pkm_id.replace("_ja", "")
            tcgdex_set = base_set.upper()  # our convention

            # Build the ptcg_sets INSERT once per set (INSERT OR IGNORE
            # so existing TCGdex rows are preserved).
            if tcgdex_set not in set_seen:
                set_seen.add(tcgdex_set)
                sets_inserts.append(build_set_insert(tcgdex_set, s))

            # Per-set cards file
            try:
                resp = client.get(f"{BASE}/cards/{pkm_id}.json")
                if resp.status_code != 200:
                    print(f"   [{pkm_id}] not found ({resp.status_code}), skipping")
                    continue
                cards = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                print(f"   [{pkm_id}] fetch error: {exc}")
                continue

            new_in_set = 0
            for c in cards:
                row = build_card_row(c, tcgdex_set)
                if row is None:
                    continue
                # Dedupe against existing D1 rows.
                if (row["card_id"], "ja") in existing:
                    continue
                cards_inserts.append((
                    row["card_id"], "ja", row["set_id"], row["local_id"],
                    row["name"], row["category"], row["rarity"], row["hp"],
                    row["types_csv"], row["stage"], row["variants_json"],
                    row["image_low"], row["image_high"], row["pricing_json"],
                    row["retreat"],
                ))
                new_in_set += 1
            new_card_count += new_in_set
            if new_in_set > 0:
                print(f"   [{pkm_id} -> {tcgdex_set}] +{new_in_set} new cards (catalog has {len(cards)})")

    print(f"\n   {len(sets_inserts)} ptcg_sets INSERT OR IGNORE statements")
    print(f"   {new_card_count} ptcg_cards rows to insert\n")
    if not cards_inserts:
        print("Nothing to insert. Exiting.")
        return

    sql_path = OUT_DIR / "pkmnbindr_jp_catalog.sql"
    with sql_path.open("w", encoding="utf-8") as f:
        for stmt in sets_inserts:
            f.write(stmt + "\n")
        # Write multi-row INSERTs in batches to keep individual SQL
        # statements small enough for D1.
        cols = (
            "card_id", "lang", "set_id", "local_id", "name", "category", "rarity",
            "hp", "types_csv", "stage", "variants_json",
            "image_low", "image_high", "pricing_json", "retreat",
        )
        for i in range(0, len(cards_inserts), INSERT_BATCH):
            batch = cards_inserts[i:i + INSERT_BATCH]
            values = ", ".join("(" + ", ".join(sql_lit(v) for v in row) + ")" for row in batch)
            f.write(
                f"INSERT OR IGNORE INTO ptcg_cards ({', '.join(cols)}) VALUES {values};\n"
            )
    print(f"4. SQL written to {sql_path}")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    print("\n5. Executing against remote D1...")
    result = subprocess.run(WRANGLER_BIN + ["--remote", f"--file={sql_path}"])
    if result.returncode != 0:
        print("Execute failed.")
        sys.exit(result.returncode)
    print("Done.")


def query_existing_card_ids() -> set[tuple[str, str]]:
    """Pull every (card_id, 'ja') tuple already in D1 so we can dedupe
    cheaply in memory before constructing the INSERT batch."""
    sql = "SELECT card_id, lang FROM ptcg_cards WHERE lang = 'ja'"
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print("D1 query failed:", out.stderr[:500])
        sys.exit(1)
    start = out.stdout.find("[")
    if start < 0:
        return set()
    try:
        payload = json.loads(out.stdout[start:])
    except json.JSONDecodeError:
        return set()
    rows = payload[0].get("results", []) if isinstance(payload, list) else payload.get("results", [])
    return {(r["card_id"], r["lang"]) for r in rows}


def build_set_insert(tcgdex_set: str, pkm_set: dict) -> str:
    # Prefer pkmnbindr's English translation for set names so the
    # filter UI (which renders these labels) shows English consistently
    # across both langs. pkmnbindr embeds translation.en.name on most
    # SV-era and modern sets; vintage sets fall back to the native
    # (Japanese) name.
    en_name = (pkm_set.get("translation") or {}).get("en", {}).get("name")
    name = en_name or pkm_set.get("name") or tcgdex_set
    series = pkm_set.get("series") or ""
    release_date = pkm_set.get("release_date") or ""
    total = pkm_set.get("total")
    printed = pkm_set.get("printed_total")
    logo = pkm_set.get("logo") or ""
    symbol = pkm_set.get("symbol") or ""
    return (
        "INSERT OR IGNORE INTO ptcg_sets "
        "(set_id, lang, name, series, release_date, card_count_total, card_count_official, logo_url, symbol_url) "
        f"VALUES ({sql_lit(tcgdex_set)}, 'ja', {sql_lit(name)}, {sql_lit(series)}, "
        f"{sql_lit(release_date)}, {sql_lit(total)}, {sql_lit(printed)}, "
        f"{sql_lit(logo)}, {sql_lit(symbol)});"
    )


def build_card_row(c: dict, tcgdex_set: str) -> dict | None:
    number = str(c.get("number") or "").strip()
    if not number:
        return None
    card_id = f"{tcgdex_set}-{number}"

    images = c.get("images") or []
    front = next((i for i in images if i.get("type") == "front"), images[0] if images else {})
    image_high = front.get("large") or front.get("medium") or front.get("small") or None
    image_low = front.get("small") or front.get("medium") or front.get("large") or None

    supertype = c.get("supertype") or ""
    category = SUPERTYPE_MAP.get(supertype, supertype or None)

    subtypes = c.get("subtypes") or []
    stage = STAGE_MAP.get(subtypes[0], subtypes[0]) if subtypes else None

    # Use the English translation of types so the AddCardsModal type
    # filter pills (POKEMON_TYPES, English) match JA rows. pkmnbindr
    # embeds translation.en.types per card. Falls back to native types
    # if the translation block is missing.
    en_types = (c.get("translation") or {}).get("en", {}).get("types") or []
    types = en_types or c.get("types") or []
    types_csv = ",".join(types) if types else None

    hp_raw = c.get("hp")
    try:
        hp = int(hp_raw) if hp_raw not in (None, "") else None
    except (TypeError, ValueError):
        hp = None

    retreat = c.get("converted_retreat_cost")
    if not isinstance(retreat, int):
        retreat = None

    rarity = c.get("rarity_code") or c.get("rarity") or None

    return {
        "card_id": card_id,
        "set_id": tcgdex_set,
        "local_id": number,
        "name": c.get("name") or "",
        "category": category,
        "rarity": rarity,
        "hp": hp,
        "types_csv": types_csv,
        "stage": stage,
        "variants_json": "{}",  # pkmnbindr's variant model differs; leave empty
        "image_low": image_low,
        "image_high": image_high,
        "pricing_json": "{}",  # filled later by Yuyutei / eBay JP
        "retreat": retreat,
    }


def sql_lit(val) -> str:
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    return "'" + str(val).replace("'", "''") + "'"


if __name__ == "__main__":
    main()
