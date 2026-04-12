/**
 * import-d1.js — reads data/cards.json + data/sets.json, generates SQL,
 * and imports into D1 via Wrangler CLI.
 *
 * Usage: node scripts/import-d1.js
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

// On Windows, npx is npx.cmd
const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';

const DATA_DIR = 'data';

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return String(val);
  if (typeof val === 'boolean') return val ? '1' : '0';
  return `'${String(val).replace(/'/g, "''")}'`;
}

function jsonOrNull(val) {
  if (val === null || val === undefined) return 'NULL';
  return escSql(JSON.stringify(val));
}

// ── Load data ──────────────────────────────────────────────────────────────
const sets = JSON.parse(readFileSync(`${DATA_DIR}/sets.json`, 'utf-8'));
const cards = JSON.parse(readFileSync(`${DATA_DIR}/cards.json`, 'utf-8'));

// Apply variant_type overrides
const overridePath = `${DATA_DIR}/variant_types.json`;
if (existsSync(overridePath)) {
  const overrides = JSON.parse(readFileSync(overridePath, 'utf-8'));
  for (const card of cards) {
    if (overrides[card.id]) {
      card.variant_type = overrides[card.id];
    }
  }
  console.log(`Applied ${Object.keys(overrides).length} variant_type overrides`);
}

// ── Generate SQL ───────────────────────────────────────────────────────────
const lines = [];

// Sets
for (const s of sets) {
  lines.push(
    `INSERT INTO sets (id, pack_id, label, card_count) VALUES (${escSql(s.set_id)}, ${escSql(s.pack_id)}, ${escSql(s.label)}, ${escSql(s.count)}) ON CONFLICT(id) DO UPDATE SET pack_id=excluded.pack_id, label=excluded.label, card_count=excluded.card_count;`
  );
}

// Cards
for (const c of cards) {
  lines.push(
    `INSERT INTO cards (id, base_id, parallel, variant_type, name, rarity, category, image_url, colors, cost, power, counter, attributes, types, effect, trigger_text) VALUES (${escSql(c.id)}, ${escSql(c.base_id)}, ${c.parallel ? 1 : 0}, ${escSql(c.variant_type)}, ${escSql(c.name)}, ${escSql(c.rarity)}, ${escSql(c.category)}, ${escSql(c.image_url)}, ${jsonOrNull(c.colors)}, ${escSql(c.cost)}, ${escSql(c.power)}, ${escSql(c.counter)}, ${jsonOrNull(c.attributes)}, ${jsonOrNull(c.types)}, ${escSql(c.effect)}, ${escSql(c.trigger)}) ON CONFLICT(id) DO UPDATE SET name=excluded.name, variant_type=excluded.variant_type, rarity=excluded.rarity, category=excluded.category, image_url=excluded.image_url, colors=excluded.colors, cost=excluded.cost, power=excluded.power, counter=excluded.counter, attributes=excluded.attributes, types=excluded.types, effect=excluded.effect, trigger_text=excluded.trigger_text;`
  );
}

// Card-set relationships
for (const c of cards) {
  lines.push(
    `INSERT INTO card_sets (card_id, set_id, pack_id) VALUES (${escSql(c.id)}, ${escSql(c.set_id)}, ${escSql(c.pack_id)}) ON CONFLICT(card_id, set_id) DO NOTHING;`
  );
}

console.log(`Total SQL statements: ${lines.length}`);

// ── Batch execution (D1 limit: ~1000 statements per request) ──────────────
const BATCH_SIZE = 900;
const batches = [];
for (let i = 0; i < lines.length; i += BATCH_SIZE) {
  batches.push(lines.slice(i, i + BATCH_SIZE));
}
console.log(`Splitting into ${batches.length} batches of up to ${BATCH_SIZE} statements`);

// ── Execute against D1 ────────────────────────────────────────────────────
console.log('Importing to D1 (remote)...');
for (let i = 0; i < batches.length; i++) {
  const sqlFile = `scripts/import_batch_${i + 1}.sql`;
  writeFileSync(sqlFile, batches[i].join('\n'), 'utf-8');
  console.log(`Executing batch ${i + 1}/${batches.length} (${batches[i].length} statements)...`);
  execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${sqlFile}`, '--remote'], { stdio: 'inherit', shell: true });
}

console.log('Done!');
