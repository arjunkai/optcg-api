# Session prompt: OPTCG pricing and stamped prints, next steps

Paste everything below into a new Claude Code session opened in
`c:\Users\arjun\code\projects\optcg-api`.

---

You're continuing work on the OPTCG API (Cloudflare Worker + D1, see
CLAUDE.md) and its main client OPBindr (`c:\Users\arjun\code\projects\opbindr`,
default branch `master`). Read CLAUDE.md first. Its rules apply: numbered
migrations only, the D1 free-tier budget, and agents never run `npm run key:*`.
Ask before anything that writes to production D1, triggers a workflow or
pushes to a default branch.

## What happened on 2026-11-16

A client (alpha430 / Nexus Card Co., key `opt_cTy3EBGD`, standard tier)
reported wrong prices. That surfaced several pipeline problems, all fixed and
merged:

- **#18** TCGPlayer price-guide slugs now come from TCGPlayer's
  `Catalog/SetNames`. New sets (OP-16+, ST-30+, EB-05) were being skipped and
  every starter deck was failing.
- **#19** `check_tcgplayer_coverage.py` fails the weekly run if a released set
  misses TCGPlayer prices. Also: retries for the dotgg and pkmnbindr steps,
  and DON `price_source`.
- **#20** Removed the no-op zone cache purge. Edge copies expire within 1h;
  an admin `?refresh=1` refreshes one colo.
- **#21** `correct_shared_tcg_ids` moves a card off a sibling's TCGPlayer
  product (OP-17 Newgate/Linlin mangas).
- **#22** `match_promo_labels`: the official card-set label picks the product
  (EB01-014_p1 Sanji → CS 25-26 Celebration Pack).
- **#23**
  - tcgcsv (`fetch_tcgcsv_optcg.py`) fills products the scrape doesn't reach.
  - **Picture check** (`image_match.py`, `verify_by_image`): official image vs
    TCGPlayer photo, right print ≈1.0, wrong stamp ≈0.3–0.5. It re-matched 178
    cards in CI. JA-site rows are skipped.
  - Variant overrides now reach the price build.
  - A "Promo & variant pricing" section in the run summary.
- **#25** Migration 022 (applied to prod): `cards.print_label` + `cards.stamp`,
  served on card routes, `?stamp=` filter, snapshots `cards-*-v2`.
- **#26** `print_label` also names premium treatments: **Super Leader Parallel**
  ("Super Leader Alternate Art", new in OP-17: OP17-079_p2 Luffy, EB05-010_p2
  Robin), Heroine Leader (EB-05), Super / Red Super Alternate Art, Manga, SP.
- **opbindr #22** Added OP-16/17/18, EB-05, ST-22..36 and fixed starter
  names. `appendUnlistedSets` shows any set with cards but no entry under
  "New sets" (Add Cards + Explore).

## First: check the last scrape

A scrape was triggered after #26 merged. Confirm on the live API, with
`X-API-Key: $OPTCG_API_KEY` and `?refresh=1`:

- `OP17-079_p2` and `EB05-010_p2` have `print_label` "Super Leader Alternate Art".
- `EB01-014_p1` has `print_label` "CS 25-26 Celebration Pack" and `stamp` `championship`.
- `/cards?stamp=winner` returns cards.

If tags are missing, read the run's "Build price catalog" log.

## Open work, in suggested order

1. **Phase B, OPBindr shows the tags** (`docs/stamped-prints-plan.md` §5).
   - Add a `print_label` chip in `CardEnlargeModal.jsx` (chip row ~531–560),
     and a short pill on grid tiles next to the "✦ Alt Art" pill
     (`AddCardsModal.jsx` ~162, `CardSlot.jsx` ~640).
   - Add a stamp filter row, and add `print_label` to `buildSearchKey`
     (`useCardCache.jsx` ~305).
   - Add stamp words to the `ebayLink.js` keywords.
   - Bump `DB_VERSION` and the SW cache name `optcg-api-v10`.
   - `normalizeCard` already passes the new fields through.
   - Note: `src/constants/sets.js` placeholder `cardCount`s (121/51) on older
     sets are still wrong (they feed "Add all N" and profile completion).
2. **Phase C, rows for stamped prints we don't list** (plan §2–3).
   - Run-summary data: ~426 prints, $54k; 121 at ≥ $50.
   - IDs `{base}_t{tcgplayer product id}` (passes OPBindr's publish regex),
     added only from a reviewed `data/stamped_prints.json`.
   - New `import-stamped-prints.js`, modeled on `import-jp-exclusives.js`.
   - `/images/:id` falls back to the TCGPlayer CDN for `_t` IDs.
   - Direct price mapping for those rows.
   - Duplicate pre-check against every existing row with `image_match.py`.
   - IDs are permanent in users' binders, so **Arjun reviews the first batch
     (Phase D) by eye before import.** Decisions taken: start with the ≥ $50
     prints, file them under set `569901`, and send API key holders a heads-up
     when they ship.
3. **Picture-check review list:** each run lists ~100 cards whose linked
   product photo doesn't match (e.g. OP09-069_p2, similarity 0.49 vs 0.62).
   Triage these. Some may need a manual mapping or a lower-but-safe threshold
   for specific cases.
4. **alpha430's usage:** on day one, 50 requests used 62k of their 200k daily
   D1 units (heavy uncached `/cards` searches). If they hit the cap, point
   them to `/sets/{id}/cards` or `/cards/index` (0 units), or discuss a tier
   change.

## Useful facts

- PR #24 (`docs/stamped-prints-plan.md`) may still be open. It's the plan for
  Phases B–E.
- Weekly scrape: Mondays (`scrape.yml`), PTCG Tuesdays. The last PTCG refresh
  failed on 2026-11-10 (transient), and #19 added a retry.
- `data/price_mapping_report.json` (written by `build_all_prices.py`) has the
  re-matches, the unresolved list and the unrepresented stamped prints.
- `docs/variant-taxonomy.md` has the background on print variants.
- Memory: `optcg-new-price-source-todo.md` (pricing roadmap, tcgcsv findings).
