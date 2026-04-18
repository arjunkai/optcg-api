/**
 * Update DON image_url in D1 to point at the API proxy route.
 *
 * Every DON row becomes:   image_url = 'https://optcg-api.arjunbansal-ai.workers.dev/images/DON-NNN'
 *
 * The API proxy (src/images.js) handles:
 *  - R2 first (curated PDF images) — highest quality
 *  - TCGPlayer CDN fallback via D1 tcg_ids lookup — for uncurated DONs
 *
 * So curated + uncurated DONs both go through the same URL; upgrades are transparent
 * once more PDFs are uploaded.
 *
 * Usage:
 *   node scripts/update_don_image_urls.js               # run remote update
 *   node scripts/update_don_image_urls.js --dry-run     # print the SQL only
 *   node scripts/update_don_image_urls.js --local       # run against local D1 instead
 */

import { spawnSync } from 'node:child_process';
import { platform } from 'node:os';
import { writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const API_BASE = 'https://optcg-api.arjunbansal-ai.workers.dev';

// Bump IMAGE_VERSION to bust wsrv.nl + browser caches (max-age=31536000)
// when the R2 contents change meaningfully. The ?v=N query is passed
// through by src/images.js as a no-op server-side, but it changes the
// cache key everywhere upstream.
const IMAGE_VERSION = 4;

const SQL = `UPDATE cards SET image_url = '${API_BASE}/images/' || id || '?v=${IMAGE_VERSION}' WHERE category = 'Don';`;

const args = process.argv.slice(2);
const dryRun = args.includes('--dry-run');
const flag = args.includes('--local') ? '--local' : '--remote';

if (dryRun) {
  console.log('[dry-run] Would execute on D1:');
  console.log(SQL);
  process.exit(0);
}

const tmpDir = path.join(__dirname, '.tmp');
mkdirSync(tmpDir, { recursive: true });
const sqlFile = path.join(tmpDir, 'update_don_image_urls.sql');
writeFileSync(sqlFile, SQL, 'utf-8');

console.log(`Executing on D1 (${flag}):`);
console.log(SQL);
console.log();

const result = spawnSync(
  npx,
  ['wrangler', 'd1', 'execute', 'optcg-cards', flag, `--file=${sqlFile}`],
  { stdio: 'inherit', shell: true }
);
process.exit(result.status ?? 1);
