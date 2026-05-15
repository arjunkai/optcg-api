-- 012_api_key_usage.sql
--
-- Daily request counters per API key. Written lazily by the rate-limit
-- middleware in src/auth.js once every 60 requests (per key) so the
-- D1 free-tier write budget (100k/day) isn't burnt by per-request
-- updates. Cache API (per-colo, no write cost) handles the live count
-- between flushes.
--
-- The MAX(...) clause in the upsert protects against a stale lazy-flush
-- overwriting a fresher one (two concurrent INSERTs from different colos
-- race to write the same row).

CREATE TABLE IF NOT EXISTS api_key_usage (
  api_key TEXT NOT NULL,
  day TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (api_key, day)
);

CREATE INDEX IF NOT EXISTS idx_api_key_usage_day ON api_key_usage (day);
