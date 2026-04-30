/**
 * import-malie-en-d1.js — fills image_high for English Pokemon cards
 * using malie.io's PTCGO/PTCGL game-client export.
 *
 * Why malie.io?  TCG Collector's About page explicitly credits the
 * community researcher "nago" (malie.io) as their image source. Game
 * client extracts cover modern English sets at high quality, including
 * trainer-kit subsets and promo sets that pokemontcg-data and the
 * live api.pokemontcg.io don't carry.
 *
 * Two source endpoints, both public CDN, no auth:
 *   - PTCGL (current, SV/Mega era): /tcgl/export/index.json
 *     -> per-set: /tcgl/export/v0.1.9.X/{set}.en-US.json
 *     -> images:  cards[i].images.tcgl.jpg.front  (full URL)
 *   - PTCGO (HGSS through SV1): /static/cheatsheets/en_US/json/{SET}.json
 *     -> images:  cards[i]._lossy_url  (full URL)
 *
 * Reads:
 *   - data/ptcg_malie_set_mapping.json (malie set code → TCGdex set id)
 *
 * Writes batched UPDATEs to scripts/pokemontcg_batches/, executes via
 * `wrangler d1 execute --remote`. COALESCE-only — never overwrites an
 * existing image. EN rows only.
 *
 * Usage:
 *   node scripts/import-malie-en-d1.js
 *   node scripts/import-malie-en-d1.js --malie-set=HGSS1
 *   node scripts/import-malie-en-d1.js --dry-run
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const MAPPING_PATH = 'data/ptcg_malie_set_mapping.json';
const BATCH_DIR = 'scripts/pokemontcg_batches';
const BATCH_SIZE = 500;
const DB_NAME = 'optcg-cards';
const PTCGO_BASE = 'https://malie.io/static/cheatsheets/en_US/json';
const PTCGL_INDEX = 'https://cdn.malie.io/file/malie-io/tcgl/export/index.json';
const PTCGL_BASE = 'https://cdn.malie.io/file/malie-io/tcgl/export';
const REQ_INTERVAL_MS = 500;

const args = parseArgs(process.argv.slice(2));
const malieSetFilter = args['malie-set'] || null;
const dryRun = args['dry-run'] === 'true';

if (!existsSync(MAPPING_PATH)) {
  console.error(`Missing ${MAPPING_PATH}.`);
  process.exit(1);
}
if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

const mapping = JSON.parse(readFileSync(MAPPING_PATH, 'utf-8'));
delete mapping._doc;

// PTCGL is the source-of-truth for the CURRENT SV-era sets; PTCGO covers
// everything before that. We pull the PTCGL index once so we know which
// set codes to route there vs. PTCGO.
let ptcglIndex = {};
try {
  ptcglIndex = await fetchJson(PTCGL_INDEX);
  ptcglIndex = ptcglIndex['en-US'] || {};
} catch (err) {
  console.warn(`Could not fetch PTCGL index: ${err.message}. Falling back to PTCGO-only.`);
}

let totalUpdates = 0;
let totalCardsWithImages = 0;
let lastReqAt = 0;

const malieSetsToProcess = malieSetFilter ? [malieSetFilter] : Object.keys(mapping);

for (const malieId of malieSetsToProcess) {
  const tcgdexId = mapping[malieId];
  if (!tcgdexId) {
    console.log(`[${malieId}] no TCGdex mapping, skipping`);
    continue;
  }

  await throttle();
  const cards = await fetchSet(malieId);
  if (!cards) continue;
  console.log(`[${malieId} → ${tcgdexId}] ${cards.length} cards`);

  const stmts = [];
  let cardsWithImages = 0;
  for (const c of cards) {
    const localId = extractLocalId(c, malieId);
    const imageUrl = extractImageUrl(c);
    if (!localId || !imageUrl) continue;
    cardsWithImages++;
    totalCardsWithImages++;

    // TCGdex's card_id format is `{tcgdexId}-{localId}`. Card numbers
    // here are unpadded; same multi-candidate pattern as the other
    // imports for TCGdex's varying padding.
    const candidates = new Set([
      `${tcgdexId}-${localId}`,
      `${tcgdexId}-${localId.replace(/^0+/, '') || localId}`,
      `${tcgdexId}-${localId.padStart(3, '0')}`,
    ]);

    for (const candidate of candidates) {
      stmts.push(
        `UPDATE ptcg_cards SET image_high = COALESCE(image_high, ${escSql(imageUrl)}), image_low = COALESCE(image_low, ${escSql(imageUrl)}) WHERE card_id = ${escSql(candidate)} AND lang = 'en';`,
      );
    }
  }

  if (stmts.length === 0) {
    console.log(`[${malieId}] no usable rows`);
    continue;
  }
  totalUpdates += stmts.length;

  for (let i = 0, batch = 1; i < stmts.length; i += BATCH_SIZE, batch++) {
    const slice = stmts.slice(i, i + BATCH_SIZE);
    const file = `${BATCH_DIR}/malie_${malieId.replace(/[^a-zA-Z0-9]/g, '_')}_${String(batch).padStart(3, '0')}.sql`;
    writeFileSync(file, slice.join('\n'));
    if (dryRun) {
      console.log(`[${malieId}] wrote ${file} (${slice.length} stmts) [dry-run]`);
      continue;
    }
    console.log(`[${malieId}] executing ${file} (${slice.length} stmts)...`);
    execFileSync(npx, ['wrangler', 'd1', 'execute', DB_NAME, `--file=${file}`, '--remote'], {
      stdio: 'inherit', shell: true,
    });
  }
}

console.log(`\nDone. ${totalUpdates} update statements across ${totalCardsWithImages} cards.`);
if (dryRun) console.log('(Dry run — no D1 writes.)');

async function fetchSet(malieId) {
  // Prefer PTCGL when the set is in the index — it's higher quality
  // (modern game client) and includes the most recent sets.
  if (ptcglIndex[malieId]?.path) {
    const url = `${PTCGL_BASE}/${ptcglIndex[malieId].path}`;
    try {
      const data = await fetchJson(url);
      return Array.isArray(data) ? data : (data?.cards || null);
    } catch (err) {
      console.log(`[${malieId}] PTCGL fetch failed (${err.message}), falling back to PTCGO`);
    }
  }
  // PTCGO fallback (HGSS through SV1).
  try {
    const data = await fetchJson(`${PTCGO_BASE}/${encodeURIComponent(malieId)}.json`);
    return Array.isArray(data) ? data : null;
  } catch (err) {
    console.log(`[${malieId}] not in PTCGO archive either: ${err.message}`);
    return null;
  }
}

async function fetchJson(url) {
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': 'opbindr-ptcg-importer/1.0 (+https://opbindr.com)' },
      });
      const ct = res.headers.get('content-type') || '';
      if (res.status === 200 && ct.includes('application/json')) {
        return await res.json();
      }
      if (res.status === 404) throw new Error('404');
      if (!ct.includes('application/json')) throw new Error(`non-JSON content-type: ${ct}`);
      throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      if (attempt === 3 || err.message === '404') throw err;
      await sleep(2000 * attempt);
    }
  }
}

function extractImageUrl(card) {
  // PTCGL shape: { images: { tcgl: { jpg: { front: 'https://...' } } } }
  const tcgl = card?.images?.tcgl;
  if (tcgl) {
    return tcgl.jpg?.front || tcgl.png?.front || tcgl.tex?.front || null;
  }
  // PTCGO shape: { _lossy_url: '...', _cropped_url: '...' }
  return card?._lossy_url || card?._cropped_url || null;
}

function extractLocalId(card, malieId) {
  // PTCGL shape: collector_number.numerator (string, e.g. "001") or .numeric
  const cn = card?.collector_number;
  if (cn?.numerator) return String(cn.numerator);
  if (cn?.numeric != null) return String(cn.numeric);
  // PTCGO shape: card_number (string, e.g. "1" or "RC1")
  if (card?.card_number) return String(card.card_number);
  // Last-resort: parse from cardID (e.g. "me1_1") or _key (e.g. "HGSS1")
  const tcglId = card?.ext?.tcgl?.cardID;
  if (tcglId) {
    const parts = tcglId.split('_');
    if (parts.length >= 2) return parts[parts.length - 1];
  }
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
