"""
One-shot dedupe: merge JA ptcg_cards rows that represent the same physical
card under different card_id conventions.

Background: two ingest pipelines disagree on ID format —
  - TCGdex (ptcg-import-d1.js)      writes lowercase set + zero-padded
                                     local_id, e.g. "sv4a-210", "m1s-001".
  - pkmnbindr (import-pkmnbindr-jp-*) writes uppercase set + unpadded
                                     local_id, e.g. "SV4A-210", "M1S-1".

Both rows refer to the same physical card. Composite PK is (card_id, lang),
so D1 keeps them as distinct rows. Each then gets pricing/image data from
independent backfills, so users see "duplicates with different prices."

This script:
  1. Groups all JA rows by (UPPER(set_id), CAST(local_id AS INTEGER)).
  2. For each group with >1 row: picks a canonical row (TCGdex-format
     preferred), merges every sparse column from the sibling(s) into
     the canonical row using "prefer non-empty, canonical wins on tie",
     then deletes the sibling(s).
  3. Writes batched UPDATE + DELETE statements to scripts/dedupe_ja/.
  4. With --execute: applies them against remote D1 via wrangler.

Usage:
    python -m scripts.dedupe_ja_duplicates --dry-run    # write SQL only
    python -m scripts.dedupe_ja_duplicates --execute    # write + apply
    python -m scripts.dedupe_ja_duplicates --inspect 10 # show 10 sample groups
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path("scripts/dedupe_ja")
BATCH_SIZE = 250  # SQL statements per file
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]

# Columns we fetch and merge. Order matters for the UPDATE statement.
MERGE_COLS = [
    "name", "name_en", "category", "rarity", "hp", "retreat",
    "types_csv", "stage", "variants_json", "image_high", "image_low",
    "pricing_json", "price_source", "dominant_color", "raw",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Write SQL files, don't execute. Inspect them, then re-run with --execute.")
    g.add_argument("--execute", action="store_true",
                   help="Write SQL files AND apply them against remote D1.")
    g.add_argument("--inspect", type=int, metavar="N",
                   help="Show N sample dupe groups + the merge decisions; don't write anything.")
    args = ap.parse_args()

    print("1. Fetching all JA rows from D1...")
    rows = _fetch_all_ja_rows()
    print(f"   {len(rows)} JA rows total")

    print("2. Grouping by (upper(set_id), int(local_id))...")
    groups = _group_dupes(rows)
    dupes = [g for g in groups.values() if len(g) > 1]
    print(f"   {len(dupes)} duplicate groups (sum of rows in dupes: {sum(len(g) for g in dupes)})")
    print(f"   net rows after dedupe: {len(rows) - sum(len(g) - 1 for g in dupes)}")

    if args.inspect:
        _print_samples(dupes, args.inspect)
        return

    print("3. Computing merge plans...")
    updates: list[tuple[str, dict]] = []  # (canonical_card_id, merged_values)
    deletes: list[str] = []
    skipped = 0
    for group in dupes:
        canonical, siblings = _choose_canonical(group)
        merged = _merge_rows(canonical, siblings)
        if merged == _row_to_merge_dict(canonical):
            # Sibling had nothing the canonical didn't — just delete siblings.
            skipped += 1
        else:
            updates.append((canonical["card_id"], merged))
        for s in siblings:
            deletes.append(s["card_id"])

    print(f"   {len(updates)} UPDATEs needed, {len(deletes)} DELETEs, "
          f"{skipped} canonical rows already complete")

    print("4. Writing batched SQL files...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = _write_batches(updates, deletes)
    print(f"   wrote {len(files)} batch files to {OUT_DIR}/")

    if args.dry_run:
        print("\nDry run done. Inspect scripts/dedupe_ja/*.sql, then run with --execute.")
        return

    print("5. Applying batches against remote D1...")
    for i, f in enumerate(files, 1):
        print(f"   [{i}/{len(files)}] executing {f.name}...")
        result = subprocess.run(
            WRANGLER + [f"--file={f}", "--remote"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"   FAIL: {(result.stderr or '')[:400]}")
            sys.exit(1)
    print("Done.")


def _fetch_all_ja_rows() -> list[dict]:
    cols = ["card_id", "set_id", "local_id"] + MERGE_COLS
    sql = f"SELECT {', '.join(cols)} FROM ptcg_cards WHERE lang='ja'"
    out = subprocess.run(
        WRANGLER + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        print("D1 query failed:", (out.stderr or "")[:500])
        sys.exit(1)
    start = (out.stdout or "").find("[")
    return json.loads(out.stdout[start:])[0]["results"]


def _group_dupes(rows: list[dict]) -> dict[tuple[str, int], list[dict]]:
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        try:
            lid_num = int(r["local_id"])
        except (TypeError, ValueError):
            # Non-numeric local_ids (rare, e.g. promo codes); skip dedupe for them.
            lid_num = -1
        key = (r["set_id"].upper(), lid_num)
        groups[key].append(r)
    return groups


def _choose_canonical(group: list[dict]) -> tuple[dict, list[dict]]:
    """Pick the TCGdex-format row as canonical.

    Preference order:
      1. Set_id is lowercase (TCGdex convention).
      2. Among same-case rows, the longer local_id wins (zero-padded > unpadded).
      3. Tiebreak: alphabetically first card_id (deterministic).
    """
    def score(r: dict) -> tuple:
        sid = r["set_id"]
        lid = r["local_id"] or ""
        return (
            sid == sid.lower(),  # True (lowercase) sorts after False, so reverse
            len(lid),            # longer local_id beats shorter
            -ord(r["card_id"][0]) if r["card_id"] else 0,
        )
    ordered = sorted(group, key=score, reverse=True)
    return ordered[0], ordered[1:]


def _merge_rows(canonical: dict, siblings: list[dict]) -> dict:
    """Merge sparse columns from siblings into the canonical row.

    Rule: prefer non-empty. If canonical has a non-empty value, keep it.
    Else take the first sibling's non-empty value. For pricing_json, do a
    top-level key deep-merge (canonical wins for collisions, sibling fills
    gaps).
    """
    merged = _row_to_merge_dict(canonical)
    for s in siblings:
        s_dict = _row_to_merge_dict(s)
        for col in MERGE_COLS:
            if col == "pricing_json":
                merged[col] = _merge_pricing(merged[col], s_dict[col])
            else:
                if _is_empty(merged[col]) and not _is_empty(s_dict[col]):
                    merged[col] = s_dict[col]
    return merged


def _row_to_merge_dict(row: dict) -> dict:
    return {col: row.get(col) for col in MERGE_COLS}


def _is_empty(v) -> bool:
    """Treat python-None, blank strings, empty containers, AND the
    literal strings "None"/"null"/"undefined" as empty. Some ingest
    scripts wrote str(None) into D1 by accident, so a sentinel check
    is needed alongside the real null check."""
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip().lower()
        if not s or s in ("none", "null", "undefined", "n/a"):
            return True
    if isinstance(v, (list, dict)) and not v:
        return True
    return False


def _merge_pricing(canon_str: str | None, sibling_str: str | None) -> str | None:
    """Top-level key merge for pricing_json. Canonical wins for keys
    present in both; sibling fills in keys canonical doesn't have."""
    canon = _safe_json(canon_str) or {}
    sib = _safe_json(sibling_str) or {}
    if not canon and not sib:
        return None
    merged = dict(canon)
    for k, v in sib.items():
        if k not in merged or _is_empty(merged[k]):
            merged[k] = v
    return json.dumps(merged, ensure_ascii=False) if merged else None


def _safe_json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _write_batches(updates: list[tuple[str, dict]], deletes: list[str]) -> list[Path]:
    stmts = []
    for card_id, vals in updates:
        set_parts = []
        for col in MERGE_COLS:
            set_parts.append(f"{col} = {_esc(vals[col])}")
        stmts.append(
            f"UPDATE ptcg_cards SET {', '.join(set_parts)} "
            f"WHERE card_id = {_esc(card_id)} AND lang = 'ja';"
        )
    for card_id in deletes:
        stmts.append(f"DELETE FROM ptcg_cards WHERE card_id = {_esc(card_id)} AND lang = 'ja';")

    files: list[Path] = []
    for i in range(0, len(stmts), BATCH_SIZE):
        batch_stmts = stmts[i:i + BATCH_SIZE]
        path = OUT_DIR / f"{(i // BATCH_SIZE) + 1:03d}.sql"
        path.write_text("\n".join(batch_stmts) + "\n", encoding="utf-8")
        files.append(path)
    return files


def _print_samples(dupes: list[list[dict]], n: int) -> None:
    print(f"\n=== first {min(n, len(dupes))} dupe groups + merge plans ===\n")
    for i, group in enumerate(dupes[:n]):
        canonical, siblings = _choose_canonical(group)
        merged = _merge_rows(canonical, siblings)
        print(f"[{i+1}] dupe group ({canonical['set_id']} lid={canonical['local_id']}):")
        for r in group:
            tag = "  CANON " if r is canonical else "  drop  "
            pj = _safe_json(r.get("pricing_json")) or {}
            price_keys = ",".join(sorted(pj.keys())) if pj else "-"
            print(f"   {tag} card_id={r['card_id']:<15} rarity={(r.get('rarity') or '-'):<12} "
                  f"image={(r.get('image_high') or '')[:35]:<35} pricing_keys={price_keys}")
        # Show net changes
        canon_dict = _row_to_merge_dict(canonical)
        changes = [c for c in MERGE_COLS if canon_dict[c] != merged[c]]
        if changes:
            print(f"   merge updates canonical's: {', '.join(changes)}")
        else:
            print(f"   canonical row already has everything — just deleting siblings")
        print()


if __name__ == "__main__":
    main()
