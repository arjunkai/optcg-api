"""
Parse a TCGPlayer price-guide markdown (from Firecrawl) into raw row JSON.

Usage:
  python scripts/parse_tcgplayer_prices.py <input.md> <set_id> <output.json>

Row structure (one per TCGPlayer product):
{
  "name":         "Adio",
  "name_suffix":  null | "Parallel" | "Manga" | "Alternate Art" | "SP" | "073",
  "tcg_id":       596948,
  "number":       "OP09-023",
  "printing":     "Normal" | "Foil",
  "rarity":       "Super Rare",
  "price":        0.78
}

The parser does NOT map to our card_id. That happens in a separate pass so
we can cross-reference with D1.
"""

import json
import re
import sys
from pathlib import Path

ROW_RE = re.compile(r"^\|\s*Select table row\s+\d+\s*\|", re.IGNORECASE)
# name link format: [Name Text](https://www.tcgplayer.com/product/596948/...)
NAME_LINK_RE = re.compile(r"\[([^\]]+)\]\(https://www\.tcgplayer\.com/product/(\d+)/[^)]+\)")
# trailing parenthetical: "(Parallel)" or "(SP)" or "(073)" or "(Alternate Art)"
SUFFIX_RE = re.compile(r"\s*\(([^)]+)\)\s*$")

# A conservative allowlist that never triggers false positives. Anything not
# on this list falls through to the heuristic below.
VARIANT_SUFFIXES = {
    "Parallel",
    "Alternate Art",
    "Manga",
    "Manga Rare",
    "SP",
    "Wanted Poster",
    "Treasure Rare",
    "Special",
    "Full Art",
    "Box Topper",
    "Reprint",
    "Pirate Foil",
}


def looks_like_variant_label(text: str) -> bool:
    """Treat any non-numeric, non-card-number parenthetical as a variant.

    Event/promo packs on TCGPlayer show up as labels like "(Judge Pack Vol. 5)",
    "(CS 25-26 Event Pack)", "(Tournament Pack 2025 Vol. 3)", "(Dash Pack)", etc.
    Listing every one is a losing battle — instead, anything that's not obviously
    a name disambiguator (numeric) or a cross-reference (card-id) is treated as
    a variant label that maps to alt_art via the default mapper rule.
    """
    s = text.strip()
    if not s:
        return False
    # Numeric disambiguator like "(073)"
    if s.isdigit():
        return False
    # Card number like "(ST18-004)"
    if NUMBER_RE.match(s):
        return False
    return True
# OP09-023, ST01-001, EB01-001, PRB-001, P-053 (promo) — allow 1-5 letter prefixes
NUMBER_RE = re.compile(r"^[A-Z]{1,5}\d{0,2}-\d{3}[A-Z]?$")
PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")
# Clean dropdown HTML like "Printing:<br>Normal<br>- Foil<br>- Normal"
# We only care about the first visible value (before <br>).
FIRST_TOKEN_RE = re.compile(r"^[^<]*")


def clean_cell(text: str) -> str:
    """Strip leading label ('Printing:', 'Condition:') and take first value."""
    # Drop 'Label:' prefix if present
    if ":" in text and "<br>" in text:
        after = text.split("<br>", 1)[1]
    else:
        after = text
    # Take text before first <br> (the currently-displayed value)
    m = FIRST_TOKEN_RE.match(after)
    return (m.group(0) if m else after).strip()


def parse_row(line: str) -> dict | None:
    """Parse one data row from the priceguide table. Returns None on skip."""
    # Split on pipes (preserving content). Markdown table rows have leading/trailing |.
    cells = [c.strip() for c in line.strip("|").split("|")]
    if len(cells) < 8:
        return None

    # Columns: [select, thumbnail, name-link, printing, condition, rarity, number, price, qty]
    name_cell = cells[2]
    printing_cell = cells[3]
    rarity_cell = cells[5]
    number_cell = cells[6]
    price_cell = cells[7]

    m = NAME_LINK_RE.search(name_cell)
    if not m:
        return None
    full_name = m.group(1).strip()
    tcg_id = int(m.group(2))

    # Strip thumbnail markers like "[Name Text Thumbnail]" that appear before the name link
    name = full_name.replace(" Thumbnail", "").strip()

    # Extract trailing parenthetical. Known variant labels go straight through;
    # anything else is accepted as a variant label if it passes the heuristic
    # (non-numeric, non-card-id). This catches promo-pack labels like
    # "(Judge Pack Vol. 5)" that we don't want to enumerate exhaustively.
    suffix_match = SUFFIX_RE.search(name)
    name_suffix: str | None = None
    if suffix_match:
        candidate = suffix_match.group(1).strip()
        if candidate in VARIANT_SUFFIXES or looks_like_variant_label(candidate):
            name_suffix = candidate
            name = SUFFIX_RE.sub("", name).strip()

    number = number_cell.strip()
    if not NUMBER_RE.match(number):
        return None

    price_match = PRICE_RE.search(price_cell)
    price = float(price_match.group(1).replace(",", "")) if price_match else None

    return {
        "name": name,
        "name_suffix": name_suffix,
        "tcg_id": tcg_id,
        "number": number,
        "printing": clean_cell(printing_cell),
        "rarity": clean_cell(rarity_cell),
        "price": price,
    }


def parse_file(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not ROW_RE.match(line):
                continue
            row = parse_row(line)
            if row:
                rows.append(row)
    return rows


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: parse_tcgplayer_prices.py <input.md> <set_id> <output.json>")
        sys.exit(2)

    in_path = Path(sys.argv[1])
    set_id = sys.argv[2]
    out_path = Path(sys.argv[3])

    rows = parse_file(in_path)

    # Summary
    suffixes: dict[str | None, int] = {}
    for r in rows:
        suffixes[r["name_suffix"]] = suffixes.get(r["name_suffix"], 0) + 1
    cross_set = sum(1 for r in rows if not r["number"].startswith(set_id.replace("-", "") + "-")
                    and not r["number"].startswith(set_id + "-"))

    print(f"  {in_path.name}")
    print(f"  Parsed {len(rows)} rows")
    print(f"  Cross-set (foreign number) rows: {cross_set}")
    print(f"  Suffix breakdown:")
    for suf, count in sorted(suffixes.items(), key=lambda x: -x[1]):
        print(f"    {suf or '(base)':20}  {count}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({"set_id": set_id, "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"  Wrote {out_path}")


if __name__ == "__main__":
    main()
