-- Fixups to migration 007 found via code review:
--   1. Three xyp-XY* cards have a cardmarket object whose every numeric
--      field is null (avg, low, trend all null). The 007 backfill stamped
--      them 'cardmarket' but they have no usable price. Reset to NULL so
--      coverage queries reflect reality.
--   2. Add an index on price_source matching the OPTCG precedent
--      (migration 004 indexed cards.price_source).

UPDATE ptcg_cards
   SET price_source = NULL
 WHERE card_id IN ('xyp-XY124', 'xyp-XY84', 'xyp-XY89')
   AND price_source = 'cardmarket';

CREATE INDEX IF NOT EXISTS idx_ptcg_cards_price_source
    ON ptcg_cards(price_source);
