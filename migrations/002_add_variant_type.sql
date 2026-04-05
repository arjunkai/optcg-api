-- Add variant_type column to cards table
-- Values: null (base card), 'alt_art', 'reprint', 'manga', 'serial'
ALTER TABLE cards ADD COLUMN IF NOT EXISTS variant_type TEXT;

-- Index for filtering by variant type
CREATE INDEX IF NOT EXISTS idx_cards_variant_type ON cards(variant_type);

-- Backfill existing parallels with defaults (classifier refines these later)
UPDATE cards SET variant_type = 'alt_art' WHERE parallel = true AND id ~ '_p\d+$';
UPDATE cards SET variant_type = 'reprint' WHERE parallel = true AND id ~ '_r\d+$';
