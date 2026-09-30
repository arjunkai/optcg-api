import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { registerSetRoutes } from './sets.js';
import { registerCardRoutes } from './cards.js';
import { registerImageRoutes } from './images.js';
import { registerDocsRoutes } from './docs.js';
import { gate, isAllowedOrigin } from './auth.js';
import { edgeCache } from './edgeCache.js';
import { registerPokemonRoutes } from './pokemon/index.js';
import { registerCanvsRoutes } from './canvs.js';
import { checkUsageAlerts, warmColdImages } from './cron.js';

const app = new Hono();

app.use('*', cors({
  origin: (origin) => {
    // Echo the request origin back only if it's in the allowlist (shared
    // with the gate, see auth.js). hono/cors expects either a string (one
    // origin), an array, or a function. The function form lets us return
    // null (no header set, browser blocks the request) for disallowed
    // origins. Non-browser callers (no Origin header) get no CORS headers;
    // the gate() middleware below handles auth via X-API-Key.
    return isAllowedOrigin(origin) ? origin : null;
  },
  allowMethods: ['GET', 'HEAD', 'OPTIONS'],
  allowHeaders: ['Content-Type', 'X-API-Key'],
  // Lets browser clients back off on a 429 (Retry-After) and see cache hits.
  exposeHeaders: ['Retry-After', 'X-Cache'],
}));

app.use('*', gate());
// After the gate (needs to know browser vs keyed caller), before routes.
app.use('*', edgeCache());

// Root is public so /docs has somewhere to send curious visitors. Don't
// enumerate the route surface here — keyholders read /openapi.json, the
// rest get pointed at /docs to request access.
app.get('/', (c) => {
  return c.json({
    name: 'OPTCG API',
    version: '1.0.0',
    docs: '/docs',
    access: 'mailto:arjunkaibansal@gmail.com',
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
registerCanvsRoutes(app);

// Exporting both fetch and scheduled lets wrangler treat this as a
// Worker with both HTTP and cron entry points. The cron schedule is
// defined in wrangler.toml [triggers].
export default {
  fetch: app.fetch,
  scheduled: async (controller, env, ctx) => {
    ctx.waitUntil(checkUsageAlerts(env));
    // Self-healing: warm any cold card images into R2 each run (incl. new sets),
    // so no card ever depends on a flaky live Bandai fetch. See src/cron.js.
    ctx.waitUntil(warmColdImages(env));
  },
};
