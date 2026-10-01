// The production guard. Nothing here can reach D1: the guard throws first,
// and remote.mjs refuses without an interactive terminal.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

delete process.env.OPTCG_ALLOW_REMOTE;
const { d1Execute, d1Query } = await import('../scripts/_d1.mjs');

test('d1Execute and d1Query refuse without OPTCG_ALLOW_REMOTE=1', () => {
  assert.throws(() => d1Execute('SELECT 1'), /Refusing to touch the production database/);
  assert.throws(() => d1Query('SELECT 1'), /Refusing to touch the production database/);
  for (const v of ['true', 'yes', '0', '']) {
    process.env.OPTCG_ALLOW_REMOTE = v;
    assert.throws(() => d1Execute('SELECT 1'), /Refusing/, v);
  }
  delete process.env.OPTCG_ALLOW_REMOTE;
});

test('remote.mjs refuses without an interactive terminal', () => {
  const env = { ...process.env };
  delete env.OPTCG_ALLOW_REMOTE;
  const r = spawnSync(process.execPath, ['scripts/remote.mjs', 'scripts/list-keys.mjs'], {
    encoding: 'utf8', input: 'prod\n', env,
  });
  assert.equal(r.status, 1);
  assert.match(r.stderr, /interactive terminal/);
  assert.match(r.stderr, /must never/);
});
