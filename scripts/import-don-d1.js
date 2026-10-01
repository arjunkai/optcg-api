/**
 * import-don-d1.js — reads data/don_cards.json and inserts DON cards into D1.
 *
 * Inserts into both `cards` (category='Don') and `card_sets` (so they show
 * up in /sets/:set_id/cards endpoints).
 *
 * Idempotent: ON CONFLICT DO UPDATE refreshes name, image, and price on re-run.
 *
 * Usage:
 *   node scripts/import-don-d1.js             # real remote write
 *   node scripts/import-don-d1.js --dry-run   # SQL only
 *   node scripts/import-don-d1.js --local     # local D1
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

const cards = JSON.parse(readFileSync('data/don_cards.json', 'utf-8'));

const lines = [];
// Skip unchanged DONs: a no-op upsert still counts as D1 rows written (plus
// the price/price_updated_at indexes). price_updated_at = "price last changed".
for (const c of cards) {
  lines.push(
    `INSERT INTO cards (id, parallel, name, rarity, category, image_url, price, tcg_ids, price_updated_at) VALUES (${escSql(c.id)}, 0, ${escSql(c.name)}, ${escSql(c.rarity)}, ${escSql(c.category)}, ${escSql(c.image_url)}, ${escSql(c.price)}, ${escSql(JSON.stringify(c.tcg_ids))}, ${escSql(c.price_updated_at)}) ON CONFLICT(id) DO UPDATE SET name=excluded.name, image_url=excluded.image_url, price=excluded.price, tcg_ids=excluded.tcg_ids, price_updated_at=excluded.price_updated_at WHERE name IS NOT excluded.name OR image_url IS NOT excluded.image_url OR price IS NOT excluded.price OR tcg_ids IS NOT excluded.tcg_ids;`
  );
}

for (const c of cards) {
  lines.push(
    `INSERT INTO card_sets (card_id, set_id) VALUES (${escSql(c.id)}, ${escSql(c.set_id)}) ON CONFLICT(card_id, set_id) DO NOTHING;`
  );
}

// Snapshot a price-history point so DON cards accrue weekly history like
// regular cards. DON go through this separate importer (not
// import-prices-d1.js, which writes the same row for the TCGPlayer path),
// so without this DON history froze at the one-time backfill and the
// PriceHistoryChart never rendered for DON. INSERT OR IGNORE dedupes within
// a snapshot (matches the regular importer's pattern + unique card_id+captured_at).
// Like import-prices-d1.js, a point is added only when the price changed.
for (const c of cards) {
  if (c.price === null || c.price === undefined) continue;
  lines.push(
    `INSERT OR IGNORE INTO card_price_history (card_id, price, captured_at) SELECT ${escSql(c.id)}, ${escSql(c.price)}, ${escSql(c.price_updated_at)} WHERE ${escSql(c.price)} IS NOT (SELECT price FROM card_price_history WHERE card_id = ${escSql(c.id)} ORDER BY captured_at DESC LIMIT 1);`
  );
}

console.log(`Total statements: ${lines.length}`);

const BATCH_SIZE = 900;
const batches = [];
for (let i = 0; i < lines.length; i += BATCH_SIZE) {
  batches.push(lines.slice(i, i + BATCH_SIZE));
}
console.log(`Batches: ${batches.length}`);

mkdirSync('scripts/don_batches', { recursive: true });

for (let i = 0; i < batches.length; i++) {
  const file = `scripts/don_batches/batch_${i + 1}.sql`;
  writeFileSync(file, batches[i].join('\n'), 'utf-8');
  if (DRY) {
    console.log(`  [dry] wrote ${file} (${batches[i].length} stmts)`);
    continue;
  }
  console.log(`Executing batch ${i + 1}/${batches.length}...`);
  execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${file}`, TARGET_FLAG], {
    stdio: 'inherit',
    shell: true,
  });
}

console.log('Done.');
