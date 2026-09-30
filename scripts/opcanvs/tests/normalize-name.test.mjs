// scripts/opcanvs/tests/normalize-name.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeName } from '../lib/normalize-name.mjs';

test('strips periods used as separators (card style)', () => {
  assert.equal(normalizeName('Monkey.D.Luffy'), 'monkey d luffy');
});
test('matches api-onepiece spacing style', () => {
  assert.equal(normalizeName('Monkey D Luffy'), 'monkey d luffy');
});
test('handles double-name + period', () => {
  assert.equal(normalizeName('Tony Tony.Chopper'), 'tony tony chopper');
});
test('collapses whitespace and lowercases', () => {
  assert.equal(normalizeName('  Nico   Robin '), 'nico robin');
});
test('drops diacritics so EN/romanized align', () => {
  assert.equal(normalizeName('Portgas D. Ace'), 'portgas d ace');
});
test('returns empty string for nullish', () => {
  assert.equal(normalizeName(null), '');
});
