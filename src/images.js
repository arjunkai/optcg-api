const IMG_HEADERS = {
  'Content-Type': 'image/png',
  'Cache-Control': 'public, max-age=86400',
  'Access-Control-Allow-Origin': '*',
};

// Bandai's CDN occasionally hot-link-blocks the CF Worker IP ranges and
// fails the request slowly (30+ second hang then non-200). Without a
// timeout the user sees a 30s spinner on every uncached card image. We
// abort the direct fetch after 5s and fall through to wsrv.nl, which can
// reach Bandai on our behalf and serves the response from its own CDN.
// Lowered 5000 -> 2000 on 2026-05-31: when Bandai is actively hot-link-
// blocking the Worker IP, a 5s wait per uncached image stacks to a ~8s
// load (5s dead wait + ~3s wsrv). 2s fails fast to the wsrv fallback so
// blocked-period loads are ~3-4s instead of ~8s. Still ample for a
// healthy Bandai response. Revert toward 5s once the block clears if it
// causes premature wsrv fallback on slow-but-valid responses.
const UPSTREAM_TIMEOUT_MS = 2000;
const WSRV_TIMEOUT_MS = 10000;

async function fetchWithTimeout(url, init = {}, timeoutMs = UPSTREAM_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(id);
  }
}

// `seen.transient` is set when an attempt failed for a reason that may pass
// (timeout, network error, 5xx, 403/429 hot-link block) rather than a clean
// 404/410. Callers only remember a miss when nothing was transient.
function noteStatus(seen, status) {
  if (seen && status !== 404 && status !== 410) seen.transient = true;
}

async function proxyAndCache(url, requestHeaders = {}, seen = null, directTimeoutMs = UPSTREAM_TIMEOUT_MS) {
  const cacheKey = new Request(url);
  const cache = caches.default;
  let cached = await cache.match(cacheKey);
  if (cached) return cached;

  // First try direct upstream (Bandai / TCGPlayer / whatever the caller
  // points at). Short timeout — if it doesn't respond in 5s, drop and try
  // the wsrv.nl fallback. Treat thrown errors and non-200 the same.
  let upstream = null;
  try {
    const res = await fetchWithTimeout(url, { headers: requestHeaders }, directTimeoutMs);
    if (res.status === 200) upstream = res;
    else noteStatus(seen, res.status);
  } catch (_e) {
    upstream = null;
    noteStatus(seen, 0);
  }

  // Fallback through wsrv.nl. It re-proxies arbitrary URLs through its
  // own CDN and absorbs intermittent upstream hot-link-blocking. We pass
  // output=png so the result matches IMG_HEADERS. maxage=30d to keep the
  // wsrv.nl edge cache warm.
  if (!upstream) {
    const proxied = `https://wsrv.nl/?url=${encodeURIComponent(url)}&output=png&maxage=30d`;
    try {
      const res = await fetchWithTimeout(proxied, {}, WSRV_TIMEOUT_MS);
      if (res.status === 200) upstream = res;
      else noteStatus(seen, res.status);
    } catch (_e) {
      upstream = null;
      noteStatus(seen, 0);
    }
  }

  if (!upstream) return null;
  // A 200 that isn't an image (an HTML soft-404, an error page) must not be
  // relabelled image/png and persisted to R2 forever.
  const contentType = upstream.headers.get('content-type') || '';
  if (!contentType.startsWith('image/')) { noteStatus(seen, 0); return null; }
  // Keep the real type (TCGPlayer DON art is JPEG); callers that persist to R2
  // store the bytes, so this only affects the response header.
  return new Response(upstream.body, { headers: { ...IMG_HEADERS, 'Content-Type': contentType } });
}

// Card and set ids are [A-Za-z0-9_-] (OP01-001, OP05-119_p8, P-001_jp1,
// DON-208, OP14-EB04, 550302). Anything else — slashes from a decoded %2F,
// dots, '?' — would be spliced into the upstream Bandai URL and the R2 key,
// so reject it before any fetch or write.
const SAFE_ID = /^[A-Za-z0-9_-]{1,40}$/;

// Ids no upstream could serve are remembered per colo for a while, so a
// missing or made-up id costs one R2 get instead of up to 4 upstream fetches
// (Bandai EN/JA + wsrv each) on every request. Short enough that a newly
// published card shows up soon after Bandai posts it.
const MISS_TTL_S = 1800;
const missKey = (kind, id) => new Request(`https://image-miss.local/${kind}/${id}`);
async function knownMiss(kind, id) {
  try { return Boolean(await caches.default.match(missKey(kind, id))); } catch { return false; }
}
function notFound(c, kind, id, seen = null) {
  // A timeout or upstream block may clear on the next request: answer 404
  // without remembering it, and don't let browsers or wsrv keep it.
  if (seen?.transient) return c.body(null, 404, { 'Cache-Control': 'no-store' });
  c.executionCtx.waitUntil(
    caches.default.put(missKey(kind, id), new Response('1', {
      headers: { 'Cache-Control': `max-age=${MISS_TTL_S}` },
    })).catch(() => {})
  );
  return c.body(null, 404, { 'Cache-Control': `public, max-age=${MISS_TTL_S}` });
}

// Fetch image bytes via proxyAndCache and reject empty bodies. Bandai (or wsrv
// on its behalf) can hand back a 200 with a 0-byte body when an id is absent on
// that host; caching that empty "success" used to poison R2/edge with a broken
// image. Returns an ArrayBuffer with real bytes, or null.
async function fetchImageBytes(url, referer, seen = null) {
  const res = await proxyAndCache(url, referer ? { Referer: referer } : {}, seen);
  if (!res) return null;
  const buf = await res.arrayBuffer();
  return buf && buf.byteLength > 0 ? buf : null;
}

// Proactively warm a regular OPTCG card image into R2 (used by the cron sweep
// in cron.js). Goes STRAIGHT through wsrv.nl: the Worker's own IP is
// hot-link-blocked by Bandai, so the request-path direct fetch almost always
// dead-waits then falls through to wsrv anyway — skipping it here saves the
// 2s timeout per card and a subrequest. Tries the EN host, then the JA host
// (JA-exclusive variants only exist there), same order as the request path.
// Idempotent (no-op if already in R2), never throws. Returns
// 'cached' | 'warmed' | 'failed'.
async function wsrvImageBytes(url) {
  const proxied = `https://wsrv.nl/?url=${encodeURIComponent(url)}&output=png&maxage=30d`;
  const res = await fetchWithTimeout(proxied, {}, WSRV_TIMEOUT_MS);
  if (res.status !== 200 || !(res.headers.get('content-type') || '').startsWith('image/')) return null;
  const buf = await res.arrayBuffer();
  return buf.byteLength > 0 ? buf : null;
}

export async function warmCardImage(env, cardId) {
  try {
    if (!env?.IMAGES) return 'failed';
    if (await env.IMAGES.head(`cards/${cardId}.png`)) return 'cached';
    const buf =
      (await wsrvImageBytes(`https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`)) ||
      (await wsrvImageBytes(`https://www.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`));
    if (!buf) return 'failed';
    await env.IMAGES.put(`cards/${cardId}.png`, buf, { httpMetadata: { contentType: 'image/png' } });
    return 'warmed';
  } catch {
    return 'failed';
  }
}

export function registerImageRoutes(app) {
  app.get('/images/:card_id', async (c) => {
    const cardId = c.req.param('card_id');
    if (!SAFE_ID.test(cardId)) return c.body(null, 404);
    // Japanese art lives under a separate R2 prefix (cards/ja/:id) and comes
    // from the JA official host. DON!! images are language-neutral synthetic
    // scans, so they ignore ?lang and always use the EN path.
    const lang = (c.req.query('lang') === 'ja' && !cardId.startsWith('DON-')) ? 'ja' : 'en';
    const r2Key = lang === 'ja' ? `cards/ja/${cardId}.png` : `cards/${cardId}.png`;

    // 1. R2 first (high-res curated images, including DON PDFs). Lang-keyed.
    //    Guard against 0-byte objects left by an earlier failed warm — an empty
    //    R2 hit used to serve a 200/0-byte "success" and mask the real image
    //    (this is what made JA-exclusive ids like ST05-015_r1 render blank).
    if (c.env.IMAGES) {
      const r2Object = await c.env.IMAGES.get(r2Key);
      if (r2Object && r2Object.size > 0) {
        return new Response(r2Object.body, { headers: IMG_HEADERS });
      }
    }

    const missKind = `card-${lang}`;
    if (await knownMiss(missKind, cardId)) {
      return c.body(null, 404, { 'Cache-Control': `public, max-age=${MISS_TTL_S}` });
    }
    const seen = { transient: false };

    // 1b. JA: proxy the Japanese official scan, persist under cards/ja/:id.
    //     If the JA host has no image at this id (the JA art is identical to
    //     EN, or simply absent), fall through to the EN image below so the JA
    //     binder still renders art — never a broken image. The EN bytes are
    //     cached under the EN key (cards/:id), NOT the JA key, so a future
    //     curated JA scan still wins once it exists.
    if (lang === 'ja') {
      const jaUrl = `https://www.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
      // fetchImageBytes rejects a 200 with an empty body, which used to be
      // written to R2 and served as a blank image.
      const buf = await fetchImageBytes(jaUrl, 'https://www.onepiece-cardgame.com/', seen);
      if (buf) {
        c.executionCtx.waitUntil(
          c.env.IMAGES
            ? c.env.IMAGES.put(r2Key, buf, { httpMetadata: { contentType: 'image/png' } })
            : caches.default.put(new Request(jaUrl), new Response(buf, { headers: IMG_HEADERS })),
        );
        return new Response(buf, { headers: IMG_HEADERS });
      }
      // JA art unavailable → serve the EN R2 object if we already have it.
      if (c.env.IMAGES) {
        const enObj = await c.env.IMAGES.get(`cards/${cardId}.png`);
        if (enObj && enObj.size > 0) return new Response(enObj.body, { headers: IMG_HEADERS });
      }
      // else fall through to the EN upstream block below.
    }

    // 2. DON cards without a curated R2 scan fall back to TCGPlayer's CDN art.
    //    The first successful fetch is kept in R2 under cards/fallback/, so
    //    the card stops depending on a live TCGPlayer fetch (a timeout there
    //    used to blank the image). The curated key (cards/DON-NNN.png, step 1)
    //    is still checked first, so a later curated upload wins.
    if (cardId.startsWith('DON-')) {
      const fallbackKey = `cards/fallback/${cardId}`;
      if (c.env.IMAGES) {
        const kept = await c.env.IMAGES.get(fallbackKey);
        if (kept && kept.size > 0) {
          return new Response(kept.body, {
            headers: { ...IMG_HEADERS, 'Content-Type': kept.httpMetadata?.contentType || 'image/jpeg' },
          });
        }
      }
      const row = await c.env.DB
        .prepare('SELECT tcg_ids FROM cards WHERE id = ?')
        .bind(cardId)
        .first();
      if (!row || !row.tcg_ids) return notFound(c, missKind, cardId);
      let tcgIds;
      try { tcgIds = JSON.parse(row.tcg_ids); } catch { return notFound(c, missKind, cardId); }
      if (!tcgIds?.length) return notFound(c, missKind, cardId);
      const url = `https://tcgplayer-cdn.tcgplayer.com/product/${tcgIds[0]}_in_1000x1000.jpg`;
      // TCGPlayer isn't the host blocking Worker IPs, so it gets longer than
      // the 2s Bandai timeout before the wsrv fallback.
      const res = await proxyAndCache(url, {}, seen, 6000);
      if (res) {
        const contentType = res.headers.get('content-type') || 'image/jpeg';
        const buf = await res.arrayBuffer();
        if (buf.byteLength > 0) {
          c.executionCtx.waitUntil(
            c.env.IMAGES
              ? c.env.IMAGES.put(fallbackKey, buf, { httpMetadata: { contentType } }).catch(() => {})
              : caches.default.put(new Request(url), new Response(buf, { headers: { ...IMG_HEADERS, 'Content-Type': contentType } })),
          );
          return new Response(buf, { headers: { ...IMG_HEADERS, 'Content-Type': contentType } });
        }
      }
      return notFound(c, missKind, cardId, seen);
    }

    // 3. Regular cards proxy from the official site, then PERSIST to R2 so we
    //    only ever fetch each card from Bandai once. R2 is checked first
    //    (step 1 above), so once a card is stored it never touches Bandai
    //    again — this is what prevents the recurring hot-link IP block:
    //    repeat traffic to Bandai drops to ~zero after the first fetch.
    //    Falls back to the ephemeral edge cache only if R2 is unbound.
    //
    //    Try the EN host first; JA-exclusive variants (_pN/_rN alt-art) 404 on
    //    the EN host but exist on the JA host, so fall back to it before giving
    //    up. Whichever wins is cached under the language-neutral EN key — it IS
    //    the canonical art for that id — so the default (no-lang) path serves it
    //    forever after. This is what fixes JA-only cards (and the OPCanvs
    //    character-page placeholders) without the caller needing ?lang=ja.
    const enUrl = `https://en.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
    const jaUrl = `https://www.onepiece-cardgame.com/images/cardlist/card/${cardId}.png`;
    // A ?lang=ja request already tried the JA host in 1b.
    const buf =
      (await fetchImageBytes(enUrl, 'https://en.onepiece-cardgame.com/', seen)) ||
      (lang === 'ja' ? null : await fetchImageBytes(jaUrl, 'https://www.onepiece-cardgame.com/', seen));
    if (buf) {
      c.executionCtx.waitUntil(
        c.env.IMAGES
          ? c.env.IMAGES.put(`cards/${cardId}.png`, buf, { httpMetadata: { contentType: 'image/png' } })
          : caches.default.put(new Request(enUrl), new Response(buf, { headers: IMG_HEADERS })),
      );
      return new Response(buf, { headers: IMG_HEADERS });
    }
    return notFound(c, missKind, cardId, seen);
  });

  // GET /images/set/:set_id?kind=box|logo
  // Set banner art for OPCanvs. Source URLs (Bandai product pages) are stored on
  // sets.box_url / sets.logo_url; we proxy out-of-band through wsrv (Bandai
  // IP-blocks the Worker) and persist to R2 under sets/{kind}/{set_id}.png so
  // each set is only fetched from Bandai once. Same tiered pattern as card art.
  app.get('/images/set/:set_id', async (c) => {
    const setId = c.req.param('set_id');
    if (!SAFE_ID.test(setId)) return c.body(null, 404);
    const kind = c.req.query('kind') === 'logo' ? 'logo' : 'box';
    const r2Key = `sets/${kind}/${setId}.png`;

    if (c.env.IMAGES) {
      const obj = await c.env.IMAGES.get(r2Key);
      if (obj && obj.size > 0) return new Response(obj.body, { headers: IMG_HEADERS });
    }

    const missKind = `set-${kind}`;
    if (await knownMiss(missKind, setId)) {
      return c.body(null, 404, { 'Cache-Control': `public, max-age=${MISS_TTL_S}` });
    }

    // kind is constrained to 'box'|'logo' above, so the column name is safe.
    const row = await c.env.DB
      .prepare(`SELECT ${kind}_url AS url FROM sets WHERE id = ?`)
      .bind(setId)
      .first();
    if (!row || !row.url) return notFound(c, missKind, setId);

    const seen = { transient: false };
    const buf = await fetchImageBytes(row.url, 'https://en.onepiece-cardgame.com/', seen);
    if (!buf) return notFound(c, missKind, setId, seen);
    c.executionCtx.waitUntil(
      c.env.IMAGES
        ? c.env.IMAGES.put(r2Key, buf, { httpMetadata: { contentType: 'image/png' } })
        : caches.default.put(new Request(row.url), new Response(buf, { headers: IMG_HEADERS })),
    );
    return new Response(buf, { headers: IMG_HEADERS });
  });
}
