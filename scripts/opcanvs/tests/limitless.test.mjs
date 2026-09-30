import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseArtistList, cardIdFromImgSrc, cardIdsFromGrid, parseMaxPage, artistQueryUrl }
  from '../lib/limitless.mjs';

test('parseArtistList returns the flat name array', () => {
  assert.deepEqual(parseArtistList('["Ai Nina","akagi","Demizu Posuka"]'),
    ['Ai Nina', 'akagi', 'Demizu Posuka']);
});

test('cardIdFromImgSrc: base card has no suffix', () => {
  assert.equal(cardIdFromImgSrc(
    'https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com/one-piece/OP15/OP15-091_EN.webp'),
    'OP15-091');
});
test('cardIdFromImgSrc: parallel keeps _pN', () => {
  assert.equal(cardIdFromImgSrc('.../one-piece/OP16/OP16-026_p1_EN.webp'), 'OP16-026_p1');
  assert.equal(cardIdFromImgSrc('.../one-piece/OP10/OP10-045_p2_EN.webp'), 'OP10-045_p2');
});
test('cardIdFromImgSrc: promo + non-OP sets', () => {
  assert.equal(cardIdFromImgSrc('.../one-piece/P/P-102_EN.webp'), 'P-102');
  assert.equal(cardIdFromImgSrc('.../one-piece/EB04/EB04-001_EN.webp'), 'EB04-001');
});

test('cardIdsFromGrid extracts every result by image src', () => {
  const html = `
   <div class="card-search-grid">
     <a href="/cards/OP16-026?v=1"><img class="card shadow"
        src="https://x/one-piece/OP16/OP16-026_p1_EN.webp" width=600 height=838></a>
     <a href="/cards/OP15-091"><img class="card shadow"
        src="https://x/one-piece/OP15/OP15-091_EN.webp" width=600 height=838></a>
   </div>`;
  assert.deepEqual(cardIdsFromGrid(html), ['OP16-026_p1', 'OP15-091']);
});

test('parseMaxPage reads data-max', () => {
  assert.equal(parseMaxPage('<ul class="pagination" data-current="1" data-max="2"></ul>'), 2);
});
test('parseMaxPage defaults to 1 when no pagination block', () => {
  assert.equal(parseMaxPage('<div>no pages</div>'), 1);
});

test('artistQueryUrl keeps quotes and encodes multi-word names', () => {
  assert.equal(artistQueryUrl('Demizu Posuka', 1),
    'https://onepiece.limitlesstcg.com/cards?q=!artist:%22Demizu%20Posuka%22&page=1');
});
