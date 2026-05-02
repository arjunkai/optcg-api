"""
Pre-fetch canonical English names for JA cards. Two-tier lookup:
  1. TCGdex /v2/en/cards/{id} — if the card was released in EN too,
     this returns the canonical English name (works for trainers,
     energies, and modern Pokemon).
  2. Fallback to /v2/ja/cards/{id} dexId → PokeAPI species map (works
     for JP-only Pokemon cards).

Output: data/ja_card_id_to_en_name.json {card_id: en_name}

Used by both image and price backfill scripts as a fallback name
source for JA cards.

Usage:
    python -m scripts.enrich_ja_card_names              # imageless cards
    python -m scripts.enrich_ja_card_names --all-unpriced  # all unpriced JA
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path("data/ja_card_id_to_en_name.json")
SPECIES_CSV = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species_names.csv"
TCGDEX_EN = "https://api.tcgdex.net/v2/en/cards/{card_id}"
TCGDEX_JA = "https://api.tcgdex.net/v2/ja/cards/{card_id}"
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-unpriced", action="store_true",
                    help="Enrich ALL JA cards needing prices (not just imageless)")
    args = ap.parse_args()

    print("1. Loading National Dex → EN species name map from PokeAPI...")
    en_by_dex = _load_dex_to_en()
    print(f"   {len(en_by_dex)} species names")

    if args.all_unpriced:
        print("2. Querying D1 for ALL unpriced JA cards...")
        sql = ("SELECT card_id, name FROM ptcg_cards WHERE lang='ja' "
               "AND (price_source IS NULL OR price_source='cardmarket') "
               "ORDER BY card_id")
    else:
        print("2. Querying D1 for JA imageless cards...")
        sql = ("SELECT card_id, name FROM ptcg_cards WHERE lang='ja' "
               "AND image_high IS NULL ORDER BY card_id")
    out = subprocess.run(
        WRANGLER + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        print("D1 query failed:", (out.stderr or "")[:500])
        sys.exit(1)
    start = (out.stdout or "").find("[")
    rows = json.loads(out.stdout[start:])[0]["results"]
    print(f"   {len(rows)} JA cards needing enrichment")

    # Resume support: load existing cache
    cache: dict[str, str] = {}
    if OUT.exists():
        cache = json.loads(OUT.read_text(encoding="utf-8"))
        print(f"   Resume: {len(cache)} already cached")

    print("3. Resolving EN names per card (TCGdex EN endpoint, then dexId fallback)...")
    new_count = 0
    headers = {"User-Agent": "OPBindr/1.0"}
    for i, row in enumerate(rows, 1):
        cid = row["card_id"]
        if cid in cache and cache[cid]:
            continue
        en_name = ""
        # Tier 1: TCGdex EN endpoint — works for cards released in both
        # langs (covers modern trainers, energies, shared Pokemon)
        try:
            req = urllib.request.Request(
                TCGDEX_EN.format(card_id=urllib.parse.quote(cid)),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
            en_name = (d.get("name") or "").strip()
        except urllib.error.HTTPError as he:
            if he.code != 404:
                pass  # other transient errors — fall through to JA tier
        except Exception:
            pass
        # Tier 2: TCGdex JA endpoint — get dexId, look up species name
        if not en_name:
            try:
                req = urllib.request.Request(
                    TCGDEX_JA.format(card_id=urllib.parse.quote(cid)),
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    d = json.load(r)
                dex_ids = d.get("dexId") or []
                if dex_ids:
                    en_name = en_by_dex.get(dex_ids[0], "")
            except Exception:
                pass
        cache[cid] = en_name
        if en_name:
            new_count += 1
        if i % 50 == 0:
            print(f"   [{i}/{len(rows)}] cached {new_count} new resolutions")
            OUT.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.12)

    OUT.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    resolved = sum(1 for v in cache.values() if v)
    print(f"\n4. Wrote {OUT}")
    print(f"   {resolved}/{len(cache)} cards have an EN Pokemon name "
          f"({len(cache) - resolved} are non-Pokemon trainers/energies)")


def _load_dex_to_en() -> dict[int, str]:
    req = urllib.request.Request(SPECIES_CSV, headers={"User-Agent": "OPBindr/1.0"})
    data = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    out: dict[int, str] = {}
    for row in csv.DictReader(io.StringIO(data)):
        if int(row["local_language_id"]) == 9:  # English
            out[int(row["pokemon_species_id"])] = row["name"]
    return out


if __name__ == "__main__":
    main()
