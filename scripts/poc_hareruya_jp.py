"""
Hareruya JP pricing PoC v2 — Phase 5.0 of SCRAPLING_PIPELINE_PLAN.md.

V1 lesson: Shopify's public /products.json silently ignores ?since_id and
caps ?page=N at page=100, so we can never see more than 25,000 unique
products through the global endpoint.

V2 strategy: walk series-level collections instead. Hareruya2 publishes
17 collection handles (sv, sm, ss, bw, xy, mega, dp, dpt, pcg, pmcg, e,
adv, legend, neo, vs, generations, sunmoon, svm) totaling ~31k Pokemon
TCG products. Each collection's /collections/{handle}/products.json is
independently paginated via ?page=N — way under the 100-page cap per
collection.

Title format from V1 still holds: {Name}{(suffix)?}{type?}〈local_id〉[set_id]
so matching is direct on (set_id, local_id) without fuzzy name matching.

Output:
    data/poc_hareruya/products_raw.jsonl
    data/poc_hareruya/matched.json
    data/poc_hareruya/unmatched.json

Usage:
    python scripts/poc_hareruya_jp.py --sample=50
    python scripts/poc_hareruya_jp.py --sample=200
    python scripts/poc_hareruya_jp.py --collections-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)  # so wrangler subprocess can find ./node_modules

BASE = "https://www.hareruya2.com"
HEADERS = {
    "User-Agent": "OPBindr-pricing-research/0.2 (https://opbindr.com; arjun@neuroplexlabs.com)",
    "Accept": "application/json",
}
OUT_DIR = Path("data/poc_hareruya")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Series-level collections covering every Pokemon TCG era. Verified
# 2026-05-04 from /collections.json — all are pure-letter handles with
# >100 products and recognizable era names. Adding a new era? Drop it
# in here and the walk picks it up next run.
COLLECTIONS = [
    "pmcg",   # PMCG (Old Base/Jungle/Fossil/Team Rocket)
    "neo",    # Neo
    "vs",     # VS series
    "e",      # eCard era
    "adv",    # ADV (EX series)
    "pcg",    # PCG (Diamond/Pearl)
    "dp",     # Diamond & Pearl
    "dpt",    # DPt (HGSS/Platinum)
    "legend", # LEGEND
    "bw",     # Black & White
    "xy",     # XY
    "generations",
    "sm",     # Sun & Moon
    "sunmoon",
    "ss",     # Sword & Shield
    "sv",     # Scarlet & Violet
    "svm",    # SV start deck Generations
    "mega",   # MEGA series
]

DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]

TITLE_RE = re.compile(
    r"〈\s*(?P<lid>[A-Za-z0-9\-/]+?)\s*(?:/\s*[\dA-Za-z\-]+)?\s*〉\s*\[\s*(?P<setid>[^\]]+?)\s*\]"
)


def query_d1(query: str) -> list[dict]:
    try:
        out = subprocess.run(
            WRANGLER_BIN + ["--remote", "--json", "--command", query],
            capture_output=True, text=True, encoding="utf-8", check=True,
            cwd=str(REPO_ROOT),
        )
        data = json.loads(out.stdout)
        if not data or not data[0].get("success"):
            print(f"D1 query reported failure: {out.stdout[:200]}", file=sys.stderr)
            return []
        return data[0]["results"] or []
    except subprocess.CalledProcessError as e:
        print(f"D1 query subprocess failed: {e.stderr[:300]}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"D1 JSON parse failed: {e}", file=sys.stderr)
        return []


def fetch_jpy_to_usd() -> float:
    try:
        url = "https://api.frankfurter.app/latest?from=JPY&to=USD"
        req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=10) as r:
            return float(json.load(r)["rates"]["USD"])
    except Exception as e:
        print(f"FX fetch failed ({e}), using fallback 0.0067", file=sys.stderr)
        return 0.0067


def walk_collection(handle: str) -> list[dict]:
    """Walk one collection's products.json. Each collection is independently
    paginated; the global 100-page cap doesn't apply per-collection."""
    products = []
    page = 1
    seen_ids = set()
    while True:
        url = f"{BASE}/collections/{handle}/products.json?limit=250&page={page}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                break
            print(f"  [{handle}] transient {e}, retrying", file=sys.stderr)
            time.sleep(5)
            continue
        except Exception as e:
            print(f"  [{handle}] network {e}, retrying", file=sys.stderr)
            time.sleep(5)
            continue
        page_products = data.get("products", [])
        if not page_products:
            break
        # Detect if Shopify is returning the same first page (no cursor advance).
        new = [p for p in page_products if p["id"] not in seen_ids]
        if not new:
            break
        for p in new:
            seen_ids.add(p["id"])
        products.extend(new)
        page += 1
        time.sleep(0.7)  # ~1.4 req/sec — polite
        if page > 100:
            print(f"  [{handle}] page-cap, stopping", file=sys.stderr)
            break
    return products


def parse_title(title: str) -> tuple[str, str] | None:
    m = TITLE_RE.search(title or "")
    if not m: return None
    return m.group("setid").strip(), m.group("lid").strip()


def normalize_local_id(lid: str) -> list[str]:
    out = [lid]
    if lid.lstrip("0") and lid.lstrip("0") != lid:
        out.append(lid.lstrip("0"))
    return out


# Hareruya's set codes diverge from TCGdex JA set IDs in known ways:
#   1. Suffix-letter case: Hareruya `SM12a` ↔ TCGdex `SM12A` (D1 uppercases)
#   2. Hareruya drops the SWSH/SM/SV prefix on shorthand: `S12a` ↔ `SWSH12A`,
#      `M2a` ↔ `MEGA02A` (some — not always)
#   3. Promo sets: Hareruya `S-P` ↔ TCGdex `SWSHP`, `SM-P` ↔ `SMP`, etc.
#   4. Some Hareruya codes (`MC`, `SI`, special tournament/event sets)
#      have no TCGdex equivalent — those simply won't match, accepted loss.
HARERUYA_PROMO_MAP = {
    "S-P":   "SWSHP",
    "SM-P":  "SMP",
    "XY-P":  "XYP",
    "BW-P":  "BWP",
    "DP-P":  "DPP",
    "SV-P":  "SVP",
    "P-P":   "PP",
}


def candidate_setids(hareruya_setid: str) -> list[str]:
    """Generate possible TCGdex set IDs from a Hareruya set code. Case
    variants + promo remap + S→SWSH expansion."""
    s = hareruya_setid.strip()
    cands = {s, s.upper(), s.lower()}
    if s in HARERUYA_PROMO_MAP:
        cands.add(HARERUYA_PROMO_MAP[s])
    # S12a → SWSH12A (S-prefix → SWSH-prefix expansion). Hareruya uses
    # the abbreviated form for SWSH-era; TCGdex uses full prefix.
    if re.match(r"^S\d", s):
        cands.add("SWSH" + s[1:].upper())
        cands.add("SWSH" + s[1:])
    # M2a → MEGA02A or just M02A. Mega series naming varies.
    if re.match(r"^M\d", s):
        cands.add("MEGA" + s[1:].upper())
        cands.add(s.upper())  # M2a → M2A
    return list(cands)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=50)
    ap.add_argument("--collections-only", action="store_true")
    ap.add_argument("--use-cached", action="store_true",
                    help="Skip the Hareruya walk and reuse data/poc_hareruya/products_raw.jsonl")
    args = ap.parse_args()

    t0 = time.time()
    raw_path = OUT_DIR / "products_raw.jsonl"

    if args.use_cached and raw_path.exists():
        print(f"1. Reusing cached products from {raw_path}...")
        all_products = []
        with raw_path.open(encoding="utf-8") as f:
            for line in f:
                all_products.append(json.loads(line))
        print(f"   total products: {len(all_products)}")
    else:
        print(f"1. Walking {len(COLLECTIONS)} series collections...")
        all_products = []
        seen = set()
        for handle in COLLECTIONS:
            products = walk_collection(handle)
            new = [p for p in products if p["id"] not in seen]
            for p in new:
                seen.add(p["id"])
            all_products.extend(new)
            print(f"   [{handle:15s}] fetched={len(products):>5d}  unique-new={len(new):>5d}  cumulative={len(all_products):>6d}", flush=True)
        print(f"\n   total unique products: {len(all_products)}")
        print(f"   walltime: {time.time()-t0:.1f}s")
        with raw_path.open("w", encoding="utf-8") as f:
            for p in all_products:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        print(f"   raw: {raw_path}")

    print("\n2. Parsing (set_id, local_id) → price (with set-id remapping)...")
    by_card: dict[tuple[str, str], list[float]] = defaultdict(list)
    parse_skips = 0
    for p in all_products:
        parsed = parse_title(p.get("title", ""))
        if not parsed:
            parse_skips += 1
            continue
        setid, lid = parsed
        prices = []
        for v in p.get("variants", []):
            if v.get("available"):
                try: prices.append(float(v["price"]))
                except (KeyError, TypeError, ValueError): pass
        if not prices: continue
        # Index under every plausible TCGdex equivalent of Hareruya's set
        # code. by_card lookups stay simple; the cost is duplicate keys
        # for unambiguous codes (no harm).
        for sid in candidate_setids(setid):
            for lid_norm in normalize_local_id(lid):
                by_card[(sid, lid_norm)].append(min(prices))

    print(f"   parsed: {len(all_products) - parse_skips} of {len(all_products)}")
    print(f"   skipped (non-card titles): {parse_skips}")
    print(f"   unique (set_id, local_id) priced pairs: {len(by_card)}")

    if args.collections_only:
        print("\n--collections-only: stopping before D1 match")
        return

    print(f"\n3. Pulling {args.sample} unpriced JA cards from D1...")
    cards = query_d1(
        "SELECT card_id, name, set_id, local_id FROM ptcg_cards "
        "WHERE lang='ja' AND price_source IS NULL AND name IS NOT NULL "
        f"ORDER BY RANDOM() LIMIT {args.sample}"
    )
    if not cards:
        print("   no cards from D1 — aborting")
        return
    print(f"   {len(cards)} test cards")

    print("\n4. FX + match...")
    fx = fetch_jpy_to_usd()
    print(f"   FX: 1 JPY = {fx:.6f} USD")

    matched, unmatched = [], []
    for c in cards:
        sid = c["set_id"]
        for lid in normalize_local_id(c["local_id"]):
            jpy_list = by_card.get((sid, lid))
            if jpy_list:
                jpy = statistics.median(jpy_list)
                matched.append({
                    "card_id": c["card_id"], "name": c["name"], "set_id": sid,
                    "local_id": c["local_id"], "jpy": jpy,
                    "usd": round(jpy * fx, 2), "n_listings": len(jpy_list),
                })
                break
        else:
            unmatched.append({
                "card_id": c["card_id"], "name": c["name"],
                "set_id": sid, "local_id": c["local_id"],
            })

    hit_rate = len(matched) / len(cards) if cards else 0

    # Coverage projection across the FULL D1 unpriced JA set
    print("\n5. Projecting full-DB coverage...")
    all_unpriced = query_d1(
        "SELECT card_id, set_id, local_id FROM ptcg_cards "
        "WHERE lang='ja' AND price_source IS NULL AND name IS NOT NULL"
    )
    full_match = 0
    if all_unpriced:
        for c in all_unpriced:
            for lid in normalize_local_id(c["local_id"]):
                if (c["set_id"], lid) in by_card:
                    full_match += 1
                    break
        print(f"   full-DB unpriced JA: {len(all_unpriced)}")
        print(f"   would-match in Hareruya: {full_match} ({100*full_match/len(all_unpriced):.1f}%)")

    print(f"\n========== HARERUYA POC v2 RESULTS ==========")
    print(f"Cards tested:      {len(cards)}")
    print(f"Sample hit rate:   {len(matched)} / {len(cards)} = {hit_rate*100:.1f}%")
    print(f"Walltime:          {time.time()-t0:.1f}s")
    print()
    print(f"Top sample matches by USD:")
    for m in sorted(matched, key=lambda x: -x["usd"])[:10]:
        print(f"  {m['card_id']:14s} {m['name'][:24]:24s}  ¥{m['jpy']:>9,.0f} = ${m['usd']:>7.2f}")
    print()
    print(f"Sample unmatched (first 10):")
    for u in unmatched[:10]:
        print(f"  {u['card_id']:14s} set={u['set_id']:8s} lid={u['local_id']:6s} {u['name']}")

    matched_path = OUT_DIR / "matched.json"
    matched_path.write_text(json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8")
    unmatched_path = OUT_DIR / "unmatched.json"
    unmatched_path.write_text(json.dumps(unmatched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {matched_path} ({len(matched)})")
    print(f"Wrote {unmatched_path} ({len(unmatched)})")

    print()
    print("===== DECISION =====")
    if hit_rate >= 0.5:
        proj = full_match if all_unpriced else int(len(by_card) * 0.7)
        print(f"GO: sample {hit_rate*100:.1f}% ≥ 50%.")
        print(f"   Projected full-DB matches: ~{proj} cards priced")
        print(f"   Path forward: collection-walk + nightly cron, no Scrapling needed")
    elif hit_rate >= 0.3:
        print(f"PARTIAL: {hit_rate*100:.1f}% in [30%, 50%).")
        print(f"   Ship as supplementary tier")
    else:
        print(f"NO-GO: {hit_rate*100:.1f}% < 30%")


if __name__ == "__main__":
    main()
