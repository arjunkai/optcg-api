"""Decision rules for scripts/check_tcgplayer_coverage.py.

The 2026 incident: new sets had no slug (skipped) and starter decks timed out
(failed) for months while the run stayed green. These pin that both shapes
now fail the run, and that the legitimate exceptions don't.
"""

import datetime as dt

from scripts.check_tcgplayer_coverage import evaluate

TODAY = dt.date(2026, 11, 16)
OLD = TODAY - dt.timedelta(days=60)


def _cat(**released):
    # abbr_key -> group; group ids are arbitrary but distinct
    return {k: {"group_id": i, "released": d} for i, (k, d) in enumerate(released.items(), 1)}


def _crit(findings):
    return {(f["set_id"], f["check"]) for f in findings if f["critical"]}


def has_singles(_gid):
    return 50


def test_skipped_new_set_is_critical():
    f = evaluate(["OP-17"], {"OP-17": {"status": "skipped", "msg": "no slug mapped"}},
                 _cat(OP17=OLD), {}, has_singles, TODAY)
    assert _crit(f) == {("OP-17", "missed")}


def test_failed_starter_deck_is_critical():
    f = evaluate(["ST-14"], {"ST-14": {"status": "failed", "msg": "timeout"}},
                 _cat(ST14=OLD), {}, has_singles, TODAY)
    assert _crit(f) == {("ST-14", "missed")}


def test_failure_within_grace_is_not_critical():
    f = evaluate(["OP-18"], {"OP-18": {"status": "failed", "msg": "timeout"}},
                 _cat(OP18=TODAY - dt.timedelta(days=2)), {}, has_singles, TODAY)
    assert _crit(f) == set() and f[0]["check"] == "missed"


def test_group_with_no_singles_is_not_critical():
    # One Piece Collection Sets: TCGPlayer's guide has no singles any more.
    f = evaluate(["569801"], {"569801": {"status": "failed", "msg": "timeout"}},
                 _cat(OPCS=OLD), {}, lambda gid: 0, TODAY)
    assert _crit(f) == set()


def test_unknown_set_id_is_critical():
    f = evaluate(["XX-01"], {"XX-01": {"status": "ok", "msg": ""}}, _cat(OP01=OLD), {}, has_singles, TODAY)
    assert _crit(f) == {("XX-01", "no-group")}


def test_low_share_after_ok_scrape_is_critical():
    f = evaluate(["OP-16"], {"OP-16": {"status": "ok", "msg": ""}}, _cat(OP16=OLD),
                 {"OP-16": {"n": 156, "tcg": 3}}, has_singles, TODAY)
    assert _crit(f) == {("OP-16", "low-share")}


def test_healthy_set_and_young_low_share_pass():
    f = evaluate(["OP-13", "OP-18"],
                 {"OP-13": {"status": "ok", "msg": ""}, "OP-18": {"status": "ok", "msg": ""}},
                 _cat(OP13=OLD, OP18=TODAY - dt.timedelta(days=3)),
                 {"OP-13": {"n": 179, "tcg": 174}, "OP-18": {"n": 120, "tcg": 0}},
                 has_singles, TODAY)
    assert f == []


def test_singles_only_queried_when_needed():
    calls = []
    evaluate(["OP-13"], {"OP-13": {"status": "ok", "msg": ""}}, _cat(OP13=OLD),
             {"OP-13": {"n": 179, "tcg": 174}}, lambda gid: calls.append(gid) or 50, TODAY)
    assert calls == []
