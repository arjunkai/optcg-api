"""
One-shot fetch of dotgg.gg's catalog — {card_id: tcg_id, price, foilPrice, ...}.
Saved to data/dotgg_catalog.json for the mapper + backfill scripts to consume.

dotgg is the authoritative tcg_id <-> card_id mapping. We use it to know
which TCGPlayer product corresponds to our _p8, _r2, etc. slots, which we
can't figure out positionally because TCGPlayer sometimes interleaves cheap
promo variants with expensive alt arts.

Usage:
  python scripts/fetch_dotgg_catalog.py
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0"
BASE = "https://api.dotgg.gg/cgfw"
BATCH_SIZE = 200
OUT_FILE = Path("data/dotgg_catalog.json")

# HTTP statuses worth retrying — gateway/upstream blips, not client errors.
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def fetch_json(url: str, *, max_retries: int = 4, timeout: int = 30):
    """GET ``url`` and parse JSON, retrying transient failures with exponential
    backoff. This step gates the whole price build (it is not continue-on-error),
    so a single dotgg 504 must not fail the run. Retries 5xx/408/429 and
    connection/timeout errors (2**attempt backoff); fails fast on 4xx and raises
    loudly after ``max_retries`` so a real outage still surfaces.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in TRANSIENT_STATUS:
                raise  # client/endpoint error — fail fast, don't retry
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException) as e:
            # HTTPException covers IncompleteRead: the stream dropped
            # mid-body (2026-11-09 run died on exactly this).
            last_err = e
        if attempt < max_retries - 1:
            wait = 2 ** attempt
            print(f"  dotgg fetch transient error ({last_err}); "
                  f"retry {attempt + 1}/{max_retries - 1} in {wait}s")
            time.sleep(wait)
    raise RuntimeError(
        f"dotgg fetch failed after {max_retries} attempts: {last_err}"
    ) from last_err


def main() -> None:
    catalog: dict[str, dict] = {}
    page = 1
    while True:
        rq = urllib.parse.quote(json.dumps({"page": page, "pageSize": BATCH_SIZE}))
        url = f"{BASE}/getcardsfiltered?game=onepiece&rq={rq}"
        data = fetch_json(url)
        rows = data if isinstance(data, list) else data.get("data") or []
        if not rows:
            break
        for r in rows:
            cid = r.get("id")
            if cid:
                catalog[cid] = r
        print(f"  page {page}: +{len(rows)} (total {len(catalog)})")
        if len(rows) < BATCH_SIZE:
            break
        page += 1
        time.sleep(0.5)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(catalog)} cards to {OUT_FILE}")


if __name__ == "__main__":
    main()
