// The Free plan's shared daily allowances, and proof that API-key limits keep
// outside keys inside a fixed slice. Browser-path (Origin) traffic isn't
// bounded here; that's Phase 2. Fails the build if a limit grows past what fits.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TIERS, KEY_POLICY, OUTSIDE_UNITS_DAILY } from '../src/limits.js';
import { MIN_CHARGED_UNITS } from '../src/edgeCache.js';

const FREE = { requests: 100_000, rowsRead: 5_000_000, rowsWritten: 100_000 };
const FIRST_PARTY = { requests: 10_000, rowsRead: 1_000_000 };   // dashboard peak, Sep 2026
const ISOLATES_PER_KEY = 5;    // isolates serving one key in a day (generous for one client)
const ISOLATES = 20;           // isolates the gate runs in per day, for key lookups
const FIVE_MIN = 288;          // five-minute windows per day
const KEYS = Object.values(KEY_POLICY).reduce((a, b) => a + b, 0);
const sum = (f) => Object.entries(KEY_POLICY).reduce((n, [t, count]) => n + count * f(TIERS[t]), 0);

test('every allowed key at its daily cap uses at most 40% of requests', () => {
  assert.ok(sum((t) => t.daily) <= 0.4 * FREE.requests, `${sum((t) => t.daily)}`);
});

test('no single key can use the whole outside units share', () => {
  for (const t of Object.values(TIERS)) assert.ok(t.dailyUnits < OUTSIDE_UNITS_DAILY);
});

test('reads: outside share + uncharged light misses + 2x first-party + limiter reads fit in 90% of 5M', () => {
  const limiterReads = KEYS * ISOLATES * FIVE_MIN * 2; // key lookups + request re-reads, 5-min each
  // Light routes (single card, set lists) aren't charged; a key can force misses
  // (e.g. junk ?lang=) at ~8 rows each, bounded by its daily request cap.
  const lightMissReads = sum((t) => t.daily) * 8;
  const total = OUTSIDE_UNITS_DAILY + lightMissReads + 2 * FIRST_PARTY.rowsRead + limiterReads;
  assert.ok(total <= 0.9 * FREE.rowsRead, `${total}`);
});

test('limiter reads alone stay under 3% of 5M', () => {
  assert.ok(KEYS * ISOLATES * FIVE_MIN * 2 < 0.03 * FREE.rowsRead);
});

test('worst-case limiter writes stay under 30% of 100k', () => {
  const keyCharges = 2 * (OUTSIDE_UNITS_DAILY / MIN_CHARGED_UNITS) + KEYS * ISOLATES;      // key + outside row per charged miss, + each isolate writes once after the cap
  const requestFlushes = Math.min(sum((t) => t.daily),                          // each flush carries >= 1 request
    sum((t) => t.daily) / 25 + KEYS * ISOLATES_PER_KEY * FIVE_MIN);             // batches + 5-minute time flushes
  const lastUsed = KEYS * ISOLATES * 4;                                         // once per 6h per key per colo
  const total = keyCharges + requestFlushes + lastUsed;
  assert.ok(total < 0.3 * FREE.rowsWritten, `${Math.round(total)}`);
});
