"""Measure Treca Sunrise (tcgsunrise.com) catalog overlap with our unpriced JA
vintage cards — the gate before building a full ingester (pokeca 618->12 precedent).

Read-only: crawls cat01 (Pokemon, raw singles; graded品 live in a separate
category ct109) page by page, collects (item_id, name, price_jpy), then estimates
how many of our unpriced vintage JA cards have a same-named product on Treca.

This is an UPPER-BOUND name-overlap (Treca names don't always encode set+number),
NOT a writer. A real ingester must match strictly on set+number+printing.

  python -m scripts.measure_treca_overlap            # crawl + measure
  python -m scripts.measure_treca_overlap --max-pages 60
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
from scrapling.fetchers import Fetcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CARDS_JSON = Path("scratch_ja_probe/ja_index_live_check.json")
OUT = Path("data/backfill/treca_overlap.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

# item-name <a> inner HTML can contain nested <font>【特価】</font> badges, so
# capture everything up to </a> then strip tags. Names are structured:
# 【SET】【RARITY】【NUM/TOTAL】NAME【condition】 — great for strict matching.
ITEM_RE = re.compile(r'item-name"><a href="/view/item/(\d+)[^"]*">(.*?)</a>', re.S)
PRICE_RE = re.compile(r'class="item-price[^"]*"[^>]*>\s*([0-9,]+)\s*円')
TAG_RE = re.compile(r'<[^>]+>')

VINTAGE_SETS = None  # filled in main

def norm_jp(s: str) -> str:
    # strip whitespace, full/half-width noise, keep JP/alnum for containment match
    s = re.sub(r"[\s　]+", "", s or "")
    return s

def fetch_page(page: int):
    url = f"https://tcgsunrise.com/view/category/cat01?page={page}"
    r = Fetcher.get(url, headers={"Accept-Language": "ja"}, stealthy_headers=True, timeout=25)
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}")
    h = r.html_content
    names = ITEM_RE.findall(h)
    prices = PRICE_RE.findall(h)
    out = []
    for i, (iid, raw) in enumerate(names):
        name = TAG_RE.sub("", raw).strip()           # strip nested <font> badges
        price = int(prices[i].replace(",", "")) if i < len(prices) else None
        out.append({"id": iid, "name": name, "price_jpy": price})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=120)
    args = ap.parse_args()

    # crawl cat01 until empty/duplicate/cap, with a circuit breaker
    products, seen_ids = [], set()
    consec_err = 0
    for page in range(1, args.max_pages + 1):
        try:
            items = fetch_page(page)
            consec_err = 0
        except Exception as e:
            consec_err += 1
            print(f"  page {page}: ERR {e}", file=sys.stderr)
            if consec_err >= 4:
                print("!! breaker: stopping", file=sys.stderr); break
            time.sleep(3.0); continue
        fresh = [it for it in items if it["id"] not in seen_ids]
        if not fresh:
            print(f"  page {page}: no new items -> end of catalog"); break
        for it in fresh:
            seen_ids.add(it["id"]); products.append(it)
        print(f"  page {page}: +{len(fresh)} (total {len(products)})", flush=True)
        time.sleep(2.0)
    print(f"\nTreca cat01 (Pokemon) products collected: {len(products)}")

    # our unpriced VINTAGE cards
    d = json.load(open(CARDS_JSON, encoding="utf-8"))["data"]
    TCGV = ['holofoil', 'normal', 'reverseHolofoil', '1stEdition', 'unlimited']
    def vis(c):
        p = c.get('pricing') or {}; src = c.get('price_source')
        t = p.get('tcgplayer') or {}
        if any(isinstance(t.get(v), dict) and isinstance(t[v].get('market'), (int, float)) for v in TCGV): return True
        if src in ('yuyutei','hareruya','fullahead') and isinstance((p.get(src) or {}).get('price_usd'), (int,float)): return True
        if src in ('ebay_jp','ebay_us') and isinstance((p.get('ebay') or {}).get('price_usd'), (int,float)): return True
        if src=='yahoo_sold' and isinstance((p.get('yahoo_sold') or {}).get('price_usd'), (int,float)): return True
        if src=='pricecharting' and isinstance((p.get('pricecharting') or {}).get('market'), (int,float)): return True
        cm = p.get('cardmarket') or {}
        return any(isinstance(cm.get(k),(int,float)) and cm[k]>0 for k in ('avg','trend','avg7','avg30','avg1','low'))
    def is_vintage(s):
        s=(s or '').upper()
        return (s.startswith(('PMCG','NEO','PCG','ADV','E','DP','PT','LP','VS','WEB','BS'))
                or s in ('TOPSUN','MP') or s.startswith('L'))
    gap = [c for c in d if not vis(c) and is_vintage(c.get('set_id'))]
    print(f"our unpriced vintage JA cards: {len(gap)}")

    # upper-bound name overlap: a gap card "covered" if its JP name appears as a
    # substring of some Treca product name (and the card name is >=2 chars).
    treca_names = [norm_jp(p["name"]) for p in products]
    treca_blob = "\n".join(treca_names)
    covered = []
    for c in gap:
        nm = norm_jp(c.get("name") or "")
        if len(nm) >= 2 and nm in treca_blob:
            covered.append(c["id"])
    print(f"\nUPPER-BOUND overlap: {len(covered)} / {len(gap)} gap cards have a same-name Treca product "
          f"({100*len(covered)/max(1,len(gap)):.1f}%)")
    print("(name-only; a real match also needs set+number+printing — true recoverable is <= this)")

    OUT.write_text(json.dumps({
        "treca_products": len(products),
        "gap_vintage": len(gap),
        "name_overlap": len(covered),
        "products": products[:500],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved -> {OUT}")


if __name__ == "__main__":
    main()
