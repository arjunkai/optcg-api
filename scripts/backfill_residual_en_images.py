"""
Residual EN image backfill for the 78 cards Bulbagarden missed.

After backfill_ptcg_images_bulbagarden.py runs, ~78 cards remain
imageless across cel25 (Celebrations Classic), svp (SV promos),
tk-xy-n/-sy / tk-dp-l (Trainer Kits), mfb, mee/sve, xya.

Strategy per cohort:

  cel25 (23 cards) — pokemontcg.io has them under set 'cel25c'
    (Celebrations Classic Collection) with URL pattern
    cel25c/{N}_{A|B|C|D}_hires.png. Multi-variants like 15A1..A4 map
    to {15_A, 15_B, 15_C, 15_D}.

  svp (13 cards) — pokemontcg.io has 'svp' (SV Promos) with raw
    numeric URLs. Direct {local_id}_hires.png lookup.

  tk-xy-n, tk-xy-sy, tk-dp-l, mfb, mee, sve, xya (~42 cards) — not on
    pokemontcg.io. Skip — the next eBay residual run picks them up,
    or they stay imageless and render as the '—' placeholder pill.

Lookups go through api.pokemontcg.io which provides authoritative
(card_id, image_url) pairs, no URL guessing.

Usage:
  python -m scripts.backfill_residual_en_images --dry-run
  python -m scripts.backfill_residual_en_images
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)

DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill/residual_en")
OUT_DIR.mkdir(parents=True, exist_ok=True)

POKEMONTCG_API = "https://api.pokemontcg.io/v2/cards"
HEADERS = {"User-Agent": "OPBindr-image-backfill/1.0"}


def query_d1(sql: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=str(REPO_ROOT),
    )
    data = json.loads(out.stdout)
    if not data or not data[0].get("success"): return []
    return data[0]["results"] or []


def fetch_pokemontcg_set(set_id: str) -> list[dict]:
    """Pull every card from a pokemontcg.io set with its image URL."""
    api_key = os.environ.get("POKEMONTCG_API_KEY")
    headers = dict(HEADERS)
    if api_key:
        headers["X-Api-Key"] = api_key
    url = f"{POKEMONTCG_API}?q=set.id:{set_id}&pageSize=250"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("data", [])


def remap_cel25_card_id(card_id: str) -> str | None:
    """cel25-2A    → cel25c-2_A
       cel25-15A1  → cel25c-15_A1
       cel25-15A2  → cel25c-15_A2
       cel25-107A  → cel25c-107_A

    pokemontcg.io's id format inserts '_' between the number and letter,
    and keeps the sub-index suffix verbatim. The image URL letter
    mapping (A1→A, A2→B, A3→C, A4→D) is computed by the API on the
    card object's images field — we don't need to figure it out here."""
    m = re.match(r"^cel25-(\d+)([A-Z]\d*)$", card_id)
    if not m: return None
    num, suffix = m.group(1), m.group(2)
    return f"cel25c-{num}_{suffix}"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("1. Querying D1 for imageless EN cards...")
    cards = query_d1(
        "SELECT card_id, name, set_id, local_id FROM ptcg_cards "
        "WHERE lang='en' AND image_high IS NULL "
        "ORDER BY set_id, local_id"
    )
    print(f"   {len(cards)} imageless cards")
    by_set = {}
    for c in cards:
        by_set.setdefault(c["set_id"], []).append(c)

    matches: list[dict] = []

    # === Cohort 1: cel25 → cel25c ===
    if "cel25" in by_set:
        print(f"\n2a. cel25 ({len(by_set['cel25'])} cards) → pokemontcg.io 'cel25c'")
        try:
            cel25c_cards = fetch_pokemontcg_set("cel25c")
        except Exception as e:
            print(f"   FAILED: {e}")
            cel25c_cards = []
        by_id = {c["id"]: c for c in cel25c_cards}
        for c in by_set["cel25"]:
            target = remap_cel25_card_id(c["card_id"])
            if not target:
                print(f"   [skip] {c['card_id']} doesn't fit the cel25c pattern")
                continue
            ptcg_card = by_id.get(target)
            if not ptcg_card:
                print(f"   [miss] {c['card_id']} → {target} not in cel25c")
                continue
            url = ptcg_card.get("images", {}).get("large")
            if not url:
                continue
            matches.append({"card_id": c["card_id"], "url": url, "source": "cel25c"})
            print(f"   [OK]   {c['card_id']:14s} → {url}")

    # === Cohort 2: svp → svp on pokemontcg.io (numeric IDs) ===
    if "svp" in by_set:
        print(f"\n2b. svp ({len(by_set['svp'])} cards) → pokemontcg.io 'svp'")
        try:
            svp_cards = fetch_pokemontcg_set("svp")
        except Exception as e:
            print(f"   FAILED: {e}")
            svp_cards = []
        by_num = {c.get("number"): c for c in svp_cards}
        for c in by_set["svp"]:
            lid = c["local_id"].lstrip("0") or c["local_id"]
            ptcg_card = by_num.get(lid) or by_num.get(c["local_id"])
            if not ptcg_card:
                print(f"   [miss] {c['card_id']} (lid {lid}) not on pokemontcg.io/svp")
                continue
            url = ptcg_card.get("images", {}).get("large")
            if not url: continue
            matches.append({"card_id": c["card_id"], "url": url, "source": "svp"})
            print(f"   [OK]   {c['card_id']:14s} → {url}")

    # === Cohort 3: report what we can't fix here ===
    fixable_sets = {"cel25", "svp"}
    untouched = [s for s in by_set if s not in fixable_sets]
    if untouched:
        print(f"\n2c. Sets not handled by this script (need eBay residual or stay imageless):")
        for s in sorted(untouched):
            ids = [c["card_id"] for c in by_set[s][:3]]
            print(f"   {s:15s} {len(by_set[s]):>3} cards (sample: {', '.join(ids)})")

    if not matches:
        print("\nNo matches resolved.")
        return

    print(f"\n3. Built {len(matches)} UPDATEs")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sql_lines = [
        "-- Residual EN image backfill via api.pokemontcg.io.",
        f"-- Generated: {fetched_at}",
    ]
    for m in matches:
        url = m["url"].replace("'", "''")
        sql_lines.append(
            f"UPDATE ptcg_cards SET image_high='{url}', image_low='{url}' "
            f"WHERE lang='en' AND card_id='{m['card_id']}' AND image_high IS NULL;"
        )

    sql_path = OUT_DIR / "residual_en_images.sql"
    sql_path.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    json_path = OUT_DIR / "matches.json"
    json_path.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   SQL: {sql_path}")

    if args.dry_run:
        print("\n--dry-run: D1 not touched")
        return

    print("\n4. Applying to D1...")
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


if __name__ == "__main__":
    main()
