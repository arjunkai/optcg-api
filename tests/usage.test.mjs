import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequestCounter, createUnitMeter, utcDay } from '../src/usage.js';
import { createD1, setCount, getCount } from './helpers/d1.mjs';
import { fakeCtx, makeClock } from './helpers/fakes.mjs';

const P = 'opt_aaaaaaaa';
const DAY = '2026-10-01';
function setup(start = `${DAY}T10:00:00Z`) {
  const d1 = createD1(); const ctx = fakeCtx(); const clock = makeClock(start);
  return { d1, ctx, clock, wu: (p) => ctx.waitUntil(p) };
}

test('utcDay', () => assert.equal(utcDay(Date.UTC(2026, 9, 1, 23, 59, 59)), DAY));

test('request counter flushes at 25 requests', async () => {
  const { d1, ctx, clock, wu } = setup();
  const r = createRequestCounter({ now: clock.now });
  for (let i = 0; i < 24; i++) await r.add(d1, P, wu);
  await ctx.drain();
  assert.equal(getCount(d1, P, DAY), 0);
  assert.equal(await r.add(d1, P, wu), 25);
  await ctx.drain();
  assert.equal(getCount(d1, P, DAY), 25);
});

test('request counter flushes a small pending count after 5 minutes', async () => {
  const { d1, ctx, clock, wu } = setup();
  const r = createRequestCounter({ now: clock.now });
  for (let i = 0; i < 3; i++) await r.add(d1, P, wu);
  clock.advance(299_000);
  await r.add(d1, P, wu);
  await ctx.drain();
  assert.equal(getCount(d1, P, DAY), 0);
  clock.advance(1_000);
  await r.add(d1, P, wu);
  await ctx.drain();
  assert.equal(getCount(d1, P, DAY), 5);
});

test('three isolates sum (the old MAX flush gave 300)', async () => {
  const { d1, ctx, clock, wu } = setup();
  const iso = [0, 1, 2].map(() => createRequestCounter({ now: clock.now }));
  for (let i = 0; i < 300; i++) for (const r of iso) await r.add(d1, P, wu);
  await ctx.drain();
  assert.equal(getCount(d1, P, DAY), 900);
});

test('peek reads D1 plus local; re-reads every 5 minutes; one read for concurrent first calls', async () => {
  const { d1, clock, wu } = setup();
  setCount(d1, P, DAY, 1000);
  const r = createRequestCounter({ now: clock.now });
  await Promise.all(Array.from({ length: 10 }, () => r.peek(d1, P, wu)));
  assert.equal(d1.queries.filter((q) => q.startsWith('SELECT count')).length, 1);
  await r.add(d1, P, wu);
  assert.equal(await r.peek(d1, P, wu), 1001);
  setCount(d1, P, DAY, 2000);
  clock.advance(299_000);
  assert.equal(await r.peek(d1, P, wu), 1001);
  clock.advance(1_000);
  assert.equal(await r.peek(d1, P, wu), 2001);
});

test('UTC midnight: old tail to old day, new day from zero', async () => {
  const { d1, ctx, clock, wu } = setup(`${DAY}T23:59:50Z`);
  const r = createRequestCounter({ now: clock.now });
  for (let i = 0; i < 10; i++) await r.add(d1, P, wu);
  clock.set('2026-10-02T00:00:01Z');
  assert.equal(await r.add(d1, P, wu), 1);
  await ctx.drain();
  assert.equal(getCount(d1, P, DAY), 10);
});

test('failed flush is put back; failed reads keep counting; no DB works', async () => {
  const { d1, ctx, clock, wu } = setup();
  const r = createRequestCounter({ now: clock.now });
  d1.failWhen = (sql) => sql.startsWith('INSERT');
  for (let i = 0; i < 25; i++) await r.add(d1, P, wu);
  await ctx.drain();                      // flush of 25 failed and was put back
  d1.failWhen = null;
  for (let i = 0; i < 25; i++) await r.add(d1, P, wu);
  await ctx.drain();                      // the first of these flushed 26; 24 pending
  assert.equal(getCount(d1, P, DAY), 26);
  assert.equal(await r.peek(d1, P, wu), 50, 'nothing lost');
  const r2 = createRequestCounter({ now: clock.now });
  d1.failWhen = (sql) => sql.startsWith('SELECT');
  let n; for (let i = 0; i < 3; i++) n = await r2.add(d1, 'opt_bbbbbbbb', wu);
  assert.equal(n, 3);
  assert.equal(await createRequestCounter({ now: clock.now }).add(undefined, P, () => {}), 1);
});

test('unit meter charges atomically and refuses past the cap', async () => {
  const { d1, clock } = setup();
  const m = createUnitMeter({ now: clock.now });
  assert.deepEqual(await m.charge(d1, 'u:' + P, 15000, 20000), { ok: true, total: 15000 });
  const r = await m.charge(d1, 'u:' + P, 15000, 20000);
  assert.equal(r.ok, false);
  assert.equal(m.isExhausted('u:' + P), true);
  assert.equal(getCount(d1, 'u:' + P, DAY), 30000, 'the refused charge still counts (stricter)');
});

test('unit meter: 10 isolates allow exactly cap / weight charges', async () => {
  const { d1, clock } = setup();
  const isolates = Array.from({ length: 10 }, () => createUnitMeter({ now: clock.now }));
  let allowed = 0;
  for (let i = 0; i < 200; i++) if ((await isolates[i % 10].charge(d1, 'u:outside', 15000, 1_800_000)).ok) allowed++;
  assert.equal(allowed, 120);
  // After the cap, each isolate writes at most once more before its memo stops it.
  assert.ok(getCount(d1, 'u:outside', DAY) <= 1_800_000 + 15000 * 10, String(getCount(d1, 'u:outside', DAY)));
});

test('unit meter memo: once exhausted, no more writes until the next UTC day', async () => {
  const { d1, clock } = setup();
  const m = createUnitMeter({ now: clock.now });
  setCount(d1, 'u:' + P, DAY, 50_000);
  assert.equal((await m.charge(d1, 'u:' + P, 1000, 50_000)).ok, false);
  const writes = d1.queries.length;
  for (let i = 0; i < 20; i++) assert.equal((await m.charge(d1, 'u:' + P, 1000, 50_000)).ok, false);
  assert.equal(d1.queries.length, writes);
  clock.set('2026-10-02T00:00:01Z');
  assert.equal(m.isExhausted('u:' + P), false);
  assert.equal((await m.charge(d1, 'u:' + P, 1000, 50_000)).ok, true);
});

test('unit meter throws on D1 failure; no DB means ok', async () => {
  const { d1, clock } = setup();
  d1.failWhen = () => true;
  await assert.rejects(() => createUnitMeter({ now: clock.now }).charge(d1, 'u:x', 500, 1000), /injected/);
  assert.deepEqual(await createUnitMeter({ now: clock.now }).charge(undefined, 'u:x', 500, 1000), { ok: true, total: 0 });
});
