// Snapshots are built outside requests (scripts/build-snapshots.mjs from
// src/snapshotDefs.js) and requests only read R2 (src/snapshot.js). These
// check the defs against the queries the routes used to run, that paging
// changes nothing, and that the request path never touches D1.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync, readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { SNAPSHOTS, fetchRowsets, buildSnapshotBody, selectSnapshots } from '../src/snapshotDefs.js';
import { serveSnapshot, loadSnapshotData } from '../src/snapshot.js';
import { parseCards } from '../src/db.js';
import { CARDS_INDEX_SQL, slimCardRow } from '../src/cards.js';
import { REP_QUERIES, buildReps } from '../src/canvs.js';
import { rowToSlim, withSlimPricing, fullPtcgRow } from '../src/pokemon/cards.js';
import { parseArgs, needsRemote } from '../scripts/build-snapshots.mjs';
import { fakeCtx, installCaches } from './helpers/fakes.mjs';

function seededDb() {
  const db = new DatabaseSync(':memory:', { enableForeignKeyConstraints: false });
  db.exec(readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'));
  const ins = (table, row) => {
    const cols = Object.keys(row);
    db.prepare(`INSERT INTO ${table} (${cols.join(',')}) VALUES (${cols.map(() => '?').join(',')})`)
      .run(...Object.values(row));
  };
  const ids = ['OP01-003', 'OP01-001', 'ST01-002', 'OP01-002', 'DON-001'];
  ids.forEach((id, i) => ins('cards', {
    id, name: `Card ${id}`, category: id.startsWith('DON') ? 'Don' : 'Character',
    colors: '["Red"]', types: '["Straw Hat Crew"]', price: i * 1.5, parallel: i % 2,
  }));
  ins('card_translations', { card_id: 'OP01-001', language: 'en', name: 'Luffy' });
  ins('card_translations', { card_id: 'OP01-001', language: 'ja', name: 'ルフィ', name_en: 'Luffy' });
  ins('card_translations', { card_id: 'OP01-002', language: 'ja', name: 'ゾロ', name_en: 'Zoro' });
  for (const id of ids) ins('card_sets', { card_id: id, set_id: id.slice(0, 4) });
  ins('illustrators', { id: 1, slug: 'oda', name: 'Oda' });
  ins('card_illustrators', { card_id: 'OP01-002', illustrator_id: 1 });
  ins('card_illustrators', { card_id: 'OP01-001', illustrator_id: 1 });
  ins('characters', { id: 7, name: 'Luffy', source: 'wiki' });
  ins('characters', { id: 8, name: 'Zoro', source: 'wiki' });
  ins('card_characters', { card_id: 'OP01-001', character_id: 7 });
  ins('card_characters', { card_id: 'OP01-003', character_id: 7 });
  ins('artwork', { id: 1, source: 'x', source_url: 'https://a', collection: 'playmat' });
  ins('artwork', { id: 2, source: 'x', source_url: 'https://b', collection: null });
  ins('artwork_characters', { artwork_id: 1, character_id: 8 });
  // PTCG rows inserted out of (set_id, local_id) order, languages
  // interleaved, so paging and the sort both have work to do.
  const ptcg = [
    ['sv2-010', 'en', 'sv2', '010'], ['sv1-002', 'ja', 'sv1', '002'], ['sv1-001', 'en', 'sv1', '001'],
    ['sv1-002', 'en', 'sv1', '002'], ['SV1-1', 'ja', 'SV1', '1'], ['sv1-001', 'ja', 'sv1', '001'],
    ['sv1-010', 'en', 'sv1', '010'], ['sv1-002x', 'en', 'sv1', '002'], ['sv3-001', 'zh-tw', 'sv3', '001'],
  ];
  for (const [card_id, lang, set_id, local_id] of ptcg) {
    ins('ptcg_cards', {
      card_id, lang, set_id, local_id, name: `${lang} ${card_id}`, updated_at: 0,
      types_csv: 'Fire,Water', variants_json: '{"normal":true}', image_high: null,
      pricing_json: JSON.stringify({ tcgplayer: { normal: { market: 1.25, low: 1 } }, cardmarket: { avg: 2, foo: 3 } }),
      raw: JSON.stringify({ id: card_id, illustrator: 'Ken', image_high: 'raw.png' }),
    });
  }
  return db;
}

const sqliteQuery = (db) => async (sql, params) => db.prepare(sql).all(...params);
const def = (name) => SNAPSHOTS.find((d) => d.name === name);
const withPageSize = (d, size) => ({ ...d, page: { ...d.page, size } });

// What the route handlers built before snapshots moved out of requests.
function oldBuild(db, name) {
  const all = (sql, ...p) => db.prepare(sql).all(...p);
  if (name === 'cards-all-v1') {
    const rows = all('SELECT * FROM cards ORDER BY id ASC');
    return { count: rows.length, data: parseCards(rows) };
  }
  if (name === 'cards-index-v1') {
    const rows = all(CARDS_INDEX_SQL);
    return { count: rows.length, data: rows.map(slimCardRow) };
  }
  if (name.startsWith('reps-')) {
    const kind = name.split('-')[1];
    return buildReps(kind, all(REP_QUERIES[kind]));
  }
  const m = name.match(/^pokemon-(index|all)-(.+)-v\d+$/);
  if (m) {
    const [, kind, lang] = m;
    if (kind === 'all') {
      const rows = all('SELECT * FROM ptcg_cards WHERE lang = ? ORDER BY set_id, local_id', lang);
      return { count: rows.length, data: rows.map(fullPtcgRow) };
    }
    const rows = lang === 'ja'
      ? all(`SELECT ja.card_id AS card_id, ja.lang AS lang, ja.set_id AS set_id, ja.local_id AS local_id,
               ja.name AS name, COALESCE(ja.name_en, en.name) AS name_en, ja.category AS category,
               ja.rarity AS rarity, ja.hp AS hp, ja.retreat AS retreat, ja.types_csv AS types_csv,
               ja.stage AS stage, ja.variants_json AS variants_json, ja.image_high AS image_high,
               ja.image_low AS image_low, ja.pricing_json AS pricing_json,
               ja.price_source AS price_source, ja.dominant_color AS dominant_color
             FROM ptcg_cards ja LEFT JOIN ptcg_cards en ON en.card_id = ja.card_id AND en.lang = 'en'
             WHERE ja.lang = 'ja' ORDER BY ja.set_id, ja.local_id`)
      : all(`SELECT card_id, lang, set_id, local_id, name, name_en, category, rarity, hp, retreat,
               types_csv, stage, variants_json, image_high, image_low, pricing_json, price_source,
               dominant_color FROM ptcg_cards WHERE lang = ? ORDER BY set_id, local_id`, lang);
    return { count: rows.length, data: rows.map((r) => withSlimPricing(rowToSlim(r))) };
  }
  return null;
}

test('every def builds what its route used to build', async () => {
  const db = seededDb();
  for (const d of SNAPSHOTS) {
    const expected = oldBuild(db, d.name);
    if (!expected) continue; // roster/collections: covered below
    const body = await buildSnapshotBody(d, sqliteQuery(db));
    assert.deepEqual(JSON.parse(body), JSON.parse(JSON.stringify(expected)), d.name);
  }
});

test('paging (any page size) gives the same bytes as one page', async () => {
  const db = seededDb();
  for (const d of SNAPSHOTS.filter((s) => s.page)) {
    const one = await buildSnapshotBody(withPageSize(d, 10_000), sqliteQuery(db));
    for (const size of [1, 2, 3]) {
      assert.equal(await buildSnapshotBody(withPageSize(d, size), sqliteQuery(db)), one, `${d.name} size ${size}`);
    }
  }
});

test('paged PTCG rows come back sorted by set_id then local_id, without the paging key', async () => {
  const db = seededDb();
  const [rows] = await fetchRowsets(withPageSize(def('pokemon-index-en-v8'), 2), sqliteQuery(db));
  assert.deepEqual(rows.map((r) => r.card_id), ['sv1-001', 'sv1-002', 'sv1-002x', 'sv1-010', 'sv2-010']);
  assert.ok(rows.every((r) => !('_rk' in r)));
});

test('roster and collection snapshots match the per-request SQL', async () => {
  const db = seededDb();
  const roster = JSON.parse(await buildSnapshotBody(def('characters-roster-v1'), sqliteQuery(db)));
  const luffy = roster.find((ch) => ch.id === 7);
  assert.equal(luffy.card_count, 2);
  assert.deepEqual(luffy.types, ['Straw Hat Crew']);
  assert.equal(roster.find((ch) => ch.id === 8).artwork_count, 1);
  const counts = JSON.parse(await buildSnapshotBody(def('artwork-collections-v1'), sqliteQuery(db)));
  assert.deepEqual(counts, [{ collection: null, n: 1 }, { collection: 'playmat', n: 1 }]);
});

test('every snapshot name a route serves has a def', () => {
  const files = [
    ...readdirSync('src').filter((f) => f.endsWith('.js')).map((f) => `src/${f}`),
    ...readdirSync('src/pokemon').map((f) => `src/pokemon/${f}`),
  ];
  const names = new Set(SNAPSHOTS.map((d) => d.name));
  const served = [];
  for (const file of files) {
    const src = readFileSync(file, 'utf8');
    for (const [, raw] of src.matchAll(/(?:serveSnapshot|loadSnapshotData|snapshotUnavailable)\(\s*(?:c,\s*)?[`']([^`']+)[`']/g)) {
      served.push(raw);
      const re = new RegExp(`^${raw.replace(/[.*+?^()|[\]\\]/g, '\\$&').replace(/\$\{[a-z]+\}/g, '[a-z-]+')}$`);
      assert.ok([...names].some((n) => re.test(n)), `${file}: no def for ${raw}`);
    }
  }
  assert.ok(served.length >= 6, `found only ${served.length} snapshot uses`);
});

test('--only selects by group, exact name or prefix, and rejects unknown patterns', () => {
  assert.deepEqual(selectSnapshots(['optcg']).map((d) => d.name), ['cards-all-v1', 'cards-index-v1']);
  assert.equal(selectSnapshots(['pokemon']).length, 8);
  assert.deepEqual(selectSnapshots(['reps-set-v1']).map((d) => d.name), ['reps-set-v1']);
  assert.equal(selectSnapshots(['pokemon-index-*']).length, 4);
  assert.equal(selectSnapshots([]).length, SNAPSHOTS.length);
  assert.throws(() => selectSnapshots(['nope']), /no snapshot matches: nope/);
});

// ── request path ─────────────────────────────────────────────────────────

function fakeR2(objects = {}) {
  const r2 = {
    gets: [],
    async get(key) {
      r2.gets.push(key);
      if (!(key in objects)) return null;
      const text = objects[key];
      return {
        get body() { return new Response(text).body; },
        async json() { return JSON.parse(text); },
      };
    },
    async put() { throw new Error('requests must not write snapshots'); },
  };
  return r2;
}

// Any D1 access throws: the request path must never reach it.
const noD1 = new Proxy({}, { get() { throw new Error('request path touched D1'); } });

function fakeC(r2, { refresh = false } = {}) {
  const ctx = fakeCtx();
  return {
    ctx,
    env: { IMAGES: r2, DB: noD1 },
    executionCtx: ctx,
    req: {
      url: `https://api.test/cards/index${refresh ? '?refresh=1' : ''}`,
      query: (k) => (k === 'refresh' && refresh ? '1' : undefined),
    },
    get: (k) => ({ caller: 'key', admin: true })[k],
  };
}

beforeEach(() => installCaches());

test('serves the R2 snapshot and puts it in the edge cache', async () => {
  const r2 = fakeR2({ 'snapshots/cards-index-v1.json': '{"count":1}' });
  const c = fakeC(r2);
  const res = await serveSnapshot(c, 'cards-index-v1');
  assert.equal(res.status, 200);
  assert.equal(await res.text(), '{"count":1}');
  await c.ctx.drain();
  const again = await serveSnapshot(fakeC(fakeR2()), 'cards-index-v1');
  assert.equal(await again.text(), '{"count":1}', 'second request is an edge hit');
});

test('falls back to last-known-good, without edge-caching it', async () => {
  const r2 = fakeR2({ 'snapshots/lkg/cards-index-v1.json': '{"lkg":true}' });
  const c = fakeC(r2);
  const res = await serveSnapshot(c, 'cards-index-v1');
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('Cache-Control'), 'public, max-age=60');
  assert.equal(await res.text(), '{"lkg":true}');
  await c.ctx.drain();
  const next = await serveSnapshot(fakeC(fakeR2()), 'cards-index-v1');
  assert.equal(next.status, 503, 'lkg copy was not pinned in the edge cache');
});

test('503s when nothing was ever built', async () => {
  const res = await serveSnapshot(fakeC(fakeR2()), 'cards-index-v1');
  assert.equal(res.status, 503);
  assert.equal(res.headers.get('Cache-Control'), 'no-store');
  assert.equal(res.headers.get('Retry-After'), '300');
  assert.deepEqual(await res.json(), { error: 'snapshot_unavailable', snapshot: 'cards-index-v1' });
});

test('refresh=1 skips the edge cache and re-reads R2', async () => {
  const first = fakeC(fakeR2({ 'snapshots/cards-index-v1.json': '"old"' }));
  await (await serveSnapshot(first, 'cards-index-v1')).text();
  await first.ctx.drain();
  const r2 = fakeR2({ 'snapshots/cards-index-v1.json': '"new"' });
  const res = await serveSnapshot(fakeC(r2, { refresh: true }), 'cards-index-v1');
  assert.equal(await res.text(), '"new"');
  assert.deepEqual(r2.gets, ['snapshots/cards-index-v1.json']);
});

test('loadSnapshotData parses and memoizes; null when missing', async () => {
  const r2 = fakeR2({ 'snapshots/memo-test-v1.json': '[1,2]' });
  assert.deepEqual(await loadSnapshotData(fakeC(r2), 'memo-test-v1'), [1, 2]);
  assert.deepEqual(await loadSnapshotData(fakeC(r2), 'memo-test-v1'), [1, 2]);
  assert.equal(r2.gets.length, 1, 'second call served from the isolate memo');
  assert.equal(await loadSnapshotData(fakeC(fakeR2()), 'never-built-v1'), null);
});

test('src/snapshot.js has no D1 access', () => {
  const src = readFileSync('src/snapshot.js', 'utf8');
  assert.doesNotMatch(src, /env\.DB|\.prepare\(|\.batch\(/);
  assert.doesNotMatch(src, /\.put\(\s*`snapshots/);
});

// ── builder safety ───────────────────────────────────────────────────────

test('builder args: only local runs avoid production', () => {
  assert.equal(needsRemote(parseArgs([])), true);
  assert.equal(needsRemote(parseArgs(['--sqlite', 'x.db'])), true, 'uploads to production R2');
  assert.equal(needsRemote(parseArgs(['--sqlite', 'x.db', '--out', 'd'])), false);
  assert.equal(needsRemote(parseArgs(['--sqlite', 'x.db', '--local'])), false);
  assert.deepEqual(parseArgs(['--only', 'optcg,canvs', '--only', 'pokemon']).only, ['optcg', 'canvs', 'pokemon']);
  assert.throws(() => parseArgs(['--out', 'd', '--local']), /exclusive/);
  assert.throws(() => parseArgs(['--bogus']), /unknown argument/);
});

test('builder refuses production without OPTCG_ALLOW_REMOTE, and always under tests', () => {
  const run = (env) => spawnSync(process.execPath, ['scripts/build-snapshots.mjs', '--only', 'optcg'], {
    encoding: 'utf8', env: { ...process.env, CLOUDFLARE_API_TOKEN: 'x', CLOUDFLARE_ACCOUNT_ID: 'x', ...env },
  });
  for (const env of [{ OPTCG_ALLOW_REMOTE: '' }, { OPTCG_ALLOW_REMOTE: '1' }]) {
    const r = run(env);
    assert.equal(r.status, 1);
    assert.match(r.stderr, /Refusing to touch production/);
  }
});
