"""
Remap every Bulbagarden EN image URL to pokemontcg.io English-print URL
where one exists. Bulbapedia's image archive serves the SAME FILE for
Sun & Moon EN and Collection Sun JA (and same for ScarletViolet ↔ sv*
JA, XY ↔ Collection X/Y JA). User-visible result: cards display Japanese
text on what should be the English binder grid.

Strategy:
1. Pull every EN card whose image_high points at archives.bulbagarden.net
2. Parse the Bulbagarden filename → (parent_set_tag, card_number)
3. Map tag → pokemontcg.io set ID via SET_TAG_MAP
4. HEAD-test pokemontcg.io URL — only remap if the English print exists
5. Cards where no English print exists keep their Bulbagarden URL (will
   show JA text but at least render). Could be re-mapped later if a
   different English-print source surfaces.

Idempotent: re-running picks up newly-added Bulbagarden URLs (e.g. from
this Monday's cron) and remaps them. Only writes to D1 if the new URL
HEAD-tests 200, so we never replace a working JA-print with a broken
English URL.

Usage:
    python -m scripts.remap_bulbagarden_to_pokemontcg --dry-run
    python -m scripts.remap_bulbagarden_to_pokemontcg
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)

DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("data/backfill/remap_bulbagarden")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Bulbapedia set-tag in filename → pokemontcg.io set ID. Verified against
# api.pokemontcg.io 2026-05-04. Tags that have no English-print equivalent
# (e.g. McDonald's-specific where pokemontcg.io's mcd23/mcd24 don't exist)
# stay unmapped and the Bulbagarden URL persists.
SET_TAG_MAP = {
    # XY era
    "KalosStarterSet":      "xy0",
    "XY":                   "xy1",
    "Flashfire":            "xy2",
    "FuriousFists":         "xy3",
    "PhantomForces":        "xy4",
    "PrimalClash":          "xy5",
    "RoaringSkies":         "xy6",
    "AncientOrigins":       "xy7",
    "BREAKthrough":         "xy8",
    "BREAKpoint":           "xy9",
    "FatesCollide":         "xy10",
    "SteamSiege":           "xy11",
    "Evolutions":           "xy12",
    "Generations":          "g1",
    "DoubleCrisis":         "dc1",
    # Sun & Moon era
    "SunMoon":              "sm1",
    "GuardiansRising":      "sm2",
    "BurningShadows":       "sm3",
    "ShiningLegends":       "sm35",
    "CrimsonInvasion":      "sm4",
    "UltraPrism":           "sm5",
    "ForbiddenLight":       "sm6",
    "CelestialStorm":       "sm7",
    "DragonMajesty":        "sm75",
    "LostThunder":          "sm8",
    "TeamUp":               "sm9",
    "DetectivePikachu":     "det1",
    "UnbrokenBonds":        "sm10",
    "UnifiedMinds":         "sm11",
    "HiddenFates":          "sm115",
    "CosmicEclipse":        "sm12",
    # Sword & Shield era
    "SwordShield":          "swsh1",
    "RebelClash":           "swsh2",
    "DarknessAblaze":       "swsh3",
    "ChampionsPath":        "swsh35",
    "VividVoltage":         "swsh4",
    "ShiningFates":         "swsh45",
    "BattleStyles":         "swsh5",
    "ChillingReign":        "swsh6",
    "EvolvingSkies":        "swsh7",
    "Celebrations":         "cel25",
    "FusionStrike":         "swsh8",
    "BrilliantStars":       "swsh9",
    "AstralRadiance":       "swsh10",
    "PokemonGo":            "pgo",
    "LostOrigin":           "swsh11",
    "SilverTempest":        "swsh12",
    "CrownZenith":          "swsh12pt5",
    # Diamond & Pearl era
    "DiamondPearl":         "dp1",
    "MysteriousTreasures":  "dp2",
    "SecretWonders":        "dp3",
    "GreatEncounters":      "dp4",
    "MajesticDawn":         "dp5",
    "LegendsAwakened":      "dp6",
    "Stormfront":           "dp7",
    "Platinum":             "pl1",
    "RisingRivals":         "pl2",
    "SupremeVictors":       "pl3",
    "Arceus":               "pl4",
    # HGSS / BW eras
    "HeartGoldSoulSilver":  "hgss1",
    "Unleashed":            "hgss2",
    "Undaunted":            "hgss3",
    "Triumphant":           "hgss4",
    "CallOfLegends":        "col1",
    "BlackWhite":           "bw1",
    "EmergingPowers":       "bw2",
    "NobleVictories":       "bw3",
    "NextDestinies":        "bw4",
    "DarkExplorers":        "bw5",
    "DragonsExalted":       "bw6",
    "DragonVault":          "dv1",
    "BoundariesCrossed":    "bw7",
    "PlasmaStorm":          "bw8",
    "PlasmaFreeze":         "bw9",
    "PlasmaBlast":          "bw10",
    "LegendaryTreasures":   "bw11",
    # Scarlet & Violet era
    "ScarletViolet":        "sv1",
    "PaldeaEvolved":        "sv2",
    "ObsidianFlames":       "sv3",
    "PaldeanFates":         "sv4pt5",
    "ParadoxRift":          "sv4",
    "TemporalForces":       "sv5",
    "TwilightMasquerade":   "sv6",
    "ShroudedFable":        "sv6pt5",
    "StellarCrown":         "sv7",
    "SurgingSparks":        "sv8",
    "PrismaticEvolutions":  "sv8pt5",
    "JourneyTogether":      "sv9",
    "DestinedRivals":       "sv10",
    # Pokemon-Card-151 (special set)
    "PokemonCard151":       "sv3pt5",
    "PokémonCard151":  "sv3pt5",
    # Vintage & misc
    "Aquapolis":            "ecard2",
    "Skyridge":             "ecard3",
    "BaseSet":              "base1",
    "Jungle":               "base2",
    "Fossil":               "base3",
    "TeamRocket":           "base5",
    "GymHeroes":            "gym1",
    "GymChallenge":         "gym2",
    "NeoGenesis":           "neo1",
    "NeoDiscovery":         "neo2",
    "NeoRevelation":        "neo3",
    "NeoDestiny":           "neo4",
    "Expedition":           "ecard1",
    "EXRubySapphire":       "ex1",
    "EXSandstorm":          "ex2",
    "EXDragon":             "ex3",
    "EXTeamMagmavsTeamAqua":"ex4",
    "EXHiddenLegends":      "ex5",
    "EXFireRedLeafGreen":   "ex6",
    "EXTeamRocketReturns":  "ex7",
    "EXDeoxys":             "ex8",
    "EXEmerald":            "ex9",
    "EXUnseenForces":       "ex10",
    "EXDeltaSpecies":       "ex11",
    "EXLegendMaker":        "ex12",
    "EXHolonPhantoms":      "ex13",
    "EXCrystalGuardians":   "ex14",
    "EXDragonFrontiers":    "ex15",
    "EXPowerKeepers":       "ex16",
}

# Filename pattern: {Name}{Tag}{Number}.{ext}. Tag is alphanumeric
# (PascalCase typically). Number is digits possibly followed by a letter.
# Examples we saw: SpoinkXY49.jpg, GeodudeMysteriousTreasures84.jpg,
# SprigatitoScarletViolet13.jpg, ReshiramCelebrations2.jpg.
FILENAME_RE = re.compile(
    r"^(?P<name>.+?)(?P<tag>[A-Z][A-Za-z0-9]*?)(?P<num>\d+\w*)\.(jpg|jpeg|png)$",
    re.IGNORECASE,
)


def query_d1(sql: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=str(REPO_ROOT),
    )
    data = json.loads(out.stdout)
    if not data or not data[0].get("success"): return []
    return data[0]["results"] or []


def head(url: str) -> int | str:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return "ERR"


def parse_bulbagarden_filename(url: str) -> tuple[str, str, str] | None:
    """Returns (name, tag, number) or None if the filename doesn't match
    the {Name}{Tag}{Number}.ext pattern. Greedy tag-matching is tricky —
    iterate over candidate tag boundaries from longest to shortest.
    """
    fname = url.rsplit("/", 1)[-1]
    # Try each known tag from longest to shortest — longer tags should
    # win (e.g. 'KalosStarterSet' beats 'Kalos').
    name_part_re = re.compile(r"^(?P<name>.+?)$")
    for tag in sorted(SET_TAG_MAP.keys(), key=len, reverse=True):
        m = re.match(rf"^(?P<name>.+?){re.escape(tag)}(?P<num>\d+\w*)\.(jpg|jpeg|png)$",
                     fname, re.IGNORECASE)
        if m:
            return m.group("name"), tag, m.group("num")
    return None


def candidate_pokemontcg_url(setid: str, number: str) -> str:
    # Strip any trailing letter for now; pokemontcg.io uses both
    # number-only and number_letter formats. Try plain first.
    return f"https://images.pokemontcg.io/{setid}/{number}_hires.png"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lang", default="en", choices=["en", "ja"])
    args = ap.parse_args()

    print(f"1. Pulling {args.lang} cards on archives.bulbagarden.net...")
    cards = query_d1(
        f"SELECT card_id, set_id, image_high FROM ptcg_cards "
        f"WHERE lang='{args.lang}' AND image_high LIKE '%bulbagarden%'"
    )
    print(f"   {len(cards)} candidates")

    print("\n2. Parsing filenames + checking pokemontcg.io English prints...")
    remappable = []
    skipped = []
    for c in cards:
        parsed = parse_bulbagarden_filename(c["image_high"])
        if not parsed:
            skipped.append((c["card_id"], "no tag match", c["image_high"]))
            continue
        _name, tag, num = parsed
        ptcg_set = SET_TAG_MAP.get(tag)
        if not ptcg_set:
            skipped.append((c["card_id"], f"tag {tag!r} not mapped", c["image_high"]))
            continue
        # Strip trailing letter for the digit portion (e.g. "74a" → "74")
        digits_only = re.sub(r"[A-Za-z]+$", "", num)
        candidate = candidate_pokemontcg_url(ptcg_set, digits_only)
        st = head(candidate)
        if st == 200:
            remappable.append({
                "card_id": c["card_id"],
                "set_id": c["set_id"],
                "old_url": c["image_high"],
                "new_url": candidate,
                "tag": tag,
                "ptcg_set": ptcg_set,
            })
            print(f"   [OK]   {c['card_id']:14s} ({tag} → {ptcg_set}) → {candidate}", flush=True)
        else:
            skipped.append((c["card_id"], f"pokemontcg.io {st}", candidate))

    print()
    print(f"Remappable: {len(remappable)}")
    print(f"Skipped:    {len(skipped)}")
    print()
    print('Skip reasons (top 5):')
    from collections import Counter
    by_reason = Counter(s[1].split(' ')[0] for s in skipped)
    for r, n in by_reason.most_common(5):
        print(f"  {n:>5}  {r}")

    if not remappable:
        print("\nNothing to remap.")
        return

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sql_lines = [
        f"-- Bulbagarden → pokemontcg.io English-print remap.",
        f"-- Generated: {fetched_at}, lang={args.lang}",
        f"-- Bulbapedia files for SunMoon/SV/XY-era are categorized under both",
        f"-- EN and JA sets and are often the JA-print scan. This swaps them",
        f"-- for the parent-set English-print URL on pokemontcg.io.",
    ]
    for r in remappable:
        url = r["new_url"].replace("'", "''")
        sql_lines.append(
            f"UPDATE ptcg_cards SET image_high='{url}', image_low='{url}' "
            f"WHERE lang='{args.lang}' AND card_id='{r['card_id']}';"
        )

    sql_path = OUT_DIR / f"remap_{args.lang}.sql"
    sql_path.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    json_path = OUT_DIR / f"remap_{args.lang}.json"
    json_path.write_text(json.dumps(remappable, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSQL: {sql_path}")

    if args.dry_run:
        print("--dry-run: D1 not touched")
        return

    print("\n3. Applying...")
    apply = subprocess.run(
        WRANGLER_BIN + ["--remote", "--file", str(sql_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT),
    )
    if apply.returncode != 0:
        print(f"D1 apply FAILED: {apply.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    for line in apply.stdout.split("\n"):
        if "rows_written" in line or "duration" in line.lower():
            print(f"   {line.strip()}")


if __name__ == "__main__":
    main()
