"""
Tag JA promo cards in ptcg_cards with the real-world campaign that
distributed them (Munch museum collab, McDonald's-by-year, movie-
commemoration pack, Pokemon Center DX, kuji prize, etc.).

Source of truth is Bulbapedia's category graph. For each signal in
CAMPAIGN_SIGNALS we walk Category:<name>, parse "(SET[-P] Promo NNN)"
out of each member title, normalize SET → our promo set_id (drop the
dash; bare-SET adds the implicit "P"), and emit a batched UPDATE that
joins by (UPPER(set_id), CAST(local_id AS INTEGER)). The normalized
join is non-negotiable here — the 2026-05-16 dedupe bug showed up
because two pipelines disagreed on case + zero-padding for the same
physical card. Normalizing at the SQL boundary is the only durable
defense.

Output: scripts/enrich_campaigns/<NNN>_<campaign_slug>.sql

Usage:
    python -m scripts.enrich_ja_promo_campaigns --dry-run
        Crawl every signal, write SQL files, don't touch D1.

    python -m scripts.enrich_ja_promo_campaigns --apply
        Crawl + write + run each batch through wrangler.

    python -m scripts.enrich_ja_promo_campaigns --campaigns munch --apply
        Limit to a single campaign slug (good for validation runs).

CAMPAIGN_SIGNALS today is intentionally short — start with what
Bulbapedia categorizes cleanly via title-parseable promo numbers
(Munch's "Cards with The Scream" is the canonical clean example).
Campaigns whose Bulbapedia category lists *main-set* card pages
instead of promo prints (McDonald's-Collection-YYYY, most movie
commemorations) need the wikitext-infobox parser path — that's
Phase 1a-2.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path("scripts/enrich_campaigns")
BATCH_SIZE = 250  # SQL statements per file (matches dedupe_ja_duplicates.py)
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
BULBAPEDIA_API = "https://bulbapedia.bulbagarden.net/w/api.php"
USER_AGENT = "OPBindr-Bot/1.0 (contact: arjun@neuroplexlabs.com)"
RATE_LIMIT_SECONDS = 1.1  # MediaWiki etiquette — single-threaded ~1 req/sec

# Signal map: Bulbapedia category title → (slug, campaign, distribution_method).
#
# slug                drives the output filename + the --campaigns filter
# campaign            user-facing free-text label written to ptcg_cards.campaign
# distribution_method coarse classifier written to ptcg_cards.distribution_method;
#                     keep the vocab small (see migration 015 header).
#
# Only include a category here if its members are titled (SET[-P] Promo N)
# — i.e. each page IS the promo print. Bulbapedia's bookkeeping
# sometimes lists the canonical-card page (titled with the main set)
# instead; those need the wikitext-infobox path.
CAMPAIGN_SIGNALS: dict[str, tuple[str, str, str]] = {
    "Cards with The Scream": (
        "munch",
        "Munch x Pokémon",
        "art_museum_collaboration",
    ),
}

# Bulbapedia set codes → our promo set_id. Bare codes (no -P) come from
# titles that use the loose "(SM Promo 244)" form; for promo prints
# those are still members of the *-P promotional category, so SM → SMP.
_SET_OVERRIDES: dict[str, str] = {
    # The S-P promotional set spans Sword & Shield-era promos. Our D1
    # uses SWSHP for these rows, mirroring TCGdex's "swshp" id.
    "S-P": "SWSHP",
    "S": "SWSHP",
    "SWSH-P": "SWSHP",
    "SWSH": "SWSHP",
}

# Regex: "(SM-P Promo 288)" or "(SV-P Promo 12)" or "(SM Promo 244)".
# Group 1 captures the set token (with optional -P), group 2 the number.
_TITLE_RE = re.compile(r"\(([A-Z]+(?:-P)?)\s+Promo\s+(\d+)\)\s*$")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Crawl + write SQL files. Don't touch D1.")
    g.add_argument("--apply", action="store_true",
                   help="Crawl, write SQL, AND apply against remote D1.")
    ap.add_argument("--campaigns", type=str, default="",
                    help="Comma-separated slugs to include. Empty = run "
                         "every signal in CAMPAIGN_SIGNALS.")
    args = ap.parse_args()

    wanted = {s.strip().lower() for s in args.campaigns.split(",") if s.strip()}
    signals = [(cat, slug, camp, dist)
               for cat, (slug, camp, dist) in CAMPAIGN_SIGNALS.items()
               if not wanted or slug in wanted]
    if not signals:
        print(f"No signals matched --campaigns={args.campaigns!r}. "
              f"Available slugs: {sorted(s[0] for s in CAMPAIGN_SIGNALS.values())}")
        sys.exit(1)

    print(f"1. Crawling {len(signals)} Bulbapedia campaign categor"
          f"{'y' if len(signals) == 1 else 'ies'}...")
    all_updates: list[tuple[str, str, str, list[tuple[str, int]]]] = []
    for category, slug, campaign, dist in signals:
        members = _fetch_category_members(category)
        keys = list(_parse_promo_keys(members))
        print(f"   {category}: {len(members)} members, {len(keys)} promo "
              f"prints → campaign={campaign!r}, dist={dist!r}")
        if keys:
            all_updates.append((slug, campaign, dist, keys))

    if not all_updates:
        print("Nothing to write.")
        return

    print("2. Writing batched UPDATE SQL...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = _write_batches(all_updates)
    print(f"   wrote {len(files)} batch file(s) to {OUT_DIR}/")

    if args.dry_run:
        print("\nDry run done. Inspect scripts/enrich_campaigns/*.sql, "
              "then re-run with --apply.")
        return

    print("3. Applying batches against remote D1...")
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


def _fetch_category_members(category: str) -> list[str]:
    """List every page in Category:<name>. Follows cmcontinue paging.
    Returns plain title strings (no namespace prefix)."""
    titles: list[str] = []
    cm_continue: str | None = None
    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmlimit": "500",
            "cmprop": "title",
            "format": "json",
        }
        if cm_continue:
            params["cmcontinue"] = cm_continue
        data = _api_get(params)
        for m in data.get("query", {}).get("categorymembers", []):
            t = m.get("title")
            if t:
                titles.append(t)
        cm_continue = data.get("continue", {}).get("cmcontinue")
        if not cm_continue:
            break
        time.sleep(RATE_LIMIT_SECONDS)
    return titles


def _parse_promo_keys(titles: list[str]) -> list[tuple[str, int]]:
    """Extract (set_id, local_id_int) pairs from page titles.

    Skips titles that don't fit the (SET[-P] Promo N) suffix — those
    are canonical-card pages reprinted as promos, and need the
    wikitext path that isn't built yet.
    """
    out: list[tuple[str, int]] = []
    skipped = 0
    for title in titles:
        m = _TITLE_RE.search(title)
        if not m:
            skipped += 1
            continue
        raw_set = m.group(1)
        set_id = _SET_OVERRIDES.get(raw_set)
        if not set_id:
            set_id = raw_set.replace("-", "")
            if not set_id.endswith("P"):
                set_id += "P"
        try:
            local_id = int(m.group(2))
        except ValueError:
            skipped += 1
            continue
        out.append((set_id, local_id))
    if skipped:
        print(f"     skipped {skipped} title(s) without (SET Promo N) suffix")
    return out


def _write_batches(updates: list[tuple[str, str, str, list[tuple[str, int]]]]
                   ) -> list[Path]:
    """One SQL file per campaign per batch. Each UPDATE joins by
    (UPPER(set_id), CAST(local_id AS INTEGER)) so case + zero-padding
    differences across ingest pipelines can't cause silent misses.
    """
    files: list[Path] = []
    for slug, campaign, dist, keys in updates:
        stmts = []
        for set_id, local_id in keys:
            stmts.append(
                "UPDATE ptcg_cards SET "
                f"campaign = {_esc(campaign)}, "
                f"distribution_method = {_esc(dist)} "
                f"WHERE lang = 'ja' "
                f"AND UPPER(set_id) = {_esc(set_id.upper())} "
                f"AND CAST(local_id AS INTEGER) = {local_id};"
            )
        for i in range(0, len(stmts), BATCH_SIZE):
            batch = stmts[i:i + BATCH_SIZE]
            idx = (i // BATCH_SIZE) + 1
            path = OUT_DIR / f"{slug}_{idx:03d}.sql"
            path.write_text("\n".join(batch) + "\n", encoding="utf-8")
            files.append(path)
    return files


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _api_get(params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BULBAPEDIA_API}?{qs}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"   HTTP {e.code} from Bulbapedia: {e.reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
