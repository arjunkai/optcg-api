/**
 * Upload curated DON PDF images to R2 bucket `optcg-images` under `cards/DON-NNN.png`.
 *
 * Input: data/don_image_mapping.json  — { "DON-004": "don_050.png", ... }
 *        data/don_images/*.png         — extracted PDF images
 *
 * Uploads via `npx wrangler r2 object put`. Existing objects are overwritten.
 * Run after curate_don_images.html has been used to produce the mapping.
 *
 * Usage:
 *   node scripts/upload_don_images_r2.js              # upload all
 *   node scripts/upload_don_images_r2.js --dry-run    # list what would upload
 *   node scripts/upload_don_images_r2.js DON-004 DON-005  # only specific ids
 */

import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { platform } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const npx = platform() === 'win32' ? 'npx.cmd' : 'npx';
const BUCKET = 'optcg-images';
const MAPPING_PATH = path.join(__dirname, '..', 'data', 'don_image_mapping.json');
const IMAGES_DIR = path.join(__dirname, '..', 'data', 'don_images');

if (!existsSync(MAPPING_PATH)) {
  console.error(`Missing mapping file: ${MAPPING_PATH}`);
  console.error('Run scripts/curate_don_images.html first and export the mapping.');
  process.exit(1);
}

const args = process.argv.slice(2);
const dryRun = args.includes('--dry-run');
const ids = args.filter(a => !a.startsWith('--'));

const mapping = JSON.parse(readFileSync(MAPPING_PATH, 'utf-8'));
const filterFn = ids.length ? (id) => ids.includes(id) : () => true;
const entries = Object.entries(mapping).filter(([id]) => filterFn(id));

console.log(`Mapping has ${Object.keys(mapping).length} entries; uploading ${entries.length}${dryRun ? ' (dry-run)' : ''}`);

let ok = 0;
let fail = 0;
for (const [donId, filename] of entries) {
  const src = path.join(IMAGES_DIR, filename);
  if (!existsSync(src)) {
    console.error(`  SKIP ${donId}: missing ${src}`);
    fail++;
    continue;
  }
  const key = `cards/${donId}.png`;
  if (dryRun) {
    console.log(`  ${donId}  <-  ${filename}  ->  r2://${BUCKET}/${key}`);
    ok++;
    continue;
  }

  const result = spawnSync(
    npx,
    ['wrangler', 'r2', 'object', 'put', `${BUCKET}/${key}`,
     '--file', src,
     '--content-type', 'image/png',
     '--remote'],
    { stdio: 'inherit', shell: true }
  );
  if (result.status === 0) {
    console.log(`  OK   ${donId}  <-  ${filename}`);
    ok++;
  } else {
    console.error(`  FAIL ${donId}  <-  ${filename}`);
    fail++;
  }
}

console.log(`\nDone: ${ok} ok, ${fail} failed`);
if (fail > 0) process.exit(1);
