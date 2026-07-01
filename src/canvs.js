import { parseCards } from './db.js';

export function registerCanvsRoutes(app) {
  // GET /illustrators
  // Lists all illustrators with pagination and sort.
  // sort=cards (default) orders by card_count DESC, name ASC.
  // sort=name orders by name ASC.
  app.get('/illustrators', async (c) => {
    const q = c.req.query();
    const page = Math.max(1, Number(q.page) || 1);
    const pageSize = Math.min(500, Math.max(1, Number(q.page_size) || 50));
    const offset = (page - 1) * pageSize;

    const orderBy = q.sort === 'name'
      ? 'ORDER BY name ASC'
      : 'ORDER BY card_count DESC, name ASC';

    const countRow = await c.env.DB.prepare(
      'SELECT count(*) AS n FROM illustrators'
    ).first();

    const { results } = await c.env.DB.prepare(
      `SELECT id, slug, name, name_ja, twitter, instagram, pixiv, tumblr, website, bio, card_count
       FROM illustrators ${orderBy} LIMIT ? OFFSET ?`
    ).bind(pageSize, offset).all();

    return c.json({
      count: results.length,
      totalCount: countRow.n,
      page,
      pageSize,
      data: results,
    });
  });

  // GET /illustrators/:slug
  // Single illustrator by slug, plus all cards they illustrated.
  app.get('/illustrators/:slug', async (c) => {
    const slug = c.req.param('slug').toLowerCase().trim();

    const illustrator = await c.env.DB.prepare(
      'SELECT * FROM illustrators WHERE slug = ?'
    ).bind(slug).first();

    if (!illustrator) return c.json({ error: 'illustrator not found' }, 404);

    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c
       JOIN card_illustrators ci ON ci.card_id = c.id
       WHERE ci.illustrator_id = ?
       ORDER BY c.id`
    ).bind(illustrator.id).all();

    return c.json({ illustrator, cards: parseCards(results) });
  });

  // GET /characters
  // Lists all characters with pagination, optional name search, and sort.
  // sort=cards (default) orders by card_count DESC, ch.name ASC.
  // sort=name orders by ch.name ASC.
  // q= filters by name LIKE %q%.
  app.get('/characters', async (c) => {
    const q = c.req.query();
    const page = Math.max(1, Number(q.page) || 1);
    const pageSize = Math.min(500, Math.max(1, Number(q.page_size) || 50));
    const offset = (page - 1) * pageSize;

    const where = q.q ? 'WHERE ch.name LIKE ?' : '';
    const searchParam = q.q ? `%${q.q}%` : null;

    const orderBy = q.sort === 'name'
      ? 'ORDER BY ch.name ASC'
      : 'ORDER BY card_count DESC, ch.name ASC';

    const countRow = searchParam
      ? await c.env.DB.prepare(
          `SELECT count(*) AS n FROM characters ch ${where}`
        ).bind(searchParam).first()
      : await c.env.DB.prepare(
          'SELECT count(*) AS n FROM characters'
        ).first();

    const sql = `
      SELECT ch.id, ch.name, ch.name_ja, ch.source, ch.wikidata_qid, ch.fandom_title,
             (SELECT count(*) FROM card_characters cc WHERE cc.character_id = ch.id) AS card_count
      FROM characters ch
      ${where}
      ${orderBy} LIMIT ? OFFSET ?`;

    const { results } = searchParam
      ? await c.env.DB.prepare(sql).bind(searchParam, pageSize, offset).all()
      : await c.env.DB.prepare(sql).bind(pageSize, offset).all();

    return c.json({
      count: results.length,
      totalCount: countRow.n,
      page,
      pageSize,
      data: results,
    });
  });

  // GET /characters/:id
  // Single character by numeric id, plus all cards they appear on.
  app.get('/characters/:id', async (c) => {
    const rawId = Number(c.req.param('id'));
    if (isNaN(rawId)) return c.json({ error: 'invalid character id' }, 400);

    const character = await c.env.DB.prepare(
      'SELECT * FROM characters WHERE id = ?'
    ).bind(rawId).first();

    if (!character) return c.json({ error: 'character not found' }, 404);

    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c
       JOIN card_characters cc ON cc.card_id = c.id
       WHERE cc.character_id = ?
       ORDER BY c.id`
    ).bind(rawId).all();

    return c.json({ character, cards: parseCards(results) });
  });

  // GET /artwork
  // Art-forward card gallery, optionally filtered by illustrator slug
  // and/or character id. Paginated. No set filter (use /sets/:id/cards).
  app.get('/artwork', async (c) => {
    const q = c.req.query();
    const page = Math.max(1, Number(q.page) || 1);
    const pageSize = Math.min(500, Math.max(1, Number(q.page_size) || 50));
    const offset = (page - 1) * pageSize;

    const joins = [];
    const where = [];
    const params = [];

    if (q.artist) {
      joins.push('JOIN card_illustrators ci ON ci.card_id = c.id JOIN illustrators i ON i.id = ci.illustrator_id');
      where.push('i.slug = ?');
      params.push(q.artist.toLowerCase());
    }

    if (q.character) {
      const charId = Number(q.character);
      if (!Number.isInteger(charId)) return c.json({ error: 'invalid character id' }, 400);
      joins.push('JOIN card_characters cc ON cc.card_id = c.id');
      where.push('cc.character_id = ?');
      params.push(charId);
    }

    const joinClause = joins.join(' ');
    const whereClause = where.length ? 'WHERE ' + where.join(' AND ') : '';

    const countRow = await c.env.DB.prepare(
      `SELECT count(*) AS n FROM cards c ${joinClause} ${whereClause}`
    ).bind(...params).first();

    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c ${joinClause} ${whereClause} ORDER BY c.id LIMIT ? OFFSET ?`
    ).bind(...params, pageSize, offset).all();

    return c.json({
      count: results.length,
      totalCount: countRow.n,
      page,
      pageSize,
      data: parseCards(results),
    });
  });
}
