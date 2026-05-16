"""
snapshot_ptcg_price_history.py — append a price-history row per
(card, source, variant) pair from the current ptcg_cards.pricing_json.

Runs at the end of every weekly PTCG cron after all sources have
landed their prices. INSERT OR IGNORE keys on (card_id, source,
variant, recorded_at), so re-runs in the same second are no-ops and
weekly runs naturally chart out one snapshot per Monday.

The Worker's /pokemon/cards/:id/price-history endpoint reads from
this table to render charts. See src/pokemon/cards.js.

Why Python and not the original node version: shell-quoting a long
SELECT through Node's execFileSync with shell:true on Windows broke
wrangler's argument parsing. Python's subprocess.run handles the
argv list reliably.

Usage:
    python -m scripts.snapshot_ptcg_price_history --dry-run
    python -m scripts.snapshot_ptcg_price_history
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
OUT_DIR = Path("scripts/ptcg_history_batches")
BATCH_SIZE = 500

# Variants we extract per source. Anything not listed is ignored so
# a future source key doesn't silently land as junk history.
TCGPLAYER_VARIANTS = [
    "holofoil", "normal", "reverseHolofoil",
    "firstEditionHolofoil", "firstEditionNormal",
    "unlimitedHolofoil", "unlimited",
]
CARDMARKET_VARIANTS = [
    "avg", "trend", "avg7", "avg30", "avg1", "low", "lowFoil",
    "avg7Foil", "avg30Foil",
    "reverseHoloSell", "reverseHoloLow", "reverseHoloTrend",
]
SINGLE_PRICE_SOURCES = ["manual", "hareruya", "yuyutei", "pricecharting", "ebay"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Write SQL files, don't execute.")
    args = ap.parse_args()

    print("1. Fetching ptcg_cards with pricing_json...")
    rows = _fetch_priced_rows()
    print(f"   {len(rows)} cards with pricing data")

    print("2. Extracting (card_id, source, variant, price) tuples...")
    now = int(time.time())
    stmts: list[str] = []
    skipped = 0
    for row in rows:
        cid = row["card_id"]
        pj_str = row.get("pricing_json")
        try:
            pj = json.loads(pj_str) if pj_str else None
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        if not isinstance(pj, dict):
            skipped += 1
            continue

        # tcgplayer: per-variant .market in USD
        tcg = pj.get("tcgplayer")
        if isinstance(tcg, dict):
            for v in TCGPLAYER_VARIANTS:
                block = tcg.get(v)
                if isinstance(block, dict):
                    market = block.get("market")
                    if isinstance(market, (int, float)) and market > 0:
                        stmts.append(_insert(cid, "tcgplayer", v, float(market), None, now))

        # cardmarket: flat keys in EUR
        cm = pj.get("cardmarket")
        if isinstance(cm, dict):
            for v in CARDMARKET_VARIANTS:
                val = cm.get(v)
                if isinstance(val, (int, float)) and val > 0:
                    stmts.append(_insert(cid, "cardmarket", v, None, float(val), now))

        # Single-price sources.
        for src in SINGLE_PRICE_SOURCES:
            block = pj.get(src)
            if not isinstance(block, dict):
                continue
            price = None
            for key in ("price", "price_usd", "market"):
                v = block.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    price = float(v)
                    break
            if price is not None:
                stmts.append(_insert(cid, src, "market", price, None, now))

    print(f"   {len(stmts)} history rows to insert ({skipped} cards skipped on bad pricing_json)")
    if not stmts:
        print("Nothing to insert. Exiting.")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y-%m-%d", time.gmtime(now))
    files: list[Path] = []
    for i in range(0, len(stmts), BATCH_SIZE):
        slice_ = stmts[i:i + BATCH_SIZE]
        path = OUT_DIR / f"{ts_tag}_{((i // BATCH_SIZE) + 1):03d}.sql"
        path.write_text("\n".join(slice_) + "\n", encoding="utf-8")
        files.append(path)
    print(f"3. Wrote {len(files)} batch files to {OUT_DIR}/")

    if args.dry_run:
        print("(Dry run — no D1 writes.)")
        return

    print("4. Applying batches to remote D1...")
    for i, f in enumerate(files, 1):
        print(f"   [{i}/{len(files)}] {f.name}")
        result = subprocess.run(
            WRANGLER + [f"--file={f}", "--remote"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"   FAIL: {(result.stderr or '')[:400]}")
            sys.exit(1)
    print("Done.")


def _fetch_priced_rows() -> list[dict]:
    sql = ("SELECT card_id, lang, pricing_json FROM ptcg_cards "
           "WHERE pricing_json IS NOT NULL AND pricing_json != '{}' AND pricing_json != ''")
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
    return json.loads(out.stdout[start:])[0]["results"]


def _insert(card_id: str, source: str, variant: str,
            price_usd: float | None, price_eur: float | None,
            recorded_at: int) -> str:
    return (
        "INSERT OR IGNORE INTO ptcg_price_history "
        "(card_id, source, variant, recorded_at, price_usd, price_eur) "
        f"VALUES ({_esc(card_id)}, {_esc(source)}, {_esc(variant)}, "
        f"{recorded_at}, "
        f"{'NULL' if price_usd is None else price_usd}, "
        f"{'NULL' if price_eur is None else price_eur});"
    )


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


if __name__ == "__main__":
    main()
