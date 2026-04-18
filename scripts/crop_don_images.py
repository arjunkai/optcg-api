"""
Clean up DON card images by clipping them to a rounded-rectangle shape.

Background: DON card images come from TCGPlayer (photos of cards on a white
background) or the official PDF (pages with white margins). Both have white
pixels both OUTSIDE the card's rounded border AND in small "pie slice" inner
corners between the rounded border and the card's content. OPBindr then
renders these with its own border-radius, which doesn't match the card's
border radius, leaving white halos visible at the 4 corners.

The fix: detect the card's content bounding box, fit a rounded rectangle to
that box, and clip the image to it. Anything outside becomes transparent.

Usage:
  python scripts/crop_don_images.py                          # all images
  python scripts/crop_don_images.py don_049.png don_053.png  # specific files
  python scripts/crop_don_images.py --dry-run                # report only

Writes in place to data/don_images/.
"""

from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image, ImageDraw

IMG_DIR = Path("data/don_images")
WHITE_THRESHOLD = 235
# Fraction of the shorter bbox side used as rounded-corner radius.
# Real DON cards have ~4-5% corner radius relative to the short side.
CORNER_RADIUS_FRACTION = 0.048


def find_content_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) — the smallest box containing any
    pixel that is NOT near-white background."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    def row_has_content(y: int) -> bool:
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            if r < WHITE_THRESHOLD or g < WHITE_THRESHOLD or b < WHITE_THRESHOLD:
                return True
        return False

    def col_has_content(x: int) -> bool:
        for y in range(0, h, 2):
            r, g, b = px[x, y]
            if r < WHITE_THRESHOLD or g < WHITE_THRESHOLD or b < WHITE_THRESHOLD:
                return True
        return False

    top = 0
    while top < h and not row_has_content(top):
        top += 1
    bottom = h - 1
    while bottom > top and not row_has_content(bottom):
        bottom -= 1
    left = 0
    while left < w and not col_has_content(left):
        left += 1
    right = w - 1
    while right > left and not col_has_content(right):
        right -= 1
    return left, top, right + 1, bottom + 1


def clip_to_rounded_card(img: Image.Image) -> Image.Image:
    """Apply a rounded-rectangle alpha mask sized to the card's content
    bounding box. Preserves original RGB; sets alpha=0 outside the mask.
    """
    img = img.convert("RGBA")
    w, h = img.size
    left, top, right, bottom = find_content_bbox(img)
    bw = right - left
    bh = bottom - top
    if bw < 10 or bh < 10:
        return img  # nothing useful

    radius = int(min(bw, bh) * CORNER_RADIUS_FRACTION)

    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (left, top, right - 1, bottom - 1),
        radius=radius,
        fill=255,
    )

    # Combine existing alpha (preserve already-transparent pixels) with mask
    existing_alpha = img.getchannel("A")
    from PIL import ImageChops
    new_alpha = ImageChops.multiply(existing_alpha, mask)

    img.putalpha(new_alpha)
    return img


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]

    files = [IMG_DIR / f for f in args] if args else sorted(IMG_DIR.glob("*.png"))
    if not files:
        print(f"No PNG files found in {IMG_DIR}")
        sys.exit(1)

    print(f"Processing {len(files)} image(s){' (dry-run)' if dry_run else ''}")
    for src in files:
        img = Image.open(src)
        w, h = img.size
        bbox = find_content_bbox(img.convert("RGB"))
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        pad_l, pad_t = bbox[0], bbox[1]
        pad_r, pad_b = w - bbox[2], h - bbox[3]
        msg = f"{src.name}: {w}x{h} bbox={bw}x{bh} pad(L={pad_l} T={pad_t} R={pad_r} B={pad_b})"
        if dry_run:
            print(f"  {msg}")
        else:
            out = clip_to_rounded_card(img)
            out.save(src, "PNG")
            print(f"  OK  {msg}")


if __name__ == "__main__":
    main()
