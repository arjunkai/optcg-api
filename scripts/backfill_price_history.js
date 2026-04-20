/**
 * backfill_price_history.js — one-time backfill of card_price_history from
 * TCGPlayer's public price-history JSON endpoint.
 *
 * Endpoint: infinite-api.tcgplayer.com/price/history/{tcg_id}/detailed?range=year
 * Returns ~122 points per card across the last year, bucketed every 3 days,
 * at Near Mint condition (matches our cards.price semantics).
 *
 * ⚠ STATUS: not currently in use. The endpoint rate-limits aggressively at
 * the AWS ELB layer — a single IP gets 403'd after ~10 requests within a
 * short window. Running this for all ~4500 cards would either take 30+
 * hours at safe pacing or risk an IP ban that affects the weekly TCGPlayer
 * set-page scrape (which shares the same IP). We shipped the forward-only
 * weekly snapshot pipeline instead; charts populate over time.
 *
 * If you revisit:
 *   • Pace at ≥ 30s per request from a fresh IP, OR
 *   • Drive it through Playwright with real browser context to inherit
 *     whatever bot-protection tokens the TCGPlayer SPA sets, OR
 *   • Scope to a curated subset (e.g., price > $5) where 2-3 hours is OK.
 *
 * Flow:
 *   1. Read (id, tcg_ids) from the live API (wrangler d1 --json --file only
 *      returns meta, so the API is the easier read path).
 *   2. For each tcg_id, fetch the history JSON (SLEEP_MS between calls).
 *   3. Write INSERT OR IGNORE batches to scripts/history_batches/ and execute
 *      them against D1 via wrangler.
 *
 * Usage:
 *   node scripts/backfill_price_history.js             # remote + execute
 *   node scripts/backfill_price_history.js --dry-run   # write SQL only
 *   node scripts/backfill_price_history.js --local     # local D1
 *   node scripts/backfill_price_history.js --limit 10  # smoke test
 */

import { writeFileSync, mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { platform } from 'os';

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';

const DRY = process.argv.includes('--dry-run');
const LOCAL = process.argv.includes('--local');
const TARGET_FLAG = LOCAL ? '--local' : '--remote';
const limitArg = process.argv.indexOf('--limit');
const LIMIT = limitArg !== -1 ? parseInt(process.argv[limitArg + 1], 10) : null;

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36';
const SLEEP_MS = 500;
const BATCH_SIZE = 900;
const HISTORY_ENDPOINT = (id) => `https://infinite-api.tcgplayer.com/price/history/${id}/detailed?range=year`;

function escSql(val) {
  if (val === null || val === undefined) return 'NULL';
  if (typeof val === 'number') return String(val);
  return `'${String(val).replace(/'/g, "''")}'`;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function fetchCardList() {
  // Use the live API — wrangler d1 execute --json with --file only returns
  // meta, not row data, so CLI-based reads aren't viable. The API exposes
  // tcg_ids on every card in /cards results.
  const base = LOCAL
    ? 'http://127.0.0.1:8787'
    : 'https://optcg-api.arjunbansal-ai.workers.dev';
  const pageSize = 500;
  const cards = [];
  let page = 1;
  while (true) {
    const res = await fetch(`${base}/cards?page=${page}&page_size=${pageSize}`, {
      headers: { 'User-Agent': UA },
    });
    if (!res.ok) throw new Error(`card list page ${page}: HTTP ${res.status}`);
    const json = await res.json();
    for (const c of json.data) {
      if (!c.tcg_ids || !Array.isArray(c.tcg_ids) || c.tcg_ids.length === 0) continue;
      cards.push({ id: c.id, tcgId: c.tcg_ids[0] });
    }
    if (json.data.length < pageSize) break;
    page += 1;
  }
  return cards;
}

function extractPoints(json) {
  // Structure: { result: [{ condition: 'Near Mint', buckets: [{ marketPrice, bucketStartDate, ... }] }] }
  const result = json?.result ?? [];
  const nm = result.find((r) => r.condition === 'Near Mint') || result[0];
  if (!nm?.buckets) return [];
  const points = [];
  for (const b of nm.buckets) {
    const price = parseFloat(b.marketPrice);
    if (!Number.isFinite(price) || price <= 0) continue;
    // bucketStartDate is YYYY-MM-DD — anchor to UTC midnight so the same day
    // import from different timezones stays deduplicated by the PK.
    const ts = Math.floor(Date.parse(`${b.bucketStartDate}T00:00:00Z`) / 1000);
    if (!Number.isFinite(ts)) continue;
    points.push({ price, capturedAt: ts });
  }
  return points;
}

async function main() {
  console.log(`Fetching card list from D1 (${TARGET_FLAG})...`);
  let cards = await fetchCardList();
  console.log(`  ${cards.length} cards with tcg_ids`);
  if (LIMIT) {
    cards = cards.slice(0, LIMIT);
    console.log(`  limited to ${cards.length} for smoke test`);
  }

  const allLines = [];
  let fetched = 0;
  let skipped = 0;
  let pointsTotal = 0;

  for (const card of cards) {
    fetched += 1;
    try {
      const res = await fetch(HISTORY_ENDPOINT(card.tcgId), {
        headers: { 'User-Agent': UA, 'Accept': 'application/json' },
      });
      if (!res.ok) {
        if (res.status === 404) { skipped += 1; }
        else { console.warn(`  [${card.id}] HTTP ${res.status}`); skipped += 1; }
        if (fetched % 50 === 0) console.log(`  ${fetched}/${cards.length}...`);
        await sleep(SLEEP_MS);
        continue;
      }
      const json = await res.json();
      const points = extractPoints(json);
      for (const p of points) {
        allLines.push(
          `INSERT OR IGNORE INTO card_price_history (card_id, price, captured_at) VALUES (${escSql(card.id)}, ${p.price}, ${p.capturedAt});`
        );
      }
      pointsTotal += points.length;
    } catch (err) {
      console.warn(`  [${card.id}] ${err.message}`);
      skipped += 1;
    }

    if (fetched % 50 === 0) {
      console.log(`  ${fetched}/${cards.length} (points: ${pointsTotal}, skipped: ${skipped})`);
    }

    // Be polite — 500ms between requests keeps us under any reasonable
    // per-client rate limit and avoids triggering Cloudflare challenges.
    await sleep(SLEEP_MS);
  }

  console.log(`\nDone fetching. Points: ${pointsTotal}, skipped: ${skipped}, total INSERTs: ${allLines.length}`);

  if (allLines.length === 0) {
    console.log('Nothing to write.');
    return;
  }

  mkdirSync('scripts/history_batches', { recursive: true });
  const batches = [];
  for (let i = 0; i < allLines.length; i += BATCH_SIZE) {
    batches.push(allLines.slice(i, i + BATCH_SIZE));
  }
  console.log(`Writing ${batches.length} batches of up to ${BATCH_SIZE}...`);

  for (let i = 0; i < batches.length; i++) {
    const file = `scripts/history_batches/batch_${i + 1}.sql`;
    writeFileSync(file, batches[i].join('\n'), 'utf-8');
    if (DRY) {
      console.log(`  [dry] ${file} (${batches[i].length})`);
      continue;
    }
    console.log(`Executing batch ${i + 1}/${batches.length} (${batches[i].length})...`);
    execFileSync(npx, ['wrangler', 'd1', 'execute', 'optcg-cards', `--file=${file}`, TARGET_FLAG], {
      stdio: 'inherit',
      shell: true,
    });
  }

  console.log('Backfill complete.');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
