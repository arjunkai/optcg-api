-- 021_compact_ptcg_price_history.sql
--
-- ptcg_price_history reached ~5.3M rows / ~650 MB (2026-09-30), taking the
-- database past D1's 500 MB free-tier cap, after which every write fails.
-- Two causes:
--   1. The weekly snapshot stored up to 12 Cardmarket (EUR) keys per card.
--      OPBindr's chart plots one USD series per card, so none of those rows
--      were ever drawn. The script now keeps one Cardmarket series (the first
--      present in the chart's priority order); this copy drops the rest.
--   2. Each row lived three times: the rowid table, the primary-key index and
--      a second (card_id, recorded_at) index. WITHOUT ROWID with the key in
--      query order stores it once and makes a row 1 write instead of 3.
--
-- FOR PROD, DON'T RUN THIS FILE: the single INSERT is far too big for one D1
-- query. Run `node scripts/compact-ptcg-price-history.mjs`, which does the
-- same copy in rowid chunks and then this file's final swap. This file is
-- the definition for fresh/local databases.
--
-- Also drops idx_price_history_card_time, which duplicated
-- card_price_history's own primary key (card_id, captured_at).

CREATE TABLE ptcg_price_history_compact (
  card_id      TEXT NOT NULL,
  source       TEXT NOT NULL,   -- 'tcgplayer' | 'cardmarket' | 'manual' | 'ebay' | ...
  variant      TEXT NOT NULL,   -- 'holofoil' | 'normal' | 'avg' | 'market' | ...
  recorded_at  INTEGER NOT NULL,
  price_usd    REAL,
  price_eur    REAL,
  -- /pokemon/cards/:id/price-history reads WHERE card_id = ? AND
  -- recorded_at >= ? ORDER BY recorded_at, so it's a straight key-range scan.
  PRIMARY KEY (card_id, recorded_at, source, variant)
) WITHOUT ROWID;

-- Cardmarket: keep a row only when no higher-priority Cardmarket key exists
-- for the same card and snapshot (priority = CARDMARKET_VARIANTS in
-- scripts/snapshot_ptcg_price_history.py).
INSERT OR IGNORE INTO ptcg_price_history_compact
  (card_id, source, variant, recorded_at, price_usd, price_eur)
WITH rank(variant, rk) AS (VALUES
  ('avg', 0), ('trend', 1), ('avg7', 2), ('avg30', 3), ('avg1', 4), ('low', 5),
  ('reverseHoloSell', 6), ('reverseHoloTrend', 7), ('reverseHoloLow', 8),
  ('avg7Foil', 9), ('avg30Foil', 10), ('lowFoil', 11))
SELECT h.card_id, h.source, h.variant, h.recorded_at, h.price_usd, h.price_eur
FROM ptcg_price_history h
WHERE (h.source <> 'cardmarket'
   OR NOT EXISTS (
        SELECT 1 FROM rank hi
        JOIN ptcg_price_history b
          ON b.card_id = h.card_id AND b.source = 'cardmarket'
         AND b.variant = hi.variant AND b.recorded_at = h.recorded_at
        WHERE hi.rk < (SELECT rk FROM rank WHERE rank.variant = h.variant)));

DROP TABLE ptcg_price_history;
ALTER TABLE ptcg_price_history_compact RENAME TO ptcg_price_history;
DROP INDEX IF EXISTS idx_price_history_card_time;
