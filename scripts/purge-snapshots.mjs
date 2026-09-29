/**
 * purge-snapshots.mjs — deletes the R2 snapshots behind the bulk index routes
 * (see src/snapshot.js) so a fresh D1 import is visible right away instead of
 * after the 6h snapshot TTL. The next request per route rebuilds its snapshot
 * once from D1. Edge-cached copies still age out within 1h per colo.
 *
 * Run as the last step of the weekly workflows (needs CLOUDFLARE_API_TOKEN +
 * CLOUDFLARE_ACCOUNT_ID with R2 edit access). Deleting a missing object is a
 * no-op, so this is safe to run any time.
 *
 * Keep NAMES in sync with the serveSnapshot() names in src/cards.js and
 * src/pokemon/cards.js — a name there that's missing here just means that
 * snapshot waits out its TTL.
 */

import { execFileSync } from 'child_process';
import { platform } from 'os';

const BUCKET = 'optcg-images';
const PTCG_LANGS = ['en', 'ja', 'zh-cn', 'zh-tw'];
const NAMES = [
  'cards-all-v1',
  'cards-index-v1',
  ...PTCG_LANGS.map((l) => `pokemon-index-${l}-v8`),
  ...PTCG_LANGS.map((l) => `pokemon-all-${l}-v1`),
];

let failed = 0;
for (const name of NAMES) {
  const key = `${BUCKET}/snapshots/${name}.json`;
  try {
    execFileSync('npx', ['wrangler', 'r2', 'object', 'delete', key, '--remote'], {
      stdio: ['ignore', 'ignore', 'pipe'],
      shell: platform() === 'win32',
    });
    console.log(`deleted ${key}`);
  } catch (err) {
    failed++;
    console.error(`failed  ${key}: ${String(err.stderr || err.message).trim().split('\n').pop()}`);
  }
}
if (failed) process.exitCode = 1;
