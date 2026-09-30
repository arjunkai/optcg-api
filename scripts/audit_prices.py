"""Post-import price audit for the OPTCG `cards` table.

Runs at the END of the weekly pricing cron (after import-prices-d1.js,
the dotgg/eBay/web backfills, and manual overrides). It does NOT write to
D1 — it reads the freshly-imported prices and surfaces three failure shapes
so a regression like the 2026-06-11 $87,500 dotgg phantom pings us instead of
shipping silently to users:

  1. Hard-ceiling breach  — price above $10k. The real OPTCG ceiling is a
     ~$8.5k Red-Manga chase card. A breach from a FALLBACK source
     (dotgg/positional/ebay) is almost certainly an asking-price phantom →
     CRITICAL, non-zero exit. A breach from an AUTHORITATIVE source
     (tcgplayer real sale / manual curation) is just a genuinely expensive
     card — it's listed for awareness but never fails CI.
  2. tcg_id collisions    — one TCGPlayer product id stamped on >1 card_id.
     The cardinal rule (docs/variant-taxonomy.md): two cards must never share
     a tcg_ids value. ~437 baseline groups exist (base+reprint+parallel
     clusters), so this is RANKED by blast radius and CAPPED — high-value
     groups (max ≥ $100) and within-group price drift are what's actionable.
  3. Price outliers       — a card priced an order of magnitude above its
     variant-class (rarity × variant_type) median. Non-authoritative sources
     (dotgg/positional/eBay/web) at ≥10× are HIGH suspects; authoritative
     real-sale sources (tcgplayer/manual) at ≥25× are LOW review items
     (usually genuine chase cards, surfaced just to eyeball).

It also echoes the dotgg rejected-high queue (data/dotgg_rejected_high.json)
written by build_all_prices.py / backfill_prices_dotgg.py — cards the $300
ceiling kicked out that now need manual curation in data/manual_prices.json.

Output:
  - data/backfill/price_audit.json   full machine-readable report
  - stdout                            ranked human summary
  - $GITHUB_STEP_SUMMARY (in CI)      markdown summary in the workflow run

Exit code:
  0  no critical findings (collisions/outliers/real-sale highs are reported
     but don't fail CI)
  2  one or more fallback-source prices above the hard ceiling (unambiguous
     phantom) — unless --no-fail

Discord posting was intentionally left out (2026-06-11): findings surface
through the CI step summary. If revisited, post via a direct DISCORD_CRON_WEBHOOK
(the path scrape.yml's coverage step already uses), not the Dugong bot — Dugong
only polls Supabase and this data lives in Cloudflare D1.

Run:
  python -m scripts.audit_prices                # remote D1, fails on critical
  python -m scripts.audit_prices --local        # local D1
  python -m scripts.audit_prices --no-fail      # always exit 0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# ── Tunable thresholds ────────────────────────────────────────────────────────
HARD_CEILING = 10_000.0          # above this $ = almost certainly a phantom
OUTLIER_RATIO = 10.0             # non-auth source: price ≥ N× class median
AUTH_OUTLIER_RATIO = 25.0        # tcgplayer/manual: higher bar (real sales)
OUTLIER_ABS_FLOOR = 50.0         # ignore cheap noise below this $
COLLISION_VALUE_FLOOR = 100.0    # only list collision groups worth real money
MIN_CLASS_SIZE = 5               # need ≥ this many priced cards for a median
TOP_N = 25                       # display cap per section

# Real-sale / human-curated sources we trust; everything else (dotgg, positional,
# ebay, web_tcgplayer, NULL) is treated as phantom-risk in the outlier test.
AUTH_SOURCES = frozenset({"tcgplayer", "manual", "manual_jp"})

OUT_AUDIT = Path("data/backfill/price_audit.json")
REJECTED_HIGH = Path("data/dotgg_rejected_high.json")

WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
SELECT_SQL = (
    "SELECT id, name, rarity, variant_type, price, price_source, tcg_ids "
    "FROM cards WHERE price IS NOT NULL"
)


# ── Pure detection logic (unit-tested in tests/test_audit_prices.py) ──────────

def parse_tcg_ids(raw) -> list[int]:
    """Parse the D1 tcg_ids TEXT column ('[485262]', '[615592,615593]', '[]',
    None, '') into a list of ints. Malformed values yield [] rather than raise."""
    if not raw or raw == "[]":
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(val, list):
        return []
    out = []
    for x in val:
        try:
            out.append(int(x))
        except (ValueError, TypeError):
            continue
    return out


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def class_key(row) -> tuple:
    """Variant class = (rarity, variant_type), nulls normalized to ''."""
    return (row.get("rarity") or "", row.get("variant_type") or "")


def compute_class_medians(rows, min_class_size: int = MIN_CLASS_SIZE) -> dict[tuple, float]:
    buckets: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("price") is None:
            continue
        buckets[class_key(r)].append(float(r["price"]))
    return {
        k: median(v)
        for k, v in buckets.items()
        if len(v) >= min_class_size
    }


def _member(r) -> dict:
    return {
        "id": r["id"],
        "price": r.get("price"),
        "price_source": r.get("price_source"),
        "rarity": r.get("rarity"),
        "variant_type": r.get("variant_type"),
    }


def find_tcg_id_collisions(rows) -> list[dict]:
    """Explode each card's tcg_ids array and group by individual tcg_id. A group
    with >1 distinct card_id is a collision (one product, many cards). Exploding
    (vs exact-string GROUP BY) also catches partial overlaps like
    [615592,615593] sharing 615592 with [615592]."""
    by_tcg: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        for tid in parse_tcg_ids(r.get("tcg_ids")):
            by_tcg[tid][r["id"]] = r  # dedup by card_id
    groups = []
    for tid, members_by_id in by_tcg.items():
        if len(members_by_id) < 2:
            continue
        members = [_member(r) for r in members_by_id.values()]
        prices = [m["price"] for m in members if m["price"] is not None]
        distinct_prices = len({round(p, 4) for p in prices})
        groups.append({
            "tcg_id": tid,
            "members": sorted(members, key=lambda m: m["id"]),
            "max_price": max(prices) if prices else None,
            "min_price": min(prices) if prices else None,
            "distinct_prices": distinct_prices,
            "price_drift": distinct_prices > 1,
        })
    groups.sort(key=lambda g: (g["max_price"] or 0), reverse=True)
    return groups


def find_hard_ceiling(rows, ceiling: float = HARD_CEILING) -> list[dict]:
    """All cards priced above the ceiling, tagged by whether the price came from
    an authoritative (real-sale / hand-curated) source. A genuinely expensive
    card from tcgplayer/manual is `authoritative=True` — it gets listed for
    awareness but does NOT count as critical, so it never fails CI. Only a
    fallback-source price above the ceiling is an unambiguous phantom."""
    flagged = [
        {
            **_member(r),
            "tcg_ids": r.get("tcg_ids"),
            "authoritative": (r.get("price_source") or "") in AUTH_SOURCES,
        }
        for r in rows
        if r.get("price") is not None and float(r["price"]) > ceiling
    ]
    flagged.sort(key=lambda f: f["price"], reverse=True)
    return flagged


def find_outliers(
    rows,
    class_medians: dict[tuple, float],
    *,
    ratio: float = OUTLIER_RATIO,
    auth_ratio: float = AUTH_OUTLIER_RATIO,
    abs_floor: float = OUTLIER_ABS_FLOOR,
) -> list[dict]:
    out = []
    for r in rows:
        price = r.get("price")
        if price is None or float(price) < abs_floor:
            continue
        med = class_medians.get(class_key(r))
        if not med or med <= 0:
            continue  # no trustworthy class median → can't ratio-test
        rr = float(price) / med
        is_auth = (r.get("price_source") or "") in AUTH_SOURCES
        threshold = auth_ratio if is_auth else ratio
        if rr < threshold:
            continue
        out.append({
            **_member(r),
            "tcg_ids": r.get("tcg_ids"),
            "class_median": round(med, 2),
            "ratio": round(rr, 1),
            "severity": "low" if is_auth else "high",
        })
    out.sort(key=lambda o: o["ratio"], reverse=True)
    return out


def build_report(rows, rejected_high, *, hard_ceiling=HARD_CEILING,
                 collision_value_floor=COLLISION_VALUE_FLOOR, **outlier_kw) -> dict:
    medians = compute_class_medians(rows)
    ceiling = find_hard_ceiling(rows, hard_ceiling)
    # Only a fallback-source price above the ceiling is an unambiguous phantom.
    # A real-sale / hand-curated high (tcgplayer/manual) is listed for awareness
    # but never fails CI — that's how a genuinely expensive card stays safe.
    critical_ceiling = [f for f in ceiling if not f["authoritative"]]
    review_ceiling = [f for f in ceiling if f["authoritative"]]
    collisions = find_tcg_id_collisions(rows)
    outliers = find_outliers(rows, medians, **outlier_kw)

    high_value_collisions = [
        g for g in collisions
        if g["max_price"] is not None and g["max_price"] >= collision_value_floor
    ]
    drift_collisions = [g for g in collisions if g["price_drift"]]
    high_outliers = [o for o in outliers if o["severity"] == "high"]
    low_outliers = [o for o in outliers if o["severity"] == "low"]

    return {
        "counts": {
            "priced_cards": sum(1 for r in rows if r.get("price") is not None),
            "hard_ceiling": len(ceiling),
            "hard_ceiling_critical": len(critical_ceiling),
            "hard_ceiling_review": len(review_ceiling),
            "tcg_id_collisions_total": len(collisions),
            "tcg_id_collisions_high_value": len(high_value_collisions),
            "tcg_id_collisions_price_drift": len(drift_collisions),
            "outliers_high": len(high_outliers),
            "outliers_low": len(low_outliers),
            "dotgg_rejected_high": len(rejected_high),
        },
        "has_critical": len(critical_ceiling) > 0,
        "hard_ceiling": ceiling,
        "tcg_id_collisions": {
            "total": len(collisions),
            "high_value": high_value_collisions,
            "price_drift": drift_collisions,
        },
        "outliers": {"high": high_outliers, "low": low_outliers},
        "dotgg_rejected_high": rejected_high,
        "thresholds": {
            "hard_ceiling": hard_ceiling,
            "outlier_ratio": OUTLIER_RATIO,
            "auth_outlier_ratio": AUTH_OUTLIER_RATIO,
            "outlier_abs_floor": OUTLIER_ABS_FLOOR,
            "collision_value_floor": collision_value_floor,
            "min_class_size": MIN_CLASS_SIZE,
        },
    }


# ── Impure shell (D1 fetch, file IO, rendering) ───────────────────────────────

def _strip_wrangler_chrome(stdout: str) -> str:
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


def fetch_rows(local: bool) -> list[dict]:
    from scripts.wrangler_retry import run_wrangler
    flag = "--local" if local else "--remote"
    result = run_wrangler(WRANGLER + [flag, "--json", "--command", SELECT_SQL])
    if result.returncode != 0:
        print(f"   FAIL: {(result.stderr or '')[:400]}")
        sys.exit(1)
    data = json.loads(_strip_wrangler_chrome(result.stdout))
    return data[0]["results"] if isinstance(data, list) else data.get("results", [])


def load_rejected_high() -> list[dict]:
    if not REJECTED_HIGH.exists():
        return []
    try:
        return json.loads(REJECTED_HIGH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []


def _money(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


def render_summary(report: dict) -> str:
    c = report["counts"]
    icon = "🔴" if report["has_critical"] else (
        "🟠" if (c["outliers_high"] or c["tcg_id_collisions_high_value"]) else "🟢"
    )
    lines = [
        f"## {icon} OPTCG price audit",
        "",
        f"- Priced cards: **{c['priced_cards']:,}**",
        f"- 🔴 Hard-ceiling phantoms (> {_money(report['thresholds']['hard_ceiling'])}, "
        f"fallback source): **{c['hard_ceiling_critical']}** "
        f"({c['hard_ceiling_review']} real-sale high{'s' if c['hard_ceiling_review'] != 1 else ''} "
        f"above ceiling, listed for awareness)",
        f"- 🟠 High-value tcg_id collisions (≥ {_money(report['thresholds']['collision_value_floor'])}): "
        f"**{c['tcg_id_collisions_high_value']}** "
        f"(of {c['tcg_id_collisions_total']} total; {c['tcg_id_collisions_price_drift']} with price drift)",
        f"- 🟠 Outlier suspects (non-authoritative source, ≥{report['thresholds']['outlier_ratio']:g}× class median): "
        f"**{c['outliers_high']}**",
        f"- ⚪ Outlier review (real-sale source, ≥{report['thresholds']['auth_outlier_ratio']:g}×): "
        f"**{c['outliers_low']}**",
        f"- ⚪ dotgg rejected-high queue (needs manual curation): **{c['dotgg_rejected_high']}**",
    ]

    if report["hard_ceiling"]:
        lines += ["", "### 🔴 Above hard ceiling"]
        for f in report["hard_ceiling"][:TOP_N]:
            tag = " ✅ real-sale (not critical)" if f["authoritative"] else " 🔴 PHANTOM"
            lines.append(f"- `{f['id']}` {_money(f['price'])} "
                         f"[{f['price_source']}]{tag} {f['rarity']}/{f['variant_type']} tcg={f['tcg_ids']}")

    if report["outliers"]["high"]:
        lines += ["", "### 🟠 Outlier suspects (non-authoritative source)"]
        for o in report["outliers"]["high"][:TOP_N]:
            lines.append(f"- `{o['id']}` {_money(o['price'])} [{o['price_source']}] "
                         f"{o['ratio']}× {o['rarity']}/{o['variant_type']} median {_money(o['class_median'])}")

    hv = report["tcg_id_collisions"]["high_value"]
    if hv:
        lines += ["", "### 🟠 High-value tcg_id collisions"]
        for g in hv[:TOP_N]:
            ids = ", ".join(f"`{m['id']}`" for m in g["members"])
            drift = " ⚠️ price drift" if g["price_drift"] else ""
            lines.append(f"- tcg `{g['tcg_id']}` → {_money(g['max_price'])}{drift}: {ids}")

    if report["dotgg_rejected_high"]:
        lines += ["", "### ⚪ dotgg rejected-high (curate in data/manual_prices.json)"]
        for r in report["dotgg_rejected_high"][:TOP_N]:
            lines.append(f"- `{r.get('card_id')}` dotgg {_money(r.get('dotgg_price'))} "
                         f"tcg={r.get('tcg_ids')}")

    return "\n".join(lines)


def write_step_summary(md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    except OSError as e:
        print(f"   (could not write step summary: {e})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Post-import OPTCG price audit (read-only).")
    ap.add_argument("--local", action="store_true", help="Target local D1 instead of remote")
    ap.add_argument("--no-fail", action="store_true",
                    help="Always exit 0 (report only, never fail CI on critical findings)")
    args = ap.parse_args()

    # The summary uses emoji + the wrangler banner carries non-cp1252 glyphs;
    # the Windows console defaults to cp1252 and would crash on them. CI is utf-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    print("1. Fetching priced cards from D1...")
    rows = fetch_rows(args.local)
    print(f"   {len(rows)} priced rows")

    print("2. Loading dotgg rejected-high queue...")
    rejected = load_rejected_high()
    print(f"   {len(rejected)} rejected-high entr{'y' if len(rejected) == 1 else 'ies'}")

    print("3. Building audit report...")
    report = build_report(rows, rejected)

    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = render_summary(report)
    print()
    print(summary)
    print()
    print(f"Full report: {OUT_AUDIT}")
    write_step_summary(summary)

    if report["has_critical"] and not args.no_fail:
        print(f"\nCRITICAL: {report['counts']['hard_ceiling']} hard-ceiling breach(es). Exiting 2.")
        sys.exit(2)


if __name__ == "__main__":
    main()
