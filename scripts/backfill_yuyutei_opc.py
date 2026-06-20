"""
Japanese OPTCG price backfill via Yuyutei (yuyu-tei.jp/sell/opc).

Populates cards.price_ja / price_source_ja / price_updated_at_ja (migration 016)
for the One Piece JA cards (the ones with a card_translations lang='ja' row).
NEVER touches the English price (cards.price / price_source) — showing an EN/USD
price on a JA card is a plausible-but-wrong price, so JA display reads price_ja
and shows nothing when it's NULL.

Matching (conflation-safe — the whole point):
  Yuyutei lists base + parallels separately. The number span carries the full
  card id (OP16-118); base rows have a bare rarity (SEC/SR/R/UC/C/L), parallels
  have a P- prefix or the SP/TR rarities or "(パラレル)" in the name.

  Per (set, number):
    * BASE:  one Yuyutei base row + one D1 base id  -> match.
    * PARALLEL: exactly one Yuyutei parallel row AND exactly one D1 parallel id
      (_pN / _rN) for that number -> match. We do NOT know which Yuyutei row
      maps to _p1 vs _p2, so 2+ on either side is ambiguous and SKIPPED (price
      base only). Mis-mapping a 19,800円 alt onto the 420円 base is exactly the
      conflation we refuse to ship.

Trust gates (no plausible-but-wrong prices):
  * IN-STOCK ONLY. Sold-out rows still show a (stale, often inflated) ask — e.g.
    OP16-065's 498,000円 sold-out phantom. Skipped.
  * PARSE CEILING ($50k): above this, treat as a parse/data error and drop.
  * CHASE THRESHOLD ($300): real One Piece chase cards exceed this, but so do
    residual conflations, so >$300 is NOT auto-written. Those ids are emitted to
    data/backfill/yuyutei_opc/chase_review.json for a manual cross-check
    (Cardrush / Fullahead / eBay-JP-sold) and curation in the manual overrides
    file. Pass --include-chase to write them anyway (after you've spot-checked).
  * MANUAL OVERRIDES win. data/manual_prices_ja_opc.json {card_id: usd | {price_usd,...}}
    is written with price_source_ja='manual' and is never clobbered by a yuyutei run.

Idempotent: the yuyutei UPDATE only writes rows where price_source_ja IS NULL or
already 'yuyutei', so re-runs refresh yuyutei prices and never stomp manual ones.

Usage:
  python -m scripts.backfill_yuyutei_opc --measure              # coverage report, no SQL
  python -m scripts.backfill_yuyutei_opc --dry-run              # build SQL, don't apply
  python -m scripts.backfill_yuyutei_opc --dry-run --set=op16   # one set
  python -m scripts.backfill_yuyutei_opc --apply                # build + apply to remote D1
  python -m scripts.backfill_yuyutei_opc --apply --use-cached   # reuse last scrape
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

from scripts.lib.yuyutei_opc_scraper import (
    LISTING_BASE,
    OPC_SET_CODES,
    REQ_INTERVAL_S,
    USER_AGENT,
    get_jpy_to_usd_rate,
    home_image_folders,
    scrape_opc_set,
)
from scripts.wrangler_retry import run_wrangler

DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]

OUT_DIR = Path("data/backfill/yuyutei_opc")
CATALOG_CACHE = OUT_DIR / "catalog.json"
MANUAL_OVERRIDES_PATH = Path("data/manual_prices_ja_opc.json")

# Trust gates (USD).
PARSE_CEILING_USD = 50_000     # above this = parse/data error, drop entirely
CHASE_THRESHOLD_USD = 300      # above this = manual-review tier, not auto-written
MIN_USD = 0.01


def query_d1_ja_cards() -> list[dict]:
    """All JA cards (id, rarity, current price_source_ja) straight from prod D1."""
    sql = (
        "SELECT c.id, c.rarity, c.price_source_ja "
        "FROM cards c "
        "WHERE c.id IN (SELECT card_id FROM card_translations WHERE language='ja')"
    )
    result = run_wrangler(WRANGLER_BIN + ["--remote", "--json", "--command", sql])
    if result.returncode != 0:
        print(f"D1 read failed: {(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = result.stdout[result.stdout.find("["):]
    return json.loads(payload)[0]["results"]


def base_number(card_id: str) -> str:
    """Strip the _pN/_rN parallel suffix: OP16-118_p1 -> OP16-118."""
    return card_id.split("_", 1)[0]


def is_parallel_id(card_id: str) -> bool:
    return "_" in card_id


def load_manual_overrides() -> dict[str, float]:
    if not MANUAL_OVERRIDES_PATH.exists():
        return {}
    raw = json.loads(MANUAL_OVERRIDES_PATH.read_text(encoding="utf-8"))
    out: dict[str, float] = {}
    for cid, v in raw.items():
        if cid.startswith("_"):  # allow a "_doc" comment key
            continue
        out[cid] = float(v["price_usd"]) if isinstance(v, dict) else float(v)
    return out


def scrape_catalog(only_set: str | None) -> dict[str, list[dict]]:
    """Scrape every opc set (or one) into {setcode: [rows]}. Polite 1s delay."""
    sets = [only_set] if only_set else OPC_SET_CODES
    catalog: dict[str, list[dict]] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=25.0,
                      follow_redirects=True) as client:
        for i, code in enumerate(sets, 1):
            time.sleep(REQ_INTERVAL_S)
            rows = scrape_opc_set(client, code)
            if rows is None:
                print(f"  [{i:>2}/{len(sets)}] {code:7s} not on Yuyutei (404)")
                continue
            catalog[code] = rows
            print(f"  [{i:>2}/{len(sets)}] {code:7s} {len(rows):>4} rows", flush=True)
    return catalog


def listing_url_for(number: str) -> str:
    """The canonical Yuyutei set-listing page for a card number, derived from the
    id prefix (OP14-084 -> .../s/op14, DON-001 -> .../s/don). Used for spot-check
    links so they point at the card's home set, not whichever page it cross-listed
    on (Yuyutei features SP cards from other sets on a set's page)."""
    return f"{LISTING_BASE}/{number.split('-', 1)[0].lower()}"


def match(catalog: dict[str, list[dict]], d1_cards: list[dict], fx: float,
          include_chase: bool) -> dict:
    """Produce matches + a full accounting of why each row was/wasn't priced.

    Yuyutei cross-lists featured SP cards from other sets on a set's page, so a
    card can appear on multiple pages. We therefore flatten ALL scraped rows,
    dedup by the globally-unique Yuyutei product id, and group by card_number
    GLOBALLY before matching — never per-page (per-page double-counts and falsely
    flags a card's base as missing just because that one page only showed its SP)."""
    # D1 index: base_number -> {'base': id|None, 'parallels': [ids]}
    d1_by_num: dict[str, dict] = defaultdict(lambda: {"base": None, "parallels": [], "rarity": {}})
    d1_ids = set()
    manual_locked = set()  # ids already price_source_ja='manual' — never auto-write
    for c in d1_cards:
        cid = c["id"]
        d1_ids.add(cid)
        grp = d1_by_num[base_number(cid)]
        grp["rarity"][cid] = c["rarity"]
        if is_parallel_id(cid):
            grp["parallels"].append(cid)
        else:
            grp["base"] = cid
        if c.get("price_source_ja") == "manual":
            manual_locked.add(cid)

    # Flatten + dedup by image_url (the globally-unique product key — the Yuyutei
    # `pid` in the image filename is only unique WITHIN a set, so two unrelated
    # cards on different set pages share a pid; the full image path embeds the
    # card's home set and is identical for a cross-listed copy).
    #
    # HOME-SET FILTER: a card's canonical printing lives on its home set page,
    # and its image is served from that set's folder (.../opc/100_140/op01/..).
    # The SAME card number is reused for reprints in other sets — PRB compilations
    # ("シャンクス(刻印あり)(PRB)"), starter-deck reprints ("(ホロなし)"), featured SP
    # reprints — each a DIFFERENT physical card at a different price. We keep only
    # rows whose image folder matches the number's set prefix, so a reprint's price
    # never lands on the original card. (Cross-listed SP cards still match: their
    # image folder is their own home set regardless of which page listed them.)
    img_set_re = __import__("re").compile(r"/opc/\d+_\d+/([a-z0-9]+)/")
    seen_img: set[str] = set()
    yt_by_num: dict[str, dict] = defaultdict(lambda: {"base": [], "parallels": []})
    dropped_reprint = 0
    for rows in catalog.values():
        for r in rows:
            img = r.get("image_url")
            if img:
                if img in seen_img:
                    continue
                seen_img.add(img)
            m = img_set_re.search(img or "")
            if m and m.group(1) not in home_image_folders(r["card_number"]):
                dropped_reprint += 1
                continue  # reprint of this number in another set — not the base card
            key = "parallels" if r["is_parallel"] else "base"
            yt_by_num[r["card_number"]][key].append(r)

    matches: list[dict] = []          # {card_id, usd, jpy, setcode, name_ja, url}
    chase: list[dict] = []            # >$300, held for manual review
    skipped = defaultdict(int)        # reason -> count
    skipped_examples = defaultdict(list)

    for num, yt in yt_by_num.items():
        d1 = d1_by_num.get(num)

        def consider(card_id: str | None, yt_rows: list[dict], kind: str):
            if not card_id:
                skipped["no_d1_id"] += 1
                return
            if card_id in manual_locked:
                skipped["manual_locked"] += 1
                return
            # only one Yuyutei row should remain for a clean match
            if len(yt_rows) != 1:
                skipped[f"ambiguous_{kind}"] += 1
                if len(skipped_examples[f"ambiguous_{kind}"]) < 8:
                    skipped_examples[f"ambiguous_{kind}"].append(
                        f"{card_id} ({len(yt_rows)} yt rows)")
                return
            row = yt_rows[0]
            if not row["in_stock"]:
                skipped["sold_out"] += 1
                return
            if row["price_jpy"] is None:
                skipped["no_price"] += 1
                return
            usd = round(row["price_jpy"] * fx, 2)
            if usd < MIN_USD or usd > PARSE_CEILING_USD:
                skipped["parse_ceiling"] += 1
                return
            rec = {
                "card_id": card_id, "usd": usd, "jpy": row["price_jpy"],
                "name_ja": row["name_ja"], "url": listing_url_for(card_id),
                "rarity": row["rarity"], "kind": kind,
            }
            if usd > CHASE_THRESHOLD_USD and not include_chase:
                chase.append(rec)
                skipped["chase_over_300"] += 1
                return
            matches.append(rec)

        # BASE: one Yuyutei base row + the D1 base id
        consider(d1["base"] if d1 else None, yt["base"], "base")

        # PARALLEL: exactly one on each side
        d1_par = d1["parallels"] if d1 else []
        if yt["parallels"]:
            if len(d1_par) == 1 and len(yt["parallels"]) == 1:
                consider(d1_par[0], yt["parallels"], "parallel")
            else:
                skipped["ambiguous_parallel_count"] += len(yt["parallels"])
                if len(skipped_examples["ambiguous_parallel_count"]) < 12:
                    skipped_examples["ambiguous_parallel_count"].append(
                        f"{num}: {len(yt['parallels'])} yt par vs {len(d1_par)} d1 par")

    skipped["reprint_other_set"] = dropped_reprint
    return {
        "matches": matches,
        "chase": chase,
        "skipped": dict(skipped),
        "skipped_examples": {k: v for k, v in skipped_examples.items()},
        "d1_ids": d1_ids,
        "manual_locked": manual_locked,
    }


def build_sql(matches: list[dict], manual: dict[str, float], d1_ids: set[str],
              fx: float) -> tuple[str, int, int]:
    ts = int(time.time())
    lines = [
        "-- Yuyutei JA price backfill for OPTCG (auto-generated).",
        f"-- Generated: {ts}  FX: 1 JPY = {fx:.6f} USD",
        "-- Idempotent: yuyutei rows only write where price_source_ja IS NULL or 'yuyutei'.",
        "-- Never touches the EN price (cards.price / price_source).",
    ]
    n_yuyutei = 0
    for m in matches:
        cid = m["card_id"].replace("'", "''")
        lines.append(
            f"UPDATE cards SET price_ja={m['usd']}, price_source_ja='yuyutei', "
            f"price_updated_at_ja={ts} "
            f"WHERE id='{cid}' AND (price_source_ja IS NULL OR price_source_ja='yuyutei');"
        )
        n_yuyutei += 1
    n_manual = 0
    for cid, usd in manual.items():
        if cid not in d1_ids:
            continue  # don't write a price for an id that isn't a JA card
        cidq = cid.replace("'", "''")
        lines.append(
            f"UPDATE cards SET price_ja={round(float(usd), 2)}, price_source_ja='manual', "
            f"price_updated_at_ja={ts} WHERE id='{cidq}';"
        )
        n_manual += 1
    return "\n".join(lines) + "\n", n_yuyutei, n_manual


def report(res: dict, manual: dict, fx: float) -> None:
    matches = res["matches"]
    total_ja = len(res["d1_ids"])
    don = sum(1 for i in res["d1_ids"] if i.startswith("DON-"))
    promo = sum(1 for i in res["d1_ids"] if i.startswith("P-"))
    addressable = total_ja - don  # DON ids are synthetic; not on Yuyutei
    base_n = sum(1 for m in matches if m["kind"] == "base")
    par_n = sum(1 for m in matches if m["kind"] == "parallel")
    priced_ids = {m["card_id"] for m in matches} | {c for c in manual if c in res["d1_ids"]}

    print("\n" + "=" * 64)
    print("COVERAGE REPORT — Yuyutei JA OPTCG prices")
    print("=" * 64)
    print(f"FX: 1 JPY = {fx:.6f} USD")
    print(f"Total JA cards in D1:        {total_ja}")
    print(f"  of which DON (synthetic):  {don}  (not sold as singles -> excluded)")
    print(f"  of which Promo (P-*):      {promo}  (range-filtered on Yuyutei -> separate pass)")
    print(f"Addressable (ex-DON):        {addressable}")
    print("-" * 64)
    print(f"Matched (auto-write):        {len(matches)}")
    print(f"  base:                      {base_n}")
    print(f"  parallel:                  {par_n}")
    print(f"Manual overrides in D1:      {sum(1 for c in manual if c in res['d1_ids'])}")
    print(f"Total priced:                {len(priced_ids)}  "
          f"({100*len(priced_ids)/max(1,addressable):.1f}% of addressable, "
          f"{100*len(priced_ids)/max(1,total_ja):.1f}% of all JA)")
    print("-" * 64)
    print("Skipped (with reason):")
    for reason, n in sorted(res["skipped"].items(), key=lambda x: -x[1]):
        print(f"  {reason:24s} {n}")
    print(f"Chase (>${CHASE_THRESHOLD_USD}, held for manual review): {len(res['chase'])}")
    if res["skipped_examples"]:
        print("\nAmbiguity examples (skipped to avoid conflation):")
        for k, ex in res["skipped_examples"].items():
            print(f"  [{k}] {', '.join(ex[:6])}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true", help="Coverage report only, no SQL")
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't apply")
    ap.add_argument("--apply", action="store_true", help="Build SQL and apply to remote D1")
    ap.add_argument("--set", dest="only_set", help="Only this Yuyutei setcode (e.g. op16)")
    ap.add_argument("--use-cached", action="store_true", help="Reuse last scraped catalog")
    ap.add_argument("--include-chase", action="store_true",
                    help="Auto-write >$300 matches too (spot-check them first!)")
    args = ap.parse_args()
    if not (args.measure or args.dry_run or args.apply):
        ap.error("pick one of --measure / --dry-run / --apply")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.use_cached and CATALOG_CACHE.exists():
        print(f"Reusing cached catalog {CATALOG_CACHE}")
        catalog = json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))
        if args.only_set:
            catalog = {args.only_set: catalog.get(args.only_set, [])}
    else:
        print(f"Scraping Yuyutei opc ({'1 set' if args.only_set else f'{len(OPC_SET_CODES)} sets'})...")
        catalog = scrape_catalog(args.only_set)
        if not args.only_set:
            CATALOG_CACHE.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    fx = get_jpy_to_usd_rate()
    print(f"\nFX rate: 1 JPY = {fx:.6f} USD")

    print("Pulling JA cards from D1...")
    d1_cards = query_d1_ja_cards()
    print(f"  {len(d1_cards)} JA cards")

    manual = load_manual_overrides()
    res = match(catalog, d1_cards, fx, args.include_chase)
    report(res, manual, fx)

    # spot-check sample (5 priciest auto-writes + 5 mid) with verify URLs
    matches = sorted(res["matches"], key=lambda m: -m["usd"])
    sample = matches[:5] + matches[len(matches) // 2: len(matches) // 2 + 5]
    print("\nSPOT-CHECK these against the live page before trusting the run:")
    for m in sample:
        print(f"  {m['card_id']:16s} {m['rarity']:6s} ¥{m['jpy']:>7,} -> ${m['usd']:<8} "
              f"{m['name_ja']}  {m['url']}")

    # always persist matches + chase list for auditing
    (OUT_DIR / "matches.json").write_text(
        json.dumps(res["matches"], ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "chase_review.json").write_text(
        json.dumps(res["chase"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_DIR}/matches.json ({len(res['matches'])}), "
          f"chase_review.json ({len(res['chase'])})")

    if args.measure:
        return

    sql, n_y, n_m = build_sql(res["matches"], manual, res["d1_ids"], fx)
    sql_path = OUT_DIR / "yuyutei_opc.sql"
    sql_path.write_text(sql, encoding="utf-8")
    print(f"SQL written: {sql_path}  ({n_y} yuyutei + {n_m} manual UPDATEs)")

    if args.dry_run:
        print("--dry-run: not applying.")
        return

    print("Applying to remote D1...")
    result = run_wrangler(WRANGLER_BIN + ["--remote", f"--file={sql_path}"])
    if result.returncode != 0:
        print(f"Apply failed: {(result.stderr or '')[:400]}")
        sys.exit(1)
    print("Done.")


if __name__ == "__main__":
    main()
