"""
Yuyutei catalog ingest — INSERT new ptcg_cards rows for cards that exist
in Yuyutei's per-set listings but are missing from D1 entirely.

Sibling to scripts/backfill_yuyutei_jp.py (which is the UPDATE consumer).
Both consumers share scripts/lib/yuyutei_scraper.py.

The 2026-05-18 Yuyutei audit found 4,118 priced Yuyutei products mapping
to TCGdex set IDs but only 1,058 of those have a corresponding D1 row —
TCGdex's import has never picked up the remaining ~2,454 (mostly modern
JA sets where TCGdex hasn't ingested the era's promos yet). This script
closes that gap.

INSERT shape carries everything Yuyutei gives us:
  card_id, lang='ja', set_id, local_id (unpadded), name (JA from <h4>),
  image_high, image_low (both = Yuyutei 100x140 thumb),
  pricing_json ({"yuyutei": {...}}), price_source ('yuyutei' or NULL).

Other denormalized fields (category/rarity/hp/types_csv/stage) stay NULL
— TCGdex eventual UPSERT fills them when (if) TCGdex catalogs the card.

Per-apply rollback: the list of card_ids INSERTed lands in
data/backfill/yuyutei_catalog_inserted_<YYYYMMDD-HHMMSS>.txt before
wrangler runs. Exact rollback: DELETE WHERE lang='ja' AND card_id IN (
the list).

Usage:
    python -m scripts.backfill_yuyutei_catalog --dry-run
    python -m scripts.backfill_yuyutei_catalog --set=SV10 --dry-run
    python -m scripts.backfill_yuyutei_catalog --limit=3 --dry-run
    python -m scripts.backfill_yuyutei_catalog --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

from scripts.lib.yuyutei_scraper import (
    IMAGE_HOST,
    REQ_INTERVAL_S,
    USER_AGENT,
    build_card_id_candidates,
    get_jpy_to_usd_rate,
    load_mapping,
    scrape_set_listing,
)
from scripts.wrangler_retry import WRANGLER_MAX_ATTEMPTS, run_wrangler


DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("scripts/insert_promo_rows")
ROLLBACK_DIR = Path("data/backfill")
BATCH_SIZE = 100  # rows per multi-statement SQL file
TARGET_LANG = "ja"


_EXISTING_LIDS: dict[str, set[str]] | None = None


def fetch_existing_lids_for_set(set_id: str) -> set[str]:
    """Return the set of local_id strings we already have for
    (set_id, lang='ja'), set_id compared case-insensitively. Used to diff
    against Yuyutei scraped products and decide which need INSERTing.

    Returns strings (not ints) because catalog rows can carry
    zero-padded local_ids like '001' that we should compare verbatim.
    Yuyutei's scraped card_number is unpadded; we compare against both
    the raw scraped value and the zfill(3) variant to catch either
    storage convention.

    Every JA (set_id, local_id) is read once per run and answered from
    memory: a per-set `WHERE UPPER(set_id) = ?` can use no index, so each
    of ~90 sets scanned all ~22k JA rows (~2M D1 rows read per run).
    """
    global _EXISTING_LIDS
    if _EXISTING_LIDS is None:
        _EXISTING_LIDS = _fetch_all_existing_lids()
    return set(_EXISTING_LIDS.get(set_id.upper(), set()))


def _fetch_all_existing_lids() -> dict[str, set[str]]:
    cmd = WRANGLER_BIN + [
        "--remote",
        "--json",
        "--command",
        f"SELECT set_id, local_id FROM ptcg_cards WHERE lang = '{TARGET_LANG}'",
    ]
    result = run_wrangler(cmd)
    if result.returncode != 0:
        print(f"   FAIL fetching existing LIDs after "
              f"{WRANGLER_MAX_ATTEMPTS} attempts: "
              f"{(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = _strip_wrangler_chrome(result.stdout)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        print(f"   FAIL parsing wrangler JSON: {e}\n"
              f"--- payload (head) ---\n{payload[:400]}")
        sys.exit(1)
    rows = data[0]["results"] if isinstance(data, list) else data.get("results", [])
    return group_lids(rows)


def group_lids(rows: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in rows:
        if r.get("local_id") is None or r.get("set_id") is None:
            continue
        out.setdefault(str(r["set_id"]).upper(), set()).add(str(r["local_id"]))
    return out


def _strip_wrangler_chrome(stdout: str) -> str:
    """Wrangler prints a config-warning banner before the JSON. Find the
    first '[' or '{' and slice from there. Lifted from backfill_mp_catalog.py."""
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


def insert_sql(
    card_id: str,
    set_id: str,
    local_id: str,
    name_ja: str,
    image_url: str | None,
    pricing_json: str | None,
    price_source: str | None,
) -> str:
    """Build one INSERT OR IGNORE statement for a Yuyutei-sourced row.

    pricing_json is the already-JSON-encoded {"yuyutei": {...}} string,
    or None for sold-out products. price_source is 'yuyutei' for
    in-stock priced products, None for sold-out.
    """
    return (
        "INSERT OR IGNORE INTO ptcg_cards "
        "(card_id, lang, set_id, local_id, name, image_high, image_low, pricing_json, price_source) "
        "VALUES ("
        + _esc(card_id) + ", "
        + _esc(TARGET_LANG) + ", "
        + _esc(set_id) + ", "
        + _esc(local_id) + ", "
        + _esc(name_ja) + ", "
        + _esc(image_url) + ", "
        + _esc(image_url) + ", "
        + (_esc(pricing_json) if pricing_json is not None else "NULL") + ", "
        + _esc(price_source) + ");"
    )


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", help="Only run this TCGdex set id (e.g. SV10)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of TCGdex sets processed (smoke tests)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Fetch + parse + diff + write SQL. Don't touch D1.")
    g.add_argument("--apply", action="store_true",
                   help="Fetch + parse + diff + write SQL AND run wrangler.")
    args = ap.parse_args()

    try:
        yuyutei_for = load_mapping()
    except FileNotFoundError as exc:
        print(exc)
        sys.exit(1)

    sets = [args.set] if args.set else list(yuyutei_for.keys())
    if args.limit:
        sets = sets[: args.limit]
    print(f"Yuyutei catalog ingest — {len(sets)} TCGdex JA sets")

    fx = get_jpy_to_usd_rate()
    print(f"FX rate: 1 JPY = {fx:.6f} USD\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)

    all_inserts: list[tuple[str, str]] = []  # (setcode, sql_line) for batched-file writing
    inserted_card_ids: list[str] = []         # full card_id list for rollback
    sets_skipped = 0
    sets_seen = 0
    warn_no_name = 0

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=True) as client:
        for tcgdex_id in sets:
            yuyutei_code = yuyutei_for.get(tcgdex_id)
            if not yuyutei_code:
                continue
            time.sleep(REQ_INTERVAL_S)
            cards = scrape_set_listing(client, yuyutei_code)
            if cards is None:
                sets_skipped += 1
                print(f"  [{tcgdex_id} -> {yuyutei_code}] not on Yuyutei, skipping")
                continue
            sets_seen += 1

            existing = fetch_existing_lids_for_set(tcgdex_id)
            new_count = 0
            set_warn_no_name = 0

            for card in cards:
                if not card["name_ja"]:
                    set_warn_no_name += 1
                    continue
                candidates = build_card_id_candidates(tcgdex_id, card["card_number"])
                # If ANY candidate's local_id is already in D1, skip — the
                # UPDATE consumer will handle that row.
                candidate_lids = {cid.split("-", 1)[1] for cid in candidates}
                if candidate_lids & existing:
                    continue
                # Canonical local_id for INSERT: unpadded (matches MP/SVP
                # catch-up convention). Canonical card_id: TCGdex set_id
                # uppercased + unpadded local_id.
                local_id = card["card_number"].lstrip("0") or card["card_number"]
                card_id = f"{tcgdex_id.upper()}-{local_id}"
                set_id = tcgdex_id.upper()
                image_url = card["image_url"]
                if card["price_jpy"] is not None:
                    price_usd = round(card["price_jpy"] * fx, 2)
                    pricing_obj = {
                        "price_jpy": card["price_jpy"],
                        "price_usd": price_usd,
                        "url": f"https://{IMAGE_HOST.replace('card.', '')}/sell/poc/s/{yuyutei_code}",
                        "marketplace": "yuyutei",
                        "updated_at": int(time.time()),
                    }
                    # Wrap the inner JSON in a SQL-quoted JSON-encoded
                    # string. The schema's pricing_json column is TEXT;
                    # we store the {"yuyutei": {...}} object as a JSON
                    # string, same as the UPDATE consumer's json_patch
                    # output.
                    pricing_json = json.dumps({"yuyutei": pricing_obj})
                    price_source = "yuyutei"
                else:
                    pricing_json = None
                    price_source = None

                sql = insert_sql(
                    card_id=card_id,
                    set_id=set_id,
                    local_id=local_id,
                    name_ja=card["name_ja"],
                    image_url=image_url,
                    pricing_json=pricing_json,
                    price_source=price_source,
                )
                all_inserts.append((yuyutei_code, sql))
                inserted_card_ids.append(card_id)
                new_count += 1

            warn_no_name += set_warn_no_name
            print(f"  [{tcgdex_id} -> {yuyutei_code}] "
                  f"parsed={len(cards)} existing={len(existing)} "
                  f"new={new_count} skipped_no_name={set_warn_no_name}")

    print(f"\nSets: {sets_seen} parsed, {sets_skipped} skipped (not on Yuyutei)")
    print(f"Products with no h4 name (skipped): {warn_no_name}")
    print(f"INSERT statements to write: {len(all_inserts)}")
    if not all_inserts:
        print("Nothing to write.")
        return

    files = _write_batches(all_inserts)
    print(f"Wrote {len(files)} batch file(s) to {OUT_DIR}/")

    # Rollback file (written before any wrangler call so it's available
    # even if --apply crashes mid-way).
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    rollback_path = ROLLBACK_DIR / f"yuyutei_catalog_inserted_{ts}.txt"
    rollback_path.write_text("\n".join(inserted_card_ids) + "\n", encoding="utf-8")
    print(f"Rollback file: {rollback_path} ({len(inserted_card_ids)} card_ids)")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    print(f"\nApplying {len(files)} batch(es) against remote D1...")
    for i, f in enumerate(files, 1):
        print(f"   [{i}/{len(files)}] executing {f.name}...")
        result = run_wrangler(WRANGLER_BIN + [f"--file={f}", "--remote"])
        if result.returncode != 0:
            print(f"   FAIL after {WRANGLER_MAX_ATTEMPTS} attempts: "
                  f"{(result.stderr or '')[:400]}")
            print(f"   Rollback list available at {rollback_path}")
            sys.exit(1)
    print("Done.")


def _write_batches(inserts: list[tuple[str, str]]) -> list[Path]:
    """Group INSERT statements by Yuyutei setcode, then split each
    group into BATCH_SIZE-row files. File naming:
    yuyutei_catalog_<setcode>_<NNN>.sql in OUT_DIR.
    """
    by_set: dict[str, list[str]] = {}
    for setcode, sql in inserts:
        by_set.setdefault(setcode, []).append(sql)
    files: list[Path] = []
    for setcode, sqls in by_set.items():
        for i in range(0, len(sqls), BATCH_SIZE):
            chunk = sqls[i:i + BATCH_SIZE]
            idx = (i // BATCH_SIZE) + 1
            path = OUT_DIR / f"yuyutei_catalog_{setcode}_{idx:03d}.sql"
            path.write_text("\n".join(chunk) + "\n", encoding="utf-8")
            files.append(path)
    return files


if __name__ == "__main__":
    main()
