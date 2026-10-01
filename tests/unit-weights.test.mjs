// Every gated route must have a deliberate weight. A new route fails here
// until it gets a rule in UNIT_RULES (src/edgeCache.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { explicitUnits, unitsFor, isHeavyMiss, MIN_CHARGED_UNITS } from '../src/edgeCache.js';

const PUBLIC = [/^\/$/, /^\/docs$/, /^\/healthz$/, /^\/images\//, /^\/pokemon\/images\//];

function routes() {
  const files = ['src', 'src/pokemon'].flatMap((d) =>
    readdirSync(d).filter((f) => f.endsWith('.js')).map((f) => `${d}/${f}`));
  const out = new Set();
  for (const f of files) {
    for (const m of readFileSync(f, 'utf8').matchAll(/\bapp\.get\(\s*'(\/[^']*)'/g)) out.add(m[1]);
  }
  return [...out].filter((r) => !PUBLIC.some((p) => p.test(r)));
}
const sample = (r) => r.replace(/:[a-z_]+/gi, 'X1');
const w = (p) => { const u = new URL('https://x' + p); return unitsFor(u.pathname, u); };

test('found routes (sanity)', () => {
  const r = routes();
  assert.ok(r.includes('/cards') && r.includes('/pokemon/sets') && r.length >= 15, r.join(' '));
});

test('every gated route has an explicit weight', () => {
  const missing = routes().filter((r) => {
    const p = sample(r);
    return explicitUnits(p, new URL('https://x' + p)) === null;
  });
  assert.deepEqual(missing, []);
});

test('table-scanning searches cost 15,000', () => {
  for (const p of ['/cards', '/cards?name=x', '/artwork']) {
    const u = new URL('https://x' + p);
    assert.equal(isHeavyMiss(u.pathname, u), true, p);
    assert.equal(w(p), 15_000, p);
  }
});

test('weights from the spec', () => {
  assert.equal(w('/artwork/gallery?page=2'), 1000);
  assert.equal(w('/cards?set_id=OP-01'), 1000);
  assert.equal(w('/sets/OP-01/cards'), 1000);
  assert.equal(w('/pokemon/sets/sv1/cards'), 1000);
  assert.equal(w('/artwork?artist=oda'), 1000);
  assert.equal(w('/illustrators/oda'), 1000);
  assert.equal(w('/characters/12'), 1000);
  assert.equal(w('/illustrators'), 3000);
  assert.equal(w('/products'), 3000);
  assert.equal(w('/characters'), 0);
  assert.equal(w('/cards/OP01-001/price-history'), 500);
  assert.equal(w('/cards/OP01-001/price-history?range=all'), 1000);
  assert.equal(w('/pokemon/cards/sv1-1/price-history'), 500);
  for (const p of ['/cards/OP01-001', '/pokemon/cards/sv1-1', '/sets', '/pokemon/sets', '/openapi.json',
    '/cards/all', '/cards/index', '/pokemon/cards/all', '/pokemon/cards/index', '/representatives']) {
    assert.equal(w(p), 0, p);
  }
  assert.equal(w('/something-new'), 3000);
});

test('no charged weight is below MIN_CHARGED_UNITS (the capacity test relies on it)', () => {
  const all = routes().map((r) => w(sample(r))).concat([w('/cards?set_id=1'), w('/cards/x/price-history')]);
  assert.ok(all.every((x) => x === 0 || x >= MIN_CHARGED_UNITS));
  assert.equal(MIN_CHARGED_UNITS, 500);
});
