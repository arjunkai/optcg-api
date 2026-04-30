/**
 * import-tcgpocket-d1.js — fills image_high / image_low in ptcg_cards
 * for the 15 TCG Pocket sets that pokemontcg-data and the live API
 * don't cover (Pocket is a separate game). Source data:
 * `data/pokemon-tcg-pocket-database/dist/cards/{flibustierSet}.json`
 * (flibustier/pokemon-tcg-pocket-database submodule). Images live on
 * the `flibustier/pokemon-tcg-exchange` repo, served via JSDelivr CDN
 * with predictable filenames `cards-by-set/{set}/{number}.webp`.
 *
 * COALESCE-only — never overwrite an existing image. TCG Pocket has no
 * secondary-market pricing so we only touch images.
 *
 * Reads:
 *   - data/pokemon-tcg-pocket-database/dist/cards/{set}.json
 *   - data/ptcg_pocket_set_mapping.json   (TCGdex_set → flibustier_set)
 *
 * Writes batched UPDATEs to scripts/pokemontcg_batches/, executes via
 * `wrangler d1 execute --remote`.
 *
 * Usage:
 *   node scripts/import-tcgpocket-d1.js
 *   node scripts/import-tcgpocket-d1.js --tcgdex-set=B2a
 *   node scripts/import-tcgpocket-d1.js --dry-run
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const POCKET_CARDS_DIR = 'data/pokemon-tcg-pocket-database/dist/cards';
const MAPPING_PATH = 'data/ptcg_pocket_set_mapping.json';
const BATCH_DIR = 'scripts/pokemontcg_batches';
const BATCH_SIZE = 500;
const DB_NAME = 'optcg-cards';
// JSDelivr CDN serves flibustier/pokemon-tcg-exchange's predictable-filename
// image directory. Using @main means weekly submodule bumps automatically
// pull new artwork — JSDelivr edge-caches each version.
const IMAGE_BASE = 'https://cdn.jsdelivr.net/gh/flibustier/pokemon-tcg-exchange@main/public/images/cards-by-set';

const args = parseArgs(process.argv.slice(2));
const tcgdexSetFilter = args['tcgdex-set'] || null;
const dryRun = args['dry-run'] === 'true';

if (!existsSync(MAPPING_PATH)) {
  console.error(`Missing ${MAPPING_PATH}.`);
  process.exit(1);
}
if (!existsSync(POCKET_CARDS_DIR)) {
  console.error(`Missing ${POCKET_CARDS_DIR}. Run \`git submodule update --init data/pokemon-tcg-pocket-database\`.`);
  process.exit(1);
}
if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

const mapping = JSON.parse(readFileSync(MAPPING_PATH, 'utf-8'));
delete mapping._doc;

let totalUpdates = 0;
const tcgdexSets = tcgdexSetFilter ? [tcgdexSetFilter] : Object.keys(mapping);

for (const tcgdexId of tcgdexSets) {
  const flibSet = mapping[tcgdexId];
  if (!flibSet) {
    console.log(`[${tcgdexId}] no flibustier mapping, skipping`);
    continue;
  }
  const path = `${POCKET_CARDS_DIR}/${flibSet}.json`;
  if (!existsSync(path)) {
    console.log(`[${tcgdexId} → ${flibSet}] no cards file in submodule, skipping`);
    continue;
  }
  const cards = JSON.parse(readFileSync(path, 'utf-8'));
  console.log(`\n[${tcgdexId} → ${flibSet}] ${cards.length} cards`);

  const stmts = [];
  for (const c of cards) {
    const number = c.number;
    if (typeof number !== 'number') continue;
    const imageUrl = `${IMAGE_BASE}/${flibSet}/${number}.webp`;
    // TCGdex's card_id format is `{tcgdexId}-{localId}`. Flibustier's
    // number is unpadded numeric. Try the candidate forms TCGdex uses
    // in practice — same multi-candidate pattern as import-pokemontcg-d1.
    const candidates = new Set([
      `${tcgdexId}-${number}`,
      `${tcgdexId}-${String(number).padStart(3, '0')}`,
    ]);

    for (const candidate of candidates) {
      stmts.push(
        `UPDATE ptcg_cards SET image_high = COALESCE(image_high, ${escSql(imageUrl)}), image_low = COALESCE(image_low, ${escSql(imageUrl)}) WHERE card_id = ${escSql(candidate)} AND lang = 'en';`,
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
    const file = `${BATCH_DIR}/pocket_${tcgdexId.replace(/[^a-zA-Z0-9]/g, '_')}_${String(batch).padStart(3, '0')}.sql`;
    writeFileSync(file, slice.join('\n'));
    if (dryRun) {
      console.log(`[${tcgdexId}] wrote ${file} (${slice.length} stmts) [dry-run]`);
      continue;
    }
    console.log(`[${tcgdexId}] executing ${file} (${slice.length} stmts)...`);
    // shell:true required on Windows for npx.cmd; interpolated args
    // are alphanumeric set ids from a committed mapping file.
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
