-- Track which data source populated each card's price. Nullable for legacy
-- rows; new writes should always set it.
--
-- Known values:
--   'tcgplayer' — scraped from TCGPlayer price guides (primary source)
--   'dotgg'     — fetched from api.dotgg.gg as a fallback for cards TCGPlayer
--                 doesn't list (event promos, championship parallels, etc.)
--   'manual'    — set by hand via a one-off SQL update
--
-- Backfill existing rows: any card with a price set before this migration
-- came from TCGPlayer (that's the only source that existed), so tag them all.

ALTER TABLE cards ADD COLUMN price_source TEXT;

UPDATE cards SET price_source = 'tcgplayer' WHERE price IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_price_source ON cards(price_source);
