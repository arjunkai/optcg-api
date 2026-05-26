"""
Pre-fetch canonical English names for JA cards. Two-tier lookup:

  1. JA card name (Japanese katakana) → PokeAPI species → EN name.
     This is a local hash lookup, no network per card. Works because
     PokeAPI's pokemon_species_names.csv carries every Pokemon's name
     in both Japanese (language_id=1) and English (language_id=9), so
     resolving "ヒトカゲ" → 4 → "Charmander" is one dict lookup. We
     match longest-prefix-first so "リザードンVMAX" picks "リザードン"
     (Charizard) over "リザード" (Charmeleon).

  2. TCGdex EN endpoint with dexId-collision verification. Used only
     when the JA name doesn't contain any Pokemon species — i.e.
     trainers, supporters, energies. Accepts the EN result only if
     both sides have no dexId (genuine non-Pokemon) OR their dexIds
     intersect (same Pokemon). Rejects "DP3-4 JA Charmander gets EN
     name 'Entei'" type collisions.

Output: data/ja_card_id_to_en_name.json {card_id: en_name}

Used by image and price backfill scripts as a fallback name source
for JA cards, and by scripts/backfill-ptcg-name-en.js to populate
ptcg_cards.name_en in D1 (drives latin-script search of Japanese
cards — e.g. typing "Charmander" finds JA ヒトカゲ rows).

Usage:
    python -m scripts.enrich_ja_card_names              # imageless cards
    python -m scripts.enrich_ja_card_names --all-unpriced  # all unpriced JA
    python -m scripts.enrich_ja_card_names --all          # every JA card
    python -m scripts.enrich_ja_card_names --all --rebuild  # full reset (after a logic fix)
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path("data/ja_card_id_to_en_name.json")
SPECIES_CSV = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species_names.csv"
TCGDEX_EN = "https://api.tcgdex.net/v2/en/cards/{card_id}"
TCGDEX_JA = "https://api.tcgdex.net/v2/ja/cards/{card_id}"
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]

# Card_ids where automated resolution gets the wrong species. Manual
# overrides always win over both tier-1 (JA-name match) and tier-2
# (TCGdex EN endpoint). Verified by hand against the JA card art.
MANUAL_OVERRIDES: dict[str, str] = {
    "DP3-82": "Golem",
}


# Energy and trainer cards whose JA name has no Pokémon species (so tier 1
# can't resolve) and no per-card_id EN counterpart in D1 (JA and EN sets
# carry different set_ids for these). Without this vocab, eBay JA search
# can't query them by EN name — see backfill_ptcg_prices_ebay's
# `_to_en_name` priority chain.
#
# Entries are restricted to canonical Pokémon TCG English names with
# high-confidence community agreement. Per [[feedback-no-plausible-wrong-prices]]
# wrong EN names produce wrong eBay matches, so we skip ambiguous cards
# (Holon series, Unit Energy variants, generic Special-prefix energies)
# rather than guess.
JA_NAME_VOCAB: dict[str, str] = {
    # Basic energies — universal canon across every era
    "基本水エネルギー": "Basic Water Energy",
    "基本炎エネルギー": "Basic Fire Energy",
    "基本草エネルギー": "Basic Grass Energy",
    "基本悪エネルギー": "Basic Darkness Energy",
    "基本超エネルギー": "Basic Psychic Energy",
    "基本闘エネルギー": "Basic Fighting Energy",
    "基本雷エネルギー": "Basic Lightning Energy",
    "基本鋼エネルギー": "Basic Metal Energy",
    "基本フェアリーエネルギー": "Basic Fairy Energy",
    # Special energies — established canonical names
    "ダブル無色エネルギー": "Double Colorless Energy",
    "レインボーエネルギー": "Rainbow Energy",
    "ダブルターボエネルギー": "Double Turbo Energy",
    "ツインエネルギー": "Twin Energy",
    "プリズムエネルギー": "Prism Energy",
    "ジェットエネルギー": "Jet Energy",
    "オーロラエネルギー": "Aurora Energy",
    "フュージョンエネルギー": "Fusion Strike Energy",
    "ビーストエネルギー": "Beast Energy",
    "リバーサルエネルギー": "Reversal Energy",
    "ルミナスエネルギー": "Luminous Energy",
    "いちげきエネルギー": "Single Strike Energy",
    "れんげきエネルギー": "Rapid Strike Energy",
    "ダブルドラゴンエネルギー": "Double Dragon Energy",
    "マルチエネルギー": "Multi Energy",
    "プラズマエネルギー": "Plasma Energy",
    "スピード雷エネルギー": "Speed Lightning Energy",
    "ヒート炎エネルギー": "Heat Fire Energy",
    "ウォッシュ水エネルギー": "Wash Water Energy",
    "ストロングエネルギー": "Strong Energy",
    "バーニングエネルギー": "Burning Energy",
    "メモリーエネルギー": "Memory Energy",
    "ヒーリングエネルギー": "Healing Energy",
    "ウィークガードエネルギー": "Weakness Guard Energy",
    "Vガードエネルギー": "V Guard Energy",
    "スクランブルエネルギー": "Scramble Energy",
    "ワープエネルギー": "Warp Energy",
    "パワフル無色エネルギー": "Powerful Colorless Energy",
    # SWSH/SV-era "Type-Modifier" energies (Bulbapedia article titles use
    # single-letter codes — "Stone F Energy" — but printed card names use
    # the full type word, which is what eBay listings use.)
    "ハイド悪エネルギー": "Hiding Darkness Energy",
    "ホラー超エネルギー": "Horror Psychic Energy",
    "ニトロ炎エネルギー": "Nitro Fire Energy",
    "ストーン闘エネルギー": "Stone Fighting Energy",
    "マグネット鋼エネルギー": "Magnetic Metal Energy",
    "バブル水エネルギー": "Bubble Water Energy",
    "グロウ草エネルギー": "Grow Grass Energy",
    "アロマ草エネルギー": "Aromatic Grass Energy",
    "テレパス超エネルギー": "Telepathic Psychic Energy",
    # Special energies — vintage to modern, canonical EN names
    "SPエネルギー": "SP Energy",
    "サイクロンエネルギー": "Cyclone Energy",
    "クリスタルエネルギー": "Crystal Energy",
    "カウンターエネルギー": "Counter Energy",
    "キャプチャーエネルギー": "Capture Energy",
    "リアクトエネルギー": "React Energy",
    "レスキューエネルギー": "Rescue Energy",
    "インパクトエネルギー": "Impact Energy",
    "リゲインエネルギー": "Regain Energy",
    "ギフトエネルギー": "Gift Energy",
    "シールドエネルギー": "Shield Energy",
    "ミステリーエネルギー": "Mystery Energy",
    "ミストエネルギー": "Mist Energy",
    "レガシーエネルギー": "Legacy Energy",
    "ブーメランエネルギー": "Boomerang Energy",
    "フラッシュエネルギー": "Flash Energy",
    "フルヒールエネルギー": "Full Heal Energy",
    "ポーションエネルギー": "Potion Energy",
    "ドローエネルギー": "Draw Energy",
    "トリプル加速エネルギー": "Triple Acceleration Energy",
    "トレジャーエネルギー": "Treasure Energy",
    "デルタレインボーエネルギー": "Delta Rainbow Energy",
    "ダークメタルエネルギー": "Dark Metal Energy",
    "ハーブエネルギー": "Herbal Energy",
    "イグニッションエネルギー": "Ignition Energy",
    "リサイクルエネルギー": "Recycle Energy",
    "闇のエネルギー": "Darkness Energy",
    # CAUTION: these two trap on literal-romanji translation. Printed
    # English names are NOT "Rich" / "Therapy".
    "リッチエネルギー": "Enriching Energy",
    "セラピーエネルギー": "Therapeutic Energy",
    # Pre-Diamond/Pearl rule-text quirk: 特殊悪/特殊鋼 ("special X") was
    # the JP descriptor for the special-energy variant of Darkness/Metal.
    # The printed English card just reads "Darkness Energy" / "Metal Energy".
    "特殊悪エネルギー": "Darkness Energy",
    "特殊鋼エネルギー": "Metal Energy",
    # Letter-code energies — eBay listings use the letter suffix.
    "ブレンドエネルギー 草炎超悪": "Blend Energy GFPD",
    "ブレンドエネルギー 水雷闘鋼": "Blend Energy WLFM",
    "ユニットエネルギー草炎水": "Unit Energy GRW",
    "ユニットエネルギー闘悪フェアリー": "Unit Energy FDY",
    "ユニットエネルギー雷超鋼": "Unit Energy LPM",
    "ホロンエネルギーGL": "Holon Energy GL",
    "ホロンエネルギーff": "Holon Energy FF",
    "ホロンエネルギーwp": "Holon Energy WP",
    # Second-pass tail additions (researched 2026-05-26).
    "エネルギーアーク": "Energy Ark",
    "コーティング鋼エネルギー": "Coating Metal Energy",
    "スパイクエネルギー": "Spiky Energy",
    "スプラッシュエネルギー": "Splash Energy",
    "ロケット団エネルギー": "Team Rocket's Energy",
    "ワンダーエネルギー": "Wonder Energy",
    "超ブーストエネルギー": "Super Boost Energy",
    "金属エネルギー": "Metal Energy",
    "アクアエネルギー": "Aqua Energy",
    "アッパーエネルギー": "Upper Energy",
    "エネルギーコイン": "Energy Coin",
    "エネルギーシール": "Energy Sticker",
    "エネルギースピナー": "Energy Spinner",
    "エネルギーポーチ": "Energy Pouch",
    "エネルギー転送PRO": "Energy Search Pro",
    "エネルギー交換装置": "Energy Exchanger",
    "エネルギーリセット": "Energy Reset",
    # Energy-related Trainer cards
    "エネルギーつけかえ": "Energy Switch",
    "エネルギー回収": "Energy Retrieval",
    "スーパーエネルギー回収": "Super Energy Retrieval",
    "エネルギーリサイクル": "Energy Recycler",
    "エネルギー電荷": "Energy Charge",
    "エネルギーリムーブ": "Energy Removal",
    "エネルギー除去": "Energy Removal",
    "エネルギー除去2": "Energy Removal 2",
    "スーパーエネルギー除去2": "Super Energy Removal 2",
    # GOTCHA: literal romaji disagrees with printed EN name.
    # エネルギー転送 (lit. "Energy Transfer") is the JP card whose
    # English print is "Energy Search". Keep below the by-name fallback
    # mappings so the eBay query doesn't search the wrong string.
    "エネルギー転送": "Energy Search",
    # Third-pass tail additions — older/alternate JP notations and
    # 1-off cards (researched 2026-05-26).
    "Wレインボーエネルギー": "Double Rainbow Energy",
    "δレインボーエネルギー": "Delta Rainbow Energy",
    "rエネルギー": "R Energy",
    "ダブルアクアエネルギー": "Double Aqua Energy",
    "ダブルマグマエネルギー": "Double Magma Energy",
    "ネオアッパーエネルギー": "Neo Upper Energy",
    # TRAP: literal romaji is "Bad Energy" but English print is "Dangerous Energy".
    "バッドエネルギー": "Dangerous Energy",
    "マグマエネルギー": "Magma Energy",
    "スパイラルエネルギー": "Spiral Energy",
    "メディカルエネルギー": "Medical Energy",
    "レトロなエネルギー": "Retro Energy",
    # Alias of エネルギーつけかえ — D1 has both forms in different rows.
    "エネルギースイッチ": "Energy Switch",
    "サイキックエネルギー": "Basic Psychic Energy",
    # *クイック is a Quick Construction Pack product tag, not a card variant.
    "基本草エネルギー*クイック": "Basic Grass Energy",
    # Older non-canonical shorthand for the basic energies (DB-only forms,
    # not printed on any card face — likely OCR/transcription artifacts).
    "水エネルギー": "Basic Water Energy",
    "火エネルギー": "Basic Fire Energy",
    "稲妻エネルギー": "Basic Lightning Energy",
    "草のエネルギー": "Basic Grass Energy",
    "二重の無色のエネルギー": "Double Colorless Energy",
    "超エネルギー除去": "Super Energy Removal",
    # Deliberately NOT mapped: エネルギーの流れ, エネルギーを高めます,
    # エネルギー回復, エネルギー検索, エネルギー根 — all confirmed by
    # 2026-05-26 research to NOT be card names (flavor text fragments,
    # attack-text snippets, or literal-translation mislabelings in the
    # source data). エネルギー循環装置 also held (no Bulbapedia exact
    # match; could be Energy Switch / Charge / Pickup depending on era).
    # 二重虹のエネルギー and スーパーエネルギー検索 also confirmed
    # not-real-cards.
    # 悪エネルギー, 鋼エネルギー (PCG10-101/102), ロック闘エネルギー
    # (M3-080) skipped due to set-context ambiguity (special vs basic
    # variants share the bare name; multiple printed EN variants exist). Per [[feedback-no-plausible-wrong-prices]],
    # holding rather than guessing.
    #
    # ホロンエネルギー SYN at PCGP-114/115/116 are handled by per-card_id
    # UPDATE rather than this name-keyed dict — the three rows share the
    # same `name` but resolve to different EN cards (GL/FF/WP) based on
    # local_id. See scripts/jp_batches/backfill_holon_syn_promos.sql.
}


# Card_id → EN name for cases where multiple physical cards share the
# same JA `name` field (so the name-keyed dict above can't disambiguate).
# Currently the PCGP Holon Energy promos: D1 has all three under the
# literal name "ホロンエネルギー SYN" (the "SYN" prefix on Holon energy
# JP names didn't get split out into a suffix during import), but the
# three card_ids resolve to GL/FF/WP variants per Bulbapedia's PCG-P
# Promotional cards listing.
JA_NAME_OVERRIDES_BY_ID: dict[str, str] = {
    "PCGP-114": "Holon Energy GL",
    "PCGP-115": "Holon Energy FF",
    "PCGP-116": "Holon Energy WP",
}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-unpriced", action="store_true",
                    help="Enrich ALL JA cards needing prices (not just imageless)")
    ap.add_argument("--all", action="store_true",
                    help="Enrich every JA card in D1. Use after fixing a "
                         "logic bug to refresh the whole mapping.")
    ap.add_argument("--rebuild", action="store_true",
                    help="Ignore the existing cache and re-resolve every "
                         "card. Pair with --all after a logic fix to "
                         "overwrite stale entries.")
    args = ap.parse_args()

    print("1. Loading PokeAPI species maps (EN + JA)...")
    en_by_dex, ja_pairs = _load_species_maps()
    print(f"   {len(en_by_dex)} EN species, {len(ja_pairs)} JA species names")

    if args.all:
        print("2. Querying D1 for ALL JA cards...")
        sql = "SELECT card_id, name FROM ptcg_cards WHERE lang='ja' ORDER BY card_id"
    elif args.all_unpriced:
        print("2. Querying D1 for ALL unpriced JA cards...")
        sql = ("SELECT card_id, name FROM ptcg_cards WHERE lang='ja' "
               "AND (price_source IS NULL OR price_source='cardmarket') "
               "ORDER BY card_id")
    else:
        print("2. Querying D1 for JA imageless cards...")
        sql = ("SELECT card_id, name FROM ptcg_cards WHERE lang='ja' "
               "AND image_high IS NULL ORDER BY card_id")
    out = subprocess.run(
        WRANGLER + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        print("D1 query failed:", (out.stderr or "")[:500])
        sys.exit(1)
    start = (out.stdout or "").find("[")
    rows = json.loads(out.stdout[start:])[0]["results"]
    print(f"   {len(rows)} JA cards needing enrichment")

    # Resume support: load existing cache (unless --rebuild)
    cache: dict[str, str] = {}
    if OUT.exists() and not args.rebuild:
        cache = json.loads(OUT.read_text(encoding="utf-8"))
        print(f"   Resume: {len(cache)} already cached")
    elif args.rebuild and OUT.exists():
        print(f"   --rebuild: ignoring existing cache, will overwrite")

    print("3. Resolving EN names...")
    new_count = 0
    species_hits = 0
    vocab_hits = 0
    tcgdex_hits = 0
    network_calls = 0
    headers = {"User-Agent": "OPBindr/1.0"}

    for i, row in enumerate(rows, 1):
        cid = row["card_id"]
        ja_name = (row.get("name") or "").strip()

        if not args.rebuild and cid in cache and cache[cid] and cid not in MANUAL_OVERRIDES:
            continue

        if cid in MANUAL_OVERRIDES:
            cache[cid] = MANUAL_OVERRIDES[cid]
            new_count += 1
            continue

        if cid in JA_NAME_OVERRIDES_BY_ID:
            cache[cid] = JA_NAME_OVERRIDES_BY_ID[cid]
            new_count += 1
            continue

        en_name = ""
        called_network = False

        # Tier 1: local JA name → species → EN. Fast hash lookup.
        if ja_name:
            en_name = _resolve_species(ja_name, ja_pairs, en_by_dex)
            if en_name:
                species_hits += 1

        # Tier 1.5: JA name vocab for energies and energy-related trainers.
        # Pokemon-species lookup doesn't match these because they carry
        # no Pokemon name, and the TCGdex EN fallback also misses (the
        # cross-language card_id rarely aligns for energies). The vocab
        # dict is the only path.
        if not en_name and ja_name in JA_NAME_VOCAB:
            en_name = JA_NAME_VOCAB[ja_name]
            vocab_hits += 1

        # Tier 2: TCGdex EN fallback for trainers/energies (no species
        # in the JA name). Verify dexId compatibility before accepting.
        if not en_name:
            en_name = _tcgdex_en_fallback(cid, headers)
            if en_name:
                tcgdex_hits += 1
            network_calls += 1
            called_network = True

        cache[cid] = en_name
        if en_name:
            new_count += 1

        if i % 200 == 0:
            print(f"   [{i}/{len(rows)}] resolved {new_count} "
                  f"(species: {species_hits}, vocab: {vocab_hits}, "
                  f"tcgdex: {tcgdex_hits}, network calls: {network_calls})")
            OUT.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

        # Rate-limit only the network path (tier 2). Tier 1 is local
        # and runs at full speed.
        if called_network:
            time.sleep(0.12)

    OUT.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    resolved = sum(1 for v in cache.values() if v)
    print(f"\n4. Wrote {OUT}")
    print(f"   {resolved}/{len(cache)} cards resolved")
    print(f"   tier-1 species hits: {species_hits}")
    print(f"   tier-2 tcgdex hits:  {tcgdex_hits}")
    print(f"   network calls made:  {network_calls}")


def _resolve_species(ja_card_name: str, ja_pairs: list, en_by_dex: dict) -> str:
    """Find the longest JA species name that appears in the card name,
    return the EN name for that species. Returns "" if no species
    matches (probably a trainer/energy)."""
    if not ja_card_name:
        return ""
    for ja_name, dex_id in ja_pairs:
        if ja_name in ja_card_name:
            return en_by_dex.get(dex_id, "")
    return ""


def _tcgdex_en_fallback(card_id: str, headers: dict) -> str:
    """Hit TCGdex EN endpoint with dexId-collision verification.

    Accepts the EN name when:
      - EN has no dexId (trainer / supporter / energy / item) — no risk
        of mixing up two Pokemon cards.
      - JA-side query also returns no dexId AND EN has no dexId.
    Rejects when EN has a dexId (it's a Pokemon card), to avoid the
    DP3-4 → Entei collision pattern.
    """
    try:
        req = urllib.request.Request(
            TCGDEX_EN.format(card_id=urllib.parse.quote(card_id)),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            d_en = json.load(r)
        en_dex_ids = d_en.get("dexId") or []
        if en_dex_ids:
            return ""
        return (d_en.get("name") or "").strip()
    except urllib.error.HTTPError:
        return ""
    except Exception:
        return ""


def _load_species_maps() -> tuple[dict, list]:
    """Load PokeAPI species CSV. Returns (en_by_dex, ja_pairs).

    ja_pairs is a list of (japanese_name, dex_id) sorted by name length
    DESC so longest-prefix matching in _resolve_species picks the most
    specific species. PokeAPI's local_language_id=1 is Japanese kana
    (what's printed on cards). language_id=9 is English.
    """
    req = urllib.request.Request(SPECIES_CSV, headers={"User-Agent": "OPBindr/1.0"})
    data = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    en_by_dex: dict[int, str] = {}
    ja_pairs: list[tuple[str, int]] = []
    for row in csv.DictReader(io.StringIO(data)):
        lang = int(row["local_language_id"])
        sid = int(row["pokemon_species_id"])
        name = (row.get("name") or "").strip()
        if not name:
            continue
        if lang == 9:
            en_by_dex[sid] = name
        elif lang == 1:
            ja_pairs.append((name, sid))
    ja_pairs.sort(key=lambda p: -len(p[0]))
    return en_by_dex, ja_pairs


if __name__ == "__main__":
    main()
