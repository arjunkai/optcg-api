"""
Yahoo Auctions "last sold" backfill for unpriced JA One Piece cards (aucfree.com).

Companion to scripts/backfill_yuyutei_opc.py. Yuyutei prices in-stock retail;
this fills the *sold-out / chase* tail with REAL Yahoo 落札 (sold) prices — the
industry-standard treatment for thin-market cards (never an estimate).

THE HARD PART — variant identity. One Piece reuses the same printed card number
(OP05-119) across the base card and many alt-art parallels. Our internal _pN
suffixes are arbitrary import-order with NO counterpart in a freetext Yahoo
title, so a number like OP05-119 with six "Alternate Art" parallels CANNOT be
safely disambiguated and is SKIPPED. We only price a card when its variant_type
is the UNIQUE one of its kind for that number AND maps to a distinct JP marker:

  variant_type 'Manga Art'      -> title must contain コミ (コミパラ/コミック)  [the chase rares]
  variant_type None (base)      -> title must NOT contain パラレル/コミ/SP markers
  variant_type 'Alternate Art'  -> title must contain パラレル (NOT コミ), ONLY if
                                   it's the single alt-art for the number
  variant_type 'Serial'         -> title must contain シリアル, single only

Everything is number-verified (the exact "OPxx-yyy" must be in the title — far
more robust than Pokemon's name matching), graded/lots/English excluded, and
requires >= MIN_MATCHES raw JP singles or it stays "—" (honest).

Writes cards.price_ja / price_source_ja='yahoo_sold' / price_updated_at_ja.
Idempotent (price_source_ja NULL or 'yahoo_sold'), never clobbers manual/yuyutei,
never touches the EN price.

Usage:
  python -m scripts.backfill_opc_prices_yahoo_sold --variant=manga --dry-run
  python -m scripts.backfill_opc_prices_yahoo_sold --dry-run        # all singleton-markers
  python -m scripts.backfill_opc_prices_yahoo_sold --apply
"""
from __future__ import annotations
import argparse, json, re, statistics, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

from scripts.wrangler_retry import run_wrangler

DB = "optcg-cards"
WR = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB]
UA = "OPBindr-pricing/1.0 (https://opbindr.com; arjun@neuroplexlabs.com)"
try:
    from scrapling.fetchers import Fetcher as _SF
    _HAVE_SCRAPLING = True
except Exception:
    _SF = None
    _HAVE_SCRAPLING = False

# Exclude graded slabs, lots/bundles, sealed, English-version, AND proxies/
# replicas. Japanese listings are full of fan-made proxies ("ACG"/オリカ/自作),
# display-only repros (観賞用/鑑賞用), and doujin reprints that carry the real
# card's number + コミパラ marker but sell for a few hundred yen — they wreck a
# median if not filtered. (Discovered 2026-06-20: OP05-069 raw コミパラ medianed
# to ¥2,500 because ACG proxies outnumbered the ¥150k+ real sales.)
EXCLUDE = re.compile(
    r'PSA|BGS|CGC|ARS|鑑定|最高評価|まとめ|セット|\d+枚|\bbox\b|ボックス|英語|海外|english'
    r'|ACG|オリカ|観賞用|鑑賞用|プロキシ|proxy|非公式|同人|自作|コピー|レプリカ', re.I)
MAX_DISPERSION = 8.0  # after filtering, max/min price ratio above this = mixed/unreliable -> skip
MANGA = re.compile(r'コミ')                       # コミパラ / コミック(パラレル)
PARALLEL = re.compile(r'パラレル')
SPECIAL = re.compile(r'スーパーパラレル|\bSP\b|シリアル|プロモ')
MIN_MATCHES = 3        # high bar — these are high-value chase cards
FX_FALLBACK = 0.0064
PARSE_CEILING_USD = 50_000


def fx():
    try:
        return float(json.load(urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=JPY&to=USD", timeout=10))["rates"]["USD"])
    except Exception:
        return FX_FALLBACK


def query_d1(sql):
    r = run_wrangler(WR + ["--remote", "--json", "--command", sql])
    if r.returncode != 0:
        print("D1 read failed:", (r.stderr or "")[:300]); sys.exit(1)
    return json.loads(r.stdout[r.stdout.find("["):])[0]["results"]


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


def title_matches(title, number, variant):
    """Number-verified + variant-marker gated. Returns True only for a raw JP
    single of the SPECIFIC variant we're pricing."""
    if EXCLUDE.search(title):
        return False
    # exact printed number must be present (boundary so OP05-119 != OP05-1190)
    if not re.search(rf'(?<!\d){re.escape(number)}(?!\d)', title):
        return False
    has_manga = bool(MANGA.search(title))
    has_par = bool(PARALLEL.search(title))
    has_sp = bool(SPECIAL.search(title))
    if variant == 'Manga Art':
        return has_manga
    if variant == 'Serial':
        return 'シリアル' in title
    if variant == 'Alternate Art':
        return has_par and not has_manga
    if variant is None:  # base
        return not (has_manga or has_par or has_sp)
    return False


def price_card(number, variant):
    """(jpy, last_date, n) or None, plus fetch_errored flag."""
    items, n_ok, n_err, seen = [], 0, 0, set()
    for q in [number, f"ワンピース {number}"]:
        try:
            for it in parse_items(fetch(q)):
                if it[0] not in seen:
                    seen.add(it[0]); items.append(it)
            n_ok += 1
        except Exception as e:
            n_err += 1
            print(f"  fetch ERR {number}: {e}", file=sys.stderr)
        raw = [(t, p, d) for t, p, d in items if title_matches(t, number, variant)]
        if len(raw) >= MIN_MATCHES:
            break
        time.sleep(1.0)
    raw = [(t, p, d) for t, p, d in items if title_matches(t, number, variant)]
    if len(raw) < MIN_MATCHES:
        return None, (n_ok == 0 and n_err > 0)
    prices = sorted(p for _, p, _ in raw)
    if len(prices) >= 4:
        prices = prices[1:-1]   # trim one extreme each end
    # Dispersion guard: a clean single-card market clusters tightly. A wide
    # spread means mixed quality (proxy/damaged vs mint) slipped the filters —
    # don't trust a median over it. Leave the card "—" (honest) for manual review.
    if prices[-1] > prices[0] * MAX_DISPERSION:
        return None, False
    med = int(statistics.median(prices))
    if med < 50:
        return None, False
    last_date = next((d for _, _, d in raw if d), None)
    return (med, last_date, len(raw)), False


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--apply', action='store_true')
    ap.add_argument('--variant', choices=['manga', 'base', 'alt', 'serial', 'all'], default='all')
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    rate = fx()
    print(f"FX 1 JPY = {rate:.6f} USD")

    # All JA cards + variant_type (to compute singleton-per-number) + which are unpriced.
    rows = query_d1(
        "SELECT c.id, c.variant_type, c.price AS en, c.price_ja "
        "FROM cards c WHERE c.id IN (SELECT card_id FROM card_translations WHERE language='ja') "
        "AND c.id NOT LIKE 'DON-%'")
    base_num = lambda i: i.split('_')[0]
    variants_by_num = defaultdict(list)
    for r in rows:
        variants_by_num[base_num(r['id'])].append(r['variant_type'])

    def singleton(cid, vt):
        return [x for x in variants_by_num[base_num(cid)] if x == vt].__len__() == 1

    VWANT = {'manga': 'Manga Art', 'base': None, 'alt': 'Alternate Art', 'serial': 'Serial'}
    targets = []
    for r in rows:
        if r['price_ja'] is not None:
            continue
        vt = r['variant_type']
        if not singleton(r['id'], vt):
            continue            # multi-same-variant -> unaddressable, skip
        if vt not in (None, 'Manga Art', 'Alternate Art', 'Serial'):
            continue            # Reprint etc. -> skip (ambiguous markers)
        if args.variant != 'all' and vt != VWANT[args.variant]:
            continue
        targets.append(r)
    targets.sort(key=lambda r: -(r['en'] or 0))   # most valuable first
    if args.limit:
        targets = targets[:args.limit]
    print(f"Targets (singleton-marker, unpriced): {len(targets)}\n")

    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    out, consec_err = [], 0
    for i, c in enumerate(targets):
        num = base_num(c['id'])
        r, errored = price_card(num, c['variant_type'])
        consec_err = consec_err + 1 if errored else 0
        if consec_err >= 5:
            print(f"\n!! CIRCUIT BREAKER at index {i} — source likely blocking. Stop.", file=sys.stderr)
            break
        if r:
            jpy, date, n = r
            usd = round(jpy * rate, 2)
            if usd > PARSE_CEILING_USD:
                continue
            out.append((c['id'], jpy, usd, date, n, c['variant_type'], c['en']))
            print(f"  [{i+1}/{len(targets)}] {c['id']:16} {c['variant_type'] or 'base':12} "
                  f"¥{jpy:>9,} (${usd}) n={n} {date}  (en ${c['en']})", flush=True)
        time.sleep(2.5)

    print(f"\nPriced {len(out)} of {len(targets)}")
    import os
    os.makedirs('data/backfill/yahoo_sold_opc', exist_ok=True)
    sql = [f"-- OPC Yahoo-sold (aucfree) backfill {now} — {len(out)} rows. FX {rate:.6f}."]
    for cid, jpy, usd, date, n, vt, en in out:
        ts = "strftime('%s','now')"
        sql.append(
            f"UPDATE cards SET price_ja={usd}, price_source_ja='yahoo_sold', "
            f"price_updated_at_ja=CAST({ts} AS INTEGER) "
            f"WHERE id='{cid}' AND (price_source_ja IS NULL OR price_source_ja='yahoo_sold');")
    sqlpath = 'data/backfill/yahoo_sold_opc/yahoo_sold_opc.sql'
    open(sqlpath, 'w', encoding='utf-8').write("\n".join(sql) + "\n")
    json.dump([{"id": r[0], "jpy": r[1], "usd": r[2], "date": r[3], "n": r[4],
                "variant": r[5], "en": r[6]} for r in out],
              open('data/backfill/yahoo_sold_opc/spotcheck.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f"SQL -> {sqlpath} ({len(out)} rows); spotcheck.json written")
    if args.dry_run:
        print("(dry-run — D1 not touched)")
        return
    if not out:
        print("nothing to apply"); return
    res = run_wrangler(WR + ["--remote", f"--file={sqlpath}"])
    print("apply:", "OK" if res.returncode == 0 else (res.stderr or "")[:300])


if __name__ == '__main__':
    main()
