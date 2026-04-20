-- Price history: one row per card per weekly price refresh, used to render
-- TCGPlayer-style price charts on OPBindr's card-enlarge modal.
--
-- Design notes:
--   * Pruning is not needed at current scale (~1500 cards * 52 weeks/year =
--     78k rows/year). Row count stays comfortable inside D1 limits.
--   * captured_at is unix seconds to match existing price_updated_at.
--   * No FK — keeping the history orphan-safe if a card id is ever changed
--     during a data migration; indexed lookup on card_id is sufficient.
--   * PRIMARY KEY (card_id, captured_at) prevents duplicate snapshots if an
--     import gets retried in the same second.

CREATE TABLE IF NOT EXISTS card_price_history (
  card_id TEXT NOT NULL,
  price REAL NOT NULL,
  captured_at INTEGER NOT NULL,
  PRIMARY KEY (card_id, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_price_history_card_time
  ON card_price_history(card_id, captured_at DESC);

-- Seed with current prices so the chart has at least one data point
-- immediately instead of being empty for a week after deploy.
INSERT OR IGNORE INTO card_price_history (card_id, price, captured_at)
SELECT id, price, COALESCE(price_updated_at, strftime('%s', 'now'))
FROM cards
WHERE price IS NOT NULL AND price > 0;
