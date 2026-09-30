"""
OPTCG Japanese Scraper — www.onepiece-cardgame.com (the bare/`www` host is the
Japanese original; `en.` is the English sister site).

Mirrors scraper.py exactly — same DOM (dl.modalCol), same CSS pagination, one
page load per set. Only the differences from the EN scrape live here:
  * BASE_URL / image host point at the JA site.
  * Game-NEUTRAL attributes (color, category, attribute) come off the JA DOM in
    Japanese, so they are normalized back to the SAME canonical English tokens
    the EN scraper produces (赤→Red, リーダー→Leader, 斬→Slash). This keeps the
    `cards` columns language-neutral so the frontend filters + placeholder tints
    keep working unchanged. Rarity is the SAME letter codes as EN (TR is EN-only,
    so it simply never appears), so RARITY_MAP is reused as-is.
  * name / effect / trigger / image_url ARE Japanese — those are the only
    per-language fields, and the importer writes them to card_translations.

Output: data/cards_ja.json + data/_checkpoint_jp.json (same shape as cards.json).

╔══════════════════════════════════════════════════════════════════════════╗
║ STEP ZERO (REQUIRED before trusting any run) — per the multilang spec:    ║
║   Run a selector probe against ONE live JA set first and confirm:         ║
║     1. dl.modalCol / .cardName / .infoCol span / .color / .cost / .power  ║
║        / .counter / .attribute i / .feature / .text / .trigger all resolve║
║        (recon says the JA DOM matches EN; this is belt-and-braces).       ║
║     2. The EXACT Japanese strings the DOM emits for color / category /    ║
║        attribute match the *_MAP_JA keys below. The scraper HARD-FAILS on ║
║        any unmapped token (it will NOT silently write Japanese into a      ║
║        language-neutral column) — so an unverified label aborts the run    ║
║        loudly. Add the missing mapping, then re-run.                       ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright, Page

# Reuse the EN lookups we share. Verified against the live JA DOM (2026-06-16):
#   * RARITY_MAP — JA uses the SAME letter codes (L/UC/SR/…); TR is EN-only.
#   * CATEGORY_MAP — the JA site emits ENGLISH category tokens (LEADER,
#     CHARACTER, EVENT, STAGE), NOT Japanese, so the EN map applies as-is.
from scraper import RARITY_MAP, CATEGORY_MAP

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL   = "https://www.onepiece-cardgame.com/cardlist/"
IMG_HOST   = "https://www.onepiece-cardgame.com/"
OUT_DIR    = Path("data")
CARDS_OUT  = OUT_DIR / "cards_ja.json"
SETS_OUT   = OUT_DIR / "sets_ja.json"
CP_FILE    = OUT_DIR / "_checkpoint_jp.json"
NAV_TO     = 30_000
SET_DELAY  = 1.0
HEADLESS   = True

# ── JA → canonical English maps for the language-NEUTRAL game attributes ──────
# VERIFIED against the live JA DOM (2026-06-16 step-zero probe):
#   * Colors render as Japanese kanji in `.color` (赤/緑/青/紫/黒/黄; 白 for
#     White, not present in the probed set but kept for completeness).
#   * Attributes render as `<img alt="射">` (Japanese kanji) inside `.attribute`,
#     NOT `<i>` text — the extractor below reads the img alt. Kanji confirmed:
#     射=Ranged, 特=Special (others by the same icon set).
# Category is NOT here — the JA site emits English category tokens, so the EN
# CATEGORY_MAP handles it. The scraper hard-fails on any unmapped color/attribute
# token so an unverified label can't leak into a language-neutral column.
COLOR_MAP_JA = {
    "赤": "Red", "緑": "Green", "青": "Blue", "紫": "Purple",
    "黒": "Black", "黄": "Yellow", "白": "White",
}
ATTRIBUTE_MAP_JA = {
    "斬": "Slash", "打": "Strike", "射": "Ranged", "特": "Special", "知": "Wisdom",
}

# JA-specific rarity tokens layered on top of the shared RARITY_MAP. Verified on
# the live site: the JA cardlist renders Special as "SPカード" (vs EN "SP CARD").
# TR (Treasure Rare) DOES appear in JA — contrary to earlier desk research — and
# is already in RARITY_MAP, so no override needed for it. Applied before the EN
# map in clean_card_ja; unknown tokens fall through to the raw value (same as the
# EN scraper) and are caught by the post-import `SELECT rarity, COUNT(*)` audit.
RARITY_MAP_JA_EXTRA = {
    "SPカード": "Special",
}

# ── Extraction JS (identical structure to scraper.py; image host swapped) ─────
EXTRACT_JS = """
() => {
  const cards = [];
  document.querySelectorAll('dl.modalCol').forEach(dl => {
    if (!dl.id) return;
    const id       = dl.id;
    const parallel = /_[pr]\\d+$/.test(id);
    const base_id  = parallel ? id.replace(/_[pr]\\d+$/, '') : null;
    const variant_type = !parallel ? null : /_r\\d+$/.test(id) ? 'Reprint' : 'Alternate Art';

    const spans    = [...dl.querySelectorAll('dt .infoCol span')];
    const rarityRaw   = spans[1]?.textContent.trim()  || null;
    const categoryRaw = spans[2]?.textContent.trim()  || null;
    const name        = dl.querySelector('.cardName')?.textContent.trim() || null;

    const imgLink = dl.previousElementSibling;
    const img     = imgLink?.querySelector('img');
    const rawSrc  = img?.getAttribute('data-src') || img?.getAttribute('src') || '';
    const image_url = rawSrc
      ? 'https://www.onepiece-cardgame.com/' + rawSrc.replace(/^\\.\\.\\//, '').split('?')[0]
      : `https://www.onepiece-cardgame.com/images/cardlist/card/${id}.png`;

    const dd = dl.querySelector('dd')?.cloneNode(true);
    if (!dd) return;
    const raw = sel => {
      const el = dd.querySelector(sel);
      if (!el) return null;
      el.querySelector('h3')?.remove();
      return el.textContent.replace(/\\s+/g, ' ').trim() || null;
    };

    const effectEl = dd.querySelector('.text');
    let effect = null;
    if (effectEl) {
      effectEl.querySelector('h3')?.remove();
      effect = effectEl.innerHTML
        .replace(/<br[^>]*>/gi, '\\n').replace(/<[^>]+>/g, '')
        .replace(/[ \\t]+/g, ' ').replace(/\\n /g, '\\n').trim() || null;
    }
    const triggerEl = dd.querySelector('.trigger');
    let trigger = null;
    if (triggerEl) {
      triggerEl.querySelector('h3')?.remove();
      trigger = triggerEl.textContent.replace(/\\s+/g, ' ').trim() || null;
    }

    cards.push({
      id, base_id, parallel, variant_type, name,
      rarity_raw: rarityRaw, category_raw: categoryRaw, image_url,
      colors_raw: raw('.color'), cost_raw: raw('.cost'), power_raw: raw('.power'),
      counter_raw: raw('.counter'),
      // JA attribute is an <img alt="射"> icon (Japanese kanji), not <i> text.
      attributes_raw: dd.querySelector('.attribute img')?.getAttribute('alt')?.trim() || null,
      types_raw: raw('.feature'), effect, trigger,
    });
  });
  return cards;
}
"""


def _map_or_fail(value, mapping, field, card_id):
    """Map a single JA token to its canonical English value, or raise. Hard-fail
    keeps un-probed Japanese strings out of the language-neutral `cards`
    columns (the feedback_validate_regex_against_real_data lesson)."""
    if value is None or value == "-":
        return None
    if value not in mapping:
        raise ValueError(
            f"[{card_id}] unmapped {field} token {value!r} — add it to the "
            f"{field} map in scraper_jp.py after the step-zero probe (do NOT "
            f"let Japanese leak into a language-neutral column)."
        )
    return mapping[value]


def _split_mapped(value, mapping, field, card_id):
    """Multi-value '赤/黄' → ['Red','Yellow'] via _map_or_fail per token."""
    if not value or value == "-":
        return None
    out = [_map_or_fail(tok.strip(), mapping, field, card_id) for tok in value.split("/") if tok.strip()]
    return out or None


def to_int(val):
    if not val or val == "-":
        return None
    m = re.search(r"\d+", val)
    return int(m.group()) if m else None


def clean_card_ja(raw: dict, set_id: str, pack_id: str) -> dict:
    """Same typed shape as scraper.clean_card, but colors/category/attributes are
    normalized JA→canonical-English, and name/effect/trigger stay Japanese.
    `types` (.feature) is free-form Japanese trait text with no finite map — it
    is captured raw; the importer keeps `cards.types` untouched for SHARED cards
    (only writes the JA translation row), and decides per JA-EXCLUSIVE."""
    cid = raw["id"]
    # Rarity + category are English tokens on the JA site → shared EN maps, with
    # safe fallback to the raw value (same as scraper.clean_card). Colors +
    # attributes are Japanese → JA maps with hard-fail on anything unmapped.
    rraw     = raw["rarity_raw"] or ""
    rarity   = RARITY_MAP_JA_EXTRA.get(rraw) or RARITY_MAP.get(rraw, rraw)
    category = CATEGORY_MAP.get(raw["category_raw"] or "", raw["category_raw"])
    colors   = _split_mapped(raw["colors_raw"], COLOR_MAP_JA, "color", cid)
    attrs    = _split_mapped(raw["attributes_raw"], ATTRIBUTE_MAP_JA, "attribute", cid)

    return {
        "id": cid,
        "base_id": raw["base_id"],
        "parallel": raw["parallel"],
        "variant_type": raw.get("variant_type"),
        "name": raw["name"],            # Japanese — goes to card_translations
        "set_id": set_id,
        "pack_id": pack_id,
        "rarity": rarity,
        "category": category,
        "image_url": raw["image_url"],  # Japanese art — card_translations
        "colors": colors,
        "cost": to_int(raw["cost_raw"]),
        "power": to_int(raw["power_raw"]),
        "counter": to_int(raw["counter_raw"]),
        "attributes": attrs,
        "types_ja_raw": raw["types_raw"],  # free-form Japanese; importer decides
        "effect": raw["effect"],           # Japanese — card_translations
        "trigger": raw["trigger"],         # Japanese — card_translations
    }


async def scrape_series(page: Page, pack_id: str, set_id: str) -> list[dict]:
    await page.goto(f"{BASE_URL}?series={pack_id}", wait_until="networkidle", timeout=NAV_TO)
    await page.wait_for_selector("dl.modalCol", state="attached", timeout=NAV_TO)
    raw_cards = await page.evaluate(EXTRACT_JS)
    return [clean_card_ja(r, set_id, pack_id) for r in raw_cards]


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master_cards: list[dict] = []
    scraped_sets: list[dict] = []
    completed_ids: set[str] = set()

    if CP_FILE.exists():
        with CP_FILE.open(encoding="utf-8") as f:
            cp = json.load(f)
        completed_ids = set(cp.get("completed", []))
        master_cards = cp.get("cards", [])
        scraped_sets = cp.get("sets", [])
        print(f"resuming JA scrape — {len(completed_ids)} sets done, {len(master_cards)} cards.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        await page.goto(BASE_URL, wait_until="networkidle", timeout=NAV_TO)
        all_series = await page.evaluate("""
        () => [...document.querySelectorAll('#series option')]
          .filter(o => o.value)
          .map(o => {
            const label = o.textContent.replace(/<br[^>]*>/gi, ' ').replace(/\\s+/g, ' ').trim();
            // JA labels use FULLWIDTH brackets 【ST-11】, not ASCII [ST-11];
            // match both so set_id is the real code (ST-11) and doesn't fall
            // back to the numeric pack_id (which produced junk numeric sets).
            const m = label.match(/[\\[【]([A-Z0-9\\-]+)[\\]】]\\s*$/);
            return { pack_id: o.value, label, set_id: m ? m[1] : o.value };
          })
        """)
        print(f"{len(all_series)} JA series discovered.")

        for s in all_series:
            set_id, pack_id = s["set_id"], s["pack_id"]
            if set_id in completed_ids:
                print(f"  skip {set_id}")
                continue
            print(f"  -- {set_id}  {s['label'][:50]}")
            try:
                cards = await scrape_series(page, pack_id, set_id)
                out = OUT_DIR / f"cards_ja_{set_id.lower().replace('-', '_')}.json"
                with out.open("w", encoding="utf-8") as f:
                    json.dump(cards, f, ensure_ascii=False, indent=2)
                master_cards.extend(cards)
                scraped_sets.append({"set_id": set_id, "pack_id": pack_id, "label": s["label"], "count": len(cards)})
                completed_ids.add(set_id)
                print(f"     ok {len(cards)} cards")
            except Exception as exc:
                # A hard-fail here is usually an unmapped JA token (see _map_or_fail)
                # — fix the map and re-run; the checkpoint resumes the rest.
                print(f"     FAIL {exc}")
            finally:
                with CP_FILE.open("w", encoding="utf-8") as f:
                    json.dump({"completed": list(completed_ids), "cards": master_cards, "sets": scraped_sets},
                              f, ensure_ascii=False)
                await asyncio.sleep(SET_DELAY)

        await browser.close()

    with CARDS_OUT.open("w", encoding="utf-8") as f:
        json.dump(master_cards, f, ensure_ascii=False, indent=2)
    with SETS_OUT.open("w", encoding="utf-8") as f:
        json.dump(scraped_sets, f, ensure_ascii=False, indent=2)
    print(f"\n{len(master_cards)} JA cards across {len(scraped_sets)} sets -> {CARDS_OUT}")


if __name__ == "__main__":
    asyncio.run(main())
