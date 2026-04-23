/**
 * import-jp-exclusives.js — seeds JP-exclusive parallel variants from
 * data/jp_exclusives.json into the cards + card_sets tables.
 *
 * Each entry names an existing base card (e.g. P-001) plus the fields that
 * differ for the JP variant (typically variant_type, finish, rarity, and a
 * note). The SQL uses INSERT ... SELECT FROM cards WHERE id = base_id so
 * the variant inherits name, colors, cost, power, counter, attributes,
 * types, effect, trigger_text from the base row — no duplication in the
 * JSON file, and any future fix to the base card's stats automatically
 * applies next time this script runs.
 *
 * The card_sets row is inherited the same way: whatever set the base card
 * lives in, the JP variant joins.
 *
 * Idempotent: re-running updates the variant-specific columns via ON
 * CONFLICT. Safe to run after every scrape.
 *
 * Rollback (one variant):
 *   wrangler d1 execute optcg-cards --remote \
 *     --command "DELETE FROM card_sets WHERE card_id='P-001_jp1'; \
 *                DELETE FROM cards WHERE id='P-001_jp1'"
 *
 * Rollback (all JP exclusives):
 *   wrangler d1 execute optcg-cards --remote \
 *     --command "DELETE FROM card_sets WHERE card_id LIKE '%_jp%'; \
 *                DELETE FROM cards WHERE id LIKE '%_jp%'"
 *
 * Usage:
 *   node scripts/import-jp-exclusives.js             # remote write
 *   node scripts/import-jp-exclusives.js --dry-run   # build SQL, don't run
 *   node scripts/import-jp-exclusives.js --local     # local D1
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
  if (typeof val === 'boolean') return val ? '1' : '0';
  return `'${String(val).replace(/'/g, "''")}'`;
}

const blob = JSON.parse(readFileSync('data/jp_exclusives.json', 'utf-8'));

const entries = [];
for (const [id, entry] of Object.entries(blob)) {
  if (id.startsWith('_')) continue; // comments
  if (!entry?.base_id) {
    console.error(`  [skip] ${id}: missing base_id`);
    continue;
  }
  entries.push({ id, ...entry });
}

console.log(`JP-exclusive entries: ${entries.length}`);
if (entries.length === 0) {
  console.log('Nothing to import.');
  process.exit(0);
}

const now = Math.floor(Date.now() / 1000);
const lines = [];
for (const e of entries) {
  // Insert (or update) the card row, inheriting name/stats from base.
  // ON CONFLICT refreshes only the variant-specific columns so the
  // next scrape's fixes to base stats don't get clobbered here.
  lines.push(
    `INSERT INTO cards (id, base_id, parallel, variant_type, finish, rarity, image_url, name, category, colors, cost, power, counter, attributes, types, effect, trigger_text) ` +
    `SELECT ${escSql(e.id)}, ${escSql(e.base_id)}, 1, ${escSql(e.variant_type ?? 'Alternate Art')}, ${escSql(e.finish ?? 'textured')}, ${escSql(e.rarity ?? 'Special')}, ${escSql(e.image_url)}, name, category, colors, cost, power, counter, attributes, types, effect, trigger_text ` +
    `FROM cards WHERE id = ${escSql(e.base_id)} ` +
    `ON CONFLICT(id) DO UPDATE SET ` +
    `base_id=excluded.base_id, parallel=excluded.parallel, variant_type=excluded.variant_type, ` +
    `finish=excluded.finish, rarity=excluded.rarity, image_url=excluded.image_url;`
  );

  // Mirror the base card's set membership.
  lines.push(
    `INSERT INTO card_sets (card_id, set_id, pack_id) ` +
    `SELECT ${escSql(e.id)}, set_id, pack_id FROM card_sets WHERE card_id = ${escSql(e.base_id)} ` +
    `ON CONFLICT(card_id, set_id) DO NOTHING;`
  );

  // Manual price seed — only runs if the JSON has `price` set.
  // Stamped price_source='manual_jp' so later sources (TCGPlayer /
  // dotgg / eBay) skip it, and so a one-line SQL query can roll them
  // back. The later price_jp_exclusives.py eBay run REPLACES this
  // only if it finds consensus; otherwise the manual floor stays.
  if (typeof e.price === 'number') {
    lines.push(
      `UPDATE cards SET price=${e.price}, price_updated_at=${now}, ` +
      `price_source='manual_jp' WHERE id=${escSql(e.id)};`
    );
  }
}

mkdirSync('scripts/jp_batches', { recursive: true });
const sqlFile = 'scripts/jp_batches/jp_exclusives.sql';
writeFileSync(sqlFile, lines.join('\n'), 'utf-8');
console.log(`SQL written to ${sqlFile} (${lines.length} statements)`);

if (DRY) {
  console.log('--dry-run: skipping D1 execution');
  process.exit(0);
}

console.log(`Executing against ${LOCAL ? 'local' : 'remote'} D1...`);
execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${sqlFile}`, TARGET_FLAG], {
  stdio: 'inherit',
  shell: true,
});
console.log('Done.');
