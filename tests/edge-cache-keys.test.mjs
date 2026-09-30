// node --test tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { cacheKeyFor, paramsFor, ipLimitKey, isHeavyMiss } from '../src/edgeCache.js';
import { likeToRegExp, binaryCompare } from '../src/canvs.js';

// Every query param a handler reads must be in that route's cache-key
// allowlist, or two requests that differ only in that param would share a
// cached response. This scans the handlers, so a new param fails here first.
test('ROUTE_PARAMS covers every query param the handlers read', () => {
  const files = [
    ...readdirSync('src').filter((f) => f.endsWith('.js')).map((f) => `src/${f}`),
    ...readdirSync('src/pokemon').map((f) => `src/pokemon/${f}`),
  ];
  const IGNORE = new Set(['refresh']); // stripped on purpose
  const missing = [];
  for (const file of files) {
    const src = readFileSync(file, 'utf8');
    // Split into route handlers; the path is the first arg of app.get.
    const parts = src.split(/app\.get\('([^']+)'/).slice(1);
    for (let i = 0; i < parts.length; i += 2) {
      const route = parts[i];
      const body = parts[i + 1];
      // Public paths skip the edge cache (auth.js PUBLIC_*); they cache themselves.
      if (/^\/(images|pokemon\/images|docs|openapi\.json)(\/|$)/.test(route)) continue;
      const used = new Set([
        ...[...body.matchAll(/req\.query\('([a-z_]+)'\)/g)].map((m) => m[1]),
        ...[...body.matchAll(/\bq\.([a-z_]+)/g)].map((m) => m[1]),
        ...[...body.matchAll(/\['((?:min|max)_[a-z]+)'/g)].map((m) => m[1]),
      ]);
      const samplePath = route.replace(/:[a-z_]+/g, 'X');
      const allowed = new Set(paramsFor(samplePath));
      for (const p of used) {
        if (!IGNORE.has(p) && !allowed.has(p)) missing.push(`${file} ${route} ?${p}`);
      }
    }
  }
  assert.deepEqual(missing, []);
});

test('cache key keeps route params only, sorted, first value wins', () => {
  const k = (u) => cacheKeyFor(`https://x.dev${u}`).url;
  assert.equal(k('/sets?zz=1'), 'https://x.dev/sets');
  assert.equal(k('/cards?rarity=SR&category=Leader&_=9'), 'https://x.dev/cards?category=Leader&rarity=SR');
  assert.equal(k('/cards?name=a&name=b'), 'https://x.dev/cards?name=a');
  assert.equal(k('/cards?parallel='), 'https://x.dev/cards?parallel=');
  assert.equal(k('/characters?q=luffy&refresh=1'), 'https://x.dev/characters?q=luffy');
  assert.equal(k('/pokemon/cards/base1-1?lang=ja&v=3'), 'https://x.dev/pokemon/cards/base1-1?lang=ja');
});

test('heavy misses', () => {
  const heavy = (u) => { const url = new URL(`https://x.dev${u}`); return isHeavyMiss(url.pathname, url); };
  assert.equal(heavy('/cards?name=luffy'), true);
  assert.equal(heavy('/cards?set_id=OP-01&sort=price'), false);
  assert.equal(heavy('/artwork?page=3'), true);
  assert.equal(heavy('/artwork?character=408'), false);
  assert.equal(heavy('/artwork/gallery?collection=box'), true);
  assert.equal(heavy('/characters?q=a'), false);
});

test('IPv6 clients are limited per /64', () => {
  assert.equal(ipLimitKey('203.0.113.9'), 'ip:203.0.113.9');
  assert.equal(ipLimitKey('2001:db8:1:2:aaaa::1'), 'ip6:2001:db8:1:2');
  assert.equal(ipLimitKey('2001:DB8:1:2:bbbb:cccc:dddd:eeee'), 'ip6:2001:db8:1:2');
  assert.equal(ipLimitKey('2001:db8::1'), 'ip6:2001:db8:0:0');
  assert.equal(ipLimitKey('::ffff:198.51.100.7'), 'ip:198.51.100.7');
});

test('LIKE and BINARY collation match SQLite', () => {
  const like = (p, v) => likeToRegExp(p).test(v);
  assert.equal(like('%luffy%', 'Monkey.D.Luffy'), true);
  assert.equal(like('%l_ffy%', 'Luffy'), true);
  assert.equal(like('%a.b%', 'axb'), false);
  assert.equal(like('%(%', 'x(y'), true);
  assert.equal(like('%é%', 'É'), false); // SQLite only folds ASCII
  assert.equal(binaryCompare('a', 'B') > 0, true);
  assert.equal(binaryCompare(null, 'a') < 0, true);
  assert.equal(binaryCompare('￿', '\u{1F600}') < 0, true); // UTF-8 byte order
});
