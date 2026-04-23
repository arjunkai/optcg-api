"""
fetch_card_image.py — finds a high-quality eBay seller photo for a card,
auto-crops it to the card bounds, and uploads to R2.

Designed for cards where the official site has no clean scan (JP-exclusive
Championship prizes etc.). The weekly pipeline handles prices via
`price_jp_exclusives.py`; this is the image-fetching counterpart.

How it picks the best photo:
  1. eBay Browse search built from the card's `note` field (same pattern
     price_jp_exclusives.py uses) + card ID.
  2. Filter out slabs / graded / sealed / proxy listings by title.
  3. Download up to --top N candidates at s-l1600 (eBay's max resolution).
  4. Score each candidate by:
       - card_fill  = fraction of the frame occupied by the card, detected
                      via row/column luminance variance. Seller photos
                      with tight framing score higher than ones with big
                      empty backgrounds.
       - sharpness  = Laplacian variance (standard no-reference metric).
                      Crisp photos beat blurry ones.
     score = card_fill * sqrt(sharpness), which balances the two.
  5. Auto-crop the winner to the detected card bounds + 4px pad.
  6. Save as PNG, upload to R2 at cards/{card_id}.png, purge /cards/all.

Usage (from optcg-api repo root):
  python -m scripts.fetch_card_image P-001_jp1              # one JP exclusive
  python -m scripts.fetch_card_image OP01-001               # ANY card — looks up
                                                            # name from D1, then
                                                            # searches eBay
  python -m scripts.fetch_card_image P-001_jp1 --dry-run    # preview only
  python -m scripts.fetch_card_image --all                  # every jp_exclusives entry
  python -m scripts.fetch_card_image P-001_jp1 --top 15     # consider more candidates

Lookup priority when a card_id is passed:
  1. data/jp_exclusives.json — if present, uses its note / image_search_query
     for a tight variant-specific search.
  2. API /cards/{id}         — otherwise, pulls the card's name from D1 and
     builds a generic "{name} {id} one piece TCG" query. Good enough for
     most alt arts / promos where the card ID is unique enough.
  3. Not found               — exits non-zero.

Environment:
  EBAY_APP_ID, EBAY_CERT_ID  (same as price_jp_exclusives.py)
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image, ImageFilter

try:
    import numpy as np
except ImportError:
    print("Install numpy: pip install numpy", file=sys.stderr)
    sys.exit(1)

from scripts.ebay_client import EbayClient, apply_title_filters


JSON_PATH = Path("data/jp_exclusives.json")
R2_BUCKET = "optcg-images"
R2_KEY_PREFIX = "cards/"
PROXY_URL = "https://optcg-api.arjunbansal-ai.workers.dev"
OFFICIAL_IMG = "https://en.onepiece-cardgame.com/images/cardlist/card/{id}.png"
WRANGLER_CMD = ["npx", "wrangler"]

# Title terms that signal a photo we can't use: graded card inside a slab,
# sealed in plastic (glare), a proxy/custom, etc.
UNUSABLE_TERMS = (
    "psa", "bgs", "cgc", "slab", "graded",
    "tag 10", "tag10", "sealed",
    "lot", "complete set", "set of",
    "proxy", "custom art", "fan made", "fan-made", "replica",
)


def load_jp_entries(card_ids: list[str] | None) -> list[dict]:
    """Load entries from data/jp_exclusives.json, with any extra fields
    (note, image_search_query) that tune the search for JP-exclusive
    Championship variants."""
    blob = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    out = []
    for key, val in blob.items():
        if key.startswith("_") or not isinstance(val, dict):
            continue
        if card_ids and key not in card_ids:
            continue
        out.append({"id": key, **val, "_source": "jp_exclusives.json"})
    return out


def load_card_from_api(card_id: str) -> dict | None:
    """Fetch a card row from the live API (D1-backed) so the script works
    for any card, not just ones in jp_exclusives.json. Returns a dict
    matching the same shape used by load_jp_entries so the rest of the
    pipeline is source-agnostic."""
    url = f"{PROXY_URL}/cards/{card_id}"
    try:
        resp = httpx.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except httpx.HTTPError:
        return None
    # Mirror the jp_exclusives.json shape so build_query and the rest of
    # the pipeline don't need to branch on source.
    return {
        "id": data.get("id", card_id),
        "base_id": data.get("base_id") or data.get("id", card_id).split("_")[0],
        "name": data.get("name"),
        "note": None,
        "_source": "api/D1",
    }


def build_query(entry: dict) -> str:
    """Build an eBay search query for this card.

    Priority:
      1. entry["image_search_query"]  — explicit override (JP exclusives use
         this to lock onto a specific stamp variant).
      2. entry["note"]               — descriptive text from jp_exclusives.json
         combined with the card ID ("P-001 2022 Championship Set Promo").
      3. DON cards                    — use the distinctive character/variant
         name since sellers don't list synthetic "DON-NNN" IDs. Query looks
         like "One Piece DON card Green Compass".
      4. fallback                    — card name + card ID ("Monkey D Luffy
         P-001 one piece TCG"), used when the entry came from D1 and has no
         note field."""
    if entry.get("image_search_query"):
        return entry["image_search_query"]
    card_id = entry["id"].split("_")[0]
    note = (entry.get("note") or "").replace("(JP)", "").replace("(JPN)", "").strip()
    if note:
        return f"{card_id} {note}".strip()
    name_raw = (entry.get("name") or "").replace(".", " ").replace('"', "").strip()
    if card_id.startswith("DON-"):
        # "DON!! Card // Green Compass" -> "Green Compass"
        distinct = name_raw.split("//")[-1].strip() if "//" in name_raw else name_raw
        # Strip "DON!! Card" prefix in case the split didn't catch it.
        distinct = distinct.replace("DON!! Card", "").strip()
        return f"One Piece DON card {distinct}".strip()
    return f"{name_raw} {card_id} one piece TCG".strip()


def compute_required_match(entry: dict) -> str:
    """Return the substring that must appear in each eBay listing title for
    the listing to count as a match for this card.

    For ordinary cards this is just the card ID ("P-001") — unambiguous and
    protects against lookalike IDs bleeding into results.

    For DON cards the synthetic ID ("DON-001") never appears in listings, so
    instead we require the distinctive character/variant part of the name
    ("Green Compass") to be in the title. This keeps us on the right DON
    across the hundreds of DON printings."""
    cid = entry["id"]
    base_id = cid.split("_")[0]
    if base_id.startswith("DON-"):
        name = entry.get("name") or ""
        # "DON!! Card // Green Compass" -> "green compass"
        distinct = name.split("//")[-1].strip() if "//" in name else name
        distinct = distinct.replace("DON!! Card", "").strip().lower()
        return distinct or "don"
    return base_id


def detect_card_bounds(im: Image.Image, var_threshold_pct: float = 0.15) -> tuple[int, int, int, int]:
    """Find the bounding box of the card inside the seller photo by looking
    at luminance variance per row/column. Background (grey mat, wood, etc.)
    has low variance; the card itself has high variance (illustration,
    borders, text). Returns (left, top, right, bottom).

    var_threshold_pct controls how strictly we separate card from background.
    Low (0.10) catches subtle card edges but over-detects on textured
    backgrounds (mesh, wood grain). High (0.40) rejects texture but may
    clip a card's dark borders. `find_card_bounds_adaptive` tries several
    thresholds and returns the one whose bbox has the most card-like
    aspect ratio."""
    arr = np.asarray(im.convert("RGB"))
    gray = arr.mean(axis=2)
    row_var = gray.var(axis=1)
    col_var = gray.var(axis=0)
    row_t = row_var.max() * var_threshold_pct
    col_t = col_var.max() * var_threshold_pct
    rows_in = np.where(row_var > row_t)[0]
    cols_in = np.where(col_var > col_t)[0]
    if len(rows_in) == 0 or len(cols_in) == 0:
        # Fallback: return the full image (no crop)
        h, w = gray.shape
        return (0, 0, w, h)
    top, bottom = int(rows_in[0]), int(rows_in[-1])
    left, right = int(cols_in[0]), int(cols_in[-1])
    return (left, top, right, bottom)


# Real OP TCG cards have ~0.72 aspect ratio (width / height).
CARD_ASPECT = 0.72


def find_card_bounds_adaptive(im: Image.Image) -> tuple[tuple[int, int, int, int], float]:
    """Try a ladder of variance thresholds and keep the bbox whose aspect
    ratio is closest to a real card (0.72 w:h). Returns (bbox, aspect_diff)
    where aspect_diff is the absolute distance from 0.72 — lower is better.

    Rationale: on a clean background (grey mat, black felt) low thresholds
    detect the card cleanly. On a textured background (mesh, wood) the
    background itself exceeds low thresholds so the bbox bleeds to the
    frame edges and lands at a square 1.0 aspect. Raising the threshold
    forces the detector to ignore mild background texture and snap onto
    the card's high-contrast artwork. Whichever threshold produces the
    most card-shaped bbox wins."""
    best_bbox = None
    best_aspect_diff = float("inf")
    for threshold in (0.10, 0.15, 0.25, 0.35, 0.50):
        bbox = detect_card_bounds(im, var_threshold_pct=threshold)
        card_w = max(1, bbox[2] - bbox[0])
        card_h = max(1, bbox[3] - bbox[1])
        diff = abs(card_w / card_h - CARD_ASPECT)
        if diff < best_aspect_diff:
            best_bbox = bbox
            best_aspect_diff = diff
    return best_bbox, best_aspect_diff


def score_candidate(im: Image.Image) -> tuple[float, dict]:
    """Return (score, metrics) where higher score = better candidate.

    card_area  — pixel count inside the detected card bbox. Higher is better
                 (more detail in the binder when the image is displayed large).
    card_fill  — card_area / total image area. Penalises photos with huge
                 empty backgrounds; we'd rather pay bandwidth for card pixels
                 than for a seller's desk.
    sharpness  — variance of the Laplacian of the luminance channel. Blurry
                 photos have low Laplacian variance; crisp ones are high.
    score      — sqrt(card_area * sharpness) * card_fill. Rewards lots of
                 sharp on-card pixels AND a tight frame. sqrt keeps one
                 metric from swamping the other."""
    w, h = im.size
    (left, top, right, bottom), aspect_diff = find_card_bounds_adaptive(im)
    card_w = max(0, right - left)
    card_h = max(0, bottom - top)
    card_area = card_w * card_h
    total_area = w * h
    card_fill = card_area / total_area if total_area else 0

    gray = im.convert("L")
    # Laplacian via PIL's built-in edge filter approximates variance cheaply.
    edges = gray.filter(ImageFilter.FIND_EDGES)
    sharpness = float(np.asarray(edges).var())

    # Penalise bboxes that don't look card-shaped. aspect_diff < 0.05 is
    # basically a perfect card; > 0.20 means the detector was confused
    # by background texture and returned a near-square bbox.
    if aspect_diff < 0.05:
        aspect_bonus = 1.0
    elif aspect_diff < 0.15:
        aspect_bonus = 0.8
    else:
        aspect_bonus = 0.3

    score = math.sqrt(card_area * sharpness) * card_fill * aspect_bonus
    return score, {
        "size": f"{w}x{h}",
        "card_px": f"{card_w}x{card_h}",
        "card_fill": round(card_fill, 3),
        "aspect_diff": round(aspect_diff, 3),
        "sharpness": round(sharpness, 1),
        "score": round(score, 0),
        "bbox": (left, top, right, bottom),
    }


def ebay_candidates(client: EbayClient, query: str, required_id: str,
                    limit: int = 50) -> list[dict]:
    """Return eBay listings matching `query` whose title contains `required_id`.
    Without the ID filter, eBay's relevance algo returns listings for nearby
    cards ("P-083 Championship") that share keywords with our query. The
    require-in-title check keeps us on the exact card."""
    raw = client.search(query, limit=limit)
    filtered = apply_title_filters(raw, blocklist=UNUSABLE_TERMS)
    required_lower = required_id.lower()
    on_card = [it for it in filtered
               if required_lower in (it.get("title") or "").lower()]
    # Prefer listings that mention NM / mint condition — cheap signal that
    # the photo shows a clean card rather than a damaged or heavily-played one.
    def has_quality(it):
        t = (it.get("title") or "").lower()
        return any(s in t for s in ("nm", "near mint", "mint", "unplayed"))
    on_card.sort(key=lambda it: (0 if has_quality(it) else 1))
    return on_card


def fetch_image(img_url: str, dest: Path) -> bool:
    # eBay thumbnails come through as s-l225.jpg; bump to s-l1600 for max res.
    hi = img_url.replace("s-l225.jpg", "s-l1600.jpg").replace("s-l500.jpg", "s-l1600.jpg")
    try:
        with httpx.stream("GET", hi, timeout=30, follow_redirects=True) as r:
            if r.status_code != 200:
                return False
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest.stat().st_size > 5000  # Tiny files are eBay placeholders
    except httpx.HTTPError:
        return False


def pick_best(client: EbayClient, query: str, required_id: str,
              top_n: int, workdir: Path) -> tuple[Path, dict] | None:
    print(f"    query: {query!r}  (require '{required_id}' in title)")
    candidates = ebay_candidates(client, query, required_id)[:top_n]
    if not candidates:
        print("    [no candidates] query returned 0 usable listings")
        return None

    best = (None, None, None, -1.0)
    for idx, it in enumerate(candidates):
        img = (it.get("image") or {}).get("imageUrl") or ""
        if not img:
            continue
        tmp = workdir / f"cand_{idx:02d}.jpg"
        if not fetch_image(img, tmp):
            continue
        try:
            im = Image.open(tmp)
            im.load()
        except Exception:
            continue
        score, metrics = score_candidate(im)
        title = (it.get("title") or "")[:45]
        print(f"    [{idx:02d}] score={metrics['score']:>8.0f}  card={metrics['card_px']:>9}  "
              f"fill={metrics['card_fill']:.2f}  aspD={metrics['aspect_diff']:.2f}  "
              f"sharp={metrics['sharpness']:>5.0f}  {title}")
        if score > best[3]:
            best = (tmp, im, metrics, score)
    if best[0] is None:
        return None
    print(f"    -> picked candidate with score={best[3]:.0f} (card px {best[2]['card_px']})")
    return best[0], best[2]


def crop_and_save(src_path: Path, out_path: Path, pad: int = 4) -> tuple[int, int]:
    im = Image.open(src_path).convert("RGB")
    w, h = im.size
    (left, top, right, bottom), _ = find_card_bounds_adaptive(im)
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(w, right + pad)
    bottom = min(h, bottom + pad)
    cropped = im.crop((left, top, right, bottom))
    cropped.save(out_path, "PNG", optimize=True)
    return cropped.size


def upload_to_r2(png_path: Path, card_id: str) -> None:
    key = f"{R2_KEY_PREFIX}{card_id}.png"
    # wrangler r2 object put requires the --file arg be a regular path (not
    # a tempdir path with spaces on Windows), so copy to the repo root first.
    target = Path(f"{card_id}.png")
    target.write_bytes(png_path.read_bytes())
    try:
        subprocess.run(
            WRANGLER_CMD + ["r2", "object", "put", f"{R2_BUCKET}/{key}",
                            f"--file={target}", "--remote"],
            check=True, shell=(sys.platform == "win32"),
        )
    finally:
        target.unlink(missing_ok=True)


def purge_cards_all() -> None:
    """Poke the Workers edge cache so /cards/all refreshes. Images in R2 are
    served fresh on every request, but the card list is cached for 1h."""
    try:
        httpx.get(f"{PROXY_URL}/cards/all?refresh=1", timeout=30)
    except httpx.HTTPError as exc:
        print(f"    [warn] failed to purge edge cache: {exc}")


def has_clean_official_image(card_id: str) -> bool:
    """Return True if en.onepiece-cardgame.com already serves a clean scan
    for this card. Bandai's official publicity images are ~514x720 PNG and
    watermark-free for regular cards — beats any eBay seller photo we could
    find. Only replace when the official site has nothing (404), which is
    the case for JP-exclusive Championship variants, DON cards, and some
    event-only promos.

    Tests the URL with HEAD so we don't download bytes just to check."""
    url = OFFICIAL_IMG.format(id=card_id)
    try:
        resp = httpx.head(url, timeout=10, follow_redirects=True)
        return resp.status_code == 200
    except httpx.HTTPError:
        # Treat network errors as "no" — err on the side of trying eBay.
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("card_id", nargs="?", help="Card ID to fetch (e.g. P-001_jp1)")
    ap.add_argument("--all", action="store_true", help="Fetch every entry in jp_exclusives.json")
    ap.add_argument("--top", type=int, default=10, help="Consider top N candidates (default 10)")
    ap.add_argument("--dry-run", action="store_true", help="Pick + show, don't upload")
    ap.add_argument("--force", action="store_true",
                    help="Fetch even if the official site already has a clean image "
                         "for this card. By default we skip those to avoid regressing "
                         "from Bandai's ~514x720 official scans to an eBay seller photo.")
    ap.add_argument("--min-card-px", type=int, default=800_000,
                    help="Reject any candidate whose detected card bbox has fewer pixels "
                         "than this. Default 800,000 (~780x1025) — beats the TCGPlayer "
                         "1000x1000 fallback DON cards use, and avoids replacing a decent "
                         "image with an eBay lot-photo crop.")
    args = ap.parse_args()

    if not args.card_id and not args.all:
        ap.error("pass a card_id or --all")

    if args.all:
        entries = load_jp_entries(None)
        if not entries:
            print(f"No entries in {JSON_PATH}")
            sys.exit(1)
    else:
        # Try the JP exclusives JSON first — it has the richest metadata
        # (note, image_search_query). If not there, fall back to the D1 row
        # via the API so this works for any card ID.
        entries = load_jp_entries([args.card_id])
        if not entries:
            print(f"{args.card_id} not in {JSON_PATH}, looking up via API…")
            row = load_card_from_api(args.card_id)
            if not row:
                print(f"  [error] card {args.card_id} not found in D1 either")
                sys.exit(1)
            entries = [row]
            print(f"  using query built from card name: {row.get('name')!r}")

    client = EbayClient()
    client.get_token()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        uploaded = 0
        for i, entry in enumerate(entries, start=1):
            print(f"\n[{i}/{len(entries)}] {entry['id']}")
            if not args.force and has_clean_official_image(entry["id"]):
                print(f"    [skip] en.onepiece-cardgame.com already has a clean scan "
                      f"for {entry['id']} (use --force to override)")
                continue
            query = build_query(entry)
            required_match = compute_required_match(entry)
            pick = pick_best(client, query, required_match, args.top, workdir)
            if not pick:
                print("    [skip] no usable candidate found")
                continue
            src_path, metrics = pick
            bbox = metrics.get("bbox") or (0, 0, 0, 0)
            card_px = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            if card_px < args.min_card_px:
                print(f"    [skip] best candidate has only {card_px:,} card pixels "
                      f"(<{args.min_card_px:,}) — would be a downgrade, leaving as is")
                continue
            out_path = workdir / f"{entry['id']}.png"
            size = crop_and_save(src_path, out_path)
            print(f"    cropped -> {size[0]}x{size[1]}  {out_path.stat().st_size // 1024} KB")
            if args.dry_run:
                final = Path(f"{entry['id']}.png")
                final.write_bytes(out_path.read_bytes())
                print(f"    [dry-run] saved locally to {final}")
                continue
            upload_to_r2(out_path, entry["id"])
            print(f"    -> uploaded to r2://{R2_BUCKET}/{R2_KEY_PREFIX}{entry['id']}.png")
            uploaded += 1
            time.sleep(0.3)  # Be polite to eBay

        if uploaded and not args.dry_run:
            print(f"\nPurging /cards/all edge cache...")
            purge_cards_all()
            print(f"Done. {uploaded}/{len(entries)} uploaded.")


if __name__ == "__main__":
    main()
