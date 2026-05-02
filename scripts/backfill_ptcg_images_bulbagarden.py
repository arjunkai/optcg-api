"""
Bulbagarden Archives image backfill for residual PTCG cards.

After every other source (TCGdex, pokemontcg-data, malie.io, flibustier,
Yuyutei, eBay) is exhausted, ~580 cards remain imageless across EN+JA.
These are concentrated in obscure subsets that the upstream catalog
sources never covered: e-Card era (Aquapolis, Skyridge), trainer kits
(XY/DP), Unseen Forces Unown collection, McDonald's Collection 2023/24,
JA classics (PMCG1-6, Neo2/4), and various energy-only sets.

Bulbagarden Archives (the MediaWiki image archive sister to Bulbapedia)
has all of these, organized into categories named after each set. We
list each category's files, parse the filename pattern
{Name}{SetTag}{Number}.{ext}, match by (set_id, local_id), and verify
the card's name appears in the filename before writing.

Variant suffix cards (e.g. ecard2-74a / ecard2-74b sharing one Drowzee
artwork) get the same image — the suffix is finish-only, not artwork.

Stamps no special source flag — the URL host (archives.bulbagarden.net)
makes the provenance obvious for audit. COALESCE-only writes; existing
images are never overwritten.

Rollback (per lang):
    wrangler d1 execute optcg-cards --remote \\
        --command "UPDATE ptcg_cards SET image_high=NULL, image_low=NULL
                   WHERE image_high LIKE '%bulbagarden%' AND lang='en'"

Usage:
    python -m scripts.backfill_ptcg_images_bulbagarden --lang=en
    python -m scripts.backfill_ptcg_images_bulbagarden --lang=ja
    python -m scripts.backfill_ptcg_images_bulbagarden --lang=en --limit=20 --dry-run
    python -m scripts.backfill_ptcg_images_bulbagarden --set-id=ecard2 --dry-run
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


API = "https://archives.bulbagarden.net/w/api.php"
HEADERS = {
    "User-Agent": "OPBindr-image-backfill/1.0 (https://opbindr.app; arjun@neuroplexlabs.com)",
}
JP_EN_MAP_PATH = Path("data/jp_to_en_pokemon.json")
JA_CARD_ID_EN_NAME_PATH = Path("data/ja_card_id_to_en_name.json")
DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Filenames that are demonstrably NOT card images.
NON_CARD_TOKENS = (
    "BoosterBox", "Booster Box", "BoosterPack", "Booster Pack", "Display",
    "Pack", "TheaterPack", "SetSymbol", "Logo", "Coin", "Sleeve",
    "Pin", "Portfolio", "Deck", "PromoSheet", "Promosheet",
    "ChallengeSet", "Bundle", "Box", "Print",
    "Contents", "Pin Set", "Tin", "Album",
)

# Per-set mapping. category=Bulbagarden category to list. set_tag=string
# expected in filename between name and number. number_pattern=regex for
# the trailing digits/letters that match local_id. Some sets (energies,
# trainer kits) have no clean category and use file-search fallback —
# those have category=None and rely solely on set_tag scoping.
SET_MAP = {
    # JP set categories often map to EN release names in filenames.
    # set_tags is a list — first match wins. None means name-only match
    # scoped to the category (used when filenames have no consistent tag,
    # e.g. McDonald's reprints whose tag is the original EN set).
    # EN
    "mep":      {"category": "MEP Black Star Promos",     "set_tags": ["MEPPromo"]},
    "mfb":      {"category": "My First Battle",            "set_tags": ["MyFirstBattle"]},
    "svp":      {"category": "SVP Black Star Promos",      "set_tags": ["SVPPromo"]},
    "cel25":    {"category": "Celebrations",               "set_tags": ["Celebrations"]},
    "ecard2":   {"category": "Aquapolis",                  "set_tags": ["Aquapolis"]},
    "ecard3":   {"category": "Skyridge",                   "set_tags": ["Skyridge"]},
    "2023sv":   {"category": "McDonald's Collection 2023", "set_tags": None},  # name-only
    "2024sv":   {"category": "McDonald's Collection 2024", "set_tags": None},  # name-only
    "ex5.5":    {"category": "Poké Card Creator Pack",     "set_tags": ["CreatorContest"]},
    "bwp":      {"category": "BW Black Star Promos",       "set_tags": ["BWPromo"]},
    "xya":      {"category": "Yellow A Alternate cards",   "set_tags": None},  # variant — see special handler
    # Trainer kits are mostly reprints from prior sets — filename uses
    # the original set's tag (DiamondPearl, KalosStarterSet, etc.). Use
    # name-only matching scoped to the category.
    "tk-xy-sy": {"category": "XY Trainer Kit",             "set_tags": None},
    "tk-xy-n":  {"category": "XY Trainer Kit",             "set_tags": None},
    "exu":      {"category": "EX Unseen Forces",           "set_tags": ["EXUnseenForces"], "letter_suffix": True},
    "tk-dp-m":  {"category": "Diamond & Pearl Trainer Kit","set_tags": None},
    "tk-dp-l":  {"category": "Diamond & Pearl Trainer Kit","set_tags": None},
    "mee":      {"category": "MEE Basic Energies",         "set_tags": ["MEEEnergy"]},
    "sve":      {"category": "SVE Basic Energies",         "set_tags": ["SVEEnergy"]},
    # JA — filenames use EN release name, NOT the literal JP→EN translation
    "PMCG1":    {"category": "Expansion Pack",             "set_tags": ["BaseSet"]},
    "PMCG2":    {"category": "Pokémon Jungle",             "set_tags": ["Jungle"]},
    "PMCG3":    {"category": "Mystery of the Fossils",     "set_tags": ["Fossil"]},
    "PMCG4":    {"category": "Team Rocket (TCG)",          "set_tags": ["TeamRocket"]},
    # PMCG5 (Leaders' Stadium) and PMCG6 (Challenge from the Darkness)
    # were each split into TWO English releases (Gym Heroes + Gym
    # Challenge). Try both tags.
    "PMCG5":    {"category": "Leaders' Stadium",           "set_tags": ["GymHeroes", "GymChallenge"]},
    "PMCG6":    {"category": "Challenge from the Darkness","set_tags": ["GymChallenge", "GymHeroes"]},
    "neo4":     {"category": "Neo Destiny",                "set_tags": ["NeoDestiny"]},
    "neo2":     {"category": "Neo Discovery",              "set_tags": ["NeoDiscovery"]},
}

# exu: local_id 1..28 → Unown letter A..Z, !, ?
EXU_LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ["!", "?"]


def main() -> None:
    # Windows console defaults to cp1252; printing arrows / Japanese
    # set names crashes without UTF-8 reconfiguration.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["en", "ja"])
    ap.add_argument("--set-id", help="Only run for this set (overrides --lang scope)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap UPDATEs (sample mode)")
    args = ap.parse_args()
    if not args.lang and not args.set_id:
        ap.error("specify --lang or --set-id")

    print("1. Querying D1 for imageless cards...")
    where = "image_high IS NULL"
    if args.set_id:
        where += f" AND set_id = '{args.set_id}'"
    if args.lang:
        where += f" AND lang = '{args.lang}'"
    cards = query_d1(
        f"SELECT card_id, name, set_id, local_id, lang FROM ptcg_cards "
        f"WHERE {where} ORDER BY lang, set_id, local_id"
    )
    print(f"   {len(cards)} imageless cards in scope")
    by_set = defaultdict(list)
    for c in cards:
        by_set[(c["lang"], c["set_id"])].append(c)
    print(f"   spanning {len(by_set)} (lang, set) combos\n")

    print("2. Resolving images per set...")
    matches: list[dict] = []
    skipped_sets: list[str] = []
    for (lang, sid), set_cards in sorted(by_set.items()):
        spec = SET_MAP.get(sid)
        if not spec:
            skipped_sets.append(f"{lang}/{sid} ({len(set_cards)} cards)")
            continue
        print(f"   [{lang}/{sid}] {len(set_cards)} cards — fetching from Bulbagarden...")
        try:
            files = fetch_files_for_set(spec)
        except Exception as exc:
            print(f"      FAILED to list files: {exc}")
            continue
        print(f"      {len(files)} candidate files in scope")
        ms = match_cards(set_cards, files, spec)
        print(f"      → {len(ms)} matches\n")
        matches.extend(ms)
        time.sleep(0.4)  # polite to Bulbagarden API

    if skipped_sets:
        print(f"   Skipped (no mapping): {skipped_sets}\n")

    if args.limit:
        matches = matches[:args.limit]

    if not matches:
        print("No matches. Nothing to write.")
        return

    print(f"3. Resolving direct image URLs for {len(matches)} matches...")
    matches = resolve_image_urls(matches)
    matches = [m for m in matches if m.get("image_url")]
    print(f"   {len(matches)} matches with resolvable URLs\n")

    sql_lines = build_update_sql(matches)
    sql_file = OUT_DIR / f"bulbagarden_images_{args.lang or args.set_id}.sql"
    sql_file.write_text("\n".join(sql_lines), encoding="utf-8")
    matches_file = OUT_DIR / f"bulbagarden_images_{args.lang or args.set_id}_matches.json"
    matches_file.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"4. SQL written to {sql_file}")
    print(f"   Matches written to {matches_file}")

    if args.dry_run:
        print("\n--dry-run: skipping D1 execution. Sample matches:")
        for m in matches[:10]:
            print(f"   {m['card_id']}: {m['image_url']}")
        return

    print(f"\n5. Executing {len(sql_lines)} UPDATEs against remote D1...")
    result = subprocess.run(WRANGLER_BIN + ["--remote", f"--file={sql_file}"])
    if result.returncode != 0:
        print("Execute failed.")
        sys.exit(result.returncode)
    print("Done.")


def query_d1(sql: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
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


def api_get(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_files_for_set(spec: dict) -> list[str]:
    """Return a list of file titles in scope for this set.
    If spec has a category, paginate through its file members.
    Otherwise (or to supplement), search files by each set_tag."""
    titles: set[str] = set()
    if spec.get("category"):
        titles.update(_paginate_category(spec["category"]))
    set_tags = spec.get("set_tags") or []
    if set_tags and (not spec.get("category") or len(titles) < 5):
        # Fallback / supplement: file-namespace search per tag
        for tag in set_tags:
            titles.update(_search_files(tag))
    return sorted(titles)


def _paginate_category(category: str) -> list[str]:
    out: list[str] = []
    cmcontinue = None
    for _ in range(20):  # cap pagination
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


def _search_files(query: str) -> list[str]:
    """Search namespace 6 (file) for filenames matching the query."""
    out: list[str] = []
    sroffset = 0
    for _ in range(10):
        d = api_get({
            "action": "query", "format": "json",
            "list": "search",
            "srsearch": query,
            "srnamespace": 6,
            "srlimit": 50,
            "sroffset": sroffset,
        })
        results = d.get("query", {}).get("search", [])
        for r in results:
            t = r.get("title", "")
            if t.startswith("File:"):
                out.append(t[5:])
        if len(results) < 50:
            break
        sroffset += 50
        time.sleep(0.2)
    return out


def match_cards(set_cards: list[dict], files: list[str], spec: dict) -> list[dict]:
    """Match imageless cards to filenames using name-first strategy.

    For each file, parse `{name_part}{set_tag}{number?}.ext` (number is
    optional — some sets like My First Battle have no number suffix).
    Then for each imageless card, find the best name match. Number is a
    tiebreaker when multiple files have the same name."""
    set_tags = spec.get("set_tags") or []
    use_name_only = not set_tags  # category-bounded matching (McDonald's, etc.)

    # Pre-parse all candidate files
    parsed: list[dict] = []
    if set_tags:
        # Per-tag regex. exu's files use letter/symbol suffix instead of
        # digit (UnownEXUnseenForcesA.jpg, ...!.jpg, ...?.jpg).
        if spec.get("letter_suffix"):
            patterns = [
                (tag, re.compile(
                    rf"^(.+?){re.escape(tag)}([A-Z!?])\.(jpg|jpeg|png)$",
                    re.IGNORECASE,
                )) for tag in set_tags
            ]
        else:
            patterns = [
                (tag, re.compile(
                    rf"^(.+?){re.escape(tag)} ?(\d+\w*?)?\.(jpg|jpeg|png)$",
                    re.IGNORECASE,
                )) for tag in set_tags
            ]
    else:
        # Name-only: anything ending in (number?).ext
        patterns = [(None, re.compile(
            r"^(.+?)\s?(\d+\w*?)?\.(jpg|jpeg|png)$",
            re.IGNORECASE,
        ))]

    for fn in files:
        skip = False
        for tok in NON_CARD_TOKENS:
            if tok.lower() in fn.lower():
                # Don't filter if any tag legitimately contains the token
                if any(tok.lower() in (t or "").lower() for t in set_tags):
                    continue
                skip = True
                break
        if skip:
            continue
        for tag, pat in patterns:
            m = pat.match(fn)
            if m:
                parsed.append({
                    "filename": fn,
                    "name_part": m.group(1),
                    "name_norm": _normalize_name(m.group(1)),
                    "number": (m.group(2) or "").lower(),
                    "matched_tag": tag,
                })
                break

    # For exu (letter-suffix), pre-compute the expected letter per card
    def expected_number(card: dict) -> str:
        lid = urllib.parse.unquote(card["local_id"])  # %3F → ?
        if spec.get("letter_suffix"):
            # Our local_id is already the letter (A-Z, !, ?). No translation
            # needed — just lowercase for comparison.
            return lid.lower()
        # Strip trailing variant letter (e.g. "74a" → "74", "92a" → "92")
        # but keep the whole id for xya (cards have e.g. "28a" as local_id and
        # that 'a' is preserved in the filename).
        if card["set_id"] == "xya":
            return lid.lower()
        m = re.match(r"^([A-Za-z]*)(\d+)([A-Za-z]*)$", lid)
        if m:
            # Drop trailing alpha, keep prefix+digits (e.g. "H01" → "h01", "74a" → "74")
            return (m.group(1) + m.group(2)).lower()
        return lid.lower()

    jp_en = _load_jp_en_map()
    card_id_to_en = _load_card_id_to_en()
    matches = []
    for c in set_cards:
        # JA cards: prefer pre-fetched canonical EN name (from TCGdex
        # dexId lookup) over the raw `name` field, which is often noisy
        # JP/phonetic/garbage data from older imports.
        enriched = card_id_to_en.get(c["card_id"])
        if enriched:
            en_name = enriched
        else:
            en_name = _to_en_name(c["name"], jp_en)
        cn = _normalize_name(en_name)
        if not cn:
            continue
        target_num = expected_number(c)
        # Score candidates: (name_match_quality, number_match)
        scored = []
        for p in parsed:
            # Name match: card name appears in filename's name_part
            if cn not in p["name_norm"]:
                continue
            # Quality: prefer exact prefix match over substring
            quality = 2 if p["name_norm"].startswith(cn) else 1
            # Number tiebreaker: prefer number match
            num_match = (p["number"] == target_num) if target_num else False
            scored.append((quality, num_match, -len(p["filename"]), p["filename"]))
        if not scored:
            continue
        scored.sort(reverse=True)
        # If any match has num_match=True, REQUIRE that. Avoids picking the
        # wrong card when multiple cards share a name prefix (e.g.
        # "Pikachu" appears in dozens of sets).
        best = scored[0]
        if any(s[1] for s in scored):
            best = next(s for s in scored if s[1])
        matches.append({
            "card_id": c["card_id"],
            "lang": c["lang"],
            "filename": best[3],
        })
    return matches


def _normalize_name(name: str) -> str:
    """Strip whitespace, apostrophes, hyphens, periods, ampersands; lowercase."""
    return re.sub(r"[\s'\-’.&]+", "", name).lower()


_JP_EN_CACHE: dict | None = None
_CARD_ID_EN_CACHE: dict | None = None

def _load_jp_en_map() -> dict:
    global _JP_EN_CACHE
    if _JP_EN_CACHE is not None:
        return _JP_EN_CACHE
    try:
        _JP_EN_CACHE = json.loads(JP_EN_MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        _JP_EN_CACHE = {}
    return _JP_EN_CACHE


def _load_card_id_to_en() -> dict:
    global _CARD_ID_EN_CACHE
    if _CARD_ID_EN_CACHE is not None:
        return _CARD_ID_EN_CACHE
    try:
        _CARD_ID_EN_CACHE = json.loads(JA_CARD_ID_EN_NAME_PATH.read_text(encoding="utf-8"))
    except Exception:
        _CARD_ID_EN_CACHE = {}
    return _CARD_ID_EN_CACHE


def _to_en_name(name: str, jp_en: dict) -> str:
    """Translate a possibly-JA card name to EN for filename matching.
    The DB's JA names mix canonical katakana, phonetic katakana, and EN.
    Try several normalizations against the canonical map; if none hit,
    return the original name (it may already be EN)."""
    if not name:
        return ""
    candidates = [
        name,
        name.replace("f", "♀").replace("m", "♂"),  # 'ニドランf' → 'ニドラン♀'
        name.replace("F", "♀").replace("M", "♂"),
    ]
    for cand in candidates:
        if cand in jp_en:
            return jp_en[cand]
    return name


def _name_matches(filename_part: str, normalized_card_name: str) -> bool:
    """Returns true if normalized_card_name looks present in filename_part."""
    fn_norm = _normalize_name(filename_part)
    if not normalized_card_name:
        return False
    # Loose: card name (or first 6 chars) should appear in filename portion
    if normalized_card_name in fn_norm:
        return True
    short = normalized_card_name[:6]
    return len(short) >= 4 and short in fn_norm


def resolve_image_urls(matches: list[dict]) -> list[dict]:
    """Resolve File:Foo.jpg → direct image URL via prop=imageinfo. Batches 50."""
    by_filename: dict[str, str] = {}
    filenames = sorted({m["filename"] for m in matches})
    for i in range(0, len(filenames), 50):
        batch = filenames[i:i + 50]
        titles = "|".join(f"File:{fn}" for fn in batch)
        d = api_get({
            "action": "query", "format": "json",
            "titles": titles,
            "prop": "imageinfo", "iiprop": "url",
        })
        # Reverse the title-normalization Wikipedia does (replaces spaces with _, etc.)
        norm_map: dict[str, str] = {}
        for n in d.get("query", {}).get("normalized", []) or []:
            norm_map[n.get("to", "")] = n.get("from", "")
        pages = d.get("query", {}).get("pages", {})
        for _, p in pages.items():
            url = (p.get("imageinfo") or [{}])[0].get("url")
            if not url:
                continue
            title = p.get("title", "")
            from_title = norm_map.get(title, title)
            if from_title.startswith("File:"):
                fn = from_title[5:]
                by_filename[fn] = url
            elif title.startswith("File:"):
                fn = title[5:]
                by_filename[fn] = url
        time.sleep(0.3)
    for m in matches:
        m["image_url"] = by_filename.get(m["filename"])
    return matches


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
