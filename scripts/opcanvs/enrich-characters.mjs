// OPCanvs character enrichment: pull epithet / devil fruit / bounty /
// affiliation / occupation / status from the One Piece Fandom wiki's rendered
// "portable infobox" (action=parse), which resolves the tabbed-page
// transclusions the raw wikitext hides. FREE (Fandom API, no key).
//
// Reads scripts/opcanvs/scrap e_input/chars.json ([{id,name,fandom_title}]),
// tries fandom_title || name as the page (redirects=1), extracts the infobox
// data-source items, cleans them, and writes:
//   scripts/opcanvs/opcanvs_batches/character_enrich.sql  (idempotent UPDATEs)
//   scripts/opcanvs/character-enrich-coverage.md
//
// Usage: node scripts/opcanvs/enrich-characters.mjs [--limit N] [--all]
//   default: sample the first 25 (validate coverage + accuracy first).
import { readFile, writeFile } from 'node:fs/promises';

const HERE = new URL('./', import.meta.url);
const UA = { 'User-Agent': 'OPCanvs/0.1 (+https://opcanvs.com)' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const args = process.argv.slice(2);
const ALL = args.includes('--all');
const LIMIT = (() => { const i = args.indexOf('--limit'); return i >= 0 ? Number(args[i + 1]) : 25; })();
const CHARS_PATH = new URL('./scratch_chars.json', HERE);

const sq = (s) => (s == null || s === '' ? 'NULL' : "'" + String(s).replace(/'/g, "''") + "'");

// Strip a rendered HTML fragment to clean text: drop <sup> reference markers,
// all tags, unescape entities, remove leftover [ n ] ref brackets, collapse ws.
function clean(htmlStr) {
  if (!htmlStr) return '';
  let s = htmlStr
    .replace(/<sup[\s\S]*?<\/sup>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<br\s*\/?>/gi, ' ; ')          // list items separated by <br>
    .replace(/<\/li>/gi, ' ; ')
    .replace(/<[^>]+>/g, ' ');
  s = s
    .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#\d+;/g, ' ');
  s = s.replace(/\[\s*\d+\s*\]/g, ' ');       // [ 1 ] ref markers
  return s.replace(/\s+/g, ' ').trim();
}

// The infobox lists (affiliation/occupation) render items separated by <br> or
// ";"; take the primary (first) entry. clean() has already flattened tags.
const stripQuotes = (s) => s.replace(/^[\s"'“”‘’]+|[\s"'“”‘’]+$/g, '');
const firstItem = (s) => stripQuotes(clean(s).split(/;|·/)[0].trim());
// epithet / fruit values carry a trailing Japanese parenthetical — drop it.
const beforeParen = (s) => stripQuotes(clean(s).split(/[;(]/)[0].trim().replace(/[,;]+$/, ''));
// bounty is a run of amounts (current first); grab the first comma-number.
const firstBounty = (s) => { const m = clean(s).match(/[\d,]{4,}/); return m ? m[0] : ''; };

// Pull { data-source -> raw inner html of pi-data-value } from the infobox.
function extractInfobox(htmlText) {
  const aside = htmlText.match(/<aside[^>]*class="[^"]*portable-infobox[\s\S]*?<\/aside>/i);
  if (!aside) return null;
  const body = aside[0];
  const out = {};
  const re = /data-source="([^"]+)"[^>]*>([\s\S]*?)(?=<(?:div|section|h2|h3)\b[^>]*data-source=|<\/aside>)/gi;
  let m;
  while ((m = re.exec(body))) {
    const key = m[1];
    const valMatch = m[2].match(/class="pi-data-value[^"]*"[^>]*>([\s\S]*?)<\/div>/i);
    if (valMatch && !(key in out)) out[key] = valMatch[1];
  }
  return out;
}

async function fetchInfobox(page) {
  const url = 'https://onepiece.fandom.com/api.php?action=parse&prop=text&format=json&redirects=1&page=' + encodeURIComponent(page);
  for (let t = 0; t < 3; t++) {
    try {
      const r = await fetch(url, { headers: UA, signal: AbortSignal.timeout(30000) });
      if (r.status === 404) return null;
      if (r.ok) {
        const j = await r.json();
        if (j.error) return null;
        return j.parse?.text?.['*'] || null;
      }
    } catch { /* retry */ }
    await sleep(1200 * (t + 1));
  }
  return null;
}

// Fallback: when name/fandom_title doesn't resolve to a page with a character
// infobox, ask Fandom's search for the closest title and try that. Self-
// validating: a group/animal/concept page has no character infobox, so it
// still yields nothing (no fabricated data).
async function searchTitle(name) {
  const url = 'https://onepiece.fandom.com/api.php?action=opensearch&limit=1&namespace=0&format=json&search=' + encodeURIComponent(name);
  try {
    const r = await fetch(url, { headers: UA, signal: AbortSignal.timeout(20000) });
    if (r.ok) { const j = await r.json(); return (j[1] && j[1][0]) || null; }
  } catch { /* ignore */ }
  return null;
}

const chars = JSON.parse(await readFile(CHARS_PATH, 'utf8'));
const work = ALL ? chars : chars.slice(0, LIMIT);
console.log(`Enriching ${work.length} of ${chars.length} characters${ALL ? ' (FULL)' : ' (sample)'}...`);

const updates = [];
const cov = { epithet: 0, fruit_name: 0, fruit_type: 0, bounty: 0, affiliation: 0, job: 0, status: 0 };
let hit = 0, miss = 0;
const samplePreview = [];

for (let i = 0; i < work.length; i++) {
  const c = work[i];
  const page = c.fandom_title || c.name;
  let html = await fetchInfobox(page);
  let box = html ? extractInfobox(html) : null;
  if (!box) {
    // Try Fandom search for the correct title (recovers name/title mismatches).
    const alt = await searchTitle(c.name);
    if (alt && alt !== page) { html = await fetchInfobox(alt); box = html ? extractInfobox(html) : null; }
  }
  if (!box) { miss++; await sleep(250); continue; }

  const fields = {
    epithet: beforeParen(box.epithet),
    fruit_name: beforeParen(box.dfename),
    fruit_type: beforeParen(box.dftype),
    bounty: firstBounty(box.bounty),
    affiliation: firstItem(box.affiliation),
    job: firstItem(box.occupation),
    status: clean(box.status),
  };
  const set = [];
  for (const [k, v] of Object.entries(fields)) {
    if (v) { set.push(`${k} = ${sq(v)}`); cov[k]++; }
  }
  if (set.length) {
    updates.push(`UPDATE characters SET ${set.join(', ')} WHERE id = ${c.id};`);
    hit++;
    if (samplePreview.length < 60) samplePreview.push({ name: c.name, ...fields });
  } else miss++;
  await sleep(250);
  if ((i + 1) % 25 === 0) console.log(`  ${i + 1}/${work.length} (hit ${hit}, miss ${miss})`);
}

await writeFile(new URL('./opcanvs_batches/character_enrich.sql', HERE), updates.join('\n') + '\n');
const n = work.length;
const rpt = [
  '# Character enrichment coverage',
  `- attempted: ${n} | infobox hit: ${hit} (${(100 * hit / n).toFixed(1)}%) | miss: ${miss}`,
  ...Object.entries(cov).map(([k, v]) => `- ${k}: ${v} (${(100 * v / n).toFixed(1)}%)`),
  '', '## Sample (spot-check these against the wiki before scaling)', '```',
  ...samplePreview.map((s) => JSON.stringify(s)), '```',
].join('\n');
await writeFile(new URL('./character-enrich-coverage.md', HERE), rpt);
console.log('\n' + rpt);
