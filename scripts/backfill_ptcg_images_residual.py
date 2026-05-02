"""
Residual pass: per-card file search on Bulbagarden Archives for cards
that the bulk category-based matcher missed.

Targets:
  - cel25 Classic Collection: each card is a reprint of a specific
    historical card; local_id like '2A', '15A1', '113A' encodes the
    ORIGINAL set's card number. Search by `{name} {number}` and pick the
    file whose trailing number matches.
  - JA non-Pokemon trainers/energies: cards without dexId where the JA
    name is too noisy to match. Search by local_id within the set's
    Bulbagarden category, taking the file whose number matches.

Output: same SQL+JSON format as backfill_ptcg_images_bulbagarden.py.

Usage:
    python -m scripts.backfill_ptcg_images_residual --target=cel25 --dry-run
    python -m scripts.backfill_ptcg_images_residual --target=ja_trainers --dry-run
    python -m scripts.backfill_ptcg_images_residual --target=all
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://archives.bulbagarden.net/w/api.php"
HEADERS = {"User-Agent": "OPBindr-image-backfill/1.0 (https://opbindr.app; arjun@neuroplexlabs.com)"}
DB_NAME = "optcg-cards"
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)

NON_CARD_TOKENS = (
    "BoosterBox", "Booster Box", "BoosterPack", "Booster Pack", "Display",
    " Pack", "TheaterPack", "SetSymbol", "Logo", "Coin", "Sleeve", "Pin",
    "Portfolio", "Deck", "PromoSheet", "ChallengeSet", "Bundle", " Box",
    "Print", "Contents", "Pin Set", " Tin", "Album",
)

# JA non-Pokemon residual categories. For these, we search by
# `{local_id_number}` across the category and take the file with a
# matching number. Risk: collision with files at the same number from
# different cards. Mitigated by category scoping.
JA_TRAINER_CATEGORIES = {
    "PMCG1": "Expansion Pack",
    "PMCG2": "Pokémon Jungle",
    "PMCG3": "Mystery of the Fossils",
    "PMCG4": "Team Rocket (TCG)",
    "PMCG5": "Leaders' Stadium",
    "PMCG6": "Challenge from the Darkness",
}

# Hand-curated cel25 → (page_title, expected filename pattern). Each
# Classic Collection card is a known reprint of a specific historical
# card. We look up the Bulbapedia wiki PAGE (not file archive) and
# extract the card image from its `prop=images` listing.
CEL25_PAGE_TITLES = {
    "cel25-2A":   "Blastoise (Base Set 2)",
    "cel25-4A":   "Charizard (Base Set 4)",
    "cel25-8A":   "Dark Gyarados (Team Rocket 8)",
    "cel25-9A":   "Team Magma's Groudon (EX Team Magma vs Team Aqua 9)",
    "cel25-15A2": "Here Comes Team Rocket! (Team Rocket 15)",
    "cel25-15A3": "Rocket's Zapdos (Team Rocket 15)",
    "cel25-17A":  "Umbreon Star (POP Series 5 17)",
    "cel25-24A":  "_____'s Pikachu (Wizards Black Star Promos 24)",
    "cel25-76A":  "M Rayquaza-EX (Roaring Skies 76)",
    "cel25-86A":  "Rocket's Admin. (EX Team Rocket Returns 86)",
    "cel25-109A": "Luxray GL LV.X (Rising Rivals 109)",
    "cel25-145A": "Garchomp C LV.X (Supreme Victors 145)",
}


# Bulbapedia hosts page-side card image references on the wiki domain;
# the actual file lives on the Archives domain.
BULBAPEDIA_API = "https://bulbapedia.bulbagarden.net/w/api.php"


def bp_get(params: dict) -> dict:
    url = BULBAPEDIA_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def resolve_cel25_handcurated() -> list[dict]:
    """Hand-curated lookup for cel25 cards still missing images. Parses
    the wiki page title (which is `Name (Set Number)`) to derive the
    expected filename `NameSetNumber.jpg` and confirms it on the page's
    image list. Avoids picking the wrong reprint."""
    matches = []
    for card_id, page_title in CEL25_PAGE_TITLES.items():
        # Parse page title: "Card Name (Set Name Number)"
        m = re.match(r"^(.+) \((.+?) (\d+)\)$", page_title)
        if not m:
            print(f"  {card_id}: can't parse title '{page_title}'")
            continue
        name_raw, set_raw, num = m.group(1), m.group(2), m.group(3)
        expected = _strip_for_filename(name_raw) + _strip_for_filename(set_raw) + num

        try:
            d = bp_get({
                "action": "query", "format": "json",
                "prop": "images", "titles": page_title, "imlimit": 50,
            })
        except Exception as e:
            print(f"  {card_id}: page lookup failed ({e})")
            continue
        pages = d.get("query", {}).get("pages", {}) or {}
        page = next(iter(pages.values()), {})
        if page.get("missing") is not None:
            print(f"  {card_id}: page '{page_title}' does not exist")
            continue
        # Find a file whose name (minus extension) matches `expected`
        # case-insensitively. This pins the filename precisely instead
        # of grabbing whatever shorter name happens to be in the list.
        target = re.match(r"^(.+)\.(jpg|jpeg|png)$", expected, re.I)
        target_norm = expected.lower()
        match_fn = None
        for img in page.get("images", []):
            t = img.get("title", "")
            if not t.startswith("File:"):
                continue
            fn = t[5:]
            base = fn.rsplit(".", 1)[0].lower()
            if base == target_norm:
                match_fn = fn
                break
        if not match_fn:
            print(f"  {card_id}: no exact-match file ('{expected}.jpg') on '{page_title}'")
            continue
        url = resolve_url(match_fn)
        if url:
            print(f"  {card_id} ({page_title}) → {match_fn}")
            matches.append({
                "card_id": card_id, "lang": "en",
                "filename": match_fn, "image_url": url,
            })
        time.sleep(0.3)
    return matches


def _strip_for_filename(s: str) -> str:
    """Convert 'Team Magma vs Team Aqua' → 'TeamMagmavsTeamAqua' for
    Bulbagarden's camelCase filename convention."""
    return re.sub(r"[\s'\-’.&!]+", "", s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["cel25", "cel25_curated", "ja_trainers", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    matches: list[dict] = []
    if args.target in ("cel25", "all"):
        print("=== Pass 1: cel25 reprints via per-card file search ===")
        matches.extend(resolve_cel25())

    if args.target in ("cel25_curated", "cel25", "all"):
        print("\n=== Pass 1b: cel25 hand-curated via Bulbapedia wiki page ===")
        matches.extend(resolve_cel25_handcurated())

    if args.target in ("ja_trainers", "all"):
        print("\n=== Pass 2: JA trainers/energies via local_id-in-category ===")
        matches.extend(resolve_ja_trainers())

    matches = [m for m in matches if m.get("image_url")]
    if not matches:
        print("\nNo matches resolved.")
        return

    sql_lines = build_update_sql(matches)
    sql_file = OUT_DIR / f"bulbagarden_residual_{args.target}.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / f"bulbagarden_residual_{args.target}_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(matches)} resolved. SQL → {sql_file}")

    if args.dry_run:
        print("--dry-run: skipping D1 execution. Sample:")
        for m in matches[:10]:
            print(f"   {m['card_id']}: {m['image_url']}")
        return

    print(f"Executing {len(sql_lines)} UPDATEs against remote D1...")
    r = subprocess.run(WRANGLER + ["--remote", f"--file={sql_file}"])
    if r.returncode != 0:
        sys.exit(r.returncode)
    print("Done.")


def resolve_cel25() -> list[dict]:
    sql = ("SELECT card_id, local_id, name FROM ptcg_cards "
           "WHERE lang='en' AND set_id='cel25' AND image_high IS NULL "
           "ORDER BY local_id")
    cards = query_d1(sql)
    print(f"  {len(cards)} imageless cel25 cards")
    matches = []
    for c in cards:
        # local_id like '2A', '15A1', '113A' → strip alpha to get original number
        num_match = re.match(r"^(\d+)", c["local_id"])
        if not num_match:
            print(f"  [skip] {c['card_id']}: can't parse number from {c['local_id']}")
            continue
        original_num = num_match.group(1)
        # Search for filename containing name + this number
        result = search_one(c["name"], original_num)
        if result:
            url = resolve_url(result)
            print(f"  {c['card_id']} ({c['name']!s} #{original_num}) → {result}")
            matches.append({
                "card_id": c["card_id"],
                "lang": "en",
                "filename": result,
                "image_url": url,
            })
        else:
            print(f"  {c['card_id']} ({c['name']!s} #{original_num}): no match")
        time.sleep(0.25)
    return matches


def resolve_ja_trainers() -> list[dict]:
    """For JA cards still imageless that aren't Pokemon (no enriched
    EN name), look up by local_id within the set's category."""
    # Load the enriched names; cards with empty enriched name are non-Pokemon.
    try:
        enriched = json.loads(Path("data/ja_card_id_to_en_name.json").read_text(encoding="utf-8"))
    except Exception:
        enriched = {}

    sql = ("SELECT card_id, local_id, name, set_id FROM ptcg_cards "
           "WHERE lang='ja' AND image_high IS NULL "
           "ORDER BY set_id, local_id")
    cards = query_d1(sql)
    candidates = [c for c in cards if not enriched.get(c["card_id"]) and c["set_id"] in JA_TRAINER_CATEGORIES]
    print(f"  {len(candidates)} JA non-Pokemon residual cards in known categories")

    by_set: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_set[c["set_id"]].append(c)

    matches = []
    for sid, set_cards in by_set.items():
        cat = JA_TRAINER_CATEGORIES[sid]
        files = list_category_files(cat)
        print(f"  [{sid}] {cat}: {len(files)} files, {len(set_cards)} cards")
        # Build {number: [filename, ...]} index
        by_num: dict[str, list[str]] = defaultdict(list)
        for f in files:
            if any(tok.lower() in f.lower() for tok in NON_CARD_TOKENS):
                continue
            n = re.search(r"(\d+\w*?)\.(jpg|jpeg|png)$", f, re.I)
            if n:
                by_num[n.group(1).lstrip("0").lower() or "0"].append(f)
        for c in set_cards:
            target = c["local_id"].lstrip("0").lower() or "0"
            files_at_num = by_num.get(target, [])
            if len(files_at_num) == 1:
                fn = files_at_num[0]
                url = resolve_url(fn)
                if url:
                    print(f"    {c['card_id']} (lid={c['local_id']}) → {fn}")
                    matches.append({
                        "card_id": c["card_id"], "lang": "ja",
                        "filename": fn, "image_url": url,
                    })
            elif len(files_at_num) > 1:
                # Disambiguate by filtering filenames matching name fragments
                # (rare; usually for trainers like 'Bill', 'Energy Removal')
                # Fall back to skip rather than pick wrong.
                pass
        time.sleep(0.3)
    return matches


# Canonical "classic era" sets — Classic Collection reprints almost
# always pull from this list. When multiple files match name+number,
# prefer files from these sets in priority order.
CLASSIC_SET_PRIORITY = [
    "BaseSet", "Jungle", "Fossil", "TeamRocket", "GymHeroes", "GymChallenge",
    "NeoGenesis", "NeoDiscovery", "NeoRevelation", "NeoDestiny",
    "Expedition", "Aquapolis", "Skyridge",
    "EXRubySapphire", "EXSandstorm", "EXDragon", "EXTeamMagmavsTeamAqua",
    "EXHiddenLegends", "EXFireRedLeafGreen", "EXTeamRocketReturns",
    "EXDeoxys", "EXEmerald", "EXUnseenForces", "EXDeltaSpecies",
    "EXLegendMaker", "EXHolonPhantoms", "EXCrystalGuardians",
    "EXDragonFrontiers", "EXPowerKeepers",
    "DiamondPearl", "MysteriousTreasures", "SecretWonders", "GreatEncounters",
    "MajesticDawn", "LegendsAwakened", "Stormfront",
    "Platinum", "RisingRivals", "SupremeVictors", "Arceus",
    "HeartGoldSoulSilver", "Unleashed", "Undaunted", "Triumphant",
    "BlackWhite", "EmergingPowers", "NobleVictories", "NextDestinies",
    "DarkExplorers", "DragonsExalted", "BoundariesCrossed", "PlasmaStorm",
    "PlasmaFreeze", "PlasmaBlast", "LegendaryTreasures",
    "XY", "Flashfire", "FuriousFists", "PhantomForces",
    "PrimalClash", "RoaringSkies", "AncientOrigins", "BREAKthrough",
    "BREAKpoint", "FatesCollide", "SteamSiege", "Evolutions",
    "SunMoon", "GuardiansRising", "BurningShadows", "ShiningLegends",
    "CrimsonInvasion", "UltraPrism", "ForbiddenLight", "CelestialStorm",
    "DragonMajesty", "LostThunder", "TeamUp", "DetectivePikachu",
    "UnbrokenBonds", "UnifiedMinds", "HiddenFates", "CosmicEclipse",
]
_PRIORITY_LOOKUP = {s.lower(): i for i, s in enumerate(CLASSIC_SET_PRIORITY)}


def _classic_priority(filename: str) -> int:
    """Return priority index for ranking; lower is better. Files matching
    a known classic set get their list-position; everything else gets a
    high sentinel so they're picked last."""
    fn_lower = filename.lower()
    for tag, idx in _PRIORITY_LOOKUP.items():
        if tag in fn_lower:
            return idx
    return 999


def search_one(name: str, number: str) -> str | None:
    """Search Bulbagarden file namespace for `{name} {number}` and return
    the filename whose trailing number matches `number`. Tie-breaks by
    classic-set priority — Base Set reprints beat later reprints."""
    q = f"{name} {number}"
    d = api_get({
        "action": "query", "format": "json",
        "list": "search", "srsearch": q,
        "srnamespace": 6, "srlimit": 30,
    })
    results = d.get("query", {}).get("search", []) or []
    name_norm = re.sub(r"[\s'\-’.&]+", "", name).lower()
    candidates = []
    for r in results:
        title = r.get("title", "")
        if not title.startswith("File:"):
            continue
        fn = title[5:]
        if any(tok.lower() in fn.lower() for tok in NON_CARD_TOKENS):
            continue
        if not fn.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        # TCG card filenames in Bulbagarden are concatenated camelCase
        # like 'BlastoiseBaseSet2.jpg'. Files with spaces, underscores,
        # or hyphens are non-TCG content (Smash Bros, anime, merch).
        if " " in fn or "_" in fn or "-" in fn:
            continue
        fn_norm = re.sub(r"[\s'\-’.&]+", "", fn.rsplit(".", 1)[0]).lower()
        if name_norm not in fn_norm:
            continue
        # The number must appear right before the extension, not anywhere
        # in the middle (avoids files like 'Charizard_4-014.png' for #4).
        m = re.search(rf"{re.escape(number)}\w?\.(jpg|jpeg|png)$", fn, re.I)
        if not m:
            continue
        candidates.append(fn)
    if not candidates:
        return None
    # Sort: classic-set priority asc, .jpg before .png, then shorter
    candidates.sort(key=lambda f: (
        _classic_priority(f),
        0 if f.lower().endswith(".jpg") else 1,
        len(f),
    ))
    return candidates[0]


def list_category_files(category: str) -> list[str]:
    out = []
    cmcontinue = None
    for _ in range(20):
        params = {
            "action": "query", "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmtype": "file", "cmlimit": 500,
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        d = api_get(params)
        for m in d.get("query", {}).get("categorymembers", []):
            t = m.get("title", "")
            if t.startswith("File:"):
                out.append(t[5:])
        cont = d.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        cmcontinue = cont
        time.sleep(0.2)
    return out


def resolve_url(filename: str) -> str | None:
    d = api_get({
        "action": "query", "format": "json",
        "titles": f"File:{filename}", "prop": "imageinfo", "iiprop": "url",
    })
    for _, p in d.get("query", {}).get("pages", {}).items():
        ii = p.get("imageinfo") or [{}]
        return ii[0].get("url")
    return None


def api_get(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def query_d1(sql: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        print("D1 query failed:", (out.stderr or "")[:500])
        sys.exit(1)
    start = (out.stdout or "").find("[")
    if start < 0:
        return []
    try:
        payload = json.loads(out.stdout[start:])
    except json.JSONDecodeError:
        return []
    rows = payload[0].get("results", []) if isinstance(payload, list) else payload.get("results", [])
    return rows or []


def build_update_sql(matches: list[dict]) -> list[str]:
    lines = []
    for m in matches:
        cid = m["card_id"].replace("'", "''")
        url = m["image_url"].replace("'", "''")
        lang = m["lang"].replace("'", "''")
        lines.append(
            f"UPDATE ptcg_cards SET "
            f"image_high = COALESCE(image_high, '{url}'), "
            f"image_low  = COALESCE(image_low,  '{url}') "
            f"WHERE card_id = '{cid}' AND lang = '{lang}' AND image_high IS NULL;"
        )
    return lines


if __name__ == "__main__":
    main()
