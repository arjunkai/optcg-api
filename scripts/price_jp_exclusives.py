"""
price_jp_exclusives.py — prices the JP-exclusive variants seeded from
data/jp_exclusives.json using eBay Browse, then writes to D1 with
price_source='ebay_jp'.

Why a separate script from backfill_prices_ebay.py:
  The generic backfill builds queries like "{name} {set_code} One Piece"
  which matches any Luffy promo, not specifically a Championship variant.
  For JP exclusives the base name is too common; the `note` field in the
  JSON ("2024 One Piece Championship Prize") is the discriminator that
  filters eBay listings to the right product. This script threads that
  note into the search query so we get listings for the actual variant.

Usage (from repo root):
  python -m scripts.price_jp_exclusives [--dry-run] [--local]

Environment:
  EBAY_APP_ID, EBAY_CERT_ID (same vars as backfill_prices_ebay.py)

Rollback:
  wrangler d1 execute optcg-cards --remote \
    --command "UPDATE cards SET price=NULL, price_source=NULL, \
               price_updated_at=NULL WHERE price_source='ebay_jp'"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.ebay_client import (
    EbayClient,
    apply_title_filters,
    consensus_price,
)


WRANGLER_CMD = ["npx", "wrangler", "d1", "execute", "optcg-cards"]
JSON_PATH = Path("data/jp_exclusives.json")


def load_entries() -> list[dict]:
    """Read the JP exclusives JSON and return a list of entries with id."""
    blob = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    entries = []
    for key, val in blob.items():
        if key.startswith("_"):
            continue
        if not isinstance(val, dict):
            continue
        entries.append({"id": key, **val})
    return entries


def lookup_base_name(base_id: str, local: bool) -> str | None:
    """Ask D1 for the base card's name so we can build a real search query."""
    flag = "--local" if local else "--remote"
    result = subprocess.run(
        WRANGLER_CMD + [flag, "--json", "--command",
                        f"SELECT name FROM cards WHERE id='{base_id}'"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        rows = payload[0]["results"] if isinstance(payload, list) else payload["results"]
        return rows[0]["name"] if rows else None
    except (KeyError, IndexError, json.JSONDecodeError):
        return None


def build_query(entry: dict, base_name: str) -> str:
    """Build an eBay search that disambiguates the Championship/JP variant.

    Uses the JSON's `note` field as a discriminator — "2024 Championship
    Prize" for Championship Luffy keeps generic Luffy promos out of the
    result set. If `price_search_query` is set in the JSON, use that
    verbatim (manual override for cards the default doesn't find)."""
    if entry.get("price_search_query"):
        return entry["price_search_query"]
    # Strip punctuation from base name ("Monkey.D.Luffy" -> "Monkey D Luffy")
    clean_name = base_name.replace(".", " ").replace('"', "").strip()
    note = entry.get("note") or ""
    # Pull the most useful keywords from the note — strip language/region
    # markers that would tighten the search too much.
    note_clean = (
        note.replace("(JP)", "")
            .replace("(JPN)", "")
            .replace("(Japanese)", "")
            .strip()
    )
    return f"{clean_name} {note_clean} One Piece TCG".strip()


def price_entry(client: EbayClient, entry: dict, base_name: str,
                min_count: int) -> dict | None:
    query = build_query(entry, base_name)
    print(f"    query: {query!r}")
    try:
        items = client.search(query, limit=50)
    except RuntimeError as exc:
        print(f"    [skip] {entry['id']}: {exc}")
        return None
    filtered = apply_title_filters(items)
    median, sample_size = consensus_price(filtered, min_count=min_count)
    if median is None:
        print(f"    [no consensus] n={sample_size} (need >={min_count})")
        return None
    return {"card_id": entry["id"], "price": round(median, 2),
            "sample_size": sample_size}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--min-count", type=int, default=3,
                    help="Minimum listings for consensus (default 3). Lower for rare cards.")
    args = ap.parse_args()

    entries = load_entries()
    print(f"JP exclusives to price: {len(entries)}\n")
    if not entries:
        print("Nothing to price.")
        return

    client = EbayClient()
    client.get_token()

    matches: list[dict] = []
    for i, entry in enumerate(entries, start=1):
        base = lookup_base_name(entry["base_id"], args.local)
        if not base:
            print(f"  [{i}/{len(entries)}] {entry['id']}: base {entry['base_id']} not in D1 — skip")
            continue
        print(f"  [{i}/{len(entries)}] {entry['id']}  (base: {base})")
        result = price_entry(client, entry, base, args.min_count)
        if result:
            matches.append(result)
            print(f"    ${result['price']} (n={result['sample_size']})")
        time.sleep(0.3)

    print(f"\nPriced {len(matches)}/{len(entries)} entries")

    if not matches:
        return

    now = int(time.time())
    lines = [
        f"UPDATE cards SET price={m['price']}, price_updated_at={now}, "
        f"price_source='ebay_jp' WHERE id='{m['card_id']}';"
        for m in matches
    ]
    sql_file = Path("scripts/jp_batches/jp_prices.sql")
    sql_file.parent.mkdir(parents=True, exist_ok=True)
    sql_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSQL written to {sql_file}")

    if args.dry_run:
        print("--dry-run: skipping D1 execution")
        return

    target = "--local" if args.local else "--remote"
    print(f"\nExecuting against {target[2:]} D1...")
    result = subprocess.run(
        WRANGLER_CMD + [target, f"--file={sql_file}"],
        shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        sys.exit(result.returncode)
    print("Done.")


if __name__ == "__main__":
    main()
