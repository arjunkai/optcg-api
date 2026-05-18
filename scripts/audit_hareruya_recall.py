"""
Read-only recall audit for the Hareruya backfill.

Loads the cached Shopify products dump (data/poc_hareruya/products_raw.jsonl)
and the live unpriced JA D1 rows, then measures how many D1 rows the
existing matching path catches vs each candidate relaxation. Writes a
per-set + per-relaxation report to stdout. **Never touches D1.**

Why: per project-ptcg-data-coverage memory, Hareruya has 827 rows of
JA pricing in D1 vs an expected scale that's much higher for a major
JP retailer. Hypothesis: fuzzy-match recall is under-firing. This
script measures the gap before any matching-logic change ships, so we
know which relaxation is worth applying and which is noise.

Discipline: measure first, don't synthesize prices, NULL beats wrong.

Usage:
    python -m scripts.audit_hareruya_recall
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_CACHE = REPO_ROOT / "data/poc_hareruya/products_raw.jsonl"
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]

# Current production regex from backfill_ptcg_prices_hareruya.py.
STRICT_RE = re.compile(
    r"〈\s*(?P<lid>[A-Za-z0-9\-/]+?)\s*(?:/\s*[\dA-Za-z\-]+)?\s*〉\s*\[\s*(?P<setid>[^\]]+?)\s*\]"
)

# Looser regex: same anchors but tolerates whitespace + alt brackets.
# Bulbapedia / Hareruya occasionally use full-width brackets variants.
LOOSE_RE = re.compile(
    r"[〈<\[]\s*(?P<lid>[A-Za-z0-9\-/]+?)\s*(?:/\s*[\dA-Za-z\-]+)?\s*[〉>\]]\s*[\[【]\s*(?P<setid>[^\]】]+?)\s*[\]】]"
)

# Current promo map from production script.
HARERUYA_PROMO_MAP = {
    "S-P": "SWSHP", "SM-P": "SMP", "XY-P": "XYP", "BW-P": "BWP",
    "DP-P": "DPP",  "SV-P": "SVP", "P-P":  "PP",
}

# Candidate extension: more promo + era codes seen in unmatched titles
# OR in the top unpriced D1 sets. Hyphen-stripped variants in particular.
EXTENDED_PROMO_MAP = {
    **HARERUYA_PROMO_MAP,
    "PMCG-P": "PMCGP",
    "PCG-P":  "PCGP",
    "M-P":    "MP",
    "L-P":    "LP",
    "PT-P":   "PTP",
    "PLAY-P": "PLAYP",
    "MISC-PP": "MISCPP",
    # Era set base codes that Hareruya might hyphenate
    "PCG-1": "PCG1", "PCG-2": "PCG2", "PCG-3": "PCG3", "PCG-4": "PCG4",
    "PCG-5": "PCG5", "PCG-6": "PCG6", "PCG-7": "PCG7", "PCG-8": "PCG8",
    "PCG-9": "PCG9", "PCG-10": "PCG10",
    "PMCG-1": "PMCG1", "PMCG-2": "PMCG2", "PMCG-3": "PMCG3",
    "PMCG-4": "PMCG4", "PMCG-5": "PMCG5",
    "ADV-1": "ADV1", "ADV-2": "ADV2", "ADV-3": "ADV3",
    "ADV-4": "ADV4", "ADV-5": "ADV5",
    "VND-1": "VND1", "VND-2": "VND2", "VND-3": "VND3",
    "LL-1": "LL1", "LL-2": "LL2",
}


def strict_candidates(setid: str) -> list[str]:
    """Mirror of production candidate_setids."""
    cands = {setid, setid.upper(), setid.lower()}
    if setid in HARERUYA_PROMO_MAP:
        cands.add(HARERUYA_PROMO_MAP[setid])
    if re.match(r"^S\d", setid):
        cands.add("SWSH" + setid[1:].upper())
        cands.add("SWSH" + setid[1:])
    if re.match(r"^M\d", setid):
        cands.add("MEGA" + setid[1:].upper())
        cands.add(setid.upper())
    return list(cands)


def extended_candidates(setid: str) -> list[str]:
    """Strict candidates + extended promo map + de-hyphenated variant.

    Adds:
    - extended promo map entries (PCG-P, PMCG-P, M-P, L-P, PT-P, PLAY-P, MISC-PP)
    - extended era hyphen variants (PCG-10 → PCG10 etc.)
    - bare de-hyphenated variant of any setid (PCG-10 → PCG10 as fallback)
    """
    cands = set(strict_candidates(setid))
    if setid in EXTENDED_PROMO_MAP:
        cands.add(EXTENDED_PROMO_MAP[setid])
    # General de-hyphenation: PCG-10 → PCG10, M-P → MP, e-1 → e1
    no_hyphen = setid.replace("-", "")
    cands.add(no_hyphen)
    cands.add(no_hyphen.upper())
    cands.add(no_hyphen.lower())
    return list(cands)


def normalize_lid_strict(lid: str) -> list[str]:
    """Mirror of production normalize_lid."""
    out = [lid]
    if lid.lstrip("0") and lid.lstrip("0") != lid:
        out.append(lid.lstrip("0"))
    return out


def normalize_lid_padded(lid: str) -> list[str]:
    """Strict + zero-padded variants (zfill(2), zfill(3))."""
    out = set(normalize_lid_strict(lid))
    # Add padded forms — D1 vintage (`neo1-078`) zero-pads, Hareruya
    # listings typically don't.
    if lid.isdigit():
        out.add(lid.zfill(2))
        out.add(lid.zfill(3))
    return list(out)


def build_index(
    products: list[dict],
    title_re: re.Pattern,
    candidates_fn,
    lid_fn,
) -> dict[tuple[str, str], list[float]]:
    by_card = defaultdict(list)
    for p in products:
        m = title_re.search(p.get("title", "") or "")
        if not m:
            continue
        setid = m.group("setid").strip()
        lid = m.group("lid").strip()
        prices = []
        for v in p.get("variants", []):
            if v.get("available"):
                try:
                    prices.append(float(v["price"]))
                except (KeyError, TypeError, ValueError):
                    pass
        if not prices:
            continue
        for sid in candidates_fn(setid):
            for normalized_lid in lid_fn(lid):
                by_card[(sid, normalized_lid)].append(min(prices))
    return by_card


def query_d1_unpriced() -> list[dict]:
    out = subprocess.run(
        WRANGLER + ["--remote", "--json", "--command",
                    "SELECT card_id, set_id, local_id FROM ptcg_cards "
                    "WHERE lang='ja' AND price_source IS NULL AND name IS NOT NULL"],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=str(REPO_ROOT),
    )
    data = json.loads(out.stdout)
    return data[0]["results"] if data and data[0].get("success") else []


def count_matches(
    cards: list[dict],
    by_card: dict[tuple[str, str], list[float]],
    lid_fn,
    candidates_fn=None,
) -> tuple[int, set[str], dict[str, int]]:
    """Count how many D1 unpriced cards match `by_card`.

    Returns (total_matches, matched_card_ids, per_set_matches).

    If candidates_fn is None, looks up using D1's raw set_id only (mirrors
    production). If provided, expands the D1 set_id symmetrically before
    lookup — that's the "what if we case-fold/de-hyphenate both sides"
    relaxation.
    """
    matched_ids: set[str] = set()
    per_set: dict[str, int] = defaultdict(int)
    for c in cards:
        d1_set_id = c["set_id"]
        sid_candidates = [d1_set_id] if candidates_fn is None else candidates_fn(d1_set_id)
        hit = False
        for sid in sid_candidates:
            for lid in lid_fn(c["local_id"]):
                if (sid, lid) in by_card:
                    hit = True
                    break
            if hit:
                break
        if hit:
            matched_ids.add(c["card_id"])
            per_set[d1_set_id] += 1
    return len(matched_ids), matched_ids, dict(per_set)


def main() -> None:
    if not RAW_CACHE.exists():
        print(f"Cache missing: {RAW_CACHE}")
        sys.exit(1)
    print(f"Loading products from {RAW_CACHE}...")
    products = []
    with RAW_CACHE.open(encoding="utf-8") as f:
        for line in f:
            products.append(json.loads(line))
    print(f"  loaded {len(products)} products")

    # Regex coverage stats — what fraction of titles even fit?
    strict_hits = sum(1 for p in products if STRICT_RE.search(p.get("title", "") or ""))
    loose_hits = sum(1 for p in products if LOOSE_RE.search(p.get("title", "") or ""))
    print(f"\nTitle regex coverage:")
    print(f"  strict regex (production): {strict_hits} / {len(products)} ({100*strict_hits/len(products):.1f}%)")
    print(f"  loose regex:               {loose_hits} / {len(products)} ({100*loose_hits/len(products):.1f}%)")
    print(f"  delta:                     +{loose_hits - strict_hits} products")

    print("\nQuerying unpriced JA rows from D1...")
    cards = query_d1_unpriced()
    print(f"  {len(cards)} unpriced candidates")

    # Build indexes under each policy.
    indexes: list[tuple[str, dict[tuple[str, str], list[float]], callable, callable | None]] = [
        ("R0 production (strict regex + strict candidates + strict lid)",
         build_index(products, STRICT_RE, strict_candidates, normalize_lid_strict),
         normalize_lid_strict, None),
        ("R1 lid-padded (D1-side: try zfill(2)/zfill(3) too)",
         build_index(products, STRICT_RE, strict_candidates, normalize_lid_padded),
         normalize_lid_padded, None),
        ("R2 setid-symmetric (D1-side: case-fold + de-hyphenate the lookup setid)",
         build_index(products, STRICT_RE, extended_candidates, normalize_lid_padded),
         normalize_lid_padded, extended_candidates),
        ("R3 loose-regex (catch alternate bracket forms)",
         build_index(products, LOOSE_RE, extended_candidates, normalize_lid_padded),
         normalize_lid_padded, extended_candidates),
    ]

    baseline_ids: set[str] = set()
    print("\nRecall by relaxation policy (cumulative):")
    print(f"{'policy':<60} {'matched':>8} {'+vs R0':>8} {'recall':>7}")
    print("-" * 86)
    prior_ids: set[str] = set()
    per_policy: list[tuple[str, set[str], dict[str, int]]] = []
    for i, (label, by_card, lid_fn, cand_fn) in enumerate(indexes):
        total, ids, per_set = count_matches(cards, by_card, lid_fn, cand_fn)
        if i == 0:
            baseline_ids = ids
        delta = len(ids - baseline_ids)
        pct = 100 * total / max(1, len(cards))
        print(f"{label:<60} {total:>8} {'+'+str(delta):>8} {pct:>6.1f}%")
        per_policy.append((label, ids, per_set))
        prior_ids = ids

    # Per-set breakdown of the marginal gain from R0 → R3.
    final_ids = per_policy[-1][1]
    marginal_ids = final_ids - baseline_ids
    print(f"\nMarginal gain (R3 - R0) by set_id:")
    per_set_marginal: dict[str, int] = defaultdict(int)
    for c in cards:
        if c["card_id"] in marginal_ids:
            per_set_marginal[c["set_id"]] += 1
    for s, n in sorted(per_set_marginal.items(), key=lambda x: -x[1])[:25]:
        print(f"  {s:<12} +{n}")

    # Per-set unmatched-after-R3 (true source gap, not match gap).
    unmatched_after_r3 = [c for c in cards if c["card_id"] not in final_ids]
    print(f"\nStill unmatched after R3: {len(unmatched_after_r3)} rows")
    per_set_residual: dict[str, int] = defaultdict(int)
    for c in unmatched_after_r3:
        per_set_residual[c["set_id"]] += 1
    print("Top 25 residual sets (true Hareruya source gap):")
    for s, n in sorted(per_set_residual.items(), key=lambda x: -x[1])[:25]:
        print(f"  {s:<12} {n}")


if __name__ == "__main__":
    main()
