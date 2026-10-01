import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  TIERS, TIER_NAMES, KEY_POLICY, OUTSIDE_UNITS_DAILY,
  tierFor, secondsUntilUtcMidnight,
} from '../src/limits.js';

test('values match the spec', () => {
  assert.deepEqual(TIER_NAMES, ['free', 'standard', 'partner']);
  const pick = (t) => [t.perMinute, t.daily, t.dailyUnits, t.heavyPerMinute];
  assert.deepEqual(pick(TIERS.free), [20, 500, 50000, 2]);
  assert.deepEqual(pick(TIERS.standard), [60, 2000, 200000, 10]);
  assert.deepEqual(pick(TIERS.partner), [120, 10000, 1000000, 30]);
  assert.deepEqual(KEY_POLICY, { free: 0, standard: 10, partner: 2 });
  assert.equal(OUTSIDE_UNITS_DAILY, 1_800_000);
});

test('unknown tiers fall back to free', () => {
  for (const t of [undefined, null, '', 'gold', 'STANDARD', '__proto__', 'constructor', 42]) {
    assert.equal(tierFor(t).name, 'free', String(t));
  }
  assert.equal(tierFor('partner').name, 'partner');
});

test('frozen', () => {
  assert.ok(Object.isFrozen(TIERS) && Object.isFrozen(TIERS.standard) && Object.isFrozen(KEY_POLICY));
});

test('seconds until UTC midnight', () => {
  assert.equal(secondsUntilUtcMidnight(Date.UTC(2026, 9, 1, 12)), 43200);
  assert.equal(secondsUntilUtcMidnight(Date.UTC(2026, 9, 1, 23, 59, 59, 500)), 1);
  assert.equal(secondsUntilUtcMidnight(Date.UTC(2026, 9, 2)), 86400);
});
