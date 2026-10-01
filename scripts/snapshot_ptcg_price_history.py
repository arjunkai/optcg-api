"""
snapshot_ptcg_price_history.py — append a price-history row per
(card, source, variant) pair from the current ptcg_cards.pricing_json,
but only when its price differs from that series' latest row.

Runs at the end of every weekly PTCG cron after all sources have
landed their prices. INSERT OR IGNORE keys on (card_id, source,
variant, recorded_at), so re-runs in the same second are no-ops.

Change-only (2026-10-01): writing every series every week was ~45-70k
D1 rows written per run against Workers Free's 100k/day, and most of it
repeated last week's value (TCGdex Cardmarket averages never refresh).
Each insert now checks the series' latest row first, a key-range read
on (card_id, recorded_at DESC) that stops at the first match, a few rows
per card. A flat price adds no point; the chart draws the line between
the points it has.

The Worker's /pokemon/cards/:id/price-history endpoint reads from
this table to render charts. See src/pokemon/cards.js.

What gets stored (2026-09-30): every USD series (TCGplayer variants and
the single-price sources), plus ONE Cardmarket series per card, the
first present in the chart's CARDMARKET_VARIANT_PRIORITY. OPBindr's
PriceHistoryChart only plots USD points and picks one series per card,
so the other 11 Cardmarket keys (EUR, never drawn) were ~80% of the rows
and pushed the database past D1's 500 MB free-tier cap.

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
# Same order as CARDMARKET_VARIANT_PRIORITY in opbindr's
# PriceHistoryChart.jsx; only the first one present is stored.
CARDMARKET_VARIANTS = [
    "avg", "trend", "avg7", "avg30", "avg1", "low",
    "reverseHoloSell", "reverseHoloTrend", "reverseHoloLow",
    "avg7Foil", "avg30Foil", "lowFoil",
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
    stmts, skipped = build_statements(rows, now)

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
        last_err = ""
        # Retry transient wrangler / CF network blips. The HTTP 502 / 503 /
        # "fetch request failed" class of error has shown up randomly inside
        # an otherwise-healthy run — don't let one blip abort everything
        # when the work is INSERT OR IGNORE idempotent anyway.
        for attempt in range(5):
            result = subprocess.run(
                WRANGLER + [f"--file={f}", "--remote"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                break
            last_err = (result.stderr or result.stdout or "")[:400]
            backoff = 5 * (2 ** attempt)  # 5, 10, 20, 40, 80 seconds
            print(f"   retry {attempt + 1}/5 after {backoff}s (err: {last_err[:120]})")
            time.sleep(backoff)
        else:
            print(f"   FAIL after 5 retries: {last_err}")
            sys.exit(1)
    print("Done.")


def build_statements(rows: list[dict], now: int) -> tuple[list[str], int]:
    """INSERT statements for one snapshot, and how many rows had bad pricing_json."""
    stmts: list[str] = []
    skipped = 0
    # ptcg_price_history has no lang column, and EN/JA rows can share a
    # card_id with the same (source, variant) (ebay, manual). Keep only the
    # first row per series (rows come in rowid order, the order the old
    # same-timestamp INSERT OR IGNORE resolved it); otherwise the change-only
    # check compares EN against JA and the series flips every week.
    seen: set[tuple[str, str, str]] = set()
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

        for source, variant, usd, eur in history_rows(pj):
            if (cid, source, variant) in seen:
                continue
            seen.add((cid, source, variant))
            stmts.append(_insert(cid, source, variant, usd, eur, now))
    return stmts, skipped


def history_rows(pj: dict) -> list[tuple[str, str, float | None, float | None]]:
    """(source, variant, price_usd, price_eur) tuples to snapshot for one card."""
    out: list[tuple[str, str, float | None, float | None]] = []

    def positive(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0

    # tcgplayer: per-variant .market in USD
    tcg = pj.get("tcgplayer")
    if isinstance(tcg, dict):
        for v in TCGPLAYER_VARIANTS:
            block = tcg.get(v)
            if isinstance(block, dict) and positive(block.get("market")):
                out.append(("tcgplayer", v, float(block["market"]), None))

    # cardmarket: flat keys in EUR, first present only
    cm = pj.get("cardmarket")
    if isinstance(cm, dict):
        for v in CARDMARKET_VARIANTS:
            if positive(cm.get(v)):
                out.append(("cardmarket", v, None, float(cm[v])))
                break

    # Single-price sources.
    for src in SINGLE_PRICE_SOURCES:
        block = pj.get(src)
        if not isinstance(block, dict):
            continue
        for key in ("price", "price_usd", "market"):
            if positive(block.get(key)):
                out.append((src, "market", float(block[key]), None))
                break
    return out


def _fetch_priced_rows() -> list[dict]:
    sql = ("SELECT card_id, lang, pricing_json FROM ptcg_cards "
           "WHERE pricing_json IS NOT NULL AND pricing_json != '{}' AND pricing_json != '' "
           "ORDER BY rowid")
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
    cid, src, var = _esc(card_id), _esc(source), _esc(variant)
    usd = 'NULL' if price_usd is None else repr(float(price_usd))
    eur = 'NULL' if price_eur is None else repr(float(price_eur))
    return (
        "INSERT OR IGNORE INTO ptcg_price_history "
        "(card_id, source, variant, recorded_at, price_usd, price_eur) "
        f"SELECT {cid}, {src}, {var}, {recorded_at}, {usd}, {eur} "
        "WHERE NOT EXISTS (SELECT 1 FROM ("
        "SELECT price_usd, price_eur FROM ptcg_price_history "
        f"WHERE card_id = {cid} AND source = {src} AND variant = {var} "
        "ORDER BY recorded_at DESC LIMIT 1) AS last "
        f"WHERE last.price_usd IS {usd} AND last.price_eur IS {eur});"
    )


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


if __name__ == "__main__":
    main()
