// scripts/opcanvs/lib/normalize-name.mjs
export function normalizeName(str) {
  if (!str || typeof str !== 'string') return '';
  return str
    .normalize('NFKD').replace(/[̀-ͯ]/g, '') // strip combining diacritical marks
    .replace(/[.・]/g, ' ')                          // periods + katakana middle dot -> space
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')                   // drop other punctuation
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}
