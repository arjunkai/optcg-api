-- 018_characters_enrich.sql — additive columns for the Wikidata/Fandom character model.
-- The 017 characters table was shaped for api-onepiece (crew_id/fruit/bounty/job/status, now unused).
-- These columns hold the new model: bilingual name + provenance + (future) epithet/affiliation.
ALTER TABLE characters ADD COLUMN name_ja TEXT;
ALTER TABLE characters ADD COLUMN source TEXT;
ALTER TABLE characters ADD COLUMN wikidata_qid TEXT;
ALTER TABLE characters ADD COLUMN fandom_title TEXT;
ALTER TABLE characters ADD COLUMN epithet TEXT;
ALTER TABLE characters ADD COLUMN affiliation TEXT;
