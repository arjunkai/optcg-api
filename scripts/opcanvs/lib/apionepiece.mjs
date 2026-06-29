const V2 = 'https://api.api-onepiece.com/v2';
// Base URLs for the three lore endpoints used by the importer.
export const BASES = {
  characters: `${V2}/characters/en`,
  crews:      `${V2}/crews/en`,
  locates:    `${V2}/locates/en`,   // NOTE: endpoint is /locates, not /locations
};

// Whole-value rules use ^…$ anchors; substring rules use \b word-boundary anchors.
const REPLACEMENTS = [
  [/\bans\b/gi, 'years'],
  [/^vivant$/i, 'alive'],
  [/^mort(e)?$/i, 'deceased'],
  [/\bZoan Mythique\b/gi, 'Mythical Zoan'],
  [/\bParamécie\b/gi, 'Paramecia'],
];

/**
 * Clean a French-inflected string from api-onepiece into readable English.
 * @param {string} str  Raw value from the API (may be null/undefined).
 * @returns {string}    Cleaned string, or '' for nullish/non-string input.
 */
export function cleanEnString(str) {
  if (!str || typeof str !== 'string') return '';
  let out = str;
  for (const [re, rep] of REPLACEMENTS) out = out.replace(re, rep);
  return out.trim();
}
