-- 011_ptcg_name_en.sql
--
-- Cross-language name alias for ptcg_cards. JA rows store name in
-- Japanese (e.g. "ヒトカゲ"), so a latin-script search like "charmander"
-- never matches them. Add name_en holding the canonical English name
-- for JA rows so /pokemon/cards/index can return it and the frontend
-- search index can index against it.
--
-- Population path:
--   * import-d1.js sets name_en on JA rows at ingest via the two-tier
--     lookup that scripts/enrich_ja_card_names.py already built
--     (TCGdex /v2/en/cards/{id} → PokeAPI species fallback).
--   * One-time backfill from data/ja_card_id_to_en_name.json takes care
--     of existing rows (run via wrangler d1 execute after this lands).
--
-- EN rows leave name_en NULL — `name` is already the English name there,
-- so duplicating it would just bloat the index endpoint payload.

ALTER TABLE ptcg_cards ADD COLUMN name_en TEXT;

CREATE INDEX IF NOT EXISTS ptcg_cards_name_en ON ptcg_cards (name_en) WHERE name_en IS NOT NULL;
