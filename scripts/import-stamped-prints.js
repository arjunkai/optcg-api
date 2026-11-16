/**
 * import-stamped-prints.js: adds card rows for stamped TCGPlayer prints the
 * official site doesn't list (championship / winner / finalist / treasure cup
 * ... prints of an existing card), from the reviewed data/stamped_prints.json.
 *
 * Plan: docs/stamped-prints-plan.md §2-3 (Phase C).
 *
 * IDs are {base}_t{TCGPlayer product id}, e.g. OP07-109_t579983. They are
 * permanent once shipped (users put them in binders), so rows come ONLY from
 * entries a person approved: each entry needs "approved": true. Candidates
 * come from scripts/build_stamped_candidates.py.
 *
 * data/stamped_prints.json, keyed by TCGPlayer product id:
 *   { "579983": { "base": "OP07-109", "label": "CS 2024 Event Pack",
 *                 "stamp": "championship", "rarity": "Promo",
 *                 "approved": true, "added": "2026-11-20" } }
 *
 * Each row is INSERT ... SELECT from the base row, so it inherits name,
 * category, colors, cost, power, counter, attributes, types, effect and
 * trigger. It sets parallel=1, base_id, variant_type ("Alternate Art" unless
 * the entry says otherwise), print_label, stamp, rarity, tcg_ids=[id] and
 * image_url=/images/{id} (R2, then the TCGPlayer photo; src/images.js). It
 * joins set 569901 (Promotion card). Prices come from the weekly price build,
 * which maps every _t row straight to its own product.
 *
 * Writes only rows that change (tests/import-writes.test.mjs): the upsert
 * skips rows whose columns already match, and card_sets is DO NOTHING.
 *
 * Rollback (one print):
 *   wrangler d1 execute optcg-cards --remote \
 *     --command "DELETE FROM card_sets WHERE card_id='OP07-109_t579983'; \
 *                DELETE FROM cards WHERE id='OP07-109_t579983'"
 * Only before users could have added it to a binder: after that, mark it
 * retired instead (plan §3, Phase E).
 *
 * Usage:
 *   node scripts/import-stamped-prints.js             # remote write
 *   node scripts/import-stamped-prints.js --dry-run   # build SQL, don't run
 *   node scripts/import-stamped-prints.js --local     # local D1
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const LOCAL = process.argv.includes('--local');
const DRY = process.argv.includes('--dry-run');
const TARGET_FLAG = LOCAL ? '--local' : '--remote';

const API_BASE = 'https://optcg-api.arjunbansal-ai.workers.dev';
const PROMO_SET = '569901';
const FILE = 'data/stamped_prints.json';
// Same shape OPBindr's publish regex and src/images.js STAMPED_ID accept.
const BASE_RE = /^[A-Z0-9]+-[A-Z0-9]+$/;

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return String(val);
  return `'${String(val).replace(/'/g, "''")}'`;
}

function stampedId(base, tcgId) {
  return `${base}_t${tcgId}`;
}

const blob = existsSync(FILE) ? JSON.parse(readFileSync(FILE, 'utf-8')) : {};
const entries = [];
for (const [key, e] of Object.entries(blob)) {
  if (key.startsWith('_')) continue; // comments
  const tcgId = Number(key);
  if (!Number.isInteger(tcgId) || tcgId <= 0) { console.error(`  [skip] ${key}: key must be a TCGPlayer product id`); continue; }
  if (!e?.approved) continue; // candidates wait for a person
  if (!BASE_RE.test(e.base || '')) { console.error(`  [skip] ${key}: bad base ${e.base}`); continue; }
  if (!e.label) { console.error(`  [skip] ${key}: missing label`); continue; }
  entries.push({ tcgId, ...e });
}

console.log(`Approved stamped prints: ${entries.length}`);
if (entries.length === 0) {
  console.log('Nothing to import.');
  process.exit(0);
}

const lines = [];
for (const e of entries) {
  const id = stampedId(e.base, e.tcgId);
  const cols = {
    base_id: e.base,
    parallel: 1,
    variant_type: e.variant_type ?? 'Alternate Art',
    print_label: e.label,
    stamp: e.stamp ?? null,
    tcg_ids: JSON.stringify([e.tcgId]),
    image_url: `${API_BASE}/images/${id}`,
  };
  const names = Object.keys(cols);
  // rarity: TCGPlayer's when the entry has one, else the base card's.
  const rarity = e.rarity ? escSql(e.rarity) : 'rarity';
  const changed = [...names, 'rarity'].map((c) => `cards.${c} IS NOT excluded.${c}`).join(' OR ');
  lines.push(
    `INSERT INTO cards (id, ${names.join(', ')}, rarity, name, category, colors, cost, power, counter, attributes, types, effect, trigger_text) ` +
    `SELECT ${escSql(id)}, ${names.map((n) => escSql(cols[n])).join(', ')}, ${rarity}, name, category, colors, cost, power, counter, attributes, types, effect, trigger_text ` +
    `FROM cards WHERE id = ${escSql(e.base)} ` +
    `ON CONFLICT(id) DO UPDATE SET ${[...names, 'rarity'].map((c) => `${c}=excluded.${c}`).join(', ')} ` +
    `WHERE ${changed};`
  );
  lines.push(
    `INSERT INTO card_sets (card_id, set_id, pack_id) ` +
    `SELECT ${escSql(id)}, ${escSql(PROMO_SET)}, ${escSql(PROMO_SET)} WHERE EXISTS (SELECT 1 FROM cards WHERE id = ${escSql(id)}) ` +
    `ON CONFLICT(card_id, set_id) DO NOTHING;`
  );
}

mkdirSync('scripts/stamped_batches', { recursive: true });
const sqlFile = 'scripts/stamped_batches/stamped_prints.sql';
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
