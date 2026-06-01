"""
Yahoo Auctions "last sold" backfill for unpriced JA promos/vintage.

Source: aucfree.com (free Yahoo Auctions 落札/sold-price aggregator). This is
REAL transaction data with dates (the industry-standard treatment for thin-
market cards — TCGPlayer "Most Recent Sale", 130point, CardLadder). NOT an
estimate; never fabricated.

Safety / anti-conflation discipline (this is the part that matters):
  - Query by the LANGUAGE-NEUTRAL number token, e.g. "260/SV-P", not the name
    (our stored names are inconsistently EN/JA). The number+set is distinctive.
  - VERIFY each listing title actually contains that number token before using
    its price — so a search can't drift to a different card.
  - EXCLUDE graded (PSA/BGS/CGC/ARS/鑑定), lots (まとめ/セット/N枚/box), and
    English-version (英語版) listings — we want raw JA singles.
  - Require >= MIN_MATCHES verified raw singles, else SKIP (stays "—", honest).
  - Sanity: drop the single highest+lowest before median if n>=4 (trims a stray
    graded/placeholder that slipped the filter). Skip suspicious round repunits.
  - Median of verified raw singles = the price; keep the most-recent date.

Writes price_source='yahoo_sold' + pricing_json.yahoo_sold =
  {price_jpy, price_usd, last_date, n_sales, source:'aucfree'}.
Frontend renders it as a "last sold" style value (see normalize/ptcg.js).

Usage:
  python -m scripts.backfill_ptcg_prices_yahoo_sold --dry-run   # scrape + SQL + spot-check, NO D1 write
  python -m scripts.backfill_ptcg_prices_yahoo_sold             # apply
"""
from __future__ import annotations
import argparse, json, re, statistics, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

UA = "OPBindr-pricing/1.0 (https://opbindr.com; arjun@neuroplexlabs.com)"
# Scrapling's lightweight HTTP Fetcher (real-browser TLS fingerprint + rotating
# stealthy headers). Aucfree's robots allows us — this is politeness/robustness
# on an allow-listed SSR site, NOT a Cloudflare/bot-wall bypass. Falls back to
# urllib if scrapling isn't installed so the script stays runnable anywhere.
try:
    from scrapling.fetchers import Fetcher as _SF
    _HAVE_SCRAPLING = True
except Exception:
    _SF = None
    _HAVE_SCRAPLING = False
EXCLUDE = re.compile(r'PSA|BGS|CGC|ARS|鑑定|最高評価|まとめ|セット|\d+枚|\bbox\b|英語版|english', re.I)
MIN_MATCHES = 2
FX_FALLBACK = 0.0064
# D1 set_id -> the hyphenated number-token used in Yahoo listings ("NNN/<TOKEN>")
SET_TOKEN = {
    'SVP': 'SV-P', 'SMP': 'SM-P', 'XYP': 'XY-P', 'BWP': 'BW-P', 'SWSHP': 'S-P',
    'DPP': 'DP-P', 'LP': 'L-P', 'MP': 'M-P', 'PTP': 'PT-P', 'PCGP': 'PCG-P',
    'ADVP': 'ADV-P',
}

def fx():
    try:
        return float(json.load(urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=JPY&to=USD", timeout=10))["rates"]["USD"])
    except Exception:
        return FX_FALLBACK

def li(x):
    m = re.match(r'^0*(\d+)', str(x)); return m.group(1) if m else str(x)

def fetch(q):
    url = "https://aucfree.com/search?q=" + urllib.parse.quote(q)
    if _HAVE_SCRAPLING:
        r = _SF.get(url, headers={"Accept-Language": "ja"}, stealthy_headers=True, timeout=30)
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.html_content
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    return urllib.request.urlopen(req, timeout=25).read().decode('utf-8', 'replace')

def parse_items(h):
    out = []
    for m in re.finditer(r'class="item_title"[^>]*>([^<]+)</a>(.*?)(?=class="item_title"|</body)', h, re.S):
        title = m.group(1).strip(); blob = m.group(2)
        pm = re.search(r'([0-9,]{2,})\s*円', blob)
        dm = re.search(r'(20\d\d)[/年\-](\d{1,2})', blob)
        if pm:
            out.append((title, int(pm.group(1).replace(',', '')), dm.group(0) if dm else None))
    return out

def price_card(card):
    """Return (result, fetch_errored).

    result is (jpy, last_date, n) or None. fetch_errored is True only when
    EVERY HTTP attempt for this card raised (no response at all) — the
    circuit-breaker signal for an IP block. A card that fetched fine but had
    no verified sold listings returns (None, False), which is a real no-market
    result, not a block.
    """
    setid = card['set_id'].upper()
    tok = SET_TOKEN.get(setid)
    if not tok:
        return None, False  # no known number-token format for this set -> skip
    n = li(card['local_id'])
    tokc = tok.replace('-', r'\-?')
    # Verify: the local number (padded or not) sits next to the set token in
    # the listing title, in any common format: "26/ADV-P", "026/ADV-P",
    # "ADV-P 26", "ADV-P026". Language-neutral, can't drift to another card.
    verify = re.compile(rf'(0*{n}\s*/\s*{tokc}|{tokc}\s*0*{n})', re.I)
    # Query variants, cheapest first; stop once we have enough verified hits.
    # Number-token formats first, then NAME-based queries (broader recall for
    # listings that don't put the clean number up front) — every result is
    # still number-verified below, so name queries can't drift to a wrong card.
    nm = (card.get('name') or '').strip()
    # GENTLE: max 2 requests/card. The 7-query version got our IP blocked by
    # aucfree (2026-06-01). Number-token first (proven highest yield); one
    # JA-name+number fallback only if that returns nothing. Run in small
    # batches (--start/--limit) with a long inter-card sleep, ideally as a
    # slow weekly cron — bulk one-shot scraping of 100s of cards gets blocked.
    queries = [f"{n}/{tok}"]
    if nm and not re.search(r'[A-Za-z]', nm):
        queries.append(f"{nm} {n}")
    items = []
    seen_titles = set()
    n_ok = n_err = 0
    for q in queries:
        try:
            for it in parse_items(fetch(q)):
                if it[0] not in seen_titles:
                    seen_titles.add(it[0]); items.append(it)
            n_ok += 1
        except Exception as e:
            n_err += 1
            print(f"  {card['id']}: fetch ERR {e}", file=sys.stderr)
        raw0 = [(t, p, d) for t, p, d in items if not EXCLUDE.search(t) and verify.search(t)]
        if len(raw0) >= MIN_MATCHES:
            break
        time.sleep(1.0)
    fetch_errored = (n_ok == 0 and n_err > 0)
    raw = [(t, p, d) for t, p, d in items if not EXCLUDE.search(t) and verify.search(t)]
    if len(raw) < MIN_MATCHES:
        return None, fetch_errored
    prices = sorted(p for _, p, _ in raw)
    if len(prices) >= 4:  # trim one extreme each end (stray graded/placeholder)
        prices = prices[1:-1]
    med = int(statistics.median(prices))
    # sanity: reject obvious placeholder repunit medians
    if str(med) in ('99999', '11111', '1234567') or med < 50:
        return None, fetch_errored
    last_date = next((d for _, _, d in raw if d), None)
    return (med, last_date, len(raw)), fetch_errored

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--start', type=int, default=0, help='skip the first N candidates (batch resume)')
    ap.add_argument('--cards-json', default='scratch_ja_probe/ja_final.json')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    rate = fx()
    d = json.load(open(args.cards_json, encoding='utf-8'))['data']
    TCGV = ['holofoil', 'normal', 'reverseHolofoil', '1stEdition', 'unlimited']
    def vis(c):
        """Does the card currently resolve to a displayed price?"""
        p = c.get('pricing') or {}; src = c.get('price_source')
        if isinstance(p.get('manual'), dict) and isinstance(p['manual'].get('price'), (int, float)): return True
        t = p.get('tcgplayer') or {}
        if any(isinstance(t.get(v), dict) and isinstance(t[v].get('market'), (int, float)) for v in TCGV): return True
        if src in ('yuyutei', 'hareruya', 'fullahead') and isinstance((p.get(src) or {}).get('price_usd'), (int, float)): return True
        if src in ('ebay_jp', 'ebay_us') and isinstance((p.get('ebay') or {}).get('price_usd'), (int, float)): return True
        if src == 'pricecharting' and isinstance((p.get('pricecharting') or {}).get('market'), (int, float)): return True
        cm = p.get('cardmarket') or {}
        return any(isinstance(cm.get(k), (int, float)) and cm[k] > 0 for k in ('avg', 'trend', 'avg7', 'avg30', 'avg1', 'low'))
    # Candidates: no RESOLVABLE price (incl ebay_jp-stamped-but-null like the
    # regional Pikachus), in a set whose number-token format we know.
    targets = [c for c in d if not vis(c) and c['set_id'].upper() in SET_TOKEN
               and c.get('price_source') not in ('manual',)]
    targets = targets[args.start:]
    if args.limit: targets = targets[:args.limit]
    print(f"FX 1 JPY={rate:.6f} USD | candidates (unpriced, known-token promos): {len(targets)}")

    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    rows = []
    consec_err = 0          # consecutive cards whose every fetch failed
    BREAKER = 5             # trip the circuit breaker after this many in a row
    aborted = False
    for i, c in enumerate(targets):
        r, errored = price_card(c)
        if errored:
            consec_err += 1
            if consec_err >= BREAKER:
                # An IP block looks like a run of total fetch failures. STOP —
                # every further request prolongs the block (it ages out in
                # tens of min to hours). Re-run gently from --start later.
                print(f"\n!! CIRCUIT BREAKER: {consec_err} consecutive cards with no response "
                      f"(card index {args.start + i}). Source is likely blocking. Stopping.\n"
                      f"   Re-run later with: --start {args.start + i}", file=sys.stderr, flush=True)
                aborted = True
                break
        else:
            consec_err = 0
        if r:
            jpy, date, n = r
            rows.append((c['id'], jpy, round(jpy * rate, 2), date, n,
                         c.get('name_en') or c.get('name', '')))
            print(f"  [{i+1}/{len(targets)}] {c['id']:10} ¥{jpy:>8,} (${round(jpy*rate,2)}) n={n} {date}", flush=True)
        time.sleep(2.5)
    status = "ABORTED (circuit breaker)" if aborted else "complete"
    print(f"\npriced {len(rows)} of {len(targets)} candidates — {status}")

    # SQL + spot-check report
    sql = [f"-- Yahoo-sold (aucfree) last-sold backfill (auto-gen {now}) — {len(rows)} rows",
           f"-- FX 1 JPY={rate:.6f} USD. Verified raw-single sold medians. Guards price_source IS NULL."]
    for cid, jpy, usd, date, n, nm in rows:
        obj = json.dumps({"price_jpy": jpy, "price_usd": usd, "last_date": date,
                          "n_sales": n, "source": "aucfree", "fetched_at": now}, ensure_ascii=False).replace("'", "''")
        # Overwrite only not-really-priced rows (null or ebay-stamped-null);
        # never clobber a real source (tcgplayer/pricecharting/yuyutei/manual).
        sql.append("UPDATE ptcg_cards SET "
                    f"pricing_json=json_patch(COALESCE(pricing_json,'{{}}'),json_object('yahoo_sold',json('{obj}'))), "
                    "price_source='yahoo_sold' "
                    f"WHERE lang='ja' AND card_id='{cid}' "
                    "AND (price_source IS NULL OR price_source IN ('ebay_jp','ebay_us'));")
    import os
    os.makedirs('data/backfill/yahoo_sold', exist_ok=True)
    open('data/backfill/yahoo_sold/yahoo_sold_prices.sql', 'w', encoding='utf-8').write("\n".join(sql) + "\n")
    json.dump([{"id": r[0], "jpy": r[1], "usd": r[2], "date": r[3], "n": r[4], "name": r[5]} for r in rows],
              open('data/backfill/yahoo_sold/spotcheck.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"SQL -> data/backfill/yahoo_sold/yahoo_sold_prices.sql ({len(rows)} rows)")
    if args.dry_run:
        print("(dry run — D1 not touched)")

if __name__ == '__main__':
    main()
