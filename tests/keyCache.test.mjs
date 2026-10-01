import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKeyCache } from '../src/keyCache.js';
import { makeClock } from './helpers/fakes.mjs';

const ROW = { key_prefix: 'opt_aaaaaaaa', tier: 'standard', scopes: 'optcg' };

test('caches found and not-found for 5 minutes by default', () => {
  const clock = makeClock('2026-10-01T00:00:00Z');
  const c = createKeyCache({ now: clock.now });
  c.set('h1', ROW);
  c.set('h2', null);
  assert.deepEqual(c.get('h1'), { row: ROW });
  assert.deepEqual(c.get('h2'), { row: null });
  clock.advance(299_999);
  assert.ok(c.get('h1'));
  clock.advance(1);
  assert.equal(c.get('h1'), undefined);
  assert.equal(c.get('h2'), undefined);
});

test('evicts oldest past max; re-set refreshes', () => {
  const c = createKeyCache({ max: 2 });
  c.set('a', null); c.set('b', null); c.set('a', ROW); c.set('c', null);
  assert.equal(c.size, 2);
  assert.ok(c.get('a'));
  assert.equal(c.get('b'), undefined);
});
