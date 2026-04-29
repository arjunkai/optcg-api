-- Adds price_source to ptcg_cards. Backfills existing priced rows to
-- 'cardmarket' (the only source the 2026-04-29 import populated).
ALTER TABLE ptcg_cards ADD COLUMN price_source TEXT;
UPDATE ptcg_cards
   SET price_source = 'cardmarket'
 WHERE price_source IS NULL
   AND pricing_json IS NOT NULL
   AND pricing_json != '{}'
   AND pricing_json NOT LIKE '%"cardmarket":null%';
