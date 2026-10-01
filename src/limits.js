// API key tiers and daily caps, sized for the Workers Free plan's shared
// daily allowances (100k requests, 5M D1 rows read, 100k written). Design:
// docs/superpowers/specs/2026-10-01-api-limits-phase1-design.md
// perMinute / heavyPerMinute must match the bindings in wrangler.toml
// (tests/bindings.test.mjs).

const f = Object.freeze;

export const TIERS = f({
  free: f({ perMinute: 20, daily: 500, dailyUnits: 50_000, heavyPerMinute: 2,
    minuteBinding: 'RL_KEY_FREE', heavyBinding: 'RL_KEY_HEAVY_FREE' }),
  standard: f({ perMinute: 60, daily: 2_000, dailyUnits: 200_000, heavyPerMinute: 10,
    minuteBinding: 'RL_MINUTE', heavyBinding: 'RL_KEY_HEAVY' }),
  partner: f({ perMinute: 120, daily: 10_000, dailyUnits: 1_000_000, heavyPerMinute: 30,
    minuteBinding: 'RL_KEY_PARTNER', heavyBinding: 'RL_KEY_HEAVY_PARTNER' }),
});

export const TIER_NAMES = Object.keys(TIERS);

// Most active keys per tier (issue-key / set-tier enforce it). With every
// key at its cap: 10 x 2,000 + 2 x 10,000 = 40k requests/day, 40% of Free.
export const KEY_POLICY = f({ free: 0, standard: 10, partner: 2 });

// D1 units (about rows read) per UTC day for all outside keys together.
// See tests/capacity.test.mjs.
export const OUTSIDE_UNITS_DAILY = 1_800_000;

// Unknown or missing tiers get the most restrictive tier (fail closed).
export function tierFor(name) {
  const key = typeof name === 'string' && Object.hasOwn(TIERS, name) ? name : 'free';
  return { name: key, ...TIERS[key] };
}

export function secondsUntilUtcMidnight(ms) {
  const next = new Date(ms);
  next.setUTCHours(24, 0, 0, 0);
  return Math.max(1, Math.ceil((next.getTime() - ms) / 1000));
}
