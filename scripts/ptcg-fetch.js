/**
 * ptcg-fetch.js — fetches Pokémon TCG data from TCGdex and caches to disk.
 *
 * TCGdex API: https://tcgdex.dev — public, no key required, no documented
 * rate limit. We still cap concurrency to be polite.
 *
 * Output:
 *   data/ptcg_cache/sets-{lang}.json   — array of full set objects
 *   data/ptcg_cache/cards-{lang}.json  — { [card_id]: full card object }
 *
 * Resumable: re-running picks up where it left off (existing cards-{lang}.json
 * is loaded; only missing cards are fetched). Periodic flush every 200 cards
 * so a crash doesn't lose progress.
 *
 * Usage:
 *   node scripts/ptcg-fetch.js                    # all 4 langs
 *   node scripts/ptcg-fetch.js --lang=en          # single language
 *   node scripts/ptcg-fetch.js --lang=en --set=sv01   # single set (testing)
 *   node scripts/ptcg-fetch.js --concurrency=4    # default 8
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';

const ALL_LANGS = ['en', 'ja', 'zh-cn', 'zh-tw'];
const CACHE_DIR = 'data/ptcg_cache';
const FLUSH_EVERY = 200;
const RETRY_LIMIT = 3;
const RETRY_DELAY_MS = 800;

const args = parseArgs(process.argv.slice(2));
const langs = args.lang ? [args.lang] : ALL_LANGS;
const setFilter = args.set || null;
const concurrency = parseInt(args.concurrency || '8', 10);

if (!existsSync(CACHE_DIR)) mkdirSync(CACHE_DIR, { recursive: true });

for (const lang of langs) {
  await fetchLanguage(lang, { setFilter, concurrency });
}

async function fetchLanguage(lang, { setFilter, concurrency }) {
  console.log(`\n[${lang}] starting fetch (concurrency=${concurrency}${setFilter ? `, set=${setFilter}` : ''})`);

  const setsPath = `${CACHE_DIR}/sets-${lang}.json`;
  const cardsPath = `${CACHE_DIR}/cards-${lang}.json`;

  const cardsCache = existsSync(cardsPath) ? JSON.parse(readFileSync(cardsPath, 'utf-8')) : {};

  // Step 1: fetch sets list (lightweight summaries).
  const setsList = await fetchJson(`https://api.tcgdex.net/v2/${lang}/sets`);
  console.log(`[${lang}] ${setsList.length} sets`);

  // Step 2: fetch full set object per set (gives card summaries + set metadata).
  const fullSets = [];
  for (const summary of setsList) {
    if (setFilter && summary.id !== setFilter) continue;
    const fullSet = await fetchJson(`https://api.tcgdex.net/v2/${lang}/sets/${encodeURIComponent(summary.id)}`);
    fullSets.push(fullSet);
  }
  writeFileSync(setsPath, JSON.stringify(fullSets, null, 2));
  console.log(`[${lang}] wrote ${fullSets.length} sets → ${setsPath}`);

  // Step 3: collect cards we still need to fetch.
  const wanted = [];
  for (const set of fullSets) {
    for (const summary of set.cards || []) {
      if (cardsCache[summary.id]) continue;
      wanted.push(summary.id);
    }
  }
  console.log(`[${lang}] ${wanted.length} cards to fetch (${Object.keys(cardsCache).length} already cached)`);

  if (wanted.length === 0) return;

  // Step 4: fetch cards with bounded concurrency.
  let completed = 0;
  let sinceFlush = 0;
  const start = Date.now();

  await runWithConcurrency(wanted, concurrency, async (cardId) => {
    try {
      const card = await fetchJson(`https://api.tcgdex.net/v2/${lang}/cards/${encodeURIComponent(cardId)}`);
      cardsCache[cardId] = card;
    } catch (err) {
      // 404 happens for prerelease IDs that vanish; log and skip.
      console.warn(`[${lang}] skip ${cardId}: ${err.message}`);
    }
    completed++;
    sinceFlush++;
    if (sinceFlush >= FLUSH_EVERY) {
      writeFileSync(cardsPath, JSON.stringify(cardsCache));
      const rate = Math.round((completed / ((Date.now() - start) / 1000)) * 10) / 10;
      console.log(`[${lang}] flushed at ${completed}/${wanted.length} (${rate}/s)`);
      sinceFlush = 0;
    }
  });

  writeFileSync(cardsPath, JSON.stringify(cardsCache));
  console.log(`[${lang}] done. ${Object.keys(cardsCache).length} cards in cache → ${cardsPath}`);
}

async function fetchJson(url) {
  let lastErr;
  for (let attempt = 1; attempt <= RETRY_LIMIT; attempt++) {
    try {
      const r = await fetch(url, { headers: { 'User-Agent': 'opbindr-ptcg-import/1.0' } });
      if (r.status === 404) throw new Error('404');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (err) {
      lastErr = err;
      if (err.message === '404') throw err;
      if (attempt < RETRY_LIMIT) await sleep(RETRY_DELAY_MS * attempt);
    }
  }
  throw new Error(`fetch failed after ${RETRY_LIMIT} attempts: ${url} (${lastErr.message})`);
}

async function runWithConcurrency(items, limit, worker) {
  const queue = items.slice();
  async function pump() {
    while (queue.length) {
      const next = queue.shift();
      await worker(next);
    }
  }
  const workers = Array.from({ length: Math.min(limit, items.length) }, pump);
  await Promise.all(workers);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function parseArgs(argv) {
  const out = {};
  for (const arg of argv) {
    const m = arg.match(/^--([^=]+)(?:=(.*))?$/);
    if (m) out[m[1]] = m[2] ?? 'true';
  }
  return out;
}
