"""
TCGCSV / TCGPlayer price backfill for PTCG cards.

For every TCGPlayer-listed product TCGCSV exposes a marketPrice (USD)
plus low/mid/high. Match our card_id → TCGPlayer productId by (set,
local_id, name) the same way the image backfill does, then write the
marketPrice to D1.

Pricing semantics:
  - marketPrice is TCGPlayer's calculated market value (USD), the same
    one shown on the product page. Most authoritative free source.
  - Some products carry multiple subTypes (Normal, Holofoil, Reverse
    Holofoil). We take the price for the subType that best matches our
    card's rarity, falling back to the first available.
  - Stamps price_source='tcgplayer' for auditability + rollback.

Does NOT overwrite manual / pokemontcg / ebay_us prices. Only fills
NULL or cardmarket-EUR rows since TCGPlayer USD beats cardmarket-only.

Rollback:
    UPDATE ptcg_cards SET price_source=NULL,
                          pricing_json=json_remove(pricing_json,'$.tcgplayer')
        WHERE price_source='tcgplayer';

Usage:
    python -m scripts.backfill_ptcg_prices_tcgcsv --dry-run
    python -m scripts.backfill_ptcg_prices_tcgcsv --lang=en
    python -m scripts.backfill_ptcg_prices_tcgcsv --lang=ja
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

PRODUCTS_URL = "https://tcgcsv.com/tcgplayer/{cat_id}/{group_id}/products"
PRICES_URL = "https://tcgcsv.com/tcgplayer/{cat_id}/{group_id}/prices"
GROUPS_URL = "https://tcgcsv.com/tcgplayer/{cat_id}/groups"
# TCGPlayer category IDs: 3=Pokemon (EN), 85=Pokemon Japan (JP-native)
CAT_EN = 3
CAT_JA = 85

# Manual overrides for JA sets where TCGdex set_id doesn't equal TCGCSV
# abbreviation. TCGCSV often uses hyphenated promo abbreviations
# (XY-P, BW-P, S-P) where TCGdex uses unhyphenated (XYP, BWP, SWSHP).
JA_OVERRIDES = {
    # Promo sets — TCGdex unhyphenated → TCGCSV hyphenated
    "SVP":   23779,  # SV-P Promotional Cards
    "SMP":   23881,  # SM-P Sun & Moon Promos
    "SWSHP": 23876,  # S-P Sword & Shield Promos
    "XYP":   23908,  # XY-P XY Promos
    "BWP":   24342,  # BW-P Promotional cards
    "MP":    24423,  # M-P Mega Evolution Promos
    "DPP":   24137,  # DP-P Promotional cards
    "ADVP":  24140,  # ADV-P Promotional cards
    "PCGP":  24138,  # PCG-P Promotional cards
    "DPtP":  24136,  # DPt-P Promotional cards
    # Modern SWSH JP sets — TCGdex uses SWSH prefix, TCGCSV uses S prefix
    "SWSH1":   23616,  # S1W: Sword (closest match for base SWSH1 / SWSH2)
    "SWSH2":   23617,  # S1H: Shield
    "SWSH1A":  23633,  # S1a: VMAX Rising
    "SWSH2A":  23634,  # S2a: Explosive Walker
    "SWSH3":   23619,  # S3: Infinity Zone
    "SWSH3A":  23635,  # S3a: Legendary Heartbeat
    "SWSH4":   23620,  # S4: Amazing Volt Tackle
    "SWSH4A":  23643,  # S4a: Shiny Star V
    "SWSH5":   23621,  # S5I: Single Strike Master (also S5R; pick I)
    "SWSH5I":  23621,  # S5I: Single Strike Master
    "SWSH5R":  23622,  # S5R: Rapid Strike Master
    "SWSH5A":  23636,  # S5a: Peerless Fighters
    "SWSH6":   23623,  # S6H: Silver Lance (also S6K)
    "SWSH6H":  23623,  # S6H: Silver Lance
    "SWSH6K":  23624,  # S6K: Jet-Black Spirit
    "SWSH6A":  23637,  # S6a: Eevee Heroes
    "SWSH7":   23625,  # S7D: Skyscraping Perfection (also S7R)
    "SWSH7D":  23625,  # S7D: Skyscraping Perfection
    "SWSH7R":  23626,  # S7R: Blue Sky Stream
    # DP era (Diamond & Pearl)
    "DP1":     23973,  # DP1: Space-Time Creation
    "DP2":     23974,  # DP2: Secret of the Lakes
    "DP3":     23975,  # DP3: Shining Darkness
    "DP5":     23978,  # DP5: Temple of Anger (also Cry from the Mysterious 23979)
    # SWSH split pairs (S1W/S1H Sword & Shield, S10D/S10P Time/Space)
    "SWSH1W":  23616,  # S1W: Sword
    "SWSH1H":  23617,  # S1H: Shield
    "SWSH10D": 23629,  # S10D: Time Gazer
    "SWSH10P": 23630,  # S10P: Space Juggler
    # XY split pairs and named subsets
    "XY1X":    23914,  # XY-Bx: Collection X
    "XY1Y":    23915,  # XY-By: Collection Y
    "XY7":     23924,  # XY7: Bandit Ring
    "XY8B":    23925,  # XY8-Bb: Blue Shock
    "XY8R":    23926,  # XY8-Br: Red Flash
    "XY9R":    23927,  # XY9: Rage of the Broken Heavens
    "XY9B":    23927,  # same combined group
    "XY11A":   23916,  # XY11-Bb: Fever-Burst Fighter (might also be 23917 Cruel Traitor)
    # XY5 Tide / Gaia split
    "XY5T":    23921,  # XY5-Bg: Gaia Volcano (combined for both halves)
    "XY5G":    23921,
    # BW era split-pair sets
    "BW2":     23895,  # BW2: Red Collection (corrected — was confused with BW1)
    "BW3F":    23896,  # BW3: Psycho Drive
    "BW3H":    23897,  # BW3: Hail Blizzard
    "BW4":     23898,  # BW4: Dark Rush
    "BW5B":    23900,  # BW5: Dragon Blade
    "BW5D":    23899,  # BW5: Dragon Blast
    "BW6F":    23901,  # BW6: Freeze Bolt
    "BW6C":    23902,  # BW6: Cold Flare
    "BW7":     23903,  # BW7: Plasma Gale
    "BW8S":    23904,  # BW8: Spiral Force
    "BW8T":    23905,  # BW8: Thunder Knuckle
    "BW9":     23906,  # BW9: Megalo Cannon
    # Legend (HGSS era)
    "L1HG":    24025,  # L1: HeartGold Collection
    "L1SS":    24026,  # L1: SoulSilver Collection
    "LP":      24023,  # L-P: Legends Promos
    # Vintage classics that DO have cat 85 entries
    "VS1":     24180,  # Pokemon VS
    "EBB1":    23912,  # EX Battle Boost (Eevee Heroes companion)
    "web1":    24141,  # Pokemon Web
    # Other vintage
    "BS":      None,   # 1996-1997 Carddass — no cat 85 group; PriceCharting only
    "SWSH8":   23627,  # S8: Fusion Arts
    "SWSH8A":  23638,  # S8a: 25th Anniversary Collection
    "SWSH8B":  23644,  # S8b: VMAX Climax
    "SWSH9":   23628,  # S9: Star Birth
    "SWSH9A":  23639,  # S9a: Battle Region
    "SWSH10":  23629,  # S10D: Time Gazer (also S10P; pick D)
    "SWSH10A": 23640,  # S10a: Dark Phantasma
    "SWSH10B": 23641,  # S10b: Pokemon GO
    "SWSH11":  23631,  # S11: Lost Abyss
    "SWSH11A": 23642,  # S11a: Incandescent Arcana
    "SWSH12":  23632,  # S12: Paradigm Trigger
    "SWSH12A": 23645,  # S12a: VSTAR Universe
    # SM era — most match by abbreviation but some uppercase variants
    # (SM8B, SM12A) need explicit mapping
    "SM8B":    23708,  # SM8b: GX Ultra Shiny
    "SM12A":   23709,  # SM12a: TAG TEAM GX: Tag All Stars
    "SM10A":   23703,  # SM10a: GG End
    "SM10B":   23704,  # SM10b: Sky Legend
    "SM11A":   23705,  # SM11a: Remix Bout
    "SM11B":   23706,  # SM11b: Dream League
    # SM "P" suffix in our DB = SM "+" enhanced expansion packs in TCGCSV
    "SM1P":    23692,  # SM1+: Sun & Moon Enhanced Expansion Pack
    "SM2P":    23693,  # SM2+: Facing a New Trial
    "SM3P":    23694,  # SM3+: Shining Legends
    "SM4P":    23707,  # SM4+: GX Battle Boost
    "SM5P":    23695,  # SM5+: Ultra Force
    # Classic JP main sets — verified by name in TCGCSV cat 85
    "PMCG1": 23721,    # Expansion Pack (JP Base Set, 1996)
    "PMCG2": 23722,    # Pokemon Jungle (JP)
    "PMCG3": 23723,    # Mystery of the Fossils (JP Fossil)
    "PMCG4": 23724,    # Rocket Gang (JP Team Rocket)
    "PMCG5": 23725,    # Leaders' Stadium
    "PMCG6": 23726,    # Challenge from the Darkness
    "neo4":  23729,    # Darkness, and to Light...
    # e-Card era
    "E1":    23730,    # Base Expansion Pack
    "E4":    23734,    # Mysterious Mountains
    "E5":    23720,    # Awakening Legends
    # ADV era
    "ADV1":  24129,    # ADV Expansion Pack
    "ADV2":  24101,    # Mirage Forest
    # PCG era — Miracle Crystal exact match; others ambiguous
    "PCG4":  24099,    # Miracle Crystal
    # VS series
    "VS1":   24180,    # Pokemon VS
    # BW JP
    "BW1":   23893,    # BW1: Black Collection
    "BW2":   23894,    # BW1: White Collection (same JP release year)
}
HEADERS = {"User-Agent": "OPBindr-price-backfill/1.0 (https://opbindr.app)"}
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = Path("data/tcgcsv_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Subtype priority for card variants. We pick the price matching this
# order when multiple subTypes exist for a productId.
SUBTYPE_PRIORITY = ["Normal", "Holofoil", "Reverse Holofoil", "Unlimited",
                     "1st Edition Holofoil", "1st Edition", "Foil"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["en", "ja"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if not args.lang:
        ap.error("specify --lang")

    # Reuse the image-backfill set→group maps so the matching stays in sync
    from scripts.backfill_ptcg_images_tcgcsv import (
        SET_TO_GROUP, _resolve_missing_groups,
    )
    cat_id = CAT_EN if args.lang == "en" else CAT_JA
    if args.lang == "en":
        print("1. Resolving missing EN groupIds...")
        _resolve_missing_groups()

    print(f"\n2. Querying D1 for {args.lang} cards needing prices...")
    sql = (f"SELECT card_id, name, set_id, local_id, lang, rarity FROM ptcg_cards "
           f"WHERE lang='{args.lang}' "
           f"AND (price_source IS NULL OR price_source='cardmarket') "
           f"ORDER BY set_id, local_id")
    cards = query_d1(sql)
    print(f"   {len(cards)} cards in scope")

    # Auto-discover groupId per set against the right category.
    print(f"\n2b. Auto-discovering TCGCSV group IDs in category {cat_id}...")
    set_ids = sorted({c["set_id"] for c in cards})
    set_to_group: dict[str, int] = {}
    if args.lang == "en":
        # Carry over the curated EN map for known matches
        for sid in set_ids:
            if SET_TO_GROUP.get(sid):
                set_to_group[sid] = SET_TO_GROUP[sid]
    for sid in set_ids:
        if sid in set_to_group:
            continue
        gid = _discover_group(sid, cat_id)
        if gid:
            set_to_group[sid] = gid
    unmapped = [s for s in set_ids if s not in set_to_group]
    print(f"   Mapped {len(set_to_group)}/{len(set_ids)} sets. "
          f"Unmapped: {unmapped[:10]}{' ...' if len(unmapped) > 10 else ''}")

    if args.limit:
        cards = cards[:args.limit]

    by_set: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        by_set[c["set_id"]].append(c)

    print(f"\n3. Fetching TCGCSV products + prices per set (cat {cat_id})...")
    matches: list[dict] = []
    for sid, set_cards in sorted(by_set.items()):
        gid = set_to_group.get(sid)
        if not gid:
            continue
        all_products = _load_products(gid, cat_id)
        all_prices: dict[int, list[dict]] = defaultdict(list)
        for pr in _load_prices(gid, cat_id):
            all_prices[pr["productId"]].append(pr)
        ms = match_set_with_prices(set_cards, all_products, all_prices)
        print(f"   [{args.lang}/{sid}] {len(set_cards)} cards, "
              f"{len(all_products)} products → {len(ms)} priced")
        matches.extend(ms)

    if not matches:
        print("\nNo prices resolved.")
        return

    sql_lines = build_update_sql(matches, args.lang)
    sql_file = OUT_DIR / f"tcgcsv_prices_{args.lang}.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / f"tcgcsv_prices_{args.lang}_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n4. {len(matches)} priced. SQL → {sql_file}")

    if args.dry_run:
        print("--dry-run: skipping D1. Sample:")
        for m in matches[:10]:
            print(f"   {m['card_id']}: ${m['price_usd']:.2f} ({m['subtype']})")
        return

    print(f"\n5. Executing {len(sql_lines)} UPDATEs against D1...")
    r = subprocess.run(WRANGLER + ["--remote", f"--file={sql_file}"])
    if r.returncode != 0:
        sys.exit(r.returncode)
    print("Done.")


def match_set_with_prices(
    set_cards: list[dict], products: list[dict], prices_by_pid: dict
) -> list[dict]:
    """Match each card to a product, then pick the best subType price."""
    from scripts.backfill_ptcg_images_tcgcsv import match_set
    from scripts.backfill_ptcg_images_bulbagarden import (
        _load_jp_en_map, _load_card_id_to_en, _to_en_name,
    )

    # For JA cards, the DB stores Japanese names but TCGCSV products
    # have English names. Translate JA → EN before matching so name
    # comparison works. Uses the dexId-derived map plus the canonical
    # JP-name dict.
    jp_en = _load_jp_en_map()
    card_id_to_en = _load_card_id_to_en()
    translated_cards = []
    for c in set_cards:
        c2 = dict(c)
        if c.get("lang") == "ja":
            enriched = card_id_to_en.get(c["card_id"])
            if enriched:
                c2["name"] = enriched
            else:
                c2["name"] = _to_en_name(c["name"], jp_en)
        translated_cards.append(c2)

    pseudo_matches = match_set(translated_cards, products)
    # Build a lookup back to the card row (we lost rarity in pseudo matches)
    by_id = {c["card_id"]: c for c in set_cards}

    out = []
    for m in pseudo_matches:
        cid = m["card_id"]
        product_id = int(m["filename"].split("-")[1])  # 'tcgplayer-12345'
        prices = prices_by_pid.get(product_id, [])
        if not prices:
            continue
        # Pick a subType. Prefer matching rarity to subType name.
        card = by_id.get(cid, {})
        rarity = (card.get("rarity") or "").lower()
        chosen = None
        # If the card is explicitly a holo/rare, prefer Holofoil
        if "holo" in rarity or "rare" in rarity or "ultra" in rarity:
            for p in prices:
                if "holofoil" in (p.get("subTypeName") or "").lower() and "reverse" not in (p.get("subTypeName") or "").lower():
                    chosen = p
                    break
        # Otherwise use SUBTYPE_PRIORITY order
        if not chosen:
            for sub in SUBTYPE_PRIORITY:
                for p in prices:
                    if (p.get("subTypeName") or "").lower() == sub.lower():
                        chosen = p
                        break
                if chosen:
                    break
        if not chosen:
            chosen = prices[0]
        market = chosen.get("marketPrice")
        if market is None or market <= 0:
            # Fall back to midPrice if marketPrice is missing
            market = chosen.get("midPrice") or chosen.get("lowPrice")
        if market is None or market <= 0:
            continue
        out.append({
            "card_id": cid,
            "price_usd": round(float(market), 2),
            "low":  chosen.get("lowPrice"),
            "mid":  chosen.get("midPrice"),
            "high": chosen.get("highPrice"),
            "direct_low": chosen.get("directLowPrice"),
            "subtype": chosen.get("subTypeName"),
            "tcgplayer_product_id": product_id,
        })
    return out


def _load_products(group_id: int, cat_id: int = CAT_EN) -> list[dict]:
    cache = CACHE_DIR / f"cat{cat_id}_group_{group_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    d = _http_json(PRODUCTS_URL.format(cat_id=cat_id, group_id=group_id))
    prods = d.get("results") or d.get("products") or []
    cache.write_text(json.dumps(prods, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.2)
    return prods


def _load_prices(group_id: int, cat_id: int = CAT_EN) -> list[dict]:
    cache = CACHE_DIR / f"cat{cat_id}_prices_{group_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    d = _http_json(PRICES_URL.format(cat_id=cat_id, group_id=group_id))
    prices = d.get("results") or d.get("prices") or []
    cache.write_text(json.dumps(prices, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.2)
    return prices


_TCGDEX_SET_NAME_CACHE: dict[str, str] = {}
_TCGCSV_GROUPS_CACHE: dict[int, list[dict]] = {}


def _tcgdex_set_name(set_id: str, lang: str = "en") -> str:
    """Get a set's English name from TCGdex (used to match against TCGCSV)."""
    if set_id in _TCGDEX_SET_NAME_CACHE:
        return _TCGDEX_SET_NAME_CACHE[set_id]
    try:
        d = _http_json(f"https://api.tcgdex.net/v2/{lang}/sets/{urllib.parse.quote(set_id)}")
        name = d.get("name", "")
    except Exception:
        name = ""
    _TCGDEX_SET_NAME_CACHE[set_id] = name
    return name


def _tcgcsv_groups(cat_id: int = CAT_EN) -> list[dict]:
    if cat_id not in _TCGCSV_GROUPS_CACHE:
        d = _http_json(GROUPS_URL.format(cat_id=cat_id))
        _TCGCSV_GROUPS_CACHE[cat_id] = d.get("results", []) or []
    return _TCGCSV_GROUPS_CACHE[cat_id]


def _discover_group(set_id: str, cat_id: int = CAT_EN) -> int | None:
    """Find a TCGCSV groupId. Match strategy:
      0. Hand-curated JA override (cat 85 only).
      1. Direct abbreviation match (case-insensitive, hyphen-tolerant).
      2. TCGdex set name → TCGCSV group name fuzzy match."""
    if cat_id == CAT_JA and set_id in JA_OVERRIDES:
        return JA_OVERRIDES[set_id]
    sid_norm = set_id.lower()
    sid_norm_nohyphen = sid_norm.replace("-", "")
    for g in _tcgcsv_groups(cat_id):
        abbr = (g.get("abbreviation") or "").lower()
        # Match either case-insensitive equality, or with hyphens stripped
        # (so 'SVP' matches 'SV-P'). Skip empty abbreviations.
        if abbr and (abbr == sid_norm or abbr.replace("-", "") == sid_norm_nohyphen):
            return g["groupId"]
    # Fallback: TCGdex name lookup. JA sets only resolve via lang=ja.
    lang = "ja" if cat_id == CAT_JA else "en"
    name = _tcgdex_set_name(set_id, lang)
    if not name:
        return None
    name_norm = re.sub(r"[^a-z0-9]+", "", name.lower())
    for g in _tcgcsv_groups(cat_id):
        gn_raw = g.get("name") or ""
        gn = re.sub(r"[^a-z0-9]+", "", gn_raw.lower())
        if not gn:
            continue
        if name_norm == gn or (len(name_norm) > 8 and name_norm in gn) or (len(gn) > 8 and gn in name_norm):
            return g["groupId"]
    return None


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


def build_update_sql(matches: list[dict], lang: str) -> list[str]:
    """Write pricing_json in pokemontcg.io's tcgplayer-shaped format so
    the frontend reads it the same way as cards already priced via
    pokemontcg. Preserves any existing cardmarket data via json_set."""
    lines = []
    for m in matches:
        cid = m["card_id"].replace("'", "''")
        # Map subType to the camelCase key pokemontcg.io uses
        sub_key = _subtype_key(m.get("subtype"))
        # JSON object literal for the variant
        variant_json = json.dumps({
            "low":      m.get("low"),
            "mid":      m.get("mid"),
            "high":     m.get("high"),
            "market":   m["price_usd"],
            "directLow": m.get("direct_low"),
        })
        # Escape single-quotes for SQL literal
        variant_sql = variant_json.replace("'", "''")
        lines.append(
            f"UPDATE ptcg_cards SET "
            f"price_source = 'tcgplayer', "
            f"pricing_json = json_set(COALESCE(pricing_json, '{{}}'), "
            f"'$.tcgplayer.{sub_key}', json('{variant_sql}')) "
            f"WHERE card_id = '{cid}' AND lang = '{lang}' "
            f"AND (price_source IS NULL OR price_source = 'cardmarket');"
        )
    return lines


def _subtype_key(name: str | None) -> str:
    """Map TCGCSV subTypeName → pokemontcg.io tcgplayer key."""
    if not name:
        return "normal"
    n = name.lower()
    if "reverse" in n:
        return "reverseHolofoil"
    if "holo" in n:
        return "holofoil"
    if "1st edition" in n:
        return "1stEdition"
    if "unlimited" in n:
        return "unlimited"
    return "normal"


if __name__ == "__main__":
    main()
