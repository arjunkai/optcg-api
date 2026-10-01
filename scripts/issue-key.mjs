#!/usr/bin/env node
// scripts/issue-key.mjs
//
// Issue a new OPTCG API key. Generates a cryptographically-random key,
// inserts the SHA-256 hash + metadata into the D1 api_keys table, and
// prints the raw key to stdout ONE TIME so it can be delivered to the
// recipient. The raw key is never recoverable after this.
//
// Usage:
//   npm run key:issue -- --owner "Name" [--contact "..."] [--notes "..."] [--tier free|standard|partner] [--scopes "optcg,ptcg"]
//
// Default scope is 'optcg' only. Pass --scopes "optcg,ptcg" to also
// grant Pokemon TCG endpoint access. Add 'admin' only for your own
// maintenance key: it lets refresh=1 purge caches and force full-table
// D1 rebuilds. Add 'firstparty' for Arjun's own services (opbindr-bot): no
// daily caps, never counted. Public paths (/, /docs, image proxies) never check scopes
// regardless.

import { parseArgs } from 'node:util';
import { webcrypto } from 'node:crypto';
import { d1Execute, d1Query, sqlLit } from './_d1.mjs';
import { TIER_NAMES, KEY_POLICY } from '../src/limits.js';

const VALID_SCOPES = new Set(['optcg', 'ptcg', 'admin', 'firstparty']);

const { values } = parseArgs({
  options: {
    owner: { type: 'string' },
    contact: { type: 'string' },
    notes: { type: 'string' },
    tier: { type: 'string', default: 'standard' },
    scopes: { type: 'string', default: 'optcg' },
  },
});

if (!values.owner) {
  console.error('usage: node scripts/issue-key.mjs --owner "Name" [--contact "..."] [--notes "..."] [--tier free|standard|partner] [--scopes "optcg,ptcg"]');
  process.exit(1);
}

const scopeList = values.scopes.split(',').map(s => s.trim()).filter(Boolean);
for (const s of scopeList) {
  if (!VALID_SCOPES.has(s)) {
    console.error(`Invalid scope: "${s}". Valid scopes: ${[...VALID_SCOPES].join(', ')}`);
    process.exit(1);
  }
}
if (scopeList.length === 0) {
  console.error('At least one scope is required.');
  process.exit(1);
}
if (!TIER_NAMES.includes(values.tier)) {
  console.error(`Invalid tier: "${values.tier}". Valid tiers: ${TIER_NAMES.join(', ')}`);
  process.exit(1);
}
const exemptFromPolicy = scopeList.includes('admin') || scopeList.includes('firstparty');
const normalisedScopes = scopeList.join(',');

function generateKey() {
  const bytes = new Uint8Array(24);
  webcrypto.getRandomValues(bytes);
  return 'opt_' + Buffer.from(bytes).toString('base64url');
}

async function sha256Hex(text) {
  const buf = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

const rawKey = generateKey();
const keyHash = await sha256Hex(rawKey);
const keyPrefix = rawKey.slice(0, 12);
const now = Date.now();

const sql = `INSERT INTO api_keys (key_hash, key_prefix, owner_name, owner_contact, notes, tier, scopes, status, created_at) VALUES (${sqlLit(keyHash)}, ${sqlLit(keyPrefix)}, ${sqlLit(values.owner)}, ${sqlLit(values.contact ?? null)}, ${sqlLit(values.notes ?? null)}, ${sqlLit(values.tier)}, ${sqlLit(normalisedScopes)}, 'active', ${now});`;

if (!exemptFromPolicy) {
  let active = 0;
  try {
    const rows = d1Query(`SELECT COUNT(*) AS n FROM api_keys WHERE status = 'active' AND tier = ${sqlLit(values.tier)} AND instr(scopes, 'admin') = 0 AND instr(scopes, 'firstparty') = 0;`);
    active = rows[0]?.n ?? 0;
  } catch (err) {
    console.error(err.message);
    process.exit(1);
  }
  if (active >= KEY_POLICY[values.tier]) {
    console.error(`Key policy: at most ${KEY_POLICY[values.tier]} active ${values.tier} keys (src/limits.js KEY_POLICY). Revoke one or use another tier.`);
    process.exit(1);
  }
}

try {
  d1Execute(sql);
} catch (err) {
  console.error('Failed to insert key into D1:', err.message);
  process.exit(1);
}

console.log('');
console.log('===============================================================');
console.log('  NEW API KEY ISSUED');
console.log('===============================================================');
console.log(`  Key:      ${rawKey}`);
console.log(`  Prefix:   ${keyPrefix}`);
console.log(`  Owner:    ${values.owner}`);
if (values.contact) console.log(`  Contact:  ${values.contact}`);
if (values.notes) console.log(`  Notes:    ${values.notes}`);
console.log(`  Tier:     ${values.tier}`);
console.log(`  Scopes:   ${normalisedScopes}`);
console.log('---------------------------------------------------------------');
console.log('  [!] Copy the key now. It will NOT be shown again.');
console.log(`  To revoke later: npm run key:revoke -- ${keyPrefix}`);
console.log('===============================================================');
