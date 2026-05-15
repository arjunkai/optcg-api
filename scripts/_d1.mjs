// Shared helpers for the api-key management scripts. Wraps wrangler d1
// in a way that avoids passing SQL through any shell — SQL is written to
// a temp file and read via --file= to keep us clear of shell injection
// even when owner names or notes contain metacharacters.

import { spawnSync } from 'node:child_process';
import { writeFileSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const NPX = process.platform === 'win32' ? 'npx.cmd' : 'npx';

// Run a SQL string against the remote D1 database. Captures wrangler's
// chatter so it doesn't pollute script output; only surfaces it if the
// command fails. Returns stdout for the caller to parse if needed.
export function d1Execute(sql) {
  const tmpFile = join(tmpdir(), `optcg-api-d1-${process.pid}-${Date.now()}.sql`);
  writeFileSync(tmpFile, sql, 'utf8');
  try {
    const args = ['wrangler', 'd1', 'execute', 'optcg-cards', '--remote', `--file=${tmpFile}`];
    // shell:true is required on Windows to spawn .cmd files (Node CVE-2024-27980).
    // There's no injection risk: tmpFile is a system temp path we control,
    // and SQL never goes through the shell — it's in the file we passed via --file=.
    const result = spawnSync(NPX, args, {
      encoding: 'utf8',
      shell: process.platform === 'win32',
    });
    if (result.status !== 0) {
      if (result.stdout) process.stderr.write(result.stdout);
      if (result.stderr) process.stderr.write(result.stderr);
      throw new Error(`wrangler d1 execute exited with status ${result.status}`);
    }
    return result.stdout || '';
  } finally {
    try { unlinkSync(tmpFile); } catch { /* ignore */ }
  }
}

// Run a SELECT query against the remote D1 and parse the returned rows.
// Uses --command= because --file= returns only metadata (rows read /
// written counts) for SELECTs, not the actual row data. The caller is
// responsible for ensuring the SQL contains no untrusted input —
// JSON.stringify guards against shell-level quoting issues but isn't a
// substitute for parameterized queries.
export function d1Query(sql) {
  const args = [
    'wrangler', 'd1', 'execute', 'optcg-cards', '--remote',
    '--command=' + JSON.stringify(sql),
    '--json',
  ];
  const result = spawnSync(NPX, args, {
    encoding: 'utf8',
    shell: process.platform === 'win32',
  });
  if (result.status !== 0) {
    if (result.stdout) process.stderr.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
    throw new Error(`wrangler d1 execute exited with status ${result.status}`);
  }
  const out = result.stdout || '';
  const match = out.match(/\[\s*\{[\s\S]+\}\s*\]\s*$/);
  if (!match) {
    throw new Error('Could not locate JSON block in wrangler output');
  }
  return JSON.parse(match[0])[0]?.results || [];
}

export function sqlLit(s) {
  if (s == null) return 'NULL';
  return "'" + String(s).replace(/'/g, "''") + "'";
}
