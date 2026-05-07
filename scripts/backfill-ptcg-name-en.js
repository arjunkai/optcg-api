#!/usr/bin/env node
/**
 * One-shot backfill: write name_en onto existing JA rows in ptcg_cards
 * using data/ja_card_id_to_en_name.json (already maintained by
 * scripts/enrich_ja_card_names.py).
 *
 * Run AFTER migration 011_ptcg_name_en.sql has been applied.
 *
 * Usage:
 *   node scripts/backfill-ptcg-name-en.js --dry-run    # write SQL but don't execute
 *   node scripts/backfill-ptcg-name-en.js              # execute against remote D1
 *
 * Output: scripts/backfill_name_en/NNN.sql files (one per batch of 500
 * statements, same shape as ptcg-import-d1.js).
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const DB_NAME = 'optcg-cards';
const NAME_EN_PATH = 'data/ja_card_id_to_en_name.json';
const BATCH_DIR = 'scripts/backfill_name_en';
const BATCH_SIZE = 500;

const dryRun = process.argv.includes('--dry-run');

if (!existsSync(NAME_EN_PATH)) {
  console.error(`Missing ${NAME_EN_PATH}. Run scripts/enrich_ja_card_names.py first.`);
  process.exit(1);
}

const map = JSON.parse(readFileSync(NAME_EN_PATH, 'utf-8'));
const entries = Object.entries(map).filter(([, name]) => name && name.trim());
console.log(`Loaded ${entries.length} JA card_id → EN name pairs from ${NAME_EN_PATH}`);

if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

const stmts = entries.map(([cardId, enName]) =>
  // Keep existing name_en if a curator has already set one. Idempotent:
  // re-runs after the column is populated are a no-op.
  `UPDATE ptcg_cards SET name_en = COALESCE(name_en, ${escSql(enName)}) WHERE card_id = ${escSql(cardId)} AND lang = 'ja';`
);

const batchCount = Math.ceil(stmts.length / BATCH_SIZE);
console.log(`Splitting ${stmts.length} UPDATE statements across ${batchCount} batch file(s)...`);

for (let i = 0; i < batchCount; i++) {
  const slice = stmts.slice(i * BATCH_SIZE, (i + 1) * BATCH_SIZE);
  const file = `${BATCH_DIR}/${String(i + 1).padStart(3, '0')}.sql`;
  writeFileSync(file, slice.join('\n') + '\n', 'utf-8');
  if (dryRun) {
    console.log(`[dry-run] wrote ${file}`);
    continue;
  }
  console.log(`Executing ${file} (${slice.length} statements)...`);
  execFileSync(npx, ['wrangler', 'd1', 'execute', DB_NAME, `--file=${file}`, '--remote'], {
    stdio: 'inherit',
    shell: true,
  });
}

console.log('Done.');
if (dryRun) console.log('(Dry run — no D1 writes.)');

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return String(val);
  return `'${String(val).replace(/'/g, "''")}'`;
}
