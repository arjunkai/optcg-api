"""Match our cards to TCGPlayer products by picture.

TCGPlayer's product photos for One Piece are the official card scans, so a
card's official image and the right product's photo are near-identical,
while a different print of the same card (another stamp, the Winner vs the
participation version, the manga vs the alt art) is clearly not:

    ST01-007_p5 (gold WINNER stamp)  vs  "2025 New Year Event Winner"  1.00
                                     vs  "2025 New Year Event"          0.50
    EB01-014_p1 (Championship stamp) vs  "CS 25-26 Celebration Pack"   1.00
                                     vs  "Tournament Pack 2025 Vol. 1"  0.52

Each image becomes a 24x33 grayscale thumbnail (792 bytes) cached in
data/image_vectors.json by URL, so a weekly run only downloads images it has
never seen. Similarity is the Pearson correlation of the two thumbnails.

Used by build_all_prices.py (verify_by_image) and the promo report.
"""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

CACHE = Path("data/image_vectors.json")
SIZE = (24, 33)  # card aspect ~ 0.72
UA = "Mozilla/5.0 (optcg-api image check)"

# Decision thresholds, from the calibration pairs above: right print >= 0.99,
# wrong print of the same card 0.35-0.55.
MATCH = 0.95       # a product this similar is the card
MARGIN = 0.15      # and beats the current mapping by at least this much
TIE = 0.02         # two products this close are indistinguishable


def tcgplayer_image(tcg_id: int) -> str:
    return f"https://tcgplayer-cdn.tcgplayer.com/product/{tcg_id}_200w.jpg"


class Vectors:
    """URL -> normalized thumbnail vector, backed by a JSON cache."""

    def __init__(self, path: Path = CACHE):
        self.path = path
        self.raw: dict[str, str | None] = {}
        if path.exists():
            self.raw = json.loads(path.read_text(encoding="utf-8"))
        self.fetched = 0

    def _download(self, url: str) -> str | None:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                im = ImageOps.fit(Image.open(io.BytesIO(data)).convert("L"), SIZE)
                return base64.b64encode(np.asarray(im, dtype=np.uint8).tobytes()).decode()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None  # remembered as missing
            except Exception:  # noqa: BLE001 — transient; retry
                pass
            time.sleep(1 + attempt)
        return ""  # transient failure: not cached, retried next run

    def ensure(self, urls, workers: int = 12) -> None:
        todo = sorted({u for u in urls if u and u not in self.raw})
        if not todo:
            return
        with ThreadPoolExecutor(workers) as ex:
            for url, val in zip(todo, ex.map(self._download, todo)):
                if val != "":
                    self.raw[url] = val
                    self.fetched += 1

    def get(self, url: str | None):
        val = self.raw.get(url) if url else None
        if not val:
            return None
        a = np.frombuffer(base64.b64decode(val), dtype=np.uint8).astype(float)
        sd = a.std()
        return None if sd < 1e-6 else (a - a.mean()) / sd

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.raw, separators=(",", ":")), encoding="utf-8")


def similarity(a, b) -> float | None:
    if a is None or b is None:
        return None
    return float((a * b).mean())


def decide(scores: dict[int, float | None], current: int | None) -> int | None:
    """The product a card should map to by picture, or None to keep current.

    scores: tcg_id -> similarity to the card's official image.
    Returns a different tcg_id only when it clearly matches (>= MATCH), beats
    the current mapping by MARGIN, and no other product ties with it.
    """
    known = {t: s for t, s in scores.items() if s is not None}
    if not known:
        return None
    best = max(known, key=known.get)
    if best == current or known[best] < MATCH:
        return None
    if any(t != best and known[best] - s < TIE for t, s in known.items()):
        return None
    cur = known.get(current) if current is not None else None
    if cur is not None and known[best] - cur < MARGIN:
        return None
    return best
