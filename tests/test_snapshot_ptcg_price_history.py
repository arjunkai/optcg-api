"""Unit tests for the pure extraction in scripts/snapshot_ptcg_price_history.py."""

from scripts.snapshot_ptcg_price_history import history_rows


def test_all_tcgplayer_variants_one_cardmarket_series():
    pj = {
        "tcgplayer": {"holofoil": {"market": 12.5}, "reverseHolofoil": {"market": 3}, "normal": {"market": 0}},
        "cardmarket": {"avg": 9.1, "trend": 8.7, "avg7": 9.0, "lowFoil": 4.0},
    }
    assert history_rows(pj) == [
        ("tcgplayer", "holofoil", 12.5, None),
        ("tcgplayer", "reverseHolofoil", 3.0, None),
        ("cardmarket", "avg", None, 9.1),
    ]


def test_cardmarket_falls_back_in_chart_priority_order():
    assert history_rows({"cardmarket": {"avg": 0, "trend": None, "avg7": 2.2, "low": 1.0}}) == [
        ("cardmarket", "avg7", None, 2.2),
    ]


def test_single_price_sources_take_the_first_positive_key():
    pj = {"manual": {"price": 99}, "ebay": {"price": 0, "market": 5.5}, "yuyutei": {"note": "x"}}
    assert history_rows(pj) == [("manual", "market", 99.0, None), ("ebay", "market", 5.5, None)]


def test_junk_is_ignored():
    assert history_rows({"tcgplayer": "nope", "cardmarket": {"avg": True}, "manual": None}) == []


# ── change-only inserts, run against the real table definition ───────────

import sqlite3
from pathlib import Path

from scripts.snapshot_ptcg_price_history import _insert


def _history_db():
    db = sqlite3.connect(":memory:")
    db.executescript(Path("schema.sql").read_text(encoding="utf-8"))
    return db


def _run(db, rows, t):
    before = db.total_changes
    for card, source, variant, usd, eur in rows:
        db.execute(_insert(card, source, variant, usd, eur, t))
    return db.total_changes - before


def test_inserts_only_when_the_series_price_changes():
    db = _history_db()
    week1 = [("sv1-1", "tcgplayer", "holofoil", 12.5, None), ("sv1-1", "cardmarket", "avg", None, 9.1),
             ("sv1-2", "manual", "market", 3.0, None)]
    assert _run(db, week1, 100) == 3
    assert _run(db, week1, 200) == 0, "an unchanged week writes nothing"
    week3 = [("sv1-1", "tcgplayer", "holofoil", 13.0, None), ("sv1-1", "cardmarket", "avg", None, 9.1),
             ("sv1-2", "manual", "market", 3.0, None), ("sv1-3", "ebay", "market", 1.25, None)]
    assert _run(db, week3, 300) == 2, "one moved price + one new series"
    # Back to an earlier price is still a change from the latest row.
    assert _run(db, [("sv1-1", "tcgplayer", "holofoil", 12.5, None)], 400) == 1
    points = db.execute("SELECT recorded_at, price_usd FROM ptcg_price_history "
                        "WHERE card_id='sv1-1' AND variant='holofoil' ORDER BY recorded_at").fetchall()
    assert points == [(100, 12.5), (300, 13.0), (400, 12.5)]


def test_same_second_rerun_is_a_no_op():
    db = _history_db()
    row = [("sv1-1", "tcgplayer", "normal", 0.1 + 0.2, None)]
    assert _run(db, row, 100) == 1
    assert _run(db, row, 100) == 0
