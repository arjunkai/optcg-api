// Pokémon cards endpoints. Three shapes mirroring the OPTCG layout:
//   GET /pokemon/cards/index?lang=en   slim list for binder render
//   GET /pokemon/cards/:id?lang=en      full detail (used by the card enlarge modal)
//   GET /pokemon/cards/all?lang=en      legacy full-shape list (fallback / debugging)
//
// All three live in D1 via the daily TCGdex import (Task 2.3, separate
// commit). Until the import has run for the requested language the
// endpoints return empty `data: []`. The frontend's normalizer + cache
// layer is tolerant of empty responses — the binder grid will just
// display "Add Cards" buttons until data lands.
//
// Slim shape mirrors the OPTCG slim shape spirit: only the fields
// the binder grid + AddCardsModal filter UI need. CardEnlargeModal
// hits /pokemon/cards/:id for the heavy fields (effect/abilities/attacks).

const VALID_LANGS = new Set(['en', 'ja', 'zh-cn', 'zh-tw']);

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400',
    },
  });
}

export function rowToSlim(row) {
  return {
    id: row.card_id,
    lang: row.lang,
    set_id: row.set_id,
    local_id: row.local_id,
    name: row.name,
    // English-name alias for JA rows so the frontend search index can
    // match a latin-script query against Japanese-named cards. Null on
    // EN/zh-* rows where `name` is already latin/local.
    name_en: row.name_en ?? null,
    category: row.category,
    rarity: row.rarity,
    hp: row.hp,
    retreat: row.retreat,
    types: row.types_csv ? row.types_csv.split(',').filter(Boolean) : [],
    stage: row.stage,
    variants: row.variants_json ? JSON.parse(row.variants_json) : {},
    image_high: row.image_high,
    image_low: row.image_low,
    pricing: row.pricing_json ? JSON.parse(row.pricing_json) : {},
    price_source: row.price_source ?? null,
    dominant_color: row.dominant_color,
  };
}

// Strip pricing_json down to just the fields the frontend's pickPrice
// reads. Used by the high-volume /index and /sets/:set_id/cards
// endpoints — the per-card detail endpoint keeps the full object so
// CardEnlargeModal has the full TCGplayer / Cardmarket spread.
//
// Keys preserved (must stay in sync with normalize/ptcg.js):
//   - pricing.manual.price
//   - pricing.tcgplayer.{holofoil|normal|reverseHolofoil}.market
//   - pricing.cardmarket.{avg|trend|avg7|avg30|avg1|low}
const TCGPLAYER_VARIANT_KEYS = ['holofoil', 'normal', 'reverseHolofoil'];
const CARDMARKET_KEYS = ['avg', 'trend', 'avg7', 'avg30', 'avg1', 'low'];
export function withSlimPricing(slim) {
  const p = slim.pricing;
  if (!p || typeof p !== 'object') return slim;
  const pruned = {};
  if (p.manual && typeof p.manual.price === 'number') {
    pruned.manual = { price: p.manual.price };
  }
  if (p.tcgplayer && typeof p.tcgplayer === 'object') {
    const tcg = {};
    for (const v of TCGPLAYER_VARIANT_KEYS) {
      const market = p.tcgplayer[v]?.market;
      if (typeof market === 'number') tcg[v] = { market };
    }
    // Keep the marketplace URL alongside prices so the frontend can
    // build a TCGplayer-affiliate buy button without a second fetch.
    if (typeof p.tcgplayer.url === 'string') tcg.url = p.tcgplayer.url;
    if (Object.keys(tcg).length) pruned.tcgplayer = tcg;
  }
  if (p.cardmarket && typeof p.cardmarket === 'object') {
    const cm = {};
    for (const k of CARDMARKET_KEYS) {
      if (typeof p.cardmarket[k] === 'number') cm[k] = p.cardmarket[k];
    }
    if (typeof p.cardmarket.url === 'string') cm.url = p.cardmarket.url;
    if (Object.keys(cm).length) pruned.cardmarket = cm;
  }
  return { ...slim, pricing: pruned };
}

export function registerPokemonCardRoutes(app) {
  // Slim index. Same edge-cache pattern as OPTCG /cards/index.
  // MUST be registered BEFORE /pokemon/cards/:id.
  app.get('/pokemon/cards/index', async (c) => {
    const lang = (c.req.query('lang') || 'en').toLowerCase();
    if (!VALID_LANGS.has(lang)) return jsonResponse({ error: 'invalid lang' }, 400);

    const cache = caches.default;
    const baseUrl = new URL(c.req.url);
    const refresh = baseUrl.searchParams.get('refresh') === '1';
    baseUrl.searchParams.delete('refresh');
    // Cache version — bump whenever the slim response shape changes so
    // pre-deploy cached payloads get effectively invalidated. The Cache
    // API keys on full URL, so a different _v query param creates a
    // distinct entry and the old one ages out naturally.
    //   v2 (2026-05-07): JA queries now JOIN to EN for name_en alias
    baseUrl.searchParams.set('_v', '2');
    const cacheKey = new Request(baseUrl.toString(), { method: 'GET' });
    if (refresh) await cache.delete(cacheKey);
    else {
      const hit = await cache.match(cacheKey);
      if (hit) return hit;
    }

    // For JA queries, LEFT JOIN to the EN row with the same card_id so
    // every card whose English counterpart exists in D1 gets a name_en
    // alias for free — no backfill bookkeeping needed. COALESCE prefers
    // the explicit ptcg_cards.name_en column when set (manual override
    // or enrich_ja_card_names.py output for JA-only cards), falls back
    // to the JOINed EN name otherwise. Other langs skip the JOIN since
    // EN's `name` is already the EN alias for them when needed.
    const sql = lang === 'ja'
      ? `
        SELECT ja.card_id AS card_id, ja.lang AS lang, ja.set_id AS set_id,
               ja.local_id AS local_id, ja.name AS name,
               COALESCE(ja.name_en, en.name) AS name_en,
               ja.category AS category, ja.rarity AS rarity,
               ja.hp AS hp, ja.retreat AS retreat, ja.types_csv AS types_csv,
               ja.stage AS stage, ja.variants_json AS variants_json,
               ja.image_high AS image_high, ja.image_low AS image_low,
               ja.pricing_json AS pricing_json, ja.price_source AS price_source,
               ja.dominant_color AS dominant_color
        FROM ptcg_cards ja
        LEFT JOIN ptcg_cards en ON en.card_id = ja.card_id AND en.lang = 'en'
        WHERE ja.lang = 'ja'
        ORDER BY ja.set_id, ja.local_id
      `
      : `
        SELECT card_id, lang, set_id, local_id, name, name_en, category, rarity,
               hp, retreat, types_csv, stage, variants_json,
               image_high, image_low, pricing_json, price_source, dominant_color
        FROM ptcg_cards
        WHERE lang = ?
        ORDER BY set_id, local_id
      `;

    const stmt = lang === 'ja' ? c.env.DB.prepare(sql) : c.env.DB.prepare(sql).bind(lang);
    const { results } = await stmt.all();

    const data = (results || []).map((row) => withSlimPricing(rowToSlim(row)));
    const response = jsonResponse({ count: data.length, data });
    c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;
  });

  // Legacy full-shape list. Useful for debugging the import; the
  // frontend uses /pokemon/cards/index in the normal path.
  // MUST be registered BEFORE /pokemon/cards/:id so Hono doesn't match
  // "all" as a :card_id.
  app.get('/pokemon/cards/all', async (c) => {
    const lang = (c.req.query('lang') || 'en').toLowerCase();
    if (!VALID_LANGS.has(lang)) return jsonResponse({ error: 'invalid lang' }, 400);
    const { results } = await c.env.DB.prepare(`
      SELECT * FROM ptcg_cards WHERE lang = ? ORDER BY set_id, local_id
    `).bind(lang).all();
    const data = (results || []).map(row => {
      const slim = rowToSlim(row);
      const raw = row.raw ? JSON.parse(row.raw) : {};
      return { ...slim, ...raw };
    });
    return jsonResponse({ count: data.length, data });
  });

  // Price history for one card. Returns the time series captured by
  // the weekly cron's snapshot-ptcg-price-history.js step. Each card
  // can have multiple sources (tcgplayer/cardmarket/manual/etc.) and
  // each source can have multiple variants (tcgplayer.holofoil etc.);
  // points are returned grouped by (source, variant) so the chart can
  // overlay or pick whichever line it cares about.
  //
  // Range caps the window: 1m, 3m, 6m, 1y (default), all.
  //
  // MUST be registered BEFORE /pokemon/cards/:card_id so Hono doesn't
  // route 'price-history' into the param.
  app.get('/pokemon/cards/:card_id/price-history', async (c) => {
    const cardId = c.req.param('card_id');
    const RANGES = { '1m': 30 * 86400, '3m': 90 * 86400, '6m': 180 * 86400, '1y': 365 * 86400, 'all': null };
    const range = RANGES[c.req.query('range')] !== undefined ? c.req.query('range') : '1y';
    const window = RANGES[range];

    let sql = 'SELECT source, variant, recorded_at, price_usd, price_eur FROM ptcg_price_history WHERE card_id = ?';
    const params = [cardId];
    if (window !== null) {
      const since = Math.floor(Date.now() / 1000) - window;
      sql += ' AND recorded_at >= ?';
      params.push(since);
    }
    sql += ' ORDER BY recorded_at ASC';

    const { results } = await c.env.DB.prepare(sql).bind(...params).all();

    // Group by (source, variant) so a chart can render one line per
    // series. Each point's `t` is unix ms for parity with OPTCG's
    // /cards/:id/price-history shape.
    const series = {};
    for (const r of results || []) {
      const key = `${r.source}.${r.variant}`;
      if (!series[key]) {
        series[key] = { source: r.source, variant: r.variant, points: [] };
      }
      series[key].points.push({
        t: r.recorded_at * 1000,
        usd: r.price_usd,
        eur: r.price_eur,
      });
    }

    return c.json({
      card_id: cardId,
      range,
      series: Object.values(series),
    });
  });

  // Single-card detail. Returns full row including raw TCGdex JSON for
  // CardEnlargeModal's heavy fields (effect/abilities/attacks).
  app.get('/pokemon/cards/:card_id', async (c) => {
    const lang = (c.req.query('lang') || 'en').toLowerCase();
    if (!VALID_LANGS.has(lang)) return jsonResponse({ error: 'invalid lang' }, 400);
    const cardId = c.req.param('card_id');

    const row = await c.env.DB.prepare(`
      SELECT * FROM ptcg_cards WHERE card_id = ? AND lang = ?
    `).bind(cardId, lang).first();
    if (!row) return jsonResponse({ error: 'not found' }, 404);

    const slim = rowToSlim(row);
    const raw = row.raw ? JSON.parse(row.raw) : {};
    // raw comes from the TCGdex import and freezes pricing/images at
    // that moment. Spread raw FIRST so slim's fresh image_high /
    // pricing_json / price_source from D1 wins — every later backfill
    // (pokemontcg-data, live API, manual) writes to D1 only and the
    // raw column is never re-touched.
    return jsonResponse({ ...raw, ...slim });
  });
}
