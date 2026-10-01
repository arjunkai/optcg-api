#!/usr/bin/env node
/**
 * build-snapshots.mjs — builds the R2 snapshots the bulk routes serve
 * (src/snapshotDefs.js, src/snapshot.js). The Worker never builds them: on
 * Workers Free a build (full-table D1 read + JSON.stringify of up to ~57 MB)
 * can't fit in the 10 ms CPU limit.
 *
 * Reads D1 through the D1 REST query API (no Worker requests), builds each
 * body in Node, and uploads snapshots/{name}.json and snapshots/lkg/{name}.json
 * with `wrangler r2 object put`. A full build reads ~100k D1 rows.
 *
 *   node scripts/build-snapshots.mjs [--only a,b] [--sqlite FILE] [--out DIR | --local]
 *
 *   --only    group (optcg | canvs | pokemon), exact name, or prefix* (default: all)
 *   --sqlite  read a local SQLite file instead of production D1
 *   --out     write {name}.json files to DIR instead of uploading
 *   --local   upload to wrangler's local R2 (for `wrangler dev`) instead of production
 *
 * Production access (reading D1 without --sqlite, or uploading without --out /
 * --local) needs OPTCG_ALLOW_REMOTE=1 plus CLOUDFLARE_API_TOKEN and
 * CLOUDFLARE_ACCOUNT_ID. The workflows set them; automated agents must not.
 */

import { spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, unlinkSync, appendFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { selectSnapshots, buildSnapshotBody } from '../src/snapshotDefs.js';

const BUCKET = 'optcg-images';
const NPX = process.platform === 'win32' ? 'npx.cmd' : 'npx';

export function parseArgs(argv) {
  const opts = { only: [], sqlite: null, out: null, local: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const value = () => {
      const v = argv[++i];
      if (v === undefined || v.startsWith('--')) throw new Error(`${a} needs a value`);
      return v;
    };
    if (a === '--only') opts.only.push(...value().split(',').map((s) => s.trim()).filter(Boolean));
    else if (a === '--sqlite') opts.sqlite = value();
    else if (a === '--out') opts.out = value();
    else if (a === '--local') opts.local = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (opts.out && opts.local) throw new Error('--out and --local are exclusive');
  return opts;
}

export function needsRemote(opts) {
  return !opts.sqlite || (!opts.out && !opts.local);
}

function assertRemoteAllowed() {
  if (process.env.NODE_TEST_CONTEXT || process.env.OPTCG_ALLOW_REMOTE !== '1') {
    throw new Error(
      'Refusing to touch production (D1 reads or R2 uploads). Set OPTCG_ALLOW_REMOTE=1 ' +
      '(the workflows do), or use --sqlite with --out/--local. Automated agents must not set it.'
    );
  }
  for (const v of ['CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID']) {
    if (!process.env[v]) throw new Error(`${v} is not set`);
  }
}

function databaseId() {
  const toml = readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
  const m = toml.match(/database_id\s*=\s*"([^"]+)"/);
  if (!m) throw new Error('database_id not found in wrangler.toml');
  return m[1];
}

// query(sql, params) against production D1 over REST, counting rows read.
function remoteQuery(stats) {
  const url = `https://api.cloudflare.com/client/v4/accounts/${process.env.CLOUDFLARE_ACCOUNT_ID}` +
    `/d1/database/${databaseId()}/query`;
  return async (sql, params) => {
    for (let attempt = 1; ; attempt++) {
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ sql, params }),
        });
        const body = await res.json().catch(() => null);
        if (!res.ok || !body?.success) {
          const msg = body?.errors?.map((e) => e.message).join('; ') || `HTTP ${res.status}`;
          throw Object.assign(new Error(`D1 query failed: ${msg}`), { retry: res.status >= 500 || res.status === 429 });
        }
        const result = body.result[0];
        stats.rowsRead += result.meta?.rows_read ?? 0;
        return result.results;
      } catch (err) {
        if (attempt >= 4 || err.retry === false) throw err;
        await new Promise((r) => setTimeout(r, 2000 * attempt));
      }
    }
  };
}

async function sqliteQuery(file) {
  const { DatabaseSync } = await import('node:sqlite');
  const db = new DatabaseSync(file, { readOnly: true });
  return async (sql, params) => db.prepare(sql).all(...params);
}

function r2Put(key, file, local) {
  const args = ['wrangler', 'r2', 'object', 'put', `${BUCKET}/${key}`, `--file=${file}`,
    '--content-type=application/json', local ? '--local' : '--remote'];
  const r = spawnSync(NPX, args, { encoding: 'utf8', shell: process.platform === 'win32' });
  if (r.status !== 0) {
    throw new Error(`wrangler r2 object put ${key} failed: ${(r.stderr || r.stdout || '').trim().split('\n').pop()}`);
  }
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (needsRemote(opts)) assertRemoteAllowed();
  const defs = selectSnapshots(opts.only);
  const stats = { rowsRead: 0 };
  const query = opts.sqlite ? await sqliteQuery(opts.sqlite) : remoteQuery(stats);
  if (opts.out) mkdirSync(opts.out, { recursive: true });

  const lines = [];
  for (const def of defs) {
    const t0 = Date.now();
    const body = await buildSnapshotBody(def, query);
    const mb = (Buffer.byteLength(body) / 1e6).toFixed(1);
    if (opts.out) {
      writeFileSync(join(opts.out, `${def.name}.json`), body);
    } else {
      const tmp = join(tmpdir(), `snapshot-${process.pid}-${def.name}.json`);
      writeFileSync(tmp, body);
      try {
        r2Put(`snapshots/${def.name}.json`, tmp, opts.local);
        r2Put(`snapshots/lkg/${def.name}.json`, tmp, opts.local);
      } finally {
        try { unlinkSync(tmp); } catch { /* ignore */ }
      }
    }
    const line = `${def.name}: ${mb} MB in ${((Date.now() - t0) / 1000).toFixed(1)}s`;
    console.log(line);
    lines.push(line);
  }
  const total = `built ${defs.length} snapshot(s)` + (opts.sqlite ? '' : `, D1 rows read: ${stats.rowsRead}`);
  console.log(total);
  if (process.env.GITHUB_STEP_SUMMARY) {
    appendFileSync(process.env.GITHUB_STEP_SUMMARY, `## Snapshots\n${total}\n\n${lines.map((l) => `- ${l}`).join('\n')}\n`);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err.message || err);
    process.exit(1);
  });
}
