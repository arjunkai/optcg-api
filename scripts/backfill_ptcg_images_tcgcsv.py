"""
TCGCSV / TCGPlayer image backfill for residual PTCG cards.

TCGCSV (tcgcsv.com) provides a free, unauthenticated mirror of
TCGPlayer's product catalog. Every Pokémon TCG product has a
TCGPlayer productId and a direct CDN URL on tcgplayer-cdn.tcgplayer.com.

This catches the cards that even Bulbagarden lacks: XY Trainer Kits,
modern SVP Black Star Promos, edge cases of Mega Evolution & Celebrations.

Per-card flow:
  1. Look up the TCGCSV groupId from our set_id via SET_TO_GROUP map
  2. Fetch all products in that group (cached per-set)
  3. Match by card number (extendedData.Number = '1/30' format)
     and verify name match
  4. Pull the imageUrl from the product, swap _200w.jpg → _in_1000x1000.jpg
     for high-res
  5. COALESCE-fill image_high

Coverage rationale: TCGPlayer carries every English card sold in the US.
Even XY Trainer Kit individual cards (which Bulbagarden doesn't have)
all exist as TCGPlayer products.

Stamps source via URL host (tcgplayer-cdn.tcgplayer.com) — auditable.

Usage:
    python -m scripts.backfill_ptcg_images_tcgcsv --dry-run
    python -m scripts.backfill_ptcg_images_tcgcsv --set-id=tk-xy-sy --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TCGCSV = "https://tcgcsv.com/tcgplayer/3/{group_id}/products"
HEADERS = {"User-Agent": "OPBindr-image-backfill/1.0 (https://opbindr.app)"}
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = Path("data/tcgcsv_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Our set_id → TCGCSV groupId. Verified from groups list at
# https://tcgcsv.com/tcgplayer/3/groups
SET_TO_GROUP = {
    # Trainer Kits — Bulbagarden coverage gap
    "tk-xy-sy": 1532,   # XY Trainer Kit: Sylveon & Noivern
    "tk-xy-n":  1532,   # same kit, two halves split in TCGdex
    "tk-dp-l":  1541,   # DP Trainer Kit: Manaphy & Lucario
    "tk-dp-m":  1541,   # same
    "tk-bw-z":  1538,   # BW Trainer Kit: Excadrill & Zoroark
    "tk-bw-e":  1538,   # same
    "tk-sm-r":  2069,   # SM Trainer Kit: Lycanroc & Alolan Raichu
    "tk-sm-l":  2069,   # same
    "tk-xy-w":  1533,   # XY Trainer Kit: Bisharp & Wigglytuff
    "tk-xy-b":  1533,   # same
    "tk-xy-su": 1796,   # XY Trainer Kit: Pikachu Libre & Suicune
    "tk-xy-p":  1796,   # same
    "tk-xy-latia": 1536,  # XY Trainer Kit: Latias & Latios
    "tk-hs-r":  1540,   # HGSS Trainer Kit: Gyarados & Raichu
    "tk-hs-g":  1540,   # same
    "tk-ex-p":  1542,   # EX Trainer Kit 2: Plusle & Minun
    "tk-ex-latia": 1543,  # EX Trainer Kit 1: Latias & Latios
    # Recent/modern promos
    "svp":      None,   # SVP — set group ID lookup needed (not in our keyword scan)
    "2023sv":   23306,  # McDonald's Promos 2023 (TCGCSV equivalent of cel-style 2023sv)
    "2024sv":   24163,  # McDonald's Promos 2024
    # Standard sets that may have Bulbagarden gaps
    "mep":      24451,  # ME: Mega Evolution Promo
    "mee":      24461,  # MEE: Mega Evolution Energies
    "mfb":      23330,  # My First Battle
    # Energy/specials
    "sve":      None,   # Scarlet & Violet Energy — group ID lookup needed
    # Older/classic — used as fallback
    "ecard2":   1397,   # Aquapolis
    "ecard3":   1372,   # Skyridge
    # cel25 special: split into Celebrations 2867 + Classic Collection 2931
    "cel25":    2931,   # Classic Collection (the residual we want)
    # exu, xya, bwp — not directly needed (Bulbagarden covers these well)
}

# JA cards: JP set ↔ EN release. The English release uses the same card
# artwork at the same card numbers, so local_id matches across languages
# for the classic-era sets. Multi-set values mean we try each in order.
JA_TO_EN_GROUPS = {
    "PMCG1": [604],          # Base Set
    "PMCG2": [635],          # Jungle
    "PMCG3": [630],          # Fossil
    "PMCG5": [1441, 1440],   # Gym Heroes (primary), Gym Challenge
    "PMCG6": [1440, 1441],   # Gym Challenge (primary), Gym Heroes
    "neo2":  [1434],         # Neo Discovery
    "neo4":  [1444],         # Neo Destiny
    # PMCG4 (Team Rocket JP) — no exact 1-set EN equivalent (got mixed
    # into multiple later releases). Skip.
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("1. Resolving missing groupIds (svp, sve)...")
    _resolve_missing_groups()

    print("\n2. Querying D1 for imageless cards (EN + JA where mappable)...")
    en_in = ",".join(f"'{sid}'" for sid in SET_TO_GROUP if SET_TO_GROUP[sid])
    ja_in = ",".join(f"'{sid}'" for sid in JA_TO_EN_GROUPS)
    if args.set_id:
        clause = f"set_id = '{args.set_id}'"
    else:
        clause = (f"((lang='en' AND set_id IN ({en_in})) OR "
                  f" (lang='ja' AND set_id IN ({ja_in})))")
    sql = (f"SELECT card_id, name, set_id, local_id, lang FROM ptcg_cards "
           f"WHERE image_high IS NULL AND {clause} "
           f"ORDER BY lang, set_id, local_id")
    cards = query_d1(sql)
    print(f"   {len(cards)} cards in scope")
    by_set: dict[tuple, list[dict]] = defaultdict(list)
    for c in cards:
        by_set[(c["lang"], c["set_id"])].append(c)

    print("\n3. Fetching TCGCSV products per set + matching...")
    matches: list[dict] = []
    for (lang, sid), set_cards in sorted(by_set.items()):
        if lang == "en":
            gid = SET_TO_GROUP.get(sid)
            gids = [gid] if gid else []
        else:
            gids = JA_TO_EN_GROUPS.get(sid, [])
        if not gids:
            print(f"   [{lang}/{sid}] no group ID, skipping {len(set_cards)} cards")
            continue
        all_products = []
        for priority, gid in enumerate(gids):
            for p in _load_products(gid):
                # Annotate each product with its source-group priority so
                # the matcher can break number-collision ties (e.g. PMCG5
                # → prefer Gym Heroes over Gym Challenge).
                p2 = dict(p)
                p2["_group_priority"] = priority
                all_products.append(p2)
        print(f"   [{lang}/{sid}] {len(all_products)} TCGCSV products from {gids}, "
              f"{len(set_cards)} imageless")
        ms = match_set(set_cards, all_products)
        print(f"      → {len(ms)} matches")
        matches.extend(ms)

    if not matches:
        print("\nNo matches.")
        return

    sql_lines = build_update_sql(matches)
    sql_file = OUT_DIR / f"tcgcsv_images{'_'+args.set_id if args.set_id else ''}.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / f"tcgcsv_images{'_'+args.set_id if args.set_id else ''}_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n4. {len(matches)} matches. SQL → {sql_file}")

    if args.dry_run:
        print("--dry-run: skipping D1. Sample:")
        for m in matches[:10]:
            print(f"   {m['card_id']}: {m['image_url']}")
        return

    print(f"\n5. Executing {len(sql_lines)} UPDATEs...")
    r = subprocess.run(WRANGLER + ["--remote", f"--file={sql_file}"])
    if r.returncode != 0:
        sys.exit(r.returncode)
    print("Done.")


def _resolve_missing_groups() -> None:
    """svp (SVP Black Star Promos) and sve (S&V Energy) need a one-time
    lookup against the TCGCSV groups list."""
    if SET_TO_GROUP.get("svp") and SET_TO_GROUP.get("sve"):
        return
    try:
        d = _http_json("https://tcgcsv.com/tcgplayer/3/groups")
    except Exception as e:
        print(f"   group list fetch failed: {e}")
        return
    for g in d.get("results", []):
        n = (g.get("name") or "").lower()
        gid = g.get("groupId")
        if SET_TO_GROUP.get("svp") is None and ("svp" in n or "scarlet & violet promo" in n
                                                 or "scarlet & violet black star" in n
                                                 or "sv black star" in n):
            print(f"   svp: groupId={gid} ({g['name']!r})")
            SET_TO_GROUP["svp"] = gid
        if SET_TO_GROUP.get("sve") is None and ("sve" in n or
                                                 "scarlet & violet basic energ" in n or
                                                 "sv energy" in n):
            print(f"   sve: groupId={gid} ({g['name']!r})")
            SET_TO_GROUP["sve"] = gid


def _load_products(group_id: int) -> list[dict]:
    cache = CACHE_DIR / f"group_{group_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    url = TCGCSV.format(group_id=group_id)
    d = _http_json(url)
    prods = d.get("results") or d.get("products") or []
    cache.write_text(json.dumps(prods, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.2)
    return prods


def match_set(set_cards: list[dict], products: list[dict]) -> list[dict]:
    """Match each card to a TCGCSV product by (number, name)."""
    # Index products by their card-number AND by normalized name. Some
    # sets (My First Battle) have no Number field at all, so name match
    # is the only option.
    by_num: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        num = ""
        for ed in p.get("extendedData", []):
            if ed.get("name") == "Number":
                # Number can be '1/30', 'SVP 176', '15/132', '8/82'
                raw = (ed.get("value") or "")
                # Take last numeric run (handles 'SVP 176' → '176',
                # '15/132' → '15' if we split-on-/, but '15A3' → '15')
                # Use whichever portion gives a clean number.
                parts = re.split(r"[/\s]+", raw)
                num = parts[0] if parts else ""
                # Also keep the right-of-/ portion for SVP-style cards
                if len(parts) > 1 and not parts[0].strip().isdigit():
                    num = parts[-1]
                break
        if num:
            by_num[_normalize_id(num)].append(p)
        name_key = _normalize_name(p.get("name") or "")
        if name_key:
            by_name[name_key].append(p)

    matches = []
    for c in set_cards:
        candidates = []
        # Try several local_id normalizations: full, stripped trailing
        # alpha (74a → 74, 8A → 8), stripped leading alpha (H01 → 01 → 1).
        lid = c["local_id"]
        for variant in [
            _normalize_id(lid),
            _normalize_id(re.sub(r"[A-Za-z]+\d*$", "", lid)),  # 8A → 8, 15A3 → 15
            _normalize_id(re.sub(r"^[A-Za-z]+", "", lid)),     # H01 → 01 → 1
        ]:
            if not variant:
                continue
            candidates = by_num.get(variant, [])
            if candidates:
                break

        # Disambiguate by name when multiple at same number, OR fall
        # back to name-only match when number-index is empty (mfb).
        cn = _normalize_name(c["name"])
        if not candidates:
            if cn and cn in by_name:
                candidates = by_name[cn]
            else:
                # Fuzzy name match: any product whose normalized name
                # contains our name and is a basic energy/trainer
                for k, prods in by_name.items():
                    if cn and (cn in k):
                        candidates.extend(prods)
                        if len(candidates) >= 5:
                            break
        if not candidates:
            continue

        best = None
        for p in candidates:
            pn = _normalize_name(p.get("name", ""))
            if cn and (cn in pn or pn.startswith(cn[:5]) or cn.startswith(pn[:5])):
                best = p
                break
        if not best and len(candidates) == 1:
            best = candidates[0]
        if not best and any("_group_priority" in p for p in candidates):
            # JP→EN ambiguous: pick highest-priority group (lowest index)
            best = sorted(candidates, key=lambda p: p.get("_group_priority", 99))[0]
        if not best:
            continue  # ambiguous, skip rather than mismatch

        url = _hires_url(best.get("imageUrl") or "")
        if not url:
            continue
        matches.append({
            "card_id": c["card_id"], "lang": c["lang"],
            "filename": f"tcgplayer-{best.get('productId')}",
            "image_url": url,
            "_match_name": best.get("name"),
        })
    return matches


def _normalize_id(s: str) -> str:
    return s.lstrip("0").lower() or "0"


def _normalize_name(s: str) -> str:
    return re.sub(r"[\s'\-’.&!#()]+", "", s or "").lower()


def _hires_url(image_url: str) -> str:
    """TCGCSV gives `_200w.jpg`. Swap to `_in_1000x1000.jpg` for high-res."""
    return re.sub(r"_\d+w\.(jpg|jpeg|png)$", r"_in_1000x1000.\1", image_url)


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def query_d1(sql: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        print("D1 query failed:", (out.stderr or "")[:500])
        sys.exit(1)
    start = (out.stdout or "").find("[")
    if start < 0:
        return []
    try:
        payload = json.loads(out.stdout[start:])
    except json.JSONDecodeError:
        return []
    rows = payload[0].get("results", []) if isinstance(payload, list) else payload.get("results", [])
    return rows or []


def build_update_sql(matches: list[dict]) -> list[str]:
    lines = []
    for m in matches:
        cid = m["card_id"].replace("'", "''")
        url = m["image_url"].replace("'", "''")
        lang = m["lang"].replace("'", "''")
        lines.append(
            f"UPDATE ptcg_cards SET "
            f"image_high = COALESCE(image_high, '{url}'), "
            f"image_low  = COALESCE(image_low,  '{url}') "
            f"WHERE card_id = '{cid}' AND lang = '{lang}' AND image_high IS NULL;"
        )
    return lines


if __name__ == "__main__":
    main()
