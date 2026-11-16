"""Fail the weekly scrape when a set is missing TCGPlayer prices it should have.

Why this exists: from mid-2026 the TCGPlayer scrape silently skipped every
new set (OP-16, OP-17, EB-05, ST-30..36 had no slug) and timed out on every
starter deck (TCGPlayer renamed the pages). Those cards fell through to dotgg
and eBay for months, e.g. ST31-004_p1 served $18.37 (eBay) against $423.30
on TCGPlayer, and nothing flagged it because the run still went green.

Read-only. Runs as the last step of .github/workflows/scrape.yml, after the
importers, and exits 1 on any critical finding so the run goes red and GitHub
sends its failure email. Checks, per set in data/sets.json:

  1. no-group   Our set has no TCGPlayer group in the SetNames catalog, so the
                abbreviation mapping in scrape_tcgplayer_prices_pw.py needs a
                look (new id shape, new catch-all).
  2. missed     The scrape skipped or failed the set, TCGPlayer released it
                at least GRACE_DAYS ago, and its price guide has singles.
                A set TCGPlayer has no singles for (One Piece Collection
                Sets, since 2026) is reported but never critical.
  3. low-share  Released at least SHARE_GRACE_DAYS ago on TCGPlayer, but
                under MIN_SHARE of its cards carry price_source='tcgplayer'.
                Catches a mapping break after a scrape that "worked".

Usage:
  python -m scripts.check_tcgplayer_coverage           # remote D1
  python -m scripts.check_tcgplayer_coverage --no-fail # report only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

DATA_DIR = Path("data")
REPORT = DATA_DIR / "tcgplayer_raw" / "_scrape_report.json"
SET_NAMES_URL = "https://mpapi.tcgplayer.com/v2/Catalog/SetNames?categoryId=68&active=true"
SINGLES_URL = "https://infinite-api.tcgplayer.com/priceguide/set/{gid}/cards/?rows=5000&productTypeID=110"
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
SHARE_SQL = (
    "SELECT cs.set_id, COUNT(*) AS n, SUM(c.price_source = 'tcgplayer') AS tcg "
    "FROM cards c JOIN card_sets cs ON cs.card_id = c.id GROUP BY cs.set_id"
)

GRACE_DAYS = 7         # TCGPlayer posts a guide around release; give it a week
SHARE_GRACE_DAYS = 14  # and a fortnight before judging how much of it mapped
MIN_SHARE = 0.5
MIN_CARDS = 5

# Our ids for TCGPlayer's catch-all groups (keep in step with the scraper).
CATCHALL_ABBR = {"569901": "OPPR", "569801": "OPCS"}


def abbr_key(s: str) -> str:
    return s.replace("-", "").upper()


def our_key(set_id: str) -> str:
    return CATCHALL_ABBR.get(set_id, abbr_key(set_id))


# ── Pure core ────────────────────────────────────────────────────────────────

def evaluate(set_ids, report, catalog, shares, singles, today):
    """Return a list of findings: {set_id, check, critical, detail}.

    catalog: abbr_key -> {"group_id": int, "released": date}
    report:  set_id -> {"status": ok|failed|skipped, "msg": str}
    shares:  set_id -> {"n": int, "tcg": int}
    singles: callable(group_id) -> int, called only when a verdict needs it
    """
    out = []

    def add(sid, check, critical, detail):
        out.append({"set_id": sid, "check": check, "critical": critical, "detail": detail})

    for sid in set_ids:
        grp = catalog.get(our_key(sid))
        if not grp:
            add(sid, "no-group", True, "no TCGPlayer group matches this set id")
            continue
        age = (today - grp["released"]).days
        r = report.get(sid, {"status": "missing", "msg": "not in scrape report"})

        if r["status"] != "ok":
            if age < GRACE_DAYS:
                add(sid, "missed", False, f"{r['status']}, but TCGPlayer release is {age}d old")
            elif singles(grp["group_id"]) == 0:
                add(sid, "missed", False, f"{r['status']}; TCGPlayer lists no singles for it")
            else:
                add(sid, "missed", True, f"scrape {r['status']}: {r['msg'][:160]}")
            continue

        s = shares.get(sid)
        if not s or s["n"] < MIN_CARDS or age < SHARE_GRACE_DAYS:
            continue
        share = (s["tcg"] or 0) / s["n"]
        if share < MIN_SHARE and singles(grp["group_id"]) > 0:
            add(sid, "low-share", True,
                f"only {s['tcg'] or 0}/{s['n']} cards ({share:.0%}) priced from TCGPlayer")
    return out


# ── Impure shell ─────────────────────────────────────────────────────────────

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"GET {url} failed: {last}")


def load_catalog():
    data = _get_json(SET_NAMES_URL)
    rows = data.get("results", data) if isinstance(data, dict) else data
    out = {}
    for x in rows:
        if x.get("abbreviation") and x.get("releaseDate"):
            out[abbr_key(x["abbreviation"])] = {
                "group_id": x["setNameId"],
                "released": dt.date.fromisoformat(x["releaseDate"][:10]),
            }
    return out


def make_singles_counter():
    cache = {}

    def singles(gid):
        if gid not in cache:
            d = _get_json(SINGLES_URL.format(gid=gid))
            cache[gid] = len(d.get("result") or []) if isinstance(d, dict) else 0
        return cache[gid]
    return singles


def load_shares():
    from scripts.wrangler_retry import run_wrangler
    res = run_wrangler(WRANGLER + ["--remote", "--json", "--command", SHARE_SQL])
    if res.returncode != 0:
        raise RuntimeError(f"D1 query failed: {(res.stderr or '')[:300]}")
    text = res.stdout[min(i for i in (res.stdout.find("["), res.stdout.find("{")) if i >= 0):]
    data = json.loads(text)
    rows = data[0]["results"] if isinstance(data, list) else data["results"]
    return {r["set_id"]: {"n": r["n"], "tcg": r["tcg"]} for r in rows}


def render(findings):
    crit = [f for f in findings if f["critical"]]
    lines = [f"### TCGPlayer coverage: {'🔴 ' + str(len(crit)) + ' set(s) missing prices' if crit else '✅ every set covered'}", ""]
    if findings:
        lines += ["| Set | Check | Critical | Detail |", "|---|---|---|---|"]
        lines += [f"| {f['set_id']} | {f['check']} | {'yes' if f['critical'] else 'no'} | {f['detail']} |"
                  for f in sorted(findings, key=lambda f: (not f["critical"], f["set_id"]))]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-fail", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    set_ids = [s["set_id"] for s in json.loads((DATA_DIR / "sets.json").read_text(encoding="utf-8"))]
    report = json.loads(REPORT.read_text(encoding="utf-8"))["sets"] if REPORT.exists() else {}
    if not REPORT.exists():
        print(f"WARNING: {REPORT} missing; every set will read as not scraped.")

    findings = evaluate(set_ids, report, load_catalog(), load_shares(),
                        make_singles_counter(), dt.date.today())
    md = render(findings)
    print(md)
    for f in findings:
        if f["critical"]:
            print(f"::error title=TCGPlayer coverage ({f['check']})::{f['set_id']}: {f['detail']}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(md + "\n")

    if any(f["critical"] for f in findings) and not args.no_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
