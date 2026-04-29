-- Adds retreat to ptcg_cards. The raw TCGdex JSON we stored on each row
-- already carries it under .retreat — backfill from there so we don't
-- need a re-fetch. Frontend's AddCardsModal exposes a "Retreat" sort
-- pill (PTCG_PROFILE.sorts) that was a no-op until this column landed.
ALTER TABLE ptcg_cards ADD COLUMN retreat INTEGER;
UPDATE ptcg_cards
   SET retreat = json_extract(raw, '$.retreat')
 WHERE raw IS NOT NULL
   AND json_extract(raw, '$.retreat') IS NOT NULL;
