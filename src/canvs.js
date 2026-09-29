import { parseCards } from './db.js';
import { countTotal } from './cards.js';
import { serveSnapshot } from './snapshot.js';

// One representative card per character / illustrator / set. Each query picks
// exactly what the per-tile request it replaces returned (see
// /representatives below). MIN(c.id) with bare columns is SQLite's
// documented "row holding the min" behaviour.
const REP_QUERIES = {
  // = /artwork?character=ID&page_size=1  (first non-DON card by id, any role)
  character: `
    SELECT cc.character_id AS rep_key, MIN(c.id) AS id, c.name, c.category
    FROM card_characters cc JOIN cards c ON c.id = cc.card_id
    WHERE c.category IS NOT 'Don'
    GROUP BY cc.character_id`,
  // = /artwork?artist=SLUG&page_size=1
  artist: `
    SELECT i.slug AS rep_key, MIN(c.id) AS id, c.name, c.category
    FROM card_illustrators ci
    JOIN illustrators i ON i.id = ci.illustrator_id
    JOIN cards c ON c.id = ci.card_id
    WHERE c.category IS NOT 'Don'
    GROUP BY i.slug`,
  // = /cards?set_id=ID&sort=price&order=desc&page_size=8; the caller then
  // takes the first non-DON of those 8 (else the first), done in JS below.
  set: `
    SELECT rep_key, id, name, category FROM (
      SELECT cs.set_id AS rep_key, c.id, c.name, c.category,
             ROW_NUMBER() OVER (PARTITION BY cs.set_id ORDER BY c.price DESC NULLS LAST, c.id ASC) AS rn
      FROM card_sets cs JOIN cards c ON c.id = cs.card_id
    ) WHERE rn <= 8
    ORDER BY rep_key, rn`,
};

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

    // Search matches the character name, its Japanese name, OR any type label
    // on a card the character appears on — so "Straw Hat Crew" surfaces every
    // crew member, mirroring the /cards label search.
    let where = '';
    let whereParams = [];
    if (q.q) {
      const like = `%${q.q}%`;
      where = `WHERE (
          ch.name LIKE ? COLLATE NOCASE
          OR ch.name_ja LIKE ? COLLATE NOCASE
          OR EXISTS (
               SELECT 1 FROM card_characters cc
               JOIN cards c ON c.id = cc.card_id
               WHERE cc.character_id = ch.id
                 AND EXISTS (SELECT 1 FROM json_each(c.types) WHERE json_each.value LIKE ? COLLATE NOCASE)
             )
        )`;
      whereParams = [like, like, like];
    }

    const orderBy = q.sort === 'name'
      ? 'ORDER BY ch.name ASC'
      : 'ORDER BY card_count DESC, ch.name ASC';

    const sql = `
      SELECT ch.id, ch.name, ch.name_ja, ch.source, ch.wikidata_qid, ch.fandom_title,
             (SELECT count(*) FROM card_characters cc WHERE cc.character_id = ch.id) AS card_count,
             (SELECT count(*) FROM artwork_characters ac JOIN artwork a ON a.id = ac.artwork_id
                WHERE ac.character_id = ch.id AND a.source_url IS NOT NULL AND a.source_url <> '') AS artwork_count
      FROM characters ch
      ${where}
      ${orderBy} LIMIT ? OFFSET ?`;

    const { results } = await c.env.DB.prepare(sql).bind(...whereParams, pageSize, offset).all();

    // A ?q= search count re-runs the whole EXISTS/json_each filter, so skip
    // it when the first page already holds every match.
    const totalCount = await countTotal(c.env.DB, page, pageSize, results.length,
      `SELECT count(*) AS total FROM characters ch ${where}`, whereParams);

    return c.json({
      count: results.length,
      totalCount,
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

    // Primary cards: this character IS the card's subject (role primary, or the
    // legacy NULL rows). role='secondary' is excluded here and served separately.
    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c
       JOIN card_characters cc ON cc.card_id = c.id
       WHERE cc.character_id = ? AND cc.role IS NOT 'secondary'
       ORDER BY c.id`
    ).bind(rawId).all();

    // Secondary cards: this character is DEPICTED IN THE ART of another
    // character's card (role='secondary'). Same card shape as the primary list.
    const { results: secondary } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c
       JOIN card_characters cc ON cc.card_id = c.id
       WHERE cc.character_id = ? AND cc.role = 'secondary'
       ORDER BY c.id`
    ).bind(rawId).all();

    // Non-card official artwork depicting this character (illustrations, ensemble
    // pieces), linked via artwork_characters. Same shape as /artwork/gallery so
    // the frontend can reuse its tiles + lightbox.
    const { results: artwork } = await c.env.DB.prepare(
      `SELECT a.id, a.kind, a.title, a.artist, a.source_url
       FROM artwork a
       JOIN artwork_characters ac ON ac.artwork_id = a.id
       WHERE ac.character_id = ? AND a.source_url IS NOT NULL AND a.source_url <> ''
       ORDER BY CASE a.kind
         WHEN 'illustration' THEN 0 WHEN 'playmat' THEN 1 WHEN 'promo' THEN 2 WHEN 'anniversary' THEN 3 ELSE 4 END, a.id`
    ).bind(rawId).all();

    // Official products featuring this character (v1: starter/ultra decks whose
    // label names the character, linked via set_characters). box_url drives the
    // box-art thumbnail; the frontend links each to its set page.
    const { results: products } = await c.env.DB.prepare(
      `SELECT s.id, s.type, s.label, s.box_url, s.card_count
       FROM sets s
       JOIN set_characters sc ON sc.set_id = s.id
       WHERE sc.character_id = ?
       ORDER BY s.id`
    ).bind(rawId).all();

    return c.json({
      character,
      cards: parseCards(results),
      secondaryCards: parseCards(secondary),
      artwork,
      products,
    });
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
    // DON!! tokens carry gameplay art, not card illustrations — exclude them
    // from the art gallery (mirrors SetDetail dropping category='Don').
    const where = ["c.category IS NOT 'Don'"];
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

    const { results } = await c.env.DB.prepare(
      `SELECT c.* FROM cards c ${joinClause} ${whereClause} ORDER BY c.id LIMIT ? OFFSET ?`
    ).bind(...params, pageSize, offset).all();

    const totalCount = await countTotal(c.env.DB, page, pageSize, results.length,
      `SELECT count(*) AS total FROM cards c ${joinClause} ${whereClause}`, params);

    return c.json({
      count: results.length,
      totalCount,
      page,
      pageSize,
      data: parseCards(results),
    });
  });

  // GET /artwork/gallery
  // Non-card official OP artwork (playmats, box/pack art, promo). Rows carry a
  // Bandai source_url the frontend renders through wsrv.nl (Bandai IP-blocks the
  // Worker, but wsrv reaches it). Playmats/promo lead; box/pack art trails.
  app.get('/artwork/gallery', async (c) => {
    const q = c.req.query();
    const page = Math.max(1, Number(q.page) || 1);
    const pageSize = Math.min(200, Math.max(1, Number(q.page_size) || 48));
    const offset = (page - 1) * pageSize;

    // Optional collection filter (character | group | ship | playmat | box).
    const collection = /^[a-z]+$/.test(q.collection || '') ? q.collection : null;
    const where = ["source_url IS NOT NULL AND source_url <> ''"];
    const params = [];
    if (collection) { where.push('collection = ?'); params.push(collection); }
    const whereClause = 'WHERE ' + where.join(' AND ');
    const order = `ORDER BY CASE collection
         WHEN 'character' THEN 0 WHEN 'group' THEN 1 WHEN 'ship' THEN 2 WHEN 'playmat' THEN 3 ELSE 4 END, id`;

    const countRow = await c.env.DB.prepare(
      `SELECT count(*) AS n FROM artwork ${whereClause}`
    ).bind(...params).first();
    const { results } = await c.env.DB.prepare(
      `SELECT id, kind, collection, title, artist, source_url
       FROM artwork ${whereClause}
       ${order} LIMIT ? OFFSET ?`
    ).bind(...params, pageSize, offset).all();
    // Per-collection counts (unfiltered) so the UI can render collection pills.
    const { results: collections } = await c.env.DB.prepare(
      `SELECT collection, count(*) AS n FROM artwork
       WHERE source_url IS NOT NULL AND source_url <> '' AND collection IS NOT NULL
       GROUP BY collection`
    ).all();
    return c.json({ count: results.length, totalCount: countRow.n, page, pageSize, collection, collections, data: results });
  });

  // GET /representatives?kind=character|artist|set
  // Every directory tile's cover card in one response: { kind, data: { key:
  // { id, name, category } } }, keyed by character id / illustrator slug /
  // set id. The Characters, Illustrators and Cards pages used to fire one
  // /artwork or /cards request per tile as it scrolled into view (~700 for a
  // full Characters scroll). Served from a shared R2 snapshot like the bulk
  // card indexes, so D1 sees a few queries a day.
  app.get('/representatives', async (c) => {
    const kind = c.req.query('kind');
    if (!Object.hasOwn(REP_QUERIES, kind)) {
      return c.json({ error: 'kind must be character, artist or set' }, 400);
    }
    return serveSnapshot(c, `reps-${kind}-v1`, async () => {
      const { results } = await c.env.DB.prepare(REP_QUERIES[kind]).all();
      const groups = new Map();
      for (const { rep_key, id, name, category } of results) {
        const key = String(rep_key);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push({ id, name, category });
      }
      const data = {};
      for (const [key, cards] of groups) {
        // character/artist groups hold one row; a set holds its top 8 by
        // price, and the tile fronts the first non-DON of those.
        data[key] = cards.find((card) => card.category !== 'Don') || cards[0];
      }
      return { kind, count: groups.size, data };
    });
  });

  // GET /products
  // Non-card merch catalog (sleeves, playmats, premium collections, special
  // sets). Card products (boosters/decks/premium boosters) come from /sets.
  // Grouped client-side by `type`.
  app.get('/products', async (c) => {
    const { results } = await c.env.DB.prepare(
      `SELECT slug, type, title, price, release, image_url, product_url
       FROM products ORDER BY
         CASE type WHEN 'collection' THEN 0 WHEN 'set' THEN 1 WHEN 'playmat' THEN 2 WHEN 'sleeve' THEN 3 ELSE 4 END, slug`
    ).all();
    return c.json({ count: results.length, data: results });
  });
}
