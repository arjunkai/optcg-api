const V2 = 'https://api.api-onepiece.com/v2';
export const BASES = {
  characters: `${V2}/characters/en`,
  crews:      `${V2}/crews/en`,
  locates:    `${V2}/locates/en`,   // NOTE: endpoint is /locates, not /locations
};

const REPLACEMENTS = [
  [/\bans\b/gi, 'years'],
  [/^vivant$/i, 'alive'],
  [/^mort(e)?$/i, 'deceased'],
  [/\bZoan Mythique\b/gi, 'Mythical Zoan'],
  [/\bParamécie\b/gi, 'Paramecia'],
  [/\bLogia\b/gi, 'Logia'],
];

export function cleanEnString(str) {
  if (!str || typeof str !== 'string') return '';
  let out = str;
  for (const [re, rep] of REPLACEMENTS) out = out.replace(re, rep);
  return out.trim();
}
