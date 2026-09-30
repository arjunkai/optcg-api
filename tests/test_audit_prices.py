"""Unit tests for the pure detection logic in scripts/audit_prices.py.

The D1 fetch / wrangler / file-IO / step-summary writing is the impure shell
and is exercised live in CI. Everything tested here is pure: given rows, it
computes medians, collisions, outliers, and the final ranked report — no
network, no filesystem.
"""

from scripts.audit_prices import (
    parse_tcg_ids,
    median,
    class_key,
    compute_class_medians,
    find_tcg_id_collisions,
    find_hard_ceiling,
    find_outliers,
    build_report,
    AUTH_SOURCES,
    HARD_CEILING,
)


def row(id, price, *, rarity="Common", variant_type=None, source="tcgplayer", tcg_ids=None):
    return {
        "id": id,
        "name": id,
        "rarity": rarity,
        "variant_type": variant_type,
        "price": price,
        "price_source": source,
        "tcg_ids": tcg_ids,
    }


# ── parse_tcg_ids ────────────────────────────────────────────────────────────

def test_parse_tcg_ids_single():
    assert parse_tcg_ids("[485262]") == [485262]


def test_parse_tcg_ids_multi():
    assert parse_tcg_ids("[615592,615593]") == [615592, 615593]


def test_parse_tcg_ids_empty_and_null():
    assert parse_tcg_ids("[]") == []
    assert parse_tcg_ids(None) == []
    assert parse_tcg_ids("") == []


def test_parse_tcg_ids_garbage_is_skipped():
    # Defensive: a malformed value yields [] rather than raising.
    assert parse_tcg_ids("not-json") == []


# ── median ───────────────────────────────────────────────────────────────────

def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5


# ── class_key / class medians ─────────────────────────────────────────────────

def test_class_key_normalizes_null():
    assert class_key(row("X", 1, rarity=None, variant_type=None)) == ("", "")
    assert class_key(row("X", 1, rarity="Rare", variant_type="Manga Art")) == ("Rare", "Manga Art")


def test_compute_class_medians_excludes_small_classes():
    # 5 Common/Alt-Art cards form a stable class; a lone Rare/Manga does not.
    rows = [row(f"C{i}", p, rarity="Common", variant_type="Alternate Art")
            for i, p in enumerate([10, 12, 14, 16, 18])]
    rows.append(row("R1", 9999, rarity="Rare", variant_type="Manga Art"))
    meds = compute_class_medians(rows, min_class_size=5)
    assert meds[("Common", "Alternate Art")] == 14
    assert ("Rare", "Manga Art") not in meds  # too small to trust


# ── tcg_id collisions ─────────────────────────────────────────────────────────

def test_collision_exact_duplicate():
    rows = [
        row("A", 100, tcg_ids="[111]"),
        row("B", 100, tcg_ids="[111]"),
        row("C", 5, tcg_ids="[222]"),  # singleton, not a collision
    ]
    groups = find_tcg_id_collisions(rows)
    assert len(groups) == 1
    g = groups[0]
    assert g["tcg_id"] == 111
    assert sorted(m["id"] for m in g["members"]) == ["A", "B"]
    assert g["max_price"] == 100
    assert g["distinct_prices"] == 1


def test_collision_partial_overlap_in_multi_array():
    # A=[615592,615593], B=[615592] share 615592 even though the array
    # strings differ — exact-string GROUP BY would miss this.
    rows = [
        row("A", 50, tcg_ids="[615592,615593]"),
        row("B", 800, tcg_ids="[615592]"),
    ]
    groups = find_tcg_id_collisions(rows)
    assert len(groups) == 1
    assert groups[0]["tcg_id"] == 615592
    assert groups[0]["max_price"] == 800


def test_collision_price_drift_flag():
    rows = [
        row("A", 399.91, tcg_ids="[557283]"),
        row("B", 1170.94, tcg_ids="[557283]"),
    ]
    g = find_tcg_id_collisions(rows)[0]
    assert g["distinct_prices"] == 2
    assert g["price_drift"] is True


# ── hard ceiling ──────────────────────────────────────────────────────────────

def test_hard_ceiling_flags_only_above():
    rows = [
        row("PHANTOM", 87500, source="dotgg"),
        row("REAL", 8500, source="manual"),
    ]
    flagged = find_hard_ceiling(rows, HARD_CEILING)
    assert [f["id"] for f in flagged] == ["PHANTOM"]


def test_hard_ceiling_tags_source_authority():
    rows = [
        row("PHANTOM", 87500, source="dotgg"),
        row("GENUINE", 12000, source="manual"),
        row("REALSALE", 11000, source="tcgplayer"),
    ]
    flagged = {f["id"]: f for f in find_hard_ceiling(rows, HARD_CEILING)}
    assert flagged["PHANTOM"]["authoritative"] is False
    assert flagged["GENUINE"]["authoritative"] is True
    assert flagged["REALSALE"]["authoritative"] is True


def test_genuine_high_from_auth_source_is_not_critical():
    # A real $12k championship card pinned in manual_prices.json is listed
    # for awareness but must NOT fail CI.
    rows = [row("GENUINE", 12000, source="manual")]
    rep = build_report(rows, [])
    assert rep["counts"]["hard_ceiling"] == 1
    assert rep["counts"]["hard_ceiling_review"] == 1
    assert rep["counts"]["hard_ceiling_critical"] == 0
    assert rep["has_critical"] is False


def test_high_from_fallback_source_is_critical():
    rows = [row("PHANTOM", 12000, source="dotgg")]
    rep = build_report(rows, [])
    assert rep["counts"]["hard_ceiling_critical"] == 1
    assert rep["has_critical"] is True


# ── outliers vs class median ──────────────────────────────────────────────────

def _common_alt_class(extra):
    base = [row(f"C{i}", p, rarity="Common", variant_type="Alternate Art")
            for i, p in enumerate([10, 12, 14, 16, 18])]  # median 14
    return base + extra


def test_outlier_nonauth_flagged_high():
    # positional card at ~425x the class median → HIGH suspect.
    rows = _common_alt_class([
        row("SUS", 5954, rarity="Common", variant_type="Alternate Art", source="positional"),
    ])
    meds = compute_class_medians(rows)
    outs = find_outliers(rows, meds, ratio=10, auth_ratio=25, abs_floor=50)
    sus = [o for o in outs if o["id"] == "SUS"]
    assert len(sus) == 1
    assert sus[0]["severity"] == "high"


def test_outlier_auth_below_auth_ratio_not_flagged_high():
    # A tcgplayer (real-sale) card at 10x median is NOT a HIGH suspect — real
    # sales are trusted; it only shows in the lower-priority review list once it
    # crosses auth_ratio.
    rows = _common_alt_class([
        row("CHASE", 140, rarity="Common", variant_type="Alternate Art", source="tcgplayer"),
    ])  # 140 / 14 = 10x  → below auth_ratio 25
    meds = compute_class_medians(rows)
    outs = find_outliers(rows, meds, ratio=10, auth_ratio=25, abs_floor=50)
    chase = [o for o in outs if o["id"] == "CHASE"]
    assert chase == []


def test_outlier_auth_above_auth_ratio_flagged_low():
    rows = _common_alt_class([
        row("CHASE", 500, rarity="Common", variant_type="Alternate Art", source="tcgplayer"),
    ])  # 500 / 14 ≈ 35x ≥ auth_ratio 25
    meds = compute_class_medians(rows)
    outs = find_outliers(rows, meds, ratio=10, auth_ratio=25, abs_floor=50)
    chase = [o for o in outs if o["id"] == "CHASE"]
    assert len(chase) == 1
    assert chase[0]["severity"] == "low"


def test_outlier_below_abs_floor_ignored():
    # A cheap card 10x its tiny-median class is not worth flagging.
    rows = [row(f"P{i}", p, rarity="Common", variant_type=None)
            for i, p in enumerate([0.5, 0.6, 0.7, 0.8, 0.9])]  # median 0.7
    rows.append(row("CHEAP", 30, rarity="Common", variant_type=None, source="dotgg"))  # 42x but $30 < floor 50
    meds = compute_class_medians(rows)
    outs = find_outliers(rows, meds, ratio=10, auth_ratio=25, abs_floor=50)
    assert [o for o in outs if o["id"] == "CHEAP"] == []


def test_outlier_skips_card_with_no_class_median():
    # Card in a too-small class has no median → can't be ratio-tested.
    rows = [row("LONE", 9999, rarity="Leader", variant_type="Serial", source="dotgg")]
    meds = compute_class_medians(rows)  # class size 1 → excluded
    outs = find_outliers(rows, meds, ratio=10, auth_ratio=25, abs_floor=50)
    assert outs == []


# ── full report ───────────────────────────────────────────────────────────────

def test_build_report_counts_and_rejected_queue():
    rows = _common_alt_class([
        row("PHANTOM", 87500, rarity="Common", variant_type="Alternate Art", source="dotgg"),
        row("DUPA", 200, rarity="Common", variant_type="Alternate Art", tcg_ids="[999]"),
        row("DUPB", 200, rarity="Common", variant_type="Alternate Art", tcg_ids="[999]"),
    ])
    rejected = [{"card_id": "OP01-XXX", "dotgg_price": 500.0}]
    rep = build_report(rows, rejected)
    assert rep["counts"]["hard_ceiling"] == 1
    assert rep["counts"]["dotgg_rejected_high"] == 1
    assert rep["counts"]["tcg_id_collisions_total"] >= 1
    # PHANTOM is above the hard ceiling, so it drives the critical exit signal.
    assert rep["has_critical"] is True


def test_auth_sources_membership():
    assert "tcgplayer" in AUTH_SOURCES
    assert "manual" in AUTH_SOURCES
    assert "dotgg" not in AUTH_SOURCES
    assert "positional" not in AUTH_SOURCES
