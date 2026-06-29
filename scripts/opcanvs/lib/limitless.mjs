const BASE = 'https://onepiece.limitlesstcg.com';

export function parseArtistList(jsonText) {
  const arr = JSON.parse(jsonText);
  if (!Array.isArray(arr)) throw new Error('artist list is not an array');
  return arr.map(String);
}

// Authoritative variant signal = the image filename suffix (shared by both systems).
export function cardIdFromImgSrc(src) {
  const m = /\/one-piece\/[^/]+\/([A-Z0-9]+-\d+(?:_p\d+)?|P-\d+)_EN\.webp/.exec(src || '');
  return m ? m[1] : null;
}

export function cardIdsFromGrid(html) {
  const ids = [];
  const re = /<img[^>]+class="card[^"]*"[^>]+src="([^"]+)"/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const id = cardIdFromImgSrc(m[1]);
    if (id) ids.push(id);
  }
  return ids;
}

export function parseMaxPage(html) {
  const m = /<ul class="pagination"[^>]*\bdata-max="(\d+)"/.exec(html || '');
  return m ? parseInt(m[1], 10) : 1;
}

// Keep `!artist:` and the quotes literal; encode only the name.
export function artistQueryUrl(name, page = 1) {
  return `${BASE}/cards?q=!artist:%22${encodeURIComponent(name)}%22&page=${page}`;
}

export const ARTISTS_ENDPOINT = `${BASE}/api/cards/artists`;
