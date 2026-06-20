"""Yuyutei (yuyu-tei.jp) scraper for the One Piece TCG (`opc`) — pure data extraction.

Sibling of scripts/lib/yuyutei_scraper.py (the Pokemon `poc` scraper). One Piece
needs its own parser because the `opc` listing DOM differs from `poc` in two ways
that matter for matching:

  1. The number span carries the FULL card id ("OP16-118"), not Pokemon's
     "118/098" fraction. So no padding-candidate guessing is needed — the id maps
     straight onto our `cards.id`.
  2. Base vs parallel is explicit. Each card-product's <img alt> is
     "{number} {rarity} {name}", e.g. "OP16-118 P-SEC ポートガス・D・エース(パラレル)".
     A parallel is flagged by a `P-` rarity prefix (P-SEC/P-SR/P-R/P-UC/P-C/P-L),
     by the SP / TR rarities (which are always alt-art parallels in our data), or
     by a "(パラレル)" suffix on the name. Base cards carry the bare rarity
     (SEC/SR/R/UC/C/L).

This module only extracts data. The matcher (base->base, single-parallel->single-
parallel, multi-parallel->skip) and all SQL emission live in
scripts/backfill_yuyutei_opc.py.

Per-set listing flow:
  GET https://yuyu-tei.jp/sell/opc/s/{setcode}
    -> parse each <div class="card-product"> for:
        card_number  (full id, e.g. "OP16-118", from the <img alt> prefix)
        rarity_token (2nd token of the alt: SEC, P-SEC, SR, P-SR, SP, TR, ...)
        is_parallel  (P- prefix / SP / TR / "(パラレル)")
        name_ja      (text of <h4 class="text-primary fw-bold">)
        image_url    (https://card.yuyu-tei.jp/opc/100_140/{setcode}/{pid}.jpg)
        price_jpy    ("7,980 円" -> 7980)  (present even when sold out)
        in_stock     (在庫 : ◯ / N 点 = in stock; × = sold out)
        yuyutei_pid  (Yuyutei's internal product id, for the detail-page URL)
"""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

# Reuse the game-neutral FX layer from the Pokemon scraper lib (ECB rate via
# frankfurter.app, 2-day on-disk cache, hardcoded fallback). One source of FX
# truth across both games' JP backfills.
from scripts.lib.yuyutei_scraper import get_jpy_to_usd_rate  # noqa: F401  (re-exported)

LISTING_BASE = "https://yuyu-tei.jp/sell/opc/s"
IMAGE_HOST = "card.yuyu-tei.jp"
REQ_INTERVAL_S = 1.0
USER_AGENT = "opbindr-optcg-importer/1.0 (+https://opbindr.com; contact arjun@neuroplexlabs.com)"

# Every `opc` set page on Yuyutei (confirmed live 2026-06-18 from the set
# selector). DON cards have their own page too. Promos (P-001..) are NOT a
# single set page on Yuyutei (range-filtered) and are handled separately.
OPC_SET_CODES: list[str] = (
    [f"op{n:02d}" for n in range(1, 17)]       # op01 .. op16
    + [f"eb{n:02d}" for n in range(1, 5)]      # eb01 .. eb04
    + [f"st{n:02d}" for n in range(1, 31)]     # st01 .. st30
    + ["prb01", "prb02"]
    + ["don"]
)

# Bare (base) rarity tokens. Anything else with a "P-" prefix, or SP / TR, is a
# parallel/alt-art in our id-space (verified: Special + Treasure Rare are 100%
# parallel ids in D1).
BASE_RARITY_TOKENS = {"SEC", "SR", "R", "UC", "C", "L"}

# A full OPTCG card id: OP16-118, ST01-001, EB04-061, PRB02-005, DON-001, P-042.
CARD_ID_RE = re.compile(r"^[A-Z0-9]+-\d+$")


def _parse_alt(alt: str) -> tuple[str | None, str | None, str | None]:
    """Split an <img alt> "OP16-118 P-SEC ポートガス・D・エース(パラレル)" into
    (number, rarity_token, name). Returns (None, None, None) if it doesn't look
    like a card alt (e.g. the decorative star.svg's alt="Star")."""
    parts = alt.strip().split(None, 2)
    if len(parts) < 2:
        return None, None, None
    number, rarity = parts[0], parts[1]
    name = parts[2] if len(parts) == 3 else None
    if not CARD_ID_RE.match(number):
        return None, None, None
    return number, rarity, name


def scrape_opc_set(client: httpx.Client, setcode: str) -> list[dict] | None:
    """Parse a Yuyutei `opc` per-set listing page. Returns a list of row dicts,
    or None if the page 404s (set not on Yuyutei).

    Row dict keys: card_number, rarity, is_parallel, name_ja, image_url,
    price_jpy, in_stock, yuyutei_pid.
    """
    try:
        r = client.get(f"{LISTING_BASE}/{setcode}")
    except httpx.HTTPError as exc:
        print(f"    fetch error: {exc}")
        return None
    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    rows: list[dict] = []
    for div in soup.find_all("div", class_="card-product"):
        # The card scan image (NOT the decorative star.svg).
        img = div.find("img", src=lambda s: s and f"{IMAGE_HOST}/opc" in s)
        if img is None:
            continue
        alt = img.get("alt", "") or ""
        src = img.get("src", "") or ""
        number, rarity, alt_name = _parse_alt(alt)
        if number is None:
            continue

        # Prefer the clean <h4> for the JA name; fall back to the alt's name.
        h4 = div.find("h4", class_="text-primary")
        name_ja = h4.get_text(strip=True) if h4 else alt_name

        is_parallel = (
            bool(rarity) and (rarity.startswith("P-") or rarity in ("SP", "TR"))
        ) or ("パラレル" in (name_ja or "")) or ("パラレル" in alt)

        text = div.get_text(separator=" ", strip=True)

        # Stock indicator: 在庫 : ◯ / N 点 = in stock; × = sold out.
        in_stock = True
        stock_m = re.search(r"在庫\s*[:：]?\s*(◯|×|\d+\s*点)", text)
        if stock_m:
            in_stock = stock_m.group(1) != "×"
        elif "sold-out" in (div.get("class") or []):
            in_stock = False

        # Price ("7,980 円"). Shown even on sold-out rows (stale ask), so the
        # consumer decides whether to trust it based on in_stock.
        price_jpy: int | None = None
        price_m = re.search(r"(\d[\d,]*)\s*円", text)
        if price_m:
            try:
                price_jpy = int(price_m.group(1).replace(",", ""))
            except ValueError:
                pass

        pid_m = re.search(r"/(\d+)\.jpg", src)
        yuyutei_pid = pid_m.group(1) if pid_m else None

        rows.append({
            "card_number": number,
            "rarity": rarity,
            "is_parallel": is_parallel,
            "name_ja": name_ja,
            "image_url": src,
            "price_jpy": price_jpy,
            "in_stock": in_stock,
            "yuyutei_pid": yuyutei_pid,
        })
    return rows
