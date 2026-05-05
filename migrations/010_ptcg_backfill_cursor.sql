-- Resumable-cursor table for long-running price/image backfills.
--
-- Why: eBay JA pricing has more than fits in GitHub Actions' 6h hard
-- ceiling. Without a cursor, every Monday the job restarts from the
-- alphabetically-first card and gets killed mid-run, so the tail of
-- the catalog never gets attempted. With a cursor, week N picks up
-- where week N-1 was killed, and we eventually cover everything.
--
-- Each backfill source owns one row keyed by `source` (e.g. 'ebay_ja',
-- 'ebay_en', 'ebay_images_ja'). Scripts read last_card_id at start,
-- query `WHERE card_id > last_card_id ORDER BY card_id`, and UPSERT
-- the cursor every N cards processed. On reaching the end of the
-- unpriced list, the script clears the cursor (last_card_id = NULL)
-- so the next run starts fresh.
--
-- Storing in D1 (not in repo or actions/cache) so:
--   - timeout-killed jobs preserve progress (FS is gone, but D1 isn't)
--   - no commit-back race with the submodule-bump step
--   - actions/cache eviction (7d) doesn't lose state
--
-- Apply: paste into Supabase-style dashboard or `wrangler d1 execute
-- optcg-cards --remote --file migrations/010_ptcg_backfill_cursor.sql`.

CREATE TABLE IF NOT EXISTS ptcg_backfill_cursor (
  source TEXT PRIMARY KEY,
  last_card_id TEXT,
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
