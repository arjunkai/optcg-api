"""Candidate list of stamped TCGPlayer prints to add as {base}_t{tcg_id} rows.

Phase C of docs/stamped-prints-plan.md. Reads the "unrepresented_stamped"
list the price build writes to data/price_mapping_report.json (stamped
products no card of ours is priced from), keeps those at or above --min-price
whose card number is a card we list, and checks each product's photo against
EVERY listed row of that number (official image, image_match.py). Japan-site
rows are left out: a Japanese print isn't the English product.

- similarity >= MATCH (0.95) to a listed row: "duplicate". It is the same
  print as that row, whose price link needs fixing instead. Not a candidate.
- otherwise: a candidate, merged into data/stamped_prints.json with
  "approved": false. Entries already in the file (approved or not) are never
  changed, so a review in progress is safe to re-run over.

Nothing here writes to D1. IDs go live only when a person sets
"approved": true and scripts/import-stamped-prints.js runs.

Also writes data/stamped_review.html: each candidate's TCGPlayer photo next
to the official images of the rows we already list, for the by-eye check.

Usage:
    python scripts/build_stamped_candidates.py                  # >= $50
    python scripts/build_stamped_candidates.py --min-price 0    # every print
    python scripts/build_stamped_candidates.py --dry-run        # print only
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_all_prices import DATA_DIR, STAMPED_FILE, stamp_for  # noqa: E402
from image_match import MATCH, similarity, tcgplayer_image  # noqa: E402
from map_prices_to_cards import load_cards  # noqa: E402

REPORT_FILE = DATA_DIR / "price_mapping_report.json"
REVIEW_FILE = DATA_DIR / "stamped_review.html"
JA_SITE = "//www.onepiece-cardgame.com"
API_BASE = "https://optcg-api.arjunbansal-ai.workers.dev"


def listed_rows(by_id: dict, number: str) -> list[dict]:
    """Every EN-site row of this card number (base + parallels/reprints)."""
    return [c for cid, c in by_id.items()
            if (c.get("base_id") or cid) == number and c.get("image_url")
            and JA_SITE not in c["image_url"]]


def classify(cands: list[dict], by_id: dict, vectors) -> list[dict]:
    """Each candidate with status (candidate / duplicate / no_base) and its
    closest listed row."""
    urls = {tcgplayer_image(c["tcg_id"]) for c in cands}
    urls |= {r["image_url"] for c in cands for r in listed_rows(by_id, c["number"])}
    vectors.ensure(urls)
    out = []
    for c in cands:
        rows = listed_rows(by_id, c["number"])
        item = {**c, "status": "candidate", "closest": None, "closest_score": None, "listed": [r["id"] for r in rows]}
        if c["number"] not in by_id:
            item["status"] = "no_base"
            out.append(item)
            continue
        photo = vectors.get(tcgplayer_image(c["tcg_id"]))
        scored = [(similarity(photo, vectors.get(r["image_url"])), r["id"]) for r in rows]
        scored = [(s, cid) for s, cid in scored if s is not None]
        if scored:
            s, cid = max(scored)
            item["closest"], item["closest_score"] = cid, round(s, 3)
            if s >= MATCH:
                item["status"] = "duplicate"
        out.append(item)
    return out


def merge_into_stamped(items: list[dict], existing: dict) -> tuple[dict, int]:
    """existing + new candidates (approved: false). Never edits an existing key."""
    merged = dict(existing)
    added = 0
    for it in items:
        key = str(it["tcg_id"])
        if it["status"] != "candidate" or key in merged:
            continue
        _, stamp = stamp_for(it["label"])
        merged[key] = {
            "base": it["number"],
            "label": it["label"],
            "stamp": stamp,
            "approved": False,
            "price_seen": it["price"],
            "closest_listed": it["closest"],
            "closest_score": it["closest_score"],
            "found": date.today().isoformat(),
        }
        added += 1
    return merged, added


def review_html(items: list[dict], by_id: dict) -> str:
    def img(url, cap):
        return (f'<figure><img loading="lazy" src="{html.escape(url)}"><figcaption>{html.escape(cap)}'
                f'</figcaption></figure>')
    blocks = []
    for it in items:
        # Our own image route: Bandai blocks hot-linked scans, so the official
        # image_url renders broken outside onepiece-cardgame.com.
        listed = "".join(img(f"{API_BASE}/images/{c}", c) for c in it["listed"][:6])
        score = "" if it["closest_score"] is None else f" · closest {it['closest']} {it['closest_score']}"
        blocks.append(
            f'<section class="{it["status"]}"><h2>{html.escape(it["number"])}_t{it["tcg_id"]} '
            f'<small>{html.escape(it["label"])} · ${it["price"]} · {it["status"]}{html.escape(score)}</small></h2>'
            f'<div class="row">{img(tcgplayer_image(it["tcg_id"]), "TCGPlayer " + str(it["tcg_id"]))}'
            f'<span class="vs">vs listed</span>{listed or "<em>no listed rows</em>"}</div></section>')
    return ("<!doctype html><meta charset=utf-8><title>Stamped print review</title><style>"
            "body{font:14px system-ui;margin:16px;background:#111;color:#eee}section{margin:0 0 24px}"
            "section.duplicate h2{color:#f88}section.no_base h2{color:#fc6}h2{font-size:15px}small{color:#aaa}"
            ".row{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start}figure{margin:0;width:150px}"
            "img{width:150px;border-radius:6px}figcaption{font-size:11px;color:#aaa}.vs{align-self:center;color:#888}"
            "</style><h1>Stamped prints to review</h1><p>Approve by setting \"approved\": true in "
            "data/stamped_prints.json. Red: looks like a listed row (fix that row's link instead). "
            "Amber: card number not listed.</p>" + "".join(blocks))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-price", type=float, default=50.0)
    ap.add_argument("--dry-run", action="store_true", help="Print the list; write nothing")
    args = ap.parse_args()

    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    cands = [r for r in report.get("unrepresented_stamped") or [] if (r.get("price") or 0) >= args.min_price]
    by_id, _ = load_cards()
    from image_match import Vectors
    vectors = Vectors()
    items = classify(cands, by_id, vectors)
    vectors.save()

    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("candidate", "duplicate", "no_base")}
    print(f"Stamped prints >= ${args.min_price:g}: {len(items)} "
          f"({counts['candidate']} candidates, {counts['duplicate']} look like a listed row, "
          f"{counts['no_base']} with no listed base)")
    for it in items:
        if it["status"] != "candidate":
            print(f"  {it['status']:9} {it['number']}_t{it['tcg_id']} {it['label']!r} ${it['price']} "
                  f"closest {it['closest']} {it['closest_score']}")
    if args.dry_run:
        return

    existing = json.loads(STAMPED_FILE.read_text(encoding="utf-8")) if STAMPED_FILE.exists() else {}
    merged, added = merge_into_stamped(items, existing)
    STAMPED_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REVIEW_FILE.write_text(review_html(items, by_id), encoding="utf-8")
    print(f"Added {added} unapproved candidate(s) to {STAMPED_FILE}; review sheet: {REVIEW_FILE}")


if __name__ == "__main__":
    main()
