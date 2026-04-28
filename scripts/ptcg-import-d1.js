/**
 * ptcg-import-d1.js — reads cached TCGdex data from data/ptcg_cache/ and
 * imports into D1 via Wrangler CLI.
 *
 * Run `node scripts/ptcg-fetch.js` first to populate the cache.
 *
 * Output SQL files: scripts/ptcg_batches/{lang}_NNN.sql
 *
 * Usage:
 *   node scripts/ptcg-import-d1.js                # all cached langs
 *   node scripts/ptcg-import-d1.js --lang=en      # single language
 *   node scripts/ptcg-import-d1.js --dry-run      # write SQL files but don't execute
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';

const ALL_LANGS = ['en', 'ja', 'zh-cn', 'zh-tw'];
const CACHE_DIR = 'data/ptcg_cache';
const BATCH_DIR = 'scripts/ptcg_batches';
const BATCH_SIZE = 500; // D1 hard limit is 1000; 500 leaves headroom for big raw blobs.
const DB_NAME = 'optcg-cards';

const args = parseArgs(process.argv.slice(2));
const langs = args.lang ? [args.lang] : ALL_LANGS;
const dryRun = args['dry-run'] === 'true';

if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

let totalSetRows = 0;
let totalCardRows = 0;

for (const lang of langs) {
  const setsPath = `${CACHE_DIR}/sets-${lang}.json`;
  const cardsPath = `${CACHE_DIR}/cards-${lang}.json`;
  if (!existsSync(setsPath) || !existsSync(cardsPath)) {
    console.log(`[${lang}] no cache, skipping (run ptcg-fetch.js first)`);
    continue;
  }

  const sets = JSON.parse(readFileSync(setsPath, 'utf-8'));
  const cardsMap = JSON.parse(readFileSync(cardsPath, 'utf-8'));
  const cards = Object.values(cardsMap);

  console.log(`\n[${lang}] ${sets.length} sets, ${cards.length} cards`);

  const stmts = [];
  for (const set of sets) stmts.push(setUpsert(set, lang));
  for (const card of cards) stmts.push(cardUpsert(card, lang));

  totalSetRows += sets.length;
  totalCardRows += cards.length;

  // Batch
  for (let i = 0, batchNum = 1; i < stmts.length; i += BATCH_SIZE, batchNum++) {
    const slice = stmts.slice(i, i + BATCH_SIZE);
    const file = `${BATCH_DIR}/${lang}_${String(batchNum).padStart(3, '0')}.sql`;
    writeFileSync(file, slice.join('\n'), 'utf-8');
    if (dryRun) {
      console.log(`[${lang}] wrote ${file} (${slice.length} statements) [dry-run]`);
      continue;
    }
    console.log(`[${lang}] executing ${file} (${slice.length} statements)...`);
    execFileSync(npx, ['wrangler', 'd1', 'execute', DB_NAME, `--file=${file}`, '--remote'], {
      stdio: 'inherit',
      shell: true,
    });
  }
}

console.log(`\nDone. ${totalSetRows} set rows, ${totalCardRows} card rows across ${langs.length} language(s).`);
if (dryRun) console.log('(Dry run — no D1 writes.)');

// ── Mappers ───────────────────────────────────────────────────────────────

function setUpsert(set, lang) {
  const cols = [
    'set_id', 'lang', 'name', 'series', 'release_date',
    'card_count_total', 'card_count_official',
    'logo_url', 'symbol_url', 'raw',
  ];
  const vals = [
    set.id,
    lang,
    set.name,
    set.serie?.name ?? null,
    set.releaseDate ?? null,
    set.cardCount?.total ?? null,
    set.cardCount?.official ?? null,
    set.logo ? `${set.logo}.webp` : null,
    set.symbol ? `${set.symbol}.webp` : null,
    JSON.stringify(stripCards(set)),
  ];
  return upsert('ptcg_sets', cols, vals, ['set_id', 'lang']);
}

function stripCards(set) {
  // Don't store the entire cards array in raw — it bloats the row and
  // we have full per-card data already. Keep a count for sanity.
  const { cards, ...rest } = set;
  return { ...rest, _cards_count: cards?.length ?? 0 };
}

function cardUpsert(card, lang) {
  const variants = card.variants ?? {};
  const types = Array.isArray(card.types) ? card.types.join(',') : null;
  const imageBase = card.image ?? null;

  const cols = [
    'card_id', 'lang', 'set_id', 'local_id', 'name',
    'category', 'rarity', 'hp', 'types_csv', 'stage',
    'variants_json', 'image_low', 'image_high',
    'pricing_json', 'dominant_color', 'raw',
  ];
  const vals = [
    card.id,
    lang,
    card.set?.id ?? null,
    card.localId ?? null,
    card.name ?? null,
    card.category ?? null,
    card.rarity ?? null,
    typeof card.hp === 'number' ? card.hp : null,
    types,
    card.stage ?? null,
    JSON.stringify(variants),
    imageBase ? `${imageBase}/low.webp` : null,
    imageBase ? `${imageBase}/high.webp` : null,
    JSON.stringify(card.pricing ?? {}),
    null, // dominant_color — populated by future backfill
    JSON.stringify(card),
  ];
  return upsert('ptcg_cards', cols, vals, ['card_id', 'lang']);
}

// ── SQL helpers ───────────────────────────────────────────────────────────

function upsert(table, cols, vals, pkCols) {
  const placeholders = vals.map(escSql).join(', ');
  const colList = cols.join(', ');
  const updateCols = cols.filter(c => !pkCols.includes(c));
  const updateClause = updateCols.length
    ? updateCols.map(c => `${c}=excluded.${c}`).join(', ') + `, updated_at=strftime('%s','now')`
    : `updated_at=strftime('%s','now')`;
  const conflict = pkCols.join(', ');
  return `INSERT INTO ${table} (${colList}) VALUES (${placeholders}) ON CONFLICT(${conflict}) DO UPDATE SET ${updateClause};`;
}

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return Number.isFinite(val) ? String(val) : 'NULL';
  if (typeof val === 'boolean') return val ? '1' : '0';
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
