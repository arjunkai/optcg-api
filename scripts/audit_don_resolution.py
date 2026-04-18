"""
Audit the resolution of every DON card image actually served in R2.

Flags any DON served at less than TARGET_MIN_WIDTH px as "low-res". For each
low-res DON, reports the best upgrade path: PDF curation (if the card's set
has unmatched PDF images available) or "accept/upscale" if no source exists.

Usage:
  python scripts/audit_don_resolution.py

Reads:
  - data/don_cards.json             (195 DONs)
  - data/don_image_map.json         (275 PDF images with set_id tags)
  - data/don_image_mapping.json     (curated DON -> PDF filename)

Probes each DON's current served size via HEAD on the API. Produces a
terminal report plus data/don_resolution_audit.json for scripting.
"""

from __future__ import annotations

import json
import urllib.request
from collections import defaultdict
from pathlib import Path
from PIL import Image
import io

API_BASE = "https://optcg-api.arjunbansal-ai.workers.dev"
TARGET_MIN_WIDTH = 700  # cards narrower than this look blurry when enlarged

DATA = Path("data")


def probe(don_id: str) -> tuple[int, int] | None:
    try:
        req = urllib.request.Request(
            f"{API_BASE}/images/{don_id}",
            headers={"User-Agent": "Mozilla/5.0 (resolution audit)"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        return Image.open(io.BytesIO(data)).size
    except Exception:
        return None


def main() -> None:
    cards = json.loads((DATA / "don_cards.json").read_text(encoding="utf-8"))
    pdf_imgs = json.loads((DATA / "don_image_map.json").read_text(encoding="utf-8"))
    mapping = json.loads((DATA / "don_image_mapping.json").read_text(encoding="utf-8"))

    pdf_by_set: dict[str, list[str]] = defaultdict(list)
    for i in pdf_imgs:
        if i.get("set_id"):
            pdf_by_set[i["set_id"]].append(i["filename"])

    used_pdfs = set(mapping.values())

    results = []
    print(f"Probing {len(cards)} DONs via {API_BASE}/images/...")
    for c in cards:
        size = probe(c["id"])
        if size is None:
            results.append({"id": c["id"], "set_id": c["set_id"], "width": None, "height": None, "status": "ERROR"})
            continue
        w, h = size
        curated = c["id"] in mapping
        set_id = c["set_id"]
        unused_pdfs = [f for f in pdf_by_set.get(set_id, []) if f not in used_pdfs]
        if w >= TARGET_MIN_WIDTH:
            status = "OK"
        elif curated:
            status = "LOW_RES_CURATED"  # already mapped to a PDF - nothing more to do
        elif unused_pdfs:
            status = f"CAN_UPGRADE_PDF ({len(unused_pdfs)} candidates in {set_id})"
        else:
            status = "STUCK_NO_SOURCE"
        results.append({
            "id": c["id"], "set_id": set_id, "width": w, "height": h,
            "status": status, "name": c["name"],
        })

    # Summary
    by_status = defaultdict(int)
    for r in results:
        key = r["status"].split(" ")[0]
        by_status[key] += 1

    print()
    print(f"{'Status':<25} Count")
    print("-" * 40)
    for k in sorted(by_status.keys()):
        print(f"{k:<25} {by_status[k]}")

    print()
    print("Stuck (no PDF source, will need alternate source or AI upscale):")
    for r in results:
        if r["status"] == "STUCK_NO_SOURCE":
            print(f"  {r['id']}  {r['set_id']:10}  {r['width']}x{r['height']}  {r['name']}")

    (DATA / "don_resolution_audit.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote detailed report to data/don_resolution_audit.json")


if __name__ == "__main__":
    main()
