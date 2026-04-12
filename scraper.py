"""
OPTCG Scraper — en.onepiece-cardgame.com
Validated against live DOM + punk-records schema (buhbbl/punk-records on GitHub)

Confirmed facts:
  - All card data is in the DOM on first page load (dl.modalCol elements)
  - Pagination is pure CSS show/hide — zero AJAX calls per page click
  - One page load per set = 51 total loads for the full DB
  - Schema validated against punk-records (most-starred community reference)

Output: data/cards.json + data/sets.json + data/cards_<set>.json per set
"""

import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL  = "https://en.onepiece-cardgame.com/cardlist/"
OUT_DIR   = Path("data")
NAV_TO    = 30_000   # navigation timeout ms
SET_DELAY = 1.0      # seconds between sets
HEADLESS  = True     # False = watch the browser

# ── Lookup tables (validated against live site) ───────────────────────────────

RARITY_MAP = {
    "L":   "Leader",
    "C":   "Common",
    "UC":  "Uncommon",
    "R":   "Rare",
    "SR":  "SuperRare",
    "SEC": "SecretRare",
    "SP":  "Special",
    "TR":  "TreasureRare",
    "PR":  "Promo",
}

CATEGORY_MAP = {
    "LEADER":    "Leader",
    "CHARACTER": "Character",
    "EVENT":     "Event",
    "STAGE":     "Stage",
    "DON!!":     "Don",
}

# ── JS that runs inside the browser — extracts every card on the current page ─

EXTRACT_JS = """
() => {
  const cards = [];

  // There is ONE .resultCol container holding all cards.
  // Iterate dl.modalCol directly; the image <a> is always previousElementSibling.
  document.querySelectorAll('dl.modalCol').forEach(dl => {
    if (!dl.id) return;

    // ── Identity ─────────────────────────────────────────────────────────────
    const id       = dl.id;                         // "OP01-001" or "OP01-001_p1"
    const parallel = /_[pr]\\d+$/.test(id);
    const base_id  = parallel ? id.replace(/_[pr]\\d+$/, '') : null;
    const variant_type = !parallel ? null : /_r\\d+$/.test(id) ? 'reprint' : 'alt_art';

    const spans    = [...dl.querySelectorAll('dt .infoCol span')];
    const rarityRaw   = spans[1]?.textContent.trim()  || null;  // "L", "C", "SR"…
    const categoryRaw = spans[2]?.textContent.trim()  || null;  // "LEADER", "CHARACTER"…
    const name        = dl.querySelector('.cardName')?.textContent.trim() || null;

    // ── Image URL (absolute, no cache-busting query string) ──────────────────
    const imgLink = dl.previousElementSibling;       // <a class="modalOpen">
    const img     = imgLink?.querySelector('img');
    const rawSrc  = img?.getAttribute('data-src') || img?.getAttribute('src') || '';
    const image_url = rawSrc
      ? 'https://en.onepiece-cardgame.com/'
          + rawSrc.replace(/^\\.\\.\\//, '').split('?')[0]
      : `https://en.onepiece-cardgame.com/images/cardlist/card/${id}.png`;

    // ── Helpers ───────────────────────────────────────────────────────────────
    const dd = dl.querySelector('dd')?.cloneNode(true);
    if (!dd) return;

    // Get raw text of a field, stripping its <h3> label
    const raw = sel => {
      const el = dd.querySelector(sel);
      if (!el) return null;
      el.querySelector('h3')?.remove();
      return el.textContent.replace(/\\s+/g, ' ').trim() || null;
    };

    // Parse "Red/Yellow" → ["Red","Yellow"],  "-" or null → null
    const toArr = str => {
      if (!str || str === '-') return null;
      return str.split('/').map(s => s.trim()).filter(Boolean);
    };

    // Parse numeric fields: "5000" → 5000,  "-" or null → null
    const toInt = str => {
      if (!str || str === '-') return null;
      const n = parseInt(str.replace(/[^0-9]/g, ''), 10);
      return isNaN(n) ? null : n;
    };

    // Effect: convert <br> → newline, strip remaining tags
    const effectEl = dd.querySelector('.text');
    let effect = null;
    if (effectEl) {
      effectEl.querySelector('h3')?.remove();
      effect = effectEl.innerHTML
        .replace(/<br[^>]*>/gi, '\\n')
        .replace(/<[^>]+>/g, '')
        .replace(/[ \\t]+/g, ' ')
        .replace(/\\n /g, '\\n')
        .trim() || null;
    }

    const triggerEl = dd.querySelector('.trigger');
    let trigger = null;
    if (triggerEl) {
      triggerEl.querySelector('h3')?.remove();
      const t = triggerEl.textContent.replace(/\\s+/g, ' ').trim();
      trigger = t || null;
    }

    cards.push({
      id,
      base_id,       // null for base cards; "OP01-001" for OP01-001_p1
      parallel,
      variant_type,
      name,
      rarity_raw:    rarityRaw,    // "L" — scraper maps this to full word
      category_raw:  categoryRaw,  // "LEADER" — scraper maps this too
      image_url,
      colors_raw:    raw('.color'),          // "Red/Yellow" — split below
      cost_raw:      raw('.cost'),           // "5" or "4" (Life for Leaders)
      power_raw:     raw('.power'),          // "5000"
      counter_raw:   raw('.counter'),        // "1000" or "-"
      attributes_raw: dd.querySelector('.attribute i')?.textContent.trim() || null,
      types_raw:     raw('.feature'),        // "Supernovas/Straw Hat Crew"
      effect,
      trigger,
    });
  });

  return cards;
}
"""


def clean_card(raw: dict, set_id: str, pack_id: str) -> dict:
    """Convert raw string fields to typed, schema-correct values."""
    rarity   = RARITY_MAP.get(raw["rarity_raw"] or "", raw["rarity_raw"])
    category = CATEGORY_MAP.get(raw["category_raw"] or "", raw["category_raw"])

    def split_arr(val):
        if not val or val == "-":
            return None
        return [s.strip() for s in val.split("/") if s.strip()]

    def to_int(val):
        if not val or val == "-":
            return None
        m = re.search(r"\d+", val)
        return int(m.group()) if m else None

    return {
        "id":         raw["id"],
        "base_id":    raw["base_id"],
        "parallel":   raw["parallel"],
        "variant_type": raw.get("variant_type"),
        "name":       raw["name"],
        "set_id":     set_id,
        "pack_id":    pack_id,
        "rarity":     rarity,
        "category":   category,
        "image_url":  raw["image_url"],
        "colors":     split_arr(raw["colors_raw"]),        # ["Red", "Yellow"]
        "cost":       to_int(raw["cost_raw"]),             # 5  (int)
        "power":      to_int(raw["power_raw"]),            # 5000 (int)
        "counter":    to_int(raw["counter_raw"]),          # 1000 or null
        "attributes": split_arr(raw["attributes_raw"]),    # ["Slash"]
        "types":      split_arr(raw["types_raw"]),         # ["Supernovas","Straw Hat Crew"]
        "effect":     raw["effect"],
        "trigger":    raw["trigger"],
    }


# ── Per-series scrape ─────────────────────────────────────────────────────────

async def scrape_series(page: Page, pack_id: str, set_id: str) -> list[dict]:
    await page.goto(
        f"{BASE_URL}?series={pack_id}",
        wait_until="networkidle",
        timeout=NAV_TO,
    )
    # state="attached" — modals are display:none by design, never "visible"
    await page.wait_for_selector("dl.modalCol", state="attached", timeout=NAV_TO)

    raw_cards: list[dict] = await page.evaluate(EXTRACT_JS)
    return [clean_card(r, set_id, pack_id) for r in raw_cards]


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cp_file        = OUT_DIR / "_checkpoint.json"
    master_cards:  list[dict] = []
    scraped_sets:  list[dict] = []
    completed_ids: set[str]   = set()

    if cp_file.exists():
        with cp_file.open() as f:
            cp = json.load(f)
        completed_ids = set(cp.get("completed", []))
        master_cards  = cp.get("cards",  [])
        scraped_sets  = cp.get("sets",   [])
        print(f"♻  Resuming — {len(completed_ids)} sets done, "
              f"{len(master_cards)} cards loaded.\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        # ── Discover all series live from #series ──────────────────────────
        print("🔍  Reading series list...")
        await page.goto(BASE_URL, wait_until="networkidle", timeout=NAV_TO)

        all_series: list[dict] = await page.evaluate("""
        () => [...document.querySelectorAll('#series option')]
          .filter(o => o.value)
          .map(o => {
            const label = o.textContent.replace(/<br[^>]*>/gi, ' ').replace(/\\s+/g, ' ').trim();
            const m = label.match(/\\[([A-Z0-9\\-]+)\\]\\s*$/);
            return { pack_id: o.value, label, set_id: m ? m[1] : o.value };
          })
        """)

        print(f"  {len(all_series)} sets:\n")
        for s in all_series:
            mark = "✓" if s["set_id"] in completed_ids else " "
            print(f"  [{mark}] {s['pack_id']}  {s['set_id']:15}  {s['label'][:55]}")
        print()

        # ── One page load per set ──────────────────────────────────────────
        for s in all_series:
            set_id  = s["set_id"]
            pack_id = s["pack_id"]

            if set_id in completed_ids:
                print(f"  ⏭  {set_id}")
                continue

            print(f"  ── {set_id}  {s['label'][:50]}")
            try:
                cards = await scrape_series(page, pack_id, set_id)

                out = OUT_DIR / f"cards_{set_id.lower().replace('-','_')}.json"
                with out.open("w", encoding="utf-8") as f:
                    json.dump(cards, f, ensure_ascii=False, indent=2)

                master_cards.extend(cards)
                scraped_sets.append({
                    "set_id":  set_id,
                    "pack_id": pack_id,
                    "label":   s["label"],
                    "count":   len(cards),
                })
                completed_ids.add(set_id)

                parallel = sum(1 for c in cards if c["parallel"])
                base     = len(cards) - parallel
                print(f"     ✅  {len(cards)} cards ({base} base + {parallel} parallel alt-art)")

            except Exception as exc:
                print(f"     ❌  {exc}")

            finally:
                with cp_file.open("w", encoding="utf-8") as f:
                    json.dump({
                        "completed": list(completed_ids),
                        "cards":     master_cards,
                        "sets":      scraped_sets,
                    }, f, ensure_ascii=False)
                await asyncio.sleep(SET_DELAY)

        await browser.close()

    with (OUT_DIR / "cards.json").open("w", encoding="utf-8") as f:
        json.dump(master_cards, f, ensure_ascii=False, indent=2)
    with (OUT_DIR / "sets.json").open("w", encoding="utf-8") as f:
        json.dump(scraped_sets, f, ensure_ascii=False, indent=2)

    total    = len(master_cards)
    parallel = sum(1 for c in master_cards if c["parallel"])
    print(f"\n🎉  {total} cards total  "
          f"({total - parallel} base + {parallel} parallel)  "
          f"across {len(scraped_sets)} sets.")
    print(f"    data/cards.json")
    print(f"    data/sets.json")


if __name__ == "__main__":
    asyncio.run(main())