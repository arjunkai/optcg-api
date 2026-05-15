#!/usr/bin/env node
// scripts/revoke-key.mjs
//
// Revoke an API key by prefix. The row is kept (status='revoked',
// revoked_at=now) so audit history is preserved; the auth gate filters
// to status='active' so revoked keys stop authenticating immediately
// on the next request (no Worker redeploy needed).
//
// Usage:
//   node scripts/revoke-key.mjs opt_aBcDeFgH

import { d1Execute, sqlLit } from './_d1.mjs';

const prefix = process.argv[2];
if (!prefix) {
  console.error('usage: node scripts/revoke-key.mjs <key_prefix>');
  console.error('example: node scripts/revoke-key.mjs opt_aBcDeFgH');
  process.exit(1);
}

if (!prefix.startsWith('opt_')) {
  console.error('Invalid prefix: must start with opt_');
  process.exit(1);
}

const now = Date.now();
const sql = `UPDATE api_keys SET status = 'revoked', revoked_at = ${now} WHERE key_prefix = ${sqlLit(prefix)} AND status = 'active';`;

try {
  d1Execute(sql);
  console.log(`Revoked: ${prefix}`);
} catch (err) {
  console.error('Failed:', err.message);
  process.exit(1);
}
