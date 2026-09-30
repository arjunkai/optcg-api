-- 020_opcanvs_artwork_products.sql
--
-- Records schema that reached production by hand without a migration: the
-- artwork source/collection columns, the products merch table and the
-- set_characters join (OPCanvs data loads, 2026-07..09), plus two older
-- columns, cards.finish and sets.type. (002 is also written in Postgres
-- syntax and was applied by hand; it's left as is for history.) Verified against
-- prod sqlite_master on 2026-09-29.
--
-- ALREADY APPLIED TO PROD: do not run this against optcg-cards --remote
-- (the ALTERs would fail on the existing columns). It exists so a fresh
-- database built from the migrations matches prod.

-- Card finish (foil etc.) and set type (booster / starter / ...).
ALTER TABLE cards ADD COLUMN finish TEXT;
ALTER TABLE sets ADD COLUMN type TEXT;

-- Non-card artwork rows (optc-artworks illustrations, Bandai box/playmat art)
-- served by /artwork/gallery and /characters/:id.
ALTER TABLE artwork ADD COLUMN source_url TEXT;
ALTER TABLE artwork ADD COLUMN kind TEXT;
ALTER TABLE artwork ADD COLUMN artist TEXT;
ALTER TABLE artwork ADD COLUMN collection TEXT;

-- Seeded by scripts/opcanvs/products_merch.sql; served by /products.
CREATE TABLE IF NOT EXISTS products (
  slug TEXT PRIMARY KEY,
  type TEXT NOT NULL,          -- sleeve | playmat | collection | set
  title TEXT NOT NULL,
  price TEXT,
  release TEXT,
  image_url TEXT,
  product_url TEXT
);

-- Seeded by scripts/opcanvs/set_characters.sql; drives /characters/:id products.
CREATE TABLE IF NOT EXISTS set_characters (
  set_id TEXT NOT NULL,
  character_id INTEGER NOT NULL,
  PRIMARY KEY (set_id, character_id)
);
