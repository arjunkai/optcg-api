/**
 * import-pokemontcg-d1.js — merges pokemontcg-data images into ptcg_cards
 * (English rows only). First-write-wins: image_high / image_low only set
 * if currently null (COALESCE).
 *
 * Pricing was the original Phase C goal too, but the static GitHub dump
 * has zero TCGplayer prices in any of its 20k card records (only the
 * paid live API at api.pokemontcg.io carries prices). USD pricing is
 * deferred to manual overrides in Phase D and a possible eBay backfill
 * in a future phase.
 *
 * Reads:
 *   - data/pokemontcg-data/cards/en/{setId}.json (per-set arrays)
 *   - data/ptcg_set_mapping.json (TCGdex_set → pokemontcg_set)
 *
 * Writes batched UPDATE SQL to scripts/pokemontcg_batches/, executes via
 * wrangler d1 execute --remote.
 *
 * Usage:
 *   node scripts/import-pokemontcg-d1.js
 *   node scripts/import-pokemontcg-d1.js --tcgdex-set=2011bw    # one TCGdex set
 *   node scripts/import-pokemontcg-d1.js --dry-run               # SQL only
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const PKM_DIR = 'data/pokemontcg-data/cards/en';
const MAPPING_PATH = 'data/ptcg_set_mapping.json';
const BATCH_DIR = 'scripts/pokemontcg_batches';
const BATCH_SIZE = 500;
const DB_NAME = 'optcg-cards';

const args = parseArgs(process.argv.slice(2));
const tcgdexSetFilter = args['tcgdex-set'] || null;
const dryRun = args['dry-run'] === 'true';

if (!existsSync(MAPPING_PATH)) {
  console.error(`Missing ${MAPPING_PATH}. Run scripts/build-ptcg-set-mapping.js first.`);
  process.exit(1);
}
if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

const mapping = JSON.parse(readFileSync(MAPPING_PATH, 'utf-8'));

let totalUpdates = 0;

const tcgdexSetsToProcess = tcgdexSetFilter
  ? [tcgdexSetFilter]
  : Object.keys(mapping);

for (const tcgdexId of tcgdexSetsToProcess) {
  const pkmId = mapping[tcgdexId];
  if (!pkmId) {
    console.log(`[${tcgdexId}] no pokemontcg mapping, skipping`);
    continue;
  }
  const path = `${PKM_DIR}/${pkmId}.json`;
  if (!existsSync(path)) {
    console.log(`[${tcgdexId} → ${pkmId}] no cards file in submodule, skipping`);
    continue;
  }
  const cards = JSON.parse(readFileSync(path, 'utf-8'));
  console.log(`\n[${tcgdexId} → ${pkmId}] ${cards.length} cards`);

  const stmts = [];
  for (const c of cards) {
    // pokemontcg.io card.id is "{setId}-{number}". TCGdex's card_id is
    // "{tcgdexSetId}-{localId}" with localId usually unpadded but
    // sometimes prefixed (TG01, SH01). Try a few candidate forms so we
    // hit whichever the import wrote into ptcg_cards.
    const pkmLocalId = String(c.number ?? c.id?.split('-')[1] ?? '').trim();
    if (!pkmLocalId) continue;

    // Three forms because TCGdex doesn't pad consistently. `|| pkmLocalId`
    // falls back when stripping leading zeros leaves the empty string
    // (e.g. pkmLocalId = "0" → "" → keep the original).
    const candidates = new Set([
      `${tcgdexId}-${pkmLocalId}`,
      `${tcgdexId}-${pkmLocalId.replace(/^0+/, '') || pkmLocalId}`,
      `${tcgdexId}-${pkmLocalId.padStart(3, '0')}`,
    ]);

    const updates = [];
    if (c.images?.large) {
      updates.push(`image_high = COALESCE(image_high, ${escSql(c.images.large)})`);
    }
    if (c.images?.small) {
      updates.push(`image_low  = COALESCE(image_low,  ${escSql(c.images.small)})`);
    }
    if (updates.length === 0) continue;

    for (const candidate of candidates) {
      stmts.push(
        `UPDATE ptcg_cards SET ${updates.join(', ')} WHERE card_id = ${escSql(candidate)} AND lang = 'en';`,
      );
    }
  }

  if (stmts.length === 0) {
    console.log(`[${tcgdexId}] no updates`);
    continue;
  }
  totalUpdates += stmts.length;

  for (let i = 0, batch = 1; i < stmts.length; i += BATCH_SIZE, batch++) {
    const slice = stmts.slice(i, i + BATCH_SIZE);
    const file = `${BATCH_DIR}/${tcgdexId}_${String(batch).padStart(3, '0')}.sql`;
    writeFileSync(file, slice.join('\n'));
    if (dryRun) {
      console.log(`[${tcgdexId}] wrote ${file} (${slice.length} stmts) [dry-run]`);
      continue;
    }
    console.log(`[${tcgdexId}] executing ${file} (${slice.length} stmts)...`);
    // `shell: true` is required on Windows so npx.cmd resolves; args are
    // shell-interpolated as a side effect. Safe here because every
    // interpolated value (tcgdexId, batch index) is alphanumeric — keys
    // from the committed mapping JSON, never user input.
    execFileSync(npx, ['wrangler', 'd1', 'execute', DB_NAME, `--file=${file}`, '--remote'], {
      stdio: 'inherit', shell: true,
    });
  }
}

console.log(`\nDone. ${totalUpdates} update statements.`);
if (dryRun) console.log('(Dry run — no D1 writes.)');

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return Number.isFinite(val) ? String(val) : 'NULL';
  return `'${String(val).replace(/'/g, "''")}'`;
}
function parseArgs(argv) {
  const out = {};
  for (const arg of argv) {
    const m = arg.match(/^--([^=]+)(?:=(.*))?$/);
    if (m) out[m[1]] = m[2] ?? 'true';
  }
  return out;
}
