-- Pricing fields modeled after dotgg.gg API.
-- tcg_ids is a JSON array (normal + foil TCGPlayer products can differ).
-- price_updated_at is a unix timestamp for staleness checks.

ALTER TABLE cards ADD COLUMN price REAL;
ALTER TABLE cards ADD COLUMN foil_price REAL;
ALTER TABLE cards ADD COLUMN delta_price REAL;
ALTER TABLE cards ADD COLUMN delta_7d_price REAL;
ALTER TABLE cards ADD COLUMN tcg_ids TEXT;
ALTER TABLE cards ADD COLUMN price_updated_at INTEGER;

CREATE INDEX IF NOT EXISTS idx_cards_price ON cards(price);
CREATE INDEX IF NOT EXISTS idx_cards_price_updated_at ON cards(price_updated_at);
