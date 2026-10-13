"""
Catch up a Japanese promo set (*-P) in ptcg_cards by inserting placeholder
rows for any LID present on the Bulbapedia master setlist page but missing
from D1. Originally M-P only (MEGA Evolution era); parameterized 2026-05-19
for SV-P (and any future *-P era set) so the same INSERT path covers all
promo-set catch-ups without code duplication.

This script INSERTs placeholder rows for any (set_id=<SET>, local_id=N)
that exists on Bulbapedia but not in D1. Placeholders carry:

  card_id = "<SET>-{N}"  (matches existing convention, unpadded)
  lang    = "ja"
  set_id  = "<SET>"
  local_id = str(N)
  name    = <English name from Bulbapedia>   (placeholder, see note)
  name_en = <English name from Bulbapedia>

NULL for everything else — TCGdex's eventual import is UPSERT-shaped
(ON CONFLICT(card_id,lang) DO UPDATE SET ...) so when cards land
upstream the placeholder gets overwritten with the proper JA name +
types_csv + hp + image_high. INSERT OR IGNORE here makes that future
overwrite a no-op for our placeholders that have already been
upgraded.

Why the English name in the JA `name` column: `name` is NOT NULL and
Bulbapedia doesn't ship a free machine-readable JA name on the master
page (the JA form lives only in the per-card page wikitext, 100x extra
API calls). Placeholder-with-EN is the pragmatic floor — better than
"Unknown" because OPBindr's search still hits, and TCGdex's UPSERT
corrects it within a refresh cycle.

The category contamination guard: Bulbapedia's broader Category:<SET>
Promotional cards is contaminated with main-set canonical-page reprints
and cross-set false positives (e.g. M-P category has 57 members but
only 5 are real M-P pages). We sidestep contamination entirely by
using the master setlist page, which is set-clean by construction
(every {{Setlist/entry|NNN/<SET-token>|...}} is a row of that set).

Bulbapedia token convention: M-P, SV-P, SM-P, etc. We derive the
Bulbapedia token from the D1 set_id by inserting a hyphen before the
trailing P: MP → M-P, SVP → SV-P, SMP → SM-P. Override via
--bulba-token if a future set breaks this convention.

Output: scripts/insert_promo_rows/<set>_catchup_<NNN>.sql

Usage:
    python -m scripts.backfill_mp_catalog --set MP --dry-run
    python -m scripts.backfill_mp_catalog --set MP --apply
    python -m scripts.backfill_mp_catalog --set SVP --apply
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
from pathlib import Path

from scripts.bulbapedia import BULBAPEDIA_API, USER_AGENT, describe_http_error
from scripts.wrangler_retry import WRANGLER_MAX_ATTEMPTS, run_wrangler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path("scripts/insert_promo_rows")
BATCH_SIZE = 100  # rows per multi-statement file
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
RATE_LIMIT_SECONDS = 1.1
TARGET_LANG = "ja"


def _default_bulba_token(set_id: str) -> str:
    """Convert a D1 promo set_id like 'MP'/'SVP'/'SMP' to Bulbapedia's
    hyphenated form 'M-P'/'SV-P'/'SM-P'. The convention is: insert a
    hyphen before the trailing P. Caller can override via --bulba-token
    if a set breaks this rule."""
    if not set_id.endswith("P"):
        raise ValueError(
            f"--set {set_id!r} doesn't end in 'P'; this script is for "
            f"*-P promo sets only. Pass --bulba-token to override."
        )
    if len(set_id) < 2:
        raise ValueError(f"--set {set_id!r} too short")
    return set_id[:-1] + "-P"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="set_id", default="MP",
                    help="D1 set_id to catch up (MP, SVP, SMP, etc.). "
                         "Default: MP for backwards compatibility.")
    ap.add_argument("--bulba-token", default=None,
                    help="Override the derived Bulbapedia token "
                         "(default: MP→M-P, SVP→SV-P). Only needed if "
                         "the set breaks the hyphen-before-P convention.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Fetch + parse + diff + write SQL. Don't touch D1.")
    g.add_argument("--apply", action="store_true",
                   help="Fetch + parse + diff + write SQL AND run wrangler.")
    args = ap.parse_args()

    set_id = args.set_id.upper()
    bulba_token = args.bulba_token or _default_bulba_token(set_id)
    master_page = f"{bulba_token} Promotional cards (TCG)"
    setlist_re = re.compile(
        rf"^\{{\{{Setlist/(?:entry|nmentry)\|(\d+)/{re.escape(bulba_token)}\|"
    )
    wikilink_name_re = re.compile(
        rf"\[\[([^|\]]+?)\s*\({re.escape(bulba_token)}\s+Promo"
    )
    slug = set_id.lower()

    print(f"Set: {set_id!r}  Bulbapedia token: {bulba_token!r}  Page: {master_page!r}")

    print(f"1. Fetching Bulbapedia master page {master_page!r}...")
    wt = _fetch_page_wikitext(master_page)
    print(f"   wikitext length: {len(wt)} chars")

    print("2. Parsing setlist entries...")
    parsed = _parse_setlist(wt, setlist_re, wikilink_name_re)
    print(f"   parsed {len(parsed)} {bulba_token} entries "
          f"(LID range {min(p[0] for p in parsed) if parsed else '-'}.."
          f"{max(p[0] for p in parsed) if parsed else '-'})")
    if not parsed:
        print("Nothing parsed — abort.")
        sys.exit(1)

    print(f"3. Querying D1 for existing {set_id}/{TARGET_LANG} LIDs...")
    existing = _fetch_existing_lids(set_id)
    print(f"   D1 has {len(existing)} {set_id}/{TARGET_LANG} rows already")

    missing = [(lid, name) for lid, name in parsed if lid not in existing]
    print(f"   {len(missing)} new row(s) to INSERT")
    if not missing:
        print("Caught up. Nothing to do.")
        return

    print("4. Writing INSERT OR IGNORE batches...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = _write_batches(missing, set_id, slug)
    print(f"   wrote {len(files)} batch file(s) to {OUT_DIR}/")

    if args.dry_run:
        print("\nDry run done. Inspect scripts/insert_promo_rows/*.sql, "
              "then re-run with --apply.")
        return

    print("5. Applying batches against remote D1...")
    for i, f in enumerate(files, 1):
        print(f"   [{i}/{len(files)}] executing {f.name}...")
        result = run_wrangler(WRANGLER + [f"--file={f}", "--remote"])
        if result.returncode != 0:
            print(f"   FAIL after {WRANGLER_MAX_ATTEMPTS} attempts: "
                  f"{(result.stderr or '')[:400]}")
            sys.exit(1)
    print("Done.")


def _fetch_page_wikitext(page: str) -> str:
    data = _api_get({
        "action": "parse",
        "page": page,
        "prop": "wikitext",
        "format": "json",
    })
    return data.get("parse", {}).get("wikitext", {}).get("*", "")


def _parse_setlist(
    wikitext: str,
    setlist_re: re.Pattern,
    wikilink_name_re: re.Pattern,
) -> list[tuple[int, str]]:
    """Pull (local_id, English card name) pairs out of every
    {{Setlist/entry|NNN/<SET-token>|...}} line. Skip lines that don't
    carry a recognizable card name template.
    """
    out: list[tuple[int, str]] = []
    skipped_no_name = 0
    for raw_line in wikitext.split("\n"):
        line = raw_line.strip()
        m = setlist_re.match(line)
        if not m:
            continue
        try:
            lid = int(m.group(1))
        except ValueError:
            continue
        name = _extract_name(line, wikilink_name_re)
        if not name:
            skipped_no_name += 1
            continue
        out.append((lid, name))
    if skipped_no_name:
        print(f"     warn: {skipped_no_name} setlist row(s) had no "
              f"parseable card name — skipped")
    return out


# Card name from {{TCG ID|<setname>|<cardname>|<num>...}}.
# Group 1 captures the cardname (3rd template arg). This pattern is
# set-agnostic, so we pull it out as a module-level constant.
_TCG_ID_RE = re.compile(r"\{\{TCG ID\|[^|]+\|([^|}]+)")


def _extract_name(setlist_line: str, wikilink_name_re: re.Pattern) -> str:
    m = _TCG_ID_RE.search(setlist_line)
    if m:
        return m.group(1).strip()
    m = wikilink_name_re.search(setlist_line)
    if m:
        return m.group(1).strip()
    return ""


def _fetch_existing_lids(set_id: str) -> set[int]:
    """Return the set of LIDs (as ints) we already have for <set_id>/<lang>."""
    cmd = WRANGLER + [
        "--remote",
        "--json",
        "--command",
        f"SELECT CAST(local_id AS INTEGER) AS lid FROM ptcg_cards "
        f"WHERE UPPER(set_id) = '{set_id}' AND lang = '{TARGET_LANG}'",
    ]
    result = run_wrangler(cmd)
    if result.returncode != 0:
        print(f"   FAIL fetching existing LIDs after "
              f"{WRANGLER_MAX_ATTEMPTS} attempts: "
              f"{(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = _strip_wrangler_chrome(result.stdout)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        print(f"   FAIL parsing wrangler JSON: {e}\n--- payload (head) ---\n"
              f"{payload[:400]}")
        sys.exit(1)
    rows = data[0]["results"] if isinstance(data, list) else data.get("results", [])
    return {int(r["lid"]) for r in rows if r.get("lid") is not None}


def _strip_wrangler_chrome(stdout: str) -> str:
    """Wrangler prints a config-warning banner before the JSON. Find the
    first '[' or '{' and slice from there."""
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


def _write_batches(
    rows: list[tuple[int, str]],
    set_id: str,
    slug: str,
) -> list[Path]:
    files: list[Path] = []
    cols = ("card_id", "lang", "set_id", "local_id", "name", "name_en")
    values: list[str] = []
    for lid, name in rows:
        card_id = f"{set_id}-{lid}"
        local_id_s = str(lid)
        values.append(
            "(" + ", ".join([
                _esc(card_id),
                _esc(TARGET_LANG),
                _esc(set_id),
                _esc(local_id_s),
                _esc(name),
                _esc(name),
            ]) + ")"
        )
    for i in range(0, len(values), BATCH_SIZE):
        chunk = values[i:i + BATCH_SIZE]
        idx = (i // BATCH_SIZE) + 1
        path = OUT_DIR / f"{slug}_catchup_{idx:03d}.sql"
        path.write_text(
            f"INSERT OR IGNORE INTO ptcg_cards "
            f"({', '.join(cols)}) VALUES\n"
            + ",\n".join(chunk) + ";\n",
            encoding="utf-8",
        )
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
        print(f"   {describe_http_error(e, 'backfill_mp_catalog')}")
        sys.exit(1)
    finally:
        time.sleep(RATE_LIMIT_SECONDS)


if __name__ == "__main__":
    main()
