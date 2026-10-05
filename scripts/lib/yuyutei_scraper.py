"""Yuyutei (yuyu-tei.jp) scraper — pure data extraction.

Used by:
  - scripts/backfill_yuyutei_jp.py     — UPDATE consumer (images + prices on existing rows)
  - scripts/backfill_yuyutei_catalog.py — INSERT consumer (new rows for catalog gap)

Both consumers share this lib so the scraper logic lives in one place
and the SQL-emission concerns live in their respective consumer scripts.

Per-set listing flow:
  GET https://yuyu-tei.jp/sell/poc/s/{setcode}
    -> parse each <div class="card-product"> for:
        card number  (numerator of "130/098")
        JA name      (text of <h4 class="text-primary fw-bold">)
        image URL    (https://card.yuyu-tei.jp/poc/100_140/{setcode}/{id}.jpg)
        price JPY    ("7,980 円" -> 7980)
        sold-out     (parent div has 'sold-out' class)

The JA name comes from <h4> only — no fallback to <img alt> even though
the alt text contains the name. The alt is prefixed with card number +
rarity ("130/098 UR ロケット団のミュウツーex") and parsing it out is
brittle. If h4 is absent, name_ja is None and the catalog consumer
skips that product. The price/image consumer doesn't read name_ja.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import httpx
import re
from bs4 import BeautifulSoup


LISTING_BASE = "https://yuyu-tei.jp/sell/poc/s"
IMAGE_HOST = "card.yuyu-tei.jp"
REQ_INTERVAL_S = 1.0
USER_AGENT = "opbindr-ptcg-importer/1.0 (+https://opbindr.com; contact arjun@neuroplexlabs.com)"

MAPPING_PATH = Path("data/ptcg_jp_set_mapping.json")

FX_CACHE_PATH = Path("data/.fx_jpy_usd.json")
FX_CACHE_TTL_S = 2 * 24 * 60 * 60
JPY_TO_USD_FALLBACK = 0.0067


def load_mapping() -> dict[str, str]:
    """Return {tcgdex_set_id: yuyutei_setcode}. Strips pkmnbindr's `_ja`
    suffix. Drops the `_doc` meta key. Raises FileNotFoundError if the
    mapping file is missing (caller decides how to react)."""
    if not MAPPING_PATH.exists():
        raise FileNotFoundError(
            f"Missing {MAPPING_PATH}. "
            f"Run scripts/build-ptcg-set-mapping.js first."
        )
    raw = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    raw.pop("_doc", None)
    return {tcgdex: pkm.replace("_ja", "") for tcgdex, pkm in raw.items()}


def scrape_set_listing(client: httpx.Client, setcode: str) -> list[dict] | None:
    """Parse a Yuyutei per-set listing page. Returns a list of dicts
    {card_number, name_ja, image_url, price_jpy, sold_out}. Returns None
    if the page 404s (set doesn't exist on Yuyutei).

    name_ja extraction is from <h4 class="text-primary fw-bold"> only.
    No fallback parse on <img alt> (brittle — see module docstring).
    """
    try:
        r = client.get(f"{LISTING_BASE}/{setcode}")
    except httpx.HTTPError as exc:
        print(f"    fetch error: {exc}")
        return None
    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    cards: list[dict] = []
    for div in soup.find_all("div", class_="card-product"):
        text = div.get_text(separator=" ", strip=True)
        # Card number: "130/098" — we want the numerator.
        num_match = re.search(r"(\d{1,4})\s*/\s*\d{1,4}", text)
        if not num_match:
            continue
        card_number = num_match.group(1)
        # JA name: clean <h4 class="text-primary fw-bold">.
        name_tag = div.find("h4", class_="text-primary")
        name_ja = name_tag.get_text(strip=True) if name_tag else None
        # Image URL.
        img_tag = div.find("img", src=lambda s: s and "card.yuyu-tei.jp/poc" in s)
        image_url = img_tag["src"] if img_tag else None
        # Price: "7,980 円" — strip commas, parse int. Sold-out cards
        # have a "sold-out" class on the parent div and either no
        # price or a struck-through one; we still keep the image.
        sold_out = "sold-out" in (div.get("class") or [])
        price_jpy: int | None = None
        if not sold_out:
            price_match = re.search(r"(\d[\d,]*)\s*円", text)
            if price_match:
                try:
                    price_jpy = int(price_match.group(1).replace(",", ""))
                except ValueError:
                    pass
        cards.append({
            "card_number": card_number,
            "name_ja": name_ja,
            "image_url": image_url,
            "price_jpy": price_jpy,
            "sold_out": sold_out,
        })
    return cards


def find_set_listing(client: httpx.Client, setcode: str) -> tuple[str, list[dict] | None]:
    """scrape_set_listing, also trying Yuyutei's zero-padded code. Yuyutei
    moved to padded set codes in 2026 (sv9 → sv09, m2a → m02a, s8b → s08b);
    the old code still answers 200 with an empty listing. Returns the code
    that worked (for listing URLs) and its cards."""
    cards = scrape_set_listing(client, setcode)
    padded = re.sub(r"^([a-z]+)(\d)(?!\d)", r"\g<1>0\2", setcode)
    if not cards and padded != setcode:
        time.sleep(0.5)
        retry = scrape_set_listing(client, padded)
        if retry:
            return padded, retry
    return setcode, cards


def build_card_id_candidates(tcgdex_id: str, number: str) -> list[str]:
    """TCGdex's JA card_id is `{setid}-{localid}`. Yuyutei's number is
    unpadded numeric. Try the same multi-padding pattern as our other
    imports for TCGdex's varying conventions."""
    seen: list[str] = []
    for variant in (number, number.lstrip("0") or number, number.zfill(3)):
        cid = f"{tcgdex_id}-{variant}"
        if cid not in seen:
            seen.append(cid)
    return seen


def get_jpy_to_usd_rate() -> float:
    """Same FX cache layer as the eBay backfill. ECB rates via
    frankfurter.app, 2-day cache, hardcoded fallback."""
    if FX_CACHE_PATH.exists():
        try:
            cached = json.loads(FX_CACHE_PATH.read_text())
            if time.time() - cached.get("ts", 0) < FX_CACHE_TTL_S:
                return float(cached["rate"])
        except Exception:
            pass
    try:
        req = urllib.request.Request(
            "https://api.frankfurter.app/latest?from=JPY&to=USD",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        rate = float(data["rates"]["USD"])
        FX_CACHE_PATH.write_text(json.dumps({"rate": rate, "ts": time.time()}))
        return rate
    except Exception as exc:
        print(f"  FX fetch failed ({exc}); using fallback {JPY_TO_USD_FALLBACK}")
        return JPY_TO_USD_FALLBACK
