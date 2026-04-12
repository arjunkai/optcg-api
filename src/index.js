import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { registerSetRoutes } from './sets.js';
import { registerCardRoutes } from './cards.js';

const app = new Hono();

app.use('*', cors({
  origin: '*',
  allowMethods: ['GET', 'HEAD'],
  allowHeaders: ['*'],
}));

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
      'GET /images/{card_id}',
    ],
  });
});

registerSetRoutes(app);
registerCardRoutes(app);

export default app;
