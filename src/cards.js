import { parseCard, parseCards } from './db.js';

// OPTCG supported languages. 'en' is the default for every endpoint so
// existing (no ?lang) callers are unchanged. Unknown values fall back to en.
const SUPPORTED_LANGS = new Set(['en', 'ja']);
function normLang(raw) {
  return SUPPORTED_LANGS.has(raw) ? raw : 'en';
}

export function registerCardRoutes(app) {
  // Single-shot "every card" endpoint. Exists so the OPBindr client can
  // warm its registry with ONE request instead of 6 paginated ones.
  //
  // Workers responses aren't auto-cached by the edge just because of a
  // Cache-Control header — that only controls downstream (browser)
  // caching. To get edge caching we have to explicitly use the Workers
  // Cache API (`caches.default`). First hit runs the D1 query and puts
  // the response in the edge cache; subsequent hits anywhere served by
  // that edge node return in ~50 ms with no D1 query.
  //
  // MUST be registered BEFORE /cards/:card_id or Hono will route 'all'
  // into that param and return a 404 for a non-existent card with
  // id 'ALL'.
  app.get('/cards/all', async (c) => {
    const cache = caches.default;
    // Keep the cache key normalized to the bare URL so ?refresh=1 purges
    // the SAME entry the cached hit would use.
    const baseUrl = new URL(c.req.url);
    const refresh = baseUrl.searchParams.get('refresh') === '1';
    baseUrl.searchParams.delete('refresh');
    const cacheKey = new Request(baseUrl.toString(), { method: 'GET' });

    if (refresh) {
      await cache.delete(cacheKey);
    } else {
      const hit = await cache.match(cacheKey);
      if (hit) return hit;
    }

    // EN-only by design. This is the legacy single-shot fallback the client
    // uses only when /cards/index 404s (older deployments). The language-aware
    // path is /cards/index (both names inline) + /cards/:id?lang= for details.
    const { results } = await c.env.DB.prepare(
      'SELECT * FROM cards ORDER BY id ASC'
    ).all();

    const response = new Response(JSON.stringify({
      count: results.length,
      data: parseCards(results),
    }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400',
      },
    });

    c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;
  });

  // Slim index. Same shape spirit as /cards/all but drops the heavy
  // fields (effect text, trigger text, image_url, tcg_ids, sets
  // membership, price_updated_at) so the OPBindr client can warm its
  // registry with ~80% fewer bytes. CardEnlargeModal fetches the full
  // shape via /cards/:id when it actually opens a card.
  //
  // dominant_color is reserved for the Phase 3 placeholder work — null
  // for now so the JSON shape doesn't have to change when the column
  // gets populated.
  //
  // Same edge-caching strategy as /cards/all: explicit Cache API put
  // so subsequent edge-served hits skip D1 entirely.
  //
  // MUST be registered BEFORE /cards/:card_id (same reason as /cards/all).
  app.get('/cards/index', async (c) => {
    const cache = caches.default;
    const baseUrl = new URL(c.req.url);
    const refresh = baseUrl.searchParams.get('refresh') === '1';
    baseUrl.searchParams.delete('refresh');
    const cacheKey = new Request(baseUrl.toString(), { method: 'GET' });

    if (refresh) {
      await cache.delete(cacheKey);
    } else {
      const hit = await cache.match(cacheKey);
      if (hit) return hit;
    }

    // Both names inline + per-language availability, so the OPBindr client
    // holds ONE row per card (not a row per language). This is the OPTCG
    // language model: One Piece is one catalog with translated display, so
    // the registry never needs a per-language key and EN/JA can't collide.
    //   - name      : EN canonical display (COALESCE so a pre-016 row without
    //                 a translation still resolves to cards.name)
    //   - name_ja   : Japanese display, NULL when no JA translation exists
    //   - name_en   : English search alias (= EN name; for JA-exclusives it's
    //                 the romaji/EN alias the importer stored on the JA row)
    //   - langs     : which languages this card is available in. A JA-exclusive
    //                 has no EN translation -> ['ja'] -> hidden in EN binders;
    //                 an EN-only card (e.g. Treasure Rare) -> ['en'].
    //   - price_ja  : real JA market price (never the EN price on a JA card).
    const { results } = await c.env.DB.prepare(`
      SELECT c.id, c.category, c.rarity, c.colors, c.attributes, c.types,
             c.cost, c.power, c.parallel, c.variant_type, c.finish,
             c.price, c.price_source, c.price_ja, c.price_source_ja,
             COALESCE(en.name, c.name)       AS name,
             ja.name                          AS name_ja,
             COALESCE(en.name, ja.name_en)    AS name_en,
             CASE WHEN en.card_id IS NOT NULL THEN 1 ELSE 0 END AS has_en,
             CASE WHEN ja.card_id IS NOT NULL THEN 1 ELSE 0 END AS has_ja
      FROM cards c
      LEFT JOIN card_translations en ON en.card_id = c.id AND en.language = 'en'
      LEFT JOIN card_translations ja ON ja.card_id = c.id AND ja.language = 'ja'
      ORDER BY c.id ASC
    `).all();

    const slim = results.map(row => {
      const langs = [];
      if (row.has_en) langs.push('en');
      if (row.has_ja) langs.push('ja');
      if (langs.length === 0) langs.push('en'); // defensive: pre-backfill rows
      return {
        id: row.id,
        name: row.name,
        name_ja: row.name_ja,
        name_en: row.name_en === row.name ? null : row.name_en, // null when alias == display (EN rows)
        langs,
        category: row.category,
        rarity: row.rarity,
        colors: row.colors ? JSON.parse(row.colors) : null,
        attributes: row.attributes ? JSON.parse(row.attributes) : null,
        types: row.types ? JSON.parse(row.types) : null,
        cost: row.cost,
        power: row.power,
        parallel: Boolean(row.parallel),
        variant_type: row.variant_type,
        finish: row.finish,
        price: row.price,
        price_source: row.price_source,
        price_ja: row.price_ja,
        price_source_ja: row.price_source_ja,
        dominant_color: null, // Phase 3 fills this in once the D1 column exists
      };
    });

    const response = new Response(JSON.stringify({
      count: slim.length,
      data: slim,
    }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400',
      },
    });

    c.executionCtx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;
  });

  // Price history for a single card. Range caps the window in seconds so we
  // don't return the entire history by default. Rows come from the
  // `card_price_history` table, populated on each weekly price refresh.
  app.get('/cards/:card_id/price-history', async (c) => {
    const raw = c.req.param('card_id');
    const m = raw.match(/^([^_]+)(_[a-zA-Z]+\d+)?$/);
    const cardId = m ? m[1].toUpperCase() + (m[2] ? m[2].toLowerCase() : '') : raw.toUpperCase();

    const RANGES = { '1m': 30 * 86400, '3m': 90 * 86400, '6m': 180 * 86400, '1y': 365 * 86400, 'all': null };
    const range = RANGES[c.req.query('range')] !== undefined ? c.req.query('range') : '1y';
    const window = RANGES[range];

    let sql = 'SELECT price, captured_at FROM card_price_history WHERE card_id = ?';
    const params = [cardId];
    if (window !== null) {
      const since = Math.floor(Date.now() / 1000) - window;
      sql += ' AND captured_at >= ?';
      params.push(since);
    }
    sql += ' ORDER BY captured_at ASC';

    const { results } = await c.env.DB.prepare(sql).bind(...params).all();

    // Current price lookup so the chart can anchor its "now" line without a
    // second request. Null if the card has no price or doesn't exist.
    const current = await c.env.DB.prepare(
      'SELECT price, price_updated_at FROM cards WHERE id = ?'
    ).bind(cardId).first();

    return c.json({
      card_id: cardId,
      range,
      current_price: current?.price ?? null,
      current_updated_at: current?.price_updated_at ?? null,
      points: results.map(r => ({ price: r.price, t: r.captured_at * 1000 })),
    });
  });

  app.get('/cards/:card_id', async (c) => {
    // Uppercase the set prefix (OP05-119) but preserve the variant suffix
    // (_p8, _r1, _jp1) since D1 stores those lowercase. The `+` on the
    // letter class lets multi-letter suffixes like `_jp1` (JP-exclusive
    // parallels) through — a plain `[a-zA-Z]` would have failed the whole
    // regex and uppercased the entire ID.
    const raw = c.req.param('card_id');
    const m = raw.match(/^([^_]+)(_[a-zA-Z]+\d+)?$/);
    const cardId = m ? m[1].toUpperCase() + (m[2] ? m[2].toLowerCase() : '') : raw.toUpperCase();

    const lang = normLang(c.req.query('lang'));

    const card = await c.env.DB.prepare(
      'SELECT * FROM cards WHERE id = ?'
    ).bind(cardId).first();

    if (!card) return c.json({ detail: `Card '${cardId}' not found` }, 404);

    // Merge the requested language's display fields over the base row. Fall
    // back to the EN translation when the requested language has no row, so
    // the response always has a name and never 500s on a missing translation.
    const tr =
      (await c.env.DB.prepare(
        'SELECT name, name_en, image_url, effect, trigger_text FROM card_translations WHERE card_id = ? AND language = ?'
      ).bind(cardId, lang).first())
      || (lang !== 'en'
        ? await c.env.DB.prepare(
            'SELECT name, name_en, image_url, effect, trigger_text FROM card_translations WHERE card_id = ? AND language = ?'
          ).bind(cardId, 'en').first()
        : null);

    if (tr) {
      if (tr.name != null) card.name = tr.name;
      if (tr.image_url != null) card.image_url = tr.image_url;
      if (tr.effect != null) card.effect = tr.effect;
      if (tr.trigger_text != null) card.trigger_text = tr.trigger_text;
      card.name_en = tr.name_en ?? null;
    }

    const { results: sets } = await c.env.DB.prepare(`
      SELECT s.* FROM sets s
      JOIN card_sets cs ON cs.set_id = s.id
      WHERE cs.card_id = ?
      ORDER BY s.pack_id
    `).bind(cardId).all();

    return c.json({ ...parseCard(card), lang, sets });
  });

  app.get('/cards', async (c) => {
    const q = c.req.query();
    const conditions = [];
    const params = [];

    if (q.set_id) {
      conditions.push('EXISTS (SELECT 1 FROM card_sets cs WHERE cs.card_id = c.id AND cs.set_id = ?)');
      params.push(q.set_id.toUpperCase());
    }

    if (q.color) {
      conditions.push("EXISTS (SELECT 1 FROM json_each(c.colors) WHERE json_each.value = ?)");
      params.push(q.color.charAt(0).toUpperCase() + q.color.slice(1).toLowerCase());
    }

    if (q.category) {
      conditions.push('c.category = ? COLLATE NOCASE');
      params.push(q.category);
    }

    if (q.rarity) {
      conditions.push('c.rarity = ? COLLATE NOCASE');
      params.push(q.rarity);
    }

    if (q.name) {
      conditions.push(
        "(c.name LIKE ? COLLATE NOCASE OR EXISTS (SELECT 1 FROM json_each(c.types) WHERE json_each.value LIKE ? COLLATE NOCASE))"
      );
      const like = `%${q.name}%`;
      params.push(like, like);
    }

    if (q.parallel !== undefined) {
      conditions.push('c.parallel = ?');
      params.push(q.parallel === 'true' ? 1 : 0);
    }

    if (q.variant_type) {
      conditions.push('c.variant_type = ? COLLATE NOCASE');
      params.push(q.variant_type);
    }

    if (q.finish) {
      conditions.push('c.finish = ? COLLATE NOCASE');
      params.push(q.finish);
    }

    if (q.min_power) {
      conditions.push('c.power >= ?');
      params.push(Number(q.min_power));
    }

    if (q.max_power) {
      conditions.push('c.power <= ?');
      params.push(Number(q.max_power));
    }

    if (q.min_cost) {
      conditions.push('c.cost >= ?');
      params.push(Number(q.min_cost));
    }

    if (q.max_cost) {
      conditions.push('c.cost <= ?');
      params.push(Number(q.max_cost));
    }

    if (q.min_price) {
      conditions.push('c.price >= ?');
      params.push(Number(q.min_price));
    }

    if (q.max_price) {
      conditions.push('c.price <= ?');
      params.push(Number(q.max_price));
    }

    const sortMap = {
      id: 'c.id',
      name: 'c.name',
      price: 'c.price',
      power: 'c.power',
      cost: 'c.cost',
    };
    const sortCol = sortMap[q.sort] || 'c.id';
    const sortDir = q.order?.toLowerCase() === 'desc' ? 'DESC' : 'ASC';
    const nullsOrder = sortCol === 'c.id' ? '' : ` NULLS ${sortDir === 'DESC' ? 'FIRST' : 'LAST'}`;
    const orderBy = `ORDER BY ${sortCol} ${sortDir}${nullsOrder}, c.id ASC`;

    const page = Math.max(1, Number(q.page) || 1);
    const pageSize = Math.min(500, Math.max(1, Number(q.page_size) || 50));
    const offset = (page - 1) * pageSize;

    const where = conditions.length ? 'WHERE ' + conditions.join(' AND ') : '';

    const countRow = await c.env.DB.prepare(
      `SELECT COUNT(*) AS total FROM cards c ${where}`
    ).bind(...params).first();

    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c ${where} ${orderBy} LIMIT ? OFFSET ?`
    ).bind(...params, pageSize, offset).all();

    return c.json({
      count: results.length,
      totalCount: countRow.total,
      page,
      pageSize,
      data: parseCards(results),
    });
  });
}
