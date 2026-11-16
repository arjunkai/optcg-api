"""Stamped-print rows ({base}_t{tcg_id}): price-build loading and the candidate list."""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_all_prices  # noqa: E402
from build_all_prices import load_stamped_prints  # noqa: E402
from build_stamped_candidates import classify, merge_into_stamped  # noqa: E402
from image_match import tcgplayer_image  # noqa: E402


def pic(seed):
    v = np.random.default_rng(seed).normal(size=792)
    return (v - v.mean()) / v.std()


class FakeVectors:
    def __init__(self, table):
        self.table = table

    def ensure(self, urls):
        pass

    def get(self, url):
        return self.table.get(url)


def test_load_stamped_prints_takes_only_approved_priced_entries(tmp_path, monkeypatch):
    f = tmp_path / "stamped.json"
    f.write_text(json.dumps({
        "_readme": "x",
        "579983": {"base": "OP07-109", "label": "CS 2024 Event Pack", "approved": True},
        "579984": {"base": "OP07-109", "label": "Judge Pack", "approved": False},
        "579985": {"base": "OP07-110", "label": "Gone", "approved": True},
    }), encoding="utf-8")
    monkeypatch.setattr(build_all_prices, "STAMPED_FILE", f)
    products = {579983: {"price": 3399.66}, 579984: {"price": 1.0}}
    assert load_stamped_prints(products) == {"OP07-109_t579983": 579983}


def test_classify_flags_a_print_that_looks_like_a_listed_row():
    listed, new = pic(1), pic(2)
    by_id = {
        "OP07-109": {"id": "OP07-109", "base_id": None, "image_url": "en/base"},
        "OP07-109_p1": {"id": "OP07-109_p1", "base_id": "OP07-109", "image_url": "en/p1"},
        # Japan-site row: never compared
        "OP07-109_p2": {"id": "OP07-109_p2", "base_id": "OP07-109",
                        "image_url": "https://www.onepiece-cardgame.com/images/cardlist/card/OP07-109_p2.png"},
    }
    vecs = FakeVectors({"en/base": pic(3), "en/p1": listed, by_id["OP07-109_p2"]["image_url"]: new,
                        tcgplayer_image(10): listed, tcgplayer_image(11): new})
    cands = [
        {"tcg_id": 10, "number": "OP07-109", "label": "CS 2024 Event Pack", "price": 99.0},
        {"tcg_id": 11, "number": "OP07-109", "label": "Winner", "price": 60.0},
        {"tcg_id": 12, "number": "ZZ99-001", "label": "Winner", "price": 60.0},
    ]
    got = {c["tcg_id"]: c for c in classify(cands, by_id, vecs)}
    assert got[10]["status"] == "duplicate" and got[10]["closest"] == "OP07-109_p1"
    assert got[11]["status"] == "candidate", "matches only the Japan-site row, which isn't compared"
    assert got[12]["status"] == "no_base"


def test_merge_adds_unapproved_candidates_and_never_edits_existing():
    existing = {"10": {"base": "OP07-109", "label": "kept as reviewed", "approved": True}}
    items = [
        {"tcg_id": 10, "number": "OP07-109", "label": "CS 2024 Event Pack", "price": 99.0, "status": "candidate",
         "closest": None, "closest_score": None},
        {"tcg_id": 11, "number": "OP07-109", "label": "2025 New Year Event Winner", "price": 60.0,
         "status": "candidate", "closest": "OP07-109_p1", "closest_score": 0.4},
        {"tcg_id": 12, "number": "OP07-109", "label": "Judge", "price": 60.0, "status": "duplicate",
         "closest": "OP07-109_p1", "closest_score": 0.99},
    ]
    merged, added = merge_into_stamped(items, existing)
    assert added == 1
    assert merged["10"] == existing["10"]
    assert merged["11"]["approved"] is False
    assert merged["11"]["stamp"] == "winner"
    assert "12" not in merged
