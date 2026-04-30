/**
 * fetch-pokemontcg-prices.js — pulls fresh TCGplayer USD + Cardmarket EUR
 * prices from the live api.pokemontcg.io REST API, writes them to D1.
 *
 * The pokemontcg-data git submodule (used by import-pokemontcg-d1.js for
 * image gap-fill) carries no pricing — that data only lives on the live
 * API. So we hit it weekly here, scoped to the 165 sets in our mapping.
 *
 * Reads:
 *   - data/ptcg_set_mapping.json (TCGdex_set → pokemontcg_set)
 *
 * Writes batched UPDATE SQL to scripts/pokemontcg_batches/, executes via
 * `wrangler d1 execute --remote`. Pricing semantics:
 *   - pricing_json.tcgplayer  → overwritten (weekly refresh, not first-write)
 *   - pricing_json.cardmarket → overwritten (live API has both feeds; trusted)
 *   - price_source            → flips to 'pokemontcg' unless already 'manual'
 *   - manual entries are preserved (CASE WHEN price_source = 'manual' …)
 *
 * Rate limits: free tier is 1000/day, 30/min. We hit ~165 set queries
 * weekly, well under the cap. Add ?key=… support if usage ever grows.
 *
 * Usage:
 *   node scripts/fetch-pokemontcg-prices.js
 *   node scripts/fetch-pokemontcg-prices.js --pokemontcg-set=mcd11
 *   node scripts/fetch-pokemontcg-prices.js --dry-run
 *
 * Optional env: POKEMONTCG_API_KEY — when set, sent as X-Api-Key, lifts
 * the daily quota to 20k/day per the docs.
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const MAPPING_PATH = 'data/ptcg_set_mapping.json';
const BATCH_DIR = 'scripts/pokemontcg_batches';
const BATCH_SIZE = 500;
const DB_NAME = 'optcg-cards';
const API_BASE = 'https://api.pokemontcg.io/v2';
// Polite client-side throttle; well below the 30/min cap.
const REQ_INTERVAL_MS = 2000;

const args = parseArgs(process.argv.slice(2));
const setFilter = args['pokemontcg-set'] || null;
const dryRun = args['dry-run'] === 'true';
const apiKey = process.env.POKEMONTCG_API_KEY || '';

if (!existsSync(MAPPING_PATH)) {
  console.error(`Missing ${MAPPING_PATH}. Run scripts/build-ptcg-set-mapping.js first.`);
  process.exit(1);
}
if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

const mapping = JSON.parse(readFileSync(MAPPING_PATH, 'utf-8'));
// pokemontcg_set → [tcgdex_set, …] — multiple TCGdex sets may share a
// pokemontcg counterpart (rare in practice, defensive here).
const reverse = {};
for (const [tcg, pkm] of Object.entries(mapping)) {
  if (!reverse[pkm]) reverse[pkm] = [];
  reverse[pkm].push(tcg);
}

const pokemontcgSets = setFilter ? [setFilter] : Object.keys(reverse).sort();

console.log(`Fetching prices for ${pokemontcgSets.length} pokemontcg set(s)...`);

let totalUpdates = 0;
let totalCardsWithPrices = 0;
let lastReqAt = 0;

for (const pkmId of pokemontcgSets) {
  const tcgdexIds = reverse[pkmId];
  if (!tcgdexIds || tcgdexIds.length === 0) {
    console.log(`[${pkmId}] no TCGdex partner, skipping`);
    continue;
  }

  await throttle();
  const cards = await fetchSet(pkmId);
  if (!cards) continue;
  const withPrices = cards.filter((c) => c.tcgplayer?.prices || c.cardmarket?.prices);
  console.log(`[${pkmId}] ${cards.length} cards, ${withPrices.length} priced → tcgdex ${tcgdexIds.join(',')}`);
  totalCardsWithPrices += withPrices.length;

  const stmts = [];
  for (const c of withPrices) {
    const pkmLocalId = String(c.id?.split('-').slice(1).join('-') ?? '').trim();
    if (!pkmLocalId) continue;

    // Same multi-candidate pattern as import-pokemontcg-d1.js for
    // TCGdex's varying ID padding.
    const candidates = new Set();
    for (const tcgdexId of tcgdexIds) {
      candidates.add(`${tcgdexId}-${pkmLocalId}`);
      candidates.add(`${tcgdexId}-${pkmLocalId.replace(/^0+/, '') || pkmLocalId}`);
      candidates.add(`${tcgdexId}-${pkmLocalId.padStart(3, '0')}`);
    }

    // Build a JSON object containing the price feeds present. Then
    // json_patch into pricing_json so existing keys (like manual) are
    // preserved.
    const patch = {};
    if (c.tcgplayer?.prices) patch.tcgplayer = c.tcgplayer.prices;
    if (c.cardmarket?.prices) patch.cardmarket = c.cardmarket.prices;
    if (Object.keys(patch).length === 0) continue;
    const patchSql = JSON.stringify(patch).replace(/'/g, "''");

    const updates = [
      `pricing_json = json_patch(COALESCE(pricing_json, '{}'), '${patchSql}')`,
      `price_source = CASE WHEN price_source = 'manual' THEN 'manual' ELSE 'pokemontcg' END`,
    ];

    for (const candidate of candidates) {
      stmts.push(
        `UPDATE ptcg_cards SET ${updates.join(', ')} WHERE card_id = ${escSql(candidate)} AND lang = 'en';`,
      );
    }
  }

  if (stmts.length === 0) {
    console.log(`[${pkmId}] no updates`);
    continue;
  }
  totalUpdates += stmts.length;

  for (let i = 0, batch = 1; i < stmts.length; i += BATCH_SIZE, batch++) {
    const slice = stmts.slice(i, i + BATCH_SIZE);
    const file = `${BATCH_DIR}/prices_${pkmId}_${String(batch).padStart(3, '0')}.sql`;
    writeFileSync(file, slice.join('\n'));
    if (dryRun) {
      console.log(`[${pkmId}] wrote ${file} (${slice.length} stmts) [dry-run]`);
      continue;
    }
    console.log(`[${pkmId}] executing ${file} (${slice.length} stmts)...`);
    // shell:true required on Windows for npx.cmd; interpolated args are
    // alphanumeric mapping keys, never user input.
    execFileSync(npx, ['wrangler', 'd1', 'execute', DB_NAME, `--file=${file}`, '--remote'], {
      stdio: 'inherit', shell: true,
    });
  }
}

console.log(`\nDone. ${totalUpdates} update statements across ${totalCardsWithPrices} priced cards.`);
if (dryRun) console.log('(Dry run — no D1 writes.)');

async function fetchSet(pkmId) {
  const url = `${API_BASE}/cards?q=set.id:${encodeURIComponent(pkmId)}&pageSize=250&select=id,tcgplayer,cardmarket`;
  const headers = apiKey ? { 'X-Api-Key': apiKey } : {};
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url, { headers });
      if (res.status === 404) {
        console.log(`[${pkmId}] not found in live API, skipping`);
        return null;
      }
      if (res.status === 429) {
        const wait = Math.min(60_000, 5000 * attempt);
        console.log(`[${pkmId}] 429 rate-limited, waiting ${wait}ms…`);
        await sleep(wait);
        continue;
      }
      if (!res.ok) {
        console.log(`[${pkmId}] HTTP ${res.status}, skipping`);
        return null;
      }
      const body = await res.json();
      return body.data ?? [];
    } catch (err) {
      console.log(`[${pkmId}] fetch error: ${err.message}, retry ${attempt}/3`);
      await sleep(2000 * attempt);
    }
  }
  console.log(`[${pkmId}] exhausted retries, skipping`);
  return null;
}

async function throttle() {
  const elapsed = Date.now() - lastReqAt;
  if (elapsed < REQ_INTERVAL_MS) await sleep(REQ_INTERVAL_MS - elapsed);
  lastReqAt = Date.now();
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

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
