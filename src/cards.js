import { parseCard, parseCards } from './db.js';

export function registerCardRoutes(app) {
  app.get('/cards/:card_id', async (c) => {
    // Uppercase the set prefix (OP05-119) but preserve the variant suffix
    // (_p8, _r1) since D1 stores those lowercase. Match `^[base-id](_[pr]\d+)?$`.
    const raw = c.req.param('card_id');
    const m = raw.match(/^([^_]+)(_[a-zA-Z]\d+)?$/);
    const cardId = m ? m[1].toUpperCase() + (m[2] ? m[2].toLowerCase() : '') : raw.toUpperCase();

    const card = await c.env.DB.prepare(
      'SELECT * FROM cards WHERE id = ?'
    ).bind(cardId).first();

    if (!card) return c.json({ detail: `Card '${cardId}' not found` }, 404);

    const { results: sets } = await c.env.DB.prepare(`
      SELECT s.* FROM sets s
      JOIN card_sets cs ON cs.set_id = s.id
      WHERE cs.card_id = ?
      ORDER BY s.pack_id
    `).bind(cardId).all();

    return c.json({ ...parseCard(card), sets });
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
