"""
Pre-fetch canonical English names for JA cards. Tiered lookup:
  1. Hit TCGdex /v2/ja/cards/{id} to get the JA card's `dexId` list.
     If it has one (i.e. the card is a Pokemon), map dexId[0] →
     PokeAPI species name. This is COLLISION-PROOF — JA Charmander
     and EN Entei may share `card_id=DP3-4` but they have different
     dexIds, so the species map never confuses them.
  2. For trainers / energies (no dexId), fall back to /v2/en/cards/{id}
     and accept the EN name only if the EN row also has no dexId. If
     EN has a dexId the IDs collide on different cards — reject and
     leave en_name empty (better NULL than wrong).

Output: data/ja_card_id_to_en_name.json {card_id: en_name}

Used by both image and price backfill scripts as a fallback name
source for JA cards, and by scripts/backfill-ptcg-name-en.js to
populate ptcg_cards.name_en in D1 (drives latin-script search of
Japanese cards).

Usage:
    python -m scripts.enrich_ja_card_names              # imageless cards
    python -m scripts.enrich_ja_card_names --all-unpriced  # all unpriced JA
    python -m scripts.enrich_ja_card_names --all          # every JA card
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

# Known card_ids where the tier-1 EN lookup returns a wrong species. The
# tier-1 logic assumes `card_id is identical in EN and JA TCGdex catalogs
# = same card`, but a handful of sets diverge (different ordering, or one
# language assigns the same numeric id to a different Pokemon). Manual
# entries here always win over both tier-1 and tier-2 lookups.
# Verified 2026-05-13 by spot-check (search for "charmander" in JP scope
# returned Golem at DP3-82 because TCGdex EN DP3-82 maps to Charmander
# while JA DP3-82 is Golem).
MANUAL_OVERRIDES: dict[str, str] = {
    "DP3-82": "Golem",
}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-unpriced", action="store_true",
                    help="Enrich ALL JA cards needing prices (not just imageless)")
    ap.add_argument("--all", action="store_true",
                    help="Enrich every JA card in D1. Use after fixing a "
                         "logic bug to refresh the whole mapping.")
    ap.add_argument("--rebuild", action="store_true",
                    help="Ignore the existing cache and re-resolve every "
                         "card. Pair with --all after a logic fix to "
                         "overwrite stale entries.")
    args = ap.parse_args()

    print("1. Loading National Dex → EN species name map from PokeAPI...")
    en_by_dex = _load_dex_to_en()
    print(f"   {len(en_by_dex)} species names")

    if args.all:
        print("2. Querying D1 for ALL JA cards...")
        sql = "SELECT card_id, name FROM ptcg_cards WHERE lang='ja' ORDER BY card_id"
    elif args.all_unpriced:
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

    # Resume support: load existing cache (unless --rebuild)
    cache: dict[str, str] = {}
    if OUT.exists() and not args.rebuild:
        cache = json.loads(OUT.read_text(encoding="utf-8"))
        print(f"   Resume: {len(cache)} already cached")
    elif args.rebuild and OUT.exists():
        print(f"   --rebuild: ignoring existing cache, will overwrite")

    print("3. Resolving EN names per card (JA dexId → PokeAPI species, "
          "fallback to EN endpoint for trainers/energies)...")
    new_count = 0
    headers = {"User-Agent": "OPBindr/1.0"}
    for i, row in enumerate(rows, 1):
        cid = row["card_id"]
        if not args.rebuild and cid in cache and cache[cid] and cid not in MANUAL_OVERRIDES:
            continue
        # Manual overrides for known cross-language card_id collisions
        # always win — see MANUAL_OVERRIDES comment above.
        if cid in MANUAL_OVERRIDES:
            cache[cid] = MANUAL_OVERRIDES[cid]
            new_count += 1
            continue
        en_name = ""
        ja_dex_ids: list[int] = []

        # Tier 1: JA dexId → PokeAPI species. Pokemon cards always have
        # a dexId; this path is collision-proof because the species
        # name is derived from the Pokemon's national-dex identity,
        # not from a card_id lookup against the EN catalog.
        try:
            req = urllib.request.Request(
                TCGDEX_JA.format(card_id=urllib.parse.quote(cid)),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                d_ja = json.load(r)
            ja_dex_ids = d_ja.get("dexId") or []
            if ja_dex_ids:
                en_name = en_by_dex.get(ja_dex_ids[0], "")
        except Exception:
            pass

        # Tier 2: trainer / energy fallback. No dexId on either side
        # → safe to accept the EN endpoint's name. If EN has a dexId
        # but JA doesn't, the IDs collide on different kinds of cards
        # (a Pokemon in EN, a trainer/energy in JA) — reject the EN
        # name and leave NULL. Better unsearchable than mis-labelled.
        if not en_name and not ja_dex_ids:
            try:
                req = urllib.request.Request(
                    TCGDEX_EN.format(card_id=urllib.parse.quote(cid)),
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    d_en = json.load(r)
                en_dex_ids = d_en.get("dexId") or []
                if not en_dex_ids:
                    en_name = (d_en.get("name") or "").strip()
            except urllib.error.HTTPError as he:
                if he.code != 404:
                    pass
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
