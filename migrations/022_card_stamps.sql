-- 022: stamp tags on cards.
--
-- print_label: the TCGPlayer product label of the print a row is priced
--   from, verbatim ("CS 25-26 Celebration Pack", "2025 New Year Event
--   Winner"). NULL for plain set prints and in-set treatments (Alternate Art,
--   Manga, SP...). Written by import-prices-d1.js from build_all_prices.py.
-- stamp: a small vocabulary derived from print_label (championship, winner,
--   finalist, top_players, treasure_cup, regional, store, judge, tournament,
--   anniversary, pre_release, release_event, collection, other), for the
--   ?stamp= filter and client badges. See docs/stamped-prints-plan.md.
--
-- Additive and nullable: safe for code that predates it. Apply with
--   npx wrangler d1 execute optcg-cards --remote --file=migrations/022_card_stamps.sql
ALTER TABLE cards ADD COLUMN print_label TEXT;
ALTER TABLE cards ADD COLUMN stamp TEXT;
CREATE INDEX idx_cards_stamp ON cards(stamp);
