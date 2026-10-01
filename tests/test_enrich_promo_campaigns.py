"""plan_updates: one SELECT + Python matching replaces ~381 unindexed UPDATEs."""

import sqlite3
from pathlib import Path

from scripts.enrich_ja_promo_campaigns import Signal, plan_updates, sqlite_int


def _sig(slug, campaign, lang="ja"):
    return Signal(slug=slug, campaign=campaign, distribution_method="event", lang=lang,
                  mode="category_title", bulbapedia_target="x")


def test_sqlite_int_matches_sqlite_cast():
    db = sqlite3.connect(":memory:")
    for v in ["085", "85", " 7", "12a", "abc", "", "-3", "+4", "1.9", 42]:
        assert sqlite_int(v) == db.execute("SELECT CAST(? AS INTEGER)", (v,)).fetchone()[0], v


def test_matches_like_the_old_sql_join_and_skips_tagged_cards():
    catalog = [
        {"card_id": "SVP-085", "lang": "ja", "set_id": "SVP", "local_id": "085", "campaign": None, "distribution_method": None},
        {"card_id": "svp-085", "lang": "en", "set_id": "svp", "local_id": "085", "campaign": None, "distribution_method": None},
        {"card_id": "SVP-12", "lang": "ja", "set_id": "svp", "local_id": "12", "campaign": "Munch", "distribution_method": "event"},
    ]
    stmts = plan_updates([(_sig("munch", "Munch"), [("SVP", 85), ("SVP", 12)])], catalog)
    assert len(stmts) == 1, "EN row untouched (other lang); SVP-12 already tagged"
    assert "WHERE card_id = 'SVP-085' AND lang = 'ja'" in stmts[0]


def test_last_signal_wins_and_the_sql_is_idempotent():
    schema = Path("schema.sql").read_text(encoding="utf-8")
    db = sqlite3.connect(":memory:")
    db.executescript(schema)
    db.execute("INSERT INTO ptcg_cards (card_id, lang, set_id, local_id, name, updated_at) "
               "VALUES ('SVP-1', 'ja', 'SVP', '001', 'x', 0)")
    updates = [(_sig("a", "First"), [("SVP", 1)]), (_sig("b", "Second"), [("svp", 1)])]

    def catalog():
        cur = db.execute("SELECT card_id, lang, set_id, local_id, campaign, distribution_method FROM ptcg_cards")
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

    stmts = plan_updates(updates, catalog())
    assert len(stmts) == 1
    for s in stmts:
        db.execute(s)
    assert db.execute("SELECT campaign FROM ptcg_cards").fetchone()[0] == "Second"
    assert plan_updates(updates, catalog()) == [], "a re-run plans nothing"
    before = db.total_changes
    for s in stmts:
        db.execute(s)
    assert db.total_changes == before, "the old statement re-run writes nothing"
