// Origin + API key gate for the OPTCG API.
//
// CORS Origin allowlist: only OPBindr frontend origins get full data access.
// Anyone else either supplies a valid API key (X-API-Key header) or gets 403/401.
//
// Public paths (root, docs, OpenAPI, image proxy) bypass the gate so the API
// stays discoverable and binder thumbnails shared on Discord/Twitter still
// render cleanly.

const ALLOWED_EXACT = new Set([
  'https://opbindr.com',
  'https://www.opbindr.com',
  'https://opbindr.pages.dev',
  'http://localhost:5173',
  'http://localhost:4173',
]);

// Regex for Cloudflare Pages preview deploys: <branch>.opbindr.pages.dev
const ALLOWED_REGEX = [
  /^https:\/\/[a-z0-9-]+\.opbindr\.pages\.dev$/,
];

// Path prefixes that bypass the gate entirely. These are the discoverable /
// public-facing endpoints that need to work without auth.
const PUBLIC_PREFIXES = [
  '/images/',          // OPTCG image proxy
  '/pokemon/images/',  // Future PTCG image proxy (Phase 2.2)
];

// Exact public paths.
const PUBLIC_EXACT = new Set([
  '/',
  '/docs',
  '/openapi.json',
  '/healthz',
]);

function isPublicPath(pathname) {
  if (PUBLIC_EXACT.has(pathname)) return true;
  for (const p of PUBLIC_PREFIXES) {
    if (pathname.startsWith(p)) return true;
  }
  return false;
}

function isAllowedOrigin(origin) {
  if (!origin) return false;
  if (ALLOWED_EXACT.has(origin)) return true;
  for (const r of ALLOWED_REGEX) {
    if (r.test(origin)) return true;
  }
  return false;
}

function isValidApiKey(provided, allKeys) {
  if (!provided || !allKeys) return false;
  // Comma-separated list in env.API_KEYS. Trim each. Constant-time compare
  // is overkill here (low value, low call frequency, no timing oracle worth
  // the dependency); plain equality is fine.
  const keys = allKeys.split(',').map(k => k.trim()).filter(Boolean);
  return keys.includes(provided.trim());
}

// Hono middleware. Place this BEFORE the route registrations.
// Returns 403 for disallowed origins, 401 for missing/invalid api keys.
export function gate() {
  return async (c, next) => {
    const url = new URL(c.req.url);
    if (isPublicPath(url.pathname)) {
      await next();
      return;
    }

    const origin = c.req.header('origin');
    if (origin) {
      if (isAllowedOrigin(origin)) {
        await next();
        return;
      }
      return c.json({ error: 'origin not allowed' }, 403);
    }

    // No Origin header — server-to-server caller. Require an API key.
    const key = c.req.header('x-api-key');
    if (isValidApiKey(key, c.env?.API_KEYS)) {
      await next();
      return;
    }
    return c.json({ error: 'api key required' }, 401);
  };
}
