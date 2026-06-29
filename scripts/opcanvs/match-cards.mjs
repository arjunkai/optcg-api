// match-cards.mjs — derives card<->character and card<->location joins. No remote D1 writes.
import { readFile, writeFile } from 'node:fs/promises';
import { normalizeName } from './lib/normalize-name.mjs';

const HERE = new URL('./', import.meta.url);
const load = async (f) => {
  const raw = await readFile(new URL(f, HERE), 'utf8');
  const parsed = JSON.parse(raw.slice(raw.indexOf('[')));
  return parsed.flatMap(b => b.results ?? b);
};
const sq = (s) => `'${String(s).replace(/'/g, "''")}'`;
const TARGET = new Set(['character', 'leader']);

const [cards, characters, locations] = await Promise.all([
  load('./match_cards.json'), load('./match_characters.json'), load('./match_locations.json'),
]);

// character match: normalized card name -> character (primary). Exact-on-normalized only (conservative).
const charByNorm = new Map(characters.map(c => [c.name_normalized || normalizeName(c.name), c.id]));
const ccRows = [], unmatchedCards = [];
for (const card of cards) {
  const norm = normalizeName(card.name);
  const charId = charByNorm.get(norm);
  if (charId) ccRows.push(`INSERT INTO card_characters (card_id,character_id,role,match_method,confidence) VALUES (${sq(card.id)},${charId},'primary','exact_norm',1.0);`);
  else if (TARGET.has((card.category || '').toLowerCase())) unmatchedCards.push({ id: card.id, name: card.name, norm });
}

// location match: location name appears as a whole word in card name (text-scan method).
const lcRows = [];
for (const card of cards) {
  const hay = ` ${(card.name || '').toLowerCase()} `;
  for (const loc of locations) {
    const needle = (loc.name || '').toLowerCase().trim();
    if (needle.length >= 4 && hay.includes(` ${needle} `))
      lcRows.push(`INSERT INTO card_locations (card_id,location_id,match_method,confidence) VALUES (${sq(card.id)},${loc.id},'name_text_scan',0.6);`);
  }
}

const targetCount = cards.filter(c => TARGET.has((c.category || '').toLowerCase())).length;
const matched = ccRows.length;
const pct = targetCount ? (100 * matched / targetCount).toFixed(1) : 'n/a';
const report = [
  `# OPCanvs match coverage`,
  `- total cards: ${cards.length}`,
  `- character-target cards (Character+Leader): ${targetCount}`,
  `- card_characters matched (exact_norm): ${matched}  (${pct}% of target)`,
  `- card_locations matched (name_text_scan): ${lcRows.length}`,
  `- unmatched character cards: ${unmatchedCards.length} (candidates for data/opcanvs_overrides.json)`,
  ``, '## Unmatched character cards (first 100)', '```',
  ...unmatchedCards.slice(0, 100).map(u => `${u.id}\t${u.name}\t-> ${u.norm}`), '```',
].join('\n');

await writeFile(new URL('./opcanvs_batches/card_characters.sql', HERE), ccRows.join('\n'));
await writeFile(new URL('./opcanvs_batches/card_locations.sql', HERE), lcRows.join('\n'));
await writeFile(new URL('./opcanvs_batches/match-coverage.md', HERE), report);
console.log(report.split('\n').slice(0, 6).join('\n'));
