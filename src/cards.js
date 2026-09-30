import { parseCard, parseCards } from './db.js';
import { serveSnapshot } from './snapshot.js';

// Numeric filter value, or null when absent/non-numeric. `?min_power=abc`
// used to bind NaN and fail the whole query; now the filter is just skipped.
export function numParam(raw) {
  if (raw === undefined || raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

// page / page_size for a list route. Floors both (a fractional or huge value
// used to bind a non-integer LIMIT/OFFSET and 500) and caps page so the
// OFFSET stays finite. Absent/invalid values fall back as before.
const MAX_PAGE = 100_000;
export function pageParams(q, { defaultSize = 50, maxSize = 500 } = {}) {
  const page = Math.min(MAX_PAGE, Math.max(1, Math.floor(Number(q.page)) || 1));
  const pageSize = Math.min(maxSize, Math.max(1, Math.floor(Number(q.page_size)) || defaultSize));
  return { page, pageSize, offset: (page - 1) * pageSize };
}

// totalCount for a paginated list. When page 1 came back short, the page IS
// the whole result, so the COUNT(*) query (often a full scan on searches) is
// skipped. Otherwise runs countSql, which must select one column `total`.
export async function countTotal(db, page, pageSize, pageRows, countSql, params) {
  if (page === 1 && pageRows < pageSize) return pageRows;
  const row = await db.prepare(countSql).bind(...params).first();
  return row.total;
}

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
  // Served from an R2 snapshot shared by every colo, fronted by the edge
  // cache (see snapshot.js), so the full-table D1 read runs a few times a
  // day at most instead of once per colo per hour.
  //
  // MUST be registered BEFORE /cards/:card_id or Hono will route 'all'
  // into that param and return a 404 for a non-existent card with
  // id 'ALL'.
  app.get('/cards/all', (c) => serveSnapshot(c, 'cards-all-v1', async () => {
    // EN-only by design. This is the legacy single-shot fallback the client
    // uses only when /cards/index 404s (older deployments). The language-aware
    // path is /cards/index (both names inline) + /cards/:id?lang= for details.
    const { results } = await c.env.DB.prepare(
      'SELECT * FROM cards ORDER BY id ASC'
    ).all();
    return { count: results.length, data: parseCards(results) };
  }));

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
  // Same R2 snapshot + edge cache strategy as /cards/all.
  //
  // MUST be registered BEFORE /cards/:card_id (same reason as /cards/all).
  app.get('/cards/index', (c) => serveSnapshot(c, 'cards-index-v1', async () => {
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

    return { count: slim.length, data: slim };
  }));

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
      // IN (not a correlated EXISTS) so SQLite drives from idx_card_sets_set_id
      // instead of scanning every card: ~450 D1 rows read per call vs ~4,500.
      // OPCanvs fires one of these per set tile, so this dominated reads.
      conditions.push('c.id IN (SELECT cs.card_id FROM card_sets cs WHERE cs.set_id = ?)');
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

    const RANGE_FILTERS = [
      ['min_power', 'c.power >= ?'], ['max_power', 'c.power <= ?'],
      ['min_cost', 'c.cost >= ?'], ['max_cost', 'c.cost <= ?'],
      ['min_price', 'c.price >= ?'], ['max_price', 'c.price <= ?'],
    ];
    for (const [key, clause] of RANGE_FILTERS) {
      const n = numParam(q[key]);
      if (n !== null) {
        conditions.push(clause);
        params.push(n);
      }
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
    // NULLS LAST for both directions: unpriced cards must never lead a
    // price/power/cost ranking (a DESC price sort should return the most
    // expensive card first, not the NULL-priced ones).
    const nullsOrder = sortCol === 'c.id' ? '' : ' NULLS LAST';
    const orderBy = `ORDER BY ${sortCol} ${sortDir}${nullsOrder}, c.id ASC`;

    const { page, pageSize, offset } = pageParams(q);

    const where = conditions.length ? 'WHERE ' + conditions.join(' AND ') : '';

    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c ${where} ${orderBy} LIMIT ? OFFSET ?`
    ).bind(...params, pageSize, offset).all();

    const totalCount = await countTotal(c.env.DB, page, pageSize, results.length,
      `SELECT COUNT(*) AS total FROM cards c ${where}`, params);

    return c.json({
      count: results.length,
      totalCount,
      page,
      pageSize,
      data: parseCards(results),
    });
  });
}
