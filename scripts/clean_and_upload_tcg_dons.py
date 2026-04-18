"""
Download every DON card image from TCGPlayer, flood-fill the white corners to
transparent, and upload to R2 at `cards/DON-NNN.png`.

Skips DONs already curated from PDF (listed in data/don_image_mapping.json)
since those are higher-resolution and already in R2.

Usage:
  python scripts/clean_and_upload_tcg_dons.py              # all uncurated
  python scripts/clean_and_upload_tcg_dons.py --dry-run    # report only
  python scripts/clean_and_upload_tcg_dons.py DON-004 ...  # specific ids

Requires:
  - `npx wrangler` available (uploads via `wrangler r2 object put`)
  - Pillow installed in the active Python env
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import deque
from pathlib import Path
from PIL import Image

BUCKET = "optcg-images"
WHITE_THRESHOLD = 235
DON_CARDS_PATH = Path("data/don_cards.json")
MAPPING_PATH = Path("data/don_image_mapping.json")
USER_AGENT = "Mozilla/5.0 (optcg-api image migrator)"


def flood_white_to_transparent(img: Image.Image) -> tuple[Image.Image, int]:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()

    def is_white(x: int, y: int) -> bool:
        r, g, b, a = px[x, y]
        if a == 0:
            return False
        return r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD

    visited: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    for seed in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if is_white(*seed):
            queue.append(seed)
            visited.add(seed)

    cleared = 0
    while queue:
        x, y = queue.popleft()
        r, g, b, _ = px[x, y]
        px[x, y] = (r, g, b, 0)
        cleared += 1
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in visited and is_white(nx, ny):
                visited.add((nx, ny))
                queue.append((nx, ny))
    return img, cleared


def fetch_tcg_image(tcg_id: int) -> bytes | None:
    url = f"https://tcgplayer-cdn.tcgplayer.com/product/{tcg_id}_in_1000x1000.jpg"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except Exception as e:
        print(f"    FETCH ERROR: {e}")
        return None


def upload_to_r2(data: bytes, key: str) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(data)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["npx", "wrangler", "r2", "object", "put", f"{BUCKET}/{key}",
             "--file", tmp_path, "--content-type", "image/png", "--remote"],
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    UPLOAD ERROR: {result.stderr[:200]}")
            return False
        return True
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    id_filter = [a for a in args if a.startswith("DON-")]

    don_cards = json.loads(DON_CARDS_PATH.read_text(encoding="utf-8"))
    mapping = {}
    if MAPPING_PATH.exists():
        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))

    targets = []
    for c in don_cards:
        don_id = c["id"]
        if id_filter and don_id not in id_filter:
            continue
        if don_id in mapping:
            continue  # skip curated PDF mappings — higher res
        if not c.get("tcg_ids"):
            continue
        targets.append((don_id, c["tcg_ids"][0], c["name"]))

    print(f"Total DON cards: {len(don_cards)}")
    print(f"Already curated from PDF (skipped): {len(mapping)}")
    print(f"TCGPlayer-backed DONs to clean + upload: {len(targets)}")
    print()

    ok = fail = skip = 0
    for i, (don_id, tcg_id, name) in enumerate(targets, start=1):
        prefix = f"[{i:3}/{len(targets)}] {don_id} (tcg {tcg_id})"
        if dry_run:
            print(f"{prefix}  WOULD fetch + clean + upload  {name}")
            ok += 1
            continue

        raw = fetch_tcg_image(tcg_id)
        if raw is None:
            print(f"{prefix}  FAIL fetch")
            fail += 1
            continue

        try:
            img = Image.open(io.BytesIO(raw))
        except Exception as e:
            print(f"{prefix}  FAIL decode: {e}")
            fail += 1
            continue

        cleaned, cleared = flood_white_to_transparent(img)
        buf = io.BytesIO()
        cleaned.save(buf, "PNG")

        if upload_to_r2(buf.getvalue(), f"cards/{don_id}.png"):
            print(f"{prefix}  OK   cleared {cleared:,}px  {name}")
            ok += 1
        else:
            fail += 1

        time.sleep(0.05)  # be nice to TCGPlayer CDN

    print()
    print(f"{'[dry-run] ' if dry_run else ''}Done: {ok} ok, {fail} failed, {skip} skipped")
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
