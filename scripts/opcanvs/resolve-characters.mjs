// OPCanvs character resolver + SQL generator.
// Roster is DERIVED FROM CARD NAMES (so every card → a character page = 100%).
// Identity/grouping via Wikidata (CC0) + Fandom redirect-resolution + alias map + card-name parsing.
// Emits characters.sql + card_characters.sql (no remote D1). api-onepiece is NOT used.
import { readFile, writeFile } from 'node:fs/promises';
const HERE = new URL('./', import.meta.url);
const OUT = new URL('./opcanvs_batches/', import.meta.url);
const UA = { 'User-Agent': 'OPCanvs/0.1 (+https://opcanvs.com)' };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const norm = (s) => !s ? '' : s.normalize('NFKD').replace(/[̀-ͯ]/g, '')
  .replace(/[.・]/g, ' ').replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
const hasJP = (s) => /[぀-ヿ一-龯]/.test(s || '');
const deWano = (n) => n.replace(/^o /, '').replace(/ (taro|tarou|juro|juurou|gorou)$/, '');
// JA key: NFKC folds full-width Ｄ→D, unifies middle-dot variants, drops spaces — so a card's
// full-width-D katakana name matches Wikidata's half-width JA label (prevents duplicate char pages).
const jaNorm = (s) => !s ? '' : s.normalize('NFKC').replace(/[・·•‧]/g, '・').replace(/\s+/g, '').trim();
const sq = (s) => s == null || s === '' ? 'NULL' : "'" + String(s).replace(/'/g, "''") + "'";

// curated alias / persona / spelling map (normalized card-form core -> canonical EN as it appears on the wikis)
const ALIAS = {
  akainu:'Sakazuki', kizaru:'Borsalino', aokiji:'Kuzan', fujitora:'Issho', ryokugyu:'Aramaki',
  'hitokiri kamazo':'Killer', kamazo:'Killer', gyukimaru:'Kawamatsu', sogeking:'Usopp', 'god usopp':'Usopp',
  'sun god nika':'Monkey D. Luffy', nika:'Monkey D. Luffy', joker:'Donquixote Doflamingo',
  corazon:'Donquixote Rosinante', kyoshiro:'Denjiro', komurasaki:'Kozuki Hiyori', shutenmaru:'Ashura Doji',
  hakuba:'Cavendish', 'dark king':'Silvers Rayleigh', whitebeard:'Edward Newgate', blackbeard:'Marshall D. Teach',
  'big mom':'Charlotte Linlin', 'tenguyama hitetsu':'Kozuki Sukiyaki', conney:'Jewelry Bonney',
  // Wano disguises / personas / spelling variants (recovered from the bare tail)
  'o robi':'Nico Robin', 'fra nosuke':'Franky', franosuke:'Franky', 'hone kichi':'Brook', 'uso hachi':'Usopp',
  'san gorou':'Sanji', 'chopa emon':'Tony Tony Chopper', 'o tama':'Tama', 'o kiku':'Kikunojo',
  'luffy taro':'Monkey D. Luffy', 'zoro juro':'Roronoa Zoro',
};

// ---------- Wikidata ----------
const wdQuery = `SELECT ?item ?en ?ja ?enAlt ?jaAlt WHERE {
  ?item wdt:P1441 wd:Q710324 .
  OPTIONAL { ?item rdfs:label ?en FILTER(LANG(?en)="en") }
  OPTIONAL { ?item rdfs:label ?ja FILTER(LANG(?ja)="ja") }
  OPTIONAL { ?item skos:altLabel ?enAlt FILTER(LANG(?enAlt)="en") }
  OPTIONAL { ?item skos:altLabel ?jaAlt FILTER(LANG(?jaAlt)="ja") } }`;
const wdRes = await fetch('https://query.wikidata.org/sparql', { method: 'POST',
  headers: { ...UA, 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/sparql-results+json' },
  body: 'query=' + encodeURIComponent(wdQuery), signal: AbortSignal.timeout(60000) });
const wdRows = (await wdRes.json()).results.bindings;
const wdEn = new Map(), wdJa = new Map();                   // key -> canonical EN
const canonJa = new Map(), canonQid = new Map();            // canonical EN -> ja label / qid
for (const r of wdRows) {
  const canon = r.en?.value || r.item.value;
  const qid = r.item.value.split('/').pop();
  if (!canonQid.has(canon)) canonQid.set(canon, qid);
  if (r.ja?.value && !canonJa.has(canon)) canonJa.set(canon, r.ja.value);
  for (const f of ['en','enAlt']) if (r[f]?.value) { wdEn.set(norm(r[f].value), canon); wdEn.set(deWano(norm(r[f].value)), canon); }
  for (const f of ['ja','jaAlt']) if (r[f]?.value) wdJa.set(jaNorm(r[f].value), canon);
}

// raw (original-case) candidate strings for Fandom titles
function rawCandidates(name) {
  const out = new Set();
  const stripQ = (x) => x.replace(/["“”][^"“”]*["“”]/g, ' ').replace(/\s+/g, ' ').trim();
  const parens = [...name.matchAll(/[\(（]([^\)）]+)[\)）]/g)].map(m => m[1].trim());
  const noParen = name.replace(/[\(（][^\)）]*[\)）]/g, ' ').replace(/\s+/g, ' ').trim();
  for (const base of [name, noParen, stripQ(name), stripQ(noParen), ...parens]) {
    if (!base) continue;
    out.add(base);
    out.add(base.replace(/\./g, '. ').replace(/\s+/g, ' ').trim());
    out.add(base.replace(/[.]/g, ' ').replace(/\s+/g, ' ').trim());
  }
  return [...out].filter(Boolean);
}
const normCands = (n) => [...new Set(rawCandidates(n).flatMap(c => {
  const nc = norm(c); return [nc, deWano(nc), ALIAS[nc] ? norm(ALIAS[nc]) : null, ALIAS[deWano(nc)] ? norm(ALIAS[deWano(nc)]) : null];
}).filter(Boolean))];

// ---------- cards ----------
const raw = await readFile(new URL('./match_cards.json', HERE), 'utf8');
const cards = JSON.parse(raw.slice(raw.indexOf('['))).flatMap(b => b.results ?? b);
const charCards = cards.filter(c => ['character', 'leader'].includes(String(c.category || '').toLowerCase()));
const names = [...new Set(charCards.map(c => c.name))];

// ---------- pass 1: Wikidata ----------
const resolution = {}; const wdMiss = [];
for (const n of names) {
  if (hasJP(n)) { const hit = wdJa.get(jaNorm(n)); if (hit) { resolution[n] = { canonical: hit, source: 'wikidata' }; continue; } }
  const hit = normCands(n).map(c => wdEn.get(c)).find(Boolean);
  if (hit) resolution[n] = { canonical: hit, source: 'wikidata' }; else wdMiss.push(n);
}

// ---------- pass 2: Fandom redirect-resolution (+ combo split) ----------
function buildResolver(q) {
  const nm = {}, rd = {}; (q.normalized || []).forEach(x => nm[x.from] = x.to); (q.redirects || []).forEach(x => rd[x.from] = x.to);
  const exist = new Set(); Object.values(q.pages || {}).forEach(p => { if (p.pageid !== undefined && p.missing === undefined) exist.add(p.title); });
  return (input) => { let t = nm[input] || input; t = rd[t] || t; return exist.has(t) ? t : null; };
}
const splitCombo = (n) => /[&＆]/.test(n) ? n.split(/\s*[&＆]\s*/).map(s => s.trim()).filter(Boolean) : [n];
const candToTitle = new Map();
for (const n of wdMiss) for (const part of splitCombo(n)) for (const c of rawCandidates(part)) if (!hasJP(c)) candToTitle.set(c, null);
const allCands = [...candToTitle.keys()];
for (let i = 0; i < allCands.length; i += 50) {
  const batch = allCands.slice(i, i + 50);
  const url = 'https://onepiece.fandom.com/api.php?action=query&format=json&redirects=1&titles=' + encodeURIComponent(batch.join('|'));
  let q; for (let t = 0; t < 3; t++) { try { const r = await fetch(url, { headers: UA, signal: AbortSignal.timeout(30000) }); if (r.ok) { q = (await r.json()).query; break; } } catch { await sleep(1500 * (t + 1)); } }
  if (!q) { console.log('fandom batch failed at', i); continue; }
  const resolve = buildResolver(q);
  for (const c of batch) { const f = resolve(c); if (f) candToTitle.set(c, f); }
  await sleep(300);
}
for (const n of wdMiss) {
  const parts = splitCombo(n);
  const titles = parts.map(p => rawCandidates(p).map(c => candToTitle.get(c)).find(Boolean)).filter(Boolean);
  if (titles.length) resolution[n] = { canonical: titles.join(' & '), source: parts.length > 1 ? 'fandom-combo' : 'fandom', fandomTitles: titles };
  else resolution[n] = { canonical: n, source: 'card-original' };
}

// ---------- build roster + SQL ----------
// roster = distinct individual canonical names (combos split into members)
const memberSource = new Map();   // canonical individual -> best source
const rank = { wikidata: 3, fandom: 2, 'fandom-combo': 2, 'card-original': 1 };
for (const n of names) {
  const r = resolution[n];
  for (const m of r.canonical.split(' & ').map(s => s.trim())) {
    if (!memberSource.has(m) || rank[r.source] > rank[memberSource.get(m)]) memberSource.set(m, r.source === 'fandom-combo' ? 'fandom' : r.source);
  }
}
const roster = [...memberSource.keys()].sort((a, b) => a.localeCompare(b));
const charId = new Map(); roster.forEach((m, i) => charId.set(m, i + 1));

const charRows = roster.map(m => {
  const src = memberSource.get(m);
  // fandom title for fandom-sourced members: find a resolution whose canonical includes m
  let fTitle = null;
  if (src === 'fandom') { for (const n of names) { const r = resolution[n]; if (r.fandomTitles && r.canonical.split(' & ').map(s=>s.trim()).includes(m)) { fTitle = r.fandomTitles[r.canonical.split(' & ').map(s=>s.trim()).indexOf(m)] || m; break; } } }
  return `INSERT INTO characters (id,source_id,name,name_normalized,name_ja,wikidata_qid,fandom_title,source) VALUES (${charId.get(m)},NULL,${sq(m)},${sq(norm(m))},${sq(canonJa.get(m) || null)},${sq(canonQid.get(m) || null)},${sq(fTitle)},${sq(src)});`;
});

const ccRows = []; const seenPair = new Set(); let dupSkipped = 0;
for (const card of charCards) {
  const r = resolution[card.name]; if (!r) continue;
  for (const m of [...new Set(r.canonical.split(' & ').map(s => s.trim()))]) {
    const id = charId.get(m); if (!id) continue;
    const key = card.id + '|' + id;
    if (seenPair.has(key)) { dupSkipped++; continue; }   // same card listed twice, or combo member repeat
    seenPair.add(key);
    ccRows.push(`INSERT INTO card_characters (card_id,character_id,role,match_method,confidence) VALUES (${sq(card.id)},${id},'primary',${sq(r.source)},${r.source === 'card-original' ? 0.5 : 1.0});`);
  }
}

await writeFile(new URL('characters.sql', OUT), charRows.join('\n'));
await writeFile(new URL('card_characters.sql', OUT), ccRows.join('\n'));

const by = (s) => names.filter(n => resolution[n].source.startsWith(s)).length;
const bare = names.filter(n => resolution[n].source === 'card-original');
const enriched = names.length - bare.length;
const rpt = [
  '# OPCanvs character resolution coverage',
  `- distinct card-character names: ${names.length}`,
  `- enriched (wikidata+fandom): ${enriched} (${(100 * enriched / names.length).toFixed(1)}%)`,
  `  - wikidata: ${by('wikidata')} | fandom: ${by('fandom')} (combo ${names.filter(n=>resolution[n].source==='fandom-combo').length})`,
  `- bare card-original pages: ${bare.length} (${(100 * bare.length / names.length).toFixed(1)}%) — still get a character page`,
  `- roster (distinct characters incl. combo members): ${roster.length}`,
  `- card_characters rows: ${ccRows.length} (covers ${new Set(charCards.map(c=>c.id)).size} distinct Character+Leader cards = 100% of those; ${dupSkipped} dup pairs deduped)`,
  `- NOTE: Event/Stage/DON cards intentionally get no character row (not character portraits).`,
  '', '## Bare card-original names (curation candidates)', '```', ...bare.sort(), '```',
].join('\n');
await writeFile(new URL('character-coverage.md', OUT), rpt);
console.log(rpt.split('\n').slice(0, 8).join('\n'));
