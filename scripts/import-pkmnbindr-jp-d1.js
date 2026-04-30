/**
 * import-pkmnbindr-jp-d1.js — gap-fills image_high for Japanese Pokemon
 * cards using pkmnbindr's public per-set JSON files.
 *
 * Why pkmnbindr?  We surveyed every free non-EN source on 2026-04-30:
 *   - TCGdex (our primary, multi-lang) is sparse upstream — 53.8% JA
 *     image coverage and stuck there.
 *   - pkmntcg-data static dump is EN-only.
 *   - The live api.pokemontcg.io (Scrydex) does not serve JP cards
 *     through /v2/cards (just confirmed: m4_ja-1 returns 404).
 *   - pkmncards / Bulbapedia forbid or have no programmatic surface.
 *   - JustTCG / PokemonPriceTracker are paid (deferred).
 *
 * pkmnbindr.com hosts a public static catalog of JP card data at
 * /data/jpNew/cards/{setCode}_ja.json (no auth, no captcha, direct
 * application/json on Cloudflare CDN). Each card carries a Scrydex
 * image URL like https://images.scrydex.com/pokemon/{setCode}_ja-{n}/large
 * — Scrydex is the new home of pokemontcg.io's CDN, public for any
 * caller. pkmnbindr is therefore an INDEX we consult to discover the
 * Scrydex card IDs; the image bandwidth lands on Scrydex, not them.
 * Polite cadence: weekly fetch of ~160 set files (~50MB total).
 *
 * Reads:
 *   - data/ptcg_jp_set_mapping.json (TCGdex_set → pkmnbindr_set_id)
 *
 * Writes batched UPDATEs to scripts/pokemontcg_batches/, executes via
 * `wrangler d1 execute --remote`. COALESCE-only — never overwrites an
 * existing TCGdex image. price_source untouched (we only fill images).
 *
 * Usage:
 *   node scripts/import-pkmnbindr-jp-d1.js
 *   node scripts/import-pkmnbindr-jp-d1.js --tcgdex-set=SV9
 *   node scripts/import-pkmnbindr-jp-d1.js --dry-run
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const MAPPING_PATH = 'data/ptcg_jp_set_mapping.json';
const BATCH_DIR = 'scripts/pokemontcg_batches';
const BATCH_SIZE = 500;
const DB_NAME = 'optcg-cards';
const PKMNBINDR_BASE = 'https://www.pkmnbindr.com/data/jpNew/cards';
// Polite client-side throttle — pkmnbindr is a single small site, the
// total weekly volume is ~160 fetches × ~350KB. 500ms spacing keeps us
// under any sane rate limit.
const REQ_INTERVAL_MS = 500;

const args = parseArgs(process.argv.slice(2));
const tcgdexSetFilter = args['tcgdex-set'] || null;
const dryRun = args['dry-run'] === 'true';

if (!existsSync(MAPPING_PATH)) {
  console.error(`Missing ${MAPPING_PATH}. Run scripts/build-ptcg-jp-set-mapping.js first.`);
  process.exit(1);
}
if (!existsSync(BATCH_DIR)) mkdirSync(BATCH_DIR, { recursive: true });

const mapping = JSON.parse(readFileSync(MAPPING_PATH, 'utf-8'));
delete mapping._doc;

let totalUpdates = 0;
let totalCardsWithImages = 0;
let lastReqAt = 0;

const tcgdexSets = tcgdexSetFilter ? [tcgdexSetFilter] : Object.keys(mapping);

for (const tcgdexId of tcgdexSets) {
  const pkmId = mapping[tcgdexId];
  if (!pkmId) {
    console.log(`[${tcgdexId}] no mapping, skipping`);
    continue;
  }

  await throttle();
  const cards = await fetchSet(pkmId);
  if (!cards) continue;
  const withImages = cards.filter((c) => extractImageUrls(c).large);
  console.log(`[${tcgdexId} → ${pkmId}] ${cards.length} cards, ${withImages.length} with image`);
  totalCardsWithImages += withImages.length;

  const stmts = [];
  for (const c of withImages) {
    const localId = extractLocalId(c);
    if (!localId) continue;
    const { large, small } = extractImageUrls(c);
    if (!large) continue;

    // TCGdex's card_id in D1 is `{tcgdexId}-{localId}`. pkmnbindr's
    // local ids are unpadded numeric. Same multi-candidate pattern as
    // import-pokemontcg-d1.js / import-tcgpocket-d1.js for TCGdex's
    // varying padding conventions.
    const candidates = new Set([
      `${tcgdexId}-${localId}`,
      `${tcgdexId}-${localId.replace(/^0+/, '') || localId}`,
      `${tcgdexId}-${localId.padStart(3, '0')}`,
    ]);

    const lowFallback = small || large;
    for (const candidate of candidates) {
      stmts.push(
        `UPDATE ptcg_cards SET image_high = COALESCE(image_high, ${escSql(large)}), image_low = COALESCE(image_low, ${escSql(lowFallback)}) WHERE card_id = ${escSql(candidate)} AND lang = 'ja';`,
      );
    }
  }

  if (stmts.length === 0) {
    console.log(`[${tcgdexId}] no updates`);
    continue;
  }
  totalUpdates += stmts.length;

  for (let i = 0, batch = 1; i < stmts.length; i += BATCH_SIZE, batch++) {
    const slice = stmts.slice(i, i + BATCH_SIZE);
    const file = `${BATCH_DIR}/jp_${tcgdexId.replace(/[^a-zA-Z0-9]/g, '_')}_${String(batch).padStart(3, '0')}.sql`;
    writeFileSync(file, slice.join('\n'));
    if (dryRun) {
      console.log(`[${tcgdexId}] wrote ${file} (${slice.length} stmts) [dry-run]`);
      continue;
    }
    console.log(`[${tcgdexId}] executing ${file} (${slice.length} stmts)...`);
    execFileSync(npx, ['wrangler', 'd1', 'execute', DB_NAME, `--file=${file}`, '--remote'], {
      stdio: 'inherit', shell: true,
    });
  }
}

console.log(`\nDone. ${totalUpdates} update statements across ${totalCardsWithImages} priced cards.`);
if (dryRun) console.log('(Dry run — no D1 writes.)');

async function fetchSet(pkmId) {
  const url = `${PKMNBINDR_BASE}/${pkmId}.json`;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': 'opbindr-ptcg-importer/1.0 (+https://opbindr.com)' },
      });
      // pkmnbindr's SPA falls back to index.html for non-existent paths
      // — content-type tells us if we got real data or HTML noise.
      const ct = res.headers.get('content-type') || '';
      if (res.status === 200 && ct.includes('application/json')) {
        return await res.json();
      }
      if (res.status === 404 || !ct.includes('application/json')) {
        console.log(`[${pkmId}] not on pkmnbindr (likely missing pre-SV-era set), skipping`);
        return null;
      }
      console.log(`[${pkmId}] HTTP ${res.status}, retry ${attempt}/3`);
      await sleep(2000 * attempt);
    } catch (err) {
      console.log(`[${pkmId}] fetch error: ${err.message}, retry ${attempt}/3`);
      await sleep(2000 * attempt);
    }
  }
  console.log(`[${pkmId}] exhausted retries, skipping`);
  return null;
}

function extractImageUrls(card) {
  // pkmnbindr's JP cards carry an array of image objects, e.g.
  // images: [{ type: 'front', small: '...', medium: '...', large: '...' }].
  // We pluck the first 'front' (or first entry) and normalize.
  const images = card.images;
  if (!Array.isArray(images) || images.length === 0) return {};
  const front = images.find((i) => i.type === 'front') || images[0];
  return {
    large: front.large || front.medium || front.small || null,
    small: front.small || front.medium || front.large || null,
  };
}

function extractLocalId(card) {
  // pkmnbindr's id is `{setCode}_ja-{number}`. The number AFTER the
  // last hyphen is what we want — splitting on the last hyphen handles
  // any future set ids that happen to contain hyphens themselves.
  const id = String(card.id || '');
  const idx = id.lastIndexOf('-');
  if (idx === -1) return String(card.number ?? '').trim() || null;
  return id.slice(idx + 1) || null;
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
