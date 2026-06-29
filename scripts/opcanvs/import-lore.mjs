// import-lore.mjs — source: api-onepiece.com (attribute in OPCanvs footer). No remote D1 writes.
import { writeFile, mkdir } from 'node:fs/promises';
import { BASES, cleanEnString } from './lib/apionepiece.mjs';
import { normalizeName } from './lib/normalize-name.mjs';

const OUT = new URL('./opcanvs_batches/', import.meta.url);
const HERE = new URL('./', import.meta.url);
const sq = (s) => s == null || s === '' ? 'NULL' : `'${String(s).replace(/'/g, "''")}'`;
const num = (b) => b ? 1 : 0;
const getJson = async (u) => {
  const r = await fetch(u, { signal: AbortSignal.timeout(30000) });
  if (!r.ok) throw new Error(`${r.status} ${u}`);
  return r.json();
};

const [crews, chars, locs] = await Promise.all([
  getJson(BASES.crews), getJson(BASES.characters), getJson(BASES.locates),
]);

const lines = [];
for (const c of crews)
  lines.push(`INSERT INTO crews (id,source_id,name,roman_name,is_yonko,total_prime,number,status) VALUES (${c.id},${c.id},${sq(cleanEnString(c.name))},${sq(c.roman_name)},${num(c.is_yonko)},${sq(c.total_prime)},${sq(c.number)},${sq(cleanEnString(c.status))});`);
for (const ch of chars)
  lines.push(`INSERT INTO characters (id,source_id,name,name_normalized,crew_id,fruit_name,fruit_type,bounty,job,status) VALUES (${ch.id},${ch.id},${sq(cleanEnString(ch.name))},${sq(normalizeName(ch.name))},${ch.crew?.id ?? 'NULL'},${sq(ch.fruit?.name)},${sq(cleanEnString(ch.fruit?.type))},${sq(ch.bounty)},${sq(cleanEnString(ch.job))},${sq(cleanEnString(ch.status))});`);
for (const l of locs)
  lines.push(`INSERT INTO locations (id,source_id,name,region_name,roman_name,sea_name,affiliation_name) VALUES (${l.id},${l.id},${sq(l.name)},${sq(l.region_name)},${sq(l.roman_name)},${sq(l.sea_name)},${sq(l.affiliation_name)});`);

// JSON sidecars consumed by the matcher (Task 7) — so it needs no D1 lore tables.
const charSidecar = chars.map(ch => ({ id: ch.id, name: cleanEnString(ch.name), name_normalized: normalizeName(ch.name) }));
const locSidecar  = locs.map(l => ({ id: l.id, name: l.name }));

await mkdir(OUT, { recursive: true });
await writeFile(new URL('lore.sql', OUT), lines.join('\n'));
await writeFile(new URL('match_characters.json', HERE), JSON.stringify(charSidecar));
await writeFile(new URL('match_locations.json', HERE), JSON.stringify(locSidecar));
console.log(`crews=${crews.length} characters=${chars.length} locations=${locs.length}`);
