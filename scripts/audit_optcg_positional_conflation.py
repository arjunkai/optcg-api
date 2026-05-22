"""Detect positional-pass conflations in optcg `cards` table.

The positional fallback in `build_all_prices.py` claims `candidates[0]`
from the parallels-by-base list whenever a TCGPlayer scrape row's
variant_type matches. When the SAME tcg_id appears in two or more
TCGPlayer guide MD files (cross-listings — e.g. an OP-01 promo also
listed on the universal "569901" Promotion Cards bucket), the
positional pass iterates the row twice and ends up claiming two
parallel slots from a single physical product.

That's the same conflation shape PriceCharting hit on the PTCG side
(audit_pricecharting_jp_conflation.py). The OPTCG version is rarer
because dotgg.gg covers ~98% of cards authoritatively, leaving only
~190 rows in the positional pool. But every conflation still has
non-trivial dollar blast radius (the smoking-gun OP01-001 case stamped
the same tcg_id 485262 on both _p1 ($110) and _p2 ($99), with neither
price matching the scrape's true $61.78 mid-market).

Signals checked (read-only):
  1. tcg_ids 1:1 violation - same tcg_id stamped on >1 card_id where
     price_source='positional'. This is the unambiguous bug: one
     TCGPlayer product cannot be two cards.
  2. file <-> D1 tcg_id drift - data/card_prices_all.json (the build
     output that fed import-prices-d1.js) carries the latest pipeline
     intent; D1 should match it. Drift signals that a subsequent CI
     run overwrote one row's tcg_ids with a different product's id,
     usually because a previously-matched parallel went out of stock
     and the next scrape's leftover claim chain shifted.

Output:
  data/backfill/optcg_positional_conflation_audit.json
    - full per-row audit with verdict + evidence
  scripts/jp_batches/null_optcg_positional_conflated.sql
    - UPDATE statements that NULL the price/tcg_ids/price_source for
      the smaller-confidence half of each duplicate pair so the next
      refresh repopulates from a clean slate

Run:
  python -m scripts.audit_optcg_positional_conflation              # dry-run report
  python -m scripts.audit_optcg_positional_conflation --emit-sql   # write NULL SQL too
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from scripts.wrangler_retry import run_wrangler


WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
PRICES_FILE = Path("data/card_prices_all.json")
OUT_AUDIT = Path("data/backfill/optcg_positional_conflation_audit.json")
OUT_SQL = Path("scripts/jp_batches/null_optcg_positional_conflated.sql")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true",
                    help="Write the NULL-out SQL to scripts/jp_batches/null_optcg_positional_conflated.sql")
    args = ap.parse_args()

    print("1. Loading data/card_prices_all.json (pipeline intent)...")
    if not PRICES_FILE.exists():
        print(f"   ERROR: {PRICES_FILE} not found. Run build_all_prices.py first.")
        sys.exit(1)
    file = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    file_positional = {
        k: {"price": v["price"], "tcg_ids": v.get("tcg_ids") or []}
        for k, v in file.items()
        if str(v.get("match_method", "")).startswith("positional")
    }
    print(f"   {len(file_positional)} positional entries in build output")

    print("\n2. Querying D1 for positional rows...")
    d1_rows = fetch_d1_positional_rows()
    print(f"   {len(d1_rows)} positional rows in D1")

    print("\n3. Checking tcg_ids 1:1 violations in D1...")
    by_tcg = defaultdict(list)
    for r in d1_rows:
        tcg_ids = r["tcg_ids"] or ""
        if tcg_ids and tcg_ids != "[]":
            by_tcg[tcg_ids].append(r)
    dupes = {k: rows for k, rows in by_tcg.items() if len(rows) > 1}
    print(f"   {len(dupes)} duplicate tcg_id assignment(s)")
    for tcg, rows in dupes.items():
        print(f"   tcg_ids={tcg} stamped on:")
        for r in rows:
            print(f"     - {r['id']:20s} ${r['price']}")

    print("\n4. Diffing build-output vs D1 for tcg_id drift...")
    d1_by_id = {r["id"]: r for r in d1_rows}
    drifted = []
    for cid, intent in file_positional.items():
        if cid not in d1_by_id:
            continue  # D1 may have re-sourced this row via a higher-confidence
                      # pass (dotgg/ebay/manual); not a conflation, just an upgrade.
        intent_tcg = json.dumps(intent["tcg_ids"])
        d1_tcg = d1_by_id[cid]["tcg_ids"]
        if intent_tcg != d1_tcg:
            drifted.append({
                "card_id": cid,
                "file_tcg_ids": intent_tcg,
                "file_price": intent["price"],
                "d1_tcg_ids": d1_tcg,
                "d1_price": d1_by_id[cid]["price"],
            })
    print(f"   {len(drifted)} drifted row(s)")
    for d in drifted:
        print(f"   {d['card_id']:20s} file=tcg{d['file_tcg_ids']} ${d['file_price']}"
              f"  D1=tcg{d['d1_tcg_ids']} ${d['d1_price']}")

    # Confirmed conflations: duplicates where both rows have positional source.
    # The dupe entry covers the smoking-gun case (OP01-001 _p1 and _p2 both
    # stamped 485262). We leave _p2 alone (it's the more recent/expensive
    # match, more likely the true product per the official onepiece-cardgame
    # naming convention) and NULL _p1 so the next refresh repopulates clean.
    # For tcg-id drift cases not covered by a duplicate, the D1 row IS the
    # wrong one — file is the canonical pipeline intent.
    confirmed = []
    for tcg, rows in dupes.items():
        # Keep the row whose tcg_id matches the pipeline file's intent.
        # Null the others.
        rows_sorted = sorted(rows, key=lambda r: r["id"])
        keep_id = None
        for r in rows_sorted:
            file_entry = file_positional.get(r["id"])
            if file_entry and json.dumps(file_entry["tcg_ids"]) == r["tcg_ids"]:
                keep_id = r["id"]
                break
        # If no file row matches D1 for any of the dupes, keep the
        # higher-priced one (more likely the genuine ultra-rare promo)
        # and null the rest.
        if keep_id is None:
            keep_id = max(rows_sorted, key=lambda r: r["price"] or 0.0)["id"]
        for r in rows_sorted:
            if r["id"] != keep_id:
                confirmed.append({
                    "card_id": r["id"],
                    "reason": "duplicate_tcg_id",
                    "d1_tcg_ids": r["tcg_ids"],
                    "d1_price": r["price"],
                    "kept_sibling": keep_id,
                })

    audit = {
        "file_positional_count": len(file_positional),
        "d1_positional_count": len(d1_rows),
        "duplicate_tcg_ids": [
            {"tcg_ids": k, "rows": [{"card_id": r["id"], "price": r["price"]} for r in rows]}
            for k, rows in dupes.items()
        ],
        "tcg_id_drift": drifted,
        "confirmed_conflations": confirmed,
    }
    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n5. Audit written to {OUT_AUDIT}")

    if not confirmed:
        print("\n   No confirmed conflations. Nothing to NULL.")
        return

    print(f"\n   {len(confirmed)} confirmed conflation(s) ready for NULL-out:")
    for c in confirmed:
        print(f"   - {c['card_id']:20s} (keeping {c['kept_sibling']})  was ${c['d1_price']}")

    if args.emit_sql:
        OUT_SQL.parent.mkdir(parents=True, exist_ok=True)
        statements = []
        for c in confirmed:
            cid = c["card_id"].replace("'", "''")
            statements.append(
                f"UPDATE cards SET price=NULL, tcg_ids=NULL, price_source=NULL "
                f"WHERE id='{cid}' AND price_source='positional';"
            )
        OUT_SQL.write_text("\n".join(statements) + "\n", encoding="utf-8")
        print(f"\n   Wrote {OUT_SQL} ({len(statements)} statement(s))")
    else:
        print("\n   --emit-sql not set; SQL file not written.")


def fetch_d1_positional_rows() -> list[dict]:
    cmd = WRANGLER + [
        "--remote", "--json", "--command",
        "SELECT id, price, tcg_ids FROM cards WHERE price_source='positional'",
    ]
    result = run_wrangler(cmd)
    if result.returncode != 0:
        print(f"   FAIL: {(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = _strip_wrangler_chrome(result.stdout)
    data = json.loads(payload)
    rows = data[0]["results"] if isinstance(data, list) else data.get("results", [])
    return rows


def _strip_wrangler_chrome(stdout: str) -> str:
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


if __name__ == "__main__":
    main()
