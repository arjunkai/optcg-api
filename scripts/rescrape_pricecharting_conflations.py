"""Variant-aware re-scrape of the 171 withheld PriceCharting conflations.

Background: `src/pricechartingConflations.js` lists 171 JA card_ids whose
PriceCharting price was matched to the WRONG card (the by-number match in the
shared `pokemon-japanese-promo` bucket grabbed the first "#N" it saw — e.g.
MP-3 Munkidori, XYP-3, LP-3, DPP-3, PCGP-3 all collided on
`venusaur-gb-game-boy-3`). The Worker withholds these so a wrong price never
shows.

This script tries to RECOVER the correct price by searching PriceCharting by
NAME and accepting a result ONLY when every gate passes:
  - not a box / sealed / lot / deck product (we want raw singles)
  - set match: promo sets must resolve to the promo bucket with the right
    set-token (sv/sm/xy/bw/...); vintage non-promo sets must match the
    set's known PC slug
  - number match: the product slug's trailing number == our local_id
  - name match: our name_en is slug-compatible with the product's name

Anything that doesn't pass ALL gates stays WITHHELD (honest "—"), per the
no-plausible-but-wrong-prices rail. This is intentionally conservative:
partial recovery with zero new conflations beats full recovery with any.

Polite: Scrapling Fetcher (real-browser TLS), one search/card, ~2s apart,
circuit breaker on a run of fetch failures.

Usage:
  python -m scripts.rescrape_pricecharting_conflations            # measure (no writes)
  python -m scripts.rescrape_pricecharting_conflations --emit-sql # also write apply SQL + new conflation list
  python -m scripts.rescrape_pricecharting_conflations --limit 30 # sample
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

from scripts.backfill_ptcg_prices_pricecharting import (
    SET_TO_PC_SLUG, SET_ID_TO_PC_TOKEN, _pokemon_slug, _name_slugs_compatible, _norm_num,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from scrapling.fetchers import Fetcher as _SF
except Exception as e:  # pragma: no cover
    print("Scrapling Fetcher required:", e)
    sys.exit(1)

CONFLATIONS_JS = Path("src/pricechartingConflations.js")
CARDS_JSON = Path("scratch_ja_probe/ja_index_live_check.json")
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)

BOXY = re.compile(r"box|sealed|bundle|\blot\b|\bcase\b|deck|booster|\bpack\b|jumbo|playmat|binder", re.I)
# product tail -> (name-part, trailing number, optional token). Handles:
#   pikachu-1sv-p           -> name=pikachu  num=1   token=sv
#   pretend-team-skull-pikachu-13sm-p -> name=...    num=13  token=sm
#   spiky-eared-pichu-9     -> name=...      num=9   token=None
#   litwick-100             -> name=litwick  num=100 token=None
TAIL_PROMO_RE = re.compile(r"^(.*?)-(\d+)([a-z]+)-p$", re.I)
TAIL_PLAIN_RE = re.compile(r"^(.*?)-(\d+)$")
ROW_RE = re.compile(r'<tr[^>]*id="product-(\d+)"[^>]*>(.*?)</tr>', re.S)
HREF_RE = re.compile(r'href="https://www\.pricecharting\.com/game/([^/]+)/([^"?]+)"')
PRICE_RE = re.compile(r'class="[^"]*js-price[^"]*"[^>]*>\s*\$?([\d,]+\.\d{2})')


def parse_conflation_ids() -> list[str]:
    txt = CONFLATIONS_JS.read_text(encoding="utf-8")
    return re.findall(r"'([A-Za-z0-9]+-[A-Za-z0-9]+)'", txt)


def parse_tail(tail: str):
    tail = tail.lower()
    m = TAIL_PROMO_RE.match(tail)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = TAIL_PLAIN_RE.match(tail)
    if m:
        return m.group(1), m.group(2), None
    return tail, None, None


def expected_promo_token(set_id: str):
    tok = SET_ID_TO_PC_TOKEN.get(set_id.upper())
    if not tok:
        return None
    return {tok, "s"} if tok == "swsh" else {tok}


def search_results(name: str) -> list[dict]:
    """Return parsed product rows from a PC name search."""
    url = "https://www.pricecharting.com/search-products?q=" + urllib.parse.quote(name) + "&type=prices"
    r = _SF.get(url, stealthy_headers=True, timeout=30)
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}")
    html = r.html_content
    out = []
    for pid, body in ROW_RE.findall(html):
        h = HREF_RE.search(body)
        if not h:
            continue
        set_slug, tail = h.group(1), h.group(2)
        pm = PRICE_RE.search(body)
        price = float(pm.group(1).replace(",", "")) if pm else None
        out.append({"product_id": pid, "set_slug": set_slug, "tail": tail, "price": price})
    return out


def evaluate(card: dict, results: list[dict]) -> dict | None:
    """Return the single gated match for this card, or None (withhold)."""
    set_id = (card["set_id"] or "").upper()
    our_lid = _norm_num(card["local_id"])
    our_name = _pokemon_slug(card.get("name_en") or card.get("name") or "")
    promo_tokens = expected_promo_token(set_id)
    vintage_slug = SET_TO_PC_SLUG.get(set_id) or SET_TO_PC_SLUG.get(card["set_id"])

    passed = []
    for r in results:
        if r["price"] is None or r["price"] <= 0:
            continue
        if BOXY.search(r["tail"]):
            continue
        name_part, num, token = parse_tail(r["tail"])
        if num is None or _norm_num(num) != our_lid:
            continue
        # name gate
        if our_name and name_part:
            if not _name_slugs_compatible(our_name, name_part.strip("-")):
                continue
        # set gate
        if promo_tokens:
            if r["set_slug"] != "pokemon-japanese-promo":
                continue
            # modern promo: require token match. vintage-tail promo (no token):
            # rely on the name+number gate (token unknowable from slug).
            if token is not None and token.lower() not in promo_tokens:
                continue
        elif vintage_slug:
            if r["set_slug"] != vintage_slug:
                continue
        else:
            # set we don't have a PC mapping for -> can't verify set -> withhold
            continue
        passed.append({**r, "name_part": name_part, "num": num, "token": token})

    if not passed:
        return None
    # de-dup by product_id; require unambiguous (one distinct product)
    distinct = {p["product_id"]: p for p in passed}
    if len(distinct) != 1:
        # ambiguous -> withhold rather than guess
        return None
    best = next(iter(distinct.values()))
    return {
        "card_id": card["id"],
        "set_id": set_id,
        "local_id": card["local_id"],
        "name_en": card.get("name_en"),
        "price_usd": round(best["price"], 2),
        "pc_url": f"https://www.pricecharting.com/game/{best['set_slug']}/{best['tail']}",
        "pc_product_id": best["product_id"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start", type=int, default=0)
    args = ap.parse_args()

    ids = parse_conflation_ids()
    cards = {c["id"]: c for c in json.load(open(CARDS_JSON, encoding="utf-8"))["data"]}
    targets = [cards[i] for i in ids if i in cards]
    missing = [i for i in ids if i not in cards]
    if missing:
        print(f"note: {len(missing)} conflation ids not in dump (skipped): {missing[:6]}...")
    targets = targets[args.start:]
    if args.limit:
        targets = targets[:args.limit]
    print(f"{len(targets)} conflation cards to re-check via PC name search\n")

    recovered, withheld = [], []
    consec_err = 0
    for i, c in enumerate(targets):
        name = c.get("name_en") or c.get("name") or ""
        try:
            res = search_results(name)
            consec_err = 0
        except Exception as e:
            consec_err += 1
            print(f"  {c['id']}: search ERR {e}", file=sys.stderr)
            if consec_err >= 5:
                print(f"\n!! CIRCUIT BREAKER at index {args.start+i}; stopping.", file=sys.stderr)
                break
            time.sleep(3.0)
            continue
        m = evaluate(c, res)
        if m:
            recovered.append(m)
            print(f"  [OK ] {c['id']:10} {name[:20]:20} -> ${m['price_usd']:>8.2f}  {m['pc_url'].split('/game/')[1]}")
        else:
            withheld.append(c["id"])
        time.sleep(2.0)

    n = len(recovered) + len(withheld)
    print(f"\nRECOVERED {len(recovered)} / {n} checked ({100*len(recovered)/n:.1f}%); {len(withheld)} stay withheld")

    rep = OUT_DIR / "pc_conflation_rescrape.json"
    rep.write_text(json.dumps({"recovered": recovered, "withheld": withheld}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report -> {rep}")

    if args.emit_sql and recovered:
        lines = [f"-- PC conflation re-scrape: {len(recovered)} verified corrected prices"]
        for m in recovered:
            cid = m["card_id"].replace("'", "''")
            block = json.dumps({"market": m["price_usd"], "url": m["pc_url"], "productId": m["pc_product_id"]}).replace("'", "''")
            lines.append(
                "UPDATE ptcg_cards SET price_source='pricecharting', "
                f"pricing_json=json_set(COALESCE(pricing_json,'{{}}'),'$.pricecharting',json('{block}')) "
                f"WHERE card_id='{cid}' AND lang='ja' AND (price_source IS NULL OR price_source='cardmarket');"
            )
        sqlf = OUT_DIR / "pc_conflation_rescrape.sql"
        sqlf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"SQL -> {sqlf}")
        still = sorted(set(parse_conflation_ids()) - {m["card_id"] for m in recovered})
        print(f"NOTE: after applying, regenerate pricechartingConflations.js to the remaining {len(still)} ids.")


if __name__ == "__main__":
    main()
