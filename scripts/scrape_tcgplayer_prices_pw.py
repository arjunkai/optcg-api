"""
Direct-Playwright replacement for scrape_tcgplayer_prices.py (no Firecrawl).

Outputs markdown compatible with parse_tcgplayer_prices.py so the downstream
pipeline (build_all_prices.py, import-prices-d1.js) is unchanged.

Usage:
  python scripts/scrape_tcgplayer_prices_pw.py           # missing sets only
  python scripts/scrape_tcgplayer_prices_pw.py --force   # re-scrape all
  python scripts/scrape_tcgplayer_prices_pw.py OP-09     # single set

Risk notes: we hit TCGPlayer at ~50 req/month with 2s delays. Well below any
rate limit. If we ever get served a Cloudflare challenge, the checkpoint logs
it as a failure and we can fall back to scrape_tcgplayer_prices.py (Firecrawl).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# Reuse the slug mapping from the Firecrawl scraper
sys.path.insert(0, str(Path(__file__).parent))
from scrape_tcgplayer_prices import SLUG_OVERRIDES, BASE_URL, DATA_DIR, RAW_DIR  # noqa: E402

CHECKPOINT = RAW_DIR / "_checkpoint_pw.json"
NAV_TIMEOUT_MS = 60_000
SELECTOR_TIMEOUT_MS = 20_000
SET_DELAY_S = 2.0

# JS that runs inside the browser — extracts every DON/price row from the DOM
# and emits it in a markdown-compatible format so parse_tcgplayer_prices.py
# can parse it unchanged. Columns match the Firecrawl table layout exactly.
EXTRACT_JS = """
() => {
  const lines = [];
  let rowIdx = 0;
  // Convert "Label:\\nValue" -> "Label:<br>Value" so parse_tcgplayer_prices.clean_cell
  // can find the label and strip it. The parser only understands <br>.
  const labelTo = (s) => s.replace(/:\\s*\\n/g, ':<br>').replace(/\\n/g, '<br>');

  document.querySelectorAll('tr').forEach(tr => {
    const cells = [...tr.querySelectorAll('td')];
    if (cells.length < 8) return;
    const link = tr.querySelector('a[href*="/product/"]');
    if (!link) return;
    const priceCell = cells.find(c => /^\\$[\\d,.]+$/.test((c.innerText || '').trim()));
    if (!priceCell) return;

    const tcgMatch = link.href.match(/\\/product\\/(\\d+)/);
    if (!tcgMatch) return;
    const tcgId = tcgMatch[1];
    const urlPath = link.href.replace(/\\?.*$/, '');

    // Column layout (observed):
    //   td[0] select | td[1] thumbnail | td[2] name | td[3] printing
    //   td[4] condition | td[5] rarity | td[6] number | td[7] price | td[8] qty
    const name = (cells[2].innerText || '').trim();
    if (!name) return;
    const printing = labelTo((cells[3].innerText || '').trim());
    const condition = labelTo((cells[4].innerText || '').trim());
    const rarity = labelTo((cells[5].innerText || '').trim());
    const number = (cells[6].innerText || '').trim();
    const priceText = (priceCell.innerText || '').trim();

    rowIdx += 1;
    const nameLink = `[${name}](${urlPath})`;
    const thumb = `[![${name} Thumbnail](https://tcgplayer-cdn.tcgplayer.com/product/${tcgId}_in_200x200.jpg)](${urlPath})`;

    lines.push(
      `| Select table row ${rowIdx} | ${thumb} | ${nameLink} | ${printing} | ${condition} | ${rarity} | ${number} | ${priceText} | Qty |`
    );
  });
  return lines.join('\\n');
}
"""


def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    return {"completed": [], "failed": [], "skipped": []}


def save_checkpoint(cp: dict) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(cp, indent=2), encoding="utf-8")


def is_valid_scrape(md_path: Path) -> bool:
    if not md_path.exists() or md_path.stat().st_size < 2_000:
        return False
    text = md_path.read_text(encoding="utf-8", errors="ignore")
    return "Select table row" in text


async def scrape_one(page: Page, set_id: str, slug: str) -> tuple[bool, str]:
    url = f"{BASE_URL}/{slug}"
    out_path = RAW_DIR / f"{set_id}.md"

    try:
        await page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        # state="attached" — table cells may be off-screen below the fold
        await page.wait_for_selector('a[href*="/product/"]', state="attached", timeout=SELECTOR_TIMEOUT_MS)
    except PWTimeout as e:
        return False, f"timeout waiting for page/selector: {e}"

    html = await page.content()
    if "Just a moment" in html or "Checking if the site connection is secure" in html:
        return False, "served Cloudflare challenge page"

    md = await page.evaluate(EXTRACT_JS)
    if not md or "Select table row" not in md:
        return False, "no price rows in DOM"

    out_path.write_text(md, encoding="utf-8")
    return True, f"ok ({out_path.stat().st_size:,} bytes)"


async def run(set_ids_filter: list[str] | None, force: bool) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cp = load_checkpoint()
    sets = json.loads((DATA_DIR / "sets.json").read_text(encoding="utf-8"))
    if set_ids_filter:
        sets = [s for s in sets if s["set_id"] in set_ids_filter]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        for s in sets:
            set_id = s["set_id"]
            slug = SLUG_OVERRIDES.get(set_id)
            if not slug:
                print(f"  [skip] {set_id:12} no slug mapped")
                if set_id not in cp["skipped"]:
                    cp["skipped"].append(set_id)
                continue

            out_path = RAW_DIR / f"{set_id}.md"
            if not force and set_id in cp["completed"] and is_valid_scrape(out_path):
                print(f"  [done] {set_id:12} {slug}")
                continue

            print(f"  -> {set_id:12} {slug}")
            ok, msg = await scrape_one(page, set_id, slug)
            print(f"         {msg}")

            if ok:
                if set_id not in cp["completed"]:
                    cp["completed"].append(set_id)
                if set_id in cp["failed"]:
                    cp["failed"].remove(set_id)
            else:
                if set_id not in cp["failed"]:
                    cp["failed"].append(set_id)

            save_checkpoint(cp)
            await asyncio.sleep(SET_DELAY_S)

        await browser.close()

    print()
    print(f"Completed: {len(cp['completed'])}  Failed: {len(cp['failed'])}  Skipped: {len(cp['skipped'])}")
    if cp["failed"]:
        print("Failed sets:", ", ".join(cp["failed"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("set_id", nargs="?")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    asyncio.run(run([args.set_id] if args.set_id else None, args.force))


if __name__ == "__main__":
    main()
