"""
Last-resort price source for Pokemon cards: query eBay's Browse API for
each card without a price, take a consensus median of active listings,
write to D1. EN cards search EBAY_US (USD direct), JA cards search
EBAY_JP (JPY, FX-converted to USD via frankfurter.app).

Mirrors the OPTCG `backfill_prices_ebay.py` flow — same OAuth, same
trimmed-median consensus, same blocklist — adapted for Pokemon's
multi-marketplace + multi-language reality.

Does NOT overwrite manual / pokemontcg / cardmarket prices. Only writes
to rows where the existing price_source is NULL or 'cardmarket'
(cardmarket is EUR-only TCGdex baseline, eBay USD is more authoritative
for chase-card valuation). Every write stamps price_source='ebay_us' or
'ebay_jp' so it's auditable and rollback-able:

    -- rollback (per marketplace):
    wrangler d1 execute optcg-cards --remote \\
        --command "UPDATE ptcg_cards SET price_source=NULL,
                   pricing_json=json_remove(pricing_json,'$.ebay')
                   WHERE price_source='ebay_us'"

Requires env vars EBAY_APP_ID + EBAY_CERT_ID (set as GitHub secrets for
the weekly workflow; set locally for dev runs). Optional
CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID for the D1 HTTP query path
— if absent, falls back to wrangler CLI.

Usage (run as a module from optcg-api root so scripts.ebay_client resolves):
    python -m scripts.backfill_ptcg_prices_ebay --lang=en
    python -m scripts.backfill_ptcg_prices_ebay --lang=ja
    python -m scripts.backfill_ptcg_prices_ebay --lang=en --limit=10 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from statistics import median as stat_median
from typing import Iterable

import httpx

from scripts.ebay_client import (
    EbayClient, EbayAccessDeniedError, apply_title_filters, consensus_price,
)


# Pokemon Individual Cards on eBay US.
EBAY_CATEGORY_POKEMON = "183454"

# eBay Browse filter applied to every search. Excludes used/damaged
# condition + auctions, both of which add price noise we'd then have to
# trim out via consensus median. Listing pool quality matters more than
# pool size for chase cards. Per docs/ebay-apis.md (Browse API section).
BROWSE_FILTER = "conditions:{NEW|LIKE_NEW},buyingOptions:{FIXED_PRICE}"
DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Two-day FX rate cache. ECB-backed via frankfurter.app — no auth, free,
# rate-limited politely. Refreshes on first call after the cache file is
# older than this.
FX_CACHE_PATH = Path("data/.fx_jpy_usd.json")
FX_CACHE_TTL_S = 2 * 24 * 60 * 60

# Pokemon-specific blocklist on top of the shared default. Pokemon cards
# attract MORE noise than One Piece (proxy farms, lots, graded resales,
# damaged singles passing as mint).
POKEMON_TITLE_BLOCKLIST: tuple[str, ...] = (
    "proxy", "custom art", "fan made", "fan-made", "replica", "fake",
    "not authentic", "art only", "fanart", "fan art",
    "lot", "bulk", "bundle", "box of", "complete set", "playset",
    "damaged", "creased", "miscut", "misprint", "error",
    "grading", "submission",  # ungraded "ready for grading" listings, distinct from PSA-graded singles
    "stamped",  # Tournament-stamped variants are differently priced
)

# eBay JP site search expects JPY in price.value. We FX-convert before
# storing so the frontend always sees USD.
JPY_TO_USD_FALLBACK = 0.0067  # last-resort hardcoded rate ≈ ¥150/$1

# Resumable-cursor knobs. Cards are processed in card_id order; every
# CURSOR_FLUSH_EVERY cards we UPSERT the cursor row in D1 so that if
# GitHub Actions kills the job for hitting the 6h ceiling, next week's
# run resumes from the latest flushed position. On reaching the end of
# the unpriced list, the cursor is cleared (last_card_id=NULL) so the
# following run starts fresh and revisits any cards that returned
# 'no consensus' last cycle.
CURSOR_FLUSH_EVERY = 100


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["en", "ja"], required=True,
                    help="Card language to backfill (en→EBAY_US, ja→EBAY_JP)")
    ap.add_argument("--dry-run", action="store_true", help="Build SQL, don't run it")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of cards queried (smoke tests)")
    ap.add_argument("--min-count", type=int, default=5,
                    help="Min listings required for consensus (default: 5)")
    ap.add_argument("--shuffle", action="store_true",
                    help="Randomize card order before --limit. Recommended for "
                         "smoke tests so the sample isn't biased toward the "
                         "alphabetically-first cohort (vintage ADV1/ADV3 etc).")
    args = ap.parse_args()

    # Cursor only applies in plain production mode. --shuffle randomizes
    # order so cursor pagination is meaningless; --limit is for smoke
    # tests; --dry-run never writes prices, so cursor advance would lie.
    cursor_source = f"ebay_prices_{args.lang}"
    use_cursor = not (args.shuffle or args.limit or args.dry_run)
    start_cursor = read_cursor(cursor_source) if use_cursor else None
    if use_cursor:
        if start_cursor:
            print(f"   resume cursor: card_id > '{start_cursor}'")
        else:
            print(f"   no cursor — starting from beginning")

    print(f"1. Querying D1 for {args.lang} cards without price...")
    cards = query_unpriced_cards(args.lang, after_card_id=start_cursor if use_cursor else None)
    if args.shuffle:
        import random
        random.shuffle(cards)
    if args.limit:
        cards = cards[:args.limit]
    print(f"   {len(cards)} cards in scope\n")
    if not cards:
        print("Nothing to backfill. Exiting.")
        # Cursor reached end-of-list (or list was empty to start). Reset
        # so next run revisits everything that's still unpriced.
        if use_cursor and start_cursor:
            print(f"   clearing cursor for {cursor_source}")
            write_cursor(cursor_source, None)
        return

    print("2. Initializing eBay client...")
    client = EbayClient()
    client.get_token()
    # 2026-05-04: Browse API rejects EBAY_JP marketplace_id with HTTP 409,
    # so JA cards have to be priced from EBAY_US listings (US sellers
    # routinely relist JA cards). Cards keep the price_source='ebay_jp'
    # tag because the card itself is JA — only the marketplace was US.
    # Avoiding the buggy generic-noise issue requires:
    #   1. JP→EN name translation in the query (sellers write English titles)
    #   2. Strict per-listing post-filter requiring name + card number
    #      + 'japanese'/'japan' in the title (rejects unrelated noise)
    #   3. Script-level sanity gate that aborts if multiple cards return
    #      identical medians (smoking gun for matched-noise mode)
    fx_jpy_to_usd = 1.0  # always USD (no JPY since EBAY_JP isn't supported)
    marketplace = "EBAY_US"
    target_currency = "USD"
    print(f"   Marketplace: {marketplace}, target currency: {target_currency}")
    if args.lang == "ja":
        print(f"   (JA cards searched on EBAY_US with 'japanese' keyword + strict per-listing filter)")
    print()

    print("3. Pricing cards via eBay...")
    matches: list[dict] = []
    last_processed_card_id: str | None = None
    for i, card in enumerate(cards, start=1):
        result = price_card(client, card, args.lang, marketplace, target_currency,
                            fx_jpy_to_usd, min_count=args.min_count,
                            verbose_skip=(args.dry_run and len(cards) <= 100))
        if result:
            matches.append(result)
            print(f"   [{i}/{len(cards)}] {card['card_id']}: ${result['price_usd']} "
                  f"(n={result['sample_size']}, src={result['price_source']})", flush=True)
        else:
            print(f"   [{i}/{len(cards)}] {card['card_id']}: no consensus", flush=True)
        last_processed_card_id = card["card_id"]
        # Periodic cursor flush so a 6h timeout preserves progress within
        # ~CURSOR_FLUSH_EVERY cards. Skipped in non-cursor modes.
        if use_cursor and i % CURSOR_FLUSH_EVERY == 0:
            write_cursor(cursor_source, last_processed_card_id)
        time.sleep(0.25)  # well under eBay's per-app rate limit
    print(f"\n   {len(matches)} cards priced via eBay\n")
    _save_translate_cache()

    if not matches:
        print("No prices found. Nothing to write.")
        # We attempted every card in scope and none matched. Clear the
        # cursor so the next run revisits everything (eBay listing
        # liquidity may have improved by next week).
        if use_cursor:
            print(f"   clearing cursor for {cursor_source}")
            write_cursor(cursor_source, None)
        return

    # Sanity gate — catches the "$7.47 on every card" failure mode.
    # Real Pokemon card prices have natural variance across different cards
    # in the same batch. If 5+ cards in our sample return medians within
    # $0.50 of each other, the matcher is in noise mode (matching all
    # cards to the same generic listing pool).
    if len(matches) >= 5:
        from statistics import stdev
        usd_vals = [m["price_usd"] for m in matches]
        spread = stdev(usd_vals)
        # Also check: distinct-value count. Even if stdev is high, if 80%
        # of cards return EXACTLY the same value, that's noise.
        from collections import Counter
        most_common_count = Counter(usd_vals).most_common(1)[0][1]
        max_concentration = most_common_count / len(usd_vals)
        if spread < 0.5 or max_concentration > 0.5:
            print(f"!!! SANITY GATE TRIPPED — REFUSING TO WRITE !!!")
            print(f"    matches:               {len(matches)}")
            print(f"    median price stdev:    ${spread:.2f} (need ≥ $0.50)")
            print(f"    most-common-value pct: {max_concentration*100:.0f}% (need ≤ 50%)")
            print(f"    Likely cause: card-identity post-filter failed; eBay returned")
            print(f"    generic noise that passed our relevance check. Inspect")
            print(f"    spotcheck_samples in {OUT_DIR}/ebay_ptcg_{args.lang}_matches.json")
            print(f"    before deciding whether to refine the filter or rerun.")
            matches_file = OUT_DIR / f"ebay_ptcg_{args.lang}_matches.json"
            matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"    matches dumped to: {matches_file}")
            # Cursor was advanced incrementally during the loop. Roll it
            # back to where we started so next week re-attempts the same
            # cohort instead of skipping past the suspect data.
            if use_cursor:
                print(f"    rolling cursor back to: {start_cursor or '(unset)'}")
                write_cursor(cursor_source, start_cursor)
            sys.exit(2)

    sql_lines = build_update_sql(matches)
    sql_file = OUT_DIR / f"ebay_ptcg_{args.lang}.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / f"ebay_ptcg_{args.lang}_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"4. SQL written to {sql_file}")
    print(f"   Matches written to {matches_file}")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution")
        return

    print(f"\n5. Executing {len(sql_lines)} UPDATEs against remote D1...")
    result = subprocess.run(
        WRANGLER_BIN + ["--remote", f"--file={sql_file}"],
    )
    if result.returncode != 0:
        print("Execute failed.")
        sys.exit(result.returncode)
    # Reached end of the unpriced cohort and successfully wrote the
    # batch — clear the cursor so next week starts fresh and revisits
    # any 'no consensus' rows that might have liquidity now.
    if use_cursor:
        print(f"   clearing cursor for {cursor_source}")
        write_cursor(cursor_source, None)
    print("Done.")


def query_unpriced_cards(lang: str, after_card_id: str | None = None) -> list[dict]:
    """Returns [{card_id, name, set_id, local_id}, ...] for cards in the
    given lang that don't have a useful price. We treat null and
    cardmarket-EUR as 'priced gap' since eBay USD beats those for
    chase-card valuation. ORDER BY card_id so a cursor advancing on
    last_card_id can paginate cleanly across timed-out runs."""
    where_extra = ""
    if after_card_id:
        cid = after_card_id.replace("'", "''")
        where_extra = f" AND card_id > '{cid}'"
    sql = (
        f"SELECT card_id, name, set_id, local_id FROM ptcg_cards WHERE lang = '{lang}' "
        f"AND (price_source IS NULL OR price_source = 'cardmarket'){where_extra} "
        "ORDER BY card_id"
    )
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        print("wrangler query failed:", (out.stderr or "")[:500])
        sys.exit(1)
    payload_start = (out.stdout or "").find("[")
    if payload_start < 0:
        return []
    payload = json.loads(out.stdout[payload_start:])
    rows = payload[0].get("results", []) if isinstance(payload, list) else payload.get("results", [])
    return rows or []


def read_cursor(source: str) -> str | None:
    """Returns last_card_id for `source` from ptcg_backfill_cursor, or
    None if no row exists or last_card_id IS NULL. Failures (network,
    table missing) are non-fatal — we just start from the beginning."""
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command",
            f"SELECT last_card_id FROM ptcg_backfill_cursor WHERE source = '{source}'"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        return None
    payload_start = (out.stdout or "").find("[")
    if payload_start < 0:
        return None
    try:
        payload = json.loads(out.stdout[payload_start:])
        rows = payload[0].get("results", []) if isinstance(payload, list) else []
        if rows and rows[0].get("last_card_id"):
            return rows[0]["last_card_id"]
    except (ValueError, KeyError, IndexError):
        return None
    return None


def write_cursor(source: str, card_id: str | None) -> None:
    """UPSERT cursor for `source`. card_id=None clears it so the next
    run starts from the beginning of the unpriced list. Failures here
    are silent — losing a cursor flush just means the next run restarts
    a small chunk earlier."""
    if card_id is None:
        sql = f"DELETE FROM ptcg_backfill_cursor WHERE source = '{source}'"
    else:
        cid = card_id.replace("'", "''")
        ts = int(time.time())
        sql = (
            f"INSERT INTO ptcg_backfill_cursor (source, last_card_id, updated_at) "
            f"VALUES ('{source}', '{cid}', {ts}) "
            f"ON CONFLICT(source) DO UPDATE SET "
            f"last_card_id = excluded.last_card_id, "
            f"updated_at = excluded.updated_at"
        )
    subprocess.run(
        WRANGLER_BIN + ["--remote", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


_JP_EN_CACHE: dict | None = None
_CARD_ID_EN_CACHE: dict | None = None


def _load_jp_en_maps() -> tuple[dict, dict]:
    """Lazy-load JP species map + card_id→EN canonical name overrides."""
    global _JP_EN_CACHE, _CARD_ID_EN_CACHE
    if _JP_EN_CACHE is None:
        try:
            _JP_EN_CACHE = json.loads(
                Path("data/jp_to_en_pokemon.json").read_text(encoding="utf-8")
            )
        except Exception:
            _JP_EN_CACHE = {}
    if _CARD_ID_EN_CACHE is None:
        try:
            _CARD_ID_EN_CACHE = json.loads(
                Path("data/ja_card_id_to_en_name.json").read_text(encoding="utf-8")
            )
        except Exception:
            _CARD_ID_EN_CACHE = {}
    return _JP_EN_CACHE, _CARD_ID_EN_CACHE


_TRANSLATE_CACHE_PATH = Path("data/.ebay_translate_cache.json")
_translate_cache: dict[str, str] | None = None


def _load_translate_cache() -> dict[str, str]:
    global _translate_cache
    if _translate_cache is None:
        try:
            _translate_cache = json.loads(_TRANSLATE_CACHE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _translate_cache = {}
    return _translate_cache


def _save_translate_cache() -> None:
    if _translate_cache is None:
        return
    _TRANSLATE_CACHE_PATH.write_text(
        json.dumps(_translate_cache, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _to_en_name(card: dict, client: EbayClient | None = None) -> str | None:
    """For a JA card, return the best English-name guess for eBay search.
    Returns None if we can't translate — caller skips the card rather than
    submitting a query that would match generic noise.
    Priority: card_id-keyed canonical override → JP-species-name lookup →
    eBay Commerce Translation fallback (cached on disk).

    Translation requires the commerce.translation scope to be enabled on
    the eBay keyset. If the scope returns invalid_scope, the fallback
    quietly skips and the caller's verification filter still gates the
    result downstream."""
    jp_en, cid_en = _load_jp_en_maps()
    cid = card.get("card_id") or ""
    if cid in cid_en and cid_en[cid]:
        return cid_en[cid]
    jp_name = (card.get("name") or "").strip()
    base = re.split(r"[(（\s]", jp_name)[0]
    if base in jp_en:
        return jp_en[base]

    # Last-resort: eBay translation. Cached per JP name on disk so a
    # weekly re-run doesn't re-translate the same 1500 unmapped names.
    if not client or not jp_name:
        return None
    cache = _load_translate_cache()
    if jp_name in cache:
        return cache[jp_name] or None
    try:
        translated = client.translate([jp_name], from_lang="ja", to_lang="en")
        candidate = (translated[0] or "").strip() if translated else ""
    except (EbayAccessDeniedError, RuntimeError, httpx.HTTPError):
        # Scope not enabled or transient failure. Skip rather than retry.
        cache[jp_name] = ""
        return None
    # Sanity: an empty/single-character translation is almost certainly
    # garbage. Cache the empty result so we don't retry forever.
    if len(candidate) < 2:
        cache[jp_name] = ""
        return None
    cache[jp_name] = candidate
    return candidate


# Acceptable forms of a card number in eBay listing titles. Sellers
# write the local_id in many ways: "025/126", "25/126", " 25 ", "#25",
# "No. 25", "125/126" (no slash). The list is matched case-insensitive
# against the title text.
def _number_forms(local_id: str) -> list[str]:
    if not local_id: return []
    lid = local_id.strip()
    no_zero = lid.lstrip("0") or lid
    forms = {
        f"{lid}/", f"{no_zero}/",          # "025/" or "25/"
        f" {lid} ", f" {no_zero} ",         # " 025 " or " 25 "
        f"#{lid}", f"#{no_zero}",            # "#025" or "#25"
        f"no. {no_zero}", f"no.{no_zero}",   # "No. 25"
    }
    return list(forms)


def is_relevant_listing(title: str, en_name: str, local_id: str) -> bool:
    """Per-listing relevance filter. Rejects generic noise that the eBay
    search returns when the card-specific query couldn't disambiguate.
    Requires:
      - EN name appears as a substring (case-insensitive)
      - Card number appears in one of the accepted forms
      - 'japanese' or 'japan' appears (avoids matching EN prints of the
        same Pokemon species)
    """
    if not title: return False
    t = title.lower()
    if en_name.lower() not in t:
        return False
    forms = _number_forms(local_id)
    if forms and not any(form in t for form in forms):
        return False
    if not any(j in t for j in ("japanese", "japan ", "jp ")):
        return False
    return True


def price_card(client: EbayClient, card: dict, lang: str, marketplace: str,
               target_currency: str, fx_jpy_to_usd: float, *, min_count: int,
               verbose_skip: bool = False) -> dict | None:
    # JA cards go through the strict-relevance path. Skip cards we can't
    # translate — submitting "japanese pokemon" alone would match noise.
    if lang == "ja":
        en_name = _to_en_name(card, client=client)
        if not en_name:
            if verbose_skip:
                print(f"   [skip] {card['card_id']}: no JP-to-EN translation available")
            return None
    else:
        en_name = (card.get("name") or "").strip()
        if not en_name:
            return None

    query = build_query(card, lang, en_name)
    try:
        items = client.search(
            query, limit=50,
            marketplace_id=marketplace,
            category_ids=EBAY_CATEGORY_POKEMON,
            filter_str=BROWSE_FILTER,
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"   [skip] {card['card_id']}: {exc}")
        return None

    # Step 1 — strip generic blocklist (proxy/lots/damaged/etc.)
    filtered = apply_title_filters(items, blocklist=POKEMON_TITLE_BLOCKLIST)

    # Step 2 — JA cards get the strict per-listing card-identity filter.
    # EN cards use the existing path (the legacy logic worked for EN
    # because the EN name + set code already disambiguated enough).
    if lang == "ja":
        before = len(filtered)
        filtered = [
            it for it in filtered
            if is_relevant_listing(it.get("title", ""), en_name, card.get("local_id", ""))
        ]
        if verbose_skip and before > 0:
            print(f"   [{card['card_id']}] {before} hits → {len(filtered)} after relevance filter")

    median, sample_size = consensus_price(filtered, min_count=min_count, currency=target_currency)
    if median is None:
        return None

    # Sample listings for offline spot-check (URL + title).
    samples = []
    for it in filtered[:5]:
        samples.append({
            "title": (it.get("title") or "")[:120],
            "price": it.get("price", {}).get("value"),
            "url": it.get("itemWebUrl") or it.get("itemUrl"),
        })

    price_usd = median if target_currency == "USD" else median * fx_jpy_to_usd
    derived_source = "ebay_us" if lang == "en" else "ebay_jp"
    return {
        "card_id": card["card_id"],
        "lang": lang,
        "price_native": round(median, 2),
        "price_currency": target_currency,
        "price_usd": round(price_usd, 2),
        "sample_size": sample_size,
        "price_source": derived_source,
        "query_used": query,
        "en_name_used": en_name,
        "spotcheck_samples": samples,
    }


def build_query(card: dict, lang: str, en_name: str) -> str:
    """Card-specific eBay search query, both langs target EBAY_US.
    EN: "{name} {setCode} pokemon"
    JA: '"{en_name}" {local_id} japanese pokemon' — quoted name forces
        the search to keep our Pokemon as the head subject; local_id
        narrows to the specific print; 'japanese' filters out EN sellers.
    """
    set_code = card["set_id"] or card["card_id"].split("-")[0]
    if lang == "en":
        return f"{en_name} {set_code} pokemon"
    lid = (card.get("local_id") or "").lstrip("0") or card.get("local_id") or ""
    if lid:
        return f'"{en_name}" {lid} japanese pokemon'
    return f'"{en_name}" japanese pokemon {set_code}'


def build_update_sql(matches: list[dict]) -> list[str]:
    """One UPDATE per card. price_source flips to ebay_us or ebay_jp;
    pricing_json gets a new {ebay: {...}} key with the native price,
    sample size, and FX rate (if JP). Manual rows are guarded by the
    WHERE clause."""
    lines = []
    for m in matches:
        card_id = m["card_id"].replace("'", "''")
        lang = m["lang"]
        price_source = m["price_source"]
        price_usd = m["price_usd"]
        ebay_payload = json.dumps({
            "price_usd": price_usd,
            "price_native": m["price_native"],
            "currency": m["price_currency"],
            "sample_size": m["sample_size"],
            "marketplace": "EBAY_US" if price_source == "ebay_us" else "EBAY_JP",
            "updated_at": int(time.time()),
        }).replace("'", "''")
        # Patch into pricing_json under .ebay; flip price_source unless
        # it's already 'manual' (manual always wins).
        lines.append(
            "UPDATE ptcg_cards SET "
            f"pricing_json = json_patch(COALESCE(pricing_json, '{{}}'), json_object('ebay', json('{ebay_payload}'))), "
            f"price_source = CASE WHEN price_source = 'manual' THEN 'manual' ELSE '{price_source}' END "
            f"WHERE card_id = '{card_id}' AND lang = '{lang}' "
            "AND (price_source IS NULL OR price_source = 'cardmarket');"
        )
    return lines


def get_jpy_to_usd_rate() -> float:
    """Read JPY→USD from frankfurter.app (free, ECB-backed, no auth).
    Caches in data/.fx_jpy_usd.json for FX_CACHE_TTL_S. Falls back to
    a hardcoded rate if the API is down."""
    if FX_CACHE_PATH.exists():
        try:
            cached = json.loads(FX_CACHE_PATH.read_text())
            if time.time() - cached.get("ts", 0) < FX_CACHE_TTL_S:
                return float(cached["rate"])
        except Exception:
            pass
    try:
        with urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=JPY&to=USD",
            timeout=10,
        ) as r:
            data = json.loads(r.read())
        rate = float(data["rates"]["USD"])
        FX_CACHE_PATH.write_text(json.dumps({"rate": rate, "ts": time.time()}))
        return rate
    except Exception as exc:
        print(f"  FX fetch failed ({exc}); using fallback {JPY_TO_USD_FALLBACK}")
        return JPY_TO_USD_FALLBACK


if __name__ == "__main__":
    main()
