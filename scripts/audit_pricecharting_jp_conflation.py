"""Detect PriceCharting variant-conflated JA rows in ptcg_cards.

The PriceCharting backfill (`backfill_ptcg_prices_pricecharting.py`) keys
match on local_id alone without verifying that the PC slug's set-token
matches our row's set_id. For JA promo cards, this is catastrophic
because every promo set (SM-P, BW-P, XY-P, SV-P, M-P, S-P, DP-P, DPT-P)
renumbers from 1 — so a SVP-37 will happily match the PC slug for
SMP-37 if SVP-37 has no exact PC match itself.

This script is read-only: it pulls all `price_source='pricecharting'`
JA rows, parses each row's pc URL slug, extracts the trailing
`(local_id)(set-token)-p` pattern, and flags any row whose extracted
token doesn't match our `set_id` (or whose extracted local_id doesn't
match our `local_id`).

Output:
  data/backfill/pricecharting_conflation_audit.json
    — full per-row audit with parsed slug + verdict
  scripts/jp_batches/null_pc_conflated.sql
    — UPDATE statements to NULL out price_source + pricing_json for
      flagged rows so the next eBay JA backfill can re-price them
      cleanly

Verdict logic:
  - "ok"           : slug set-token matches our set_id, slug lid matches
  - "set_mismatch" : slug set-token points to a different set
  - "lid_mismatch" : slug set-token matches but the numeric lid differs
  - "unparseable"  : slug doesn't fit the `...-{N}{tag}-p` pattern
                    (vintage non-promo sets sometimes use other URL shapes)

Run:
  python -m scripts.audit_pricecharting_jp_conflation              # dry-run report
  python -m scripts.audit_pricecharting_jp_conflation --emit-sql   # write the NULL SQL too
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from scripts.wrangler_retry import run_wrangler


WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
OUT_AUDIT = Path("data/backfill/pricecharting_conflation_audit.json")
OUT_SQL_DIR = Path("scripts/jp_batches")
BATCH_SIZE = 100

# Maps our D1 set_id (uppercase) → the PC URL set-token (lowercase, no '-p').
# Derived from inspecting PC URL conventions for each JA promo era.
SET_ID_TO_PC_TOKEN = {
    "SVP": "sv",
    "SMP": "sm",
    "XYP": "xy",
    "BWP": "bw",
    "SWSHP": "swsh",     # SWSH era — also seen as 's' for some cards (we tolerate both below)
    "MP": "m",
    "DPP": "dp",
    "DPT-P": "dpt",       # not in our D1 set_ids today, but listed for completeness
    "LP": "l",
    "PLP": "pl",
    "PCGP": "pcg",
    "PMCGP": "pmcg",
    "VS1": "vs1",         # exact match — non-suffix sets
    "TOPSUN": "topsun",
}
# Sets that can ALSO use a shorter alt token. PC isn't consistent on swsh
# vs s, so the audit tolerates both for SWSHP rows.
ALT_TOKENS = {
    "SWSHP": {"s"},
}

# Promo slug tail: `-{N}{letters}-p` (e.g. `pikachu-mega-tokyo's-98xy-p`).
SLUG_TAIL_RE = re.compile(r"(?<!\w)(\d+)([a-z]+)-p\s*$", re.IGNORECASE)

# Vintage / non-promo slug tail: `-{N}` (e.g. `nidoran-29`, `shining-charizard-6`).
# Used when SLUG_TAIL_RE doesn't match — vintage Japanese sets (Base / Jungle
# / Fossil / Rocket / Neo / e-Card / EX-ADV) use a name-then-number URL shape
# without the `-p` promo marker.
SLUG_VINTAGE_TAIL_RE = re.compile(r"-(\d+)\s*$")

# Extract the set-portion of a PC URL: `/game/{SET-SLUG}/...`
SLUG_SETSLUG_RE = re.compile(r"/game/([^/]+)/")

# PC vintage slug → our D1 set_id (from research 2026-05-21, see
# reference_pokemon_optcg_numbering memory). Used to detect cross-set
# conflations on vintage non-promo URLs where the trailing number can't
# be trusted but the set-slug is authoritative.
PC_SET_SLUG_TO_OUR_SET = {
    "pokemon-japanese-expansion-pack": "PMCG1",
    "pokemon-japanese-jungle": "PMCG2",
    "pokemon-japanese-fossil": "PMCG3",
    "pokemon-japanese-mystery-of-the-fossils": "PMCG3",
    "pokemon-japanese-team-rocket": "PMCG4",
    "pokemon-japanese-yamabuki-city-gym": "PMCG5",
    "pokemon-japanese-gold-silver-new-world": "NEO1",
    "pokemon-japanese-crossing-the-ruins": "NEO2",
    "pokemon-japanese-awakening-legends": "NEO3",
    "pokemon-japanese-darkness-and-to-light": "NEO4",
    # `pokemon-japanese-promo` is the universal P bucket — set determined
    # by the trailing era-tag, not the slug itself. Handle separately.
    # `pokemon-japanese-rocket-gang` is NOT PMCG4 (that's TR Returns,
    # PCG-era) — leave unmapped to avoid false negatives.
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true",
                    help="Write the NULL-out SQL to scripts/jp_batches/null_pc_conflated.sql")
    args = ap.parse_args()

    print("1. Querying D1 for price_source='pricecharting' JA rows...")
    rows = fetch_pc_ja_rows()
    print(f"   {len(rows)} rows in scope")

    print("\n2. Auditing each row's slug...")
    audit = []
    verdicts = Counter()
    for row in rows:
        result = audit_row(row)
        audit.append(result)
        verdicts[result["verdict"]] += 1

    print("\n3. Verdict summary:")
    for v, n in verdicts.most_common():
        print(f"   {v:15s} {n}")

    print("\n4. Per-set breakdown of conflated rows:")
    by_set = Counter()
    for r in audit:
        if r["verdict"] in ("set_mismatch", "lid_mismatch"):
            by_set[r["set_id"]] += 1
    for s, n in by_set.most_common(15):
        print(f"   {s:10s} {n}")

    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n5. Full audit written to {OUT_AUDIT}")

    # HIGH-confidence conflations (unambiguous: wrong set-tag, wrong slug
    # set-portion, or promo-bucket lid mismatch). These all NULL.
    bad_rows = [r for r in audit if r["verdict"] in (
        "set_mismatch", "lid_mismatch", "vintage_set_mismatch"
    )]
    # LOW-confidence: vintage lid mismatch where slug set MATCHES or is
    # unmapped. PC's number convention often differs from ours — defer
    # to a name-similarity pass.
    vintage_flagged = [r for r in audit if r["verdict"] == "vintage_lid_mismatch"]
    if vintage_flagged:
        print(f"\n   [defer] {len(vintage_flagged)} vintage row(s) have lid mismatches but")
        print(f"           the slug set-portion matches (or is unmapped). Need name-")
        print(f"           similarity analysis before NULLing — PC's JA-native numbering")
        print(f"           often differs from our local_id for the same card.")
    if not bad_rows:
        print("\nNo high-confidence conflated rows. Nothing to NULL.")
        return
    print(f"\n{len(bad_rows)} row(s) flagged for NULL-out (high confidence).")

    # Sample 10 conflated rows by name for human spot-check
    print("\nSample 10 conflated rows:")
    for r in bad_rows[:10]:
        print(f"   {r['card_id']:14s} set={r['set_id']:6s} lid={r['local_id']:>4s}  "
              f"PC slug tag='{r.get('slug_tag','?')}' slug_lid='{r.get('slug_lid','?')}'  "
              f"price=${r['pc_price']}")

    if args.emit_sql:
        OUT_SQL_DIR.mkdir(parents=True, exist_ok=True)
        files = write_null_batches(bad_rows)
        print(f"\nWrote {len(files)} NULL-out SQL file(s) to {OUT_SQL_DIR}/")
        for f in files:
            print(f"   {f}")
    else:
        print("\n--emit-sql not set; SQL files not written.")


def fetch_pc_ja_rows() -> list[dict]:
    cmd = WRANGLER + [
        "--remote", "--json", "--command",
        "SELECT card_id, set_id, local_id, "
        "json_extract(pricing_json, '$.pricecharting.url') AS pc_url, "
        "json_extract(pricing_json, '$.pricecharting.market') AS pc_price "
        "FROM ptcg_cards WHERE lang='ja' AND price_source='pricecharting'",
    ]
    result = run_wrangler(cmd)
    if result.returncode != 0:
        print(f"FAIL: {(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = _strip_wrangler_chrome(result.stdout)
    data = json.loads(payload)
    rows = data[0]["results"] if isinstance(data, list) else data.get("results", [])
    return rows


def audit_row(row: dict) -> dict:
    set_id = (row.get("set_id") or "").upper()
    local_id = str(row.get("local_id") or "")
    pc_url = row.get("pc_url") or ""
    pc_price = row.get("pc_price")

    out = {
        "card_id": row.get("card_id"),
        "set_id": set_id,
        "local_id": local_id,
        "pc_url": pc_url,
        "pc_price": pc_price,
    }

    our_lid = local_id.lstrip("0") or local_id

    # First try promo pattern (has `-p` suffix marking the set token).
    # Promo conflations are HIGH CONFIDENCE — the `-p` suffix means PC tags
    # the slug with the source set explicitly, so a wrong tag is unambiguously
    # a wrong card.
    m = SLUG_TAIL_RE.search(pc_url)
    if m:
        slug_lid = m.group(1).lstrip("0") or m.group(1)
        slug_tag = m.group(2).lower()
        out["slug_lid"] = slug_lid
        out["slug_tag"] = slug_tag

        expected = SET_ID_TO_PC_TOKEN.get(set_id)
        if expected:
            alt_ok = ALT_TOKENS.get(set_id, set())
            if slug_tag != expected and slug_tag not in alt_ok:
                out["verdict"] = "set_mismatch"
                out["path"] = "promo"
                return out
            if slug_lid != our_lid:
                out["verdict"] = "lid_mismatch"
                out["path"] = "promo"
                return out
            out["verdict"] = "ok"
            out["path"] = "promo"
            return out
        # else fall through to vintage check below

    # Vintage / non-promo pattern. Per the 2026-05-21 research, the URL
    # trailing number is the JA-native set position (not Pokédex, not EN
    # position) — but JA and our D1 local_id can use different conventions
    # for the same card. So the trailing-number check is LOW confidence.
    #
    # The HIGH-confidence vintage check is the slug's set-portion: if the
    # PC URL is `/game/pokemon-japanese-jungle/...` and our set_id is
    # PMCG2 (which maps to JA Jungle), they should match. If they don't,
    # it's an unambiguous conflation regardless of the number.
    setslug_match = SLUG_SETSLUG_RE.search(pc_url)
    if setslug_match:
        pc_setslug = setslug_match.group(1).lower()
        out["pc_setslug"] = pc_setslug
        expected_d1_set = PC_SET_SLUG_TO_OUR_SET.get(pc_setslug)
        if expected_d1_set and expected_d1_set != set_id:
            # PC URL points to a different JA set than our card's set_id.
            # This is unambiguous regardless of name or number.
            out["verdict"] = "vintage_set_mismatch"
            out["path"] = "vintage_setslug"
            return out

    vm = SLUG_VINTAGE_TAIL_RE.search(pc_url)
    if vm:
        slug_lid = vm.group(1).lstrip("0") or vm.group(1)
        out["slug_lid"] = slug_lid
        out["slug_tag"] = "(vintage)"
        out["path"] = "vintage"
        if slug_lid == our_lid:
            out["verdict"] = "ok"
        else:
            # Low-confidence flag: number mismatch on a slug whose set
            # MATCHES (or is unmapped). Need name check to be sure.
            out["verdict"] = "vintage_lid_mismatch"
        return out

    out["verdict"] = "unparseable"
    return out


def write_null_batches(bad_rows: list[dict]) -> list[Path]:
    """Write NULL-out UPDATE statements in batches."""
    statements = []
    for r in bad_rows:
        cid = (r["card_id"] or "").replace("'", "''")
        statements.append(
            f"UPDATE ptcg_cards SET "
            f"price_source = NULL, "
            f"pricing_json = json_remove(pricing_json, '$.pricecharting') "
            f"WHERE card_id = '{cid}' AND lang = 'ja' AND price_source = 'pricecharting';"
        )
    files = []
    for i in range(0, len(statements), BATCH_SIZE):
        chunk = statements[i:i + BATCH_SIZE]
        idx = (i // BATCH_SIZE) + 1
        path = OUT_SQL_DIR / f"null_pc_conflated_{idx:03d}.sql"
        path.write_text("\n".join(chunk) + "\n", encoding="utf-8")
        files.append(path)
    return files


def _strip_wrangler_chrome(stdout: str) -> str:
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


if __name__ == "__main__":
    main()
