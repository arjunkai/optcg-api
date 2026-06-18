/**
 * import-jp-d1.js — reads data/cards_ja.json (from scraper_jp.py) and data/
 * cards.json (the EN catalog), then writes Japanese data to D1 following the
 * multilang spec's three-case rule:
 *
 *   1. id EXISTS in EN cards (SHARED card)
 *        → upsert card_translations (card_id,'ja', name, name_en, image_url,
 *          effect, trigger_text). name_en = the EN name (cross-script search).
 *          `cards` is NOT touched (game-neutral fields stay EN-authoritative).
 *        → if a game mechanic (cost/power/counter) disagrees, EN WINS; the
 *          disagreement is logged to data/_jp_disagreements.json for review.
 *
 *   2. id NOT in EN cards (genuine JA-EXCLUSIVE, e.g. a P-### promo)
 *        → insert a `cards` row (game-neutral, normalized to canonical English
 *          tokens by the scraper) + a card_sets row + a card_translations 'ja'
 *          row. NO 'en' translation is written, so the availability gate keeps
 *          it hidden in EN binders (langs = ['ja']).
 *
 * Usage:
 *   node scripts/import-jp-d1.js --dry-run     # write SQL files, do NOT execute
 *   node scripts/import-jp-d1.js               # execute against remote D1
 *   node scripts/import-jp-d1.js --check-images # HEAD-check JA image_urls first
 *
 * NOTE: run AFTER the EN import (so shared ids already exist) and AFTER
 * migration 016 (card_translations table). Per the spec, the first production
 * run's disagreement log should be reviewed before trusting subsequent runs.
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const DATA_DIR = 'data';
const DRY_RUN = process.argv.includes('--dry-run');
const CHECK_IMAGES = process.argv.includes('--check-images');

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
if (!existsSync(`${DATA_DIR}/cards_ja.json`)) {
  console.error('data/cards_ja.json not found — run scraper_jp.py first.');
  process.exit(1);
}
const jaCards = JSON.parse(readFileSync(`${DATA_DIR}/cards_ja.json`, 'utf-8'));

// Classify shared-vs-exclusive against the LIVE D1 catalog, not a local
// data/cards.json — that file can lag prod (e.g. miss a freshly-imported set),
// which would misclassify existing cards as "JA-exclusive" and create bogus
// `cards` rows. D1 is the source of truth for "does this id already exist".
function loadEnCatalogFromD1() {
  // Must use --command (not --file): --file returns batch stats
  // ("Total queries executed"), while --command returns the actual SELECT
  // rows. The SQL is pre-quoted as a single arg so shell:true doesn't
  // word-split the spaces/commas; the array form (no string interpolation)
  // keeps it injection-safe. --json sends clean JSON to stdout; warnings go
  // to stderr (ignored), so stdout parses directly.
  const out = execFileSync(
    npx,
    ['wrangler', 'd1', 'execute', 'optcg-cards', '--remote', '--json',
     '--command', '"SELECT id, name, cost, power, counter FROM cards"'],
    { encoding: 'utf-8', shell: true, maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] },
  );
  const rows = JSON.parse(out.slice(out.indexOf('[')))[0].results;
  return new Map(rows.map(r => [r.id, r]));
}
const enById = loadEnCatalogFromD1();
console.log(`Loaded ${enById.size} existing card ids from D1 for shared/exclusive classification.`);

// ── Optional HEAD-check so we never write a 404 image_url (write NULL instead).
// The Worker /images/:id?lang=ja route already falls back to the EN scan when
// the JA host has no art, so this is belt-and-braces, off by default.
async function resolveImage(url) {
  if (!CHECK_IMAGES || !url) return url ?? null;
  try {
    const res = await fetch(url, { method: 'HEAD' });
    return res.ok ? url : null;
  } catch {
    return null;
  }
}

async function build() {
  const trLines = [];      // card_translations (shared + exclusive)
  const cardLines = [];    // cards rows (exclusives only)
  const setRowLines = [];  // sets rows (exclusives only)
  const cardSetLines = []; // card_sets rows (exclusives only) — FK → cards + sets
  const disagreements = [];
  let shared = 0, exclusive = 0;

  for (const c of jaCards) {
    const en = enById.get(c.id);
    const imageUrl = await resolveImage(c.image_url);

    if (en) {
      // ── Case 1: shared card → JA translation only ──
      shared++;
      // EN wins on game-mechanic disagreement; record it, never write cards.
      for (const field of ['cost', 'power', 'counter']) {
        if (c[field] != null && en[field] != null && c[field] !== en[field]) {
          disagreements.push({ id: c.id, field, en: en[field], ja: c[field] });
        }
      }
      trLines.push(
        `INSERT INTO card_translations (card_id, language, name, name_en, image_url, effect, trigger_text) ` +
        `VALUES (${escSql(c.id)}, 'ja', ${escSql(c.name)}, ${escSql(en.name)}, ${escSql(imageUrl)}, ${escSql(c.effect)}, ${escSql(c.trigger)}) ` +
        `ON CONFLICT(card_id, language) DO UPDATE SET name=excluded.name, name_en=excluded.name_en, image_url=excluded.image_url, effect=excluded.effect, trigger_text=excluded.trigger_text;`
      );
    } else {
      // ── Case 2: JA-exclusive → cards + card_sets + JA translation (no EN) ──
      exclusive++;
      cardLines.push(
        `INSERT INTO cards (id, base_id, parallel, variant_type, name, rarity, category, image_url, colors, cost, power, counter, attributes, types) ` +
        `VALUES (${escSql(c.id)}, ${escSql(c.base_id)}, ${c.parallel ? 1 : 0}, ${escSql(c.variant_type)}, ${escSql(c.name)}, ${escSql(c.rarity)}, ${escSql(c.category)}, ${escSql(imageUrl)}, ${jsonOrNull(c.colors)}, ${escSql(c.cost)}, ${escSql(c.power)}, ${escSql(c.counter)}, ${jsonOrNull(c.attributes)}, NULL) ` +
        // types is free-form Japanese for exclusives (no canonical EN map) → NULL
        // rather than poison the language-neutral column. effect/trigger live on
        // the translation row, so cards.effect/trigger_text stay NULL here.
        `ON CONFLICT(id) DO UPDATE SET name=excluded.name, rarity=excluded.rarity, category=excluded.category, image_url=excluded.image_url, colors=excluded.colors, cost=excluded.cost, power=excluded.power, counter=excluded.counter, attributes=excluded.attributes;`
      );
      if (c.set_id) {
        setRowLines.push(
          `INSERT INTO sets (id, pack_id, label, card_count) VALUES (${escSql(c.set_id)}, ${escSql(c.pack_id)}, ${escSql(c.set_id)}, 0) ON CONFLICT(id) DO NOTHING;`
        );
        cardSetLines.push(
          `INSERT INTO card_sets (card_id, set_id, pack_id) VALUES (${escSql(c.id)}, ${escSql(c.set_id)}, ${escSql(c.pack_id)}) ON CONFLICT(card_id, set_id) DO NOTHING;`
        );
      }
      // JA translation row (no EN alias from scrape → name_en NULL; a manual
      // romaji alias can be added later for latin-script search).
      trLines.push(
        `INSERT INTO card_translations (card_id, language, name, name_en, image_url, effect, trigger_text) ` +
        `VALUES (${escSql(c.id)}, 'ja', ${escSql(c.name)}, NULL, ${escSql(imageUrl)}, ${escSql(c.effect)}, ${escSql(c.trigger)}) ` +
        `ON CONFLICT(card_id, language) DO UPDATE SET name=excluded.name, image_url=excluded.image_url, effect=excluded.effect, trigger_text=excluded.trigger_text;`
      );
    }
  }

  // FK-safe order: sets + cards (no FK) → card_sets (FK→cards+sets) →
  // translations (FK→cards). Emitting card_sets before its cards row trips
  // FOREIGN KEY constraint failed (D1 enforces FKs).
  const lines = [...setRowLines, ...cardLines, ...cardSetLines, ...trLines];

  writeFileSync(`${DATA_DIR}/_jp_disagreements.json`, JSON.stringify(disagreements, null, 2), 'utf-8');
  console.log(`JA cards: ${jaCards.length}  (shared: ${shared}, exclusive: ${exclusive})`);
  console.log(`SQL statements: ${lines.length}   mechanic disagreements: ${disagreements.length} (EN wins; see data/_jp_disagreements.json)`);
  return lines;
}

// ── Batch execution (same pattern as import-d1.js) ──────────────────────────
const lines = await build();
const BATCH_SIZE = 900;
const batches = [];
for (let i = 0; i < lines.length; i += BATCH_SIZE) batches.push(lines.slice(i, i + BATCH_SIZE));

if (DRY_RUN) {
  for (let i = 0; i < batches.length; i++) {
    writeFileSync(`scripts/import_jp_batch_${i + 1}.sql`, batches[i].join('\n'), 'utf-8');
  }
  console.log(`--dry-run: wrote ${batches.length} SQL file(s) to scripts/import_jp_batch_*.sql (not executed).`);
  process.exit(0);
}

console.log('Importing JA data to D1 (remote)...');
for (let i = 0; i < batches.length; i++) {
  const sqlFile = `scripts/import_jp_batch_${i + 1}.sql`;
  writeFileSync(sqlFile, batches[i].join('\n'), 'utf-8');
  console.log(`Executing batch ${i + 1}/${batches.length} (${batches[i].length} statements)...`);
  execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${sqlFile}`, '--remote'], { stdio: 'inherit', shell: true });
}
console.log('Done!');
