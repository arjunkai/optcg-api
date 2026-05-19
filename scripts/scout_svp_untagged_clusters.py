"""Scout untagged SV-P promo distribution clusters on Bulbapedia.

Fetches the SV-P master setlist page wikitext, parses each Setlist/entry,
and for any LID whose D1 row currently has no distribution_method,
extracts the trailing distribution text (after the last pipe) and counts
clusters of common substrings. Surfaces clusters of 3+ untagged LIDs
sharing a clean phrase — those are candidates for a new Signal entry
in scripts/enrich_ja_promo_campaigns.py.

Read-only — no D1 writes, no Bulbapedia POSTs. Single-threaded crawl at
RATE_LIMIT_SECONDS per request, polite per MediaWiki etiquette.

This is the same shape as the Phase 1f scout (2026-05-18) but
parameterized and persisted as a script so the workflow can re-run it
weekly after each catch-up pass.

Usage:
    python -m scripts.scout_svp_untagged_clusters
    python -m scripts.scout_svp_untagged_clusters --set MP
    python -m scripts.scout_svp_untagged_clusters --min-cluster 2

Output: prints top untagged-LID clusters by substring frequency.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator

from scripts.wrangler_retry import run_wrangler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
BULBAPEDIA_API = "https://bulbapedia.bulbagarden.net/w/api.php"
USER_AGENT = "OPBindr-Bot/1.0 (contact: arjun@neuroplexlabs.com)"
RATE_LIMIT_SECONDS = 1.1

# Reuse the brace-depth-aware setlist line regex from
# enrich_ja_promo_campaigns. Setlist row leader:
#   {{Setlist/entry|NNN/...   or   {{Setlist/nmentry|NNN/...
_SETLIST_LINE_RE = re.compile(r"^\{\{Setlist/(?:entry|nmentry)\|(\d+)/")

# Distribution-text substring stop words / boilerplate noise. Lines
# carrying just "Holofoil", "Promo Card", etc. with no specific event
# are too generic to surface as a cluster — they're floor-stock signal.
_NOISE_PHRASES = {
    "promo card", "holofoil", "non-holofoil", "j", "english",
    "japanese", "promo", "card", "regular", "reprint",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="SVP",
                    help="D1 set_id to scout (default SVP). Pass MP for "
                         "the M-P master page.")
    ap.add_argument("--min-cluster", type=int, default=3,
                    help="Only print substrings appearing in this many "
                         "untagged LIDs (default 3).")
    ap.add_argument("--min-words", type=int, default=2,
                    help="Skip substrings with fewer than this many "
                         "alphanumeric words (default 2).")
    args = ap.parse_args()

    set_id = args.set.upper()
    bulba_token = _default_bulba_token(set_id)
    master_page = f"{bulba_token} Promotional cards (TCG)"

    print(f"Set: {set_id} ({bulba_token}). Master page: {master_page!r}")
    print(f"1. Fetching tagged LIDs from D1...")
    tagged = _fetch_tagged_lids(set_id)
    print(f"   D1 has {len(tagged)} LIDs already tagged with a "
          f"distribution_method")

    print(f"2. Fetching Bulbapedia master page wikitext...")
    wt = _fetch_page_wikitext(master_page)
    print(f"   wikitext length: {len(wt)} chars")

    print(f"3. Parsing setlist entries...")
    entries = list(_iter_setlist_entries(wt))
    print(f"   {len(entries)} setlist entries parsed")

    print(f"4. Filtering to UNTAGGED LIDs...")
    untagged = [(lid, body) for (lid, body) in entries if lid not in tagged]
    print(f"   {len(untagged)} untagged LIDs to scan")

    print(f"5. Extracting distribution-text segments...")
    # Distribution text is the LAST pipe-delimited segment of the entry
    # body (after stripping the trailing }}). For multi-line bodies, we
    # join non-leader continuation lines onto the search haystack.
    snippets_by_lid: dict[int, str] = {}
    for lid, body in untagged:
        snippet = _extract_distribution_snippet(body)
        if snippet:
            snippets_by_lid[lid] = snippet

    print(f"   {len(snippets_by_lid)} untagged LIDs have parseable "
          f"distribution text")

    print(f"\n6. Counting common substrings...")
    occurrences = _cluster_substrings(snippets_by_lid)

    # Sort by (count_of_LIDs_descending, phrase_length_descending) so
    # longer / more specific phrases surface before short prefixes of
    # the same cluster.
    ranked = sorted(
        occurrences.items(),
        key=lambda kv: (-len(kv[1]), -len(kv[0])),
    )

    print(f"\n--- Untagged clusters (>= {args.min_cluster} LIDs, "
          f">= {args.min_words} alphanumeric words) ---")
    printed = 0
    seen_lid_sets: list[frozenset[int]] = []
    for phrase, lids in ranked:
        if len(lids) < args.min_cluster:
            break
        words = re.findall(r"\w+", phrase)
        if len(words) < args.min_words:
            continue
        if phrase.lower() in _NOISE_PHRASES:
            continue
        # Skip a phrase whose LID set is a subset of one we already
        # printed (these are noisy substrings of a longer cluster).
        fl = frozenset(lids)
        if any(fl.issubset(prev) for prev in seen_lid_sets):
            continue
        seen_lid_sets.append(fl)

        sample = sorted(lids)[:8]
        more = "..." if len(lids) > 8 else ""
        print(f"  {len(lids):3} rows | {phrase!r}")
        print(f"           LIDs: {sample}{more}")
        first_lid = sample[0]
        ctx = snippets_by_lid[first_lid][:140].replace("\n", " ")
        print(f"           sample (LID {first_lid}): {ctx!r}")
        printed += 1
        if printed >= 20:
            print(f"  ... (truncated; pass --min-cluster higher to filter)")
            break
    if not printed:
        print(f"  (no clusters meet the bar — set may be exhausted)")


def _default_bulba_token(set_id: str) -> str:
    if not set_id.endswith("P") or len(set_id) < 2:
        raise ValueError(f"--set {set_id!r} must be a *-P promo set")
    return set_id[:-1] + "-P"


def _fetch_tagged_lids(set_id: str) -> set[int]:
    """Return the set of LIDs (as ints) already tagged for <set_id>/ja."""
    sql = (f"SELECT CAST(local_id AS INTEGER) AS lid FROM ptcg_cards "
           f"WHERE UPPER(set_id) = '{set_id}' AND lang = 'ja' "
           f"AND distribution_method IS NOT NULL")
    result = run_wrangler(WRANGLER + ["--remote", "--json", "--command", sql])
    if result.returncode != 0:
        print(f"   FAIL fetching tagged LIDs: "
              f"{(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = _strip_wrangler_chrome(result.stdout)
    data = json.loads(payload)
    rows = data[0]["results"] if isinstance(data, list) else data.get("results", [])
    return {int(r["lid"]) for r in rows if r.get("lid") is not None}


def _strip_wrangler_chrome(stdout: str) -> str:
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


def _fetch_page_wikitext(page: str) -> str:
    qs = urllib.parse.urlencode({
        "action": "parse", "page": page, "prop": "wikitext", "format": "json",
    })
    req = urllib.request.Request(
        f"{BULBAPEDIA_API}?{qs}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    time.sleep(RATE_LIMIT_SECONDS)
    return data.get("parse", {}).get("wikitext", {}).get("*", "")


def _iter_setlist_entries(wikitext: str) -> Iterator[tuple[int, str]]:
    """Yield (local_id, body) for each Setlist/entry template.
    Body spans from the opener line through the closer line (net {{ vs
    }} depth returns to zero), mirroring the parser in
    enrich_ja_promo_campaigns._iter_setlist_entries.
    """
    lines = wikitext.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].lstrip()
        if not (line.startswith("{{Setlist/entry") or
                line.startswith("{{Setlist/nmentry")):
            i += 1
            continue
        m = _SETLIST_LINE_RE.match(line)
        if not m:
            i += 1
            continue
        try:
            local_id = int(m.group(1))
        except ValueError:
            i += 1
            continue
        depth = 0
        body_parts: list[str] = []
        j = i
        closed = False
        while j < n:
            ln = lines[j]
            body_parts.append(ln)
            depth += ln.count("{{") - ln.count("}}")
            if depth <= 0:
                yield (local_id, "\n".join(body_parts))
                i = j + 1
                closed = True
                break
            j += 1
        if not closed:
            yield (local_id, "\n".join(body_parts))
            return


def _extract_distribution_snippet(body: str) -> str:
    """The distribution text for a Setlist/entry typically sits in the
    final pipe-delimited field of the opener line, plus any bullet-list
    continuation lines. Returns a cleaned-up substring suitable for
    cluster-counting. Empty string if nothing parseable.
    """
    # Strip leading template/wiki markup so we focus on prose
    cleaned = body
    # Drop {{TCG ID|...}} markers — they're noise for clustering
    cleaned = re.sub(r"\{\{TCG ID\|[^}]*\}\}", " ", cleaned)
    # Drop wikilinks but keep the display text
    cleaned = re.sub(r"\[\[([^|\]]+\|)?([^\]]+)\]\]", r"\2", cleaned)
    # Drop remaining templates entirely (these include {{tt|J|Japanese}})
    cleaned = re.sub(r"\{\{[^}]*\}\}", " ", cleaned)
    # Collapse pipes (the trailing field stuck inside an entry opener)
    parts = [p.strip() for p in cleaned.split("|") if p.strip()]
    if not parts:
        return ""
    # Join the last few segments — most distribution text lives in the
    # last pipe-field for single-line entries and on continuation lines
    # for multi-line ones
    snippet = " ".join(parts[-4:])
    # Normalise whitespace
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet


def _cluster_substrings(
    snippets_by_lid: dict[int, str],
) -> dict[str, set[int]]:
    """Build a {substring → set-of-LIDs} dict.

    Strategy: extract overlapping n-grams of 2..5 alphanumeric words
    from each snippet, then keep only n-grams that don't decompose into
    a known-noise phrase. Caller filters by cluster size.
    """
    occurrences: dict[str, set[int]] = {}
    for lid, snippet in snippets_by_lid.items():
        # Word-tokenise on alphanum + apostrophes + unicode word chars
        tokens = re.findall(r"[\w'À-￿]+", snippet)
        seen_in_this_lid: set[str] = set()
        for n in (2, 3, 4, 5):
            for i in range(len(tokens) - n + 1):
                phrase = " ".join(tokens[i:i + n])
                lp = phrase.lower()
                if lp in _NOISE_PHRASES:
                    continue
                if all(t.lower() in _NOISE_PHRASES for t in tokens[i:i + n]):
                    continue
                seen_in_this_lid.add(phrase)
        for phrase in seen_in_this_lid:
            occurrences.setdefault(phrase, set()).add(lid)
    return occurrences


if __name__ == "__main__":
    main()
