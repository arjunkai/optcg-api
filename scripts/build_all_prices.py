"""
Build card_id -> price mapping from TCGPlayer scrapes, using dotgg.gg's
authoritative tcg_id<->card_id map to avoid positional-guess errors.

Pipeline:
  1. Parse every data/tcgplayer_raw/*.md into (tcg_id, price, variant_label) rows.
  2. Index them as a flat dict {tcg_id: {price, name, ...}}.
  3. Load dotgg catalog (data/dotgg_catalog.json) which maps every card_id to
     its definitive tcg_id(s). This is the ground truth — our _p8 vs _p6
     can't be figured out from TCGPlayer naming alone because cheap promo
     variants get interleaved with expensive alt-arts.
  4. For every card in cards.json:
     a. If dotgg knows the card, use its tcg_id to look up TCGPlayer's price
        (TCGPlayer is still our price-of-record since it's the live market).
     b. If that tcg_id isn't in our TCGPlayer scrape, fall back to dotgg's
        own price data (less fresh but correct).
     c. If dotgg doesn't have the card at all, fall back to the old
        positional matching as last resort.

The old positional matching is kept only for cards dotgg doesn't cover
(~2% of DB) — mostly brand-new sets, truly unique event promos.

Usage:
  python scripts/fetch_dotgg_catalog.py          # run once or before build
  python scripts/build_all_prices.py

Outputs:
  data/card_prices_all.json            {card_id: {price, tcg_ids, source_set}}
  data/card_prices_all.unmatched.json  list of unmappable TCGPlayer rows
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from parse_tcgplayer_prices import parse_file as parse_md
from map_prices_to_cards import VARIANT_LABEL_TO_TYPE, load_cards

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "tcgplayer_raw"
DOTGG_FILE = DATA_DIR / "dotgg_catalog.json"
TCGCSV_FILE = DATA_DIR / "tcgcsv_optcg.json"  # scripts/fetch_tcgcsv_optcg.py
# Hand-checked card -> TCGPlayer product links, applied after every automatic
# step: {"OP09-004_p5": {"tcg_id": 657442, "note": "Gold SP, checked by eye"}}.
OVERRIDES_FILE = DATA_DIR / "price_mapping_overrides.json"

# Sanity ceiling for dotgg-only fallback prices (no TCGPlayer sale exists for
# the card). Above this, dotgg's number is almost always the TCGPlayer listed
# median (an asking price), not a real sale — the source of phantom values like
# $87,500. Rejected entries are logged for manual curation in
# data/manual_prices.json. See also backfill_prices_dotgg.py.
DOTGG_PRICE_CEILING = 300.0


def to_float(v) -> float:
    try:
        return float(v) if v not in (None, "", "0", "0.000000") else 0.0
    except (TypeError, ValueError):
        return 0.0


def to_tcg_ids(v) -> list[int]:
    if not v:
        return []
    return [int(x) for x in str(v).split(",") if str(x).strip().isdigit()]


STRICT_ALT_LABELS = {"Alternate Art", "Parallel"}


def label_type(product: dict) -> str:
    """What a TCGPlayer product's labels say it is.

    'base' (no label), 'manga' (any "(Manga)", e.g. "Shanks (Parallel) (Manga)
    (Alternate Art)"), 'reprint', 'alt_art' (Alternate Art / Parallel only), or
    'other' for promo labels (Judge, Treasure Cup, ...) that can't be told
    apart from the label alone.
    """
    suffix = product.get("name_suffix")
    if suffix in ("Manga", "Manga Rare") or "(Manga)" in (product.get("name") or ""):
        return "manga"
    if suffix is None:
        return "base"
    if suffix == "Reprint":
        return "reprint"
    if suffix in STRICT_ALT_LABELS:
        return "alt_art"
    return "other"


def card_type(card: dict) -> str:
    return (card.get("variant_type") or "alt_art") if card.get("parallel") else "base"


def correct_shared_tcg_ids(assign: dict, cards: dict, products: dict, rows_by_number: dict) -> dict:
    """Fix dotgg mappings that hand a family member's product to another card.

    dotgg sometimes maps a parallel to its base's or sibling's product: OP-17
    had OP17-005_p1/_p2 on the base Newgate (711499, $0.70) and the Linlin
    manga OP17-112_p2 on the alt art (711324), while TCGPlayer lists the
    mangas at ~$1,058 and ~$709.

    A card is corrected only when all of these hold, so a right mapping is
    never moved (OP01-120_p2 shares the OP-01 manga product with the PRB-01
    manga reprint, and both are right):
      * its product is shared with another card of the same base,
      * that other card's type matches the product's label (it owns it),
      * this card's type doesn't,
      * exactly one unclaimed product for the number carries this card's
        exact label (Manga / Reprint / Alternate Art / Parallel).

    assign: card_id -> tcg_id. Returns {card_id: corrected tcg_id}.
    """
    family: dict[str, list[str]] = defaultdict(list)
    for cid in assign:
        family[cards[cid].get("base_id") or cid].append(cid)
    fixes: dict[str, int] = {}
    for base, ids in family.items():
        used = {assign[i] for i in ids}
        for cid in sorted(ids):
            tid = assign[cid]
            label = label_type(products[tid])
            want = card_type(cards[cid])
            if want == label or want not in ("manga", "reprint", "alt_art"):
                continue
            if label == "other" and want == "alt_art":
                continue  # a promo-labeled alt art is plausibly right
            owners = [i for i in ids if i != cid and assign[i] == tid and card_type(cards[i]) == label]
            if not owners:
                continue
            cands = list(dict.fromkeys(
                r["tcg_id"] for r in rows_by_number.get(base, [])
                if label_type(r) == want and r["tcg_id"] not in used and r["price"] is not None
            ))
            if len(cands) == 1:
                fixes[cid] = cands[0]
                used.add(cands[0])
    return fixes


SET_TEXT_RE = re.compile(r"\[[A-Z]{2,4}-?\d")  # "-ROMANCE DAWN- [OP-01]": a set, not a promo label


def _norm_label(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Words an official card-set text may leave out of TCGPlayer's label for the
# same product: "Japanese 2nd Anniversary Set" is TCGPlayer's "One Piece
# Japanese Version 2nd Anniversary Set", "Premium Card Collection -Best
# Selection-" its "... -Best Selection Vol. 1-". A stamp word (winner,
# finalist, judge, ...) never counts as filler.
LABEL_FILLER = {"one", "piece", "version", "edition", "vol", "the", "english", "japanese"}


def _label_tokens(s: str | None) -> set[str]:
    s = re.sub(r"([a-z])-(\d)", r"\1\2", (s or "").lower())  # "OP-12" == "OP12"
    return set(re.findall(r"[a-z0-9]+", s))


def labels_agree(official: str | None, product: str | None) -> bool:
    """The official card-set text names this TCGPlayer product label.

    Every official word must be in the product label, and the product label
    may add only filler words and numbers. Set texts ("-ROMANCE DAWN-
    [OP-01]") never agree: they name a pack, not a print.
    """
    if not official or not product or SET_TEXT_RE.search(official):
        return False
    o, p = _label_tokens(official), _label_tokens(product)
    if len(o) < 2 or not o <= p:
        return False
    return all(t in LABEL_FILLER or t.isdigit() for t in p - o)


def match_promo_labels(cards: dict, dotgg: dict, rows_by_number: dict) -> dict:
    """card_id -> tcg_id where the official card-set label names the product.

    For promos the official site's card-set text says how the card was
    distributed ("CS 25-26 Celebration Pack"), and TCGPlayer uses the same
    words as the product label. dotgg's tcg_id sometimes points at a sibling
    print instead: EB01-014_p1 (official: CS 25-26 Celebration Pack, $67.58)
    was mapped to the Tournament Pack print ($0.50). Matches only when exactly
    one product for the number has that exact label and only one card of the
    family carries the label. dotgg's CardSets field is the official text.
    """
    by_family: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for cid in cards:
        label = (dotgg.get(cid) or {}).get("CardSets") or ""
        if label and not SET_TEXT_RE.search(label):
            by_family[cards[cid].get("base_id") or cid].append((cid, _norm_label(label)))
    out: dict[str, int] = {}
    for base, members in by_family.items():
        counts = Counter(lab for _, lab in members)
        for cid, lab in members:
            if counts[lab] != 1:
                continue
            hits = {r["tcg_id"] for r in rows_by_number.get(base, [])
                    if _norm_label(r["name_suffix"]) == lab and r["price"] is not None}
            if len(hits) == 1:
                out[cid] = hits.pop()
    return out


# A product whose label the official card-set text names may win on a weaker
# picture match (TCGPlayer photos of stamped anniversary / premium prints
# often score 0.55-0.85 against the clean official scan), as long as it leads
# every other product by LABEL_LEAD.
LABEL_MATCH = 0.55
LABEL_LEAD = 0.10


def label_backed(scores: dict, current: int | None, official: str | None, labels: dict) -> int | None:
    """The product the picture and the official label agree on, or None."""
    known = {t: s for t, s in scores.items() if s is not None}
    if not known or not official:
        return None
    best = max(known, key=known.get)
    if best == current or known[best] < LABEL_MATCH:
        return None
    if not labels_agree(official, labels.get(best)):
        return None
    if current is not None and labels_agree(official, labels.get(current)):
        return None  # the current product is the one the label names
    if any(t != best and known[best] - s < LABEL_LEAD for t, s in known.items()):
        return None
    return best


def verify_by_image(assign: dict, cards: dict, rows_by_number: dict, vectors, unresolved: list | None = None,
                    official: dict | None = None) -> dict:
    """card_id -> tcg_id where the official image picks a different product.

    Checks parallels, P- promos and cards with no mapping yet against every
    TCGPlayer product printed with the same number (image_match.py). A card
    moves only when one product clearly matches its picture (decide()), or
    matches it well and is the product the official card-set text names
    (label_backed(); `official` is card_id -> dotgg CardSets). The product
    must not be held by another card of the family that matches it as well,
    and no other card of the family may want it about as much.
    """
    from image_match import MATCH, TIE, decide, similarity, tcgplayer_image
    official = official or {}

    def wants_check(cid: str) -> bool:
        c = cards[cid]
        # Japan-exclusive rows (image from the JA site) share art with English
        # prints but aren't them: OP07-064_p3's Japanese manga matched the EN
        # "Championship 2024 Finalist" product. An EN TCGPlayer price never
        # applies to them.
        if "//www.onepiece-cardgame.com" in (c.get("image_url") or ""):
            return False
        return bool(c.get("parallel")) or cid.startswith("P-") or cid not in assign

    family: dict[str, list[str]] = defaultdict(list)
    for cid, c in cards.items():
        family[c.get("base_id") or cid].append(cid)

    todo = [cid for cid in cards if wants_check(cid) and rows_by_number.get(cards[cid].get("base_id") or cid)
            and cards[cid].get("image_url")]
    urls = {cards[c]["image_url"] for c in todo}
    urls |= {tcgplayer_image(r["tcg_id"]) for c in todo
             for r in rows_by_number[cards[c].get("base_id") or c]}
    vectors.ensure(urls)

    def score(cid: str, tid: int):
        return similarity(vectors.get(cards[cid].get("image_url")), vectors.get(tcgplayer_image(tid)))

    proposals: dict[str, tuple[int, float]] = {}
    for cid in todo:
        base = cards[cid].get("base_id") or cid
        cands = {r["tcg_id"] for r in rows_by_number[base] if r.get("price") is not None}
        labels = {r["tcg_id"]: r.get("name_suffix") for r in rows_by_number[base]}
        scores = {t: score(cid, t) for t in cands}
        best = decide(scores, assign.get(cid))
        if best is None:
            best = label_backed(scores, assign.get(cid), official.get(cid), labels)
        if best is None:
            cur = scores.get(assign.get(cid))
            known = [s for s in scores.values() if s is not None]
            if (unresolved is not None and cur is not None and cur < 0.8 and known
                    and max(known) - cur >= TIE):
                # Linked product's photo doesn't look like the card, another
                # product looks more like it, but not clearly: a human should
                # look. (When the linked product is already the best match,
                # its photo is just poor: stamped prints often score 0.6-0.8.)
                unresolved.append({"card_id": cid, "tcg_id": assign.get(cid), "score": round(cur, 3),
                                   "best_other": round(max(known), 3)})
            continue
        holders = [o for o in family[base] if o != cid and assign.get(o) == best]
        if any((score(o, best) or 0) >= MATCH for o in holders):
            continue  # another print of this card owns that product
        proposals[cid] = (best, scores[best])

    out: dict[str, int] = {}
    by_product: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for cid, (tid, s) in proposals.items():
        by_product[tid].append((s, cid))
    for tid, wanted in by_product.items():
        wanted.sort(reverse=True)
        if len(wanted) > 1 and wanted[0][0] - wanted[1][0] < TIE:
            continue  # two cards look the same as this product; can't tell
        out[wanted[0][1]] = tid
    return out


STAMP_RE = re.compile(r"championship|\bcs\b|celebration|winner|finalist|top \d|regional|treasure|"
                      r"store|judge|tournament|participation|event pack|anniversary|pre-?release", re.I)


def load_overrides(cards: dict, products: dict) -> dict:
    """card_id -> tcg_id from OVERRIDES_FILE, skipping (and warning about)
    entries whose card or priced product no longer exists."""
    if not OVERRIDES_FILE.exists():
        return {}
    out: dict[str, int] = {}
    for cid, entry in json.loads(OVERRIDES_FILE.read_text(encoding="utf-8")).items():
        if cid.startswith("_"):
            continue  # comment keys
        tid = int(entry["tcg_id"] if isinstance(entry, dict) else entry)
        if cid not in cards:
            print(f"WARNING: override {cid}: no such card, skipped")
        elif tid not in products or products[tid].get("price") is None:
            print(f"WARNING: override {cid}: product {tid} missing or unpriced, skipped")
        else:
            out[cid] = tid
    return out


def write_mapping_summary(report: dict, matched: dict, products: dict, rows_by_number: dict) -> None:
    """Markdown for the run summary + data/price_mapping_report.json."""
    used = {t for v in matched.values() for t in v["tcg_ids"]}
    missing = sorted(
        (r for rows in rows_by_number.values() for r in rows
         if r["tcg_id"] not in used and r.get("price") and r.get("name_suffix") and STAMP_RE.search(r["name_suffix"])),
        key=lambda r: -r["price"])
    missing = list({r["tcg_id"]: r for r in missing}.values())
    report["unrepresented_stamped"] = [
        {"tcg_id": r["tcg_id"], "number": r["number"], "label": r["name_suffix"], "price": r["price"]} for r in missing]
    (DATA_DIR / "price_mapping_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def fmt(t):
        p = products.get(t) or {}
        return f"{p.get('name_suffix') or 'base'} ${p.get('price')}" if t else "-"
    lines = ["### Promo & variant pricing", ""]
    for key, title in (("label", "Matched by official promo label"), ("shared", "Moved off a sibling's product"),
                       ("image", "Re-matched by picture"), ("override", "Hand-checked overrides")):
        changes = report.get(key) or {}
        lines.append(f"**{title}: {len(changes)}**")
        lines += [f"- {c}: {fmt(o)} → {fmt(n)}" for c, (o, n) in sorted(changes.items())[:40]]
        lines.append("")
    un = report.get("unresolved") or []
    lines.append(f"**Picture doesn't match its product, no clear fix (check by hand): {len(un)}**")
    lines += [f"- {u['card_id']}: linked {fmt(u['tcg_id'])} (similarity {u['score']}, best other {u['best_other']})"
              for u in un[:30]]
    lines += ["", f"**Stamped TCGPlayer prints with no card of ours: {len(missing)}** "
              f"(${sum(r['price'] for r in missing):,.0f} total; top 15)"]
    lines += [f"- {r['number']} ({r['name_suffix']}) ${r['price']}" for r in missing[:15]]
    md = "\n".join(lines)
    print(md)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")


# Most specific first: "OP11 Release Event Winner" is a winner print,
# "Championship 2024 Finalist Card Set" a finalist one.
STAMP_RULES = [
    ("winner", r"winner"),
    ("finalist", r"finalist"),
    ("top_players", r"top players|top \d"),
    ("treasure_cup", r"treasure (cup|campaign|booster)"),  # not the in-set "Treasure Rare"
    ("regional", r"regional"),
    ("store", r"\bstore\b"),  # Store Championship packs are store-level events
    ("championship", r"championship|\bcs ?\d|celebration"),
    ("judge", r"judge"),
    ("pre_release", r"pre-?release"),
    ("release_event", r"release event"),
    ("anniversary", r"anniversary"),
    ("tournament", r"tournament|participation|event pack|promotion pack|battle|league"),
    ("collection", r"collection|binder|illustration box|gift|\bset\b|\bdeck\b|sound loader|playmat|magazine|"
                   r"welcome pack|retail"),
    ("event", r"expo|gen con|convention|one piece day|new year|pirates party|film red|dash pack|round 1"),
]


# Named premium treatments worth surfacing as print_label (no stamp):
# Super Leader Parallel (new in OP-17), Super / Red Super Alternate Art,
# Heroine Leader (EB-05), Manga, SP, Treasure Rare, Wanted Poster, ...
TREATMENT_RE = re.compile(r"super|heroine leader|manga|\bsp\b|treasure rare|^tr$|wanted poster|full art|"
                          r"box topper|serial", re.I)


def stamp_for(label: str | None) -> tuple[str | None, str | None]:
    """(print_label, stamp) for a TCGPlayer product label.

    * Event/promo stamps: the label and its category ("CS 25-26 Celebration
      Pack" -> championship).
    * Named premium treatments: the label with no stamp, so clients can tell
      a Super Leader Parallel ("Super Leader Alternate Art", OP17-079_p2,
      EB05-010_p2) or a "Heroine Leader" (EB-05) from a plain alt art; our
      variant_type only knows alt_art / manga / reprint / serial.
    * None for generic labels (Alternate Art, Parallel, Reprint, foils) and
      labels that only tell two cards of the same name apart ("Daz.Bonez",
      "Zala", DON "Gold").
    """
    if not label:
        return None, None
    # Event words first: "2025 Pirates League Finals Textured Foil" is an
    # event print even though it says "foil".
    for stamp, pat in STAMP_RULES:
        if re.search(pat, label, re.I):
            return label, stamp
    if TREATMENT_RE.search(label):
        return label, None
    return None, None  # generic in-set label or a name disambiguator


def main() -> None:
    if not DOTGG_FILE.exists():
        print(f"ERROR: {DOTGG_FILE} not found. Run `python scripts/fetch_dotgg_catalog.py` first.")
        return

    sets_meta = json.loads((DATA_DIR / "sets.json").read_text(encoding="utf-8"))
    by_id, parallels_by_base = load_cards()
    dotgg = json.loads(DOTGG_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(dotgg)} cards from dotgg catalog")

    # ── 1. Parse all TCGPlayer scrapes into a flat tcg_id -> {price, set_id, name} map.
    tcg_products: dict[int, dict] = {}
    # Order: regulars first (newer to older), then PRB last. Keeps the original-set
    # price as canonical when the same tcg_id appears on multiple guides.
    PREMIUM_SET_PREFIXES = ("PRB-",)
    sets_meta.sort(key=lambda s: (s["set_id"].startswith(PREMIUM_SET_PREFIXES), s["pack_id"]))

    total_rows = 0
    rows_by_number: dict[str, list[dict]] = defaultdict(list)
    for s in sets_meta:
        md_path = RAW_DIR / f"{s['set_id']}.md"
        if not md_path.exists():
            continue
        for row in parse_md(md_path):
            total_rows += 1
            tcg_id = row["tcg_id"]
            if tcg_id in tcg_products:
                continue  # first-seen wins (original set)
            rows_by_number[row["number"]].append(row)
            tcg_products[tcg_id] = {
                "price": row["price"],
                "set_id": s["set_id"],
                "name": row["name"],
                "name_suffix": row["name_suffix"],
            }
    scraped = len(tcg_products)
    # tcgcsv (TCGPlayer's catalog) fills every product the price-guide scrape
    # didn't reach: release-event, pre-release, anniversary, demo-deck groups
    # that have no sets.json entry. Scraped prices stay primary where both
    # exist (a few two-printing products differ: ST01-001 guide $0.49 Normal,
    # tcgcsv only lists Foil $30.49).
    if TCGCSV_FILE.exists():
        for row in json.loads(TCGCSV_FILE.read_text(encoding="utf-8")):
            if row["tcg_id"] in tcg_products:
                continue
            tcg_products[row["tcg_id"]] = {
                "price": row["price"],
                "set_id": f"tcgcsv:{row['group_id']}",
                "name": row["name"],
                "name_suffix": row["name_suffix"],
            }
            rows_by_number[row["number"]].append(row)
    else:
        print(f"WARNING: {TCGCSV_FILE} missing; using the price-guide scrape only")
    print(f"Indexed {len(tcg_products)} unique TCGPlayer products "
          f"({scraped} from price guides across {total_rows} rows, {len(tcg_products) - scraped} from tcgcsv)")

    # ── 2. Authoritative pass: dotgg tells us which tcg_id each card_id maps to.
    matched: dict[str, dict] = {}
    now = int(time.time())

    def upsert(card_id: str, price: float, tcg_ids: list[int], source_set: str, method: str) -> None:
        matched[card_id] = {
            "price": round(price, 2),
            "tcg_ids": tcg_ids,
            "source_set": source_set,
            "match_method": method,
            "price_updated_at": now,
        }

    authoritative = 0
    dotgg_fallback = 0
    dotgg_rejected_high = []
    no_dotgg_entry = []

    # dotgg's tcg_id for each card with a priced TCGPlayer product, then fix
    # the ones where dotgg handed a base's product to two cards.
    assign: dict[str, int] = {}
    for card_id in by_id.keys():
        ids = to_tcg_ids((dotgg.get(card_id) or {}).get("tcg_ids"))
        tid = next((t for t in ids if t in tcg_products), None)
        if tid is not None and tcg_products[tid]["price"] is not None:
            assign[card_id] = tid
    mapping_report: dict = {"image": {}, "unresolved": []}
    labeled = match_promo_labels(by_id, dotgg, rows_by_number)
    for cid, tid in sorted(labeled.items()):
        if assign.get(cid) != tid:
            old = assign.get(cid)
            print(f"  promo label {cid}: {old} (${tcg_products[old]['price'] if old else '-'}) "
                  f"-> {tid} (${tcg_products[tid]['price']})")
    assign_before = dict(assign)
    assign.update(labeled)
    fixes = correct_shared_tcg_ids(assign, by_id, tcg_products, rows_by_number)
    mapping_report["label"] = {c: [assign_before.get(c), t] for c, t in labeled.items() if assign_before.get(c) != t}
    mapping_report["shared"] = {c: [assign[c], t] for c, t in fixes.items()}
    print(f"Relabeled {len(fixes)} dotgg mapping(s) that shared a tcg_id within a base:")
    for cid, new in sorted(fixes.items()):
        print(f"  {cid}: {assign[cid]} (${tcg_products[assign[cid]]['price']}) "
              f"-> {new} (${tcg_products[new]['price']})")
    for cid, tid in labeled.items():
        fixes.setdefault(cid, tid)  # label matches are final; write them like fixes

    # Strongest evidence last: the official picture vs each product's photo.
    if os.environ.get("OPTCG_SKIP_IMAGE_CHECK") != "1":
        from image_match import Vectors
        vectors = Vectors()
        current = {**assign, **fixes}
        unresolved: list[dict] = []
        official = {cid: (dotgg.get(cid) or {}).get("CardSets") for cid in by_id}
        by_image = verify_by_image(current, by_id, rows_by_number, vectors, unresolved, official)
        vectors.save()
        print(f"Image check: {len(by_image)} card(s) re-matched by picture "
              f"({vectors.fetched} new thumbnails downloaded)")
        for cid, tid in sorted(by_image.items()):
            old = current.get(cid)
            print(f"  image {cid}: {old} (${tcg_products[old]['price'] if old else '-'}) "
                  f"-> {tid} {tcg_products[tid]['name_suffix']!r} (${tcg_products[tid]['price']})")
        fixes.update(by_image)
        mapping_report["image"] = {c: [current.get(c), t] for c, t in by_image.items()}
        mapping_report["unresolved"] = unresolved

    # Hand-checked links win over everything automatic.
    overrides = load_overrides(by_id, tcg_products)
    before = {**assign, **fixes}
    for cid, tid in sorted(overrides.items()):
        if before.get(cid) != tid:
            print(f"  override {cid}: {before.get(cid)} -> {tid} {tcg_products[tid]['name_suffix']!r} "
                  f"(${tcg_products[tid]['price']})")
    mapping_report["override"] = {c: [before.get(c), t] for c, t in overrides.items() if before.get(c) != t}
    fixes.update(overrides)
    mapping_report["unresolved"] = [u for u in mapping_report["unresolved"] if u["card_id"] not in overrides]

    for card_id in by_id.keys():
        if card_id in fixes:
            tid = fixes[card_id]
            upsert(card_id, tcg_products[tid]["price"], [tid], tcg_products[tid]["set_id"], "dotgg+tcgplayer")
            authoritative += 1
            continue
        dotgg_row = dotgg.get(card_id)
        if not dotgg_row:
            no_dotgg_entry.append(card_id)
            continue

        tcg_ids = to_tcg_ids(dotgg_row.get("tcg_ids"))
        # Try TCGPlayer first — fresher, it's our primary market.
        tcg_match = next((tcg_products[tid] for tid in tcg_ids if tid in tcg_products), None)
        if tcg_match and tcg_match["price"] is not None:
            upsert(card_id, tcg_match["price"], tcg_ids, tcg_match["set_id"], "dotgg+tcgplayer")
            authoritative += 1
            continue

        # Dotgg price fallback (uses their cached value).
        price = to_float(dotgg_row.get("price"))
        if price <= 0:
            price = to_float(dotgg_row.get("foilPrice"))
        # Reject asking-price noise — see DOTGG_PRICE_CEILING note above.
        if price > DOTGG_PRICE_CEILING:
            dotgg_rejected_high.append({"card_id": card_id, "dotgg_price": round(price, 2), "tcg_ids": tcg_ids})
            continue
        if price > 0:
            upsert(card_id, price, tcg_ids, "dotgg", "dotgg-only")
            dotgg_fallback += 1

    print(f"\nAuthoritative pass (dotgg tcg_id -> TCGPlayer scrape): {authoritative}")
    print(f"Dotgg-own-price fallback:                              {dotgg_fallback}")
    print(f"Cards NOT in dotgg:                                    {len(no_dotgg_entry)}")
    if dotgg_rejected_high:
        rej = DATA_DIR / "dotgg_rejected_high.json"
        rej.write_text(json.dumps(dotgg_rejected_high, indent=2), encoding="utf-8")
        print(f"Dotgg prices rejected above ${DOTGG_PRICE_CEILING:.0f} (asking-price noise): "
              f"{len(dotgg_rejected_high)} -> {rej}; curate in data/manual_prices.json")

    # ── 3. Positional fallback ONLY for cards dotgg doesn't know about.
    # Uses the old logic — builds claim-state fresh, iterates sets in release order.
    claimed: set[str] = set()
    unmatched: list[dict] = []
    by_id_no_dotgg = set(no_dotgg_entry)
    # Skip a tcg_id once it has claimed a parallel. Otherwise a product
    # cross-listed across multiple guide MD files (e.g. an OP-01 alt in
    # 569901.md the universal Promotion bucket AND OP-01.md the set guide)
    # claims candidates[0] in the first iteration, then candidates[0]-after-
    # claim in the second, stamping the same tcg_id on two different
    # parallels. Saw this on 2026-05-22 for OP01-001 (485262 stamped on
    # both _p1 and _p2). See audit_optcg_positional_conflation.py.
    seen_tcg_ids: set[int] = set(fixes.values())  # never reuse a corrected card's product

    # Re-use all scraped rows (not just deduped products)
    for s in sets_meta:
        md_path = RAW_DIR / f"{s['set_id']}.md"
        if not md_path.exists():
            continue
        for row in parse_md(md_path):
            suffix = row["name_suffix"]
            number = row["number"]
            # Base card fallback
            if suffix is None:
                if number in by_id_no_dotgg and number not in matched:
                    upsert(number, row["price"], [row["tcg_id"]], s["set_id"], "positional-base")
                continue
            if row["tcg_id"] in seen_tcg_ids:
                continue
            # Variant fallback — only among parallels not in dotgg
            target_type = VARIANT_LABEL_TO_TYPE.get(suffix, "alt_art")
            candidates = [
                c for c in parallels_by_base.get(number, [])
                if c["id"] in by_id_no_dotgg
                and c["id"] not in claimed
                and c.get("variant_type") == target_type
            ]
            if not candidates:
                continue
            chosen = candidates[0]
            claimed.add(chosen["id"])
            seen_tcg_ids.add(row["tcg_id"])
            upsert(chosen["id"], row["price"], [row["tcg_id"]], s["set_id"], "positional-parallel")

    positional = sum(1 for v in matched.values() if v["match_method"].startswith("positional"))
    print(f"Positional fallback (cards not in dotgg):              {positional}")

    for entry in matched.values():
        product = tcg_products.get(entry["tcg_ids"][0]) if entry["tcg_ids"] else None
        entry["print_label"], entry["stamp"] = stamp_for(product and product["name_suffix"])

    write_mapping_summary(mapping_report, matched, tcg_products, rows_by_number)

    # ── 4. Write outputs
    (DATA_DIR / "card_prices_all.json").write_text(
        json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DATA_DIR / "card_prices_all.unmatched.json").write_text(
        json.dumps(unmatched, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    methods = defaultdict(int)
    for v in matched.values():
        methods[v["match_method"]] += 1
    print(f"\nTotal matched cards: {len(matched)}")
    for method, count in sorted(methods.items(), key=lambda x: -x[1]):
        print(f"  {count:5}  {method}")
    print()
    print("Wrote data/card_prices_all.json")


if __name__ == "__main__":
    main()
