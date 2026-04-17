/**
 * import-manual-prices.js — applies pinned price overrides from
 * data/manual_prices.json. Runs LAST in the refresh pipeline so manual values
 * take precedence over TCGPlayer and dotgg.
 *
 * Entries with `price: null` are skipped (placeholders waiting for a real
 * number). Every applied row is stamped `price_source='manual'`. The
 * TCGPlayer importer's WHERE clause skips manual rows on future runs, so once
 * you pin a price here it stays pinned until you remove the entry from JSON
 * and run the rollback SQL below.
 *
 * Rollback one card:
 *   wrangler d1 execute optcg-cards --remote \
 *     --command "UPDATE cards SET price=NULL, price_source=NULL, price_updated_at=NULL WHERE id='P-053' AND price_source='manual'"
 *
 * Rollback ALL manual:
 *   wrangler d1 execute optcg-cards --remote \
 *     --command "UPDATE cards SET price=NULL, price_source=NULL, price_updated_at=NULL WHERE price_source='manual'"
 *
 * Usage:
 *   node scripts/import-manual-prices.js             # real remote write
 *   node scripts/import-manual-prices.js --dry-run   # SQL only
 *   node scripts/import-manual-prices.js --local     # local D1
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const LOCAL = process.argv.includes('--local');
const DRY = process.argv.includes('--dry-run');
const TARGET_FLAG = LOCAL ? '--local' : '--remote';

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return String(val);
  return `'${String(val).replace(/'/g, "''")}'`;
}

const blob = JSON.parse(readFileSync('data/manual_prices.json', 'utf-8'));
const now = Math.floor(Date.now() / 1000);

const applied = [];
const skipped = [];
for (const [cardId, entry] of Object.entries(blob)) {
  if (cardId.startsWith('_')) continue; // comments
  if (typeof entry?.price !== 'number') {
    skipped.push({ cardId, reason: 'no price' });
    continue;
  }
  applied.push({ cardId, price: entry.price, note: entry.note });
}

console.log(`Manual entries: ${applied.length + skipped.length}`);
console.log(`  to apply: ${applied.length}`);
console.log(`  skipped (null price):  ${skipped.length}`);

if (applied.length === 0) {
  console.log('\nNothing to apply. Fill in prices in data/manual_prices.json first.');
  process.exit(0);
}

const lines = applied.map(({ cardId, price }) =>
  `UPDATE cards SET price=${price}, price_updated_at=${now}, price_source='manual' WHERE id=${escSql(cardId)};`
);

mkdirSync('scripts/manual_batches', { recursive: true });
const sqlFile = 'scripts/manual_batches/manual_prices.sql';
writeFileSync(sqlFile, lines.join('\n'), 'utf-8');
console.log(`\nSQL written to ${sqlFile}`);

if (DRY) {
  console.log('--dry-run: skipping D1 execution');
  process.exit(0);
}

console.log(`\nExecuting ${lines.length} UPDATEs against ${LOCAL ? 'local' : 'remote'} D1...`);
execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${sqlFile}`, TARGET_FLAG], {
  stdio: 'inherit',
  shell: true,
});
console.log('Done.');
