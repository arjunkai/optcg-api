import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { registerSetRoutes } from './sets.js';
import { registerCardRoutes } from './cards.js';
import { registerImageRoutes } from './images.js';
import { registerDocsRoutes } from './docs.js';
import { gate } from './auth.js';

const CORS_ALLOWED_EXACT = [
  'https://opbindr.com',
  'https://www.opbindr.com',
  'https://opbindr.pages.dev',
  'http://localhost:5173',
  'http://localhost:4173',
];

const app = new Hono();

app.use('*', cors({
  origin: (origin) => {
    // Echo the request origin back only if it's in our allowlist.
    // hono/cors expects either a string (one origin), an array, or a
    // function. The function form lets us return null (no header set,
    // browser blocks the request) for disallowed origins.
    if (CORS_ALLOWED_EXACT.includes(origin)) return origin;
    if (/^https:\/\/[a-z0-9-]+\.opbindr\.pages\.dev$/.test(origin || '')) return origin;
    // For non-browser callers (no Origin header) hono/cors gets undefined
    // and we don't set the CORS headers. The gate() middleware below
    // handles auth via X-API-Key.
    return null;
  },
  allowMethods: ['GET', 'HEAD', 'OPTIONS'],
  allowHeaders: ['Content-Type', 'X-API-Key'],
}));

app.use('*', gate());

app.get('/', (c) => {
  return c.json({
    name: 'OPTCG API',
    version: '1.0.0',
    docs: '/docs',
    endpoints: [
      'GET /sets',
      'GET /sets/{id}/cards',
      'GET /cards',
      'GET /cards/{id}',
      'GET /cards/{id}/price-history',
      'GET /images/{card_id}',
    ],
  });
});

registerSetRoutes(app);
registerCardRoutes(app);
registerImageRoutes(app);
registerDocsRoutes(app);

export default app;
