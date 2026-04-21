/**
 * update-price-source.js — one-off script.
 *
 * Before this was added, import-prices-d1.js stamped every row as
 * price_source='tcgplayer' regardless of whether the match was a
 * confident dotgg+tcgplayer pairing or a last-resort positional guess.
 * This script reads the current card_prices_all.json, computes the
 * proper price_source per row, and UPDATEs D1 in place so users see
 * trustworthy confidence labels before the next weekly cron runs.
 *
 * Usage:
 *   node scripts/update-price-source.js            # --remote, writes to prod
 *   node scripts/update-price-source.js --local    # local D1
 *   node scripts/update-price-source.js --dry-run  # write SQL only
 *
 * Safe to re-run. Only touches rows whose price_source isn't 'manual'.
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

function priceSourceFromMatch(method) {
  if (method === 'dotgg-only') return 'dotgg';
  if (method && method.startsWith('positional')) return 'positional';
  return 'tcgplayer';
}

const prices = JSON.parse(readFileSync('data/card_prices_all.json', 'utf-8'));

const lines = [];
const counts = { tcgplayer: 0, dotgg: 0, positional: 0 };
for (const [cardId, entry] of Object.entries(prices)) {
  const source = priceSourceFromMatch(entry.match_method);
  counts[source]++;
  lines.push(
    `UPDATE cards SET price_source=${escSql(source)} WHERE id=${escSql(cardId)} AND (price_source IS NULL OR price_source != 'manual');`
  );
}

console.log('Source distribution:');
for (const [k, v] of Object.entries(counts)) console.log(`  ${k.padEnd(12)} ${v}`);
console.log(`Total UPDATEs: ${lines.length}`);

const BATCH_SIZE = 900;
const batches = [];
for (let i = 0; i < lines.length; i += BATCH_SIZE) {
  batches.push(lines.slice(i, i + BATCH_SIZE));
}

mkdirSync('scripts/price_source_batches', { recursive: true });
for (let i = 0; i < batches.length; i++) {
  const file = `scripts/price_source_batches/batch_${i + 1}.sql`;
  writeFileSync(file, batches[i].join('\n'), 'utf-8');
  if (DRY) {
    console.log(`  [dry] wrote ${file} (${batches[i].length} statements)`);
    continue;
  }
  console.log(`Executing batch ${i + 1}/${batches.length}...`);
  execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${file}`, TARGET_FLAG], {
    stdio: 'inherit',
    shell: true,
  });
}
console.log('Done.');
