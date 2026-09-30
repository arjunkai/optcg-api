// import-artists.mjs — source: Limitless TCG (attribute in OPCanvs footer). No remote D1 writes.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { parseArtistList, artistQueryUrl, cardIdsFromGrid, parseMaxPage, ARTISTS_ENDPOINT }
  from './lib/limitless.mjs';

const OUT = new URL('./opcanvs_batches/', import.meta.url);
const HERE = new URL('./', import.meta.url);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const sq = (s) => `'${String(s).replace(/'/g, "''")}'`;
const slugify = (s) => s.normalize('NFKD').replace(/[̀-ͯ]/g, '')
  .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

async function getText(url, tries = 3) {
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(url, { headers: { 'User-Agent': 'OPCanvs/0.1 (+https://opcanvs.com)' }, signal: AbortSignal.timeout(30000) });
      if (res.ok) return res.text();
      throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      if (i === tries - 1) throw new Error(`${url}: ${e.message}`);
      await sleep(2000 * (i + 1));
    }
  }
}

const raw = await readFile(new URL('./cards_ids.json', HERE), 'utf8');
const validIds = new Set(
  JSON.parse(raw.slice(raw.indexOf('[')))
    .flatMap(b => (b.results ?? b)).map(r => r.id ?? r)
);
console.log(`valid card ids: ${validIds.size}`);

const names = parseArtistList(await getText(ARTISTS_ENDPOINT));
const illustratorRows = [], joinRows = [], unmatched = {}, failedArtists = [];
let illId = 0;

for (const name of names) {
  illId += 1;
  const slug = slugify(name);
  illustratorRows.push(`INSERT INTO illustrators (id,slug,name,source) VALUES (${illId},${sq(slug)},${sq(name)},'limitless');`);
  try {
    const page1 = await getText(artistQueryUrl(name, 1));
    const maxPage = parseMaxPage(page1);
    const ids = new Set(cardIdsFromGrid(page1));
    for (let p = 2; p <= maxPage; p++) { await sleep(1000); cardIdsFromGrid(await getText(artistQueryUrl(name, p))).forEach(id => ids.add(id)); }
    for (const id of ids) {
      if (validIds.has(id)) joinRows.push(`INSERT INTO card_illustrators (card_id,illustrator_id) VALUES (${sq(id)},${illId});`);
      else (unmatched[name] ??= []).push(id);
    }
    console.log(`${illId}/${names.length} ${name}: ${ids.size} ids, ${maxPage} page(s)`);
  } catch (e) {
    failedArtists.push({ name, error: e.message });
    console.log(`FAILED ${name}: ${e.message}`);
  }
  await sleep(1000);
}

const counts = joinRows.reduce((m, r) => { const id = r.match(/,(\d+)\);$/)[1]; m[id] = (m[id] || 0) + 1; return m; }, {});
const countRows = Object.entries(counts).map(([id, n]) => `UPDATE illustrators SET card_count=${n} WHERE id=${id};`);

await mkdir(OUT, { recursive: true });
await writeFile(new URL('illustrators.sql', OUT), illustratorRows.concat(countRows).join('\n'));
await writeFile(new URL('card_illustrators.sql', OUT), joinRows.join('\n'));
await writeFile(new URL('artists-unmatched.json', HERE), JSON.stringify(unmatched, null, 2));
await writeFile(new URL('artists-failed.json', HERE), JSON.stringify(failedArtists, null, 2));
console.log(`illustrators=${illustratorRows.length} joins=${joinRows.length} unmatched-keys=${Object.keys(unmatched).length} failed=${failedArtists.length}`);
