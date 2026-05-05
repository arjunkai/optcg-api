"""
Hareruya JA-print image backfill — closes vintage e-card era image gap.

E1-E5 (492 vintage Japanese e-card era cards) had no images after the
Bulbagarden JA backfill skipped them (Bulbapedia has no JA-print scans
for that era; the existing SET_MAP doesn't include e-card variants).

Hareruya2 carries 558 products tagged [E1]-[E5] in its Shopify catalog,
each with a Shopify-CDN-hosted image URL of the actual JA-print scan.
Same product cache we already walk for pricing in
backfill_ptcg_prices_hareruya.py.

Strategy:
1. Walk products_raw.jsonl (or live-fetch if cache is stale)
2. Parse title for [setid] + 〈local_id〉
3. Match against D1's JA cards where image_high IS NULL
4. Write Shopify CDN URL into image_high / image_low

Idempotent. Only fills nulls — never overwrites existing images.

The script's set_id remapping reuses HARERUYA_PROMO_MAP from the
pricing script. Both share `candidate_setids()` for case-folding +
prefix expansion.

Usage:
    python -m scripts.backfill_ptcg_images_hareruya --dry-run
    python -m scripts.backfill_ptcg_images_hareruya
    python -m scripts.backfill_ptcg_images_hareruya --use-cached  # skip walk
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)

BASE = "https://www.hareruya2.com"
HEADERS = {
    "User-Agent": "OPBindr-image-backfill/1.0 (https://opbindr.com; arjun@neuroplexlabs.com)",
    "Accept": "application/json",
}
DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
RAW_CACHE = Path("data/poc_hareruya/products_raw.jsonl")
OUT_DIR = Path("data/backfill/hareruya_images")
OUT_DIR.mkdir(parents=True, exist_ok=True)

COLLECTIONS = [
    "pmcg", "neo", "vs", "e", "adv", "pcg", "dp", "dpt", "legend",
    "bw", "xy", "generations", "sm", "sunmoon", "ss", "sv", "svm", "mega",
]

TITLE_RE = re.compile(
    r"〈\s*(?P<lid>[A-Za-z0-9\-/]+?)\s*(?:/\s*[\dA-Za-z\-]+)?\s*〉\s*\[\s*(?P<setid>[^\]]+?)\s*\]"
)

# Same remap shape as backfill_ptcg_prices_hareruya.py — keeps both
# scripts' (set_id, lid) keys aligned.
HARERUYA_PROMO_MAP = {
    "S-P": "SWSHP", "SM-P": "SMP", "XY-P": "XYP", "BW-P": "BWP",
    "DP-P": "DPP",  "SV-P": "SVP", "P-P":  "PP",
}


def candidate_setids(s: str) -> list[str]:
    cands = {s, s.upper(), s.lower()}
    if s in HARERUYA_PROMO_MAP:
        cands.add(HARERUYA_PROMO_MAP[s])
    if re.match(r"^S\d", s):
        cands.add("SWSH" + s[1:].upper())
        cands.add("SWSH" + s[1:])
    if re.match(r"^M\d", s):
        cands.add("MEGA" + s[1:].upper())
        cands.add(s.upper())
    return list(cands)


def normalize_lid(lid: str) -> list[str]:
    out = [lid]
    if lid.lstrip("0") and lid.lstrip("0") != lid:
        out.append(lid.lstrip("0"))
    # eg "024" → ["024", "24"]; D1 stores both forms across sets
    return out


def query_d1(query: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", query],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=str(REPO_ROOT),
    )
    data = json.loads(out.stdout)
    if not data or not data[0].get("success"): return []
    return data[0]["results"] or []


def walk_collection(handle: str) -> list[dict]:
    products = []
    page = 1
    seen = set()
    while True:
        url = f"{BASE}/collections/{handle}/products.json?limit=250&page={page}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404): break
            time.sleep(5); continue
        except Exception:
            time.sleep(5); continue
        page_products = data.get("products", [])
        if not page_products: break
        new = [p for p in page_products if p["id"] not in seen]
        if not new: break
        for p in new: seen.add(p["id"])
        products.extend(new)
        page += 1
        time.sleep(0.7)
        if page > 100: break
    return products


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--use-cached", action="store_true",
                    help="Reuse data/poc_hareruya/products_raw.jsonl from a prior walk")
    args = ap.parse_args()

    t0 = time.time()
    if args.use_cached and RAW_CACHE.exists():
        print(f"1. Reusing cached products from {RAW_CACHE}")
        products = []
        with RAW_CACHE.open(encoding="utf-8") as f:
            for line in f:
                products.append(json.loads(line))
        print(f"   total: {len(products)}")
    else:
        print(f"1. Walking {len(COLLECTIONS)} series collections...")
        products = []
        seen = set()
        for handle in COLLECTIONS:
            ps = walk_collection(handle)
            new = [p for p in ps if p["id"] not in seen]
            for p in new: seen.add(p["id"])
            products.extend(new)
            print(f"   [{handle:15s}] +{len(new):>5d}  total={len(products):>6d}", flush=True)
        RAW_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with RAW_CACHE.open("w", encoding="utf-8") as f:
            for p in products:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")

    print("\n2. Indexing (set_id, local_id) → image URL...")
    by_card: dict[tuple[str, str], str] = {}
    for p in products:
        m = TITLE_RE.search(p.get("title", "") or "")
        if not m: continue
        setid, lid = m.group("setid").strip(), m.group("lid").strip()
        images = p.get("images", [])
        if not images or not isinstance(images[0], dict): continue
        img_url = images[0].get("src")
        if not img_url: continue
        # Strip Shopify cache-busting suffix like ?v=1234567890. Also strip
        # the trailing _600x.jpg-style transformation suffix so we get the
        # canonical original; wsrv on the frontend handles resizing.
        img_url = img_url.split("?")[0]
        # First-seen wins per (set_id, local_id) candidate key. Multiple
        # condition variants of the same card share artwork.
        for sid in candidate_setids(setid):
            for nlid in normalize_lid(lid):
                by_card.setdefault((sid, nlid), img_url)

    print(f"   indexed: {len(by_card)} unique (setid, lid) → image pairs")

    print("\n3. Pulling JA cards without images...")
    cards = query_d1(
        "SELECT card_id, set_id, local_id FROM ptcg_cards "
        "WHERE lang='ja' AND image_high IS NULL"
    )
    print(f"   {len(cards)} candidates")

    matches = []
    for c in cards:
        sid = c["set_id"]
        for lid in normalize_lid(c["local_id"]):
            url = by_card.get((sid, lid))
            if url:
                matches.append({"card_id": c["card_id"], "url": url, "set_id": sid})
                break

    print(f"   matched: {len(matches)} / {len(cards)} ({100*len(matches)/max(1,len(cards)):.1f}%)")

    if not matches:
        print("Nothing to write.")
        return

    from collections import Counter
    by_set = Counter(m["set_id"] for m in matches)
    print("\n   By set:")
    for s, n in by_set.most_common(15):
        print(f"     {s:10s} {n}")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sql_lines = [
        f"-- Hareruya JA-print image backfill. Generated {fetched_at}.",
        "-- Only fills cards where image_high IS NULL — never overwrites.",
    ]
    for m in matches:
        url = m["url"].replace("'", "''")
        sql_lines.append(
            f"UPDATE ptcg_cards SET image_high='{url}', image_low='{url}' "
            f"WHERE lang='ja' AND card_id='{m['card_id']}' AND image_high IS NULL;"
        )

    sql_path = OUT_DIR / "hareruya_images_ja.sql"
    sql_path.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    json_path = OUT_DIR / "matches.json"
    json_path.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSQL written: {sql_path}")

    if args.dry_run:
        print("(dry run — D1 not touched)")
        return

    print("\n4. Applying...")
    apply = subprocess.run(
        WRANGLER_BIN + ["--remote", "--file", str(sql_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT),
    )
    if apply.returncode != 0:
        print(f"D1 apply FAILED: {apply.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    for line in apply.stdout.split("\n"):
        if "rows_written" in line or "duration" in line.lower():
            print(f"   {line.strip()}")
    print(f"   walltime: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
