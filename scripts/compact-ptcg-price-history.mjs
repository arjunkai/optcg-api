/**
 * compact-ptcg-price-history.mjs — applies migrations/021 to the live D1
 * database in chunks (the migration's single INSERT is far too big for one
 * D1 query at ~5.3M rows).
 *
 *   1. CREATE TABLE ptcg_price_history_compact (WITHOUT ROWID)
 *   2. copy the chart-usable rows over, one rowid range per query
 *      (resumable: progress is kept in scripts/.tmp/compact-progress.json)
 *   3. print old/new row counts, then swap: DROP the old table, RENAME the
 *      compact one, drop the duplicate card_price_history index
 *
 * Cost: reads each old row once plus up to ~11 primary-key seeks per
 * Cardmarket row (~25M rows read in total), and writes ~1 row per kept row
 * (~1M). That's fine on Workers Paid; on the free tier (5M reads / 100k
 * writes per day) it can't finish, and while the database is over the
 * 500 MB cap no INSERT succeeds anyway. `--discard` is the free-tier escape
 * hatch: it drops the whole PTCG price history and starts an empty compact
 * table (charts restart from the next weekly snapshot).
 *
 * Don't run it while the Monday PTCG refresh is running (its snapshot step
 * writes to the old table).
 *
 * Usage:
 *   node scripts/compact-ptcg-price-history.mjs --dry-run   # plan only
 *   node scripts/compact-ptcg-price-history.mjs             # copy + swap
 *   node scripts/compact-ptcg-price-history.mjs --no-swap   # copy, stop before swap
 *   node scripts/compact-ptcg-price-history.mjs --discard   # drop history instead
 *   ... --local [--persist-to=DIR]                          # against a local D1
 *   ... --chunk=N                                           # rowids per query (default 200000)
 */

import { execFileSync } from 'child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';

const DB = 'optcg-cards';
const TMP = 'scripts/.tmp';
const PROGRESS = `${TMP}/compact-progress.json`;
const args = new Set(process.argv.slice(2));
const argValue = (name) => process.argv.find((a) => a.startsWith(`--${name}=`))?.split('=').slice(1).join('=');
const CHUNK = Number(argValue('chunk')) || 200_000;
const TARGET = args.has('--local')
  ? ['--local', ...(argValue('persist-to') ? [`--persist-to=${argValue('persist-to')}`] : [])]
  : ['--remote'];

// Statements come from the migration so the two can't drift apart.
const migration = readFileSync('migrations/021_compact_ptcg_price_history.sql', 'utf8')
  .split('\n').filter((l) => !l.trimStart().startsWith('--')).join('\n');
function stmt(prefix) {
  const found = migration.split(';').map((s) => s.trim()).find((s) => s.startsWith(prefix));
  if (!found) throw new Error(`021 has no statement starting "${prefix}"`);
  return found;
}
const CREATE = stmt('CREATE TABLE ptcg_price_history_compact').replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS');
// Its WHERE is one parenthesised condition, so a rowid range can be ANDed on.
const COPY = stmt('INSERT OR IGNORE INTO ptcg_price_history_compact');
const SWAP = [
  'DROP TABLE ptcg_price_history',
  'ALTER TABLE ptcg_price_history_compact RENAME TO ptcg_price_history',
  'DROP INDEX IF EXISTS idx_price_history_card_time',
].join(';\n') + ';';

// Runs SQL and returns the per-statement results with their meta. Calls
// wrangler's JS entry directly (no shell), so the SQL reaches it as one argv
// entry and needs no quoting.
function d1(sql) {
  const out = execFileSync(process.execPath, [
    'node_modules/wrangler/bin/wrangler.js', 'd1', 'execute', DB, ...TARGET, '--json', '--command', sql,
  ], { encoding: 'utf8', maxBuffer: 1 << 26, stdio: ['ignore', 'pipe', 'pipe'] });
  return JSON.parse(out.slice(out.indexOf('[')));
}
const one = (sql) => d1(sql)[0].results[0];

if (args.has('--dry-run')) {
  console.log(`target: ${TARGET.join(' ')}, chunk: ${CHUNK} rowids\n\n-- create\n${CREATE};\n\n-- copy, per chunk\n${COPY}\n  AND h.rowid > :lo AND h.rowid <= :hi;\n\n-- swap\n${SWAP}`);
  process.exit(0);
}

if (args.has('--discard')) {
  console.log('Dropping all PTCG price history and creating an empty compact table...');
  d1([
    'DROP TABLE IF EXISTS ptcg_price_history_compact',
    'DROP TABLE ptcg_price_history',
    CREATE.replace(/ptcg_price_history_compact/, 'ptcg_price_history'),
    'DROP INDEX IF EXISTS idx_price_history_card_time',
  ].join(';\n') + ';');
  console.log('Done. Charts fill in again from the next weekly snapshot.');
  process.exit(0);
}

mkdirSync(TMP, { recursive: true });
const progress = existsSync(PROGRESS) ? JSON.parse(readFileSync(PROGRESS, 'utf8')) : { lo: 0 };
if (progress.done) {
  console.log(`Already compacted (${PROGRESS} says done). Delete that file to run again.`);
  process.exit(0);
}

d1(CREATE + ';');
const { max } = one('SELECT max(rowid) AS max FROM ptcg_price_history;');
let lo = progress.lo;
console.log(`old table rowids 1..${max}; copying from ${lo} in chunks of ${CHUNK}`);

while (lo < max) {
  const hi = Math.min(lo + CHUNK, max);
  const [res] = d1(`${COPY}\n  AND h.rowid > ${lo} AND h.rowid <= ${hi};`);
  console.log(`  rowid ${lo + 1}..${hi}: read ${res.meta?.rows_read ?? '?'}, wrote ${res.meta?.rows_written ?? '?'}`);
  lo = hi;
  writeFileSync(PROGRESS, JSON.stringify({ lo }));
}

const counts = one(
  'SELECT (SELECT count(*) FROM ptcg_price_history) AS old_rows, (SELECT count(*) FROM ptcg_price_history_compact) AS new_rows;',
);
console.log(`old table ${counts.old_rows} rows -> compact ${counts.new_rows} rows`);

if (args.has('--no-swap')) {
  console.log('Stopped before the swap (--no-swap). Re-run without it to swap.');
  process.exit(0);
}
d1(SWAP);
writeFileSync(PROGRESS, JSON.stringify({ lo: 0, done: true }));
console.log('Swapped. ptcg_price_history is now the compact table.');
