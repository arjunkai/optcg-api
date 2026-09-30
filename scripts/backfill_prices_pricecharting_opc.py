"""
PriceCharting (USD) price backfill for unpriced One Piece TCG (OPTCG) cards.

FREE replacement for the paid Firecrawl gap-filler. PriceCharting aggregates
ungraded/loose market prices from eBay sold listings and other US marketplaces.
Its set pages ("consoles") list EACH printing of a card as its OWN row, with the
full card number AND an explicit bracket label in the title, e.g.

    "Gol.D.Roger [Manga] OP09-118"            $3,320.88
    "Gol.D.Roger [Foil] OP09-118"             $32.81
    "Gol.D.Roger [Alternate Art] OP09-118"    $89.26
    "Shanks OP09-001"                          $0.99   (base, no label)

Per-set flow:
  GET /console/{slug}     (browser UA -> gets past the 403 plain fetchers hit)
    -> parse <tr id="product-..."> rows for: title, full card number, variant
       LABEL (the bracket text), and the ungraded/loose price.
  -> match to our `cards` rows by (number + variant CLASS) with strict 1:1 gating.

#1 REQUIREMENT — variant-safe matching (no conflation):
  One printed number (e.g. OP09-051) maps to a base + many parallels whose prices
  span $0.99 -> $1,479. PriceCharting also lists 5+ rows for that number. Our DB
  uses arbitrary import-order _p1/_p2/_r1 suffixes that have NO counterpart in a
  PC label, so we CANNOT know which _pN is PC's [Special Alternate Art] vs
  [2nd Anniversary]. Therefore we:

    1. Classify every PC row into a variant CLASS from its bracket label
       (base / alt / manga / special / foil / anniversary / wanted-poster / ...).
    2. Classify every one of OUR cards for that number into a class from its
       variant_type + finish + suffix.
    3. Assign a price to one of our cards ONLY when, for its class, there is
       EXACTLY ONE PC row AND EXACTLY ONE of our unpriced cards. Anything else
       (2+ on either side, or an our-class with no clean PC counterpart) is
       SKIPPED and left unpriced. A high skip count is GOOD — it is the scraper
       refusing to guess, which is exactly the bug fixed on 2026-06-11
       (a Championship promo inheriting a $0.19 base price).

  We NEVER fall back to the base price for a parallel. We NEVER pick
  candidates[0] from a multi-row class.

Scope:
  * Only cards where `price IS NULL` (never overwrites an existing price).
  * DON-* synthetic ids excluded (not singles).
  * Stamps price_source='pricecharting'.

Trust gates:
  * PARSE_CEILING ($50k): above this = parse/data error, drop.
  * Graded/box/sealed PC rows are excluded by label (Booster Box, [PSA ...], etc.).

Usage:
  python -m scripts.backfill_prices_pricecharting_opc --dry-run
  python -m scripts.backfill_prices_pricecharting_opc --dry-run --set OP09
  python -m scripts.backfill_prices_pricecharting_opc --dry-run --limit 50
  python -m scripts.backfill_prices_pricecharting_opc --apply      # writes to D1

This script is BUILD + DRY-RUN-first. --dry-run writes SQL to data/backfill/ and
never touches D1. Spot-check the emitted matches before --apply.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from scripts.wrangler_retry import run_wrangler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://www.pricecharting.com"
# Browser UA + identity encoding — copied from the PTCG scraper; this is what
# gets past PriceCharting's 403 for plain/script fetchers.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "identity",
}
REQ_INTERVAL_S = 1.0  # polite delay between PriceCharting fetches

DB_NAME = "optcg-cards"
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill/pricecharting_opc")
CACHE_DIR = Path("data/pricecharting_cache_opc")

PARSE_CEILING_USD = 50_000.0
MIN_USD = 0.01

# ---------------------------------------------------------------------------
# OPTCG set code -> PriceCharting console slug.
#
# VERIFIED 2026-06-24: each slug fetched with the browser UA returned HTTP 200
# with a populated card table (row count in the trailing comment). Apostrophes
# in slugs are real (OP14 / OP15) — PriceCharting's URL keeps them, so the
# fetch URL-encodes the slug (%27). Sets with no English PriceCharting page are
# listed in SETS_WITHOUT_PC below.
# ---------------------------------------------------------------------------
SET_TO_PC_SLUG: dict[str, str] = {
    # --- Main booster sets (OP01..OP16) ---
    "OP01": "one-piece-romance-dawn",                  # 150 rows
    "OP02": "one-piece-paramount-war",                 # 150
    "OP03": "one-piece-pillars-of-strength",           # 150
    "OP04": "one-piece-kingdoms-of-intrigue",          # 150
    "OP05": "one-piece-awakening-of-the-new-era",      # 150
    "OP06": "one-piece-wings-of-the-captain",          # 150
    "OP07": "one-piece-500-years-in-the-future",       # 150
    "OP08": "one-piece-two-legends",                   # 150
    "OP09": "one-piece-emperors-in-the-new-world",     # 150
    "OP10": "one-piece-royal-blood",                   # 150
    "OP11": "one-piece-fist-of-divine-speed",          # 150
    "OP12": "one-piece-legacy-of-the-master",          # 150
    "OP13": "one-piece-carrying-on-his-will",          # 150
    "OP14": "one-piece-azure-sea's-seven",             # 150 (apostrophe slug)
    "OP15": "one-piece-adventure-on-kami's-island",    # 150 (apostrophe slug)
    "OP16": "one-piece-the-time-of-battle",            # 150
    # --- Extra Boosters ---
    "EB01": "one-piece-extra-booster-memorial-collection",   # 92
    "EB02": "one-piece-extra-booster-anime-25th-collection", # 110
    "EB03": "one-piece-extra-booster-heroines-edition",      # 106
    "EB04": "one-piece-extra-booster-eb04",                  # 80
    # --- Premium Boosters ---
    "PRB01": "one-piece-premium-booster",      # 99
    "PRB02": "one-piece-premium-booster-2",    # 150
    # --- Promos (universal bucket) ---
    "P": "one-piece-promo",                    # 150+ (paginated; see note)
    # --- Starter / Ultra decks (ST01..ST30) ---
    "ST01": "one-piece-starter-deck-1-straw-hat-crew",
    "ST02": "one-piece-starter-deck-2-worst-generation",
    "ST03": "one-piece-starter-deck-3-the-seven-warlords-of-the-sea",
    "ST04": "one-piece-starter-deck-4-animal-kingdom-pirates",
    "ST05": "one-piece-starter-deck-5-film-edition",
    "ST06": "one-piece-starter-deck-6-absolute-justice",
    "ST07": "one-piece-starter-deck-7-big-mom-pirates",
    "ST08": "one-piece-starter-deck-8-monkeydluffy",
    "ST09": "one-piece-starter-deck-9-yamato",
    # ST10/ST11 are not single starter decks on PC (Ultra deck + Uta structure):
    "ST10": "one-piece-ultra-deck-the-three-captains",   # ST10 = Ultra Deck "The Three Captains"
    "ST11": "one-piece-starter-deck-11-uta",
    "ST12": "one-piece-starter-deck-12",
    "ST13": "one-piece-ultra-deck-the-three-brothers",   # ST13 = Ultra Deck "The Three Brothers"
    "ST14": "one-piece-starter-deck-14-3d2y",
    "ST15": "one-piece-starter-deck-15-edward-newgate",
    "ST16": "one-piece-starter-deck-16-uta",
    "ST17": "one-piece-starter-deck-17-donquixote-donflamingo",
    "ST18": "one-piece-starter-deck-18-monkeydluffy",
    "ST19": "one-piece-starter-deck-19-smoker",
    "ST20": "one-piece-starter-deck-20-charlotte-katakuri",
    "ST21": "one-piece-starter-deck-21-gear5",
    "ST22": "one-piece-starter-deck-22-ace-",
    "ST23": "one-piece-starter-deck-23-red-shanks",
    "ST24": "one-piece-starter-deck-24-green-jewelry-bonney",  # 17 rows
    "ST25": "one-piece-starter-deck-25-blue-buggy",
    "ST26": "one-piece-starter-deck-26-purple-monkeydluffy",
    "ST27": "one-piece-starter-deck-27-black-marshalldteach",  # 17 rows
    "ST28": "one-piece-starter-deck-28-yellow-yamato",
    "ST29": "one-piece-starter-deck-29-egghead",
    # ST22 / ST30 (Ultra Deck EX) — PriceCharting's search index returns
    # trailing-dash slugs (one-piece-starter-deck-22-ace- / ...-ex-30-luffy-)
    # but those 404 on the /console/ page, and no product page exposed a
    # canonical slug as of 2026-06-24. Left unmapped -> those cards stay NULL.
}

# Sets with no resolvable English PriceCharting console page (cards left NULL).
# The runtime 404-guard also skips any slug that returns 404 defensively.
SETS_WITHOUT_PC = ["ST22", "ST30"]


# ===========================================================================
# Variant classification — the heart of the conflation-safe matcher.
# ===========================================================================
#
# Each PriceCharting title carries a (possibly empty) bracket label. We map the
# label to a coarse CLASS. Our own cards map to the SAME class space from their
# variant_type + finish + suffix. A price only transfers within a class when the
# mapping is 1:1.
#
# PC label vocabulary observed on OPTCG pages (2026-06-24):
#   (none)                       -> base
#   [Foil]                       -> base_foil
#   [Alternate Art]/[Alt Art]/[Parallel] -> alt
#   [Manga]/[Alternate Art Manga]/[Manga Alternate Art] -> manga
#   [SP]/[Special Alternate Art] -> special
#   [2nd Anniversary]/[Anniversary] -> anniversary
#   [Wanted Poster]/[Wanted Poster Foil] -> wanted
#   [Manga PRB01]/[Alternate Art Manga PRB01] -> manga_prb (a DIFFERENT product)
#   ...and many more event/gift labels -> their own opaque classes.
#
# Anything containing a grading token, "Booster Box", "Case", "Deck", "Sealed",
# "Bundle", "Display" is dropped entirely (not a single card).
#
# !!! WHY 'Reprint' IS NEVER MATCHED !!!
# Our variant_type='Reprint' (finish=holo, suffix _r1) is used INCONSISTENTLY in
# the catalog. Cross-checking _r1 cards already priced by TCGPlayer against their
# PC pages (2026-06-24) showed:
#   OP02-013_r1 ($1,727 tcgplayer)  -> PC has NO [Foil]; its _r1 is the [Manga] ($1,512)
#   OP05-069_r1 ($1,675 tcgplayer)  -> PC has NO [Foil]; its _r1 is [Alternate Art Manga] ($1,300)
#   OP04-083_r1 ($943 tcgplayer)    -> PC has NO [Foil] at all
#   OP09-051_r1 (NULL)              -> PC HAS a [Foil] ($373) AND a [Manga] ($1,110)
# So a 'Reprint' may be a cheap [Foil] OR an expensive [Manga]/[Wanted Poster
# Foil] depending on the card, with no field on our row to tell which. Mapping
# Reprint -> [Foil] would assign the $373 foil price to a card that might really
# be the $1,110 manga (or vice-versa) — exactly the conflation we must not ship.
# Reprint cards are therefore classified into an opaque class that can never
# match a PC row, and are reported as skipped (residual manual-curation set).

_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_NUM_RE = re.compile(r"((?:OP|EB|ST|PRB)\d{2}-\d{3}|P-\d{3})", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")

# Non-single product titles to drop entirely.
_DROP_RE = re.compile(
    r"booster\s*box|booster\s*case|\bcase\b|\bdeck\b|sealed|bundle|display|"
    r"\bPSA\b|\bBGS\b|\bCGC\b|\bACE\b|graded|\blot\b|playset|carton|pack\b",
    re.IGNORECASE,
)

# Row regex — identical structure to the PTCG scraper (verified to match 150/150
# OPTCG rows): <tr id="product-N">...<a href="/game/...">TITLE</a>...
# <td class="price numeric used_price">...<span class="js-price">$X.XX</span>
_ROW_PAT = re.compile(
    r'<tr[^>]*id="product-(\d+)"[^>]*>'
    r'.*?<a[^>]+href="(/game/[^"]+)"[^>]*>([^<]+)</a>'
    r'.*?<td[^>]*class="price numeric used_price"[^>]*>'
    r'.*?<span[^>]*class="js-price"[^>]*>([^<]*?)</span>',
    re.DOTALL,
)


def classify_pc_label(label: str) -> str:
    """Map a PriceCharting bracket label (already lowercased, [] stripped) to a class."""
    l = label.strip().lower()
    if not l:
        return "base"
    # order matters: manga before alt (label can be "alternate art manga")
    if "manga" in l:
        return "manga"
    if "special alternate" in l or l in ("sp", "special") or "super alternate" in l:
        return "special"
    if "anniversary" in l:
        return "anniversary"
    if "wanted poster" in l:
        return "wanted"
    if "pre-release" in l or "pre release" in l or "prerelease" in l or "box topper" in l:
        return "prerelease"
    if "gift" in l:
        return "gift"
    if "reprint" in l:
        return "reprint"
    if "alternate art" in l or "alt art" in l or l == "parallel":
        return "alt"
    if l == "foil" or l == "holo" or l == "textured":
        # A plain foil/holo print. We DO NOT match our 'Reprint' cards to this
        # (see the long note above) — keep it as its own class so it never
        # collides with 'base' or 'alt'.
        return "base_foil"
    # Unknown label -> its own opaque class so it can never collide with base/alt.
    return f"other:{l}"


def classify_our_card(card: dict) -> str:
    """Map one of OUR cards to the same class space.

    card has: id, base_id, variant_type, finish, rarity.
    """
    cid = card["id"]
    vt = (card.get("variant_type") or "").strip()
    finish = (card.get("finish") or "").strip().lower()
    is_parallel = "_" in cid  # _p1/_p2/_r1 suffix

    if vt == "Manga Art":
        return "manga"
    if vt == "Reprint":
        # DELIBERATELY UNMATCHABLE. See the long note above classify_pc_label:
        # 'Reprint' maps to no single PC label reliably (can be [Foil], [Manga],
        # [Alternate Art Manga], [Wanted Poster Foil]...), so we refuse to guess.
        # These are emitted as skipped -> the residual manual-curation set.
        return "reprint_unmatchable"
    if vt == "Alternate Art":
        return "alt"
    if vt == "Serial":
        return "serial"
    if not vt and not is_parallel:
        return "base"
    # parallel with no variant_type, or anything unexpected -> opaque, won't match.
    return f"our_other:{vt or 'none'}:{finish}"


# ===========================================================================
# Fetch + parse a PriceCharting set page.
# ===========================================================================
def fetch_set_html(slug: str, use_cached: bool) -> str | None:
    """Return the HTML for a console page, or None on 404. Caches to disk."""
    safe = re.sub(r"[^a-z0-9_.-]", "_", slug.lower())
    cache = CACHE_DIR / f"{safe}.html"
    if use_cached and cache.exists():
        return cache.read_text(encoding="utf-8")
    url = f"{BASE}/console/{urllib.parse.quote(slug, safe='')}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(html, encoding="utf-8")
    return html


def parse_pc_rows(html: str) -> list[dict]:
    """Parse all single-card rows from a console page.

    Returns [{product_id, url, title, number, label, pc_class, price}].
    """
    out = []
    for m in _ROW_PAT.finditer(html):
        product_id, href, title, price_html = m.group(1), m.group(2), m.group(3), m.group(4)
        title = title.strip().replace("&amp;", "&").replace("&apos;", "'").replace("&#39;", "'")
        if _DROP_RE.search(title):
            continue
        num_m = _NUM_RE.search(title)
        if not num_m:
            continue  # no card number in title -> can't safely place it
        number = num_m.group(1).upper()
        bracket = _BRACKET_RE.search(title)
        label = bracket.group(1) if bracket else ""
        pm = _PRICE_RE.search(price_html)
        if not pm:
            continue
        price = float(pm.group(1).replace(",", ""))
        if price < MIN_USD or price > PARSE_CEILING_USD:
            continue
        out.append({
            "product_id": product_id,
            "url": href,
            "title": title,
            "number": number,
            "label": label,
            "pc_class": classify_pc_label(label),
            "price": round(price, 2),
        })
    return out


# ===========================================================================
# Matching.
# ===========================================================================
def base_number(cid: str) -> str:
    """OP09-051_p2 -> OP09-051 ; OP09-051 -> OP09-051."""
    return cid.split("_", 1)[0].upper()


def match_set(our_cards: list[dict], pc_rows: list[dict],
              full_class_count: dict[tuple[str, str], int],
              skip_log: list[dict]) -> list[dict]:
    """Conflation-safe match.

    our_cards: the UNPRICED cards in scope for this set (price IS NULL).
    pc_rows:   all parsed PC single-card rows for this set page.
    full_class_count: (number, class) -> count of ALL our cards in that class
                      (priced + unpriced). The gate keys on this, NOT on the
                      unpriced subset — otherwise a number like OP01-033 (six
                      Alternate-Art variants, five already priced, one NULL)
                      would look like a clean 1:1 against PC's single alt row
                      and the NULL one would inherit a price for a product we
                      can't actually identify. That is the conflation bug.

    For each (number, class):
      Assign ONLY when:
        * exactly ONE of our cards exists in that class for the number
          (across priced + unpriced), AND
        * exactly ONE PC row exists in that class.
      Otherwise SKIP (log reason).
    """
    # Index PC rows by (number, class). Dedup by product_id (a product can appear
    # twice if cross-listed within the same page).
    pc_by_num_cls: dict[tuple[str, str], list[dict]] = defaultdict(list)
    seen_pid: set[tuple[str, str, str]] = set()
    for r in pc_rows:
        key = (r["number"], r["pc_class"], r["product_id"])
        if key in seen_pid:
            continue
        seen_pid.add(key)
        pc_by_num_cls[(r["number"], r["pc_class"])].append(r)

    # Index our UNPRICED cards by (number, class).
    our_by_num_cls: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in our_cards:
        cls = classify_our_card(c)
        our_by_num_cls[(base_number(c["id"]), cls)].append(c)

    matches: list[dict] = []
    for (number, cls), ours in sorted(our_by_num_cls.items()):
        pcs = pc_by_num_cls.get((number, cls), [])

        # Gate on the FULL class size (priced + unpriced), not just the unpriced
        # cards in scope. >1 means the class is not 1:1 identifiable.
        total_in_class = full_class_count.get((number, cls), len(ours))
        if total_in_class > 1 or len(ours) > 1:
            skip_log.append({
                "number": number, "class": cls, "reason": "ambiguous_our_side",
                "our_ids": [c["id"] for c in ours],
                "our_total_in_class": total_in_class,
                "pc_rows": [f"{p['title']} (${p['price']})" for p in pcs],
            })
            continue
        if len(pcs) == 0:
            skip_log.append({
                "number": number, "class": cls, "reason": "no_pc_row_in_class",
                "our_ids": [c["id"] for c in ours],
                "pc_rows_any_class": [
                    f"{p['title']} [{p['pc_class']}] (${p['price']})"
                    for p in pc_rows if p["number"] == number
                ][:8],
            })
            continue
        if len(pcs) > 1:
            skip_log.append({
                "number": number, "class": cls, "reason": "ambiguous_pc_side",
                "our_ids": [c["id"] for c in ours],
                "pc_rows": [f"{p['title']} (${p['price']})" for p in pcs],
            })
            continue

        # 1:1 — safe to assign.
        c = ours[0]
        p = pcs[0]
        matches.append({
            "card_id": c["id"],
            "price_usd": p["price"],
            "class": cls,
            "variant_type": c.get("variant_type"),
            "finish": c.get("finish"),
            "pc_title": p["title"],
            "pc_label": p["label"],
            "pc_url": f"{BASE}{p['url']}",
            "pc_product_id": p["product_id"],
        })
    return matches


# ===========================================================================
# D1 + SQL.
# ===========================================================================
def query_d1(sql: str) -> list[dict]:
    r = run_wrangler(WRANGLER + ["--remote", "--json", "--command", sql])
    if r.returncode != 0:
        print("D1 query failed:", (r.stderr or "")[:500])
        sys.exit(1)
    s = r.stdout or ""
    i = s.find("[")
    if i < 0:
        return []
    payload = json.loads(s[i:])
    return payload[0].get("results", []) if isinstance(payload, list) else payload.get("results", [])


def _scope_clause(only_set: str | None) -> str:
    where = "id NOT LIKE 'DON-%'"
    if only_set:
        if only_set.upper() == "P":
            where += " AND id LIKE 'P-%'"
        else:
            where += f" AND id LIKE '{only_set.upper()}-%'"
    return where


def fetch_unpriced_cards(only_set: str | None) -> list[dict]:
    sql = (f"SELECT id, base_id, parallel, variant_type, finish, name, rarity "
           f"FROM cards WHERE price IS NULL AND {_scope_clause(only_set)} ORDER BY id")
    return query_d1(sql)


def fetch_full_class_count(only_set: str | None) -> dict[tuple[str, str], int]:
    """Count ALL our cards (priced + unpriced) per (number, variant-class).

    Used by the matcher to refuse a 1:1 match whenever a number has more than
    one card in the same class — even if all-but-one are already priced. Without
    this, an unpriced lone survivor in a 6-deep class looks falsely unique.
    """
    sql = (f"SELECT id, variant_type, finish FROM cards "
           f"WHERE {_scope_clause(only_set)}")
    rows = query_d1(sql)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for c in rows:
        cls = classify_our_card(c)
        counts[(base_number(c["id"]), cls)] += 1
    return counts


def set_code_of(cid: str) -> str:
    if cid.startswith("P-"):
        return "P"
    return cid.split("-", 1)[0].upper()


def build_update_sql(matches: list[dict]) -> list[str]:
    lines = [
        "-- PriceCharting (USD) backfill for unpriced OPTCG cards (auto-generated).",
        "-- Idempotent + safe: only writes rows still NULL, stamps price_source='pricecharting'.",
        "-- Variant-safe: every row below is a 1:1 (number,class) match (see matches.json).",
    ]
    for m in matches:
        cid = m["card_id"].replace("'", "''")
        lines.append(
            f"UPDATE cards SET price={m['price_usd']}, price_source='pricecharting' "
            f"WHERE id='{cid}' AND price IS NULL;"
        )
    return lines


# ===========================================================================
# Main.
# ===========================================================================
def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Build SQL to data/backfill/, do NOT execute")
    g.add_argument("--apply", action="store_true", help="Build SQL and execute against remote D1")
    ap.add_argument("--set", dest="only_set", help="Only this set code (e.g. OP09, ST13, P)")
    ap.add_argument("--limit", type=int, default=None, help="Cap total matches written (for testing)")
    ap.add_argument("--use-cached", action="store_true", help="Reuse cached PC HTML if present")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("1. Querying D1 for unpriced OPTCG cards...")
    cards = fetch_unpriced_cards(args.only_set)
    print(f"   {len(cards)} unpriced cards in scope "
          f"({'set=' + args.only_set if args.only_set else 'all sets'})")
    print("   Querying full per-(number,class) cardinality (for the ambiguity gate)...")
    full_class_count = fetch_full_class_count(args.only_set)

    by_set: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        by_set[set_code_of(c["id"])].append(c)

    all_matches: list[dict] = []
    skip_log: list[dict] = []
    no_pc_page: list[str] = []

    for sid in sorted(by_set.keys()):
        our_cards = by_set[sid]
        slug = SET_TO_PC_SLUG.get(sid)
        if not slug:
            no_pc_page.append(sid)
            for c in our_cards:
                skip_log.append({"number": base_number(c["id"]), "class": classify_our_card(c),
                                 "reason": "set_not_mapped", "our_ids": [c["id"]]})
            continue
        try:
            html = fetch_set_html(slug, args.use_cached)
        except Exception as e:
            print(f"   [{sid}] FETCH FAIL ({slug}): {e}")
            html = None
        if html is None:
            print(f"   [{sid}] no PC page (404): {slug}")
            no_pc_page.append(sid)
            for c in our_cards:
                skip_log.append({"number": base_number(c["id"]), "class": classify_our_card(c),
                                 "reason": "pc_page_404", "our_ids": [c["id"]]})
            continue

        pc_rows = parse_pc_rows(html)
        before = len(skip_log)
        ms = match_set(our_cards, pc_rows, full_class_count, skip_log)
        all_matches.extend(ms)
        print(f"   [{sid} -> {slug}]  {len(our_cards)} unpriced, {len(pc_rows)} PC rows "
              f"-> {len(ms)} priced, {len(skip_log) - before} skipped")
        if args.verbose:
            for m in ms[:6]:
                print(f"        + {m['card_id']:16} {m['class']:10} ${m['price_usd']:<9} "
                      f"<- {m['pc_title']}")
        if not args.use_cached:
            time.sleep(REQ_INTERVAL_S)

    if args.limit:
        all_matches = all_matches[:args.limit]

    # Persist artifacts.
    sql_lines = build_update_sql(all_matches)
    sql_path = OUT_DIR / "pricecharting_opc.sql"
    sql_path.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    (OUT_DIR / "matches.json").write_text(
        json.dumps(all_matches, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "skipped.json").write_text(
        json.dumps(skip_log, indent=2, ensure_ascii=False), encoding="utf-8")

    # Report.
    skip_reasons: dict[str, int] = defaultdict(int)
    for s in skip_log:
        skip_reasons[s["reason"]] += 1
    print("\n" + "=" * 66)
    print("COVERAGE — PriceCharting OPTCG backfill")
    print("=" * 66)
    print(f"Unpriced in scope:   {len(cards)}")
    print(f"Priced (1:1 safe):   {len(all_matches)}")
    print(f"Skipped (ambiguous / no PC row): {len(skip_log)}")
    for reason, n in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason:24s} {n}")
    if no_pc_page:
        print(f"Sets with no PC page: {sorted(set(no_pc_page))}")
    by_class: dict[str, int] = defaultdict(int)
    for m in all_matches:
        by_class[m["class"]] += 1
    print("Priced by class:", dict(sorted(by_class.items(), key=lambda x: -x[1])))
    print(f"\nSQL    -> {sql_path}")
    print(f"matches-> {OUT_DIR / 'matches.json'}")
    print(f"skipped-> {OUT_DIR / 'skipped.json'}")

    # Spot-check sample: priciest 5 + 5 multi-class examples.
    sample = sorted(all_matches, key=lambda m: -m["price_usd"])[:5]
    print("\nSPOT-CHECK (verify against the live PC page before --apply):")
    for m in sample:
        print(f"  {m['card_id']:16} {m['class']:10} ${m['price_usd']:<9} "
              f"{m['pc_title']}  {m['pc_url']}")

    if args.dry_run:
        print("\n--dry-run: D1 NOT touched.")
        return

    print(f"\nApplying {len(sql_lines)} statements to remote D1...")
    r = run_wrangler(WRANGLER + ["--remote", f"--file={sql_path}"])
    if r.returncode != 0:
        print("Apply failed:", (r.stderr or "")[:400])
        sys.exit(r.returncode)
    print("Done.")


if __name__ == "__main__":
    main()
