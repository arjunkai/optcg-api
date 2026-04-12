import { parseCards } from './db.js';

export function registerSetRoutes(app) {
  app.get('/sets', async (c) => {
    const { results } = await c.env.DB.prepare(
      'SELECT * FROM sets ORDER BY pack_id DESC'
    ).all();
    return c.json({ count: results.length, data: results });
  });

  app.get('/sets/:set_id/cards', async (c) => {
    const setId = c.req.param('set_id').toUpperCase();

    const set = await c.env.DB.prepare(
      'SELECT * FROM sets WHERE id = ?'
    ).bind(setId).first();

    if (!set) return c.json({ detail: `Set '${setId}' not found` }, 404);

    const { results } = await c.env.DB.prepare(`
      SELECT c.* FROM cards c
      JOIN card_sets cs ON cs.card_id = c.id
      WHERE cs.set_id = ?
      ORDER BY c.id
    `).bind(setId).all();

    return c.json({ set, count: results.length, data: parseCards(results) });
  });
}
