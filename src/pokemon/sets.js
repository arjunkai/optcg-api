// Pokémon sets endpoints, mirroring OPTCG /sets layout:
//   GET /pokemon/sets?lang=en                  list of sets in the language
//   GET /pokemon/sets/:set_id/cards?lang=en    slim cards in that set

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

function rowToSet(row) {
  return {
    id: row.set_id,
    lang: row.lang,
    name: row.name,
    series: row.series,
    release_date: row.release_date,
    card_count_total: row.card_count_total,
    card_count_official: row.card_count_official,
    logo_url: row.logo_url,
    symbol_url: row.symbol_url,
  };
}

export function registerPokemonSetRoutes(app) {
  app.get('/pokemon/sets', async (c) => {
    const lang = (c.req.query('lang') || 'en').toLowerCase();
    if (!VALID_LANGS.has(lang)) return jsonResponse({ error: 'invalid lang' }, 400);
    const { results } = await c.env.DB.prepare(`
      SELECT set_id, lang, name, series, release_date, card_count_total, card_count_official, logo_url, symbol_url
      FROM ptcg_sets WHERE lang = ?
      ORDER BY release_date DESC, set_id ASC
    `).bind(lang).all();
    return jsonResponse({ count: (results || []).length, data: (results || []).map(rowToSet) });
  });

  app.get('/pokemon/sets/:set_id/cards', async (c) => {
    const lang = (c.req.query('lang') || 'en').toLowerCase();
    if (!VALID_LANGS.has(lang)) return jsonResponse({ error: 'invalid lang' }, 400);
    const setId = c.req.param('set_id');
    const { results } = await c.env.DB.prepare(`
      SELECT card_id, lang, set_id, local_id, name, category, rarity,
             hp, retreat, types_csv, stage, variants_json,
             image_high, image_low, pricing_json, price_source, dominant_color
      FROM ptcg_cards
      WHERE set_id = ? AND lang = ?
      ORDER BY local_id
    `).bind(setId, lang).all();
    const data = (results || []).map(row => ({
      id: row.card_id,
      lang: row.lang,
      set_id: row.set_id,
      local_id: row.local_id,
      name: row.name,
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
    }));
    return jsonResponse({ count: data.length, data });
  });
}
