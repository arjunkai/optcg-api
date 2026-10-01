// Every R2 snapshot the Worker serves (src/snapshot.js), with how to build it.
// Only scripts/build-snapshots.mjs and tests import this; the Worker never
// builds a snapshot, it only reads the built copy.
//
// A def is { name, group, build } plus either
//   sql:  [sql, ...]                       run once each, unpaged
//   page: { sql, params, key, start, size, sort }
//         sql takes params(cursor, size) and returns rows ordered by `key`,
//         the column the next page starts after. Rows are read once each
//         (keyset paging, no OFFSET), then sorted by `sort`.
// build(...rowsets) returns the response object, one rowset per query.
//
// `group` is what the workflows pass to --only: the OPTCG scrape rebuilds
// optcg + canvs, the PTCG refresh rebuilds pokemon.

import { parseCards } from './db.js';
import { CARDS_ALL_PAGE_SQL, CARDS_INDEX_SQL, slimCardRow } from './cards.js';
import { REP_QUERIES, ROSTER_SQL, COLLECTIONS_SQL, buildReps, buildRoster, binaryCompare } from './canvs.js';
import {
  VALID_LANGS, ptcgIndexPageSql, PTCG_ALL_PAGE_SQL, rowToSlim, withSlimPricing, fullPtcgRow,
} from './pokemon/cards.js';

// The order the PTCG routes always returned: ORDER BY set_id, local_id
// (SQLite BINARY collation), rowid breaking ties.
function bySetThenLocal(a, b) {
  return binaryCompare(a.set_id, b.set_id) || binaryCompare(a.local_id, b.local_id) || a._rk - b._rk;
}

const list = (data) => ({ count: data.length, data });

const ptcgPage = (sql, lang, size) => ({
  sql,
  params: (cursor, n) => [lang, cursor, n],
  key: '_rk',
  start: 0,
  size,
  sort: bySetThenLocal,
});

export const SNAPSHOTS = [
  {
    name: 'cards-all-v1',
    group: 'optcg',
    page: { sql: CARDS_ALL_PAGE_SQL, params: (cursor, n) => [cursor, n], key: 'id', start: '', size: 1000 },
    build: (rows) => list(parseCards(rows)),
  },
  {
    name: 'cards-index-v1',
    group: 'optcg',
    sql: [CARDS_INDEX_SQL],
    build: (rows) => list(rows.map(slimCardRow)),
  },
  ...Object.keys(REP_QUERIES).map((kind) => ({
    name: `reps-${kind}-v1`,
    group: 'canvs',
    sql: [REP_QUERIES[kind]],
    build: (rows) => buildReps(kind, rows),
  })),
  {
    name: 'characters-roster-v1',
    group: 'canvs',
    sql: [ROSTER_SQL.characters, ROSTER_SQL.types],
    build: buildRoster,
  },
  {
    name: 'artwork-collections-v1',
    group: 'canvs',
    sql: [COLLECTIONS_SQL],
    build: (rows) => rows,
  },
  ...[...VALID_LANGS].flatMap((lang) => [
    {
      name: `pokemon-index-${lang}-v8`,
      group: 'pokemon',
      page: ptcgPage(ptcgIndexPageSql(lang), lang, 2000),
      build: (rows) => list(rows.map((row) => withSlimPricing(rowToSlim(row)))),
    },
    {
      // SELECT * carries the raw TCGdex blob (~2.6 KB a row): smaller pages.
      name: `pokemon-all-${lang}-v2`,
      group: 'pokemon',
      page: ptcgPage(PTCG_ALL_PAGE_SQL, lang, 500),
      build: (rows) => list(rows.map(fullPtcgRow)),
    },
  ]),
];

// Rows for one def, via query(sql, params) -> rows. Paged defs are read with
// keyset paging and sorted; a leading-underscore key column is dropped.
export async function fetchRowsets(def, query) {
  if (!def.page) {
    const out = [];
    for (const sql of def.sql) out.push(await query(sql, []));
    return out;
  }
  const { sql, params, key, start, size, sort } = def.page;
  const rows = [];
  let cursor = start;
  for (;;) {
    const page = await query(sql, params(cursor, size));
    for (const row of page) rows.push(row);
    if (page.length < size) break;
    cursor = page[page.length - 1][key];
  }
  if (sort) rows.sort(sort);
  if (key.startsWith('_')) for (const row of rows) delete row[key];
  return [rows];
}

// The JSON body for one def.
export async function buildSnapshotBody(def, query) {
  return JSON.stringify(def.build(...(await fetchRowsets(def, query))));
}

// Defs matching --only patterns: a group name, an exact name, or a name
// prefix ending in '*'. No patterns means every def.
export function selectSnapshots(patterns = []) {
  if (!patterns.length) return SNAPSHOTS;
  const unknown = patterns.filter((p) => !SNAPSHOTS.some((d) => matches(d, p)));
  if (unknown.length) throw new Error(`no snapshot matches: ${unknown.join(', ')}`);
  return SNAPSHOTS.filter((d) => patterns.some((p) => matches(d, p)));
}

function matches(def, pattern) {
  if (pattern === def.group || pattern === def.name) return true;
  return pattern.endsWith('*') && def.name.startsWith(pattern.slice(0, -1));
}
