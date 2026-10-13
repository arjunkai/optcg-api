"""
Tag promo cards in ptcg_cards with the real-world campaign that
distributed them (Munch museum collab, Van Gogh Museum, McDonald's-by-
year, Pokemon Center DX, Champions League, kuji prize, etc.).

Source of truth is Bulbapedia. Each Signal in CAMPAIGN_SIGNALS picks
one of two enumeration modes:

  category_title  Walk Category:<name>. Members are pages titled with
                  the (SET[-P] Promo NNN) suffix and we parse them
                  directly. Cleanest when Bulbapedia has a dedicated
                  category for the campaign and members are the promo
                  prints themselves (Munch's Cards-with-The-Scream,
                  Van Gogh's Cards-with-Pika-Portrait).

  page_setlist    Fetch one master page's wikitext (e.g.
                  "SV-P Promotional cards (TCG)") and scan
                  {{Setlist/entry|NNN/SET-TOKEN|...|<campaign-text>}}
                  lines. Any row whose body contains
                  signal.setlist_match is tagged. Needed when the
                  campaign isn't categorized — Bulbapedia stores
                  Champions League 2023 only as setlist-row prose on
                  the SV-P master page, no per-card category.

Each Signal also carries `lang` so the UPDATE WHERE clause matches the
right language partition. This is non-negotiable post-Van Gogh:
`svp-085` lang=en (Pikachu with Grey Felt Hat, Van Gogh Museum) and
`SVP-85` lang=ja (Basic Darkness Energy) share the same (set_id,
local_id) and would collide if we matched cross-language. See
feedback_no_local_id_collision.md.

The match key is `(lang, UPPER(set_id), CAST(local_id AS INTEGER))` — the
2026-05-16 dedupe bug came from two ingest pipelines disagreeing on
case + zero-padding for the same physical card. Since 2026-10-01 the
matching runs in Python over ONE SELECT of the signals' languages
(sqlite_int reproduces SQLite's CAST), and each matched card gets one
UPDATE by primary key, only when its tags differ. The old per-key
UPDATE ... WHERE UPPER(set_id) = ? could use no index, so each of ~381
statements scanned the whole language: ~8.4M D1 rows read per run,
over Workers Free's 5M/day on its own. When several signals match one
card the last one in CAMPAIGN_SIGNALS wins, as it did when the UPDATEs
ran in order.

Output: scripts/enrich_campaigns/<slug>_<NNN>.sql

Usage:
    python -m scripts.enrich_ja_promo_campaigns --dry-run
        Crawl every signal, read D1 once to match cards, write SQL files.
        No D1 writes.

    python -m scripts.enrich_ja_promo_campaigns --apply
        Crawl + write + run each batch through wrangler.

    python -m scripts.enrich_ja_promo_campaigns --campaigns munch --apply
        Limit to a single campaign slug (good for validation runs).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from scripts.bulbapedia import BULBAPEDIA_API, USER_AGENT, describe_http_error
from scripts.wrangler_retry import WRANGLER_MAX_ATTEMPTS, run_wrangler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path("scripts/enrich_campaigns")
BATCH_SIZE = 250  # SQL statements per file (matches dedupe_ja_duplicates.py)
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
RATE_LIMIT_SECONDS = 1.1  # MediaWiki etiquette — single-threaded ~1 req/sec

Mode = Literal["category_title", "page_setlist"]


@dataclass(frozen=True)
class Signal:
    """One campaign tag and how to enumerate its members on Bulbapedia.

    slug                drives the output filename + the --campaigns filter
    campaign            free-text label stamped into ptcg_cards.campaign
    distribution_method coarse classifier stamped into ptcg_cards.distribution_method
                        (keep the vocab small — see migration 015 header)
    lang                'ja' or 'en' — added to UPDATE WHERE; never matches
                        cross-language because svp-085 lang=en and SVP-85
                        lang=ja are different cards
    mode                category_title | page_setlist
    bulbapedia_target   For category_title: category name (no 'Category:' prefix).
                        For page_setlist:   page name to fetch wikitext for.
    setlist_match       page_setlist only. Substring required in a setlist
                        entry line for it to count as part of this campaign.
                        Free-text — e.g. "Champions League 2023".
    set_id_override     page_setlist only. The D1 set_id to use for every
                        matched row in this page (master pages don't carry
                        per-row set ids — they're implicit from the page).
    """
    slug: str
    campaign: str
    distribution_method: str
    lang: str
    mode: Mode
    bulbapedia_target: str
    setlist_match: Optional[str] = None
    set_id_override: Optional[str] = None


# Order matters for last-write-wins UX: signals later in the list will
# overwrite a card's campaign tag if both apply. Keep cleanest signals
# (specific museum collabs) before broader ones (championship events).
CAMPAIGN_SIGNALS: list[Signal] = [
    Signal(
        slug="munch",
        campaign="Munch x Pokémon",
        distribution_method="art_museum_collaboration",
        lang="ja",
        mode="category_title",
        bulbapedia_target="Cards with The Scream",
    ),
    Signal(
        slug="van_gogh",
        campaign="Pokémon × Van Gogh Museum",
        distribution_method="art_museum_collaboration",
        lang="en",
        mode="category_title",
        bulbapedia_target="Cards with Pika-Portrait",
    ),
    Signal(
        slug="champions_league_2023",
        campaign="Champions League 2023",
        distribution_method="championship_event",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Champions League 2023",
        set_id_override="SVP",
    ),
    Signal(
        slug="champions_league_2024",
        campaign="Champions League 2024",
        distribution_method="championship_event",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Champions League 2024",
        set_id_override="SVP",
    ),
    Signal(
        slug="champions_league_2025",
        campaign="Champions League 2025",
        distribution_method="championship_event",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Champions League 2025",
        set_id_override="SVP",
    ),
    Signal(
        slug="champions_league_2026",
        campaign="Champions League 2026",
        distribution_method="championship_event",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="Champions League 2026",
        set_id_override="MP",
    ),
    Signal(
        slug="championship_series_2026_mp",
        campaign="Pokémon Card Game Championship Series 2026",
        distribution_method="championship_event",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="Championship Series 2026",
        set_id_override="MP",
    ),
    # Sibling Pokémon Card Gym distributions other than the boxed Promo
    # Card Pack: Entry Campaigns (digital-stamp redemption or new-player
    # onboarding), New Release Battle winner prizes, and events
    # participation prizes. Each is mechanically different from buying a
    # Pack, so they get their own distribution_method buckets
    # (card_gym_campaign / card_gym_event). These sit BEFORE the Promo
    # Card Pack signals in the list because SVP-237 is listed under both
    # Pack 9 AND First Entry Campaign — Pack 9 is the dominant
    # attribution, so Promo Card Pack must write last to win on overlap.
    # All setlist_match strings include "Card Gym" as a false-positive
    # guard (verified 2026-05-17: no other context on the SV-P/M-P pages
    # uses these phrases outside Card Gym entries).
    Signal(
        slug="card_gym_entry_campaign_mp",
        campaign="Pokémon Card Gym Entry Campaign",
        distribution_method="card_gym_campaign",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="Pokémon Card Gym Entry Campaign",
        set_id_override="MP",
    ),
    Signal(
        slug="card_gym_first_entry_campaign_svp",
        campaign="Pokémon Card Gym First Entry Campaign",
        distribution_method="card_gym_campaign",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Card Gym First Entry Campaign",
        set_id_override="SVP",
    ),
    Signal(
        slug="card_gym_battle_prize_svp",
        campaign="Pokémon Card Gym New Release Battle winner prize",
        distribution_method="card_gym_event",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Card Gym New Release Battle",
        set_id_override="SVP",
    ),
    Signal(
        slug="card_gym_event_prize_svp",
        campaign="Pokémon Card Gym events participation prize",
        distribution_method="card_gym_event",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Card Gym events participation prize",
        set_id_override="SVP",
    ),
    # Pokémon Card Gym Promo Card Pack: boxed promo packs sold at
    # participating Pokémon Card Gym stores in Japan. Recurring SKU
    # spanning multiple sets — SV-era ran Packs 1-10 across SV-P, MEGA-era
    # is mid-stream with Packs 1-4 across M-P. setlist_match is the bare
    # phrase; it matches both {{TCGMerch|...|Pokémon Card Gym Promo Card
    # Pack N}} header rows and plain "Pokémon Card Gym Promo Card Pack N"
    # follow-up rows. Stays at the END of the Card Gym block so it writes
    # last and wins on LID 237 overlap with First Entry Campaign.
    # Multi-line entries are handled — M-P LID 85 has Pack 4 on a
    # bullet-list continuation line that _iter_setlist_entries walks
    # via brace-depth scoping, so this signal now covers it alongside
    # the Entry Campaign on line 100 (Pack writes last → wins).
    Signal(
        slug="card_gym_promo_pack_mp",
        campaign="Pokémon Card Gym Promo Card Pack",
        distribution_method="card_gym_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="Pokémon Card Gym Promo Card Pack",
        set_id_override="MP",
    ),
    Signal(
        slug="card_gym_promo_pack_svp",
        campaign="Pokémon Card Gym Promo Card Pack",
        distribution_method="card_gym_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Pokémon Card Gym Promo Card Pack",
        set_id_override="SVP",
    ),
    # Tournament-shaped event promos: Extra Battle Day (recurring one-day
    # competitive event) and Endorsed Independent Event Trainers Pack
    # (Pokémon Japan-endorsed local tournaments run by independent
    # organizers). Both are event-distributed but distinct from Card Gym
    # in-store events — separate `event_promo` bucket. No overlap with
    # existing tagged ranges (verified: EBD/EIET LIDs don't collide with
    # Card Gym Promo Card Pack / Champions League 2023 / Card Gym
    # sibling LIDs).
    Signal(
        slug="extra_battle_day_svp",
        campaign="Pokémon Extra Battle Day",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Extra Battle Day",
        set_id_override="SVP",
    ),
    Signal(
        slug="extra_battle_day_mp",
        campaign="Pokémon Extra Battle Day",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="Extra Battle Day",
        set_id_override="MP",
    ),
    Signal(
        slug="endorsed_independent_event_svp",
        campaign="Endorsed Independent Event Trainers Pack",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Endorsed Independent Event Trainers Pack",
        set_id_override="SVP",
    ),
    Signal(
        slug="endorsed_independent_event_mp",
        campaign="Endorsed Independent Event Trainers Pack",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="Endorsed Independent Event Trainers Pack",
        set_id_override="MP",
    ),
    # Victini BWR Competition: tournament event running Promo Card Pack
    # (paid product, LIDs 272-279) + participation prize (LIDs 280+).
    # Single substring "Victini BWR Competition" catches both since
    # they're the same event-distributed semantic; campaign string is
    # generic so a future split into Pack-only / Prize-only signals
    # can specialize. Single bucket: event_promo.
    Signal(
        slug="victini_bwr_competition_svp",
        campaign="Victini BWR Competition",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Victini BWR Competition",
        set_id_override="SVP",
    ),
    # Scramble Battle: M-P era casual play event distribution.
    Signal(
        slug="scramble_battle_mp",
        campaign="Scramble Battle",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="Scramble Battle",
        set_id_override="MP",
    ),
    # Friend Battle Together: recurring event distributing basic-energy
    # holofoil promos at Pokémon Card Gym + retail. Same physical product
    # across SV-P (Jul-Sep 2024, LIDs 175-182) and M-P (Aug-Sep 2025
    # LIDs 9-16 + Mar-May 2026 LIDs 87-94). Wikitext frames each era
    # differently — SV-P calls it "participation prize", M-P calls it
    # "Holofoil Promo Card". Two signals with distinct campaign strings
    # preserve the era wording; both in event_promo bucket.
    #
    # The M-P substring must be the quoted form `"Friend Battle Together"
    # Holofoil` — bare "Holofoil" would collide with M-P LID 34
    # (Champions League 2026 Holofoil participation prize), and bare
    # "Friend Battle Together" is fine on M-P today but defensive
    # specificity is cheap.
    Signal(
        slug="friend_battle_together_svp",
        campaign="Friend Battle Together participation prize",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Friend Battle Together participation prize",
        set_id_override="SVP",
    ),
    Signal(
        slug="friend_battle_together_mp",
        campaign="Friend Battle Together Holofoil Promo Card",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match='"Friend Battle Together" Holofoil',
        set_id_override="MP",
    ),
    # Illustrator / streetwear collabs on the SV-P era — distinct from
    # museum-led art collabs (Munch, Van Gogh) which stay in
    # art_museum_collaboration. NAKANO STYLING TANTO is a Tokyo
    # streetwear brand; Yu Nagaba is an illustrator known for his
    # Pokémon Eeveelutions series. Bulbapedia uses unicode × in both
    # collab markers — setlist_match avoids the unicode char with
    # shorter unique substrings ("NAKANO" / "Yu Nagaba").
    Signal(
        slug="nakano_styling_tanto_svp",
        campaign="NAKANO STYLING TANTO × Pokémon Card Game",
        distribution_method="illustrator_collaboration",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="NAKANO",
        set_id_override="SVP",
    ),
    Signal(
        slug="yu_nagaba_svp",
        campaign="Yu Nagaba × Pokémon Card Game",
        distribution_method="illustrator_collaboration",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Yu Nagaba",
        set_id_override="SVP",
    ),
    # McDonald's Japan Promo Card on M-P — closes the McD gap deferred
    # in Phase 1a-2 (the McDonald's Collection canonical-page parsing
    # path is still pending; this signal works because M-P's master
    # setlist has the McD distribution inline rather than as a separate
    # set). 6 rows on M-P, none on SV-P.
    Signal(
        slug="mcdonalds_japan_mp",
        campaign="McDonald's Japan Promo Card",
        distribution_method="fast_food",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="M-P Promotional cards (TCG)",
        setlist_match="McDonald",
        set_id_override="MP",
    ),
    # Sealed Battle: umbrella for Triplet Beat Sealed Battle, Hot Wind
    # Arena Sealed Battle, and other recurring sealed-format event
    # distributions. Single substring catches them all; a future split
    # into per-event signals can specialize if filter UIs need it.
    # No SV-P card name contains "Sealed Battle" so the substring is
    # false-positive-safe.
    Signal(
        slug="sealed_battle_svp",
        campaign="Sealed Battle",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Sealed Battle",
        set_id_override="SVP",
    ),
    # Let's Start Playing Pokémon Card Game Campaign: SV-era
    # new-player onboarding promotion bundled with starter packs.
    Signal(
        slug="lets_start_playing_svp",
        campaign="Let's Start Playing Pokémon Card Game Campaign",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Let's Start Playing",
        set_id_override="SVP",
    ),
    # ex Starter Sets: the SV1-launch retail starter decks (Fuecoco &
    # Ampharos / Quaxly & Mimikyu / Sprigatito & Lucario / Pikachu &
    # Pawmot) each shipped with a Promo Card Pack. The 8 cards on LIDs
    # 2-9 are dual-distributed with Let's Start Playing; LID 13 (Rotom)
    # is ex Starter Set only. Sits AFTER lets_start_playing_svp so the
    # more specific retail-product attribution wins on overlap.
    # Substring "ex Starter Set" catches both singular and plural
    # ("ex Starter Sets Promo Card Pack"). Stays in event_promo bucket
    # to avoid new filter-pill vocab; the campaign string preserves
    # the distinct attribution for search.
    Signal(
        slug="ex_starter_sets_svp",
        campaign="ex Starter Sets",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="ex Starter Set",
        set_id_override="SVP",
    ),
    # Pokémon Card Summer Is Here: 2024 summer campaign distribution.
    Signal(
        slug="pokemon_summer_is_here_svp",
        campaign="Pokémon Card Summer Is Here! Promo Card Get Campaign",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Pokémon Card Summer Is Here",
        set_id_override="SVP",
    ),
    # Generations Start Deck Special Battle Set: tournament-shaped
    # bundle, 4 cards (LIDs 192-195).
    Signal(
        slug="special_battle_set_svp",
        campaign="Generations Start Deck Special Battle Set",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Special Battle Set",
        set_id_override="SVP",
    ),
    # Pokémon Trading Card Game Illustration Contest 2024 winning works.
    Signal(
        slug="illustration_contest_2024_svp",
        campaign="Pokémon Trading Card Game Illustration Contest 2024 Winning Work",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Illustration Contest",
        set_id_override="SVP",
    ),
    # CoroCoro magazine inserts. Recurring monthly distribution via
    # CoroCoro Ichiban! and CoroCoro Comic. 6 of the 12 hits are
    # multi-distribution cards also inserted in Pokémon Fan magazine —
    # CoroCoro takes precedence here because every Pokémon Fan hit is
    # also a CoroCoro hit (no Pokémon Fan-only cards), so a standalone
    # Pokémon Fan signal would tag zero unique cards.
    Signal(
        slug="corocoro_magazine_svp",
        campaign="CoroCoro magazine insert",
        distribution_method="magazine_insert",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="CoroCoro",
        set_id_override="SVP",
    ),
    # Energy Card Set: SV1-launch retail bundle (January 20, 2023) — a
    # 3-card item promo set distributed alongside the early SV expansion
    # boxes. LIDs 10-12: Poké Ball, Pokémon Switch, Nemona. Substring
    # "Energy Card Set" is a clean 3-hit catch on the SV-P master page;
    # no overlap with the "ex Starter Sets" (LIDs 2-9, 13) or "Let's
    # Start Playing" (LIDs 2-9) signals ordered before this one. Stays
    # in event_promo bucket alongside other retail-launch bundles.
    Signal(
        slug="energy_card_set_svp",
        campaign="Energy Card Set",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Energy Card Set",
        set_id_override="SVP",
    ),
    # Pokémon Center regional Special Boxes — Tohoku (LID 260, mid-Aug
    # 2025), Hiroshima (LID 261, early-Sep 2025), Fukuoka (LID 289,
    # late-Sep 2025). All three are city-themed Pikachu cards
    # distributed in their respective regional Pokémon Center stores.
    # setlist_match="Special Box" is a clean 3-hit substring — the
    # bare "Pokémon Center" form would false-positive on LIDs 239 and
    # 250 ("Pokémon Center Lady" card name) which are correctly tagged
    # Card Gym Pack 9 / Endorsed Independent Event. Verified by grep
    # on the live SV-P wikitext 2026-05-19. Three regional boxes share
    # the same product format so a single signal under one campaign
    # label is correct; future Pokémon Center Special Box releases
    # auto-pick up. Stays in event_promo bucket — no new filter-pill
    # vocab; campaign string preserves the regional/retail attribution.
    Signal(
        slug="pokemon_center_special_box_svp",
        campaign="Pokémon Center Special Box",
        distribution_method="event_promo",
        lang="ja",
        mode="page_setlist",
        bulbapedia_target="SV-P Promotional cards (TCG)",
        setlist_match="Special Box",
        set_id_override="SVP",
    ),
]

# Bulbapedia set codes → our promo set_id. Bare codes (no -P) come from
# titles that use the loose "(SM Promo 244)" form; for promo prints
# those are still members of the *-P promotional category, so SM → SMP.
_SET_OVERRIDES: dict[str, str] = {
    # The S-P promotional set spans Sword & Shield-era promos. Our D1
    # uses SWSHP for these rows, mirroring TCGdex's "swshp" id.
    "S-P": "SWSHP",
    "S": "SWSHP",
    "SWSH-P": "SWSHP",
    "SWSH": "SWSHP",
}

# Regex: "(SM-P Promo 288)" or "(SV-P Promo 12)" or "(SM Promo 244)".
_TITLE_RE = re.compile(r"\(([A-Z]+(?:-P)?)\s+Promo\s+(\d+)\)\s*$")

# Setlist row leader: {{Setlist/entry|NNN/...   or  {{Setlist/nmentry|NNN/...
# Only captures local_id — the per-row set token is informational on
# master pages (it's "SV-P" everywhere on the SV-P page), so we rely
# on Signal.set_id_override instead of re-parsing it.
_SETLIST_LINE_RE = re.compile(r"^\{\{Setlist/(?:entry|nmentry)\|(\d+)/")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Crawl + read D1 + write SQL files. No D1 writes.")
    g.add_argument("--apply", action="store_true",
                   help="Crawl, write SQL, AND apply against remote D1.")
    ap.add_argument("--campaigns", type=str, default="",
                    help="Comma-separated slugs to include. Empty = run "
                         "every signal in CAMPAIGN_SIGNALS.")
    args = ap.parse_args()

    wanted = {s.strip().lower() for s in args.campaigns.split(",") if s.strip()}
    signals = [s for s in CAMPAIGN_SIGNALS if not wanted or s.slug in wanted]
    if not signals:
        print(f"No signals matched --campaigns={args.campaigns!r}. "
              f"Available slugs: {sorted(s.slug for s in CAMPAIGN_SIGNALS)}")
        sys.exit(1)

    print(f"1. Crawling {len(signals)} Bulbapedia target"
          f"{'' if len(signals) == 1 else 's'}...")
    all_updates: list[tuple[Signal, list[tuple[str, int]]]] = []
    for s in signals:
        if s.mode == "category_title":
            members = _fetch_category_members(s.bulbapedia_target)
            keys = list(_parse_promo_keys(members))
            print(f"   [category_title] Category:{s.bulbapedia_target}: "
                  f"{len(members)} members, {len(keys)} promo prints "
                  f"→ slug={s.slug!r} lang={s.lang!r}")
        elif s.mode == "page_setlist":
            if not s.setlist_match or not s.set_id_override:
                raise ValueError(f"signal {s.slug!r} mode=page_setlist requires "
                                 f"setlist_match + set_id_override")
            wt = _fetch_page_wikitext(s.bulbapedia_target)
            keys = list(_parse_setlist_keys(wt, s))
            print(f"   [page_setlist]    {s.bulbapedia_target}: "
                  f"{len(keys)} rows matched {s.setlist_match!r} "
                  f"→ slug={s.slug!r} lang={s.lang!r} "
                  f"set_id={s.set_id_override}")
        else:
            raise ValueError(f"unknown mode: {s.mode!r}")
        if keys:
            all_updates.append((s, keys))
        time.sleep(RATE_LIMIT_SECONDS)

    if not all_updates:
        print("Nothing to write.")
        return

    langs = sorted({s.lang for s, _ in all_updates})
    print(f"2. Reading ptcg_cards for lang {', '.join(langs)} (one query)...")
    catalog = _fetch_catalog(langs)
    print(f"   {len(catalog)} rows")
    stmts = plan_updates(all_updates, catalog)
    print(f"   {len(stmts)} card(s) need new tags")
    if not stmts:
        print("Nothing to write.")
        return

    print("3. Writing batched UPDATE SQL...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = _write_batches(stmts)
    print(f"   wrote {len(files)} batch file(s) to {OUT_DIR}/")

    if args.dry_run:
        print("\nDry run done. Inspect scripts/enrich_campaigns/*.sql, "
              "then re-run with --apply.")
        return

    print("4. Applying batches against remote D1...")
    for i, f in enumerate(files, 1):
        print(f"   [{i}/{len(files)}] executing {f.name}...")
        result = run_wrangler(WRANGLER + [f"--file={f}", "--remote"])
        if result.returncode != 0:
            print(f"   FAIL after {WRANGLER_MAX_ATTEMPTS} attempts: "
                  f"{(result.stderr or '')[:400]}")
            sys.exit(1)
    print("Done.")


def _fetch_category_members(category: str) -> list[str]:
    """List every page in Category:<name>. Follows cmcontinue paging.
    Returns plain title strings (no namespace prefix)."""
    titles: list[str] = []
    cm_continue: str | None = None
    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmlimit": "500",
            "cmprop": "title",
            "format": "json",
        }
        if cm_continue:
            params["cmcontinue"] = cm_continue
        data = _api_get(params)
        for m in data.get("query", {}).get("categorymembers", []):
            t = m.get("title")
            if t:
                titles.append(t)
        cm_continue = data.get("continue", {}).get("cmcontinue")
        if not cm_continue:
            break
        time.sleep(RATE_LIMIT_SECONDS)
    return titles


def _parse_promo_keys(titles: list[str]) -> list[tuple[str, int]]:
    """Extract (set_id, local_id_int) pairs from page titles.

    Skips titles that don't fit the (SET[-P] Promo N) suffix — those
    are canonical-card pages reprinted as promos, and need the
    page_setlist path instead.
    """
    out: list[tuple[str, int]] = []
    skipped = 0
    for title in titles:
        m = _TITLE_RE.search(title)
        if not m:
            skipped += 1
            continue
        raw_set = m.group(1)
        set_id = _SET_OVERRIDES.get(raw_set)
        if not set_id:
            set_id = raw_set.replace("-", "")
            if not set_id.endswith("P"):
                set_id += "P"
        try:
            local_id = int(m.group(2))
        except ValueError:
            skipped += 1
            continue
        out.append((set_id, local_id))
    if skipped:
        print(f"     skipped {skipped} title(s) without (SET Promo N) suffix")
    return out


def _fetch_page_wikitext(page: str) -> str:
    """Fetch raw wikitext for a single Bulbapedia page."""
    data = _api_get({
        "action": "parse",
        "page": page,
        "prop": "wikitext",
        "format": "json",
    })
    wt = data.get("parse", {}).get("wikitext", {}).get("*")
    if not wt:
        print(f"   warn: no wikitext returned for page {page!r}")
        return ""
    return wt


def _iter_setlist_entries(wikitext: str) -> Iterator[tuple[int, str]]:
    """Yield (local_id, body) for each {{Setlist/entry|...}} or
    {{Setlist/nmentry|...}} template in the wikitext.

    The body is the joined text from the opener line through the
    closer line (net {{ vs }} depth returns to zero), so bullet-list
    continuation lines on multi-line entries are visible to a
    substring check downstream. Example — M-P LID 85 spans 3 lines:

        {{Setlist/entry|085/M-P|J|{{TCG ID|...|85}}|Item|||
        * Pokémon Card Gym Entry Campaign...
        * Pokémon Card Gym Promo Card Pack 4...}}

    The previous line-scoped parser saw only the opener and missed
    both distributions.

    Brace counting uses the `{{`/`}}` double-brace pair that wiki
    templates require; bare `{`/`}` are ignored. If an entry has
    unbalanced braces (malformed wikitext) the body is emitted at
    EOF so we never hang.
    """
    lines = wikitext.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].lstrip()
        if not (line.startswith("{{Setlist/entry") or
                line.startswith("{{Setlist/nmentry")):
            i += 1
            continue
        m = _SETLIST_LINE_RE.match(line)
        if not m:
            i += 1
            continue
        try:
            local_id = int(m.group(1))
        except ValueError:
            i += 1
            continue
        depth = 0
        body_parts: list[str] = []
        j = i
        closed = False
        while j < n:
            ln = lines[j]
            body_parts.append(ln)
            depth += ln.count("{{") - ln.count("}}")
            if depth <= 0:
                yield (local_id, "\n".join(body_parts))
                i = j + 1
                closed = True
                break
            j += 1
        if not closed:
            # Unbalanced — emit partial body and stop scanning.
            yield (local_id, "\n".join(body_parts))
            return


def _parse_setlist_keys(wikitext: str, signal: Signal) -> list[tuple[str, int]]:
    """Scan a master page's wikitext for setlist entries and emit
    (set_id, local_id_int) for each entry whose body contains
    signal.setlist_match.

    Entries are brace-depth-scoped (see _iter_setlist_entries), so a
    continuation line that carries the campaign attribution (M-P LID
    85's bullet-list Pack 4 entry) is part of the haystack.
    """
    matched = 0
    out: list[tuple[str, int]] = []
    for local_id, body in _iter_setlist_entries(wikitext):
        if signal.setlist_match not in body:
            continue
        matched += 1
        out.append((signal.set_id_override, local_id))  # type: ignore[arg-type]
    if matched == 0:
        print(f"     warn: 0 setlist rows matched — check setlist_match "
              f"{signal.setlist_match!r} against the page")
    return out


def sqlite_int(v) -> int:
    """SQLite's CAST(v AS INTEGER) for a TEXT/INTEGER local_id: optional
    leading spaces and sign, then the leading digits; no digits -> 0."""
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    m = re.match(r"[ \t\n\f\r]*([+-]?[0-9]+)", str(v))
    if not m:
        return 0
    return max(-(2 ** 63), min(2 ** 63 - 1, int(m.group(1))))  # SQLite clamps to int64


def plan_updates(updates: list[tuple[Signal, list[tuple[str, int]]]],
                 catalog: list[dict]) -> list[str]:
    """One UPDATE per card whose campaign/distribution_method must change.

    Matches (lang, UPPER(set_id), CAST(local_id AS INTEGER)) like the old
    SQL join did. The last matching signal wins per card; a card already
    carrying its tags gets no statement.
    """
    index: dict[tuple[str, str, int], list[dict]] = {}
    for row in catalog:
        key = (row["lang"], str(row["set_id"]).upper(), sqlite_int(row["local_id"]))
        index.setdefault(key, []).append(row)

    wanted: dict[tuple[str, str], tuple[dict, Signal]] = {}
    for sig, keys in updates:
        for set_id, local_id in keys:
            for row in index.get((sig.lang, set_id.upper(), int(local_id)), []):
                wanted[(row["card_id"], row["lang"])] = (row, sig)

    stmts = []
    for (card_id, lang), (row, sig) in wanted.items():
        if row.get("campaign") == sig.campaign and row.get("distribution_method") == sig.distribution_method:
            continue
        stmts.append(
            "UPDATE ptcg_cards SET "
            f"campaign = {_esc(sig.campaign)}, "
            f"distribution_method = {_esc(sig.distribution_method)} "
            f"WHERE card_id = {_esc(card_id)} AND lang = {_esc(lang)} "
            f"AND (campaign IS NOT {_esc(sig.campaign)} "
            f"OR distribution_method IS NOT {_esc(sig.distribution_method)});"
        )
    return stmts


def _fetch_catalog(langs: list[str]) -> list[dict]:
    sql = ("SELECT card_id, lang, set_id, local_id, campaign, distribution_method "
           f"FROM ptcg_cards WHERE lang IN ({', '.join(_esc(l) for l in langs)})")
    result = run_wrangler(WRANGLER + ["--remote", "--json", "--command", sql])
    if result.returncode != 0:
        print(f"   D1 read failed: {(result.stderr or '')[:400]}")
        sys.exit(1)
    start = (result.stdout or "").find("[")
    if start < 0:
        print("   D1 read returned no JSON")
        sys.exit(1)
    return json.loads(result.stdout[start:])[0]["results"]


def _write_batches(stmts: list[str]) -> list[Path]:
    files: list[Path] = []
    for i in range(0, len(stmts), BATCH_SIZE):
        path = OUT_DIR / f"campaigns_{(i // BATCH_SIZE) + 1:03d}.sql"
        path.write_text("\n".join(stmts[i:i + BATCH_SIZE]) + "\n", encoding="utf-8")
        files.append(path)
    return files


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _api_get(params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BULBAPEDIA_API}?{qs}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"   {describe_http_error(e, 'enrich_ja_promo_campaigns')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
