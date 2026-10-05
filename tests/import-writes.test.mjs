// The weekly imports must write only rows that changed: on Workers Free D1
// allows 100k rows written per day, and a no-op UPDATE/upsert still counts
// (plus one per index on a written column). Each test runs a real importer
// with --dry-run in a temp dir, then applies the SQL it wrote to the full
// schema in node:sqlite, twice.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const SCHEMA = readFileSync(new URL('../schema.sql', import.meta.url), 'utf8');

function freshDb() {
  const db = new DatabaseSync(':memory:', { enableForeignKeyConstraints: false });
  db.exec(SCHEMA);
  return db;
}

// Rows changed by `sql` (SQLite counts every row an UPDATE/INSERT touched,
// no-op updates included, which is what D1 bills).
function apply(db, sql) {
  const before = db.prepare('SELECT total_changes() AS n').get().n;
  db.exec(sql);
  return db.prepare('SELECT total_changes() AS n').get().n - before;
}

// Run `script --dry-run` with cwd = a temp dir holding `files`; return the SQL
// it wrote under `batchDir`, in file order.
function dryRun(script, files, batchDir, args = []) {
  const dir = mkdtempSync(join(tmpdir(), 'optcg-import-'));
  try {
    for (const [path, content] of Object.entries(files)) {
      mkdirSync(dirname(join(dir, path)), { recursive: true });
      writeFileSync(join(dir, path), typeof content === 'string' ? content : JSON.stringify(content));
    }
    const r = spawnSync(process.execPath, [resolve(script), '--dry-run', ...args], { cwd: dir, encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    const out = join(dir, batchDir);
    return readdirSync(out).sort().map((f) => readFileSync(join(out, f), 'utf8')).join('\n');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('import-prices-d1: re-running unchanged prices writes nothing; history only on change', () => {
  const db = freshDb();
  db.exec(`INSERT INTO cards (id, name) VALUES ('OP01-001', 'Luffy'), ('OP01-002', 'Zoro'), ('OP01-003', 'Nami')`);
  db.exec(`UPDATE cards SET price = 50, price_source = 'manual' WHERE id = 'OP01-003'`);
  const prices = (p1, at) => ({
    'OP01-001': { price: p1, tcg_ids: [1], price_updated_at: at, match_method: 'dotgg+tcgplayer' },
    'OP01-002': { price: 2, tcg_ids: [2], price_updated_at: at, match_method: 'dotgg-only' },
    'OP01-003': { price: 9, tcg_ids: [3], price_updated_at: at, match_method: 'dotgg+tcgplayer' },
  });
  const run = (p1, at) => dryRun('scripts/import-prices-d1.js', { 'data/card_prices_all.json': prices(p1, at) }, 'scripts/price_batches');

  assert.equal(apply(db, run(1, 1000)), 5, '2 cards (manual skipped) + 3 history points (manual cards keep history)');
  assert.equal(apply(db, run(1, 2000)), 0, 'same prices a week later: no writes');
  assert.equal(apply(db, run(1.5, 3000)), 2, 'one moved price: its row + one history point');
  assert.deepEqual(
    db.prepare(`SELECT price, captured_at FROM card_price_history WHERE card_id = 'OP01-001' ORDER BY captured_at`).all().map((r) => [r.price, r.captured_at]),
    [[1, 1000], [1.5, 3000]],
  );
  assert.equal(db.prepare(`SELECT price FROM cards WHERE id = 'OP01-003'`).get().price, 50, 'manual price kept');
});

test('import-don-d1: unchanged DONs write nothing; history only on change', () => {
  const db = freshDb();
  const dons = (price, at) => [
    { id: 'DON-001', name: 'DON!!', rarity: 'DON', category: 'Don', image_url: 'a.png', price, tcg_ids: [9], price_updated_at: at, set_id: 'DON' },
  ];
  const run = (price, at) => dryRun('scripts/import-don-d1.js', { 'data/don_cards.json': dons(price, at) }, 'scripts/don_batches');

  assert.equal(apply(db, run(3, 1000)), 3, 'card + card_sets + history');
  assert.equal(apply(db, run(3, 2000)), 0, 'same DON next week: no writes');
  assert.equal(apply(db, run(4, 3000)), 2, 'price move: card row + history point');
});

const SET = { id: 'sv1', name: 'Scarlet & Violet', serie: { name: 'SV' }, releaseDate: '2023-03-31', cardCount: { total: 2, official: 2 } };
const card = (id, pricing) => ({
  id, localId: id.split('-')[1], name: `Card ${id}`, category: 'Pokemon', rarity: 'Common', hp: 60,
  types: ['Fire'], stage: 'Basic', variants: { normal: true }, image: `https://img/${id}`, set: { id: 'sv1' }, pricing,
});
const TCGDEX_PRICING = { cardmarket: { avg: 1.1, avg7: 1.2, avg30: 1.3, 'avg-holo': null }, tcgplayer: { normal: { marketPrice: 0.9 } } };
const tcgdexFiles = {
  'data/ptcg_cache/sets-en.json': [SET],
  'data/ptcg_cache/cards-en.json': { 'sv1-001': card('sv1-001', TCGDEX_PRICING), 'sv1-002': card('sv1-002', TCGDEX_PRICING) },
};
const tcgdexSql = () => dryRun('scripts/ptcg-import-d1.js', tcgdexFiles, 'scripts/ptcg_batches', ['--lang=en']);

test('ptcg-import-d1: an unchanged cache re-import writes nothing', () => {
  const db = freshDb();
  const sql = tcgdexSql();
  assert.equal(apply(db, sql), 3, '1 set + 2 cards');
  assert.equal(apply(db, sql), 0);
});

test('ptcg-import-d1 and fetch-pokemontcg-prices no longer undo each other', () => {
  const db = freshDb();
  const sql = tcgdexSql();
  apply(db, sql);
  // What fetch-pokemontcg-prices.js leaves behind on a row it prices: live
  // Cardmarket averages under the same keys the stale TCGdex cache carries.
  const live = JSON.stringify({ cardmarket: { avg: 2.5, avg7: 2.4, avg30: 2.2 }, tcgplayer: { normal: { market: 2.0 } } });
  db.prepare(`UPDATE ptcg_cards SET pricing_json = json_patch(pricing_json, ?), price_source = 'pokemontcg' WHERE card_id = 'sv1-001'`).run(live);
  // A backfill source on the other card (TCGdex must keep merging there).
  db.exec(`UPDATE ptcg_cards SET pricing_json = json_patch(pricing_json, '{"yuyutei":{"price_usd":3}}'), price_source = 'yuyutei' WHERE card_id = 'sv1-002'`);

  assert.equal(apply(db, sql), 0, 'next week\'s import rewrites neither row');
  const p1 = JSON.parse(db.prepare(`SELECT pricing_json FROM ptcg_cards WHERE card_id = 'sv1-001'`).get().pricing_json);
  assert.equal(p1.cardmarket.avg, 2.5, 'live price survives the import');
  const p2 = JSON.parse(db.prepare(`SELECT pricing_json FROM ptcg_cards WHERE card_id = 'sv1-002'`).get().pricing_json);
  assert.equal(p2.yuyutei.price_usd, 3);
  assert.equal(p2.cardmarket.avg, 1.1);
});

// fetch-pokemontcg-prices.js and backfill-ptcg-name-en.js need the network
// or the enrich cache, so their statements are checked in shape here.
test('pokemontcg price statements: unchanged prices and an already-set source write nothing', () => {
  const db = freshDb();
  db.exec(`INSERT INTO ptcg_cards (card_id, lang, set_id, local_id, name, updated_at, pricing_json, price_source)
           VALUES ('sv1-001', 'en', 'sv1', '001', 'x', 0, '{}', NULL), ('sv1-002', 'en', 'sv1', '002', 'y', 0, '{}', 'manual')`);
  const src = readFileSync('scripts/fetch-pokemontcg-prices.js', 'utf8');
  assert.match(src, /SET pricing_json = \$\{newPricing\} WHERE \$\{where\} AND pricing_json IS NOT \$\{newPricing\}/);
  assert.match(src, /SET price_source = 'pokemontcg' WHERE \$\{where\} AND price_source IS NOT 'pokemontcg' AND price_source IS NOT 'manual'/);
  const patch = `json_patch(COALESCE(pricing_json, '{}'), '{"tcgplayer":{"normal":{"market":1,"directLow":null}}}')`;
  const stmts = (id) => `
    UPDATE ptcg_cards SET pricing_json = ${patch} WHERE card_id = '${id}' AND lang = 'en' AND pricing_json IS NOT ${patch};
    UPDATE ptcg_cards SET price_source = 'pokemontcg' WHERE card_id = '${id}' AND lang = 'en' AND price_source IS NOT 'pokemontcg' AND price_source IS NOT 'manual';`;
  assert.equal(apply(db, stmts('sv1-001') + stmts('sv1-002')), 3, 'two price patches + one source flip (manual kept)');
  assert.equal(apply(db, stmts('sv1-001') + stmts('sv1-002')), 0, 'same prices again: nothing');
});

test('name_en backfill only fills NULLs', () => {
  const src = readFileSync('scripts/backfill-ptcg-name-en.js', 'utf8');
  assert.match(src, /AND lang = 'ja' AND name_en IS NULL;`/);
});

test('ptcg-import-d1: a JA card already stored under another id format is not inserted twice', () => {
  const jaSet = { ...SET, id: 'M4' };
  const jaCard = (localId) => ({ ...card(`M4-${localId}`, null), localId, set: { id: 'M4' } });
  const files = (stored) => ({
    'data/ptcg_cache/sets-ja.json': [jaSet],
    'data/ptcg_cache/cards-ja.json': { 'M4-001': jaCard('001'), 'M4-002': jaCard('002') },
    'data/ja_ids.json': stored,
  });
  const run = (stored) => dryRun('scripts/ptcg-import-d1.js', files(stored), 'scripts/ptcg_batches', ['--lang=ja', '--ja-ids=data/ja_ids.json']);

  // pkmnbindr seeded "M4-1" first: TCGdex's "M4-001" is the same card.
  const sql = run([{ card_id: 'M4-1', set_id: 'M4', local_id: '1' }]);
  assert.doesNotMatch(sql, /'M4-001'/);
  assert.match(sql, /'M4-002'/);
  // Once both exist (or after a dedupe keeps "M4-001"), TCGdex updates its own row.
  const both = run([{ card_id: 'M4-1', set_id: 'M4', local_id: '1' }, { card_id: 'M4-001', set_id: 'M4', local_id: '001' }]);
  assert.match(both, /'M4-001'/);
});
