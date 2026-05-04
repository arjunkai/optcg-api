// Pokémon image proxy. Path:
//   /pokemon/images/:lang/:series/:setId/:localId/{quality}.{ext}
// where quality is 'low' | 'high' and ext is 'webp' | 'png' | 'jpg'.
//
// First request lazy-fetches from TCGdex's CDN, caches in R2 forever
// at key `ptcg/{lang}/{series}/{setId}/{localId}/{quality}.{ext}`. Subsequent
// requests (and the public CDN downstream of this Worker) hit R2 only.
//
// TCGdex assets URL pattern (verified):
//   https://assets.tcgdex.net/{lang}/{series}/{setId}/{localId}/{quality}.{ext}
// The series segment is required (e.g. 'sv', 'base', 'xy') and is stored
// in ptcg_sets.series populated during import. The frontend constructs the
// image URL from the card's image_high / image_low fields seeded at import
// time, so the series is already baked in — this proxy just forwards it.

const TCGDEX_ASSETS = 'https://assets.tcgdex.net';

const ALLOWED_QUALITIES = new Set(['low', 'high']);
const ALLOWED_EXTS = new Set(['webp', 'png', 'jpg']);

function mimeFor(ext) {
  return ext === 'webp' ? 'image/webp' : ext === 'png' ? 'image/png' : 'image/jpeg';
}

export function registerPokemonImageRoutes(app) {
  // Pattern: /pokemon/images/:lang/:series/:setId/:localId/:filename
  app.get('/pokemon/images/:lang/:series/:setId/:localId/:filename', async (c) => {
    const { lang, series, setId, localId, filename } = c.req.param();
    const m = filename.match(/^(low|high)\.(webp|png|jpg)$/);
    if (!m) return c.text('Bad filename', 400);
    const quality = m[1];
    const ext = m[2];
    if (!ALLOWED_QUALITIES.has(quality) || !ALLOWED_EXTS.has(ext)) return c.text('Bad params', 400);

    // Validate path components — defensive against path traversal.
    // setId / localId allow dots because TCGdex uses them for half-step
    // expansions (sv03.5, sm7.5, swsh4.5, me02.5 — 14 sets total) and
    // some local_ids (cel25-2A-style variants in Celebrations). Reject
    // path-traversal attempts explicitly: no '..', no leading '.', no
    // slashes (already excluded by character class).
    if (!/^[a-z]{2}(-[a-z]{2})?$/.test(lang)) return c.text('Bad lang', 400);
    if (!/^[a-zA-Z0-9_-]+$/.test(series)) return c.text('Bad series', 400);
    if (!/^[a-zA-Z0-9_.-]+$/.test(setId) || setId.startsWith('.') || setId.includes('..')) return c.text('Bad setId', 400);
    if (!/^[a-zA-Z0-9_.-]+$/.test(localId) || localId.startsWith('.') || localId.includes('..')) return c.text('Bad localId', 400);

    const r2Key = `ptcg/${lang}/${series}/${setId}/${localId}/${quality}.${ext}`;

    // R2 first
    const cached = await c.env.IMAGES.get(r2Key);
    if (cached) {
      return new Response(cached.body, {
        headers: {
          'Content-Type': mimeFor(ext),
          'Cache-Control': 'public, max-age=31536000, immutable',
          'X-Cache': 'HIT',
        },
      });
    }

    // Upstream fetch from TCGdex CDN
    const upstream = `${TCGDEX_ASSETS}/${lang}/${series}/${setId}/${localId}/${quality}.${ext}`;
    const resp = await fetch(upstream, { cf: { cacheTtl: 3600 } });
    if (!resp.ok) return c.text('Upstream missing', resp.status);

    const buf = await resp.arrayBuffer();
    // Persist to R2 in the background. Don't block the response.
    c.executionCtx.waitUntil(
      c.env.IMAGES.put(r2Key, buf, {
        httpMetadata: { contentType: mimeFor(ext) },
      })
    );

    return new Response(buf, {
      headers: {
        'Content-Type': mimeFor(ext),
        'Cache-Control': 'public, max-age=31536000, immutable',
        'X-Cache': 'MISS',
      },
    });
  });
}
