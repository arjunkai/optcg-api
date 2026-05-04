"""
Bulbagarden image backfill for the 51 EN cards stranded by the
images.pokemontcg.io CDN gap (mcd14/mcd15/mcd17/mcd18 + 3 xyp promos).

Audit 2026-05-02 verified via HEAD that pokemontcg-data lists URLs like
https://images.pokemontcg.io/mcd14/1_hires.png but the CDN returns 404
for every number in those 4 McDonald's sets, plus xyp-XY39/XY46/XY68.

Strategy — same as the existing backfill_ptcg_images_bulbagarden.py for
2023sv/2024sv: list each Bulbapedia category, then match by Pokemon name
only (the filename embeds the ORIGINAL reprint set's number, not the
McDonald's number, so number tiebreakers don't help here). The image
artwork is identical between the original and the McDonald's reprint —
McDonald's just adds a logo overlay that the un-stamped Bulbapedia
version omits. Better to render the right Pokemon than a placeholder.

For xyp (3 cards from XY Black Star Promos), the category has hundreds
of files so we fall back to per-card search by `{Name}XYPromo{N}.jpg`.

Apply with:
    python scripts/backfill_ptcg_images_mcd_xyp.py --dry-run
    python scripts/backfill_ptcg_images_mcd_xyp.py
    npx wrangler d1 execute optcg-cards --remote \\
        --file=data/backfill/mcd_xyp_images.sql

Then bust the Worker edge cache:
    curl -H "Origin: http://localhost:5173" \\
      'https://optcg-api.arjunbansal-ai.workers.dev/pokemon/cards/index?lang=en&refresh=1'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

API = "https://archives.bulbagarden.net/w/api.php"
HEADERS = {
    "User-Agent": "OPBindr-image-backfill/1.0 (https://opbindr.app; arjun@neuroplexlabs.com)",
}
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Authoritative names pulled from TCGdex on 2026-05-02 — hand-typed lists
# missed several (mcd14-2 is Chespin not Pikachu; mcd14-5 is Pikachu, etc).
TARGETS = [
    ("2014xy-1",  "Weedle"),     ("2014xy-2",  "Chespin"),
    ("2014xy-3",  "Fennekin"),   ("2014xy-4",  "Froakie"),
    ("2014xy-5",  "Pikachu"),    ("2014xy-6",  "Inkay"),
    ("2014xy-7",  "Honedge"),    ("2014xy-8",  "Snubbull"),
    ("2014xy-9",  "Swirlix"),    ("2014xy-10", "Bunnelby"),
    ("2014xy-11", "Fletchling"), ("2014xy-12", "Furfrou"),
    ("2015xy-1",  "Treecko"),    ("2015xy-2",  "Lotad"),
    ("2015xy-3",  "Torchic"),    ("2015xy-4",  "Staryu"),
    ("2015xy-5",  "Mudkip"),     ("2015xy-6",  "Pikachu"),
    ("2015xy-7",  "Electrike"),  ("2015xy-8",  "Rhyhorn"),
    ("2015xy-9",  "Meditite"),   ("2015xy-10", "Marill"),
    ("2015xy-11", "Zigzagoon"),  ("2015xy-12", "Skitty"),
    ("2017sm-1",  "Rowlet"),     ("2017sm-2",  "Grubbin"),
    ("2017sm-3",  "Litten"),     ("2017sm-4",  "Popplio"),
    ("2017sm-5",  "Pikachu"),    ("2017sm-6",  "Cosmog"),
    ("2017sm-7",  "Crabrawler"), ("2017sm-8",  "Alolan Meowth"),
    ("2017sm-9",  "Alolan Diglett"), ("2017sm-10", "Cutiefly"),
    ("2017sm-11", "Pikipek"),    ("2017sm-12", "Yungoos"),
    ("2018sm-1",  "Growlithe"),  ("2018sm-2",  "Psyduck"),
    ("2018sm-3",  "Horsea"),     ("2018sm-4",  "Pikachu"),
    ("2018sm-5",  "Slowpoke"),   ("2018sm-6",  "Machop"),
    ("2018sm-7",  "Cubone"),     ("2018sm-8",  "Magnemite"),
    ("2018sm-9",  "Dratini"),    ("2018sm-10", "Chansey"),
    ("2018sm-11", "Eevee"),      ("2018sm-12", "Porygon"),
    ("xyp-XY39",  "Kingdra"),    ("xyp-XY46",  "Altaria"),
    ("xyp-XY68",  "Chesnaught"),
]

CATEGORY_FOR_SET = {
    "2014xy": "McDonald's Collection 2014",
    "2015xy": "McDonald's Collection 2015",
    "2017sm": "McDonald's Collection 2017",
    "2018sm": "McDonald's Collection 2018 (EN)",
}

# Manual filename overrides for the cards that the heuristic searches miss
# (verified by direct File: namespace search on Bulbapedia 2026-05-02).
MANUAL_FILENAMES = {
    "2018sm-9":  "DratiniSunMoon94.jpg",
    "xyp-XY39":  "KingdraXYPromo39.jpg",
    "xyp-XY46":  "AltariaXYPromo46.jpg",
    "xyp-XY68":  "ChesnaughtXYPromo68.jpg",
}

# Filename tokens that demonstrably aren't card art images.
NON_CARD_TOKENS = (
    "BoosterBox", "Booster Box", "BoosterPack", "Booster Pack", "Display",
    "Pack", "TheaterPack", "SetSymbol", "Logo", "Coin", "Sleeve",
    "Pin ", "Portfolio", "Deck", "PromoSheet", "Promosheet",
    "ChallengeSet", "Bundle", "Box", "Print",
    "Contents", "Pin Set", "Tin", "Album", "anime", "QR",
)


def api_get(params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API}?{qs}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def list_category_files(category: str) -> list[str]:
    out: list[str] = []
    cmcontinue = None
    for _ in range(10):
        params = {
            "action": "query", "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmtype": "file", "cmlimit": 200,
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        d = api_get(params)
        for m in d.get("query", {}).get("categorymembers", []):
            t = m.get("title", "")
            if t.startswith("File:"):
                out.append(t[5:])
        cont = d.get("continue") or {}
        cmcontinue = cont.get("cmcontinue")
        if not cmcontinue:
            break
        time.sleep(0.2)
    return out


def search_files(query: str) -> list[str]:
    d = api_get({
        "action": "query", "format": "json",
        "list": "search", "srsearch": query,
        "srnamespace": 6, "srlimit": 30,
    })
    return [
        h["title"][5:]
        for h in d.get("query", {}).get("search", [])
        if h.get("title", "").startswith("File:")
    ]


def is_card_art(filename: str) -> bool:
    fl = filename.lower()
    if not re.search(r"\.(jpg|jpeg|png)$", fl):
        return False
    for tok in NON_CARD_TOKENS:
        if tok.lower() in fl:
            return False
    return True


def normalize(name: str) -> str:
    return re.sub(r"[\s'\-’.&]+", "", name).lower()


def best_match_in_category(card_name: str, files: list[str]) -> str | None:
    """Pick the file whose name segment starts with the Pokemon name and
    has no extra letters before the set tag. McDonald's reprints share
    artwork with the original so any file with the right name in the
    category has the right card art."""
    cn = normalize(card_name)
    # Also try last word for "Alolan Meowth" → "Meowth"
    last_word = card_name.split()[-1]
    cn_last = normalize(last_word) if last_word != card_name else None

    scored = []
    for f in files:
        if not is_card_art(f):
            continue
        m = re.match(r"^(.+?)\s?(\d+\w*)?\.(jpg|jpeg|png)$", f, re.IGNORECASE)
        if not m:
            continue
        name_part = normalize(m.group(1))
        # Prefer match against the full multi-word name first.
        starts_full = name_part.startswith(cn)
        starts_last = bool(cn_last) and name_part.startswith(cn_last)
        contains_full = cn in name_part
        if not (starts_full or starts_last or contains_full):
            continue
        # Quality: starts-with full > starts-with last word > contains.
        # Among same quality, prefer shorter filename (less specific tag).
        quality = 3 if starts_full else (2 if starts_last else 1)
        scored.append((quality, -len(f), f))

    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


def best_match_xyp(card_name: str, local_id: str) -> str | None:
    """xyp filenames follow {Name}XYPromo{N}.jpg. Search for that exact
    pattern; pick the one whose number matches the local_id digits."""
    digit_match = re.search(r"(\d+)$", local_id)
    target_num = digit_match.group(1) if digit_match else ""
    files = search_files(f"{card_name} XYPromo")
    cn = normalize(card_name)
    for f in files:
        if not is_card_art(f):
            continue
        m = re.match(rf"^(.+?)XYPromo(\d+)\.(jpg|jpeg|png)$", f, re.IGNORECASE)
        if not m:
            continue
        if normalize(m.group(1)) != cn:
            continue
        if m.group(2) == target_num:
            return f
    return None


def resolve_image_url(filename: str) -> str | None:
    d = api_get({
        "action": "query", "format": "json",
        "titles": f"File:{filename}",
        "prop": "imageinfo", "iiprop": "url",
    })
    pages = d.get("query", {}).get("pages", {})
    for page in pages.values():
        ii = page.get("imageinfo") or []
        if ii and ii[0].get("url"):
            return ii[0]["url"]
    return None


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Group cards by set so we list each McDonald's category once.
    by_set: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for cid, name in TARGETS:
        sid = cid.rsplit("-", 1)[0]
        by_set[sid].append((cid, name))

    matches: list[dict] = []

    for sid, cards in by_set.items():
        if sid == "xyp":
            continue  # handled per-card below
        cat = CATEGORY_FOR_SET.get(sid)
        if not cat:
            print(f"[skip] no category mapping for {sid}")
            continue
        print(f"[{sid}] listing category: {cat}")
        files = list_category_files(cat)
        print(f"   {len(files)} files in category")
        for cid, name in cards:
            # Manual override takes precedence — covers the few cards the
            # category-walk heuristic misses (e.g. Dratini in mcd18, where
            # the file lives in Burning Shadows category instead of mcd18).
            chosen = MANUAL_FILENAMES.get(cid) or best_match_in_category(name, files)
            if not chosen:
                print(f"   [MISS] {cid:14s} ({name})")
                continue
            url = resolve_image_url(chosen)
            if not url:
                print(f"   [URL?] {cid:14s} ({name}) chosen={chosen}")
                continue
            print(f"   [OK]   {cid:14s} → {chosen}")
            matches.append({"card_id": cid, "filename": chosen, "url": url})
            time.sleep(0.3)
        time.sleep(0.4)

    # xyp — per-card targeted search; manual fallback covers all 3.
    print("[xyp] per-card search")
    for cid, name in by_set.get("xyp", []):
        local_id = cid.rsplit("-", 1)[-1]
        chosen = MANUAL_FILENAMES.get(cid) or best_match_xyp(name, local_id)
        if not chosen:
            print(f"   [MISS] {cid:14s} ({name})")
            continue
        url = resolve_image_url(chosen)
        if not url:
            print(f"   [URL?] {cid:14s} ({name}) chosen={chosen}")
            continue
        print(f"   [OK]   {cid:14s} → {chosen}")
        matches.append({"card_id": cid, "filename": chosen, "url": url})
        time.sleep(0.3)

    if not matches:
        print("\nNo matches resolved.")
        return

    sql_lines = [
        "-- Bulbagarden image backfill for the 51 mcd14/mcd15/mcd17/mcd18",
        "-- + xyp cards that pokemontcg.io's CDN doesn't host. Fills only",
        "-- rows where image_high is NULL (the 51 we nullified earlier).",
    ]
    for m in matches:
        url = m["url"].replace("'", "''")
        sql_lines.append(
            f"UPDATE ptcg_cards SET image_high='{url}', image_low='{url}' "
            f"WHERE lang='en' AND card_id='{m['card_id']}' AND image_high IS NULL;"
        )

    out_sql = OUT_DIR / "mcd_xyp_images.sql"
    out_sql.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    out_json = OUT_DIR / "mcd_xyp_images.json"
    out_json.write_text(json.dumps(matches, indent=2), encoding="utf-8")
    print()
    print(f"Resolved {len(matches)} / {len(TARGETS)}")
    print(f"  SQL:    {out_sql}")
    print(f"  JSON:   {out_json}")
    if args.dry_run:
        print("(dry run — D1 not touched)")
    else:
        print(
            f"\nApply via:\n  npx wrangler d1 execute optcg-cards --remote --file={out_sql}"
        )


if __name__ == "__main__":
    main()
