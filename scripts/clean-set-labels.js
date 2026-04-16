/**
 * Clean set labels and add type field.
 *
 * Transforms:
 *   "BOOSTER PACK -ROMANCE DAWN- [OP-01]"  →  label: "Romance Dawn", type: "booster"
 *   "STARTER DECK -Straw Hat Crew- [ST-01]" →  label: "Straw Hat Crew", type: "starter"
 *
 * Usage:
 *   node scripts/clean-set-labels.js           # preview changes
 *   node scripts/clean-set-labels.js --apply   # write to data/sets.json
 */

import { readFileSync, writeFileSync } from 'fs';

const sets = JSON.parse(readFileSync('data/sets.json', 'utf-8'));

function classifyType(label, setId) {
  const upper = label.toUpperCase();
  if (upper.startsWith('PREMIUM BOOSTER')) return 'premium';
  if (upper.startsWith('EXTRA BOOSTER')) return 'extra';
  if (upper.includes('ULTRA DECK')) return 'ultra';
  if (upper.startsWith('STARTER DECK')) {
    if (upper.includes(' EX')) return 'starter-ex';
    return 'starter';
  }
  if (upper.startsWith('BOOSTER PACK')) return 'booster';
  if (setId === '569901') return 'promo';
  if (setId === '569801') return 'other';
  return 'other';
}

function cleanLabel(label, setId) {
  // Special cases
  if (setId === '569901') return 'Promo Cards';
  if (setId === '569801') return 'Other Products';

  // Extract the name between dashes: "BOOSTER PACK -ROMANCE DAWN- [OP-01]" → "ROMANCE DAWN"
  const dashMatch = label.match(/-([^-]+)-/);
  if (dashMatch) {
    let name = dashMatch[1].trim();
    // Title case it properly
    return toTitleCase(name);
  }

  // Fallback: strip prefix and brackets
  return label
    .replace(/^(BOOSTER PACK|STARTER DECK|EXTRA BOOSTER|PREMIUM BOOSTER|ULTRA DECK)\s*/i, '')
    .replace(/\s*\[.*?\]\s*$/, '')
    .replace(/^-\s*/, '')
    .replace(/\s*-\s*$/, '')
    .trim();
}

function toTitleCase(str) {
  // Handle special cases
  const specialCases = {
    'ONE PIECE CARD THE BEST': 'The Best',
    'ONE PIECE CARD THE BEST vol.2': 'The Best Vol. 2',
    'ONE PIECE HEROINES EDITION': 'Heroines Edition',
    'ONE PIECE FILM edition': 'Film Edition',
    'MEMORIAL COLLECTION': 'Memorial Collection',
    'Anime 25th Collection': 'Anime 25th Collection',
    '3D2Y': '3D2Y',
  };

  for (const [key, val] of Object.entries(specialCases)) {
    if (str.toUpperCase() === key.toUpperCase()) return val;
  }

  // Standard title case — lowercase everything first, then capitalize
  const smallWords = new Set(['of', 'the', 'in', 'on', 'and', 'a', 'an', 'for']);
  return str
    .toLowerCase()
    .split(/\s+/)
    .map((word, i) => {
      // Preserve character name dots: "Monkey.D.Luffy" -> keep original casing
      if (word.includes('.')) {
        return word.split('.').map(seg => seg ? seg.charAt(0).toUpperCase() + seg.slice(1) : '').join('.');
      }
      // Handle slash-separated words: "green/yellow" -> "Green/Yellow"
      if (word.includes('/')) {
        return word.split('/').map(seg => seg ? seg.charAt(0).toUpperCase() + seg.slice(1) : '').join('/');
      }
      // Small words stay lowercase unless first word
      if (i > 0 && smallWords.has(word)) return word;
      // Capitalize first letter
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(' ');
}

// Process
const cleaned = sets.map(s => {
  const type = classifyType(s.label, s.set_id);
  const newLabel = cleanLabel(s.label, s.set_id);
  return {
    ...s,
    label: newLabel,
    type,
    original_label: s.label,
  };
});

// Preview
console.log('Set label cleanup preview:\n');
for (const s of cleaned) {
  const changed = s.original_label !== s.label;
  console.log(`${s.set_id.padEnd(12)} [${s.type.padEnd(10)}] ${s.label}`);
  if (changed) {
    console.log(`${''.padEnd(12)} was: ${s.original_label}`);
  }
}

if (process.argv.includes('--apply')) {
  // Remove original_label before saving
  const output = cleaned.map(({ original_label, ...rest }) => rest);
  writeFileSync('data/sets.json', JSON.stringify(output, null, 2));
  console.log('\n✓ Written to data/sets.json');
} else {
  console.log('\nRun with --apply to save changes.');
}
