-- 015_ptcg_campaign.sql
--
-- Promo campaign / distribution metadata for ptcg_cards. JA-side promos
-- ship in waves attached to a specific real-world event or partner
-- (Munch museum collab, McDonald's Happy Meal year, Pokemon Center DX,
-- movie commemoration, Champions League, Ichiban Kuji prize, etc.).
-- Without these columns, "show me every Munch promo" or "every 2024
-- McDonald's card" is unanswerable from D1 even though the rows exist.
--
-- Population path:
--   * scripts/enrich_ja_promo_campaigns.py walks a curated set of
--     Bulbapedia categories (e.g. Cards_with_The_Scream for Munch,
--     McDonald's_Collection_YYYY_cards for fast-food promos) and maps
--     each member back to our (set_id, local_id) via the title regex
--     "(<SET>-P Promo <N>)" or "(<SET> Promo <N>)". UPDATEs run with
--     a normalized join: UPPER(set_id) + CAST(local_id AS INTEGER) so
--     the lowercase-vs-uppercase set_id collision pattern that bit the
--     2026-05-16 dedupe can't recur here.
--   * EN rows leave both columns NULL — they aren't part of these JA-
--     campaign waves, and stamping a Bulbapedia campaign onto an EN
--     reprint would over-claim.
--
-- distribution_method is a coarse classifier kept small on purpose so
-- future filter UIs ("show me all PC-DX promos") have predictable
-- buckets. Today's vocab (extend in the script's DISTRIBUTION_METHODS
-- constant): art_museum_collaboration, fast_food, theatrical_release,
-- magazine_insert, pokemon_center, retail_giveaway, championship_event,
-- kuji_prize, anniversary.

ALTER TABLE ptcg_cards ADD COLUMN campaign TEXT;
ALTER TABLE ptcg_cards ADD COLUMN distribution_method TEXT;

CREATE INDEX IF NOT EXISTS ptcg_cards_campaign ON ptcg_cards (campaign) WHERE campaign IS NOT NULL;
CREATE INDEX IF NOT EXISTS ptcg_cards_distribution_method ON ptcg_cards (distribution_method) WHERE distribution_method IS NOT NULL;
