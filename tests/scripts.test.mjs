// Validation happens before any D1 call, and without OPTCG_ALLOW_REMOTE the
// D1 helper refuses anyway (Task 1). Nothing here can reach production.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

const env = { ...process.env };
delete env.OPTCG_ALLOW_REMOTE;
const run = (script, args) => spawnSync(process.execPath, [script, ...args], { encoding: 'utf8', env });

test('issue-key rejects an unknown tier', () => {
  const r = run('scripts/issue-key.mjs', ['--owner', 'Test', '--tier', 'gold']);
  assert.equal(r.status, 1);
  assert.match(r.stderr, /Invalid tier: "gold". Valid tiers: free, standard, partner/);
});

test('issue-key accepts the firstparty scope in validation, then refuses production', () => {
  const r = run('scripts/issue-key.mjs', ['--owner', 'Bot', '--scopes', 'optcg,ptcg,firstparty']);
  assert.notEqual(r.status, 0);
  assert.doesNotMatch(r.stderr, /Invalid scope/);
  assert.match(r.stderr + r.stdout, /Refusing to touch the production database/);
});

test('set-tier validates, then refuses production', () => {
  assert.match(run('scripts/set-tier.mjs', []).stderr, /usage/);
  assert.match(run('scripts/set-tier.mjs', ['--prefix', 'abc', '--tier', 'free']).stderr, /Invalid prefix: expected opt_ followed by 8 characters/);
  const inj = run('scripts/set-tier.mjs', ['--prefix', 'opt_x" & calc & "', '--tier', 'partner']);
  assert.equal(inj.status, 1);
  assert.match(inj.stderr, /Invalid prefix/);
  assert.match(run('scripts/set-tier.mjs', ['--prefix', 'opt_aaaaaaaa', '--tier', 'gold']).stderr, /Invalid tier/);
  assert.match(run('scripts/set-tier.mjs', ['--prefix', 'opt_aaaaaaaa', '--tier', 'partner']).stderr, /Refusing/);
});
