"""
Clean up PDF-extracted DON card images.

The PDF extraction leaves white background visible around the rounded corners
of each card. This script flood-fills the white corner regions to transparent
so OPBindr's UI can apply its own border-radius without a white halo leaking
around the card edge.

Usage:
  python scripts/crop_don_images.py                          # all images
  python scripts/crop_don_images.py don_049.png don_053.png  # specific files
  python scripts/crop_don_images.py --dry-run                # report only

Writes in place to data/don_images/.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path
from PIL import Image

IMG_DIR = Path("data/don_images")
WHITE_THRESHOLD = 235  # any pixel with all channels >= this counts as white


def flood_white_to_transparent(img: Image.Image) -> tuple[Image.Image, int]:
    """Flood-fill white pixels starting from each corner, marking them transparent.

    Returns (new_image, pixels_cleared).
    """
    img = img.convert("RGBA")
    w, h = img.size
    pixels = img.load()

    def is_white(x: int, y: int) -> bool:
        r, g, b, a = pixels[x, y]
        if a == 0:
            return False  # already transparent
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
        r, g, b, _ = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)
        cleared += 1
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in visited and is_white(nx, ny):
                visited.add((nx, ny))
                queue.append((nx, ny))

    return img, cleared


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]

    files = [IMG_DIR / f for f in args] if args else sorted(IMG_DIR.glob("*.png"))
    if not files:
        print(f"No PNG files found in {IMG_DIR}")
        sys.exit(1)

    print(f"Processing {len(files)} image(s){' (dry-run)' if dry_run else ''}")

    total = 0
    for src in files:
        img = Image.open(src)
        w, h = img.size
        cleaned, cleared = flood_white_to_transparent(img)
        pct = cleared / (w * h) * 100
        if cleared == 0:
            print(f"  SKIP {src.name}: no white corners detected")
            continue
        total += 1
        msg = f"{src.name}: cleared {cleared:,} corner px ({pct:.1f}%)"
        if dry_run:
            print(f"  {msg}")
        else:
            cleaned.save(src, "PNG")
            print(f"  OK  {msg}")

    print(f"\n{'[dry-run] ' if dry_run else ''}Cleaned {total}/{len(files)} images")


if __name__ == "__main__":
    main()
