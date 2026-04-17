/**
 * import-prices-d1.js — reads data/card_prices_all.json, generates UPDATE
 * statements for the cards table, batches, and runs via wrangler.
 *
 * Usage:
 *   node scripts/import-prices-d1.js            # --remote, produces real writes
 *   node scripts/import-prices-d1.js --local    # target local D1 for testing
 *   node scripts/import-prices-d1.js --dry-run  # write SQL only, don't execute
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

const prices = JSON.parse(readFileSync('data/card_prices_all.json', 'utf-8'));

const lines = [];
for (const [cardId, entry] of Object.entries(prices)) {
  // Skip cards the user has manually pinned — the manual source always wins.
  lines.push(
    `UPDATE cards SET price=${escSql(entry.price)}, tcg_ids=${escSql(JSON.stringify(entry.tcg_ids))}, price_updated_at=${escSql(entry.price_updated_at)}, price_source='tcgplayer' WHERE id=${escSql(cardId)} AND (price_source IS NULL OR price_source != 'manual');`
  );
}

console.log(`Total UPDATEs: ${lines.length}`);

const BATCH_SIZE = 900;
const batches = [];
for (let i = 0; i < lines.length; i += BATCH_SIZE) {
  batches.push(lines.slice(i, i + BATCH_SIZE));
}
console.log(`Batches: ${batches.length} (of up to ${BATCH_SIZE})`);

mkdirSync('scripts/price_batches', { recursive: true });

for (let i = 0; i < batches.length; i++) {
  const file = `scripts/price_batches/batch_${i + 1}.sql`;
  writeFileSync(file, batches[i].join('\n'), 'utf-8');
  if (DRY) {
    console.log(`  [dry] wrote ${file} (${batches[i].length} statements)`);
    continue;
  }
  console.log(`Executing batch ${i + 1}/${batches.length} (${batches[i].length})...`);
  execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${file}`, TARGET_FLAG], {
    stdio: 'inherit',
    shell: true,
  });
}

console.log('Done.');
