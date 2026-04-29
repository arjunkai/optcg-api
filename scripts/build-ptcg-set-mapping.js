/**
 * build-ptcg-set-mapping.js — proposes a TCGdex_set_id → pokemontcg_set_id
 * mapping by matching set names (loose) and release years.
 *
 * Reads:
 *   - data/ptcg_cache/sets-en.json (cached TCGdex sets)
 *   - data/pokemontcg-data/sets/en.json
 *
 * Writes:
 *   - data/ptcg_set_mapping.json  ({ tcgdex_id: pokemontcg_id })
 *   - data/ptcg_set_mapping.unmatched.json  (TCGdex sets we couldn't map)
 *
 * Manual review the unmatched file and add corrections to the mapping
 * before re-running the import.
 *
 * Match passes (in order):
 *   1. Same id in both databases (e.g. base1 = base1, xy2 = xy2)
 *   2. Exact normalized-name + same release year
 *   3. Normalized-name match with year ±1 (release-date discrepancies)
 *
 * No substring fallback — it produced false positives like XY Trainer
 * Kits matching the base XY set. Anything below this bar lands in
 * unmatched.json for hand curation.
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';

const TCGDEX_CACHE = 'data/ptcg_cache/sets-en.json';
const PKM_SETS = 'data/pokemontcg-data/sets/en.json';
const OUT_MAPPING = 'data/ptcg_set_mapping.json';
const OUT_UNMATCHED = 'data/ptcg_set_mapping.unmatched.json';

if (!existsSync(TCGDEX_CACHE)) {
  console.error(`Missing ${TCGDEX_CACHE}. Run scripts/ptcg-fetch.js first.`);
  process.exit(1);
}

const tcgdexSets = JSON.parse(readFileSync(TCGDEX_CACHE, 'utf-8'));
const pkmSets = JSON.parse(readFileSync(PKM_SETS, 'utf-8'));

// Normalize names for fuzzy matching: lowercase, strip punctuation,
// collapse whitespace. "Scarlet & Violet" → "scarlet violet".
function normalizeName(s) {
  return (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function yearOf(dateStr) {
  if (!dateStr) return null;
  const m = String(dateStr).match(/^(\d{4})/);
  return m ? Number(m[1]) : null;
}

const pkmById = new Map();
const pkmByNameYear = new Map();
for (const s of pkmSets) {
  pkmById.set(s.id, s);
  pkmByNameYear.set(`${normalizeName(s.name)}::${yearOf(s.releaseDate)}`, s.id);
}

const mapping = {};
const unmatched = [];
const trace = { id: 0, nameYear: 0, nameYearFuzz: 0 };

for (const t of tcgdexSets) {
  const tName = normalizeName(t.name);
  const tYear = yearOf(t.releaseDate);

  // Pass 1: identical IDs in both databases.
  if (pkmById.has(t.id)) {
    mapping[t.id] = t.id;
    trace.id++;
    continue;
  }
  // Pass 2: exact name+year.
  let candidate = pkmByNameYear.get(`${tName}::${tYear}`);
  if (candidate) {
    mapping[t.id] = candidate;
    trace.nameYear++;
    continue;
  }
  // Pass 3: name match within ±1 year (release-date drift between dbs).
  if (tYear != null) {
    candidate =
      pkmByNameYear.get(`${tName}::${tYear - 1}`) ||
      pkmByNameYear.get(`${tName}::${tYear + 1}`);
    if (candidate) {
      mapping[t.id] = candidate;
      trace.nameYearFuzz++;
      continue;
    }
  }

  unmatched.push({ tcgdex_id: t.id, name: t.name, releaseDate: t.releaseDate });
}

writeFileSync(OUT_MAPPING, JSON.stringify(mapping, null, 2));
writeFileSync(OUT_UNMATCHED, JSON.stringify(unmatched, null, 2));

console.log(`Mapped: ${Object.keys(mapping).length}`);
console.log(`  by id          : ${trace.id}`);
console.log(`  by name+year   : ${trace.nameYear}`);
console.log(`  by name+year±1 : ${trace.nameYearFuzz}`);
console.log(`Unmatched: ${unmatched.length} → ${OUT_UNMATCHED}`);
