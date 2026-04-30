/**
 * import-ptcg-manual-prices.js — applies manual USD price overrides
 * to ptcg_cards. Each entry's pricing JSON gets a `manual.price` key,
 * and price_source flips to 'manual' (highest priority in the
 * normalizer's pickPrice ladder). Touches all language rows because
 * a manual price is correct regardless of localization.
 *
 * Usage: node scripts/import-ptcg-manual-prices.js
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const OVERRIDES_PATH = 'data/ptcg_manual_prices.json';
const BATCH_DIR = 'scripts/pokemontcg_batches';
const DB_NAME = 'optcg-cards';

if (!existsSync(OVERRIDES_PATH)) {
  console.error(`Missing ${OVERRIDES_PATH}.`);
  process.exit(1);
}
if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

const overrides = JSON.parse(readFileSync(OVERRIDES_PATH, 'utf-8'));
delete overrides._doc;

const stmts = [];
for (const [cardId, price] of Object.entries(overrides)) {
  if (typeof price !== 'number' || !Number.isFinite(price)) {
    console.warn(`Skip ${cardId}: price is not a finite number (${price})`);
    continue;
  }
  stmts.push(
    `UPDATE ptcg_cards SET pricing_json = json_patch(COALESCE(pricing_json, '{}'), json_object('manual', json_object('price', ${price}))), price_source = 'manual' WHERE card_id = ${escSql(cardId)};`,
  );
}

if (stmts.length === 0) {
  console.log('No overrides to apply.');
  process.exit(0);
}

const path = `${BATCH_DIR}/manual.sql`;
writeFileSync(path, stmts.join('\n'));
console.log(`Applying ${stmts.length} manual override(s)...`);
// `shell: true` is required on Windows so npx.cmd resolves; args are
// shell-interpolated. Safe here — every value is a card_id from the
// committed overrides file or a literal we control.
execFileSync(npx, ['wrangler', 'd1', 'execute', DB_NAME, `--file=${path}`, '--remote'], {
  stdio: 'inherit', shell: true,
});
console.log('Done.');

function escSql(val) {
  return `'${String(val).replace(/'/g, "''")}'`;
}
