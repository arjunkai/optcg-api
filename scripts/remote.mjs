#!/usr/bin/env node
// Runs a production key script (npm run key:*) after a person confirms.
// Refuses in tests, CI and any non-interactive shell, so an automated agent
// can't write to production by following an error message.
import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline/promises';

const [script, ...args] = process.argv.slice(2);
if (!script) {
  console.error('usage: node scripts/remote.mjs <script> [args...]');
  process.exit(1);
}
if (process.env.NODE_TEST_CONTEXT || !process.stdin.isTTY) {
  console.error('Refusing: production key scripts need a person at an interactive terminal. Automated agents must never run them.');
  process.exit(1);
}
const rl = createInterface({ input: process.stdin, output: process.stdout });
const answer = await rl.question('This touches the PRODUCTION database. Type "prod" to continue: ');
rl.close();
if (answer.trim() !== 'prod') {
  console.error('Cancelled.');
  process.exit(1);
}
const r = spawnSync(process.execPath, [script, ...args], {
  stdio: 'inherit',
  env: { ...process.env, OPTCG_ALLOW_REMOTE: '1' },
});
process.exit(r.status ?? 1);
