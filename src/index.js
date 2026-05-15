import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { registerSetRoutes } from './sets.js';
import { registerCardRoutes } from './cards.js';
import { registerImageRoutes } from './images.js';
import { registerDocsRoutes } from './docs.js';
import { gate } from './auth.js';
import { registerPokemonRoutes } from './pokemon/index.js';
import { checkUsageAlerts } from './cron.js';

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

// Root is public so /docs has somewhere to send curious visitors. Don't
// enumerate the route surface here — keyholders read /openapi.json, the
// rest get pointed at /docs to request access.
app.get('/', (c) => {
  return c.json({
    name: 'OPTCG API',
    version: '1.0.0',
    docs: '/docs',
    access: 'https://forms.gle/56bcJgdKKSVRzjtA7',
  });
});

app.get('/healthz', (c) => {
  return c.json({ ok: true, ts: Date.now() });
});

registerSetRoutes(app);
registerCardRoutes(app);
registerImageRoutes(app);
registerDocsRoutes(app);
registerPokemonRoutes(app);

// Exporting both fetch and scheduled lets wrangler treat this as a
// Worker with both HTTP and cron entry points. The cron schedule is
// defined in wrangler.toml [triggers].
export default {
  fetch: app.fetch,
  scheduled: async (controller, env, ctx) => {
    ctx.waitUntil(checkUsageAlerts(env));
  },
};
