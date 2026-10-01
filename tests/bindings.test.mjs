import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { TIERS } from '../src/limits.js';

const toml = readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
function bindings() {
  const out = {};
  for (const block of toml.split('[[unsafe.bindings]]').slice(1)) {
    const name = block.match(/name = "([^"]+)"/)?.[1];
    out[name] = {
      limit: Number(block.match(/limit = (\d+)/)?.[1]),
      period: Number(block.match(/period = (\d+)/)?.[1]),
      ns: block.match(/namespace_id = "(\d+)"/)?.[1],
    };
  }
  return out;
}

test('tier bindings match TIERS; browser bindings unchanged', () => {
  const b = bindings();
  for (const [name, t] of Object.entries(TIERS)) {
    assert.deepEqual([b[t.minuteBinding]?.limit, b[t.minuteBinding]?.period], [t.perMinute, 60], `${name} minute`);
    assert.deepEqual([b[t.heavyBinding]?.limit, b[t.heavyBinding]?.period], [t.heavyPerMinute, 60], `${name} heavy`);
  }
  assert.deepEqual([b.RL_KEY_LOOKUP?.limit, b.RL_KEY_LOOKUP?.period], [30, 60]);
  assert.deepEqual([b.RL_IP?.limit, b.RL_IP_HEAVY?.limit], [1500, 60]);
});

test('namespace ids are unique', () => {
  const ids = Object.values(bindings()).map((x) => x.ns);
  assert.equal(new Set(ids).size, ids.length);
});
