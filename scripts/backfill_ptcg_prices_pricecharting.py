"""
PriceCharting JP price backfill — covers vintage and obscure JP sets
that TCGCSV/Yuyutei don't have (PCG/Holon era, neo, ADV, e-Card, BS).

PriceCharting (pricecharting.com) aggregates Pokemon JP card prices
from eBay sold listings and other US-marketplace sources. Coverage
includes 1996 Base Set through current modern sets.

Per-set flow:
  GET /console/pokemon-japanese-{slug}    # set listing page (HTML)
    -> parse <table> rows for: card name + number + ungraded NM price
  -> match to our cards by (set_id, local_id, name)
  -> write to pricing_json.pricecharting.market and price

Stamps price_source='pricecharting' for auditability.

Usage:
    python -m scripts.backfill_ptcg_prices_pricecharting --dry-run
    python -m scripts.backfill_ptcg_prices_pricecharting
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
from html.parser import HTMLParser
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://www.pricecharting.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "identity",
}
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = Path("data/pricecharting_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Our JA set_id → PriceCharting slug. Verified against the PriceCharting
# Japanese sets index. Sets without a PC equivalent are simply absent.
SET_TO_PC_SLUG = {
    # Vintage classics (PMCG era, 1996-2003)
    "PMCG1":  "pokemon-japanese-expansion-pack",
    "PMCG2":  "pokemon-japanese-jungle",
    "PMCG3":  "pokemon-japanese-mystery-of-the-fossils",
    "PMCG4":  "pokemon-japanese-rocket-gang",
    "PMCG5":  "pokemon-japanese-yamabuki-city-gym",       # Leaders' Stadium = Saffron City Gym
    "PMCG6":  "pokemon-japanese-challenge-from-the-darkness",
    # Vending / Web era
    "web1":   "pokemon-japanese-vending",
    "WEB1":   "pokemon-japanese-vending",
    "web2":   "pokemon-japanese-vending",
    "web3":   "pokemon-japanese-vending",
    # 1996-1997 Carddass / TopSun (extremely vintage)
    "BS":     "pokemon-japanese-1996-carddass",
    "TOPSUN": "pokemon-japanese-topsun",
    # Neo era
    "neo1":   "pokemon-japanese-gold-silver-new-world",
    "neo2":   "pokemon-japanese-crossing-the-ruins",
    "neo3":   "pokemon-japanese-awakening-legends",       # Awakening Legends in some sources
    "neo4":   "pokemon-japanese-darkness-and-to-light",
    "NEO1":   "pokemon-japanese-gold-silver-new-world",
    "NEO2":   "pokemon-japanese-crossing-the-ruins",
    "NEO3":   "pokemon-japanese-awakening-legends",
    "NEO4":   "pokemon-japanese-darkness-and-to-light",
    # E-Card series
    "E1":     "pokemon-japanese-expedition-expansion-pack",
    "E4":     "pokemon-japanese-mysterious-mountains",
    "E5":     "pokemon-japanese-awakening-legends",
    # ADV era
    "ADV2":   "pokemon-japanese-mirage-forest",
    # PCG / Holon era — TCGdex JP names verified
    # PCG1 伝説の飛翔 = "Legendary Flight" — closest PC: awakening-legends
    "PCG1":   "pokemon-japanese-awakening-legends",
    # PCG2 蒼空の激突 = "Sky Conflict" — closest PC: clash-of-the-blue-sky
    "PCG2":   "pokemon-japanese-clash-of-the-blue-sky",
    "PCG3":   "pokemon-japanese-rocket-gang-strikes-back",
    # PCG4 金の空、銀の海 = "Gold Sky Silver Sea" — pokemon-japanese-golden-sky-silvery-ocean
    "PCG4":   "pokemon-japanese-golden-sky-silvery-ocean",
    "PCG5":   "pokemon-japanese-mirage-forest",  # まぼろしの森
    "PCG6":   "pokemon-japanese-holon-research",
    "PCG7":   "pokemon-japanese-holon-phantom",
    "PCG8":   "pokemon-japanese-miracle-crystal",
    "PCG9":   "pokemon-japanese-offense-and-defense-of-the-furthest-ends",
    "PCG10":  "pokemon-japanese-world-championships-2023",
    # DP era
    "DP1":    "pokemon-japanese-space-time-creation",
    "DP2":    "pokemon-japanese-secret-of-the-lakes",
    "DP3":    "pokemon-japanese-shining-darkness",
    "DP4":    "pokemon-japanese-shockwaves",  # may not exist
    "DP5":    "pokemon-japanese-temple-of-anger",
    "DP6":    "pokemon-japanese-intense-fight-in-the-destroyed-sky",
    # E-Card era
    "E2":     "pokemon-japanese-aquapolis",
    "E3":     "pokemon-japanese-skyridge",
    # XY base
    "XY":     "pokemon-japanese-collection-x",
    # SM split pairs
    "SM6a":   "pokemon-japanese-dragon-storm",
    "SM6b":   "pokemon-japanese-champion-road",
    "SM7a":   "pokemon-japanese-thunderclap-spark",
    "SM7b":   "pokemon-japanese-fairy-rise",
    "SM8a":   "pokemon-japanese-dark-order",
    # VS series
    "VS1":    "pokemon-japanese-vs",
    "VS2":    "pokemon-japanese-vs",
    # Legend (LV.X era)
    # Modern SM era — most match by abbreviation but PC fills gaps
    "SM10A":  "pokemon-japanese-gg-end",
    "SM10B":  "pokemon-japanese-sky-legend",
    "SM11A":  "pokemon-japanese-remix-bout",
    "SM11B":  "pokemon-japanese-dream-league",
    "SM12A":  "pokemon-japanese-tag-all-stars",
    "SM8B":   "pokemon-japanese-gx-ultra-shiny",
    "SM10":   "pokemon-japanese-double-blaze",
    "SM11":   "pokemon-japanese-miracle-twins",
    "SM12":   "pokemon-japanese-alter-genesis",
    "SM9":    "pokemon-japanese-tag-bolt",
    "SM3P":   "pokemon-japanese-shining-legends",
    # Modern SWSH (S* prefix in PC) — split pairs
    "SWSH5I":  "pokemon-japanese-single-strike-master",
    "SWSH5R":  "pokemon-japanese-rapid-strike-master",
    "SWSH6H":  "pokemon-japanese-silver-lance",
    "SWSH6K":  "pokemon-japanese-jet-black-spirit",
    "SWSH7D":  "pokemon-japanese-skyscraping-perfection",
    "SWSH7R":  "pokemon-japanese-blue-sky-stream",
    "SWSH4A":  "pokemon-japanese-shiny-star-v",
    "SWSH8":   "pokemon-japanese-fusion-arts",
    "SWSH8A":  "pokemon-japanese-25th-anniversary-collection",
    "SWSH8B":  "pokemon-japanese-vmax-climax",
    "SWSH9":   "pokemon-japanese-star-birth",
    "SWSH9A":  "pokemon-japanese-battle-region",
    "SWSH10":  "pokemon-japanese-time-gazer",
    "SWSH10A": "pokemon-japanese-dark-phantasma",
    "SWSH10B": "pokemon-japanese-go",
    "SWSH11":  "pokemon-japanese-lost-abyss",
    "SWSH11A": "pokemon-japanese-incandescent-arcana",
    "SWSH12":  "pokemon-japanese-paradigm-trigger",
    "SWSH12A": "pokemon-japanese-vstar-universe",
    # Modern SV (Scarlet & Violet)
    "SV1S":   "pokemon-japanese-scarlet-ex",
    "SV1V":   "pokemon-japanese-violet-ex",
    "SV1a":   "pokemon-japanese-triplet-beat",
    "SV2P":   "pokemon-japanese-snow-hazard",
    "SV2D":   "pokemon-japanese-clay-burst",
    "SV2a":   "pokemon-japanese-pokemon-card-151",  # might be missing
    "SV2A":   "pokemon-japanese-pokemon-card-151",
    "SV3":    "pokemon-japanese-ruler-of-the-black-flame",
    "SV3a":   "pokemon-japanese-raging-surf",
    "SV4K":   "pokemon-japanese-ancient-roar",
    "SV4M":   "pokemon-japanese-future-flash",
    "SV4a":   "pokemon-japanese-shiny-treasure-ex",
    "SV4A":   "pokemon-japanese-shiny-treasure-ex",
    "SV5K":   "pokemon-japanese-wild-force",
    "SV5M":   "pokemon-japanese-cyber-judge",
    "SV5a":   "pokemon-japanese-crimson-haze",
    "SV5A":   "pokemon-japanese-crimson-haze",
    "SV6":    "pokemon-japanese-mask-of-change",
    "SV6a":   "pokemon-japanese-night-wanderer",
    "SV6A":   "pokemon-japanese-night-wanderer",
    "SV7":    "pokemon-japanese-stellar-miracle",
    "SV7a":   "pokemon-japanese-paradise-dragona",
    "SV7A":   "pokemon-japanese-paradise-dragona",
    "SV8":    "pokemon-japanese-super-electric-breaker",
    "SV8a":   "pokemon-japanese-terastal-festival",
    "SV8A":   "pokemon-japanese-terastal-festival",
    "SV9":    "pokemon-japanese-battle-partners",
    "SV9a":   "pokemon-japanese-heat-wave-arena",
    "SV9A":   "pokemon-japanese-heat-wave-arena",
    "SV10":   "pokemon-japanese-glory-of-team-rocket",
    "SV11B":  "pokemon-japanese-black-bolt",
    "SV11W":  "pokemon-japanese-white-flare",
    # Mega era
    "M2":     "pokemon-japanese-inferno-x",
    "M2a":    "pokemon-japanese-mega-dream-ex",
    "M2A":    "pokemon-japanese-mega-dream-ex",
    "M3":     "pokemon-japanese-nihil-zero",
    "M4":     "pokemon-japanese-ninja-spinner",
    # Promo aggregates
    "XYP":    "pokemon-japanese-promo",
    "SMP":    "pokemon-japanese-promo",
    "BWP":    "pokemon-japanese-promo",
    "SVP":    "pokemon-japanese-promo",
    "SWSHP":  "pokemon-japanese-promo",
    "MP":     "pokemon-japanese-promo",
    # XY era main sets (some PC slugs)
    "XY1a":   "pokemon-japanese-collection-x",
    "XY1b":   "pokemon-japanese-collection-y",
    "XY3":    "pokemon-japanese-rising-fist",
    "XY4":    "pokemon-japanese-phantom-gate",
    "XY5a":   "pokemon-japanese-emerald-break",
    "XY5b":   "pokemon-japanese-double-crisis",
    "XY6":    "pokemon-japanese-bandit-ring",
    "XY7":    "pokemon-japanese-clash-of-the-blue-sky",
    "XY8a":   "pokemon-japanese-yamabuki-city-gym",  # might be wrong
    "XY9":    "pokemon-japanese-rage-of-the-broken-heavens",
    "CP1":    "pokemon-japanese-double-crisis",
    "CP4":    "pokemon-japanese-pokekyun-collection",
    "CP5":    "pokemon-japanese-mythical-legendary-dream-shine",
    "CP6":    "pokemon-japanese-20th-anniversary",
    # BW era
    "BW1":    "pokemon-japanese-black-collection",
    "BW2":    "pokemon-japanese-white-collection",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("1. Querying D1 for unpriced JA cards in PC-mapped sets...")
    sids = list(SET_TO_PC_SLUG.keys()) if not args.set_id else [args.set_id]
    in_clause = ",".join(f"'{sid}'" for sid in sids)
    sql = (f"SELECT card_id, name, set_id, local_id FROM ptcg_cards "
           f"WHERE lang='ja' AND set_id IN ({in_clause}) "
           f"AND (price_source IS NULL OR price_source='cardmarket') "
           f"ORDER BY set_id, local_id")
    cards = query_d1(sql)
    print(f"   {len(cards)} cards in scope")

    by_set: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        by_set[c["set_id"]].append(c)

    matches: list[dict] = []
    for sid, set_cards in sorted(by_set.items()):
        slug = SET_TO_PC_SLUG.get(sid)
        if not slug:
            continue
        try:
            pc_cards = _fetch_set_cards(slug)
        except Exception as e:
            print(f"   [{sid}] FAIL: {e}")
            continue
        ms = match_set(set_cards, pc_cards, sid)
        print(f"   [{sid} → {slug}] {len(set_cards)} cards, {len(pc_cards)} PC entries → {len(ms)} priced")
        matches.extend(ms)
        time.sleep(1.0)  # polite to PriceCharting

    if not matches:
        print("\nNo matches.")
        return

    sql_lines = build_update_sql(matches)
    sql_file = OUT_DIR / "pricecharting_prices_ja.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / "pricecharting_prices_ja_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(matches)} priced. SQL → {sql_file}")

    if args.dry_run:
        print("--dry-run: skipping D1. Sample:")
        for m in matches[:10]:
            print(f"   {m['card_id']}: ${m['price_usd']:.2f} ({m.get('pc_name','')})")
        return

    print(f"\nExecuting {len(sql_lines)} UPDATEs against D1...")
    r = subprocess.run(WRANGLER + ["--remote", f"--file={sql_file}"])
    if r.returncode != 0:
        sys.exit(r.returncode)
    print("Done.")


def _fetch_set_cards(slug: str) -> list[dict]:
    """Fetch a PC set page, parse card rows, return [{name, number, price}]."""
    cache = CACHE_DIR / f"{slug}.html"
    if cache.exists():
        html = cache.read_text(encoding="utf-8")
    else:
        url = f"{BASE}/console/{slug}"
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
        cache.write_text(html, encoding="utf-8")

    # Parse the cards table. PC structure: each row is a card with
    # link containing name, plus a price cell.
    cards = []
    # Each row has class "card" or similar. The parsing approach: find
    # <a href="/game/{slug}/...">{Name} #N</a>, then near it find the
    # ungraded NM price (first $X.XX after the name).
    # The actual structure on PC is a table where each row has:
    #   <td class="title"><a href="/game/...">CardName #N</a></td>
    #   <td class="price">$X.XX</td>  (NM ungraded)
    row_pat = re.compile(
        r'<tr[^>]*id="product-(\d+)"[^>]*>'
        r'.*?<a[^>]+href="(/game/[^"]+)"[^>]*>([^<]+)</a>'
        r'.*?<td[^>]*class="price numeric used_price"[^>]*>'
        r'.*?<span[^>]*class="js-price"[^>]*>([^<]*?)</span>',
        re.DOTALL,
    )
    for m in row_pat.finditer(html):
        product_id, href, title, price_html = m.group(1), m.group(2), m.group(3), m.group(4)
        title = title.strip()
        # Title is like "Mew ex #41" or "Pikachu &delta;1"
        num_match = re.search(r"#(\w+)", title)
        number = num_match.group(1) if num_match else ""
        name = re.sub(r"\s*#\w+\s*$", "", title).strip()
        # Decode HTML entities loosely
        name = name.replace("&amp;", "&").replace("&apos;", "'")
        # Extract price
        price_match = re.search(r"\$([\d,]+\.\d{2})", price_html)
        if not price_match:
            continue
        price = float(price_match.group(1).replace(",", ""))
        if price <= 0:
            continue
        cards.append({
            "name": name,
            "number": number,
            "price": price,
            "url": href,
            "product_id": product_id,
        })
    return cards


def match_set(set_cards: list[dict], pc_cards: list[dict], set_id: str) -> list[dict]:
    """Match each card to a PC entry by (number, name)."""
    # Index PC cards by normalized number
    by_num: dict[str, list[dict]] = defaultdict(list)
    for p in pc_cards:
        if p["number"]:
            by_num[_norm_num(p["number"])].append(p)

    out = []
    for c in set_cards:
        target = _norm_num(c["local_id"])
        candidates = by_num.get(target, [])
        if not candidates:
            # Try with leading-zero / alpha stripping variations
            for variant in [
                c["local_id"].lstrip("0").lower(),
                re.sub(r"^[A-Za-z]+", "", c["local_id"]).lstrip("0"),
                re.sub(r"[A-Za-z]+$", "", c["local_id"]).lstrip("0"),
            ]:
                if variant and variant in by_num:
                    candidates = by_num[variant]
                    break
        if not candidates:
            continue
        # Prefer first candidate (PC sorts by relevance)
        best = candidates[0]
        out.append({
            "card_id": c["card_id"],
            "price_usd": round(best["price"], 2),
            "pc_name": best["name"],
            "pc_url": best["url"],
            "pc_product_id": best["product_id"],
        })
    return out


def _norm_num(s: str) -> str:
    return s.lstrip("0").lower() or "0"


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
        url = (m.get("pc_url") or "").replace("'", "''")
        block = json.dumps({
            "market": m["price_usd"],
            "url": f"https://www.pricecharting.com{url}" if url else None,
            "productId": m.get("pc_product_id"),
        })
        block_sql = block.replace("'", "''")
        lines.append(
            f"UPDATE ptcg_cards SET "
            f"price_source = 'pricecharting', "
            f"pricing_json = json_set(COALESCE(pricing_json, '{{}}'), "
            f"'$.pricecharting', json('{block_sql}')) "
            f"WHERE card_id = '{cid}' AND lang = 'ja' "
            f"AND (price_source IS NULL OR price_source = 'cardmarket');"
        )
    return lines


if __name__ == "__main__":
    main()
