-- 016_card_translations.sql
--
-- Japanese language support for OPTCG (mirrors PTCG's *UI* but NOT its data
-- model — see opbindr/docs/superpowers/plans/2026-06-15-optcg-japanese-language.md
-- and optcg-api/docs/superpowers/specs/2026-04-23-multilang-card-db-design.md).
--
-- One Piece is ONE catalog with translated display, unlike Pokémon's two
-- independent per-language catalogs. So we do NOT denormalize a row per
-- (card, lang) like ptcg_cards. Instead, language-neutral fields (id, set,
-- rarity, colors, types, cost/power/counter, price) stay on the single
-- `cards` row, and only the fields that vary by language move into this
-- side-table: name, image_url, effect, trigger_text.
--
--   * name_en holds the canonical English name on JA rows so a latin-script
--     search ("luffy") matches a Japanese-named row. NULL on EN rows (name
--     is already English there) — same rationale as ptcg_cards.name_en (011).
--   * EN data is backfilled from the existing cards columns (below); the old
--     cards.name/image_url/effect/trigger_text columns are NOT dropped here so
--     existing readers keep working. Drop them in a later migration once the
--     ?lang= API read-layer is confirmed stable.
--
-- Re-runnability: the CREATE TABLE + index + backfill INSERT are idempotent
-- (IF NOT EXISTS / ON CONFLICT DO NOTHING). The three ALTER TABLE ADD COLUMN
-- statements are run-once — SQLite has no ADD COLUMN IF NOT EXISTS, and a
-- second run errors "duplicate column name" (same convention as 003/004/011).
-- Apply once; if re-applying the file, strip the ALTERs or split them out.

CREATE TABLE IF NOT EXISTS card_translations (
  card_id      TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  language     TEXT NOT NULL CHECK (language IN ('en', 'ja')),
  name         TEXT NOT NULL,
  name_en      TEXT,           -- canonical English alias for cross-script search; NULL on EN rows
  image_url    TEXT,
  effect       TEXT,
  trigger_text TEXT,
  PRIMARY KEY (card_id, language)
);

CREATE INDEX IF NOT EXISTS idx_card_translations_language ON card_translations (language);
CREATE INDEX IF NOT EXISTS idx_card_translations_name     ON card_translations (language, name);

-- Backfill the English translation from the existing single-language rows.
-- ON CONFLICT DO NOTHING keeps re-runs idempotent and never clobbers a row
-- a later EN re-import may have refreshed.
INSERT INTO card_translations (card_id, language, name, name_en, image_url, effect, trigger_text)
SELECT id, 'en', name, NULL, image_url, effect, trigger_text
FROM cards
WHERE name IS NOT NULL
ON CONFLICT (card_id, language) DO NOTHING;

-- Per-language Japanese price (real JA market value from Yuyutei `opc`, etc.).
-- Kept on `cards` (not card_translations) so the existing pricing cascade and
-- card_price_history stay untouched and the EN price stays exactly where it is.
-- Showing the EN/USD price on a JA card would be a plausible-but-wrong price,
-- so JA display reads price_ja and shows nothing when it is NULL.
-- SQLite requires one ADD COLUMN per statement.
ALTER TABLE cards ADD COLUMN price_ja REAL;
ALTER TABLE cards ADD COLUMN price_source_ja TEXT;
ALTER TABLE cards ADD COLUMN price_updated_at_ja INTEGER;
