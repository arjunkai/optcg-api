#!/usr/bin/env node
// scripts/set-tier.mjs: change an API key's tier without reissuing it,
// within KEY_POLICY. Takes effect within 5 minutes (key lookups are cached).
//
// Usage: npm run key:set-tier -- --prefix opt_aBcDeFgH --tier partner

import { parseArgs } from 'node:util';
import { d1Execute, d1Query, sqlLit } from './_d1.mjs';
import { TIER_NAMES, KEY_POLICY } from '../src/limits.js';

const { values } = parseArgs({ options: { prefix: { type: 'string' }, tier: { type: 'string' } } });

if (!values.prefix || !values.tier) {
  console.error(`usage: npm run key:set-tier -- --prefix opt_xxxxxxxx --tier ${TIER_NAMES.join('|')}`);
  process.exit(1);
}
if (!/^opt_[A-Za-z0-9_-]{8}$/.test(values.prefix)) {
  console.error('Invalid prefix: expected opt_ followed by 8 characters (see npm run key:list)');
  process.exit(1);
}
if (!TIER_NAMES.includes(values.tier)) {
  console.error(`Invalid tier: "${values.tier}". Valid tiers: ${TIER_NAMES.join(', ')}`);
  process.exit(1);
}

try {
  const rows = d1Query(`SELECT COUNT(*) AS n FROM api_keys WHERE status = 'active' AND tier = ${sqlLit(values.tier)} AND key_prefix != ${sqlLit(values.prefix)} AND instr(scopes, 'admin') = 0 AND instr(scopes, 'firstparty') = 0;`);
  if ((rows[0]?.n ?? 0) >= KEY_POLICY[values.tier]) {
    console.error(`Key policy: at most ${KEY_POLICY[values.tier]} active ${values.tier} keys (src/limits.js KEY_POLICY).`);
    process.exit(1);
  }
  d1Execute(`UPDATE api_keys SET tier = ${sqlLit(values.tier)} WHERE key_prefix = ${sqlLit(values.prefix)} AND status = 'active';`);
} catch (err) {
  console.error(err.message);
  process.exit(1);
}
console.log(`Set ${values.prefix} to ${values.tier}. Takes effect within 5 minutes. Check with: npm run key:list`);
